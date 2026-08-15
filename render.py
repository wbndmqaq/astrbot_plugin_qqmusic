from __future__ import annotations

import asyncio
import contextlib

# Chromium 启动参数（对齐原 JS 插件 puppeteer 启动参数）
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


async def _get_browser():

    global _playwright, _browser
    if _browser is not None:
        return _browser
    async with _lock:
        if _browser is None:
            from playwright.async_api import async_playwright

            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=True,
                args=_CHROME_ARGS,
            )
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
