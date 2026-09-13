from __future__ import annotations

import random
import re

from astrbot.api.event import AstrMessageEvent

from ..core import api as qqapi
from ..core import cards as cardlib
from ..core import messages as msg
from ..core.delivery import deliver_song
from ..core.service import MusicService
from .base import Route


async def chart(service: MusicService, event: AstrMessageEvent):
    """#qqm排行 榜单名 查看排行榜（飙升/热歌/新歌等）"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*排行\s*(.*)$", event.message_str.strip(), re.IGNORECASE
    )
    keyword = (m.group(1) if m else "").strip()
    scope = service.scope(event)
    user_key = service.user_key(event)
    if not keyword:
        await _chart_overview(service, event, scope, user_key)
    else:
        await _chart_detail(service, event, scope, user_key, keyword)
    event.stop_event()


async def _chart_overview(
    service: MusicService, event: AstrMessageEvent, scope: str, user_key: str
) -> None:
    """#qqm排行（无参）：分组列出全部榜单，写入 topCategory 会话供 #qqm歌词N 识别。"""
    try:
        groups = await qqapi.top_category(user_key)
        if not groups:
            await service.reply(event, "获取排行榜失败")
            event.stop_event()
            return
        all_tops = []
        lines = ["♫ QQ音乐排行榜"]
        for g in groups:
            lines.append(f"【{g.get('title', '')}】")
            for t in g.get("list") or []:
                all_tops.append({**t, "group": g.get("title")})
                lines.append(f"  {t.get('label', '')}（#qqm排行 {t.get('label', '')}）")
        lines.append("\n发送 #qqm排行 榜单名 查看具体榜单")
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {"type": "topCategory", "data": all_tops},
        )
        await service.reply(event, "\n".join(lines))
    except Exception as err:
        service.log_warn(f"排行失败: {err}")
        await service.reply(event, msg.TOP_FAIL_HINT)


def _match_top(all_tops: list, keyword: str) -> dict | None:
    """榜单匹配：label 包含 → label 被包含 → topId 精确。"""
    return (
        next((t for t in all_tops if t.get("label") and keyword in t["label"]), None)
        or next((t for t in all_tops if t.get("label") and t["label"] in keyword), None)
        or next(
            (t for t in all_tops if t.get("topId") and str(t["topId"]) == keyword),
            None,
        )
    )


async def _chart_detail(
    service: MusicService,
    event: AstrMessageEvent,
    scope: str,
    user_key: str,
    keyword: str,
) -> None:
    """#qqm排行 榜单名：匹配榜单 → 取曲目 → 归一化入会话 → 列表卡。"""
    try:
        groups = await qqapi.top_category(user_key)
        all_tops = [t for g in groups for t in (g.get("list") or [])]
        match = _match_top(all_tops, keyword)
        if not match:
            names = "、".join(t.get("label", "") for t in all_tops if t.get("label"))[:200]
            await service.reply(event, f"未找到「{keyword}」\n可用榜单：{names}")
            event.stop_event()
            return
        # 按 label 匹配到的条目不保证带 topId（只有第三个分支校验过），缺了就退回报错
        top_id = match.get("topId")
        if not top_id:
            await service.reply(event, f"榜单「{match.get('label') or keyword}」缺少 ID，无法查询")
            event.stop_event()
            return
        await service.reply(event, f"正在获取 {match.get('label')}...")
        detail = await qqapi.top_detail(top_id, user_key=user_key)
        if isinstance(detail, dict):
            songs_raw = detail.get("list") or (detail.get("data") or {}).get("list") or []
        else:
            songs_raw = []
        # 归一化统一走 api.normalize_track_item（全仓唯一一份实现）：字段链比原先的
        # MusicService.normalize_song 更全（含 album.pic/cover 封面兜底、file.media_mid
        # 兜底），榜单列表卡封面不再可能为空、取链档位不再退化
        songs = [
            n
            for n in (
                qqapi.normalize_track_item(s, i) for i, s in enumerate(songs_raw or [])
            )
            if n
        ]
        if not songs:
            await service.reply(event, "该榜单暂无数据")
            event.stop_event()
            return
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {"type": "top", "data": songs},
        )
        await service.reply_song_list(event, match.get("label", ""), songs)
    except Exception as err:
        service.log_warn(f"排行失败: {err}")
        await service.reply(event, msg.TOP_FAIL_HINT)


