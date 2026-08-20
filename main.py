from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin, urlparse

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star

from . import api as qqapi
from . import cards as cardlib
from .delivery import (
    _is_passive_limited,
    _schedule_cleanup,
    deliver_song,
    deliver_video,
    send_native_music_card,
)
from .quality import QUALITY_LABEL

PLUGIN_DIR = str(Path(__file__).resolve().parent)

# 播放投递时跳过全部附加卡片/文案（文本+原生卡+自定义卡）
_SKIP_ALL = {
    "skipTextInfo": True,
    "skipNativeCard": True,
    "skipCustomCard": True,
}
# 仅跳过文本信息（详情卡已含歌名/歌手/音质），原生/自定义音乐卡按配置发送
_SKIP_TEXT = {"skipTextInfo": True}


def _get_local_version(plugin_dir: str) -> str:
    """从 metadata.yaml 读取插件版本号（用于帮助卡片/配置面板展示）。"""
    try:
        import yaml

        with open(
            os.path.join(plugin_dir, "metadata.yaml"), "r", encoding="utf-8"
        ) as f:
            meta = yaml.safe_load(f) or {}
        return str(meta.get("version", "?")).lstrip("v")
    except Exception:
        return "?"


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# QQ 音乐相关域名白名单（短链重定向仅允许这些 host，防 SSRF）—— 对齐 JS 版 resolve.js
_QQ_HOST_SUFFIXES = ("y.qq.com", "qq.com", "gtimg.cn", "url.cn", "qpic.cn")


def _is_qq_host(hostname: str = "") -> bool:
    host = str(hostname or "").strip().lower()
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in _QQ_HOST_SUFFIXES)


