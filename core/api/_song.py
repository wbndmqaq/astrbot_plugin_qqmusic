"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

import re
from typing import Any

import aiohttp

from ..quality import (
    QUALITY_LABEL,
    build_degrade_note,
    is_quality_size_ok,
    pick_best_available_quality,
    quality_candidates,
)
from ._core import UA, ApiError, _empty_url_result, _get_session, _safe_num, request, unwrap_data
from ._search import normalize_track_item

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
        norm = normalize_track_item(item, idx, unwrap=True)
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
def extract_track_meta(detail: dict, *, preferred: str = "") -> dict:
    """从 ``/song`` 详情响应提取统一字段视图。

    此前 ``_probe_media_info`` / ``resolve._backfill_song_meta`` / ``resolve.ids_to_song``
    三处各自实现一套「track_info → file → album」字段提取，且优先级链互有出入
    （``master_tape_media_mid`` 只有一处认识、``info`` 兜底只有一处认识），已经漂移过。
    统一到这里：取各链的并集，宁多勿缺。

    返回键（缺失一律为空值）：
        track        原始 track_info（可能为 {}）
        file         体积表 dict（无则 {}）
        media_mid    file.media_mid → file.master_tape_media_mid → track.media_mid
        songid       track.id
        albummid     track.album.mid
        album_name   track.album.name
        name         track.name → track.title
        singer       track.singer（列表 join）→ track.singername
        mv_vid       track.mv.vid
        size_info    即 ``file``（与 media_mid 同源）
        predicted    ``pick_best_available_quality(file, preferred)``（未传 preferred 则空串）
    """
    d = detail if isinstance(detail, dict) else {}
    track = d.get("track_info")
    if not isinstance(track, dict):
        track = d.get("info") if isinstance(d.get("info"), dict) else {}
    if not track:
        track = {k: v for k, v in d.items() if k not in ("file",)} if isinstance(d, dict) else {}
    file = track.get("file") if isinstance(track.get("file"), dict) else None
    if not isinstance(file, dict):
        file = d.get("file") if isinstance(d.get("file"), dict) else {}
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    singers = track.get("singer") if isinstance(track.get("singer"), list) else None
    singer = (
        " / ".join(s.get("name", "") for s in singers if isinstance(s, dict))
        if singers
        else (track.get("singername") or "")
    )
    mv = track.get("mv") if isinstance(track.get("mv"), dict) else {}
    media_mid = (
        file.get("media_mid")
        or file.get("master_tape_media_mid")
        or track.get("media_mid")
        or ""
    )
    predicted = ""
    if file:
        predicted = pick_best_available_quality(file, preferred)
    return {
        "track": track,
        "file": file,
        "media_mid": media_mid,
        "songid": track.get("id") or 0,
        "albummid": album.get("mid") or "",
        "album_name": album.get("name") or "",
        "name": track.get("name") or track.get("title") or "",
        "singer": singer,
        "mv_vid": str(mv.get("vid") or ""),
        "size_info": file or None,
        "predicted": predicted,
    }


async def _probe_media_info(
    songmid: str, media_id: str, user_key: str, preferred: str
) -> tuple[str, dict | None, str, str]:
    """预取 song_detail：真实 media_mid / 体积表 / 预测最高可用音质 / MV vid。

    与原实现一致：任何异常都退化为「无体积表」，不能影响后续取链。
    """
    real_media = media_id or songmid
    size_info: dict | None = None
    predicted = ""
    mv_vid = ""
    try:
        # 字段提取统一走 extract_track_meta（此前与 resolve 的两套实现已互相漂移）
        meta = extract_track_meta(await song_detail(songmid, user_key), preferred=preferred)
        real_media = media_id or meta["media_mid"] or songmid
        size_info = meta["size_info"]
        predicted = meta["predicted"]
        # 歌曲 MV vid（track_info.mv.vid），供「本曲 MV」操作与点歌卡 🎬 徽标
        mv_vid = meta["mv_vid"]
    except Exception:
        pass
    return real_media, size_info, predicted, mv_vid
async def _try_one_quality(
    songmid: str,
    type_: str,
    real_media: str,
    size_info: dict | None,
    probe: bool,
    user_key: str,
) -> tuple[dict | None, str, Exception | None]:
    """尝试单档音质。

    返回 ``(结果, tried 标记, 需要记录的 last_err)``：

    * 命中 → ``(r, f"{type_}:ok", None)``
    * 跳过/失败 → ``(None, 标记, err)``，``err`` 为 None 表示保留上一次的 last_err
      （``skip-size`` / ``skip-fake-file`` 与原实现一致地不改写 last_err）
    """
    if size_info and not is_quality_size_ok(type_, size_info):
        return None, f"{type_}:skip-size", None
    try:
        r = await song_url(
            songmid, type_=type_, media_id=real_media, user_key=user_key
        )
        if not r.get("url"):
            return (
                None,
                f"{type_}:no-url",
                ApiError(
                    r.get("tip") or f"{type_} 无播放链",
                    payload=r.get("raw") or r,
                    pay=r.get("pay"),
                    retcode=r.get("retcode"),
                ),
            )

        file_name = str(r.get("file") or r.get("url") or "")
        if (
            re.search(r"RS01|RS02|Q000", file_name, re.IGNORECASE)
            and size_info
            and not is_quality_size_ok(type_, size_info)
        ):
            return None, f"{type_}:skip-fake-file", None

        if probe:
            ok = await _probe_url_alive(r["url"])
            if not ok:
                return None, f"{type_}:cdn-dead", ApiError(f"{type_} CDN 不可用")

        return r, f"{type_}:ok", None
    except Exception as e:  # ApiError 也在此列，统一记录后继续下一档
        return None, f"{type_}:err", e
def _build_url_error(tried: list[str], last_err: Exception | None) -> Exception:
    """所有音质都失败时的错误对象（保留原实现的 args/pay/payload 修补行为）。"""
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
    return err
async def song_url_best(
    songmid: str,
    *,
    quality: str = "flac",
    media_id: str = "",
    fallback: bool = True,
    probe: bool = True,
    user_key: str = "",
) -> dict:
    """按候选音质阶梯逐档取链，返回第一档可用结果；全失败抛最后一次错误。

    降级顺序与 ``fallback`` 语义完全由 ``quality_candidates`` 决定（未改变）。
    """
    preferred = (quality or "auto").lower()
    candidate_list = quality_candidates(preferred, fallback)
    real_media, size_info, predicted, mv_vid = await _probe_media_info(
        songmid, media_id, user_key, preferred
    )

    last_err: ApiError | Exception | None = None
    tried: list[str] = []

    for type_ in candidate_list:
        r, tag, err = await _try_one_quality(
            songmid, type_, real_media, size_info, probe, user_key
        )
        tried.append(tag)
        if err is not None:
            last_err = err
        if r is None:
            continue
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

    raise _build_url_error(tried, last_err)
# ──────────── 歌词/热搜 ────────────


async def lyric(songmid: str, user_key: str = "") -> Any:
    body = await request("/lyric", {"songmid": songmid}, "get", user_key)
    return (body or {}).get("data") or body
async def hot_keys(user_key: str = "") -> list:
    body = await request("/search/hot", {}, "get", user_key)
    return (body or {}).get("data") or []
