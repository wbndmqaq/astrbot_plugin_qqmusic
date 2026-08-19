from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys

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


def _switch_apt_to_aliyun():
    """Debian/Ubuntu 容器下把官方 apt 源换成阿里镜像，加速 install-deps 下载。

    仅当存在 apt-get 且源文件指向官方域名时才改写（幂等，不覆盖用户自选镜像），
    首次改写前备份为 .bak；任何失败只记日志不影响后续。返回是否发生了改动。
    """

    import glob
    import shutil

    if not shutil.which("apt-get"):
        return False
    targets = ["/etc/apt/sources.list"]
    targets += glob.glob("/etc/apt/sources.list.d/*.sources")
    mapping = [
        ("http://deb.debian.org/debian", "http://mirrors.aliyun.com/debian"),
        ("https://deb.debian.org/debian", "http://mirrors.aliyun.com/debian"),
        ("http://security.debian.org/debian-security", "http://mirrors.aliyun.com/debian-security"),
        ("https://security.debian.org/debian-security", "http://mirrors.aliyun.com/debian-security"),
        ("http://archive.ubuntu.com/ubuntu", "http://mirrors.aliyun.com/ubuntu"),
        ("https://archive.ubuntu.com/ubuntu", "http://mirrors.aliyun.com/ubuntu"),
        ("http://security.ubuntu.com/ubuntu", "http://mirrors.aliyun.com/ubuntu"),
        ("https://security.ubuntu.com/ubuntu", "http://mirrors.aliyun.com/ubuntu"),
    ]
    changed = False
    for path in targets:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            content = None
        if content is None:
            continue
        new_content = content
        for old, new in mapping:
            if old in new_content:
                new_content = new_content.replace(old, new)
        if new_content == content:
            continue
        bak = path + ".bak"
        try:
            if not os.path.exists(bak):
                with open(bak, "w", encoding="utf-8") as f:
                    f.write(content)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            logger.warning(f"[qqmusic] apt 源改写失败 {path}: {e}")
            continue
        changed = True
        logger.info(f"[qqmusic] apt 源 {path} 已切换阿里镜像（原文件备份为 {bak}）")
    if changed:
        try:
            subprocess.run(["apt-get", "update"], capture_output=True, text=True, check=False, timeout=300)
        except Exception as e:
            logger.warning(f"[qqmusic] apt-get update 失败: {e}")
    return changed


def _install_deps_sync():
    """在子线程中同步执行 playwright install-deps（先切阿里 apt 源再装系统运行库）。"""
    _switch_apt_to_aliyun()
    cmd = [sys.executable, "-m", "playwright", "install-deps", "chromium"]
    logger.info(f"[qqmusic] 正在安装 Chromium 系统依赖: {' '.join(cmd)}")
    env = os.environ.copy()
    env["PLAYWRIGHT_DOWNLOAD_HOST"] = "https://npmmirror.com/mirrors/playwright/"
    return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)


def _install_chromium_sync():
    """在子线程中同步执行 playwright install chromium（附带 npmmirror 加速镜像源）。"""
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    logger.info(f"[qqmusic] 正在自动安装 Playwright Chromium: {' '.join(cmd)}")
    env = os.environ.copy()
    env["PLAYWRIGHT_DOWNLOAD_HOST"] = "https://npmmirror.com/mirrors/playwright/"
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        logger.error(f"[qqmusic] 自动安装 Chromium 失败: {res.stderr}")
        raise RuntimeError(f"Playwright Chromium 安装失败: {res.stderr or res.stdout}")
    logger.info("[qqmusic] Playwright Chromium 安装完成！")


async def _get_browser():

    global _playwright, _browser
    if _browser is not None:
        return _browser
    async with _lock:
        if _browser is None:
            from playwright.async_api import async_playwright

            _playwright = await async_playwright().start()
            try:
                _browser = await _playwright.chromium.launch(
                    headless=True,
                    args=_CHROME_ARGS,
                )
            except Exception as e:
                err_msg = str(e)
                if "Executable doesn't exist" in err_msg or "playwright install" in err_msg:
                    logger.warning("[qqmusic] 未找到 Playwright Chromium，正在尝试通过 npmmirror 镜像源自动下载安装...")
                    await asyncio.to_thread(_install_chromium_sync)
                    _browser = await _playwright.chromium.launch(
                        headless=True,
                        args=_CHROME_ARGS,
                    )
                elif "error while loading shared libraries" in err_msg or "shared object file" in err_msg:
                    # 二进制已下载但容器缺系统运行库（libnspr4/libnss3 等）。
                    # playwright install 只下载二进制、不装 OS 包；先切阿里 apt 源再
                    # install-deps（需 apt + root），失败则给出可直接执行的安装命令。
                    logger.warning("[qqmusic] Chromium 缺少系统运行库，尝试执行 playwright install-deps 自动安装...")
                    res = await asyncio.to_thread(_install_deps_sync)
                    if res.returncode == 0:
                        logger.info("[qqmusic] playwright install-deps 完成，重新启动浏览器...")
                        _browser = await _playwright.chromium.launch(
                            headless=True,
                            args=_CHROME_ARGS,
                        )
                    else:
                        hint = (
                            "请在容器内以 root 执行：\n"
                            "python -m playwright install-deps chromium\n"
                            "或手动：apt-get update && apt-get install -y libnspr4 libnss3 "
                            "libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 libxfixes3 "
                            "libxrandr2 libgbm1 libasound2 libatk1.0-0 libatk-bridge2.0-0 "
                            "libcairo2 libcups2 libdrm2 libxkbcommon0 libxext6 libpango-1.0-0"
                        )
                        logger.error(
                            f"[qqmusic] 自动安装系统依赖失败，请手动安装后重试：\n{hint}\n\n安装输出：\n{res.stdout[-2000:]}\n{res.stderr[-2000:]}"
                        )
                        raise
                else:
                    raise e
    return _browser


async def render_html_to_png(html: str, out_path: str) -> bool:

    browser = await _get_browser()
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
