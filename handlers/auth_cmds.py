from __future__ import annotations

import asyncio
import re

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image

try:
    from ..core import api as qqapi
    from ..core import cards as cardlib
    from ..core.service import MusicService
    from .base import Route
except (ImportError, ValueError):
    from core import api as qqapi
    from core import cards as cardlib
    from core.service import MusicService
    from handlers.base import Route


async def start_qr_login(service: MusicService, event: AstrMessageEvent):
    """#qqm登录qq QQ音乐 App 扫码登录（MQTT 备用通道，主人）"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    if cfg.get("qrLoginEnable") is False:
        await service.reply(event, "扫码登录已在配置中关闭")
        event.stop_event()
        return
    service.stop_poll(service.user_key(event))
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在获取 QQ 音乐登录二维码…")
        body = await qqapi.request("/login/qr", {}, "get", user_key)
        data = qqapi.unwrap_data(body)
        if not data.get("qrcodeID"):
            await service.reply(
                event, f"获取二维码失败：{(body or {}).get('errMsg') or '未知错误'}"
            )
            event.stop_event()
            return
        qrcode_id = data.get("qrcodeID")
        qrcode_b64 = data.get("qrcodeBase64")
        qrcode = data.get("qrcode")
        try:
            expires_in = int(data.get("expiresIn") or 900)
        except (TypeError, ValueError):
            expires_in = 900
        tips = data.get("tips") or "请使用 QQ / 微信 / QQ音乐 App 扫码"
        tip_text = "\n".join(
            x for x in [tips, f"二维码 {round(expires_in / 60)} 分钟内有效"] if x
        )
        qr_path = None
        if qrcode_b64:
            qr_path = await service.save_qr_image(qrcode_b64)
        elif qrcode and qrcode.startswith("data:"):
            b64 = qrcode.split(",", 1)[1]
            qr_path = await service.save_qr_image(b64)
        img_sent = False
        if qr_path:
            try:
                await service.send_chain(
                    event, Image.fromFileSystem(qr_path), service.plain(tip_text)
                )
                img_sent = True
            except Exception:
                pass
            loop = asyncio.get_event_loop()
            loop.call_later(120, lambda: service.safe_unlink(qr_path))
        if not img_sent:
            await service.reply(
                event, tip_text + "\n（图片发送失败可重新 #qqm登录qq）"
            )
        try:
            poll_interval = float(data.get("pollInterval") or 2)
        except (TypeError, ValueError):
            poll_interval = 2
        if poll_interval <= 0 or poll_interval > 30:
            poll_interval = 2
        service.start_poll(event, qrcode_id, expires_in, poll_interval)
    except Exception as err:
        await service.reply(event, f"扫码登录失败：{err}")
    event.stop_event()


async def start_webqr_login(service: MusicService, event: AstrMessageEvent):
    """#qqm登录 无感扫码登录（一张 QQ 码，主人）"""
    cfg = service.cfg()
    if not cfg.get("enable", True):
        return
    if cfg.get("qrLoginEnable") is False:
        await service.reply(event, "扫码登录已在配置中关闭")
        event.stop_event()
        return
    service.stop_poll(service.user_key(event))
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在生成登录二维码…")
        body = await qqapi.request("/login/webqr", {}, "post", user_key)
        data = qqapi.unwrap_data(body)
        if not data.get("sessionId") or not (
            data.get("qrcodeWx") or data.get("qrcodeQq")
        ):
            await service.reply(
                event,
                f"获取二维码失败：{(body or {}).get('errMsg') or '未知错误'}",
            )
            event.stop_event()
            return
        session_id = data.get("sessionId")
        qrcode_wx = data.get("qrcodeWx")
        qrcode_qq = data.get("qrcodeQq")
        try:
            expires_in = int(data.get("expiresIn") or 180)
        except (TypeError, ValueError):
            expires_in = 180
        want_wx = bool(re.search(r"微信|wx", event.message_str, re.IGNORECASE))
        if want_wx and qrcode_wx:
            codes = [("微信", qrcode_wx)]
        elif qrcode_qq:
            codes = [("QQ", qrcode_qq)]
        elif qrcode_wx:
            codes = [("微信", qrcode_wx)]
        else:
            codes = [
                c
                for c in (("QQ", qrcode_qq), ("微信", qrcode_wx))
                if c[1] and c[1].startswith("data:")
            ]
        img_sent = 0
        for _, code in codes:
            if not (code and code.startswith("data:")):
                continue
            try:
                b64 = code.split(",", 1)[1]
                qr_path = await service.save_qr_image(b64)
                await service.send_chain(event, Image.fromFileSystem(qr_path))
                img_sent += 1
                loop = asyncio.get_event_loop()
                loop.call_later(120, lambda p=qr_path: service.safe_unlink(p))
            except Exception as err:
                service.log_warn(f"二维码图片发送失败: {err}")
                continue
        if len(codes) == 1:
            tips = f"请用{codes[0][0]}扫一扫，确认后自动登录"
        else:
            tips = (
                "，".join(f"{label}码用{label}扫" for label, _ in codes)
                + "，确认后自动登录"
            )
        tip_text = "\n".join([tips, f"二维码 {round(expires_in / 60)} 分钟内有效"])
        if img_sent < len(codes):
            tip_text += "\n（图片发送失败可重新发命令）"
        await service.reply(event, tip_text)
        service.start_webqr_poll(event, session_id, expires_in)
    except Exception as err:
        await service.reply(event, f"扫码登录失败：{err}")
    event.stop_event()


