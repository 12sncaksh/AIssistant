"""列出 MCP server 状态与工具（验证用）。

用法（任意目录；建议在 conda 环境 ai_assistant 下运行）：
    python scripts\\list_mcp_tools.py

依赖：该环境已装 mcp 包（requirements.txt 中 mcp>=2.2.0）。
脚本会按 config/mcp.json 启动各 server，等工具就绪后打印名称、参数与描述，然后关闭。
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcp_client import MCPManager  # noqa: E402

TIMEOUT = 150


def main():
    MCPManager.ensure_started()

    deadline = time.time() + TIMEOUT
    tools = []
    while time.time() < deadline:
        tools = MCPManager.get_tool_definitions()
        if tools:
            break
        if any(s.get("error") for s in MCPManager.status()):
            break
        time.sleep(1)

    servers = MCPManager.status()
    print("配置文件: %s" % MCPManager.config_path())
    print("Server 数量: %d" % len(servers))
    for s in servers:
        state = "就绪" if s["ready"] else "未就绪"
        extra = ("  错误: %s" % s["error"]) if s["error"] else ""
        print("  - %s [%s] 工具数=%d%s" % (s["name"], state, s["tools"], extra))

    print("MCP 工具数量: %d" % len(tools))
    for d in tools:
        fn = d.get("function", {})
        desc = (fn.get("description") or "").replace("\n", " ")[:80]
        params = list((fn.get("parameters", {}).get("properties") or {}).keys())
        print("  - %s(%s)  %s" % (fn.get("name"), ", ".join(params), desc))

    MCPManager.shutdown()


if __name__ == "__main__":
    main()
