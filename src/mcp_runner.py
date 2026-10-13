"""MCP server process runner and shared spec/schema helpers."""

from __future__ import annotations
import asyncio
import hashlib
import logging
import os
import re
import sys
import threading

# MCP 命名与超时常量放在这里（而不是 mcp_client）：本模块的 spec/schema 辅助函数
# 和 _ServerRunner 都要用，而 mcp_client 又要反过来 import 本模块的 runner。
# 常量留在 mcp_client 会形成双向依赖，先 import 本模块时就会 ImportError。
TOOL_PREFIX = "mcp__"
MAX_TOOL_NAME_LEN = 64
CALL_TIMEOUT = 180
# 单个工具结果进上下文前的硬上限：所有 MCP 工具共用，是防止某个工具一次性灌爆
# 上下文的兜底。它必须 ≥ memory_runtime.PULL_MAX_CHARS（30000）—— search_memory
# 已迁到 MCP，本地路径的结果要经过 _convert_result，比它长就会被截掉一截、还多一句
# "结果过长已截断"。其余自带工具输出的都是结构化短文本，抬高这个常量不会让它们变长。
MAX_RESULT_CHARS = 32000
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")

# ---- 自带 MCP server ----
# 名字 -> (脚本文件名, 是否免确认)。这三个 server 承载程序自身的一部分能力
# （见 ActionHandler.MCP_MIGRATED_TOOLS），路径必须按当前机器解析，所以配置由
# MCPManager.ensure_builtin_servers() 在启动时生成/校正，不写进配置模板。
BUILTIN_SERVERS = (
    ("aissistant", "aissistant_mcp_server.py", True),
    ("aissistant-power", "aissistant_power_server.py", False),
    ("aissistant-pet", "aissistant_pet_server.py", True),
)



