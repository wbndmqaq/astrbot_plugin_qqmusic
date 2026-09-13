"""状态卡 / 设置卡的数据构造（纯装配逻辑，不投递、不渲染）。

从 ``MusicService`` 整块搬出：这些方法只依赖配置、API 客户端与文案工具，
与「服务编排」职责无关。``MusicService`` 上保留同名薄委托，调用方无需改动。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from . import api as qqapi
from . import cards as cardlib
from . import messages as msg
from .quality import QUALITY_LABEL

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)

_LOGO_DATA_URI: str | None = None


async def _fallback_avatar_data_uri() -> str:
    """未登录时的头像：内联 logo 的 data URI。

    不能把本地文件路径直接塞进 ``<img src>``：卡片页由 ``page.set_content()``
    加载，文档基址是 ``about:blank``，绝对路径与 ``file://`` 都取不到图片，
    未登录用户会在状态卡上看到一个破图占位符。内联成 data URI 才能真正显示。

    IO 说明：读盘 + base64 编码仅在首次未命中缓存时发生，且整体丢线程池执行
    （虽然是几十 KB 的小文件，但它是纯阻塞 IO，没必要占用事件循环）；
    之后命中模块级 ``_LOGO_DATA_URI`` 直接返回。
    """
    global _LOGO_DATA_URI
    if _LOGO_DATA_URI is None:
        try:
            import base64

            path = os.path.join(PLUGIN_DIR, "resources", "img", "logo-64.png")

            def _read_logo() -> str:
                with open(path, "rb") as f:
                    return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")

            _LOGO_DATA_URI = await asyncio.to_thread(_read_logo)
        except Exception:
            _LOGO_DATA_URI = ""
    return _LOGO_DATA_URI


async def _fetch_login_snapshot(user_key: str) -> tuple[dict, dict, bool, str]:
    """登录状态 + cookie 快照；API 失败时 api_ok=False（状态卡照常渲染，不抛出）。"""
    status: dict = {}
    cookie: dict = {}
    api_ok = True
    api_error = ""
    try:
        st = await qqapi.request("/login/status", {}, "get", user_key)
        status = (st or {}).get("data") or {}
    except Exception as e:
        api_ok = False
        api_error = str(e)
        status = {"login": False}
    try:
        c = await qqapi.user_cookie(user_key)
        cookie = (
            (c or {}).get("server")
            if isinstance((c or {}).get("server"), dict)
            else (c or {}).get("data") or {}
        )
        if not isinstance(cookie, dict):
            cookie = {}
    except Exception:
        cookie = {}
    return status, cookie, api_ok, api_error


async def _fetch_profile(
    service, uin: str, user_key: str, *, logged_in: bool
) -> tuple[str, str]:
    """昵称 / 头像：登录时查 user_detail，失败回退 QQ 头像接口与脱敏占位。"""
    nickname = "未登录"
    avatar_url = ""
    if not (logged_in and uin):
        return nickname, await _fallback_avatar_data_uri()
    try:
        body = await qqapi.user_detail(uin, user_key)
        d = qqapi.unwrap_data(body)
        creator = d.get("creator") or d.get("base") or d
        nickname = (
            (
                creator.get("nick")
                or creator.get("nickname")
                or creator.get("name")
                or d.get("nickname")
                or d.get("nick")
                or ""
            )
            if isinstance(creator, dict)
            else ""
        )
        avatar_url = (
            (
                creator.get("headurl")
                or creator.get("avatarUrl")
                or creator.get("avatar")
                or d.get("headurl")
                or d.get("avatarUrl")
                or ""
            )
            if isinstance(creator, dict)
            else ""
        )
    except Exception:
        pass
    if not avatar_url and re.match(r"^\d{5,}$", str(uin)):
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"
    if not nickname:
        nickname = f"用户 {mask_uin(uin)}"
    return nickname, avatar_url


def _vip_texts(
    *, api_ok: bool, logged_in: bool, status: dict, cookie: dict, api_error: str
) -> tuple[str, str, str]:
    """状态卡会员区文案：API 异常 → 已绑定 → 普通。"""
    if not api_ok:
        return ("API 异常", "无法连接", api_error or "请检查 apiBase 与 API 是否启动")
    if logged_in:
        state = "Key 可刷新" if status.get("hasRefresh") else "已登录"
        expire = (
            f"Key 相关时效字段: {cookie.get('keyExpiresIn')}"
            if cookie.get("keyExpiresIn")
            else "建议定期 #qqm登录 保持高音质可用"
        )
        return ("已绑定账号", state, expire)
    return ("普通用户", "未开通 / 未检测", "登录后可解锁付费曲与更高音质解析")


def _subtitle_text(
    *, api_ok: bool, logged_in: bool, status: dict
) -> str:
    """状态卡副标题：按 API / 绑定 / 登录三种状态给行动指引。"""
    bound = bool(status.get("bound"))
    user_key_slot = status.get("userKey") or ""
    if not api_ok:
        return "API 连接失败"
    if logged_in and bound:
        return f"Token 绑定槽 {user_key_slot or 'default'} · 可扫码更新"
    if logged_in:
        return "账号已绑定到 API"
    if bound:
        return "本 Token 尚未扫码 · 发 #qqm登录 绑定 QQ 音乐"
    return msg.SCAN_LOGIN_HINT


async def build_status_data(service, user_key: str = "") -> dict:
    cfg = service.cfg()
    status, cookie, api_ok, api_error = await _fetch_login_snapshot(user_key)

    logged_in = bool(
        status.get("login") or (status.get("uin") and status.get("hasKey"))
    )
    uin = re.sub(
        r"\D",
        "",
        str(
            status.get("uin")
            or cookie.get("uin")
            or cookie.get("qqmusic_uin")
            or ""
        ),
    )
    key = cookie.get("qm_keyst") or cookie.get("qqmusic_key") or ""
    quality = cfg.get("quality") or "auto"
    quality_label = QUALITY_LABEL.get(quality, quality)

    nickname, avatar_url = await _fetch_profile(
        service, uin, user_key, logged_in=logged_in
    )
    vip_title, vip_state_text, vip_expire_text = _vip_texts(
        api_ok=api_ok, logged_in=logged_in, status=status, cookie=cookie, api_error=api_error
    )

    avatar_is_photo = bool(
        logged_in
        and avatar_url
        and "/resources/img/logo" not in str(avatar_url)
        and not str(avatar_url).startswith("file")
    )

    subtitle = _subtitle_text(api_ok=api_ok, logged_in=logged_in, status=status)

    return {
        "title": "QQ音乐状态",
        "loggedIn": logged_in,
        "nickname": cookie.get("nick") or nickname,
        "subtitle": subtitle,
        "uin": mask_uin(uin) if logged_in else "-",
        "loginTypeText": login_type_text(cookie, status),
        "apiBase": cardlib.mask_api_base(cfg.get("apiBase", "")),
        "keyStatus": (
            f"API 保管 {mask_key(key)}"
            + (" · 可刷新" if status.get("hasRefresh") else "")
        )
        if logged_in
        else "未配置",
        "vipTitle": vip_title,
        "vipStateText": vip_state_text,
        "musicQuality": quality_label,
        "vipExpireText": vip_expire_text,
        "stats": [
            {
                "label": "点歌",
                "value": "开"
                if cfg.get("enableSongRequest") is not False
                else "关",
            },
            {
                "label": "解析",
                "value": "开" if cfg.get("enableResolve") is not False else "关",
            },
            {
                "label": "降级",
                "value": "开" if cfg.get("qualityFallback") is not False else "关",
            },
        ],
        "footer": "QQMusic Plugin · 状态卡片",
        "avatarUrl": avatar_url,
        "avatarIsPhoto": avatar_is_photo,
    }


def login_type_text(cookie: dict, status: dict) -> str:
    t = 0
    try:
        t = int(
            status.get("login_type")
            or cookie.get("login_type")
            or cookie.get("tmeLoginType")
            or 0
        )
    except Exception:
        t = 0
    if str(cookie.get("tmeLoginType")) == "1" or (t == 2 and cookie.get("wxuin")):
        return "微信登录"
    if str(cookie.get("tmeLoginType")) == "2" or t == 1:
        return "QQ 登录"
    if t == 2:
        return "微信登录"
    if cookie.get("qm_keyst") or cookie.get("qqmusic_key") or status.get("hasKey"):
        return "扫码登录"
    return "未登录"


def mask_uin(uin: str = "") -> str:
    s = str(uin or "")
    if not s or s == "0":
        return "-"
    if len(s) <= 4:
        return s
    return f"{s[:3]}***{s[-2:]}"


def mask_key(key: str = "") -> str:
    s = str(key or "")
    if not s:
        return "无"
    if len(s) < 12:
        return "已配置"
    return f"{s[:6]}…{s[-4:]}"


def platform_name(event) -> str:
    try:
        p = event.platform
        return getattr(p, "name", None) or type(p).__name__ or "unknown"
    except Exception:
        return "unknown"


async def _fetch_settings_login(service, event) -> dict:
    """设置卡登录区：必须带 user_key 查询（多账号按 userKey 存 cookie，不带会查错槽位，
    出现「#qqm状态 已登录、#qqm设置 未绑定」的自相矛盾）。"""
    try:
        st = await qqapi.request("/login/status", {}, "get", service.user_key(event))
        d = (st or {}).get("data") or {}
        if d.get("login"):
            return {
                "ok": True,
                "text": f"已绑定{(' · ' + d.get('nick', '')) if d.get('nick') else ''}",
            }
        return {"ok": False, "text": "未绑定（#qqm登录）"}
    except Exception:
        return {"ok": False, "text": "API 异常"}


