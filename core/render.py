from __future__ import annotations

import asyncio
import contextlib
import os

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

# 启动失败后的短路标记：渲染环境缺失（缺包/缺 Chromium/缺系统库）是**稳定**状态，
# 原先每渲染一张卡片都要重跑 async_playwright().start() + launch()（含超时等待），
# 一次列表卡片就能白付几秒。故失败后置位，后续渲染直接返回 None（走文本兜底）；
# 用户装好环境后重载插件即可（模块级变量随重载复位，日志指引也是这么写的）。
_launch_failed = False

# 渲染环境缺失的完整安装指引只输出一次，避免每次渲染刷屏
_hint_logged = False

# 手动安装教程（与 README「卡片渲染」章节保持一致）
_RENDER_TUTORIAL = (
    "本插件不自动执行任何系统级安装，请按以下步骤手动操作：\n"
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
    "③ Linux 容器且报 libnspr4/libnss3/shared libraries 缺失时（需 root）：\n"
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
    """获取常驻 Chromium 实例；环境不可用返回 None（由调用方回退文本）。

    失败后由 ``_launch_failed`` 短路：同一进程内不再重复尝试启动（环境缺失是稳定
    状态，重试只是白等超时），插件重载后自动复位重试。
    """
    global _playwright, _browser, _launch_failed
    if _browser is not None:
        return _browser
    if _launch_failed:
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        _launch_failed = True
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
            _launch_failed = True
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


def _remove_output(path: str) -> None:
    """删除渲染产物（best-effort）：文件不存在/被占用一律忽略，不影响主流程。"""
    with contextlib.suppress(OSError):
        os.remove(path)


# ──────────── 模板编译与卡片数据包装 ────────────

# 已编译模板缓存：模板文件在进程内不变（改模板需重载插件），因此「读盘 + 编译」只在
# 首次发生，并且整体丢进线程池（读盘是阻塞 IO、编译是纯 CPU），不占用事件循环。
_compiled_templates: dict[str, object] = {}


def _compile_template(tmpl_path: str):
    import jinja2

    with open(tmpl_path, "r", encoding="utf-8") as f:
        src = f.read()
    # autoescape=True：卡片数据含用户可控文本（点歌关键词、昵称、评论），
    # 默认不转义会把 <img onerror>/<script> 原样注入渲染页。
    return jinja2.Template(src, autoescape=True)


async def load_template(tmpl_path: str):
    """按路径缓存并返回已编译模板（首次在线程池内完成读盘与编译）。"""
    template = _compiled_templates.get(tmpl_path)
    if template is None:
        template = await asyncio.to_thread(_compile_template, tmpl_path)
        _compiled_templates[tmpl_path] = template
    return template


# 与字典方法同名的键：模板若写 ``data.items``（属性取值），Jinja 会先取属性，普通 dict
# 上拿到的是 bound method（渲染成方法对象、被 for 迭代时 TypeError）。当前模板都已改用
# ``data['items']`` 下标写法，此处是防止以后改回属性写法时踩坑的防御。
_DICT_METHODS = frozenset(
    {"items", "keys", "values", "get", "copy", "update", "pop", "popitem", "clear", "setdefault"}
)


class _SafeData(dict):
    """卡片数据包装：缺失键一律给空值，而不是 Jinja 的 Undefined。

    解决两类会让整张卡片渲染失败/渲染错的情况：

    1. **缺失键**：``data['items']`` 取不到时 Jinja 给 Undefined，被 ``for`` 迭代直接
       抛错（整卡退化成纯文本）。这里 ``__missing__`` 返回空串——``for`` 迭代 0 次、
       ``|length`` 得 0。
    2. **与字典方法同名的键**：``data.items`` 是属性取值，普通 dict 会命中 ``dict.items``
       方法。这里在 ``__getattribute__`` 里让同名**数据键优先**，没有该键时返回空串
       （模板不会去调字典方法，返回空串比返回 bound method 安全）。
    """

    def __missing__(self, key):
        return ""

    def __getattribute__(self, key):
        if key in _DICT_METHODS:
            try:
                return dict.__getitem__(self, key)
            except KeyError:
                return ""
        return dict.__getattribute__(self, key)


def wrap_card_data(value):
    """递归包装卡片数据：dict → _SafeData，list/tuple → 同构容器。"""
    if isinstance(value, dict):
        return _SafeData({k: wrap_card_data(v) for k, v in value.items()})
    if isinstance(value, list):
        return [wrap_card_data(v) for v in value]
    if isinstance(value, tuple):
        return tuple(wrap_card_data(v) for v in value)
    return value

async def render_html_to_png(html: str, out_path: str) -> bool:

    browser = await _get_browser()
    if browser is None:
        return False
    # device_scale_factor 必须是独立 kwarg：塞进 viewport 字典会被 Playwright 静默丢弃
    # （page.viewport_size 只返回 width/height），卡片退化为 1x 模糊图
    page = await browser.new_page(
        viewport={"width": 640, "height": 2200}, device_scale_factor=3
    )
    try:
        # 网络空闲（含远程封面图加载完成）；超时则退回 load 兜底
        # 封面 CDN 抖动时 load/networkidle 可能一直不触发：set_content 已同步写好 DOM，
        # 超时不该让整张卡片退化成纯文本——两段都吞掉异常继续截图（封面缺失但排版在），
        # 否则单张卡最长要卡 60s（30s networkidle + 30s load）。
        try:
            await page.set_content(html, wait_until="networkidle", timeout=12000)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await page.set_content(html, wait_until="load", timeout=8000)
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
                # set_viewport_size 只接受 width/height（缩放已在 context 层固定）
                await page.set_viewport_size(
                    {
                        "width": max(cur["width"], need_w),
                        "height": max(cur["height"], need_h),
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
        _remove_output(out_path)
        return False
    except BaseException:
        # 取消（插件重载/卸载）不落 except Exception：截图可能已经落盘，这里同样要删，
        # 否则返回路径既不上报也不登记，PNG 永久残留在 temp
        _remove_output(out_path)
        raise
    finally:
        # 截图已成功落盘后 page.close() 再报错不应把整张卡片判死：原先异常从 finally
        # 抛出会覆盖 return True，调用方拿到 None 又漏登记清理。这里吞掉关闭异常。
        with contextlib.suppress(Exception):
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
