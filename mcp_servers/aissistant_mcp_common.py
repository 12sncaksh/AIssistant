"""Aissistant 本地 MCP server 的共享工具。

设计要点：
- 不 fork action_control.py：工具的 description / JSON Schema 全部取自
  ActionHandler 现有的 get_mcp_tool_definitions()，这里只做「JSON Schema → 函数签名」
  的翻译，再交给 mcp 2.x 的 MCPServer.add_tool()。
- 刻意不在 schema 里产生 $defs/$ref：嵌套对象的形状会被渲染成一句人话写进
  description。因为这份 schema 会同时流向 Aissistant 的 MCPManager 和 DSH 的
  dsh-mcp-client，扁平结构对两边都最安全。
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, create_model

from mcp.server.mcpserver.exceptions import ToolError

_MCP_SERVERS_DIR = os.path.dirname(os.path.abspath(__file__))
# 默认按脚本位置推断项目根；AISSIANT_ROOT 仅用于把 server 放到非标准位置时覆盖（测试用）。
PROJECT_ROOT = os.environ.get("AISSIANT_ROOT") or os.path.dirname(_MCP_SERVERS_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

PRIMITIVE_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def ensure_src_on_path() -> None:
    """让 server 能 import 到 src/ 下的 action_control。"""
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)


def load_tool_specs(names) -> list[dict[str, Any]]:
    """按名字取 action_control 里的工具定义（保持原顺序）。"""
    ensure_src_on_path()
    from action_control import ActionHandler

    wanted = list(names)
    index = {
        spec.get("function", {}).get("name"): spec
        for spec in ActionHandler.get_mcp_tool_definitions()
    }
    specs, missing = [], []
    for name in wanted:
        spec = index.get(name)
        if spec is None:
            missing.append(name)
        else:
            specs.append(spec)
    if missing:
        # 例如 open_app 只在 config/apps.json 有内容时才存在；缺失不是错误。
        print(
            "[aissistant-mcp] 以下工具在 action_control 里不存在，已跳过：%s" % "、".join(missing),
            file=sys.stderr,
        )
    return specs


def render_shape(schema: Any) -> str:
    """把嵌套对象/数组的形状渲染成一句人话，替代 $defs/$ref。"""
    if not isinstance(schema, dict):
        return ""
    kind = schema.get("type")
    if kind == "array":
        inner = render_shape(schema.get("items") or {})
        return ("每项：" + inner) if inner else ""
    if kind == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return ""
        required = set(schema.get("required") or [])
        parts = []
        for prop_name, prop_schema in properties.items():
            prop_schema = prop_schema if isinstance(prop_schema, dict) else {}
            type_name = str(prop_schema.get("type") or "any")
            description = str(prop_schema.get("description") or "").strip()
            mark = "" if prop_name in required else "，可选"
            parts.append(
                "%s(%s%s%s)" % (prop_name, type_name, mark, ("，" + description) if description else "")
            )
        return "、".join(parts)
    return ""


def schema_to_annotation(schema: Any, name: str) -> Any:
    """JSON Schema 片段 → Python 类型注解（对象一律降级为 dict，避免 $defs）。"""
    if not isinstance(schema, dict):
        return Any

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        try:
            return Literal[tuple(enum_values)]
        except TypeError:
            return str

    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict) and option.get("type") != "null":
                    return schema_to_annotation(option, name)

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), None)

    if kind == "array":
        return list[schema_to_annotation(schema.get("items") or {}, name + "_item")]
    if kind == "object":
        return dict[str, Any]
    return PRIMITIVE_TYPES.get(kind, Any)


def build_signature(parameters_schema: Any) -> inspect.Signature:
    """把工具定义里的 parameters 翻成函数签名，供 MCPServer 推导 inputSchema。"""
    schema = parameters_schema if isinstance(parameters_schema, dict) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = set(schema.get("required") or [])

    parameters = []
    for prop_name, prop_schema in properties.items():
        annotation = schema_to_annotation(prop_schema, prop_name)
        description = ""
        if isinstance(prop_schema, dict):
            description = str(prop_schema.get("description") or "").strip()
            shape = render_shape(prop_schema)
            if shape:
                if description and description[-1] in "。．.!！?？;；,，:：":
                    description = description + shape
                else:
                    description = (description + "；" + shape) if description else shape
        if description:
            annotation = Annotated[annotation, Field(description=description)]
        if prop_name in required:
            parameters.append(
                inspect.Parameter(prop_name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)
            )
        else:
            parameters.append(
                inspect.Parameter(
                    prop_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=None,
                )
            )
    return inspect.Signature(parameters=parameters)


def make_handler(tool_name: str, executor) -> Any:
    """生成一个把 **kwargs 转发给 executor 的函数，并挂上真实签名。"""

    def handler(**kwargs):
        arguments = {key: value for key, value in kwargs.items() if value is not None}
        return executor(tool_name, arguments)

    handler.__name__ = tool_name
    handler.__doc__ = None
    return handler


def register_tools(server, specs, executor) -> list[str]:
    """把 action_control 的工具定义注册到 MCPServer 上，返回成功注册的名字。"""
    registered = []
    for spec in specs:
        function = spec.get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        handler = make_handler(name, executor)
        handler.__signature__ = build_signature(function.get("parameters"))
        description = str(function.get("description") or "").strip() or name
        server.add_tool(handler, name=name, description=description, structured_output=False)
        registered.append(name)
    return registered


def unwrap_tool_result(result: Any) -> str:
    """action_control 的 {success, message, data} → MCP 文本；失败抛 ToolError。"""
    if not isinstance(result, dict):
        return str(result)
    message = str(result.get("message") or "").strip()
    if result.get("success"):
        return message or "完成。"
    raise ToolError(message or "工具执行失败。")


# ---------------- 桌宠本地接口 ----------------

ENDPOINT_FILENAME = "runtime_endpoint.json"


def endpoint_path() -> str:
    return os.path.join(PROJECT_ROOT, "data", ENDPOINT_FILENAME)


def read_pet_endpoint() -> tuple[str, int]:
    """读主程序写下的本地接口地址；读不到就给出可执行的提示。"""
    path = endpoint_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise ToolError(
            "找不到 %s：Aissistant 主程序没有在运行，或它启动时 Live2D 本地服务没起来。"
            "请先打开 Aissistant 桌宠窗口再试。" % path
        )
    except (OSError, ValueError) as exc:
        raise ToolError("读取 %s 失败：%s" % (path, exc))

    pet = document.get("pet") if isinstance(document, dict) else None
    pet = pet if isinstance(pet, dict) else {}
    host = str(pet.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(pet.get("port"))
    except (TypeError, ValueError):
        raise ToolError("%s 里的 pet.port 无效，请重启 Aissistant 主程序。" % path)
    return host, port