def runtime_dir() -> str:
    """和 main.get_runtime_dir() 保持一致：冻结后是 exe 目录，源码运行时是项目根目录（src 的上一级）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def builtin_servers_dir() -> str:
    """自带 MCP server 脚本所在目录：<根目录>/mcp_servers。"""
    return os.path.join(runtime_dir(), "mcp_servers")


def builtin_interpreter() -> str:
    """用哪个解释器跑自带 server；拿不到可用解释器时返回空串。

    冻结（免安装版）下 sys.executable 就是 Aissistant.exe 本身，拿它当 command
    等于每个 server 再把整个 GUI 拉起来一次，所以这里必须返回空串。
    """
    if getattr(sys, "frozen", False):
        return ""
    executable = os.path.abspath(sys.executable or "")
    return executable if os.path.isfile(executable) else ""


def builtin_server_specs() -> dict:
    """算出本机自带 server 的正确配置；解释器或脚本缺失时整体返回空。"""
    interpreter = builtin_interpreter()
    if not interpreter:
        return {}
    script_dir = builtin_servers_dir()
    specs = {}
    for name, filename, auto_approve in BUILTIN_SERVERS:
        script = os.path.join(script_dir, filename)
        if not os.path.isfile(script):
            logging.warning("自带 MCP server 脚本缺失，本次跳过：%s", script)
            continue
        specs[name] = {
            "command": interpreter,
            "args": [script],
            "autoApprove": auto_approve,
            "dsh": True,
            "enabled": True,
            "_说明": "本程序自带能力的本地 MCP server，启动时自动生成/校正，一般不用手改。",
        }
    return specs


def safe_tool_name(server: str, tool: str) -> str:
    """拼出既好读、又不超长的工具名；超长时用短哈希保证唯一。"""
    raw = "%s%s__%s" % (TOOL_PREFIX, server, tool)
    name = _SAFE_CHARS.sub("_", raw)
    if len(name) <= MAX_TOOL_NAME_LEN:
        return name
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return name[: MAX_TOOL_NAME_LEN - 9] + "_" + digest


def _schema_of(tool) -> dict:
    for attr in ("inputSchema", "input_schema"):
        schema = getattr(tool, attr, None)
        if isinstance(schema, dict) and schema:
            return schema
    return {"type": "object", "properties": {}}


def _convert_result(result) -> dict:
    """把 CallToolResult 转成项目内部统一的 {success, message, data}。"""
    parts = []
    for block in getattr(result, "content", None) or []:
        kind = str(getattr(block, "type", "") or "").strip()
        if kind == "text":
            parts.append(str(getattr(block, "text", "") or ""))
        else:
            parts.append("[%s 类型内容，暂未展示]" % (kind or "未知"))
    text = "\n".join(p for p in parts if p).strip() or "（MCP 工具返回了空结果）"
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n…（结果过长已截断）"
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    data = {"tool_result": text}
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        data["structured"] = structured
    return {"success": not is_error, "message": text, "data": data}


def _spec_transport(spec: dict) -> str:
    """判断 server 的传输方式：stdio（默认）/ http（Streamable HTTP）/ sse。

    约定：写 "type": "http"|"sse" 显式指定；只写 url 时按 http 处理；否则 stdio。
    """
    kind = str((spec or {}).get("type") or "").strip().lower().replace("_", "-")
    if kind in ("http", "streamable-http", "streamablehttp"):
        return "http"
    if kind == "sse":
        return "sse"
    if str((spec or {}).get("url") or "").strip():
        return "http"
    return "stdio"


class _ServerRunner(threading.Thread):
    """一个 MCP server = 一个线程 + 一个常驻事件循环。"""

    def __init__(self, server_name: str, spec: dict):
        super().__init__(name="mcp-server-%s" % server_name, daemon=True)
        self.server_name = server_name
        self.spec = dict(spec or {})
        self.loop = None
        self.session = None
        self.tools = []
        self.error = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._errlog = None

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def call(self, tool_name: str, arguments: dict, timeout: float = CALL_TIMEOUT):
        if self.session is None or self.loop is None:
            raise RuntimeError(self.error or "MCP server 尚未就绪")
        future = asyncio.run_coroutine_threadsafe(
            self.session.call_tool(tool_name, arguments, read_timeout_seconds=timeout),
            self.loop,
        )
        return future.result(timeout=timeout + 15)

    def stop(self, timeout: float = 10) -> None:
        self._stop_requested.set()
        self.join(timeout)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception:
            logging.exception("MCP server %s 异常退出", self.server_name)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self.loop = None
            self.session = None
            self._ready.set()
            if self._errlog is not None:
                try:
                    self._errlog.close()
                except Exception:
                    pass
                self._errlog = None

    async def _serve(self) -> None:
        # mcp 的导入放在这里而不是模块顶部：它会连带拉进 pydantic/starlette/uvicorn，
        # 实测 import 一次要 ~750ms。没有配置任何 MCP server 时，这 0.75 秒不该让桌宠开机白等。
        transport = _spec_transport(self.spec)
        try:
            from mcp import ClientSession
            if transport == "stdio":
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client
            elif transport == "http":
                from mcp.client.streamable_http import streamable_http_client
                # 私有模块，但这是 SDK 官方给 HTTP 客户端配置 headers/超时的工厂；
                # streamable_http_client 本身不接受 headers，只能这样注入。
                from mcp.shared._httpx_utils import create_mcp_http_client
            else:
                from mcp.client.sse import sse_client
        except Exception as exc:
            self.error = exc
            logging.warning("%s 用不了 MCP 工具：没装 mcp 包（%s）", self.server_name, exc)
            self._ready.set()
            return

        self._errlog = self._open_errlog()
        try:
            if transport == "stdio":
                params = StdioServerParameters(
                    command=str(self.spec.get("command") or "").strip(),
                    args=[str(a) for a in (self.spec.get("args") or [])],
                    env={str(k): str(v) for k, v in (self.spec.get("env") or {}).items()} or None,
                    cwd=self.spec.get("cwd"),
                )
                async with stdio_client(params, errlog=self._errlog) as streams:
                    await self._drive_session(ClientSession, streams[0], streams[1])
            else:
                url = str(self.spec.get("url") or "").strip()
                if not url:
                    raise RuntimeError("远程 MCP server 缺少 url")
                headers = {
                    str(k): str(v) for k, v in (self.spec.get("headers") or {}).items()
                } or None
                if transport == "http":
                    async with create_mcp_http_client(headers=headers) as http_client:
                        async with streamable_http_client(url, http_client=http_client) as streams:
                            await self._drive_session(ClientSession, streams[0], streams[1])
                else:
                    async with sse_client(url, headers=headers) as streams:
                        await self._drive_session(ClientSession, streams[0], streams[1])
        except Exception as exc:
            self.error = exc
            logging.warning("MCP server %s 启动失败：%s: %s", self.server_name, type(exc).__name__, exc)
        finally:
            self.session = None
            self._ready.set()

    async def _drive_session(self, client_session_cls, read, write) -> None:
        """连上后初始化、拉取工具表并保持会话，直到收到停止请求。"""
        async with client_session_cls(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            self.tools = list(getattr(listed, "tools", None) or [])
            self.session = session
            logging.info(
                "MCP server %s 已就绪，提供 %d 个工具", self.server_name, len(self.tools)
            )
            self._ready.set()
            while not self._stop_requested.is_set():
                await asyncio.sleep(0.25)

    def _open_errlog(self):
        """server 的 stderr 落到 logs 下，方便排查；顺带避开冻结后 sys.stderr 为 None。"""
        try:
            log_dir = os.path.join(runtime_dir(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            return open(
                os.path.join(log_dir, "mcp_%s.log" % self.server_name),
                "a", encoding="utf-8", errors="replace",
            )
        except Exception:
            return open(os.devnull, "w", encoding="utf-8")
