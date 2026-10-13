"""Reasoning-effort tiers and gateway error classification."""

from __future__ import annotations
import json
import re



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


REASONING_EFFORT_ERROR_HINTS = (
    "unsupported", "not support", "does not support", "unknown", "invalid",
    "unrecognized", "unrecognised", "unexpected", "illegal",
    "不支持", "无效", "非法", "未知", "无法识别",
)


# DSH 推理档位（ACP 的 reasoning_effort）：这里列出 pi-ai 适配器允许的等级全集，
# 真正可选的值取决于所选模型在 DSH 里声明的 reasoningEfforts——没声明时 DSH 不会公布该选项。
DSH_REASONING_CHOICES = (
    ("关闭推理（off）", "off"),
    ("最低（minimal）", "minimal"),
    ("低（low）", "low"),
    ("中（medium）", "medium"),
    ("高（high）", "high"),
    ("极高（xhigh）", "xhigh"),
    ("最高（max）", "max"),
)


def reasoning_effort_rank(value: str) -> int:
    """档位排名，数值越小档位越高；"关闭" 与未知取值排在最后。"""
    try:
        return REASONING_EFFORT_ORDER.index((value or "").strip().lower())
    except ValueError:
        return len(REASONING_EFFORT_ORDER)


def reasoning_effort_label(value: str) -> str:
    """把档位取值翻译成中文标签，空值表示关闭。"""
    return REASONING_EFFORT_LABELS.get((value or "").strip().lower(), "关闭")


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
