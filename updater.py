from __future__ import annotations

import asyncio
import os
import re

UPDATING = {"flag": False}


def _is_git_repo(plugin_dir: str) -> bool:
    return os.path.isdir(os.path.join(plugin_dir, ".git"))


def get_local_version(plugin_dir: str) -> str:
    try:
        import yaml

        with open(
            os.path.join(plugin_dir, "metadata.yaml"), "r", encoding="utf-8"
        ) as f:
            meta = yaml.safe_load(f) or {}
        return str(meta.get("version", "?")).lstrip("v")
    except Exception:
        return "?"


async def _run_git(
    args: list[str], cwd: str, timeout: int = 120
) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "git 未安装"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "git 执行超时"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "ignore"),
        stderr.decode("utf-8", "ignore"),
    )


def _format_git_error(err: str, out: str = "") -> str:


    text = f"{err}\n{out}".strip()
    if not text:
        return text
    if re.search(
        r"Authentication failed|Permission denied|Could not read from remote|"
        r"invalid credentials|403|401",
        text,
        re.IGNORECASE,
    ):
        return "鉴权失败：请检查仓库访问凭证（token / SSH key）"
    if re.search(
        r"merge conflict|conflict|overwritten by merge|local changes would be overwritten",
        text,
        re.IGNORECASE,
    ):
        return "合并冲突：本地有修改与远程冲突，可备份后使用 #qqm强制更新"
    if re.search(r"\bdiverg", text, re.IGNORECASE):  # diverged / divergent
        return "本地与远程已分叉：可尝试 #qqm强制更新 覆盖本地"
    if re.search(r"not a git repository|不是 git 仓库", text, re.IGNORECASE):
        return "当前目录不是 git 仓库，无法更新（请用 git clone 安装）"
    if re.search(
        r"Could not resolve host|unable to access|Failed to connect|Timed out|"
        r"Could not read from remote",
        text,
        re.IGNORECASE,
    ):
        return "网络不通：无法连接远程仓库"
    return text


async def update_plugin(plugin_dir: str, *, force: bool = False) -> dict:
    if UPDATING["flag"]:
        return {"ok": False, "message": "已有更新任务进行中"}
    if not _is_git_repo(plugin_dir):
        return {
            "ok": False,
            "message": "当前插件目录不是 git 仓库，无法执行更新（请用 git clone 安装）",
        }

    UPDATING["flag"] = True
    try:
        old_commit = (await _run_git(["rev-parse", "--short", "HEAD"], plugin_dir, 10))[
            1
        ].strip()
        old_version = get_local_version(plugin_dir)

        # fetch
        code, out, err = await _run_git(["fetch", "--all", "--prune"], plugin_dir, 180)
        if code != 0:
            return {
                "ok": False,
                "message": f"fetch 失败: {_format_git_error(err, out)}",
            }

        if force:
            # 优先 reset 到当前分支上游（@{u}），失败再回退 main/master（对齐原版 update.js）
            _, upstream, _ = await _run_git(
                ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                plugin_dir,
                10,
            )
            refs = [upstream.strip()] if upstream.strip() else []
            refs += ["origin/main", "origin/master"]
            for ref in refs:
                code, out, err = await _run_git(
                    ["reset", "--hard", ref], plugin_dir, 60
                )
                if code == 0:
                    break
            if code != 0:
                return {
                    "ok": False,
                    "message": f"重置到远程分支失败: {_format_git_error(err, out)}",
                }
            code, out, err = await _run_git(
                ["clean", "-fd", "-e", "config", "-e", "temp"], plugin_dir, 60
            )
            if code != 0:
                return {
                    "ok": False,
                    "message": f"清理未跟踪文件失败: {_format_git_error(err, out)}",
                }
        else:
            code, out, err = await _run_git(["pull", "--ff-only"], plugin_dir, 120)
            if code != 0:
                # 试 rebase
                code, out, err = await _run_git(
                    ["pull", "--rebase", "--autostash"], plugin_dir, 120
                )
                if code != 0:
                    return {
                        "ok": False,
                        "message": f"pull 失败: {_format_git_error(err, out)}",
                    }

        new_commit = (await _run_git(["rev-parse", "--short", "HEAD"], plugin_dir, 10))[
            1
        ].strip()
        new_version = get_local_version(plugin_dir)
        # commit 未变也视为已是最新（rebase 走通但无变更时避免误报"更新完成"）
        already = old_commit == new_commit or bool(
            re.search(r"Already up|已经是最新|up to date", out + err, re.IGNORECASE)
        )

        if already:
            return {
                "ok": True,
                "already": True,
                "message": f"已是最新版本 v{new_version}（{new_commit}）",
            }

        # diff
        _, diff_out, _ = await _run_git(
            ["diff", "--name-only", f"{old_commit}..HEAD"], plugin_dir, 30
        )
        changed = bool(diff_out.strip())

        _, branch_out, _ = await _run_git(["branch", "--show-current"], plugin_dir, 10)
        branch = branch_out.strip() or "?"
        _, remote_out, _ = await _run_git(
            ["remote", "get-url", "origin"], plugin_dir, 10
        )
        remote_url = re.sub(r"//([^@/]+)@", "//***@", remote_out.strip())

        msg = f"更新完成：v{old_version} → v{new_version}\n提交: {old_commit} → {new_commit}"
        msg += f"\n分支: {branch}  仓库: {remote_url or '未知'}"
        if changed:
            msg += f"\n变更文件:\n{diff_out.strip()[:500]}"
        if re.search(r"(^|\n)requirements\.txt($|\n)", diff_out):
            msg += (
                "\n⚠️ requirements.txt 有变更，"
                "请重启 AstrBot 或手动执行 pip install -r requirements.txt"
            )
        msg += "\n重启 AstrBot 使插件代码生效"
        return {
            "ok": True,
            "already": False,
            "message": msg,
            "oldVersion": old_version,
            "newVersion": new_version,
        }
    finally:
        UPDATING["flag"] = False


async def get_update_log(plugin_dir: str, *, limit: int = 15, since: str = "") -> dict:
    if not _is_git_repo(plugin_dir):
        return {"ok": False, "message": "非 git 仓库，无法获取更新日志"}
    args = ["log", f"-{limit}", "--pretty=%h||%cd||%s", "--date=format:%F %T"]
    if since:
        args += [f"{since}..HEAD"]
    code, out, err = await _run_git(args, plugin_dir, 30)
    if code != 0:
        return {"ok": False, "message": f"获取日志失败: {err.strip()}"}
    version = get_local_version(plugin_dir)
    commit = (await _run_git(["rev-parse", "--short", "HEAD"], plugin_dir, 10))[
        1
    ].strip()
    lines = []
    for row in out.splitlines():
        if not row.strip():
            continue
        parts = row.split("||", 2)
        if len(parts) != 3:
            continue
        h, cd, s = parts
        if re.search(r"Merge branch|Merge pull request", s):
            continue
        lines.append(f"{h} {cd}\n  {s}")
    body = "\n\n".join(lines) if lines else "暂无提交"
    _, remote_out, _ = await _run_git(["remote", "get-url", "origin"], plugin_dir, 10)
    remote_url = re.sub(r"//([^@/]+)@", "//***@", remote_out.strip())
    return {
        "ok": True,
        "message": f"版本: v{version}  提交: {commit}\n仓库: {remote_url or '未知'}\n\n{body}",
    }
