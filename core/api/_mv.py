"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import _safe_num, request, singer_text, unwrap_data

# ──────────── MV ────────────


def normalize_mv_item(item: dict, idx: int = 0) -> dict | None:
    """MV 条目规范化：兼容 /mv/tag（vid/mvtitle/singer_name/picurl/publish_date/play_count）
    与 /search t=12（v_id/mv_name/mv_pic_url）两种返回结构。"""
    if not isinstance(item, dict):
        return None
    vid = item.get("vid") or item.get("v_id") or ""
    if not vid:
        return None
    title = (
        item.get("mv_name")
        or item.get("mvname")
        or item.get("name")
        or item.get("mvtitle")
        or item.get("title")
        or item.get("songname")
        or ""
    )
    return {
        "index": idx + 1,
        "vid": vid,
        "mvtitle": title,
        "name": title,
        "singerName": singer_text(item),
        "cover": (
            item.get("mv_pic_url")
            or item.get("pic")
            or item.get("picurl")
            or item.get("cover")
            or ""
        ),
        "pubdate": (
            item.get("publish_date")
            or item.get("pubdate")
            or item.get("pub_date")
            or item.get("publictime")
            or ""
        ),
        "listennum": int(
            _safe_num(
                item.get("play_count")
                or item.get("listennum")
                or item.get("listenNum")
                or item.get("playcnt")
                or item.get("cnt")
                or 0
            )
        ),
        "duration": int(
            _safe_num(
                item.get("duration")
                or item.get("durationSec")
                or item.get("mv_duration")
                or 0
            )
        ),
        "raw": item,
    }
async def search_mv(
    keyword: str, *, page_no: int = 1, page_size: int = 10, user_key: str = ""
) -> list:
    """搜索 MV（/search t=12）"""
    body = await request(
        "/search",
        {"key": keyword, "t": 12, "pageNo": page_no, "pageSize": page_size},
        "get",
        user_key,
    )
    raw_list = unwrap_data(body).get("list") or []
    return [n for n in (normalize_mv_item(it, i) for i, it in enumerate(raw_list)) if n]
async def mv_category(user_key: str = "") -> dict:
    """MV 分类（/mv/category）"""
    body = await request("/mv/category", {}, "get", user_key)
    d = unwrap_data(body)
    return {
        "area": d.get("area") if isinstance(d.get("area"), list) else [],
        "version": d.get("version") if isinstance(d.get("version"), list) else [],
        "list": d.get("list") if isinstance(d.get("list"), list) else [],
    }
async def mv_by_tag(
    tag_id, *, page_no: int = 1, page_size: int = 20, user_key: str = ""
) -> dict:
    """按分类标签浏览 MV（/mv/tag）"""
    body = await request(
        "/mv/tag",
        {"tagId": tag_id, "pageNo": page_no, "pageSize": page_size},
        "get",
        user_key,
    )
    d = unwrap_data(body)
    raw = (
        d.get("list")
        if isinstance(d.get("list"), list)
        else (d.get("mvlist") if isinstance(d.get("mvlist"), list) else [])
    )
    return {
        "list": [n for n in (normalize_mv_item(it, i) for i, it in enumerate(raw)) if n],
        "total": int(_safe_num(d.get("total") or 0)),
    }
async def mv_url(vid: str, user_key: str = "") -> str:
    """获取 MV 播放地址（/mv/url）。仅取 code=0 可用条目，倒序优先体积小的，提高发送成功率。"""
    if not vid:
        return ""
    body = await request("/mv/url", {"id": vid}, "get", user_key)
    d = unwrap_data(body)
    mp4s = d.get("mp4") if isinstance(d.get("mp4"), list) else []
    usable = [m for m in mp4s if isinstance(m, dict) and _safe_num(m.get("code")) == 0]
    src = ""
    for m in reversed(usable):
        for key in ("freeflow_url", "comm_url"):
            arr = m.get(key)
            if isinstance(arr, list):
                for u in arr:
                    if isinstance(u, str) and u.strip():
                        src = u
                        break
                if src:
                    break
        if src:
            break
        base = ""
        if isinstance(m.get("url"), list):
            for u in m["url"]:
                if isinstance(u, str) and u.strip():
                    base = u
                    break
        url_path = m.get("urlPath")
        if base and url_path:
            p = str(url_path)
            src = base.rstrip("/") + (p if p.startswith("/") else f"/{p}")
            break
        if base and len(base) > 10:
            src = base
            break
    url = str(src or "").strip()
    return url.replace("http://", "https://", 1) if url else ""
