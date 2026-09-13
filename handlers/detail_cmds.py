from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent

from ..core import api as qqapi
from ..core import cards as cardlib
from ..core import messages as msg
from ..core.service import MusicService, follow_qq_redirect
from .base import Route


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
    # 解析目标歌曲：序号 / 链接 / 关键词三个分支；helper 回复并终止后返回 None
    resolved: tuple[str, dict] | None = (key, {"songName": key, "singerName": "", "cover": "", "albumName": ""})
    if re.match(r"^\d{1,2}$", key):
        resolved = await _lyric_from_session(service, event, key)
    elif re.search(r"https?://|y\.qq\.com", key, re.IGNORECASE):
        resolved = await _lyric_from_link(service, event, key, user_key)
    elif not re.match(r"^[0-9A-Za-z]{10,}$", key) or re.search(r"[\u4e00-\u9fa5]", key):
        resolved = await _lyric_by_search(service, event, key, user_key)
    if resolved is None:
        return
    songmid, song_meta = resolved

    await service.show_lyric_by_meta(event, songmid, song_meta, user_key)
    event.stop_event()


def _lyric_meta(key: str, song: dict) -> dict:
    return {
        "songName": song.get("songName") or key,
        "singerName": song.get("singerName") or "",
        "cover": song.get("cover") or "",
        "albumName": song.get("albumName") or "",
    }


async def _lyric_from_session(
    service: MusicService, event: AstrMessageEvent, key: str
) -> tuple[str, dict] | None:
    """按会话序号取歌词目标；分支自身回复并 stop_event 后返回 None。"""
    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope)
    n = int(key)
    if session and session.get("type") in ("topCategory", "recommend"):
        await service.reply(
            event,
            msg.SESSION_TOP_CATEGORY_HINT
            if session.get("type") == "topCategory"
            else msg.SESSION_RECOMMEND_HINT,
        )
        event.stop_event()
        return None
    if session and session.get("type") == "mvList":
        await service.reply(event, "当前列表是 MV，请用 #qqmMV 播放 序号；查歌词请先 #qqm点歌")
        event.stop_event()
        return None
    session_data = (session or {}).get("data") or []
    if not (session_data and 1 <= n <= len(session_data)):
        await service.reply(
            event,
            msg.out_of_range(len(session_data))
            if session_data
            else "暂无点歌列表，请先 #qqm点歌，或用 #qqm歌词 关键词",
        )
        event.stop_event()
        return None
    song = session_data[n - 1]
    mid = song.get("songmid") or song.get("songMid") or ""
    if not mid:
        await service.reply(event, "该歌曲缺少 songmid，无法查询歌词，请用 #qqm歌词 关键词")
        event.stop_event()
        return None
    return mid, _lyric_meta(key, song)


async def _lyric_from_link(
    service: MusicService, event: AstrMessageEvent, key: str, user_key: str
) -> tuple[str, dict] | None:
    """从歌曲链接解析歌词目标；c6.y.qq.com 短链先跟随重定向（白名单内）再解析。"""
    try:
        ids = qqapi.parse_qqmusic_ids(key)
        if not ids.get("songmid") and "c6.y.qq.com" in key:
            url_m = re.search(r"https?://\S+", key)
            if url_m:
                ids = qqapi.parse_qqmusic_ids(await follow_qq_redirect(url_m.group(0)))
        song = await service.ids_to_song(ids, key, user_key)
    except Exception as err:
        await service.reply(event, f"歌词失败：{err}")
        event.stop_event()
        return None
    mid = (song or {}).get("songmid") or ""
    if not mid:
        await service.reply(event, "未能从链接中解析出歌曲，请改用 #qqm歌词 关键词")
        event.stop_event()
        return None
    return mid, _lyric_meta(key, song)


async def _lyric_by_search(
    service: MusicService, event: AstrMessageEvent, key: str, user_key: str
) -> tuple[str, dict] | None:
    """纯文本关键词搜索第一条作为歌词目标。"""
    try:
        lst = await qqapi.search_songs(key, page_size=1, user_key=user_key)
        if not lst:
            await service.reply(event, "未找到歌曲")
            event.stop_event()
            return None
        return (lst[0].get("songmid") or lst[0].get("songMid") or ""), _lyric_meta(key, lst[0])
    except Exception as err:
        await service.reply(event, f"歌词失败：{err}")
        event.stop_event()
        return None


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
        await service.reply_song_list(event, title, songs)
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
