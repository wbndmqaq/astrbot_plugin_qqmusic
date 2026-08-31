from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image

try:
    from ..core import api as qqapi
    from ..core import cards as cardlib
    from ..core.service import PLUGIN_DIR, MusicService, get_local_version
    from .base import Route
except (ImportError, ValueError):
    from core import api as qqapi
    from core import cards as cardlib
    from core.service import PLUGIN_DIR, MusicService, get_local_version
    from handlers.base import Route


async def hot_search(service: MusicService, event: AstrMessageEvent):
    """#qqm热搜 查看热搜榜"""
    try:
        lst = await qqapi.hot_keys(service.user_key(event))
        tops = (lst if isinstance(lst, list) else [])[:15]
        if not tops:
            await service.reply(event, "暂无热搜")
            event.stop_event()
            return
        data = cardlib.build_hot_card_data(tops, cfg=service.cfg())
        await service.reply_card_or_text(
            event,
            tpl_name="qqmusic-hot",
            data=data,
            format_text=lambda d: cardlib.format_hot_text(tops),
        )
    except Exception as err:
        await service.reply(event, f"热搜失败：{err}")
    event.stop_event()


async def help(service: MusicService, event: AstrMessageEvent):
    """#qqm帮助 帮助图片卡片"""
    cfg = service.cfg()
    try:
        version = get_local_version(PLUGIN_DIR)
        data = cardlib.build_help_card_data(cfg=service.cfg(), version=version)
        url = await service.render_card(event, data, "qqmusic-help")
        if url:
            await service.send_chain(event, Image.fromFileSystem(url))
            event.stop_event()
            return
    except Exception as err:
        service.log_warn(f"帮助图渲染失败: {err}")
    await service.reply(
        event,
        "\n".join(
            [
                "【QQ音乐插件帮助】",
                "— 点歌（均需 #qqm 前缀）—",
                "#qqm点歌 七里香  →  #qqm听1（会话内也可 #听1）",
                "#qqm播放 晴天",
                "#qqm歌词 七里香  /  #qqm热搜",
                "— MV —",
                "#qqmMV 搜索 周杰伦  →  #qqmMV 播放1 / 下载1",
                "#qqmMV（分类浏览）；点歌后再发 #qqmMV 播放/下载 = 本曲 MV",
                "— 状态 —",
                "#qqm登录 / #qqm登录微信 / #qqm登录qq / #qqm状态  /  #qms  /  #qqm登出",
                "— 管理（主人）—",
                "#qqm设置  /  #qqm 音质 flac  /  #qqm 测试",
                "— 解析 —",
                "分享 QQ 音乐卡片或 y.qq.com 链接自动解析",
                f"API: {'已配置' if cfg.get('apiBase') else '未配置'}",
            ]
        ),
    )
    event.stop_event()


async def show_config(service: MusicService, event: AstrMessageEvent):
    """#qqm设置 查看当前配置"""
    try:
        data = await service.build_settings_data(event)
        url = await service.render_card(event, data, "qqmusic-settings")
        if url:
            await service.send_chain(event, Image.fromFileSystem(url))
            event.stop_event()
            return
    except Exception as err:
        service.log_warn(f"设置卡片渲染失败，回退文本: {err}")
    c = service.cfg()
    login_line = "login: (查询失败)"
    try:
        st = await qqapi.request("/login/status", {}, "get", service.user_key(event))
        d = (st or {}).get("data") or {}
        login_line = (
            f"login: 已绑定 uin={d.get('uin')} ({d.get('nick')})"
            if d.get("login")
            else "login: 未绑定（#qqm登录 扫码）"
        )
    except Exception:
        pass
    adapter_line = f"adapter: {service.platform_name(event)}"
    try:
        version_line = f"version: v{get_local_version(PLUGIN_DIR)}"
    except Exception:
        version_line = ""
    await service.reply(
        event,
        "\n".join(
            [
                "【QQ音乐插件配置】",
                f"enable: {c.get('enable', True)}",
                f"apiBase: {cardlib.mask_api_base(c.get('apiBase', ''))}",
                login_line,
                adapter_line,
                version_line,
                f"点歌: {c.get('enableSongRequest', True)}  解析: {c.get('enableResolve', True)}",
                (
                    f"音质: {c.get('quality', 'auto')}"
                    f"（自动降级: {c.get('qualityFallback', True) is not False}）"
                    f"  列表: {c.get('maxList', 10)}"
                ),
                f"语音: {c.get('sendVocal', True)}  群文件: {c.get('uploadFile', True)}",
                f"原生卡: {c.get('sendNativeCard', False)}  自定义卡: {c.get('sendCustomCard', False)}",
                "",
                "主人命令：",
                "#qqm登录          无感扫码（QQ 码，覆盖 QQ/App，主通道）",
                "#qqm登录微信      无感扫码（微信码）",
                "#qqm登录qq        QQ音乐 App 扫码（备用）",
                "#qqm状态 / #qms   状态图片卡片",
                "#qqm绑定 deeplink",
                "#qqm api <地址>   （设置 API 地址）",
                "#qqm 开启点歌 / #qqm 关闭解析",
                "#qqm 音质 flac",
                "#qqm 测试",
            ]
        ),
    )
    event.stop_event()


