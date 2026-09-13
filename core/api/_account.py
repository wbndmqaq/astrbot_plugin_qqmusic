"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

import re
from typing import Any

from ._core import ApiError, request

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
# ──────────── 用户 ────────────


async def user_detail(qq_id: str, user_key: str = "") -> Any:
    body = await request("/user/detail", {"id": qq_id}, "get", user_key)
    return (body or {}).get("data") or body
async def user_cookie(user_key: str = "") -> Any:
    body = await request("/user/cookie", {}, "get", user_key)
    return (body or {}).get("data") or body
