from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import time

import aiohttp
from astrbot.api import logger
from astrbot.api.message_components import File, Record, Video

from . import api as qqapi
from . import messages as msg
from .quality import QUALITY_LABEL
from .utils import safe_int

PLUGIN_NAME = "astrbot_plugin_qqmusic"

# ──────────── 体积/超时/码率等阈值常量 ────────────

# 下载超时兜底（毫秒）与临时文件保留兜底（秒）：与 _conf_schema.json 的默认值保持一致
_DEFAULT_DOWNLOAD_TIMEOUT_MS = 120000
_DEFAULT_KEEP_FILE_SEC = 60
# 流式下载块大小与落盘攒批大小
_DOWNLOAD_CHUNK_BYTES = 256 * 1024
_DOWNLOAD_FLUSH_BYTES = 1024 * 1024
# 小于该字节数视为无效下载 / 无效转码产物
_MIN_VALID_BYTES = 256
# 语音转码码率：体积越大码率越低，避免语音消息过大被协议端拒收
_LARGE_AUDIO_BYTES = 16 * 1024 * 1024
_MEDIUM_AUDIO_BYTES = 8 * 1024 * 1024
_BITRATE_SMALL = "128k"
_BITRATE_MEDIUM = "96k"
_BITRATE_LARGE = "64k"
_BITRATE_LOW_QUALITY = "32k"


# 被动回复受限平台：QQ 官方机器人
# 需合并消息省被动回复额度，且无 OneBot send_api（原生/自定义音乐卡 no-op）
# 注意：weixin_oc 是微信个人号平台，具备完整主动/被动消息能力，不属于受限平台
_PASSIVE_LIMITED = ("qq_official",)


def _platform_matches(event, tokens) -> bool:
    """事件平台名是否命中 tokens 之一（取不到平台名时按「不命中」处理）。

    四个平台判定原先各写一份同样的 try/取名字/any(in) 骨架，其中 `_is_aiocqhttp`
    还漏了 try 保护、并且在 OneBot 元组变化时需要单独改一处。
    """
    try:
        name = str(event.get_platform_name() or "")
    except Exception:  # noqa: BLE001
        return False
    return any(k in name for k in tokens)


def _is_passive_limited(event) -> bool:
    return _platform_matches(event, _PASSIVE_LIMITED)


# 语音(Record)出站不支持的平台：weixin_oc 个人微信仅支持 Plain/Image/Video/File，
# Record 会被适配器丢弃并抛"unsupported outbound segment"——发送前直接跳过
_NO_VOCAL_PLATFORMS = ("weixin_oc",)


def _no_vocal(event) -> bool:
    return _platform_matches(event, _NO_VOCAL_PLATFORMS)


# OneBot 系平台（aiocqhttp → QQ 个人号，底层 NapCat/LLOneBot/Lagrange 等 NTQQ 协议端）。
# 与 Yunzai 版 OneBot 行为对齐：语音先压成紧凑 mp3、群文件优先传压缩版（NTQQ 拒 .flac）
_ONEBOT_PLATFORMS = ("aiocqhttp",)


def _is_onebot(event) -> bool:
    return _platform_matches(event, _ONEBOT_PLATFORMS)


def _is_aiocqhttp(event) -> bool:
    """与 ``_is_onebot`` 同一判定：当前 OneBot 元组只有 aiocqhttp。

    单独保留这个函数是为了让调用点读起来贴近语义，实现共用同一个 helper，
    元组将来变大时两处不会漂移。
    """
    return _platform_matches(event, _ONEBOT_PLATFORMS)


# base64 内联直发的体积上限：读盘 + b64 编码（+33%）都发生在内存里，超过该值
# 的文件（异常巨大的 MV/无损）不再内联——WebSocket 载荷上限也注定发不出去。
BASE64_INLINE_MAX_BYTES = 150 * 1024 * 1024


