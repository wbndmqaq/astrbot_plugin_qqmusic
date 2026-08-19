from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import aiohttp
from astrbot.api.message_components import File, Record

from . import api as qqapi
from .quality import QUALITY_LABEL

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


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


# OneBot 系平台（aiocqhttp → QQ 个人号，底层 NapCat/LLOneBot/Lagrange 等 NTQQ 协议端）。
# 与 Yunzai 版 OneBot 行为对齐：语音先压成紧凑 mp3、群文件优先传压缩版（NTQQ 拒 .flac）
_ONEBOT_PLATFORMS = ("aiocqhttp",)


def _is_onebot(event) -> bool:
    try:
        name = str(event.get_platform_name() or "")
        return any(k in name for k in _ONEBOT_PLATFORMS)
    except Exception:
        return False


def _is_aiocqhttp(event) -> bool:
    return "aiocqhttp" in str(event.get_platform_name() or "")


def _file_to_base64(path: str) -> str:
    import base64

    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _bytes_to_base64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


async def _aiocq_call_action(event, action: str, sid: int, segs: list) -> None:
    """aiocqhttp 直连 OneBot call_action 发送原始消息段。

    bot 实例来自 event.bot（CQHttp）；napcat 与 AstrBot 跨容器时 file:// 路径
    不可见（realpath ENOENT），且 File/Record 组件对 base64:// 有各自的坑
    （File 擦空不存在路径、Record 强制转 WAV），因此走底层 action 直发 base64。
    """
    bot = getattr(event, "bot", None)
    if bot is None:
        bot = getattr(getattr(event, "platform", None), "bot", None)
    if bot is None:
        raise RuntimeError("无法获取 aiocqhttp bot 实例")
    is_group = action == "send_group_msg"
    if is_group:
        await bot.call_action(action, group_id=int(sid), message=segs)
    else:
        await bot.call_action(action, user_id=int(sid), message=segs)


async def _aiocq_send_file(event, text: str, display: str, path: str) -> None:
    """aiocqhttp 发送文件：base64 内联直发，不依赖 napcat 与 AstrBot 共享文件系统。"""
    b64 = await asyncio.to_thread(_file_to_base64, path)
    segs: list = []
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    segs.append({"type": "file", "data": {"file": f"base64://{b64}", "name": display}})
    is_group = bool(getattr(event.message_obj, "group_id", None))
    sid = event.message_obj.group_id if is_group else event.get_sender_id()
    await _aiocq_call_action(event, "send_group_msg" if is_group else "send_private_msg", int(sid), segs)


