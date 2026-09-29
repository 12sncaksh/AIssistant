"""DSH（DeepSeek Harness）ACP 桥接客户端。

通过 Agent Client Protocol（JSON-RPC over stdio）驱动本机的 `dsh --profile acp`
进程，并对外暴露与 APICallThread 完全一致的 Qt 信号，使界面层可以零改动复用。

设计边界（与路线图一致）：
- DSH 负责 Agent 循环、模型、会话、工具执行；
- Aissistant 负责桌面呈现（桌宠 / Live2D / TTS / 气泡）与本地交互；
- 桥接开启时本地工具循环不参与，避免两边各跑一套。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time

from PyQt6.QtCore import QThread, pyqtSignal

PROTOCOL_VERSION = 1
DEFAULT_COMMAND = "dsh"
DEFAULT_PROFILE = "acp"
PROMPT_TIMEOUT_SECONDS = 1800
PERMISSION_TIMEOUT_SECONDS = 300
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_DIRNAME = "agent_workspace"
SESSION_MAP_FILENAME = "dsh_sessions.json"
HISTORY_SEED_TURNS = 5
HISTORY_SEED_CHARS = 600


MCP_CONFIG_FILENAME = "mcp.json"


def _resolve_stdio_command(command):
    """把 mcp.json 里的 command 转成绝对路径。

    dsh-acp 会校验 mcpServers[].command 必须是绝对路径，相对路径直接抛
    AcpMcpConfigError；而且它把 failOnStartupError 写死为 true——任何一个 server 起不来
    都会让整个 DSH 会话建立失败。所以这里解析不出来就跳过该 server，绝不硬塞进去。
    """
    command = str(command or "").strip()
    if not command:
        return ""
    if os.path.isabs(command):
        return command if os.path.exists(command) else ""
    found = shutil.which(command)
    return found or ""


def build_acp_mcp_servers(config=None):
    """把 config/mcp.json 里「显式声明 dsh: true」的 stdio server 翻译成 ACP 形状。

    为什么只取 dsh: true 的：config/mcp.json 里的 server 是给本程序自己的 MCPManager 用的，
    其中可能有需要联网下载（npx/uvx）或依赖本机 node 的条目。整份透传给 DSH 的话，
    任何一个起不来都会连带整个桥接会话建不起来。
    ACP 侧只认 name/command/args/env，且 env 必须是 [{name,value}] 列表，不能是字典。
    """
    path = os.path.join(PROJECT_ROOT, "config", MCP_CONFIG_FILENAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logging.warning("读取 %s 失败，本次 DSH 会话不附带任何 MCP server", path, exc_info=True)
        return []

    servers = document.get("mcpServers") if isinstance(document, dict) else None
    if not isinstance(servers, dict):
        return []

    result = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        if not spec.get("dsh"):
            continue
        if spec.get("enabled") is False:
            continue
        if str(spec.get("type") or "").strip().lower():
            logging.info("DSH 桥接暂不支持非 stdio 的 MCP server，已跳过：%s", name)
            continue
        command = _resolve_stdio_command(spec.get("command"))
        if not command:
            logging.warning(
                "MCP server %s 的 command 无法解析成绝对路径，已跳过（否则会拖垮整个 DSH 会话）", name
            )
            continue
        raw_env = spec.get("env")
        env = []
        if isinstance(raw_env, dict):
            env = [{"name": str(key), "value": str(value)} for key, value in raw_env.items()]
        args = [str(item) for item in (spec.get("args") or [])]
        result.append({"name": str(name), "command": command, "args": args, "env": env})

    if result:
        logging.info("DSH 桥接将附带 %d 个 MCP server：%s", len(result), "、".join(item["name"] for item in result))
    return result


def default_workspace():
    """DSH 的工作目录固定在 <项目根>/agent_workspace，不存在时自动创建。"""
    path = os.path.join(PROJECT_ROOT, WORKSPACE_DIRNAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return PROJECT_ROOT
    return path


def default_session_map_path():
    """Aissistant 会话 -> DSH 会话 的映射文件：config/dsh_sessions.json。"""
    return os.path.join(PROJECT_ROOT, "config", SESSION_MAP_FILENAME)


def _message_text(content):
    """从 OpenAI 风格的消息内容里取出纯文本，供历史背景摘录使用。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                parts.append(str(part["text"]))
        return "\n".join(parts)
    return ""


