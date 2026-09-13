"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from ._core import _safe_num, _strip_tags, payplay_of, request, singer_text
from ._resolve_link import cover_url

# ──────────── 搜索 ────────────


async def search_songs(
    keyword: str, *, page_no: int = 1, page_size: int = 10, user_key: str = ""
) -> list:
    body = await request(
        "/search",
        {"key": keyword, "t": 0, "pageNo": page_no, "pageSize": page_size},
        "get",
        user_key,
    )
    raw_list = (((body or {}).get("data") or {}).get("list")) or []
    out = []
    for idx, item in enumerate(raw_list):
        norm = normalize_track_item(item, idx, unwrap=True)
        if norm:
            out.append(norm)
    return out
def normalize_track_item(item: dict, idx: int = 0, *, unwrap: bool = False) -> dict | None:
    """把各端点返回的原始曲目条目归一化为插件内部统一格式（全仓唯一实现）。

    原先存在三份字段集互不一致的实现（搜索结果归一化、电台条目归一化、
    ``songlist_detail`` 内联循环）外加 ``MusicService.normalize_song``，字段
    优先级的差异只能靠肉眼比对。这里合并为一份，取值链是各调用点原有逻辑的并集，
    每个调用点的结果都不劣于原实现：

    * ``media_mid``：media_mid → strMediaMid → songmid → file.media_mid → mid
      （电台条目只有 mid，故结果与原实现一致；其余链路优先显式 media_mid/songmid，
      缺失时才用 file.media_mid 兜底，比原先直接用 songmid 更准确）
    * ``cover``：有 albummid 走封面 CDN，否则回退 album.pic / album.cover
      （原实现有两处缺这个兜底，导致卡片封面为空）
    * ``songName``：songname → songname_hilight（去标签）→ name → title
    * ``unwrap=True``：兼容搜索/详情等把曲目包在 data / track_info 里的返回结构
    """
    if not isinstance(item, dict):
        return None
    src = item
    if unwrap:
        for key in ("data", "track_info"):
            inner = item.get(key)
            if isinstance(inner, dict):
                src = inner
                break

    singer = singer_text(src)
    album = src.get("album") if isinstance(src.get("album"), dict) else {}
    albummid = src.get("albummid") or album.get("mid") or ""
    cover = (
        cover_url(albummid)
        if albummid
        else album.get("pic") or album.get("cover") or ""
    )
    interval = int(_safe_num(src.get("interval") or src.get("songTime") or 0))
    duration = f"{interval // 60:02d}:{interval % 60:02d}" if interval > 0 else ""
    f = src.get("file") if isinstance(src.get("file"), dict) else {}
    payplay = payplay_of(src)
    return {
        "index": idx + 1,
        "songmid": src.get("songmid") or src.get("mid") or "",
        "songid": src.get("songid") or src.get("id") or 0,
        "media_mid": src.get("media_mid")
        or src.get("strMediaMid")
        or src.get("songmid")
        or (f.get("media_mid") if f else "")
        or src.get("mid")
        or "",
        "songName": src.get("songname")
        or _strip_tags(src.get("songname_hilight"))
        or src.get("name")
        or src.get("title")
        or "",
        "singerName": singer,
        "albumName": src.get("albumname") or album.get("name") or "",
        "albummid": albummid,
        "cover": cover,
        "duration": duration,
        "interval": interval,
        "payplay": payplay,
        "msgid": src.get("msgid"),
        "raw": item,
    }
# ──────────── 搜索扩展 ────────────


async def search_singers(
    keyword: str, *, page_no: int = 1, page_size: int = 20, user_key: str = ""
) -> list:
    body = await request(
        "/search",
        {"key": keyword, "t": 9, "pageNo": page_no, "pageSize": page_size},
        "get",
        user_key,
    )
    raw_list = (((body or {}).get("data") or {}).get("list")) or []
    out = []
    for idx, item in enumerate(raw_list):
        mid = item.get("singerMID") or item.get("singermid") or item.get("mid") or ""
        out.append(
            {
                "index": idx + 1,
                "singermid": mid,
                "singerName": item.get("singerName")
                or item.get("name")
                or item.get("singer")
                or "",
                "songNum": item.get("songNum") or item.get("songnum") or 0,
                "albumNum": item.get("albumNum") or item.get("albumnum") or 0,
                "cover": f"https://y.gtimg.cn/music/photo_new/T001R300x300M000{mid}.jpg"
                if mid
                else "",
                "raw": item,
            }
        )
    return out
async def search_albums(
    keyword: str, *, page_no: int = 1, page_size: int = 20, user_key: str = ""
) -> list:
    body = await request(
        "/search",
        {"key": keyword, "t": 8, "pageNo": page_no, "pageSize": page_size},
        "get",
        user_key,
    )
    raw_list = (((body or {}).get("data") or {}).get("list")) or []
    out = []
    for idx, item in enumerate(raw_list):
        mid = item.get("albumMID") or item.get("albummid") or item.get("mid") or ""
        out.append(
            {
                "index": idx + 1,
                "albummid": mid,
                "albumName": item.get("albumName") or item.get("name") or "",
                "singerName": item.get("singerName") or item.get("singer") or "",
                "songCount": item.get("song_count") or item.get("songCount") or 0,
                "publicTime": item.get("publicTime") or item.get("publish_date") or "",
                "cover": cover_url(mid) if mid else "",
                "raw": item,
            }
        )
    return out
async def search_songlists(
    keyword: str, *, page_no: int = 1, page_size: int = 20, user_key: str = ""
) -> list:
    body = await request(
        "/search",
        {"key": keyword, "t": 2, "pageNo": page_no, "pageSize": page_size},
        "get",
        user_key,
    )
    raw_list = (((body or {}).get("data") or {}).get("list")) or []
    out = []
    for idx, item in enumerate(raw_list):
        creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
        out.append(
            {
                "index": idx + 1,
                "disstid": item.get("dissid")
                or item.get("disstid")
                or item.get("id")
                or "",
                "dissname": item.get("dissname")
                or item.get("title")
                or item.get("name")
                or "",
                "creator": creator.get("nick")
                or creator.get("nickname")
                or item.get("nickname")
                or "",
                "songCount": item.get("song_count")
                or item.get("songCount")
                or item.get("songnum")
                or 0,
                "listenNum": item.get("listennum") or item.get("listen_count") or 0,
                "cover": item.get("imgurl") or item.get("logo") or "",
                "raw": item,
            }
        )
    return out
