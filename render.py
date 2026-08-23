from __future__ import annotations

import asyncio
import contextlib

from astrbot.api import logger

# Chromium 启动参数（对齐原 JS 插件 puppeteer 启动参数 + Docker 防崩溃）
_CHROME_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--font-render-hinting=none",
    "--enable-font-antialiasing",
    "--hide-scrollbars",
]

_playwright = None
_browser = None
_lock = asyncio.Lock()

# 渲染环境缺失的完整安装指引只输出一次，避免每次渲染刷屏
_hint_logged = False

# 手动安装教程（与 README「卡片渲染」章节保持一致）
_RENDER_TUTORIAL = (
    "本插件绝不自动执行任何系统级安装（不改 apt 源 / 不跑 apt-get / 不自动 pip 装包 /\n"
    "不自动下载内核），请按以下步骤手动操作：\n"
    "\n"
    "① 安装 playwright Python 包：\n"
    "     pip install playwright\n"
    "     # 国内镜像：pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple\n"
    "\n"
    "② 下载 Chromium 浏览器内核：\n"
    "     python -m playwright install chromium\n"
    "     # 国内加速：PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ \\\n"
    "     #           python -m playwright install chromium\n"
    "\n"
    "③ 仅 Linux 容器且报 libnspr4/libnss3/shared libraries 缺失时（需 root）：\n"
    "     python -m playwright install-deps chromium\n"
    "     # 或手动装库：apt-get update && apt-get install -y libnspr4 libnss3 libgbm1\n"
    "     #   libasound2 libatk-bridge2.0-0 libatk1.0-0 libcairo2 libcups2 libdrm2 \\\n"
    "     #   libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 \\\n"
    "     #   libxrandr2 libxext6 libpango-1.0-0\n"
    "     # 容器内 apt 下载慢可先换阿里源（Debian 12 示例）：\n"
    "     #   sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources\n"
    "\n"
    "完成后重载本插件即可渲染图片；未安装期间所有指令自动回退纯文本，点歌播放不受影响。\n"
    "更多说明见 README「卡片渲染」章节。"
)


def _log_env_hint(reason: str) -> None:
    """卡片渲染不可用时在日志输出完整的手动安装教程。

    安全约束：插件绝不自动执行 pip/apt/Chromium 下载等系统级安装，
    仅记录指引交由用户确认后自行操作。
    """
    global _hint_logged
    if _hint_logged:
        logger.warning(f"[qqmusic] 卡片渲染不可用（{reason}），已回退纯文本")
        return
    _hint_logged = True
    logger.error(f"[qqmusic] 卡片渲染不可用（{reason}）。\n{_RENDER_TUTORIAL}")


async def _get_browser():
    """获取常驻 Chromium 实例；环境不可用返回 None（由调用方回退文本）。"""
    global _playwright, _browser
    if _browser is not None:
        return _browser
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        _log_env_hint(f"缺少依赖 {e.name}")
        return None
    async with _lock:
        if _browser is not None:
            return _browser
        pw = None
        try:
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(headless=True, args=_CHROME_ARGS)
            _playwright = pw
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                _log_env_hint("未下载 Chromium")
            elif "shared libraries" in msg or "shared object" in msg:
                # 二进制存在但容器缺 libnspr4/libnss3 等系统运行库
                _log_env_hint("Chromium 缺少系统运行库")
            else:
                logger.error(f"[qqmusic] Chromium 启动失败: {e}")
            # 清理半初始化的 playwright 运行时，避免进程残留
            if pw is not None:
                with contextlib.suppress(Exception):
                    await pw.stop()
            return None
    return _browser


async def render_html_to_png(html: str, out_path: str) -> bool:

    browser = await _get_browser()
    if browser is None:
        return False
    page = await browser.new_page(
        viewport={"width": 640, "height": 2200, "device_scale_factor": 3}
    )
    try:
        # 网络空闲（含远程封面图加载完成）；超时则退回 load 兜底
        try:
            await page.set_content(html, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.set_content(html, wait_until="load", timeout=30000)
        # 与 JS 版一致：固定不透明浅绿底，防止协议把透明填充成白色
        await page.evaluate(
            """() => {
                const bg = '#e6f6ee';
                document.documentElement.style.background = bg;
                document.body.style.background = bg;
                document.documentElement.style.width = 'fit-content';
                document.body.style.width = 'fit-content';
                document.documentElement.style.margin = '0';
                document.body.style.margin = '0';
            }"""
        )
        with contextlib.suppress(Exception):
            await page.evaluate("document.fonts.ready.then(() => {})")
        await page.wait_for_timeout(200)

        # 截 .page（含浅绿底 + 卡片），整图不透明
        el = (
            await page.query_selector(".page")
            or await page.query_selector(".card")
            or page
        )
        box = await el.bounding_box()
        if box:
            need_w = int(box["x"] + box["width"] + 4)
            need_h = int(box["y"] + box["height"] + 4)
            cur = page.viewport_size
            if need_w > cur["width"] or need_h > cur["height"]:
                await page.set_viewport_size(
                    {
                        "width": max(cur["width"], need_w),
                        "height": max(cur["height"], need_h),
                        "device_scale_factor": 3,
                    }
                )
                await page.wait_for_timeout(80)
        await el.screenshot(
            path=out_path,
            type="png",
            omit_background=False,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"[qqmusic] 渲染失败: {e}")
        return False
    finally:
        await page.close()


async def close():

    global _playwright, _browser
    if _browser is not None:
        with contextlib.suppress(Exception):
            await _browser.close()
        _browser = None
    if _playwright is not None:
        with contextlib.suppress(Exception):
            await _playwright.stop()
        _playwright = None
