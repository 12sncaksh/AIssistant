"""Aissistant 高风险操作 MCP server（锁屏 / 关机 / 重启）。

单独成 server 的原因：MCP 的 autoApprove 是 server 级开关，不是工具级。
把这三个和只读工具放一起，就只能二选一——要么查系统状态也弹窗，要么关机不弹窗。

配置里必须保持 autoApprove: false（默认值），每次调用都要确认。
"""

from __future__ import annotations

from aissistant_mcp_common import (
    ensure_src_on_path,
    load_tool_specs,
    register_tools,
    unwrap_tool_result,
)
from mcp.server.mcpserver import MCPServer

SERVER_NAME = "aissistant-power"

TOOL_NAMES = (
    "lock_screen",
    "shutdown",
    "reboot",
)

server = MCPServer(SERVER_NAME, version="1.0.0")


def _execute(tool_name: str, arguments: dict) -> str:
    ensure_src_on_path()
    from action_control import ActionHandler

    return unwrap_tool_result(ActionHandler.execute_tool(tool_name, arguments))


register_tools(server, load_tool_specs(TOOL_NAMES), _execute)


if __name__ == "__main__":
    server.run()
