"""MCP（Model Context Protocol）客户端。

把外部 MCP server 提供的工具接进 Aissist 的工具表：
- 工具名统一 mcp__<server>__<tool>，避免和内置工具撞名；
- 工具表在 ActionHandler.get_tool_definitions() 末尾合并；
- 调用在 APICallThread._execute_tool_call 里按前缀分发回来。

每个 server 一个后台线程跑常驻 asyncio 事件循环，会话一直开着；上层用同步的
call_tool() 拿结果，不必把 PyQt 的线程模型改成异步。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time


TOOL_PREFIX = "mcp__"
MAX_TOOL_NAME_LEN = 64
SERVER_READY_TIMEOUT = 120
CALL_TIMEOUT = 180
CONFIG_FILENAME = "mcp.json"
MAX_RESULT_CHARS = 20000
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def runtime_dir() -> str:
    """和 main.get_runtime_dir() 保持一致：冻结后是 exe 目录，源码运行时是项目根目录（src 的上一级）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


class MCPManager:
    """MCP server 注册表 + 工具表 + 调用分发。全部 classmethod，供工具表和执行层调用。"""

    _lock = threading.RLock()
    _runners = {}
    _tool_defs = []
    _tool_index = {}
    _config = None
    _started = False
    _shutdown = False

    # ---------- 配置 ----------
    @classmethod
    def config_path(cls) -> str:
        return os.path.join(runtime_dir(), "config", CONFIG_FILENAME)

    @classmethod
    def load_config(cls) -> dict:
        """读 mcp.json；只认 mcpServers 这一段，格式对齐 Claude Desktop。"""
        with cls._lock:
            if cls._config is not None:
                return cls._config
        servers = {}
        path = cls.config_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                raw = data.get("mcpServers") if isinstance(data, dict) else None
                if isinstance(raw, dict):
                    servers = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            logging.warning("读取 %s 失败，MCP 工具不可用", path, exc_info=True)
        with cls._lock:
            cls._config = servers
        return servers

    @classmethod
    def reload_config(cls) -> None:
        with cls._lock:
            cls._config = None

    # ---------- 启停 ----------
    @classmethod
    def ensure_started(cls) -> None:
        """第一次访问时把启用的 server 拉起来。非阻塞：工具随后就绪，不当场等。"""
        with cls._lock:
            if cls._started or cls._shutdown:
                return
            cls._started = True
            servers = cls.load_config()
            for name, spec in servers.items():
                if spec.get("enabled") is False:
                    continue
                if not str(spec.get("command") or "").strip() and not str(spec.get("url") or "").strip():
                    logging.warning("mcp.json 里的 %s 没写 command/url，已跳过", name)
                    continue
                runner = _ServerRunner(name, spec)
                cls._runners[name] = runner
                runner.start()
            if cls._runners:
                threading.Thread(
                    target=cls._wait_all_ready, name="mcp-ready-watch", daemon=True
                ).start()

    @classmethod
    def _wait_all_ready(cls) -> None:
        """盯住各 server 的就绪状态：一有新的 server 连上就立刻刷新工具表。

        不能等所有 server 都就绪再刷——一个坏掉的 server 会让好 server 的工具
        最长两分钟都看不见。"""
        deadline = time.time() + SERVER_READY_TIMEOUT
        last_live = -1
        while True:
            with cls._lock:
                runners = list(cls._runners.values())
            live = sum(1 for r in runners if r.session is not None)
            settled = all(r._ready.is_set() for r in runners)
            if live != last_live or settled:
                last_live = live
                cls.refresh()
            if settled or time.time() >= deadline:
                break
            time.sleep(0.3)
        cls.refresh()

    @classmethod
    def shutdown(cls) -> None:
        with cls._lock:
            runners = list(cls._runners.values())
            cls._runners = {}
            cls._tool_defs = []
            cls._tool_index = {}
            cls._shutdown = True
        for runner in runners:
            try:
                runner.stop()
            except Exception:
                logging.warning("关闭 MCP server %s 失败", runner.server_name, exc_info=True)

    # ---------- 工具表 ----------
    @classmethod
    def refresh(cls) -> list:
        """按当前已就绪的 server 重建工具表缓存。"""
        defs, index = [], {}
        with cls._lock:
            runners = list(cls._runners.items())
        for server_name, runner in runners:
            if runner.error is not None or runner.session is None:
                continue
            for tool in runner.tools:
                raw_name = str(getattr(tool, "name", "") or "").strip()
                if not raw_name:
                    continue
                exposed = safe_tool_name(server_name, raw_name)
                description = str(getattr(tool, "description", "") or "").strip()
                defs.append({
                    "type": "function",
                    "function": {
                        "name": exposed,
                        "description": (description or "MCP 工具 %s" % raw_name)[:1024],
                        "parameters": _schema_of(tool),
                    },
                })
                index[exposed] = (server_name, raw_name)
        with cls._lock:
            cls._tool_defs = defs
            cls._tool_index = index
        return defs

    @classmethod
    def get_tool_definitions(cls) -> list:
        """给工具表用的快照，永不阻塞。"""
        try:
            cls.ensure_started()
        except Exception:
            logging.warning("启动 MCP server 失败", exc_info=True)
        with cls._lock:
            return list(cls._tool_defs)

    # ---------- 调用 ----------
    @staticmethod
    def is_mcp_tool(tool_name: str) -> bool:
        return str(tool_name or "").startswith(TOOL_PREFIX)

    @classmethod
    def server_of(cls, tool_name: str) -> str:
        with cls._lock:
            entry = cls._tool_index.get(tool_name)
        return entry[0] if entry else ""

    @classmethod
    def needs_confirmation(cls, tool_name: str) -> bool:
        """默认每个 MCP 工具都要用户点头；某个 server 想免确认就在 mcp.json 里写 autoApprove。"""
        server = cls.server_of(tool_name)
        if not server:
            return True
        spec = cls.load_config().get(server) or {}
        return not bool(spec.get("autoApprove"))

    @classmethod
    def call_tool(cls, tool_name: str, arguments: dict) -> dict:
        with cls._lock:
            entry = cls._tool_index.get(tool_name)
            runner = cls._runners.get(entry[0]) if entry else None
        if not entry or runner is None:
            return {"success": False, "message": "未知的 MCP 工具：%s" % tool_name}
        server, raw_name = entry
        started = time.perf_counter()
        try:
            result = runner.call(raw_name, arguments if isinstance(arguments, dict) else {})
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logging.warning("MCP 工具 %s 调用失败（%dms）：%s", tool_name, elapsed, exc)
            return {
                "success": False,
                "message": "MCP 工具 %s 调用失败：%s" % (tool_name, exc),
                "data": {"server": server, "tool": raw_name, "elapsed_ms": elapsed},
            }
        payload = _convert_result(result)
        payload.setdefault("data", {})
        payload["data"].update({
            "server": server,
            "tool": raw_name,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })
        return payload

    # ---------- 状态 ----------
    @classmethod
    def status(cls) -> list:
        try:
            cls.ensure_started()
        except Exception:
            pass
        with cls._lock:
            return [
                {
                    "name": name,
                    "ready": runner.session is not None,
                    "tools": len(runner.tools),
                    "error": str(runner.error) if runner.error else "",
                }
                for name, runner in cls._runners.items()
            ]

    @classmethod
    def server_tools(cls, server_name: str) -> list:
        """返回某个 server 当前已就绪的工具（名称/描述/参数 schema），供 UI 只读展示。"""
        with cls._lock:
            index = list(cls._tool_index.items())
            defs = {d["function"]["name"]: d["function"] for d in cls._tool_defs}
        tools = []
        for exposed, entry in index:
            if not entry or entry[0] != server_name:
                continue
            fn = defs.get(exposed, {})
            tools.append({
                "name": exposed,
                "raw_name": entry[1],
                "description": str(fn.get("description", "") or ""),
                "parameters": fn.get("parameters", {}) or {},
            })
        return tools
