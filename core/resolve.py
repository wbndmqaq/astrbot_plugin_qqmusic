"""QQ 音乐分享卡片 / 链接的识别与解析流程。

从 ``MusicService`` 整块搬出。原 ``handle_resolve`` 单函数 229 行、内嵌
「卡片 → 短链跟随 → 专辑/歌单/歌手/单曲四类链接 → 取播放链 → 失败提示 →
详情卡/文本回执 → 投递」共 7 个阶段，任何一处改动都要在 229 行里定位。
这里按原来的**执行顺序**拆成有序私有步骤，分支与降级顺序完全不变：

    handle_resolve
      ├─ _song_from_card         卡片识别（resolveCards）
      ├─ _song_from_link         链接识别（resolveLinks）
      │    ├─ _follow_short_link      c6 短链跟随（SSRF 白名单内）
      │    ├─ _try_album_link         专辑链接
      │    ├─ _try_playlist_link      歌单链接
      │    ├─ _try_singer_link        歌手链接
      │    └─ ids_to_song             单曲链接
      ├─ _build_play             取播放链（含 song_detail 字段回填）
      ├─ _fail_hint              无播放链时的原因提示
      └─ _reply_detail           详情卡 / 文本回执
"""

from __future__ import annotations

import re
from pathlib import Path

from . import api as qqapi
from . import cards as cardlib
from . import messages as msg
from .api import extract_track_meta
from .delivery import deliver_song
from .utils import safe_int

# 与 core/service.py 同一表达式（不能 import service：service 反向委托本模块）
PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)


async def handle_resolve(service, event, text: str, cfg: dict, *, skip_options: dict) -> bool:
    """自动解析分享卡片/链接；返回 True 表示已处理（调用方应终止事件）。"""
    user_key = service.user_key(event)
    song: dict | None = None
    from_card = False

    if cfg.get("resolveCards", True):
        song, from_card = await _song_from_card(service, text, user_key)

    if not song and cfg.get("resolveLinks", True):
        song, handled = await _song_from_link(service, event, text, user_key)
        if handled:
            return True

    if not song:
        if from_card:
            await service.reply(event, "识别到 QQ 音乐分享，但未能提取歌曲信息")
            return True
        return False

    play = await _build_play(service, song, cfg, user_key)
    await _reply_detail(service, event, song, play, cfg, from_card)
    await deliver_song(
        service.plugin,
        event,
        song,
        play,
        cfg=service.cfg(),
        options=skip_options,
        user_key=user_key,
    )
    return True


# ──────────── 卡片 ────────────


async def _song_from_card(service, text: str, user_key: str):
    """卡片识别：返回 (song, from_card)。"""
    card = qqapi.parse_qqmusic_card(text)
    if not card:
        return None, False
    service.log_info(f"识别卡片: {card.get('title')} - {card.get('desc')}")
    return await card_to_song(service, card, user_key), True


# ──────────── 链接 ────────────


async def _follow_short_link(service, url: str) -> str:
    """c6.y.qq.com 短链 / ``?__=`` 跳转链跟随（白名单内），失败回退原链接。"""
    if re.search(
        r"c6\.y\.qq\.com/base/fcgi-bin/u\?", url, re.IGNORECASE
    ) or re.search(r"[?&]__=", url):
        from .service import follow_qq_redirect

        try:
            final_url = await follow_qq_redirect(url)
            if final_url and final_url != url:
                service.log_info(f"短链跟随: {final_url}")
                return final_url
        except Exception as err:
            service.log_warn(f"短链跟随失败: {err}")
    return url


async def _try_album_link(service, event, ext_ids: dict, user_key: str) -> bool:
    """专辑链接：写入专辑会话并提示用 #qqm听N 展开。"""
    scope = service.scope(event)
    try:
        result = await qqapi.album_songs(ext_ids["albummid"], user_key=user_key)
        songs = result.get("list") or []
        if songs:
            for s in songs:
                s["albummid"] = s.get("albummid") or ext_ids["albummid"]
            await cardlib.SessionStore.set(
                service.plugin,
                scope,
                {
                    "type": "album",
                    "data": songs,
                },
            )
            await service.reply(
                event,
                f"识别到专辑链接，共{len(songs)}首。{msg.LISTEN_TO_PLAY_HINT}",
            )
            return True
    except Exception as err:
        service.log_warn(f"专辑解析失败: {err}")
    return False


async def _try_playlist_link(service, event, ext_ids: dict, user_key: str) -> bool:
    """歌单链接：写入歌单会话并提示用 #qqm听N 展开。"""
    scope = service.scope(event)
    try:
        detail = await qqapi.songlist_detail(ext_ids["disstid"], user_key)
        songs = detail.get("songlist") or []
        if songs:
            title = detail.get("dissname") or detail.get("title") or "歌单"
            await cardlib.SessionStore.set(
                service.plugin,
                scope,
                {
                    "type": "playlist",
                    "data": songs,
                },
            )
            await service.reply(
                event,
                f"识别到歌单「{title}」，共{len(songs)}首。{msg.LISTEN_TO_PLAY_HINT}",
            )
            return True
    except Exception as err:
        service.log_warn(f"歌单解析失败: {err}")
    return False


async def _try_singer_link(service, event, ext_ids: dict, user_key: str) -> bool:
    """歌手链接：写入歌手会话并提示用 #qqm听N 展开。"""
    scope = service.scope(event)
    try:
        result = await qqapi.singer_songs(
            ext_ids["singermid"], page_size=30, user_key=user_key
        )
        if result.get("list"):
            await cardlib.SessionStore.set(
                service.plugin,
                scope,
                {
                    "type": "singer",
                    "data": result["list"],
                },
            )
            await service.reply(
                event,
                f"识别到歌手链接，热门歌曲{len(result['list'])}首。"
                f"{msg.LISTEN_TO_PLAY_HINT}",
            )
            return True
    except Exception as err:
        service.log_warn(f"歌手解析失败: {err}")
    return False


