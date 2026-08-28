"""QQ音乐插件 —— AstrBot 音乐点播、榜单、MV、解析与登录（模块化主入口）。

架构：
    main.py            仅保留插件生命周期与初始化，指令通过声明式路由安装
    core/              核心服务层（MusicService、状态、会话、登录轮询、解析与投递流程）
    handlers/          指令路由表（按域拆分：播放/探索/详情/鉴权/系统/分享解析）
    cards.py           卡片数据构造与格式化
    delivery.py        多平台音频/视频投递与转换
    render.py          Playwright HTML 渲染引擎
    api.py             qqmusic-api 客户端封装
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

try:
    from .core.service import MusicService
    from .handlers import ALL_ROUTES
    from .handlers import install as install_routes
except ImportError:  # 兼容以文件方式直接加载的旧版内核
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from core.service import MusicService
    from handlers import ALL_ROUTES
    from handlers import install as install_routes

PLUGIN_NAME = "astrbot_plugin_qqmusic"


class QQMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = MusicService(self)

    async def initialize(self):
        logger.info(f"[{PLUGIN_NAME}] 插件已加载，共注册 {len(ALL_ROUTES)} 条指令路由")

    async def terminate(self):
        await self.service.terminate()
        try:
            from .core.render import close as close_renderer

            await close_renderer()
        except Exception:
            pass


# 安装全部指令路由（handlers/ 目录按业务域维护）
install_routes(QQMusicPlugin, filter, __name__, ALL_ROUTES)
