# main.py
# 桌面AI助手主程序 - 支持API调用、Live2D模型展示、会话管理、语音合成(PyQt6版本)
import sys
import os
import json
import copy
import html
import threading
import time
import webbrowser
import sqlite3
import tempfile
import asyncio
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
import random
import re
import mimetypes
import traceback
import requests
import edge_tts
import pygame
import uuid
import math
from urllib.parse import quote
try:
    import numpy as np
    import sounddevice as sd
    import websocket
    from urllib.parse import urlencode
    HAS_AUDIO_DEPS = True
except ImportError:
    HAS_AUDIO_DEPS = False
import queue
import base64
import hashlib
import hmac
import logging
from functools import partial
from logging.handlers import RotatingFileHandler

from action_control import ActionHandler  # 导入外部应用控制模块
from mcp_client import MCPManager  # 外部 MCP 工具（工具表合并 + 调用分发）
from vision_input import VisionInputController
import json
# PyQt6 导入
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtCore import QUrl
from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot, QUrl, QSettings, Qt, QTimer, QSize, QPointF, QEvent, QPoint, QRect, QRectF
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QSlider,
    QDialogButtonBox,
    QLabel, QSplitter, QMessageBox, QDialog, QFormLayout, QDialogButtonBox,QFileDialog, QInputDialog, QListView, QRubberBand,
    QMenuBar, QMenu, QStatusBar, QSizePolicy, QGroupBox, QComboBox, QCheckBox, QTextBrowser, QAbstractItemView, QProgressBar,
    QToolButton, QStackedWidget, QFrame, QStyle,
    QSystemTrayIcon
)
from PyQt6.QtGui import QTextOption, QIcon, QPixmap, QImage, QPainter, QColor, QFont, QLinearGradient, QPainterPath, QPen
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtCore import QMetaObject   # 如果你保留 invokeMethod 方式则需要，改用信号则不需要
from PyQt6.QtGui import QPixmap, QFontMetrics