async def login_status(service: MusicService, event: AstrMessageEvent):
    """#qqm状态 / #qms 登录状态卡片"""
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在生成 QQ 音乐状态卡片…")
        api_hint = ""
        try:
            st = await qqapi.request("/login/status", {}, "get", user_key)
            d = (st or {}).get("data") or {}
            if d.get("login"):
                key_txt = "有" if d.get("hasKey") else "无"
                ref_txt = "有" if d.get("hasRefresh") else "无"
                api_hint = (
                    f"API已登录 uin={d.get('uin')} key={key_txt} refresh={ref_txt}"
                )
                if d.get("keyAgeSec") is not None:
                    api_hint += f" age={d['keyAgeSec']}s"
            else:
                api_hint = "API 显示未登录"
            service.log_info(api_hint)
        except Exception as err:
            api_hint = f"API状态查询失败: {err}"
        data = await service.build_status_data(user_key)
        if not data.get("loggedIn") and api_hint:
            data["vipExpireText"] = api_hint
        url = await service.render_card(event, data, "qqmusic-status")
        if url:
            await service.send_chain(event, Image.fromFileSystem(url))
        else:
            await service.reply(
                event,
                cardlib.format_status_text(data)
                + (f"\n{api_hint}" if api_hint else ""),
            )
    except Exception as err:
        service.log_warn(f"状态卡片失败: {err}")
        await service.reply(event, f"获取状态失败：{err}")
    event.stop_event()


async def logout(service: MusicService, event: AstrMessageEvent):
    """#qqm登出 清除登录态（主人）"""
    service.stop_poll(service.user_key(event))
    user_key = service.user_key(event)
    try:
        await qqapi.request("/login/logout", {}, "post", user_key)
        await service.reply(event, "已解除登录绑定")
    except Exception as err:
        await service.reply(event, f"登出失败：{err}")
    event.stop_event()


async def sync_from_api(service: MusicService, event: AstrMessageEvent):
    """#qqm同步 从 API 同步登录态（主人）"""
    user_key = service.user_key(event)
    try:
        meta = await qqapi.pull_login_meta(user_key)
        if not meta.get("login") or not meta.get("hasKey"):
            await service.reply(event, "API 当前未登录，请先 #qqm登录")
            event.stop_event()
            return
        lines = ["✅ 登录态正常"]
        if meta.get("uin"):
            lines.append(f"uin: {meta['uin']}")
        if meta.get("nick"):
            lines.append(f"昵称: {meta['nick']}")
        if meta.get("hasRefresh"):
            lines.append("含 refresh，可自动续期")
        else:
            lines.append("⚠️ 无 refresh")
        if meta.get("keyAgeSec") is not None:
            lines.append(f"key 已用 {meta['keyAgeSec']}s")
        await service.reply(event, "\n".join(lines))
    except Exception as err:
        await service.reply(event, f"查询失败：{err}")
    event.stop_event()


async def refresh_key(service: MusicService, event: AstrMessageEvent):
    """#qqm刷新 续期 key（主人）"""
    user_key = service.user_key(event)
    try:
        await service.reply(event, "正在刷新登录 key…")
        body = await qqapi.refresh_login(user_key)
        d = qqapi.unwrap_data(body)
        result = (body or {}).get("result")
        if result is not None and result not in (100, 0):
            lines = [
                f"刷新失败：{(body or {}).get('errMsg') or result}",
                (body or {}).get("tip") or d.get("tip") or "",
                "请重新 #qqm登录",
            ]
            await service.reply(event, "\n".join(x for x in lines if x))
            event.stop_event()
            return
        lines = ["✅ key 已刷新"]
        if d.get("uin"):
            lines.append(f"uin: {d['uin']}")
        if d.get("changed") is False:
            lines.append("（key 未变化）")
        if d.get("hasRefresh") is False:
            lines.append("⚠️ 无 refresh，过期后需重新扫码")
        elif d.get("hasRefresh"):
            lines.append("含 refresh，后续可自动续期")
        await service.reply(event, "\n".join(lines))
    except Exception as err:
        await service.reply(event, f"刷新失败：{err}\n请重新 #qqm登录")
    event.stop_event()