def _file_to_base64(path: str) -> str:
    import base64

    size = os.path.getsize(path)
    if size > BASE64_INLINE_MAX_BYTES:
        raise RuntimeError(
            f"文件 {size / 1024 / 1024:.0f}MB 超过 base64 直发上限"
            f"（{BASE64_INLINE_MAX_BYTES // 1024 // 1024}MB），请改用文件通道"
        )
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


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


async def _aiocq_send_record(event, text: str, src_path: str) -> tuple[bool, str]:
    """aiocqhttp 语音：紧凑 mp3 以 base64:// record 段直发（OneBot v11 标准段）。

    不再插件侧预编码 silk：协议端（NapCat / SnowLuma 等按 OneBot v11 实现的 NTQQ 框架）
    对 record 段统一「非 silk → 自带 ffmpeg addon 转 Tencent silk（\\x02#!SILK_V3、
    24kHz 单声道），时长用 ffmpeg 对原始文件精确探测」。此前插件自编的裸 silk 会被
    协议端按魔数透传——非标码流导致手机无法播放、时长探测失败钳到 1 秒。
    走 base64 直发而非 Record 组件，是为了绕开 AstrBot 把语音强制转 WAV 再编码
    （载荷 ~50MB+ → WS 超时）。任何一步失败返回 (False, 原因)，由调用方退回
    标准 Record 组件发送。
    """
    try:
        b64 = await asyncio.to_thread(_file_to_base64, src_path)
        segs: list = []
        if text:
            segs.append({"type": "text", "data": {"text": text}})
        segs.append({"type": "record", "data": {"file": f"base64://{b64}"}})
        is_group = bool(getattr(event.message_obj, "group_id", None))
        sid = event.message_obj.group_id if is_group else event.get_sender_id()
        await _aiocq_call_action(event, "send_group_msg" if is_group else "send_private_msg", int(sid), segs)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


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
            if os.path.getsize(out) > _MIN_VALID_BYTES:
                return out
        except OSError:
            pass

    bitrate = (
        _BITRATE_LARGE
        if size > _LARGE_AUDIO_BYTES
        else _BITRATE_MEDIUM
        if size > _MEDIUM_AUDIO_BYTES
        else _BITRATE_SMALL
    )
    common = ["ffmpeg", "-y", "-i", abs_path, "-vn", "-acodec", "libmp3lame"]
    args = (
        common + ["-ar", "16000", "-ac", "1", "-b:a", _BITRATE_LOW_QUALITY, out]
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
        if (
            res.returncode != 0
            or not os.path.exists(out)
            or os.path.getsize(out) <= _MIN_VALID_BYTES
        ):
            with contextlib.suppress(OSError):
                os.remove(out)
            return file_path
        return out
    except Exception:
        # 超时/被取消：半成品 mp3 不会被任何人登记进清理表，就地删掉，
        # 否则 temp 目录会随每次失败的压缩持续堆积。
        with contextlib.suppress(OSError):
            os.remove(out)
        return file_path
# 临时目录：固定在插件数据目录下（``data/plugin_data/<插件名>/temp``）。
# 不放插件自身目录——插件更新/重装会整体替换该目录，会让在途的下载与卡片图失效。
# 首次调用在线程池里建目录（阻塞 IO 不占事件循环），此后直接命中进程内缓存。
_temp_dir: str = ""


async def get_temp_dir() -> str:
    """返回（并按需创建）插件数据目录下的临时目录。"""
    global _temp_dir
    if _temp_dir:
        return _temp_dir

    def _resolve() -> str:
        from astrbot.api.star import StarTools

        d = StarTools.get_data_dir(PLUGIN_NAME) / "temp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    _temp_dir = await asyncio.to_thread(_resolve)
    return _temp_dir




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
    timeout_ms: int = _DEFAULT_DOWNLOAD_TIMEOUT_MS,
    quality_hint: str = "",
) -> dict:
    headers = {
        "User-Agent": qqapi.UA,
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
    size = 0
    # 确保目标文件不存在（时间戳通常唯一，此处双保险避免 ab 追加到残留文件）
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception:
            pass
    try:
        # 复用模块级会话（api._get_session）：连播 30 首不再各自新建 TCP/DNS 握手；
        # 超时按每请求传入，不改复用会话的全局默认超时
        sess = qqapi.get_session()
        async with sess.get(
            url, headers=headers, timeout=timeout, allow_redirects=True
        ) as res:
            if res.status >= 400:
                raise RuntimeError(f"下载失败 HTTP {res.status}")
            # 流式分块下载：避免整文件读进内存；每攒 ~1MB 落盘一次（线程池，不阻塞事件循环）
            pending = bytearray()
            first_chunk = True
            async for chunk in res.content.iter_chunked(_DOWNLOAD_CHUNK_BYTES):
                if first_chunk:
                    head = chunk[:32].decode("utf-8", errors="ignore").lower()
                    if "<html" in head or "<!doctype" in head:
                        raise RuntimeError("下载内容为 HTML，音频链接已失效")
                    first_chunk = False
                size += len(chunk)
                pending.extend(chunk)
                if len(pending) >= _DOWNLOAD_FLUSH_BYTES:
                    await asyncio.to_thread(_append_bytes, file_path, bytes(pending))
                    pending.clear()
            if pending:
                await asyncio.to_thread(_append_bytes, file_path, bytes(pending))
            if size < _MIN_VALID_BYTES:
                raise RuntimeError("下载内容过小，可能是无效链接")
    # 显式捕获 CancelledError（3.8+ 属 BaseException，不落 except Exception）：插件
    # 卸载/重载取消后台任务时同样要删掉半截文件，否则 terminate() 后永久残留在 temp
    except (Exception, asyncio.CancelledError):
        _remove_file(file_path)
        raise
    return {"filePath": file_path, "size": size}


def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)