def _on_off(v) -> str:
    """布尔开关的展示值：仅显式 False 视为关（配置缺失默认开）。"""
    return "关" if v is False else "开"


def _settings_tiles(c: dict) -> list:
    """设置卡开关磁贴（6 个）。"""
    on_off = _on_off  # 局部短别名，保持磁贴行紧凑
    return [
        {"label": "点歌", "value": on_off(c.get("enableSongRequest")), "on": c.get("enableSongRequest") is not False},
        {"label": "解析", "value": on_off(c.get("enableResolve")), "on": c.get("enableResolve") is not False},
        {"label": "列表卡", "value": on_off(c.get("renderListCard")), "on": c.get("renderListCard") is not False},
        {"label": "语音", "value": on_off(c.get("sendVocal")), "on": c.get("sendVocal") is not False},
        {"label": "群文件", "value": on_off(c.get("uploadFile")), "on": c.get("uploadFile") is not False},
        {"label": "降级", "value": on_off(c.get("qualityFallback")), "on": c.get("qualityFallback") is not False},
    ]


# 设置卡展示的命令分组：与帮助卡同源（core/messages.HELP_SECTIONS），
# 此前设置卡内联手抄一份、措辞已与帮助卡漂移（如「App 扫码」desc 两种写法）。
_SETTINGS_SECTIONS = ("账号状态", "主人管理")


