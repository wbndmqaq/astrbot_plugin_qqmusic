from __future__ import annotations

import asyncio
import re

from astrbot.api.event import AstrMessageEvent

try:
    from ..core import api as qqapi
    from ..core import cards as cardlib
    from ..core.delivery import deliver_song, send_native_music_card
    from ..core.service import (
        PLUGIN_DIR,
        _PLAY_ALL_LIMIT,
        _SKIP_ALL,
        _SKIP_TEXT,
        MusicService,
    )
    from .base import Route
except (ImportError, ValueError):
    from core import api as qqapi
    from core import cards as cardlib
    from core.delivery import deliver_song, send_native_music_card
    from core.service import (
        PLUGIN_DIR,
        _PLAY_ALL_LIMIT,
        _SKIP_ALL,
        _SKIP_TEXT,
        MusicService,
    )
    from handlers.base import Route


async def pick_song(service: MusicService, event: AstrMessageEvent):
    """#qqm点歌 关键词 搜索并展示歌曲列表（带MV徽标）"""
    cfg = service.cfg()
    if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*点歌\s*(.+)$", event.message_str.strip(), re.IGNORECASE
    )
    keyword = m.group(1).strip() if m else ""
    if not keyword:
        await service.reply(event, "用法：#qqm点歌 关键词")
        return
    try:
        await service.reply(event, f"正在搜索：{keyword}")
        page_size = min(int(cfg.get("maxList") or 10), 20)
        lst = await qqapi.search_songs(keyword, page_size=page_size)
        if not lst:
            await service.reply(event, "没有搜到相关歌曲")
            return
        try:
            mids = [s["songmid"] for s in lst if s.get("songmid")]
            infos = await qqapi.song_info_batch(
                mids, user_key=service.user_key(event)
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
            pass
        scope = service.scope(event)
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {"keyword": keyword, "data": lst},
        )
        if cfg.get("renderListCard", True):
            data = cardlib.build_list_card_data(keyword, lst, cfg=service.cfg())
            if await service.reply_card_or_text(
                event,
                tpl_name="qqmusic-list",
                data=data,
                format_text=lambda d: cardlib.format_list_text(lst),
            ):
                return
        await service.reply(event, cardlib.format_list_text(lst))
    except Exception as err:
        service.log_warn(f"点歌失败: {err}")
        await service.reply(event, f"点歌失败：{err}")
    finally:
        event.stop_event()


async def choose_song(service: MusicService, event: AstrMessageEvent):
    """#qqm听N / #qqm推荐听N 播放当前会话列表第 N 首"""
    cfg = service.cfg()
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
        want_recommend = bool(m.group(1))
        n = int(m.group(2) or m.group(3) or 0)
    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope)
    if not session or not session.get("data"):
        return
    stype = session.get("type") or "pick"
    if stype == "mvList":
        return
    if want_recommend and stype != "recommend":
        return
    if stype == "topCategory":
        await service.reply(event, "该会话是榜单分类，请先 #qqm排行 榜单名 查看歌曲")
        event.stop_event()
        return
    if stype == "albumList":
        albums = session.get("data") or []
        if n < 1 or n > len(albums):
            await service.reply(event, f"请选择 1-{len(albums)}")
            event.stop_event()
            return
        alb = albums[n - 1]
        try:
            result = await qqapi.album_songs(
                alb["albummid"], user_key=service.user_key(event)
            )
        except Exception as err:
            service.log_warn(f"专辑展开失败: {err}")
            await service.reply(event, f"专辑展开失败：{err}")
            event.stop_event()
            return
        tracks = result.get("list") or []
        if not tracks:
            await service.reply(event, "该专辑暂无曲目")
            event.stop_event()
            return
        await service.show_album_tracks(
            event, alb, tracks, scope, cfg
        )
        event.stop_event()
        return
    if stype == "recommend":
        await service.expand_recommend(event, session, n)
        return
    if n < 1 or n > len(session["data"]):
        await service.reply(event, f"请选择 1-{len(session['data'])}")
        event.stop_event()
        return
    song = session["data"][n - 1]
    user_key = service.user_key(event)
    action = session.get("action") or "play"
    if action != "play":
        try:
            _s = dict(session)
            _s["action"] = "play"
            await cardlib.SessionStore.set(service.plugin, scope, _s)
        except Exception:
            pass
    if action != "play":
        try:
            if action == "lyric":
                await service.show_lyric(event, song, user_key)
            elif action == "comment":
                await service.show_comment(event, song, user_key)
            elif action == "mv":
                await service.show_mv(event, song, user_key)
            else:
                await service.reply(event, f"未知动作：{action}")
        except Exception as err:
            service.log_warn(f"执行失败: {err}")
            await service.reply(event, f"操作失败：{err}")
        event.stop_event()
        return
    play = await service.resolve_play(song, cfg, user_key)
    mv_vid = play.get("mvVid") or ""
    if mv_vid and session.get("data"):
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {**session, "lastMvVid": mv_vid},
        )
    await service.send_detail_card(event, song, play, source="点歌", mv_vid=mv_vid)
    if not play.get("url"):
        if cfg.get("sendNativeCard") and song.get("songid"):
            await send_native_music_card(event, "qq", song["songid"])
        event.stop_event()
        return
    await deliver_song(
        service.plugin,
        event,
        song,
        play,
        cfg=service.cfg(),
        plugin_dir=PLUGIN_DIR,
        options=_SKIP_TEXT,
    )
    event.stop_event()


