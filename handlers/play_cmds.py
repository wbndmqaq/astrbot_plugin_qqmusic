from __future__ import annotations

import asyncio
import re

from astrbot.api.event import AstrMessageEvent

from ..core import api as qqapi
from ..core import cards as cardlib
from ..core import messages as msg
from ..core.delivery import deliver_song, send_native_music_card
from ..core.service import _PLAY_ALL_LIMIT, _SKIP_ALL, _SKIP_TEXT, MusicService
from ..core.utils import safe_int
from .base import Route


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
        page_size = max(1, min(safe_int(cfg.get("maxList") or 10, 10), 20))
        lst = await qqapi.search_songs(keyword, page_size=page_size)
        if not lst:
            await service.reply(event, msg.NO_SONG_FOUND)
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
        await service.reply_song_list(
            event, keyword, lst, text_formatter=cardlib.format_list_text
        )
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
    # 裸 #听N（无 qqm 前缀）仅由最近活跃的音乐插件响应，避免多插件同装时抢占顺序取决于加载顺序
    if m and m.group(3) and not await service.is_session_owner():
        return
    # 显式写了 #qqm（group(3) 为空）却匹配不上会话时不能零响应：裸 #听N 的静默是
    # 「让位给别的音乐插件」的刻意设计，但显式前缀说明用户就是在用本插件
    prefixed = bool(m and m.group(3) is None)

    async def _hint(text: str):
        if prefixed:
            await service.reply(event, text)
            event.stop_event()

    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope)
    if not session or not session.get("data"):
        await _hint("列表已过期，请重新 #qqm点歌")
        return
    stype = session.get("type") or "pick"
    if stype == "mvList":
        await _hint("当前列表是 MV，请用 #qqmMV 播放 序号")
        return
    if want_recommend and stype != "recommend":
        await _hint("当前列表不是推荐歌单，请先 #qqm推荐")
        return
    if stype == "topCategory":
        await service.reply(event, "该会话是榜单分类，请先 #qqm排行 榜单名 查看歌曲")
        event.stop_event()
        return
    if stype == "albumList":
        await _play_from_album(service, event, cfg, session, scope, n)
        return
    if stype == "recommend":
        await service.expand_recommend(event, session, n)
        return
    if n < 1 or n > len(session["data"]):
        await service.reply(event, msg.out_of_range(len(session["data"])))
        event.stop_event()
        return
    song = session["data"][n - 1]
    user_key = service.user_key(event)
    action = session.get("action") or "play"
    if action != "play":
        await _run_pending_action(service, event, session, scope, song, user_key, action)
        return
    await _play_resolved(service, event, cfg, session, scope, song, user_key)


async def _play_from_album(
    service: MusicService,
    event: AstrMessageEvent,
    cfg: dict,
    session: dict,
    scope: str,
    n: int,
) -> None:
    """albumList 会话：按序号取专辑 → 拉曲目 → 专辑曲目卡。"""
    albums = session.get("data") or []
    if n < 1 or n > len(albums):
        await service.reply(event, msg.out_of_range(len(albums)))
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
    await service.show_album_tracks(event, alb, tracks, scope, cfg)
    event.stop_event()


async def _run_pending_action(
    service: MusicService,
    event: AstrMessageEvent,
    session: dict,
    scope: str,
    song: dict,
    user_key: str,
    action: str,
) -> None:
    """一次性动作（#qqm评论 选歌）：先复位会话为播放，再执行对应动作。"""
    try:
        _s = dict(session)
        _s["action"] = "play"
        await cardlib.SessionStore.set(service.plugin, scope, _s)
    except Exception:
        pass
    try:
        if action == "comment":
            await service.show_comment(event, song, user_key)
        else:
            await service.reply(event, f"未知动作：{action}")
    except Exception as err:
        service.log_warn(f"执行失败: {err}")
        await service.reply(event, f"操作失败：{err}")
    event.stop_event()


async def _play_resolved(
    service: MusicService,
    event: AstrMessageEvent,
    cfg: dict,
    session: dict,
    scope: str,
    song: dict,
    user_key: str,
) -> None:
    """普通播放：详情卡 + 投递；无播放链时退回原生音乐卡。"""
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
        options=_SKIP_TEXT,
        user_key=user_key,
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
    # 裸 #听所有（无 qqm 前缀）仅由最近活跃的音乐插件响应
    if not re.match(r"^#?(?:qq|QQ)m", event.message_str.strip(), re.IGNORECASE) and not await service.is_session_owner():
        return
    scope = service.scope(event)
    session = await cardlib.SessionStore.get(service.plugin, scope)
    if not session or not session.get("data"):
        # 显式 #qqm听所有 却无会话时不能零响应（裸 #听所有 保持静默让位）
        if re.match(r"^#?(?:qq|QQ)m", event.message_str.strip(), re.IGNORECASE):
            await service.reply(event, "列表已过期，请重新 #qqm点歌")
            event.stop_event()
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
            r = await deliver_song(
                service.plugin,
                event,
                song,
                play,
                cfg=cfg,
                options=_SKIP_ALL,
                user_key=user_key,
            )
            # 投递层返回真实结果（语音/文件两条通道都没发出去时 ok=False）：
            # 原先无条件 ok += 1，导致「连播完成：成功 N 首」虚高
            if r.get("ok"):
                ok += 1
            else:
                fail += 1
                service.log_warn(
                    f"连播 {i + 1}/{len(batch)} 投递失败: {song.get('songName')}"
                )
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
            await service.reply(event, msg.NO_SONG_FOUND)
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
            options=_SKIP_TEXT,
            user_key=user_key,
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
