from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .quality import (
    QUALITY_LABEL,
    is_quality_size_ok,
    pick_best_available_quality,
    quality_candidates,
    summarize_file_sizes,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 模块级配置访问器，由 main.py 在插件加载时注入
_cfg_getter = None


def set_config_getter(fn):
    global _cfg_getter
    _cfg_getter = fn


def _cfg() -> dict:
    if _cfg_getter is not None:
        try:
            return _cfg_getter() or {}
        except Exception:
            return {}
    return {}


class ApiError(Exception):

    def __init__(self, message: str, *, code=None, payload=None, pay=None, retcode=None, tip=None):
        super().__init__(message)
        self.code = code
        self.payload = payload
        self.pay = pay
        self.retcode = retcode
        self.tip = tip


def _get_base() -> str:
    base = str(_cfg().get("apiBase") or "")
    return base.rstrip("/")


def _get_token() -> str:
    c = _cfg()
    token = c.get("apiToken") or c.get("api_token") or ""
    if not token:
        # 与 JS 版一致：支持环境变量 QQMUSIC_API_TOKEN（对应 API 端 .env 的 QQMUSIC_API_TOKEN）
        import os
        token = os.environ.get("QQMUSIC_API_TOKEN") or ""
    return str(token).strip()


def _sanitize_for_header(value: str) -> str:

    return re.sub(r"[\r\n\t]", "", re.sub(r"[^\x20-\x7E]", "", str(value or ""))).strip()


def _query_safe_params(params: dict) -> dict:

    out: dict = {}
    for k, v in params.items():
        if isinstance(v, bool):
            out[k] = int(v)
        elif v is None:
            continue
        elif isinstance(v, (str, int, float)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _empty_url_result(type_: str, media_id: str, extra: dict | None = None) -> dict:
    base = {
        "url": "",
        "quality": type_,
        "mediaId": media_id,
        "tip": "",
        "retcode": None,
        "hasLogin": None,
        "pay": None,
        "refreshed": None,
        "refreshReason": None,
        "triedChannels": None,
        "raw": None,
    }
    if extra:
        base.update(extra)
    return base


async def request(pathname: str, params: dict | None = None, method: str = "get", user_key: str = "") -> Any:

    params = dict(params or {})
    base = _get_base()
    url = f"{base}{pathname if pathname.startswith('/') else '/' + pathname}"
    if user_key:
        params["userKey"] = user_key

    token = _get_token()
    headers = {}
    safe_user_key = _sanitize_for_header(user_key)
    if safe_user_key:
        headers["x-qqmusic-user"] = safe_user_key
    if token:
        headers["x-api-token"] = token
        headers["Authorization"] = f"Bearer {token}"

    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            if method == "get":
                # GET 参数走 query：规整 bool/None，避免 aiohttp 严格类型校验抛错
                async with sess.get(url, params=_query_safe_params(params), headers=headers or None) as res:
                    return await _handle_response(res, base, url)
            else:
                async with sess.post(url, json=params, headers=headers or None) as res:
                    return await _handle_response(res, base, url)
    except aiohttp.ClientConnectorError as e:
        raise ApiError(f"无法连接 QQ 音乐 API（{base}），请先申请API") from e
    except aiohttp.ServerTimeoutError as e:
        raise ApiError(f"请求超时：{base}") from e
    except aiohttp.ClientError as e:
        raise ApiError(f"网络错误：{e}") from e


async def _handle_response(res: aiohttp.ClientResponse, base: str, url: str):
    status = res.status
    try:
        data = await res.json(content_type=None)
    except Exception:
        text = await res.text()
        if status == 401:
            raise ApiError("API 鉴权失败（401）：请检查插件 apiToken 与 API 的 QQMUSIC_API_TOKEN 是否一致")
        if status == 403:
            raise ApiError("API 拒绝访问（403）：IP 可能不在白名单")
        if status == 429:
            raise ApiError("API 限流（429）：请求过于频繁")
        if status >= 400:
            raise ApiError(f"HTTP {status}")
        raise ApiError(f"返回非 JSON：{text[:200]}")

    if status == 401:
        raise ApiError("API 鉴权失败（401）：请检查插件 apiToken 与 API 的 QQMUSIC_API_TOKEN 是否一致")
    if status == 403:
        raise ApiError("API 拒绝访问（403）：IP 可能不在白名单")
    if status == 429:
        raise ApiError("API 限流（429）：请求过于频繁")
    if status >= 400:
        raise ApiError(f"HTTP {status}")

    result = data.get("result") if isinstance(data, dict) else None
    if result is not None and result not in (100, 0):
        err_msg = data.get("errMsg") or f"API result={result}"
        raise ApiError(
            err_msg,
            code=result,
            payload=data,
            pay=data.get("pay"),
            retcode=data.get("retcode"),
            tip=data.get("tip"),
        )
    return data


# ──────────── 登录态 ────────────


async def pull_login_meta(user_key: str = "") -> dict:
    st = await request("/login/status", {}, "get", user_key)
    d = (st or {}).get("data") or {}
    return {
        "login": bool(d.get("login")),
        "userKey": d.get("userKey") or user_key or "default",
        "uin": re.sub(r"\D", "", str(d.get("uin") or "")),
        "nick": d.get("nick") or "",
        "hasKey": bool(d.get("hasKey")),
        "hasRefresh": bool(d.get("hasRefresh")),
        "loginType": d.get("login_type"),
        "tmeLoginType": d.get("tmeLoginType"),
        "keyAgeSec": d.get("keyAgeSec"),
    }


async def list_accounts() -> list:
    body = await request("/login/accounts")
    return (((body or {}).get("data") or {}).get("accounts")) or []


async def refresh_login(user_key: str = ""):
    try:
        return await request("/login/refresh", {}, "post", user_key)
    except ApiError:
        return await request("/user/refresh", {}, "get", user_key)


# ──────────── 搜索 ────────────


async def search_songs(keyword: str, *, page_no: int = 1, page_size: int = 10, user_key: str = "") -> list:
    body = await request("/search", {"key": keyword, "t": 0, "pageNo": page_no, "pageSize": page_size}, "get", user_key)
    raw_list = (((body or {}).get("data") or {}).get("list")) or []
    out = []
    for idx, item in enumerate(raw_list):
        norm = _normalize_search_item(item, idx)
        if norm:
            out.append(norm)
    return out


def _normalize_search_item(item: dict, idx: int = 0) -> dict | None:
    raw = item.get("data") if isinstance(item, dict) else None
    if not isinstance(raw, dict):
        raw = item.get("track_info") if isinstance(item, dict) else None
    if not isinstance(raw, dict):
        raw = item if isinstance(item, dict) else None
    if not isinstance(raw, dict):
        return None

    singer_arr = raw.get("singer")
    if isinstance(singer_arr, list):
        singer = " / ".join(s.get("name") or s.get("title") or "" for s in singer_arr if isinstance(s, dict))
    else:
        singer = raw.get("singername") or raw.get("singerName") or raw.get("singer") or ""

    albummid = raw.get("albummid") or (raw.get("album") or {}).get("mid") if isinstance(raw.get("album"), dict) else raw.get("albummid") or ""
    cover = cover_url(albummid) if albummid else ((raw.get("album") or {}).get("pic") if isinstance(raw.get("album"), dict) else "") or ((raw.get("album") or {}).get("cover") if isinstance(raw.get("album"), dict) else "")
    interval = int(_safe_num(raw.get("interval") or raw.get("songTime") or 0))
    duration = f"{interval // 60:02d}:{interval % 60:02d}" if interval > 0 else ""

    pay = raw.get("pay") or {}
    return {
        "index": idx + 1,
        "songmid": raw.get("songmid") or raw.get("mid") or "",
        "songid": raw.get("songid") or raw.get("id") or 0,
        "media_mid": raw.get("media_mid") or raw.get("strMediaMid") or raw.get("songmid") or "",
        "songName": raw.get("songname") or _strip_tags(raw.get("songname_hilight")) or raw.get("name") or raw.get("title") or "",
        "singerName": singer,
        "albumName": raw.get("albumname") or (raw.get("album") or {}).get("name") or "",
        "albummid": albummid,
        "cover": cover,
        "duration": duration,
        "interval": interval,
        "payplay": pay.get("payplay") if isinstance(pay, dict) else (pay.get("pay_play") if isinstance(pay, dict) else raw.get("payplay")),
        "msgid": raw.get("msgid"),
        "raw": item,
    }


# ──────────── 歌曲详情/播放链 ────────────


async def song_detail(songmid: str, user_key: str = "") -> Any:
    body = await request("/song", {"songmid": songmid}, "get", user_key)
    return (body or {}).get("data")


def _map_song_url_body(body: dict, type_: str, real_media: str) -> dict:
    url = ""
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, str):
        url = data
    elif isinstance(data, dict):
        url = data.get("url") or ""

    if url:
        return {
            "url": url,
            "file": body.get("file") or (data.get("file") if isinstance(data, dict) else None),
            "domain": body.get("domain") or (data.get("domain") if isinstance(data, dict) else None),
            "purl": body.get("purl") or (data.get("purl") if isinstance(data, dict) else None),
            "quality": type_,
            "mediaId": body.get("mediaId") or real_media,
            "pay": body.get("pay") or (data.get("pay") if isinstance(data, dict) else None),
            "refreshed": body.get("refreshed"),
            "playChannel": body.get("playChannel"),
        }
    return _empty_url_result(
        type_,
        body.get("mediaId") or real_media,
        {
            "raw": body,
            "tip": body.get("tip") or body.get("errMsg") or "",
            "retcode": body.get("retcode"),
            "hasLogin": body.get("hasLogin"),
            "pay": body.get("pay"),
            "refreshed": body.get("refreshed"),
            "refreshReason": body.get("refreshReason"),
            "triedChannels": body.get("triedChannels"),
        },
    )


async def song_url(songmid: str, *, type_: str = "128", media_id: str = "", channel: str = "auto", user_key: str = "") -> dict:
    real_media = media_id or songmid
    try:
        body = await request(
            "/song/url",
            {"id": songmid, "type": type_, "mediaId": real_media, "channel": channel},
            "post",
            user_key,
        )
        return _map_song_url_body(body or {}, type_, real_media)
    except ApiError as e:
        p = e.payload or {}
        return _empty_url_result(
            type_,
            p.get("mediaId") or real_media,
            {
                "raw": p,
                "tip": p.get("errMsg") or p.get("tip") or str(e),
                "retcode": p.get("retcode") if e.retcode is None else e.retcode,
                "hasLogin": p.get("hasLogin"),
                "pay": p.get("pay") if e.pay is None else e.pay,
                "refreshed": p.get("refreshed"),
                "refreshReason": p.get("refreshReason"),
                "triedChannels": p.get("triedChannels"),
                "error": str(e),
            },
        )


async def _probe_url_alive(url: str, timeout: int = 6) -> bool:
    if not url:
        return False
    headers = {"User-Agent": UA, "Referer": "https://y.qq.com/", "Origin": "https://y.qq.com/"}
    # 两阶段：先 HEAD，失败再 Range GET 嗅探首字节
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            try:
                async with sess.head(url, headers=headers, allow_redirects=True) as head:
                    if 0 < head.status < 400:
                        return True
                    if head.status in (401, 403, 404):
                        return False
            except Exception:
                pass
            headers["Range"] = "bytes=0-1023"
            async with sess.get(url, headers=headers, allow_redirects=True) as g:
                if g.status >= 400:
                    return False
                buf = await g.content.read(1024)
                if len(buf) < 16:
                    return False
                head_str = buf[:32].decode("utf-8", errors="ignore").lower()
                if "<html" in head_str or "<!doctype" in head_str:
                    return False
                return True
    except Exception:
        return False


async def song_url_best(
    songmid: str,
    *,
    quality: str = "flac",
    media_id: str = "",
    fallback: bool = True,
    probe: bool = True,
    user_key: str = "",
) -> dict:

    preferred = (quality or "flac").lower()
    candidate_list = quality_candidates(preferred, fallback)
    real_media = media_id or songmid
    size_info: dict | None = None
    predicted = ""

    try:
        detail = await song_detail(songmid, user_key)
        file = (detail or {}).get("track_info") or {}
        file = file.get("file") if isinstance(file, dict) else None
        if not isinstance(file, dict):
            file = (detail or {}).get("file") if isinstance(detail, dict) else None
        if isinstance(file, dict):
            real_media = media_id or file.get("media_mid") or file.get("master_tape_media_mid") or songmid
            size_info = file
            predicted = pick_best_available_quality(file, preferred)
    except Exception:
        pass

    last_err: ApiError | Exception | None = None
    tried: list[str] = []

    for type_ in candidate_list:
        if size_info and not is_quality_size_ok(type_, size_info):
            tried.append(f"{type_}:skip-size")
            continue
        try:
            r = await song_url(songmid, type_=type_, media_id=real_media, user_key=user_key)
            if not r.get("url"):
                tried.append(f"{type_}:no-url")
                last_err = ApiError(r.get("tip") or f"{type_} 无播放链", payload=r.get("raw") or r, pay=r.get("pay"), retcode=r.get("retcode"))
                continue

            file_name = str(r.get("file") or r.get("url") or "")
            if re.search(r"RS01|RS02|Q000", file_name, re.I) and size_info and not is_quality_size_ok(type_, size_info):
                tried.append(f"{type_}:skip-fake-file")
                continue

            if probe:
                ok = await _probe_url_alive(r["url"])
                if not ok:
                    tried.append(f"{type_}:cdn-dead")
                    last_err = ApiError(f"{type_} CDN 不可用")
                    continue

            tried.append(f"{type_}:ok")
            r.update(
                quality=type_,
                qualityLabel=QUALITY_LABEL.get(type_, type_),
                mediaId=real_media,
                adaptedFrom=preferred,
                predicted=predicted,
                tried=tried,
                playChannel=r.get("playChannel"),
            )
            return r
        except ApiError as e:
            last_err = e
            tried.append(f"{type_}:err")
        except Exception as e:
            last_err = e
            tried.append(f"{type_}:err")

    hint = f" 已尝试: {', '.join(tried)}" if tried else ""
    payload = (getattr(last_err, "payload", None) if last_err else {}) or {}
    pay = payload.get("pay") if isinstance(payload, dict) else None
    if last_err is not None and getattr(last_err, "pay", None):
        pay = last_err.pay
    pay_hint = " 该曲需会员播放，请 #qqm登录" if pay and _safe_num((pay or {}).get("pay_play")) == 1 else ""
    detail_msg = ""
    if isinstance(payload, dict):
        detail_msg = payload.get("errMsg") or payload.get("tip") or ""
    if not detail_msg and last_err:
        detail_msg = str(last_err)
    msg = f"{detail_msg}{pay_hint}{hint}" if detail_msg else f"所有音质均无可用链接（可 #qqm登录 重新扫码）{pay_hint}{hint}"
    err = last_err or ApiError(msg)
    if isinstance(err, ApiError):
        if not err.args[0] or err.args[0] == "Error":
            err.args = (msg,)
        elif hint and "已尝试" not in err.args[0]:
            err.args = (err.args[0] + hint,)
        if pay:
            err.pay = pay
        err.payload = payload
    raise err


# ──────────── 歌词/热搜 ────────────


async def lyric(songmid: str, user_key: str = "") -> Any:
    body = await request("/lyric", {"songmid": songmid}, "get", user_key)
    return (body or {}).get("data") or body


async def hot_keys(user_key: str = "") -> list:
    body = await request("/search/hot", {}, "get", user_key)
    return (body or {}).get("data") or []


# ──────────── 排行榜 ────────────


async def top_category(user_key: str = "") -> list:
    body = await request("/top/category", {}, "get", user_key)
    return (body or {}).get("data") or []


async def top_detail(top_id, *, page_no: int = 1, page_size: int = 100, period: str = "", user_key: str = "") -> Any:
    params = {"id": top_id, "pageNo": page_no, "pageSize": page_size}
    if period:
        params["period"] = period
    body = await request("/top", params, "get", user_key)
    return (body or {}).get("data") or body


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
                "param": json.dumps({"direction": 1, "page": 1, "v_cache": [], "v_uniq": [], "s_num": 0}),
            },
            "get",
            user_key,
        )
        v_shelf = (((body or {}).get("data") or {}).get("data") or {}).get("v_shelf") or ((body or {}).get("data") or {}).get("v_shelf") or []
        playlists = []
        for shelf in v_shelf:
            for niche in (shelf.get("v_niche") or []):
                for card in (niche.get("v_card") or []):
                    if card.get("id") and card.get("type") == 500:
                        playlists.append({"disstid": card.get("id"), "dissname": card.get("title") or "", "cover": card.get("cover") or "", "listenNum": card.get("cnt") or 0})
        if not playlists:
            return []
        pl = random.choice(playlists)
        try:
            detail = await songlist_detail(pl["disstid"], user_key)
            return detail.get("songlist") or []
        except Exception:
            return []
    except Exception:
        return []


