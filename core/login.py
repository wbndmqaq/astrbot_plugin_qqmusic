"""扫码登录：两条二维码通道 + 一套参数化轮询骨架。

``MusicService.start_poll``（App/MQTT 扫码通道）与 ``start_webqr_poll``（无感扫码
通道）原先各自维护一份 ~90% 逐行重复的轮询实现（``_spawn`` / ``_schedule`` /
``_tick`` / ``failStreak`` / ``finish_ok`` 全量复制），共有逻辑改一次要改两处、
分支细节一旦漂移就会各自出 bug（v2.0.3 修「扫码成功后所有回执丢失」时就发现
两份 ``_finish_ok`` 的差异点无法一眼看清）。故本次把这部分整块搬来此处并合并。

现在「取状态 → 判成功 → 成功回调 → 过期/失败文案 → 重排」只实现一次
（``_drive_poll``），两条通道各自只提供：

* ``poll_once``：取一次状态并处理该通道特有的分支，返回是否已终结本次轮询；
* ``baseline``（可选）：首拍之前要预取的基线状态。

两条通道的真实差异点（合并时必须保留的分支）：

* App/MQTT（``_qr_poll_once`` ↔ ``/login/qr/check``）：状态键 ``qrcodeID``；循环
  上限 ``_QR_MAX_SEC``（900s）；过期文案「请重新 #qqm登录」；轮询间隔可由调用方
  传入（``pollInterval``）；额外处理 ``scanUser`` 伪扫码过滤、``userMessage`` 逐条
  去重转发与「已被其它 APP 扫描」正则失效判定、``/login/qr/complete`` 兜底重试；
  并由 ``baseline`` 首拍前预取 ``/login/status`` 基线（扫码后 API 侧已登录的兜底判定）。
* 无感 webqr（``_webqr_poll_once`` ↔ ``/login/webqr/check``）：状态键 ``sessionId``；
  循环上限 ``_WEBQR_MAX_SEC``（180s）；过期文案「请重新扫码」；间隔固定
  ``_POLL_INTERVAL_SEC``；``status == "success"`` 且 ``hasKey`` 即判成功，
  ``expired`` / ``error`` 直接回 ``data.error``；无需基线预取。

命名注意（不要把这里读成 HEAD 的历史 bug）：合并过程中曾有一次中间态——骨架里引用
``msg.POLL_FAIL_MAX_HINT``，而通道钩子内仍用局部变量 ``msg`` 承接 ``userMessage``，
造成 ``UnboundLocalError`` 遮蔽。该遮蔽在合并完成时即已消除（钩子内改用
``user_message``）；**HEAD 版本并不存在这个 bug**：它那时的失败文案是硬编码字符串，
且代码里还没有 ``messages`` 模块，无遮蔽可谈。

公开方法名/签名与原 ``MusicService`` 完全一致，``MusicService`` 上保留薄委托。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api.message_components import Image

from . import api as qqapi
from . import cards as cardlib
from . import messages as msg

if TYPE_CHECKING:  # 仅类型注解（MusicService 反向委托到本模块，运行时不导入）
    from .service import MusicService

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)

# 登录轮询：默认间隔与「上一轮仍在执行」时的重排间隔（秒）
_POLL_INTERVAL_SEC = 2.0
_POLL_BUSY_RETRY_SEC = 0.8

# 两条通道各自的二维码有效期上限（秒）：App 码 15 分钟，无感码 3 分钟
_QR_MAX_SEC = 900
_WEBQR_MAX_SEC = 180

# 轮询连续失败多少次时提示一次 / 多少次后放弃
_FAIL_NOTIFY_AT = 5
_FAIL_ABORT_AT = 25


class PollContext:
    """一次轮询的共享上下文（骨架与通道钩子之间的唯一接口）。"""

    __slots__ = (
        "event",
        "max_sec",
        "service",
        "started",
        "state_key",
        "state_value",
        "task",
        "user_id",
        "user_key",
    )

    def __init__(
        self,
        service: MusicService,
        event,
        task: dict,
        *,
        user_id: str,
        user_key: str,
        started: float,
        max_sec: int,
        state_key: str,
        state_value: str,
    ) -> None:
        self.service = service
        self.event = event
        self.task = task
        self.user_id = user_id
        self.user_key = user_key
        self.started = started
        self.max_sec = max_sec
        self.state_key = state_key
        self.state_value = state_value

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    async def finish_ok(self, info) -> None:
        """登录成功终态：置停、取消定时器、注销任务、回报用户。"""
        task = self.task
        if task["stopped"]:
            return
        task["stopped"] = True
        timer = task.get("timer")
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.cancel()
        self.service.pop_task_if_current(self.user_id, task)
        await self.service.on_login_success(self.event, info)


# ──────────── 轮询骨架 ────────────


def _make_poll_context(
    service: MusicService,
    event,
    *,
    state_key: str,
    state_value: str,
    expires_in: int,
    max_sec: int,
    extra_task: dict,
) -> tuple[dict, PollContext]:
    """登记轮询任务并构造轮询上下文（_drive_poll 的装配段）。"""
    user_id = str(event.get_sender_id())
    task = {
        state_key: state_value,
        "stopped": False,
        "busy": False,
        "failStreak": 0,
        "jobs": [],
        "timer": None,
        **extra_task,
    }
    service.register_task(user_id, task)
    ctx = PollContext(
        service,
        event,
        task,
        user_id=user_id,
        user_key=user_id,
        started=time.time(),
        max_sec=min(expires_in, max_sec),
        state_key=state_key,
        state_value=state_value,
    )
    return task, ctx


def _make_poll_scheduler(task: dict, loop: asyncio.AbstractEventLoop, spawn):
    """返回轮询的 ``_schedule(delay)``：任意时刻只保留一个待触发定时器。

    旧句柄先取消再存 ``task["timer"]``，不进 ``jobs`` 列表（TimerHandle 不是 Task，
    触发后 ``cancelled()`` 仍为 False，混在 jobs 里会被 ``_prune_jobs`` 漏掉，
    本轮登录期间句柄只增不减）。
    """

    def _schedule(delay: float):
        old = task.get("timer")
        if old is not None:
            with contextlib.suppress(Exception):
                old.cancel()
        handle = loop.call_later(delay, spawn)
        task["timer"] = handle
        return handle

    return _schedule


async def _handle_poll_error(
    service: MusicService, event, task: dict, err: Exception, *, user_id: str
) -> bool:
    """轮询异常的统一处理：连败提示 → 连败上限终止。返回是否已终止本轮。"""
    task["failStreak"] += 1
    if task["failStreak"] == _FAIL_NOTIFY_AT:
        await service.reply(event, f"轮询暂时失败：{err}（继续重试）")
    if task["failStreak"] >= _FAIL_ABORT_AT:
        task["stopped"] = True
        service.pop_task_if_current(user_id, task)
        await service.reply(event, msg.POLL_FAIL_MAX_HINT)
        return True
    return False


def _drive_poll(
    service: MusicService,
    event,
    *,
    state_key: str,
    state_value: str,
    expires_in: int,
    max_sec: int,
    expire_text: str,
    extra_task: dict,
    poll_once,
    baseline=None,
) -> None:
    """参数化轮询骨架。

    ``poll_once(ctx) -> bool``：True 表示本次轮询已终结（成功/失败/过期内联处理），
    骨架不再重排定时器；False 表示继续。
    """
    user_id = str(event.get_sender_id())
    task, ctx = _make_poll_context(
        service,
        event,
        state_key=state_key,
        state_value=state_value,
        expires_in=expires_in,
        max_sec=max_sec,
        extra_task=extra_task,
    )
    loop = asyncio.get_running_loop()

    def _spawn():
        service._prune_jobs(task)
        t = service._spawn_bg(_tick())
        if t is None:
            return None
        task["jobs"].append(t)
        return t

    _schedule = _make_poll_scheduler(task, loop, _spawn)

    if baseline is not None:
        # 首拍之前预取一次登录态基线（用于「扫码后 API 侧已登录」的兜底判定）
        baseline_task = service._spawn_bg(baseline(ctx))
        if baseline_task is not None:
            task["jobs"].append(baseline_task)

    async def _tick():
        if task["stopped"]:
            return
        if task["busy"]:
            _schedule(_POLL_BUSY_RETRY_SEC)
            return
        # 必须用 ctx.max_sec（已按二维码真实有效期 expires_in 收敛）；
        # 读外层 max_sec 会忽略各通道更短的有效期
        if ctx.elapsed > ctx.max_sec:
            task["stopped"] = True
            service.pop_task_if_current(user_id, task)
            await service.reply(event, expire_text)
            return
        task["busy"] = True
        try:
            if await poll_once(ctx):
                return
        except Exception as err:
            stopped = await _handle_poll_error(service, event, task, err, user_id=user_id)
            if stopped:
                return
        finally:
            task["busy"] = False
        if (
            not task["stopped"]
            and service._active_logins.get(user_id, {}).get(state_key) == state_value
        ):
            task["timer"] = _schedule(task.get("pollInterval") or _POLL_INTERVAL_SEC)

    task["timer"] = _schedule(_POLL_INTERVAL_SEC)


# ──────────── 通道一：App/MQTT 扫码（/login/qr/*） ────────────


async def _qr_check_request(ctx: PollContext) -> dict:
    """上游 /login/qr/check 一跳。"""
    task = ctx.task
    is_first = task["pollCount"] == 0
    task["pollCount"] += 1
    return await qqapi.request(
        "/login/qr/check",
        {
            "qrcodeID": ctx.state_value,
            "elapsed": int(ctx.elapsed * 1000),
            "isFirstScan": 1 if is_first else 0,
            "completeTried": 1 if task["completeTried"] > 0 else 0,
        },
        "get",
        ctx.user_key,
    )


def _classify_scan_status(service: MusicService, task: dict, data: dict) -> str:
    """归一化本次扫码状态：过滤「scanUser 无真实用户」的伪扫码。"""
    status = data.get("status") or "wait"
    task["failStreak"] = 0
    scan_user = data.get("scanUser") or {}
    try:
        scan_uid = int(scan_user.get("userID") or 0)
    except (TypeError, ValueError):
        scan_uid = 0
    real_scan = bool(
        scan_uid > 0
        or scan_user.get("openid")
        or scan_user.get("musicId")
        or scan_user.get("uin")
    )
    if status in ("scanned", "confirmed") and not real_scan:
        service.log_info(
            f"忽略伪扫码状态（scanUser 无真实用户: {str(scan_user)[:60]}）"
        )
        status = "wait"
    if status in ("scanned", "confirmed") and not task["notifiedScan"]:
        task["notifiedScan"] = True
    return status


async def _handle_user_message(
    service: MusicService, ctx: PollContext, data: dict, task: dict
) -> bool:
    """转发上游 userMessage（去重）；二维码被其它 APP 扫描时终止本轮。

    返回 True 表示本轮已终结。
    """
    if not data.get("userMessage"):
        return False
    user_message = str(data["userMessage"])
    if task.get("lastUserMessage") != user_message:
        task["lastUserMessage"] = user_message
        await service.reply(ctx.event, user_message)
    if re.search(
        r"scanned by another|已被其他|其他.*扫描|二维码.*失效|已失效|\binvalid\b",
        user_message,
        re.IGNORECASE,
    ):
        task["stopped"] = True
        service.pop_task_if_current(ctx.user_id, task)
        await service.reply(
            ctx.event,
            "二维码已失效（可能被其它 APP 扫描），"
            "请重新发送 #qqm登录qq 获取新二维码",
        )
        return True
    return False


async def _try_complete(
    service: MusicService, ctx: PollContext, task: dict, status: str
) -> bool:
    """已扫码后的主动 complete 兜底（最多 2 次）：成功即终结本轮。"""
    if not (task["completeTried"] < 2 and status in ("scanned", "confirmed") and ctx.elapsed > 10):
        return False
    task["completeTried"] += 1
    try:
        done = await qqapi.request(
            "/login/qr/complete",
            {"qrcodeID": ctx.state_value},
            "post",
            ctx.user_key,
        )
        info = await service.pick_login_success(done or {})
        if info and info.get("ok") and info.get("hasKey"):
            await ctx.finish_ok(info)
            return True
    except Exception:
        pass
    return False


async def _try_status_baseline(service: MusicService, ctx: PollContext, task: dict) -> bool:
    """扫码 12s 后轮询 /login/status 兜底：基线发生变化即视为登录成功。

    覆盖「手机上已确认、但二维码轮询通道没回 success」的场景。
    """
    if not (task["notifiedScan"] and ctx.elapsed > 12 and task["statusBaseline"]):
        return False
    try:
        st = await qqapi.request("/login/status", {}, "get", ctx.user_key)
        d = (st or {}).get("data") or {}
        base = task["statusBaseline"]
        changed = (
            d.get("login")
            and d.get("uin")
            and d.get("hasKey")
            and (
                not base.get("login")
                or not base.get("hasKey")
                or str(d.get("uin")) != str(base.get("uin") or "")
            )
        )
        if changed:
            await ctx.finish_ok(
                {
                    "uin": d.get("uin"),
                    "nick": d.get("nick"),
                    "hasKey": d.get("hasKey"),
                    "channel": "status-poll",
                }
            )
            return True
    except Exception:
        pass
    return False


async def _qr_poll_once(ctx: PollContext) -> bool:
    service = ctx.service
    task = ctx.task
    body = await _qr_check_request(ctx)
    data = (body or {}).get("data") or {}
    status = _classify_scan_status(service, task, data)
    if await _handle_user_message(service, ctx, data, task):
        return True
    ok_info = await service.pick_login_success(body or {})
    if ok_info and ok_info.get("ok"):
        await ctx.finish_ok(ok_info)
        return True
    if status in ("expired", "cancel", "loginFailed"):
        task["stopped"] = True
        service.pop_task_if_current(ctx.user_id, task)
        await service.reply(
            ctx.event,
            "登录已取消或失败（二维码可能已过期），请重新 #qqm登录qq",
        )
        return True
    if await _try_complete(service, ctx, task, status):
        return True
    if await _try_status_baseline(service, ctx, task):
        return True
    return False


def start_qr_poll(
    service: MusicService,
    event,
    qrcode_id: str,
    expires_in: int,
    poll_interval: float = _POLL_INTERVAL_SEC,
) -> None:
    """App/MQTT 扫码通道轮询（公开入口：``MusicService.start_poll``）。"""
    user_key = str(event.get_sender_id())

    async def _baseline(ctx: PollContext):
        try:
            st = await qqapi.request("/login/status", {}, "get", user_key)
            d = (st or {}).get("data") or {}
            base = {
                "uin": str(d.get("uin") or ""),
                "hasKey": bool(d.get("hasKey")),
                "login": bool(d.get("login")),
            }
        except Exception:
            base = {"uin": "", "hasKey": False, "login": False}
        ctx.task["statusBaseline"] = base

    _drive_poll(
        service,
        event,
        state_key="qrcodeID",
        state_value=qrcode_id,
        expires_in=expires_in,
        max_sec=_QR_MAX_SEC,
        expire_text="二维码已过期，请重新 #qqm登录",
        extra_task={
            "busy": False,
            "completeTried": 0,
            "statusBaseline": None,
            "notifiedScan": False,
            "pollCount": 0,
            "pollInterval": poll_interval,
        },
        poll_once=_qr_poll_once,
        baseline=_baseline,
    )


# ──────────── 通道二：无感扫码（/login/webqr/*） ────────────


async def _webqr_poll_once(ctx: PollContext) -> bool:
    service, event, task = ctx.service, ctx.event, ctx.task
    body = await qqapi.request(
        "/login/webqr/check", {"sessionId": ctx.state_value}, "get", ctx.user_key
    )
    data = (body or {}).get("data") or {}
    status = data.get("status") or "wait"
    task["failStreak"] = 0
    if status == "success" and data.get("hasKey"):
        await ctx.finish_ok(
            {
                "uin": data.get("uin"),
                "nick": data.get("nick"),
                "hasKey": True,
                "channel": "webqr",
            }
        )
        return True
    if status in ("expired", "error"):
        task["stopped"] = True
        service.pop_task_if_current(ctx.user_id, task)
        await service.reply(
            event,
            data.get("error") or "二维码已过期或扫码失败，请重新扫码",
        )
        return True
    return False


def start_webqr_poll(
    service: MusicService, event, session_id: str, expires_in: int
) -> None:
    """无感扫码通道轮询（公开入口：``MusicService.start_webqr_poll``）。"""
    _drive_poll(
        service,
        event,
        state_key="sessionId",
        state_value=session_id,
        expires_in=expires_in,
        max_sec=_WEBQR_MAX_SEC,
        expire_text="二维码已过期，请重新扫码",
        extra_task={},
        poll_once=_webqr_poll_once,
    )


# ──────────── 任务登记与生命周期 ────────────


def stop_poll(service: MusicService, user_id: str) -> None:
    t = service._active_logins.get(str(user_id))
    if t:
        t["stopped"] = True
        timer = t.get("timer")
        if timer:
            with contextlib.suppress(Exception):
                timer.cancel()
        cancel_jobs(t)
        service._active_logins.pop(str(user_id), None)


def cancel_jobs(task: dict) -> None:
    """取消已创建但仍在运行的轮询协程/定时器，避免重载/登出后残留对旧 event 发消息。

    必须跳过当前正在执行的协程：登录成功等终态提示是在 ``_tick`` 内部 await 的，
    若把自身一起 cancel，紧跟其后的 await 会立刻抛 CancelledError，回执全部丢失。
    """
    current = asyncio.current_task()
    for job in task.get("jobs") or []:
        if job is None or job is current or not hasattr(job, "cancel"):
            continue
        try:
            job.cancel()
        except Exception:
            pass
    timer = task.get("timer")
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass


def prune_jobs(task: dict) -> None:
    """丢弃已结束的轮询协程引用：长轮询每 2s 产生一个 Task，不清理会一直堆积。

    定时器句柄不进 jobs 列表（存 task["timer"]，任意时刻最多一个），
    故这里只回收已完成的 Task。
    """
    jobs = task.get("jobs")
    if jobs:
        task["jobs"] = [
            j
            for j in jobs
            if j is not None and not (isinstance(j, asyncio.Task) and j.done())
        ]


def register_task(service: MusicService, user_id: str, task: dict) -> None:
    old = service._active_logins.get(user_id)
    if old is not None and old is not task:
        old["stopped"] = True
        t = old.get("timer")
        if t is not None:
            with contextlib.suppress(Exception):
                t.cancel()
        cancel_jobs(old)
    service._active_logins[user_id] = task


def pop_task_if_current(service: MusicService, user_id: str, task: dict) -> None:
    if service._active_logins.get(user_id) is task:
        cancel_jobs(task)
        service._active_logins.pop(user_id, None)


# ──────────── 登录结果判定与回执 ────────────


async def pick_login_success(body: dict) -> dict | None:
    """从登录接口返回体里判定「是否拿到可用登录态」，返回归一化后的账号信息。"""
    data = qqapi.unwrap_data(body)
    uin = data.get("uin") or ""
    has_key = bool(data.get("hasKey", data.get("qm_keyst")))
    nick = data.get("nick") or ""
    channel = data.get("channel") or ""
    ok = (
        (data.get("status") == "success" and (uin or has_key))
        or (uin and has_key is True)
        or (data.get("login") is True and uin and has_key)
    )
    if not ok:
        return None
    if not channel:
        channel = "mqtt" if data.get("status") == "success" else "status"
    return {
        "ok": True,
        **data,
        "uin": uin,
        "nick": nick,
        "hasKey": has_key,
        "channel": channel,
    }


async def on_login_success(service: MusicService, event, info: dict | None = None):
    """登录成功回执：文字摘要 + 状态卡片（渲染不可用时回退文本）。"""
    info = info or {}
    uin = info.get("uin") or ""
    nick = info.get("nick") or ""
    has_key = info.get("hasKey", True)
    user_key = service.user_key(event)
    meta = None
    try:
        meta = await qqapi.pull_login_meta(user_key)
    except Exception as e:
        service.log_warn(f"拉取登录态元信息失败: {e}")
    lines = ["✅ 登录成功"]
    if uin:
        lines.append(f"uin: {uin}")
    if nick:
        lines.append(f"昵称: {nick}")
    if has_key is False:
        lines.append("⚠️ 未拿到 key，付费曲可能仍无法播放")
    if meta is None:
        # 拉取失败 ≠ 没有 refresh：不能把「未知」说成「没有」（会误导用户重新扫码）
        lines.append("未能确认 refresh 材料（可 #qqm状态 复查）")
    elif meta.get("hasRefresh"):
        lines.append("含 refresh 材料，过期可自动续期")
    else:
        lines.append(msg.NO_REFRESH_WARN)
    lines.append("正在生成状态卡片…")
    await service.reply(event, "\n".join(lines))
    await asyncio.sleep(0.4)
    try:
        data = await service.build_status_data(user_key)
        url = await service.render_card(event, data, "qqmusic-status")
        if url and await service.send_chain(event, Image.fromFileSystem(url)):
            pass
        else:
            await service.reply(event, cardlib.format_status_text(data))
    except Exception as e:
        service.log_warn(f"登录后状态卡失败: {e}")
        await service.reply(
            event, "登录已成功，但状态卡渲染失败，可手动发送 #qqm状态"
        )


async def save_qr_image(base64_str: str) -> str:
    """二维码 base64 → 临时文件，返回路径（读盘走线程池）。"""
    import base64

    from .delivery import _write_bytes, get_temp_dir

    d = await get_temp_dir()
    os.makedirs(d, exist_ok=True)
    file_path = os.path.join(d, f"qr_{int(time.time() * 1000)}.png")
    await asyncio.to_thread(_write_bytes, file_path, base64.b64decode(base64_str))
    return file_path


__all__ = [
    "PollContext",
    "cancel_jobs",
    "on_login_success",
    "pick_login_success",
    "pop_task_if_current",
    "prune_jobs",
    "register_task",
    "save_qr_image",
    "start_qr_poll",
    "start_webqr_poll",
    "stop_poll",
]
