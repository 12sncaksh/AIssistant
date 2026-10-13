"""Sandbox / permission policy for tool execution.

Deliberately pure: every rule takes the state it needs (sandbox mode, workspace)
as an argument instead of reading ActionHandler's class attributes. ActionHandler
keeps the mutable state and delegates here, which makes the policy testable
without Qt, config files or a live agent.
"""

from __future__ import annotations

import logging
import os
import re

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX_MODE = "workspace-write"
FULL_ACCESS_MODE = "danger-full-access"
READ_ONLY_MODE = "read-only"

# read-only 沙箱下直接拒绝的状态改变类工具（只读工具不受影响）
READ_ONLY_BLOCKED_TOOLS = frozenset({
    "run_command", "write_file", "shutdown", "reboot", "lock_screen",
    "mouse_move", "mouse_click", "verified_mouse_click",
    "keyboard_type", "keyboard_hotkey",
})
READ_ONLY_REJECT_MESSAGE = (
    "当前处于「只读」权限模式，沙箱拒绝任何写入或状态改变操作，已拒绝执行 {tool}。"
    "需要执行时请在「设置 → 权限模式」里切换到工作区可写或完全权限模式。"
)

# 高风险/破坏性命令黑名单（不区分大小写匹配）
BLOCKED_COMMAND_PATTERNS = (
    # 递归/强制删除
    (r"Remove-Item\s+.*-(Recurse|R)\b", "命令包含递归删除操作（Remove-Item -Recurse），已阻止。"),
    (r"Remove-Item\s+.*-Force\b", "命令包含强制删除操作（Remove-Item -Force），已阻止。"),
    (r"\|\s*Remove-Item\b", "命令包含管道删除操作（| Remove-Item），已阻止。"),
    (r"rd\s+/s", "命令包含递归删除操作（rd /s），已阻止。"),
    (r"rmdir\s+/s", "命令包含递归删除操作（rmdir /s），已阻止。"),
    (r"\bdel\s+/(?:s|q|f)", "命令包含强制删除操作（del /s /q），已阻止。"),
    (r"\|\s*rm\b", "命令包含管道删除操作（| rm），已阻止。"),
    (r"Clear-RecycleBin", "命令包含清空回收站操作，已阻止。"),
    # 磁盘格式化 / 分区 / 擦除
    (r"Format-(?:Volume|Drive|Partition)\b", "命令包含磁盘格式化操作，已阻止。"),
    (r"\bformat\s+[a-zA-Z]:", "命令包含磁盘格式化操作，已阻止。"),
    (r"Clear-Disk\b", "命令包含磁盘清理操作，已阻止。"),
    (r"Initialize-Disk\b", "命令包含磁盘初始化操作，已阻止。"),
    (r"diskpart\b", "命令包含磁盘分区操作（diskpart），已阻止。"),
    (r"\bclean\s+all\b", "命令包含磁盘擦除操作，已阻止。"),
    (r"cipher\s+/w:", "命令包含磁盘擦除操作，已阻止。"),
    # 关机 / 重启
    (r"\bshutdown\b", "命令包含关机/重启操作，请改用专门的关机、重启工具。"),
    (r"\breboot\b", "命令包含关机/重启操作，请改用专门的关机、重启工具。"),
    (r"\bStop-Computer\b", "命令包含关机操作，请改用专门的关机工具。"),
    (r"\bRestart-Computer\b", "命令包含重启操作，请改用专门的重启工具。"),
    # 注册表
    (r"reg\s+delete\b", "命令包含注册表删除操作，已阻止。"),
    (r"reg\s+import\b", "命令包含注册表导入操作，已阻止。"),
    (r"regedit\s*/s", "命令包含静默导入注册表操作，已阻止。"),
    # 系统关键区域
    (r"takeown\s+/f\s+c:\\", "命令涉及系统关键目录所有权修改，已阻止。"),
    (r"icacls\s+.*/grant:r", "命令包含系统权限重置修改，已阻止。"),
    (r"vssadmin\s+delete\b", "命令包含卷影副本删除操作，已阻止。"),
    (r"bcdedit\b", "命令包含启动配置修改操作，已阻止。"),
    # 混淆 / 绕过
    (r"-EncodedCommand", "命令疑似使用编码混淆，已阻止。"),
)

