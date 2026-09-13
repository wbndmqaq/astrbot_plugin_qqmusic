from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urljoin, urlparse

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File, Image, Plain

from . import api as qqapi
from . import cards as cardlib
from . import login as loginflow
from . import messages as msg
from . import panels
from . import resolve as resolver
from .delivery import (
    _DEFAULT_KEEP_FILE_SEC,
    _aiocq_send_file,
    _is_aiocqhttp,
    _is_passive_limited,
    _no_vocal,
    _remove_file,
    _schedule_cleanup,
    cancel_cleanup_timers,
    deliver_video,
)
from .login import (
    _POLL_INTERVAL_SEC,
)
from .quality import QUALITY_LABEL
from .utils import safe_int

if TYPE_CHECKING:  # 仅类型注解使用，运行时不导入（main.py 会反向 import 本模块）
    from ..main import QQMusicPlugin

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)

# 播放投递时跳过全部附加卡片/文案（文本+原生卡+自定义卡）
_SKIP_ALL = {
    "skipTextInfo": True,
    "skipNativeCard": True,
    "skipCustomCard": True,
}
# 仅跳过文本信息（详情卡已含歌名/歌手/音质），原生/自定义音乐卡按配置发送
_SKIP_TEXT = {"skipTextInfo": True}

# #qqm听所有 连播上限（防止误触发刷屏/大量下载）
_PLAY_ALL_LIMIT = 30

# 歌词卡片每页行数（超长歌词自动分页）
_LYRIC_PAGE_LINES = 36


_VERSION_CACHE: str | None = None


async def get_local_version(plugin_dir: str = PLUGIN_DIR) -> str:
    """从 metadata.yaml 读取插件版本号（用于帮助卡片/配置面板展示）。

    metadata.yaml 在进程内不变，读一次即缓存；读盘走线程池，避免同步 IO
    阻塞事件循环（该函数由 async handler 调用）。

    形参说明：``_VERSION_CACHE`` 是**进程级**单值缓存，只认第一次调用时传入的
    ``plugin_dir``；唯一调用点固定传 ``PLUGIN_DIR``（= 本插件根目录），故缓存
    与实际读取的目录始终一致——保留形参只为兼容历史调用签名，若将来出现
    「多插件目录共用一个进程」的调用方式，需把缓存改为按目录分桶的 dict。
    """
    global _VERSION_CACHE
    if _VERSION_CACHE is not None:
        return _VERSION_CACHE

    def _read() -> str:
        try:
            import yaml

            with open(
                os.path.join(plugin_dir, "metadata.yaml"), "r", encoding="utf-8"
            ) as f:
                meta = yaml.safe_load(f) or {}
            return str(meta.get("version", "?")).lstrip("v")
        except Exception:
            return "?"

    _VERSION_CACHE = await asyncio.to_thread(_read)
    return _VERSION_CACHE


# safe_int 由 core/utils.py 提供（无内部依赖的小模块），各模块统一从那里导入。

# QQ 音乐相关域名白名单（短链重定向仅允许这些 host，防 SSRF）—— 对齐 JS 版 resolve.js
_QQ_HOST_SUFFIXES = ("y.qq.com", "qq.com", "gtimg.cn", "url.cn", "qpic.cn")


def is_qq_host(hostname: str = "") -> bool:
    host = str(hostname or "").strip().lower()
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in _QQ_HOST_SUFFIXES)


