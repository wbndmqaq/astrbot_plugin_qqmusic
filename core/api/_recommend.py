"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

import json

from ._browse import songlist_detail
from ._core import ApiError, request, unwrap_data
from ._search import normalize_track_item

# ──────────── 推荐 ────────────


async def recommend_hot(user_key: str = "") -> list:
    body = await request("/recommend/playlist/u", {}, "get", user_key)
    return (((body or {}).get("data") or {}).get("list")) or []
async def recommend_feed(user_key: str = "") -> list:

    import random

    try:
        body = await request(
            "/cgi",
            {
                "module": "recommend.RecommendFeedServer",
                "method": "get_recommend_feed",
                "param": json.dumps(
                    {"direction": 1, "page": 1, "v_cache": [], "v_uniq": [], "s_num": 0}
                ),
            },
            "get",
            user_key,
        )
        data = (body or {}).get("data") or {}
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        v_shelf = inner.get("v_shelf") or data.get("v_shelf") or []
        playlists = []
        for shelf in v_shelf:
            for niche in shelf.get("v_niche") or []:
                for card in niche.get("v_card") or []:
                    if card.get("id") and card.get("type") == 500:
                        playlists.append(
                            {
                                "disstid": card.get("id"),
                                "dissname": card.get("title") or "",
                                "cover": card.get("cover") or "",
                                "listenNum": card.get("cnt") or 0,
                            }
                        )
        if not playlists:
            return []
        pl = random.choice(playlists)  # noqa: S311 非安全场景（随机推荐歌单）
        detail = await songlist_detail(pl["disstid"], user_key)
        return detail.get("songlist") or []
    except Exception:
        return []
async def personal_radio(count: int = 5, user_key: str = "") -> list:
    body = await request(
        "/cgi",
        {
            "module": "pc_track_radio_svr",
            "method": "get_radio_track",
            "param": json.dumps({"id": 99, "num": count}),
        },
        "get",
        user_key,
    )
    data = (body or {}).get("data") or {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    tracks = inner.get("tracks") or data.get("tracks") or []
    # 与其它端点一致地过滤归一化失败的条目（normalize_track_item 会返回 None）：
    # 否则 #qqm电台 会在卡片构建时解引用 None，真实原因被 handler 的通用 except
    # 兜成「电台失败，请稍后重试」
    return [
        n
        for n in (normalize_track_item(t, idx) for idx, t in enumerate(tracks) if t)
        if n
    ]
# ──────────── 每日推荐 / 收藏（dirid: 202=日推, 201=收藏） ────────────


async def user_diss_list(
    dirid: int = 202, *, song_begin: int = 0, song_num: int = 30, user_key: str = ""
) -> dict:
    body = await request(
        "/cgi",
        {
            "module": "srf_diss_info.DissInfoServer",
            "method": "CgiGetDiss",
            "param": json.dumps(
                {
                    "disstid": 0,
                    "dirid": dirid,
                    "onlysonglist": 0,
                    "song_begin": song_begin,
                    "song_num": song_num,
                    "userinfo": 1,
                    "pic_dpi": 800,
                    "orderlist": 1,
                }
            ),
        },
        "get",
        user_key,
    )
    d = unwrap_data(unwrap_data(body))
    songlist = d.get("songlist") or d.get("song_list") or []
    songs = [
        n for n in (normalize_track_item(it, i, unwrap=True) for i, it in enumerate(songlist)) if n
    ]
    dirinfo = d.get("dirinfo") or {}
    return {
        "songs": songs,
        "title": dirinfo.get("title") or "",
        "desc": dirinfo.get("desc") or "",
    }
def _diss_payload(body) -> dict:
    """把 API 专用端点返回的 { list, title, desc } 转成插件侧 { songs, title, desc }"""
    d = unwrap_data(body)
    lst = d.get("list") if isinstance(d.get("list"), list) else []
    songs = [
        n for n in (normalize_track_item(it, i, unwrap=True) for i, it in enumerate(lst)) if n
    ]
    return {"songs": songs, "title": d.get("title") or "", "desc": d.get("desc") or ""}
async def daily_recommend(
    *, song_begin: int = 0, song_num: int = 30, user_key: str = ""
) -> dict:
    """每日推荐：优先 /recommend/daily 专用端点，失败回退 /cgi disslist(202)"""
    try:
        body = await request(
            "/recommend/daily",
            {"songBegin": song_begin, "num": song_num},
            "get",
            user_key,
        )
        return _diss_payload(body)
    except ApiError:
        return await user_diss_list(
            202, song_begin=song_begin, song_num=song_num, user_key=user_key
        )
async def user_favorites(
    *, song_begin: int = 0, song_num: int = 30, user_key: str = ""
) -> dict:
    """我的收藏：优先 /user/liked 专用端点，失败回退 /cgi disslist(201)。

    大数量请求（>30）两个端点都失败时，自动降级为 30 再试一次。
    """
    for num in list(dict.fromkeys((int(song_num), min(int(song_num), 30)))):
        try:
            try:
                body = await request(
                    "/user/liked",
                    {"songBegin": song_begin, "num": num},
                    "get",
                    user_key,
                )
                return _diss_payload(body)
            except ApiError:
                return await user_diss_list(
                    201, song_begin=song_begin, song_num=num, user_key=user_key
                )
        except ApiError:
            continue
    raise ApiError("获取收藏列表失败")
