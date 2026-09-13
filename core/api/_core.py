"""astrbot_plugin_qqmusic API —— （自 core/api.py 拆分；公开符号经本包 __init__ 再导出）。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 管理接口请求超时（秒）——短连接元数据接口，20s 足够
_API_TIMEOUT_SEC = 20
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
#
# 共享范围：进程级（同一进程内所有 MusicService 实例共用一条连接池）。这是刻意的
# ——会话只承载无状态 HTTP 请求，单实例加载下与实例级等价；若同进程两个实例并存，
# 任一实例 terminate() 会关掉整条会话，另一实例的下一次请求会经 _get_session()
# 重新懒加载（``_session.closed`` 判空），故最坏只是重建连接，无功能危害。
_session: aiohttp.ClientSession | None = None
def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session
def get_session() -> aiohttp.ClientSession:
    """复用会话的公开入口（音频/MV 下载等模块外调用，避免引用私有名）。"""
    return _get_session()
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

    timeout = aiohttp.ClientTimeout(total=_API_TIMEOUT_SEC)
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
    except (TimeoutError, asyncio.TimeoutError) as e:
        # ClientTimeout(total=...) 到期抛的是 asyncio.TimeoutError（3.11 起即内建 TimeoutError），
        # 不是 aiohttp.ClientError，只写 ClientError 会让总超时穿透到 handler：except ApiError
        # 接不住 → 用户拿不到错误回复，且 event.stop_event() 不执行。
        raise ApiError(f"请求超时：{base}") from e
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
