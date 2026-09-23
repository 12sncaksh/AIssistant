"""最小可用的 MCP server 示例（stdio）。

用途：不装任何第三方包也能验证 Aissist 的 MCP 链路是否通。
在 mcp.json 里把 demo 这段的 enabled 改成 true，重启 Aissist，
工具表里就会出现 mcp__demo__echo 和 mcp__demo__now。

注意：它依赖运行 Aissist 的那个 python 环境里装了 mcp 包；
打包后的 exe 本身不带 python，所以这个示例只在源码方式运行时可用。
"""

from datetime import datetime

from mcp.server.mcpserver import MCPServer

server = MCPServer("aissist-demo", version="1.0.0")


@server.tool(description="把传入的文字原样回显，用来确认 MCP 链路是否打通。")
def echo(text: str) -> str:
    return "echo: %s" % text


@server.tool(description="返回本机当前时间，格式 年-月-日 时:分:秒。")
def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    server.run()