async def _follow_qq_redirect(url: str, max_redirects: int = 5) -> str:
    """跟随 c6.y.qq.com 短链重定向（SSRF 白名单内），拿最终 songDetail 链接；失败回退原链接。

    最后一跳同样校验 host，避免把非白名单 URL 交回上层解析（SSRF 加固）。
    """
    current = url
    hops = 0
    while True:
        try:
            parsed = urlparse(current)
            if not _is_qq_host(parsed.hostname):
                return url
        except Exception:
            return url
        try:
            async with aiohttp.ClientSession(
                headers={"User-Agent": qqapi.UA},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as sess, sess.get(current, allow_redirects=False) as res:
                if res.status in (301, 302, 303, 307, 308):
                    location = res.headers.get("Location")
                    if not location:
                        return current
                    next_url = urljoin(current, location)
                    if hops >= max_redirects:
                        # 最后一跳也校验 host，避免把非白名单 URL 交回上层解析
                        try:
                            if not _is_qq_host(urlparse(next_url).hostname):
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


def _is_plugin_command_msg(msg: str) -> bool:

    # 注意 m 后不能用 \b（Python 的 \w 含 CJK，\b 对「#qqm点歌 xxx」不生效）：
    # 排除的只是 ASCII 字母数字/下划线，CJK 后缀（点歌/歌词…）与空格、结尾均通过
    return bool(
        re.match(
            r"^#?(qq|QQ)m(?![\dA-Za-z_])|^#?(qq|QQ)音乐|^#听\s*[1-9]|^#qm帮助",
            str(msg or "").strip(),
            re.IGNORECASE,
        )
    )


def _is_qqmusic_message(text: str) -> bool:
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


def _collect_message_text(event: AstrMessageEvent) -> str:

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
            else:
                # 其它组件（含可能的 JSON/分享）序列化
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


class QQMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 注入配置访问器给 api 模块
        qqapi.set_config_getter(lambda: self.config or {})

    # ──────────── 辅助 ────────────

    def _cfg(self) -> dict:
        return self.config or {}

    def _log_warn(self, msg: str):
        logger.warning(f"[qqmusic] {msg}")

    def _log_info(self, msg: str):
        logger.info(f"[qqmusic] {msg}")

    def _plain(self, text: str) -> Plain:
        return Plain(text=text)

    async def _send_chain(self, event: AstrMessageEvent, *components):

        comps = [c for c in components if c is not None]
        if not comps:
            return
        mc = MessageChain(chain=list(comps))
        # 防御：适配器内部对组件处理出错时（如 'Plain' object has no attribute 'chain'），
        # 捕获并记录完整堆栈，避免整个 handler 崩溃；再降级为纯文本兜底重发一次。
        try:
            await event.send(mc)
        except AttributeError:
            import traceback as _tb

            self._log_warn(
                f"_send_chain 发送失败（AttributeError）:\n{_tb.format_exc()}"
            )
            texts = []
            for _c in comps:
                t = getattr(_c, "text", None)
                if t:
                    texts.append(str(t))
            if texts:
                try:
                    await event.send(
                        MessageChain(chain=[self._plain("\n".join(texts))])
                    )
                except Exception as _e2:
                    self._log_warn(f"_send_chain 文本兜底也失败: {_e2}")
            # 纯媒体组件（如仅 Image）：无法兜底成文本，仅记录

    async def _reply(self, event: AstrMessageEvent, text: str):
        try:
            await self._send_chain(event, self._plain(text))
        except Exception as e:
            import traceback as _tb

            self._log_warn(f"_reply 发送失败: {e}\n{_tb.format_exc()}")

    def _scope(self, event: AstrMessageEvent) -> str:
        gid = getattr(event.message_obj, "group_id", None)
        if gid:
            return str(gid)
        return event.get_sender_id()

    def _user_key(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "")

    def _quality_label(self, q: str) -> str:
        return QUALITY_LABEL.get(q, q or "")

    @staticmethod
    def _play_view(play: dict) -> dict:
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

    async def _resolve_play(self, song: dict, cfg: dict, user_key: str = "") -> dict:
        quality = cfg.get("quality") or "flac"
        fallback = cfg.get("qualityFallback", True) is not False
        try:
            play = await qqapi.song_url_best(
                song["songmid"],
                quality=quality,
                media_id=song.get("media_mid") or song.get("songmid"),
                fallback=fallback,
                user_key=user_key,
            )
            return self._play_view(play)
        except Exception as e:
            return {
                "url": "",
                "quality": quality,
                "qualityLabel": self._quality_label(quality),
                "error": str(e),
                "raw": getattr(e, "payload", None),
            }

    async def _render_card(
        self, event: AstrMessageEvent, data: dict, tpl_name: str
    ) -> str | None:

        try:
            from jinja2 import Environment

            from .delivery import get_temp_dir
            from .render import render_html_to_png
            from .tpl_adapter import get_jinja_template

            tmpl_path = os.path.join(
                PLUGIN_DIR, "resources", "html", tpl_name, f"{tpl_name}.html"
            )
            if not os.path.exists(tmpl_path):
                return None
            tmpl = get_jinja_template(tmpl_path)
            # noqa: S701 模板为插件自有文件，且部分值需保留 HTML（如搜索命中高亮），不开启 autoescape
            html = Environment().from_string(tmpl).render(data=data)  # noqa: S701
            # 本地 playwright 直渲：元素截图自带背景与裁剪，无需远程 t2i 服务
            d = get_temp_dir(self._cfg(), PLUGIN_DIR)
            file_path = os.path.join(
                d, f"card_{tpl_name}_{int(time.time() * 1000)}.png"
            )
            if not await render_html_to_png(html, file_path):
                self._log_warn(
                    f"{tpl_name} 渲染失败"
                    "（playwright 不可用？请确认已安装并执行 playwright install chromium）"
                )
                return None
            # 卡片 PNG 发送后延迟清理，防止 temp 目录无限增长
            loop = asyncio.get_event_loop()
            loop.call_later(120, lambda p=file_path: self._safe_unlink(p))
            return file_path
        except Exception as e:
            self._log_warn(f"{tpl_name} 渲染失败: {e}")
            return None

    async def _reply_card_or_text(
        self,
        event: AstrMessageEvent,
        *,
        tpl_name: str,
        data: dict,
        format_text,
    ):

        try:
            url = await self._render_card(event, data, tpl_name)
            if url:
                await self._send_chain(event, Image.fromFileSystem(url))
                return True
        except Exception as e:
            self._log_warn(f"{tpl_name} 卡片渲染失败，回退文本: {e}")
        try:
            text = format_text(data)
            if text:
                await self._send_chain(event, self._plain(text))
                return True
        except Exception as e:
            self._log_warn(f"{tpl_name} 文本兜底失败: {e}")
        return False

    # ══════════════════ 点歌 ══════════════════

    @filter.regex(r"^#?(qq|QQ)m\s*点歌\s*(.+)$")
    async def pick_song(self, event: AstrMessageEvent):
        """#qqm点歌 关键词 搜索并展示歌曲列表（带MV徽标）"""


        cfg = self._cfg()
        if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*点歌\s*(.+)$", event.message_str.strip(), re.IGNORECASE
        )
        keyword = m.group(1).strip() if m else ""
        if not keyword:
            await self._reply(event, "用法：#qqm点歌 关键词")
            return
        try:
            await self._reply(event, f"正在搜索：{keyword}")
            page_size = min(int(cfg.get("maxList") or 10), 20)
            lst = await qqapi.search_songs(keyword, page_size=page_size)
            if not lst:
                await self._reply(event, "没有搜到相关歌曲")
                return
            # 批量查各曲是否带 MV（一次 /song/info），列表打 🎬 徽标 + 支持 #qqmMV 播放 序号
            try:
                mids = [s["songmid"] for s in lst if s.get("songmid")]
                infos = await qqapi.song_info_batch(
                    mids, user_key=self._user_key(event)
                )
                mv_map = {
                    n["songmid"]: n.get("mvVid")
                    for n in infos
                    if n.get("songmid") and n.get("mvVid")
                }
                for s in lst:
                    if s.get("songmid") and s["songmid"] in mv_map:
                        s["mvVid"] = mv_map[s["songmid"]]
            except Exception:
                pass  # MV 徽标失败不影响点歌
            scope = self._scope(event)
            await cardlib.SessionStore.set(
                self,
                scope,
                {"keyword": keyword, "data": lst},
            )
            if cfg.get("renderListCard", True):
                data = cardlib.build_list_card_data(keyword, lst, cfg=self._cfg())
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-list",
                    data=data,
                    format_text=lambda d: cardlib.format_list_text(lst),
                ):
                    return
            await self._reply(event, cardlib.format_list_text(lst))
        except Exception as err:
            self._log_warn(f"点歌失败: {err}")
            await self._reply(event, f"点歌失败：{err}")
        finally:
            event.stop_event()

    async def _start_select(
        self,
        event: AstrMessageEvent,
        action: str,
        kw: str,
        *,
        label: str,
        verb: str,
        user_key: str,
    ) -> None:
        """进入"先选歌再操作"流程。

        带关键词→搜索出候选列表；不带→复用会话列表（无列表则提示先搜）。
        列表写入会话并记录 ``action``（#qqm听N 消费后一次性执行，用完恢复播放）。
        """
        cfg = self._cfg()
        scope = self._scope(event)
        session = await cardlib.SessionStore.get(self, scope)
        if (kw or "").strip():
            try:
                page_size = min(int(cfg.get("maxList") or 10), 20)
                lst = await qqapi.search_songs(
                    kw, page_size=page_size, user_key=user_key
                )
            except Exception as err:  # noqa: BLE001  # 防御：搜索失败不崩 handler
                self._log_warn(f"{label}选歌搜索失败: {err}")
                await self._reply(event, f"搜索失败：{err}")
                return
            if not lst:
                await self._reply(event, f"没有搜到「{kw}」")
                return
            keyword = kw
        else:
            lst = (session or {}).get("data") or []
            # 会话非歌曲列表（如 MV 列表 / 榜单分类）时不可复用，提示先搜
            if not lst or not (
                isinstance(lst[0], dict) and lst[0].get("songmid")
            ):
                await self._reply(
                    event,
                    f"用法：先 #qqm点歌 关键词 选中歌曲，再发 #qqm{label}；或直接 #qqm{label} 关键词 选择",
                )
                return
            keyword = (session or {}).get("keyword") or "当前会话"
        base = dict(session) if session else {}
        base.update(
            {"type": "songs", "keyword": keyword, "data": lst, "action": action}
        )
        await cardlib.SessionStore.set(self, scope, base)
        tip = f"回复 #qqm听N 即可{verb}"
        if cfg.get("renderListCard", True):
            data = cardlib.build_list_card_data(
                keyword, lst, options={"tip": tip}, cfg=cfg
            )
            if await self._reply_card_or_text(
                event,
                tpl_name="qqmusic-list",
                data=data,
                format_text=lambda d: cardlib.format_song_list(lst, keyword)
                + f"\n回复 #qqm听N 即可{verb}",
            ):
                return
        await self._reply(
            event,
            cardlib.format_song_list(lst, keyword)
            + f"\n回复 #qqm听N 即可{verb}",
        )

    @filter.regex(r"^#?(qq|QQ)m\s*(推荐)?听\s*([1-9][0-9]?)$|^#听\s*([1-9][0-9]?)$")
    async def choose_song(self, event: AstrMessageEvent):
        """#qqm听N / #qqm推荐听N 播放当前会话列表第 N 首"""


        cfg = self._cfg()
        if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*(推荐)?听\s*([1-9][0-9]?)$|^#听\s*([1-9][0-9]?)$",
            event.message_str.strip(),
            re.IGNORECASE,
        )
        n = 0
        want_recommend = False
        if m:
            want_recommend = bool(m.group(1))  # #qqm推荐听N 变体
            n = int(m.group(2) or m.group(3) or 0)
        scope = self._scope(event)
        session = await cardlib.SessionStore.get(self, scope)
        if not session or not session.get("data"):
            # 无本插件会话时不抢其它插件的 #听
            return
        stype = session.get("type") or "pick"  # 点歌会话未写 type
        if stype == "mvList":
            # MV 列表会话：音频播放流程不适用，放行
            return
        if want_recommend and stype != "recommend":
            return  # 推荐听只作用于推荐歌单会话，不抢其它
        if stype == "topCategory":
            # 榜单分类会话的 data 是分类对象，不是歌曲
            await self._reply(event, "该会话是榜单分类，请先 #qqm排行 榜单名 查看歌曲")
            event.stop_event()
            return
        if stype == "recommend":
            # 推荐歌单会话：展开所选歌单（原「#qqm推荐听序号」提示是死指令）
            await self._expand_recommend(event, session, n)
            return
        if n < 1 or n > len(session["data"]):
            await self._reply(event, f"请选择 1-{len(session['data'])}")
            event.stop_event()
            return
        song = session["data"][n - 1]
        user_key = self._user_key(event)
        action = session.get("action") or "play"
        # 待办动作一次性消费：先清掉 action，避免下次 #qqm听N 误触发
        if action != "play":
            try:
                _s = dict(session)
                _s["action"] = "play"
                await cardlib.SessionStore.set(self, scope, _s)
            except Exception:  # noqa: S110, BLE001  # 消费失败不影响本次执行
                pass
        if action != "play":
            try:
                if action == "lyric":
                    await self._show_lyric(event, song, user_key)
                elif action == "comment":
                    await self._show_comment(event, song, user_key)
                elif action == "mv":
                    await self._show_mv(event, song, user_key)
                else:
                    await self._reply(event, f"未知动作：{action}")
            except Exception as err:  # noqa: BLE001  # 防御：动作执行失败不崩 handler
                self._log_warn(f"执行失败: {err}")
                await self._reply(event, f"操作失败：{err}")
            event.stop_event()
            return
        play = await self._resolve_play(song, cfg, user_key)
        # 记住本曲 MV，支持「#qqmMV 播放/下载」（不带参数）直接操作该曲 MV
        mv_vid = play.get("mvVid") or ""
        if mv_vid and session.get("data"):
            await cardlib.SessionStore.set(
                self,
                scope,
                {**session, "lastMvVid": mv_vid},
            )
        await self._send_detail_card(event, song, play, source="点歌", mv_vid=mv_vid)
        if not play.get("url"):
            if cfg.get("sendNativeCard") and song.get("songid"):
                await send_native_music_card(event, "qq", song["songid"])
            event.stop_event()
            return
        await deliver_song(
            self,
            event,
            song,
            play,
            cfg=self._cfg(),
            plugin_dir=PLUGIN_DIR,
            options=_SKIP_TEXT,
        )
        event.stop_event()

    async def _expand_recommend(self, event: AstrMessageEvent, session: dict, n: int):


        lst = session["data"]
        if n < 1 or n > len(lst):
            await self._reply(event, f"请选择 1-{len(lst)}")
            event.stop_event()
            return
        pl = lst[n - 1]
        disstid = pl.get("disstid") or pl.get("dissid") or pl.get("tid")
        if not disstid:
            await self._reply(event, "该歌单缺少 ID，无法展开")
            event.stop_event()
            return
        try:
            detail = await qqapi.songlist_detail(disstid, self._user_key(event))
        except Exception as err:
            self._log_warn(f"推荐歌单展开失败: {err}")
            await self._reply(event, f"歌单展开失败：{err}")
            event.stop_event()
            return
        songs = detail.get("songlist") if isinstance(detail, dict) else []
        if not songs:
            await self._reply(event, "该歌单暂无歌曲")
            event.stop_event()
            return
        title = (
            detail.get("dissname")
            or pl.get("title")
            or pl.get("dissname")
            or "推荐歌单"
        )
        scope = self._scope(event)
        await cardlib.SessionStore.set(
            self,
            scope,
            {
                "type": "playlist",
                "data": songs,
            },
        )
        cfg = self._cfg()
        if cfg.get("renderListCard", True):
            data = cardlib.build_list_card_data(title, songs, cfg=cfg)
            if await self._reply_card_or_text(
                event,
                tpl_name="qqmusic-list",
                data=data,
                format_text=lambda d: cardlib.format_song_list(songs, title),
            ):
                event.stop_event()
                return
        await self._reply(event, cardlib.format_song_list(songs, title))
        event.stop_event()

    @filter.regex(r"^#?(qq|QQ)m\s*播放\s*(.+)$")
    async def play_direct(self, event: AstrMessageEvent):
        """#qqm播放 关键词 直接播放第一条"""


        cfg = self._cfg()
        if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*播放\s*(.+)$", event.message_str.strip(), re.IGNORECASE
        )
        keyword = m.group(1).strip() if m else ""
        if not keyword:
            await self._reply(event, "用法：#qqm播放 关键词")
            event.stop_event()
            return
        try:
            lst = await qqapi.search_songs(keyword, page_size=1)
            if not lst:
                await self._reply(event, "没有搜到相关歌曲")
                event.stop_event()
                return
            song = lst[0]
            user_key = self._user_key(event)
            play = await self._resolve_play(song, cfg, user_key)
            await self._send_detail_card(
                event, song, play, source="播放", mv_vid=play.get("mvVid") or ""
            )
            if not play.get("url"):
                if cfg.get("sendNativeCard") and song.get("songid"):
                    await send_native_music_card(event, "qq", song["songid"])
                event.stop_event()
                return
            await deliver_song(
                self,
                event,
                song,
                play,
                cfg=self._cfg(),
                plugin_dir=PLUGIN_DIR,
                options=_SKIP_TEXT,
            )
        except Exception as err:
            await self._reply(event, f"播放失败：{err}")
        event.stop_event()

    async def _send_detail_card(
        self,
        event: AstrMessageEvent,
        song: dict,
        play: dict,
        *,
        source: str,
        mv_vid: str = "",
    ):

        q_label = play.get("qualityLabel") or play.get("quality") or ""
        card_data = cardlib.build_detail_card_data(
            song,
            quality_label=q_label,
            payplay=bool(song.get("payplay")),
            source=source,
            has_url=bool(play.get("url")),
        )
        if play.get("url"):
            tip = f"正在下载并发送语音（{q_label or '默认音质'}）..."
        else:
            err_txt = play["error"] if play.get("error") else ""
            tip = f"获取播放链接失败{('：' + err_txt) if err_txt else ''}\n请 #qqm登录"
        if play.get("degradeNote"):
            tip += f" · {play['degradeNote']}"
        if mv_vid:
            tip += " · 🎬 该曲有 MV：#qqmMV 播放/下载 直接操作"
        card_data["tip"] = tip
        url = await self._render_card(event, card_data, "qqmusic-detail")
        if url:
            await self._send_chain(event, Image.fromFileSystem(url))
            return
        await self._reply(
            event,
            cardlib.format_detail_text(
                song, quality_label=q_label, has_url=bool(play.get("url"))
            ),
        )

    # ══════════════════ 歌词/热搜/帮助 ══════════════════

    @filter.regex(r"^#?(qq|QQ)m\s*歌词\s*(.*)$")
    async def get_lyric(self, event: AstrMessageEvent):
        """#qqm歌词 [关键词]：先选歌（回复 #qqm听N）再显示歌词"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*歌词\s*(.*)$", event.message_str.strip(), re.IGNORECASE
        )
        if not m:
            return
        await self._start_select(
            event,
            "lyric",
            m.group(1).strip(),
            label="歌词",
            verb="查看歌词",
            user_key=self._user_key(event),
        )
        event.stop_event()

    async def _show_lyric(self, event: AstrMessageEvent, song: dict, user_key: str) -> None:
        """显示所选歌曲的歌词（#qqm听N 触发）。"""
        songmid = song.get("songmid") or song.get("songMid") or ""
        if not songmid:
            await self._reply(event, "该歌曲缺少 songmid，无法获取歌词")
            return
        try:
            data = await qqapi.lyric(songmid, user_key)
        except Exception as err:  # noqa: BLE001  # 防御：歌词失败不崩 handler
            self._log_warn(f"歌词失败: {err}")
            await self._reply(event, f"歌词失败：{err}")
            return
        text = (data or {}).get("lyric") or ""
        lines = [
            re.sub(r"^\[[^\]]*]", "", ln).strip()
            for ln in text.split("\n")
            if ln.strip()
        ]
        lines = [ln for ln in lines if ln][:40]
        if not lines:
            await self._reply(event, "暂无歌词")
            return
        song_name = song.get("songName") or ""
        singer_name = song.get("singerName") or ""
        card = cardlib.build_lyric_card_data(
            song_name=song_name,
            singer_name=singer_name,
            cover=song.get("cover") or "",
            album_name=song.get("albumName") or "",
            songmid=songmid,
            lines=lines,
            cfg=self._cfg(),
        )
        await self._reply_card_or_text(
            event,
            tpl_name="qqmusic-lyric",
            data=card,
            format_text=cardlib.format_lyric_text,
        )

    @filter.regex(r"^#?(qq|QQ)m\s*热搜$")
    async def hot_search(self, event: AstrMessageEvent):
        """#qqm热搜 查看热搜榜"""


        try:
            lst = await qqapi.hot_keys(self._user_key(event))
            tops = (lst if isinstance(lst, list) else [])[:15]
            if not tops:
                await self._reply(event, "暂无热搜")
                event.stop_event()
                return
            data = cardlib.build_hot_card_data(tops, cfg=self._cfg())
            await self._reply_card_or_text(
                event,
                tpl_name="qqmusic-hot",
                data=data,
                format_text=lambda d: cardlib.format_hot_text(tops),
            )
        except Exception as err:
            await self._reply(event, f"热搜失败：{err}")
        event.stop_event()

    @filter.regex(
        r"^#?(qq|QQ)m\s*help$|^#?(qq|QQ)m\s*帮助$|^#?(qq|QQ)音乐帮助$|^#qm帮助$"
    )
    async def help(self, event: AstrMessageEvent):
        """#qqm帮助 帮助图片卡片"""


        cfg = self._cfg()
        try:
            version = _get_local_version(PLUGIN_DIR)
            data = cardlib.build_help_card_data(cfg=self._cfg(), version=version)
            url = await self._render_card(event, data, "qqmusic-help")
            if url:
                await self._send_chain(event, Image.fromFileSystem(url))
                event.stop_event()
                return
        except Exception as err:
            self._log_warn(f"帮助图渲染失败: {err}")
        await self._reply(
            event,
            "\n".join(
                [
                    "【QQ音乐插件帮助】",
                    "— 点歌（均需 #qqm 前缀）—",
                    "#qqm点歌 七里香  →  #qqm听1（会话内也可 #听1）",
                    "#qqm播放 晴天",
                    "#qqm歌词 七里香  /  #qqm热搜",
                    "— MV —",
                    "#qqmMV 搜索 周杰伦  →  #qqmMV 播放1 / 下载1",
                    "#qqmMV（分类浏览）；点歌后再发 #qqmMV 播放/下载 = 本曲 MV",
                    "— 状态 —",
                    "#qqm登录 / #qqm登录微信 / #qqm登录qq / #qqm状态  /  #qms  /  #qqm登出",
                    "— 管理（主人）—",
                    "#qqm设置  /  #qqm 音质 flac  /  #qqm 测试",
                    "— 解析 —",
                    "分享 QQ 音乐卡片或 y.qq.com 链接自动解析",
                    f"API: {'已配置' if cfg.get('apiBase') else '未配置'}",
                ]
            ),
        )
        event.stop_event()

    # ══════════════════ 排行/推荐/电台/日推/收藏 ══════════════════

    @filter.regex(r"^#?(qq|QQ)m\s*排行\s*(.*)$")
    async def chart(self, event: AstrMessageEvent):
        """#qqm排行 榜单名 查看排行榜（飙升/热歌/新歌等）"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*排行\s*(.*)$", event.message_str.strip(), re.IGNORECASE
        )
        keyword = (m.group(1) if m else "").strip()
        scope = self._scope(event)
        user_key = self._user_key(event)
        if not keyword:
            try:
                groups = await qqapi.top_category(user_key)
                if not groups:
                    await self._reply(event, "获取排行榜失败")
                    event.stop_event()
                    return
                all_tops = []
                lines = ["♫ QQ音乐排行榜"]
                for g in groups:
                    lines.append(f"【{g.get('title', '')}】")
                    for t in g.get("list") or []:
                        all_tops.append({**t, "group": g.get("title")})
                        lines.append(
                            f"  {t.get('label', '')}（#qqm排行 {t.get('label', '')}）"
                        )
                lines.append("\n发送 #qqm排行 榜单名 查看具体榜单")
                await cardlib.SessionStore.set(
                    self,
                    scope,
                    {
                        "type": "topCategory",
                        "data": all_tops,
                    },
                )
                await self._reply(event, "\n".join(lines))
            except Exception as err:
                self._log_warn(f"排行失败: {err}")
                await self._reply(event, "排行失败，请稍后重试")
            event.stop_event()
            return
        try:
            groups = await qqapi.top_category(user_key)
            all_tops = [t for g in groups for t in (g.get("list") or [])]
            match = (
                next(
                    (t for t in all_tops if t.get("label") and keyword in t["label"]),
                    None,
                )
                or next(
                    (t for t in all_tops if t.get("label") and t["label"] in keyword),
                    None,
                )
                or next(
                    (
                        t
                        for t in all_tops
                        if t.get("topId") and str(t["topId"]) == keyword
                    ),
                    None,
                )
            )
            if not match:
                names = "、".join(
                    t.get("label", "") for t in all_tops if t.get("label")
                )[:200]
                await self._reply(event, f"未找到「{keyword}」\n可用榜单：{names}")
                event.stop_event()
                return
            await self._reply(event, f"正在获取 {match.get('label')}...")
            detail = await qqapi.top_detail(match["topId"], user_key=user_key)
            # 对应 JS chart.js: detail.list || detail.data?.list || []（避免非 dict 时 .get 抛错）
            if isinstance(detail, dict):
                songs_raw = (
                    detail.get("list") or (detail.get("data") or {}).get("list") or []
                )
            else:
                songs_raw = []
            songs = [self._normalize_song(s, i) for i, s in enumerate(songs_raw or [])]
            songs = [s for s in songs if s]
            if not songs:
                await self._reply(event, "该榜单暂无数据")
                event.stop_event()
                return
            await cardlib.SessionStore.set(
                self,
                scope,
                {
                    "type": "top",
                    "data": songs,
                },
            )
            if cfg.get("renderListCard", True):
                data = cardlib.build_list_card_data(
                    match.get("label", ""), songs, cfg=self._cfg()
                )
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-list",
                    data=data,
                    format_text=lambda d: cardlib.format_song_list(
                        songs, match.get("label", "")
                    ),
                ):
                    event.stop_event()
                    return
            await self._reply(
                event, cardlib.format_song_list(songs, match.get("label", ""))
            )
        except Exception as err:
            self._log_warn(f"排行失败: {err}")
            await self._reply(event, "排行失败，请稍后重试")
        event.stop_event()

    def _normalize_song(self, item: dict, idx: int = 0) -> dict | None:
        if not isinstance(item, dict):
            return None
        singer = qqapi.singer_text(item)
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        albummid = item.get("albummid") or album.get("mid") or ""
        interval = _safe_int(item.get("interval") or item.get("songTime") or 0)
        duration = f"{interval // 60:02d}:{interval % 60:02d}" if interval > 0 else ""
        payplay = qqapi.payplay_of(item)
        return {
            "index": idx + 1,
            "songmid": item.get("songmid") or item.get("mid") or "",
            "songid": item.get("songid") or item.get("id") or 0,
            "media_mid": item.get("media_mid")
            or item.get("strMediaMid")
            or item.get("songmid")
            or "",
            "songName": item.get("songname")
            or re.sub(r"<[^>]+>", "", item.get("songname_hilight") or "")
            or item.get("title")
            or item.get("name")
            or "",
            "singerName": singer,
            "albumName": item.get("albumname") or album.get("name") or "",
            "albummid": albummid,
            "cover": qqapi.cover_url(albummid) if albummid else "",
            "duration": duration,
            "interval": interval,
            "payplay": payplay,
            "raw": item,
        }

    @filter.regex(r"^#?(qq|QQ)m\s*新歌(?:\s*(\d+))?$")
    async def new_songs(self, event: AstrMessageEvent):
        """新歌速递：type 1 内地 / 2 欧美 / 3 日本 / 4 韩国 / 5 最新 / 6 港台，默认 5"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        scope = self._scope(event)
        user_key = self._user_key(event)
        m = re.match(
            r"^#?(?:qq|QQ)m\s*新歌(?:\s*(\d+))?$",
            event.message_str.strip(),
            re.IGNORECASE,
        )
        type_ = int(m.group(1)) if (m and m.group(1)) else 5
        try:
            await self._reply(event, "正在获取新歌速递...")
            songs = await qqapi.new_songs(type_, num=20, user_key=user_key)
            if not songs:
                await self._reply(event, "获取新歌失败，请稍后重试")
                event.stop_event()
                return
            title = "新歌速递"
            await cardlib.SessionStore.set(
                self,
                scope,
                {
                    "type": "newSongs",
                    "data": songs,
                },
            )
            if cfg.get("renderListCard", True):
                data = cardlib.build_list_card_data(title, songs, cfg=cfg)
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-list",
                    data=data,
                    format_text=lambda d: cardlib.format_song_list(songs, title),
                ):
                    event.stop_event()
                    return
            await self._reply(event, cardlib.format_song_list(songs, title))
        except Exception as err:
            self._log_warn(f"新歌失败: {err}")
            await self._reply(event, "新歌速递获取失败，请稍后重试")
        event.stop_event()

    # ══════════════════ MV ══════════════════

    @filter.regex(r"^#?(qq|QQ)m\s*(MV|mv)\s*(.*)$")
    async def mv(self, event: AstrMessageEvent):
        """MV：搜索 / 播放 / 下载 / 分类浏览"""


        cfg = self._cfg()
        if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*(?:MV|mv)\s*(.*)$",
            event.message_str.strip(),
            re.IGNORECASE,
        )
        rest = (m.group(1).strip() if m else "").strip()
        scope = self._scope(event)
        user_key = self._user_key(event)

        # 播放/下载/搜索 前缀分发（兼容紧凑写法：播放1 / 搜索周杰伦）
        verb_match = re.match(r"^(播放|下载|搜索)[:：]?\s*(.*)$", rest)
        verb = verb_match.group(1) if verb_match else ""
        arg_text = (verb_match.group(2).strip() if verb_match else rest).strip()
        is_play = verb == "播放"
        is_dl = verb == "下载"
        is_search = verb == "搜索"

        # ── 搜索 ──
        if is_search:
            if not arg_text:
                await self._reply(event, "用法：#qqmMV 搜索 关键词")
                event.stop_event()
                return
            try:
                lst = await qqapi.search_mv(arg_text, page_size=10, user_key=user_key)
                if not lst:
                    await self._reply(event, "没有搜到相关 MV")
                    event.stop_event()
                    return
                await cardlib.SessionStore.set(
                    self,
                    scope,
                    {
                        "type": "mvList",
                        "data": lst,
                    },
                )
                await self._reply(
                    event,
                    cardlib.format_mv_list_text(lst)
                    + "\n\n发 #qqmMV 播放 / 下载 序号",
                )
            except Exception as err:
                self._log_warn(f"MV 搜索失败: {err}")
                await self._reply(event, f"MV 搜索失败：{err}")
            event.stop_event()
            return

        # ── 播放 / 下载 ──
        if is_play or is_dl:
            session = await cardlib.SessionStore.get(self, scope)
            mv_obj = None
            if not arg_text:
                # 无目标：试试点歌后记住的「本曲 MV」
                last_vid = (session or {}).get("lastMvVid") or ""
                if last_vid:
                    mv_obj = {
                        "vid": last_vid,
                        "mvtitle": "本曲 MV",
                        "name": "本曲 MV",
                        "singerName": "",
                    }
                else:
                    await self._reply(
                        event,
                        "用法：#qqmMV 播放/下载 序号 或 vid；"
                        "点歌后再发 #qqmMV 播放/下载 可直接操作该曲 MV",
                    )
                    event.stop_event()
                    return
            elif re.fullmatch(r"\d+", arg_text):
                # 序号：MV 列表直接取；歌曲列表取带 🎬 的那首
                n = int(arg_text)
                if not session or not session.get("data"):
                    await self._reply(event, "请先 #qqm点歌 / #qqmMV 搜索 出列表")
                    event.stop_event()
                    return
                if n < 1 or n > len(session["data"]):
                    await self._reply(event, f"请选择 1-{len(session['data'])}")
                    event.stop_event()
                    return
                if session.get("type") == "mvList":
                    mv_obj = session["data"][n - 1]
                else:
                    song = session["data"][n - 1]
                    song_vid = song.get("mvVid") or ""
                    if not song_vid:
                        await self._reply(event, "该曲没有 MV")
                        event.stop_event()
                        return
                    mv_obj = {
                        "vid": song_vid,
                        "mvtitle": song.get("songName") or "本曲 MV",
                        "name": song.get("songName") or "本曲 MV",
                        "singerName": song.get("singerName") or "",
                        "cover": song.get("cover") or "",
                    }
            else:
                # 直接 vid
                mv_obj = {
                    "vid": arg_text,
                    "mvtitle": arg_text,
                    "name": arg_text,
                    "singerName": "",
                }

            await self._deliver_mv(event, mv_obj, download=is_dl, user_key=user_key)
            event.stop_event()
            return

        # ── 分类浏览：#qqmMV 分类 [序号/分类名] ──
        if rest.startswith("分类"):
            cat_rest = rest[len("分类") :].strip()
            try:
                cats = await qqapi.mv_category(user_key)
            except Exception as err:
                self._log_warn(f"MV 分类失败: {err}")
                cats = {"area": [], "version": [], "list": []}
            all_cats = (
                (cats.get("area") or [])
                + (cats.get("version") or [])
                + (cats.get("list") or [])
            )
            if not all_cats:
                await self._reply(event, "没有获取到 MV 分类")
                event.stop_event()
                return
            if not cat_rest:
                lines = ["♫ MV 分类"]
                for i, t in enumerate(all_cats):
                    lines.append(f"{i + 1}. {t.get('name') or t.get('title') or ''}")
                lines.append("\n发送 #qqmMV 分类 序号 或 #qqmMV 分类 分类名 浏览该类 MV")
                await self._reply(event, "\n".join(lines))
                event.stop_event()
                return
            try:
                idx = int(cat_rest) - 1
                tag = (
                    all_cats[idx] if (idx >= 0 and idx < len(all_cats)) else None
                )
            except (ValueError, TypeError):
                tag = next(
                    (
                        t
                        for t in all_cats
                        if cat_rest
                        in str(t.get("name") or t.get("title") or "")
                    ),
                    None,
                )
            if not tag:
                await self._reply(event, f"未找到分类「{cat_rest}」")
                event.stop_event()
                return
            try:
                res = await qqapi.mv_by_tag(
                    tag.get("id") or tag.get("tagId"),
                    page_size=20,
                    user_key=user_key,
                )
                lst = res.get("list") or []
                if not lst:
                    await self._reply(event, "该分类暂无 MV")
                    event.stop_event()
                    return
                await cardlib.SessionStore.set(
                    self,
                    scope,
                    {
                        "type": "mvList",
                        "data": lst,
                    },
                )
                await self._reply(
                    event,
                    cardlib.format_mv_list_text(lst[:15])
                    + "\n\n发 #qqmMV 播放 / 下载 序号",
                )
            except Exception as err:
                self._log_warn(f"MV 分类浏览失败: {err}")
                await self._reply(event, f"MV 分类浏览失败：{err}")
            event.stop_event()
            return

        # ── 无动词：先选歌再查看 MV ──
        await self._start_select(
            event,
            "mv",
            rest,
            label="MV",
            verb="查看MV",
            user_key=user_key,
        )
        event.stop_event()

    async def _deliver_mv(
        self,
        event: AstrMessageEvent,
        mv_obj: dict,
        *,
        download: bool,
        user_key: str,
    ) -> bool:
        """获取 MV 播放链接并投递视频/下载文件/链接。成功返回 True。"""
        try:
            url = await qqapi.mv_url(mv_obj["vid"], user_key)
            if not url:
                await self._reply(event, "获取 MV 播放链接失败，可能需 #qqm登录")
                return False
        except Exception as err:
            await self._reply(event, f"获取 MV 播放链接失败：{err}")
            return False

        # MV 详情卡（复用 qqmusic-detail 模板）
        # 受限平台（qq_official 被动回复受限）把卡片并入视频同一条消息，省被动回复额度
        passive_limited = _is_passive_limited(event)
        img_path = ""
        try:
            card = cardlib.build_mv_card_data(mv_obj, cfg=self._cfg())
            img_path = await self._render_card(event, card, "qqmusic-detail") or ""
            if img_path and not passive_limited:
                await self._send_chain(event, Image.fromFileSystem(img_path))
            elif not img_path:
                await self._reply(event, cardlib.format_mv_text(mv_obj))
        except Exception:
            await self._reply(event, cardlib.format_mv_text(mv_obj))

        ret = await deliver_video(
            self,
            event,
            mv_obj,
            url,
            cfg=self._cfg(),
            plugin_dir=PLUGIN_DIR,
            download=download,
            extra=[Image.fromFileSystem(img_path)]
            if (img_path and passive_limited)
            else None,
        )
        if not ret.get("ok"):
            # 视频/文件均失败：本地文件还在则再试一次发下载文件，否则回退链接
            fp = ret.get("filePath") or ""
            sent_file = False
            if fp and os.path.exists(fp):
                title_f = mv_obj.get("mvtitle") or mv_obj.get("name") or "MV"
                try:
                    await self._send_chain(
                        event,
                        File(
                            re.sub(r'[\\/:*?"<>|]', "", str(title_f))[:30] + ".mp4",
                            file=fp,
                        ),
                    )
                    sent_file = True
                    _schedule_cleanup(
                        fp, int(self._cfg().get("keepFileSec", 120))
                    )
                except Exception:
                    pass
            if sent_file:
                await self._reply(event, "视频消息发送失败，已改发下载文件")
            else:
                await self._reply(event, f"视频发送失败，可点击查看：{url}")
        return True

    async def _show_mv(self, event: AstrMessageEvent, song: dict, user_key: str) -> None:
        """播放所选歌曲的 MV（#qqm听N 触发）。"""
        song_vid = song.get("mvVid") or ""
        if not song_vid:
            songmid = song.get("songmid") or ""
            if songmid:
                try:
                    infos = await qqapi.song_info_batch(
                        [songmid], user_key=user_key
                    )
                    song_vid = (infos[0].get("mvVid") if infos else "") or ""
                except Exception:  # noqa: S110, BLE001  # 查 MV 徽标为可选项，失败忽略
                    pass
        if not song_vid:
            await self._reply(event, "该曲没有 MV")
            return
        mv_obj = {
            "vid": song_vid,
            "mvtitle": song.get("songName") or "本曲 MV",
            "name": song.get("songName") or "本曲 MV",
            "singerName": song.get("singerName") or "",
            "cover": song.get("cover") or "",
        }
        await self._deliver_mv(event, mv_obj, download=False, user_key=user_key)

    @filter.regex(r"^#?(qq|QQ)m\s*推荐$")
    async def recommend(self, event: AstrMessageEvent):
        """#qqm推荐 热门推荐歌单"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        scope = self._scope(event)
        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在获取推荐歌单...")
            lst = await qqapi.recommend_hot(user_key)
            if not lst:
                await self._reply(event, "获取推荐失败")
                event.stop_event()
                return
            lines = ["♫ 热门推荐歌单"]
            for i, p in enumerate(lst[:15]):
                name = p.get("title") or p.get("dissname") or "未知"
                cnt = p.get("listenNum") or p.get("listennum") or 0
                lines.append(f"{i + 1}. {name} ({cnt}次播放)")
            lines.append("\n发送 #qqm推荐听序号 查看歌单歌曲")
            await cardlib.SessionStore.set(
                self,
                scope,
                {"type": "recommend", "data": lst},
            )
            if cfg.get("renderListCard", True):
                hot_items = [
                    {
                        "k": p.get("title") or p.get("dissname") or "",
                        "n": p.get("listenNum") or p.get("listennum") or 0,
                    }
                    for p in lst[:15]
                ]
                data = cardlib.build_hot_card_data(hot_items, cfg=self._cfg())
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-hot",
                    data=data,
                    format_text=lambda d: "\n".join(lines),
                ):
                    event.stop_event()
                    return
            await self._reply(event, "\n".join(lines))
        except Exception as err:
            self._log_warn(f"推荐失败: {err}")
            await self._reply(event, "推荐失败，请稍后重试")
        event.stop_event()

    @filter.regex(r"^#?(qq|QQ)m\s*(来首歌|随机|放一首|来一首)$")
    async def random_song(self, event: AstrMessageEvent):
        """#qqm来首歌 随机推荐一首并播放"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在为你推荐...")
            songs = await qqapi.recommend_feed(user_key)
            if not songs:
                await self._reply(event, "获取推荐失败，请重试")
                event.stop_event()
                return
            song = random.choice(songs)  # noqa: S311 非安全场景（随机推荐一首歌）
            await self._reply(
                event, f"♪ {song.get('songName')} - {song.get('singerName')}"
            )
            play = await qqapi.song_url_best(
                song["songmid"],
                quality=cfg.get("quality") or "flac",
                media_id=song.get("media_mid") or song.get("songmid"),
                fallback=cfg.get("qualityFallback", True) is not False,
                user_key=user_key,
            )
            if not play.get("url"):
                await self._reply(event, "获取播放链失败，请 #qqm登录")
                event.stop_event()
                return
            await deliver_song(
                self, event, song, play, cfg=self._cfg(), plugin_dir=PLUGIN_DIR
            )
        except Exception as err:
            self._log_warn(f"推荐失败: {err}")
            await self._reply(event, "推荐失败，请稍后重试")
        event.stop_event()

    @filter.regex(r"^#?(qq|QQ)m\s*电台$")
    async def radio(self, event: AstrMessageEvent):
        """#qqm电台 个性电台 5 首"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        scope = self._scope(event)
        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在获取个性电台...")
            songs = await qqapi.personal_radio(5, user_key)
            if not songs:
                await self._reply(event, "获取电台失败，请重试")
                event.stop_event()
                return
            await cardlib.SessionStore.set(
                self,
                scope,
                {
                    "type": "radio",
                    "data": songs,
                },
            )
            await self._reply(event, cardlib.format_song_list(songs, "个性电台"))
        except Exception as err:
            self._log_warn(f"电台失败: {err}")
            await self._reply(event, "电台失败，请稍后重试")
        event.stop_event()

    @filter.regex(r"^#?(qq|QQ)m\s*(日推|每日推荐)$")
    async def daily(self, event: AstrMessageEvent):
        """#qqm日推 每日推荐（需登录）"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        scope = self._scope(event)
        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在获取每日推荐...")
            res = await qqapi.daily_recommend(song_num=30, user_key=user_key)
            songs = res.get("songs") or []
            if not songs:
                await self._reply(
                    event,
                    "📭 每日推荐为空\n可能原因：\n"
                    "1. 今日已获取过，请明天再试\n"
                    "2. 账号无听歌记录，无法生成推荐\n"
                    "请先 #qqm登录 绑定有听歌记录的账号",
                )
                event.stop_event()
                return
            title = res.get("title") or "每日推荐"
            await cardlib.SessionStore.set(
                self,
                scope,
                {
                    "type": "daily",
                    "data": songs,
                },
            )
            if cfg.get("renderListCard", True):
                data = cardlib.build_list_card_data(title, songs, cfg=cfg)
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-list",
                    data=data,
                    format_text=lambda d: cardlib.format_song_list(songs, title),
                ):
                    event.stop_event()
                    return
            await self._reply(event, cardlib.format_song_list(songs, title))
        except Exception as err:
            self._log_warn(f"日推失败: {err}")
            # 对应 JS chart.js: err.code === -1 || 文案含登录（API result=-1 表示未登录）
            if (
                getattr(err, "code", None) == -1
                or "登录" in str(err)
                or "login" in str(err).lower()
            ):
                await self._reply(event, "日推失败，请先 #qqm登录 后重试")
            else:
                await self._reply(event, f"日推失败：{err}")
        event.stop_event()

    @filter.regex(r"^#?(qq|QQ)m\s*收藏$")
    async def favorites(self, event: AstrMessageEvent):
        """#qqm收藏 我的收藏（需登录）"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        scope = self._scope(event)
        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在获取收藏...")
            res = await qqapi.user_favorites(song_num=30, user_key=user_key)
            songs = res.get("songs") or []
            if not songs:
                await self._reply(
                    event,
                    "📭 我的收藏为空\n"
                    "你的 QQ 音乐「我喜欢」歌单还没有收藏任何歌曲\n\n"
                    "💡 你可以在 QQ 音乐 App 中收藏歌曲后再来查看",
                )
                event.stop_event()
                return
            title = res.get("title") or "我的收藏"
            await cardlib.SessionStore.set(
                self,
                scope,
                {
                    "type": "favorites",
                    "data": songs,
                },
            )
            if cfg.get("renderListCard", True):
                data = cardlib.build_list_card_data(title, songs, cfg=cfg)
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-list",
                    data=data,
                    format_text=lambda d: cardlib.format_song_list(songs, title),
                ):
                    event.stop_event()
                    return
            await self._reply(event, cardlib.format_song_list(songs, title))
        except Exception as err:
            self._log_warn(f"收藏失败: {err}")
            # 对应 JS chart.js: err.code === -1 || 文案含登录
            if (
                getattr(err, "code", None) == -1
                or "登录" in str(err)
                or "login" in str(err).lower()
            ):
                await self._reply(event, "收藏失败，请先 #qqm登录 后重试")
            else:
                await self._reply(event, f"收藏失败：{err}")
        event.stop_event()

    # ══════════════════ 歌手/专辑/歌单/评论 ══════════════════

    @filter.regex(r"^#?(qq|QQ)m\s*歌手\s+(.+)$")
    async def artist(self, event: AstrMessageEvent):
        """#qqm歌手 关键词 搜索歌手，展示热门歌曲"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*歌手\s+(.+)$", event.message_str.strip(), re.IGNORECASE
        )
        keyword = m.group(1).strip() if m else ""
        if not keyword:
            return
        scope = self._scope(event)
        user_key = self._user_key(event)
        try:
            await self._reply(event, f"正在搜索歌手：{keyword}")
            singers = await qqapi.search_singers(
                keyword, page_size=5, user_key=user_key
            )
            if not singers:
                await self._reply(event, "没有找到相关歌手")
                event.stop_event()
                return
            singer = singers[0]
            result = await qqapi.singer_songs(
                singer["singermid"], page_size=30, user_key=user_key
            )
            if not result.get("list"):
                await self._reply(event, "该歌手暂无歌曲")
                event.stop_event()
                return
            title = f"{singer.get('singerName', '')} 热门歌曲"
            await cardlib.SessionStore.set(
                self,
                scope,
                {
                    "type": "singer",
                    "data": result["list"],
                    "singer": singer,
                },
            )
            if cfg.get("renderListCard", True):
                data = cardlib.build_list_card_data(
                    title,
                    result["list"],
                    {
                        "tip": f"发送 #qqm听序号 播放「{singer.get('singerName', '')}」的歌曲"
                    },
                    cfg=self._cfg(),
                )
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-list",
                    data=data,
                    format_text=lambda d: cardlib.format_song_list(
                        result["list"], title
                    ),
                ):
                    await self._send_singer_desc(event, singer, user_key)
                    event.stop_event()
                    return
            await self._reply(event, cardlib.format_song_list(result["list"], title))
            await self._send_singer_desc(event, singer, user_key)
        except Exception as err:
            self._log_warn(f"歌手搜索失败: {err}")
            await self._reply(event, "歌手搜索失败，请稍后重试")
        event.stop_event()

    async def _send_singer_desc(
        self, event: AstrMessageEvent, singer: dict, user_key: str
    ):
        try:
            desc = await qqapi.singer_desc(singer["singermid"], user_key)
            d = desc.get("desc") if isinstance(desc, dict) else None
            if d:
                brief = str(d)[:200]
                suffix = "..." if len(d) > 200 else ""
                await self._reply(
                    event, f"【{singer.get('singerName', '')}】{brief}{suffix}"
                )
        except Exception:
            pass

    @filter.regex(r"^#?(qq|QQ)m\s*专辑\s+(.+)$")
    async def album(self, event: AstrMessageEvent):
        """#qqm专辑 关键词 搜索专辑，展示曲目列表"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*专辑\s+(.+)$", event.message_str.strip(), re.IGNORECASE
        )
        keyword = m.group(1).strip() if m else ""
        if not keyword:
            return
        scope = self._scope(event)
        user_key = self._user_key(event)
        try:
            await self._reply(event, f"正在搜索专辑：{keyword}")
            albums = await qqapi.search_albums(keyword, page_size=5, user_key=user_key)
            if not albums:
                await self._reply(event, "没有找到相关专辑")
                event.stop_event()
                return
            alb = albums[0]
            result = await qqapi.album_songs(alb["albummid"], user_key=user_key)
            if not result.get("list"):
                await self._reply(event, "该专辑暂无曲目")
                event.stop_event()
                return
            title = f"{alb.get('singerName', '')} - {alb.get('albumName', '')}"
            await cardlib.SessionStore.set(
                self,
                scope,
                {
                    "type": "album",
                    "data": result["list"],
                    "album": alb,
                },
            )
            if cfg.get("renderListCard", True):
                data = cardlib.build_list_card_data(
                    title,
                    result["list"],
                    {"tip": f"发送 #qqm听序号 播放「{alb.get('albumName', '')}」"},
                    cfg=self._cfg(),
                )
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-list",
                    data=data,
                    format_text=lambda d: cardlib.format_song_list(
                        result["list"], title
                    ),
                ):
                    if alb.get("publicTime"):
                        await self._reply(event, f"发行时间：{alb['publicTime']}")
                    event.stop_event()
                    return
            await self._reply(event, cardlib.format_song_list(result["list"], title))
            if alb.get("publicTime"):
                await self._reply(event, f"发行时间：{alb['publicTime']}")
        except Exception as err:
            self._log_warn(f"专辑搜索失败: {err}")
            await self._reply(event, "专辑搜索失败，请稍后重试")
        event.stop_event()

    @filter.regex(r"^#?(qq|QQ)m\s*歌单\s+(.+)$")
    async def playlist(self, event: AstrMessageEvent):
        """#qqm歌单 关键词 搜索歌单，展示歌曲"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*歌单\s+(.+)$", event.message_str.strip(), re.IGNORECASE
        )
        keyword = m.group(1).strip() if m else ""
        if not keyword:
            return
        scope = self._scope(event)
        user_key = self._user_key(event)
        try:
            await self._reply(event, f"正在搜索歌单：{keyword}")
            lists = await qqapi.search_songlists(
                keyword, page_size=5, user_key=user_key
            )
            if not lists:
                await self._reply(event, "没有找到相关歌单")
                event.stop_event()
                return
            pl = lists[0]
            detail = await qqapi.songlist_detail(pl["disstid"], user_key)
            songs = detail.get("songlist") or []
            if not songs:
                await self._reply(event, "该歌单暂无歌曲")
                event.stop_event()
                return
            title = detail.get("dissname") or pl.get("dissname") or "歌单"
            await cardlib.SessionStore.set(
                self,
                scope,
                {
                    "type": "playlist",
                    "data": songs,
                    "playlist": pl,
                },
            )
            if cfg.get("renderListCard", True):
                data = cardlib.build_list_card_data(title, songs, cfg=self._cfg())
                if await self._reply_card_or_text(
                    event,
                    tpl_name="qqmusic-list",
                    data=data,
                    format_text=lambda d: cardlib.format_song_list(songs, title),
                ):
                    event.stop_event()
                    return
            await self._reply(event, cardlib.format_song_list(songs, title))
        except Exception as err:
            self._log_warn(f"歌单搜索失败: {err}")
            await self._reply(event, "歌单搜索失败，请稍后重试")
        event.stop_event()

    @filter.regex(r"^#?(qq|QQ)m\s*评论\s*(.*)$")
    async def get_comment(self, event: AstrMessageEvent):
        """#qqm评论 [关键词]：先选歌（回复 #qqm听N）再显示热评"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        m = re.match(
            r"^#?(?:qq|QQ)m\s*评论\s*(.*)$", event.message_str.strip(), re.IGNORECASE
        )
        if not m:
            return
        await self._start_select(
            event,
            "comment",
            m.group(1).strip(),
            label="评论",
            verb="查看评论",
            user_key=self._user_key(event),
        )
        event.stop_event()

    async def _show_comment(self, event: AstrMessageEvent, song: dict, user_key: str) -> None:
        """显示所选歌曲的热门评论（#qqm听N 触发）。"""
        songid = song.get("songid") or song.get("songId") or ""
        if not songid:
            await self._reply(event, "未能获取歌曲ID，无法查询评论")
            return
        try:
            result = await qqapi.comment(
                songid, page_size=20, user_key=user_key
            )
        except Exception as err:  # noqa: BLE001  # 防御：评论失败不崩 handler
            self._log_warn(f"评论失败: {err}")
            await self._reply(event, "评论获取失败，请稍后重试")
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
            await self._reply(
                event,
                f"【{song.get('songName')} - {song.get('singerName')}】\n暂无评论",
            )
            return
        # 对齐原插件：cleanCommentText 清洗（表情代码/[音频]标记/\\r\\n）
        # + middlecommentcontent 字段
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
        if self._cfg().get("renderListCard", True):
            # 独立评论卡片：每条评论一个格子（头像 + 昵称 + 时间 + 内容 + 赞数）
            card = cardlib.build_comment_card_data(
                song_name=song.get("songName", ""),
                singer_name=song.get("singerName", ""),
                cover=song.get("cover") or "",
                album_name=song.get("albumName") or "",
                songmid=song.get("songmid") or "",
                comments=all_comments[:10],
                cfg=self._cfg(),
            )
            header = [
                f"♫ {song.get('songName')} - {song.get('singerName')} 热门评论",
                "",
            ]
            if await self._reply_card_or_text(
                event,
                tpl_name="qqmusic-comment",
                data=card,
                format_text=lambda d: "\n".join(header + comment_lines),
            ):
                return
        await self._reply(
            event,
            "\n".join(
                [
                    f"♫ {song.get('songName')} - {song.get('singerName')} 热门评论",
                    "",
                ]
                + comment_lines
            ),
        )

    # ══════════════════ 智能解析 ══════════════════

    @filter.regex(
        r"(y\.qq\.com|c6\.y\.qq\.com|i\.y\.qq\.com|qqmusic|QQ音乐|100497308|music\.lua|structmsg|songmid|sdkshare_music)",
        priority=8,  # 高优先级抢先解析，避免与其他插件重复处理（对齐原版 accept 抢占）
    )
    async def resolve(self, event: AstrMessageEvent):
        """自动解析 QQ 音乐分享卡片 / 链接"""


        cfg = self._cfg()
        if not cfg.get("enable", True) or cfg.get("enableResolve") is False:
            return
        text = _collect_message_text(event)
        if not _is_qqmusic_message(text):
            return
        if _is_plugin_command_msg(event.message_str):
            return
        try:
            ok = await self._handle_resolve(event, text, cfg)
            if ok:
                event.stop_event()
        except Exception as err:
            self._log_warn(f"解析失败: {err}")

    async def _handle_resolve(
        self, event: AstrMessageEvent, text: str, cfg: dict
    ) -> bool:
        user_key = self._user_key(event)
        song = None
        from_card = False

        if cfg.get("resolveCards", True):
            card = qqapi.parse_qqmusic_card(text)
            if card:
                from_card = True
                self._log_info(f"识别卡片: {card.get('title')} - {card.get('desc')}")
                song = await self._card_to_song(card, user_key)

        if not song and cfg.get("resolveLinks", True):
            # 排除中文标点，避免把链接后的「，很好听」之类吞入（对齐 JS 版 rconsole 同款）
            url_match = re.search(
                r"https?://(?:[a-z0-9-]+\.)?(?:y\.qq\.com|c6\.y\.qq\.com)"
                r'[^\s，。；：！？、（）【】《》»"“”\'`一-龥]*',
                text,
                re.IGNORECASE,
            )
            if url_match:
                url = url_match.group(0)
                self._log_info(f"识别链接: {url}")

                # c6.y.qq.com 短链（移动端分享形态）跟随重定向，拿最终 songDetail 链接
                if re.search(
                    r"c6\.y\.qq\.com/base/fcgi-bin/u\?", url, re.IGNORECASE
                ) or re.search(r"[?&]__=", url):
                    try:
                        final_url = await _follow_qq_redirect(url)
                        if final_url and final_url != url:
                            url = final_url
                            self._log_info(f"短链跟随: {url}")
                    except Exception as err:
                        self._log_warn(f"短链跟随失败: {err}")

                ext_ids = qqapi.parse_qqmusic_extended_ids(url)
                scope = self._scope(event)

                if ext_ids.get("albummid"):
                    try:
                        result = await qqapi.album_songs(
                            ext_ids["albummid"], user_key=user_key
                        )
                        songs = result.get("list") or []
                        if songs:
                            # 统一用链接里的 albummid（专辑曲目接口返回的字段可能缺失）
                            for s in songs:
                                s["albummid"] = s.get("albummid") or ext_ids["albummid"]
                            await cardlib.SessionStore.set(
                                self,
                                scope,
                                {
                                    "type": "album",
                                    "data": songs,
                                },
                            )
                            await self._reply(
                                event,
                                f"识别到专辑链接，共{len(songs)}首。发送 #qqm听序号 播放",
                            )
                            return True
                    except Exception as err:
                        self._log_warn(f"专辑解析失败: {err}")

                if ext_ids.get("disstid"):
                    try:
                        detail = await qqapi.songlist_detail(
                            ext_ids["disstid"], user_key
                        )
                        songs = detail.get("songlist") or []
                        if songs:
                            title = (
                                detail.get("dissname") or detail.get("title") or "歌单"
                            )
                            await cardlib.SessionStore.set(
                                self,
                                scope,
                                {
                                    "type": "playlist",
                                    "data": songs,
                                },
                            )
                            await self._reply(
                                event,
                                f"识别到歌单「{title}」，共{len(songs)}首。发送 #qqm听序号 播放",
                            )
                            return True
                    except Exception as err:
                        self._log_warn(f"歌单解析失败: {err}")

                if ext_ids.get("singermid"):
                    try:
                        result = await qqapi.singer_songs(
                            ext_ids["singermid"], page_size=30, user_key=user_key
                        )
                        if result.get("list"):
                            await cardlib.SessionStore.set(
                                self,
                                scope,
                                {
                                    "type": "singer",
                                    "data": result["list"],
                                },
                            )
                            await self._reply(
                                event,
                                f"识别到歌手链接，热门歌曲{len(result['list'])}首。"
                                "发送 #qqm听序号 播放",
                            )
                            return True
                    except Exception as err:
                        self._log_warn(f"歌手解析失败: {err}")

                ids = qqapi.parse_qqmusic_ids(url)
                song = await self._ids_to_song(ids, text, user_key)

        if not song:
            if from_card:
                await self._reply(event, "识别到 QQ 音乐分享，但未能提取歌曲信息")
                return True
            return False

        play = {
            "url": "",
            "quality": cfg.get("quality") or "flac",
            "qualityLabel": self._quality_label(cfg.get("quality") or "flac"),
        }
        if song.get("songmid"):
            try:
                if not song.get("media_mid") or song.get("media_mid") == song.get(
                    "songmid"
                ):
                    try:
                        detail = await qqapi.song_detail(song["songmid"], user_key)
                        ti = (
                            (detail or {}).get("track_info")
                            if isinstance(detail, dict)
                            else None
                        )
                        if ti:
                            file = ti.get("file") if isinstance(ti, dict) else None
                            if isinstance(file, dict) and file.get("media_mid"):
                                song["media_mid"] = file["media_mid"]
                            if not song.get("songid") and ti.get("id"):
                                song["songid"] = ti["id"]
                            if (
                                not song.get("albummid")
                                and isinstance(ti.get("album"), dict)
                                and ti["album"].get("mid")
                            ):
                                song["albummid"] = ti["album"]["mid"]
                    except Exception:
                        pass
                play = await qqapi.song_url_best(
                    song["songmid"],
                    quality=cfg.get("quality") or "flac",
                    media_id=song.get("media_mid") or song.get("songmid"),
                    fallback=cfg.get("qualityFallback", True) is not False,
                    user_key=user_key,
                )
                play = self._play_view(play)
            except Exception as err:
                self._log_warn(f"播放链: {err}")
                play["error"] = str(err)

        q_label = (
            play.get("qualityLabel")
            or self._quality_label(play.get("quality", ""))
            or ""
        )
        prefix = cfg.get("identifyPrefix") or "识别："
        fail_hint = ""
        if not play.get("url"):
            err_text = str(play.get("error") or "")[:220]
            raw = play.get("raw") if isinstance(play.get("raw"), dict) else {}
            pay = (
                raw.get("pay") if isinstance(raw.get("pay"), dict) else play.get("pay")
            )
            if err_text:
                fail_hint = f"⚠ {err_text}"
            elif pay and _safe_int(pay.get("pay_play")) == 1:
                fail_hint = "⚠ 该曲需会员播放，请 #qqm登录"
            else:
                fail_hint = (
                    "⚠ 未获取到播放链（请 #qqm登录 重新扫码；或 #qqm刷新 后重试）"
                )

        card_data = cardlib.build_detail_card_data(
            song,
            quality_label=q_label,
            payplay=bool(song.get("payplay")),
            source=("卡片" if from_card else "链接"),
            has_url=bool(play.get("url")),
        )
        tip = (
            f"正在下载并发送语音（{q_label or '默认音质'}）..."
            if play.get("url")
            else (fail_hint or "未获取到播放链接")
        )
        if play.get("degradeNote"):
            tip += f" · {play['degradeNote']}"
        card_data["tip"] = tip
        url = await self._render_card(event, card_data, "qqmusic-detail")
        if url:
            await self._send_chain(event, Image.fromFileSystem(url))
        else:
            text_block = [
                f"{prefix}QQ音乐 · 解析下载中",
                cardlib.format_detail_text(
                    song, quality_label=q_label, has_url=bool(play.get("url"))
                ),
            ]
            if fail_hint:
                text_block.append(fail_hint)
            await self._reply(event, "\n".join(t for t in text_block if t))

        await deliver_song(
            self,
            event,
            song,
            play,
            cfg=self._cfg(),
            plugin_dir=PLUGIN_DIR,
            options=_SKIP_ALL,
        )
        return True

    async def _card_to_song(self, card: dict, user_key: str = "") -> dict | None:
        if card.get("songmid"):
            return await self._ids_to_song(card, "", user_key)
        if card.get("keyword") or card.get("title"):
            kw = (
                card.get("keyword") or f"{card.get('title', '')} {card.get('desc', '')}"
            ).strip()
            lst = await qqapi.search_songs(kw, page_size=5)
            if lst:
                hit = lst[0]
                # 标题匹配最佳结果
                for s in lst:
                    if card.get("title") and (
                        card["title"][:8] in s.get("songName", "")
                        or s.get("songName", "")[:8] in card["title"]
                    ):
                        hit = s
                        break
                if card.get("cover"):
                    hit["cover"] = card["cover"]
                return hit
        return {
            "songmid": card.get("songmid") or "",
            "songid": card.get("songid") or 0,
            "media_mid": card.get("media_mid") or "",
            "songName": card.get("title") or "未知",
            "singerName": card.get("desc") or "",
            "cover": card.get("cover") or "",
            "albumName": "",
            "albummid": card.get("albummid") or "",
        }

    async def _ids_to_song(
        self, ids: dict, fallback_text: str = "", user_key: str = ""
    ) -> dict | None:
        songmid = ids.get("songmid") or ""
        songid = ids.get("songid") or 0
        media_mid = ids.get("media_mid") or ""
        albummid = ids.get("albummid") or ""

        if songmid:
            try:
                detail = await qqapi.song_detail(songmid, user_key)
                track = (
                    (detail or {}).get("track_info")
                    if isinstance(detail, dict)
                    else None
                )
                if not isinstance(track, dict):
                    track = (
                        (detail or {}).get("info") if isinstance(detail, dict) else None
                    )
                if not isinstance(track, dict):
                    track = detail if isinstance(detail, dict) else {}
                name = (
                    track.get("name")
                    or track.get("title")
                    or (detail or {}).get("name")
                    or songmid
                )
                singers = (
                    track.get("singer")
                    if isinstance(track.get("singer"), list)
                    else None
                )
                if singers:
                    singer = " / ".join(
                        s.get("name", "") for s in singers if isinstance(s, dict)
                    )
                else:
                    singer = track.get("singername") or ""
                album = (
                    track.get("album") if isinstance(track.get("album"), dict) else {}
                )
                albummid = albummid or album.get("mid") or ""
                file = (
                    track.get("file") if isinstance(track.get("file"), dict) else None
                )
                media_mid = (
                    media_mid
                    or (file.get("media_mid") if file else "")
                    or track.get("media_mid")
                    or songmid
                )
                songid = songid or track.get("id") or 0
                return {
                    "songmid": songmid,
                    "songid": songid,
                    "media_mid": media_mid,
                    "songName": name,
                    "singerName": singer,
                    "albumName": album.get("name") or "",
                    "albummid": albummid,
                    "cover": qqapi.cover_url(albummid) if albummid else "",
                }
            except Exception as err:
                self._log_warn(f"详情失败: {err}")

        prefix = (
            re.sub(r"@\S+", "", re.sub(r"https?://\S+", "", fallback_text))
            .replace("《", " ")
            .replace("》", " ")
            .strip()
        )
        if prefix:
            lst = await qqapi.search_songs(prefix, page_size=3)
            if lst:
                return lst[0]

        if songid and not songmid:
            return {
                "songmid": "",
                "songid": songid,
                "media_mid": "",
                "songName": f"歌曲{songid}",
                "singerName": "",
                "albumName": "",
                "albummid": "",
                "cover": "",
            }
        return None

    # ══════════════════ 扫码登录 ══════════════════

    # user_id -> {qrcodeID, timer, stopped}
    _active_logins: ClassVar[dict] = {}

    def _stop_poll(self, user_id: str):
        t = self._active_logins.get(str(user_id))
        if t:
            t["stopped"] = True
            timer = t.get("timer")
            if timer:
                timer.cancel()
            self._active_logins.pop(str(user_id), None)

    def _register_task(self, user_id: str, task: dict):
        # 并发注册防覆盖：同 user_id 若已有旧任务（两个通道先后发起时可能残留），
        # 先停旧任务再注册，避免旧任务 tick 把新任务条目 pop 掉导致轮询静默停止
        old = self._active_logins.get(user_id)
        if old is not None and old is not task:
            old["stopped"] = True
            t = old.get("timer")
            if t is not None:
                with contextlib.suppress(Exception):
                    t.cancel()
        self._active_logins[user_id] = task

    def _pop_task_if_current(self, user_id: str, task: dict):
        # 仅当该任务仍是当前注册任务时才移除；旧任务（已被新任务覆盖）tick 不得误删
        if self._active_logins.get(user_id) is task:
            self._active_logins.pop(user_id, None)

    async def _save_qr_image(self, base64_str: str) -> str:
        import base64

        from .delivery import _write_bytes, get_temp_dir

        d = get_temp_dir(self._cfg(), PLUGIN_DIR)
        os.makedirs(d, exist_ok=True)
        file_path = os.path.join(d, f"qr_{int(time.time() * 1000)}.png")
        await asyncio.to_thread(_write_bytes, file_path, base64.b64decode(base64_str))
        return file_path

    async def _pick_login_success(self, body: dict) -> dict | None:
        data = qqapi.unwrap_data(body)
        uin = data.get("uin") or ""
        has_key = bool(data.get("hasKey", data.get("qm_keyst")))
        nick = data.get("nick") or ""
        channel = data.get("channel") or ""
        ok = (
            (data.get("status") == "success" and (uin or has_key))
            or (uin and has_key is True)
            or (data.get("login") is True and uin and has_key)
        )
        if not ok:
            return None
        if not channel:
            channel = "mqtt" if data.get("status") == "success" else "status"
        return {
            "ok": True,
            **data,
            "uin": uin,
            "nick": nick,
            "hasKey": has_key,
            "channel": channel,
        }

    async def _on_login_success(
        self, event: AstrMessageEvent, info: dict | None = None
    ):
        info = info or {}
        uin = info.get("uin") or ""
        nick = info.get("nick") or ""
        has_key = info.get("hasKey", True)
        user_key = self._user_key(event)
        meta = None
        try:
            meta = await qqapi.pull_login_meta(user_key)
        except Exception as e:
            self._log_warn(f"拉取登录态元信息失败: {e}")
        lines = ["✅ 登录成功"]
        if uin:
            lines.append(f"uin: {uin}")
        if nick:
            lines.append(f"昵称: {nick}")
        if has_key is False:
            lines.append("⚠️ 未拿到 key，付费曲可能仍无法播放")
        if meta and meta.get("hasRefresh"):
            lines.append("含 refresh 材料，过期可自动续期")
        else:
            lines.append("⚠️ 无 refresh，过期后需重新扫码")
        lines.append("正在生成状态卡片…")
        await self._reply(event, "\n".join(lines))
        await asyncio.sleep(0.4)
        try:
            data = await self._build_status_data(user_key)
            url = await self._render_card(event, data, "qqmusic-status")
            if url:
                await self._send_chain(event, Image.fromFileSystem(url))
            else:
                await self._reply(event, cardlib.format_status_text(data))
        except Exception as e:
            self._log_warn(f"登录后状态卡失败: {e}")
            await self._reply(
                event, "登录已成功，但状态卡渲染失败，可手动发送 #qqm状态"
            )

    @filter.regex(
        re.compile(r"^#?(qqm登录(qq|app))$", re.IGNORECASE),
        priority=6,
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def start_qr_login(self, event: AstrMessageEvent):
        """#qqm登录qq QQ音乐 App 扫码登录（MQTT 备用通道，主人）"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        if cfg.get("qrLoginEnable") is False:
            await self._reply(event, "扫码登录已在配置中关闭")
            event.stop_event()
            return
        self._stop_poll(self._user_key(event))
        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在获取 QQ 音乐登录二维码…")
            body = await qqapi.request("/login/qr", {}, "get", user_key)
            data = qqapi.unwrap_data(body)
            if not data.get("qrcodeID"):
                await self._reply(
                    event, f"获取二维码失败：{(body or {}).get('errMsg') or '未知错误'}"
                )
                event.stop_event()
                return
            qrcode_id = data.get("qrcodeID")
            qrcode_b64 = data.get("qrcodeBase64")
            qrcode = data.get("qrcode")
            try:
                expires_in = int(
                    data.get("expiresIn") or 900
                )  # 对应 JS Number(expiresIn || 900)
            except (TypeError, ValueError):
                expires_in = 900
            tips = data.get("tips") or "请使用 QQ / 微信 / QQ音乐 App 扫码"
            tip_text = "\n".join(
                x for x in [tips, f"二维码 {round(expires_in / 60)} 分钟内有效"] if x
            )
            # 准备二维码图片
            qr_path = None
            if qrcode_b64:
                qr_path = await self._save_qr_image(qrcode_b64)
            elif qrcode and qrcode.startswith("data:"):
                b64 = qrcode.split(",", 1)[1]
                qr_path = await self._save_qr_image(b64)
            img_sent = False
            if qr_path:
                try:
                    # 图片+提示合并为一条消息（QQ 官方可省一次被动回复额度）
                    await self._send_chain(
                        event, Image.fromFileSystem(qr_path), self._plain(tip_text)
                    )
                    img_sent = True
                except Exception:
                    pass
                # 120s 后清理
                loop = asyncio.get_event_loop()
                loop.call_later(120, lambda: self._safe_unlink(qr_path))
            if not img_sent:
                await self._reply(
                    event, tip_text + "\n（图片发送失败可重新 #qqm登录qq）"
                )
            # 轮询间隔按 API 建议（pollInterval，秒），默认 2s（开发指南 6.2）
            try:
                poll_interval = float(data.get("pollInterval") or 2)
            except (TypeError, ValueError):
                poll_interval = 2
            if poll_interval <= 0 or poll_interval > 30:
                poll_interval = 2
            self._start_poll(event, qrcode_id, expires_in, poll_interval)
        except Exception as err:
            await self._reply(event, f"扫码登录失败：{err}")
        event.stop_event()

    def _safe_unlink(self, path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _start_poll(
        self,
        event: AstrMessageEvent,
        qrcode_id: str,
        expires_in: int,
        poll_interval: float = 2.0,
    ):
        user_id = str(event.get_sender_id())
        user_key = user_id
        started = time.time()
        max_sec = min(expires_in, 900)
        task = {
            "qrcodeID": qrcode_id,
            "stopped": False,
            "busy": False,
            "completeTried": 0,
            "statusBaseline": None,
            "notifiedScan": False,
            "failStreak": 0,
            "pollCount": 0,
            "pollInterval": poll_interval,
        }
        self._register_task(user_id, task)

        # 基线
        async def _baseline():
            try:
                st = await qqapi.request("/login/status", {}, "get", user_key)
                d = (st or {}).get("data") or {}
                task["statusBaseline"] = {
                    "uin": str(d.get("uin") or ""),
                    "hasKey": bool(d.get("hasKey")),
                    "login": bool(d.get("login")),
                }
            except Exception:
                task["statusBaseline"] = {"uin": "", "hasKey": False, "login": False}

        asyncio.create_task(_baseline())

        async def _finish_ok(info):
            if task["stopped"]:
                return
            task["stopped"] = True
            # 取消已调度的下一次轮询（对应原插件 finishOk 的 clearTimeout）
            timer = task.get("timer")
            if timer is not None:
                with contextlib.suppress(Exception):
                    timer.cancel()
            self._pop_task_if_current(user_id, task)
            await self._on_login_success(event, info)

        async def _tick():
            if task["stopped"]:
                return
            if task["busy"]:
                loop.call_later(0.8, lambda: asyncio.create_task(_tick()))
                return
            elapsed = time.time() - started
            if elapsed > max_sec:
                task["stopped"] = True
                self._pop_task_if_current(user_id, task)
                await self._reply(event, "二维码已过期，请重新 #qqm登录")
                return
            elapsed_ms = int(
                elapsed * 1000
            )  # API 要求毫秒（见开发指南 /login/qr/check）
            task["busy"] = True
            try:
                # isFirstScan 仅在第一次轮询为 true（后端据此建立扫码会话）；
                # 之后必须为 false，否则后端会误判为“二维码被另一个 APP 扫描”。
                is_first = task["pollCount"] == 0
                task["pollCount"] += 1
                body = await qqapi.request(
                    "/login/qr/check",
                    {
                        "qrcodeID": qrcode_id,
                        "elapsed": elapsed_ms,
                        "isFirstScan": 1 if is_first else 0,
                        "completeTried": 1 if task["completeTried"] > 0 else 0,
                    },
                    "get",
                    user_key,
                )
                data = (body or {}).get("data") or {}
                status = data.get("status") or "wait"
                task["failStreak"] = 0
                # 占位伪扫码过滤：上游（腾讯 MQTT）可能在未扫码时推送无真实用户的
                # scanned 消息（scanUser.userID=0、仅 appName="QQ音乐"）。旧版 API
                # 无 hasUser 守卫会直接采纳，导致假"已扫码"并诱发后续 "scanned by
                # another APP"。真实扫码必带 userID（QQ 号）/openid/musicId。
                scan_user = data.get("scanUser") or {}
                try:
                    scan_uid = int(scan_user.get("userID") or 0)
                except (TypeError, ValueError):
                    scan_uid = 0
                real_scan = bool(
                    scan_uid > 0
                    or scan_user.get("openid")
                    or scan_user.get("musicId")
                    or scan_user.get("uin")
                )
                if status in ("scanned", "confirmed") and not real_scan:
                    # 伪扫码：忽略该状态，不提示、不触发 complete，继续等待真实扫码
                    self._log_info(
                        f"忽略伪扫码状态（scanUser 无真实用户: {str(scan_user)[:60]}）"
                    )
                    status = "wait"
                if status in ("scanned", "confirmed") and not task["notifiedScan"]:
                    task["notifiedScan"] = True
                if data.get("userMessage"):
                    msg = str(data["userMessage"])
                    # 同一条提示只转发一次，避免 API 缓存消息导致每 tick 刷屏
                    if task.get("lastUserMessage") != msg:
                        task["lastUserMessage"] = msg
                        await self._reply(event, msg)
                    # 致命消息：二维码已被其它 APP 扫描 / 已失效，轮询无法再完成
                    # （\binvalid\b 带词边界，避免 "token invalid" 类非致命提示误停）
                    if re.search(
                        r"scanned by another|已被其他|其他.*扫描|二维码.*失效|已失效|\binvalid\b",
                        msg,
                        re.IGNORECASE,
                    ):
                        task["stopped"] = True
                        self._pop_task_if_current(user_id, task)
                        await self._reply(
                            event,
                            "二维码已失效（可能被其它 APP 扫描），"
                            "请重新发送 #qqm登录qq 获取新二维码",
                        )
                        return
                ok_info = await self._pick_login_success(body or {})
                if ok_info and ok_info.get("ok"):
                    # status=="success" 即登录完成（临时 key 稍后异步升级，hasKey 可能暂为
                    # False，见开发指南 FAQ"扫码成功但 hasKey=false"）——必须停止轮询
                    await _finish_ok(ok_info)
                    return
                if status in ("expired", "cancel", "loginFailed"):
                    task["stopped"] = True
                    self._pop_task_if_current(user_id, task)
                    await self._reply(
                        event,
                        "登录已取消或失败（二维码可能已过期），请重新 #qqm登录qq",
                    )
                    return
                if (
                    status in ("scanned", "confirmed")
                    and task["completeTried"] < 2
                    and elapsed > 10
                ):
                    task["completeTried"] += 1
                    try:
                        done = await qqapi.request(
                            "/login/qr/complete",
                            {"qrcodeID": qrcode_id},
                            "post",
                            user_key,
                        )
                        info = await self._pick_login_success(done or {})
                        if info and info.get("ok") and info.get("hasKey"):
                            await _finish_ok(info)
                            return
                    except Exception:
                        pass
                if task["notifiedScan"] and elapsed > 12 and task["statusBaseline"]:
                    try:
                        st = await qqapi.request("/login/status", {}, "get", user_key)
                        d = (st or {}).get("data") or {}
                        base = task["statusBaseline"]
                        changed = (
                            d.get("login")
                            and d.get("uin")
                            and d.get("hasKey")
                            and (
                                not base.get("login")
                                or not base.get("hasKey")
                                or str(d.get("uin")) != str(base.get("uin") or "")
                            )
                        )
                        if changed:
                            await _finish_ok(
                                {
                                    "uin": d.get("uin"),
                                    "nick": d.get("nick"),
                                    "hasKey": d.get("hasKey"),
                                    "channel": "status-poll",
                                }
                            )
                            return
                    except Exception:
                        pass
            except Exception as err:
                task["failStreak"] += 1
                if task["failStreak"] == 5:
                    await self._reply(event, f"轮询暂时失败：{err}（继续重试）")
                if task["failStreak"] >= 25:
                    task["stopped"] = True
                    self._pop_task_if_current(user_id, task)
                    await self._reply(event, "轮询失败过多，请检查 API 或自行获取ck")
                    return
            finally:
                task["busy"] = False
            if (
                not task["stopped"]
                and self._active_logins.get(user_id, {}).get("qrcodeID") == qrcode_id
            ):
                task["timer"] = loop.call_later(
                    task.get("pollInterval", 2), lambda: asyncio.create_task(_tick())
                )

        loop = asyncio.get_event_loop()
        task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

    @filter.regex(
        re.compile(
            r"^#?(qqm(登录|登陆)(微信|wx)?|qq音乐(登录|登陆)(微信|wx)?)$",
            re.IGNORECASE,
        ),
        priority=6,
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def start_webqr_login(self, event: AstrMessageEvent):
        """#qqm登录 无感扫码登录（一张 QQ 码，主人）"""


        cfg = self._cfg()
        if not cfg.get("enable", True):
            return
        if cfg.get("qrLoginEnable") is False:
            await self._reply(event, "扫码登录已在配置中关闭")
            event.stop_event()
            return
        self._stop_poll(self._user_key(event))
        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在生成登录二维码…")
            body = await qqapi.request("/login/webqr", {}, "post", user_key)
            data = qqapi.unwrap_data(body)
            if not data.get("sessionId") or not (
                data.get("qrcodeWx") or data.get("qrcodeQq")
            ):
                await self._reply(
                    event,
                    f"获取二维码失败：{(body or {}).get('errMsg') or '未知错误'}",
                )
                event.stop_event()
                return
            session_id = data.get("sessionId")
            qrcode_wx = data.get("qrcodeWx")
            qrcode_qq = data.get("qrcodeQq")
            try:
                expires_in = int(data.get("expiresIn") or 180)
            except (TypeError, ValueError):
                expires_in = 180
            # 平台取巧：机器人跑在 QQ 平台，命令发起者必是 QQ 用户 → 只发 QQ 码
            # （QQ 用户扫 QQ 码 = 登录自己的 QQ 音乐账号，同时覆盖 QQ音乐 App 用户）
            # 微信场景极少，用 #qqm登录微信 显式请求微信码（官方无通用码，微信/QQ 各占独立 OAuth）
            want_wx = bool(re.search(r"微信|wx", event.message_str, re.IGNORECASE))
            if want_wx and qrcode_wx:
                codes = [("微信", qrcode_wx)]
            elif qrcode_qq:
                codes = [("QQ", qrcode_qq)]
            elif qrcode_wx:
                codes = [("微信", qrcode_wx)]
            else:
                codes = [
                    c
                    for c in (("QQ", qrcode_qq), ("微信", qrcode_wx))
                    if c[1] and c[1].startswith("data:")
                ]
            # 逐张发图（与 JS 版一致），最后统一发文本提示
            img_sent = 0
            for _, code in codes:
                if not (code and code.startswith("data:")):
                    continue
                try:
                    b64 = code.split(",", 1)[1]
                    qr_path = await self._save_qr_image(b64)
                    await self._send_chain(event, Image.fromFileSystem(qr_path))
                    img_sent += 1
                    loop = asyncio.get_event_loop()
                    loop.call_later(120, lambda p=qr_path: self._safe_unlink(p))
                except Exception as err:
                    self._log_warn(f"二维码图片发送失败: {err}")
                    continue  # 单张失败继续
            if len(codes) == 1:
                tips = f"请用{codes[0][0]}扫一扫，确认后自动登录"
            else:
                tips = (
                    "，".join(f"{label}码用{label}扫" for label, _ in codes)
                    + "，确认后自动登录"
                )
            tip_text = "\n".join([tips, f"二维码 {round(expires_in / 60)} 分钟内有效"])
            if img_sent < len(codes):
                tip_text += "\n（图片发送失败可重新发命令）"
            await self._reply(event, tip_text)
            self._start_webqr_poll(event, session_id, expires_in)
        except Exception as err:
            await self._reply(event, f"扫码登录失败：{err}")
        event.stop_event()

    def _start_webqr_poll(
        self, event: AstrMessageEvent, session_id: str, expires_in: int
    ):


        user_id = str(event.get_sender_id())
        user_key = user_id
        started = time.time()
        max_sec = min(expires_in, 180)
        task = {
            "sessionId": session_id,
            "stopped": False,
            "busy": False,
            "failStreak": 0,
        }
        self._register_task(user_id, task)

        async def _finish_ok(info):
            if task["stopped"]:
                return
            task["stopped"] = True
            timer = task.get("timer")
            if timer is not None:
                with contextlib.suppress(Exception):
                    timer.cancel()
            self._pop_task_if_current(user_id, task)
            await self._on_login_success(event, info)

        async def _tick():
            if task["stopped"]:
                return
            if task["busy"]:
                loop.call_later(0.8, lambda: asyncio.create_task(_tick()))
                return
            if time.time() - started > max_sec:
                task["stopped"] = True
                self._pop_task_if_current(user_id, task)
                await self._reply(event, "二维码已过期，请重新扫码")
                return
            task["busy"] = True
            try:
                body = await qqapi.request(
                    "/login/webqr/check", {"sessionId": session_id}, "get", user_key
                )
                data = (body or {}).get("data") or {}
                status = data.get("status") or "wait"
                task["failStreak"] = 0
                if status == "success" and data.get("hasKey"):
                    await _finish_ok(
                        {
                            "uin": data.get("uin"),
                            "nick": data.get("nick"),
                            "hasKey": True,
                            "channel": "webqr",
                        }
                    )
                    return
                if status in ("expired", "error"):
                    task["stopped"] = True
                    self._pop_task_if_current(user_id, task)
                    await self._reply(
                        event,
                        data.get("error") or "二维码已过期或扫码失败，请重新扫码",
                    )
                    return
            except Exception as err:
                task["failStreak"] += 1
                if task["failStreak"] == 5:
                    await self._reply(event, f"轮询暂时失败：{err}（继续重试）")
                if task["failStreak"] >= 25:
                    task["stopped"] = True
                    self._pop_task_if_current(user_id, task)
                    await self._reply(event, "轮询失败过多，请检查 API 或自行获取ck")
                    return
            finally:
                task["busy"] = False
            if (
                not task["stopped"]
                and self._active_logins.get(user_id, {}).get("sessionId") == session_id
            ):
                task["timer"] = loop.call_later(
                    2.5, lambda: asyncio.create_task(_tick())
                )

        loop = asyncio.get_event_loop()
        task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

    @filter.regex(
        re.compile(
            r"^#?(qqm状态|qqm登录状态|qqm登陆状态|qq音乐状态|qq状态|qms)$",
            re.IGNORECASE,
        ),
        priority=6,
    )
    async def login_status(self, event: AstrMessageEvent):
        """#qqm状态 / #qms 登录状态卡片"""


        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在生成 QQ 音乐状态卡片…")
            api_hint = ""
            try:
                st = await qqapi.request("/login/status", {}, "get", user_key)
                d = (st or {}).get("data") or {}
                if d.get("login"):
                    key_txt = "有" if d.get("hasKey") else "无"
                    ref_txt = "有" if d.get("hasRefresh") else "无"
                    api_hint = (
                        f"API已登录 uin={d.get('uin')} key={key_txt} refresh={ref_txt}"
                    )
                    if d.get("keyAgeSec") is not None:
                        api_hint += f" age={d['keyAgeSec']}s"
                else:
                    api_hint = "API 显示未登录"
                self._log_info(api_hint)
            except Exception as err:
                api_hint = f"API状态查询失败: {err}"
            data = await self._build_status_data(user_key)
            if not data.get("loggedIn") and api_hint:
                data["vipExpireText"] = api_hint
            url = await self._render_card(event, data, "qqmusic-status")
            if url:
                await self._send_chain(event, Image.fromFileSystem(url))
            else:
                await self._reply(
                    event,
                    cardlib.format_status_text(data)
                    + (f"\n{api_hint}" if api_hint else ""),
                )
        except Exception as err:
            self._log_warn(f"状态卡片失败: {err}")
            await self._reply(event, f"获取状态失败：{err}")
        event.stop_event()

    async def _build_status_data(self, user_key: str = "") -> dict:

        cfg = self._cfg()
        status = {}
        cookie = {}
        api_ok = True
        api_error = ""
        try:
            st = await qqapi.request("/login/status", {}, "get", user_key)
            status = (st or {}).get("data") or {}
        except Exception as e:
            api_ok = False
            api_error = str(e)
            status = {"login": False}
        try:
            c = await qqapi.user_cookie(user_key)
            cookie = (
                (c or {}).get("server")
                if isinstance((c or {}).get("server"), dict)
                else (c or {}).get("data") or {}
            )
            if not isinstance(cookie, dict):
                cookie = {}
        except Exception:
            cookie = {}

        logged_in = bool(
            status.get("login") or (status.get("uin") and status.get("hasKey"))
        )
        uin = re.sub(
            r"\D",
            "",
            str(
                status.get("uin")
                or cookie.get("uin")
                or cookie.get("qqmusic_uin")
                or ""
            ),
        )
        key = cookie.get("qm_keyst") or cookie.get("qqmusic_key") or ""
        quality = cfg.get("quality") or "flac"
        quality_label = QUALITY_LABEL.get(quality, quality)

        # 资料
        nickname = "未登录"
        avatar_url = ""
        if logged_in and uin:
            try:
                body = await qqapi.user_detail(uin, user_key)
                d = qqapi.unwrap_data(body)
                creator = d.get("creator") or d.get("base") or d
                nickname = (
                    (
                        creator.get("nick")
                        or creator.get("nickname")
                        or creator.get("name")
                        or d.get("nickname")
                        or d.get("nick")
                        or ""
                    )
                    if isinstance(creator, dict)
                    else ""
                )
                avatar_url = (
                    (
                        creator.get("headurl")
                        or creator.get("avatarUrl")
                        or creator.get("avatar")
                        or d.get("headurl")
                        or d.get("avatarUrl")
                        or ""
                    )
                    if isinstance(creator, dict)
                    else ""
                )
            except Exception:
                pass
            if not avatar_url and re.match(r"^\d{5,}$", str(uin)):
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"
            if not nickname:
                nickname = f"用户 {self._mask_uin(uin)}"
        else:
            avatar_url = os.path.join(PLUGIN_DIR, "resources", "img", "logo.png")

        vip_title = "普通用户"
        vip_state_text = "未开通 / 未检测"
        vip_expire_text = "登录后可解锁付费曲与更高音质解析"
        if not api_ok:
            vip_title = "API 异常"
            vip_state_text = "无法连接"
            vip_expire_text = api_error or "请检查 apiBase 与 API 是否启动"
        elif logged_in:
            vip_title = "已绑定账号"
            vip_state_text = "Key 可刷新" if status.get("hasRefresh") else "已登录"
            vip_expire_text = (
                f"Key 相关时效字段: {cookie.get('keyExpiresIn')}"
                if cookie.get("keyExpiresIn")
                else "建议定期 #qqm登录 保持高音质可用"
            )

        avatar_is_photo = bool(
            logged_in
            and avatar_url
            and "/resources/img/logo" not in str(avatar_url)
            and not str(avatar_url).startswith("file")
        )

        # 多账号 Token 绑定槽提示（对齐原版 status-card 四分支）
        bound = bool(status.get("bound"))
        user_key_slot = status.get("userKey") or ""
        if not api_ok:
            subtitle = "API 连接失败"
        elif logged_in and bound:
            subtitle = f"Token 绑定槽 {user_key_slot or 'default'} · 可扫码更新"
        elif logged_in:
            subtitle = "账号已绑定到 API"
        elif bound:
            subtitle = "本 Token 尚未扫码 · 发 #qqm登录 绑定 QQ 音乐"
        else:
            subtitle = "发送 #qqm登录 扫码绑定"

        return {
            "title": "QQ音乐状态",
            "loggedIn": logged_in,
            "nickname": cookie.get("nick") or nickname,
            "subtitle": subtitle,
            "uin": self._mask_uin(uin) if logged_in else "-",
            "loginTypeText": self._login_type_text(cookie, status),
            "apiBase": cardlib.mask_api_base(cfg.get("apiBase", "")),
            "keyStatus": (
                f"API 保管 {self._mask_key(key)}"
                + (" · 可刷新" if status.get("hasRefresh") else "")
            )
            if logged_in
            else "未配置",
            "vipTitle": vip_title,
            "vipStateText": vip_state_text,
            "musicQuality": quality_label,
            "vipExpireText": vip_expire_text,
            "stats": [
                {
                    "label": "点歌",
                    "value": "开"
                    if cfg.get("enableSongRequest") is not False
                    else "关",
                },
                {
                    "label": "解析",
                    "value": "开" if cfg.get("enableResolve") is not False else "关",
                },
                {
                    "label": "降级",
                    "value": "开" if cfg.get("qualityFallback") is not False else "关",
                },
            ],
            "footer": "QQMusic Plugin · 状态卡片",
            "avatarUrl": avatar_url,
            "avatarIsPhoto": avatar_is_photo,
        }

    def _login_type_text(self, cookie: dict, status: dict) -> str:
        t = 0
        try:
            t = int(
                status.get("login_type")
                or cookie.get("login_type")
                or cookie.get("tmeLoginType")
                or 0
            )
        except Exception:
            t = 0
        if str(cookie.get("tmeLoginType")) == "1" or (t == 2 and cookie.get("wxuin")):
            return "微信登录"
        if str(cookie.get("tmeLoginType")) == "2" or t == 1:
            return "QQ 登录"
        if t == 2:
            return "微信登录"
        if cookie.get("qm_keyst") or cookie.get("qqmusic_key") or status.get("hasKey"):
            return "扫码登录"
        return "未登录"

    def _mask_uin(self, uin: str = "") -> str:
        s = str(uin or "")
        if not s or s == "0":
            return "-"
        if len(s) <= 4:
            return s
        return f"{s[:3]}***{s[-2:]}"

    def _mask_key(self, key: str = "") -> str:
        s = str(key or "")
        if not s:
            return "无"
        if len(s) < 12:
            return "已配置"
        return f"{s[:6]}…{s[-4:]}"

    @filter.regex(
        re.compile(
            r"^#?(qqm登出|qqm注销|qqm解绑|qq音乐登出|qq音乐解绑)$", re.IGNORECASE
        ),
        priority=6,
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def logout(self, event: AstrMessageEvent):
        """#qqm登出 清除登录态（主人）"""


        self._stop_poll(self._user_key(event))
        user_key = self._user_key(event)
        try:
            await qqapi.request("/login/logout", {}, "post", user_key)
            await self._reply(event, "已解除登录绑定")
        except Exception as err:
            await self._reply(event, f"登出失败：{err}")
        event.stop_event()

    @filter.regex(
        re.compile(r"^#?qqm(同步|拉取|sync)(登录态)?$", re.IGNORECASE),
        priority=6,
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sync_from_api(self, event: AstrMessageEvent):
        """#qqm同步 从 API 同步登录态（主人）"""


        user_key = self._user_key(event)
        try:
            meta = await qqapi.pull_login_meta(user_key)
            if not meta.get("login") or not meta.get("hasKey"):
                await self._reply(event, "API 当前未登录，请先 #qqm登录")
                event.stop_event()
                return
            lines = ["✅ 登录态正常"]
            if meta.get("uin"):
                lines.append(f"uin: {meta['uin']}")
            if meta.get("nick"):
                lines.append(f"昵称: {meta['nick']}")
            if meta.get("hasRefresh"):
                lines.append("含 refresh，可自动续期")
            else:
                lines.append("⚠️ 无 refresh")
            if meta.get("keyAgeSec") is not None:
                lines.append(f"key 已用 {meta['keyAgeSec']}s")
            await self._reply(event, "\n".join(lines))
        except Exception as err:
            await self._reply(event, f"查询失败：{err}")
        event.stop_event()

    @filter.regex(
        re.compile(r"^#?qqm(刷新|续期|refresh)(登录|key)?$", re.IGNORECASE),
        priority=6,
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def refresh_key(self, event: AstrMessageEvent):
        """#qqm刷新 续期 key（主人）"""


        user_key = self._user_key(event)
        try:
            await self._reply(event, "正在刷新登录 key…")
            body = await qqapi.refresh_login(user_key)
            d = qqapi.unwrap_data(body)
            result = (body or {}).get("result")
            if result is not None and result not in (100, 0):
                lines = [
                    f"刷新失败：{(body or {}).get('errMsg') or result}",
                    (body or {}).get("tip") or d.get("tip") or "",
                    "请重新 #qqm登录",
                ]
                await self._reply(event, "\n".join(x for x in lines if x))
                event.stop_event()
                return
            lines = ["✅ key 已刷新"]
            if d.get("uin"):
                lines.append(f"uin: {d['uin']}")
            if d.get("changed") is False:
                lines.append("（key 未变化）")
            if d.get("hasRefresh") is False:
                lines.append("⚠️ 无 refresh，过期后需重新扫码")
            elif d.get("hasRefresh"):
                lines.append("含 refresh，后续可自动续期")
            await self._reply(event, "\n".join(lines))
        except Exception as err:
            await self._reply(event, f"刷新失败：{err}\n请重新 #qqm登录")
        event.stop_event()

    @filter.regex(
        re.compile(r"^#?(qqm绑定|qqm导入)(?:\s.*)?$", re.IGNORECASE), priority=6
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bind_manual(self, event: AstrMessageEvent):
        """#qqm绑定 qqmusic://... DeepLink 导入（主人）"""

        text = event.message_str.strip()
        m = re.match(r"^#?(?:qq|QQ)m(?:绑定|导入)\s*(.+)$", text, re.IGNORECASE)
        raw = m.group(1).strip() if m else ""
        if not raw:
            await self._reply(event, "用法：#qqm绑定 qqmusic://...")
            event.stop_event()
            return
        try:
            body = await qqapi.request(
                "/login/deeplink", {"url": raw}, "post", self._user_key(event)
            )
            d = (body or {}).get("data") or {}
            await self._on_login_success(
                event, {**d, "channel": d.get("channel") or "deeplink"}
            )
        except Exception as err:
            await self._reply(event, f"绑定失败：{err}")
        event.stop_event()

    @filter.regex(r"qqmusic://")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def import_deeplink(self, event: AstrMessageEvent):
        """识别 qqmusic:// DeepLink 并导入登录态（主人）"""


        text = event.message_str
        m = re.search(r"qqmusic://[^\s]+", text, re.IGNORECASE)
        if not m:
            return
        try:
            body = await qqapi.request(
                "/login/deeplink", {"url": m.group(0)}, "post", self._user_key(event)
            )
            d = (body or {}).get("data") or {}
            self._stop_poll(self._user_key(event))
            await self._on_login_success(
                event, {**d, "channel": d.get("channel") or "deeplink"}
            )
        except Exception as err:
            await self._reply(event, f"DeepLink 导入失败：{err}")
        event.stop_event()

    # ══════════════════ 管理 ══════════════════

    @filter.regex(
        re.compile(r"^#?(qqm设置|qqm配置|qq音乐设置)$", re.IGNORECASE), priority=6
    )
    async def show_config(self, event: AstrMessageEvent):
        """#qqm设置 查看当前配置"""


        try:
            data = await self._build_settings_data(event)
            url = await self._render_card(event, data, "qqmusic-settings")
            if url:
                await self._send_chain(event, Image.fromFileSystem(url))
                event.stop_event()
                return
        except Exception as err:
            self._log_warn(f"设置卡片渲染失败，回退文本: {err}")
        # 文本兜底
        c = self._cfg()
        login_line = "login: (查询失败)"
        try:
            st = await qqapi.request("/login/status", {}, "get", self._user_key(event))
            d = (st or {}).get("data") or {}
            login_line = (
                f"login: 已绑定 uin={d.get('uin')} ({d.get('nick')})"
                if d.get("login")
                else "login: 未绑定（#qqm登录 扫码）"
            )
        except Exception:
            pass
        adapter_line = f"adapter: {self._platform_name(event)}"
        try:
            version_line = f"version: v{_get_local_version(PLUGIN_DIR)}"
        except Exception:
            version_line = ""
        await self._reply(
            event,
            "\n".join(
                [
                    "【QQ音乐插件配置】",
                    f"enable: {c.get('enable', True)}",
                    f"apiBase: {cardlib.mask_api_base(c.get('apiBase', ''))}",
                    login_line,
                    adapter_line,
                    version_line,
                    f"点歌: {c.get('enableSongRequest', True)}"
                    f"  解析: {c.get('enableResolve', True)}",
                    f"音质: {c.get('quality', 'auto')}"
                    f"（自动降级: {c.get('qualityFallback', True) is not False}）"
                    f"  列表: {c.get('maxList', 10)}",
                    f"语音: {c.get('sendVocal', True)}  群文件: {c.get('uploadFile', True)}",
                    f"原生卡: {c.get('sendNativeCard', False)}"
                    f"  自定义卡: {c.get('sendCustomCard', False)}",
                    "",
                    "主人命令：",
                    "#qqm登录          无感扫码（QQ 码，覆盖 QQ/App，主通道）",
                    "#qqm登录微信      无感扫码（微信码）",
                    "#qqm登录qq        QQ音乐 App 扫码（备用）",
                    "#qqm状态 / #qms   状态图片卡片",
                    "#qqm绑定 deeplink",
                    "#qqm api <地址>   （设置 API 地址）",
                    "#qqm 开启点歌 / #qqm 关闭解析",
                    "#qqm 音质 flac",
                    "#qqm 测试",
                ]
            ),
        )
        event.stop_event()

    def _platform_name(self, event: AstrMessageEvent) -> str:
        try:
            p = event.platform
            return getattr(p, "name", None) or type(p).__name__ or "unknown"
        except Exception:
            return "unknown"

    async def _build_settings_data(self, event: AstrMessageEvent) -> dict:
        c = self._cfg()
        login = {"ok": False, "text": "查询失败"}
        try:
            st = await qqapi.request("/login/status")
            d = (st or {}).get("data") or {}
            if d.get("login"):
                login = {
                    "ok": True,
                    "text": f"已绑定{(' · ' + d.get('nick', '')) if d.get('nick') else ''}",
                }
            else:
                login = {"ok": False, "text": "未绑定（#qqm登录）"}
        except Exception:
            login = {"ok": False, "text": "API 异常"}

        q = c.get("quality") or "auto"
        quality_label = QUALITY_LABEL.get(q, str(q).upper())
        api_base_view = cardlib.mask_api_base(c.get("apiBase", ""))
        api_hint = (
            f"API · {api_base_view.replace('https://', '').replace('http://', '')}"
            if c.get("apiBase")
            else "API 未配置"
        )
        def on_off(v) -> str:
            return "关" if v is False else "开"

        return {
            "title": "QQ音乐设置",
            "subtitle": "当前插件运行配置一览",
            "apiBase": api_base_view,
            "apiHint": api_hint,
            "quality": quality_label,
            "loginOk": login["ok"],
            "tiles": [
                {
                    "label": "点歌",
                    "value": on_off(c.get("enableSongRequest")),
                    "on": c.get("enableSongRequest") is not False,
                },
                {
                    "label": "解析",
                    "value": on_off(c.get("enableResolve")),
                    "on": c.get("enableResolve") is not False,
                },
                {
                    "label": "列表卡",
                    "value": on_off(c.get("renderListCard")),
                    "on": c.get("renderListCard") is not False,
                },
                {
                    "label": "语音",
                    "value": on_off(c.get("sendVocal")),
                    "on": c.get("sendVocal") is not False,
                },
                {
                    "label": "群文件",
                    "value": on_off(c.get("uploadFile")),
                    "on": c.get("uploadFile") is not False,
                },
                {
                    "label": "降级",
                    "value": on_off(c.get("qualityFallback")),
                    "on": c.get("qualityFallback") is not False,
                },
            ],
            "rows": [
                {"k": "API", "v": api_base_view},
                {"k": "登录", "v": login["text"]},
                {"k": "适配器", "v": self._platform_name(event)},
                {
                    "k": "音质",
                    "v": quality_label
                    + (" · 自动降级" if c.get("qualityFallback") is not False else ""),
                },
                {"k": "列表数", "v": str(int(c.get("maxList") or 10))},
                {
                    "k": "发送",
                    "v": (
                        f"语音 {on_off(c.get('sendVocal'))}"
                        f" / 文件 {on_off(c.get('uploadFile'))}"
                        f" / 原生卡 {on_off(c.get('sendNativeCard'))}"
                        f" / 自定义卡 {on_off(c.get('sendCustomCard'))}"
                        + (" / 禁高清语音" if c.get("disableHighQualityVocal") else "")
                    ),
                },
            ],
            "commands": [
                {
                    "name": "无感扫码",
                    "desc": "主通道：一张 QQ 码覆盖 QQ / QQ音乐 App 用户",
                    "example": "#qqm登录",
                },
                {
                    "name": "微信扫码",
                    "desc": "无感扫码（微信码，备用）",
                    "example": "#qqm登录微信",
                },
                {
                    "name": "App 扫码",
                    "desc": "QQ音乐 App 扫码（MQTT 备用通道）",
                    "example": "#qqm登录qq",
                },
                {
                    "name": "状态卡片",
                    "desc": "查看当前插件运行状态",
                    "example": "#qqm状态",
                },
                {
                    "name": "改 API",
                    "desc": "切换 qqmusic-api 地址（主人）",
                    "example": "#qqm api <地址>",
                },
                {
                    "name": "改音质",
                    "desc": "设置最高播放音质",
                    "example": "#qqm 音质 flac",
                },
                {
                    "name": "开关点歌",
                    "desc": "开启 / 关闭点歌功能",
                    "example": "#qqm 开启点歌",
                },
                {
                    "name": "连通测试",
                    "desc": "测试 API 是否正常响应",
                    "example": "#qqm 测试",
                },
            ],
            "tip": "详细开关可在 AstrBot 管理面板修改",
        }

    @filter.regex(r"^#?(qq|QQ)m\s*api\s*(https?://\S+)$")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_api(self, event: AstrMessageEvent):
        """#qqm api <地址> 设置 API 地址（主人）"""


        m = re.search(r"api\s*(https?://\S+)", event.message_str, re.IGNORECASE)
        url = m.group(1).rstrip("/") if m else ""
        if not url:
            await self._reply(event, "用法：#qqm api http://你的API地址:端口")
            event.stop_event()
            return
        self.config["apiBase"] = url
        try:
            self.config.save_config()
        except Exception as e:
            self._log_warn(f"配置保存失败: {e}")
        await self._reply(event, "API 地址已更新")
        event.stop_event()

    @filter.regex(r"^#?(qq|QQ)m\s*(开启|关闭)(点歌|解析)$")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle(self, event: AstrMessageEvent):
        """#qqm 开启/关闭 点歌/解析 功能开关（主人）"""


        m = re.search(r"(开启|关闭)(点歌|解析)", event.message_str)
        if not m:
            event.stop_event()
            return
        on = m.group(1) == "开启"
        key = "enableSongRequest" if m.group(2) == "点歌" else "enableResolve"
        self.config[key] = on
        try:
            self.config.save_config()
        except Exception as e:
            self._log_warn(f"配置保存失败: {e}")
        await self._reply(event, f"已{m.group(1)}{m.group(2)}")
        event.stop_event()

    @filter.regex(
        r"^#?(qq|QQ)m\s*音质\s*(128|m4a|320|flac|ape|hires|atmos|master|atmos_master)$"
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_quality(self, event: AstrMessageEvent):
        """#qqm 音质 <档位> 设置最高音质（主人）"""

        m = re.search(
            r"音质\s*(128|m4a|320|flac|ape|hires|atmos|master|atmos_master)",
            event.message_str,
            re.IGNORECASE,
        )
        q = m.group(1).lower() if m else ""
        if not q:
            event.stop_event()
            return
        self.config["quality"] = q
        try:
            self.config.save_config()
        except Exception as e:
            self._log_warn(f"配置保存失败: {e}")
        await self._reply(
            event,
            f"默认最高音质已设为 {q}\n"
            "可选: 128 / m4a / 320 / flac / ape / hires / atmos / master / atmos_master",
        )
        event.stop_event()

    @filter.regex(
        re.compile(r"^#?(?:qq|QQ)m\s*(?:测试|ping)$|^#?(?:qq|QQ)音乐测试$", re.IGNORECASE),
        priority=6,
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ping(self, event: AstrMessageEvent):
        """#qqm 测试 测试 API 连通（主人）"""


        try:
            data = await qqapi.request("/")
            await self._reply(
                event,
                f"API 正常\nroutes: {len(data.get('routes') or [])}"
                f"\n已登录: {len(data.get('accounts') or [])}",
            )
        except Exception as err:
            await self._reply(event, f"API 不可用：{err}")
        event.stop_event()

    @filter.regex(
        re.compile(r"^#?qqm\s*(账号|accounts|已登录)$", re.IGNORECASE), priority=6
    )
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_accounts(self, event: AstrMessageEvent):
        """#qqm账号 已登录账号列表（主人）"""


        try:
            lst = await qqapi.list_accounts()
            if not lst:
                await self._reply(event, "当前没有任何账号登录")
                event.stop_event()
                return
            out = []
            for i, a in enumerate(lst):
                nick = a.get("nick")
                out.append(
                    f"{i + 1}. userKey={a.get('userKey')} uin={a.get('uin')}"
                    + (f" ({nick})" if nick else "")
                )
            lines = out
            await self._reply(event, f"已登录账号 {len(lst)} 个：\n" + "\n".join(lines))
        except Exception as err:
            await self._reply(event, f"查询失败：{err}")
        event.stop_event()

    async def terminate(self):
        for uid in list(self._active_logins.keys()):
            self._stop_poll(uid)
        try:
            from .render import close as close_renderer

            await close_renderer()
        except Exception:
            pass