def _append_bytes(path: str, data: bytes):
    with open(path, "ab") as f:
        f.write(data)


# 清理延迟下限：keepFileSec=0 时若立即删除，event.send() 可能还没读盘（QQ 官方分片上传
# 是异步读文件的），故至少留 5 秒
_MIN_KEEP_SEC = 5
# ``cancel_cleanup_timers()`` 的「刚登记」宽限期（秒）：登记后不足该秒数的条目视为
# 可能仍在被发送方读取——卡片图在 ``render_card`` 返回前就已登记、调用方随后才
# ``event.send()``；压缩语音同样在发送前登记。此时若 terminate() 立即删除文件，
# 正在发送的消息会读不到盘（3x 卡片 0.5–5MB，窗口可达秒级）。
# 做成模块常量便于测试（值本身不参与业务逻辑分支）。
_CANCEL_GRACE_SEC = 5
# 待执行的清理定时器：``TimerHandle -> (待删除的临时文件路径, 登记时间 monotonic)``。
#
# 登记范围（全进程唯一的一份，不存在第二份实例级注册表）：
#   * 本模块内部的下载/转码产物（deliver_song / deliver_video / download_audio）；
#   * ``MusicService.schedule_unlink()``（卡片图等）只是本登记处的门面，直接转发到
#     ``_schedule_cleanup()``；
#   * 插件卸载/重载时由 ``MusicService.terminate()`` 调 ``cancel_cleanup_timers()``
#     统一回收。
# 多实例说明：本表按「进程」而非按插件实例隔离，若同一进程里同时存在两个插件实例，
# 任一实例的 terminate() 会连带清理另一实例已排期的临时文件——临时文件本就只服务
# 于一次发送，重载时提前删除是安全方向（只会少留垃圾，不会少发消息）。
_cleanup_timers: dict = {}


def _remove_file(path: str) -> None:
    """best-effort 删除临时文件：不存在/被占用/无权限一律忽略（不影响主流程）。"""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _schedule_cleanup(file_path: str, keep_sec: int):
    """延迟删除临时文件（句柄、路径与登记时间一起记录，卸载时由 cancel_cleanup_timers() 取消并补偿删除）。"""
    try:
        delay = max(_MIN_KEEP_SEC, int(keep_sec))
    except (TypeError, ValueError):
        delay = _DEFAULT_KEEP_FILE_SEC
    loop = asyncio.get_running_loop()

    def _rm():
        _cleanup_timers.pop(handle, None)
        _remove_file(file_path)

    handle = loop.call_later(delay, _rm)
    _cleanup_timers[handle] = (file_path, time.monotonic())
    return handle