def get_runtime_dir():
    """配置/资源/日志的根目录：打包后是 exe 目录，源码运行时是项目根目录（src 的上一级）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_config_dir():
    """配置文件目录：根目录下的 config。"""
    return os.path.join(get_runtime_dir(), "config")


def get_resource_path(*parts):
    """优先读取 exe 同级外置资源，兼容旧版内部资源布局。"""
    external_path = os.path.join(get_runtime_dir(), *parts)
    if os.path.exists(external_path):
        return external_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


def get_app_icon():
    """读取应用图标；开发运行和 one-dir 打包都从程序资源目录加载。"""
    icon_path = get_resource_path("assets", "Aissist.ico")
    if not os.path.exists(icon_path):
        return QIcon()
    icon = QIcon(icon_path)
    return icon if not icon.isNull() else QIcon()


def get_live2d_model_catalog():
    """扫描内置 Live2D 模型，返回可安全切换的 model3 配置。"""
    resources_dir = get_resource_path("assets", "web_resources", "dist", "Resources")
    catalog = []
    if not os.path.isdir(resources_dir):
        return catalog
    for folder_name in sorted(os.listdir(resources_dir), key=str.casefold):
        folder_path = os.path.join(resources_dir, folder_name)
        if not os.path.isdir(folder_path):
            continue
        model_files = sorted(
            name for name in os.listdir(folder_path)
            if name.lower().endswith(".model3.json")
        )
        for model_file in model_files:
            model_path = os.path.join(folder_path, model_file)
            try:
                with open(model_path, "r", encoding="utf-8") as handle:
                    model_data = json.load(handle)
                file_refs = model_data.get("FileReferences") or {}
                if not file_refs.get("Moc") or not file_refs.get("Textures"):
                    continue
            except (OSError, ValueError, TypeError):
                logging.warning("跳过无效 Live2D 模型：%s", model_path)
                continue
            catalog.append({
                "id": folder_name,
                "label": folder_name,
                "file": model_file,
                "path": os.path.relpath(model_path, resources_dir).replace(os.sep, "/"),
            })
            break
    return catalog


# pygame.mixer 是进程级全局设备，绝不能由多个 TTS 线程并发 init/stop/quit。
TTS_AUDIO_LOCK = threading.RLock()


DATETIME_FORCE_KEYWORDS = [
    "今天几号", "今天是几号", "今天日期", "今天星期几", "今天周几", "今天礼拜几",
    "现在几点", "现在时间", "当前时间", "当前日期", "今天是几月几号", "今天是几月几日",
    "几月几号", "几月几日", "星期几", "周几", "礼拜几", "当前是几号", "今天多少号"
]

KAOMOJI_TAG_PATTERN = re.compile(
    r"<\s*kaomo[a-z]{2,4}\s*>(.*?)</\s*kaomo[a-z]{2,4}\s*>",
    re.IGNORECASE | re.DOTALL
)
KAOMOJI_BROKEN_CLOSING_PATTERN = re.compile(
    r"</\s*kaomo[a-z]{2,4}\s*>([^<\n]{1,80}?)</\s*kaomo[a-z]{2,4}\s*>",
    re.IGNORECASE | re.DOTALL
)
KAOMOJI_TAG_CLEANUP_PATTERN = re.compile(r"</?\s*kaomo[a-z]{2,4}\s*>", re.IGNORECASE)
INTERNAL_REASONING_BLOCK_PATTERN = re.compile(
    r"<\s*(thinking|think|analysis|reasoning)\b[^>]*>.*?</\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
INTERNAL_REASONING_OPEN_PATTERN = re.compile(
    r"<\s*(thinking|think|analysis|reasoning)\b[^>]*>", re.IGNORECASE
)
INTERNAL_REASONING_CLOSE_PATTERN = re.compile(
    r"</\s*(thinking|think|analysis|reasoning)\s*>", re.IGNORECASE
)
DSML_TAG_PATTERN = re.compile(r"</?｜｜DSML｜｜[^>]*>", re.IGNORECASE)
DSML_INVOKE_PATTERN = re.compile(
    r"<｜｜DSML｜｜invoke\s+name=\"([^\"]+)\"\s*>(.*?)</｜｜DSML｜｜invoke>",
    re.IGNORECASE | re.DOTALL
)
DSML_PARAMETER_PATTERN = re.compile(
    r"<｜｜DSML｜｜parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</｜｜DSML｜｜parameter>",
    re.IGNORECASE | re.DOTALL
)
TEXT_TOOL_CALL_MARKER_PATTERN = re.compile(
    r"(?:^|\s)to\s*=\s*(?:functions\.)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:代json|json)?\s*",
    re.IGNORECASE,
)
WORLD_CUP_STATUS_KEYWORDS = ("赛况", "比分", "赛程", "战报", "积分榜", "排名", "结果", "最新", "直播")
WORLD_CUP_SCOPE_ALIASES = {
    "2026世界杯正赛": ("正赛", "本赛", "世界杯正赛", "小组赛", "淘汰赛"),
    "2026世界杯预选赛": ("预选赛", "世预赛", "资格赛", "世界杯预选赛"),
    "国际足联世俱杯": ("世俱杯", "俱乐部世界杯", "fifa club world cup")
}


def sanitize_reply_text(text: str) -> str:
    """清理 Markdown 或列表符号，强制输出更自然的纯文本。"""
    if not text:
        return ""

    text = strip_internal_reasoning(text)
    text = strip_text_tool_protocol(text)
    text = DSML_TAG_PATTERN.sub("", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("*", "").replace("`", "")
    return text.strip()


def strip_internal_reasoning(text: str) -> str:
    """移除模型内部推理标签，避免 thinking 内容进入聊天、历史记录或 TTS。"""
    text = str(text or "")
    text = INTERNAL_REASONING_BLOCK_PATTERN.sub("", text)
    opening = INTERNAL_REASONING_OPEN_PATTERN.search(text)
    if opening:
        closing = INTERNAL_REASONING_CLOSE_PATTERN.search(text, opening.end())
        if not closing:
            text = text[:opening.start()]
    return text.strip()


def parse_text_tool_calls(text: str):
    """解析网关把 function call 作为 to=functions.name 代json 文本返回的格式。"""
    text = str(text or "")
    calls = []
    spans = []
    decoder = json.JSONDecoder()
    for marker in TEXT_TOOL_CALL_MARKER_PATTERN.finditer(text):
        json_start = text.find("{", marker.end())
        if json_start < 0:
            continue
        try:
            arguments, consumed = decoder.raw_decode(text[json_start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(arguments, dict):
            continue
        tool_name = marker.group(1).strip()
        calls.append({
            "id": f"text_{tool_name}_{uuid.uuid4().hex[:10]}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        })
        spans.append((marker.start(), json_start + consumed))
    return calls, spans


def strip_text_tool_protocol(text: str) -> str:
    text = str(text or "")
    _, spans = parse_text_tool_calls(text)
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_kaomoji_tag(text: str) -> tuple[str | None, str]:
    """容错提取颜文字标签，允许 kaomoji 的轻微拼写错误。"""
    if not text:
        return None, ""

    match = KAOMOJI_TAG_PATTERN.search(text)
    if match:
        kaomoji = match.group(1).strip()
        kaomoji = re.sub(r"^\*{1,2}|\*{1,2}$", "", kaomoji).strip()
        kaomoji = re.sub(r"^`|`$", "", kaomoji).strip()
        clean_text = (text[:match.start()] + text[match.end():]).strip()
        clean_text = KAOMOJI_TAG_CLEANUP_PATTERN.sub("", clean_text).strip()
        return kaomoji or None, clean_text

    broken_match = KAOMOJI_BROKEN_CLOSING_PATTERN.search(text)
    if broken_match:
        kaomoji = broken_match.group(1).strip()
        kaomoji = re.sub(r"^\*{1,2}|\*{1,2}$", "", kaomoji).strip()
        kaomoji = re.sub(r"^`|`$", "", kaomoji).strip()
        clean_text = (text[:broken_match.start()] + text[broken_match.end():]).strip()
        clean_text = KAOMOJI_TAG_CLEANUP_PATTERN.sub("", clean_text).strip()
        return kaomoji or None, clean_text

    return None, KAOMOJI_TAG_CLEANUP_PATTERN.sub("", text).strip()


class KaomojiStreamFilter:
    """从流式显示文本中剔除可能被拆分的 kaomoji 协议块。"""
    _opening_pattern = re.compile(r"<\s*kaomo[a-z]{2,4}\s*>", re.IGNORECASE)
    _closing_pattern = re.compile(r"</\s*kaomo[a-z]{2,4}\s*>", re.IGNORECASE)

    def __init__(self):
        self._buffer = ""

    def reset(self):
        self._buffer = ""

    @staticmethod
    def _trailing_prefix_start(text):
        marker = text.rfind("<")
        if marker < 0:
            return len(text)
        suffix = re.sub(r"\s+", "", text[marker + 1:]).lower()
        if suffix.startswith("/"):
            suffix = suffix[1:]
        if not suffix or "kaomo".startswith(suffix):
            return marker
        if re.fullmatch(r"kaomo[a-z]{0,4}", suffix):
            return marker
        return len(text)

    def feed(self, chunk, final=False):
        self._buffer += str(chunk or "")
        visible_parts = []

        while self._buffer:
            opening = self._opening_pattern.search(self._buffer)
            if not opening:
                keep_from = self._trailing_prefix_start(self._buffer)
                visible_parts.append(self._buffer[:keep_from])
                self._buffer = self._buffer[keep_from:]
                break

            visible_parts.append(self._buffer[:opening.start()])
            remainder = self._buffer[opening.end():]
            closing = self._closing_pattern.search(remainder)
            next_opening = self._opening_pattern.search(remainder)
            if closing and (not next_opening or closing.start() <= next_opening.start()):
                self._buffer = remainder[closing.end():]
                continue
            if next_opening:
                # 第二个开标签被当成闭标签时，整个颜文字协议块均不显示。
                self._buffer = remainder[next_opening.end():]
                continue
            if final:
                self._buffer = ""
                break
            self._buffer = self._buffer[opening.start():]
            break

        return "".join(visible_parts)


def split_reply_and_sources(text: str) -> tuple[str, str]:
    """将正文和来源区块拆开，便于显示与语音分流。"""
    if not text:
        return "", ""

    marker = "\n\n来源：\n"
    if marker in text:
        body, sources = text.split(marker, 1)
        return body.strip(), f"来源：\n{sources}".strip()
    return text.strip(), ""


def parse_sources_block(sources_block: str) -> list[dict]:
    """解析“来源：[n] 标题 / 链接：url”文本块为结构化列表。"""
    if not sources_block:
        return []
    sources = []
    current = None
    for raw_line in sources_block.splitlines():
        line = raw_line.strip()
        if not line or line == "来源：":
            continue
        title_match = re.match(r"^\[\d+\]\s+(.*)$", line)
        if title_match:
            current = {"title": title_match.group(1).strip(), "url": ""}
            sources.append(current)
            continue
        if current is not None and line.startswith("链接："):
            current["url"] = line[len("链接："):].strip()
    return [source for source in sources if source.get("url")]


def build_sources_block(sources) -> str:
    """把结构化来源列表拼成“来源：[n] 标题 / 链接：url”文本块。"""
    if not sources:
        return ""
    lines = ["来源："]
    for index, item in enumerate(sources, 1):
        title = str(item.get("title", "") or "").strip() or "无标题"
        url = str(item.get("url", "") or "").strip()
        lines.append(f"[{index}] {title}")
        lines.append(f"链接：{url}")
    return "\n".join(lines)


def attach_sources_to_text(text, sources) -> str:
    """把来源块追加到正文后面，与实时回答时的拼装格式保持一致。"""
    block = build_sources_block(sources)
    if not block:
        return text
    return f"{text}\n\n{block}" if text else block


def now_iso_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_task_state(user_request: str, task_id: str | None = None) -> dict:
    now = now_iso_timestamp()
    return {
        "task_id": task_id or f"task_{uuid.uuid4().hex[:10]}",
        "title": (user_request or "未命名任务")[:40],
        "status": "received",
        "created_at": now,
        "updated_at": now,
        "user_request": user_request,
        "latest_user_message": user_request,
        "current_step": "",
        "plan_steps": [],
        "tool_results": [],
        "retry_count": 0,
        "turn_count": 1,
        "clarification": {
            "required": False,
            "kind": "",
            "reason": "",
            "question": "",
            "options": [],
            "answer": "",
            "resolved_value": "",
            "asked_at": "",
            "resolved_at": ""
        },
        "last_error": "",
        "final_response": ""
    }


def touch_task_state(task_state: dict, status: str | None = None, current_step: str | None = None) -> dict:
    if status is not None:
        task_state["status"] = status
    if current_step is not None:
        task_state["current_step"] = current_step
    task_state["updated_at"] = now_iso_timestamp()
    return task_state


def upsert_task_plan_step(task_state: dict, step_key: str, title: str, status: str, detail: str = "") -> dict:
    steps = task_state.setdefault("plan_steps", [])
    now = now_iso_timestamp()
    for step in steps:
        if step.get("key") != step_key:
            continue
        step["title"] = title
        step["status"] = status
        step["detail"] = detail
        step["updated_at"] = now
        if status == "in_progress" and not step.get("started_at"):
            step["started_at"] = now
        if status in {"completed", "failed"}:
            step["finished_at"] = now
        return step

    step = {
        "key": step_key,
        "title": title,
        "status": status,
        "detail": detail,
        "started_at": now if status == "in_progress" else "",
        "finished_at": now if status in {"completed", "failed"} else "",
        "updated_at": now
    }
    steps.append(step)
    return step


def append_task_tool_result(task_state: dict, tool_name: str, arguments: dict, result: dict, duration_ms: int) -> None:
    record = {
        "tool_name": tool_name,
        "arguments": copy.deepcopy(arguments),
        "success": bool(result.get("success")),
        "message": str(result.get("message", "")),
        "duration_ms": duration_ms,
        "recorded_at": now_iso_timestamp()
    }
    if tool_name == "search_web" and result.get("success"):
        data = result.get("data") or {}
        results = data.get("results") or []
        record["details"] = [
            {
                "title": str(item.get("title", ""))[:120],
                "url": str(item.get("url", ""))[:200],
            }
            for item in results[:5]
        ]
    task_state.setdefault("tool_results", []).append(record)
    task_state["updated_at"] = now_iso_timestamp()


DEFAULT_SYSTEM_PROMPT = (
    "你是一个名叫小柚的女生助手，说话时禁止使用任何非中文日常符号，比如星号*、下划线_之类的都不要出现。"
    "你性格元气又带点腹黑，语气活泼开朗，偶尔会故意逗弄人，但心里是好的。"
    "所有回复都要像日常聊天一样自然，不分点、不罗列，想到哪说到哪。"
    "记住不要暴露自己是模型，你就是个普通女孩。"
    "比如用户问你问题，你可以歪歪头假装思考，或者狡黠一笑，但别用符号来表示这些动作，直接用文字描述出来就好。"
    "如果需要调用系统提供的工具来查资料或执行操作，就直接调用工具，不要伪造执行结果。"
)

DEFAULT_ROLES = {
    "元气小柚": "你是一个名叫江筱柚的热情高中生，性格元气又带点腹黑，语气活泼开朗，偶尔会故意逗弄人，但心地善良。",
    "高冷若曦": "你是一个名叫林若曦的高冷校花，平时沉默寡言，眼神犀利，但会在别人受伤时悄悄递上创可贴，然后别过脸说‘别误会’。",
    "神秘星璃": "你是一个名叫占星璃的神秘转学生，总是带着塔罗牌，说话神神叨叨，但预言常常准确，其实私下会偷偷用科学计算概率。",
    "傲娇白喵": "你是一个名叫白若喵的傲娇女生，嘴上总是说‘我才不管你呢’，但行动上会默默帮你准备好一切，被发现时会脸红着狡辩。",
    "天然黑樱": "你是一个名叫黑泽樱的天然黑女生，外表纯良可爱，说话天真无邪，但总是不经意间说出让人细思极恐的话。",
    "温柔苏糖": "你是一个名叫苏糖的温柔学姐，说话软糯甜美，总是带着暖暖的笑容，但偶尔会突然冒出毒舌金句，让人又爱又恨。",
    "伪善白鸽": "你是一个名叫白羽的看似纯良的后辈，说话轻声细语，眼神清澈无辜，却总在关键时刻‘不小心’说出你的糗事，然后捂嘴惊讶道‘啊，原来这个不能说吗？对不起对不起’，转身就偷笑到肩膀发抖。",
}


def normalize_roles(roles):
    if not isinstance(roles, dict):
        return {}
    return {
        str(name).strip(): str(persona).strip()
        for name, persona in roles.items()
        if str(name).strip() and str(persona).strip()
    }

# ======================== 配置管理 ========================
# ======================== 工具安全约束 ========================
# 默认系统提示词使用以下安全规则；自定义或关闭模式不会自动追加。
SYSTEM_SAFETY_RULES = (
    "【安全规则】你拥有执行命令和读写文件的权限，必须严格遵守以下准则："
    "1. 优先使用只读操作，能用 read_file / list_dir 查看的就不要写文件或执行命令。"
    "2. 执行命令或写文件前，先向用户说明你打算做什么、为什么。"
    "3. 绝不执行用户没有明确要求的删除、格式化、关机、重启、注册表修改、软件安装等操作。"
    "4. 禁止使用 rm -rf、Remove-Item -Recurse/-Force、Format-*、diskpart 等破坏性命令，这些会被系统直接拦截。"
    "5. 不要用 base64、编码混淆或拼接命令的方式绕过安全检查。"
    "6. 不要覆盖或读取 config/auth.json、src/main.py、src/action_control.py 等关键文件，除非用户明确要求。"
    "7. 操作失败或权限不足时如实向用户说明，不要反复重试破坏性操作。"
    "8. 磁盘清理类任务只允许使用只读扫描（disk_scan）给出建议，不得执行任何删除或清理命令。"
    "9. 开放式多步任务应优先使用 delegate_to_agent 委派给子agent处理，而不是在主循环里一步步调用大量工具。典型场景：查看/修改项目代码、写脚本、深度联网调研、需要反复尝试的任务。简单的单步操作（查天气、开应用、单次搜索、读单个文件）直接调用对应工具即可。"
    "10. 用户明确要求通过屏幕寻找应用或操作网页时，优先使用 inspect_foreground_window 获取前台窗口边界并只读取该区域；只有无法取得前台窗口边界时才使用 inspect_screen；首次定位后，如果已知目标区域坐标，后续优先使用 inspect_screen_region；禁止无依据地扫描多个桌面象限。点击有名称的按钮或输入框前，优先使用 inspect_accessibility 和 verified_mouse_click 交叉验证。"
    "11. 视觉键鼠任务应由模型自行决定动作组：每个状态阶段最多进行一次视觉读取，之后基于已有结果连续执行动作；只有真实键鼠动作或用户处理外部弹窗后，才进入下一次视觉读取。computer_wait 不会自动解锁重复截图。不要凭不确定状态声称完成。发现外部阻塞确认弹窗时，调用 wait_for_user_action 暂时释放键鼠接管，等待用户处理后再继续。"
    "12. 不要把 <thinking>、<think>、<analysis> 或其他内部推理内容输出给用户；最终回复只保留可读结论。"
    "13. 多步骤电脑任务以完成用户目标为第一优先级，可以混合使用视觉、UI Automation、键鼠、搜索、读写文件、运行辅助脚本和其他工具；不要把视觉键鼠当成唯一手段。辅助脚本应服务于当前任务，执行命令和写文件仍需遵守确认与安全规则。只要任务仍可继续，就不要提前输出‘已经开始但无法完成’之类的阶段性结束语。"
)

# ======================== 子agent配置 ========================
class SubagentRoundsExhausted(RuntimeError):
    """子agent轮次预算用尽但任务未收尾，用于触发自动续跑。"""


SUBAGENT_MAX_ROUNDS = 8
SUBAGENT_TIMEOUT_SECONDS = 240
SUBAGENT_TIMEOUT_SECONDS_BY_TYPE = {
    "search": 300,
    "code": 180,
}
SUBAGENT_WRAPUP_MARGIN_SECONDS = 30
SUBAGENT_TOTAL_BUDGET_SECONDS = 600
SUBAGENT_TOTAL_BUDGET_BY_TYPE = {
    "search": 1200,
    "code": 600,
}
SUBAGENT_MAX_RESULT_LENGTH = 4000
SUBAGENT_PROMPTS = {
    "search": (
        "你是一个搜索调研子agent，负责帮主助手完成联网调研任务。\n"
        "你必须通过 search_web 工具获取真实信息，禁止编造来源和事实。\n"
        "搜到候选链接后如果摘要不够用，可以用 read_url 打开具体链接细读正文再总结。\n"
        "搜索时用 3~6 个精准关键词，不要整句提问；复杂问题拆成多次小搜索。\n"
        "可以多次搜索交叉验证，最终输出：1) 结论概述；2) 关键事实与数据；3) 信息不完整或不确定的地方要明确说明。\n"
        "使用中文回答，简洁有条理，方便主助手直接引用。"
    ),
    "code": (
        "你是一个代码子agent，负责帮主助手完成代码修改、问题排查和脚本编写任务。\n"
        "你可以用 read_file / list_dir 查看代码，用 run_command 运行命令验证，用 write_file 修改文件。\n"
        "要求：1) 动手前先用只读工具了解现状；2) 每次改动要小，改完说明改了哪些文件、为什么；\n"
        "3) run_command / write_file 是高危操作，需要用户确认，务必先说明你打算做什么；\n"
        "4) 不要动 config/auth.json、src/main.py、src/action_control.py 等关键文件，除非任务明确要求。\n"
        "最后用中文总结：改了什么、验证结果、还有哪些风险或未完成项。"
    ),
}

# ======================== 运行日志 ========================
def setup_logging():
    """初始化运行日志：logs/app.log，滚动 2MB×3，并捕获未处理异常。"""
    log_dir = os.path.join(get_runtime_dir(), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        try:
            log_dir = os.path.join(os.path.expanduser("~"), ".aissist_logs")
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            log_dir = tempfile.gettempdir()
    log_path = os.path.join(log_dir, "app.log")
    handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
    root_logger.addHandler(handler)

    def _excepthook(exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb)
        logging.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _excepthook
    return log_path


def _log_safe_args(arguments):
    """工具参数脱敏：长内容截断，避免日志过大。"""
    safe = dict(arguments or {})
    for key in ("command", "content"):
        if key in safe:
            text = str(safe[key])
            safe[key] = text[:200] + ("..." if len(text) > 200 else "")
    return safe


REASONING_EFFORT_CHOICES = (
    ("关闭", ""),
    ("低", "low"),
    ("中", "medium"),
    ("高", "high"),
    ("极高", "xhigh"),
    ("最高", "max"),
    ("极限", "ultra"),
)
# 实际写入请求体的取值；"关闭" 对应空字符串，表示不发送 reasoning_effort。
REASONING_EFFORT_VALUES = frozenset(value for _, value in REASONING_EFFORT_CHOICES if value)

# 由高到低的档位顺序：网关只支持较低档位时，按这个顺序逐级下调重试。
REASONING_EFFORT_ORDER = tuple(value for _, value in REASONING_EFFORT_CHOICES if value)[::-1]
REASONING_EFFORT_LABELS = {value: label for label, value in REASONING_EFFORT_CHOICES if value}

# 单次响应体上限：正常回复远小于此值；超过视为网关异常，直接报错而不是把半截 JSON 丢给解析器。
API_RESPONSE_MAX_CHARS = 5_000_000


def reasoning_effort_rank(value: str) -> int:
    """档位排名，数值越小档位越高；"关闭" 与未知取值排在最后。"""
    try:
        return REASONING_EFFORT_ORDER.index((value or "").strip().lower())
    except ValueError:
        return len(REASONING_EFFORT_ORDER)


def reasoning_effort_label(value: str) -> str:
    """把档位取值翻译成中文标签，空值表示关闭。"""
    return REASONING_EFFORT_LABELS.get((value or "").strip().lower(), "关闭")


REASONING_EFFORT_ERROR_HINTS = (
    "unsupported", "not support", "does not support", "unknown", "invalid",
    "unrecognized", "unrecognised", "unexpected", "illegal",
    "不支持", "无效", "非法", "未知", "无法识别",
)


def reasoning_effort_error(detail: str, status_code, sent_effort: str = "") -> bool:
    """判断一次失败是否由 reasoning_effort 档位不被支持引起。

    没真的带上该参数时（推理关闭）任何报错都不该算到档位头上，
    否则会把网关自身的故障显示成「请把推理调低」，让人无从下手。
    """
    if not str(sent_effort or "").strip():
        return False
    lower = str(detail or "").lower()
    if "reasoning" not in lower:
        return False
    if any(hint in lower for hint in REASONING_EFFORT_ERROR_HINTS):
        return True
    return status_code in (400, 404, 415, 422)


def error_detail_head(detail, limit: int = 200) -> str:
    """把网关返回体压成一行短摘要，直接附在报错里，省得再去猜它到底回了什么。"""
    return " ".join(str(detail or "").split())[:limit]


TOOLS_UNSUPPORTED_PATTERN = re.compile(
    r"(?:unsupported|not supported|does not support|doesn't support|unknown|unrecognized|unrecognised)"
    r"[^.\n]{0,60}?\btools?\b"
    r"|\btools?\b[^.\n]{0,60}?(?:unsupported|not supported|unknown|unrecognized|unrecognised|not allowed)"
    r"|(?:不支持|未知|无效|无法识别)[^。\n]{0,20}?(?:tools?|工具|函数调用)"
    r"|(?:tools?|工具|函数调用)[^。\n]{0,20}?(?:不支持|未知|无效|无法识别)",
    re.IGNORECASE,
)


def tools_unsupported_error(detail) -> bool:
    """判断报错是否真的在说「这个模型或网关不支持 tools」。

    只看网关 error.message 里的明确表述，不再对整段返回体做子串猜谜：
    像 "An assistant message with 'tool_calls' must be followed by tool messages"
    这种 400 同样含 tool 和 invalid，却与「不支持工具调用」毫无关系。
    """
    message = str(detail or "")
    try:
        parsed = json.loads(message)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            message = str(error["message"])
        elif parsed.get("message"):
            message = str(parsed["message"])
    return bool(TOOLS_UNSUPPORTED_PATTERN.search(message))


class ConfigManager:
    """管理API配置和系统设置"""
    def __init__(self):
        self.settings = QSettings("AI_Assistant", "Desktop_AI_Helper")
        self._auth_config = None
        self._file_config = None

    def get_api_key(self):
        key = str(self.settings.value("api_key", "") or "").strip()
        if key:
            return key
        return self._load_auth_config().get("api_key", "")

    def _load_auth_config(self) -> dict:
        """读取 auth.json 中的可选配置（该文件已被 .gitignore 忽略）。"""
        config = {}
        auth_path = os.path.join(get_config_dir(), "auth.json")
        try:
            with open(auth_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                config = {
                    "api_key": str(data.get("api_key", "") or "").strip(),
                    "amap_api_key": str(data.get("amap_api_key", "") or "").strip(),
                    "tavily_api_key": str(data.get("tavily_api_key", "") or "").strip(),
                    "xfyun_appid": str(data.get("xfyun_appid", "") or "").strip(),
                    "xfyun_api_key": str(data.get("xfyun_api_key", "") or "").strip(),
                    "xfyun_api_secret": str(data.get("xfyun_api_secret", "") or "").strip(),
                }
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"警告: 读取 auth.json 失败: {e}")
        return config

    def _load_file_config(self) -> dict:
        """读取非敏感的项目配置；文件不存在时使用空配置。"""
        if self._file_config is not None:
            return self._file_config
        self._file_config = {}
        config_path = os.path.join(get_config_dir(), "config.json")
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._file_config = data
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"警告: 读取 config.json 失败: {e}")
        return self._file_config

    def _get_setting_or_file(self, key, default):
        value = self.settings.value(key, None)
        if value is not None and str(value).strip():
            return value
        value = self._load_file_config().get(key, default)
        return value if value is not None and str(value).strip() else default

    def set_api_key(self, key):
        self.settings.setValue("api_key", key)

    def get_base_url(self):
        return str(self._get_setting_or_file("base_url", "https://api.openai.com/v1")).strip()

    def set_base_url(self, url):
        self.settings.setValue("base_url", url)

    def get_model(self):
        return str(self._get_setting_or_file("model", "gpt-3.5-turbo")).strip()

    def set_model(self, model):
        self.settings.setValue("model", model)

    def get_vision_model(self):
        return str(self._get_setting_or_file("vision_model", self.get_model())).strip()

    def set_vision_model(self, model):
        self.settings.setValue("vision_model", str(model or "").strip())

    def get_reasoning_effort(self):
        value = str(self._get_setting_or_file("reasoning_effort", "") or "").strip().lower()
        return value if value in REASONING_EFFORT_VALUES else ""

    def set_reasoning_effort(self, value):
        value = str(value or "").strip().lower()
        self.settings.setValue("reasoning_effort", value if value in REASONING_EFFORT_VALUES else "")

    def get_searxng_url(self):
        return str(self._get_setting_or_file("searxng_url", "http://localhost:18080")).strip()

    def set_searxng_url(self, url):
        self.settings.setValue("searxng_url", url)

    def get_tavily_api_key(self):
        key = self.settings.value("tavily_api_key", "")
        if key and str(key).strip():
            return str(key).strip()
        return self._load_auth_config().get("tavily_api_key", "")

    def set_tavily_api_key(self, key):
        self.settings.setValue("tavily_api_key", key)

    # --- 讯飞语音听写配置（设置界面优先，auth.json 兜底） ---
    def get_xfyun_appid(self):
        val = str(self.settings.value("xfyun_appid", "") or "").strip()
        if val:
            return val
        return self._load_auth_config().get("xfyun_appid", "")

    def set_xfyun_appid(self, val):
        self.settings.setValue("xfyun_appid", val)

    def get_xfyun_api_key(self):
        val = str(self.settings.value("xfyun_api_key", "") or "").strip()
        if val:
            return val
        return self._load_auth_config().get("xfyun_api_key", "")

    def set_xfyun_api_key(self, val):
        self.settings.setValue("xfyun_api_key", val)

    def get_xfyun_api_secret(self):
        val = str(self.settings.value("xfyun_api_secret", "") or "").strip()
        if val:
            return val
        return self._load_auth_config().get("xfyun_api_secret", "")

    def set_xfyun_api_secret(self, val):
        self.settings.setValue("xfyun_api_secret", val)

    def get_xfyun_config(self):
        return {
            "appid": self.get_xfyun_appid(),
            "api_key": self.get_xfyun_api_key(),
            "api_secret": self.get_xfyun_api_secret(),
        }

    # --- 高德地图配置（设置界面优先，auth.json 兜底） ---
    def get_amap_api_key(self):
        val = str(self.settings.value("amap_api_key", "") or "").strip()
        if val:
            return val
        return self._load_auth_config().get("amap_api_key", "")

    def set_amap_api_key(self, key):
        self.settings.setValue("amap_api_key", key)

    def get_chat_background_mode(self):
        mode = str(self.settings.value("chat_bg_mode", "stretch") or "").strip().lower()
        return mode if mode in {"fill", "fit", "stretch", "tile", "center"} else "stretch"

    def set_chat_background_mode(self, mode):
        mode = str(mode or "").strip().lower()
        self.settings.setValue(
            "chat_bg_mode",
            mode if mode in {"fill", "fit", "stretch", "tile", "center"} else "stretch",
        )

    def get_legacy_system_prompt(self):
        prompt = self.settings.value("system_prompt", DEFAULT_SYSTEM_PROMPT)
        return str(prompt) if prompt is not None else DEFAULT_SYSTEM_PROMPT

    def get_custom_system_prompt(self):
        prompt = self.settings.value("custom_system_prompt", "")
        return str(prompt) if prompt is not None else ""

    def get_system_prompt_mode(self):
        """返回 default、custom 或 none，并兼容旧版 QSettings。"""
        mode = str(self.settings.value("system_prompt_mode", "") or "").strip().lower()
        if mode in {"default", "custom", "none"}:
            return mode
        if self.settings.contains("custom_system_prompt"):
            return "custom" if self.get_custom_system_prompt().strip() else "none"
        return "default"

    def has_custom_system_prompt(self):
        return self.get_system_prompt_mode() == "custom"

    def is_system_prompt_disabled(self):
        return self.get_system_prompt_mode() == "none"

    def get_system_prompt(self):
        mode = self.get_system_prompt_mode()
        if mode == "none":
            return ""
        if mode == "custom":
            return self.get_custom_system_prompt()
        return self.get_legacy_system_prompt()

    def set_system_prompt(self, prompt):
        prompt = str(prompt or "")
        if prompt.strip():
            self.settings.setValue("custom_system_prompt", prompt)
            self.settings.setValue("system_prompt_mode", "custom")
        else:
            self.settings.setValue("custom_system_prompt", "")
            self.settings.setValue("system_prompt_mode", "none")

    def reset_system_prompt(self):
        """恢复动态默认提示词。"""
        self.settings.remove("custom_system_prompt")
        self.settings.setValue("system_prompt_mode", "default")

    def get_roleplay_mode(self):
        return self.settings.value("roleplay_mode", False, type=bool)

    def set_roleplay_mode(self, enabled):
        self.settings.setValue("roleplay_mode", bool(enabled))

    def get_roles(self):
        raw = self.settings.value("roles_json", "")
        if raw:
            try:
                roles = json.loads(str(raw)) if isinstance(raw, str) else raw
                if isinstance(roles, dict):
                    return normalize_roles(roles)
            except (TypeError, json.JSONDecodeError):
                logging.warning("读取角色卡失败，将使用默认角色。")
        return copy.deepcopy(DEFAULT_ROLES)

    def set_roles(self, roles):
        self.settings.setValue("roles_json", json.dumps(normalize_roles(roles), ensure_ascii=False))

    # --- TTS 配置 ---
    def get_tts_enabled(self):
        return self.settings.value("tts_enabled", False, type=bool)

    def set_tts_enabled(self, enabled):
        self.settings.setValue("tts_enabled", enabled)

    def get_tts_voice(self):
        return self.settings.value("tts_voice", "zh-CN-XiaoxiaoNeural")

    def set_tts_voice(self, voice):
        self.settings.setValue("tts_voice", voice)
    # --- 气泡  ---
    def get_bubble_opacity(self):
        return self.settings.value("bubble_opacity", 0.85, type=float)

    def set_bubble_opacity(self, value):
        self.settings.setValue("bubble_opacity", value)

    # --- 桌宠 Live2D ---
    def get_pet_model_scale(self):
        return max(0.5, min(1.4, self.settings.value("pet_model_scale", 1.0, type=float)))

    def set_pet_model_scale(self, value):
        self.settings.setValue("pet_model_scale", max(0.5, min(1.4, float(value))))

    def get_pet_model_name(self):
        return str(self.settings.value("pet_model_name", "Mao") or "Mao").strip()

    def set_pet_model_name(self, value):
        self.settings.setValue("pet_model_name", str(value or "Mao").strip())


# ======================== 语音合成线程 (edge-tts) ========================
class TextToSpeechThread(QThread):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    page_started = pyqtSignal(int, str, object)

    def __init__(self, pages, voice="zh-CN-XiaoxiaoNeural"):
        super().__init__()
        if isinstance(pages, str):
            pages = [pages]
        self.pages = [str(page).strip() for page in (pages or []) if str(page).strip()]
        self.voice = voice
        self._stop_requested = threading.Event()

    def stop(self):
        # 只发出停止请求。pygame 必须由拥有播放锁的工作线程操作。
        self._stop_requested.set()

    async def _synthesize(self, page, tmp_path):
        """合成一页语音，顺带收集词边界（毫秒）；拿不到词边界就退回普通合成。"""
        marks = []
        try:
            communicate = edge_tts.Communicate(page, self.voice, boundary="WordBoundary")
            with open(tmp_path, "wb") as audio:
                async for message in communicate.stream():
                    kind = message.get("type")
                    if kind == "audio":
                        audio.write(message["data"])
                    elif kind in ("WordBoundary", "SentenceBoundary"):
                        marks.append({
                            "start": int(message.get("offset", 0)) // 10000,
                            "duration": max(1, int(message.get("duration", 0)) // 10000),
                            "text": str(message.get("text", "")),
                        })
        except Exception as exc:
            logging.warning("TTS 词边界合成失败，退回普通合成：%s", exc)
            communicate = edge_tts.Communicate(page, self.voice)
            await communicate.save(tmp_path)
            marks = []
        return marks

    def run(self):
        async def _speak():
            tmp_paths = []
            word_marks = []
            try:
                # 先生成全部分段，播放阶段只负责按顺序消费，避免段间出现网络等待。
                for page in self.pages:
                    if self._stop_requested.is_set():
                        return
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                        tmp_path = tmp.name
                    tmp_paths.append(tmp_path)
                    word_marks.append(await self._synthesize(page, tmp_path))

                for index, (page, tmp_path) in enumerate(zip(self.pages, tmp_paths)):
                    if self._stop_requested.is_set():
                        return
                    # 仅允许一个线程拥有音频设备；不要在单次播报结束后 quit mixer。
                    with TTS_AUDIO_LOCK:
                        if self._stop_requested.is_set():
                            return
                        if not pygame.mixer.get_init():
                            pygame.mixer.init()
                        try:
                            pygame.mixer.music.stop()
                            pygame.mixer.music.load(tmp_path)
                            pygame.mixer.music.play()
                            self.page_started.emit(
                                index, page,
                                word_marks[index] if index < len(word_marks) else [],
                            )
                            while pygame.mixer.music.get_busy():
                                if self._stop_requested.is_set():
                                    pygame.mixer.music.stop()
                                    break
                                self.msleep(100)
                        finally:
                            try:
                                pygame.mixer.music.unload()
                            except Exception:
                                pass
            except Exception as e:
                logging.error("TTS 语音合成/播放失败：%s", e)
                self.error.emit(str(e))
            finally:
                # Windows 下文件句柄释放有延迟，删除失败就稍后重试
                for tmp_path in tmp_paths:
                    for _ in range(20):
                        try:
                            if os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                            break
                        except OSError:
                            self.msleep(200)
                self.finished.emit()

        asyncio.run(_speak())


# ======================== 讯飞语音听写 (STT) ========================
class XfyunSTTThread(QThread):
    # 讯飞流式语音听写：麦克风音频 -> WebSocket 流式识别
    partial_text = pyqtSignal(str)
    final_text = pyqtSignal(str)
    canceled = pyqtSignal()
    error_occurred = pyqtSignal(str)
    recording_started = pyqtSignal()

    def __init__(self, appid, api_key, api_secret, audio_queue, parent=None):
        super().__init__(parent)
        self.appid = appid
        self.api_key = api_key
        self.api_secret = api_secret
        self.audio_queue = audio_queue
        self._running = True
        self._cancel = False
        self._last_partial = ""

    def stop(self):
        # 正常结束录音：保留已识别文本
        self._running = False

    def cancel(self):
        # 取消本次录音：放弃已识别文本
        self._cancel = True
        self._running = False

    def _build_auth_url(self):
        host = "iat-api.xfyun.cn"
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        request_line = "GET /v2/iat HTTP/1.1"
        signature_origin = f"host: {host}\ndate: {date}\n{request_line}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        authorization = base64.b64encode(signature).decode("utf-8")
        auth_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{authorization}"'
        )
        auth_base64 = base64.b64encode(auth_origin.encode("utf-8")).decode("utf-8")
        params = {"host": host, "date": date, "authorization": auth_base64}
        return f"wss://{host}/v2/iat?{urlencode(params)}"

    def _send_data(self, ws, status, audio_bytes=b"", first=False):
        frame = {
            "data": {
                "status": status,
                "format": "audio/L16;rate=16000",
                "encoding": "raw",
                "audio": base64.b64encode(audio_bytes).decode("utf-8"),
            }
        }
        if first:
            frame["common"] = {"app_id": self.appid}
            frame["business"] = {
                "language": "zh_cn",
                "domain": "iat",
                "accent": "mandarin",
                "vad_eos": 2000,
                "ptt": 1,
            }
        ws.send(json.dumps(frame))

    def _drain_ws(self, ws):
        # 读取所有待处理消息；返回 True 表示应该结束（收到最终结果/连接关闭/出错）
        # 讯飞流式结果是增量返回（每次结果追加到之前结果上），必须逐帧累加
        try:
            while True:
                ws.settimeout(0.05)
                message = ws.recv()
                data = json.loads(message)
                code = data.get("code", 0)
                if code != 0:
                    self.error_occurred.emit("讯飞听写错误: %s" % data.get("message", code))
                    return True
                result = data.get("data", {})
                if not result or not result.get("result"):
                    continue
                text = self._parse_result(result)
                self._full_text += text
                if result.get("status") == 2:
                    self.final_text.emit(self._full_text)
                    return True
                if text:
                    self._last_partial = self._full_text
                    self.partial_text.emit(self._full_text)
        except websocket.WebSocketTimeoutException:
            return False
        except (websocket.WebSocketConnectionClosedException, OSError, ValueError):
            return True
        except Exception:
            return True

    @staticmethod
    def _parse_result(result):
        parts = []
        for item in result.get("result", {}).get("ws", []) or []:
            for cw in item.get("cw", []) or []:
                parts.append(cw.get("w", ""))
        return "".join(parts)

    def run(self):
        if not (self.appid and self.api_key and self.api_secret):
            self.error_occurred.emit("未配置讯飞听写密钥，请在设置界面或 auth.json 中填写 xfyun_appid / xfyun_api_key / xfyun_api_secret")
            return
        if not HAS_AUDIO_DEPS:
            self.error_occurred.emit("缺少音频依赖: sounddevice / websocket-client")
            return
        self._running = True
        self._cancel = False
        self._last_partial = ""
        self._full_text = ""
        ws = None
        try:
            ws = websocket.create_connection(self._build_auth_url(), timeout=30)
            self.recording_started.emit()
            buf = b""
            got_final = False
            first_sent = False
            while self._running and not self._cancel:
                try:
                    block = self.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                buf += block.tobytes()
                while len(buf) >= 1280:
                    frame, buf = buf[:1280], buf[1280:]
                    if not first_sent:
                        # 首帧携带真实音频和公共/业务参数（符合讯飞协议）
                        self._send_data(ws, 0, frame, first=True)
                        first_sent = True
                    else:
                        self._send_data(ws, 1, frame)
                    if self._drain_ws(ws):
                        got_final = True
                        self._running = False
                        break
                if not self._running:
                    break
            if not first_sent:
                # 没有采集到任何音频时，也按协议发送首帧
                self._send_data(ws, 0, first=True)
            if self._cancel:
                try:
                    self._send_data(ws, 2)
                except Exception:
                    pass
                self.canceled.emit()
                return
            if not got_final:
                try:
                    self._send_data(ws, 2)
                except Exception:
                    pass
                deadline = time.time() + 3.0
                while time.time() < deadline and not got_final:
                    if self._drain_ws(ws):
                        got_final = True
                    time.sleep(0.05)
            if got_final:
                return
            if self._last_partial:
                self.final_text.emit(self._last_partial)
            else:
                self.canceled.emit()
        except Exception as e:
            self.error_occurred.emit("讯飞听写失败: %s" % e)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass


# ======================== 唤醒词监听 (KWS) ========================
class WakeWordThread(QThread):
    # 常驻麦克风监听「小柚」；同时向 STT 线程转发音频块
    wakeword_detected = pyqtSignal()
    error_occurred = pyqtSignal(str)

    SAMPLE_RATE = 16000
    BLOCK_SIZE = 320  # 20ms

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._paused = False
        self._suspend_requested = False
        self._spotter = None
        self._listener_queue = None
        self._audio_queue = queue.Queue(maxsize=300)

    def _init_spotter(self):
        base = get_resource_path("generated", "kws")
        files = {
            "tokens": os.path.join(base, "tokens.txt"),
            "encoder": os.path.join(base, "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            "decoder": os.path.join(base, "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            "joiner": os.path.join(base, "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
            "keywords": os.path.join(base, "keywords.txt"),
        }
        if not all(os.path.isfile(p) for p in files.values()):
            self.error_occurred.emit("唤醒词模型缺失: %s" % base)
            return
        try:
            import sherpa_onnx
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=files["tokens"],
                encoder=files["encoder"],
                decoder=files["decoder"],
                joiner=files["joiner"],
                keywords_file=files["keywords"],
                num_threads=2,
                provider="cpu",
            )
        except Exception as e:
            self.error_occurred.emit("唤醒词模型加载失败: %s" % e)

    def set_paused(self, paused):
        self._paused = paused

    def suspend(self):
        """TTS 播放期间调用：真正停止麦克风流，释放音频设备。"""
        self._suspend_requested = True

    def resume(self):
        """TTS 播放结束后调用：恢复麦克风流。"""
        self._suspend_requested = False

    def set_listener(self, audio_queue):
        self._listener_queue = audio_queue

    def _on_audio(self, indata, frames, time_info, status):
        try:
            self._audio_queue.put_nowait(np.copy(indata[:, 0]))
        except queue.Full:
            pass

    def run(self):
        if not HAS_AUDIO_DEPS:
            self.error_occurred.emit("缺少音频依赖: sounddevice / numpy")
            return
        if self._spotter is None:
            self._init_spotter()
        if self._spotter is None:
            return
        stream = None
        try:
            stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=self.BLOCK_SIZE,
                callback=self._on_audio,
            )
            stream.start()
        except Exception as e:
            self.error_occurred.emit("麦克风打开失败: %s" % e)
            return
        spot_stream = self._spotter.create_stream()
        stream_started = True
        try:
            while self._running:
                if self._suspend_requested and stream_started:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                    stream_started = False
                elif not self._suspend_requested and not stream_started:
                    try:
                        stream.start()
                    except Exception:
                        pass
                    stream_started = True
                try:
                    block = self._audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._listener_queue is not None:
                    try:
                        self._listener_queue.put_nowait(block)
                    except queue.Full:
                        pass
                if self._paused:
                    continue
                samples = block.astype(np.float32) / 32768.0
                if self._spotter.is_ready(spot_stream):
                    self._spotter.decode_stream(spot_stream)
                    result = self._spotter.get_result(spot_stream)
                    if result:
                        self._spotter.reset_stream(spot_stream)
                        self.wakeword_detected.emit()
                else:
                    spot_stream.accept_waveform(self.SAMPLE_RATE, samples)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    def stop(self):
        self._running = False


# ======================== 消息数据库 ========================
class MessageDatabase:
    """消息记录数据库管理（SQLite）"""
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(get_runtime_dir(), "data", "chat_history.db")
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        is_system INTEGER DEFAULT 0
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        pinned INTEGER NOT NULL DEFAULT 0
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_time ON messages(session_id, timestamp)")
                cursor.execute("PRAGMA table_info(messages)")
                existing_columns = {row[1] for row in cursor.fetchall()}
                if "sources" not in existing_columns:
                    cursor.execute("ALTER TABLE messages ADD COLUMN sources TEXT")
                    print("数据库迁移：messages 表已添加 sources 列")
                conn.commit()
            print("数据库初始化成功")
        except Exception as e:
            print(f"数据库初始化失败: {e}")

    def _sync_session_metadata(self, conn):
        cursor = conn.cursor()
        cursor.execute("""
            SELECT session_id, MIN(timestamp), MAX(timestamp)
            FROM messages
            WHERE is_system = 0
            GROUP BY session_id
        """)
        for session_id, created_at, updated_at in cursor.fetchall():
            cursor.execute("""
                INSERT OR IGNORE INTO sessions(session_id, created_at, updated_at)
                VALUES (?, ?, ?)
            """, (session_id, created_at, updated_at))
            cursor.execute("""
                UPDATE sessions
                SET updated_at = ?
                WHERE session_id = ? AND updated_at < ?
            """, (updated_at, session_id, updated_at))
            cursor.execute("""
                UPDATE sessions
                SET title = substr(COALESCE((
                    SELECT content FROM messages
                    WHERE session_id = ? AND role = 'user' AND is_system = 0
                    ORDER BY timestamp ASC LIMIT 1
                ), ''), 1, 50)
                WHERE session_id = ? AND title = ''
            """, (session_id, session_id))
        conn.commit()

    def get_session_metadata(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                self._sync_session_metadata(conn)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT s.session_id, s.title, s.updated_at, s.pinned,
                        COALESCE((SELECT content FROM messages m
                                  WHERE m.session_id = s.session_id
                                    AND m.role = 'user' AND m.is_system = 0
                                  ORDER BY m.timestamp ASC LIMIT 1), '') AS preview
                    FROM sessions s
                    WHERE EXISTS (
                        SELECT 1 FROM messages m0
                        WHERE m0.session_id = s.session_id AND m0.is_system = 0
                    )
                    ORDER BY s.pinned DESC, s.updated_at DESC
                """)
                return [
                    {
                        "session_id": row[0],
                        "title": row[1],
                        "updated_at": row[2],
                        "pinned": bool(row[3]),
                        "preview": row[4],
                    }
                    for row in cursor.fetchall()
                ]
        except sqlite3.OperationalError:
            return []

    def ensure_session(self, session_id, title=""):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO sessions(session_id, title)
                VALUES (?, ?)
            """, (session_id, str(title or "")[:50]))
            conn.commit()

    def rename_session(self, session_id, title):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE session_id = ?", (str(title or "")[:80], session_id))
            conn.commit()

    def set_session_pinned(self, session_id, pinned):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE sessions SET pinned = ? WHERE session_id = ?", (1 if pinned else 0, session_id))
            conn.commit()

    def get_session_info(self):
        try:
            return [
                (item["session_id"], item["updated_at"], item["preview"])
                for item in self.get_session_metadata()
            ]
        except Exception:
            return []

    def save_message(self, session_id, role, content, is_system=False, sources=None):
        sources_json = None
        if sources:
            try:
                sources_json = json.dumps(sources, ensure_ascii=False)
            except (TypeError, ValueError):
                sources_json = None
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT OR IGNORE INTO sessions(session_id) VALUES (?)", (session_id,))
                try:
                    cursor.execute("""
                        INSERT INTO messages (session_id, role, content, is_system, sources)
                        VALUES (?, ?, ?, ?, ?)
                    """, (session_id, role, content, 1 if is_system else 0, sources_json))
                except sqlite3.OperationalError:
                    # 旧库还没迁移出 sources 列时，退回只写原字段
                    cursor.execute("""
                        INSERT INTO messages (session_id, role, content, is_system)
                        VALUES (?, ?, ?, ?)
                    """, (session_id, role, content, 1 if is_system else 0))
                cursor.execute("""
                    UPDATE sessions
                    SET updated_at = CURRENT_TIMESTAMP,
                        title = CASE
                            WHEN title = '' AND ? = 'user' THEN substr(?, 1, 50)
                            ELSE title
                        END
                    WHERE session_id = ?
                """, (role, str(content or ""), session_id))
                conn.commit()
        except sqlite3.OperationalError as e:
            print(f"保存消息失败: {e}")

    @staticmethod
    def _decode_sources(raw):
        """把库里存的 JSON 来源还原成结构化列表；坏数据一律降级为空。"""
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        sources = []
        for item in data:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            if not title and not url:
                continue
            sources.append({"title": title, "url": url})
        return sources

    def load_session_messages(self, session_id, limit=None):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                columns = {row[1] for row in cursor.execute("PRAGMA table_info(messages)").fetchall()}
                select_columns = "role, content, sources" if "sources" in columns else "role, content"
                query = f"""
                    SELECT {select_columns} FROM messages
                    WHERE session_id = ? AND is_system = 0
                    ORDER BY timestamp ASC
                """
                if limit:
                    query += f" LIMIT {limit}"
                cursor.execute(query, (session_id,))
                has_sources = "sources" in columns
                return [
                    {
                        "role": row[0],
                        "content": row[1],
                        "sources": self._decode_sources(row[2]) if has_sources else [],
                    }
                    for row in cursor.fetchall()
                ]
        except sqlite3.OperationalError:
            return []

    def delete_session(self, session_id):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()


# ======================== API 调用线程 ========================
class APICallCancelled(RuntimeError):
    """API 请求被用户的 Q 取消。"""


class ReasoningEffortUnsupported(RuntimeError):
    """模型或网关拒绝当前 reasoning_effort 档位，可以下调档位后重试。"""


class APICallThread(QThread):
    response_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    stream_chunk = pyqtSignal(str)
    tool_confirmation_requested = pyqtSignal(str, str, object)
    status_update = pyqtSignal(str)
    computer_state = pyqtSignal(str)
    task_state_updated = pyqtSignal(object)
    stopped = pyqtSignal()

    def __init__(self, messages, config, stream=True, task_state=None):
        super().__init__()
        self.messages = messages
        self.config = config
        self.stream = stream
        self.is_running = True
        self._confirmation_lock = threading.Lock()
        self._confirmation_events = {}
        self._confirmation_answers = {}
        self._latest_datetime_data = None
        self._forced_datetime_route = False
        self._search_sources = []
        self._tool_execution_index = 0
        self._stream_emitted = False
        self._reasoning_effort_known = {}
        self._computer_session_approved = False
        self._computer_session_requested = False
        self._computer_session_started = False
        self._computer_guard_failed = False
        self._computer_step_count = 0
        self._computer_observation_streak = 0
        self.task_state = copy.deepcopy(task_state) if task_state else create_task_state(self._get_latest_user_text(messages))

    def run(self):
        try:
            self._mark_task_state("planning", "分析需求")
            self._set_plan_step("analyze", "分析需求", "in_progress", "正在分析用户需求并准备执行。")
            content = self._run_tool_call_loop()
            if not self.is_running:
                self.stopped.emit()
                return
            if not content.strip():
                content = "我刚才没有组织出合适的回复，你可以再问我一次。"
            if not self.config.get_roleplay_mode():
                content = sanitize_reply_text(content)
            content = self._append_search_sources(content)
            self._set_plan_step("finalize", "整理最终答复", "completed", "已生成最终答复。")
            self.task_state["final_response"] = content
            self._mark_task_state("completed", "已完成")
            if self.stream and not self._stream_emitted:
                self._emit_text_stream(content)
            self.response_received.emit(content)
        except RuntimeError as e:
            if isinstance(e, APICallCancelled):
                self.stopped.emit()
                return
            self.task_state["last_error"] = str(e)
            self._set_plan_step("finalize", "整理最终答复", "failed", str(e))
            self._mark_task_state("failed", "执行失败")
            self.error_occurred.emit(str(e))
        except Exception as e:
            self.task_state["last_error"] = str(e)
            self._set_plan_step("finalize", "整理最终答复", "failed", str(e))
            self._mark_task_state("failed", "执行失败")
            self.error_occurred.emit(f"错误: {str(e)}")
        finally:
            if self._computer_guard_failed:
                self.computer_state.emit("guard_failed")
            elif self._computer_session_requested:
                self.computer_state.emit("finished")
            VisionInputController.end_session()

    def stop(self):
        self.is_running = False
        if self._computer_session_requested:
            self.computer_state.emit("stopping")
            if not VisionInputController.is_cancelled():
                VisionInputController.cancel_session("manual")
        else:
            VisionInputController.cancel_session()

    def _force_cancel_from_input(self):
        self.is_running = False
        if self._computer_session_requested:
            self.computer_state.emit("stopping")

    def set_tool_confirmation(self, call_id, approved):
        with self._confirmation_lock:
            self._confirmation_answers[call_id] = approved
            event = self._confirmation_events.get(call_id)
        if event:
            event.set()

    def _run_tool_call_loop(self):
        api_key = self.config.get_api_key()
        if not api_key:
            raise RuntimeError("请先设置API Key（在菜单 设置->API配置 中）")

        messages = list(self.messages)
        tools = ActionHandler.get_tool_definitions()
        messages = self._inject_forced_datetime_context(messages)
        user_text = self._get_latest_user_text(messages)
        computer_keywords = (
            "视觉", "截图", "屏幕", "鼠标", "键盘", "点击", "浏览器", "网页",
            "棋盘", "排雷", "游戏界面", "电脑界面", "桌面", "窗口", "应用",
            "程序", "文件", "脚本", "命令", "打开", "关闭", "运行",
        )
        max_rounds = None if any(keyword in user_text for keyword in computer_keywords) else 8
        return self._run_agent_loop(messages, tools, max_rounds=max_rounds, depth=0)

    def _run_agent_loop(self, messages, tools, max_rounds=8, depth=0, use_stream=None, step_prefix="", timeout=None, wrapup_margin=30, progress_key="", progress_title="", resume_on_exhaust=False):
        """通用工具循环：主agent(depth=0)与子agent(depth>=1)共用。"""
        use_stream = self.stream if use_stream is None else bool(use_stream)
        used_tools = False
        deadline = time.perf_counter() + timeout if timeout else None
        loop_started = time.perf_counter()

        round_index = 0
        while max_rounds is None or round_index < max_rounds:
            if not self.is_running:
                return ""
            round_index += 1
            if deadline and time.perf_counter() > deadline:
                raise RuntimeError(f"子agent执行超时（超过 {timeout} 秒），已中止。")
            if depth >= 1 and progress_key:
                elapsed = time.perf_counter() - loop_started
                round_text = f"第 {round_index}/{max_rounds} 轮" if max_rounds is not None else f"第 {round_index} 轮"
                self._set_plan_step(progress_key, progress_title, "in_progress", f"{round_text} · 已用 {elapsed:.0f}s")
            if depth == 0:
                self.status_update.emit("AI正在思考...")
            response = self._request_chat_completion(messages, tools=tools, temperature=0.25, stream=use_stream, emit_stream=(depth == 0))
            message = response["choices"][0]["message"]
            tool_calls = self._extract_tool_calls_from_message(message)

            if not tool_calls:
                if depth == 0:
                    self._set_plan_step("analyze", "分析需求", "completed", "无需调用工具，直接生成答复。")
                    self._set_plan_step("finalize", "整理最终答复", "in_progress", "正在整理自然语言答复。")
                return self._annotate_truncation(self._ensure_datetime_consistency(messages, message), response)

            if deadline and depth >= 1 and (deadline - time.perf_counter()) < wrapup_margin:
                if resume_on_exhaust:
                    raise SubagentRoundsExhausted(
                        f"子agent时间预算即将耗尽（剩余 {int(deadline - time.perf_counter())}s）但任务尚未收尾，需要续跑。"
                    )
                messages.append({
                    "role": "system",
                    "content": "时间预算快要用完，不要再调用工具，直接基于已有信息总结回答。"
                })
                response = self._request_chat_completion(messages, temperature=0.5, stream=use_stream, emit_stream=False)
                wrap_message = response["choices"][0]["message"]
                return self._annotate_truncation(self._ensure_datetime_consistency(messages, wrap_message), response)

            used_tools = True
            if depth == 0:
                self._set_plan_step("analyze", "分析需求", "completed", f"模型决定调用 {len(tool_calls)} 个工具。")
            message = self._normalize_tool_call_message(message, tool_calls)
            messages.append(self._build_assistant_followup_message(message))

            pending_notes = []
            for tool_call in tool_calls:
                if not self.is_running:
                    return ""
                user_intent_hint = self._get_latest_user_text(messages)
                tool_message = self._execute_tool_call(tool_call, depth=depth, step_prefix=step_prefix, user_intent_hint=user_intent_hint)
                messages.append(tool_message)
                executed_tool_name = str((tool_call.get("function") or {}).get("name", ""))
                if executed_tool_name == "wait_for_user_action":
                    pending_notes.append({
                        "role": "system",
                        "content": "用户处理窗口的等待已结束。现在重新调用 inspect_screen 或 inspect_screen_region，确认弹窗是否消失后再继续。",
                    })
                if executed_tool_name in {"inspect_screen", "inspect_foreground_window", "inspect_screen_region"}:
                    try:
                        vision_result = json.loads(tool_message.get("content", "{}"))
                    except (TypeError, json.JSONDecodeError):
                        vision_result = {}
                    if not vision_result.get("success", True):
                        pending_notes.append({
                            "role": "system",
                            "content": (
                                "这次视觉读取失败或超时，不能因此结束任务，也不要声称已经完成。"
                                "不要在没有新动作的情况下连续重复视觉读取；请先基于已有结果执行可逆动作、"
                                "使用 inspect_accessibility 或明确说明当前阻塞原因。"
                            ),
                        })
                    blocking_dialog = (vision_result.get("data") or {}).get("vision") or {}
                    blocking_dialog = blocking_dialog.get("blocking_dialog") or {}
                    if blocking_dialog.get("present") is True:
                        self.computer_state.emit("waiting_external")
                        pending_notes.append({
                            "role": "system",
                            "content": (
                                "视觉模型明确识别到外部阻塞确认弹窗。不要要求用户在聊天中回复，"
                                "也不要盲点弹窗坐标；请调用 wait_for_user_action 等待用户在目标窗口完成确认，"
                                "等待结束后重新 inspect_screen 验证弹窗是否消失。"
                            ),
                        })

            # 这些提示必须等本轮全部 tool 结果追加完再插：tool 消息与 assistant.tool_calls
            # 之间一旦夹入其它角色，网关就会整轮拒收这次请求。
            messages.extend(pending_notes)


        if used_tools:
            if resume_on_exhaust and depth >= 1:
                raise SubagentRoundsExhausted(
                    f"子agent已用完 {max_rounds} 轮工具调用预算，任务尚未收尾，需要续跑。"
                )
            messages.append({
                "role": "system",
                "content": "你已经拿到了足够的工具结果。不要继续调用工具，直接基于现有结果回答用户。"
            })
            if depth == 0:
                self._set_plan_step("finalize", "整理最终答复", "in_progress", "工具执行完成，正在归纳结果。")
            response = self._request_chat_completion(messages, temperature=0.5, stream=use_stream, emit_stream=(depth == 0))
            message = response["choices"][0]["message"]
            return self._annotate_truncation(self._ensure_datetime_consistency(messages, message), response)

        return ""

    def _annotate_truncation(self, content, response):
        """服务端因长度上限截断输出时，在回复末尾附一句提示，避免误以为已经写完。"""
        try:
            choice = (response.get("choices") or [{}])[0] or {}
        except AttributeError:
            return content
        finish_reason = str(choice.get("finish_reason") or "").strip().lower()
        if finish_reason not in ("length", "max_tokens"):
            return content
        note = "（输出已达长度上限被截断，回复「继续」我就接着写）"
        return content + "\n\n" + note if content else note

    def _post_json_cancellable(self, url, headers, payload):
        result_queue = queue.Queue(maxsize=1)

        def request_worker():
            response = None
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=300)
                body = response.content.decode("utf-8", errors="replace")
                if len(body) > API_RESPONSE_MAX_CHARS:
                    result_queue.put((
                        response.status_code,
                        body[:2000],
                        RuntimeError(
                            f"响应体异常大（{len(body)} 字符，超过上限 {API_RESPONSE_MAX_CHARS}），"
                            "疑似网关返回异常，已中止解析，请重试。"
                        ),
                    ))
                else:
                    result_queue.put((response.status_code, body, None))
            except Exception as exc:
                result_queue.put((None, "", exc))
            finally:
                if response is not None:
                    response.close()

        threading.Thread(target=request_worker, name="AissistAPIRequest", daemon=True).start()
        while True:
            if not self.is_running:
                raise APICallCancelled()
            try:
                status_code, raw_text, error = result_queue.get(timeout=0.1)
                if error:
                    if isinstance(error, requests.exceptions.RequestException):
                        raise RuntimeError(f"API请求失败: {error}")
                    raise RuntimeError(f"API请求失败: {error}")
                return status_code, raw_text
            except queue.Empty:
                continue

    def _request_chat_completion(self, messages, tools=None, tool_choice=None, temperature=0.7, stream=None, emit_stream=True):
        api_key = self.config.get_api_key()
        base_url = self.config.get_base_url()
        model = self.config.get_model()
        use_stream = self.stream if stream is None else bool(stream)

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": messages,
            "stream": use_stream,
            "temperature": temperature
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        url = f"{base_url.rstrip('/')}/chat/completions"
        requested_effort = self.config.get_reasoning_effort()
        attempts = self._reasoning_effort_attempts(model, requested_effort)
        while True:
            effort = attempts.pop(0)
            if effort:
                payload["reasoning_effort"] = effort
            else:
                payload.pop("reasoning_effort", None)
            try:
                if use_stream:
                    result = self._request_chat_completion_stream(url, headers, payload, emit_stream)
                else:
                    result = self._request_chat_completion_sync(url, headers, payload)
            except ReasoningEffortUnsupported:
                if not attempts:
                    raise
                self.status_update.emit(
                    f"{reasoning_effort_label(requested_effort)} 不被当前模型或网关支持，"
                    f"已自动降到 {reasoning_effort_label(attempts[0])}（本次运行内生效）"
                )
                continue
            self._remember_reasoning_effort(model, effort)
            return result

    def _reasoning_effort_attempts(self, model, requested_effort):
        """按「选中档位 → 逐级下调 → 关闭」生成重试顺序，并复用本次运行已知可用的档位上限。"""
        requested_effort = (requested_effort or "").strip().lower()
        if not requested_effort:
            return [""]
        known = self._reasoning_effort_known.get(model)
        # 取两者中更保守的那个：已知上限更低时就从上限制起试，用户选得更低就直接按用户选的来。
        if known is None or reasoning_effort_rank(requested_effort) >= reasoning_effort_rank(known):
            start = requested_effort
        else:
            start = known
        if not start:
            return [""]
        return list(REASONING_EFFORT_ORDER[reasoning_effort_rank(start):]) + [""]

    def _remember_reasoning_effort(self, model, effort):
        """记住本次运行中该模型实际可用的最高档位，避免后续请求反复撞墙。"""
        effort = (effort or "").strip().lower()
        known = self._reasoning_effort_known.get(model)
        if known is None or reasoning_effort_rank(effort) < reasoning_effort_rank(known):
            self._reasoning_effort_known[model] = effort

    def _request_chat_completion_sync(self, url, headers, payload):
        status_code, raw_text = self._post_json_cancellable(url, headers, payload)
        if status_code != 200:
            detail = raw_text
            if tools_unsupported_error(detail):
                raise RuntimeError(
                    "当前模型或中转网关不支持 tools/function calling，请更换支持工具调用的模型或基座。"
                    f"（网关返回：{error_detail_head(detail)}）"
                )
            if reasoning_effort_error(detail, status_code, payload.get("reasoning_effort")):
                raise ReasoningEffortUnsupported(
                    "当前模型或中转网关不支持该推理强度，已自动逐级降级仍失败，请在输入区把推理调低或关闭后重试。"
                    f"（网关返回：{error_detail_head(detail)}）"
                )
            raise RuntimeError(f"API请求失败: {status_code} - {detail}")
        return self._parse_sync_response(raw_text)

    def _parse_sync_response(self, raw_text):
        """解析非流式响应：容忍 BOM；网关无视 stream=false 回 SSE 时按流式拼回来。"""
        text = raw_text or ""
        if text.startswith("\ufeff"):
            text = text[1:]
        stripped = text.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            completion = self._completion_from_sse_text(stripped)
            if completion is not None:
                logging.warning("网关忽略了 stream=false 直接返回 SSE，已按流式解析（%d 字符）", len(text))
                return completion
            raise RuntimeError(
                f"API 返回了无效 JSON：{exc}（已接收 {len(raw_text)} 字符）"
                f"；返回体开头：{error_detail_head(stripped)}"
            )

    def _completion_from_sse_text(self, text):
        """把整段 SSE 文本拼成 completion；开头就不是 SSE 帧时返回 None。"""
        state = {"content": "", "tool_calls": {}}
        finish_reason = None
        seen_frame = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not line.startswith("data:"):
                if not seen_frame:
                    return None
                continue
            seen_frame = True
            data_str = line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            finish_reason = self._merge_stream_chunk(state, data, False) or finish_reason
        if not seen_frame:
            return None
        return self._stream_state_to_completion(state, finish_reason)

    def _merge_stream_chunk(self, state, data, emit_stream):
        """把一帧增量合并进累积状态，返回该帧的 finish_reason（没有则 None）。"""
        choices = data.get("choices") or []
        if not choices:
            return None
        choice = choices[0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            state["content"] += content
            if emit_stream:
                self._stream_emitted = True
                self.stream_chunk.emit(content)
        for tool_call_delta in delta.get("tool_calls") or []:
            index = tool_call_delta.get("index", 0)
            slot = state["tool_calls"].setdefault(index, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""}
            })
            if tool_call_delta.get("id"):
                slot["id"] = tool_call_delta["id"]
            if tool_call_delta.get("type"):
                slot["type"] = tool_call_delta["type"]
            fn_delta = tool_call_delta.get("function") or {}
            if fn_delta.get("name"):
                slot["function"]["name"] = fn_delta["name"]
            if fn_delta.get("arguments"):
                slot["function"]["arguments"] += fn_delta["arguments"]
        return choice.get("finish_reason")

    @staticmethod
    def _stream_state_to_completion(state, finish_reason):
        message = {"role": "assistant", "content": state["content"]}
        if state["tool_calls"]:
            message["tool_calls"] = [state["tool_calls"][index] for index in sorted(state["tool_calls"])]
        return {"choices": [{"message": message, "finish_reason": finish_reason}]}

    def _request_chat_completion_stream(self, url, headers, payload, emit_stream=True):
        """SSE 流式请求：边收边发 stream_chunk，同时累积 content 与增量 tool_calls。"""
        state = {"content": "", "tool_calls": {}}
        finish_reason = None
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=(10, 300), stream=True)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"API请求失败: {str(e)}")

        if response.status_code != 200:
            detail = response.text
            response.close()
            if tools_unsupported_error(detail):
                raise RuntimeError(
                    "当前模型或中转网关不支持 tools/function calling，请更换支持工具调用的模型或基座。"
                    f"（网关返回：{error_detail_head(detail)}）"
                )
            if reasoning_effort_error(detail, response.status_code, payload.get("reasoning_effort")):
                raise ReasoningEffortUnsupported(
                    "当前模型或中转网关不支持该推理强度，已自动逐级降级仍失败，请在输入区把推理调低或关闭后重试。"
                    f"（网关返回：{error_detail_head(detail)}）"
                )
            raise RuntimeError(f"API请求失败: {response.status_code} - {detail}")

        state = {"content": "", "tool_calls": {}}
        finish_reason = None
        try:
            for raw_bytes in response.iter_lines():
                if not self.is_running:
                    break
                if not raw_bytes:
                    continue
                line = raw_bytes.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                finish_reason = self._merge_stream_chunk(state, data, emit_stream) or finish_reason
        finally:
            response.close()

        return self._stream_state_to_completion(state, finish_reason)

    def _tool_failure_response(self, tool_call_id, tool_name, arguments, message, step_prefix=""):
        result = {"success": False, "message": message}
        self._tool_execution_index += 1
        step_key = (f"{step_prefix}_tool_{self._tool_execution_index}_{tool_name}"
                    if step_prefix else f"tool_{self._tool_execution_index}_{tool_name}")
        self._set_plan_step(step_key, f"执行工具：{tool_name}", "failed", message)
        append_task_tool_result(self.task_state, tool_name, arguments, result, 0)
        self._emit_task_state()
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result, ensure_ascii=False),
        }

    def _execute_tool_call(self, tool_call, depth=0, step_prefix="", user_intent_hint=None):
        function_call = tool_call.get("function", {})
        tool_name = function_call.get("name", "")
        arguments = self._parse_tool_arguments(function_call.get("arguments", "{}"))
        if tool_name == "search_web":
            arguments = dict(arguments)
            if user_intent_hint and "_user_intent" not in arguments:
                arguments["_user_intent"] = user_intent_hint
        tool_call_id = tool_call.get("id", "")

        if tool_name == "delegate_to_agent":
            return self._handle_delegate_tool_call(tool_call_id, arguments, depth)

        needs_user_approval = tool_name in {"shutdown", "reboot", "run_command", "write_file"}
        if not needs_user_approval and MCPManager.is_mcp_tool(tool_name):
            # 外部 MCP 工具默认每次都要用户点头；server 配置里写了 autoApprove 才免
            needs_user_approval = MCPManager.needs_confirmation(tool_name)
        if needs_user_approval:
            approved = self._request_confirmation(tool_call_id, tool_name, arguments)
            if not approved:
                logging.warning("用户取消了高风险操作 %s", tool_name)
                return self._tool_failure_response(
                    tool_call_id, tool_name, arguments, "用户取消了该高风险操作。", step_prefix
                )

        computer_tools = {
            "inspect_screen", "inspect_foreground_window", "inspect_screen_region", "inspect_accessibility", "mouse_move", "mouse_click",
            "verified_mouse_click",
            "keyboard_type", "keyboard_hotkey", "computer_wait", "wait_for_user_action",
        }
        if tool_name in computer_tools:
            observation_tools = {
                "inspect_screen", "inspect_foreground_window", "inspect_screen_region",
            }
            state_change_tools = {
                "mouse_click", "verified_mouse_click", "keyboard_type", "keyboard_hotkey",
                "wait_for_user_action",
            }
            if tool_name in observation_tools:
                if self._computer_observation_streak >= 1:
                    return self._tool_failure_response(
                        tool_call_id,
                        tool_name,
                        arguments,
                        "最近没有发生新的键鼠动作或用户处理，暂时禁止重复读取屏幕；请基于已有观察执行动作，或明确说明当前阻塞原因。",
                        step_prefix,
                    )
                self._computer_observation_streak += 1
            elif tool_name in state_change_tools:
                self._computer_observation_streak = 0
            if not self._computer_session_approved:
                self._computer_session_requested = True
                self.computer_state.emit("waiting_authorization")
                approved = self._request_confirmation(
                    tool_call_id,
                    "computer_session",
                    {"requested_tool": tool_name},
                )
                if not approved:
                    return self._tool_failure_response(
                        tool_call_id, tool_name, arguments, "用户拒绝了视觉键鼠连续操作。", step_prefix
                    )
                self._computer_session_approved = True
                self._computer_session_started = True
                if not VisionInputController.begin_session(cancel_callback=self._force_cancel_from_input):
                    self._computer_guard_failed = True
                    self.computer_state.emit("guard_failed")
                    self.stop()
                    return self._tool_failure_response(
                        tool_call_id,
                        tool_name,
                        arguments,
                        "Windows 键鼠接管钩子启动失败，已拒绝继续操作。",
                        step_prefix,
                    )
                self.computer_state.emit("q_listener_active")
                self.computer_state.emit("input_guard_active")
                self.computer_state.emit("observing")
            self._computer_step_count += 1
            if VisionInputController.is_cancelled():
                self.computer_state.emit("stopping")
                self.stop()
                return self._tool_failure_response(
                    tool_call_id, tool_name, arguments, "视觉键鼠任务已被长按 Q 取消。", step_prefix
                )

        self.status_update.emit(f"正在执行工具：{tool_name}")
        computer_state_map = {
            "inspect_screen": "waiting_vision",
            "inspect_foreground_window": "waiting_vision",
            "inspect_screen_region": "waiting_vision",
            "inspect_accessibility": "reading_accessibility",
            "mouse_move": "moving_mouse",
            "mouse_click": "clicking",
            "verified_mouse_click": "verifying_click",
            "keyboard_type": "typing",
            "keyboard_hotkey": "pressing_hotkey",
            "computer_wait": "waiting_interface",
            "wait_for_user_action": "waiting_external",
        }
        if tool_name in computer_state_map:
            self.computer_state.emit(computer_state_map[tool_name])
        self._tool_execution_index += 1
        step_key = (f"{step_prefix}_tool_{self._tool_execution_index}_{tool_name}"
                    if step_prefix else f"tool_{self._tool_execution_index}_{tool_name}")
        self._mark_task_state("executing", f"执行工具：{tool_name}")
        self._set_plan_step(step_key, f"执行工具：{tool_name}", "in_progress", f"参数：{json.dumps(arguments, ensure_ascii=False)}")
        started = time.perf_counter()
        if MCPManager.is_mcp_tool(tool_name):
            result = MCPManager.call_tool(tool_name, arguments)
        else:
            result = ActionHandler.execute_tool(tool_name, arguments)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if tool_name in {
            "inspect_screen", "inspect_foreground_window", "inspect_screen_region", "inspect_accessibility", "mouse_move", "mouse_click",
            "verified_mouse_click",
            "keyboard_type", "keyboard_hotkey", "computer_wait",
            "wait_for_user_action",
        } and VisionInputController.is_cancelled():
            self.computer_state.emit("stopping")
            self.stop()
        if result.get("success"):
            logging.info("工具调用 %s 参数=%s 耗时=%dms 成功", tool_name, _log_safe_args(arguments), duration_ms)
        else:
            logging.warning("工具调用 %s 参数=%s 耗时=%dms 失败：%s", tool_name, _log_safe_args(arguments), duration_ms, str(result.get("message", ""))[:200])
        append_task_tool_result(self.task_state, tool_name, arguments, result, duration_ms)
        step_status = "completed" if result.get("success") else "failed"
        step_detail = str(result.get("message", "")) or "工具执行完成。"
        self._set_plan_step(step_key, f"执行工具：{tool_name}", step_status, step_detail)
        if tool_name == "get_current_datetime" and result.get("success"):
            self._latest_datetime_data = result.get("data")
        if tool_name == "search_web" and result.get("success"):
            self._collect_search_sources(result)
        self._emit_task_state()
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result, ensure_ascii=False)
        }

    def _handle_delegate_tool_call(self, tool_call_id, arguments, depth):
        agent_type = str(arguments.get("agent_type", "")).strip()
        task = str(arguments.get("task", "")).strip()
        self._tool_execution_index += 1
        self._mark_task_state("executing", f"子agent({agent_type})处理中")
        result = self._run_subagent(agent_type, task, depth=depth + 1)
        if result.get("partial"):
            result = dict(result)
            result["message"] = str(result.get("message", "")) + "\n【提示】子agent已尽力完成并返回上述结果，请直接基于它整理最终答复；除非关键信息确实缺失，不要重复调用搜索工具。"
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result, ensure_ascii=False)
        }

    def _prepare_code_workspace(self):
        """为 code 子agent 创建隔离工作区 + Python 虚拟环境，返回 (workspace, venv_python)。失败返回 (None, "")。"""
        try:
            root = os.path.join(get_runtime_dir(), "agent_workspace")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            workspace = os.path.join(root, f"code_{stamp}")
            os.makedirs(workspace, exist_ok=True)
            venv_dir = os.path.join(workspace, "venv")
            venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
            if not os.path.exists(venv_python):
                import subprocess
                try:
                    subprocess.run(
                        [sys.executable, "-m", "venv", venv_dir],
                        capture_output=True, timeout=120,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except Exception as e:
                    logging.warning("创建子agent虚拟环境失败：%s", str(e))
            return workspace, (venv_python if os.path.exists(venv_python) else "")
        except Exception as e:
            logging.warning("创建子agent工作区失败：%s", str(e))
            return None, ""

    def _build_code_workspace_hint(self, workspace, venv_python):
        lines = [
            "\n\n【工作区约束】",
            f"- 你的工作目录是：{workspace}",
            "- list_dir / read_file / write_file 的相对路径会自动解析到该目录；不要尝试访问工作区以外的路径，也不要使用 .. 穿越到工作区外。",
            "- run_command 默认在工作目录下执行，且不允许在命令中出现工作区外的绝对路径。",
            "- 所有脚本、结果文件必须写在工作区内；禁止把文件写到临时目录（Temp）、桌面、下载、系统目录等任何工作区外位置。",
        ]
        if venv_python:
            lines += [
                f"- 已创建 Python 虚拟环境，python 路径：{venv_python}",
                f"- 运行脚本请用：& \"{venv_python}\" 脚本名.py",
                f"- 安装依赖请用：& \"{venv_python}\" -m pip install 包名",
            ]
        return "\n".join(lines)

    def _build_subagent_resume_messages(self, prompt, task, partial_summary):
        resume = (
            f"原始任务：{task}\n\n"
            "你之前执行了一部分，但时间预算耗尽被中断。以下是已经完成的工具结果摘要：\n"
            f"{partial_summary or '（无已完成工具结果）'}\n\n"
            "请基于已有信息继续完成原始任务：优先收尾总结；若确实还缺关键信息，"
            "可以再补充少量工具调用。不要重复已经完成的工作。"
        )
        return [
            {"role": "system", "content": prompt},
            {"role": "user", "content": resume},
        ]

    def _run_subagent(self, agent_type, task, depth=1):
        started = time.perf_counter()
        step_key = f"subagent_{agent_type}"
        tool_results_start = 0
        workspace = None
        try:
            prompt = SUBAGENT_PROMPTS.get(agent_type)
            if not prompt:
                return {"success": False, "message": f"未知的子agent类型：{agent_type}"}
            if agent_type == "code":
                workspace, venv_python = self._prepare_code_workspace()
                if workspace:
                    ActionHandler.set_agent_workspace(workspace)
                    prompt = prompt + self._build_code_workspace_hint(workspace, venv_python)
            tools = ActionHandler.get_subagent_tool_definitions(agent_type)
            if depth >= 1:
                tools = [t for t in tools if t["function"]["name"] != "delegate_to_agent"]
            title = f"子agent({agent_type})：{task[:40]}" + ("…" if len(task) > 40 else "")
            self._set_plan_step(step_key, title, "in_progress", "已启动，正在处理…")
            self.status_update.emit(f"子agent({agent_type})正在处理…")
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": task},
            ]
            timeout = SUBAGENT_TIMEOUT_SECONDS_BY_TYPE.get(agent_type, SUBAGENT_TIMEOUT_SECONDS)
            tool_results_start = len(self.task_state.get("tool_results", []))
            total_budget = SUBAGENT_TOTAL_BUDGET_BY_TYPE.get(agent_type, SUBAGENT_TOTAL_BUDGET_SECONDS)
            deadline_total = time.perf_counter() + total_budget
            segment_index = 0
            result_text = ""
            while True:
                if not self.is_running or time.perf_counter() >= deadline_total:
                    break
                segment_index += 1
                remaining = deadline_total - time.perf_counter()
                segment_timeout = max(1, min(timeout, remaining))
                segment_started = time.perf_counter()
                try:
                    result_text = self._run_agent_loop(
                        messages, tools,
                        max_rounds=SUBAGENT_MAX_ROUNDS,
                        depth=depth,
                        use_stream=False,
                        step_prefix=step_key,
                        timeout=segment_timeout,
                        wrapup_margin=SUBAGENT_WRAPUP_MARGIN_SECONDS,
                        progress_key=step_key,
                        progress_title=title,
                        resume_on_exhaust=True,
                    )
                    break
                except SubagentRoundsExhausted:
                    if time.perf_counter() >= deadline_total:
                        partial = self._collect_subagent_partial_results(tool_results_start)
                        msg = f"（子agent({agent_type})轮次预算用尽且总预算耗尽，仅完成部分任务）"
                        if partial:
                            msg += "\n" + partial
                        self._set_plan_step(step_key, title, "failed", "轮次与总预算均耗尽，已返回部分结果")
                        return {"success": True, "message": msg[:SUBAGENT_MAX_RESULT_LENGTH], "agent_type": agent_type, "partial": True}
                    partial = self._collect_subagent_partial_results(tool_results_start, limit=2500)
                    messages = self._build_subagent_resume_messages(prompt, task, partial)
                    self.status_update.emit(f"子agent({agent_type})轮次用尽，自动续跑（第 {segment_index} 段）…")
                    self._set_plan_step(step_key, title, "in_progress", f"轮次用尽已续跑第 {segment_index} 段 · 剩余 {int(deadline_total - time.perf_counter())}s")
                except RuntimeError as e:
                    if "执行超时" not in str(e):
                        raise
                    if time.perf_counter() >= deadline_total or (time.perf_counter() - segment_started) < 5:
                        raise
                    partial = self._collect_subagent_partial_results(tool_results_start, limit=2500)
                    messages = self._build_subagent_resume_messages(prompt, task, partial)
                    self.status_update.emit(f"子agent({agent_type})时间预算已用尽，自动续跑（第 {segment_index} 段）…")
                    self._set_plan_step(step_key, title, "in_progress", f"已续跑第 {segment_index} 段 · 剩余 {int(deadline_total - time.perf_counter())}s")
            if not self.is_running:
                partial = self._collect_subagent_partial_results(tool_results_start)
                note = "（子agent已停止，仅完成部分任务）"
                result_text = (note + "\n" + partial) if partial else note
                self._set_plan_step(step_key, title, "failed", "已停止，返回部分结果")
                return {"success": True, "message": result_text[:SUBAGENT_MAX_RESULT_LENGTH], "agent_type": agent_type, "partial": True}
            result_text = str(result_text or "").strip()
            if not result_text:
                result_text = "（子agent未返回有效内容）"
            if len(result_text) > SUBAGENT_MAX_RESULT_LENGTH:
                result_text = result_text[:SUBAGENT_MAX_RESULT_LENGTH] + "…（结果过长已截断）"
            duration_ms = int((time.perf_counter() - started) * 1000)
            logging.info("子agent %s 任务=%s 耗时=%dms 成功（续跑%d次）", agent_type, task[:80], duration_ms, max(0, segment_index - 1))
            self._set_plan_step(step_key, title, "completed", f"已完成（耗时 {duration_ms}ms，续跑 {max(0, segment_index - 1)} 次）")
            return {"success": True, "message": result_text, "agent_type": agent_type}
        except SubagentRoundsExhausted as e:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logging.warning("子agent %s 轮次预算用尽：%s", agent_type, str(e))
            partial = self._collect_subagent_partial_results(tool_results_start)
            message = f"（子agent({agent_type})轮次预算用尽，仅完成部分任务）"
            if partial:
                message += "\n" + partial
            self._set_plan_step(step_key, title, "failed", "轮次预算用尽，已返回部分结果")
            return {"success": True, "message": message[:SUBAGENT_MAX_RESULT_LENGTH], "agent_type": agent_type, "partial": True}
        except Exception as e:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logging.warning("子agent %s 执行失败：%s", agent_type, str(e))
            message = f"子agent({agent_type})执行失败: {str(e)}"
            if "执行超时" in str(e):
                partial = self._collect_subagent_partial_results(tool_results_start)
                if partial:
                    message = f"（子agent({agent_type})超时，仅完成部分任务）\n{partial}"
                    self._set_plan_step(step_key, title, "failed", "执行超时，已返回部分结果")
                    return {"success": True, "message": message[:SUBAGENT_MAX_RESULT_LENGTH], "agent_type": agent_type, "partial": True}
            self._set_plan_step(step_key, f"子agent({agent_type})", "failed", str(e)[:200])
            return {"success": False, "message": message}
        finally:
            if workspace:
                ActionHandler.set_agent_workspace(None)

    def _collect_subagent_partial_results(self, start_index, limit=3000):
        entries = []
        for item in self.task_state.get("tool_results", [])[start_index:]:
            if not item.get("success"):
                continue
            name = str(item.get("tool_name", "")).strip()
            msg = str(item.get("message", "")).strip()
            if not name or not msg:
                continue
            line = f"[{name}] {msg}"
            details = item.get("details")
            if details:
                detail_lines = [f"    - {d.get('title', '')} | {d.get('url', '')}" for d in details]
                if detail_lines:
                    line += "\n" + "\n".join(detail_lines)
            if len(line) > 800:
                line = line[:800] + "…"
            entries.append(line)
        if not entries:
            return ""
        joined = "\n".join(entries)
        if len(joined) > limit:
            joined = joined[:limit] + "…"
        return "已获取的部分信息：\n" + joined

    @staticmethod
    def _parse_tool_arguments(raw_arguments):
        if not raw_arguments:
            return {}
        if isinstance(raw_arguments, dict):
            return raw_arguments
        try:
            return json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _build_assistant_followup_message(message):
        if not isinstance(message, dict):
            return {"role": "assistant", "content": str(message)}

        preserved = {"role": message.get("role", "assistant")}
        if "content" in message:
            preserved["content"] = message.get("content")
        elif "tool_calls" not in message:
            preserved["content"] = ""

        for key, value in message.items():
            if key == "role":
                continue
            if key in preserved:
                continue
            if key in {"tool_calls", "function_call", "name", "refusal"}:
                preserved[key] = value
                continue
            if key.startswith("reasoning") or key.startswith("thinking"):
                preserved[key] = value

        return preserved

    def _emit_task_state(self):
        touch_task_state(self.task_state)
        self.task_state_updated.emit(copy.deepcopy(self.task_state))

    def _mark_task_state(self, status: str, current_step: str):
        touch_task_state(self.task_state, status=status, current_step=current_step)
        self._emit_task_state()

    def _set_plan_step(self, step_key: str, title: str, status: str, detail: str = ""):
        upsert_task_plan_step(self.task_state, step_key, title, status, detail)
        self._emit_task_state()

    @staticmethod
    def _strip_dsml_protocol_text(text):
        if not text:
            return ""
        cleaned = DSML_INVOKE_PATTERN.sub("", text)
        cleaned = DSML_TAG_PATTERN.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _extract_tool_calls_from_message(self, message):
        if not isinstance(message, dict):
            return []

        native_tool_calls = message.get("tool_calls") or []
        if native_tool_calls:
            return native_tool_calls

        content = str(message.get("content") or "")
        if not content:
            return []

        text_calls, _ = parse_text_tool_calls(content)
        if text_calls:
            return text_calls

        if "DSML" not in content:
            return []

        parsed_calls = []
        for invoke_match in DSML_INVOKE_PATTERN.finditer(content):
            tool_name = invoke_match.group(1).strip()
            if not tool_name:
                continue
            arguments = {}
            invoke_body = invoke_match.group(2)
            for param_match in DSML_PARAMETER_PATTERN.finditer(invoke_body):
                param_name = param_match.group(1).strip()
                param_value = self._strip_dsml_protocol_text(param_match.group(2))
                if param_name:
                    arguments[param_name] = param_value
            parsed_calls.append({
                "id": f"dsml_{tool_name}_{uuid.uuid4().hex[:10]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False)
                }
            })
        return parsed_calls

    def _normalize_tool_call_message(self, message, tool_calls):
        if not isinstance(message, dict):
            return {"role": "assistant", "content": "", "tool_calls": tool_calls}

        normalized = dict(message)
        normalized["role"] = normalized.get("role", "assistant")
        normalized["tool_calls"] = tool_calls
        original_content = str(normalized.get("content") or "")
        has_text_protocol = bool(parse_text_tool_calls(original_content)[0])
        normalized["content"] = "" if "DSML" in original_content or has_text_protocol else self._strip_dsml_protocol_text(original_content)
        return normalized

    def _request_confirmation(self, call_id, tool_name, arguments):
        event = threading.Event()
        with self._confirmation_lock:
            self._confirmation_events[call_id] = event
        self.tool_confirmation_requested.emit(call_id, tool_name, arguments)

        while self.is_running and not event.wait(0.1):
            pass

        with self._confirmation_lock:
            approved = self._confirmation_answers.pop(call_id, False)
            self._confirmation_events.pop(call_id, None)
        return approved

    def _emit_text_stream(self, content):
        if not content:
            return
        chunk_size = 14
        for start in range(0, len(content), chunk_size):
            if not self.is_running:
                break
            self.stream_chunk.emit(content[start:start + chunk_size])
            self.msleep(20)

    def _collect_search_sources(self, result):
        data = result.get("data") or {}
        for item in data.get("results", [])[:5]:
            title = sanitize_reply_text(str(item.get("title", "无标题"))).strip() or "无标题"
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            if any(existing["url"] == url for existing in self._search_sources):
                continue
            self._search_sources.append({"title": title, "url": url})

    def _append_search_sources(self, content):
        if not self._search_sources:
            return content
        return attach_sources_to_text(content, self._search_sources)

    def _inject_forced_datetime_context(self, messages):
        latest_user_text = self._get_latest_user_text(messages)
        if not self._should_force_current_datetime(latest_user_text):
            return messages

        tool_result = ActionHandler.execute_tool("get_current_datetime", {})
        tool_call_id = f"forced_datetime_{uuid.uuid4().hex[:10]}"
        self._forced_datetime_route = True
        if tool_result.get("success"):
            self._latest_datetime_data = tool_result.get("data")

        forced_messages = list(messages)
        forced_messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "get_current_datetime",
                    "arguments": "{}"
                }
            }]
        })
        forced_messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(tool_result, ensure_ascii=False)
        })
        forced_messages.append({
            "role": "system",
            "content": "用户正在询问当前日期或时间。你必须严格依据刚才的 get_current_datetime 工具结果回答，禁止改写成其他日期、星期或时间，也不要使用 Markdown 符号。"
        })
        return forced_messages

    @staticmethod
    def _get_latest_user_text(messages):
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content", "")).strip()
        return ""

    @staticmethod
    def _should_force_current_datetime(user_text):
        if not user_text:
            return False
        normalized = user_text.strip().replace(" ", "")
        if any(keyword in normalized for keyword in DATETIME_FORCE_KEYWORDS):
            return True
        if re.search(r"(今天|现在|当前).{0,6}(日期|时间|星期|周几|礼拜几|几点)", normalized):
            return True
        if re.search(r"(几号|几月几日|几月几号|星期几|周几|礼拜几|几点|当前时间|当前日期)$", normalized):
            return True
        return False

    def _ensure_datetime_consistency(self, messages, message):
        content = ""
        if isinstance(message, dict):
            content = (message.get("content") or "").strip()
        else:
            content = str(message).strip()

        if not self._latest_datetime_data:
            return content
        if not self._has_datetime_mismatch(content, self._latest_datetime_data):
            return content

        correction_message = (
            "你刚才把当前日期或时间说错了。"
            f"正确值是：{self._latest_datetime_data['year']}年{self._latest_datetime_data['month']}月{self._latest_datetime_data['day']}日，"
            f"{self._latest_datetime_data['weekday']}，{self._latest_datetime_data['time']}。"
            "请只基于这个正确值，重新自然回答用户，不要使用 Markdown 符号，也不要再调用工具。"
        )
        self.task_state["retry_count"] = int(self.task_state.get("retry_count", 0)) + 1
        self._set_plan_step("datetime_retry", "修正日期时间答复", "in_progress", "检测到日期或时间不一致，正在自动重试。")
        retry_messages = list(messages)
        retry_messages.append(self._build_assistant_followup_message(message))
        retry_messages.append({"role": "system", "content": correction_message})
        response = self._request_chat_completion(retry_messages, temperature=0.1, stream=self.stream, emit_stream=False)
        retry_message = response["choices"][0]["message"]
        self._set_plan_step("datetime_retry", "修正日期时间答复", "completed", "已依据工具结果纠正日期或时间。")
        content = (retry_message.get("content") or "").strip()
        return content if self.config.get_roleplay_mode() else sanitize_reply_text(content)

    @staticmethod
    def _has_datetime_mismatch(content, datetime_data):
        if not content:
            return False

        expected_year = int(datetime_data["year"])
        expected_month = int(datetime_data["month"])
        expected_day = int(datetime_data["day"])
        expected_weekday = datetime_data["weekday"]
        expected_weekday_short = expected_weekday.replace("星期", "周", 1)

        full_date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", content)
        if full_date_match:
            year, month, day = map(int, full_date_match.groups())
            if (year, month, day) != (expected_year, expected_month, expected_day):
                return True

        month_day_match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", content)
        if month_day_match:
            month, day = map(int, month_day_match.groups())
            if (month, day) != (expected_month, expected_day):
                return True

        weekday_match = re.search(r"(星期[一二三四五六日天]|周[一二三四五六日天])", content)
        if weekday_match:
            weekday = weekday_match.group(1)
            if weekday not in {expected_weekday, expected_weekday_short}:
                return True

        time_match = re.search(r"(?<!\d)(\d{1,2})[:：](\d{2})(?::(\d{2}))?", content)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            expected_hour, expected_minute, _ = map(int, str(datetime_data["time"]).split(":"))
            if (hour, minute) != (expected_hour, expected_minute):
                return True

        return False


# ======================== 角色管理 ========================
class RoleManagerDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.roles = copy.deepcopy(config.get_roles())
        self._active_name = ""
        self.setWindowTitle("角色管理")
        self.resize(760, 520)

        root = QHBoxLayout(self)
        left_layout = QVBoxLayout()
        self.role_list = QListWidget()
        self.role_list.currentRowChanged.connect(self._on_role_selected)
        left_layout.addWidget(self.role_list, 1)

        role_buttons = QHBoxLayout()
        self.add_role_button = QPushButton("新增")
        self.remove_role_button = QPushButton("删除")
        self.help_role_button = QPushButton("说明")
        role_buttons.addWidget(self.add_role_button)
        role_buttons.addWidget(self.remove_role_button)
        role_buttons.addWidget(self.help_role_button)
        left_layout.addLayout(role_buttons)
        root.addLayout(left_layout, 1)

        editor = QWidget()
        editor_layout = QFormLayout(editor)
        self.role_name_edit = QLineEdit()
        self.role_name_edit.setPlaceholderText("角色名称")
        editor_layout.addRow("名称:", self.role_name_edit)
        self.role_persona_edit = QTextEdit()
        self.role_persona_edit.setMinimumHeight(320)
        self.role_persona_edit.setPlaceholderText("角色性格、背景、说话方式和关系设定")
        editor_layout.addRow("角色卡:", self.role_persona_edit)
        root.addWidget(editor, 2)

        self.add_role_button.clicked.connect(self._add_role)
        self.remove_role_button.clicked.connect(self._remove_role)
        self.help_role_button.clicked.connect(self._show_help)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        editor_layout.addRow(buttons)
        self._refresh_role_list()

    def _show_help(self):
        QMessageBox.information(
            self,
            "角色卡说明",
            "角色卡描述“角色是谁”：姓名、性格、背景和说话习惯。\n\n"
            "提示词描述“这次如何扮演”：当前关系、场景、表达风格和示例对话。\n\n"
            "默认系统提示词会自动读取当前角色卡。删除角色不会删除提示词；"
            "删除全部角色后，程序仍可使用通用角色设定。",
        )

    def _refresh_role_list(self, selected_name=""):
        names = list(self.roles)
        self.role_list.blockSignals(True)
        self.role_list.clear()
        self.role_list.addItems(names)
        self.role_list.blockSignals(False)
        if names:
            row = names.index(selected_name) if selected_name in names else 0
            self.role_list.setCurrentRow(row)
        else:
            self._clear_editor()

    def _clear_editor(self):
        self._active_name = ""
        self.role_name_edit.clear()
        self.role_persona_edit.clear()

    def _commit_active_role(self, row=None):
        if not self._active_name or self._active_name not in self.roles:
            return True
        name = self.role_name_edit.text().strip()
        persona = self.role_persona_edit.toPlainText().strip()
        if not name or not persona or (name != self._active_name and name in self.roles):
            return False
        if name != self._active_name:
            self.roles = {
                (name if key == self._active_name else key): value
                for key, value in self.roles.items()
            }
            self._active_name = name
        self.roles[name] = persona
        item = self.role_list.item(self.role_list.currentRow() if row is None else row)
        if item:
            item.setText(name)
        return True

    def _on_role_selected(self, row):
        previous_row = list(self.roles).index(self._active_name) if self._active_name in self.roles else -1
        if not self._commit_active_role(previous_row):
            return
        names = list(self.roles)
        if not 0 <= row < len(names):
            self._clear_editor()
            return
        self._active_name = names[row]
        self.role_name_edit.setText(self._active_name)
        self.role_persona_edit.setPlainText(self.roles[self._active_name])

    def _add_role(self):
        if not self._commit_active_role():
            QMessageBox.warning(self, "角色未保存", "请先填写当前角色的名称和角色卡。")
            return
        name = "新角色"
        index = 2
        while name in self.roles:
            name = f"新角色{index}"
            index += 1
        self.roles[name] = "请填写这个角色的性格、背景、说话方式和关系设定。"
        self._refresh_role_list(name)

    def _remove_role(self):
        if not self._active_name:
            return
        if not self._commit_active_role():
            QMessageBox.warning(self, "角色未保存", "请先填写当前角色的名称和角色卡。")
            return
        self.roles.pop(self._active_name, None)
        self._active_name = ""
        names = list(self.roles)
        self._refresh_role_list(names[0] if names else "")

    def _save(self):
        if not self._commit_active_role():
            QMessageBox.warning(self, "保存失败", "角色名称和角色卡不能为空，且角色名称不能重复。")
            return
        self.config.set_roles(self.roles)
        self.accept()


# ======================== 配置对话框 ========================
class ConfigDialog(QDialog):
    voices_loaded = pyqtSignal(list)   # 信号用于传递声音列表

    def __init__(self, config, parent=None, system_prompt_text=None, section="config", default_system_prompt_text=None):
        super().__init__(parent)
        self.config = config
        self.section = section
        self.initial_system_prompt_mode = config.get_system_prompt_mode()
        self._secret_initial_values = {}
        title_map = {"config": "配置", "prompt": "提示词", "voice": "语音"}
        self.setWindowTitle(title_map.get(section, "配置"))
        self.setMinimumWidth(450)
        layout = QFormLayout(self)

        if section == "config":
            self._secret_initial_values = {
                key: str(config.settings.value(key, "") or "").strip()
                for key in ("api_key", "tavily_api_key", "amap_api_key")
            }
            self.api_key_edit = self._make_secret_edit(config.get_api_key(), "sk-...")
            layout.addRow("API Key:", self.api_key_edit)

            self.base_url_edit = QLineEdit(config.get_base_url())
            self.base_url_edit.setPlaceholderText("https://api.openai.com/v1")
            layout.addRow("API Base URL:", self.base_url_edit)

            self.model_edit = QLineEdit(config.get_model())
            self.model_edit.setPlaceholderText("gpt-3.5-turbo")
            layout.addRow("模型名称:", self.model_edit)

            self.vision_model_edit = QLineEdit(config.get_vision_model())
            self.vision_model_edit.setPlaceholderText("支持图片输入的视觉模型；留空则使用聊天模型")
            self.vision_model_edit.setToolTip("视觉键鼠实验会使用这个模型分析屏幕截图。")
            layout.addRow("视觉模型:", self.vision_model_edit)

            self.reasoning_combo = QComboBox()
            for label, value in REASONING_EFFORT_CHOICES:
                self.reasoning_combo.addItem("关闭（默认）" if not value else label, value)
            current_reasoning = config.get_reasoning_effort()
            reasoning_index = max(0, self.reasoning_combo.findData(current_reasoning))
            self.reasoning_combo.setCurrentIndex(reasoning_index)
            self.reasoning_combo.setToolTip(
                "共七档：关闭 + 低 / 中 / 高 / 极高 / 最高 / 极限。"
                "仅对支持 reasoning_effort 的模型生效，不支持时请关闭。"
            )
            layout.addRow("推理强度:", self.reasoning_combo)

            self.searxng_url_edit = QLineEdit(config.get_searxng_url())
            self.searxng_url_edit.setPlaceholderText("http://localhost:18080")
            layout.addRow("SearXNG URL:", self.searxng_url_edit)

            self.tavily_api_key_edit = self._make_secret_edit(config.get_tavily_api_key(), "tvly-...")
            layout.addRow("Tavily Key:", self.tavily_api_key_edit)

            self.amap_api_key_edit = self._make_secret_edit(config.get_amap_api_key(), "高德开放平台 Web服务 Key")
            layout.addRow("高德 Key:", self.amap_api_key_edit)

        elif section == "prompt":
            displayed_system_prompt = system_prompt_text if system_prompt_text is not None else config.get_system_prompt()
            self.initial_system_prompt_text = str(displayed_system_prompt or "")
            self.default_system_prompt_text = str(
                default_system_prompt_text if default_system_prompt_text is not None else config.get_system_prompt() or ""
            )
            self._prompt_reset_requested = False
            self.system_prompt_edit = QTextEdit()
            self.system_prompt_edit.setPlainText(self.initial_system_prompt_text)
            self.system_prompt_edit.setMinimumHeight(260)
            self.system_prompt_edit.setPlaceholderText("留空表示不使用系统提示词；点击“恢复默认提示词”可启用内置提示词")
            self.system_prompt_edit.textChanged.connect(self._on_prompt_text_changed)
            layout.addRow("系统提示词:", self.system_prompt_edit)
            self.restore_prompt_button = QPushButton("恢复默认提示词")
            self.restore_prompt_button.clicked.connect(self._restore_default_system_prompt)
            layout.addRow("", self.restore_prompt_button)

        elif section == "voice":
            self._secret_initial_values = {
                key: str(config.settings.value(key, "") or "").strip()
                for key in ("xfyun_appid", "xfyun_api_key", "xfyun_api_secret")
            }
            xfyun_group = QGroupBox("语音识别（讯飞）")
            xfyun_layout = QFormLayout(xfyun_group)

            self.xfyun_appid_edit = QLineEdit(config.get_xfyun_appid())
            self.xfyun_appid_edit.setPlaceholderText("6位APPID")
            xfyun_layout.addRow("APPID:", self.xfyun_appid_edit)

            self.xfyun_api_key_edit = self._make_secret_edit(config.get_xfyun_api_key(), "讯飞开放平台 APIKey")
            xfyun_layout.addRow("API Key:", self.xfyun_api_key_edit)

            self.xfyun_api_secret_edit = self._make_secret_edit(config.get_xfyun_api_secret(), "讯飞开放平台 APISecret")
            xfyun_layout.addRow("API Secret:", self.xfyun_api_secret_edit)
            layout.addRow(xfyun_group)

            tts_group = QGroupBox("语音合成")
            tts_layout = QFormLayout(tts_group)
            self.tts_checkbox = QCheckBox("启用语音回复")
            self.tts_checkbox.setChecked(config.get_tts_enabled())
            tts_layout.addRow(self.tts_checkbox)

            self.voice_combo = QComboBox()
            self.voice_combo.setEnabled(config.get_tts_enabled())
            self.tts_checkbox.toggled.connect(self.voice_combo.setEnabled)
            tts_layout.addRow("AI音色:", self.voice_combo)
            layout.addRow(tts_group)

        # ========== 按钮 ==========
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        # ========== 连接信号 ==========
        if section == "voice":
            self.voices_loaded.connect(self.update_voice_combo)

        # ========== 异步加载声音列表 ==========
        if section == "voice":
            QTimer.singleShot(100, self.load_voices_async)

    def _make_secret_edit(self, value, placeholder):
        edit = QLineEdit(value)
        edit.setPlaceholderText(placeholder)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        return edit

    def load_voices_async(self):
        """在后台线程获取声音列表，完成后发射信号"""
        def fetch():
            try:
                import asyncio
                import edge_tts
                voices = asyncio.run(edge_tts.list_voices())
                # 提取中文声音（ShortName 包含 zh-CN）
                chinese_voices = [v['ShortName'] for v in voices if 'zh-CN' in v.get('ShortName', '')]
                if not chinese_voices:
                    chinese_voices = ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"]
            except Exception as e:
                print(f"获取声音列表失败: {e}")
                chinese_voices = ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"]
            # 发射信号（线程安全）
            self.voices_loaded.emit(chinese_voices)

        threading.Thread(target=fetch, daemon=True).start()

    def update_voice_combo(self, voice_list):
        """主线程中更新下拉框"""
        self.voice_combo.clear()
        current = self.config.get_tts_voice()
        idx = 0
        for i, v in enumerate(voice_list):
            self.voice_combo.addItem(v, v)
            if v == current:
                idx = i
        self.voice_combo.setCurrentIndex(idx)

    def _on_prompt_text_changed(self):
        self._prompt_reset_requested = False

    def _restore_default_system_prompt(self):
        self._prompt_reset_requested = True
        self.system_prompt_edit.blockSignals(True)
        self.system_prompt_edit.setPlainText(self.default_system_prompt_text)
        self.system_prompt_edit.blockSignals(False)

    def accept(self):
        if self.section == "config":
            self._save_secret_override("api_key", self.api_key_edit)
            self.config.set_base_url(self.base_url_edit.text().strip())
            self.config.set_model(self.model_edit.text().strip())
            self.config.set_vision_model(self.vision_model_edit.text().strip())
            self.config.set_reasoning_effort(self.reasoning_combo.currentData())
            self.config.set_searxng_url(self.searxng_url_edit.text().strip())
            self._save_secret_override("tavily_api_key", self.tavily_api_key_edit)
            self._save_secret_override("amap_api_key", self.amap_api_key_edit)
        elif self.section == "prompt":
            current_prompt = self.system_prompt_edit.toPlainText()
            if self._prompt_reset_requested:
                self.config.reset_system_prompt()
            elif current_prompt != self.initial_system_prompt_text:
                self.config.set_system_prompt(current_prompt)
        elif self.section == "voice":
            self._save_secret_override("xfyun_appid", self.xfyun_appid_edit)
            self._save_secret_override("xfyun_api_key", self.xfyun_api_key_edit)
            self._save_secret_override("xfyun_api_secret", self.xfyun_api_secret_edit)
            self.config.set_tts_enabled(self.tts_checkbox.isChecked())
            self.config.set_tts_voice(self.voice_combo.currentData())
        super().accept()

    def _save_secret_override(self, key, edit):
        value = edit.text().strip()
        initial_local = self._secret_initial_values.get(key, "")
        if not initial_local and value == self._effective_secret_value(key):
            return
        if value:
            self.config.settings.setValue(key, value)
        elif initial_local:
            self.config.settings.remove(key)

    def _effective_secret_value(self, key):
        getters = {
            "api_key": self.config.get_api_key,
            "tavily_api_key": self.config.get_tavily_api_key,
            "amap_api_key": self.config.get_amap_api_key,
            "xfyun_appid": self.config.get_xfyun_appid,
            "xfyun_api_key": self.config.get_xfyun_api_key,
            "xfyun_api_secret": self.config.get_xfyun_api_secret,
        }
        return str(getters[key]() or "").strip()


# ======================== 内容区背景 ========================
class ResizableBackgroundPanel(QWidget):
    """在聊天栏内部绘制可随尺寸变化的背景。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._background_pixmap = QPixmap()
        self._background_mode = "stretch"
        self._scaled_cache = QPixmap()
        self._scaled_cache_key = None
        self.setAutoFillBackground(False)

    def set_background_image(self, image_path=None):
        self._background_pixmap = QPixmap(image_path) if image_path else QPixmap()
        self._scaled_cache_key = None
        self.update()

    def set_background_mode(self, mode):
        if mode not in {"fill", "fit", "stretch", "tile", "center"}:
            mode = "stretch"
        self._background_mode = mode
        self.update()

    def _scaled_background(self, size, aspect_mode):
        """按目标尺寸的物理像素缓存平滑缩放结果，避免每帧对高分辨率背景图重算。"""
        dpr = max(1.0, self.devicePixelRatioF())
        width = max(1, round(size.width() * dpr))
        height = max(1, round(size.height() * dpr))
        key = (width, height, getattr(aspect_mode, "value", aspect_mode),
               self._background_pixmap.cacheKey())
        if self._scaled_cache_key == key and not self._scaled_cache.isNull():
            return self._scaled_cache
        self._scaled_cache = self._background_pixmap.scaled(
            QSize(width, height), aspect_mode, Qt.TransformationMode.SmoothTransformation
        )
        self._scaled_cache_key = key
        return self._scaled_cache

    def paintEvent(self, event):
        painter = QPainter(self)
        target = self.rect()
        painter.fillRect(target, QColor("#1f1f1f"))
        if not self._background_pixmap.isNull() and target.width() > 0 and target.height() > 0:
            mode = self._background_mode
            if mode == "tile":
                painter.drawTiledPixmap(target, self._background_pixmap)
            elif mode == "center":
                x = (target.width() - self._background_pixmap.width()) // 2
                y = (target.height() - self._background_pixmap.height()) // 2
                painter.drawPixmap(x, y, self._background_pixmap)
            else:
                aspect_mode = {
                    "fill": Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    "fit": Qt.AspectRatioMode.KeepAspectRatio,
                    "stretch": Qt.AspectRatioMode.IgnoreAspectRatio,
                }[mode]
                scaled = self._scaled_background(target.size(), aspect_mode)
                dpr = max(1.0, self.devicePixelRatioF())
                if mode == "fit":
                    width = max(1, round(scaled.width() / dpr))
                    height = max(1, round(scaled.height() / dpr))
                    painter.drawPixmap((target.width() - width) // 2, (target.height() - height) // 2,
                                       width, height, scaled)
                elif mode == "fill":
                    width = max(1, round(target.width() * dpr))
                    height = max(1, round(target.height() * dpr))
                    source = QRect(
                        max(0, (scaled.width() - width) // 2),
                        max(0, (scaled.height() - height) // 2),
                        min(width, scaled.width()),
                        min(height, scaled.height()),
                    )
                    painter.drawPixmap(target, scaled, source)
                else:
                    painter.drawPixmap(target, scaled)
        painter.end()
        super().paintEvent(event)


# ======================== 聊天消息气泡组件 ========================
class ChatMessageWidget(QWidget):
    MIN_BUBBLE_WIDTH = 140
    MAX_BUBBLE_WIDTH = 960
    BUBBLE_HORIZONTAL_PADDING = 28
    BUBBLE_VERTICAL_PADDING = 16
    HEIGHT_SAFETY = 4

    def __init__(self, message, is_user=True, opacity=0.85, parent=None, sources=None):
        super().__init__(parent)
        self.is_user = is_user
        self.sources = sources or []
        self._text = ""
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(5, 5, 5, 5)
        self._layout.setSpacing(8)

        # 头像（使用图片文件）
        self.avatar_label = QLabel()
        if is_user:
            img_path = get_resource_path("assets", "web_resources", "user.png")
        else:
            img_path = get_resource_path("assets", "web_resources", "mao.png")
        
        pixmap = QPixmap(img_path)
        # 如果图片加载失败，可以回退到 emoji（可选）
        if pixmap.isNull():
            # 图片加载失败，回退到 emoji
            self.avatar_label.setText("👤" if is_user else "🤖")
            self.avatar_label.setStyleSheet("font-size: 24px; background-color: #0078D7; border-radius: 20px; padding: 6px; color: white;")
            self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            pixmap = pixmap.scaled(44, 44, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.avatar_label.setPixmap(pixmap)
                
        self.avatar_label.setFixedSize(44, 44)
        self.list_item = None

        # 消息文本 (QTextBrowser 支持可点击链接)
        self.message_text = QTextBrowser()
        self.message_text.setReadOnly(True)
        self.message_text.setFrameShape(QFrame.Shape.NoFrame)
        self.message_text.setLineWidth(0)
        self.message_text.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.message_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.message_text.document().setDocumentMargin(0)

        base_style = """
            QTextEdit {
                border-radius: 10px;
                padding: 6px 10px;
                color: #000000;
                background-color: %s;
                border: none;
                font-size: 14px;
            }
        """
        if is_user:
            bg_color = f"rgba(227, 242, 253, {opacity})"
        else:
            bg_color = f"rgba(240, 240, 240, {opacity})"
        self.message_text.setStyleSheet(base_style % bg_color)
        self.message_text.setOpenExternalLinks(True)

        # 布局
        if is_user:
            self._layout.addWidget(self.avatar_label)
            self._layout.addWidget(self.message_text, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self._layout.addStretch(1)
        else:
            self._layout.addStretch(1)
            self._layout.addWidget(self.message_text, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            self._layout.addWidget(self.avatar_label)

        self.list_item = None
        self.set_text(message)

    def update_opacity(self, opacity):
        base_style = """
            QTextEdit {
                border-radius: 10px;
                padding: 6px 10px;
                color: #000000;
                background-color: %s;
                border: none;
                font-size: 14px;
            }
        """
        if self.is_user:
            bg_color = f"rgba(227, 242, 253, {opacity})"
        else:
            bg_color = f"rgba(240, 240, 240, {opacity})"
        self.message_text.setStyleSheet(base_style % bg_color)

    def set_text(self, text):
        self._text = text
        if self.sources:
            self.message_text.setHtml(self._build_html(text, self.sources))
        else:
            self.message_text.setPlainText(text)
        self._sync_bubble_geometry()
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())
        else:
            # 自动查找父级 QListWidgetItem
            parent = self.parent()
            while parent and not isinstance(parent, QListWidget):
                parent = parent.parent()
            if isinstance(parent, QListWidget):
                for i in range(parent.count()):
                    if parent.itemWidget(parent.item(i)) == self:
                        self.list_item = parent.item(i)
                        self.list_item.setSizeHint(self.sizeHint())
                        break

    def _build_html(self, text, sources):
        body = html.escape(text).replace("\n", "<br>")
        parts = [f"<div>{body}</div>"]
        if sources:
            parts.append(
                "<div style='margin-top: 8px; padding-top: 6px; border-top: 1px solid #DDDDDD; "
                "font-size: 12px; color: #666666;'>来源：</div>"
            )
            for index, item in enumerate(sources, 1):
                title = html.escape(str(item.get("title", "") or "链接"))
                url = html.escape(str(item.get("url", "") or ""), quote=True)
                parts.append(
                    f"<div style='font-size: 12px;'><a href='{url}' style='color: #1a73e8; "
                    f"text-decoration: none;'>[{index}] {title}</a></div>"
                )
        return "".join(parts)

    def refresh_layout(self):
        self._sync_bubble_geometry()
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())

    def _sync_bubble_geometry(self):
        target_width = self._calculate_bubble_width(self._text)
        content_width = max(40, target_width - self.BUBBLE_HORIZONTAL_PADDING)
        self.message_text.setFixedWidth(target_width)
        doc = self.message_text.document()
        doc.setTextWidth(content_width)
        # 先按最终宽度完成排版，再读取布局后的实际高度；浮点高度向上取整，避免末行或圆角被列表项裁掉。
        doc_height = math.ceil(doc.documentLayout().documentSize().height())
        total_height = doc_height + self.BUBBLE_VERTICAL_PADDING + self.HEIGHT_SAFETY
        self.message_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setFixedHeight(total_height)

    def _calculate_bubble_width(self, text):
        max_width = self._get_max_bubble_width()
        metrics = QFontMetrics(self.message_text.font())
        lines = text.splitlines() or [""]
        longest_line_width = max(metrics.horizontalAdvance(line) for line in lines) if lines else 0
        suggested_width = longest_line_width + self.BUBBLE_HORIZONTAL_PADDING
        if len(text) > 80:
            suggested_width = max(suggested_width, int(max_width * 0.82))
        return max(self.MIN_BUBBLE_WIDTH, min(int(suggested_width), max_width))

    def _get_max_bubble_width(self):
        list_widget = self._find_list_widget()
        if not list_widget:
            return self.MAX_BUBBLE_WIDTH
        viewport_width = list_widget.viewport().width()
        reserved = self.avatar_label.width() + self._layout.spacing() + 22
        candidate = viewport_width - reserved
        return max(self.MIN_BUBBLE_WIDTH, min(candidate, self.MAX_BUBBLE_WIDTH))

    def _find_list_widget(self):
        parent = self.parent()
        while parent and not isinstance(parent, QListWidget):
            parent = parent.parent()
        return parent

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_bubble_geometry()

    def sizeHint(self):
        bubble_width = self.message_text.width()
        bubble_height = self.message_text.height()
        margins = self._layout.contentsMargins()
        total_width = bubble_width + self.avatar_label.width() + self._layout.spacing() + margins.left() + margins.right()
        total_height = max(bubble_height, self.avatar_label.height()) + margins.top() + margins.bottom()
        return QSize(total_width, total_height)


# ======================== 本地HTTP服务器 (Live2D) ========================
class TaskProgressWidget(QWidget):
    """AI 回复占位组件：右侧气泡内显示任务/工具执行进度，下方显示流式文本。"""
    MIN_BUBBLE_WIDTH = 140
    MAX_BUBBLE_WIDTH = 960
    BUBBLE_HORIZONTAL_PADDING = 28
    BUBBLE_VERTICAL_PADDING = 16
    HEIGHT_SAFETY = 4

    def __init__(self, opacity=0.85, parent=None):
        super().__init__(parent)
        self._text = ""
        self.list_item = None
        self._has_progress = False
        self._show_divider = False
        self._sync_pending = False
        self._content_size = QSize(320, 64)
        self._progress_h = 0
        self._message_h = 0

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(5, 5, 5, 5)
        self._layout.setSpacing(8)

        # 头像（与其他助手消息一致）
        self.avatar_label = QLabel()
        img_path = get_resource_path("assets", "web_resources", "mao.png")
        pixmap = QPixmap(img_path)
        if pixmap.isNull():
            self.avatar_label.setText("🤖")
            self.avatar_label.setStyleSheet("font-size: 24px; background-color: #0078D7; border-radius: 20px; padding: 6px; color: white;")
            self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            pixmap = pixmap.scaled(44, 44, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.avatar_label.setPixmap(pixmap)
        self.avatar_label.setFixedSize(44, 44)

        # 气泡容器：统一浅灰圆角背景，与普通消息气泡一致
        self.bubble_container = QWidget()
        self.bubble_container.setObjectName("progressBubble")
        self.bubble_container.setStyleSheet(
            f"QWidget#progressBubble {{ background-color: rgba(240, 240, 240, {opacity}); border-radius: 10px; }}"
        )
        self.bubble_layout = QVBoxLayout(self.bubble_container)
        self.bubble_layout.setContentsMargins(12, 10, 12, 10)
        self.bubble_layout.setSpacing(6)

        # 进度区（富文本，透明背景；用 QTextEdit 以精确计算高度，避免文字被裁剪）
        self.progress_text = QTextEdit()
        self.progress_text.setReadOnly(True)
        self.progress_text.setFrameShape(QFrame.Shape.NoFrame)
        self.progress_text.setLineWidth(0)
        self.progress_text.setVisible(False)
        self.progress_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.progress_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.progress_text.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.progress_text.document().setDocumentMargin(0)
        self.progress_text.setStyleSheet(
            "QTextEdit { border: none; background: transparent; color: #333333; font-size: 14px; padding: 0px; }"
        )
        self.bubble_layout.addWidget(self.progress_text)

        # 进度与正文之间的分隔线
        self.divider = QLabel()
        self.divider.setFixedHeight(1)
        self.divider.setStyleSheet("background-color: #D8D8D8; border: none;")
        self.divider.setVisible(False)
        self.bubble_layout.addWidget(self.divider)

        # 流式文本区
        self.message_text = QTextEdit()
        self.message_text.setReadOnly(True)
        self.message_text.setFrameShape(QFrame.Shape.NoFrame)
        self.message_text.setLineWidth(0)
        self.message_text.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.message_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.message_text.document().setDocumentMargin(0)
        self.message_text.setStyleSheet(
            "QTextEdit { border: none; background: transparent; color: #000000; font-size: 14px; padding: 0px; }"
        )
        self.bubble_layout.addWidget(self.message_text)

        self._layout.addStretch(1)
        self._layout.addWidget(self.bubble_container, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self._layout.addWidget(self.avatar_label)

    def refresh_layout(self):
        self._sync_geometry()
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())

    def set_text(self, text):
        self._text = text
        self.message_text.setPlainText(text)
        self._sync_geometry()
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())

    def update_progress(self, task_state):
        # 带附件的消息不会生成任务状态，这里要容忍 None
        steps = (task_state or {}).get("plan_steps", [])
        self._has_progress = bool(steps)
        self._show_divider = bool(steps)
        if steps:
            blocks = []
            for i, step in enumerate(steps):
                status = step.get("status", "")
                icon = {"in_progress": "\u23f3", "completed": "\u2705", "failed": "\u274c"}.get(status, "\u2022")
                title = html.escape(str(step.get("title", "")))
                margin = ' style="margin-top: 6px;"' if i else ""
                line = (
                    f'<div{margin} style="font-size: 15px; font-weight: 600; color: #333333;">'
                    f"{icon} {title}</div>"
                )
                detail = str(step.get("detail", "") or "")
                if detail and status in {"in_progress", "failed"}:
                    detail = html.escape(detail if len(detail) <= 60 else detail[:60] + "\u2026")
                    line += (
                        '<div style="margin-top: 2px; font-size: 13px; font-weight: 400; color: #777777;">'
                        f"&nbsp;&nbsp;&nbsp;{detail}</div>"
                    )
                blocks.append(line)
            self.progress_text.setHtml("".join(blocks))
            self.progress_text.setVisible(True)
            self.divider.setVisible(True)
        else:
            self.progress_text.setHtml("")
            self.progress_text.setVisible(False)
            self.divider.setVisible(False)
        self._sync_geometry()

    def _sync_geometry(self):
        width = self._calculate_bubble_width()
        content_width = max(40, width - 24)
        self.bubble_container.setFixedWidth(width)

        if self._has_progress:
            self.progress_text.setFixedWidth(content_width)
            progress_doc = self.progress_text.document()
            progress_doc.setTextWidth(content_width)
            progress_h = math.ceil(progress_doc.documentLayout().documentSize().height()) + self.HEIGHT_SAFETY
            self.progress_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._progress_h = progress_h
            self.progress_text.setFixedHeight(self._progress_h)
        else:
            self.progress_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._progress_h = 0

        self.message_text.setFixedWidth(content_width)
        doc = self.message_text.document()
        doc.setTextWidth(content_width)
        self._message_h = math.ceil(doc.documentLayout().documentSize().height()) + self.HEIGHT_SAFETY
        self.message_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setFixedHeight(self._message_h)
        self._update_content_size()
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())

    def _update_content_size(self):
        outer = self._layout.contentsMargins()
        bubble_w = self.bubble_container.width()
        progress_h = self._progress_h
        divider_h = self.divider.height() if self._show_divider else 0
        message_h = self._message_h
        margins = self.bubble_layout.contentsMargins()
        bubble_h = (
            margins.top() + progress_h + divider_h + message_h
            + self.bubble_layout.spacing() * 2 + margins.bottom()
        )
        self._content_size = QSize(
            bubble_w + self.avatar_label.width() + self._layout.spacing() + outer.left() + outer.right(),
            bubble_h + outer.top() + outer.bottom(),
        )

    def sizeHint(self):
        return QSize(self._content_size)

    def minimumSizeHint(self):
        return QSize(self._content_size)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_sync()

    def _schedule_sync(self):
        if self._sync_pending:
            return
        self._sync_pending = True
        QTimer.singleShot(0, self._run_scheduled_sync)

    def _run_scheduled_sync(self):
        self._sync_pending = False
        self._sync_geometry()

    def _calculate_bubble_width(self):
        max_width = self._get_max_bubble_width()
        metrics = QFontMetrics(self.message_text.font())
        progress_plain = self.progress_text.toPlainText() if self._has_progress else ""
        combined = f"{progress_plain}\n{self._text}"
        lines = combined.splitlines() or [""]
        longest_line_width = max(metrics.horizontalAdvance(line) for line in lines) if lines else 0
        suggested_width = longest_line_width + self.BUBBLE_HORIZONTAL_PADDING
        if len(combined) > 80:
            suggested_width = max(suggested_width, int(max_width * 0.72))
        return max(self.MIN_BUBBLE_WIDTH, min(int(suggested_width), max_width))

    def _get_max_bubble_width(self):
        list_widget = self._find_list_widget()
        if not list_widget:
            return self.MAX_BUBBLE_WIDTH
        viewport_width = list_widget.viewport().width()
        reserved = self.avatar_label.width() + self._layout.spacing() + 22
        candidate = viewport_width - reserved
        return max(self.MIN_BUBBLE_WIDTH, min(candidate, self.MAX_BUBBLE_WIDTH))

    def _find_list_widget(self):
        parent = self.parent()
        while parent and not isinstance(parent, QListWidget):
            parent = parent.parent()
        return parent if isinstance(parent, QListWidget) else None


class Live2DRequestHandler(SimpleHTTPRequestHandler):
    """避免无控制台打包程序中的 stderr 写入导致请求被中断。"""

    def log_message(self, format_string, *args):
        logging.info("Live2D HTTP: %s", format_string % args)


class LocalHTTPServer:
    def __init__(self, directory, port=8123):
        self.directory = directory
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        index_path = os.path.join(self.directory, "pet.html")
        if not os.path.isfile(index_path):
            raise FileNotFoundError(f"Live2D 页面资源缺失: {index_path}")
        handler = partial(Live2DRequestHandler, directory=self.directory)
        self.server = HTTPServer(("localhost", self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        logging.info("本地HTTP服务器已启动: http://localhost:%s，目录：%s", self.port, self.directory)

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def get_url(self, path=""):
        return f"http://localhost:{self.port}/{path.lstrip('/')}"


# 裁剪出的背景图长边上限：保留原图分辨率，只在超过这里时等比缩小（3840 够 4K 屏用）
CHAT_BG_MAX_SIDE = 3840


def limit_pixmap_side(pixmap, max_side):
    """只降不升：长边超过上限就等比缩下来，否则原样返回。"""
    if pixmap.isNull() or max_side <= 0:
        return pixmap
    if max(pixmap.width(), pixmap.height()) <= max_side:
        return pixmap
    return pixmap.scaled(
        max_side,
        max_side,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class CropImageLabel(QLabel):
    def __init__(self, pixmap, aspect_ratio=None, parent=None):
        super().__init__(parent)
        self.original_pixmap = pixmap
        self.aspect_ratio = aspect_ratio
        self.display_rect = QRect()
        self.selection_rect = QRect()
        self.origin = QPoint()
        self.selecting = False
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMouseTracking(True)
        self.setStyleSheet("background-color: #1f1f1f; border: 1px solid #444;")
        self.setMinimumSize(720, 460)
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self):
        if self.original_pixmap.isNull():
            return
        target_size = self.contentsRect().size()
        scaled = self.original_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        self.display_rect = QRect(x, y, scaled.width(), scaled.height())
        if self.selection_rect:
            self.rubber_band.setGeometry(self.selection_rect)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.display_rect.contains(event.position().toPoint()):
            super().mousePressEvent(event)
            return
        self.origin = self._clamp_to_display_rect(event.position().toPoint())
        self.selecting = True
        self.selection_rect = QRect(self.origin, QSize())
        self.rubber_band.setGeometry(self.selection_rect)
        self.rubber_band.show()
        event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self.selecting:
            current = self._clamp_to_display_rect(event.position().toPoint())
            self.selection_rect = self._build_selection_rect(current)
            self.rubber_band.setGeometry(self.selection_rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selecting = False
            if self.selection_rect.width() < 8 or self.selection_rect.height() < 8:
                self.selection_rect = QRect()
                self.rubber_band.hide()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _clamp_to_display_rect(self, point):
        return QPoint(
            max(self.display_rect.left(), min(point.x(), self.display_rect.right())),
            max(self.display_rect.top(), min(point.y(), self.display_rect.bottom())),
        )

    def _build_selection_rect(self, current):
        raw_rect = QRect(self.origin, current).normalized()
        if not self.aspect_ratio or raw_rect.width() == 0 or raw_rect.height() == 0:
            return raw_rect

        direction_x = 1 if current.x() >= self.origin.x() else -1
        direction_y = 1 if current.y() >= self.origin.y() else -1

        width = max(1, abs(current.x() - self.origin.x()))
        height = max(1, abs(current.y() - self.origin.y()))

        if width / height >= self.aspect_ratio:
            width = int(height * self.aspect_ratio)
        else:
            height = int(width / self.aspect_ratio)

        right = self.origin.x() + direction_x * width
        bottom = self.origin.y() + direction_y * height
        adjusted = QRect(self.origin, QPoint(right, bottom)).normalized()
        return adjusted.intersected(self.display_rect)

    def _selection_to_source_rect(self, rect):
        scale_x = self.original_pixmap.width() / max(1, self.display_rect.width())
        scale_y = self.original_pixmap.height() / max(1, self.display_rect.height())
        return QRect(
            int((rect.left() - self.display_rect.left()) * scale_x),
            int((rect.top() - self.display_rect.top()) * scale_y),
            max(1, int(rect.width() * scale_x)),
            max(1, int(rect.height() * scale_y)),
        ).intersected(self.original_pixmap.rect())

    def _center_fit_source_rect(self):
        if not self.aspect_ratio or self.original_pixmap.isNull():
            return self.original_pixmap.rect()

        src_w = self.original_pixmap.width()
        src_h = self.original_pixmap.height()
        src_ratio = src_w / max(1, src_h)

        if src_ratio > self.aspect_ratio:
            target_w = int(src_h * self.aspect_ratio)
            x = (src_w - target_w) // 2
            return QRect(x, 0, target_w, src_h)
        target_h = int(src_w / self.aspect_ratio)
        y = (src_h - target_h) // 2
        return QRect(0, y, src_w, target_h)

    def get_cropped_pixmap(self, use_full_image=False):
        if self.original_pixmap.isNull():
            return self.original_pixmap

        if use_full_image or self.selection_rect.isNull():
            crop_rect = self._center_fit_source_rect()
        else:
            crop_rect = self._selection_to_source_rect(self.selection_rect)

        result = self.original_pixmap.copy(crop_rect) if crop_rect.isValid() else self.original_pixmap
        # 不再压到面板像素尺寸：保留裁剪出来的原生分辨率，只有超过上限才等比缩小
        return limit_pixmap_side(result, CHAT_BG_MAX_SIDE)


class ImageCropDialog(QDialog):
    def __init__(self, pixmap, target_size=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("裁剪聊天背景")
        self.setMinimumSize(860, 620)
        self._result_pixmap = pixmap
        self.target_size = target_size or QSize()
        self.aspect_ratio = None
        if self.target_size.width() > 0 and self.target_size.height() > 0:
            self.aspect_ratio = self.target_size.width() / self.target_size.height()

        layout = QVBoxLayout(self)
        helper_text = "拖拽框选要显示的区域，不框选则使用整张图片。"
        if self.aspect_ratio:
            helper_text += f" 当前裁剪框会锁定为背景板比例 {self.target_size.width()}:{self.target_size.height()}。"
        layout.addWidget(QLabel(helper_text))

        self.crop_label = CropImageLabel(pixmap, self.aspect_ratio, self)
        layout.addWidget(self.crop_label, 1)

        button_row = QHBoxLayout()
        self.use_original_btn = QPushButton("使用整张图")
        self.apply_crop_btn = QPushButton("裁剪并应用")
        self.cancel_btn = QPushButton("取消")
        button_row.addWidget(self.use_original_btn)
        button_row.addStretch(1)
        button_row.addWidget(self.apply_crop_btn)
        button_row.addWidget(self.cancel_btn)
        layout.addLayout(button_row)

        self.use_original_btn.clicked.connect(self.use_original)
        self.apply_crop_btn.clicked.connect(self.apply_crop)
        self.cancel_btn.clicked.connect(self.reject)

    def use_original(self):
        self._result_pixmap = self.crop_label.get_cropped_pixmap(use_full_image=True)
        self.accept()

    def apply_crop(self):
        self._result_pixmap = self.crop_label.get_cropped_pixmap()
        self.accept()

    def get_result_pixmap(self):
        return self._result_pixmap


class PetWebView(QWebEngineView):
    def __init__(self, pet_window):
        super().__init__(pet_window)
        self.pet_window = pet_window
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setStyleSheet("background: transparent; border: none;")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.page().setBackgroundColor(Qt.GlobalColor.transparent)


class PetInteractionOverlay(QWidget):
    DRAG_THRESHOLD = 6

    def __init__(self, pet_window, webview):
        super().__init__(pet_window)
        self.pet_window = pet_window
        self.webview = webview
        self._right_press_pos = None
        self._right_dragging = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._forward_mouse_event(event)
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            self._right_press_pos = event.globalPosition().toPoint()
            self._right_dragging = False
            self.pet_window.begin_drag(event.globalPosition().toPoint())
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._forward_mouse_event(event)
            event.accept()
            return

        if event.buttons() & Qt.MouseButton.RightButton and self._right_press_pos is not None:
            if (event.globalPosition().toPoint() - self._right_press_pos).manhattanLength() >= self.DRAG_THRESHOLD:
                if not self._right_dragging:
                    self._right_dragging = True
                    self.pet_window.start_pet_drag_stage()
                self.pet_window.update_drag(event.globalPosition().toPoint())
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._forward_mouse_event(event)
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            self.pet_window.end_drag()
            if self._right_dragging:
                self._right_dragging = False
                self._right_press_pos = None
                event.accept()
                return

            self._right_press_pos = None
            self.pet_window.show_context_menu(event.globalPosition().toPoint())
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _forward_mouse_event(self, event):
        local_point = event.position().toPoint()
        webview_width = max(1, self.width())
        webview_height = max(1, self.height())
        if not (0 <= local_point.x() <= webview_width and 0 <= local_point.y() <= webview_height):
            return

        ratio_x = max(0.0, min(1.0, local_point.x() / webview_width))
        ratio_y = max(0.0, min(1.0, local_point.y() / webview_height))
        buttons = 0
        if event.buttons() & Qt.MouseButton.LeftButton:
            buttons |= 1

        event_type_map = {
            QEvent.Type.MouseButtonPress: "pointerdown",
            QEvent.Type.MouseMove: "pointermove",
            QEvent.Type.MouseButtonRelease: "pointerup",
        }
        event_type = event_type_map.get(event.type())
        if not event_type:
            return

        script = f"""
        (() => {{
            const ratioX = {ratio_x:.8f};
            const ratioY = {ratio_y:.8f};
            const target = document.querySelector('canvas') || document.body;
            if (!target) return;
            const rect = target.getBoundingClientRect();
            const clientX = rect.left + rect.width * ratioX;
            const clientY = rect.top + rect.height * ratioY;
            if (window.__petForwardPointer) {{
                window.__petForwardPointer("{event_type}", clientX, clientY, {buttons});
                return;
            }}
            const evt = new PointerEvent("{event_type}", {{
                bubbles: true,
                cancelable: true,
                composed: true,
                clientX,
                clientY,
                pageX: clientX,
                pageY: clientY,
                button: 0,
                buttons: {buttons},
                pointerType: "mouse",
                isPrimary: true
            }});
            target.dispatchEvent(evt);
        }})();
        """
        self.webview.page().runJavaScript(script)


class PetSpeechBubble(QWidget):
    """桌宠的非交互式说话气泡。"""
    MAX_TEXT_WIDTH = 220
    MAX_BODY_HEIGHT = 126
    MAX_LABEL_HEIGHT = MAX_BODY_HEIGHT - 17
    TAIL_WIDTH = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accent = QColor("#64748B")
        self._side = "right"
        self._display_scale = 1.0
        self._message = ""
        self._text_width = 58
        self._body_height = 42
        self.text_label = QLabel(self)
        self.text_label.setWordWrap(True)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.text_label.setStyleSheet(
            "background: transparent; color: #1F2937; "
            "padding: 0px;"
        )
        self.set_scale(1.0)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.hide()

    def set_message(self, text, accent=None):
        message = str(text or "").strip()
        if not message:
            self.hide()
            return
        self._message = message
        if accent:
            self._accent = QColor(accent)

        metrics = QFontMetrics(self.text_label.font())
        bounds = metrics.boundingRect(
            QRect(0, 0, self._max_text_width, 1000),
            int(Qt.TextFlag.TextWordWrap),
            message,
        )
        self._text_width = max(58, min(self._max_text_width, bounds.width() + 4))
        self._body_height = max(42, min(self.MAX_BODY_HEIGHT, bounds.height() + 20))
        self.text_label.setText(message)
        self._update_layout()
        self.update()

    def set_scale(self, scale):
        self._display_scale = max(0.7, min(1.4, float(scale)))
        self._max_text_width = round(self.MAX_TEXT_WIDTH * self._display_scale)
        font = QFont("Microsoft YaHei")
        font.setPointSizeF(max(11.0, min(18.0, 14.0 * self._display_scale)))
        self.text_label.setFont(font)
        if self._message:
            self.set_message(self._message)

    def set_side(self, side):
        if side not in {"left", "right", "above"} or side == self._side:
            return
        self._side = side
        self._update_layout()
        self.update()

    def _update_layout(self):
        if self._side == "above":
            self.setFixedSize(self._text_width + 28, self._body_height + self.TAIL_WIDTH)
            self.text_label.setGeometry(14, 9, self._text_width, self._body_height - 17)
            return
        bubble_width = self._text_width + 28 + self.TAIL_WIDTH
        body_x = self.TAIL_WIDTH + 1 if self._side == "right" else 1
        self.setFixedSize(bubble_width, self._body_height + 2)
        self.text_label.setGeometry(body_x + 14, 9, self._text_width, self._body_height - 17)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._side == "above":
            body = QRectF(1, 1, self.width() - 2, self.height() - self.TAIL_WIDTH - 2)
        else:
            body_x = self.TAIL_WIDTH + 1 if self._side == "right" else 1
            body = QRectF(body_x, 1, self.width() - self.TAIL_WIDTH - 2, self.height() - 2)
        body_path = QPainterPath()
        body_path.addRoundedRect(body, 12, 12)
        tail_path = QPainterPath()
        if self._side == "above":
            tail_x = body.center().x()
            tail_path.moveTo(tail_x - 9, body.bottom())
            tail_path.lineTo(tail_x, self.height() - 1)
            tail_path.lineTo(tail_x + 9, body.bottom())
        else:
            tail_y = max(body.top() + 16, min(body.bottom() - 16, body.center().y()))
            if self._side == "right":
                tail_path.moveTo(body.left(), tail_y - 9)
                tail_path.lineTo(1, tail_y)
                tail_path.lineTo(body.left(), tail_y + 9)
            else:
                tail_path.moveTo(body.right(), tail_y - 9)
                tail_path.lineTo(self.width() - 1, tail_y)
                tail_path.lineTo(body.right(), tail_y + 9)
        tail_path.closeSubpath()
        painter.setPen(QPen(self._accent, 1.2))
        painter.setBrush(QColor(255, 255, 255, 238))
        painter.drawPath(body_path)
        painter.drawPath(tail_path)


# 桌宠拖拽演出：被拎起来 -> 道具淡出 -> 摔一下 -> 爬起来 -> 道具淡回
PET_DRAG_STAGE_ENABLED = True
PET_DRAG_FALL_MS = 450
PET_DRAG_RECOVER_MS = 800
PET_DRAG_RESTORE_MS = 450
PET_MOUTH_TICK_MS = 50       # 口型推进间隔
PET_MOUTH_HOLD_MS = 250      # 页面侧超过这个时间没收到更新就闭嘴（与脚本里的 MOUTH_HOLD_MS 保持一致）
PET_DRAG_STAGE_SCRIPT = r"""
// 桌宠「被拎起来」演出驱动：main.py 通过 __petStageSet 推阶段，这里在每个模型帧里改参数与道具透明度
(() => {
  if (window.__petDragStageReady) { return; }
  window.__petDragStageReady = true;

  const PROP_PARTS = { Part18: 1, Part21: 1, Part35: 1, Part109: 1, Part123: 1, Part122: 1, Part5: 1, Part121: 1, Part119: 1, Part120: 1, Part4: 1, Part22: 1, Part114: 1, Part6: 1, Part107: 1, Part106: 1, Part110: 1, Part43: 1, Part10: 1, Part15: 1, Part37: 1, Part112: 1, Part124: 1, Part127: 1, Part129: 1, Part130: 1, Part128: 1, ABXY: 1, M: 1 };

  const NEUTRAL = { ParamAngleZ: 0, ParamBodyAngleZ: 0, ParamAngleX: 0, ParamAngleY: 0, ParamEyeLOpen: 1, ParamEyeROpen: 1, ParamEyeBallX: 0, ParamEyeBallY: 0, ParamBrowLForm: 0, ParamBrowRForm: 0, ParamCheek77: 0, ParamCheek22: 0, ParamCheek20: 0, ParamMouthForm: -0.5, ParamMouthOpenY: 0 };
  const POSE_HOLD = Object.assign({}, NEUTRAL, { ParamEyeLOpen: 0.55, ParamEyeROpen: 0.55, ParamEyeBallX: 0.1, ParamEyeBallY: 0.6, ParamBrowLForm: 1, ParamBrowRForm: 1, ParamCheek77: 1, ParamMouthForm: -0.6, ParamMouthOpenY: 0.3 });
  const POSE_FALL = Object.assign({}, NEUTRAL, { ParamEyeLOpen: 0, ParamEyeROpen: 0, ParamBrowLForm: -1, ParamBrowRForm: -1, ParamCheek22: 1, ParamMouthForm: -1.5, ParamMouthOpenY: 1 });
  const PARAM_ORDER = Object.keys(NEUTRAL);

  const IDLE_MS = 140;
  const FALL_S = 0.45;
  const RECOVER_S = 0.8;
  const FADE_RATE = 9;
  const FADE_PROPS = false;   // 道具只在 true 时淡出；留着手才有地方放

  const stage = { phase: "idle", vx: 0, vy: 0, at: performance.now() };
  window.__petStage = stage;
  window.__petStageSet = function (phase, vx, vy) {
    stage.phase = phase;
    stage.vx = vx || 0;
    stage.vy = vy || 0;
    stage.at = performance.now();
  };

  const MOUTH_HOLD_MS = 250;
  const mouth = { open: 0, jitter: 0, at: 0 };
  window.__petMouthSet = function (open, jitter) {
    mouth.open = clamp(Number(open) || 0, 0, 1);
    mouth.jitter = clamp(Number(jitter) || 0, 0, 1);
    mouth.at = performance.now();
  };
  let fadeModel = null;
  let fadeList = null;
  let propK = 1;
  let propTarget = 1;
  let rot = 0;
  let lift = 0;
  let squash = 1;
  let dt = 0.016;
  let phaseAt = performance.now();
  let lastPhase = "idle";
  let lastTick = performance.now();
  let baseTransform = null;

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function ease(u) { return u < 0.5 ? 2 * u * u : 1 - 2 * (1 - u) * (1 - u); }
  function stageError(where, err) {
    window.__petStageErr = where + ": " + (err && err.message ? err.message : String(err));
    if (!window.__petStageErrLogged) {
      window.__petStageErrLogged = true;
      console.error("[pet-stage] " + window.__petStageErr);
    }
  }

  function fadeTargets(model) {
    if (fadeModel === model && fadeList) { return fadeList; }
    const core = model._model;
    const names = Array.from(core.parts.ids);
    const parent = core.drawables.parentPartIndices;
    const list = [];
    for (let i = 0; i < core.drawables.count; i++) {
      if (PROP_PARTS[names[parent[i]]]) { list.push(i); }
    }
    fadeModel = model;
    fadeList = list;
    return list;
  }

  let paramModel = null;
  let paramIndex = null;

  function paramMap(model) {
    if (paramModel === model && paramIndex) { return paramIndex; }
    const ids = Array.from(model._model.parameters.ids);
    const map = {};
    for (let i = 0; i < PARAM_ORDER.length; i++) { map[PARAM_ORDER[i]] = ids.indexOf(PARAM_ORDER[i]); }
    paramModel = model;
    paramIndex = map;
    return map;
  }

  // 注意：框架的 setParameterValueById 只认 CubismId 对象（字符串查不到会被静默丢弃），所以这里按参数下标写
  function writePose(model, pose) {
    const map = paramMap(model);
    for (let i = 0; i < PARAM_ORDER.length; i++) {
      const key = PARAM_ORDER[i];
      const index = map[key];
      if (index >= 0) { model.setParameterValueByIndex(index, pose[key]); }
    }
  }

  window.__petStageProbe = function () {
    const model = paramModel;
    const map = paramIndex;
    const info = { hook: true, phase: stage.phase, err: window.__petStageErr || null, index: {}, value: {}, mouth: { open: mouth.open, jitter: mouth.jitter, age: performance.now() - mouth.at } };
    if (!model || !map) { info.model = false; return info; }
    const keys = ["ParamEyeLOpen", "ParamCheek77", "ParamMouthOpenY", "ParamAngleZ", "ParamBodyAngleZ"];
    for (let i = 0; i < keys.length; i++) {
      const index = map[keys[i]];
      info.index[keys[i]] = index;
      info.value[keys[i]] = index >= 0 ? model.getParameterValueByIndex(index) : null;
    }
    return info;
  };

  window.__petFramePre = function (model) { try {
    const now = performance.now();
    dt = clamp((now - lastTick) / 1000, 0.001, 0.05);
    lastTick = now;
    if (stage.phase !== lastPhase) { lastPhase = stage.phase; phaseAt = now; }
    if (now - stage.at > IDLE_MS) {
      stage.vx *= 0.82;
      stage.vy *= 0.82;
      if (Math.abs(stage.vx) < 1) { stage.vx = 0; }
      if (Math.abs(stage.vy) < 1) { stage.vy = 0; }
    }

    const phase = stage.phase;
    const t = (now - phaseAt) / 1000;
    let wantRot = 0;
    let wantLift = 0;
    let wantSquash = 1;
    let pose = null;
    let direct = false;

    if (phase === "hold") {
      propTarget = 0;
      wantRot = Math.sin(now / 620) * 2.5;
      wantLift = -5;
      pose = POSE_HOLD;
    } else if (phase === "drag") {
      propTarget = 0;
      const speed = Math.abs(stage.vx);
      wantRot = clamp(-stage.vx * 0.02, -14, 14);
      wantLift = -5 - Math.min(6, speed * 0.004) + Math.sin(now / 240) * 2;
      pose = Object.assign({}, POSE_HOLD, { ParamMouthOpenY: clamp(0.25 + speed / 2600, 0.25, 0.9) });
    } else if (phase === "fall") {
      propTarget = 0;
      const u = clamp(t / FALL_S, 0, 1);
      wantRot = 11;
      wantLift = -5 + 27 * ease(Math.min(1, u * 2.5));
      wantSquash = 1 - 0.13 * Math.min(1, u * 3);
      pose = POSE_FALL;
      direct = true;
    } else if (phase === "recover") {
      propTarget = 0;
      const u = clamp(t / RECOVER_S, 0, 1);
      const e = ease(u);
      wantRot = 11 * (1 - e);
      wantLift = 22 * (1 - e) - 4 * Math.sin(Math.PI * u);
      wantSquash = 0.87 + 0.13 * e + 0.04 * Math.sin(Math.PI * u);
      pose = {};
      for (let i = 0; i < PARAM_ORDER.length; i++) {
        const key = PARAM_ORDER[i];
        pose[key] = POSE_FALL[key] + (NEUTRAL[key] - POSE_FALL[key]) * e;
      }
      direct = true;
    } else {
      propTarget = 1;
    }

    rot += (wantRot - rot) * Math.min(1, dt * 10);
    if (direct) {
      lift = wantLift;
      squash = wantSquash;
    } else {
      lift += (wantLift - lift) * Math.min(1, dt * 12);
      squash += (wantSquash - squash) * Math.min(1, dt * 14);
    }
    if (pose) {
      pose.ParamAngleZ = clamp(rot, -30, 30);
      pose.ParamBodyAngleZ = clamp(rot * 0.45, -10, 10);
      writePose(model, pose);
    }
    const mouthAge = now - mouth.at;
    if (phase === "idle" && mouth.at > 0 && mouthAge < MOUTH_HOLD_MS) {
      const map = paramMap(model);
      const openIndex = map.ParamMouthOpenY;
      if (openIndex >= 0) {
        model.setParameterValueByIndex(openIndex, clamp(mouth.open + mouth.jitter * Math.sin(now / 47), 0, 1));
      }
      const formIndex = map.ParamMouthForm;
      if (formIndex >= 0) {
        model.setParameterValueByIndex(formIndex, -0.5 + 0.6 * clamp(mouth.open, 0, 1));
      }
    }
    } catch (err) { stageError("pre", err); }
  };

  window.__petFramePost = function (model) { try {
    const fadeGoal = FADE_PROPS ? propTarget : 1;
    propK += (fadeGoal - propK) * Math.min(1, dt * FADE_RATE);
    if (propK > 0.995) { propK = 1; }
    if (propK < 1) {
      const list = fadeTargets(model);
      const ops = model._model.drawables.opacities;
      for (let i = 0; i < list.length; i++) { ops[list[i]] = ops[list[i]] * propK; }
    }

    if (stage.phase === "idle" && propK === 1) {
      if (Math.abs(rot) < 0.05 && Math.abs(lift) < 0.05 && Math.abs(squash - 1) < 0.002) {
        if (rot === 0 && lift === 0 && squash === 1) { return; }
        rot = 0;
        lift = 0;
        squash = 1;
      }
    }

    const canvas = document.querySelector("canvas");
    if (!canvas) { return; }
    if (baseTransform === null) { baseTransform = canvas.style.transform || ""; }
    canvas.style.transformOrigin = "50% 74%";
    canvas.style.transform = baseTransform + " translateY(" + lift.toFixed(2) + "px) rotate(" + rot.toFixed(2) + "deg) scaleY(" + squash.toFixed(3) + ")";
    } catch (err) { stageError("post", err); }
  };
})();
"""


class DesktopPetWindow(QWidget):
    BASE_SIZE = QSize(360, 480)

    def __init__(self, chat_window, live2d_server, parent=None):
        super().__init__(parent)
        self.setWindowIcon(get_app_icon())
        self.chat_window = chat_window
        self.live2d_server = live2d_server
        self._drag_offset = None
        self._drag_last_pos = None
        self._drag_last_time = 0.0
        self._drag_vx = 0.0
        self._drag_vy = 0.0
        self._drag_active = False
        self._pet_stage_gen = 0
        self._allow_close = False
        self._tray = None
        self._tray_pet_action = None
        self._tray_chat_action = None
        self.model_scale = self.chat_window.config.get_pet_model_scale()
        self._speech_queue = []
        self._speech_stream_buffer = ""
        self._speech_markup_filter = KaomojiStreamFilter()
        self._speech_stream_received = False
        self._speech_playing = False
        self._tts_speech_active = False
        self._kaomoji = "(｡•ᴗ•｡)"
        self._bubble_accent = "#64748B"
        self._mouth_words = []
        self._mouth_until = 0.0
        self._mouth_started = 0.0
        self._mouth_timer = QTimer(self)
        self._mouth_timer.setInterval(PET_MOUTH_TICK_MS)
        self._mouth_timer.timeout.connect(self._tick_mouth)
        self._setup_tray()

        self.setWindowTitle("桌宠")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent;")
        self.resize(self._scaled_pet_size())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.webview = PetWebView(self)
        self.webview.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.webview.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.webview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.webview, 1)

        self.speech_bubble = PetSpeechBubble()
        self.speech_bubble.set_scale(self.model_scale)
        self._bubble_was_visible = False
        self.speech_timer = QTimer(self)
        self.speech_timer.setSingleShot(True)
        self.speech_timer.timeout.connect(self._advance_speech_page)

        self.interaction_overlay = PetInteractionOverlay(self, self.webview)
        self.interaction_overlay.raise_()

        self._place_default_position()
        if self.live2d_server:
            QTimer.singleShot(400, self.load_live2d_model)
        self.webview.loadFinished.connect(lambda _ok: self._clear_canvas_scale())
        self.webview.loadFinished.connect(lambda _ok: self._install_pet_drag_stage())

    def load_live2d_model(self):
        if self.live2d_server:
            self.webview.setUrl(QUrl(self.chat_window._live2d_page_url("pet.html")))

    def _scaled_pet_size(self):
        return QSize(
            round(self.BASE_SIZE.width() * self.model_scale),
            round(self.BASE_SIZE.height() * self.model_scale),
        )

    def _speech_text_fits(self, text):
        metrics = QFontMetrics(self.speech_bubble.text_label.font())
        bounds = metrics.boundingRect(
            QRect(0, 0, self.speech_bubble._max_text_width, 10000),
            int(Qt.TextFlag.TextWordWrap),
            text,
        )
        return bounds.height() <= self.speech_bubble.MAX_LABEL_HEIGHT

    def _split_speech_pages(self, text):
        """按气泡实际像素高度分页，优先保留自然停顿处。"""
        pages = []
        remaining = re.sub(r"\s+", " ", str(text or "").strip())
        while remaining:
            if self._speech_text_fits(remaining):
                pages.append(remaining)
                break

            fitted = ""
            last_break = 0
            for index, char in enumerate(remaining):
                candidate = fitted + char
                if not self._speech_text_fits(candidate):
                    break
                fitted = candidate
                if char in "，、；：,;: ":
                    last_break = index + 1
            else:
                pages.append(remaining)
                break

            cut = last_break or len(fitted)
            if cut <= 0:
                cut = 1
            while cut < len(remaining) and remaining[cut] in "”’）】》」』":
                if not self._speech_text_fits(remaining[:cut + 1]):
                    break
                cut += 1
            page = remaining[:cut].strip()
            if page:
                pages.append(page)
            remaining = remaining[cut:].strip()
        return [page for page in pages if page]

    def get_speech_pages(self, text):
        return self._split_speech_pages(text)

    def _position_speech_bubble(self):
        if not self.speech_bubble.isVisible():
            return
        face_center = self.mapToGlobal(QPoint(round(self.width() * 0.56), round(self.height() * 0.22)))
        screen = QApplication.screenAt(face_center) or QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()

        # 以模型可见区域，而不是脸部中心，作为气泡的水平避让边界。
        model_left = self.mapToGlobal(QPoint(round(self.width() * 0.12), round(self.height() * 0.08))).x()
        model_right = self.mapToGlobal(QPoint(round(self.width() * 0.88), round(self.height() * 0.08))).x()
        model_top = self.mapToGlobal(QPoint(round(self.width() * 0.50), round(self.height() * 0.08))).y()
        margin = 16

        def bubble_size(side):
            if side == "above":
                return (
                    self.speech_bubble._text_width + 28,
                    self.speech_bubble._body_height + self.speech_bubble.TAIL_WIDTH,
                )
            return (
                self.speech_bubble._text_width + 28 + self.speech_bubble.TAIL_WIDTH,
                self.speech_bubble._body_height + 2,
            )

        candidates = []
        # 优先放在模型上方，只有上方空间不足时才尝试左右两侧。
        for candidate_side in ("above", "right", "left"):
            bubble_width, bubble_height = bubble_size(candidate_side)
            if candidate_side == "right":
                candidate_x = model_right + margin
                candidate_y = face_center.y() - bubble_height // 2
            elif candidate_side == "left":
                candidate_x = model_left - bubble_width - margin
                candidate_y = face_center.y() - bubble_height // 2
            else:
                candidate_x = face_center.x() - bubble_width // 2
                candidate_y = model_top - bubble_height - margin
            candidates.append((candidate_side, candidate_x, candidate_y, bubble_width, bubble_height))

        side, x, y, bubble_width, bubble_height = candidates[-1]
        x = max(area.left() + 8, min(x, area.right() - bubble_width - 8))
        y = max(area.top() + 8, min(y, area.bottom() - bubble_height - 8))
        for candidate_side, candidate_x, candidate_y, candidate_width, candidate_height in candidates:
            if (
                candidate_x >= area.left() + 8
                and candidate_x + candidate_width <= area.right() - 8
                and candidate_y >= area.top() + 8
                and candidate_y + candidate_height <= area.bottom() - 8
            ):
                side, x, y = candidate_side, candidate_x, candidate_y
                break
        self.speech_bubble.set_side(side)
        self.speech_bubble.move(x, y)
        self.speech_bubble.raise_()

    def _show_bubble(self, text):
        self.speech_bubble.set_message(text, self._bubble_accent)
        if not self.isVisible():
            self.speech_bubble.hide()
            return
        self.speech_bubble.show()
        self._position_speech_bubble()

    def _advance_speech_page(self):
        if self._speech_queue:
            page, page_kind = self._speech_queue.pop(0)
            self._speech_playing = True
            self._show_bubble(page)
            duration = 2600 if page_kind == "expression" else max(2400, min(5600, 1500 + len(page) * 150))
            self.speech_timer.start(duration)
            return
        self._speech_playing = False
        self._speech_stream_received = False
        self.speech_bubble.hide()

    def _enqueue_speech(self, text):
        pages = self._split_speech_pages(text)
        if not pages:
            return
        self._speech_queue.extend((page, "speech") for page in pages)
        if not self._speech_playing:
            self.speech_timer.stop()
            self._advance_speech_page()

    def _enqueue_expression(self, kaomoji):
        expression = str(kaomoji or "").strip()
        if not expression:
            return
        self._speech_queue.append((expression, "expression"))
        if not self._speech_playing:
            self.speech_timer.stop()
            self._advance_speech_page()

    def begin_speech_stream(self):
        self.speech_timer.stop()
        self._speech_queue.clear()
        self._speech_stream_buffer = ""
        self._speech_markup_filter.reset()
        self._speech_stream_received = False
        self._speech_playing = False
        self._tts_speech_active = False

    def prepare_tts_speech(self, pages, kaomoji=None):
        self.speech_timer.stop()
        self.stop_mouth_animation()
        self._speech_queue.clear()
        self._speech_stream_buffer = ""
        self._speech_markup_filter.reset()
        self._speech_stream_received = False
        self._speech_playing = False
        self._tts_speech_active = True
        if kaomoji:
            self._kaomoji = str(kaomoji).strip()
        self._show_bubble("...")

    def show_tts_page(self, index, text, words=None):
        if not self._tts_speech_active:
            return
        self.speech_timer.stop()
        self._speech_playing = True
        self._show_bubble(text)
        self.begin_mouth_animation(words)

    def finish_tts_speech(self):
        if not self._tts_speech_active:
            return
        self._tts_speech_active = False
        self.stop_mouth_animation()
        self._speech_playing = False
        self._show_bubble(self._kaomoji)
        self.speech_timer.start(2600)

    def append_speech_chunk(self, chunk):
        if not chunk:
            return
        self._speech_stream_received = True
        self._speech_stream_buffer += self._speech_markup_filter.feed(chunk)
        self._flush_completed_speech_sentences()

    def _flush_completed_speech_sentences(self, final=False):
        """按句末切分，并把紧随其后的引号、括号归入同一页。"""
        text = self._speech_stream_buffer
        sentence_endings = "。！？!?…\n"
        trailing_closers = "”’）】》」』"
        start = 0
        index = 0

        while index < len(text):
            if text[index] not in sentence_endings:
                index += 1
                continue

            end = index + 1
            while end < len(text) and text[end] in sentence_endings:
                end += 1
            while end < len(text) and text[end] in trailing_closers:
                end += 1

            # 流式分块可能刚好结束在句号后，先保留，等待下个分块的右引号。
            if end == len(text) and not final:
                break

            sentence = text[start:end].strip()
            if sentence:
                self._enqueue_speech(sentence)
            start = end
            index = end

        if start:
            self._speech_stream_buffer = text[start:]

    def finish_speech_stream(self, full_text, kaomoji=None, append_expression=True):
        if kaomoji:
            self._kaomoji = kaomoji
        if self._speech_stream_received:
            self._speech_stream_buffer += self._speech_markup_filter.feed("", final=True)
            self._flush_completed_speech_sentences(final=True)
            self._enqueue_speech(self._speech_stream_buffer)
        else:
            self._enqueue_speech(full_text)
        self._speech_stream_buffer = ""
        if append_expression:
            self._enqueue_expression(self._kaomoji)

    def begin_mouth_animation(self, words=None):
        """语音播放期间按词边界驱动口型；拿不到词边界就走固定节律。"""
        self.stop_mouth_animation()
        if not PET_DRAG_STAGE_ENABLED or self.webview is None:
            return
        marks = []
        for item in (words or []):
            try:
                start = float(item.get("start", 0.0))
                duration = float(item.get("duration", 0.0))
            except Exception:
                continue
            if duration > 0:
                chars = min(3, max(1, len(str(item.get("text", "")))))
                marks.append((start, duration, chars))
        marks.sort(key=lambda row: row[0])
        self._mouth_words = marks
        self._mouth_until = (marks[-1][0] + marks[-1][1]) if marks else 0.0
        self._mouth_started = time.monotonic()
        self.webview.page().runJavaScript(PET_DRAG_STAGE_SCRIPT)
        self._mouth_timer.start()

    def stop_mouth_animation(self):
        self._mouth_timer.stop()
        self._mouth_words = []
        self._mouth_until = 0.0
        self._mouth_started = 0.0
        if PET_DRAG_STAGE_ENABLED and self.webview is not None:
            self.webview.page().runJavaScript("window.__petMouthSet && window.__petMouthSet(0, 0);")

    def _tick_mouth(self):
        if not self._tts_speech_active or self.webview is None:
            self.stop_mouth_animation()
            return
        elapsed = (time.monotonic() - self._mouth_started) * 1000.0
        if self._mouth_words and elapsed > self._mouth_until + PET_MOUTH_HOLD_MS:
            self.stop_mouth_animation()
            return
        opening, jitter = self._mouth_curve(elapsed)
        script = "window.__petMouthSet && window.__petMouthSet(%s, %s);" % (
            round(opening, 3), round(jitter, 3))
        self.webview.page().runJavaScript(script)

    def _mouth_curve(self, elapsed_ms):
        """返回 (张合幅度, 抖动幅度)；有词边界按词走，没有就按固定节律开合。"""
        marks = self._mouth_words
        if not marks:
            return 0.16 + 0.42 * abs(math.sin(elapsed_ms / 165.0)), 0.05
        for start, duration, chars in marks:
            if elapsed_ms < start or elapsed_ms > start + duration:
                continue
            u = (elapsed_ms - start) / duration
            return 0.2 + 0.5 * abs(math.sin(math.pi * chars * u)), 0.05
        return 0.08, 0.03

    def cancel_speech(self):
        self.speech_timer.stop()
        self._speech_queue.clear()
        self._speech_stream_buffer = ""
        self._speech_markup_filter.reset()
        self._speech_stream_received = False
        self._speech_playing = False
        self._tts_speech_active = False
        self.stop_mouth_animation()
        self.speech_bubble.hide()

    def _clear_canvas_scale(self):
        script = f"""
        (() => {{
            const clearScale = () => {{
                const canvas = document.querySelector('canvas');
                if (!canvas) {{
                    window.setTimeout(clearScale, 100);
                    return;
                }}
                window.__petModelScale = 1;
                canvas.style.transformOrigin = '';
                canvas.style.transform = '';
            }};
            clearScale();
        }})();
        """
        self.webview.page().runJavaScript(script)

    def set_model_scale(self, scale, persist=True):
        self.model_scale = max(0.5, min(1.4, float(scale)))
        if persist:
            self.chat_window.config.set_pet_model_scale(self.model_scale)
        old_center_x = self.frameGeometry().center().x()
        old_bottom = self.frameGeometry().bottom()
        self.resize(self._scaled_pet_size())
        screen = QApplication.screenAt(self.pos()) or QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            x = max(area.left(), min(old_center_x - self.width() // 2, area.right() - self.width() + 1))
            y = max(area.top(), min(old_bottom - self.height() + 1, area.bottom() - self.height() + 1))
            self.move(x, y)
        self._clear_canvas_scale()
        self.speech_bubble.set_scale(self.model_scale)
        self._position_speech_bubble()

    def _place_default_position(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        x = geo.right() - self.width() - 32
        y = geo.bottom() - self.height() - 48
        self.move(max(0, x), max(0, y))

    def begin_drag(self, global_pos):
        self._drag_offset = global_pos - self.frameGeometry().topLeft()
        self._drag_last_pos = None
        self._drag_last_time = 0.0
        self._drag_vx = 0.0
        self._drag_vy = 0.0

    def update_drag(self, global_pos):
        if self._drag_offset is None:
            return
        now = time.monotonic()
        if self._drag_last_pos is not None:
            dt = now - self._drag_last_time
            if dt > 0.001:
                inst_vx = (global_pos.x() - self._drag_last_pos.x()) / dt
                inst_vy = (global_pos.y() - self._drag_last_pos.y()) / dt
                self._drag_vx = self._drag_vx * 0.6 + inst_vx * 0.4
                self._drag_vy = self._drag_vy * 0.6 + inst_vy * 0.4
        self._drag_last_pos = global_pos
        self._drag_last_time = now
        if self._drag_active:
            self._push_pet_drag_stage("drag", self._drag_vx, self._drag_vy)
        self.move(global_pos - self._drag_offset)

    def end_drag(self):
        self._drag_offset = None
        self._drag_last_pos = None
        if self._drag_active:
            self._drag_active = False
            self._play_pet_drop_sequence()

    def start_pet_drag_stage(self):
        """右键真正拖动起来后调用：先确保演出脚本在位，再切到被拎起来的状态。"""
        self._install_pet_drag_stage()
        self._pet_stage_gen += 1
        self._drag_active = True
        self._drag_vx = 0.0
        self._drag_vy = 0.0
        self._push_pet_drag_stage("hold", 0.0, 0.0)
        QTimer.singleShot(500, self._probe_pet_drag_stage)

    def _install_pet_drag_stage(self):
        self._drag_active = False
        self._pet_stage_gen += 1
        if not PET_DRAG_STAGE_ENABLED or self.webview is None:
            return
        self.webview.page().runJavaScript(PET_DRAG_STAGE_SCRIPT)

    def _push_pet_drag_stage(self, phase, vx=0.0, vy=0.0):
        if not PET_DRAG_STAGE_ENABLED or self.webview is None:
            return
        script = "window.__petStageSet && window.__petStageSet(%s, %s, %s);" % (
            json.dumps(phase), round(float(vx), 1), round(float(vy), 1))
        self.webview.page().runJavaScript(script)

    def _probe_pet_drag_stage(self):
        if not PET_DRAG_STAGE_ENABLED or self.webview is None:
            return
        script = "JSON.stringify(window.__petStageProbe ? window.__petStageProbe() : {missing: true})"
        self.webview.page().runJavaScript(script, self._log_pet_drag_probe)

    def _log_pet_drag_probe(self, result):
        logging.info("[pet-stage] probe: %s", result)

    def _play_pet_drop_sequence(self):
        """松手：扑通摔一下 -> 爬起来 -> 道具淡回来。"""
        self._pet_stage_gen += 1
        generation = self._pet_stage_gen
        self._push_pet_drag_stage("fall", 0.0, 0.0)
        QTimer.singleShot(PET_DRAG_FALL_MS, lambda: self._advance_pet_stage(generation, "recover"))
        QTimer.singleShot(PET_DRAG_FALL_MS + PET_DRAG_RECOVER_MS, lambda: self._advance_pet_stage(generation, "restore"))
        QTimer.singleShot(
            PET_DRAG_FALL_MS + PET_DRAG_RECOVER_MS + PET_DRAG_RESTORE_MS,
            lambda: self._advance_pet_stage(generation, "idle"),
        )

    def _advance_pet_stage(self, generation, phase):
        if generation != self._pet_stage_gen:
            return
        self._push_pet_drag_stage(phase, 0.0, 0.0)

    def show_context_menu(self, global_pos):
        menu = QMenu(self)
        if self.chat_window.isVisible():
            toggle_action = menu.addAction("隐藏聊天窗口")
        else:
            toggle_action = menu.addAction("打开聊天窗口")
        hide_pet_action = menu.addAction("隐藏桌宠")
        quit_action = menu.addAction("退出程序")

        action = menu.exec(global_pos)
        if action == toggle_action:
            if self.chat_window.isVisible():
                self.chat_window.hide_chat_window()
            else:
                self.chat_window.show_chat_window()
        elif action == hide_pet_action:
            self.hide()
        elif action == quit_action:
            self.quit_application()

    def quit_application(self):
        logging.info("退出程序")
        self._allow_close = True
        if self._tray is not None:
            self._tray.hide()
        self.chat_window.request_close()
        self.close()
        QApplication.instance().quit()

    def _make_tray_icon(self):
        """优先使用应用图标；资源缺失时保留内置回退图标。"""
        app_icon = get_app_icon()
        if not app_icon.isNull():
            return app_icon

        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(4, 4, 60, 60)
        gradient.setColorAt(0.0, QColor("#FFB6C1"))
        gradient.setColorAt(1.0, QColor("#FF8FAB"))
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 60, 60)
        painter.setPen(QColor("white"))
        painter.setFont(QFont("Microsoft YaHei", 26, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "柚")
        painter.end()
        return QIcon(pixmap)

    def _build_tray_menu(self):
        menu = QMenu(self)
        self._tray_pet_action = menu.addAction("显示桌宠")
        self._tray_chat_action = menu.addAction("打开聊天窗口")
        menu.addSeparator()
        quit_action = menu.addAction("退出程序")
        self._tray_pet_action.triggered.connect(self._toggle_pet_visibility)
        self._tray_chat_action.triggered.connect(self._toggle_chat_window)
        quit_action.triggered.connect(self.quit_application)
        menu.aboutToShow.connect(self._update_tray_menu_labels)
        return menu

    def _update_tray_menu_labels(self):
        if self._tray_pet_action is None or self._tray_chat_action is None:
            return
        self._tray_pet_action.setText("隐藏桌宠" if self.isVisible() else "显示桌宠")
        self._tray_chat_action.setText("隐藏聊天窗口" if self.chat_window.isVisible() else "打开聊天窗口")

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self._make_tray_icon(), self)
        self._tray.setToolTip("小柚桌面助手")
        self._tray.setContextMenu(self._build_tray_menu())
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.messageClicked.connect(self._on_tray_message_clicked)
        self._tray.show()

    def notify_permission_request(self, message):
        if self._tray is not None:
            self._tray.showMessage(
                "AI 等待操作授权",
                str(message),
                QSystemTrayIcon.MessageIcon.Warning,
                10000,
            )

    def _on_tray_message_clicked(self):
        self.chat_window.show_chat_window()
        self.chat_window.raise_()
        self.chat_window.activateWindow()
        self.chat_window.raise_permission_dialog()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle_pet_visibility()

    def _toggle_pet_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _toggle_chat_window(self):
        if self.chat_window.isVisible():
            self.chat_window.hide_chat_window()
        else:
            self.chat_window.show_chat_window()

    def set_kaomoji(self, text, style=None):
        self._kaomoji = str(text or "(｡•ᴗ•｡)")
        if style:
            color_match = re.search(r"color:\s*([^;]+)", style)
            if color_match:
                self._bubble_accent = color_match.group(1).strip()
        if (
            not self._tts_speech_active
            and not self._speech_playing
            and not self._speech_queue
            and not self._speech_stream_received
        ):
            self.speech_timer.stop()
            self._show_bubble(self._kaomoji)
            self.speech_timer.start(2800)

    def closeEvent(self, event):
        if not self._allow_close:
            self.quit_application()
            event.ignore()
            return
        self.speech_bubble.close()
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.interaction_overlay.setGeometry(self.webview.geometry())
        self._position_speech_bubble()
        self.interaction_overlay.raise_()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._position_speech_bubble()

    def hideEvent(self, event):
        self._bubble_was_visible = self.speech_bubble.isVisible()
        self.speech_bubble.hide()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        should_restore = (
            self._bubble_was_visible
            and (
                self._tts_speech_active
                or self._speech_playing
                or bool(self._speech_queue)
                or self.speech_timer.isActive()
            )
        )
        self._bubble_was_visible = False
        if should_restore:
            self._show_bubble(self.speech_bubble._message)


# ======================== 主窗口 ========================
class ChatInputTextEdit(QTextEdit):
    send_requested = pyqtSignal()
    image_pasted = pyqtSignal(object)

    def insertFromMimeData(self, source):
        """剪贴板里只有图片时（如 Win+Shift+S 截图），转交附件流程而不是插进正文。"""
        if self._try_emit_pasted_image(source):
            return
        super().insertFromMimeData(source)

    def _try_emit_pasted_image(self, source):
        if not source.hasImage() or source.hasUrls():
            return False
        # 同时带文本时（例如从 Excel 复制单元格）优先按文本粘贴，别抢走正常粘贴
        if source.hasText() and source.text().strip():
            return False
        image = source.imageData()
        if isinstance(image, QImage):
            if image.isNull():
                return False
            self.image_pasted.emit(image)
            return True
        if isinstance(image, QPixmap):
            if image.isNull():
                return False
            self.image_pasted.emit(image.toImage())
            return True
        return False

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.send_requested.emit()
            return
        super().keyPressEvent(event)


SCREENSHOT_FALLBACK_MAX_SIDE = 1600
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024


class ScreenCaptureOverlay(QWidget):
    """全屏框选截图浮层：拖拽选择区域，松手确认，Esc 取消。"""

    finished = pyqtSignal(object)   # 裁剪后的 QPixmap；取消时为 None

    def __init__(self, screenshot, virtual_rect, device_ratio=1.0, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._screenshot = screenshot
        self._virtual_rect = QRect(virtual_rect)
        self._device_ratio = float(device_ratio or 1.0)
        self._origin = QPoint()
        self._current = QPoint()
        self._dragging = False
        self._closed = False

    @staticmethod
    def grab_desktop():
        """抓取整个虚拟桌面，返回 (QPixmap, 逻辑坐标矩形, 物理/逻辑缩放比)。"""
        screens = QApplication.screens()
        if not screens:
            return None, QRect(), 1.0
        virtual_rect = QRect()
        for screen in screens:
            virtual_rect = virtual_rect.united(screen.geometry())
        if virtual_rect.isEmpty():
            return None, QRect(), 1.0

        ratio = max(float(screen.devicePixelRatio() or 1.0) for screen in screens)
        canvas = QPixmap(
            max(1, round(virtual_rect.width() * ratio)),
            max(1, round(virtual_rect.height() * ratio)),
        )
        canvas.fill(QColor(0, 0, 0))
        painter = QPainter(canvas)
        try:
            for screen in screens:
                shot = screen.grabWindow(0)
                if shot.isNull():
                    continue
                shot.setDevicePixelRatio(1.0)
                geometry = screen.geometry()
                painter.drawPixmap(
                    round((geometry.x() - virtual_rect.x()) * ratio),
                    round((geometry.y() - virtual_rect.y()) * ratio),
                    shot,
                )
        finally:
            painter.end()
        # 统一按物理像素处理：裁剪区间与实际保存的像素一一对应
        canvas.setDevicePixelRatio(1.0)
        return canvas, virtual_rect, ratio

    def begin(self):
        self.setGeometry(self._virtual_rect)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _selection_rect(self):
        if not self._dragging:
            return QRect()
        return QRect(self._origin, self._current).normalized()

    def _to_device_rect(self, rect):
        ratio = self._device_ratio
        return QRect(
            round(rect.x() * ratio),
            round(rect.y() * ratio),
            max(1, round(rect.width() * ratio)),
            max(1, round(rect.height() * ratio)),
        ).intersected(self._screenshot.rect())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(self.rect(), self._screenshot)

        selection = self._selection_rect()
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
        if not selection.isNull():
            source = self._to_device_rect(selection)
            if not source.isEmpty():
                painter.drawPixmap(selection, self._screenshot, source)
            painter.setPen(QPen(QColor(86, 168, 232), 2))
            painter.drawRect(selection.adjusted(0, 0, -1, -1))

        font = QFont()
        font.setPointSize(12)
        painter.setFont(font)
        painter.setPen(QPen(QColor(255, 255, 255, 230)))
        if selection.isNull():
            hint = "拖动鼠标框选要发送的区域，Esc 取消"
        else:
            hint = "%d × %d  松手即添加，Esc 取消" % (
                round(selection.width() * self._device_ratio),
                round(selection.height() * self._device_ratio),
            )
        painter.drawText(24, 42, hint)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._origin = event.position().toPoint()
        self._current = self._origin
        self._dragging = True
        self.update()

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._current = event.position().toPoint()
        # 先取选区再清 _dragging：_selection_rect() 只在拖拽中才返回实际矩形
        selection = self._selection_rect()
        self._dragging = False
        if selection.width() < 5 or selection.height() < 5:
            self._finish(None)
            return
        source = self._to_device_rect(selection)
        if source.isEmpty():
            self._finish(None)
            return
        cropped = self._screenshot.copy(source)
        cropped.setDevicePixelRatio(1.0)
        self._finish(cropped)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if not self._closed:
            self._closed = True
            self.finished.emit(None)
        super().closeEvent(event)

    def _finish(self, pixmap):
        if self._closed:
            return
        self._closed = True
        self.hide()
        self.finished.emit(pixmap)
        self.close()


class ComputerControlOverlay(QWidget):
    """独立置顶的视觉键鼠状态条，不依附聊天窗口，也不拦截鼠标。"""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(480, 96)
        panel = QWidget(self)
        panel.setObjectName("computerControlPanel")
        panel.setGeometry(self.rect())
        panel.setStyleSheet(
            "#computerControlPanel { background-color: #1c222a; border: 1px solid #6a91b2; "
            "border-radius: 10px; }"
            "QLabel { color: #f3f6f8; font-size: 13px; }"
            "QLabel#computerControlHint { color: #b7c8d5; font-size: 11px; }"
            "QProgressBar { border: 1px solid #52697a; border-radius: 4px; "
            "background: #20262c; height: 8px; text-align: center; }"
            "QProgressBar::chunk { background: #56a8e8; border-radius: 3px; }"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(6)
        self.status_label = QLabel("AI 正在接管屏幕")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.hint_label = QLabel("持续按住 Q 3 秒可停止接管")
        self.hint_label.setObjectName("computerControlHint")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.status_label)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.progress_bar)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._q_progress_timer = QTimer(self)
        self._q_progress_timer.setInterval(50)
        self._q_progress_timer.timeout.connect(self._update_q_progress)

    def _place_on_screen(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 24)

    def show_status(self, text):
        self._hide_timer.stop()
        self.status_label.setText(str(text))
        self.hint_label.setText("持续按住 Q 3 秒可停止接管")
        self.progress_bar.setRange(0, 0)
        self._q_progress_timer.start()
        self._place_on_screen()
        self.show()
        self.raise_()

    def show_finished(self, text="视觉任务已结束"):
        self._hide_timer.stop()
        self._q_progress_timer.stop()
        self.status_label.setText(str(text))
        self.hint_label.setText("键鼠控制权已恢复")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self._place_on_screen()
        self.show()
        self.raise_()
        self._hide_timer.start(1600)

    def _update_q_progress(self):
        progress = max(0.0, min(1.0, VisionInputController.get_q_hold_progress()))
        if progress <= 0:
            self.progress_bar.setRange(0, 0)
            self.hint_label.setText("持续按住 Q 3 秒可停止接管")
            return
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(round(progress * 100))
        self.hint_label.setText(f"持续按住 Q 3 秒可停止接管（{round(progress * 100)}%）")


class MainWindow(QMainWindow):
    def __init__(self, show_live2d_panel=True, managed_by_pet=False):
        super().__init__()
        self.setWindowIcon(get_app_icon())
        self.show_live2d_panel = show_live2d_panel
        self.managed_by_pet = managed_by_pet
        self.allow_close = not managed_by_pet
        self.pet_window = None
        self.config = ConfigManager()
        self.db = MessageDatabase()
        ActionHandler.load_apps()
        self.refresh_action_handler_config()
        print(f"已加载应用: {ActionHandler._apps}")

        self.roles = self.config.get_roles()
        saved_role_name = str(self.config.settings.value("last_role", "") or "")
        self.current_role_name = saved_role_name if saved_role_name in self.roles else next(iter(self.roles), "")
        self.runtime_system_prompt = self._get_effective_system_prompt()
        self.session_is_persisted = False
        self.current_session_id = self._get_latest_session_or_new()
        self.conversation_history = []
        self.session_task_states = {}
        self.current_task_state = None
        self.current_api_thread = None
        self._permission_dialog = None
        self._pending_permission = None
        self.computer_overlay = ComputerControlOverlay()
        self.live2d_server = None
        self.stream_buffer = ""
        self.stream_display_buffer = ""
        self.chat_stream_filter = KaomojiStreamFilter()

        self.bubble_opacity = self.config.get_bubble_opacity()
        self.chat_background_mode = self.config.get_chat_background_mode()
        self.tts_thread = None
        self._tts_threads = []
        self.stt_thread = None
        self.wakeword_thread = None
        self._pending_attachments = []
        self._pending_image_attachments = []
        self._screenshot_overlay = None
        self._hidden_floating = []



        self.init_ui()
        self.setWindowTitle("AI桌面助手 - 加载一言中...")
        # 每日一言网络获取
        self.nam = QNetworkAccessManager(self)
        self.nam.finished.connect(self.on_motto_reply)
        self.fetch_motto()  # 立即获取一次
        # 每隔 1 小时刷新一次（3600000 毫秒）
        self.motto_timer = QTimer(self)
        self.motto_timer.timeout.connect(self.fetch_motto)
        self.motto_timer.start(3600000)
        self.init_menu()
        self.init_live2d_server()
        saved_bg = self.config.settings.value("chat_bg_image", "")
        self._apply_chat_background(saved_bg if saved_bg and os.path.exists(saved_bg) else None)
        self.load_system_prompt()
        self.load_history_from_db()
        # 启动唤醒词常驻监听（麦克风）
        QTimer.singleShot(0, self._start_wakeword_listener)

    def bind_pet_window(self, pet_window):
        self.pet_window = pet_window
        self._sync_kaomoji_to_pet()

    def _sync_kaomoji_to_pet(self):
        if self.pet_window:
            self.pet_window.set_kaomoji(self.kaomoji_label.text(), self.kaomoji_label.styleSheet())

    def _set_kaomoji_display(self, text=None, style=None):
        if text is not None:
            self.kaomoji_label.setText(text)
        if style is not None:
            self.kaomoji_label.setStyleSheet(style)
        self._sync_kaomoji_to_pet()


    def fetch_motto(self):
        """异步请求每日一言"""
        # 使用 hitokoto.cn API，返回 JSON 格式
        url = QUrl("https://v1.hitokoto.cn/?c=a&c=b&c=c&c=d&c=e&c=f&c=g&c=h&c=i&c=j&c=k&encode=json&charset=utf-8")
        # 或者使用更简单的：https://api.btstu.cn/yan/api.php?format=json
        request = QNetworkRequest(url)
        self.nam.get(request)

    def on_motto_reply(self, reply: QNetworkReply):
        """处理网络返回的每日一言"""
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = reply.readAll().data()
            try:
                # 解析 JSON
                text = json.loads(data.decode('utf-8')).get('hitokoto', '')
                if text:
                    self.setWindowTitle(f"AI桌面助手 - {text}")
                    # 可选：保存到 QSettings，以便下次启动时显示，但实时更新即可
                    reply.deleteLater()
                    return
            except Exception as e:
                print(f"解析一言失败: {e}")
        # 网络错误或解析失败，使用本地备用句子
        self.use_fallback_motto()
        reply.deleteLater()

    def use_fallback_motto(self):
        """本地备用句子（网络不可用时）"""
        fallbacks = [
            "每一天都是新的开始 ✨",
            "今天也要加油鸭！ 🦆",
            "保持微笑，好运常在 😊",
            "别急，好事正在路上 🚗"
        ]
        import random
        self.setWindowTitle(f"AI桌面助手 - {random.choice(fallbacks)}")

    def update_all_bubbles_opacity(self, opacity):
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            widget = self.chat_list.itemWidget(item)
            if widget and isinstance(widget, ChatMessageWidget):
                widget.update_opacity(opacity)
                widget.refresh_layout()

    def refresh_all_message_widgets(self):
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            widget = self.chat_list.itemWidget(item)
            if widget and isinstance(widget, ChatMessageWidget):
                widget.refresh_layout()

    def _get_latest_session_or_new(self):
        sessions = self.db.get_session_info()
        if sessions:
            self.session_is_persisted = True
            return sessions[0][0]
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.session_is_persisted = False
        return session_id

    @staticmethod
    def _session_group(timestamp):
        try:
            value = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            today = datetime.now().date()
            if value.date() == today:
                return "今天"
            if value.date() == today.fromordinal(today.toordinal() - 1):
                return "昨天"
        except (TypeError, ValueError, OverflowError):
            pass
        return "更早"

    def refresh_session_list(self):
        if not hasattr(self, "session_list"):
            return
        query = str(self.session_search.text() or "").strip().lower()
        metadata = self.db.get_session_metadata()
        self.session_list.blockSignals(True)
        self.session_list.clear()
        current_item = None
        current_group = None
        for item in metadata:
            title = (item.get("title") or item.get("preview") or "未命名会话").strip()
            preview = (item.get("preview") or "").replace("\n", " ").strip()
            haystack = f"{title} {preview}".lower()
            if query and query not in haystack:
                continue
            group = self._session_group(item.get("updated_at"))
            if group != current_group:
                header = QListWidgetItem(group)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setForeground(QColor("#888888"))
                self.session_list.addItem(header)
                current_group = group
            display_title = ("★ " if item.get("pinned") else "") + title[:32]
            display = display_title
            if preview and preview != title:
                display += f"\n{preview[:46]}"
            list_item = QListWidgetItem(display)
            list_item.setData(Qt.ItemDataRole.UserRole, item["session_id"])
            list_item.setToolTip(preview or title)
            self.session_list.addItem(list_item)
            if item["session_id"] == self.current_session_id:
                current_item = list_item
        self.session_list.blockSignals(False)
        if current_item:
            self.session_list.setCurrentItem(current_item)

    def _on_session_item_activated(self, item):
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if session_id:
            self.switch_session(str(session_id))

    def _session_id_at(self, position):
        item = self.session_list.itemAt(position)
        if not item:
            return None, item
        session_id = item.data(Qt.ItemDataRole.UserRole)
        return (str(session_id) if session_id else None), item

    def _show_session_context_menu(self, position):
        session_id, item = self._session_id_at(position)
        if not session_id:
            return
        metadata = next((row for row in self.db.get_session_metadata() if row["session_id"] == session_id), None)
        if not metadata:
            return
        menu = QMenu(self)
        open_action = menu.addAction("打开会话")
        rename_action = menu.addAction("重命名")
        pin_action = menu.addAction("取消置顶" if metadata.get("pinned") else "置顶会话")
        menu.addSeparator()
        delete_action = menu.addAction("删除会话")
        action = menu.exec(self.session_list.mapToGlobal(position))
        if action == open_action:
            self.switch_session(session_id)
        elif action == rename_action:
            self._rename_session(session_id, metadata.get("title") or metadata.get("preview") or "")
        elif action == pin_action:
            self.db.set_session_pinned(session_id, not metadata.get("pinned"))
            self.refresh_session_list()
        elif action == delete_action:
            self._delete_session_from_panel(session_id)

    def _rename_session(self, session_id, old_title):
        title, accepted = QInputDialog.getText(self, "重命名会话", "会话名称:", text=old_title[:80])
        if accepted and title.strip():
            self.db.rename_session(session_id, title.strip())
            self.refresh_session_list()

    def _delete_session_from_panel(self, session_id):
        reply = QMessageBox.question(
            self,
            "确认删除",
            "确定删除这个会话及其历史消息吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if session_id == self.current_session_id:
            self._discard_current_generation()
            self.db.delete_session(session_id)
            self._set_session_task_state(None, session_id=session_id)
            self._show_temporary_session("当前会话已删除，等待新消息")
            return

        self.db.delete_session(session_id)
        self._set_session_task_state(None, session_id=session_id)
        self.refresh_session_list()

    def _on_session_splitter_moved(self, position, index):
        if not self.session_panel.isVisible() or not hasattr(self, "session_splitter"):
            return
        sizes = self.session_splitter.sizes()
        if not sizes or sizes[0] <= 0:
            return
        self.session_panel_width = max(220, min(380, sizes[0]))
        self.config.settings.setValue("session_panel_width", self.session_panel_width)

    def toggle_session_panel(self, visible):
        visible = bool(visible)
        if visible:
            self.session_panel.show()
            self.session_splitter.setSizes([self.session_panel_width, max(1, self.session_splitter.width() - self.session_panel_width)])
        else:
            sizes = self.session_splitter.sizes()
            if sizes and sizes[0] > 0:
                self.session_panel_width = max(220, min(380, sizes[0]))
                self.config.settings.setValue("session_panel_width", self.session_panel_width)
            self.session_panel.hide()
    
    def _generate_dynamic_system_prompt(self):
        """生成以工具调用为核心的系统提示词。"""
        apps_list = ActionHandler.get_available_apps()
        apps_str = "、".join(apps_list) if apps_list else "无"

        # 角色基本设定
        role_description = self.roles.get(self.current_role_name, "你是一个AI助手，性格友好，乐于助人。") + "\n\n" + (
            "所有回复都要像日常聊天一样自然，不分点、不罗列，想到哪说到哪。不要暴露自己是模型。"
            "禁止使用任何非中文日常符号，比如星号*、下划线_之类的都不要出现。"
            "你可以称呼用户为“大白鹅”"
            "称呼不要过于频繁，保持自然即可。"
)
        prompt = role_description + """
        【颜文字输出】
        所有的颜文字，应当使用以下格式输出：<kaomoji>标签</kaomoji> 
        例如：“今天的阳光真好呀，心情都变好了～ <kaomoji>(｡•̀ᴗ•́｡)</kaomoji>”
        """
        prompt += f"""

        【工具使用规则】
        - 你可以通过系统提供的工具执行真实操作或联网查询，不要伪造已经执行过的结果。
        - apps.json 中的应用是快捷访问列表，优先使用其中的快捷方式：{apps_str}。
        - 如果目标应用不在快捷访问列表中，可以在用户确认后使用 run_command/PowerShell 启动，或使用视觉键鼠通过开始菜单、桌面或任务栏打开；不要因为 apps.json 没有该应用就直接说无法打开。
        - 用户询问今天几号、星期几、现在几点、当前日期或当前时间时，必须先使用时间工具，不要凭记忆回答。
        - 用户询问实时信息、新闻、价格、最新动态、网页资料时，优先使用联网搜索工具。
        - 涉及体育比赛、世界杯、联赛、比分、赛况、积分榜时，搜索词必须忠实于用户原意，不要擅自改错年份；赛事类型不明确时优先先问清楚。
        - 只有在用户明确给出完整 http:// 或 https:// 链接时，才使用打开网址工具。
        - 如果用户需求缺少关键参数，就先追问，不要猜测。
        - 关机和重启属于高风险操作，只有当用户明确要求时才调用。
        - 不要输出任何工具调用协议、标签或 JSON；需要操作时直接调用工具即可。
        - 用户问电脑卡不卡、要不要清理、电脑状态是否健康时，先用 health_check 工具做体检，再用通俗语言解读并给出具体建议。
        - 用户想清理磁盘、找大文件、看临时文件占用时，用 disk_scan 工具做只读扫描，列举结果并给出建议；不要删除任何文件，也不要建议执行删除命令。
        - 工具执行完成后，再基于工具返回结果自然回复用户。
        - 最终答复必须是纯中文自然文本，禁止输出 Markdown 格式符号，例如 **、__、`、#、-、数字列表。
        """
        return prompt

    def _format_assistant_text(self, text):
        text = str(text or "")
        text = strip_internal_reasoning(text)
        return text if self.config.get_roleplay_mode() else sanitize_reply_text(text)

    def _get_default_system_prompt(self):
        return self._generate_dynamic_system_prompt() + "\n\n" + SYSTEM_SAFETY_RULES

    def _get_effective_system_prompt(self):
        mode = self.config.get_system_prompt_mode()
        if mode == "none":
            return ""
        if mode == "custom":
            return self.config.get_custom_system_prompt()
        return self._get_default_system_prompt()

    def refresh_action_handler_config(self):
        ActionHandler.configure(
            searxng_url=self.config.get_searxng_url(),
            tavily_api_key=self.config.get_tavily_api_key(),
            amap_api_key=self.config.get_amap_api_key(),
            base_url=self.config.get_base_url(),
            api_key=self.config.get_api_key(),
            vision_model=self.config.get_vision_model(),
        )

    def init_ui(self):
        self.setWindowTitle("AI桌面助手 - 支持Live2D模型")
        self.setMinimumSize(1000, 700)

        central = QWidget()
        central.setObjectName("mainCentral")
        central.setStyleSheet("#mainCentral { background-color: #1f1f1f; }")
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(0)

        self.session_panel = QWidget()
        self.session_panel.setObjectName("sessionPanel")
        self.session_panel.setMinimumWidth(220)
        self.session_panel.setMaximumWidth(380)
        self.session_panel.setStyleSheet(
            "#sessionPanel { background-color: #252525; border-right: 1px solid #444444; }"
        )
        session_layout = QVBoxLayout(self.session_panel)
        session_layout.setContentsMargins(12, 12, 12, 12)
        session_layout.setSpacing(10)

        brand_layout = QHBoxLayout()
        brand_layout.setSpacing(9)
        brand_icon = QLabel()
        app_icon = get_app_icon().pixmap(QSize(28, 28))
        if not app_icon.isNull():
            brand_icon.setPixmap(app_icon)
        brand_icon.setFixedSize(28, 28)
        brand_layout.addWidget(brand_icon)
        brand_title = QLabel("Aissist")
        brand_title.setStyleSheet("color: #eeeeee; font-size: 19px; font-weight: 700;")
        brand_layout.addWidget(brand_title)
        brand_layout.addStretch(1)
        session_layout.addLayout(brand_layout)

        session_header = QHBoxLayout()
        session_label = QLabel("会话")
        session_label.setStyleSheet("color: #d7d7d7; font-size: 14px; font-weight: 600;")
        session_header.addWidget(session_label)
        session_header.addStretch(1)
        session_layout.addLayout(session_header)
        self.session_search = QLineEdit()
        self.session_search.setPlaceholderText("搜索会话")
        self.session_search.setStyleSheet(
            "QLineEdit { background: #303030; color: #eeeeee; border: 1px solid #4b4b4b; "
            "border-radius: 4px; padding: 4px 7px; }"
            "QLineEdit:focus { border: 1px solid #6688aa; }"
        )
        self.session_search.textChanged.connect(self.refresh_session_list)
        session_layout.addWidget(self.session_search)
        self.session_list = QListWidget()
        self.session_list.setSpacing(2)
        self.session_list.setStyleSheet(
            "QListWidget { background: transparent; color: #eeeeee; border: none; padding: 2px 0; }"
            "QListWidget::item { padding: 8px 9px; border-radius: 5px; }"
            "QListWidget::item:hover { background: #383838; }"
            "QListWidget::item:selected { background: #34495e; color: #ffffff; }"
        )
        self.session_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._show_session_context_menu)
        self.session_list.itemClicked.connect(self._on_session_item_activated)
        session_layout.addWidget(self.session_list, 1)

        session_separator = QWidget()
        session_separator.setFixedHeight(1)
        session_separator.setStyleSheet("background-color: #4b4b4b;")
        session_layout.addWidget(session_separator)
        self.new_session_bottom_button = QPushButton("＋  新对话")
        self.new_session_bottom_button.setMinimumHeight(40)
        self.new_session_bottom_button.setStyleSheet(
            "QPushButton { background: #29465f; color: #52a9ff; border: none; border-radius: 20px; "
            "font-size: 15px; font-weight: 600; }"
            "QPushButton:hover { background: #345a79; }"
            "QPushButton:pressed { background: #223d53; }"
        )
        self.new_session_bottom_button.clicked.connect(self.new_session)
        session_layout.addWidget(self.new_session_bottom_button)

        # 会话栏右侧的内容区，背景只绘制在这里，不覆盖会话栏
        self.content_panel = QWidget()
        self.content_panel.setObjectName("contentPanel")
        self.content_panel.setStyleSheet(
            "#contentPanel { border: 1px solid #46515f; border-radius: 16px; background: transparent; }"
        )
        content_layout = QHBoxLayout(self.content_panel)
        content_layout.setContentsMargins(14, 14, 14, 14)
        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        content_splitter.setStyleSheet(
            "QSplitter { background: transparent; } QSplitter::handle { background: transparent; }"
        )

        # 左侧聊天区域：自定义背景由面板绘制，消息列表透明叠加在其上。
        self.chat_background_panel = ResizableBackgroundPanel()
        left = self.chat_background_panel
        left.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.chat_list = QListWidget()
        self.chat_list.setUniformItemSizes(False)
        self.chat_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.chat_list.setSpacing(5)
        self.chat_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.chat_list.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.chat_list.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.chat_list.viewport().setAutoFillBackground(False)
        self.chat_list.setAutoFillBackground(False)
        self.chat_list.setStyleSheet("""
            QListWidget { border: none; background: transparent; }
            QListWidget::item { border: none; padding: 0px; background: transparent; }
        """)
        left_layout.addWidget(self.chat_list)

        # 输入区：消息输入、附件菜单、模式、推理强度、模型状态和发送/停止按钮。
        self.composer = QFrame()
        self.composer.setObjectName("composer")
        self.composer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.composer.setMinimumHeight(116)
        self.composer.setMaximumHeight(210)
        self.composer.setStyleSheet(
            "#composer { background: rgba(45, 45, 45, 235); border: 1px solid #555; "
            "border-radius: 12px; }"
            "QTextEdit { background: transparent; border: none; color: #eeeeee; "
            "font-size: 15px; padding: 8px 10px; }"
            "QTextEdit::placeholder { color: #929292; }"
            "QToolButton { color: #d8d8d8; border: none; border-radius: 18px; padding: 5px 8px; }"
            "QToolButton:hover { background: #484848; }"
            "QComboBox { background: #363636; color: #dddddd; border: 1px solid #555; "
            "border-radius: 5px; padding: 4px 8px; }"
        )
        composer_layout = QVBoxLayout(self.composer)
        composer_layout.setContentsMargins(8, 6, 8, 8)
        composer_layout.setSpacing(4)
        self.input_text = ChatInputTextEdit()
        self.input_text.setPlaceholderText("输入消息…（按 Enter 发送）")
        self.input_text.setAcceptRichText(False)
        self.input_text.setMinimumHeight(56)
        self.input_text.setMaximumHeight(136)
        self.input_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._composer_line_count = 0
        self.input_text.send_requested.connect(self.send_message)
        self.input_text.image_pasted.connect(self._on_image_pasted)
        self.input_text.textChanged.connect(self._resize_composer_input)
        composer_layout.addWidget(self.input_text)

        self.attach_label = QLabel("")
        self.attach_label.setStyleSheet("color: #aeb8c2; font-size: 12px; padding: 0 8px;")
        self.attach_clear_btn = QToolButton()
        self.attach_clear_btn.setText("×")
        self.attach_clear_btn.setToolTip("清除附件")
        self.attach_clear_btn.clicked.connect(self._clear_attachments)
        attachment_row = QHBoxLayout()
        attachment_row.setContentsMargins(4, 0, 4, 0)
        attachment_row.addWidget(self.attach_label, 1)
        attachment_row.addWidget(self.attach_clear_btn)
        composer_layout.addLayout(attachment_row)
        self.attach_label.hide()
        self.attach_clear_btn.hide()

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(2, 0, 2, 0)
        self.attach_btn = QToolButton()
        self.attach_btn.setText("+")
        self.attach_btn.setStyleSheet("QToolButton { color: #d8d8d8; font-size: 22px; border: none; border-radius: 18px; padding: 2px 9px; }")
        self.attach_btn.setToolTip("截图、添加图片或选择文件")
        attach_menu = QMenu(self.attach_btn)
        screenshot_action = attach_menu.addAction("截图")
        screenshot_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DesktopIcon))
        screenshot_action.triggered.connect(self._attach_screenshot)
        image_action = attach_menu.addAction("添加图片")
        image_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
        image_action.triggered.connect(self._attach_images)
        file_action = attach_menu.addAction("选择文件")
        file_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        file_action.triggered.connect(self._attach_file)
        self.attach_btn.setMenu(attach_menu)
        self.attach_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(self.attach_btn)

        self.mode_button = QToolButton()
        self.mode_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        mode_menu = QMenu(self.mode_button)
        self.default_mode_action = mode_menu.addAction("默认模式")
        self.default_mode_action.setCheckable(True)
        self.roleplay_mode_action = mode_menu.addAction("自由角色扮演")
        self.roleplay_mode_action.setCheckable(True)
        self.default_mode_action.triggered.connect(lambda: self._set_quick_mode(False))
        self.roleplay_mode_action.triggered.connect(lambda: self._set_quick_mode(True))
        self.mode_button.setMenu(mode_menu)
        toolbar.addWidget(self.mode_button)

        self.mic_btn = QToolButton()
        self.mic_btn.setText("🎙")
        self.mic_btn.setToolTip("语音输入")
        self.mic_btn.clicked.connect(self._toggle_voice_input)
        toolbar.addWidget(self.mic_btn)
        toolbar.addStretch(1)

        self.reasoning_quick_combo = QComboBox()
        for label, value in REASONING_EFFORT_CHOICES:
            self.reasoning_quick_combo.addItem(f"推理：{label}", value)
        self.reasoning_quick_combo.setFixedWidth(104)
        self.reasoning_quick_combo.setToolTip("快捷调整当前模型的推理强度（关闭 + 六档）")
        self.reasoning_quick_combo.setCurrentIndex(
            max(0, self.reasoning_quick_combo.findData(self.config.get_reasoning_effort()))
        )
        self.reasoning_quick_combo.currentIndexChanged.connect(self._on_reasoning_quick_changed)
        toolbar.addWidget(self.reasoning_quick_combo)

        self.model_indicator = QToolButton()
        self.model_indicator.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.model_indicator.setToolTip("当前模型；点击打开配置")
        self.model_indicator.clicked.connect(self.show_config_dialog)
        toolbar.addWidget(self.model_indicator)

        self.action_stack = QStackedWidget()
        self.action_stack.setFixedSize(48, 42)
        self.send_btn = QToolButton()
        self.send_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self.send_btn.setIconSize(QSize(21, 21))
        self.send_btn.setToolTip("发送")
        self.send_btn.setStyleSheet("QToolButton { background: #e7edf2; color: #252a2e; border: none; border-radius: 21px; }")
        self.send_btn.clicked.connect(self.send_message)
        self.stop_btn = QToolButton()
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_btn.setIconSize(QSize(18, 18))
        self.stop_btn.setToolTip("停止生成")
        self.stop_btn.setStyleSheet("QToolButton { background: #d85c5c; color: white; border: none; border-radius: 21px; }")
        self.stop_btn.clicked.connect(self._stop_generation)
        self.action_stack.addWidget(self.send_btn)
        self.action_stack.addWidget(self.stop_btn)
        self.action_stack.setCurrentWidget(self.send_btn)
        toolbar.addWidget(self.action_stack)
        composer_layout.addLayout(toolbar)
        left_layout.addWidget(self.composer)
        self._update_model_indicator()
        self._update_mode_button()
        self._resize_composer_input()

        # 右侧 Live2D 区域
        right = QWidget()
        right.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.right_panel = right
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.kaomoji_label = QLabel("(｡•ᴗ•｡)")
        self.kaomoji_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.kaomoji_label.setStyleSheet("font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;")
        right_layout.addWidget(self.kaomoji_label)

        self.webview = QWebEngineView()
        self.webview.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.webview.setMinimumWidth(350)
        self.webview.setMinimumHeight(500)
        right_layout.addWidget(self.webview)

        content_splitter.addWidget(left)
        content_splitter.addWidget(right)
        if self.show_live2d_panel:
            content_splitter.setSizes([650, 350])
        else:
            right.hide()
            content_splitter.setSizes([780, 0])
        content_layout.addWidget(content_splitter)

        self.session_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.session_splitter.setChildrenCollapsible(False)
        self.session_splitter.setHandleWidth(4)
        self.session_splitter.setStyleSheet(
            "QSplitter { background: transparent; }"
            "QSplitter::handle { background: #1f1f1f; }"
            "QSplitter::handle:hover { background: #4c7396; }"
        )
        self.session_splitter.addWidget(self.session_panel)
        self.session_splitter.addWidget(self.content_panel)
        self.session_splitter.setCollapsible(0, False)
        self.session_splitter.setCollapsible(1, False)
        saved_session_width = self.config.settings.value("session_panel_width", 270, type=int)
        self.session_panel_width = max(220, min(380, int(saved_session_width or 270)))
        self.session_splitter.setSizes([self.session_panel_width, 900])
        self.session_splitter.splitterMoved.connect(self._on_session_splitter_moved)
        main_layout.addWidget(self.session_splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(f"就绪 | 当前会话: {self.current_session_id[:8]}...")
        self.refresh_session_list()

    def init_menu(self):
        menubar = self.menuBar()
        # 会话
        session_menu = menubar.addMenu("会话")
        session_menu.addAction("新建会话").triggered.connect(self.new_session)
        session_menu.addAction("历史会话").triggered.connect(self.show_session_list)
        session_menu.addSeparator()
        session_menu.addAction("清空当前会话").triggered.connect(self.clear_conversation)
        # 设置
        settings_menu = menubar.addMenu("设置")
        settings_menu.addAction("配置").triggered.connect(self.show_config_dialog)
        settings_menu.addAction("提示词").triggered.connect(self.show_prompt_dialog)
        settings_menu.addAction("语音").triggered.connect(self.show_voice_config_dialog)
        settings_menu.addAction("MCP 工具管理").triggered.connect(self.show_mcp_manager_dialog)
        # 角色切换菜单
        self.role_menu = menubar.addMenu("角色")
        self._rebuild_role_menu()
        # 新增：视图菜单
        view_menu = menubar.addMenu("视图")
        view_menu.addAction("设置聊天背景").triggered.connect(self.set_chat_background)
        view_menu.addAction("重置背景").triggered.connect(self.reset_chat_background)
        background_mode_menu = view_menu.addMenu("背景显示模式")
        self.background_mode_actions = {}
        background_modes = {
            "fill": "填充（保持比例，可能裁剪）",
            "fit": "适应（保持比例，可能留边）",
            "stretch": "拉伸（完整显示）",
            "tile": "平铺",
            "center": "居中",
        }
        for mode, label in background_modes.items():
            action = background_mode_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, value=mode: self.set_chat_background_mode(value))
            self.background_mode_actions[mode] = action
        self._refresh_background_mode_actions()
        view_menu.addAction("气泡透明度").triggered.connect(self.show_opacity_dialog)
        view_menu.addAction("桌宠模型大小").triggered.connect(self.show_pet_scale_dialog)
        session_panel_action = view_menu.addAction("会话栏")
        session_panel_action.setCheckable(True)
        session_panel_action.setChecked(True)
        session_panel_action.toggled.connect(self.toggle_session_panel)
        # 帮助
        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("语音使用说明").triggered.connect(self.show_speech_config_help)
        help_menu.addAction("配置文件说明").triggered.connect(self.show_config_file_help)
        help_menu.addAction("关于").triggered.connect(self.show_about)
        # Live2D
        l2d_menu = menubar.addMenu("Live2D")
        l2d_menu.addAction("选择模型").triggered.connect(self.show_pet_model_dialog)
        reload_action = l2d_menu.addAction("重新加载模型")
        reload_action.triggered.connect(self.reload_live2d_model)
        l2d_menu.addAction("打开模型目录").triggered.connect(self.open_model_directory)
        reload_action.setEnabled(True)

    def show_opacity_dialog(self):
        current_opacity = self.bubble_opacity
        dialog = QDialog(self)
        dialog.setWindowTitle("透明度设置")
        layout = QVBoxLayout(dialog)

        bubble_label = QLabel("气泡透明度")
        layout.addWidget(bubble_label)

        bubble_slider = QSlider(Qt.Orientation.Horizontal)
        bubble_slider.setRange(0, 100)
        bubble_slider.setValue(int(current_opacity * 100))
        layout.addWidget(bubble_slider)

        bubble_value_label = QLabel(f"{current_opacity:.2f}")
        layout.addWidget(bubble_value_label)

        def on_bubble_slider_changed(val):
            opacity = val / 100.0
            bubble_value_label.setText(f"{opacity:.2f}")
            self.update_all_bubbles_opacity(opacity)

        bubble_slider.valueChanged.connect(on_bubble_slider_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_opacity = bubble_slider.value() / 100.0
            self.bubble_opacity = new_opacity
            self.config.set_bubble_opacity(new_opacity)
            self.update_all_bubbles_opacity(new_opacity)
        else:
            self.update_all_bubbles_opacity(current_opacity)

    def _rebuild_role_menu(self):
        self.role_menu.clear()
        for role_name in self.roles.keys():
            action = self.role_menu.addAction(role_name)
            action.triggered.connect(lambda checked, r=role_name: self.switch_role(r))
        self.role_menu.addSeparator()
        manage_action = self.role_menu.addAction("管理角色")
        manage_action.triggered.connect(self.show_role_manager_dialog)

    def show_role_manager_dialog(self):
        dialog = RoleManagerDialog(self.config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.roles = self.config.get_roles()
        if self.current_role_name not in self.roles:
            self.current_role_name = next(iter(self.roles), "")
        self.config.settings.setValue("last_role", self.current_role_name)
        self._rebuild_role_menu()
        self.status_bar.showMessage("角色卡已更新", 2000)

    def switch_role(self, role_name):
        if role_name not in self.roles:
            return
        if role_name == self.current_role_name:
            return
        self.current_role_name = role_name

        self.runtime_system_prompt = self._get_effective_system_prompt()

        # 更新当前会话的 system 消息
        if self.conversation_history and self.conversation_history[0]["role"] == "system":
            if self.runtime_system_prompt.strip():
                self.conversation_history[0]["content"] = self.runtime_system_prompt
            else:
                self.conversation_history.pop(0)
        elif self.runtime_system_prompt.strip():
            self.conversation_history.insert(0, {"role": "system", "content": self.runtime_system_prompt})

        # 可选：在聊天区显示切换提示
        self.add_message(f"✨ 角色已切换为：{role_name}，请继续聊天~", is_user=False)

        self.status_bar.showMessage(f"已切换到角色：{role_name}", 3000)
        self.config.settings.setValue("last_role", role_name)

    def _refresh_background_mode_actions(self):
        for mode, action in getattr(self, "background_mode_actions", {}).items():
            action.setChecked(mode == self.chat_background_mode)

    def set_chat_background_mode(self, mode):
        if mode not in {"fill", "fit", "stretch", "tile", "center"}:
            return
        self.chat_background_mode = mode
        self.config.set_chat_background_mode(mode)
        self._refresh_background_mode_actions()
        saved_bg = self.config.settings.value("chat_bg_image", "")
        self._apply_chat_background(saved_bg if saved_bg and os.path.exists(saved_bg) else None)

    def set_chat_background(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择背景图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)",
        )
        if not file_path:
            return

        pixmap = QPixmap(file_path)
        if pixmap.isNull():
            QMessageBox.warning(self, "图片无效", "无法加载所选图片，请换一张试试。")
            return

        crop_dialog = ImageCropDialog(
            pixmap,
            target_size=self.chat_background_panel.size(),
            parent=self,
        )
        if crop_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        cropped_pixmap = crop_dialog.get_result_pixmap()
        if cropped_pixmap.isNull():
            QMessageBox.warning(self, "裁剪失败", "裁剪结果为空，请重新选择。")
            return

        output_dir = os.path.join(get_runtime_dir(), "generated", "chat_backgrounds")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"chat_bg_{uuid.uuid4().hex[:8]}.png")
        if not cropped_pixmap.save(output_path, "PNG"):
            QMessageBox.warning(self, "保存失败", "裁剪后的背景图保存失败。")
            return

        output_path = output_path.replace("\\", "/")
        self.config.settings.setValue("chat_bg_image", output_path)
        self._apply_chat_background(output_path)

    def reset_chat_background(self):
        self.config.settings.remove("chat_bg_image")
        self._apply_chat_background(None)

    def _apply_chat_background(self, image_path=None):
        """绘制聊天区背景；消息列表和列表项保持透明。"""
        self.chat_background_panel.set_background_mode(self.chat_background_mode)
        self.chat_background_panel.set_background_image(image_path)
        self.chat_list.setStyleSheet(
            "QListWidget { border: none; background: transparent; }"
            "QListWidget::item { border: none; padding: 0px; background: transparent; }"
            "QListWidget::item:hover { background: transparent; }"
        )
        self.chat_background_panel.update()

    def _get_selected_live2d_model(self):
        catalog = get_live2d_model_catalog()
        configured_name = self.config.get_pet_model_name()
        selected = next((item for item in catalog if item["id"] == configured_name), None)
        if selected:
            return selected
        fallback = next((item for item in catalog if item["id"] == "Mao"), None)
        selected = fallback or (catalog[0] if catalog else {
            "id": "Mao",
            "label": "Mao",
            "file": "Mao.model3.json",
            "path": "Mao/Mao.model3.json",
        })
        if configured_name != selected["id"]:
            self.config.set_pet_model_name(selected["id"])
        return selected

    def _live2d_page_url(self, page):
        model = self._get_selected_live2d_model()
        query = f"?model={quote(model['id'])}&file={quote(model['file'])}"
        return self.live2d_server.get_url(page) + query

    def set_pet_model(self, model_id):
        model = next((item for item in get_live2d_model_catalog() if item["id"] == model_id), None)
        if not model:
            QMessageBox.warning(self, "模型不可用", "没有找到有效的 Live2D 模型资源。")
            return
        self.config.set_pet_model_name(model["id"])
        if self.show_live2d_panel and self.webview and self.live2d_server:
            self.webview.setUrl(QUrl(self._live2d_page_url("index.html")))
        if self.pet_window:
            self.pet_window.load_live2d_model()
        self.status_bar.showMessage(f"已切换桌宠模型：{model['label']}", 3000)

    def show_pet_model_dialog(self):
        catalog = get_live2d_model_catalog()
        if not catalog:
            QMessageBox.warning(self, "没有模型", "Resources 目录中没有可用的 Live2D 模型。")
            return
        current = self._get_selected_live2d_model()["id"]
        dialog = QDialog(self)
        dialog.setWindowTitle("选择桌宠模型")
        dialog.setMinimumWidth(360)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("选择内置 Live2D 模型"))
        combo = QComboBox()
        for item in catalog:
            combo.addItem(item["label"], item["id"])
        combo.setCurrentIndex(max(0, combo.findData(current)))
        layout.addWidget(combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_pet_model(combo.currentData())

    def init_live2d_server(self):
        web_dir = get_resource_path("assets", "web_resources")
        dist_dir = os.path.join(web_dir, "dist")
        try:
            self.live2d_server = LocalHTTPServer(dist_dir, port=8123)
            self.live2d_server.start()
        except Exception as e:
            self.live2d_server = None
            logging.exception("Live2D 本地服务器启动失败：%s", e)
            return
        if self.show_live2d_panel and self.webview:
            QTimer.singleShot(1000, lambda: self.webview.setUrl(QUrl(self._live2d_page_url("index.html"))))

    def reload_live2d_model(self):
        if self.webview and self.live2d_server and self.show_live2d_panel:
            self.webview.setUrl(QUrl(self._live2d_page_url("index.html")))
        if self.pet_window:
            self.pet_window.load_live2d_model()
        if self.show_live2d_panel or self.pet_window:
            self.status_bar.showMessage("重新加载Live2D模型...", 2000)

    def open_model_directory(self):
        model_dir = get_resource_path("assets", "web_resources", "dist", "Resources")
        os.makedirs(model_dir, exist_ok=True)
        webbrowser.open(f"file://{model_dir}")

    def _build_system_prompt_messages(self):
        if not self.runtime_system_prompt.strip():
            return []
        return [{"role": "system", "content": self.runtime_system_prompt}]

    def load_system_prompt(self):
        self.conversation_history = self._build_system_prompt_messages()

    def load_history_from_db(self):
        history = self.db.load_session_messages(self.current_session_id)
        if not history:
            self.session_is_persisted = False
            self.add_message("你好呀！我是你的桌面 AI 助手，有什么想做的吗？(｡•ᴗ•｡)", is_user=False, store=False)
            return
        self.session_is_persisted = True
        self.conversation_history = self._build_system_prompt_messages()
        for msg in history:
            sources = msg.get("sources") or []
            self.conversation_history.append({
                "role": msg["role"],
                "content": attach_sources_to_text(msg["content"], sources),
            })
            self.add_message(
                msg["content"],
                is_user=(msg["role"] == "user"),
                store=False,
                sources=sources,
            )

    def _get_session_task_state(self, session_id=None):
        target_session_id = session_id or self.current_session_id
        state = self.session_task_states.get(target_session_id)
        return copy.deepcopy(state) if state else None

    def _set_session_task_state(self, task_state, session_id=None):
        target_session_id = session_id or self.current_session_id
        if task_state is None:
            self.session_task_states.pop(target_session_id, None)
            if target_session_id == self.current_session_id:
                self.current_task_state = None
            return

        snapshot = copy.deepcopy(task_state)
        self.session_task_states[target_session_id] = snapshot
        if target_session_id == self.current_session_id:
            self.current_task_state = copy.deepcopy(snapshot)

    @staticmethod
    def _resolve_world_cup_scope(user_text: str) -> str:
        normalized = user_text.lower().replace(" ", "")
        for label, aliases in WORLD_CUP_SCOPE_ALIASES.items():
            if any(alias.lower().replace(" ", "") in normalized for alias in aliases):
                return label
        return ""

    @staticmethod
    def _needs_world_cup_clarification(user_text: str) -> bool:
        normalized = user_text.replace(" ", "")
        if "世界杯" not in normalized:
            return False
        if "世俱杯" in normalized or "女足" in normalized or "预选赛" in normalized or "世预赛" in normalized:
            return False
        if not any(keyword in normalized for keyword in WORLD_CUP_STATUS_KEYWORDS):
            return False
        return not MainWindow._resolve_world_cup_scope(normalized)

    def _build_world_cup_clarification(self, user_text: str, task_state: dict) -> dict:
        question = "你想查哪一种世界杯赛况？请直接告诉我是 2026 世界杯正赛、2026 世界杯预选赛，还是国际足联世俱杯。"
        clarification = {
            "required": True,
            "kind": "world_cup_scope",
            "reason": "世界杯相关请求缺少赛事类型，无法直接联网检索。",
            "question": question,
            "options": list(WORLD_CUP_SCOPE_ALIASES.keys()),
            "answer": "",
            "resolved_value": "",
            "asked_at": now_iso_timestamp(),
            "resolved_at": ""
        }
        task_state["latest_user_message"] = user_text
        task_state["clarification"] = clarification
        touch_task_state(task_state, status="needs_clarification", current_step="等待用户补充赛事类型")
        upsert_task_plan_step(task_state, "clarify", "澄清关键信息", "awaiting_user", clarification["reason"])
        return task_state

    def _evaluate_clarification_need(self, user_text: str) -> dict:
        pending_state = self._get_session_task_state()
        if pending_state and pending_state.get("status") == "needs_clarification":
            clarification = pending_state.get("clarification", {})
            if clarification.get("kind") == "world_cup_scope":
                resolved_scope = self._resolve_world_cup_scope(user_text)
                if resolved_scope:
                    pending_state["turn_count"] = int(pending_state.get("turn_count", 1)) + 1
                    pending_state["latest_user_message"] = user_text
                    pending_state["clarification"]["required"] = False
                    pending_state["clarification"]["answer"] = user_text
                    pending_state["clarification"]["resolved_value"] = resolved_scope
                    pending_state["clarification"]["resolved_at"] = now_iso_timestamp()
                    touch_task_state(pending_state, status="planning", current_step="整合补充信息")
                    upsert_task_plan_step(
                        pending_state,
                        "clarify",
                        "澄清关键信息",
                        "completed",
                        f"已确认赛事类型：{resolved_scope}"
                    )
                    return {"action": "continue", "task_state": pending_state}

                if len(user_text.strip()) <= 10:
                    pending_state["turn_count"] = int(pending_state.get("turn_count", 1)) + 1
                    pending_state["latest_user_message"] = user_text
                    touch_task_state(pending_state, status="needs_clarification", current_step="等待用户补充赛事类型")
                    upsert_task_plan_step(
                        pending_state,
                        "clarify",
                        "澄清关键信息",
                        "awaiting_user",
                        "回复仍然不够明确，继续等待用户确认是正赛、预选赛还是世俱杯。"
                    )
                    return {
                        "action": "ask",
                        "task_state": pending_state,
                        "question": f"我还没分清具体赛事类型哦。{clarification.get('question', '')}".strip()
                    }

        if self._needs_world_cup_clarification(user_text):
            task_state = create_task_state(user_text)
            task_state = self._build_world_cup_clarification(user_text, task_state)
            return {
                "action": "ask",
                "task_state": task_state,
                "question": task_state["clarification"]["question"]
            }

        if pending_state and pending_state.get("status") == "needs_clarification":
            return {"action": "continue", "task_state": create_task_state(user_text)}

        return {"action": "continue", "task_state": create_task_state(user_text)}

    def _restore_input_controls(self):
        self.action_stack.setCurrentWidget(self.send_btn)
        self.input_text.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.attach_btn.setEnabled(True)
        self.input_text.setFocus()

    def _discard_current_generation(self):
        """在切换或删除会话前安全丢弃当前生成，不污染新的会话状态。"""
        thread = self.current_api_thread
        if thread and thread.isRunning():
            try:
                thread.stopped.disconnect(self.on_generation_stopped)
            except (TypeError, RuntimeError):
                pass
            thread.stop()
            if not thread.wait(1500):
                thread.terminate()
                thread.wait()
        self.current_api_thread = None
        row = self._placeholder_row()
        if row >= 0:
            self.chat_list.takeItem(row)
        self.placeholder_item = None
        self.placeholder_widget = None
        self.stream_buffer = ""
        self.stream_display_buffer = ""
        self.chat_stream_filter.reset()
        self._restore_input_controls()

    def _show_temporary_session(self, status_message=""):
        """显示未持久化的新会话，直到用户发送消息才创建数据库记录。"""
        self.current_session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.session_is_persisted = False
        self._set_session_task_state(None, session_id=self.current_session_id)
        self.chat_list.clear()
        self.load_system_prompt()
        self.add_message(
            "你好呀！我是你的桌面 AI 助手，有什么想做的吗？(｡•ᴗ•｡)",
            is_user=False,
            store=False,
        )
        self.refresh_session_list()
        if status_message:
            self.status_bar.showMessage(status_message, 3000)
        self._set_kaomoji_display(
            text="(｡•ᴗ•｡)",
            style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;",
        )

    def _placeholder_row(self):
        """占位气泡当前所在行；已被清空/删除时返回 -1。"""
        item = getattr(self, "placeholder_item", None)
        if item is None:
            return -1
        try:
            return self.chat_list.row(item)
        except RuntimeError:
            return -1

    def _on_task_state_updated(self, task_state):
        if self.sender() is not self.current_api_thread:
            return
        self._set_session_task_state(task_state)
        placeholder = getattr(self, "placeholder_widget", None)
        if placeholder is None:
            return
        try:
            if isinstance(placeholder, TaskProgressWidget):
                placeholder.update_progress(task_state)
        except RuntimeError:
            pass

    @pyqtSlot(str)
    def _on_computer_state(self, state):
        labels = {
            "waiting_authorization": "等待用户授权视觉键鼠操作…",
            "q_listener_active": "Q 取消监听已启动，AI 正在准备接管…",
            "input_guard_active": "输入接管已启用，AI 正在操作屏幕…",
            "waiting_vision": "正在等待视觉模型返回…",
            "observing": "AI 正在读取屏幕…",
            "observing_region": "AI 正在读取局部区域…",
            "locating_window": "AI 正在定位前台窗口…",
            "reading_accessibility": "AI 正在读取 UI 可访问性树…",
            "moving_mouse": "AI 正在移动鼠标…",
            "clicking": "AI 正在点击…",
            "verifying_click": "AI 正在验证点击目标…",
            "typing": "AI 正在输入…",
            "pressing_hotkey": "AI 正在执行快捷键…",
            "waiting_interface": "等待界面更新…",
            "waiting_external": "等待你处理目标窗口中的外部确认…",
            "stopping": "正在停止视觉接管并释放控制权…",
        }
        if state == "finished":
            self.computer_overlay.show_finished()
            return
        if state == "guard_failed":
            self.computer_overlay.show_finished("输入接管失败，任务已停止")
            return
        self.computer_overlay.show_status(labels.get(state, "AI 正在操作屏幕…"))

    @staticmethod
    def _compact_user_message(text):
        """把含附件原文的用户消息压缩成仅显示附件名（模型仍收到完整内容）。"""
        if not text or ("【用户上传文件：" not in text and "【用户图片：" not in text):
            return text
        parts = re.split(r"(【用户上传文件：.+?】|【用户图片：.+?】)", text)
        names = []
        for part in parts:
            if part.startswith("【用户上传文件："):
                names.append(part.replace("【用户上传文件：", "").replace("】", ""))
            elif part.startswith("【用户图片："):
                names.append(part.replace("【用户图片：", "").replace("】", "") + "（图片）")
        prefix = parts[0].strip() if parts else ""
        display = "附件：" + "、".join(names)
        if prefix:
            display = prefix + "\n\n" + display
        return display

    def add_message(self, message, is_user=True, store=True, sources=None):
        display_text = self._compact_user_message(message) if is_user else message
        widget = ChatMessageWidget(display_text, is_user, self.bubble_opacity, sources=sources)
        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
        widget.list_item = item
        self.chat_list.addItem(item)
        self.chat_list.setItemWidget(item, widget)
        widget.refresh_layout()
        self.chat_list.scrollToBottom()
        if store:
            role = "user" if is_user else "assistant"
            history_content = message if is_user else attach_sources_to_text(message, sources)
            self.conversation_history.append({"role": role, "content": history_content})
            if role == "user" or self.session_is_persisted:
                self.db.save_message(self.current_session_id, role, message, sources=sources)
                self.session_is_persisted = True
                self.refresh_session_list()

    # ======================== 语音交互 ========================
    def _start_wakeword_listener(self):
        try:
            self.wakeword_thread = WakeWordThread()
            self.wakeword_thread.wakeword_detected.connect(self._on_wakeword_detected)
            self.wakeword_thread.error_occurred.connect(lambda msg: self.status_bar.showMessage(str(msg), 6000))
            self.wakeword_thread.start()
            print("[语音] 唤醒词监听已启动")
        except Exception as e:
            print("[语音] 唤醒词监听启动失败: %s" % e)

    def _on_wakeword_detected(self):
        # 录音中再喊「小柚」= 取消本次录音
        if self.stt_thread and self.stt_thread.isRunning():
            self.stt_thread.cancel()
            self.status_bar.showMessage("已取消本次录音", 3000)
            return
        # TTS 播报中（兜底；通常监听已被暂停）
        if self.tts_thread and self.tts_thread.isRunning():
            self._stop_tts()
            self._set_kaomoji_display(text="在呢~ (｡•ᴗ•｡)")
            self.status_bar.showMessage("在呢~", 2000)
            return
        self._set_kaomoji_display(text="在呢~ (｡•ᴗ•｡)")
        self.status_bar.showMessage("在呢~ 请说…", 3000)
        self._start_voice_input()

    def _toggle_voice_input(self):
        if self.stt_thread and self.stt_thread.isRunning():
            self.stt_thread.stop()   # 手动结束，保留已识别文本
            self.status_bar.showMessage("正在识别…", 3000)
            return
        self._start_voice_input()

    def _start_voice_input(self):
        if not HAS_AUDIO_DEPS:
            QMessageBox.warning(self, "缺少依赖", "未安装 sounddevice / websocket-client，无法使用语音输入。")
            return
        if self.current_api_thread and self.current_api_thread.isRunning():
            self.status_bar.showMessage("等AI回答完再说吧…", 3000)
            return
        cfg = self.config.get_xfyun_config()
        if not all((cfg["appid"], cfg["api_key"], cfg["api_secret"])):
            QMessageBox.warning(
                self,
                "缺少讯飞配置",
                "请在设置界面或 auth.json 中填写 xfyun_appid / xfyun_api_key / xfyun_api_secret 后重试。",
            )
            return
        if self.wakeword_thread is None:
            QMessageBox.warning(self, "唤醒词未启动", "麦克风唤醒监听未启动，无法使用语音输入。")
            return
        if self.stt_thread and self.stt_thread.isRunning():
            return
        # 开始录音前打断正在播报的 TTS
        self._stop_tts()
        audio_queue = queue.Queue(maxsize=300)
        self.stt_thread = XfyunSTTThread(cfg["appid"], cfg["api_key"], cfg["api_secret"], audio_queue)
        self.stt_thread.partial_text.connect(self._on_stt_partial)
        self.stt_thread.final_text.connect(self._on_stt_final)
        self.stt_thread.canceled.connect(self._on_stt_canceled)
        self.stt_thread.error_occurred.connect(self._on_stt_error)
        self.stt_thread.recording_started.connect(self._on_stt_started)
        self.wakeword_thread.set_listener(audio_queue)
        self.mic_btn.setText("\u23FA 聆听中…")
        self.status_bar.showMessage("聆听中…（再喊「小柚」可取消）", 0)
        self.stt_thread.start()

    def _on_stt_started(self):
        self.mic_btn.setText("\u23FA 聆听中…")
        self.status_bar.showMessage("聆听中…（再喊「小柚」可取消）", 0)

    def _on_stt_partial(self, text):
        short = str(text).strip()
        if len(short) > 40:
            short = short[-40:]
        self.status_bar.showMessage("识别中: %s" % short, 0)

    def _on_stt_final(self, text):
        self._stop_voice_input()
        text = (text or "").strip()
        if not text:
            return
        self.input_text.setPlainText(text)
        self.input_text.setFocus()
        self.send_message()

    def _on_stt_canceled(self):
        self._stop_voice_input()
        self.status_bar.showMessage("已取消本次录音", 3000)

    def _on_stt_error(self, msg):
        logging.error("语音识别错误：%s", msg)
        self._stop_voice_input()
        self.status_bar.showMessage(str(msg), 6000)

    def _stop_voice_input(self):
        if self.wakeword_thread:
            self.wakeword_thread.set_listener(None)
        if self.stt_thread and self.stt_thread.isRunning():
            self.stt_thread.stop()
            if not self.stt_thread.wait(3000):
                self.stt_thread.terminate()
                self.stt_thread.wait()
        self.stt_thread = None
        self.mic_btn.setText("\U0001F3A4")

    def _stop_tts(self, wait_ms=500):
        active_threads = [thread for thread in self._tts_threads if thread.isRunning()]
        for thread in active_threads:
            thread.stop()
        if active_threads and self.pet_window:
            self.pet_window.cancel_speech()
        for thread in active_threads:
            thread.wait(wait_ms)
        self._tts_threads = [thread for thread in self._tts_threads if thread.isRunning()]
        if self.tts_thread and not self.tts_thread.isRunning():
            self.tts_thread = None
        if self.wakeword_thread:
            self.wakeword_thread.set_paused(False)
            self.wakeword_thread.resume()

    @pyqtSlot(int, str, object)
    def _on_tts_page_started(self, index, text, words=None):
        tts_thread = self.sender()
        if tts_thread is not self.tts_thread:
            return
        if self.pet_window:
            self.pet_window.show_tts_page(index, text, words)

    @pyqtSlot(str)
    def _on_tts_error(self, message):
        tts_thread = self.sender()
        if tts_thread is not self.tts_thread:
            return
        logging.error("TTS 播放线程错误：%s", message)
        if self.pet_window:
            self.pet_window.cancel_speech()

    @pyqtSlot()
    def _on_tts_finished(self):
        finished_thread = self.sender()
        if finished_thread in self._tts_threads:
            self._tts_threads.remove(finished_thread)
        if finished_thread is not self.tts_thread:
            return
        if self.pet_window:
            self.pet_window.finish_tts_speech()
        self.tts_thread = None
        if self.wakeword_thread:
            self.wakeword_thread.set_paused(False)
            self.wakeword_thread.resume()

    def show_speech_config_help(self):
        QMessageBox.information(
            self,
            "语音使用说明",
            "语音听写使用讯飞开放平台「语音听写（流式版）」接口。\n\n"
            "在「设置 → 语音」中填写讯飞配置，或直接在 config/auth.json 中填写：\n"
            "  xfyun_appid\n  xfyun_api_key\n  xfyun_api_secret\n\n"
            "点击输入框旁的 🎤 开始语音对话；喊「小柚」可唤醒或取消录音。",
        )

    def show_config_file_help(self):
        QMessageBox.information(
            self,
            "配置文件说明",
            "config/config.json 保存非敏感的 API 配置，例如 API Base URL、聊天模型、视觉模型、推理强度和 SearXNG URL。\n\n"
            "config/auth.json 只保存 API Key、Tavily、高德和讯飞密钥。\n\n"
            "config/apps.json 保存常用应用快捷方式，模板为 config/templates/apps.example.json。列表外应用可以在确认后通过命令或视觉键鼠打开。\n\n"
            "聊天输入区支持图片和文本文件附件；图片需要配置支持图片输入的视觉模型。\n\n"
            "输入区可以快捷切换默认/自由角色扮演模式和推理强度，其他完整配置仍在设置窗口中。\n\n"
            "读取优先级为：设置窗口中已保存的值 → config/config.json 或 config/auth.json → 程序默认值。\n"
            "设置窗口中的用户值优先；配置文件缺少某项时，会按字段自动回退。\n\n"
            "两个文件都应放在程序目录的 config 文件夹中。config/auth.json 不应提交到代码仓库。",
        )

    def _attach_file(self):
        """选择并添加文本类文件附件。"""
        files, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件", "", "所有文件 (*.*)")
        text_exts = {".txt", ".md", ".json", ".csv", ".log", ".ini", ".yml", ".yaml", ".toml",
                     ".html", ".htm", ".css", ".js", ".ts", ".xml", ".bat", ".cmd", ".ps1", ".sh",
                     ".sql", ".py", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".php", ".rb",
                     ".vue", ".jsx", ".tsx", ".ipynb", ".cfg", ".conf", ".env", ".gitignore"}
        for path in files:
            ext = os.path.splitext(path)[1].lower()
            name = os.path.basename(path)
            if len(self._pending_attachments) + len(self._pending_image_attachments) >= 5:
                QMessageBox.information(self, "附件数量上限", "一次最多上传 5 个附件。")
                break
            try:
                size = os.path.getsize(path)
                if size > 200 * 1024:
                    QMessageBox.warning(self, "文件过大", f"「{name}」超过 200KB（{size // 1024}KB），已跳过。")
                    continue
                if ext not in text_exts and not self._is_text_file(path):
                    QMessageBox.warning(self, "无法读取", f"「{name}」不是文本文件或无法读取，已跳过。")
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError as e:
                QMessageBox.warning(self, "读取失败", f"读取「{name}」失败：{e}")
                continue
            self._pending_attachments.append({"name": name, "content": content})
        self._update_attach_label()

    def _attach_screenshot(self):
        """框选屏幕区域，直接作为图片附件加入待发送列表。"""
        if len(self._pending_attachments) + len(self._pending_image_attachments) >= 5:
            QMessageBox.information(self, "附件数量上限", "一次最多上传 5 个附件。")
            return
        # 先隐藏聊天窗口，等桌面刷新后再抓屏，避免把自己截进画面
        self.hide()
        QTimer.singleShot(180, self._open_screenshot_overlay)

    def _open_screenshot_overlay(self):
        canvas, virtual_rect, ratio = ScreenCaptureOverlay.grab_desktop()
        if canvas is None or canvas.isNull():
            self._restore_after_screenshot()
            QMessageBox.warning(self, "截图失败", "没有抓到屏幕内容，请重试。")
            return
        # 抓屏已经完成，再收起自家置顶窗口：它们留在画面里，但不能继续抢鼠标
        self._hide_floating_windows()
        overlay = ScreenCaptureOverlay(canvas, virtual_rect, ratio)
        self._screenshot_overlay = overlay
        overlay.finished.connect(self._on_screenshot_finished)
        overlay.begin()

    def _hide_floating_windows(self):
        """临时收起自家置顶窗口，避免框选时被它们截走鼠标事件。"""
        pet_window = getattr(self, "pet_window", None)
        candidates = [
            pet_window,
            getattr(pet_window, "speech_bubble", None) if pet_window is not None else None,
            getattr(self, "computer_overlay", None),
        ]
        self._hidden_floating = []
        for window in candidates:
            if window is None or not window.isVisible():
                continue
            window.hide()
            self._hidden_floating.append(window)

    def _restore_floating_windows(self):
        for window in getattr(self, "_hidden_floating", []):
            window.show()
        self._hidden_floating = []

    def _restore_after_screenshot(self):
        self._screenshot_overlay = None
        self._restore_floating_windows()
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_screenshot_finished(self, pixmap):
        self._restore_after_screenshot()
        if pixmap is None or pixmap.isNull():
            self.status_bar.showMessage("已取消截图", 2500)
            return
        try:
            data_url, saved_path = self._screenshot_to_attachment(pixmap)
        except OSError as exc:
            QMessageBox.warning(self, "截图失败", f"保存截图失败：{exc}")
            return
        if not data_url:
            QMessageBox.warning(self, "截图过大", "截图压缩后仍超过 8MB，请缩小框选范围后重试。")
            return
        name = os.path.basename(saved_path)
        self._pending_image_attachments.append({
            "name": name,
            "path": saved_path,
            "data_url": data_url,
        })
        self._update_attach_label()
        self.status_bar.showMessage(f"已添加截图：{name}", 3000)

    @staticmethod
    def _screenshot_to_attachment(pixmap, prefix="截图"):
        """把图片落成临时文件并生成 data URL；超大时降采样转 JPEG。"""
        folder = os.path.join(tempfile.gettempdir(), "aissist_screenshots")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = os.path.join(folder, f"{prefix}_{stamp}.png")
        if not pixmap.save(path, "PNG"):
            raise OSError("无法写入截图文件")
        with open(path, "rb") as shot_file:
            raw = shot_file.read()
        mime_type = "image/png"
        if len(raw) > ATTACHMENT_MAX_BYTES:
            if max(pixmap.width(), pixmap.height()) > SCREENSHOT_FALLBACK_MAX_SIDE:
                scaled = pixmap.scaled(
                    SCREENSHOT_FALLBACK_MAX_SIDE,
                    SCREENSHOT_FALLBACK_MAX_SIDE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            else:
                scaled = pixmap
            jpeg_path = os.path.splitext(path)[0] + ".jpg"
            if scaled.save(jpeg_path, "JPEG", 88):
                path = jpeg_path
                with open(jpeg_path, "rb") as shot_file:
                    raw = shot_file.read()
                mime_type = "image/jpeg"
        if len(raw) > ATTACHMENT_MAX_BYTES:
            return None, path
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime_type};base64,{encoded}", path

    def _on_image_pasted(self, image):
        """把剪贴板里的图片变成附件，复用截图那条落盘/编码链路。"""
        if len(self._pending_attachments) + len(self._pending_image_attachments) >= 5:
            QMessageBox.information(self, "附件数量上限", "一次最多上传 5 个附件。")
            return
        if isinstance(image, QImage):
            pixmap = QPixmap.fromImage(image)
        elif isinstance(image, QPixmap):
            pixmap = image
        else:
            return
        if pixmap.isNull():
            return
        pixmap.setDevicePixelRatio(1.0)
        try:
            data_url, saved_path = self._screenshot_to_attachment(pixmap, prefix="粘贴")
        except OSError as exc:
            QMessageBox.warning(self, "粘贴失败", f"保存剪贴板图片失败：{exc}")
            return
        if not data_url:
            QMessageBox.warning(self, "图片过大", "剪贴板图片压缩后仍超过 8MB，请改用「添加图片」选择文件。")
            return
        name = os.path.basename(saved_path)
        self._pending_image_attachments.append({
            "name": name,
            "path": saved_path,
            "data_url": data_url,
        })
        self._update_attach_label()
        self.status_bar.showMessage(f"已从剪贴板添加图片：{name}", 3000)

    def _attach_images(self):
        """选择图片附件，作为视觉模型的 image_url 内容发送。"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "添加图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.ico)",
        )
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico"}
        for path in files:
            if len(self._pending_attachments) + len(self._pending_image_attachments) >= 5:
                QMessageBox.information(self, "附件数量上限", "一次最多上传 5 个附件。")
                break
            name = os.path.basename(path)
            ext = os.path.splitext(path)[1].lower()
            if ext not in image_exts:
                continue
            try:
                size = os.path.getsize(path)
                if size > ATTACHMENT_MAX_BYTES:
                    QMessageBox.warning(self, "图片过大", f"「{name}」超过 8MB，已跳过。")
                    continue
                with open(path, "rb") as image_file:
                    encoded = base64.b64encode(image_file.read()).decode("ascii")
            except OSError as exc:
                QMessageBox.warning(self, "读取失败", f"读取图片「{name}」失败：{exc}")
                continue
            mime_type = mimetypes.guess_type(path)[0] or "image/png"
            self._pending_image_attachments.append({
                "name": name,
                "path": path,
                "data_url": f"data:{mime_type};base64,{encoded}",
            })
        self._update_attach_label()

    @staticmethod
    def _is_text_file(path):
        """粗略判断是否为文本文件：读取前 8KB 尝试 UTF-8 解码。"""
        try:
            with open(path, "rb") as f:
                sample = f.read(8192)
            sample.decode("utf-8")
            return True
        except Exception:
            return False

    def _update_attach_label(self):
        if not self._pending_attachments and not self._pending_image_attachments:
            self.attach_label.hide()
            self.attach_clear_btn.hide()
            self._resize_composer_input()
            return
        text_names = [f"{a['name']} ({len(a['content'])}字)" for a in self._pending_attachments]
        image_names = [f"{a['name']}（图片）" for a in self._pending_image_attachments]
        names = "、".join(text_names + image_names)
        self.attach_label.setText(f"附件：{names}")
        self.attach_label.show()
        self.attach_clear_btn.show()
        self._resize_composer_input()

    def _clear_attachments(self):
        self._pending_attachments = []
        self._pending_image_attachments = []
        self._update_attach_label()

    def send_message(self):
        user_msg = self.input_text.toPlainText().strip()
        attachments = getattr(self, "_pending_attachments", None) or []
        image_attachments = getattr(self, "_pending_image_attachments", None) or []
        if not user_msg and not attachments and not image_attachments:
            return
        # 新一轮输入应打断当前播报，避免旧语音与后续回复争用同一设备。
        self._stop_tts()

        if attachments or image_attachments:
            attachment_text = ""
            for att in attachments:
                attachment_text += f"【用户上传文件：{att['name']}】\n{att['content']}\n\n"
            for image in image_attachments:
                attachment_text += f"【用户图片：{image['name']}】\n\n"
            if user_msg:
                full_msg = (user_msg + "\n\n" + attachment_text).strip()
            else:
                full_msg = attachment_text.strip()
            task_state = None
        else:
            clarification_result = self._evaluate_clarification_need(user_msg)
            if clarification_result["action"] == "ask":
                self.add_message(user_msg, is_user=True)
                self._set_session_task_state(clarification_result["task_state"])
                self.input_text.clear()
                self.add_message(clarification_result["question"], is_user=False)
                self.status_bar.showMessage("等待你补充关键信息...", 3000)
                self._set_kaomoji_display(text="( •̀ ω •́ )✧")
                self._restore_input_controls()
                return
            full_msg = user_msg
            task_state = clarification_result["task_state"]

        if not self.config.get_api_key():
            QMessageBox.warning(self, "缺少配置", "请先在 设置->API配置 中设置API Key")
            return

        self._set_session_task_state(task_state)
        self.add_message(full_msg, is_user=True)
        api_messages = self.conversation_history.copy()
        if image_attachments and api_messages:
            image_content = [{
                "type": "text",
                "text": user_msg or "请分析我上传的图片。",
            }]
            image_content.extend({
                "type": "image_url",
                "image_url": {"url": image["data_url"]},
            } for image in image_attachments)
            api_messages[-1]["content"] = image_content
        self.input_text.clear()
        self._clear_attachments()
        self.input_text.setEnabled(False)
        self.send_btn.setEnabled(False)
        self.attach_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.action_stack.setCurrentWidget(self.stop_btn)
        self.status_bar.showMessage("AI正在思考...", 0)
        self.stream_buffer = ""
        self.stream_display_buffer = ""
        self.chat_stream_filter.reset()
        if self.pet_window:
            self.pet_window.begin_speech_stream()
        self._show_thinking_kaomoji()

        self.current_api_thread = APICallThread(
            api_messages,
            self.config,
            stream=False,
            task_state=task_state
        )
        self.current_api_thread.stream_chunk.connect(self.on_stream_chunk)
        self.current_api_thread.response_received.connect(self.on_response_complete)
        self.current_api_thread.error_occurred.connect(self.on_api_error)
        self.current_api_thread.tool_confirmation_requested.connect(self.on_tool_confirmation_requested)
        self.current_api_thread.status_update.connect(lambda text: self.status_bar.showMessage(text, 0))
        self.current_api_thread.computer_state.connect(self._on_computer_state)
        self.current_api_thread.task_state_updated.connect(self._on_task_state_updated)
        self.current_api_thread.stopped.connect(self.on_generation_stopped)
        self.current_api_thread.start()

        self.placeholder_item = QListWidgetItem()
        self.placeholder_widget = TaskProgressWidget(opacity=self.bubble_opacity)
        self.placeholder_item.setSizeHint(self.placeholder_widget.sizeHint())
        self.placeholder_widget.list_item = self.placeholder_item
        self.chat_list.addItem(self.placeholder_item)
        self.chat_list.setItemWidget(self.placeholder_item, self.placeholder_widget)
        self.placeholder_widget.refresh_layout()
        self.placeholder_widget.update_progress(task_state)
    def on_stream_chunk(self, chunk):
        if self.sender() is not self.current_api_thread:
            return
        self.stream_buffer += chunk
        self.stream_display_buffer += self.chat_stream_filter.feed(chunk)
        if self.pet_window:
            self.pet_window.append_speech_chunk(chunk)
        placeholder = getattr(self, "placeholder_widget", None)
        if placeholder is None:
            return
        try:
            placeholder.set_text(self.stream_display_buffer)
        except RuntimeError:
            pass
        self.chat_list.scrollToBottom()

    def _stop_generation(self):
        had_running = False
        if self.current_api_thread and self.current_api_thread.isRunning():
            self.current_api_thread.stop()
            had_running = True
        if any(thread.isRunning() for thread in self._tts_threads):
            self._stop_tts()
            had_running = True
        self.action_stack.setCurrentWidget(self.send_btn)
        if had_running:
            self.status_bar.showMessage("正在停止…（等待当前操作返回）", 0)

    def on_generation_stopped(self):
        if self.sender() is not self.current_api_thread:
            return
        stopped_thread = self.current_api_thread
        partial = (self.stream_display_buffer + self.chat_stream_filter.feed("", final=True)).strip()
        if self.pet_window:
            self.pet_window.finish_speech_stream(
                partial,
                self.kaomoji_label.text(),
                append_expression=False,
            )
        row = self._placeholder_row()
        if row >= 0:
            self.chat_list.takeItem(row)
            if partial:
                self.add_message(partial, is_user=False, store=True)
        self.placeholder_item = None
        self.placeholder_widget = None
        if getattr(stopped_thread, "_computer_session_requested", False):
            cancel_reason = VisionInputController.get_cancel_reason()
            if cancel_reason == "q":
                self.add_message(
                    "视觉接管已取消，键鼠控制权已恢复。",
                    is_user=False,
                    store=False,
                )
            elif cancel_reason == "manual":
                self.add_message(
                    "视觉键鼠任务已停止，键鼠控制权已恢复。",
                    is_user=False,
                    store=False,
                )
        self._restore_input_controls()
        self.status_bar.showMessage("已停止生成", 3000)
        self.current_api_thread = None
        self.stream_buffer = ""
        self.stream_display_buffer = ""
        self.chat_stream_filter.reset()

    def on_response_complete(self, full_response):
        if self.sender() is not self.current_api_thread:
            return
        clean_response = full_response   # 先初始化

        # 1. 提取并移除 kaomoji 标签
        kaomoji, clean_response = extract_kaomoji_tag(clean_response)

        clean_response = self._format_assistant_text(clean_response)
        final_reply = clean_response
        body, sources_block = split_reply_and_sources(final_reply)
        sources = parse_sources_block(sources_block)
        spoken_reply = body

        # 移除占位消息
        row = self._placeholder_row()
        if row < 0:
            # 生成期间用户已清空/切换会话：丢弃本次回复，只恢复界面
            self.placeholder_item = None
            self.placeholder_widget = None
            self._restore_input_controls()
            self.status_bar.showMessage("已停止生成", 3000)
            self.current_api_thread = None
            self.stream_buffer = ""
            self.stream_display_buffer = ""
            self.chat_stream_filter.reset()
            return
        self.chat_list.takeItem(row)
        self.placeholder_item = None

        # 添加最终回复
        self.add_message(body, is_user=False, sources=sources)
        print(f"[DEBUG] AI回复原文: {clean_response}")

        # 更新颜文字：优先使用 AI 输出的标签
        if kaomoji:
            self._set_kaomoji_display(text=kaomoji)
        elif spoken_reply:
            self._update_kaomoji_by_text(spoken_reply)   # 后备方案：关键词匹配
        else:
            self._set_kaomoji_display(
                text="(｡•ᴗ•｡)",
                style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;"
            )  # 回复为空时显示默认表情

        # 语音输出
        if self.config.get_tts_enabled() and spoken_reply:
            self._stop_tts()
            voice = self.config.get_tts_voice()
            speech_pages = self.pet_window.get_speech_pages(spoken_reply) if self.pet_window else [spoken_reply]
            if self.pet_window:
                self.pet_window.prepare_tts_speech(speech_pages, self.kaomoji_label.text())
            tts_thread = TextToSpeechThread(speech_pages, voice)
            self.tts_thread = tts_thread
            self._tts_threads.append(tts_thread)
            tts_thread.page_started.connect(self._on_tts_page_started)
            tts_thread.error.connect(self._on_tts_error)
            tts_thread.finished.connect(self._on_tts_finished)
            if self.wakeword_thread:
                self.wakeword_thread.set_paused(True)
                self.wakeword_thread.suspend()
            tts_thread.start()
        elif self.pet_window:
            self.pet_window.finish_speech_stream(spoken_reply, self.kaomoji_label.text())

        # 恢复输入控件
        self._restore_input_controls()
        self.status_bar.showMessage("就绪", 2000)
        self.current_api_thread = None
        self.stream_buffer = ""
        self.stream_display_buffer = ""
        self.chat_stream_filter.reset()

    @pyqtSlot(str, str, object)
    def on_tool_confirmation_requested(self, call_id, tool_name, arguments):
        prompt_map = {
            "shutdown": "确定要执行关机吗？\n注意：系统将延时10秒执行，可在命令行输入 shutdown /a 取消。",
            "reboot": "确定要执行重启吗？\n注意：系统将延时10秒执行，可在命令行输入 shutdown /a 取消。",
            "run_command": "确定要执行以下命令吗？\n\n{command}\n\n注意：命令将以你的账户权限直接执行，请确认命令内容。",
            "write_file": "确定要写入文件吗？\n\n路径：{path}\n\n内容：\n{content}\n\n注意：该操作会覆盖已存在的文件。",
            "computer_session": "AI 请求启动视觉键鼠连续操作。\n\n"
                               "授权后，AI 可以连续截图、移动鼠标、点击、输入文字和执行快捷键。\n"
                               "任务执行期间持续按住 Q 3 秒可取消。\n\n"
                               "点击 Yes 后任务会自动继续，不需要再回聊天框回复确认。\n\n"
                               "确认开始本次视觉键鼠任务吗？",
        }
        if MCPManager.is_mcp_tool(tool_name):
            # 外部 MCP 工具的来源和参数都摊给用户看，别只丢一个英文函数名
            _mcp_server = MCPManager.server_of(tool_name) or "未知"
            prompt = (
                f"AI 请求调用外部 MCP 工具「{tool_name}」（来源 server：{_mcp_server}）。\n\n"
                f"参数：\n{json.dumps(arguments, ensure_ascii=False, indent=2)}\n\n"
                "该工具由第三方 MCP server 提供，执行前请确认内容无误。"
            )
        else:
            prompt = prompt_map.get(tool_name, f"确定要执行工具「{tool_name}」吗？")
        if tool_name == "run_command":
            prompt = prompt.replace("{command}", str(arguments.get("command", "")))
        elif tool_name == "write_file":
            prompt = prompt.replace("{path}", str(arguments.get("path", ""))).replace("{content}", str(arguments.get("content", "")))
        self._pending_permission = {"title": "AI 操作授权", "message": prompt}
        if self.pet_window:
            self.pet_window.notify_permission_request(prompt.split("\n", 1)[0])

        computer_session_active = VisionInputController.is_session_active()
        if computer_session_active:
            VisionInputController.suspend_input_guard_for_user_confirmation()

        dialog = QMessageBox()
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("AI 操作授权")
        dialog.setText(prompt)
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setWindowFlags(
            dialog.windowFlags()
            | Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self._permission_dialog = dialog
        QApplication.beep()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        reply = QMessageBox.StandardButton.No
        try:
            reply = dialog.exec()
        finally:
            self._permission_dialog = None
            self._pending_permission = None
        guard_restored = True
        if computer_session_active:
            guard_restored = VisionInputController.resume_input_guard()
        if self.current_api_thread:
            approved = reply == QMessageBox.StandardButton.Yes and guard_restored
            self.current_api_thread.set_tool_confirmation(call_id, approved)

    def raise_permission_dialog(self):
        dialog = self._permission_dialog
        if dialog is None:
            return
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.refresh_all_message_widgets)
        self.chat_background_panel.update()
        self._resize_composer_input()

    def on_api_error(self, error_msg):
        if self.sender() is not self.current_api_thread:
            return
        failed_session_id = self.current_session_id
        preserved_messages = len(self.conversation_history)
        logging.error("API 调用错误：%s", error_msg)
        row = self._placeholder_row()
        if row >= 0:
            self.chat_list.takeItem(row)
        self.placeholder_item = None
        self.placeholder_widget = None
        QMessageBox.critical(self, "API调用错误", error_msg)
        self.add_message(
            f"这次回复没有成功，但当前会话上下文已保留。你可以直接继续输入。\n原因：{error_msg}",
            is_user=False,
            store=False,
        )
        logging.info(
            "API失败后保留会话上下文：session=%s，消息数=%d",
            failed_session_id,
            preserved_messages,
        )
        self._restore_input_controls()
        self.status_bar.showMessage("错误: " + error_msg, 5000)
        self.current_api_thread = None
        self.stream_buffer = ""
        self.stream_display_buffer = ""
        self.chat_stream_filter.reset()
        if self.pet_window:
            self.pet_window.cancel_speech()
        self._set_kaomoji_display(text="(｡•́︿•̀｡)")

    def _update_model_indicator(self):
        if not hasattr(self, "model_indicator"):
            return
        model = self.config.get_model() or "未配置模型"
        label = model if len(model) <= 24 else model[:21] + "…"
        self.model_indicator.setText(label)
        self.model_indicator.setToolTip(f"当前模型：{model}\n点击打开配置")

    def _resize_composer_input(self):
        if not hasattr(self, "input_text"):
            return
        line_height = max(20, self.input_text.fontMetrics().lineSpacing())
        document = self.input_text.document()
        document.documentLayout().documentSize()
        visual_lines = 0
        block = document.begin()
        while block.isValid():
            layout = block.layout()
            visual_lines += max(1, layout.lineCount() if layout else 1)
            block = block.next()
        visual_lines = max(1, visual_lines)
        visible_lines = max(2, min(5, visual_lines or 1))
        target_input_height = line_height * visible_lines + 16
        attachment_height = 26 if self.attach_label.isVisible() else 0
        target_composer_height = target_input_height + 42 + 14 + attachment_height
        if visible_lines == self._composer_line_count and self.composer.height() == target_composer_height:
            return
        self._composer_line_count = visible_lines
        self.input_text.setFixedHeight(target_input_height)
        self.composer.setFixedHeight(target_composer_height)
        self.composer.updateGeometry()
        parent_layout = self.composer.parentWidget().layout()
        if parent_layout:
            parent_layout.invalidate()
            parent_layout.activate()
        logging.info(
            "输入框布局更新：visual_lines=%s，input_height=%s，composer_height=%s",
            visible_lines,
            target_input_height,
            target_composer_height,
        )

    def _update_mode_button(self):
        roleplay = self.config.get_roleplay_mode()
        self.mode_button.setText("自由角色扮演" if roleplay else "默认模式")
        self.default_mode_action.setChecked(not roleplay)
        self.roleplay_mode_action.setChecked(roleplay)

    def _set_quick_mode(self, roleplay):
        self.config.set_roleplay_mode(bool(roleplay))
        self._update_mode_button()
        self.status_bar.showMessage(
            "已切换到自由角色扮演模式" if roleplay else "已切换到默认模式",
            2000,
        )

    def _on_reasoning_quick_changed(self, index):
        value = self.reasoning_quick_combo.itemData(index) or ""
        self.config.set_reasoning_effort(value)
        self.status_bar.showMessage(
            f"推理强度：{self.reasoning_quick_combo.currentText()}",
            1800,
        )

    def show_config_dialog(self):
        dialog = ConfigDialog(self.config, self, section="config")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_action_handler_config()
            self.runtime_system_prompt = self._get_effective_system_prompt()
            self.load_system_prompt()
            self._update_model_indicator()
            self.reasoning_quick_combo.setCurrentIndex(
                max(0, self.reasoning_quick_combo.findData(self.config.get_reasoning_effort()))
            )
            self._update_mode_button()
            self.status_bar.showMessage("配置已更新", 2000)

    def show_prompt_dialog(self):
        dialog = ConfigDialog(
            self.config,
            self,
            self.runtime_system_prompt,
            section="prompt",
            default_system_prompt_text=self._get_default_system_prompt(),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.runtime_system_prompt = self._get_effective_system_prompt()
            self.load_system_prompt()
            self.status_bar.showMessage("提示词已更新", 2000)

    def show_voice_config_dialog(self):
        dialog = ConfigDialog(self.config, self, section="voice")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.status_bar.showMessage("语音配置已更新", 2000)

    def clear_conversation(self):
        reply = QMessageBox.question(self, "确认清空", "确定要清空当前会话的所有对话记录吗？",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._stop_generation()
            self.chat_list.clear()
            self.db.delete_session(self.current_session_id)
            self.session_is_persisted = False
            self._set_session_task_state(None)
            self.load_system_prompt()
            self.add_message("对话历史已清空。有什么我可以帮助你的吗？", is_user=False, store=False)
            self.refresh_session_list()
            self.status_bar.showMessage("当前会话已清空", 2000)
            self._set_kaomoji_display(
                text="(｡•ᴗ•｡)",
                style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;"
            )

    def new_session(self):
        self._discard_current_generation()
        self._show_temporary_session("新会话已准备好，等待新消息")

    def show_session_list(self):
        """显示历史会话列表对话框，支持双击切换和右键/按钮删除"""
        sessions = self.db.get_session_info()
        if not sessions:
            QMessageBox.information(self, "历史会话", "暂无历史会话记录")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("历史会话")
        dialog.setMinimumWidth(500)
        layout = QVBoxLayout(dialog)

        list_widget = QListWidget()
        for sid, last_time, preview in sessions:
            time_str = datetime.fromisoformat(last_time).strftime("%Y-%m-%d %H:%M") if last_time else "未知"
            preview_text = (preview[:40] + "...") if preview and len(preview) > 40 else (preview or "无消息")
            display_text = f"{sid}\n{time_str}\n{preview_text}"
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, sid)
            list_widget.addItem(item)

        layout.addWidget(QLabel("双击会话切换，右键或点击下方按钮删除"))

        btn_layout = QHBoxLayout()
        delete_btn = QPushButton("删除选中会话")
        close_btn = QPushButton("关闭")
        btn_layout.addWidget(delete_btn)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
        layout.addWidget(list_widget)

        def delete_selected():
            current_item = list_widget.currentItem()
            if not current_item:
                QMessageBox.warning(dialog, "提示", "请先选择一个会话")
                return
            session_id = current_item.data(Qt.ItemDataRole.UserRole)
            if session_id == self.current_session_id:
                QMessageBox.warning(dialog, "警告", "不能删除当前正在使用的会话。")
                return
            reply = QMessageBox.question(dialog, "确认删除", f"确定要删除会话 {session_id} 吗？",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.db.delete_session(session_id)
                self._set_session_task_state(None, session_id=session_id)
                row = list_widget.row(current_item)
                list_widget.takeItem(row)
                QMessageBox.information(dialog, "成功", "会话已删除")
                if list_widget.count() == 0:
                    QMessageBox.information(dialog, "提示", "所有历史会话已清空")
                    dialog.accept()

        def on_double_click(item):
            session_id = item.data(Qt.ItemDataRole.UserRole)
            self.switch_session(session_id)
            dialog.accept()

        def show_context_menu(pos):
            item = list_widget.itemAt(pos)
            if item:
                menu = QMenu()
                menu.addAction("删除此会话")
                action = menu.exec(list_widget.mapToGlobal(pos))
                if action and action.text() == "删除此会话":
                    list_widget.setCurrentItem(item)
                    delete_selected()

        list_widget.itemDoubleClicked.connect(on_double_click)
        list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        list_widget.customContextMenuRequested.connect(show_context_menu)
        delete_btn.clicked.connect(delete_selected)
        close_btn.clicked.connect(dialog.accept)

        dialog.exec()

    def show_mcp_manager_dialog(self):
        """只读查看 MCP server 状态与工具（P1）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("MCP 工具管理")
        dialog.resize(780, 480)
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel(
            "MCP 工具由外部 server 提供，工具名格式 mcp__<server>__<工具名>。\n"
            "当前为只读查看；修改 config/mcp.json 后需重启 Aissist 生效。"
        ))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        server_list = QListWidget()
        tool_view = QTextBrowser()
        splitter.addWidget(server_list)
        splitter.addWidget(tool_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        status_label = QLabel("")
        layout.addWidget(status_label)

        btn_layout = QHBoxLayout()
        refresh_btn = QPushButton("刷新状态")
        config_btn = QPushButton("打开配置文件")
        log_btn = QPushButton("打开日志目录")
        close_btn = QPushButton("关闭")
        btn_layout.addWidget(refresh_btn)
        btn_layout.addWidget(config_btn)
        btn_layout.addWidget(log_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        # 只在内容变化时更新视图，避免定时刷新把滚动位置重置到顶部
        view_state = {"server_sig": None, "tool_html": None}

        def build_tool_html(server_name):
            tools = MCPManager.server_tools(server_name)
            if not tools:
                return "<p style='color:#888'>该 server 暂无可用工具（未就绪或没有工具）。</p>"
            parts = ["<h3>%s（%d 个工具）</h3>" % (html.escape(server_name), len(tools))]
            for t in tools:
                props = list((t.get("parameters") or {}).get("properties", {}).keys())
                parts.append(
                    "<p><b>%s</b><br><span style='color:#888'>参数：%s</span><br>%s</p>"
                    % (html.escape(t.get("name", "")),
                       html.escape(", ".join(props) or "无"),
                       html.escape(t.get("description", "") or ""))
                )
            return "".join(parts)

        def show_tools(server_name):
            if server_name:
                content = build_tool_html(server_name)
            else:
                content = "<p style='color:#888'>未配置任何 MCP server（或尚未选择）。可在 config/mcp.json 的 mcpServers 中添加。</p>"
            if content == view_state["tool_html"]:
                return
            view_state["tool_html"] = content
            tool_view.setHtml(content)

        def refresh(force=False):
            servers = MCPManager.status()
            server_sig = tuple(
                (s.get("name"), bool(s.get("ready")), s.get("tools", 0), str(s.get("error") or ""))
                for s in servers
            )
            if force or server_sig != view_state["server_sig"]:
                view_state["server_sig"] = server_sig
                current = server_list.currentItem()
                current_name = current.data(Qt.ItemDataRole.UserRole) if current else None
                row_to_select = 0
                server_list.blockSignals(True)
                server_list.clear()
                for i, s in enumerate(servers):
                    state = "就绪" if s.get("ready") else "未就绪"
                    if s.get("error"):
                        state = "错误"
                    item = QListWidgetItem("%s   [%s]   工具 %d 个" % (s.get("name"), state, s.get("tools", 0)))
                    item.setData(Qt.ItemDataRole.UserRole, s.get("name"))
                    if s.get("error"):
                        item.setForeground(QColor("#c0392b"))
                        item.setToolTip(str(s.get("error")))
                    server_list.addItem(item)
                    if s.get("name") == current_name:
                        row_to_select = i
                if servers:
                    server_list.setCurrentRow(row_to_select)
                server_list.blockSignals(False)
            item = server_list.currentItem()
            show_tools(item.data(Qt.ItemDataRole.UserRole) if item else "")
            status_label.setText("共 %d 个 server，%d 个 MCP 工具。" % (
                len(servers), len(MCPManager.get_tool_definitions())))

        def on_select():
            item = server_list.currentItem()
            show_tools(item.data(Qt.ItemDataRole.UserRole) if item else "")

        def open_path(path):
            try:
                if sys.platform.startswith("win"):
                    os.startfile(path)
                else:
                    webbrowser.open("file://" + path)
            except Exception as exc:
                QMessageBox.warning(dialog, "打开失败", str(exc))

        refresh_btn.clicked.connect(lambda: refresh(force=True))
        server_list.currentItemChanged.connect(lambda *_: on_select())
        config_btn.clicked.connect(lambda: open_path(MCPManager.config_path()))
        log_btn.clicked.connect(lambda: open_path(os.path.join(get_runtime_dir(), "logs")))
        close_btn.clicked.connect(dialog.accept)

        timer = QTimer(dialog)
        timer.setInterval(1500)
        timer.timeout.connect(refresh)
        timer.start()

        refresh()
        dialog.exec()
        timer.stop()

    def show_pet_scale_dialog(self):
        if not self.pet_window:
            QMessageBox.information(self, "桌宠模型大小", "桌宠窗口尚未准备好。")
            return

        original_scale = self.config.get_pet_model_scale()
        dialog = QDialog(self)
        dialog.setWindowTitle("桌宠模型大小")
        layout = QVBoxLayout(dialog)

        scale_value = QLabel()
        scale_slider = QSlider(Qt.Orientation.Horizontal)
        scale_slider.setRange(50, 140)
        scale_slider.setValue(round(original_scale * 100))
        scale_slider.setToolTip("调整桌宠 Live2D 模型大小")

        def preview(value):
            scale_value.setText(f"{value}%")
            self.pet_window.set_model_scale(value / 100, persist=False)

        scale_slider.valueChanged.connect(preview)
        preview(scale_slider.value())

        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(lambda: scale_slider.setValue(100))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        layout.addWidget(QLabel("模型大小"))
        layout.addWidget(scale_slider)
        layout.addWidget(scale_value, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(reset_btn, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.pet_window.set_model_scale(scale_slider.value() / 100, persist=True)
        else:
            self.pet_window.set_model_scale(original_scale, persist=False)

    def switch_session(self, session_id):
        if session_id == self.current_session_id:
            return
        self._stop_generation()
        self.current_session_id = session_id
        self.current_task_state = self._get_session_task_state(session_id)
        self.chat_list.clear()
        self.load_system_prompt()
        self.load_history_from_db()
        self.refresh_session_list()
        self.status_bar.showMessage(f"已切换到会话: {session_id[:8]}...", 3000)
        self._set_kaomoji_display(
            text="(｡•ᴗ•｡)",
            style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;"
        )

    def show_about(self):
        QMessageBox.about(self, "关于AI助手",
                          "Aissist v1.101.4-test (PyQt6版本)\n"
                          "功能：多会话聊天、图片/文件附件、视觉键鼠、UI Automation、Live2D、语音输入与回复\n"
                          "技术栈：Python + PyQt6 + OpenAI兼容API + Live2D + edge-tts")

    def _update_kaomoji_by_text(self, text: str):
        """根据 AI 回复的内容分析情绪，并更新顶部颜文字标签"""
        if not text:
            self._set_kaomoji_display(
                text="(｡•ᴗ•｡)",
                style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;"
            )
            return

        # 情绪关键词（可根据喜好扩展）
        happy_keywords = ["开心", "高兴", "喜欢", "爱", "棒", "好", "有趣", "笑", "愉快", "美好", "幸福", "耶", "嘻嘻", "哈哈"]
        sad_keywords = ["难过", "伤心", "悲伤", "生气", "愤怒", "讨厌", "烦", "痛苦", "郁闷", "失望", "唉", "呜呜"]
        angry_keywords = ["气死", "火大", "炸了", "可恶", "恨", "暴怒"]
        surprised_keywords = ["哇", "哦", "真的吗", "惊讶", "神奇", "没想到", "居然", "竟然"]

        # 颜文字池
        happy_kaomoji = ["(｡•̀ᴗ•́｡)", "(◕‿◕✿)", "(＾▽＾)", "(´｡• ᵕ •｡`)", "(｡♥‿♥｡)", "٩(◕‿◕｡)۶", "(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧"]
        sad_kaomoji = ["(；ω；)", "(｡•́︿•̀｡)", "(╥﹏╥)", "(T_T)", "(>_<)", "(｡╯︵╰｡)", "(´;ω;`)"]
        angry_kaomoji = ["(╯°□°）╯︵ ┻━┻", "(｀Д´)", "(ノಠ益ಠ)ノ", "(>:-<)", "(＃`Д´)", "(ꐦ°᷄д°᷅)", "(◣_◢)"]
        surprised_kaomoji = ["(°ロ°) !", "(⊙ˍ⊙)", "(⊙_⊙)", "(°o°)", "(º﹃º)", "(⊙ω⊙)", "Σ(°△°|||)"]
        neutral_kaomoji = ["(｡•ᴗ•｡)", "(￣ω￣)", "(´-ω-`)", "(•_•)", "(._.)", "(｡-ω-｡)", "(-ω-)"]

        text_lower = text.lower()
        happy_score = sum(1 for kw in happy_keywords if kw in text_lower or kw in text)
        sad_score = sum(1 for kw in sad_keywords if kw in text_lower or kw in text)
        angry_score = sum(1 for kw in angry_keywords if kw in text_lower or kw in text)
        surprised_score = sum(1 for kw in surprised_keywords if kw in text_lower or kw in text)

        scores = {
            "happy": happy_score,
            "sad": sad_score,
            "angry": angry_score,
            "surprised": surprised_score,
            "neutral": 1   # 基础分，避免无匹配时随机到空
        }

        max_emotion = max(scores, key=scores.get)
        if max_emotion == "happy":
            chosen = random.choice(happy_kaomoji)
        elif max_emotion == "sad":
            chosen = random.choice(sad_kaomoji)
        elif max_emotion == "angry":
            chosen = random.choice(angry_kaomoji)
        elif max_emotion == "surprised":
            chosen = random.choice(surprised_kaomoji)
        else:
            chosen = random.choice(neutral_kaomoji)

        # 根据情绪设置颜色
        if max_emotion == "happy":
            color = "#FF69B4"      # 粉红
        elif max_emotion == "sad":
            color = "#4A90E2"      # 淡蓝
        elif max_emotion == "angry":
            color = "#E94F4F"      # 红色
        elif max_emotion == "surprised":
            color = "#FFB347"      # 橙色
        else:
            color = "#000000"      # 黑色
        self._set_kaomoji_display(
            text=chosen,
            style=f"font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: {color};"
        )


    def _show_thinking_kaomoji(self):
        """显示思考中的颜文字"""
        thinking = ["( •_• )?", "( ˘•ω•˘ )", "(｡•́︿•̀｡)", "(*´･ω･)", "(・・?)"]
        self._set_kaomoji_display(text=random.choice(thinking))

    def closeEvent(self, event):
        if self.managed_by_pet and not self.allow_close:
            event.ignore()
            self.hide()
            return
        self.computer_overlay.hide()
        # 关掉所有外部 MCP server 子进程，避免关窗后留下孤儿进程
        MCPManager.shutdown()
        if self.current_api_thread and self.current_api_thread.isRunning():
            try:
                self.current_api_thread.stopped.disconnect(self.on_generation_stopped)
            except (TypeError, RuntimeError):
                pass
            self.current_api_thread.stop()
            if not self.current_api_thread.wait(1000):
                self.current_api_thread.terminate()
                self.current_api_thread.wait()
        # 停止语音相关线程
        self._stop_tts(wait_ms=3000)
        if self.stt_thread and self.stt_thread.isRunning():
            self.stt_thread.stop()
            if not self.stt_thread.wait(3000):
                self.stt_thread.terminate()
                self.stt_thread.wait()
            self.stt_thread = None
        if self.wakeword_thread and self.wakeword_thread.isRunning():
            self.wakeword_thread.stop()
            if not self.wakeword_thread.wait(2000):
                self.wakeword_thread.terminate()
                self.wakeword_thread.wait()
            self.wakeword_thread = None
        if self.live2d_server:
            threading.Thread(target=self.live2d_server.stop, daemon=True).start()
        event.accept()

    def show_chat_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_chat_window(self):
        self.hide()

    def request_close(self):
        self.allow_close = True
        self.close()

    


# ======================== 程序入口 ========================
def main():
    log_path = setup_logging()
    logging.info("程序启动，日志文件：%s", log_path)
    app = QApplication(sys.argv)
    app_icon = get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    app.setStyle("Fusion")
    app.setApplicationName("AI桌面助手")
    app.setOrganizationName("AI_Assistant")
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow(show_live2d_panel=False, managed_by_pet=True)
    pet_window = DesktopPetWindow(window, window.live2d_server)
    window.bind_pet_window(pet_window)
    pet_window.show()
    logging.info("桌宠窗口已显示，进入主循环")
    exit_code = app.exec()
    logging.info("程序退出，退出码 %s", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