async def _song_from_link(service, event, text: str, user_key: str):
    """链接识别：返回 (song, handled)。handled=True 表示已直接回执并结束流程。"""
    url_match = re.search(
        r"https?://(?:[a-z0-9-]+\.)?(?:y\.qq\.com|c6\.y\.qq\.com)"
        r'[^\s，。；：！？、（）【】《》»"“”\'`一-龥]*',
        text,
        re.IGNORECASE,
    )
    if not url_match:
        return None, False
    url = url_match.group(0)
    service.log_info(f"识别链接: {url}")
    url = await _follow_short_link(service, url)

    ext_ids = qqapi.parse_qqmusic_extended_ids(url)
    if ext_ids.get("albummid") and await _try_album_link(
        service, event, ext_ids, user_key
    ):
        return None, True
    if ext_ids.get("disstid") and await _try_playlist_link(
        service, event, ext_ids, user_key
    ):
        return None, True
    if ext_ids.get("singermid") and await _try_singer_link(
        service, event, ext_ids, user_key
    ):
        return None, True

    ids = qqapi.parse_qqmusic_ids(url)
    return await ids_to_song(service, ids, text, user_key), False


# ──────────── 播放链 ────────────


async def _backfill_song_meta(song: dict, user_key: str) -> None:
    """song_detail 回填 media_mid / songid / albummid（缺失时）。

    字段提取统一走 ``api.extract_track_meta``。
    """
    if song.get("_detailFetched"):
        return
    if song.get("media_mid") and song.get("media_mid") != song.get("songmid"):
        return
    try:
        meta = extract_track_meta(await qqapi.song_detail(song["songmid"], user_key))
        if meta["media_mid"]:
            song["media_mid"] = meta["media_mid"]
        if not song.get("songid") and meta["songid"]:
            song["songid"] = meta["songid"]
        if not song.get("albummid") and meta["albummid"]:
            song["albummid"] = meta["albummid"]
    except Exception:
        pass


async def _build_play(service, song: dict, cfg: dict, user_key: str) -> dict:
    """取播放链并归一化为投递视图（失败时把原因写进 play["error"]）。"""
    play = {
        "url": "",
        "quality": cfg.get("quality") or "auto",
        "qualityLabel": service.quality_label(cfg.get("quality") or "auto"),
    }
    if not song.get("songmid"):
        play["error"] = "未能从分享中提取歌曲 songmid，无法取播放链"
        return play
    try:
        await _backfill_song_meta(song, user_key)
        play = await qqapi.song_url_best(
            song["songmid"],
            quality=cfg.get("quality") or "auto",
            media_id=song.get("media_mid") or song.get("songmid"),
            fallback=cfg.get("qualityFallback", True) is not False,
            user_key=user_key,
        )
        play = service.play_view(play)
    except Exception as err:
        service.log_warn(f"播放链: {err}")
        play["error"] = str(err)
    return play


def _fail_hint(play: dict) -> str:
    """无播放链时的原因提示（错误文本 → 会员 → 通用）。"""
    if play.get("url"):
        return ""
    err_text = str(play.get("error") or "")[:220]
    raw = play.get("raw") if isinstance(play.get("raw"), dict) else {}
    pay = raw.get("pay") if isinstance(raw.get("pay"), dict) else play.get("pay")
    if err_text:
        return f"⚠ {err_text}"
    if pay and safe_int(pay.get("pay_play")) == 1:
        return "⚠ 该曲需会员播放，请 #qqm登录"
    return "⚠ 未获取到播放链（请 #qqm登录 重新扫码；或 #qqm刷新 后重试）"


async def _reply_detail(
    service, event, song: dict, play: dict, cfg: dict, from_card: bool
) -> None:
    """详情卡回执；渲染不可用时按原顺序拼文本兜底。"""
    q_label = (
        play.get("qualityLabel")
        or service.quality_label(play.get("quality", ""))
        or ""
    )
    prefix = cfg.get("identifyPrefix") or "识别："
    fail_hint = _fail_hint(play)

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
    url = await service.render_card(event, card_data, "qqmusic-detail")
    if url:
        from astrbot.api.message_components import Image

        if await service.send_chain(event, Image.fromFileSystem(url)):
            return
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
    await service.reply(event, "\n".join(t for t in text_block if t))


# ──────────── 卡片/ID → 歌曲 ────────────


async def card_to_song(service, card: dict, user_key: str = "") -> dict | None:
    if card.get("songmid"):
        return await ids_to_song(service, card, "", user_key)
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
    service, ids: dict, fallback_text: str = "", user_key: str = ""
) -> dict | None:
    songmid = ids.get("songmid") or ""
    songid = ids.get("songid") or 0
    media_mid = ids.get("media_mid") or ""
    albummid = ids.get("albummid") or ""

    if songmid:
        try:
            # 字段提取统一走 api.extract_track_meta
            meta = extract_track_meta(await qqapi.song_detail(songmid, user_key))
            albummid = albummid or meta["albummid"]
            media_mid = media_mid or meta["media_mid"] or songmid
            return {
                "songmid": songmid,
                "songid": songid or meta["songid"],
                "media_mid": media_mid,
                "songName": meta["name"] or songmid,
                "singerName": meta["singer"],
                "albumName": meta["album_name"],
                "albummid": albummid,
                "cover": qqapi.cover_url(albummid) if albummid else "",
                "_detailFetched": True,
            }
        except Exception as err:
            service.log_warn(f"详情失败: {err}")

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
