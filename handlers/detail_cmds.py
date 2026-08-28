from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent

try:
    from ..core import api as qqapi
    from ..core import cards as cardlib
    from ..core.service import MusicService
    from .base import Route
except (ImportError, ValueError):
    from core import api as qqapi
    from core import cards as cardlib
    from core.service import MusicService
    from handlers.base import Route


async def get_lyric(service: MusicService, event: AstrMessageEvent):
    """#qqm歌词 关键词 / 歌曲链接 / 序号：支持直接按序号、关键词、链接查词"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*歌词\s*(.*)$", event.message_str.strip(), re.IGNORECASE
    )
    if not m:
        return
    key = m.group(1).strip()
    if not key:
        await service.reply(event, "用法：#qqm歌词 关键词 / 歌曲链接 / 序号")
        event.stop_event()
        return

    user_key = service.user_key(event)
    songmid = key
    song_meta = {"songName": key, "singerName": "", "cover": "", "albumName": ""}

    if re.match(r"^\d{1,2}$", key):
        scope = service.scope(event)
        session = await cardlib.SessionStore.get(service.plugin, scope)
        n = int(key)
        if session and session.get("type") in ("topCategory", "recommend"):
            await service.reply(
                event,
                "当前是榜单分类列表，请先 #qqm排行 榜单名 列出歌曲"
                if session.get("type") == "topCategory"
                else "当前是推荐歌单列表，请先 #qqm推荐听序号 列出歌曲",
            )
            event.stop_event()
            return
        if session and session.get("type") == "mvList":
            await service.reply(event, "当前列表是 MV，请用 #qqmMV 播放 序号；查歌词请先 #qqm点歌")
            event.stop_event()
            return
        session_data = (session or {}).get("data") or []
        if session_data and 1 <= n <= len(session_data):
            song = session_data[n - 1]
            mid = song.get("songmid") or song.get("songMid") or ""
            if not mid:
                await service.reply(event, "该歌曲缺少 songmid，无法查询歌词，请用 #qqm歌词 关键词")
                event.stop_event()
                return
            songmid = mid
            song_meta = {
                "songName": song.get("songName") or key,
                "singerName": song.get("singerName") or "",
                "cover": song.get("cover") or "",
                "albumName": song.get("albumName") or "",
            }
        else:
            await service.reply(
                event,
                f"请选择 1-{len(session_data)}"
                if session_data
                else "暂无点歌列表，请先 #qqm点歌，或用 #qqm歌词 关键词",
            )
            event.stop_event()
            return
    elif not re.match(r"^[0-9A-Za-z]{10,}$", key) or re.search(r"[\u4e00-\u9fa5]", key):
        try:
            lst = await qqapi.search_songs(key, page_size=1, user_key=user_key)
            if not lst:
                await service.reply(event, "未找到歌曲")
                event.stop_event()
                return
            first = lst[0]
            songmid = first.get("songmid") or first.get("songMid") or ""
            song_meta = {
                "songName": first.get("songName") or key,
                "singerName": first.get("singerName") or "",
                "cover": first.get("cover") or "",
                "albumName": first.get("albumName") or "",
            }
        except Exception as err:
            await service.reply(event, f"歌词失败：{err}")
            event.stop_event()
            return

    await service.show_lyric_by_meta(event, songmid, song_meta, user_key)
    event.stop_event()


async def get_comment(service: MusicService, event: AstrMessageEvent):
    """#qqm评论 [关键词]：先选歌（回复 #qqm听N）再显示热评"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*评论\s*(.*)$", event.message_str.strip(), re.IGNORECASE
    )
    if not m:
        return
    await service.start_select(
        event,
        "comment",
        m.group(1).strip(),
        label="评论",
        verb="查看评论",
        user_key=service.user_key(event),
    )
    event.stop_event()


async def favorites(service: MusicService, event: AstrMessageEvent):
    """#qqm收藏 我的收藏（需登录）"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    scope = service.scope(event)
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在获取收藏...")
        res = await qqapi.user_favorites(song_num=100, user_key=user_key)
        songs = res.get("songs") or []
        if not songs:
            await service.reply(
                event,
                "📭 我的收藏为空\n"
                "你的 QQ 音乐「我喜欢」歌单还没有收藏任何歌曲\n\n"
                "💡 你可以在 QQ 音乐 App 中收藏歌曲后再来查看",
            )
            event.stop_event()
            return
        title = res.get("title") or "我的收藏"
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {
                "type": "favorites",
                "data": songs,
            },
        )
        if cfg.get("renderListCard", True):
            data = cardlib.build_list_card_data(title, songs, cfg=cfg)
            if await service.reply_card_or_text(
                event,
                tpl_name="qqmusic-list",
                data=data,
                format_text=lambda d: cardlib.format_song_list(songs, title),
            ):
                event.stop_event()
                return
        await service.reply(event, cardlib.format_song_list(songs, title))
    except Exception as err:
        service.log_warn(f"收藏失败: {err}")
        if (
            getattr(err, "code", None) == -1
            or "登录" in str(err)
            or "login" in str(err).lower()
        ):
            await service.reply(event, "收藏失败，请先 #qqm登录 后重试")
        else:
            await service.reply(event, f"收藏失败：{err}")
    event.stop_event()


ROUTES: list[Route] = [
    Route(
        pattern=r"^#?(qq|QQ)m\s*歌词\s*(.*)$",
        name="get_lyric",
        doc="#qqm歌词 关键词 / 歌曲链接 / 序号：支持直接按序号、关键词、链接查词",
        run=get_lyric,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*评论\s*(.*)$",
        name="get_comment",
        doc="#qqm评论 [关键词]：先选歌（回复 #qqm听N）再显示热评",
        run=get_comment,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*收藏$",
        name="favorites",
        doc="#qqm收藏 我的收藏（需登录）",
        run=favorites,
    ),
]