async def follow_qq_redirect(url: str, max_redirects: int = 5) -> str:
    """跟随 c6.y.qq.com 短链重定向（SSRF 白名单内），拿最终 songDetail 链接；失败回退原链接。

    最后一跳同样校验 host，避免把非白名单 URL 交回上层解析（SSRF 加固）。
    """
    current = url
    hops = 0
    while True:
        try:
            parsed = urlparse(current)
            if not is_qq_host(parsed.hostname):
                return url
        except Exception:
            return url
        try:
            sess = qqapi.get_session()
            async with sess.get(
                current,
                allow_redirects=False,
                headers={"User-Agent": qqapi.UA},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as res:
                if res.status in (301, 302, 303, 307, 308):
                    location = res.headers.get("Location")
                    if not location:
                        return current
                    next_url = urljoin(current, location)
                    if hops >= max_redirects:
                        try:
                            if not is_qq_host(urlparse(next_url).hostname):
                                return url
                        except Exception:
                            return url
                        return next_url
                    current = next_url
                    hops += 1
                    continue
                return current
        except Exception:
            return url


def is_plugin_command_msg(msg: str) -> bool:
    return bool(
        re.match(
            r"^#?(qq|QQ)m(?![\dA-Za-z_])|^#?(qq|QQ)音乐|^#听\s*[1-9]|^#qm帮助",
            str(msg or "").strip(),
            re.IGNORECASE,
        )
    )


def is_qqmusic_message(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(
            r"y\.qq\.com|c6\.y\.qq\.com|i\.y\.qq\.com|qqmusic|QQ音乐|"
            r"100497308|com\.tencent\.(structmsg|music\.lua)|"
            r"sdkshare_music|songmid=|playsong\.html",
            text,
            re.IGNORECASE,
        )
    )


def collect_message_text(event: AstrMessageEvent) -> str:
    parts: list[str] = []
    try:
        msg_str = event.message_str
        if msg_str:
            parts.append(str(msg_str))
    except Exception:
        pass
    # 消息链中各组件
    try:
        mobj = event.message_obj
        chain = getattr(mobj, "message", None) or []
        for seg in chain:
            seg_type = type(seg).__name__
            if isinstance(seg, Plain):
                t = seg.text if hasattr(seg, "text") else None
                if t:
                    parts.append(str(t))
            elif seg_type in ("Image", "File", "Record", "Video", "Face"):
                continue
            elif seg_type == "Json":
                d = getattr(seg, "data", None)
                if d:
                    parts.append(
                        d if isinstance(d, str) else json.dumps(d, ensure_ascii=False, default=str)
                    )
            else:
                with contextlib.suppress(Exception):
                    parts.append(json.dumps(seg, ensure_ascii=False, default=str))
    except Exception:
        pass
    # 原始消息
    try:
        raw = event.message_obj.raw_message
        if raw:
            if isinstance(raw, str):
                parts.append(raw)
            else:
                parts.append(json.dumps(raw, ensure_ascii=False, default=str))
    except Exception:
        pass
    return "\n".join(p for p in parts if p)


class MusicService:
    """QQ 音乐插件核心业务服务类。"""

    # user_id -> {qrcodeID/sessionId, timer, stopped, ...}
    #
    # 刻意用类级（跨实例共享）而非实例级：轮询任务由 handlers 通过
    # ``service.stop_poll(user_id)`` 按用户管理，而「某用户正在登录」本就是进程内
    # 唯一事实，共享能让重复命令互相顶替。AstrBot 正常单实例加载下无实际危害；
    # 仅当同一进程里两个实例瞬时并存（重载交接）时，旧实例的 terminate() 会顺带
    # 停掉新实例刚登记的轮询——后果仅为需重新扫码，不会串账号，故保持现状。
    _active_logins: ClassVar[dict] = {}

    def __init__(self, plugin: QQMusicPlugin):
        self.plugin = plugin
        self.config = plugin.config
        # fire-and-forget 后台任务：持引用避免被 GC，卸载时统一取消
        self._bg_tasks: set[asyncio.Task] = set()
        qqapi.set_config_getter(lambda: self.config or {})

    # ──────────── 裸 #听N 跨插件抢占 ────────────

    async def is_session_owner(self) -> bool:
        """本插件是否为最近活跃的音乐插件（供裸 #听N 抢占，标记在 SessionStore.set 写入）。"""
        name = str(getattr(self.plugin, "name", "") or "")
        return await asyncio.to_thread(cardlib.is_session_owner, name)

    # ──────────── 基础辅助 ────────────

    def cfg(self) -> dict:
        return self.config or {}

    def log_warn(self, msg: str):
        logger.warning(f"[qqmusic] {msg}")

    def log_info(self, msg: str):
        logger.info(f"[qqmusic] {msg}")

    def plain(self, text: str) -> Plain:
        return Plain(text=text)

    def _spawn_bg(self, coro):
        """后台任务：持有引用避免被 GC，并记录异常，插件卸载时统一取消。"""
        try:
            task = asyncio.create_task(coro)
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"后台任务启动失败: {e}")
            return None
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task):
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.log_warn(f"后台任务异常: {t.exception()}")

        task.add_done_callback(_done)
        return task

    async def save_config(self) -> bool:
        """安全写盘：兼容 AstrBot <4.26.7（无 save_config_async）。返回是否写入成功。"""
        cfg = self.config
        try:
            saver = getattr(cfg, "save_config_async", None)
            if callable(saver):
                # 4.27.4 起返回「本次快照是否被提交」：并发写盘时有更新的快照先落地则返回 False。
                # 必须返回真实结果，否则又变成「写盘失败却谎报成功」。
                # 旧版无返回值（None）表示无此语义，按成功处理。
                committed = await saver()
                return committed is None or bool(committed)
            sync_saver = getattr(cfg, "save_config", None)
            if callable(sync_saver):
                await asyncio.to_thread(sync_saver)
                return True
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"配置保存失败: {e}")
            return False
        self.log_warn("配置保存失败: 当前 AstrBot 版本不支持配置写盘 API")
        return False

    def schedule_unlink(self, path: str, delay_sec: int = 120):
        """延迟删除临时文件（卡片图 / 二维码等）。

        登记表唯一存放在 ``core/delivery._cleanup_timers``（本方法是它的门面），
        ``terminate()`` 时由 ``cancel_cleanup_timers()`` 取消定时器并立即删除文件。
        """
        if not path:
            return None
        return _schedule_cleanup(path, delay_sec)

    async def send_chain(self, event: AstrMessageEvent, *components, raise_on_error: bool = False) -> bool:
        """发送消息链；返回是否真的发出。

        ``raise_on_error=True`` 会把发送失败重新抛出：投递层（core/delivery.py）依赖
        这个异常触发降级链——视频直发失败改落盘发文件、语音/文件失败改走重试分支。
        把异常吞掉，整条降级链就都成了死代码。

        默认 ``False``：只记日志并返回 ``False``，避免平台侧发送失败（风控/超限/协议
        错误）穿透成内核的通用报错。调用方按返回值决定是否降级。
        """
        comps = [c for c in components if c is not None]
        if not comps:
            return False
        # 先探测能力，而不是捕获 AttributeError：区分「内核太旧」与「适配器内部抛
        # AttributeError」，后者按发送失败处理。
        if getattr(event, "send", None) is None:
            # 事件对象根本没有 send（极旧内核）：退无可退，直接按发送失败处理。
            # 注意这里**不能**再调 event.send 重发文本——它按定义就是 None，
            # 只会抛 AttributeError 掩盖真正的原因。
            texts = [str(t) for t in (getattr(_c, "text", None) for _c in comps) if t]
            self.log_warn(
                "send_chain：事件对象没有 send 方法，无法发送"
                + (f"（原有文本 {len(texts)} 段）" if texts else "")
            )
            if raise_on_error:
                raise RuntimeError("event.send 不可用，无法发送消息")
            return False
        mc = MessageChain(chain=list(comps))
        try:
            await event.send(mc)
            return True
        except Exception as err:  # noqa: BLE001
            self.log_warn(f"send_chain 发送失败: {err}")
            if raise_on_error:
                raise
            return False

    async def reply(self, event: AstrMessageEvent, text: str):
        try:
            await self.send_chain(event, self.plain(text))
        except Exception as e:  # noqa: BLE001
            import traceback as _tb

            self.log_warn(f"reply 发送失败: {e}\n{_tb.format_exc()}")

    def scope(self, event: AstrMessageEvent) -> str:
        gid = getattr(event.message_obj, "group_id", None)
        if gid:
            return str(gid)
        return event.get_sender_id()

    def user_key(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "")

    def quality_label(self, q: str) -> str:
        return QUALITY_LABEL.get(q, q or "")

    @staticmethod
    def play_view(play: dict) -> dict:
        """把 song_url_best 结果归一化为投递视图（详情卡/语音投递共用）。"""
        q = play.get("quality") or ""
        return {
            "url": play.get("url", ""),
            "quality": play.get("quality"),
            "qualityLabel": play.get("qualityLabel") or QUALITY_LABEL.get(q, q or ""),
            "mvVid": play.get("mvVid") or "",
            "degradeNote": play.get("degradeNote") or "",
            "raw": play,
        }

    async def resolve_play(self, song: dict, cfg: dict, user_key: str = "") -> dict:
        quality = cfg.get("quality") or "auto"
        fallback = cfg.get("qualityFallback", True) is not False
        try:
            play = await qqapi.song_url_best(
                song["songmid"],
                quality=quality,
                media_id=song.get("media_mid") or song.get("songmid"),
                fallback=fallback,
                user_key=user_key,
            )
            return self.play_view(play)
        except Exception as e:  # noqa: BLE001
            return {
                "url": "",
                "quality": quality,
                "qualityLabel": self.quality_label(quality),
                "error": str(e),
                "raw": getattr(e, "payload", None),
            }

    async def render_card(
        self, event: AstrMessageEvent, data: dict, tpl_name: str
    ) -> str | None:
        try:
            from .delivery import get_temp_dir
            from .render import load_template, render_html_to_png, wrap_card_data

            tmpl_path = os.path.join(
                PLUGIN_DIR, "resources", "html", tpl_name, f"{tpl_name}.html"
            )
            if not os.path.exists(tmpl_path):
                return None
            # 模板按路径缓存编译结果（首次读盘 + 编译在线程池内完成）；卡片数据包装成
            # 「缺键即空值」，少一个字段不会让整张卡片渲染失败。
            template = await load_template(tmpl_path)
            html = template.render(data=wrap_card_data(data))
            d = await get_temp_dir()
            file_path = os.path.join(
                d, f"card_{tpl_name}_{int(time.time() * 1000)}.png"
            )
            if not await render_html_to_png(html, file_path):
                # 失败时截图可能已落盘（渲染中途报错 / 任务被取消），此处兜底删除：
                # 既不回执也不登记的 PNG 无人清理，只会永久留在 temp
                # （render_html_to_png 内部已删一次，重复删除是幂等的）
                _remove_file(file_path)
                self.log_warn(
                    f"{tpl_name} 渲染失败"
                    "（playwright 不可用？请确认已安装并执行 playwright install chromium）"
                )
                return None
            self.schedule_unlink(file_path, 120)
            return file_path
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"{tpl_name} 渲染失败: {e}")
            return None

    async def reply_card_or_text(
        self,
        event: AstrMessageEvent,
        *,
        tpl_name: str,
        data: dict,
        format_text,
    ) -> bool:
        try:
            url = await self.render_card(event, data, tpl_name)
            # 必须看发送结果：卡片被平台拒绝时要落到下面的纯文本兜底
            if url and await self.send_chain(event, Image.fromFileSystem(url)):
                return True
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"{tpl_name} 卡片渲染失败，回退文本: {e}")
        try:
            text = format_text(data)
            if text:
                await self.send_chain(event, self.plain(text))
                return True
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"{tpl_name} 文本兜底失败: {e}")
        return False

    async def reply_song_list(
        self,
        event: AstrMessageEvent,
        title: str,
        songs: list,
        *,
        tip: str | None = None,
        commands: list | None = None,
        text_suffix: str = "",
        text_formatter=None,
        card_builder=None,
    ) -> bool:
        """列表回执的统一实现：优先发送列表卡片，渲染不可用时回退纯文本。

        全仓原先有十余处「build_list_card_data → reply_card_or_text → 再 reply
        纯文本」的近重复样板，卡片 tip / 文本兜底 / 尾部引导在各处写法不一。此处
        收敛为一份实现，参数与各调用点原有语义一一对应：

        * ``tip`` / ``commands``：只透传给卡片（与原先各处传的 options 一致）
        * ``text_formatter``：纯文本兜底的格式化函数（默认 ``format_song_list``）
        * ``text_suffix``：纯文本兜底尾部**原样追加**的引导语（如选歌的「回复 #qqm听N…」）
        * ``card_builder``：卡片数据构造器（默认 ``build_list_card_data``；MV 列表传
          ``build_mv_list_card_data``）

        返回是否发出了卡片：调用方据此决定只有卡片分支才需补发的后续内容（如专辑
        发行时间）。纯文本兜底已由本方法负责发出，调用方**不要再发一次**。
        """
        cfg = self.cfg()
        formatter = text_formatter or cardlib.format_song_list
        text = formatter(songs, title) + text_suffix
        if cfg.get("renderListCard", True):
            if card_builder is not None:
                data = card_builder(title, songs, cfg=cfg)
            else:
                options: dict = {}
                if tip:
                    options["tip"] = tip
                if commands:
                    options["commands"] = commands
                data = cardlib.build_list_card_data(title, songs, options, cfg)
            if await self.reply_card_or_text(
                event,
                tpl_name="qqmusic-list",
                data=data,
                format_text=lambda d: text,
            ):
                return True
        await self.reply(event, text)
        return False

    async def send_detail_card(
        self,
        event: AstrMessageEvent,
        song: dict,
        play: dict,
        *,
        source: str,
        mv_vid: str = "",
        tip: str = "",
    ):
        q_label = play.get("qualityLabel") or play.get("quality") or ""
        cfg = self.cfg()
        card_data = cardlib.build_detail_card_data(
            song,
            quality_label=q_label,
            payplay=bool(song.get("payplay")),
            source=source,
            has_url=bool(play.get("url")),
            mv_vid=mv_vid,
            tip=tip,
            degrade_note=play.get("degradeNote") or "",
            error=play.get("error") or "",
            cfg=cfg,
            # weixin_oc 等平台出站不支持语音：tip 不能再写「正在发送语音…」
            no_vocal=_no_vocal(event),
        )
        url = await self.render_card(event, card_data, "qqmusic-detail")
        if url and await self.send_chain(event, Image.fromFileSystem(url)):
            return
        await self.reply(
            event,
            cardlib.format_detail_text(
                song, quality_label=q_label, has_url=bool(play.get("url"))
            ),
        )

    # ──────────── 选歌流程与公共操作 ────────────

    async def start_select(
        self,
        event: AstrMessageEvent,
        action: str,
        kw: str,
        *,
        label: str,
        verb: str,
        user_key: str,
    ) -> None:
        cfg = self.cfg()
        scope = self.scope(event)
        session = await cardlib.SessionStore.get(self.plugin, scope)
        if (kw or "").strip():
            try:
                page_size = max(1, min(safe_int(cfg.get("maxList") or 10, 10), 20))
                lst = await qqapi.search_songs(
                    kw, page_size=page_size, user_key=user_key
                )
            except Exception as err:
                self.log_warn(f"{label}选歌搜索失败: {err}")
                await self.reply(event, f"搜索失败：{err}")
                return
            if not lst:
                await self.reply(event, f"没有搜到「{kw}」")
                return
            keyword = kw
        else:
            lst = (session or {}).get("data") or []
            if not lst or not (
                isinstance(lst[0], dict) and lst[0].get("songmid")
            ):
                await self.reply(
                    event,
                    f"用法：先 #qqm点歌 关键词 选中歌曲，再发 #qqm{label}；或直接 #qqm{label} 关键词 选择",
                )
                return
            keyword = (session or {}).get("keyword") or "当前会话"
        base = dict(session) if session else {}
        base.update(
            {"type": "songs", "keyword": keyword, "data": lst, "action": action}
        )
        await cardlib.SessionStore.set(self.plugin, scope, base)
        listen_tip = msg.listen_reply_hint(verb)
        await self.reply_song_list(
            event,
            keyword,
            lst,
            tip=listen_tip,
            text_suffix=f"\n{listen_tip}",
        )

    async def show_album_tracks(
        self, event: AstrMessageEvent, alb: dict, tracks: list, scope: str, cfg: dict
    ) -> None:
        title = f"{alb.get('singerName', '')} - {alb.get('albumName', '')}"
        await cardlib.SessionStore.set(
            self.plugin,
            scope,
            {"type": "album", "data": tracks, "album": alb},
        )
        await self.reply_song_list(
            event,
            title,
            tracks,
            tip=f"发送 #qqm听序号 播放「{alb.get('albumName', '')}」，"
            "#qqm听所有 连播整张专辑",
        )
        if alb.get("publicTime"):
            await self.reply(event, f"发行时间：{alb['publicTime']}")

    async def expand_recommend(
        self, event: AstrMessageEvent, session: dict, n: int
    ) -> None:
        lst = session["data"]
        if n < 1 or n > len(lst):
            await self.reply(event, msg.out_of_range(len(lst)))
            event.stop_event()
            return
        pl = lst[n - 1]
        disstid = pl.get("disstid") or pl.get("dissid") or pl.get("tid")
        if not disstid:
            await self.reply(event, "该歌单缺少 ID，无法展开")
            event.stop_event()
            return
        try:
            detail = await qqapi.songlist_detail(disstid, self.user_key(event))
        except Exception as err:
            self.log_warn(f"推荐歌单展开失败: {err}")
            await self.reply(event, f"歌单展开失败：{err}")
            event.stop_event()
            return
        songs = detail.get("songlist") if isinstance(detail, dict) else []
        if not songs:
            await self.reply(event, "该歌单暂无歌曲")
            event.stop_event()
            return
        title = (
            detail.get("dissname")
            or pl.get("title")
            or pl.get("dissname")
            or "推荐歌单"
        )
        scope = self.scope(event)
        await cardlib.SessionStore.set(
            self.plugin,
            scope,
            {
                "type": "playlist",
                "data": songs,
            },
        )
        await self.reply_song_list(event, title, songs)
        event.stop_event()

    async def show_lyric_by_meta(
        self, event: AstrMessageEvent, songmid: str, song_meta: dict, user_key: str
    ) -> None:
        if not songmid:
            await self.reply(event, "该歌曲缺少 songmid，无法获取歌词")
            return
        try:
            data = await qqapi.lyric(songmid, user_key)
        except Exception as err:
            self.log_warn(f"歌词失败: {err}")
            await self.reply(event, f"歌词失败：{err}")
            return
        text = (data or {}).get("lyric") or ""
        lines = [
            re.sub(r"^\[[^\]]*]", "", ln).strip()
            for ln in text.split("\n")
            if ln.strip()
        ]
        lines = [ln for ln in lines if ln]
        if not lines:
            await self.reply(event, "暂无歌词")
            return
        pages = [
            lines[i : i + _LYRIC_PAGE_LINES]
            for i in range(0, len(lines), _LYRIC_PAGE_LINES)
        ]
        total = len(lines)
        for pi, page_lines in enumerate(pages):
            card = cardlib.build_lyric_card_data(
                song_name=song_meta.get("songName") or "",
                singer_name=song_meta.get("singerName") or "",
                cover=song_meta.get("cover") or "",
                album_name=song_meta.get("albumName") or "",
                songmid=songmid,
                lines=page_lines,
                cfg=self.cfg(),
            )
            if len(pages) > 1:
                card["tip"] = f"第 {pi + 1}/{len(pages)} 页 · 共 {total} 行"
            ok = await self.reply_card_or_text(
                event,
                tpl_name="qqmusic-lyric",
                data=card,
                format_text=cardlib.format_lyric_text,
            )
            if not ok:
                break

    async def show_comment(
        self, event: AstrMessageEvent, song: dict, user_key: str
    ) -> None:
        songid = song.get("songid") or song.get("songId") or ""
        if not songid:
            await self.reply(event, "未能获取歌曲ID，无法查询评论")
            return
        try:
            result = await qqapi.comment(
                songid, page_size=20, user_key=user_key
            )
        except Exception as err:
            self.log_warn(f"评论失败: {err}")
            await self.reply(event, "评论获取失败，请稍后重试")
            return
        hot = (
            (result.get("hot_comment") or {}).get("commentlist")
            if isinstance(result.get("hot_comment"), dict)
            else result.get("hot_comment") or []
        )
        normal = (
            (result.get("comment") or {}).get("commentlist")
            if isinstance(result.get("comment"), dict)
            else result.get("comment") or []
        )
        all_comments = (hot if isinstance(hot, list) else []) + (
            normal if isinstance(normal, list) else []
        )
        if not all_comments:
            await self.reply(
                event,
                f"【{song.get('songName')} - {song.get('singerName')}】\n暂无评论",
            )
            return
        comment_lines = []
        for i, c in enumerate(all_comments[:10]):
            nick = c.get("nick") or c.get("nickname") or "匿名"
            text = cardlib.clean_comment_text(
                c.get("rootcommentcontent")
                or c.get("middlecommentcontent")
                or c.get("content")
                or c.get("comment")
            )
            likes = c.get("praisenum") or c.get("likeCount") or 0
            comment_lines.append(f"{i + 1}. {nick}（{likes}赞）：{str(text)[:80]}")
        if self.cfg().get("renderListCard", True):
            card = cardlib.build_comment_card_data(
                song_name=song.get("songName", ""),
                singer_name=song.get("singerName", ""),
                cover=song.get("cover") or "",
                album_name=song.get("albumName") or "",
                songmid=song.get("songmid") or "",
                comments=all_comments[:10],
                cfg=self.cfg(),
            )
            header = [
                f"♫ {song.get('songName')} - {song.get('singerName')} 热门评论",
                "",
            ]
            if await self.reply_card_or_text(
                event,
                tpl_name="qqmusic-comment",
                data=card,
                format_text=lambda d: "\n".join(header + comment_lines),
            ):
                return
        await self.reply(
            event,
            "\n".join(
                [
                    f"♫ {song.get('songName')} - {song.get('singerName')} 热门评论",
                    "",
                ]
                + comment_lines
            ),
        )

    async def deliver_mv(
        self,
        event: AstrMessageEvent,
        mv_obj: dict,
        *,
        download: bool,
        user_key: str,
    ) -> bool:
        try:
            url = await qqapi.mv_url(mv_obj["vid"], user_key)
            if not url:
                await self.reply(event, "获取 MV 播放链接失败，可能需 #qqm登录")
                return False
        except Exception as err:
            await self.reply(event, f"获取 MV 播放链接失败：{err}")
            return False

        passive_limited = _is_passive_limited(event)
        img_path = ""
        try:
            card = cardlib.build_mv_card_data(mv_obj, cfg=self.cfg())
            img_path = await self.render_card(event, card, "qqmusic-detail") or ""
            if img_path and not passive_limited:
                if not await self.send_chain(event, Image.fromFileSystem(img_path)):
                    # 图片被平台拒绝：补一条 MV 文本信息，别让用户只拿到链接
                    await self.reply(event, cardlib.format_mv_text(mv_obj))
            elif not img_path:
                await self.reply(event, cardlib.format_mv_text(mv_obj))
        except Exception:
            await self.reply(event, cardlib.format_mv_text(mv_obj))

        ret = await deliver_video(
            self.plugin,
            event,
            mv_obj,
            url,
            cfg=self.cfg(),
            download=download,
            extra=[Image.fromFileSystem(img_path)]
            if (img_path and passive_limited)
            else None,
        )
        if not ret.get("ok"):
            fp = ret.get("filePath") or ""
            # reason == "send_fail" 表示 deliver_video 内部已经把「发文件 + aiocq base64」
            # 都试过且都失败；这里再发同一份文件只会重复读盘/编码（大视频要整份读进内存），
            # 直接回退成链接。
            # 例外：受限平台下那两次发送都夹带了卡片图（extra），失败原因可能是**图片**而非
            # 视频文件，此时保留「只发文件」的重试仍然有意义。
            if ret.get("reason") == "send_fail" and not (img_path and passive_limited):
                fp = ""
            sent_file = False
            if fp and os.path.exists(fp):
                title_f = mv_obj.get("mvtitle") or mv_obj.get("name") or "MV"
                disp_name = re.sub(r'[\\/:*?"<>|]', "", str(title_f))[:30] + ".mp4"
                try:
                    if _is_aiocqhttp(event):
                        # aiocqhttp 跨容器 file:// 不可见 → base64 直发（同音频文件通道）
                        # （该通道失败会抛异常，由下面的 except 处理）
                        await _aiocq_send_file(event, "", disp_name, fp)
                        sent_file = True
                    else:
                        # 必须看发送结果，否则会回复「已改发下载文件」但实际什么都没发出去
                        sent_file = await self.send_chain(
                            event, File(disp_name, file=fp)
                        )
                    _schedule_cleanup(
                        fp,
                        safe_int(
                            self.cfg().get("keepFileSec"), _DEFAULT_KEEP_FILE_SEC
                        ),
                    )
                except Exception:
                    pass
            if sent_file:
                await self.reply(event, "视频消息发送失败，已改发下载文件")
            else:
                await self.reply(event, f"已改为链接发送（可在线播放/下载）：{url}")
        return True

    async def send_singer_desc(
        self, event: AstrMessageEvent, singer: dict, user_key: str
    ):
        try:
            desc = await qqapi.singer_desc(singer["singermid"], user_key)
            d = desc.get("desc") if isinstance(desc, dict) else None
            if d:
                brief = str(d)[:200]
                suffix = "..." if len(d) > 200 else ""
                await self.reply(
                    event, f"【{singer.get('singerName', '')}】{brief}{suffix}"
                )
        except Exception:
            pass

    # ──────────── 分享与卡片解析（实现见 core/resolve.py） ────────────

    async def handle_resolve(
        self, event: AstrMessageEvent, text: str, cfg: dict
    ) -> bool:
        return await resolver.handle_resolve(
            self, event, text, cfg, skip_options=_SKIP_TEXT
        )

    async def ids_to_song(
        self, ids: dict, fallback_text: str = "", user_key: str = ""
    ) -> dict | None:
        return await resolver.ids_to_song(self, ids, fallback_text, user_key)

    # ──────────── 扫码登录（实现见 core/login.py） ────────────

    def stop_poll(self, user_id: str):
        loginflow.stop_poll(self, user_id)

    @staticmethod
    def _prune_jobs(task: dict):
        loginflow.prune_jobs(task)

    def register_task(self, user_id: str, task: dict):
        loginflow.register_task(self, user_id, task)

    def pop_task_if_current(self, user_id: str, task: dict):
        loginflow.pop_task_if_current(self, user_id, task)

    async def save_qr_image(self, base64_str: str) -> str:
        return await loginflow.save_qr_image(base64_str)

    async def pick_login_success(self, body: dict) -> dict | None:
        return await loginflow.pick_login_success(body)

    async def on_login_success(
        self, event: AstrMessageEvent, info: dict | None = None
    ):
        await loginflow.on_login_success(self, event, info)

    def start_poll(
        self,
        event: AstrMessageEvent,
        qrcode_id: str,
        expires_in: int,
        poll_interval: float = _POLL_INTERVAL_SEC,
    ):
        loginflow.start_qr_poll(self, event, qrcode_id, expires_in, poll_interval)

    def start_webqr_poll(
        self, event: AstrMessageEvent, session_id: str, expires_in: int
    ):
        loginflow.start_webqr_poll(self, event, session_id, expires_in)

    # ──────────── 状态卡 / 设置卡数据（实现见 core/panels.py） ────────────

    async def build_status_data(self, user_key: str = "") -> dict:
        return await panels.build_status_data(self, user_key)

    def platform_name(self, event: AstrMessageEvent) -> str:
        return panels.platform_name(event)

    async def build_settings_data(self, event: AstrMessageEvent) -> dict:
        return await panels.build_settings_data(self, event)

    async def terminate(self):
        for uid in list(self._active_logins.keys()):
            self.stop_poll(uid)
        # 取消全部 fire-and-forget 后台任务与待执行的临时文件清理定时器
        tasks = [t for t in self._bg_tasks if not t.done()]
        for t in tasks:
            with contextlib.suppress(Exception):
                t.cancel()
        # cancel() 只投递取消请求、不等待协程收尾：必须先 gather 让被取消的任务把
        # 自己的清理分支跑完（例如 download_audio 删除半截文件），再关闭共享会话。
        # 否则在途下载会在会话关闭后才抛 "Session is closed" 噪声，且半截临时文件
        # 永久残留（CancelledError 在 3.8+ 属 BaseException，不会落 except Exception）。
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()
        cancel_cleanup_timers()
        # 关闭复用的 aiohttp 会话，避免热重载后残留连接
        await qqapi.close_session()
        # 关闭常驻 Chromium，避免热重载后残留浏览器进程
        with contextlib.suppress(Exception):
            from .render import close as close_render

            await close_render()