async def play_all(service: MusicService, event: AstrMessageEvent):
    """#qqm听所有：依次发送当前会话列表的全部歌曲（语音+文件，上限 30 首）"""
    cfg = service.cfg()
    if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
        return
    if not re.match(
        r"^#?(?:qq|QQ)m\s*听\s*所有$|^#\s*听\s*所有$",
        event.message_str.strip(),
        re.IGNORECASE,
    ):
        return
    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope)
    if not session or not session.get("data"):
        return
    stype = session.get("type") or "pick"
    if stype in ("mvList", "topCategory", "albumList", "recommend"):
        return
    songs = session.get("data") or []
    batch = songs[:_PLAY_ALL_LIMIT]
    title = session.get("keyword") or "当前列表"
    await service.reply(
        event,
        f"▶ 开始连播「{title}」共 {len(batch)} 首"
        + (f"（列表共 {len(songs)} 首，仅连播前 {_PLAY_ALL_LIMIT} 首）" if len(songs) > len(batch) else "")
        + "，逐首下载发送需要一些时间…",
    )
    ok = fail = 0
    user_key = service.user_key(event)
    for i, song in enumerate(batch):
        try:
            play = await service.resolve_play(song, cfg, user_key)
            if not play.get("url"):
                fail += 1
                service.log_warn(
                    f"连播 {i + 1}/{len(batch)} 无播放链: {song.get('songName')}"
                )
                continue
            await deliver_song(
                service.plugin,
                event,
                song,
                play,
                cfg=cfg,
                plugin_dir=PLUGIN_DIR,
                options=_SKIP_ALL,
            )
            ok += 1
        except Exception as err:
            fail += 1
            service.log_warn(f"连播 {i + 1}/{len(batch)} 失败: {err}")
        if i < len(batch) - 1:
            await asyncio.sleep(1)
    await service.reply(event, f"连播完成：成功 {ok} 首，失败 {fail} 首")
    event.stop_event()


async def play_direct(service: MusicService, event: AstrMessageEvent):
    """#qqm播放 关键词 直接播放第一条"""
    cfg = service.cfg()
    if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*播放\s*(.+)$", event.message_str.strip(), re.IGNORECASE
    )
    keyword = m.group(1).strip() if m else ""
    if not keyword:
        await service.reply(event, "用法：#qqm播放 关键词")
        event.stop_event()
        return
    try:
        lst = await qqapi.search_songs(keyword, page_size=1)
        if not lst:
            await service.reply(event, "没有搜到相关歌曲")
            event.stop_event()
            return
        song = lst[0]
        user_key = service.user_key(event)
        play = await service.resolve_play(song, cfg, user_key)
        await service.send_detail_card(
            event, song, play, source="播放", mv_vid=play.get("mvVid") or ""
        )
        if not play.get("url"):
            if cfg.get("sendNativeCard") and song.get("songid"):
                await send_native_music_card(event, "qq", song["songid"])
            event.stop_event()
            return
        await deliver_song(
            service.plugin,
            event,
            song,
            play,
            cfg=service.cfg(),
            plugin_dir=PLUGIN_DIR,
            options=_SKIP_TEXT,
        )
    except Exception as err:
        await service.reply(event, f"播放失败：{err}")
    event.stop_event()


ROUTES: list[Route] = [
    Route(
        pattern=r"^#?(qq|QQ)m\s*点歌\s*(.+)$",
        name="pick_song",
        doc="#qqm点歌 关键词 搜索并展示歌曲列表（带MV徽标）",
        run=pick_song,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*(推荐)?听\s*([1-9][0-9]?)$|^#听\s*([1-9][0-9]?)$",
        name="choose_song",
        doc="#qqm听N / #qqm推荐听N 播放当前会话列表第 N 首",
        run=choose_song,
    ),
    Route(
        pattern=r"^#?(?:qq|QQ)m\s*听\s*所有$|^#听\s*所有$",
        name="play_all",
        doc="#qqm听所有：依次发送当前会话列表的全部歌曲（语音+文件，上限 30 首）",
        run=play_all,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*播放\s*(.+)$",
        name="play_direct",
        doc="#qqm播放 关键词 直接播放第一条",
        run=play_direct,
    ),
]
