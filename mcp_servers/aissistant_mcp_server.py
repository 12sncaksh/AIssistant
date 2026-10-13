"""Aissistant 本地能力 MCP server（系统 / 生活服务，只读或低风险）。

工具实现全部复用 src/action_control.py 的 ActionHandler.execute_tool()，
这里只负责把工具定义暴露成 MCP 工具。

配置里建议 autoApprove: true —— 本组工具不修改系统状态。
"""

from __future__ import annotations

from aissistant_mcp_common import (
    ensure_src_on_path,
    load_tool_specs,
    register_tools,
    unwrap_tool_result,
)
from mcp.server.mcpserver import MCPServer

SERVER_NAME = "aissistant"

TOOL_NAMES = (
    "system_status",
    "health_check",
    "disk_scan",
    "network_info",
    "speedtest",
    "ping_host",
    "get_weather",
    "get_forecast",
    "get_route",
    "recommend_food_by_address",
    "recommend_food_by_city",
    "recommend_food_by_coordinates",
    "open_app",
    "open_url",
    # 记忆检索（pull）：让 DSH 侧那个 agent 自己决定要不要翻本地历史。
    # 它跑在独立进程里，靠 memory_index.db 里落库的"当前会话"定位该查哪个会话。
    "search_memory",
)

server = MCPServer(SERVER_NAME, version="1.0.0")


def _execute(tool_name: str, arguments: dict) -> str:
    ensure_src_on_path()
    from action_control import ActionHandler

    return unwrap_tool_result(ActionHandler.execute_tool(tool_name, arguments))


register_tools(server, load_tool_specs(TOOL_NAMES), _execute)


if __name__ == "__main__":
    server.run()