async def bind_manual(service: MusicService, event: AstrMessageEvent):
    """#qqm绑定 qqmusic://... DeepLink 导入（主人）"""
    text = event.message_str.strip()
    m = re.match(r"^#?(?:qq|QQ)m(?:绑定|导入)\s*(.+)$", text, re.IGNORECASE)
    raw = m.group(1).strip() if m else ""
    if not raw:
        await service.reply(event, "用法：#qqm绑定 qqmusic://...")
        event.stop_event()
        return
    try:
        body = await qqapi.request(
            "/login/deeplink", {"url": raw}, "post", service.user_key(event)
        )
        d = (body or {}).get("data") or {}
        await service.on_login_success(
            event, {**d, "channel": d.get("channel") or "deeplink"}
        )
    except Exception as err:
        await service.reply(event, f"绑定失败：{err}")
    event.stop_event()


async def import_deeplink(service: MusicService, event: AstrMessageEvent):
    """识别 qqmusic:// DeepLink 并导入登录态（主人）"""
    text = event.message_str
    m = re.search(r"qqmusic://[^\s]+", text, re.IGNORECASE)
    if not m:
        return
    try:
        body = await qqapi.request(
            "/login/deeplink", {"url": m.group(0)}, "post", service.user_key(event)
        )
        d = (body or {}).get("data") or {}
        service.stop_poll(service.user_key(event))
        await service.on_login_success(
            event, {**d, "channel": d.get("channel") or "deeplink"}
        )
    except Exception as err:
        await service.reply(event, f"DeepLink 导入失败：{err}")
    event.stop_event()


async def list_accounts(service: MusicService, event: AstrMessageEvent):
    """#qqm账号 已登录账号列表（主人）"""
    try:
        lst = await qqapi.list_accounts()
        if not lst:
            await service.reply(event, "当前没有任何账号登录")
            event.stop_event()
            return
        out = []
        for i, a in enumerate(lst):
            nick = a.get("nick")
            out.append(
                f"{i + 1}. userKey={a.get('userKey')} uin={a.get('uin')}"
                + (f" ({nick})" if nick else "")
            )
        lines = out
        await service.reply(event, f"已登录账号 {len(lst)} 个：\n" + "\n".join(lines))
    except Exception as err:
        await service.reply(event, f"查询失败：{err}")
    event.stop_event()


ROUTES: list[Route] = [
    Route(
        pattern=re.compile(r"^#?(qqm登录(qq|app))$", re.IGNORECASE),
        name="start_qr_login",
        doc="#qqm登录qq QQ音乐 App 扫码登录（MQTT 备用通道，主人）",
        run=start_qr_login,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(
            r"^#?(qqm(登录|登陆)(微信|wx)?|qq音乐(登录|登陆)(微信|wx)?)$",
            re.IGNORECASE,
        ),
        name="start_webqr_login",
        doc="#qqm登录 无感扫码登录（一张 QQ 码，主人）",
        run=start_webqr_login,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(
            r"^#?(qqm状态|qqm登录状态|qqm登陆状态|qq音乐状态|qq状态|qms)$",
            re.IGNORECASE,
        ),
        name="login_status",
        doc="#qqm状态 / #qms 登录状态卡片",
        run=login_status,
        priority=6,
    ),
    Route(
        pattern=re.compile(
            r"^#?(qqm登出|qqm注销|qqm解绑|qq音乐登出|qq音乐解绑)$", re.IGNORECASE
        ),
        name="logout",
        doc="#qqm登出 清除登录态（主人）",
        run=logout,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?qqm(同步|拉取|sync)(登录态)?$", re.IGNORECASE),
        name="sync_from_api",
        doc="#qqm同步 从 API 同步登录态（主人）",
        run=sync_from_api,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?qqm(刷新|续期|refresh)(登录|key)?$", re.IGNORECASE),
        name="refresh_key",
        doc="#qqm刷新 续期 key（主人）",
        run=refresh_key,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=re.compile(r"^#?(qqm绑定|qqm导入)(?:\s.*)?$", re.IGNORECASE),
        name="bind_manual",
        doc="#qqm绑定 qqmusic://... DeepLink 导入（主人）",
        run=bind_manual,
        admin=True,
        priority=6,
    ),
    Route(
        pattern=r"qqmusic://",
        name="import_deeplink",
        doc="识别 qqmusic:// DeepLink 并导入登录态（主人）",
        run=import_deeplink,
        admin=True,
    ),
    Route(
        pattern=re.compile(r"^#?qqm\s*(账号|accounts|已登录)$", re.IGNORECASE),
        name="list_accounts",
        doc="#qqm账号 已登录账号列表（主人）",
        run=list_accounts,
        admin=True,
        priority=6,
    ),
]
