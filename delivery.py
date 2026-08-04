"""音频下载与投递 —— 移植自 utils/send.js

AstrBot 的消息组件（Record / File / Image）已屏蔽适配器差异，因此这里比原 send.js 简洁得多。
- 下载：用 aiohttp 流式下载到临时文件，按音质/URL 推断扩展名。
- 投递：语音用 Record.fromFileSystem，群/好友文件用 File.fromFileSystem。
- 原生/自定义音乐卡：尽力而为，仅在 AIOCQHTTP 平台尝试 OneBot sendApi。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

import aiohttp

from .quality import QUALITY_LABEL
from . import api as qqapi

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


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


def build_music_filename(*, singer: str, title: str, quality: str = "", ext: str = "", ascii_only: bool = False, include_quality: bool = False) -> str:
    s = _clean_track_text(singer, 30)
    t = _clean_track_text(title, 40)
    if ascii_only:
        ascii_only_name = re.sub(r"[^\x20-\x7E]", "", f"{s}-{t}").strip().replace("-", "").strip()
        if not ascii_only_name:
            # 退一步：尝试仅标题 ASCII
            ascii_only_name = re.sub(r"[^\x20-\x7E]", "", t).strip()
        if not ascii_only_name:
            return f"QQMusic_{time.time_ns():x}{ext}"
        base = ascii_only_name
    else:
        base = f"{s}-{t}" if (s and t) else (s or t or "QQMusic")
    if include_quality and quality:
        base = f"{base}_{quality}"
    return f"{base}{ext}"


def _ext_for_quality(quality_hint: str, url: str) -> str:
    q = (quality_hint or "").lower()
    if q in ("flac", "hires", "master", "atmos", "atmos_master", "ape"):
        return ".flac" if q != "ape" else ".ape"
    if q in ("m4a",):
        return ".m4a"
    u = (url or "").lower()
    if re.search(r"f000|rs01|rs02|q000", u):
        return ".flac"
    if re.search(r"c400|m4a", u):
        return ".m4a"
    if re.search(r"m800|m500|mp3", u):
        return ".mp3"
    return ".mp3"


async def download_audio(url: str, save_dir: str, filename: str = "qqmusic", timeout_ms: int = 90000, quality_hint: str = "") -> dict:
    """下载音频到本地，返回 {filePath, size}"""
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
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url, headers=headers, allow_redirects=True) as res:
            if res.status >= 400:
                raise RuntimeError(f"下载失败 HTTP {res.status}")
            data = await res.read()
            if len(data) < 256:
                raise RuntimeError("下载内容过小，可能是无效链接")
            head = data[:32].decode("utf-8", errors="ignore").lower()
            if "<html" in head or "<!doctype" in head:
                raise RuntimeError("下载内容为 HTML，音频链接已失效")
            with open(file_path, "wb") as f:
                f.write(data)
    return {"filePath": file_path, "size": len(data)}


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
    """OneBot 原生音乐卡片：{type:'music', data:{type, id}}。尽力而为，仅 AIOCQHTTP。"""
    try:
        from astrbot.api.message_components import Plain
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
            await send_api(action, {"group_id" if is_group else "user_id": int(sid), "message": msg})
            return True
        except Exception:
            try:
                await send_api("send_msg", {"message": msg})
                return True
            except Exception:
                return False
    except Exception:
        return False


async def send_custom_music_card(event, *, url: str, audio: str, title: str, image: str = "", content: str = "") -> bool:
    """OneBot 自定义音乐卡片。尽力而为。"""
    try:
        bot = event.platform
        send_api = getattr(bot, "send_api", None) or getattr(bot, "sendApi", None)
        if send_api is None:
            return False
        data = {"type": "custom", "url": url or audio, "audio": audio or url, "title": title or "QQ音乐", "image": image or ""}
        if content:
            data["content"] = content
        msg = [{"type": "music", "data": data}]
        is_group = bool(getattr(event.message_obj, "group_id", None))
        sid = event.message_obj.group_id if is_group else event.get_sender_id()
        action = "send_group_msg" if is_group else "send_private_msg"
        try:
            await send_api(action, {"group_id" if is_group else "user_id": int(sid), "message": msg})
            return True
        except Exception:
            return False
    except Exception:
        return False


async def deliver_song(plugin, event, song: dict, play: dict, *, cfg: dict, plugin_dir: str, options: dict | None = None) -> dict:
    """综合发送：可选文案/音乐卡 → 下载 → 语音 → 文件"""
    options = options or {}
    title = song.get("songName") or "未知歌曲"
    singer = song.get("singerName") or "未知歌手"
    cover = song.get("cover") or (f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{song['albummid']}.jpg" if song.get("albummid") else "https://y.gtimg.cn/mediastyle/global/img/album_300.png")
    page_url = f"https://y.qq.com/n/ryqq/songDetail/{song['songmid']}" if song.get("songmid") else "https://y.qq.com/"

    quality_label = play.get("qualityLabel") or QUALITY_LABEL.get(play.get("quality", "")) or play.get("quality") or cfg.get("quality") or ""

    skip_text = options.get("skipTextInfo", False)
    skip_native = options.get("skipNativeCard", False)
    skip_custom = options.get("skipCustomCard", False)

    allow_native = (not skip_native) and cfg.get("sendNativeCard")
    allow_custom = (not skip_custom) and cfg.get("sendCustomCard")

    if not skip_text and cfg.get("sendTextInfo", True):
        lines = [
            f"{cfg.get('identifyPrefix') or ''}QQ音乐",
            f"♪ {title} - {singer}",
            f"专辑：{song['albumName']}" if song.get("albumName") else "",
            f"音质：{quality_label}" if quality_label else "",
            "" if play.get("url") else "⚠ 未获取到播放链，请 #qqm登录",
        ]
        await event.send(plugin._plain("\n".join(x for x in lines if x)))

    if allow_native and song.get("songid"):
        await send_native_music_card(event, "qq", song["songid"])

    if allow_custom and play.get("url"):
        await send_custom_music_card(event, url=page_url, audio=play["url"], title=title, image=cover, content=singer)

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
                dl = await download_audio(u, save_dir, "qqmusic", timeout, play.get("quality") or cfg.get("quality") or "")
                break
            except Exception as err:
                last_err = err
                # 刷新播放链重试一次
                if song.get("songmid") and i == 0:
                    try:
                        prefer = "flac" if re.search(r"RS01|RS02|Q000|master|atmos|hires", str(play.get("quality", "")) + str(play.get("url", "")), re.I) else (play.get("quality") or cfg.get("quality") or "flac")
                        fresh = await qqapi.song_url_best(song["songmid"], quality=prefer, media_id=song.get("media_mid") or play.get("mediaId") or song.get("songmid"), fallback=True)
                        if fresh.get("url") and fresh["url"] != try_urls[0]:
                            try_urls.append(fresh["url"])
                            play.update(fresh)
                    except Exception:
                        pass
        if not dl:
            raise last_err or RuntimeError("下载失败")
        local_path = dl["filePath"]
    except Exception as err:
        await event.send(plugin._plain(f"下载音频失败：{err}\n可尝试 #qqm登录 后重发，或换一首歌"))
        return {"ok": False, "reason": "download_fail", "error": str(err)}

    import astrbot.api.message_components as Comp
    from astrbot.api.event import MessageChain
    keep_sec = int(cfg.get("keepFileSec", 60))

    if cfg.get("sendVocal"):
        try:
            await event.send(Comp.Record.fromFileSystem(local_path))
        except Exception as e:
            plugin._log_warn(f"语音发送失败: {e}")
        _schedule_cleanup(local_path, keep_sec)

    if cfg.get("uploadFile"):
        ext = os.path.splitext(local_path)[1] or ".mp3"
        display = build_music_filename(singer=singer, title=title, quality=play.get("quality") or cfg.get("quality") or "", ext=ext)
        try:
            await event.send(Comp.File.fromFileSystem(local_path, display))
        except Exception as e:
            plugin._log_warn(f"文件发送失败: {e}")
        # 文件发送后稍后清理（给协议上传留时间）
        if not cfg.get("sendVocal"):
            _schedule_cleanup(local_path, keep_sec)

    return {"ok": True, "downloaded": True}
