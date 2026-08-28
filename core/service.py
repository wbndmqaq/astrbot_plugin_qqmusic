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
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File, Image, Plain

try:
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
except (ImportError, ValueError):
    from core import api as qqapi
    from core import cards as cardlib
    from core.delivery import (
        _is_passive_limited,
        _schedule_cleanup,
        deliver_song,
        deliver_video,
        send_native_music_card,
    )
    from core.quality import QUALITY_LABEL

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


def get_local_version(plugin_dir: str = PLUGIN_DIR) -> str:
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


def safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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
                    parts.append(str(d))
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
    _active_logins: ClassVar[dict] = {}

    def __init__(self, plugin: QQMusicPlugin):
        self.plugin = plugin
        self.config = plugin.config
        qqapi.set_config_getter(lambda: self.config or {})

    # ──────────── 基础辅助 ────────────

    def cfg(self) -> dict:
        return self.config or {}

    def log_warn(self, msg: str):
        logger.warning(f"[qqmusic] {msg}")

    def log_info(self, msg: str):
        logger.info(f"[qqmusic] {msg}")

    def plain(self, text: str) -> Plain:
        return Plain(text=text)

    async def send_chain(self, event: AstrMessageEvent, *components):
        comps = [c for c in components if c is not None]
        if not comps:
            return
        mc = MessageChain(chain=list(comps))
        try:
            await event.send(mc)
        except AttributeError:
            import traceback as _tb

            self.log_warn(
                f"send_chain 发送失败（AttributeError）:\n{_tb.format_exc()}"
            )
            texts = []
            for _c in comps:
                t = getattr(_c, "text", None)
                if t:
                    texts.append(str(t))
            if texts:
                try:
                    await event.send(
                        MessageChain(chain=[self.plain("\n".join(texts))])
                    )
                except Exception as _e2:
                    self.log_warn(f"send_chain 文本兜底也失败: {_e2}")

    async def reply(self, event: AstrMessageEvent, text: str):
        try:
            await self.send_chain(event, self.plain(text))
        except Exception as e:
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
            return self.play_view(play)
        except Exception as e:
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
            from jinja2 import Environment

            try:
                from .delivery import get_temp_dir
                from .render import render_html_to_png
                from .tpl_adapter import get_jinja_template
            except ImportError:
                from core.delivery import get_temp_dir
                from core.render import render_html_to_png
                from core.tpl_adapter import get_jinja_template

            tmpl_path = os.path.join(
                PLUGIN_DIR, "resources", "html", tpl_name, f"{tpl_name}.html"
            )
            if not os.path.exists(tmpl_path):
                return None
            tmpl = get_jinja_template(tmpl_path)
            html = Environment().from_string(tmpl).render(data=data)  # noqa: S701
            d = get_temp_dir(self.cfg(), PLUGIN_DIR)
            file_path = os.path.join(
                d, f"card_{tpl_name}_{int(time.time() * 1000)}.png"
            )
            if not await render_html_to_png(html, file_path):
                self.log_warn(
                    f"{tpl_name} 渲染失败"
                    "（playwright 不可用？请确认已安装并执行 playwright install chromium）"
                )
                return None
            asyncio.get_running_loop().call_later(
                120, lambda p=file_path: self.safe_unlink(p)
            )
            return file_path
        except Exception as e:
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
            if url:
                await self.send_chain(event, Image.fromFileSystem(url))
                return True
        except Exception as e:
            self.log_warn(f"{tpl_name} 卡片渲染失败，回退文本: {e}")
        try:
            text = format_text(data)
            if text:
                await self.send_chain(event, self.plain(text))
                return True
        except Exception as e:
            self.log_warn(f"{tpl_name} 文本兜底失败: {e}")
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
        )
        url = await self.render_card(event, card_data, "qqmusic-detail")
        if url:
            await self.send_chain(event, Image.fromFileSystem(url))
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
                page_size = min(int(cfg.get("maxList") or 10), 20)
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
        tip = f"回复 #qqm听N 即可{verb}"
        if cfg.get("renderListCard", True):
            data = cardlib.build_list_card_data(
                keyword, lst, options={"tip": tip}, cfg=cfg
            )
            if await self.reply_card_or_text(
                event,
                tpl_name="qqmusic-list",
                data=data,
                format_text=lambda d: cardlib.format_song_list(lst, keyword)
                + f"\n回复 #qqm听N 即可{verb}",
            ):
                return
        await self.reply(
            event,
            cardlib.format_song_list(lst, keyword)
            + f"\n回复 #qqm听N 即可{verb}",
        )

    def normalize_song(self, item: dict, idx: int = 0) -> dict | None:
        if not isinstance(item, dict):
            return None
        singer = qqapi.singer_text(item)
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        albummid = item.get("albummid") or album.get("mid") or ""
        interval = safe_int(item.get("interval") or item.get("songTime") or 0)
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

    async def show_album_tracks(
        self, event: AstrMessageEvent, alb: dict, tracks: list, scope: str, cfg: dict
    ) -> None:
        title = f"{alb.get('singerName', '')} - {alb.get('albumName', '')}"
        await cardlib.SessionStore.set(
            self.plugin,
            scope,
            {"type": "album", "data": tracks, "album": alb},
        )
        if cfg.get("renderListCard", True):
            data = cardlib.build_list_card_data(
                title,
                tracks,
                {
                    "tip": f"发送 #qqm听序号 播放「{alb.get('albumName', '')}」，"
                    "#qqm听所有 连播整张专辑"
                },
                cfg=self.cfg(),
            )
            if await self.reply_card_or_text(
                event,
                tpl_name="qqmusic-list",
                data=data,
                format_text=lambda d: cardlib.format_song_list(tracks, title),
            ):
                if alb.get("publicTime"):
                    await self.reply(event, f"发行时间：{alb['publicTime']}")
                return
        await self.reply(event, cardlib.format_song_list(tracks, title))
        if alb.get("publicTime"):
            await self.reply(event, f"发行时间：{alb['publicTime']}")

    async def expand_recommend(
        self, event: AstrMessageEvent, session: dict, n: int
    ) -> None:
        lst = session["data"]
        if n < 1 or n > len(lst):
            await self.reply(event, f"请选择 1-{len(lst)}")
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
        cfg = self.cfg()
        if cfg.get("renderListCard", True):
            data = cardlib.build_list_card_data(title, songs, cfg=cfg)
            if await self.reply_card_or_text(
                event,
                tpl_name="qqmusic-list",
                data=data,
                format_text=lambda d: cardlib.format_song_list(songs, title),
            ):
                event.stop_event()
                return
        await self.reply(event, cardlib.format_song_list(songs, title))
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
        pages = [lines[i : i + 36] for i in range(0, len(lines), 36)]
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

    async def show_lyric(
        self, event: AstrMessageEvent, song: dict, user_key: str
    ) -> None:
        songmid = song.get("songmid") or song.get("songMid") or ""
        await self.show_lyric_by_meta(event, songmid, song, user_key)

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
                await self.send_chain(event, Image.fromFileSystem(img_path))
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
            plugin_dir=PLUGIN_DIR,
            download=download,
            extra=[Image.fromFileSystem(img_path)]
            if (img_path and passive_limited)
            else None,
        )
        if not ret.get("ok"):
            fp = ret.get("filePath") or ""
            sent_file = False
            if fp and os.path.exists(fp):
                title_f = mv_obj.get("mvtitle") or mv_obj.get("name") or "MV"
                try:
                    await self.send_chain(
                        event,
                        File(
                            re.sub(r'[\\/:*?"<>|]', "", str(title_f))[:30] + ".mp4",
                            file=fp,
                        ),
                    )
                    sent_file = True
                    _schedule_cleanup(
                        fp, int(self.cfg().get("keepFileSec", 120))
                    )
                except Exception:
                    pass
            if sent_file:
                await self.reply(event, "视频消息发送失败，已改发下载文件")
            else:
                await self.reply(event, f"已改为链接发送（可在线播放/下载）：{url}")
        return True

    async def show_mv(
        self, event: AstrMessageEvent, song: dict, user_key: str
    ) -> None:
        song_vid = song.get("mvVid") or ""
        if not song_vid:
            songmid = song.get("songmid") or ""
            if songmid:
                try:
                    infos = await qqapi.song_info_batch(
                        [songmid], user_key=user_key
                    )
                    song_vid = (infos[0].get("mvVid") if infos else "") or ""
                except Exception:
                    pass
        if not song_vid:
            await self.reply(event, "该曲没有 MV")
            return
        mv_obj = {
            "vid": song_vid,
            "mvtitle": song.get("songName") or "本曲 MV",
            "name": song.get("songName") or "本曲 MV",
            "singerName": song.get("singerName") or "",
            "cover": song.get("cover") or "",
        }
        await self.deliver_mv(event, mv_obj, download=False, user_key=user_key)

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

    # ──────────── 分享与卡片解析 ────────────

    async def handle_resolve(
        self, event: AstrMessageEvent, text: str, cfg: dict
    ) -> bool:
        user_key = self.user_key(event)
        song = None
        from_card = False

        if cfg.get("resolveCards", True):
            card = qqapi.parse_qqmusic_card(text)
            if card:
                from_card = True
                self.log_info(f"识别卡片: {card.get('title')} - {card.get('desc')}")
                song = await self.card_to_song(card, user_key)

        if not song and cfg.get("resolveLinks", True):
            url_match = re.search(
                r"https?://(?:[a-z0-9-]+\.)?(?:y\.qq\.com|c6\.y\.qq\.com)"
                r'[^\\s，。；：！？、（）【】《》»"“”\'`一-龥]*',
                text,
                re.IGNORECASE,
            )
            if url_match:
                url = url_match.group(0)
                self.log_info(f"识别链接: {url}")

                if re.search(
                    r"c6\.y\.qq\.com/base/fcgi-bin/u\?", url, re.IGNORECASE
                ) or re.search(r"[?&]__=", url):
                    try:
                        final_url = await follow_qq_redirect(url)
                        if final_url and final_url != url:
                            url = final_url
                            self.log_info(f"短链跟随: {url}")
                    except Exception as err:
                        self.log_warn(f"短链跟随失败: {err}")

                ext_ids = qqapi.parse_qqmusic_extended_ids(url)
                scope = self.scope(event)

                if ext_ids.get("albummid"):
                    try:
                        result = await qqapi.album_songs(
                            ext_ids["albummid"], user_key=user_key
                        )
                        songs = result.get("list") or []
                        if songs:
                            for s in songs:
                                s["albummid"] = s.get("albummid") or ext_ids["albummid"]
                            await cardlib.SessionStore.set(
                                self.plugin,
                                scope,
                                {
                                    "type": "album",
                                    "data": songs,
                                },
                            )
                            await self.reply(
                                event,
                                f"识别到专辑链接，共{len(songs)}首。发送 #qqm听序号 播放",
                            )
                            return True
                    except Exception as err:
                        self.log_warn(f"专辑解析失败: {err}")

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
                                self.plugin,
                                scope,
                                {
                                    "type": "playlist",
                                    "data": songs,
                                },
                            )
                            await self.reply(
                                event,
                                f"识别到歌单「{title}」，共{len(songs)}首。发送 #qqm听序号 播放",
                            )
                            return True
                    except Exception as err:
                        self.log_warn(f"歌单解析失败: {err}")

                if ext_ids.get("singermid"):
                    try:
                        result = await qqapi.singer_songs(
                            ext_ids["singermid"], page_size=30, user_key=user_key
                        )
                        if result.get("list"):
                            await cardlib.SessionStore.set(
                                self.plugin,
                                scope,
                                {
                                    "type": "singer",
                                    "data": result["list"],
                                },
                            )
                            await self.reply(
                                event,
                                f"识别到歌手链接，热门歌曲{len(result['list'])}首。"
                                "发送 #qqm听序号 播放",
                            )
                            return True
                    except Exception as err:
                        self.log_warn(f"歌手解析失败: {err}")

                ids = qqapi.parse_qqmusic_ids(url)
                song = await self.ids_to_song(ids, text, user_key)

        if not song:
            if from_card:
                await self.reply(event, "识别到 QQ 音乐分享，但未能提取歌曲信息")
                return True
            return False

        play = {
            "url": "",
            "quality": cfg.get("quality") or "flac",
            "qualityLabel": self.quality_label(cfg.get("quality") or "flac"),
        }
        if song.get("songmid"):
            try:
                if not song.get("_detailFetched") and (
                    not song.get("media_mid")
                    or song.get("media_mid") == song.get("songmid")
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
                play = self.play_view(play)
            except Exception as err:
                self.log_warn(f"播放链: {err}")
                play["error"] = str(err)
        else:
            play["error"] = "未能从分享中提取歌曲 songmid，无法取播放链"

        q_label = (
            play.get("qualityLabel")
            or self.quality_label(play.get("quality", ""))
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
            elif pay and safe_int(pay.get("pay_play")) == 1:
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
            tip=fail_hint if (not play.get("url") and fail_hint) else "",
            degrade_note=play.get("degradeNote") or "",
            error=play.get("error") or "",
            cfg=cfg,
        )
        url = await self.render_card(event, card_data, "qqmusic-detail")
        if url:
            await self.send_chain(event, Image.fromFileSystem(url))
        else:
            text_block = [
                f"{prefix}QQ音乐 · 解析完成",
                cardlib.format_detail_text(
                    song, quality_label=q_label, has_url=bool(play.get("url"))
                ),
            ]
            if play.get("degradeNote"):
                text_block.append(f"音质说明：{play['degradeNote']}")
            if fail_hint:
                text_block.append(fail_hint)
            await self.reply(event, "\n".join(t for t in text_block if t))

        await deliver_song(
            self.plugin,
            event,
            song,
            play,
            cfg=self.cfg(),
            plugin_dir=PLUGIN_DIR,
            options=_SKIP_TEXT,
        )
        return True

    async def card_to_song(self, card: dict, user_key: str = "") -> dict | None:
        if card.get("songmid"):
            return await self.ids_to_song(card, "", user_key)
        if card.get("keyword") or card.get("title"):
            kw = (
                card.get("keyword") or f"{card.get('title', '')} {card.get('desc', '')}"
            ).strip()
            lst = await qqapi.search_songs(kw, page_size=5, user_key=user_key)
            if lst:
                hit = lst[0]
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

    async def ids_to_song(
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
                    "_detailFetched": True,
                }
            except Exception as err:
                self.log_warn(f"详情失败: {err}")

        prefix = (
            re.sub(r"@\S+", "", re.sub(r"https?://\S+", "", fallback_text))
            .replace("《", " ")
            .replace("》", " ")
            .strip()
        )
        if prefix:
            lst = await qqapi.search_songs(prefix, page_size=3, user_key=user_key)
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

    # ──────────── 扫码登录与状态 ────────────

    def stop_poll(self, user_id: str):
        t = self._active_logins.get(str(user_id))
        if t:
            t["stopped"] = True
            timer = t.get("timer")
            if timer:
                timer.cancel()
            self._active_logins.pop(str(user_id), None)

    def register_task(self, user_id: str, task: dict):
        old = self._active_logins.get(user_id)
        if old is not None and old is not task:
            old["stopped"] = True
            t = old.get("timer")
            if t is not None:
                with contextlib.suppress(Exception):
                    t.cancel()
        self._active_logins[user_id] = task

    def pop_task_if_current(self, user_id: str, task: dict):
        if self._active_logins.get(user_id) is task:
            self._active_logins.pop(user_id, None)

    async def save_qr_image(self, base64_str: str) -> str:
        import base64

        try:
            from .delivery import _write_bytes, get_temp_dir
        except ImportError:
            from core.delivery import _write_bytes, get_temp_dir

        d = get_temp_dir(self.cfg(), PLUGIN_DIR)
        os.makedirs(d, exist_ok=True)
        file_path = os.path.join(d, f"qr_{int(time.time() * 1000)}.png")
        await asyncio.to_thread(_write_bytes, file_path, base64.b64decode(base64_str))
        return file_path

    async def pick_login_success(self, body: dict) -> dict | None:
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

    async def on_login_success(
        self, event: AstrMessageEvent, info: dict | None = None
    ):
        info = info or {}
        uin = info.get("uin") or ""
        nick = info.get("nick") or ""
        has_key = info.get("hasKey", True)
        user_key = self.user_key(event)
        meta = None
        try:
            meta = await qqapi.pull_login_meta(user_key)
        except Exception as e:
            self.log_warn(f"拉取登录态元信息失败: {e}")
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
        await self.reply(event, "\n".join(lines))
        await asyncio.sleep(0.4)
        try:
            data = await self.build_status_data(user_key)
            url = await self.render_card(event, data, "qqmusic-status")
            if url:
                await self.send_chain(event, Image.fromFileSystem(url))
            else:
                await self.reply(event, cardlib.format_status_text(data))
        except Exception as e:
            self.log_warn(f"登录后状态卡失败: {e}")
            await self.reply(
                event, "登录已成功，但状态卡渲染失败，可手动发送 #qqm状态"
            )

    def safe_unlink(self, path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def start_poll(
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
        self.register_task(user_id, task)

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

        loop = asyncio.get_event_loop()

        async def _finish_ok(info):
            if task["stopped"]:
                return
            task["stopped"] = True
            timer = task.get("timer")
            if timer is not None:
                with contextlib.suppress(Exception):
                    timer.cancel()
            self.pop_task_if_current(user_id, task)
            await self.on_login_success(event, info)

        async def _tick():
            if task["stopped"]:
                return
            if task["busy"]:
                loop.call_later(0.8, lambda: asyncio.create_task(_tick()))
                return
            elapsed = time.time() - started
            if elapsed > max_sec:
                task["stopped"] = True
                self.pop_task_if_current(user_id, task)
                await self.reply(event, "二维码已过期，请重新 #qqm登录")
                return
            elapsed_ms = int(elapsed * 1000)
            task["busy"] = True
            try:
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
                    self.log_info(
                        f"忽略伪扫码状态（scanUser 无真实用户: {str(scan_user)[:60]}）"
                    )
                    status = "wait"
                if status in ("scanned", "confirmed") and not task["notifiedScan"]:
                    task["notifiedScan"] = True
                if data.get("userMessage"):
                    msg = str(data["userMessage"])
                    if task.get("lastUserMessage") != msg:
                        task["lastUserMessage"] = msg
                        await self.reply(event, msg)
                    if re.search(
                        r"scanned by another|已被其他|其他.*扫描|二维码.*失效|已失效|\binvalid\b",
                        msg,
                        re.IGNORECASE,
                    ):
                        task["stopped"] = True
                        self.pop_task_if_current(user_id, task)
                        await self.reply(
                            event,
                            "二维码已失效（可能被其它 APP 扫描），"
                            "请重新发送 #qqm登录qq 获取新二维码",
                        )
                        return
                ok_info = await self.pick_login_success(body or {})
                if ok_info and ok_info.get("ok"):
                    await _finish_ok(ok_info)
                    return
                if status in ("expired", "cancel", "loginFailed"):
                    task["stopped"] = True
                    self.pop_task_if_current(user_id, task)
                    await self.reply(
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
                        info = await self.pick_login_success(done or {})
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
                    await self.reply(event, f"轮询暂时失败：{err}（继续重试）")
                if task["failStreak"] >= 25:
                    task["stopped"] = True
                    self.pop_task_if_current(user_id, task)
                    await self.reply(event, "轮询失败过多，请检查 API 或自行获取ck")
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

        task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

    def start_webqr_poll(
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
        self.register_task(user_id, task)

        loop = asyncio.get_event_loop()

        async def _finish_ok(info):
            if task["stopped"]:
                return
            task["stopped"] = True
            timer = task.get("timer")
            if timer is not None:
                with contextlib.suppress(Exception):
                    timer.cancel()
            self.pop_task_if_current(user_id, task)
            await self.on_login_success(event, info)

        async def _tick():
            if task["stopped"]:
                return
            if task["busy"]:
                loop.call_later(0.8, lambda: asyncio.create_task(_tick()))
                return
            if time.time() - started > max_sec:
                task["stopped"] = True
                self.pop_task_if_current(user_id, task)
                await self.reply(event, "二维码已过期，请重新扫码")
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
                    self.pop_task_if_current(user_id, task)
                    await self.reply(
                        event,
                        data.get("error") or "二维码已过期或扫码失败，请重新扫码",
                    )
                    return
            except Exception as err:
                task["failStreak"] += 1
                if task["failStreak"] == 5:
                    await self.reply(event, f"轮询暂时失败：{err}（继续重试）")
                if task["failStreak"] >= 25:
                    task["stopped"] = True
                    self.pop_task_if_current(user_id, task)
                    await self.reply(event, "轮询失败过多，请检查 API 或自行获取ck")
                    return
            finally:
                task["busy"] = False
            if (
                not task["stopped"]
                and self._active_logins.get(user_id, {}).get("sessionId") == session_id
            ):
                task["timer"] = loop.call_later(
                    task.get("pollInterval", 2), lambda: asyncio.create_task(_tick())
                )

        task["timer"] = loop.call_later(2, lambda: asyncio.create_task(_tick()))

    async def build_status_data(self, user_key: str = "") -> dict:
        cfg = self.cfg()
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
                nickname = f"用户 {self.mask_uin(uin)}"
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
            "uin": self.mask_uin(uin) if logged_in else "-",
            "loginTypeText": self.login_type_text(cookie, status),
            "apiBase": cardlib.mask_api_base(cfg.get("apiBase", "")),
            "keyStatus": (
                f"API 保管 {self.mask_key(key)}"
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

    def login_type_text(self, cookie: dict, status: dict) -> str:
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

    def mask_uin(self, uin: str = "") -> str:
        s = str(uin or "")
        if not s or s == "0":
            return "-"
        if len(s) <= 4:
            return s
        return f"{s[:3]}***{s[-2:]}"

    def mask_key(self, key: str = "") -> str:
        s = str(key or "")
        if not s:
            return "无"
        if len(s) < 12:
            return "已配置"
        return f"{s[:6]}…{s[-4:]}"

    def platform_name(self, event: AstrMessageEvent) -> str:
        try:
            p = event.platform
            return getattr(p, "name", None) or type(p).__name__ or "unknown"
        except Exception:
            return "unknown"

    async def build_settings_data(self, event: AstrMessageEvent) -> dict:
        c = self.cfg()
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
                {"k": "适配器", "v": self.platform_name(event)},
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

    async def terminate(self):
        for uid in list(self._active_logins.keys()):
            self.stop_poll(uid)
