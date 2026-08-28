from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter

try:
    from ..core.service import (
        MusicService,
        collect_message_text,
        is_plugin_command_msg,
        is_qqmusic_message,
    )
    from .base import Route
except (ImportError, ValueError):
    from core.service import (
        MusicService,
        collect_message_text,
        is_plugin_command_msg,
        is_qqmusic_message,
    )
    from handlers.base import Route


async def resolve(service: MusicService, event: AstrMessageEvent):
    """自动解析 QQ 音乐分享卡片 / 链接。

    用全量事件而非 @filter.regex：OneBot json 段（分享卡片）不会写入
    message_str，regex 过滤器永远匹配不到 → 群里发卡片无反应。
    高优先级抢先解析，避免与其他插件重复处理（对齐原版 accept 抢占）。
    """
    cfg = service.cfg()
    if not cfg.get("enable", True) or cfg.get("enableResolve") is False:
        return
    msg_str = str(event.message_str or "")
    chain = getattr(getattr(event, "message_obj", None), "message", None) or []
    has_json = any(type(seg).__name__ == "Json" for seg in chain)
    if not has_json and not is_qqmusic_message(msg_str):
        return
    text = collect_message_text(event)
    if not is_qqmusic_message(text):
        return
    if is_plugin_command_msg(msg_str):
        return
    try:
        ok = await service.handle_resolve(event, text, cfg)
        if ok:
            event.stop_event()
    except Exception as err:
        service.log_warn(f"解析失败: {err}")


ROUTES: list[Route] = [
    Route(
        pattern=None,
        name="resolve",
        doc="自动解析 QQ 音乐分享卡片 / 链接",
        run=resolve,
        priority=8,
        event_message_type=filter.EventMessageType.ALL,
    ),
]