def cancel_cleanup_timers() -> None:
    """取消全部待执行的清理定时器，并立即补偿删除它们登记的临时文件。

    只 ``cancel()`` 不删文件会真漏：卡片图排期 120s（``render_card``）、音频/MV 产物
    排期 ``keepFileSec`` 秒，而插件重载/卸载通常远早于这两个时限——定时器被取消后
    其 ``_rm`` 永不执行，文件永久留在 temp 目录（单次重载可残留数十至上百 MB）。

    宽限期（``_CANCEL_GRACE_SEC``）：登记后不足该秒数的条目可能**仍在被发送读取**
    （卡片图在 ``render_card`` 返回前就登记、调用方随后才 ``event.send()``；压缩语音
    也在发送前登记），此刻删除会让在途消息读不到盘。这类条目既不取消也不删除，
    留给原定时器到点自行清理（重载后事件循环仍在，定时器照常触发，不会漏）。
    """
    now = time.monotonic()
    for handle, entry in list(_cleanup_timers.items()):
        path, registered_at = entry
        if now - registered_at < _CANCEL_GRACE_SEC:
            # 刚登记：可能正在发送中，跳过（定时器保留，到点仍会删除）
            continue
        try:
            handle.cancel()
        except Exception:
            pass
        _remove_file(path)
        _cleanup_timers.pop(handle, None)


