from __future__ import annotations

import json
import re
from typing import Any

import aiohttp

from .quality import (
    QUALITY_LABEL,
    build_degrade_note,
    is_quality_size_ok,
    pick_best_available_quality,
    quality_candidates,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 模块级配置访问器，由 main.py 在插件加载时注入
_cfg_getter = None


def normalize_api_base(raw: str | None) -> str:
    """归一化 apiBase：兼容用户手改配置文件时的常见写法问题，
    不再因格式问题抛裸的 Invalid URL：
     - 漏写 http:// / https:// 协议头（如 127.0.0.1:3300）→ 自动补 http://
     - 中文输入法的全角冒号（http：//）→ 转半角
     - 复制粘贴带入的首尾引号、空格、零宽字符 / BOM → 清除
    """
    s = str(raw or "")
    s = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", s)
    s = re.sub(r"^[\"'`]+|[\"'`]+$", "", s)
    s = s.replace("：", ":").strip()
    if not s:
        return ""
    if re.match(r"^https?://", s, re.IGNORECASE):
        return s.rstrip("/")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.IGNORECASE):
        s = f"http://{s}"
    return s.rstrip("/")


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
    def __init__(
        self, message: str, *, code=None, payload=None, pay=None, retcode=None, tip=None
    ):
        super().__init__(message)
        self.code = code
        self.payload = payload
        self.pay = pay
        self.retcode = retcode
        self.tip = tip


def _get_base() -> str:
    base = normalize_api_base(str(_cfg().get("apiBase") or ""))
    return base


# ──────────── 复用 HTTP 会话 ────────────

# 模块级 aiohttp.ClientSession 复用：避免每请求新建会话（反复 TCP 握手 + DNS 解析）。
# 会话由 service.terminate() 调 close_session() 统一关闭；懒加载，未使用时不会创建。
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    """关闭并释放复用的 HTTP 会话（插件卸载/重载时由 service 调用）。"""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _get_token() -> str:
    c = _cfg()
    token = c.get("apiToken") or ""
    if not token:
        # 与 JS 版一致：支持环境变量 QQMUSIC_API_TOKEN（对应 API 端 .env 的 QQMUSIC_API_TOKEN）
        import os

        token = os.environ.get("QQMUSIC_API_TOKEN") or ""
    return str(token).strip()


def _sanitize_for_header(value: str) -> str:

    return re.sub(
        r"[\r\n\t]", "", re.sub(r"[^\x20-\x7E]", "", str(value or ""))
    ).strip()


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


async def request(
    pathname: str, params: dict | None = None, method: str = "get", user_key: str = ""
) -> Any:

    params = dict(params or {})
    base = _get_base()
    if not base:
        raise ApiError("API 地址未配置：请发送 #qqm api <地址>，或在插件设置面板填写 apiBase")
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
        sess = _get_session()
        if method == "get":
            # GET 参数走 query：规整 bool/None，避免 aiohttp 严格类型校验抛错
            async with sess.get(
                url, params=_query_safe_params(params), headers=headers or None, timeout=timeout
            ) as res:
                return await _handle_response(res)
        else:
            async with sess.post(url, json=params, headers=headers or None, timeout=timeout) as res:
                return await _handle_response(res)
    except aiohttp.ClientConnectorError as e:
        raise ApiError(f"无法连接 QQ 音乐 API（{base}），请先申请API") from e
    except (aiohttp.InvalidURL, ValueError) as e:
        raise ApiError("API 地址无效，请检查配置里的 apiBase（需形如 http://IP:端口）") from e
    except aiohttp.ServerTimeoutError as e:
        raise ApiError(f"请求超时：{base}") from e
    except aiohttp.ClientError as e:
        raise ApiError(f"网络错误：{e}") from e