async def set_api(service: MusicService, event: AstrMessageEvent):
    """#qqm api <地址> 设置 API 地址（主人）"""
    m = re.search(r"api\s*(\S+)", event.message_str, re.IGNORECASE)
    url = qqapi.normalize_api_base(m.group(1)) if m else ""
    if not url or not re.match(r"^https?://", url, re.IGNORECASE):
        await service.reply(event, "用法：#qqm api http://你的API地址:端口")
        event.stop_event()
        return
    service.config["apiBase"] = url
    try:
        service.config.save_config()
    except Exception as e:
        service.log_warn(f"配置保存失败: {e}")
    await service.reply(event, f"API 地址已更新：{cardlib.mask_api_base(url)}")
    event.stop_event()


async def toggle(service: MusicService, event: AstrMessageEvent):
    """#qqm 开启/关闭 点歌/解析 功能开关（主人）"""
    m = re.search(r"(开启|关闭)(点歌|解析)", event.message_str)
    if not m:
        event.stop_event()
        return
    on = m.group(1) == "开启"
    key = "enableSongRequest" if m.group(2) == "点歌" else "enableResolve"
    service.config[key] = on
    try:
        service.config.save_config()
    except Exception as e:
        service.log_warn(f"配置保存失败: {e}")
    await service.reply(event, f"已{m.group(1)}{m.group(2)}")
    event.stop_event()


async def set_quality(service: MusicService, event: AstrMessageEvent):
    """#qqm 音质 <档位> 设置最高音质（主人）"""
    m = re.search(
        r"音质\s*(auto|128|m4a|320|flac|ape|hires|atmos|master|atmos_master)",
        event.message_str,
        re.IGNORECASE,
    )
    q = m.group(1).lower() if m else ""
    if not q:
        event.stop_event()
        return
    service.config["quality"] = q
    try:
        service.config.save_config()
    except Exception as e:
        service.log_warn(f"配置保存失败: {e}")
    await service.reply(
        event,
        f"默认最高音质已设为 {q}\n"
        "可选: auto / 128 / m4a / 320 / flac / ape / hires / atmos / master / atmos_master",
    )
    event.stop_event()


async def ping(service: MusicService, event: AstrMessageEvent):
    """#qqm 测试 测试 API 连通（主人）"""
    try:
        data = await qqapi.request("/")
        await service.reply(
            event,
            f"API 正常\nroutes: {len(data.get('routes') or [])}"
            f"\n已登录: {len(data.get('accounts') or [])}",
        )
    except Exception as err:
        await service.reply(event, f"API 不可用：{err}")
    event.stop_event()


ROUTES: list[Route] = [
    Route(
        pattern=r"^#?(qq|QQ)m\s*热搜$",
        name="hot_search",
        doc="#qqm热搜 查看热搜榜",
        run=hot_search,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*help$|^#?(qq|QQ)m\s*帮助$|^#?(qq|QQ)音乐帮助$|^#qm帮助$",
        name="help",
        doc="#qqm帮助 帮助图片卡片",
        run=help,
    ),
    Route(
        pattern=re.compile(r"^#?(qqm设置|qqm配置|qq音乐设置)$", re.IGNORECASE),
        name="show_config",
        doc="#qqm设置 查看当前配置",
        run=show_config,
        priority=6,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*api\s*(\S+)$",
        name="set_api",
        doc="#qqm api <地址> 设置 API 地址（主人）",
        run=set_api,
        admin=True,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*(开启|关闭)(点歌|解析)$",
        name="toggle",
        doc="#qqm 开启/关闭 点歌/解析 功能开关（主人）",
        run=toggle,
        admin=True,
    ),
    Route(
        pattern=r"^#?(qq|QQ)m\s*音质\s*(auto|128|m4a|320|flac|ape|hires|atmos|master|atmos_master)$",
        name="set_quality",
        doc="#qqm 音质 <档位> 设置最高音质（主人）",
        run=set_quality,
        admin=True,
    ),
    Route(
        pattern=re.compile(
            r"^#?(?:qq|QQ)m\s*(?:测试|ping)$|^#?(?:qq|QQ)音乐测试$", re.IGNORECASE
        ),
        name="ping",
        doc="#qqm 测试 测试 API 连通（主人）",
        run=ping,
        admin=True,
        priority=6,
    ),
]