async def _aiocq_send_silk_record(event, text: str, src_path: str) -> tuple[bool, str]:
    """aiocqhttp 语音：→24kHz 单声道 wav→pysilk 编成标准 silk，base64 直发。

    Record 组件会把音频转 WAV 再 base64（载荷 ~50MB+，napcat 转码数分钟→WS 超时）；
    silk 仅几 MB，napcat 无需转码直接上传。任何一步失败返回 (False, 原因)，由调用方
    退回标准 Record 组件发送。OneBot/napcat 用标准 silk（#!SILK_V3），不可用
    tencent=True（0x02 前缀是 QQ 官方专用，napcat 解析会只剩 1 秒）。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False, "no_ffmpeg"
    try:
        import pysilk
    except Exception:
        return False, "no_pysilk"
    import wave as _wave
    from io import BytesIO as _BytesIO

    tmp = os.path.join(os.path.dirname(src_path), f"silk_{int(time.time() * 1000)}")
    wav_path = tmp + ".wav"
    silk_path = tmp + ".silk"
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", src_path, "-ar", "24000", "-ac", "1", "-f", "wav", wav_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await proc.wait()
        if rc != 0 or not os.path.exists(wav_path):
            return False, "ffmpeg_wav_fail"
        with _wave.open(wav_path, "rb") as wav:
            rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
        out = _BytesIO()
        pysilk.encode(_BytesIO(pcm), out, rate, rate, tencent=False)
        silk_bytes = out.getvalue()
        segs: list = []
        if text:
            segs.append({"type": "text", "data": {"text": text}})
        # silk 直接来自内存，无需先落盘再读
        segs.append(
            {"type": "record", "data": {"file": f"base64://{_bytes_to_base64(silk_bytes)}"}}
        )
        is_group = bool(getattr(event.message_obj, "group_id", None))
        sid = event.message_obj.group_id if is_group else event.get_sender_id()
        await _aiocq_call_action(event, "send_group_msg" if is_group else "send_private_msg", int(sid), segs)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        for p in (wav_path, silk_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# ──────────── QQ 官方分片文件上传（AstrBot ≥4.27.3）────────────

# 对齐 AstrBot 4.27.3 `qqofficial_chunked_upload.py` 的分片阈值：
# qq_official 适配器对 >10MB 的本地 File 组件自动走 QQ 多媒体分片上传
_QQ_CHUNKED_UPLOAD_THRESHOLD = 10 * 1024 * 1024  # 10MB


def _qq_chunked_available(cfg: dict) -> bool:
    """qq_official 大文件分片上传是否可用。

    - 配置 `qqofficialChunkedUpload` 关闭时不可用
    - AstrBot ≥4.27.3（`astrbot.__version__`）起适配器支持；旧版无此能力
    """
    if cfg.get("qqofficialChunkedUpload", True) is False:
        return False
    try:
        from astrbot import __version__ as _ver

        parts = [int(x) for x in str(_ver).lstrip("v").split(".")]
        return parts[:3] >= [4, 27, 3]
    except Exception:
        # 版本不可读时保守视为不支持，交给调用方走压缩版兜底
        return False


# ──────────── 语音压缩（ffmpeg）────────────

# 语音直传白名单：体积不大时直接发，避免无谓转码（对齐 JS 版 send.js）
_VOCAL_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_VOCAL_DIRECT_EXT = {"mp3", "silk", "wav", "amr", "m4a", "ogg", "flac"}
# QQ 官方机器人：适配器内部转码为 silk，直传白名单收窄（silk/wav/mp3/flac）
_QQOFFICIAL_DIRECT_EXT = {"silk", "wav", "mp3", "flac"}

_ffmpeg_checked = False
_ffmpeg_ok = False


def _ffmpeg_available() -> bool:
    global _ffmpeg_checked, _ffmpeg_ok
    if not _ffmpeg_checked:
        _ffmpeg_checked = True
        _ffmpeg_ok = shutil.which("ffmpeg") is not None
    return _ffmpeg_ok


async def prepare_vocal_file(
    file_path: str,
    *,
    direct_ext: set[str] | None = None,
    max_bytes: int = _VOCAL_MAX_BYTES,
    low_quality: bool = False,
) -> str:
    """把高音质音频压成紧凑 mp3（语音发送用），返回可发送路径。

    - FLAC 等高音质文件直接作语音(Record)会被协议端以体积/格式拒绝，先 ffmpeg 压成 mp3
    - low_quality（禁用高清语音）：PC QQ 播放不了 44.1k 立体声语音时开启，强制重编码成
      mono 16k/32k（QQ 语音标准规格），输出 _vocal_low.mp3 防与高音质版本串台
    - 低音质仅用于语音；群文件兜底仍保高音质（调用方决定）
    - ffmpeg 缺失 / 压缩失败时回退原文件，保证功能可用
    """
    if not file_path or not os.path.exists(file_path):
        return file_path
    abs_path = os.path.abspath(file_path)
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        return file_path
    ext = os.path.splitext(abs_path)[1].lstrip(".").lower()
    direct = direct_ext if direct_ext is not None else _VOCAL_DIRECT_EXT
    # 低音质模式强制重编码（即使源是小 mp3，也要转成 PC 兼容格式）
    if not low_quality and ext in direct and size <= max_bytes:
        return abs_path
    if not _ffmpeg_available():
        return file_path

    stem = os.path.splitext(os.path.basename(abs_path))[0]
    out = os.path.join(
        os.path.dirname(abs_path),
        f"{stem}_vocal_low.mp3" if low_quality else f"{stem}_vocal.mp3",
    )
    if os.path.exists(out):
        try:
            if os.path.getsize(out) > 256:
                return out
        except OSError:
            pass

    bitrate = "64k" if size > 16 * 1024 * 1024 else "96k" if size > 8 * 1024 * 1024 else "128k"
    common = ["ffmpeg", "-y", "-i", abs_path, "-vn", "-acodec", "libmp3lame"]
    args = (
        common + ["-ar", "16000", "-ac", "1", "-b:a", "32k", out]
        if low_quality
        else common + ["-ar", "44100", "-ac", "2", "-b:a", bitrate, out]
    )
    try:
        # 编码是 CPU 密集操作，放线程池避免阻塞事件循环
        res = await asyncio.to_thread(
            subprocess.run,
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
        if res.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) <= 256:
            with contextlib.suppress(OSError):
                os.remove(out)
            return file_path
        return out
    except Exception:
        return file_path


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
    event,
    *,
    url: str,
    audio: str,
    title: str,
    image: str = "",
    content: str = "",
    singer: str = "",
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
        if singer:
            data["singer"] = singer
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
    is_aiocq = _is_aiocqhttp(event)

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
        native_ok = await send_native_music_card(event, "qq", song["songid"])
        # NTQQ 系协议端不支持 type:qq 原生卡，失败时若有直链降级为自定义卡（对齐 JS 版 send.js）
        if not native_ok and allow_custom and play.get("url"):
            await send_custom_music_card(
                event,
                url=page_url,
                audio=play["url"],
                title=title,
                image=cover,
                content=singer,
                singer=singer,
            )
    elif allow_custom and play.get("url"):
        await send_custom_music_card(
            event,
            url=page_url,
            audio=play["url"],
            title=title,
            image=cover,
            content=singer,
            singer=singer,
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

    # 语音 / OneBot 群文件都需要「紧凑 mp3」：FLAC 直接作语音会被协议端拒；
    # OneBot(NTQQ) 群文件对 .flac 也常报「未知文件类型或路径不存在」。
    # 其余平台群文件保留原始高音质文件；低音质只用于语音（禁用高清语音时 PC 可播）。
    # 无语音平台（weixin_oc 等）语音被跳过时不压缩，避免浪费编码
    is_onebot = _is_onebot(event)
    no_vocal = _no_vocal(event)
    need_compress = bool(local_path) and (
        (want_vocal and not no_vocal) or (want_file and is_onebot)
    )
    vocal_path = ""
    if need_compress:
        vocal_path = await prepare_vocal_file(
            local_path,
            direct_ext=(
                _QQOFFICIAL_DIRECT_EXT
                if _is_passive_limited(event)
                else _VOCAL_DIRECT_EXT
            ),
            low_quality=want_vocal and cfg.get("disableHighQualityVocal") is True,
        )
    has_compressed = bool(vocal_path and vocal_path != local_path)
    # 压缩产物同样纳入清理，避免 temp 目录累积
    if has_compressed:
        _schedule_cleanup(vocal_path, keep_sec)

    # 语音/文件分别尝试发送，互不阻塞（语音失败不影响文件）
    # 受限平台（QQ 官方）下语音由适配器转码，文案并入首条媒体省被动回复额度
    limited_tag = "（受限平台）" if is_limited else ""
    if want_vocal and no_vocal:
        plugin._log_info("当前平台不支持语音，跳过语音只发文件")
    elif want_vocal:
        voice_source = vocal_path or local_path
        silk_ok = False
        if is_aiocq:
            # aiocqhttp：silk 小载荷直发（napcat 免转码），失败退回标准 Record 组件
            ok, reason = await _aiocq_send_silk_record(event, pending_text, voice_source)
            silk_ok = ok
            if ok:
                pending_text = ""
            else:
                plugin._log_warn(f"aiocqhttp 语音 silk 直发失败（{reason}），退回 Record 组件")
        if not silk_ok:
            try:
                await _send_media(Record.fromFileSystem(voice_source))
                pending_text = ""
            except Exception as e:
                plugin._log_warn(f"语音发送失败{limited_tag}: {e}")
    if want_file:
        # OneBot：有压缩版优先传压缩 mp3（可靠）；其余平台 / 无压缩版保留原始文件
        # QQ 官方：大文件默认发原始文件走适配器分片上传（AstrBot ≥4.27.3，>10MB 自动分片）；
        # 分片不可用/关闭且是大文件时改传压缩 mp3 兜底（旧版大 FLAC 直发必败）
        comp_display = (
            file_display.rsplit(".", 1)[0] + "_压缩版.mp3"
            if "." in file_display
            else "QQ音乐_压缩版.mp3"
        )
        if is_aiocq:
            # aiocqhttp：napcat 与 AstrBot 跨容器不共享文件系统，file:// 路径不可见
            # (realpath ENOENT) → base64 内联直发；优先用压缩 mp3(vocal_path) 控制载荷
            try:
                await _aiocq_send_file(
                    event,
                    pending_text,
                    comp_display if has_compressed else file_display,
                    vocal_path or local_path,
                )
                pending_text = ""
            except Exception as e:
                plugin._log_warn(f"文件发送失败{limited_tag}: {e}")
        else:
            is_qqofficial = _is_passive_limited(event)
            qq_large_file = False
            if is_qqofficial:
                try:
                    qq_large_file = os.path.getsize(local_path) > _QQ_CHUNKED_UPLOAD_THRESHOLD
                except OSError:
                    qq_large_file = False
            qq_chunk = is_qqofficial and _qq_chunked_available(cfg) and qq_large_file
            # 走压缩 mp3 的两类情形：OneBot 群文件 / QQ 官方大文件但分片不可用
            use_compressed = has_compressed and (
                is_onebot
                or (is_qqofficial and qq_large_file and not qq_chunk)
            )
            if use_compressed:
                try:
                    await _send_media(File(comp_display, file=vocal_path))
                    pending_text = ""
                except Exception as e:
                    plugin._log_warn(f"文件发送失败{limited_tag}: {e}")
            else:
                if qq_chunk:
                    plugin._log_info(
                        f"QQ官方大文件 {os.path.basename(local_path)} 走适配器分片上传"
                    )
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
    plugin,
    event,
    mv: dict,
    url: str,
    *,
    cfg: dict,
    plugin_dir: str,
    download: bool = False,
    extra: list | None = None,
) -> dict:
    """发送 MV 视频（多适配器降级链）：

    - 受限平台（qq_official 被动回复受限）：跳过 URL 直发（官方 API 不支持外链视频直发），
      直接落盘发文件；extra（如 MV 详情卡图片）并入同一条消息省被动回复额度
    - 播放（其余平台）：Video.fromURL 直发 → 失败落盘 → 发下载文件（File，全平台支持）
    - 下载：落盘后直接发下载文件（File 主发送）
    - File 也失败：再试 Video.fromFileSystem（内嵌视频）
    - 全部失败：返回 filePath 供调用方兜底，最终回退 URL 文本

    返回 {"ok": bool, "reason": str, "url": str, "filePath": str}
    """
    from astrbot.api.message_components import File, Video

    title = mv.get("mvtitle") or mv.get("name") or mv.get("songName") or "MV"
    extra = list(extra or [])
    local_path = ""
    keep_sec = int(cfg.get("keepFileSec", 120))
    passive_limited = _is_passive_limited(event)
    file_name = _clean_track_text(title, 30) + ".mp4"

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

    if not local_path and not download and not passive_limited:
        # 播放：直发 URL（受限平台跳过，官方 API 需先上传文件）
        try:
            await plugin._send_chain(event, *extra, Video.fromURL(url))
            return {"ok": True, "reason": "url", "url": url}
        except Exception as err:
            plugin._log_warn(f"MV 直发 URL 失败，尝试落盘: {err}")
            try:
                local_path = await _download()
            except Exception as err2:
                plugin._log_warn(f"MV 落盘失败: {err2}")

    if not local_path and (download or passive_limited):
        try:
            local_path = await _download()
        except Exception as err:
            plugin._log_warn(f"MV 落盘失败: {err}")

    if local_path:
        # 视频发送失败 / 下载模式：发下载文件（File 全平台支持）
        try:
            await plugin._send_chain(event, *extra, File(file_name, file=local_path))
            _schedule_cleanup(local_path, keep_sec)
            return {"ok": True, "reason": "file", "url": url, "filePath": local_path}
        except Exception as err:
            plugin._log_warn(f"MV 文件发送失败，重试视频组件: {err}")
            try:
                await plugin._send_chain(event, *extra, Video.fromFileSystem(local_path))
                _schedule_cleanup(local_path, keep_sec)
                return {"ok": True, "reason": "video-file", "url": url, "filePath": local_path}
            except Exception as err2:
                plugin._log_warn(f"MV 视频组件发送也失败: {err2}")
                _schedule_cleanup(local_path, 10)

    return {"ok": False, "reason": "send_fail", "url": url, "filePath": local_path}