async def new_songs(service: MusicService, event: AstrMessageEvent):
    """新歌速递：type 1 内地 / 2 欧美 / 3 日本 / 4 韩国 / 5 最新 / 6 港台，默认 5"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    scope = service.scope(event)
    user_key = service.user_key(event)
    m = re.match(
        r"^#?(?:qq|QQ)m\s*新歌(?:\s*(\d+))?$",
        event.message_str.strip(),
        re.IGNORECASE,
    )
    type_ = int(m.group(1)) if (m and m.group(1)) else 5
    try:
        await service.reply(event, "正在获取新歌速递...")
        songs = await qqapi.new_songs(type_, num=20, user_key=user_key)
        if not songs:
            await service.reply(event, "获取新歌失败，请稍后重试")
            event.stop_event()
            return
        title = "新歌速递"
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {
                "type": "newSongs",
                "data": songs,
            },
        )
        await service.reply_song_list(event, title, songs)
    except Exception as err:
        service.log_warn(f"新歌失败: {err}")
        await service.reply(event, "新歌速递获取失败，请稍后重试")
    event.stop_event()


async def _mv_search(
    service: MusicService, event: AstrMessageEvent, *, keyword: str, scope: str, user_key: str, missing_hint: str
) -> None:
    """MV 搜索并写会话（动词「搜索」与裸关键词共用，仅空结果文案不同）。"""
    try:
        lst = await qqapi.search_mv(keyword, page_size=10, user_key=user_key)
        if not lst:
            await service.reply(event, missing_hint)
            event.stop_event()
            return
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {
                "type": "mvList",
                "data": lst,
            },
        )
        await service.reply_song_list(
            event,
            f"MV · {keyword}",
            lst,
            text_suffix="\n\n" + msg.MV_LIST_HINT,
            text_formatter=cardlib.format_mv_list_text,
            card_builder=cardlib.build_mv_list_card_data,
        )
    except Exception as err:
        service.log_warn(f"MV 搜索失败: {err}")
        await service.reply(event, msg.mv_search_fail(err))
    event.stop_event()


async def _mv_play_or_download(
    service: MusicService, event: AstrMessageEvent, *, arg_text: str, is_dl: bool, scope: str, user_key: str
) -> None:
    """动词「播放/下载」：解析 序号 / vid / 本曲 MV 三种来源并投递。"""
    session = await cardlib.SessionStore.get(service.plugin, scope)
    mv_obj = None
    if not arg_text:
        last_vid = (session or {}).get("lastMvVid") or ""
        if last_vid:
            mv_obj = {
                "vid": last_vid,
                "mvtitle": "本曲 MV",
                "name": "本曲 MV",
                "singerName": "",
            }
        else:
            await service.reply(
                event,
                "用法：#qqmMV 播放 序号 或 #qqmMV 下载 序号（也可直接给 vid）；"
                "点歌后再发 #qqmMV 播放 可直接操作该曲 MV",
            )
            event.stop_event()
            return
    elif re.fullmatch(r"\d+", arg_text):
        n = int(arg_text)
        if not session or not session.get("data"):
            await service.reply(event, "请先 #qqm点歌 / #qqmMV 搜索 出列表")
            event.stop_event()
            return
        if n < 1 or n > len(session["data"]):
            await service.reply(event, msg.out_of_range(len(session["data"])))
            event.stop_event()
            return
        if session.get("type") in ("topCategory", "recommend"):
            await service.reply(
                event,
                msg.SESSION_TOP_CATEGORY_HINT
                if session.get("type") == "topCategory"
                else msg.SESSION_RECOMMEND_HINT,
            )
            event.stop_event()
            return
        if session.get("type") == "mvList":
            mv_obj = session["data"][n - 1]
        else:
            song = session["data"][n - 1]
            song_vid = song.get("mvVid") or ""
            if not song_vid:
                await service.reply(event, msg.NO_MV_HINT)
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
        mv_obj = {
            "vid": arg_text,
            "mvtitle": arg_text,
            "name": arg_text,
            "singerName": "",
        }

    await service.deliver_mv(event, mv_obj, download=is_dl, user_key=user_key)
    event.stop_event()


async def _mv_category(
    service: MusicService, event: AstrMessageEvent, *, cat_rest: str, scope: str, user_key: str
) -> None:
    """动词「分类」：无参列出分类，带 序号/分类名 浏览该类 MV。"""
    try:
        cats = await qqapi.mv_category(user_key)
    except Exception as err:
        service.log_warn(f"MV 分类失败: {err}")
        cats = {"area": [], "version": [], "list": []}
    all_cats = (
        (cats.get("area") or [])
        + (cats.get("version") or [])
        + (cats.get("list") or [])
    )
    if not all_cats:
        await service.reply(event, "没有获取到 MV 分类")
        event.stop_event()
        return
    if not cat_rest:
        lines = ["♫ MV 分类"]
        for i, t in enumerate(all_cats):
            lines.append(f"{i + 1}. {t.get('name') or t.get('title') or ''}")
        lines.append("\n发送 #qqmMV 分类 序号 或 #qqmMV 分类 分类名 浏览该类 MV")
        await service.reply(event, "\n".join(lines))
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
        await service.reply(event, f"未找到分类「{cat_rest}」")
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
            await service.reply(event, "该分类暂无 MV")
            event.stop_event()
            return
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {
                "type": "mvList",
                "data": lst,
            },
        )
        shown = lst[:15]
        await service.reply_song_list(
            event,
            f"MV 分类 · {tag.get('name') or tag.get('title') or ''}",
            shown,
            text_suffix="\n\n" + msg.MV_LIST_HINT,
            text_formatter=cardlib.format_mv_list_text,
            card_builder=cardlib.build_mv_list_card_data,
        )
    except Exception as err:
        service.log_warn(f"MV 分类浏览失败: {err}")
        await service.reply(event, f"MV 分类浏览失败：{err}")
    event.stop_event()


async def mv(service: MusicService, event: AstrMessageEvent):
    """MV：搜索 / 播放 / 下载 / 分类浏览（按动词分发到 _mv_* 助手）"""
    cfg = service.cfg()
    if not cfg.get("enable", True) or cfg.get("enableSongRequest") is False:
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*(?:MV|mv)\s*(.*)$",
        event.message_str.strip(),
        re.IGNORECASE,
    )
    rest = (m.group(1).strip() if m else "").strip()
    scope = service.scope(event)
    user_key = service.user_key(event)

    verb_match = re.match(r"^(播放|下载|搜索)[:：]?\s*(.*)$", rest)
    verb = verb_match.group(1) if verb_match else ""
    arg_text = (verb_match.group(2).strip() if verb_match else rest).strip()

    if verb == "搜索":
        if not arg_text:
            await service.reply(event, "用法：#qqmMV 搜索 关键词")
            event.stop_event()
            return
        await _mv_search(
            service, event, keyword=arg_text, scope=scope, user_key=user_key,
            missing_hint="没有搜到相关 MV",
        )
        return

    if verb in ("播放", "下载"):
        await _mv_play_or_download(
            service, event, arg_text=arg_text, is_dl=verb == "下载",
            scope=scope, user_key=user_key,
        )
        return

    if rest.startswith("分类"):
        await _mv_category(
            service, event, cat_rest=rest[len("分类"):].strip(), scope=scope, user_key=user_key
        )
        return

    # 裸关键词（无 播放/下载/搜索/分类 动词）默认视为 MV 搜索
    if not rest:
        await service.reply(event, "用法：#qqmMV 搜索 关键词")
        event.stop_event()
        return
    await _mv_search(
        service, event, keyword=rest, scope=scope, user_key=user_key,
        missing_hint=f"没有搜到相关 MV：{rest}",
    )


async def recommend(service: MusicService, event: AstrMessageEvent):
    """#qqm推荐 热门推荐歌单"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    scope = service.scope(event)
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在获取推荐歌单...")
        lst = await qqapi.recommend_hot(user_key)
        if not lst:
            await service.reply(event, "获取推荐失败")
            event.stop_event()
            return
        lines = ["♫ 热门推荐歌单"]
        for i, p in enumerate(lst[:15]):
            name = p.get("title") or p.get("dissname") or "未知"
            cnt = p.get("listenNum") or p.get("listennum") or 0
            lines.append(f"{i + 1}. {name} ({cnt}次播放)")
        lines.append("\n发送 #qqm推荐听序号 查看歌单歌曲")
        await cardlib.SessionStore.set(
            service.plugin,
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
            data = cardlib.build_hot_card_data(hot_items, cfg=service.cfg())
            if await service.reply_card_or_text(
                event,
                tpl_name="qqmusic-hot",
                data=data,
                format_text=lambda d: "\n".join(lines),
            ):
                event.stop_event()
                return
        await service.reply(event, "\n".join(lines))
    except Exception as err:
        service.log_warn(f"推荐失败: {err}")
        await service.reply(event, msg.RECOMMEND_FAIL_HINT)
    event.stop_event()


async def random_song(service: MusicService, event: AstrMessageEvent):
    """#qqm来首歌 随机推荐一首并播放"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在为你推荐...")
        songs = await qqapi.recommend_feed(user_key)
        if not songs:
            await service.reply(event, "获取推荐失败，请重试")
            event.stop_event()
            return
        song = random.choice(songs)
        await service.reply(
            event, f"♪ {song.get('songName')} - {song.get('singerName')}"
        )
        play = await qqapi.song_url_best(
            song["songmid"],
            quality=cfg.get("quality") or "auto",
            media_id=song.get("media_mid") or song.get("songmid"),
            fallback=cfg.get("qualityFallback", True) is not False,
            user_key=user_key,
        )
        if not play.get("url"):
            await service.reply(event, "获取播放链失败，请 #qqm登录")
            event.stop_event()
            return
        await deliver_song(
            service.plugin,
            event,
            song,
            play,
            cfg=service.cfg(),
            user_key=user_key,
        )
    except Exception as err:
        service.log_warn(f"推荐失败: {err}")
        await service.reply(event, msg.RECOMMEND_FAIL_HINT)
    event.stop_event()


async def radio(service: MusicService, event: AstrMessageEvent):
    """#qqm电台 个性电台 5 首"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    scope = service.scope(event)
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在获取个性电台...")
        songs = await qqapi.personal_radio(5, user_key)
        if not songs:
            await service.reply(event, "获取电台失败，请重试")
            event.stop_event()
            return
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {
                "type": "radio",
                "data": songs,
            },
        )
        await service.reply_song_list(event, "个性电台", songs)
    except Exception as err:
        service.log_warn(f"电台失败: {err}")
        await service.reply(event, "电台失败，请稍后重试")
    event.stop_event()


