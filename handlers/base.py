"""声明式路由基座。

AstrBot 通过 handler.__module__ 与插件主模块做【精确匹配】来绑定实例
（见 star_handler.get_handlers_by_module_name），因此所有被装饰的函数
必须归属到主模块。install() 在装饰前重写 __module__，从而允许把路由表
安全地拆分到任意子模块中。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import re
from typing import Any


@dataclass
class Route:
    pattern: str | re.Pattern | None
    name: str
    doc: str
    run: Callable[..., Awaitable]
    admin: bool = False
    priority: int = 0
    event_message_type: Any = None
    extras: dict = field(default_factory=dict)


def install(cls, flt, module_path: str, routes: list[Route]) -> int:
    """把路由安装到插件类上，返回安装数量。

    flt 为 astrbot.api.event.filter 子模块。
    """
    installed = 0
    for route in routes:

        async def handler(self, event, _route=route):
            await _route.run(self.service, event)

        handler.__name__ = route.name
        handler.__qualname__ = f"{cls.__name__}.{route.name}"
        handler.__doc__ = route.doc
        handler.__module__ = module_path

        if route.admin:
            handler = flt.permission_type(flt.PermissionType.ADMIN)(handler)

        if route.event_message_type is not None:
            handler = flt.event_message_type(
                route.event_message_type, priority=route.priority
            )(handler)
        elif route.pattern is not None:
            handler = flt.regex(route.pattern, priority=route.priority)(handler)

        setattr(cls, route.name, handler)
        installed += 1
    return installed
