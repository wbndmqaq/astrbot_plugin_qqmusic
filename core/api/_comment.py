"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

from typing import Any

from ._core import request

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