# 禁止写入的受保护路径（不区分大小写，前缀匹配）
BLOCKED_WRITE_PATHS = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    "c:\\$recycle.bin",
    "c:\\recovery",
    "c:\\system volume information",
    "c:\\users\\default",
)


def normalize_mode(mode) -> str:
    """把任意输入收敛到合法沙箱模式；非法值回落到工作区可写。"""
    value = str(mode or "").strip().lower()
    return value if value in SANDBOX_MODES else DEFAULT_SANDBOX_MODE


def is_full_access(mode) -> bool:
    """完全权限模式：放开沙箱边界（工作区外的读写范围、子agent的工作区路径限制）。

    注意：破坏性命令黑名单（command_blocked）和受保护写入路径（BLOCKED_WRITE_PATHS、
    .git 目录、auth.json）不在此列，它们在所有权限模式下都拦截。
    """
    return normalize_mode(mode) == FULL_ACCESS_MODE


def is_read_only(mode) -> bool:
    return normalize_mode(mode) == READ_ONLY_MODE


def read_only_blocks_tool(mode, tool_name) -> bool:
    """只读模式下是否应拒绝该工具。"""
    return is_read_only(mode) and str(tool_name or "") in READ_ONLY_BLOCKED_TOOLS


def read_only_rejection(tool_name) -> str:
    return READ_ONLY_REJECT_MESSAGE.format(tool=str(tool_name or ""))


def command_blocked(command: str):
    """检查命令是否命中黑名单，命中返回拦截原因，否则返回 None。"""
    lower_cmd = (command or "").lower()
    if "remove-item" in lower_cmd and ("-recurse" in lower_cmd or "-force" in lower_cmd):
        return "命令包含递归/强制删除操作（Remove-Item -Recurse/-Force），已阻止。"
    if re.search(r"\brm\b", lower_cmd) and re.search(r"-(?:rf|fr|r|f|recurse|force)\b", lower_cmd):
        return "命令包含递归/强制删除操作（rm -rf），已阻止。"
    for pattern, reason in BLOCKED_COMMAND_PATTERNS:
        if re.search(pattern, lower_cmd, re.IGNORECASE):
            return reason
    return None


def outside_workspace_paths(command: str, workspace: str) -> list[str]:
    """提取命令中出现在工作区外的字面绝对路径（如 C: 盘符路径），用于限制子agent命令范围。"""
    ws_norm = os.path.normpath(workspace)
    found = []
    for match in re.finditer(r"[A-Za-z]:\\[^\s\"'|&;<>()]*", command or ""):
        raw = match.group(0).rstrip("\\/.,;:")
        if not raw:
            continue
        try:
            norm = os.path.normpath(raw)
        except Exception:
            logging.debug("路径规范化失败：%s", raw, exc_info=True)
            continue
        if norm == ws_norm or norm.startswith(ws_norm + os.sep):
            continue
        found.append(raw)
    return found


def resolve_agent_path(path: str, workspace, full_access: bool):
    """子agent工作区路径解析：有工作区时相对路径落到工作区内，工作区外绝对路径拒绝。

    返回 (解析后路径, 错误信息)；错误信息为 None 表示允许。
    """
    if not workspace:
        return path, None
    path = str(path or "").strip() or "."
    ws_norm = os.path.normpath(workspace)
    if not os.path.isabs(path):
        norm = os.path.normpath(os.path.join(workspace, path))
    else:
        norm = os.path.normpath(path)
    if norm == ws_norm or norm.startswith(ws_norm + os.sep):
        return norm, None
    if full_access:
        # 完全权限模式下沙箱不再限制子agent的访问范围（相对路径仍解析到工作区内）
        return norm, None
    return path, f"当前子agent的工作区被限制在 {workspace}，禁止访问工作区外的路径：{path}"
