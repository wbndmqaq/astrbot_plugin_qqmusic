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
        with open(os.path.join(plugin_dir, "metadata.yaml"), "r", encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
        return str(meta.get("version", "?")).lstrip("v")
    except Exception:
        return "?"


async def _run_git(args: list[str], cwd: str, timeout: int = 120) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
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
    return proc.returncode or 0, stdout.decode("utf-8", "ignore"), stderr.decode("utf-8", "ignore")


def _mask_remote(url: str) -> str:
    return re.sub(r"//([^@]+)@", "//***@", url)


async def update_plugin(plugin_dir: str, *, force: bool = False) -> dict:
    if UPDATING["flag"]:
        return {"ok": False, "message": "已有更新任务进行中"}
    if not _is_git_repo(plugin_dir):
        return {"ok": False, "message": "当前插件目录不是 git 仓库，无法执行更新（请用 git clone 安装）"}

    UPDATING["flag"] = True
    try:
        old_commit = (await _run_git(["rev-parse", "--short", "HEAD"], plugin_dir, 10))[1].strip()
        old_version = get_local_version(plugin_dir)

        # fetch
        code, out, err = await _run_git(["fetch", "--all", "--prune"], plugin_dir, 180)
        if code != 0:
            return {"ok": False, "message": f"fetch 失败: {err.strip() or out.strip()}"}

        if force:
            # reset --hard 到远程
            for ref in ("origin/main", "origin/master"):
                code, out, err = await _run_git(["reset", "--hard", ref], plugin_dir, 60)
                if code == 0:
                    break
            await _run_git(["clean", "-fd", "-e", "config", "-e", "temp"], plugin_dir, 60)
        else:
            code, out, err = await _run_git(["pull", "--ff-only"], plugin_dir, 120)
            if code != 0:
                # 试 rebase
                code, out, err = await _run_git(["pull", "--rebase", "--autostash"], plugin_dir, 120)
                if code != 0:
                    return {"ok": False, "message": f"pull 失败: {err.strip() or out.strip()}"}

        new_commit = (await _run_git(["rev-parse", "--short", "HEAD"], plugin_dir, 10))[1].strip()
        new_version = get_local_version(plugin_dir)
        already = bool(re.search(r"Already up|已经是最新|up to date", out + err, re.I))

        if already:
            return {"ok": True, "already": True, "message": f"已是最新版本 v{new_version}（{new_commit}）"}

        # diff
        diff_code, diff_out, _ = await _run_git(["diff", "--name-only", f"{old_commit}..HEAD"], plugin_dir, 30)
        changed = bool(diff_out.strip())

        msg = f"更新完成：v{old_version} → v{new_version}\n提交: {old_commit} → {new_commit}"
        if changed:
            msg += f"\n变更文件:\n{diff_out.strip()[:500]}"
        return {"ok": True, "already": False, "message": msg, "oldVersion": old_version, "newVersion": new_version}
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
    commit = (await _run_git(["rev-parse", "--short", "HEAD"], plugin_dir, 10))[1].strip()
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
    return {"ok": True, "message": f"版本: v{version}  提交: {commit}\n\n{body}"}
