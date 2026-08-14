from __future__ import annotations

import math
from typing import Any

QQMUSIC_QUALITY_LIST = [
    {"label": "自动（自适配最高可用）", "value": "auto"},
    {"label": "标准 128K", "value": "128"},
    {"label": "较高 M4A", "value": "m4a"},
    {"label": "极高 320K", "value": "320"},
    {"label": "无损 FLAC", "value": "flac"},
    {"label": "无损 APE", "value": "ape"},
    {"label": "Hi-Res", "value": "hires"},
    {"label": "臻品全景声", "value": "atmos"},
    {"label": "臻品母带", "value": "master"},
    {"label": "臻品母带2.0", "value": "atmos_master"},
]

# 从高到低完整阶梯
QUALITY_LADDER = [
    "atmos_master",
    "master",
    "atmos",
    "hires",
    "flac",
    "ape",
    "320",
    "m4a",
    "128",
]

QUALITY_LABEL = {"auto": "自动适配"}
QUALITY_LABEL.update(
    {
        item["value"]: item["label"]
        for item in QQMUSIC_QUALITY_LIST
        if item["value"] != "auto"
    }
)


def _num(v: Any) -> float:

    if v is None or isinstance(v, (list, dict)):
        return 0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return f if math.isfinite(f) else 0  # NaN/±inf guard


def size_new_at(file: dict, idx: int = 0) -> float:
    arr = file.get("size_new") if isinstance(file, dict) else None
    if not isinstance(arr, list):
        return 0
    if idx < 0 or idx >= len(arr):
        return 0
    v = _num(arr[idx])
    return max(0, v)


def quality_candidates(preferred: str = "flac", fallback: bool = True) -> list[str]:
    q = (preferred or "flac").lower()
    if q in ("auto", "adaptive", "best"):
        return list(QUALITY_LADDER) if fallback else ["flac", "320", "128"]
    idx = (
        QUALITY_LADDER.index(q) if q in QUALITY_LADDER else QUALITY_LADDER.index("flac")
    )
    if not fallback:
        return [QUALITY_LADDER[idx]]
    return QUALITY_LADDER[idx:]


def is_quality_size_ok(type_: str, file: dict | None = None) -> bool:
    if not isinstance(file, dict):
        return True
    t = (type_ or "").lower()
    n = lambda k: _num(file.get(k))

    if t == "128":
        return n("size_128mp3") > 0 or n("size_96aac") > 0 or n("size_48aac") > 0
    if t == "m4a":
        return n("size_48aac") > 0 or n("size_96aac") > 0 or n("size_192aac") > 0
    if t == "320":
        return n("size_320mp3") > 0 or size_new_at(file, 3) > 0
    if t == "flac":
        return n("size_flac") > 0 or size_new_at(file, 1) > 0
    if t == "ape":
        return n("size_ape") > 0
    if t == "hires":
        return n("size_hires") > 0 or size_new_at(file, 2) > 0
    if t in ("atmos", "dolby"):
        return n("size_dolby") > 0 or size_new_at(file, 10) > 0
    if t in ("master", "atmos_master"):
        return n("size_master") > 0 or size_new_at(file, 0) > 0
    return True


def pick_best_available_quality(file: dict, preferred: str = "auto") -> str:
    for type_ in quality_candidates(preferred, True):
        if is_quality_size_ok(type_, file):
            return type_
    return "128"


def build_degrade_note(preferred: str, achieved: str, tried: list[str]) -> str:
    """生成音质降级说明：显式请求的高音质没拿到时，向用户解释原因（多为需绿钻会员）。

    - 未降级（auto / 相同 / 更高）不提示
    - 根据尝试记录定位具体原因：skip-size（歌曲未出该档）/ cdn-dead（链接不可用）/
      no-url（未返回链接）
    """
    req = str(preferred or "")
    if not req or req == "auto" or not achieved or req == achieved:
        return ""
    hi = QUALITY_LADDER.index(req) if req in QUALITY_LADDER else -1
    lo = QUALITY_LADDER.index(achieved) if achieved in QUALITY_LADDER else -1
    if hi < 0 or lo < 0 or lo <= hi:
        return ""
    entry = ""
    for t in tried or []:
        if str(t).startswith(f"{req}:"):
            entry = str(t)
            break
    reason = "获取失败（该音质通常需绿钻会员）"
    if entry.endswith(":skip-size"):
        reason = "该歌曲未提供此音质"
    elif entry.endswith(":cdn-dead"):
        reason = "播放链接不可用"
    elif entry.endswith(":no-url"):
        reason = "API 未返回链接（通常需绿钻会员）"
    return (
        f"已请求{QUALITY_LABEL.get(req, req)}，"
        f"实际{QUALITY_LABEL.get(achieved, achieved)}（{reason}）"
    )
