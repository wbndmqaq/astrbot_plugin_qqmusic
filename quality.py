from __future__ import annotations

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
QUALITY_LABEL.update({item["value"]: item["label"] for item in QQMUSIC_QUALITY_LIST if item["value"] != "auto"})

# size_new 下标 → 逻辑音质（仅映射有把握的）
SIZE_NEW_INDEX = {0: "master", 2: "hires", 10: "atmos"}


def _num(v: Any) -> float:

    if v is None or isinstance(v, (list, dict)):
        return 0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return f if f == f and f != float("inf") else 0  # NaN/inf guard


def size_new_at(file: dict, idx: int = 0) -> float:
    arr = file.get("size_new") if isinstance(file, dict) else None
    if not isinstance(arr, list):
        return 0
    if idx < 0 or idx >= len(arr):
        return 0
    v = _num(arr[idx])
    return v if v > 0 else 0


def size_new_for_quality(file: dict, type_: str) -> float:
    t = (type_ or "").lower()
    if t in ("master", "atmos_master"):
        return size_new_at(file, 0)
    if t == "hires":
        return size_new_at(file, 2)
    if t in ("atmos", "dolby"):
        return size_new_at(file, 10)
    return 0


def quality_candidates(preferred: str = "flac", fallback: bool = True) -> list[str]:
    q = (preferred or "flac").lower()
    if q in ("auto", "adaptive", "best"):
        return list(QUALITY_LADDER) if fallback else ["flac", "320", "128"]
    idx = QUALITY_LADDER.index(q) if q in QUALITY_LADDER else QUALITY_LADDER.index("flac")
    if not fallback:
        return [QUALITY_LADDER[idx] or "128"]
    return QUALITY_LADDER[idx:]


def is_quality_size_ok(type_: str, file: dict | None = None) -> bool:
    if not isinstance(file, dict):
        return True
    t = (type_ or "").lower()
    n = lambda k: _num(file.get(k))  # noqa: E731

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


def summarize_file_sizes(file: dict | None) -> dict:
    if not isinstance(file, dict):
        return {}
    n = lambda k: _num(file.get(k))  # noqa: E731
    arr = file.get("size_new")
    arr = [_num(x) for x in arr] if isinstance(arr, list) else []
    return {
        "flac": n("size_flac"),
        "hires": n("size_hires"),
        "dolby": n("size_dolby"),
        "s320": n("size_320mp3"),
        "s128": n("size_128mp3"),
        "new0": arr[0] if len(arr) > 0 else 0,
        "new2": arr[2] if len(arr) > 2 else 0,
        "new10": arr[10] if len(arr) > 10 else 0,
    }


# 兼容别名
is_quality_available = is_quality_size_ok
