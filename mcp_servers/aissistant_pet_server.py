"""Aissistant 桌宠控制 MCP server。

桌宠表情最终由主程序进程里的 Live2D 页面执行（window.__petEmotionSet），而 MCP server
是独立进程，拿不到主程序的回调对象。所以这里通过主程序暴露的本地 HTTP 接口转发：

    POST /v1/pet/emote    {emotion, intensity, duration_ms}
    POST /v1/pet/param    {parameters: [{id, value}], duration_ms}
    GET  /v1/pet/params?keyword=

接口监听的端口不是固定的（主程序从 8123 起顺延找可用端口），实际端口由主程序写进
data/runtime_endpoint.json，本 server 启动时读取。

工具实现仍然复用 action_control 的定义与校验逻辑，主程序侧调用的是同一个
execute_tool()，所以「桥接模式」和「本地直连模式」的行为完全一致。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from aissistant_mcp_common import (
    load_tool_specs,
    read_pet_endpoint,
    register_tools,
    unwrap_tool_result,
)
from mcp.server.mcpserver import MCPServer

SERVER_NAME = "aissistant-pet"

TOOL_NAMES = (
    "control_pet",
    "set_live2d_param",
    "list_live2d_params",
)

REQUEST_TIMEOUT_SECONDS = 10

server = MCPServer(SERVER_NAME, version="1.0.0")


def _call_app(method: str, path: str, payload: dict[str, Any] | None = None, query: dict[str, str] | None = None) -> dict:
    host, port = read_pet_endpoint()
    url = "http://%s:%d%s" % (host, port, path)
    if query:
        url += "?" + urllib.parse.urlencode(query)

    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        parsed = _parse(raw)
        if parsed is not None:
            return parsed
        return {"success": False, "message": "Aissistant 本地接口返回 HTTP %s：%s" % (exc.code, raw[:200])}
    except urllib.error.URLError as exc:
        return {
            "success": False,
            "message": (
                "连不上 Aissistant 本地接口 %s：%s。请确认 Aissistant 正在运行，"
                "且桌宠窗口/Live2D 页面资源正常（接口只在 Live2D 本地服务起来后才有）。"
                % (url, getattr(exc, "reason", exc))
            ),
        }
    except OSError as exc:
        return {"success": False, "message": "访问 Aissistant 本地接口失败：%s" % exc}

    parsed = _parse(raw)
    if parsed is None:
        return {"success": False, "message": "Aissistant 本地接口返回了非 JSON 内容：%s" % raw[:200]}
    return parsed


def _parse(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _execute(tool_name: str, arguments: dict) -> str:
    if tool_name == "control_pet":
        payload = {
            "emotion": arguments.get("emotion"),
            "intensity": arguments.get("intensity"),
            "duration_ms": arguments.get("duration_ms"),
        }
        return unwrap_tool_result(_call_app("POST", "/v1/pet/emote", payload))

    if tool_name == "set_live2d_param":
        payload = {
            "parameters": arguments.get("parameters"),
            "duration_ms": arguments.get("duration_ms"),
        }
        return unwrap_tool_result(_call_app("POST", "/v1/pet/param", payload))

    if tool_name == "list_live2d_params":
        keyword = arguments.get("keyword")
        query = {"keyword": "" if keyword is None else str(keyword)}
        return unwrap_tool_result(_call_app("GET", "/v1/pet/params", None, query))

    return "未知的桌宠工具：%s" % tool_name


register_tools(server, load_tool_specs(TOOL_NAMES), _execute)


if __name__ == "__main__":
    server.run()
