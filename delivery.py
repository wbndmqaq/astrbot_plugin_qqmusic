from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

import aiohttp
from astrbot.api.message_components import File, Record

from . import api as qqapi
from .quality import QUALITY_LABEL

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# 被动回复受限平台：QQ 官方机器人
# 需合并消息省被动回复额度，且无 OneBot send_api（原生/自定义音乐卡 no-op）
# 注意：weixin_oc 是微信个人号平台，具备完整主动/被动消息能力，不属于受限平台
_PASSIVE_LIMITED = ("qq_official",)


def _is_passive_limited(event) -> bool:
    try:
        name = str(event.get_platform_name() or "")
        return any(k in name for k in _PASSIVE_LIMITED)
    except Exception:
        return False


# 语音(Record)出站不支持的平台：weixin_oc 个人微信仅支持 Plain/Image/Video/File，
# Record 会被适配器丢弃并抛"unsupported outbound segment"——发送前直接跳过
_NO_VOCAL_PLATFORMS = ("weixin_oc",)


def _no_vocal(event) -> bool:
    try:
        name = str(event.get_platform_name() or "")
        return any(k in name for k in _NO_VOCAL_PLATFORMS)
    except Exception:
        return False


def get_temp_dir(cfg: dict, plugin_dir: str) -> str:
    d = str(cfg.get("tempDir") or "temp/qqmusic")
    p = Path(d)
    if not p.is_absolute():
        p = Path(plugin_dir) / p
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _clean_track_text(s: str, max_len: int = 40) -> str:
    if not s:
        return ""
    s = str(s)
    s = s.replace("【", "(").replace("】", ")").replace("《", "(").replace("》", ")")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len]
    return re.sub(r'[\\/:*?"<>|]', "", s).strip()


def build_music_filename(*, singer: str, title: str, ext: str = "") -> str:
    s = _clean_track_text(singer, 30)
    t = _clean_track_text(title, 40)
    base = f"{s}-{t}" if (s and t) else (s or t or "QQMusic")
    return f"{base}{ext}"


def _ext_for_quality(quality_hint: str, url: str) -> str:
    q = (quality_hint or "").lower()
    if q == "video":
        return ".mp4"
    if q in ("flac", "hires", "master", "atmos", "atmos_master", "ape"):
        return ".flac" if q != "ape" else ".ape"
    if q in ("m4a",):
        return ".m4a"
    u = (url or "").lower()
    # 与 JS send.js 对齐：显式扩展名优先，再按前缀推断
    for ext in (".ape", ".ogg", ".flac", ".m4a", ".mp3"):
        if ext in u:
            return ext
    if re.search(r"f000|rs01|rs02|q000", u):
        return ".flac"
    if re.search(r"a000|ape", u):
        return ".ape"
    if re.search(r"c400|m4a", u):
        return ".m4a"
    if re.search(r"m800|m500|mp3", u):
        return ".mp3"
    return ".mp3"


async def download_audio(
    url: str,
    save_dir: str,
    filename: str = "qqmusic",
    timeout_ms: int = 90000,
    quality_hint: str = "",
) -> dict:
    headers = {
        "User-Agent": UA,
        "Referer": "https://y.qq.com/",
        "Origin": "https://y.qq.com",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    ext = _ext_for_quality(quality_hint, url)
    safe_name = re.sub(r"[^\w.-]", "", filename) or "qqmusic"
    file_path = os.path.join(save_dir, f"{safe_name}_{int(time.time() * 1000)}{ext}")

    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with (
        aiohttp.ClientSession(timeout=timeout) as sess,
        sess.get(url, headers=headers, allow_redirects=True) as res,
    ):
        if res.status >= 400:
            raise RuntimeError(f"下载失败 HTTP {res.status}")
        data = await res.read()
        if len(data) < 256:
            raise RuntimeError("下载内容过小，可能是无效链接")
        head = data[:32].decode("utf-8", errors="ignore").lower()
        if "<html" in head or "<!doctype" in head:
            raise RuntimeError("下载内容为 HTML，音频链接已失效")
        # 音频文件可能较大，写盘放线程池避免阻塞事件循环
        await asyncio.to_thread(_write_bytes, file_path, data)
    return {"filePath": file_path, "size": len(data)}


def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)


def _schedule_cleanup(file_path: str, keep_sec: int):
    delay = max(0, keep_sec)
    loop = asyncio.get_event_loop()

    def _rm():
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

    loop.call_later(delay, _rm)


