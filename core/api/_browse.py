"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from typing import Any

from ._core import request, unwrap_data
from ._search import normalize_track_item

# ──────────── 歌手 ────────────


async def singer_songs(
    singermid: str,
    *,
    page_no: int = 1,
    page_size: int = 50,
    order: int = 1,
    user_key: str = "",
) -> dict:
    body = await request(
        "/singer/songs",
        {
            "singermid": singermid,
            "pageNo": page_no,
            "pageSize": page_size,
            "order": order,
        },
        "get",
        user_key,
    )
    d = (body or {}).get("data") or {}
    raw_list = d.get("list") or []
    out = []
    for idx, item in enumerate(raw_list):
        raw = item.get("songInfo") if isinstance(item, dict) else None
        if not isinstance(raw, dict):
            raw = item
        norm = normalize_track_item(raw, idx, unwrap=True)
        if norm:
            out.append(norm)
    return {
        "list": out,
        "total": d.get("total") or 0,
        "pageNo": d.get("pageNo") or page_no,
        "singermid": singermid,
    }
async def singer_desc(singermid: str, user_key: str = "") -> Any:
    body = await request("/singer/desc", {"singermid": singermid}, "get", user_key)
    return (body or {}).get("data") or body
# ──────────── 专辑 ────────────


async def album_songs(
    albummid: str, *, begin: int = 0, num: int = 999, user_key: str = ""
) -> dict:
    body = await request(
        "/album/songs",
        {"albummid": albummid, "begin": begin, "num": num},
        "get",
        user_key,
    )
    d = (body or {}).get("data") or {}
    lst = (
        d.get("list")
        if isinstance(d.get("list"), list)
        else (d.get("songs") if isinstance(d.get("songs"), list) else [])
    )
    out = [n for n in (normalize_track_item(it, i, unwrap=True) for i, it in enumerate(lst)) if n]
    return {"list": out, "total": d.get("total") or 0, "albummid": albummid}
# ──────────── 歌单 ────────────


def _playlist_track_view(norm: dict) -> dict:
    """歌单曲目对外形态（字段集与历史实现一致）。"""
    return {
        "index": norm["index"],
        "songmid": norm["songmid"],
        "songid": norm["songid"],
        "media_mid": norm["media_mid"],
        "songName": norm["songName"],
        "singerName": norm["singerName"],
        "albumName": norm["albumName"],
        "albummid": norm["albummid"],
        "cover": norm["cover"],
        "payplay": norm["payplay"],
    }
async def songlist_detail(disstid: str, user_key: str = "") -> dict:
    body = await request("/songlist", {"id": disstid}, "get", user_key)
    d = unwrap_data(body)
    raw = d.get("songlist") or d.get("songs") or d.get("list") or []
    # 取值优先级复用统一归一化实现；字段集保持歌单卡/文案的历史形态
    # （不含 duration / interval / raw，避免给 #qqm歌单 的列表文案新增时长后缀）
    songs = []
    for idx, item in enumerate(raw if isinstance(raw, list) else []):
        norm = normalize_track_item(item, idx)
        if not norm or not norm.get("songmid"):
            continue
        songs.append(_playlist_track_view(norm))
    creator_obj = d.get("creator") if isinstance(d.get("creator"), dict) else {}
    creator = (
        (creator_obj.get("nick") or creator_obj.get("nickname") or "")
        or d.get("nickname")
        or ""
    )
    return {
        "dissname": d.get("dissname") or d.get("title") or d.get("name") or "",
        "songCount": d.get("song_count") or d.get("songCount") or len(songs),
        "listenNum": d.get("listennum") or d.get("listenNum") or 0,
        "disstid": disstid,
        "creator": creator,
        "songlist": songs,
        "raw": d,
    }