async def _send_music_segment(event, music_data: dict) -> bool:
    """以 OneBot v11 ``music`` 段直发原生/自定义音乐卡，平台不支持时返回 False。

    适配器实例在 aiocqhttp 下是 ``event.bot``（CQHttp），``event.platform`` 只是
    PlatformMetadata 元数据、没有发送接口；OneBot 侧只有 ``call_action``，
    不存在 ``send_api``/``sendApi``，因此统一走 ``call_action``。
    """
    bot = getattr(event, "bot", None)
    if bot is None:
        bot = getattr(getattr(event, "platform", None), "bot", None)
    call_action = getattr(bot, "call_action", None)
    if call_action is None:
        return False
    is_group = bool(getattr(event.message_obj, "group_id", None))
    sid = event.message_obj.group_id if is_group else event.get_sender_id()
    target = {"group_id": int(sid)} if is_group else {"user_id": int(sid)}
    action = "send_group_msg" if is_group else "send_private_msg"
    try:
        await call_action(
            action,
            message=[{"type": "music", "data": music_data}],
            **target,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[qqmusic] 音乐卡片发送失败（{action}）: {e}")
        return False


async def send_native_music_card(event, platform_type: str, music_id: str) -> bool:
    return await _send_music_segment(event, {"type": platform_type, "id": str(music_id)})


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
    return await _send_music_segment(event, data)


def _delivery_view(song: dict, play: dict, cfg: dict) -> tuple[str, str, str, str, str]:
    """投递所需的展示字段：标题 / 歌手 / 封面 / 页面链接 / 音质标签。"""
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
    return title, singer, cover, page_url, quality_label


async def _prepare_compressed_vocal(
    event, cfg: dict, local_path: str, *, want_vocal: bool, want_file: bool
) -> str:
    """按需产出「紧凑 mp3」副本，返回其路径（无需压缩时返回空串）。

    语音 / OneBot 群文件都需要「紧凑 mp3」：FLAC 直接作语音会被协议端拒；
    OneBot(NTQQ) 群文件对 .flac 也常报「未知文件类型或路径不存在」。
    其余平台群文件保留原始高音质文件；低音质只用于语音（禁用高清语音时 PC 可播）。
    无语音平台（weixin_oc 等）语音被跳过时不压缩，避免浪费编码。
    """
    need_compress = bool(local_path) and (
        (want_vocal and not _no_vocal(event)) or (want_file and _is_onebot(event))
    )
    if not need_compress:
        return ""
    return await prepare_vocal_file(
        local_path,
        direct_ext=(
            _QQOFFICIAL_DIRECT_EXT if _is_passive_limited(event) else _VOCAL_DIRECT_EXT
        ),
        low_quality=want_vocal and cfg.get("disableHighQualityVocal") is True,
    )


async def _send_media_with_text(plugin, event, media_comp, pending_text: str) -> None:
    """发送「文案 + 媒体」消息链（文案为空则只发媒体）。

    失败会抛出（门面对含媒体的消息启用 raise_on_error），由调用方走降级分支。
    """
    comps = [plugin._plain(pending_text), media_comp] if pending_text else [media_comp]
    await plugin._send_chain(event, *comps)


async def _stage_text_info(
    plugin,
    event,
    *,
    song: dict,
    play: dict,
    cfg: dict,
    title: str,
    singer: str,
    quality_label: str,
    skip_text: bool,
    is_limited: bool,
) -> str:
    """构造歌曲信息文案：普通平台立即发送并返回空串；受限平台返回待合并文案。

    受限平台（QQ 官方/公众号）延后文案、与首个媒体合并发送以省被动回复额度。
    """
    if skip_text or not cfg.get("sendTextInfo", True):
        return ""
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
        return ""
    return pending_text


async def _send_music_cards(
    event,
    *,
    song: dict,
    play: dict,
    cfg: dict,
    options: dict,
    title: str,
    singer: str,
    cover: str,
    page_url: str,
    is_limited: bool,
) -> None:
    """按配置发送原生/自定义音乐卡；NTQQ 不支持 type:qq 原生卡时降级自定义卡（对齐 JS 版 send.js）。"""
    allow_native = (
        (not options.get("skipNativeCard", False))
        and cfg.get("sendNativeCard")
        and not is_limited
    )
    allow_custom = (
        (not options.get("skipCustomCard", False))
        and cfg.get("sendCustomCard")
        and not is_limited
    )
    if allow_native and song.get("songid"):
        native_ok = await send_native_music_card(event, "qq", song["songid"])
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


async def _download_with_retry(song: dict, play: dict, *, cfg: dict, user_key: str) -> str:
    """下载音频；首链失败时刷新播放链重试一次。全部失败抛出最后一个异常。"""
    save_dir = await get_temp_dir()
    timeout = safe_int(
        cfg.get("downloadTimeout") or _DEFAULT_DOWNLOAD_TIMEOUT_MS,
        _DEFAULT_DOWNLOAD_TIMEOUT_MS,
    )
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
                        else (play.get("quality") or cfg.get("quality") or "auto")
                    )
                    # 必须带 user_key：不带就退回「默认账号」取链，
                    # 会员曲重试必然再失败，用户已登录却被告知要登录
                    fresh = await qqapi.song_url_best(
                        song["songmid"],
                        quality=prefer,
                        media_id=song.get("media_mid")
                        or play.get("mediaId")
                        or song.get("songmid"),
                        fallback=True,
                        user_key=user_key,
                    )
                    if fresh.get("url") and fresh["url"] != try_urls[0]:
                        try_urls.append(fresh["url"])
                        play.update(fresh)
                except Exception:
                    pass
    if not dl:
        raise last_err or RuntimeError("下载失败")
    return dl["filePath"]