async def daily(service: MusicService, event: AstrMessageEvent):
    """#qqm日推 每日推荐（需登录）"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    scope = service.scope(event)
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在获取每日推荐...")
        res = await qqapi.daily_recommend(song_num=30, user_key=user_key)
        songs = res.get("songs") or []
        if not songs:
            await service.reply(
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
            service.plugin,
            scope,
            {
                "type": "daily",
                "data": songs,
            },
        )
        await service.reply_song_list(event, title, songs)
    except Exception as err:
        service.log_warn(f"日推失败: {err}")
        if (
            getattr(err, "code", None) == -1
            or "登录" in str(err)
            or "login" in str(err).lower()
        ):
            await service.reply(event, "日推失败，请先 #qqm登录 后重试")
        else:
            await service.reply(event, f"日推失败：{err}")
    event.stop_event()


async def artist(service: MusicService, event: AstrMessageEvent):
    """#qqm歌手 关键词 搜索歌手，展示热门歌曲"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*歌手\s+(.+)$", event.message_str.strip(), re.IGNORECASE
    )
    keyword = m.group(1).strip() if m else ""
    if not keyword:
        return
    scope = service.scope(event)
    user_key = service.user_key(event)
    try:
        await service.reply(event, f"正在搜索歌手：{keyword}")
        singers = await qqapi.search_singers(
            keyword, page_size=5, user_key=user_key
        )
        if not singers:
            await service.reply(event, "没有找到相关歌手")
            event.stop_event()
            return
        singer = singers[0]
        result = await qqapi.singer_songs(
            singer["singermid"], page_size=30, user_key=user_key
        )
        if not result.get("list"):
            await service.reply(event, "该歌手暂无歌曲")
            event.stop_event()
            return
        title = f"{singer.get('singerName', '')} 热门歌曲"
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {
                "type": "singer",
                "data": result["list"],
                "singer": singer,
            },
        )
        await service.reply_song_list(
            event,
            title,
            result["list"],
            tip=f"发送 #qqm听序号 播放「{singer.get('singerName', '')}」的歌曲",
        )
        await service.send_singer_desc(event, singer, user_key)
    except Exception as err:
        service.log_warn(f"歌手搜索失败: {err}")
        await service.reply(event, "歌手搜索失败，请稍后重试")
    event.stop_event()