async def _handle_response(res: aiohttp.ClientResponse):
    status = res.status
    if status == 401:
        raise ApiError(
            "API 鉴权失败（401）：请检查插件 apiToken 与 API 的 QQMUSIC_API_TOKEN 是否一致"
        )
    if status == 403:
        raise ApiError("API 拒绝访问（403）：IP 可能不在白名单")
    if status == 429:
        raise ApiError("API 限流（429）：请求过于频繁")
    if status >= 400:
        raise ApiError(f"HTTP {status}")
    try:
        data = await res.json(content_type=None)
    except Exception as e:
        text = await res.text()
        raise ApiError(f"返回非 JSON：{text[:200]}") from e

    result = data.get("result") if isinstance(data, dict) else None
    if result not in (None, 100, 0):
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


# ──────────── 通用取值辅助（统一各处的剥壳/回退写法） ────────────


def unwrap_data(body) -> dict:


    return (body or {}).get("data") or body or {}


def payplay_of(item: dict) -> Any:


    pay = item.get("pay") if isinstance(item.get("pay"), dict) else {}
    for k in ("payplay", "pay_play"):
        if pay.get(k) is not None:
            return pay[k]
    return item.get("payplay")


def singer_text(item: dict) -> str:


    arr = item.get("singer")
    if isinstance(arr, list) and arr:
        names = [
            str(s.get("name") or s.get("title") or "")
            for s in arr
            if isinstance(s, dict) and (s.get("name") or s.get("title"))
        ]
        if names:
            return " / ".join(names)
    return str(
        item.get("singername")
        or item.get("singerName")
        or item.get("singer_name")
        or item.get("singer")
        or ""
    )


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

    singer = singer_text(raw)

    album = raw.get("album") if isinstance(raw.get("album"), dict) else {}
    albummid = raw.get("albummid") or album.get("mid") or ""
    cover = (
        cover_url(albummid)
        if albummid
        else album.get("pic") or album.get("cover") or ""
    )
    interval = _safe_num(raw.get("interval") or raw.get("songTime") or 0)
    interval = int(interval)
    duration = f"{interval // 60:02d}:{interval % 60:02d}" if interval > 0 else ""

    payplay = payplay_of(raw)
    return {
        "index": idx + 1,
        "songmid": raw.get("songmid") or raw.get("mid") or "",
        "songid": raw.get("songid") or raw.get("id") or 0,
        "media_mid": raw.get("media_mid")
        or raw.get("strMediaMid")
        or raw.get("songmid")
        or "",
        "songName": raw.get("songname")
        or _strip_tags(raw.get("songname_hilight"))
        or raw.get("name")
        or raw.get("title")
        or "",
        "singerName": singer,
        "albumName": raw.get("albumname") or album.get("name") or "",
        "albummid": albummid,
        "cover": cover,
        "duration": duration,
        "interval": interval,
        "payplay": payplay,
        "msgid": raw.get("msgid"),
        "raw": item,
    }


# ──────────── 歌曲详情/播放链 ────────────


async def song_detail(songmid: str, user_key: str = "") -> Any:
    body = await request("/song", {"songmid": songmid}, "get", user_key)
    return (body or {}).get("data")


async def song_info_batch(ids: list, *, user_key: str = "") -> list:
    """批量查歌曲详情（/song/info，一次请求），返回带 mvVid 的规范化歌曲（供列表卡 🎬 徽标）。"""
    ids = [str(i) for i in (ids or []) if str(i)]
    if not ids:
        return []
    body = await request("/song/info", {"ids": ",".join(ids)}, "get", user_key)
    raw_list = unwrap_data(body).get("list") or []
    out = []
    for idx, item in enumerate(raw_list):
        norm = _normalize_search_item(item, idx)
        if norm:
            track = item.get("track_info") if isinstance(item, dict) else None
            mv_vid = ""
            if isinstance(track, dict) and isinstance(track.get("mv"), dict):
                mv_vid = str(track["mv"].get("vid") or "")
            norm["mvVid"] = mv_vid
            out.append(norm)
    return out


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
            "file": body.get("file")
            or (data.get("file") if isinstance(data, dict) else None),
            "domain": body.get("domain")
            or (data.get("domain") if isinstance(data, dict) else None),
            "purl": body.get("purl")
            or (data.get("purl") if isinstance(data, dict) else None),
            "quality": type_,
            "mediaId": body.get("mediaId") or real_media,
            "pay": body.get("pay")
            or (data.get("pay") if isinstance(data, dict) else None),
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


