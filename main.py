"""QQ音乐插件 —— AstrBot 音乐点播、榜单、MV、解析与登录（模块化主入口）。

架构：
    main.py            Star 插件主入口，仅保留生命周期与路由安装
    core/service.py    MusicService：服务门面（会话归属、配置写盘、卡片渲染、歌词解析）
    core/api/          qqmusic-api 客户端包（_core HTTP 会话与鉴权 + 按域端点模块）
    core/delivery.py   音频/视频下载、ffmpeg 转码与多平台双通道投递
    core/login.py      扫码登录（App/MQTT 与无感 webqr 两条通道）与轮询
    core/panels.py     状态卡/设置卡数据构造与脱敏
    core/resolve.py    分享卡片/链接自动解析
    core/cards.py      会话存储、卡片数据构造与纯文本格式化
    core/render.py     Playwright HTML 渲染引擎
    core/messages.py   用户可见文案常量
    core/quality.py    音质档位常量与标签映射
    core/utils.py      无依赖小工具
    handlers/          声明式指令路由表（play/explore/detail/auth/system/share）
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

from .core.service import MusicService
from .handlers import ALL_ROUTES
from .handlers import install as install_routes

PLUGIN_NAME = "astrbot_plugin_qqmusic"


class QQMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = MusicService(self)

    # delivery.py 以 plugin._xxx 的形式调用投递辅助方法，实际实现挂在 service 上
    # 仅供 core/delivery.py 使用的发送门面：**只在消息含媒体组件时**于失败处抛异常。
    # 投递层靠这个异常触发降级链（语音失败退回 Record 组件、文件失败用 ffmpeg 压成紧凑
    # mp3 重试、视频直发失败改落盘）。纯文案（Plain）只是附带信息，失败不该中断投递——
    # 抛出去会让用户连音频带回复都拿不到。
    async def _send_chain(self, event, *components):
        has_media = any(c is not None and not isinstance(c, Plain) for c in components)
        return await self.service.send_chain(
            event, *components, raise_on_error=has_media
        )

    def _plain(self, text: str):
        return self.service.plain(text)

    def _log_warn(self, msg: str):
        self.service.log_warn(msg)

    def _log_info(self, msg: str):
        self.service.log_info(msg)

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