def _album_list_item(alb: dict) -> dict:
    return {
        "songName": alb.get("albumName") or "未知专辑",
        "singerName": alb.get("singerName") or "",
        "albumName": str(alb.get("songCount") or "") + "首"
        if alb.get("songCount")
        else "",
        "cover": alb.get("cover") or "",
        "duration": alb.get("publicTime") or "",
        "payplay": False,
        "mvVid": "",
    }


async def album(service: MusicService, event: AstrMessageEvent):
    """#qqm专辑 关键词 搜索专辑出候选列表，回复 #qqm听N 查看曲目"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*专辑\s+(.+)$", event.message_str.strip(), re.IGNORECASE
    )
    keyword = m.group(1).strip() if m else ""
    if not keyword:
        return
    scope = service.scope(event)
    user_key = service.user_key(event)
    try:
        await service.reply(event, f"正在搜索专辑：{keyword}")
        albums = await qqapi.search_albums(keyword, page_size=10, user_key=user_key)
        if not albums:
            await service.reply(event, "没有找到相关专辑")
            event.stop_event()
            return
        if len(albums) == 1:
            alb = albums[0]
            result = await qqapi.album_songs(alb["albummid"], user_key=user_key)
            if result.get("list"):
                await service.show_album_tracks(event, alb, result["list"], scope, cfg)
                event.stop_event()
                return
        view = [_album_list_item(a) for a in albums]
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {"type": "albumList", "keyword": f"专辑 · {keyword}", "data": albums},
        )
        tip = "查看该专辑的曲目列表"
        await service.reply_song_list(
            event,
            f"专辑 · {keyword}",
            view,
            tip=tip,
            text_suffix=f"\n\n回复 #qqm听序号 {tip}",
            commands=[
                {
                    "name": "#qqm听序号",
                    "desc": "展开该专辑曲目，随后可再选歌播放",
                    "example": "#qqm听1",
                },
                {
                    "name": "列表有效期",
                    "desc": "本列表约 10 分钟内有效，过期请重新搜索",
                    "example": "#qqm专辑 关键词",
                },
            ],
        )
    except Exception as err:
        service.log_warn(f"专辑搜索失败: {err}")
        await service.reply(event, "专辑搜索失败，请稍后重试")
    event.stop_event()


async def playlist(service: MusicService, event: AstrMessageEvent):
    """#qqm歌单 关键词 搜索歌单，展示歌曲"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    m = re.match(
        r"^#?(?:qq|QQ)m\s*歌单\s+(.+)$", event.message_str.strip(), re.IGNORECASE
    )
    keyword = m.group(1).strip() if m else ""
    if not keyword:
        return
    scope = service.scope(event)
    user_key = service.user_key(event)
    try:
        await service.reply(event, f"正在搜索歌单：{keyword}")
        lists = await qqapi.search_songlists(
            keyword, page_size=5, user_key=user_key
        )
        if not lists:
            await service.reply(event, "没有找到相关歌单")
            event.stop_event()
            return
        pl = lists[0]
        detail = await qqapi.songlist_detail(pl["disstid"], user_key)
        songs = detail.get("songlist") or []
        if not songs:
            await service.reply(event, "该歌单暂无歌曲")
            event.stop_event()
            return
        title = detail.get("dissname") or pl.get("dissname") or "歌单"
        await cardlib.SessionStore.set(
            service.plugin,
            scope,
            {
                "type": "playlist",
                "data": songs,
                "playlist": pl,
            },
        )
        await service.reply_song_list(event, title, songs)
    except Exception as err:
        service.log_warn(f"歌单搜索失败: {err}")
        await service.reply(event, "歌单搜索失败，请稍后重试")
    event.stop_event()


