"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

import json
import re

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