class DshAcpError(RuntimeError):
    """桥接层自身的失败：进程启动、协议错误或超时。"""


def _data_url_to_image_block(url):
    """把 data:image/...;base64,xxx 转成 ACP 的 image 内容块。"""
    if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
        return None
    header, payload = url.split(",", 1)
    mime = header[5:].split(";", 1)[0].strip() or "image/png"
    payload = payload.strip()
    if not payload:
        return None
    return {"type": "image", "data": payload, "mimeType": mime}


def _tool_title(tool_call):
    if not isinstance(tool_call, dict):
        return "工具调用"
    return str(tool_call.get("title") or tool_call.get("toolCallId") or "工具调用")


def _tool_arguments(tool_call):
    """从 ACP 的 toolCall 里尽量取出可展示的参数，供授权弹窗使用。"""
    if not isinstance(tool_call, dict):
        return {}
    for key in ("rawInput", "input", "arguments"):
        value = tool_call.get(key)
        if isinstance(value, dict) and value:
            return value
    locations = tool_call.get("locations")
    if isinstance(locations, list) and locations:
        return {"locations": locations}
    return {}


def build_prompt_blocks(content, fallback_text=""):
    """把 Aissistant 的消息内容转成 ACP 的 prompt 内容块。"""
    if isinstance(content, list):
        blocks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text" and part.get("text"):
                blocks.append({"type": "text", "text": str(part["text"])})
            elif kind == "image_url":
                image = _data_url_to_image_block((part.get("image_url") or {}).get("url"))
                if image is not None:
                    blocks.append(image)
        if blocks:
            return blocks
        return [{"type": "text", "text": str(fallback_text or "")}]
    return [{"type": "text", "text": str(content or fallback_text or "")}]


def sync_launch_overlay(config, root=None):
    """把 Aissistant 侧配置的路由写进 DSH 启动 overlay，返回文件路径。

    为什么需要：`~/.dsh/profiles/acp` 自带的 patch 会把 ACP 路由钉死在某个 provider 上，
    与用户在 DSH 里选的内网网关不一致，于是网关 Key 被拿去请求公网 API 而认证失败。
    这里用 `dsh --patch <文件>` 在 profile 层之上覆盖回用户配置的路由。
    未配置 provider 时返回空串，表示不改动 DSH 原有路由。
    """
    provider = str(getattr(config, "get_dsh_provider", lambda: "")() or "").strip() if config is not None else ""
    if not provider:
        return ""
    model = str(getattr(config, "get_dsh_model", lambda: "")() or "").strip() if config is not None else ""
    lines = [
        "# Generated by Aissistant: override the ACP route back to the provider/model",
        "# selected in DSH settings, instead of the one pinned by the profile patch.",
        "- id: acp",
        "  config:",
        "    provider: " + provider,
    ]
    if model:
        lines.append("    model: " + model)
    path = os.path.join(root or PROJECT_ROOT, "config", "dsh_acp_overlay.yml")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        return ""
    return path


