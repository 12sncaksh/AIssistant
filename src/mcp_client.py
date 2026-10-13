"""MCP（Model Context Protocol）客户端。

把外部 MCP server 提供的工具接进 Aissistant 的工具表：
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
import thread_registry


SERVER_READY_TIMEOUT = 120
CONFIG_FILENAME = "mcp.json"


# ---- method groups moved into their own modules ----
# TOOL_PREFIX / MAX_TOOL_NAME_LEN / CALL_TIMEOUT / MAX_RESULT_CHARS / _SAFE_CHARS /
# BUILTIN_SERVERS 定义在 mcp_runner：那里是它们的主要使用者，留在这里会让
# mcp_runner 反向 import 本模块，形成先导入 mcp_runner 就失败的循环依赖。
from mcp_runner import (  # noqa: F401
    BUILTIN_SERVERS,
    CALL_TIMEOUT,
    MAX_RESULT_CHARS,
    MAX_TOOL_NAME_LEN,
    TOOL_PREFIX,
    _SAFE_CHARS,
    runtime_dir,
    builtin_servers_dir,
    builtin_interpreter,
    builtin_server_specs,
    safe_tool_name,
    _schema_of,
    _convert_result,
    _spec_transport,
    _ServerRunner,
)


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
    def ensure_builtin_servers(cls) -> list:
        """把自带 server 的配置写回 config/mcp.json，返回新增或校正过的名字。

        为什么必须有这一步：ActionHandler 把 system_status / lock_screen / control_pet
        这些工具从内置表让给了 MCP，而 command/args 是逐机的。用户手抄模板很容易留下
        过时路径（模板里曾经写死过绝对路径），结果就是工具静默消失。
        这里只校正 command/args，从不改 enabled——界面上关掉的 server 不该被重新打开。
        """
        specs = builtin_server_specs()
        if not specs:
            return []

        path = cls.config_path()
        document = {"mcpServers": {}}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    loaded = json.load(handle)
            except (OSError, ValueError):
                logging.warning("读取 %s 失败，本次不改动它", path, exc_info=True)
                return []
            if not isinstance(loaded, dict):
                logging.warning("%s 顶层不是对象，本次不改动它", path)
                return []
            document = loaded

        servers = document.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            document["mcpServers"] = servers

        changed = []
        for name, spec in specs.items():
            current = servers.get(name)
            if not isinstance(current, dict):
                servers[name] = dict(spec)
                changed.append(name)
                continue
            if (
                current.get("command") != spec["command"]
                or list(current.get("args") or []) != spec["args"]
            ):
                current["command"] = spec["command"]
                current["args"] = list(spec["args"])
                changed.append(name)
        if not changed:
            return []

        temp_path = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_path, path)
        except OSError as exc:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            logging.warning("写入 %s 失败，自带 MCP server 配置未生效：%s", path, exc)
            return []

        with cls._lock:
            cls._config = None
        logging.info("已自动配置自带 MCP server：%s", "、".join(changed))
        return changed

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

    @staticmethod
    def _is_spec_enabled(spec: dict) -> bool:
        return (spec or {}).get("enabled") is not False

    @staticmethod
    def _has_endpoint(spec: dict) -> bool:
        return bool(
            str((spec or {}).get("command") or "").strip()
            or str((spec or {}).get("url") or "").strip()
        )

    @classmethod
    def is_server_enabled(cls, server_name: str) -> bool:
        spec = cls.load_config().get(str(server_name or ""))
        return bool(spec is not None and cls._is_spec_enabled(spec))

    @classmethod
    def set_server_enabled(cls, server_name: str, enabled: bool) -> None:
        """写回 mcp.json，并立即启动或停止对应 server。"""
        server_name = str(server_name or "").strip()
        if not server_name:
            raise ValueError("MCP server 名称不能为空")

        path = cls.config_path()
        temp_path = path + ".tmp"
        with cls._lock:
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    document = json.load(handle)
            except FileNotFoundError as exc:
                raise FileNotFoundError("找不到 MCP 配置文件：%s" % path) from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("无法读取 MCP 配置：%s" % exc) from exc

            servers = document.get("mcpServers") if isinstance(document, dict) else None
            spec = servers.get(server_name) if isinstance(servers, dict) else None
            if not isinstance(spec, dict):
                raise ValueError("MCP 配置中不存在 server：%s" % server_name)

            if enabled and not cls._has_endpoint(spec):
                raise ValueError("MCP server 缺少 command/url：%s" % server_name)

            spec["enabled"] = bool(enabled)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(document, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(temp_path, path)
            except OSError as exc:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
                raise OSError("写入 MCP 配置失败：%s" % exc) from exc

            cls._config = None
            cls.load_config()

        if enabled:
            cls.start_server(server_name)
        else:
            cls.stop_server(server_name)

    @classmethod
    def start_server(cls, server_name: str) -> None:
        """启动一个已配置且已启用的 server；重复调用不会创建第二个 runner。"""
        server_name = str(server_name or "").strip()
        with cls._lock:
            if cls._shutdown:
                raise RuntimeError("MCPManager 已关闭")
            spec = cls.load_config().get(server_name)
            if not isinstance(spec, dict):
                raise ValueError("MCP 配置中不存在 server：%s" % server_name)
            if not cls._is_spec_enabled(spec):
                raise ValueError("MCP server 尚未启用：%s" % server_name)
            if not cls._has_endpoint(spec):
                raise ValueError("MCP server 缺少 command/url：%s" % server_name)

            existing = cls._runners.get(server_name)
            if existing is not None and existing.is_alive():
                return
            if existing is not None:
                cls._runners.pop(server_name, None)

            runner = _ServerRunner(server_name, spec)
            cls._runners[server_name] = runner
            cls._started = True
            runner.start()

        thread_registry.spawn(
            cls._wait_all_ready,
            name="mcp-ready-watch-%s" % server_name,
            group="mcp_ready",
        )

    @classmethod
    def stop_server(cls, server_name: str) -> None:
        """立即移出工具表，在后台停止进程，避免阻塞 UI 线程。"""
        server_name = str(server_name or "").strip()
        with cls._lock:
            runner = cls._runners.pop(server_name, None)
            cls.refresh()
        if runner is not None:
            thread_registry.spawn(
                runner.stop, name="mcp-stop-%s" % server_name, group="mcp_stop"
            )

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
                thread_registry.spawn(
                    cls._wait_all_ready, name="mcp-ready-watch", group="mcp_ready"
                )

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
            servers = dict(cls.load_config())
            runners = dict(cls._runners)

        result = []
        for name, spec in servers.items():
            enabled = cls._is_spec_enabled(spec)
            runner = runners.get(name)
            error = str(runner.error) if runner is not None and runner.error else ""
            if enabled and not cls._has_endpoint(spec):
                error = "缺少 command/url"
            result.append({
                "name": name,
                "enabled": enabled,
                "running": runner is not None,
                "ready": bool(
                    enabled
                    and runner is not None
                    and runner.session is not None
                    and runner.error is None
                ),
                "tools": len(runner.tools) if runner is not None else 0,
                "error": error,
            })
        return result

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
