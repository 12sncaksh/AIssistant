"""DSH（DeepSeek Harness）ACP 桥接客户端。

通过 Agent Client Protocol（JSON-RPC over stdio）驱动本机的 `dsh --profile acp`
进程，并对外暴露与 APICallThread 完全一致的 Qt 信号，使界面层可以零改动复用。

设计边界（与路线图一致）：
- DSH 负责 Agent 循环、模型、会话、工具执行；
- Aissistant 负责桌面呈现（桌宠 / Live2D / TTS / 气泡）与本地交互；
- 桥接开启时本地工具循环不参与，避免两边各跑一套。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from PyQt6.QtCore import QThread, pyqtSignal

from dsh_acp_permission_patch import ensure_permission_api
import thread_registry

PROTOCOL_VERSION = 1
DEFAULT_COMMAND = "dsh"
DEFAULT_PROFILE = "acp"
PROMPT_TIMEOUT_SECONDS = 1800
# 单轮 prompt 的等待策略：不再拿"总时长"当超时——长任务（深度调研、大范围改代码）
# 常常要跑几十分钟，按总时长计时会把正常推进的任务当成卡死杀掉。改成两条线：
# - 空闲超时：期间只要还收到任何事件/消息就重置计时，只有彻底静默这么久才判卡死；
# - 硬上限：兜底，防止协议层面真的无限挂住。
PROMPT_IDLE_TIMEOUT_SECONDS = 300
PROMPT_HARD_TIMEOUT_SECONDS = 4 * 3600
PERMISSION_TIMEOUT_SECONDS = 300
CONTEXT_OVERFLOW_MARKERS = (
    "context overflow",
    "context length exceeded",
    "maximum context length",
    "prompt is too long",
    "too many tokens",
)
# 「会话已不存在」的 DSH 错误标记。故意收窄：像 "invalid session" 这种子串会
# 误伤 "invalid session configuration" 之类的普通错误，触发无谓的 reset+retry。
INVALID_SESSION_MARKERS = (
    "session not found",
    "session does not exist",
    "session no longer exists",
    "no such session",
    "unknown session",
    "session expired",
)
# DSH 的 sessionId 是 UUID；据此把「本地会话键」和「DSH sessionId」区分开。
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# 权限模式：环境变量设定 DSH 的新会话默认值；已有会话由自定义 ACP 请求同步并校验。
# dsh-base 用 DSH_PERMISSION_MODE 配置默认沙箱与审批策略；会话级请求直接追加权限预设事件。
DSH_PERMISSION_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_PERMISSION_MODE = "workspace-write"
FULL_ACCESS_MODE = "danger-full-access"
# ACP 标准会话配置项：DSH 通过 session/new 的 configOptions 公布「模型」和「推理档位」。
#   model            → category "model"，候选项按 provider 分组，值形如 ["provider","model"] 的 JSON 串
#   reasoning_effort → category "thought_level"，只有所选模型声明了 reasoningEfforts 时才出现
# 客户端用 session/set_config_option 改写它们，等价于 DSH 网页端 composer 里的模型 / 推理强度控件。
MODEL_CONFIG_ID = "model"
REASONING_CONFIG_ID = "reasoning_effort"
MODEL_OPTION_CATEGORY = "model"
REASONING_OPTION_CATEGORY = "thought_level"
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


def is_context_overflow_error(error):
    """Return True only for provider/DSH context-size failures."""
    text = str(error or "").lower()
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


def is_invalid_session_error(error):
    """Return True only when DSH reports the session itself no longer exists."""
    text = str(error or "").lower()
    return any(marker in text for marker in INVALID_SESSION_MARKERS)


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


def find_config_option(options, config_id="", category=""):
    """在 ACP 公布的 configOptions 里找一项：优先按 id，其次按 category。"""
    for option in options or []:
        if not isinstance(option, dict):
            continue
        if config_id and option.get("id") == config_id:
            return option
        if category and option.get("category") == category:
            return option
    return None


def config_option_choices(option):
    """把一个 select 型配置项的候选项拍平成 [{value,name,group}]。

    ACP 的 select 既可以是不分组的一层列表，也可以是按 provider 分组的两层列表
    （DSH 的 model 选项就是后者），这里统一成一种形状给 UI 用。
    """
    choices = []
    for entry in (option or {}).get("options") or []:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("options")
        group = str(entry.get("name") or entry.get("group") or "")
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict) and item.get("value") is not None:
                    choices.append({
                        "value": str(item.get("value")),
                        "name": str(item.get("name") or item.get("value")),
                        "description": str(item.get("description") or ""),
                        "group": group,
                    })
        elif entry.get("value") is not None:
            choices.append({
                "value": str(entry.get("value")),
                "name": str(entry.get("name") or entry.get("value")),
                "description": str(entry.get("description") or ""),
                "group": "",
            })
    return choices


def model_selection_value(provider, model):
    """ACP 的 model 选项值就是把 [provider, model] 序列化成 JSON 字符串。"""
    return json.dumps([str(provider or ""), str(model or "")], separators=(",", ":"))


def parse_model_selection_value(value):
    """反解 model 选项值，返回 (provider, model)。

    兼容三种写法：ACP 的 JSON 数组、`provider/model`、以及裸 model id（provider 留空沿用旧值）。
    """
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list) and len(parsed) == 2:
        return str(parsed[0] or ""), str(parsed[1] or "")
    text = str(value or "").strip()
    if "/" in text:
        provider, _, model = text.partition("/")
        return provider.strip(), model.strip()
    return "", text


def valid_dsh_providers(options):
    """从一份 configOptions 里取出 DSH 当前可用的 provider 集合。

    DSH 的 model 选项是按 provider 分组的两层列表，每个候选值都是
    ["provider","model"] 的 JSON；把 provider 拍平成集合，用来校验路由覆盖。
    """
    option = find_config_option(options or [], "", MODEL_OPTION_CATEGORY)
    providers = set()
    for choice in config_option_choices(option):
        provider, _ = parse_model_selection_value(choice.get("value"))
        if provider:
            providers.add(provider)
    return providers


def known_dsh_providers(config):
    """读「上次探测确认可用」的 provider 集合；没有记录时返回空集合（表示未知）。"""
    getter = getattr(config, "get_dsh_known_providers", None) if config is not None else None
    if not callable(getter):
        return set()
    try:
        return {str(item).strip() for item in (getter() or []) if str(item).strip()}
    except Exception:
        return set()


def rejected_dsh_providers(config):
    """读「实测判定失效」的 provider 集合。

    单独存一份的原因：`set_dsh_provider("")` 会被 config.json 的文件回退盖回来，
    光清配置项挡不住旧值，必须有一份显式的拒绝名单。
    """
    getter = getattr(config, "get_dsh_rejected_providers", None) if config is not None else None
    if not callable(getter):
        return set()
    try:
        return {str(item).strip() for item in (getter() or []) if str(item).strip()}
    except Exception:
        return set()


def remember_known_dsh_providers(config, options):
    """把这次探测确认可用的 provider 记下来，供 sync_launch_overlay 校验路由。

    这样 config.json 里残留一个失效 provider 时，下一次启动就不会再拿它去写
    overlay 把桥接打死，而是直接跟随 DSH 自己的路由。
    """
    if config is None:
        return
    setter = getattr(config, "set_dsh_known_providers", None)
    if not callable(setter):
        return
    providers = valid_dsh_providers(options)
    if not providers:
        return
    try:
        setter(providers)
    except Exception:
        logging.warning("记录 DSH 可用 provider 失败", exc_info=True)


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
    """把选定的 ACP 路由写进 DSH 启动 overlay，返回文件路径。

    为什么需要：`~/.dsh/profiles/acp` 自带的 patch 会把 ACP 路由钉死在某个 provider 上，
    与用户在 DSH 里选的内网网关不一致，于是网关 Key 被拿去请求公网 API 而认证失败。
    这里用 `dsh --patch <文件>` 在 profile 层之上覆盖回用户配置的路由。
    没有配置 provider 时返回空串，表示不改动 DSH 原有行为。
    """
    provider = str(getattr(config, "get_dsh_provider", lambda: "")() or "").strip() if config is not None else ""
    model = str(getattr(config, "get_dsh_model", lambda: "")() or "").strip() if config is not None else ""
    if not provider:
        return ""
    # DSH 的 provider / 模型会经常变：配置里残留的旧值一旦写进 overlay，DSH 会直接
    # 回 Internal error 把桥接打死。所以只有确认它在上次探测到的可用集合里才写，
    # 否则干脆不写，让 DSH 用它自己的 profile 路由。
    known = known_dsh_providers(config)
    rejected = rejected_dsh_providers(config)
    # 明确出现在可用集合里的 provider 一律放行（哪怕曾被判失效，DSH 后来又加回来了）；
    # 否则：只要探测过（known 非空）或它已被实测判失效，就不写 overlay，改为跟随 DSH。
    if provider not in known and (known or provider in rejected):
        logging.warning(
            "DSH provider %s 不可用（可用：%s），跳过路由覆盖，改为跟随 DSH",
            provider,
            ", ".join(sorted(known)) or "未知",
        )
        return ""
    lines = [
        "# Generated by Aissistant: override the ACP route.",
        "# 本文件由程序生成，手工修改会在下次启动时被覆盖；模板见 config/templates/。",
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

    def __init__(self, command=DEFAULT_COMMAND, profile=DEFAULT_PROFILE, cwd=None, overlay_path="",
                 permission_mode=DEFAULT_PERMISSION_MODE, provider="", model="", reasoning="",
                 config=None):
        # 配置对象只用于「路由自愈」：provider 失效时清掉配置项、成功后回写实际路由。
        self.config = config
        self.command = command or DEFAULT_COMMAND
        self.profile = profile or DEFAULT_PROFILE
        self.cwd = cwd or default_workspace()
        self.overlay_path = overlay_path or ""
        self.permission_mode = (
            permission_mode if permission_mode in DSH_PERMISSION_MODES else DEFAULT_PERMISSION_MODE
        )
        self._process_permission_mode = None
        # 期望的模型 / 推理档位：会话建立或复用时按 DSH 公布的可选项落到 ACP 会话上
        self.desired_provider = str(provider or "").strip()
        self.desired_model = str(model or "").strip()
        self.desired_reasoning = str(reasoning or "").strip()
        self._needs_restart = False
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
        self._config_options = {}
        # 每个会话最后一次已处理的 (provider, model, reasoning)，避免复用会话时反复下发 / 反复提示
        self._applied_selection = {}
        # 每个 DSH sessionId 最近一次已注入的角色设定文本：ACP 的 session/new 不接受
        # 系统提示词，只能在 prompt blocks 里补给，这里按会话记账避免每轮重复注入。
        self._session_role_prompt = {}
        self._store = SessionMapStore(default_session_map_path())
        self._stderr_tail = []
        self.update_handler = None
        self.permission_handler = None
        self.status_handler = None

    # ---------------- 进程与传输 ----------------

    def _spawn(self):
        argv = [self.command, "--profile", self.profile]
        try:
            patched_module = ensure_permission_api()
            logging.info("DSH ACP 会话权限接口可用：%s", patched_module)
        except Exception as exc:
            raise DshAcpError(f"无法启用 DSH 会话权限接口：{exc}") from exc
        if self.overlay_path:
            argv += ["--patch", self.overlay_path]
        if os.name == "nt":
            argv = ["cmd", "/c"] + argv
        # 权限模式：dsh-base 读 DSH_PERMISSION_MODE 决定沙箱与审批策略（danger-full-access 即完全权限）
        env = os.environ.copy()
        env["DSH_PERMISSION_MODE"] = self.permission_mode
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
                env=env,
                # 明确不弹控制台窗口（在全局补丁之外再写一次，让意图在调用点可见）
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise DshAcpError(f"无法启动 DSH（{self.command}）：{exc}") from exc
        self._process_permission_mode = self.permission_mode
        # 管道泵随连接存活，数量由连接数封顶，因此不设并发上限，只登记保活
        thread_registry.spawn(self._pump_stdout, name="DSH-stdout", group="dsh_pump")
        thread_registry.spawn(self._pump_stderr, name="DSH-stderr", group="dsh_pump")

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
        # 任何入站消息（事件、反向请求、响应）都算"连接还在推进"，
        # 供 request() 的空闲超时判断使用：只有彻底静默才算卡死。
        self._last_activity = time.monotonic()
        if "id" not in message:
            if message.get("method") == "session/update":
                self._notify_update(message.get("params") or {})
            return
        if "method" in message:
            # 每个反向请求起一个线程：走注册表以设上限，突发请求不会无限堆积
            try:
                thread_registry.spawn(
                    self._answer_reverse_request,
                    name="DSH-reverse",
                    group="dsh_reverse",
                    args=(message,),
                )
            except thread_registry.ConcurrencyLimit as exc:
                logging.warning("已忽略一个反向请求：%s", exc)
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

    def request(self, method, params, timeout=PROMPT_TIMEOUT_SECONDS, idle_timeout=None):
        """发一个 JSON-RPC 请求并等待结果。

        idle_timeout 不为空时按"空闲"计时：等待期间每收到一条入站消息（事件 / 反向请求 /
        响应）就重置计时，只有连续静默 idle_timeout 秒才判定卡死；此时 timeout 降级为硬上限，
        只作兜底。这样"跑了很久但一直在推进"的长任务不会被误杀。
        """
        self._next_id += 1
        request_id = self._next_id
        waiter = queue.Queue()
        self._pending[request_id] = waiter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        self._last_activity = time.monotonic()
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                try:
                    message = waiter.get(timeout=idle_timeout or timeout)
                    break
                except queue.Empty:
                    if not idle_timeout:
                        raise
                    silent = time.monotonic() - self._last_activity
                    if silent < idle_timeout and (deadline is None or time.monotonic() < deadline):
                        continue
                    self._pending.pop(request_id, None)
                    if silent >= idle_timeout:
                        raise DshAcpError(
                            f"{method} 超时（已静默 {silent:.0f}s，没有任何事件）"
                        ) from None
                    raise DshAcpError(f"{method} 超时（超过硬上限 {timeout}s）") from None
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

    def set_permission_mode(self, mode):
        """更新权限模式：进程在跑且模式变化时标记重启，下一次提问前以新环境变量重启 DSH。"""
        value = str(mode or "").strip().lower()
        if value not in DSH_PERMISSION_MODES:
            value = DEFAULT_PERMISSION_MODE
        with self._state_lock:
            if value == self.permission_mode:
                return False
            self.permission_mode = value
            self._needs_restart = True
        logging.info("DSH 权限模式已切换为 %s（下一次提问生效）", value)
        return True

    def ensure_started(self):
        with self._state_lock:
            if self._needs_restart:
                # 权限模式变了：丢掉旧进程，让新的 DSH_PERMISSION_MODE 生效
                self._needs_restart = False
                self.close()
            if self._proc is not None and self._proc.poll() is None:
                return
            self._initialized = False
            self.image_prompt_enabled = False
            self._sessions = {}
            self._session_origin = {}
            self._config_options = {}
            self._applied_selection = {}
            self._session_role_prompt = {}
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
            self.apply_permission_preset(cached)
            # 配置可能在会话存活期间改过：复用前把模型 / 推理档位对齐一次
            self.apply_model_selection(cached)
            return cached

        persisted = self._store.get(key)
        if persisted and self._resume_session(persisted):
            self._sessions[key] = persisted
            self._session_origin[key] = "resumed"
            self._notify_status("已恢复上次的 DSH 会话上下文")
            self.apply_permission_preset(persisted)
            self.apply_model_selection(persisted)
            return persisted
        if persisted:
            self._store.pop(key)
            self._notify_status("上次的 DSH 会话已不可恢复，将新建会话")

        session_id, _result = self.create_session()
        self._sessions[key] = session_id
        self._session_origin[key] = "created"
        self._store.set(key, session_id)
        self.apply_permission_preset(session_id)
        self.apply_model_selection(session_id)
        return session_id

    def create_session(self, mcp_servers=None, timeout=180):
        """新建一个 DSH 会话，返回 (sessionId, 原始响应)。

        路由覆盖（``--patch overlay``）是这里唯一可能把 DSH 打死的东西：config 里的
        provider 失效时 DSH 直接回 Internal error。此时分两级自愈：

        1. 丢掉覆盖、把该 provider 记进拒绝名单，用「跟随 DSH」的新进程重试；
        2. 成功后再看 DSH 公布的 provider × model 矩阵，按同名 model 把用户真正想要的
           provider 修回来，带 overlay 重建一次，最后回写实际生效的路由。

        两级都失败就如实抛错，不做无上限重试。
        """
        params = {
            "cwd": self.cwd,
            "mcpServers": build_acp_mcp_servers() if mcp_servers is None else list(mcp_servers),
        }
        try:
            result = self.request("session/new", params, timeout=timeout)
        except DshAcpError:
            if not self._drop_route_override():
                raise
            self.ensure_initialized()
            result = self.request("session/new", params, timeout=timeout)
            # 已经用「跟随 DSH」建起来了：DSH 这时会公布完整的 provider × model
            # 矩阵，据此把用户真正想要的 provider 修回来，再带 overlay 建一次。
            if self._repair_route_from_options(result):
                self.ensure_initialized()
                result = self.request("session/new", params, timeout=timeout)
                self._remember_actual_route(result)
        session_id = str((result or {}).get("sessionId") or "")
        if not session_id:
            raise DshAcpError("DSH 未返回 sessionId，无法建立会话")
        self._remember_config_options(session_id, result)
        remember_known_dsh_providers(self.config, self.session_config_options(session_id))
        return session_id, result

    def _drop_route_override(self):
        """丢掉失效的路由覆盖，退回 DSH 自己的 profile 路由。

        只对「配置了 --patch overlay」的会话生效；返回 True 表示调用方可以重试。
        """
        with self._state_lock:
            if not self.overlay_path:
                return False
            self.overlay_path = ""
            self._needs_restart = True
        self._forget_configured_route()
        logging.warning("DSH 路由覆盖不可用，已退回 DSH 自身路由重试")
        self._notify_status("DSH 配置里的 provider 不可用，正在自动修正路由")
        return True

    def _repair_route_from_options(self, result):
        """用 DSH 自己公布的 provider 列表修正配置里的失效 provider。

        不带 overlay 时 DSH 会公布完整的 provider × model 矩阵（实测 gpt 与
        deepseek-official 两组都在），所以按「同名 model」反查 provider，比盲猜或
        读静态 settings.yaml 都可靠 —— 静态文件里的 provider 名字自己就会漂。
        返回修正后的 provider；无法修正时返回空串（保持「跟随 DSH」）。
        """
        config = self.config
        model = str(self.desired_model or "").strip()
        bad = str(self.desired_provider or "").strip()
        if config is None or not model:
            return ""
        options = list((result or {}).get("configOptions") or [])
        providers = valid_dsh_providers(options)
        if not providers or bad in providers:
            return ""
        option = find_config_option(options, "", MODEL_OPTION_CATEGORY)
        for choice in config_option_choices(option):
            provider, choice_model = parse_model_selection_value(choice.get("value"))
            if not provider or choice_model != model:
                continue
            setter = getattr(config, "set_dsh_provider", None)
            if not callable(setter):
                return ""
            try:
                setter(provider)
            except Exception:
                logging.warning("回写修正后的 DSH provider 失败", exc_info=True)
                return ""
            # 先让它进入「已知可用」集合：这个值刚由 DSH 亲自公布，否则
            # sync_launch_overlay 的校验会把它当成没探测过的值而拒绝写覆盖。
            remember_known_dsh_providers(config, options)
            self.desired_provider = provider
            overlay = sync_launch_overlay(config)
            with self._state_lock:
                self.overlay_path = overlay
                self._needs_restart = True
            logging.warning(
                "DSH provider %s 不可用，已按 DSH 公布的模型列表修正为 %s", bad, provider
            )
            self._notify_status(f"已自动把 DSH provider 修正为 {provider}")
            return provider
        return ""

    def _forget_configured_route(self):
        """把失效的 provider 记进拒绝名单，并尽量清掉配置里的值。

        `set_dsh_provider("")` 在 config.json 仍有非空值时会被文件回退盖回来，
        所以真正让「跟随 DSH」生效的是拒绝名单：sync_launch_overlay 会跳过它。
        """
        if self.config is None:
            return
        bad = str(self.desired_provider or "").strip()
        reject = getattr(self.config, "add_dsh_rejected_provider", None)
        if bad and callable(reject):
            try:
                reject(bad)
            except Exception:
                logging.warning("记录失效 DSH provider 失败", exc_info=True)
        setter = getattr(self.config, "set_dsh_provider", None)
        if not callable(setter):
            return
        try:
            setter("")
        except Exception:
            logging.warning("清空 DSH provider 配置失败", exc_info=True)

    def _remember_actual_route(self, result):
        """把 DSH 实际生效的 provider / model 回写进配置，供下次启动直接使用。"""
        if self.config is None:
            return
        option = find_config_option(
            list((result or {}).get("configOptions") or []), "", MODEL_OPTION_CATEGORY
        )
        provider, model = parse_model_selection_value((option or {}).get("currentValue"))
        if not provider:
            return
        try:
            setter = getattr(self.config, "set_dsh_provider", None)
            if callable(setter):
                setter(provider)
            setter_model = getattr(self.config, "set_dsh_model", None)
            if callable(setter_model) and model:
                setter_model(model)
            logging.info("已把 DSH 实际路由回写为 %s/%s", provider, model)
        except Exception:
            logging.warning("回写 DSH 路由失败", exc_info=True)

    def _resume_session(self, session_id):
        """尝试恢复已持久化的 DSH 会话；不可恢复或 cwd 变更时返回 False。"""
        try:
            result = self.request(
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
        # ACP 的 session/resume 只表示「DSH 接受了请求」，并不保证它仍持有原上下文
        # （ACP 没有 compaction 通知）。这里只拦得住「DSH 换了 sessionId」的情况；
        # 「id 没变但上下文已丢」由调用方对 resumed 也补一轮 seed 来兜底。
        returned = str((result or {}).get("sessionId") or "")
        if returned and returned != str(session_id):
            logging.warning(
                "session/resume 返回了不同的 sessionId（%s → %s），按新建处理",
                str(session_id)[:12],
                returned[:12],
            )
            return False
        self._remember_config_options(returned or session_id, result)
        return True

    # ---------------- 模型 / 推理档位（ACP 会话配置项） ----------------

    def _remember_config_options(self, session_id, result):
        """记住一次会话响应里公布的 configOptions（session/new、session/resume、set_config_option 都会返回）。"""
        options = result.get("configOptions") if isinstance(result, dict) else None
        if not isinstance(options, list):
            return
        with self._state_lock:
            self._config_options[str(session_id or "")] = options

    def session_config_options(self, session_id):
        """某个 DSH 会话当前公布的配置项快照（模型 / 推理档位等）。"""
        with self._state_lock:
            return list(self._config_options.get(str(session_id or "")) or [])

    def set_model_selection(self, provider=None, model=None, reasoning=None):
        """更新期望的模型 / 推理档位，返回 True 表示有变化。

        只改期望值，不打断正在跑的回合；下一次 ensure_session（含复用）时落到 ACP 会话上。
        """
        provider = str(provider or "").strip()
        model = str(model or "").strip()
        reasoning = str(reasoning or "").strip()
        with self._state_lock:
            changed = (provider, model, reasoning) != (
                self.desired_provider, self.desired_model, self.desired_reasoning
            )
            self.desired_provider = provider
            self.desired_model = model
            self.desired_reasoning = reasoning
        if changed:
            logging.info(
                "DSH 模型/推理设置已更新：%s/%s · %s",
                provider or "(默认)", model or "(默认)", reasoning or "(默认)",
            )
        return changed

    def sync_model_selection(self):
        """把当前期望的模型 / 推理档位补写到所有已知会话（尽力而为）。"""
        with self._state_lock:
            session_ids = list(self._sessions.values())
        for session_id in session_ids:
            try:
                self.apply_model_selection(session_id)
            except Exception:
                logging.warning("同步 DSH 模型/推理设置失败：%s", session_id, exc_info=True)

    def _set_config_option(self, session_id, config_id, value):
        """调用 session/set_config_option，并把返回的新 configOptions 存下来。"""
        result = self.request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
            timeout=120,
        )
        self._remember_config_options(session_id, result)
        return self.session_config_options(session_id)

    def apply_permission_preset(self, session_id):
        """Synchronize and verify the effective permission preset on one DSH session."""
        session_id = str(session_id or "")
        if not session_id:
            raise DshAcpError("无法设置 DSH 权限：sessionId 为空")
        try:
            result = self.request(
                "session/set_permission_preset",
                {"sessionId": session_id, "preset": self.permission_mode},
                timeout=30,
            )
        except DshAcpError as exc:
            # 回合执行中（prompt in flight）DSH 不允许改权限——这是正常的时序竞争，
            # 不该当成桥接故障把整轮打断：记警告、跳过本次下发，等空闲后自然会重新同步。
            if "in flight" in str(exc):
                logging.warning(
                    "DSH 会话 %s 正在执行回合，本次权限暂不下发（保持原权限）：%s",
                    session_id,
                    exc,
                )
                return False
            raise DshAcpError(f"设置 DSH 会话权限失败：{exc}") from exc
        actual = result.get("permissionPreset") if isinstance(result, dict) else None
        if actual != self.permission_mode:
            raise DshAcpError(
                f"DSH 会话权限校验失败：期望 {self.permission_mode}，实际 {actual or '未知'}"
            )
        logging.info("DSH 会话 %s 权限已确认：%s", session_id, actual)

    def apply_model_selection(self, session_id):
        """把期望的模型 / 推理档位落到某个 ACP 会话。

        只在 DSH 确实公布了对应候选项时才设置：模型 id 写错、或模型没有声明
        reasoningEfforts（DSH 就不会公布 thought_level 选项）时只记日志，绝不硬塞。
        """
        session_id = str(session_id or "")
        options = self.session_config_options(session_id)
        if not session_id or not options:
            return
        signature = (self.desired_provider, self.desired_model, self.desired_reasoning)
        with self._state_lock:
            if self._applied_selection.get(session_id) == signature:
                return
        # 逐项记账：只有真正下发成功、或确认 DSH 当前值已等于目标值时才写签名。
        # 跳过项（模型未声明档位 / 未提供该档位）不计入，否则签名会被永久占位，
        # 之后模型切换到位、档位可用了也不会再补发，表现为「设置保存了但不生效」。
        applied = True
        if self.desired_provider and self.desired_model:
            option = find_config_option(options, MODEL_CONFIG_ID, MODEL_OPTION_CATEGORY)
            if option is not None:
                want = model_selection_value(self.desired_provider, self.desired_model)
                available = {item["value"] for item in config_option_choices(option)}
                if want not in available:
                    applied = False
                    logging.warning(
                        "DSH 未提供模型 %s/%s，沿用当前模型 %s",
                        self.desired_provider, self.desired_model, option.get("currentValue"),
                    )
                    self._notify_status(
                        f"DSH 没有 {self.desired_provider}/{self.desired_model} 这个模型，已沿用 DSH 当前模型"
                    )
                elif str(option.get("currentValue") or "") != want:
                    try:
                        options = self._set_config_option(session_id, MODEL_CONFIG_ID, want)
                    except DshAcpError as exc:
                        applied = False
                        logging.warning("切换 DSH 模型失败：%s", exc)
                        self._notify_status(f"切换 DSH 模型失败：{exc}")
        if self.desired_reasoning:
            option = find_config_option(options, REASONING_CONFIG_ID, REASONING_OPTION_CATEGORY)
            if option is None:
                # 模型没声明 reasoningEfforts：暂时记不下签名，等模型切好后重试
                applied = False
                self._notify_status(
                    "该 DSH 模型未声明推理档位（reasoningEfforts），推理深度设置已跳过"
                )
            else:
                available = {item["value"] for item in config_option_choices(option)}
                if self.desired_reasoning not in available:
                    applied = False
                    self._notify_status(f"DSH 未提供推理档位 {self.desired_reasoning}，已跳过")
                elif str(option.get("currentValue") or "") != self.desired_reasoning:
                    try:
                        self._set_config_option(
                            session_id, REASONING_CONFIG_ID, self.desired_reasoning
                        )
                    except DshAcpError as exc:
                        applied = False
                        logging.warning("设置 DSH 推理档位失败：%s", exc)
                        self._notify_status(f"设置 DSH 推理档位失败：{exc}")
        if applied:
            with self._state_lock:
                self._applied_selection[session_id] = signature

    def session_origin(self, session_key=None):
        """最近一次 ensure_session 的结果：created / resumed / reused。"""
        return self._session_origin.get(str(session_key or ""), "")

    # ---------------- 角色设定补给 ----------------

    def role_prompt_needed(self, session_id, role_text):
        """某个 DSH 会话是否还没收到当前版本的角色设定。

        ACP 的 session/new / session/resume 都不接受系统提示词，角色只能在
        prompt blocks 里补给。新会话、恢复的会话、以及角色文本发生变化时必须
        注入；同一会话的后续轮次直接复用上下文里的旧块，不再重复。
        """
        role_text = str(role_text or "").strip()
        if not role_text:
            return False
        with self._state_lock:
            return self._session_role_prompt.get(str(session_id or "")) != role_text

    def mark_role_prompt(self, session_id, role_text):
        """记下某个 DSH 会话已收到的角色设定版本。"""
        role_text = str(role_text or "").strip()
        if not role_text:
            return
        with self._state_lock:
            self._session_role_prompt[str(session_id or "")] = role_text

    def prompt(self, blocks, session_key=None):
        session_id = self.ensure_session(session_key)
        try:
            return self.request(
                "session/prompt",
                {"sessionId": session_id, "prompt": blocks},
                timeout=PROMPT_HARD_TIMEOUT_SECONDS,
                idle_timeout=PROMPT_IDLE_TIMEOUT_SECONDS,
            )
        except DshAcpError:
            # 超时 / 失败后必须真的让服务端停下：否则那一轮 prompt 仍在跑，
            # 会话会卡在 "prompt in flight"，之后所有权限切换都会失败。
            # cancel() 内部吞掉自身异常，这里只负责发出取消，再把原错误抛回去。
            self.cancel(session_id)
            raise

    def cancel(self, session_key=None):
        """中断某个会话正在跑的回合。

        调用方可能给出本地会话键（MainWindow 的 current_session_id），也可能给出
        DSH 侧的 sessionId，这里两种都认。DSH 的 sessionId 是 UUID，所以即便映射
        已经被 forget_session 清掉（删除会话是先 forget 再 stop 的场景），
        仍然可以直接把 UUID 当 sessionId 发出去；否则取消通知发不出，暂停就变成空操作。
        """
        token = str(session_key or "")
        with self._state_lock:
            sessions = dict(self._sessions)
        session_id = sessions.get(token)
        if not session_id and _UUID_RE.fullmatch(token):
            session_id = token
        if not session_id:
            return
        try:
            self.notify("session/cancel", {"sessionId": session_id})
        except Exception:
            pass

    def reset_session(self, session_key):
        """Drop one DSH mapping after a pre-execution context overflow.

        ACP has no portable "clear history" method. Forgetting the local mapping
        is therefore the interoperable recovery path; the next prompt creates a
        fresh DSH session and persists its new id.
        """
        key = str(session_key or "")
        with self._state_lock:
            old_id = self._sessions.pop(key, None)
            self._session_origin.pop(key, None)
            if old_id:
                self._config_options.pop(str(old_id), None)
                self._applied_selection.pop(str(old_id), None)
                self._session_role_prompt.pop(str(old_id), None)
        self._store.pop(key)
        return old_id

    def forget_session(self, session_key):
        """丢弃某个本地会话到 DSH 会话的映射（删除本地会话时调用）。"""
        key = session_key or ""
        with self._state_lock:
            old_id = self._sessions.pop(key, None)
            self._session_origin.pop(key, None)
            if old_id:
                self._config_options.pop(str(old_id), None)
                self._applied_selection.pop(str(old_id), None)
                self._session_role_prompt.pop(str(old_id), None)
        return self._store.pop(key)

    def close(self):
        with self._state_lock:
            proc = self._proc
            self._proc = None
            self._process_permission_mode = None
            self._sessions = {}
            self._session_origin = {}
            self._session_role_prompt = {}
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


def _permission_mode_of(config):
    """从配置对象读权限模式；配置缺失或取值非法时回落到工作区可写。"""
    getter = getattr(config, "get_permission_mode", None) if config is not None else None
    if getter is None:
        return DEFAULT_PERMISSION_MODE
    try:
        value = str(getter() or "").strip().lower()
    except Exception:
        return DEFAULT_PERMISSION_MODE
    return value if value in DSH_PERMISSION_MODES else DEFAULT_PERMISSION_MODE


def _dsh_selection_of(config):
    """从配置对象读 DSH 的 provider / model / reasoning；任意一项缺失都回落到空串。"""
    def read(name):
        getter = getattr(config, name, None) if config is not None else None
        if getter is None:
            return ""
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""
    return (
        read("get_dsh_provider"),
        read("get_dsh_model"),
        read("get_dsh_reasoning_effort"),
    )


def get_connection(config=None):
    """进程内共享一个 DSH 连接，避免每次提问都重启 harness。"""
    global _connection
    with _connection_lock:
        if _connection is None:
            command = config.get_dsh_command() if config is not None else DEFAULT_COMMAND
            profile = config.get_dsh_profile() if config is not None else DEFAULT_PROFILE
            overlay = sync_launch_overlay(config)
            provider, model, reasoning = _dsh_selection_of(config)
            _connection = DshAcpConnection(
                command=command,
                profile=profile,
                overlay_path=overlay,
                permission_mode=_permission_mode_of(config),
                provider=provider,
                model=model,
                reasoning=reasoning,
                config=config,
            )
        else:
            _connection.set_permission_mode(_permission_mode_of(config))
        return _connection


def probe_session_options(config=None, timeout=180, provider=None, model=None):
    """起一个临时 ACP 连接读取目标模型公布的模型 / 推理档位。

    即使 config 里的 provider 已经失效（DSH 直接回 Internal error），create_session
    也会自动退回「跟随 DSH」并重试一次，所以这个探测总能拿到 DSH 权威的
    provider × model 列表，并顺手把配置修好。
    """
    current_provider, current_model, reasoning = _dsh_selection_of(config)
    provider = current_provider if provider is None else str(provider or "").strip()
    model = current_model if model is None else str(model or "").strip()
    connection = DshAcpConnection(
        command=config.get_dsh_command() if config is not None else DEFAULT_COMMAND,
        profile=config.get_dsh_profile() if config is not None else DEFAULT_PROFILE,
        overlay_path=sync_launch_overlay(config),
        permission_mode=_permission_mode_of(config),
        provider=provider,
        model=model,
        reasoning=reasoning,
        config=config,
    )
    try:
        connection.ensure_initialized()
        session_id, result = connection.create_session(mcp_servers=[], timeout=timeout)
        if session_id:
            connection.set_model_selection(provider, model, reasoning)
            connection.apply_model_selection(session_id)
            return connection.session_config_options(session_id), ""
        return list((result or {}).get("configOptions") or []), ""
    except Exception as exc:  # noqa: BLE001 - 探测失败要把原因原样带回 UI
        return [], str(exc)
    finally:
        connection.close()


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