async def send_native_music_card(event, platform_type: str, music_id: str) -> bool:
    try:
        bot = event.platform
        # 仅 aiocqhttp 平台具备 send_api 风格接口
        send_api = getattr(bot, "send_api", None) or getattr(bot, "sendApi", None)
        if send_api is None:
            return False
        # 兼容不同封装：尝试调用
        msg = [{"type": "music", "data": {"type": platform_type, "id": str(music_id)}}]
        is_group = bool(getattr(event.message_obj, "group_id", None))
        action = "send_group_msg" if is_group else "send_private_msg"
        sid = event.message_obj.group_id if is_group else event.get_sender_id()
        try:
            await send_api(
                action,
                {"group_id" if is_group else "user_id": int(sid), "message": msg},
            )
            return True
        except Exception:
            try:
                await send_api("send_msg", {"message": msg})
                return True
            except Exception:
                return False
    except Exception:
        return False


async def send_custom_music_card(
    event, *, url: str, audio: str, title: str, image: str = "", content: str = ""
) -> bool:
    try:
        bot = event.platform
        send_api = getattr(bot, "send_api", None) or getattr(bot, "sendApi", None)
        if send_api is None:
            return False
        data = {
            "type": "custom",
            "url": url or audio,
            "audio": audio or url,
            "title": title or "QQ音乐",
            "image": image or "",
        }
        if content:
            data["content"] = content
        msg = [{"type": "music", "data": data}]
        is_group = bool(getattr(event.message_obj, "group_id", None))
        sid = event.message_obj.group_id if is_group else event.get_sender_id()
        action = "send_group_msg" if is_group else "send_private_msg"
        try:
            await send_api(
                action,
                {"group_id" if is_group else "user_id": int(sid), "message": msg},
            )
            return True
        except Exception:
            return False
    except Exception:
        return False


async def deliver_song(
    plugin,
    event,
    song: dict,
    play: dict,
    *,
    cfg: dict,
    plugin_dir: str,
    options: dict | None = None,
) -> dict:
    options = options or {}
    title = song.get("songName") or "未知歌曲"
    singer = song.get("singerName") or "未知歌手"
    cover = song.get("cover") or (
        f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{song['albummid']}.jpg"
        if song.get("albummid")
        else "https://y.gtimg.cn/mediastyle/global/img/album_300.png"
    )
    page_url = (
        f"https://y.qq.com/n/ryqq/songDetail/{song['songmid']}"
        if song.get("songmid")
        else "https://y.qq.com/"
    )

    quality_label = (
        play.get("qualityLabel")
        or QUALITY_LABEL.get(play.get("quality", ""))
        or play.get("quality")
        or cfg.get("quality")
        or ""
    )

    skip_text = options.get("skipTextInfo", False)
    skip_native = options.get("skipNativeCard", False)
    skip_custom = options.get("skipCustomCard", False)

    is_limited = (
        _is_passive_limited(event) and cfg.get("qqofficialAdapt", True) is not False
    )

    # 受限平台无 OneBot send_api，原生/自定义音乐卡本就是 no-op，显式跳过避免误导
    allow_native = (not skip_native) and cfg.get("sendNativeCard") and not is_limited
    allow_custom = (not skip_custom) and cfg.get("sendCustomCard") and not is_limited

    # 文案：普通平台直接发；受限平台（QQ 官方/公众号）延后，与首个媒体合并省被动回复额度
    pending_text = ""
    if not skip_text and cfg.get("sendTextInfo", True):
        lines = [
            f"{cfg.get('identifyPrefix') or ''}QQ音乐",
            f"♪ {title} - {singer}",
            f"专辑：{song['albumName']}" if song.get("albumName") else "",
            f"音质：{quality_label}" if quality_label else "",
            "" if play.get("url") else "⚠ 未获取到播放链，请 #qqm登录",
        ]
        pending_text = "\n".join(x for x in lines if x)
        if not is_limited:
            await plugin._send_chain(event, plugin._plain(pending_text))
            pending_text = ""

    if allow_native and song.get("songid"):
        await send_native_music_card(event, "qq", song["songid"])

    if allow_custom and play.get("url"):
        await send_custom_music_card(
            event,
            url=page_url,
            audio=play["url"],
            title=title,
            image=cover,
            content=singer,
        )

    if not play.get("url"):
        return {"ok": False, "reason": "no_url"}

    need_download = cfg.get("sendVocal") or cfg.get("uploadFile")
    if not need_download:
        return {"ok": True, "downloaded": False}

    local_path = ""
    try:
        save_dir = get_temp_dir(cfg, plugin_dir)
        timeout = int(cfg.get("downloadTimeout") or 120000)
        try_urls = [play["url"]]
        dl = None
        last_err = None
        for i, u in enumerate(try_urls):
            try:
                dl = await download_audio(
                    u,
                    save_dir,
                    "qqmusic",
                    timeout,
                    play.get("quality") or cfg.get("quality") or "",
                )
                break
            except Exception as err:
                last_err = err
                # 刷新播放链重试一次
                if song.get("songmid") and i == 0:
                    try:
                        prefer = (
                            "flac"
                            if re.search(
                                r"RS01|RS02|Q000|master|atmos|hires",
                                str(play.get("quality", "")) + str(play.get("url", "")),
                                re.IGNORECASE,
                            )
                            else (play.get("quality") or cfg.get("quality") or "flac")
                        )
                        fresh = await qqapi.song_url_best(
                            song["songmid"],
                            quality=prefer,
                            media_id=song.get("media_mid")
                            or play.get("mediaId")
                            or song.get("songmid"),
                            fallback=True,
                        )
                        if fresh.get("url") and fresh["url"] != try_urls[0]:
                            try_urls.append(fresh["url"])
                            play.update(fresh)
                    except Exception:
                        pass
        if not dl:
            raise last_err or RuntimeError("下载失败")
        local_path = dl["filePath"]
    except Exception as err:
        await plugin._send_chain(
            event,
            plugin._plain(f"下载音频失败：{err}\n可尝试 #qqm登录 后重发，或换一首歌"),
        )
        return {"ok": False, "reason": "download_fail", "error": str(err)}

    keep_sec = int(cfg.get("keepFileSec", 60))
    want_vocal = bool(cfg.get("sendVocal"))
    want_file = bool(cfg.get("uploadFile"))
    ext = os.path.splitext(local_path)[1] or ".mp3"
    file_display = build_music_filename(singer=singer, title=title, ext=ext)

    async def _send_media(media_comp):
        comps = (
            [plugin._plain(pending_text), media_comp] if pending_text else [media_comp]
        )
        await plugin._send_chain(event, *comps)

    # 语音/文件分别尝试发送，互不阻塞（语音失败不影响文件）
    # 受限平台（QQ 官方）下语音由适配器转码，文案并入首条媒体省被动回复额度
    limited_tag = "（受限平台）" if is_limited else ""
    no_vocal = _no_vocal(event)
    if want_vocal and no_vocal:
        plugin._log_info("当前平台不支持语音，跳过语音只发文件")
    elif want_vocal:
        try:
            await _send_media(Record.fromFileSystem(local_path))
            pending_text = ""
        except Exception as e:
            plugin._log_warn(f"语音发送失败{limited_tag}: {e}")
    if want_file:
        try:
            await _send_media(File(file_display, file=local_path))
            pending_text = ""
        except Exception as e:
            plugin._log_warn(f"文件发送失败{limited_tag}: {e}")
    # 临时文件清理：语音已发 / 平台无语音只发文件 / 仅文件时都需调度
    # （避免 weixin_oc 等 no_vocal 平台在 want_vocal 下语音被跳过、文件又不清理导致泄漏）
    if (want_vocal and not no_vocal) or want_file:
        _schedule_cleanup(local_path, keep_sec)

    # 兜底：所有媒体发送均失败时，至少把文案发出去
    if pending_text:
        await plugin._send_chain(event, plugin._plain(pending_text))

    return {"ok": True, "downloaded": True}


