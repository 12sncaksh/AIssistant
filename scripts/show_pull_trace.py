"""看一眼 pull 调用痕迹：**DSH 到底调没调 `search_memory`**。

用法：
    python scripts\\show_pull_trace.py
    python scripts\\show_pull_trace.py --limit 30
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pull_trace  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="打印最近几次")
    args = parser.parse_args()

    summary = pull_trace.summarize()
    print(f"痕迹文件：{summary['path']}")
    if not summary["total"]:
        print("还没有任何 pull 调用记录。")
        print("→ 如果模型该查却一直是空的，那就说明它**一次都没调**，问题在「没想到要查」，")
        print("  不在检索本身。")
        return 0

    print(f"总计 {summary['total']} 次，时间范围 {summary['first']} ~ {summary['last']}")
    for name, count in sorted(summary["channels"].items()):
        label = "MCP 子进程（桥接 / 外部 agent）" if name == "mcp" else "主程序内（本地工具循环）"
        print(f"  {name:6} {count:4} 次   {label}")

    print(f"\n最近 {args.limit} 次：")
    for entry in pull_trace.read_recent(args.limit):
        print(
            "  {ts}  [{channel}] pid={pid}  groups={groups} chars={chars}  err={err}  query={query}".format(
                ts=entry.get("ts", "?"),
                channel=entry.get("channel", "?"),
                pid=entry.get("pid", "?"),
                groups=entry.get("groups", "?"),
                chars=entry.get("chars", "?"),
                err=entry.get("error") or "-",
                query=entry.get("query", ""),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