def _settings_commands() -> list:
    """从帮助清单主数据源提取设置卡命令区（name/desc/example）。"""
    out = []
    for sec in msg.HELP_SECTIONS:
        if sec.get("title") not in _SETTINGS_SECTIONS:
            continue
        for it in sec.get("items") or []:
            out.append(
                {
                    "name": it.get("name") or "",
                    "desc": it.get("desc") or "",
                    "example": it.get("example") or "",
                }
            )
    return out


async def build_settings_data(service, event) -> dict:
    c = service.cfg()
    login = await _fetch_settings_login(service, event)

    q = c.get("quality") or "auto"
    quality_label = QUALITY_LABEL.get(q, str(q).upper())
    api_base_view = cardlib.mask_api_base(c.get("apiBase", ""))
    api_hint = (
        f"API · {api_base_view.replace('https://', '').replace('http://', '')}"
        if c.get("apiBase")
        else "API 未配置"
    )

    tiles = _settings_tiles(c)

    return {
        "title": "QQ音乐设置",
        "subtitle": "当前插件运行配置一览",
        "apiHint": api_hint,
        "quality": quality_label,
        "loginOk": login["ok"],
        "switchOn": sum(1 for t in tiles if t["on"]),
        "switchTotal": len(tiles),
        "tiles": tiles,
        "rows": [
            {"k": "API", "v": api_base_view},
            {"k": "登录", "v": login["text"]},
            {"k": "适配器", "v": platform_name(event)},
            {
                "k": "音质",
                "v": quality_label
                + (" · 自动降级" if c.get("qualityFallback") is not False else ""),
            },
            {"k": "列表数", "v": str(int(c.get("maxList") or 10))},
            {
                "k": "发送",
                "v": (
                    f"语音 {_on_off(c.get('sendVocal'))}"
                    f" / 文件 {_on_off(c.get('uploadFile'))}"
                    f" / 原生卡 {_on_off(c.get('sendNativeCard'))}"
                    f" / 自定义卡 {_on_off(c.get('sendCustomCard'))}"
                    + (" / 禁高清语音" if c.get("disableHighQualityVocal") else "")
                ),
            },
        ],
        "commands": _settings_commands(),
        "tip": "详细开关可在 AstrBot 管理面板修改",
    }