async def _send_vocal_channel(
    plugin,
    event,
    *,
    local_path: str,
    vocal_path: str,
    pending_text: str,
    want_file: bool,
    is_aiocq: bool,
    is_limited: bool,
) -> tuple[bool, str]:
    """语音通道：aiocqhttp base64 直发 → 失败退回 Record 组件。

    返回 ``(是否真的发出, 更新后的待发文案)``——文案已随语音发出则为空串。
    """
    if _no_vocal(event):
        plugin._log_info("当前平台不支持语音，跳过语音只发文件")
        if not want_file:
            # 该平台没有语音通道且未开启群文件：音频已下载却无处可发，明确告知用户
            await plugin._send_chain(event, plugin._plain(msg.NO_VOCAL_NO_FILE_HINT))
        return False, pending_text
    limited_tag = "（受限平台）" if is_limited else ""
    voice_source = vocal_path or local_path
    if is_aiocq:
        # aiocqhttp：紧凑音频 base64 直发，协议端自行转标准 Tencent silk（时长/兼容性正确）
        ok, reason = await _aiocq_send_record(event, pending_text, voice_source)
        if ok:
            return True, ""
        plugin._log_warn(f"aiocqhttp 语音直发失败（{reason}），退回 Record 组件")
    try:
        await _send_media_with_text(
            plugin, event, Record.fromFileSystem(voice_source), pending_text
        )
        return True, ""
    except Exception as e:
        plugin._log_warn(f"语音发送失败{limited_tag}: {e}")
    return False, pending_text


async def _send_file_channel(
    plugin,
    event,
    *,
    cfg: dict,
    local_path: str,
    vocal_path: str,
    pending_text: str,
    file_display: str,
    is_aiocq: bool,
    is_limited: bool,
) -> tuple[bool, str]:
    """文件通道：aiocqhttp base64 直发 / QQ 官方分片 / 压缩 mp3 兜底。

    返回 ``(是否真的发出, 更新后的待发文案)``。
    """
    limited_tag = "（受限平台）" if is_limited else ""
    has_compressed = bool(vocal_path and vocal_path != local_path)
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
            return True, ""
        except Exception as e:
            plugin._log_warn(f"文件发送失败{limited_tag}: {e}")
        return False, pending_text
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
        _is_onebot(event) or (is_qqofficial and qq_large_file and not qq_chunk)
    )
    if use_compressed:
        try:
            await _send_media_with_text(
                plugin, event, File(comp_display, file=vocal_path), pending_text
            )
            return True, ""
        except Exception as e:
            plugin._log_warn(f"文件发送失败{limited_tag}: {e}")
        return False, pending_text
    if qq_chunk:
        plugin._log_info(f"QQ官方大文件 {os.path.basename(local_path)} 走适配器分片上传")
    try:
        await _send_media_with_text(
            plugin, event, File(file_display, file=local_path), pending_text
        )
        return True, ""
    except Exception as e:
        plugin._log_warn(f"文件发送失败{limited_tag}: {e}")
    return False, pending_text


async def _pre_download_stage(
    plugin, event, song: dict, play: dict, cfg: dict, options: dict
) -> tuple[str, dict | None]:
    """下载前阶段：发信息文案、按配置发音乐卡、做前置校验。

    返回 ``(待合并文案, 提前结束的结果)``——可以继续下载时 ``early`` 为 ``None``；
    提前结束（无播放链 / 双通道全关）时 ``early`` 携带结果 dict，且暂存文案已补发。
    """
    title, singer, cover, page_url, quality_label = _delivery_view(song, play, cfg)

    is_limited = (
        _is_passive_limited(event) and cfg.get("qqofficialAdapt", True) is not False
    )

    # 文案：普通平台直接发；受限平台（QQ 官方/公众号）延后，与首个媒体合并省被动回复额度
    pending_text = await _stage_text_info(
        plugin,
        event,
        song=song,
        play=play,
        cfg=cfg,
        title=title,
        singer=singer,
        quality_label=quality_label,
        skip_text=options.get("skipTextInfo", False),
        is_limited=is_limited,
    )

    await _send_music_cards(
        event,
        song=song,
        play=play,
        cfg=cfg,
        options=options,
        title=title,
        singer=singer,
        cover=cover,
        page_url=page_url,
        is_limited=is_limited,
    )

    if not play.get("url"):
        # 受限平台暂存的文案在提前 return 前必须补发，否则整行丢失（用户只看到错误/无反应）
        if pending_text:
            await plugin._send_chain(event, plugin._plain(pending_text))
        return "", {"ok": False, "reason": "no_url"}

    need_download = cfg.get("sendVocal") or cfg.get("uploadFile")
    if not need_download:
        if pending_text:
            await plugin._send_chain(event, plugin._plain(pending_text))
        # 两个通道都关了：媒体确实没发出去 → 不能回 ok=True
        return "", {"ok": False, "reason": "no_channel", "downloaded": False}

    return pending_text, None