async def deliver_video(
    plugin, event, mv: dict, url: str, *, cfg: dict, plugin_dir: str, download: bool = False
) -> dict:
    """发送 MV 视频：下载 时先落盘再发文件；播放 时优先直发 URL，失败再落盘；最终回退 URL 文本。

    返回 {"ok": bool, "reason": str, "url": str}
    """
    from astrbot.api.message_components import Video

    title = mv.get("mvtitle") or mv.get("name") or mv.get("songName") or "MV"
    local_path = ""
    keep_sec = int(cfg.get("keepFileSec", 120))

    async def _download() -> str:
        save_dir = get_temp_dir(cfg, plugin_dir)
        timeout = int(cfg.get("downloadTimeout") or 120000)
        dl = await download_audio(
            url, save_dir, "mv_" + _clean_track_text(title, 30), timeout, "video"
        )
        return dl["filePath"]

    if download:
        try:
            local_path = await _download()
        except Exception as err:
            plugin._log_warn(f"MV 下载失败: {err}")

    if not local_path and not download:
        # 播放：直发 URL
        try:
            await plugin._send_chain(event, Video.fromURL(url))
            return {"ok": True, "reason": "url", "url": url}
        except Exception as err:
            plugin._log_warn(f"MV 直发 URL 失败，尝试落盘: {err}")
            try:
                local_path = await _download()
            except Exception as err2:
                plugin._log_warn(f"MV 落盘失败: {err2}")

    if local_path:
        try:
            await plugin._send_chain(event, Video.fromFileSystem(local_path))
            _schedule_cleanup(local_path, keep_sec)
            return {"ok": True, "reason": "file", "url": url}
        except Exception as err:
            plugin._log_warn(f"MV 文件发送失败: {err}")
            _schedule_cleanup(local_path, 10)

    return {"ok": False, "reason": "send_fail", "url": url}
