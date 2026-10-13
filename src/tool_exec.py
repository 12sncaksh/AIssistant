"""Command execution and its confirmation callback."""

from __future__ import annotations
import logging

# 必须定义在 _run_command 之前：它是默认参数值，在函数定义时就会求值。
COMMAND_TIMEOUT_SECONDS = 120


def set_agent_workspace(cls, workspace):
    """设置当前子agent的隔离工作区（None 表示主循环，不限制路径）。"""
    cls._agent_workspace = workspace


def set_timeout_confirm_callback(cls, callback):
    """设置命令超时续等确认回调：callback(command, elapsed, timeout) -> bool。"""
    cls._timeout_confirm_callback = callback


def _confirm_continue_waiting(cls, command, elapsed, timeout):
    """命令超时后询问用户是否继续等待；无回调时按原超时行为处理。"""
    callback = cls._timeout_confirm_callback
    if callback is None:
        return False
    try:
        return bool(callback(command, elapsed, timeout))
    except Exception:
        logging.warning("命令超时续等确认回调失败", exc_info=True)
        return False


def _run_command(command: str, timeout: int = COMMAND_TIMEOUT_SECONDS, cwd: str | None = None):
    """执行 PowerShell 命令，返回 (成功, 消息, 数据)。带超时和输出截断。

    破坏性命令黑名单在任何权限模式下都生效：这些命令正常干活基本用不到，只会用来搞破坏，
    属于保命拦截而非沙箱边界。完全权限模式（danger-full-access）放开的是子agent的工作区路径限制，
    以及工具层与审批层的其它沙箱约束。
    """
    blocked_reason = cls._check_command_blocked(command)
    if blocked_reason:
        return False, "已阻止危险命令：" + blocked_reason, None
    if not cls.is_full_access() and cwd:
        outside = cls._find_outside_workspace_paths(command, cwd)
        if outside:
            return False, (
                f"命令包含工作区外的路径（{outside[0]}）。子agent 被限制在 {cwd} 内执行命令，"
                "请改用相对路径或工作区内的路径。"
            ), None
    import subprocess
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return False, "命令执行失败: {e}".format(e=e), None
    waited = 0
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            break
        except subprocess.TimeoutExpired:
            waited += timeout
            if not cls._confirm_continue_waiting(command, waited, timeout):
                try:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                except Exception:
                    stdout, stderr = b"", b""
                return False, "命令执行超时（已等待 {waited} 秒且用户选择结束），已终止。".format(waited=waited), None
    raw = (stdout or b"") + ((b"\n" + stderr) if stderr else b"")
    output = None
    for enc in ("utf-8", "gbk"):
        try:
            output = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if output is None:
        output = raw.decode("utf-8", errors="replace")
    output = output.strip()
    if len(output) > 4000:
        output = output[:4000] + "\n" + "...（输出过长已截断）"
    exit_code = getattr(proc, "returncode", -1)
    msg = "命令已执行，退出码 {exit_code}。".format(exit_code=exit_code)
    if output:
        msg += "\n" + output
    else:
        msg += "（无输出）"
    return True, msg, {"exit_code": exit_code, "output": output}