async def personal_radio(count: int = 5, user_key: str = "") -> list:
    body = await request(
        "/cgi",
        {"module": "pc_track_radio_svr", "method": "get_radio_track", "param": json.dumps({"id": 99, "num": count})},
        "get",
        user_key,
    )
    tracks = (((body or {}).get("data") or {}).get("data") or {}).get("tracks") or ((body or {}).get("data") or {}).get("tracks") or []
    return [_normalize_radio_track(t, idx) for idx, t in enumerate(tracks) if t]


def _normalize_radio_track(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    singer_arr = item.get("singer")
    if isinstance(singer_arr, list):
        singer = " / ".join(s.get("name") or s.get("title") or "" for s in singer_arr if isinstance(s, dict))
    else:
        singer = item.get("singername") or item.get("singerName") or item.get("singer") or ""
    albummid = item.get("albummid") or (item.get("album") or {}).get("mid") if isinstance(item.get("album"), dict) else item.get("albummid") or ""
    interval = int(_safe_num(item.get("interval") or 0))
    duration = f"{interval // 60:02d}:{interval % 60:02d}" if interval > 0 else ""
    f = item.get("file") if isinstance(item.get("file"), dict) else {}
    return {
        "index": idx + 1,
        "songmid": item.get("mid") or item.get("songmid") or "",
        "songid": item.get("id") or item.get("songid") or 0,
        "media_mid": (f.get("media_mid") if f else "") or item.get("media_mid") or item.get("mid") or "",
        "songName": item.get("name") or item.get("title") or item.get("songname") or "",
        "singerName": singer,
        "albumName": (item.get("album") or {}).get("name") if isinstance(item.get("album"), dict) else item.get("albumname") or "",
        "albummid": albummid,
        "cover": cover_url(albummid) if albummid else ((item.get("album") or {}).get("cover") if isinstance(item.get("album"), dict) else ""),
        "duration": duration,
        "interval": interval,
        "payplay": (item.get("pay") or {}).get("pay_play") if isinstance(item.get("pay"), dict) else item.get("payplay"),
        "raw": item,
    }


# ──────────── 每日推荐 / 收藏（dirid: 202=日推, 201=收藏） ────────────


async def user_diss_list(dirid: int = 202, *, song_begin: int = 0, song_num: int = 30, user_key: str = "") -> dict:
    body = await request(
        "/cgi",
        {
            "module": "srf_diss_info.DissInfoServer",
            "method": "CgiGetDiss",
            "param": json.dumps({"disstid": 0, "dirid": dirid, "onlysonglist": 0, "song_begin": song_begin, "song_num": song_num, "userinfo": 1, "pic_dpi": 800, "orderlist": 1}),
        },
        "get",
        user_key,
    )
    d = (((body or {}).get("data") or {}).get("data") or {}) or (body or {}).get("data") or {}
    songlist = d.get("songlist") or d.get("song_list") or []
    songs = [n for n in (_normalize_search_item(it, i) for i, it in enumerate(songlist)) if n]
    dirinfo = d.get("dirinfo") or {}
    return {"songs": songs, "title": dirinfo.get("title") or "", "desc": dirinfo.get("desc") or ""}


async def daily_recommend(**opts) -> dict:
    return await user_diss_list(202, **opts)


async def user_favorites(**opts) -> dict:
    return await user_diss_list(201, **opts)


# ──────────── 搜索扩展 ────────────


async def search_singers(keyword: str, *, page_no: int = 1, page_size: int = 20, user_key: str = "") -> list:
    body = await request("/search", {"key": keyword, "t": 9, "pageNo": page_no, "pageSize": page_size}, "get", user_key)
    raw_list = (((body or {}).get("data") or {}).get("list")) or []
    out = []
    for idx, item in enumerate(raw_list):
        mid = item.get("singerMID") or item.get("singermid") or item.get("mid") or ""
        out.append({
            "index": idx + 1,
            "singermid": mid,
            "singerName": item.get("singerName") or item.get("name") or item.get("singer") or "",
            "songNum": item.get("songNum") or item.get("songnum") or 0,
            "albumNum": item.get("albumNum") or item.get("albumnum") or 0,
            "cover": f"https://y.gtimg.cn/music/photo_new/T001R300x300M000{mid}.jpg" if mid else "",
            "raw": item,
        })
    return out


async def search_albums(keyword: str, *, page_no: int = 1, page_size: int = 20, user_key: str = "") -> list:
    body = await request("/search", {"key": keyword, "t": 8, "pageNo": page_no, "pageSize": page_size}, "get", user_key)
    raw_list = (((body or {}).get("data") or {}).get("list")) or []
    out = []
    for idx, item in enumerate(raw_list):
        mid = item.get("albumMID") or item.get("albummid") or item.get("mid") or ""
        out.append({
            "index": idx + 1,
            "albummid": mid,
            "albumName": item.get("albumName") or item.get("name") or "",
            "singerName": item.get("singerName") or item.get("singer") or "",
            "songCount": item.get("song_count") or item.get("songCount") or 0,
            "publicTime": item.get("publicTime") or item.get("publish_date") or "",
            "cover": cover_url(mid) if mid else "",
            "raw": item,
        })
    return out


async def search_songlists(keyword: str, *, page_no: int = 1, page_size: int = 20, user_key: str = "") -> list:
    body = await request("/search", {"key": keyword, "t": 2, "pageNo": page_no, "pageSize": page_size}, "get", user_key)
    raw_list = (((body or {}).get("data") or {}).get("list")) or []
    out = []
    for idx, item in enumerate(raw_list):
        out.append({
            "index": idx + 1,
            "disstid": item.get("dissid") or item.get("disstid") or item.get("id") or "",
            "dissname": item.get("dissname") or item.get("title") or item.get("name") or "",
            "creator": ((item.get("creator") or {}).get("nick") if isinstance(item.get("creator"), dict) else "") or (item.get("creator") or {}).get("nickname") if isinstance(item.get("creator"), dict) else item.get("nickname") or "",
            "songCount": item.get("song_count") or item.get("songCount") or item.get("songnum") or 0,
            "listenNum": item.get("listennum") or item.get("listen_count") or 0,
            "cover": item.get("imgurl") or item.get("logo") or "",
            "raw": item,
        })
    return out


# ──────────── 歌手 ────────────


async def singer_songs(singermid: str, *, page_no: int = 1, page_size: int = 50, order: int = 1, user_key: str = "") -> dict:
    body = await request("/singer/songs", {"singermid": singermid, "pageNo": page_no, "pageSize": page_size, "order": order}, "get", user_key)
    d = (body or {}).get("data") or {}
    raw_list = d.get("list") or []
    out = []
    for idx, item in enumerate(raw_list):
        raw = item.get("songInfo") if isinstance(item, dict) else None
        if not isinstance(raw, dict):
            raw = item
        norm = _normalize_search_item(raw, idx)
        if norm:
            out.append(norm)
    return {"list": out, "total": d.get("total") or 0, "pageNo": d.get("pageNo") or page_no, "singermid": singermid}


async def singer_album(singermid: str, *, page_no: int = 1, page_size: int = 50, user_key: str = "") -> Any:
    body = await request("/singer/album", {"singermid": singermid, "pageNo": page_no, "pageSize": page_size}, "get", user_key)
    return (body or {}).get("data") or {"list": [], "total": 0}


async def singer_desc(singermid: str, user_key: str = "") -> Any:
    body = await request("/singer/desc", {"singermid": singermid}, "get", user_key)
    return (body or {}).get("data") or body


# ──────────── 专辑 ────────────


async def album_detail(albummid: str, user_key: str = "") -> Any:
    body = await request("/album", {"albummid": albummid}, "get", user_key)
    return (body or {}).get("data") or body


async def album_songs(albummid: str, *, begin: int = 0, num: int = 999, user_key: str = "") -> dict:
    body = await request("/album/songs", {"albummid": albummid, "begin": begin, "num": num}, "get", user_key)
    d = (body or {}).get("data") or {}
    lst = d.get("list") if isinstance(d.get("list"), list) else (d.get("songs") if isinstance(d.get("songs"), list) else [])
    out = [n for n in (_normalize_search_item(it, i) for i, it in enumerate(lst)) if n]
    return {"list": out, "total": d.get("total") or 0, "albummid": albummid}


# ──────────── 歌单 ────────────


async def songlist_detail(disstid: str, user_key: str = "") -> dict:
    body = await request("/songlist", {"id": disstid}, "get", user_key)
    d = (body or {}).get("data") or body or {}
    raw = d.get("songlist") or d.get("songs") or d.get("list") or []
    songs = []
    for idx, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        singer_arr = item.get("singer")
        if isinstance(singer_arr, list):
            singer = " / ".join(s.get("name") for s in singer_arr if isinstance(s, dict) and s.get("name"))
        else:
            singer = item.get("singername") or item.get("singer") or ""
        albummid = item.get("albummid") or (item.get("album") or {}).get("mid") if isinstance(item.get("album"), dict) else item.get("albummid") or ""
        songs.append({
            "index": idx + 1,
            "songmid": item.get("songmid") or item.get("mid") or "",
            "songid": item.get("songid") or item.get("id") or 0,
            "media_mid": item.get("media_mid") or item.get("songmid") or "",
            "songName": item.get("songname") or item.get("title") or item.get("name") or "",
            "singerName": singer,
            "albumName": item.get("albumname") or (item.get("album") or {}).get("name") or "",
            "albummid": albummid,
            "cover": cover_url(albummid) if albummid else "",
            "payplay": (item.get("pay") or {}).get("pay_play") if isinstance(item.get("pay"), dict) else item.get("payplay"),
        })
    songs = [s for s in songs if s.get("songmid")]
    creator_obj = d.get("creator") if isinstance(d.get("creator"), dict) else {}
    creator = (creator_obj.get("nick") or creator_obj.get("nickname") or "") or d.get("nickname") or ""
    return {
        "dissname": d.get("dissname") or d.get("title") or d.get("name") or "",
        "songCount": d.get("song_count") or d.get("songCount") or len(songs),
        "listenNum": d.get("listennum") or d.get("listenNum") or 0,
        "disstid": disstid,
        "creator": creator,
        "songlist": songs,
        "raw": d,
    }


# ──────────── 评论 ────────────


async def comment(songid, *, page_no: int = 1, page_size: int = 20, biztype: int = 1, user_key: str = "") -> Any:
    body = await request("/comment", {"id": songid, "pageNo": page_no, "pageSize": page_size, "biztype": biztype}, "get", user_key)
    return (body or {}).get("data") or body


# ──────────── 用户歌单 ────────────


async def user_songlists(qq_id: str, user_key: str = "") -> list:
    body = await request("/user/songlist", {"id": qq_id}, "get", user_key)
    return (((body or {}).get("data") or {}).get("list")) or []


async def user_collect_songlists(qq_id: str, *, page_no: int = 1, page_size: int = 20, user_key: str = "") -> Any:
    body = await request("/user/collect/songlist", {"id": qq_id, "pageNo": page_no, "pageSize": page_size}, "get", user_key)
    return (body or {}).get("data") or {"list": []}


async def user_detail(qq_id: str, user_key: str = "") -> Any:
    body = await request("/user/detail", {"id": qq_id}, "get", user_key)
    return (body or {}).get("data") or body


async def user_cookie(user_key: str = "") -> Any:
    body = await request("/user/cookie", {}, "get", user_key)
    return (body or {}).get("data") or body


# ──────────── CGI 代理 ────────────


async def cgi_proxy(module: str, method: str, param: dict | None = None, user_key: str = "") -> Any:
    body = await request("/cgi", {"module": module, "method": method, "param": json.dumps(param or {})}, "get", user_key)
    return (body or {}).get("data") or body


# ──────────── 链接/卡片解析 ────────────


def parse_qqmusic_ids(text: str = "") -> dict:
    s = str(text or "")
    out = {"songmid": "", "songid": "", "albummid": "", "media_mid": ""}

    m = re.search(r"[?&]songmid=([A-Za-z0-9]+)", s, re.I) or re.search(r"/songDetail/([A-Za-z0-9]+)", s, re.I) or re.search(r"/song/([A-Za-z0-9]{14})", s, re.I)
    if m:
        out["songmid"] = m.group(1)
    m = re.search(r"[?&]songid=(\d+)", s, re.I) or re.search(r"[?&]id=(\d{5,})", s, re.I)
    if m:
        out["songid"] = m.group(1)
    m = re.search(r"[?&]albummid=([A-Za-z0-9]+)", s, re.I)
    if m:
        out["albummid"] = m.group(1)
    m = re.search(r"[?&]media_mid=([A-Za-z0-9]+)", s, re.I)
    if m:
        out["media_mid"] = m.group(1)
    return out


def parse_qqmusic_extended_ids(text: str = "") -> dict:
    s = str(text or "")
    out = parse_qqmusic_ids(s)

    m = re.search(r"/album/([A-Za-z0-9]+)", s, re.I) or re.search(r"[?&]albummid=([A-Za-z0-9]+)", s, re.I)
    if m and not out["albummid"]:
        out["albummid"] = m.group(1)

    m = re.search(r"/playlist/(\d+)", s, re.I) or re.search(r"disstid[=:](\d+)", s, re.I)
    if m:
        out["disstid"] = m.group(1)

    m = re.search(r"/singer/([A-Za-z0-9]+)", s, re.I) or re.search(r"[?&]singermid=([A-Za-z0-9]+)", s, re.I)
    if m:
        out["singermid"] = m.group(1)
    return out


def _try_parse_json_loose(text: str):

    if not text or not isinstance(text, str):
        return None
    t = text.strip()
    if not t.startswith("{"):
        return None
    try:
        return json.loads(t)
    except Exception:
        try:
            return json.loads(t.replace('\\"', '"').replace("\\\\", "\\"))
        except Exception:
            return None


def parse_qqmusic_card(msg) -> dict | None:
    text = msg if isinstance(msg, str) else ""
    if not text:
        return None
    obj = _try_parse_json_loose(text)
    if obj is None:
        return None
    if not isinstance(obj, dict):
        return None

    app = str(obj.get("app") or "")
    blob = json.dumps(obj, ensure_ascii=False)
    looks_qqmusic = (
        "structmsg" in app
        or "music.lua" in app
        or "tencent.qqmusic" in app
        or (isinstance(obj.get("meta"), dict) and (obj["meta"].get("music") is not None or obj["meta"].get("news") is not None))
        or "100497308" in blob
        or "y.qq.com" in blob
    )
    if not looks_qqmusic:
        return None

    meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
    news = meta.get("news") or {}
    music = meta.get("music") or {}
    title = news.get("title") or music.get("title") or obj.get("prompt") or ""
    desc = news.get("desc") or music.get("desc") or music.get("tag") or ""
    jump_url = news.get("jumpUrl") or music.get("jumpUrl") or music.get("musicUrl") or ""
    preview = news.get("preview") or music.get("preview") or music.get("picture") or ""
    ids = parse_qqmusic_ids(jump_url or blob)

    keyword = " ".join(x for x in [title, desc] if x)
    keyword = re.sub(r"[《》【】\[\]]", " ", keyword).strip()
    return {
        "title": title.replace("…", "").strip(),
        "desc": str(desc).strip(),
        "jumpUrl": jump_url,
        "cover": preview,
        "keyword": keyword,
        **ids,
        "raw": obj,
    }


def cover_url(albummid: str, size: int = 300) -> str:
    if not albummid:
        return ""
    return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{albummid}.jpg"


# ──────────── 辅助 ────────────


def _safe_num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def _strip_tags(s) -> str:
    if not s:
        return ""
    return re.sub(r"<[^>]+>", "", str(s))
