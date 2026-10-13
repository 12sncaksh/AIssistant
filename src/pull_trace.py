"""pull 调用的落盘痕迹：**跨进程**看得见"模型到底调没调 search_memory"。

为什么需要它
------------
`search_memory` 在桥接路径上是走 **MCP 子进程**执行的，而子进程的 `logging` 走
stderr，**不进主程序的 `logs/app.log`**。实测过：手动调一次、返回正常，
`logs/app.log` 里 `memory-pull-tool` 仍然是 **0 条**。

所以"DSH 到底调没调这个工具"用日志**永远查不出来**。这里另记一份独立痕迹。

为什么用独立文件、不用 memory_meta
---------------------------------
要留下 **query 原文**。"调了没有"只是一个数；而"它查的是什么"才决定那次调用
有没有意义 —— 一个查"你好"的调用和一个查"常驻块预算"的调用，价值完全不同。

写失败一律吞掉
-------------
痕迹是**观测手段**，绝不能因为它（磁盘满、权限、路径不存在）把检索本身弄挂。
所以 `record()` 只返回真假，从不抛异常。
"""

from __future__ import annotations

import json
import os
import sys
import time

# 单文件上限，超过就只留最后 KEEP_LINES 行（痕迹是排查用的，不是账本）
MAX_BYTES = 262144
KEEP_LINES = 200
# query 只留开头这么多字符：够判断意图，又不至于把长查询整段抄进去
QUERY_CHARS = 200

TRACE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "memory_pull.log"
)


def channel(argv=None):
    """调用来自哪条通道：MCP 子进程（桥接 / 外部 agent）还是主程序内。

    判据是 argv 里有没有 mcp_server 脚本名 —— 这是**启发式**，但足以把
    "桥上来的调用"和"本地工具循环里的调用"分开。
    """
    joined = " ".join(str(part) for part in (sys.argv if argv is None else argv))
    return "mcp" if "mcp_server" in joined else "local"


def record(event, path=None):
    """追加一行 JSON 痕迹。返回是否写成；**任何异常都吞掉**。"""
    target = path or TRACE_PATH
    payload = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
        "channel": channel(),
    }
    payload.update(event or {})
    try:
        folder = os.path.dirname(target)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _rotate(target)
        return True
    except Exception:
        return False


def _rotate(target):
    """超上限就把老行砍掉，保证文件有界。失败不影响刚写进去的那一行。"""
    try:
        if os.path.getsize(target) <= MAX_BYTES:
            return
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        with open(target, "w", encoding="utf-8") as handle:
            handle.writelines(lines[-KEEP_LINES:])
    except Exception:
        pass


def read_recent(limit=20, path=None):
    """读回最近若干条（坏行跳过，不抛）。"""
    target = path or TRACE_PATH
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return []
    entries = []
    for line in lines[-max(1, int(limit)) :]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def summarize(path=None):
    """排查用汇总：总次数、各通道次数、时间范围。"""
    target = path or TRACE_PATH
    entries = read_recent(10**6, target)
    channels = {}
    for entry in entries:
        key = str(entry.get("channel") or "?")
        channels[key] = channels.get(key, 0) + 1
    return {
        "path": target,
        "total": len(entries),
        "channels": channels,
        "first": entries[0].get("ts") if entries else "",
        "last": entries[-1].get("ts") if entries else "",
    }