async def _prepare_audio_assets(
    event, cfg: dict, *, local_path: str, title: str, singer: str
) -> tuple[int, str, str, bool, bool]:
    """投递资产准备：keep_sec / 压缩语音产物 / 文件展示名 / 双通道开关。

    压缩产物（与原文件不同路径时）在此纳入清理登记，避免 temp 目录累积。
    """
    keep_sec = safe_int(cfg.get("keepFileSec"), _DEFAULT_KEEP_FILE_SEC)
    ext = os.path.splitext(local_path)[1] or ".mp3"
    file_display = build_music_filename(singer=singer, title=title, ext=ext)
    want_vocal = bool(cfg.get("sendVocal"))
    want_file = bool(cfg.get("uploadFile"))
    vocal_path = await _prepare_compressed_vocal(
        event, cfg, local_path, want_vocal=want_vocal, want_file=want_file
    )
    if vocal_path and vocal_path != local_path:
        _schedule_cleanup(vocal_path, keep_sec)
    return keep_sec, vocal_path or "", file_display, want_vocal, want_file


async def _deliver_channels(
    plugin,
    event,
    *,
    cfg: dict,
    local_path: str,
    vocal_path: str,
    file_display: str,
    want_vocal: bool,
    want_file: bool,
    pending_text: str,
    is_aiocq: bool,
    is_limited: bool,
) -> tuple[str, bool]:
    """语音/文件双通道依次尝试，互不阻塞。

    sent_media：只要有一条媒体通道真的发出即为 True——调用方（#qqm听所有 连播统计）
    依赖它判断真实结果，两条通道都失败时不能再谎报成功。
    """
    sent_media = False
    if want_vocal:
        sent_media, pending_text = await _send_vocal_channel(
            plugin,
            event,
            local_path=local_path,
            vocal_path=vocal_path,
            pending_text=pending_text,
            want_file=want_file,
            is_aiocq=is_aiocq,
            is_limited=is_limited,
        )
    if want_file:
        file_sent, pending_text = await _send_file_channel(
            plugin,
            event,
            cfg=cfg,
            local_path=local_path,
            vocal_path=vocal_path,
            pending_text=pending_text,
            file_display=file_display,
            is_aiocq=is_aiocq,
            is_limited=is_limited,
        )
        sent_media = sent_media or file_sent
    return pending_text, sent_media


async def deliver_song(
    plugin,
    event,
    song: dict,
    play: dict,
    *,
    cfg: dict,
    options: dict | None = None,
    user_key: str = "",
) -> dict:
    """交付一首歌：文案 / 音乐卡 → 下载 → 双通道投递（语音 + 文件）。

    步骤拆到 ``_stage_text_info`` / ``_send_music_cards`` / ``_download_with_retry`` /
    ``_send_vocal_channel`` / ``_send_file_channel``；本函数只负责编排与结果汇总。
    """
    options = options or {}
    pending_text, early = await _pre_download_stage(plugin, event, song, play, cfg, options)
    if early is not None:
        return early
    title, singer, _, _, _ = _delivery_view(song, play, cfg)

    is_limited = (
        _is_passive_limited(event) and cfg.get("qqofficialAdapt", True) is not False
    )
    is_aiocq = _is_aiocqhttp(event)

    try:
        local_path = await _download_with_retry(song, play, cfg=cfg, user_key=user_key)
    except Exception as err:
        await plugin._send_chain(
            event,
            plugin._plain(f"下载音频失败：{err}\n可尝试 #qqm登录 后重发，或换一首歌"),
        )
        return {"ok": False, "reason": "download_fail", "error": str(err)}

    keep_sec, vocal_path, file_display, want_vocal, want_file = await _prepare_audio_assets(
        event, cfg, local_path=local_path, title=title, singer=singer
    )
    pending_text, sent_media = await _deliver_channels(
        plugin,
        event,
        cfg=cfg,
        local_path=local_path,
        vocal_path=vocal_path,
        file_display=file_display,
        want_vocal=want_vocal,
        want_file=want_file,
        pending_text=pending_text,
        is_aiocq=is_aiocq,
        is_limited=is_limited,
    )
    # 临时文件清理：只要已下载就登记——含「平台无语音被跳过且未开群文件」这类
    # 「下载了却发不出去」的情形，否则产物永久残留在 temp
    _schedule_cleanup(local_path, keep_sec)

    # 兜底：所有媒体发送均失败时，至少把文案发出去
    if pending_text:
        await plugin._send_chain(event, plugin._plain(pending_text))

    if not sent_media:
        # 语音/文件两条通道都没发出去（平台不支持语音且未开群文件等）：如实返回失败
        return {"ok": False, "reason": "send_fail", "downloaded": True}
    return {"ok": True, "downloaded": True}