async def song_url(
    songmid: str,
    *,
    type_: str = "128",
    media_id: str = "",
    channel: str = "auto",
    user_key: str = "",
) -> dict:
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
    headers = {
        "User-Agent": UA,
        "Referer": "https://y.qq.com/",
        "Origin": "https://y.qq.com/",
    }
    # 两阶段：先 HEAD，失败再 Range GET 嗅探首字节
    try:
        sess = _get_session()
        t = aiohttp.ClientTimeout(total=timeout)
        try:
            async with sess.head(
                url, headers=headers, allow_redirects=True, timeout=t
            ) as head:
                if 0 < head.status < 400:
                    return True
                if head.status in (401, 403, 404):
                    return False
        except Exception:
            pass
        headers["Range"] = "bytes=0-1023"
        async with sess.get(url, headers=headers, allow_redirects=True, timeout=t) as g:
            if g.status >= 400:
                return False
            buf = await g.content.read(1024)
            if len(buf) < 16:
                return False
            head_str = buf[:32].decode("utf-8", errors="ignore").lower()
            return not ("<html" in head_str or "<!doctype" in head_str)
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

    preferred = (quality or "auto").lower()
    candidate_list = quality_candidates(preferred, fallback)
    real_media = media_id or songmid
    size_info: dict | None = None
    predicted = ""

    try:
        detail = await song_detail(songmid, user_key)
        file = (detail or {}).get("track_info") or {}
        track = file if isinstance(file, dict) else {}
        file = file.get("file") if isinstance(file, dict) else None
        if not isinstance(file, dict):
            file = (detail or {}).get("file") if isinstance(detail, dict) else None
        if isinstance(file, dict):
            real_media = (
                media_id
                or file.get("media_mid")
                or file.get("master_tape_media_mid")
                or songmid
            )
            size_info = file
            predicted = pick_best_available_quality(file, preferred)
    except Exception:
        track = {}

    # 歌曲 MV vid（track_info.mv.vid），供「本曲 MV」操作与点歌卡 🎬 徽标
    mv_vid = ""
    try:
        if isinstance(track, dict) and isinstance(track.get("mv"), dict):
            mv_vid = str(track["mv"].get("vid") or "")
    except Exception:
        mv_vid = ""

    last_err: ApiError | Exception | None = None
    tried: list[str] = []

    for type_ in candidate_list:
        if size_info and not is_quality_size_ok(type_, size_info):
            tried.append(f"{type_}:skip-size")
            continue
        try:
            r = await song_url(
                songmid, type_=type_, media_id=real_media, user_key=user_key
            )
            if not r.get("url"):
                tried.append(f"{type_}:no-url")
                last_err = ApiError(
                    r.get("tip") or f"{type_} 无播放链",
                    payload=r.get("raw") or r,
                    pay=r.get("pay"),
                    retcode=r.get("retcode"),
                )
                continue

            file_name = str(r.get("file") or r.get("url") or "")
            if (
                re.search(r"RS01|RS02|Q000", file_name, re.IGNORECASE)
                and size_info
                and not is_quality_size_ok(type_, size_info)
            ):
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
                mvVid=mv_vid,
                degradeNote=build_degrade_note(preferred, type_, tried),
            )
            return r
        except Exception as e:  # ApiError 也在此列，统一记录后继续下一档
            last_err = e
            tried.append(f"{type_}:err")

    hint = f" 已尝试: {', '.join(tried)}" if tried else ""
    payload = (getattr(last_err, "payload", None) if last_err else {}) or {}
    pay = payload.get("pay") if isinstance(payload, dict) else None
    if last_err is not None and getattr(last_err, "pay", None):
        pay = last_err.pay
    pay_hint = (
        " 该曲需会员播放，请 #qqm登录"
        if pay and _safe_num((pay or {}).get("pay_play")) == 1
        else ""
    )
    detail_msg = ""
    if isinstance(payload, dict):
        detail_msg = payload.get("errMsg") or payload.get("tip") or ""
    if not detail_msg and last_err:
        detail_msg = str(last_err)
    msg = (
        f"{detail_msg}{pay_hint}{hint}"
        if detail_msg
        else f"所有音质均无可用链接（可 #qqm登录 重新扫码）{pay_hint}{hint}"
    )
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
        for n in (_normalize_search_item(it, i) for i, it in enumerate(raw_list))
        if n
    ]


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
    return [_normalize_radio_track(t, idx) for idx, t in enumerate(tracks) if t]


