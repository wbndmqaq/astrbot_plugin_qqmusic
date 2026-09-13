"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from typing import Any

from ._core import request, unwrap_data
from ._search import normalize_track_item

# ──────────── 排行榜 ────────────


async def top_category(user_key: str = "") -> list:
    body = await request("/top/category", {}, "get", user_key)
    return (body or {}).get("data") or []
async def top_detail(
    top_id,
    *,
    page_no: int = 1,
    page_size: int = 100,
    period: str = "",
    user_key: str = "",
) -> Any:
    params = {"id": top_id, "pageNo": page_no, "pageSize": page_size}
    if period:
        params["period"] = period
    body = await request("/top", params, "get", user_key)
    return (body or {}).get("data") or body
# ──────────── 新歌速递 ────────────


async def new_songs(type_: int = 5, *, num: int = 20, user_key: str = "") -> list:
    """新歌速递（/song/new）：type 1 内地 / 2 欧美 / 3 日本 / 4 韩国 / 5 最新 / 6 港台，默认 5"""
    body = await request(
        "/song/new", {"type": int(type_), "num": int(num)}, "get", user_key
    )
    raw_list = unwrap_data(body).get("list") or []
    return [
        n
        for n in (normalize_track_item(it, i, unwrap=True) for i, it in enumerate(raw_list))
        if n
    ]