async def deliver_video(
    plugin,
    event,
    mv: dict,
    url: str,
    *,
    cfg: dict,
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
    title = mv.get("mvtitle") or mv.get("name") or mv.get("songName") or "MV"
    extra = list(extra or [])
    local_path = ""
    keep_sec = safe_int(cfg.get("keepFileSec"), _DEFAULT_KEEP_FILE_SEC)
    passive_limited = _is_passive_limited(event)
    file_name = _clean_track_text(title, 30) + ".mp4"

    async def _download() -> str:
        save_dir = await get_temp_dir()
        timeout = safe_int(
            cfg.get("downloadTimeout") or _DEFAULT_DOWNLOAD_TIMEOUT_MS,
            _DEFAULT_DOWNLOAD_TIMEOUT_MS,
        )
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

    # 受限平台（官方 API / 个人微信）必须落盘。这里的 ``not download`` 不能少：
    # download=True 时上面已经下过一次，同一 URL 再下一次只会白等两倍 downloadTimeout。
    if not local_path and passive_limited and not download:
        try:
            local_path = await _download()
        except Exception as err:
            plugin._log_warn(f"MV 落盘失败: {err}")

    if local_path:
        result = await _send_mv_file_with_fallback(
            plugin, event, file_name=file_name, local_path=local_path, url=url, extra=extra
        )
        if result is not None:
            _schedule_cleanup(local_path, keep_sec)
            return result
        # 全部发送方式都失败：按短保留期登记清理（调用方回退 URL 文本兜底）
        _schedule_cleanup(local_path, 10)

    return {"ok": False, "reason": "send_fail", "url": url, "filePath": local_path}


async def _send_mv_file_with_fallback(
    plugin, event, *, file_name: str, local_path: str, url: str, extra: list
) -> dict | None:
    """MV 落盘后的发送降级链：File → aiocqhttp base64 直发 / Video 内嵌。

    成功返回结果 dict（调用方据此登记清理）；全部失败返回 None。
    """
    # 视频发送失败 / 下载模式：发下载文件（File 全平台支持）
    try:
        await plugin._send_chain(event, *extra, File(file_name, file=local_path))
        return {"ok": True, "reason": "file", "url": url, "filePath": local_path}
    except Exception as err:
        plugin._log_warn(f"MV 文件发送失败: {err}")
    if _is_aiocqhttp(event):
        # aiocqhttp 跨容器 file:// 路径不可见 → 重试改走 base64 直发，
        # 不再用同一套 File/Video 组件机制重试（必然再失败）
        try:
            await _aiocq_send_file(event, "", file_name, local_path)
            return {"ok": True, "reason": "file", "url": url, "filePath": local_path}
        except Exception as err2:
            plugin._log_warn(f"aiocqhttp MV 直发也失败: {err2}")
        return None
    plugin._log_warn("MV 文件发送失败，重试视频组件")
    try:
        await plugin._send_chain(event, *extra, Video.fromFileSystem(local_path))
        return {"ok": True, "reason": "video-file", "url": url, "filePath": local_path}
    except Exception as err2:
        plugin._log_warn(f"MV 视频组件发送也失败: {err2}")
    return None