def _normalize_radio_track(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    singer = singer_text(item)
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    albummid = item.get("albummid") or album.get("mid") or ""
    interval = int(_safe_num(item.get("interval") or 0))
    duration = f"{interval // 60:02d}:{interval % 60:02d}" if interval > 0 else ""
    f = item.get("file") if isinstance(item.get("file"), dict) else {}
    payplay = payplay_of(item)
    return {
        "index": idx + 1,
        "songmid": item.get("mid") or item.get("songmid") or "",
        "songid": item.get("id") or item.get("songid") or 0,
        "media_mid": (f.get("media_mid") if f else "")
        or item.get("media_mid")
        or item.get("mid")
        or "",
        "songName": item.get("name") or item.get("title") or item.get("songname") or "",
        "singerName": singer,
        "albumName": album.get("name") or item.get("albumname") or "",
        "albummid": albummid,
        "cover": cover_url(albummid) if albummid else album.get("cover") or "",
        "duration": duration,
        "interval": interval,
        "payplay": payplay,
        "raw": item,
    }


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
        n for n in (_normalize_search_item(it, i) for i, it in enumerate(songlist)) if n
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
        n for n in (_normalize_search_item(it, i) for i, it in enumerate(lst)) if n
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
        norm = _normalize_search_item(raw, idx)
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
    out = [n for n in (_normalize_search_item(it, i) for i, it in enumerate(lst)) if n]
    return {"list": out, "total": d.get("total") or 0, "albummid": albummid}


# ──────────── 歌单 ────────────


async def songlist_detail(disstid: str, user_key: str = "") -> dict:
    body = await request("/songlist", {"id": disstid}, "get", user_key)
    d = unwrap_data(body)
    raw = d.get("songlist") or d.get("songs") or d.get("list") or []
    songs = []
    for idx, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        singer = singer_text(item)
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        albummid = item.get("albummid") or album.get("mid") or ""
        payplay = payplay_of(item)
        songs.append(
            {
                "index": idx + 1,
                "songmid": item.get("songmid") or item.get("mid") or "",
                "songid": item.get("songid") or item.get("id") or 0,
                "media_mid": item.get("media_mid") or item.get("songmid") or "",
                "songName": item.get("songname")
                or item.get("title")
                or item.get("name")
                or "",
                "singerName": singer,
                "albumName": item.get("albumname") or album.get("name") or "",
                "albummid": albummid,
                "cover": cover_url(albummid) if albummid else "",
                "payplay": payplay,
            }
        )
    songs = [s for s in songs if s.get("songmid")]
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


# ──────────── 评论 ────────────


async def comment(
    songid,
    *,
    page_no: int = 1,
    page_size: int = 20,
    biztype: int = 1,
    user_key: str = "",
) -> Any:
    body = await request(
        "/comment",
        {"id": songid, "pageNo": page_no, "pageSize": page_size, "biztype": biztype},
        "get",
        user_key,
    )
    return (body or {}).get("data") or body


# ──────────── 用户 ────────────


async def user_detail(qq_id: str, user_key: str = "") -> Any:
    body = await request("/user/detail", {"id": qq_id}, "get", user_key)
    return (body or {}).get("data") or body


async def user_cookie(user_key: str = "") -> Any:
    body = await request("/user/cookie", {}, "get", user_key)
    return (body or {}).get("data") or body


# ──────────── 链接/卡片解析 ────────────


def parse_qqmusic_ids(text: str = "") -> dict:
    s = str(text or "")
    out = {"songmid": "", "songid": "", "albummid": "", "media_mid": ""}

    # songmid / song_mid / songMid / mid 参数变体，或 /songDetail|/song|/playsong.html 路径
    m = (
        re.search(r"[?&](?:songmid|song_mid|songMid|mid)=([A-Za-z0-9]{5,})", s, re.IGNORECASE)
        or re.search(
            r"/(?:songDetail|song|playsong\.html)\/?[?#]*([A-Za-z0-9]{10,})",
            s,
            re.IGNORECASE,
        )
        or re.search(r"/song/([A-Za-z0-9]{14})", s, re.IGNORECASE)
    )
    if m:
        out["songmid"] = m.group(1)
    m = re.search(r"[?&]songid=(\d+)", s, re.IGNORECASE) or re.search(
        r"[?&]id=(\d{5,})", s, re.IGNORECASE
    )
    if m:
        out["songid"] = m.group(1)
    m = re.search(r"[?&]albummid=([A-Za-z0-9]+)", s, re.IGNORECASE)
    if m:
        out["albummid"] = m.group(1)
    # media_mid / mediaMid / mediaid 参数变体
    m = re.search(r"[?&](?:media_mid|mediaMid|mediaid)=([A-Za-z0-9]+)", s, re.IGNORECASE)
    if m:
        out["media_mid"] = m.group(1)
    return out


def parse_qqmusic_extended_ids(text: str = "") -> dict:
    s = str(text or "")
    out = parse_qqmusic_ids(s)

    m = re.search(r"/album/([A-Za-z0-9]+)", s, re.IGNORECASE) or re.search(
        r"[?&]albummid=([A-Za-z0-9]+)", s, re.IGNORECASE
    )
    if m and not out["albummid"]:
        out["albummid"] = m.group(1)

    m = re.search(r"/playlist/(\d+)", s, re.IGNORECASE) or re.search(
        r"disstid[=:](\d+)", s, re.IGNORECASE
    )
    if m:
        out["disstid"] = m.group(1)

    m = re.search(r"/singer/([A-Za-z0-9]+)", s, re.IGNORECASE) or re.search(
        r"[?&]singermid=([A-Za-z0-9]+)", s, re.IGNORECASE
    )
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
        or (
            isinstance(obj.get("meta"), dict)
            and (
                obj["meta"].get("music") is not None
                or obj["meta"].get("news") is not None
            )
        )
        or "100497308" in blob
        or "y.qq.com" in blob
    )
    if not looks_qqmusic:
        return None

    # 防御：畸形/第三方分享卡片里 meta 下可能是非 dict（如字符串），逐个 isinstance 兜底
    meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
    news = meta.get("news") if isinstance(meta.get("news"), dict) else {}
    music = meta.get("music") if isinstance(meta.get("music"), dict) else {}
    title = str(news.get("title") or music.get("title") or obj.get("prompt") or "")
    desc = str(news.get("desc") or music.get("desc") or music.get("tag") or "")
    jump_url = str(
        news.get("jumpUrl") or music.get("jumpUrl") or music.get("musicUrl") or ""
    )
    preview = str(
        news.get("preview") or music.get("preview") or music.get("picture") or ""
    )
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