class SessionMapStore:
    """Aissistant 会话 id -> DSH sessionId 的持久映射，重启后可继续恢复会话。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in data.items()
            if isinstance(value, str) and value
        }

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temp_path = self.path + ".tmp"
            with open(temp_path, "w", encoding="utf-8", newline="") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temp_path, self.path)
        except OSError:
            pass

    def get(self, key):
        with self._lock:
            return self._data.get(str(key or ""))

    def set(self, key, session_id):
        key = str(key or "")
        if not key or not session_id:
            return
        with self._lock:
            self._data[key] = str(session_id)
            self._save()

    def pop(self, key):
        with self._lock:
            value = self._data.pop(str(key or ""), None)
            if value is not None:
                self._save()
            return value


class DshAcpConnection:
    """持有一个 `dsh --profile acp` 子进程与其 JSON-RPC 管道（进程内单例）。"""

    def __init__(self, command=DEFAULT_COMMAND, profile=DEFAULT_PROFILE, cwd=None, overlay_path=""):
        self.command = command or DEFAULT_COMMAND
        self.profile = profile or DEFAULT_PROFILE
        self.cwd = cwd or default_workspace()
        self.overlay_path = overlay_path or ""
        self.image_prompt_enabled = False
        self._proc = None
        self._next_id = 0
        self._pending = {}
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._init_lock = threading.RLock()
        self._handler_lock = threading.RLock()
        self._initialized = False
        self._sessions = {}
        self._session_origin = {}
        self._session_handlers = {}
        self._store = SessionMapStore(default_session_map_path())
        self._stderr_tail = []
        self.update_handler = None
        self.permission_handler = None
        self.status_handler = None

    # ---------------- 进程与传输 ----------------

    def _spawn(self):
        argv = [self.command, "--profile", self.profile]
        if self.overlay_path:
            argv += ["--patch", self.overlay_path]
        if os.name == "nt":
            argv = ["cmd", "/c"] + argv
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
            )
        except OSError as exc:
            raise DshAcpError(f"无法启动 DSH（{self.command}）：{exc}") from exc
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _notify_status(self, text, session_id=None):
        """状态提示按会话分流，避免后台会话占住当前状态栏。"""
        handler = self._handler_for(session_id)
        listener = getattr(handler, "handle_status", None) if handler is not None else self.status_handler
        if listener is None:
            return
        try:
            listener(text)
        except Exception:
            pass

    def _pump_stdout(self):
        stream = self._proc.stdout
        if stream is None:
            return
        for raw in stream:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            try:
                self._dispatch(message)
            except Exception as exc:  # 读取线程不能因为单条消息崩掉
                self._notify_status(f"DSH 消息处理异常：{exc}")

    def _pump_stderr(self):
        stream = self._proc.stderr
        if stream is None:
            return
        for raw in stream:
            line = raw.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            del self._stderr_tail[:-20]

    def _handler_for(self, session_id):
        with self._handler_lock:
            return self._session_handlers.get(str(session_id or ""))

    def register_session_handler(self, session_id, handler):
        """把某个 DSH 会话的事件绑定到对应界面线程，实现多会话并行不串台。"""
        if not session_id:
            return
        with self._handler_lock:
            self._session_handlers[str(session_id)] = handler

    def unregister_session_handler(self, session_id):
        with self._handler_lock:
            self._session_handlers.pop(str(session_id or ""), None)

    def _notify_update(self, params):
        handler = self._handler_for(params.get("sessionId"))
        listener = getattr(handler, "handle_session_update", None) if handler is not None else None
        if listener is None:
            listener = self.update_handler
        if listener is None:
            return
        try:
            listener(params)
        except Exception as exc:
            self._notify_status(f"DSH 事件处理异常：{exc}")

    def _dispatch(self, message):
        if "id" not in message:
            if message.get("method") == "session/update":
                self._notify_update(message.get("params") or {})
            return
        if "method" in message:
            threading.Thread(
                target=self._answer_reverse_request, args=(message,), daemon=True
            ).start()
            return
        waiter = self._pending.pop(message.get("id"), None)
        if waiter is not None:
            waiter.put(message)

    def _reply(self, request_id, result):
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _reply_error(self, request_id, message):
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": message},
            }
        )

    def _answer_reverse_request(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        if method == "session/request_permission":
            handler = self._handler_for(params.get("sessionId"))
            listener = getattr(handler, "handle_permission_request", None) if handler is not None else None
            if listener is None:
                listener = self.permission_handler
            approved = False
            if listener is not None:
                try:
                    approved = bool(listener(params))
                except Exception:
                    approved = False
            result = {"outcome": {"outcome": "cancelled"}}
            if approved:
                options = params.get("options") or []
                picked = next(
                    (o for o in options if str(o.get("kind", "")).startswith("allow")), None
                )
                if picked is not None and picked.get("optionId"):
                    result = {
                        "outcome": {"outcome": "selected", "optionId": picked["optionId"]}
                    }
            self._reply(message["id"], result)
            return
        self._reply_error(message["id"], f"Aissistant 桥接未实现客户端方法：{method}")

    def _send(self, message):
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise DshAcpError("DSH 进程尚未启动")
        with self._write_lock:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()

    def request(self, method, params, timeout=PROMPT_TIMEOUT_SECONDS):
        self._next_id += 1
        request_id = self._next_id
        waiter = queue.Queue()
        self._pending[request_id] = waiter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            message = waiter.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(request_id, None)
            raise DshAcpError(f"{method} 超时（{timeout}s 无响应）") from None
        if "error" in message:
            detail = message["error"]
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
            raise DshAcpError(f"DSH {method} 失败：{detail}")
        return message.get("result") or {}

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ---------------- 生命周期 ----------------

    def ensure_started(self):
        with self._state_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._initialized = False
            self.image_prompt_enabled = False
            self._sessions = {}
            self._session_origin = {}
            with self._handler_lock:
                self._session_handlers = {}
            self._spawn()

    def ensure_initialized(self):
        with self._init_lock:
            self.ensure_started()
            if self._initialized:
                return
            result = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
                timeout=120,
            )
            capabilities = (result.get("agentCapabilities") or {}).get("promptCapabilities") or {}
            self.image_prompt_enabled = bool(capabilities.get("image"))
            self._initialized = True

    def ensure_session(self, session_key=None):
        """一个 Aissistant 会话对应一个 DSH 会话，避免不同对话共用上下文。"""
        self.ensure_initialized()
        key = session_key or ""
        cached = self._sessions.get(key)
        if cached:
            self._session_origin[key] = "reused"
            return cached

        persisted = self._store.get(key)
        if persisted and self._resume_session(persisted):
            self._sessions[key] = persisted
            self._session_origin[key] = "resumed"
            self._notify_status("已恢复上次的 DSH 会话上下文")
            return persisted
        if persisted:
            self._store.pop(key)
            self._notify_status("上次的 DSH 会话已不可恢复，将新建会话")

        result = self.request(
            "session/new",
            {"cwd": self.cwd, "mcpServers": build_acp_mcp_servers()},
            timeout=180,
        )
        session_id = result.get("sessionId")
        if not session_id:
            raise DshAcpError("DSH 未返回 sessionId，无法建立会话")
        self._sessions[key] = session_id
        self._session_origin[key] = "created"
        self._store.set(key, session_id)
        return session_id

    def _resume_session(self, session_id):
        """尝试恢复已持久化的 DSH 会话；不可恢复或 cwd 变更时返回 False。"""
        try:
            self.request(
                "session/resume",
                {
                    "sessionId": session_id,
                    "cwd": self.cwd,
                    "mcpServers": build_acp_mcp_servers(),
                },
                timeout=180,
            )
        except DshAcpError:
            return False
        return True

    def session_origin(self, session_key=None):
        """最近一次 ensure_session 的结果：created / resumed / reused。"""
        return self._session_origin.get(str(session_key or ""), "")

    def prompt(self, blocks, session_key=None):
        session_id = self.ensure_session(session_key)
        return self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": blocks},
            timeout=PROMPT_TIMEOUT_SECONDS,
        )

    def cancel(self, session_key=None):
        session_id = self._sessions.get(session_key or "")
        if not session_id:
            return
        try:
            self.notify("session/cancel", {"sessionId": session_id})
        except Exception:
            pass

    def forget_session(self, session_key):
        """丢弃某个本地会话到 DSH 会话的映射（删除本地会话时调用）。"""
        key = session_key or ""
        with self._state_lock:
            self._sessions.pop(key, None)
            self._session_origin.pop(key, None)
        return self._store.pop(key)

    def close(self):
        with self._state_lock:
            proc = self._proc
            self._proc = None
            self._sessions = {}
            self._session_origin = {}
            with self._handler_lock:
                self._session_handlers = {}
            self._initialized = False
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


_connection = None
_connection_lock = threading.Lock()


def get_connection(config=None):
    """进程内共享一个 DSH 连接，避免每次提问都重启 harness。"""
    global _connection
    with _connection_lock:
        if _connection is None:
            command = config.get_dsh_command() if config is not None else DEFAULT_COMMAND
            profile = config.get_dsh_profile() if config is not None else DEFAULT_PROFILE
            overlay = sync_launch_overlay(config)
            _connection = DshAcpConnection(
                command=command, profile=profile, overlay_path=overlay
            )
        return _connection


def current_connection():
    """返回已存在的连接（不会启动 DSH 进程）。"""
    return _connection


def reset_connection():
    """关闭并丢弃当前连接（配置变更或排错时使用）。"""
    global _connection
    with _connection_lock:
        connection = _connection
        _connection = None
    if connection is not None:
        connection.close()


class DSHSessionThread(QThread):
    """信号面与 APICallThread 一致，供 MainWindow 直接接线。"""

    response_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    stream_chunk = pyqtSignal(str)
    tool_confirmation_requested = pyqtSignal(str, str, object)
    status_update = pyqtSignal(str)
    computer_state = pyqtSignal(str)
    task_state_updated = pyqtSignal(object)
    stopped = pyqtSignal()

    def __init__(self, messages, config, task_state=None, session_id=None):
        super().__init__()
        self.messages = messages
        self.config = config
        self.session_id = str(session_id or "")
        self.task_state = dict(task_state) if isinstance(task_state, dict) else {}
        self.is_running = True
        self._reply_parts = []
        self._tool_index = 0
        self._plan_index = 0
        self._confirmation_lock = threading.Lock()
        self._confirmation_events = {}
        self._confirmation_answers = {}
        self._session_key = ""
        self._connection = get_connection(config)

    # ---------------- 对外接口（与 APICallThread 对齐） ----------------

    def stop(self):
        self.is_running = False
        self._release_pending_confirmations()
        try:
            self._connection.cancel(self.session_id)
        except Exception:
            pass

    def set_tool_confirmation(self, call_id, approved):
        with self._confirmation_lock:
            self._confirmation_answers[call_id] = bool(approved)
            event = self._confirmation_events.get(call_id)
        if event is not None:
            event.set()

    def _release_pending_confirmations(self):
        with self._confirmation_lock:
            events = list(self._confirmation_events.values())
        for event in events:
            event.set()

    # ---------------- 线程主体 ----------------

    def run(self):
        try:
            self._touch("planning", "连接 DSH")
            self._set_step("connect", "连接 DSH 运行时", "in_progress", "正在启动本机 DSH。")
            self.status_update.emit("正在连接 DSH…")

            connection = self._connection
            # 会话建立前的兜底通道；建立后按 sessionId 精确分流。
            connection.status_handler = self.status_update.emit
            connection.ensure_started()
            connection.ensure_initialized()
            self._session_key = connection.ensure_session(self.session_id)
            connection.register_session_handler(self._session_key, self)
            self._set_step("connect", "连接 DSH 运行时", "completed", "已连接本机 DSH。")

            if not self.is_running:
                self.stopped.emit()
                return

            self._touch("executing", "DSH 正在处理")
            self._set_step("analyze", "分析需求", "completed", "已把消息交给 DSH。")
            self._set_step("execute", "DSH 执行", "in_progress", "DSH 正在推理与调用工具。")
            self.status_update.emit("DSH 正在思考…")

            blocks = self._build_prompt_blocks()
            seed = self._history_seed_blocks(connection.session_origin(self.session_id))
            if seed:
                blocks = seed + blocks
                self.status_update.emit("新 DSH 会话：已附带最近几轮本地上下文")
            try:
                result = connection.prompt(blocks, self.session_id)
            finally:
                connection.unregister_session_handler(self._session_key)
            if not self.is_running:
                self.stopped.emit()
                return

            content = "".join(self._reply_parts).strip()
            if not content:
                stop_reason = result.get("stopReason") if isinstance(result, dict) else ""
                content = (
                    "DSH 这一轮没有返回文本内容"
                    + (f"（stopReason={stop_reason}）。" if stop_reason else "。")
                )
            self._set_step("execute", "DSH 执行", "completed", "DSH 已完成本轮。")
            self._set_step("finalize", "整理最终答复", "completed", "已生成最终答复。")
            self.task_state["final_response"] = content
            self._touch("completed", "已完成")

            if not self._reply_parts and content:
                self.stream_chunk.emit(content)
            self.response_received.emit(content)
        except DshAcpError as exc:
            self._fail(str(exc))
            self.error_occurred.emit(f"DSH 桥接失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默退出
            self._fail(str(exc))
            self.error_occurred.emit(f"DSH 桥接异常：{exc}")

    def _fail(self, detail):
        self.task_state["last_error"] = detail
        self._set_step("execute", "DSH 执行", "failed", detail)
        self._touch("failed", "执行失败")

    # ---------------- 消息构造 ----------------

    def _latest_user_content(self):
        for message in reversed(self.messages or []):
            if isinstance(message, dict) and message.get("role") == "user":
                return message.get("content")
        return ""

    def _build_prompt_blocks(self):
        content = self._latest_user_content()
        fallback = content if isinstance(content, str) else ""
        blocks = build_prompt_blocks(content, fallback)
        if self._connection.image_prompt_enabled:
            return blocks
        if not any(block.get("type") == "image" for block in blocks):
            return blocks
        kept = [block for block in blocks if block.get("type") != "image"]
        dropped = len(blocks) - len(kept)
        self.status_update.emit(
            f"当前 DSH 路由未声明图片输入，已跳过 {dropped} 张图片"
        )
        kept.append({
            "type": "text",
            "text": f"（本条消息附带 {dropped} 张图片，但当前 DSH 模型不支持图片输入，已忽略。）",
        })
        return kept

    def _history_seed_blocks(self, origin):
        """新会话（非恢复）时，把最近几轮本地对话作为背景补给 DSH，避免完全失忆。"""
        if origin == "resumed":
            return []
        recap = self._history_recap_text()
        if not recap:
            return []
        return [{"type": "text", "text": recap}]

    def _history_recap_text(self):
        """把本地历史压成一段文字：最近 HISTORY_SEED_TURNS 轮，单条超长截断。"""
        history = list(self.messages or [])
        for index in range(len(history) - 1, -1, -1):
            message = history[index]
            if isinstance(message, dict) and message.get("role") == "user":
                history = history[:index]
                break
        lines = []
        for message in history[-HISTORY_SEED_TURNS * 2:]:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _message_text(message.get("content")).strip()
            if not text:
                continue
            if len(text) > HISTORY_SEED_CHARS:
                text = text[:HISTORY_SEED_CHARS] + "…（截断）"
            lines.append(("用户：" if role == "user" else "助手：") + text)
        if not any(line.startswith("用户：") for line in lines):
            return ""
        return (
            "【背景：本窗口此前的对话摘录，仅供你理解上下文】\n"
            + "\n".join(lines)
            + "\n【以上是历史记录，不要复述、不要评价本段；请直接回答接下来的这条用户消息。】"
        )

    # ---------------- 连接层回调（按会话分发） ----------------

    def handle_session_update(self, params):
        self._on_update(params)

    def handle_permission_request(self, params):
        return self._on_permission(params)

    def handle_status(self, text):
        self.status_update.emit(text)

    # ---------------- 事件映射 ----------------

    def _on_update(self, params):
        update = (params or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = (update.get("content") or {}).get("text", "")
            if text:
                self._reply_parts.append(text)
                self.stream_chunk.emit(text)
        elif kind == "agent_thought_chunk":
            self.status_update.emit("DSH 正在思考…")
        elif kind == "tool_call":
            self._tool_index += 1
            title = _tool_title(update)
            self._set_step(
                f"dsh_tool_{self._tool_index}",
                f"DSH 调用工具：{title}",
                "in_progress",
                "DSH 正在执行该工具。",
            )
            self._touch("executing", f"DSH 调用工具：{title}")
            self.status_update.emit(f"DSH 正在执行：{title}")
        elif kind == "tool_call_update":
            status = str(update.get("status") or "")
            if status in ("completed", "failed"):
                self._set_step(
                    f"dsh_tool_{self._tool_index}",
                    f"DSH 调用工具：{_tool_title(update)}",
                    "completed" if status == "completed" else "failed",
                    f"工具状态：{status}",
                )
        elif kind == "plan":
            entries = update.get("entries") or []
            for entry in entries:
                self._plan_index += 1
                self._set_step(
                    f"dsh_plan_{self._plan_index}",
                    str(entry.get("content") or "DSH 计划步骤"),
                    "in_progress" if entry.get("status") in (None, "in_progress") else "completed",
                    str(entry.get("priority") or ""),
                )
        elif kind == "session_info_update":
            title = update.get("title")
            if title:
                self.status_update.emit(f"DSH 会话：{title}")

    def _on_permission(self, params):
        """在独立线程里等待用户授权，避免阻塞 ACP 读取线程。"""
        tool_call = params.get("toolCall") or {}
        self._tool_index += 1
        call_id = f"dsh_perm_{self._tool_index}"
        title = _tool_title(tool_call)
        arguments = _tool_arguments(tool_call)
        event = threading.Event()
        with self._confirmation_lock:
            self._confirmation_events[call_id] = event
        try:
            self.tool_confirmation_requested.emit(call_id, title, arguments)
            if not self.is_running:
                return False
            if not event.wait(PERMISSION_TIMEOUT_SECONDS):
                return False
            with self._confirmation_lock:
                return bool(self._confirmation_answers.pop(call_id, False))
        finally:
            with self._confirmation_lock:
                self._confirmation_events.pop(call_id, None)

    # ---------------- 任务状态（复用主程序的任务面板结构） ----------------

    def _touch(self, status, current_step):
        if not self.task_state:
            return
        self.task_state["status"] = status
        self.task_state["current_step"] = current_step
        self.task_state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.task_state_updated.emit(dict(self.task_state))

    def _set_step(self, key, title, status, detail=""):
        if not self.task_state:
            return
        steps = self.task_state.setdefault("plan_steps", [])
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for step in steps:
            if step.get("key") != key:
                continue
            step["title"] = title
            step["status"] = status
            step["detail"] = detail
            step["updated_at"] = now
            if status == "in_progress" and not step.get("started_at"):
                step["started_at"] = now
            if status in ("completed", "failed"):
                step["finished_at"] = now
            self.task_state_updated.emit(dict(self.task_state))
            return
        steps.append(
            {
                "key": key,
                "title": title,
                "status": status,
                "detail": detail,
                "started_at": now if status == "in_progress" else "",
                "finished_at": now if status in ("completed", "failed") else "",
                "updated_at": now,
            }
        )
        self.task_state_updated.emit(dict(self.task_state))