ROUTES: list[Route] = [
    Route(
        pattern=r"^#?(qq|QQ)m\s*排行\s*(.*)$",
        name="chart",
        doc="#qqm排行 榜单名 查看排行榜（飙升/热歌/新歌等）",
        run=chart,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*新歌(?:\s*(\d+))?$",
        name="new_songs",
        doc="新歌速递：type 1 内地 / 2 欧美 / 3 日本 / 4 韩国 / 5 最新 / 6 港台，默认 5",
        run=new_songs,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*(MV|mv)\s*(.*)$",
        name="mv",
        doc="MV：搜索 / 播放 / 下载 / 分类浏览",
        run=mv,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*推荐$",
        name="recommend",
        doc="#qqm推荐 热门推荐歌单",
        run=recommend,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*(来首歌|随机|放一首|来一首)$",
        name="random_song",
        doc="#qqm来首歌 随机推荐一首并播放",
        run=random_song,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*电台$",
        name="radio",
        doc="#qqm电台 个性电台 5 首",
        run=radio,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*(日推|每日推荐)$",
        name="daily",
        doc="#qqm日推 每日推荐（需登录）",
        run=daily,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*歌手\s+(.+)$",
        name="artist",
        doc="#qqm歌手 关键词 搜索歌手，展示热门歌曲",
        run=artist,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*专辑\s+(.+)$",
        name="album",
        doc="#qqm专辑 关键词 搜索专辑出候选列表，回复 #qqm听N 查看曲目",
        run=album,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*歌单\s+(.+)$",
        name="playlist",
        doc="#qqm歌单 关键词 搜索歌单，展示歌曲",
        run=playlist,
    ),
]
