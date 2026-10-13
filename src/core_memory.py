"""字段化常驻块（core memory）+ 偏好自动提炼。

为什么单独一个模块：

* 常驻块是「永远在场」的记忆，和检索出来的历史片段走**完全不同**的路径 ——
  它不参与打分，也就没有 0.55/0.7 这类阈值问题；
* 它的内容必须是**有结构、有预算、有来源**的，否则会慢慢长成一个没人管的大字符串。

三条硬规则（对应需求）：

1. **字段化 + 各带预算**：preferences / capabilities / task 各自独立字符上限，超了截断；
2. **带来源**：每条偏好记下来自哪个会话、哪条消息、什么时候写进来的；
3. **不覆盖**：新旧偏好冲突时把旧条目标成 superseded（保留可审计），绝不就地改写或删除。

提炼是 LLM 调用，所以**永远走后台线程**：调用线程只读缓存，绝不为它阻塞一次发送。
调用失败一律静默降级（这一轮就用旧块），不影响聊天。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid

import http_client
import thread_registry
from app_paths import get_runtime_dir

CORE_MEMORY_FILENAME = "core_memory.json"

# 字段名 -> 默认字符预算。总量刻意压在 1.5k 字符以内：
# 它是每一轮都在烧的预算，贵。
DEFAULT_FIELD_BUDGETS = {
    "preferences": 600,
    "capabilities": 300,
    "task": 400,
}

FIELD_TITLES = {
    "preferences": "用户长期偏好",
    "capabilities": "能力索引",
    "task": "当前任务",
}

FIELD_ORDER = ("preferences", "capabilities", "task")

# 「能力索引」这一栏是给模型看的菜单索引：只说明「有什么」，不写详细用法
# （详细用法属于工具描述，写在这里等于每轮白烧预算）。
#
# 它同时承担 pull 通道最要命的那个失败模式的兜底：「模型不知道自己不知道」——
# 上周说过一件重要的事，这次它没想到要查，那就永远查不到。在常驻块里点名
# search_memory，等于给模型递一张菜单索引，能大幅降低"没想到要查"的概率。
DEFAULT_CAPABILITY_HINT = (
    "长期偏好以本块为准，不需要检索；历史对话细节可调用 search_memory 查，"
    "它查不到时会明确说明，不要臆造。"
)
# pull 通道出现之前的版本。首次读取时若发现能力索引还是这条出厂原文，
# 就把它升级成上面那条（用户自己改过则一律不碰）。
LEGACY_CAPABILITY_HINT = (
    "长期偏好以本块为准，不需要检索；历史对话细节由本地记忆检索补足，"
    "检索不到时会明确说明，不要臆造。"
)

BLOCK_HEADER = "【长期背景（常驻，每轮都在；优先级高于检索片段）】"
BLOCK_FOOTER = "【以上为长期背景，不要复述本段，直接回答用户。】"

# 提炼节流：至少攒够这么多条新消息才值得花一次 LLM 调用。
DEFAULT_EXTRACT_MIN_NEW_MESSAGES = 6
DEFAULT_EXTRACT_COOLDOWN_SECONDS = 600
DEFAULT_EXTRACT_INPUT_CHARS = 8000
DEFAULT_EXTRACT_PER_MESSAGE_CHARS = 600
DEFAULT_EXTRACT_MAX_ITEMS = 5
DEFAULT_EXTRACT_ITEM_CHARS = 60
DEFAULT_EXTRACT_TIMEOUT = (5, 60)

# 提炼调用的输出预算。**512 是错的，而且错得完全没有声音。**
#
# 实测（真库会话 81 条消息、模型 deepseek-v4-flash）：
#   max_tokens=512  → finish_reason='length'，completion_tokens=512 且
#                     **全部 512 个都是 reasoning_tokens**，正文 content 是空字符串。
#                     空正文 → _parse_extraction 在 405 行 `return []` → 日志只有
#                     「抽出 0 条」。这条链上一个异常都不留，所以它挂了很久没人发现。
#   max_tokens=4096 → finish_reason='stop'，推理 2323 + 正文 132，稳定抽出 4 条真偏好。
#
# 也就是说：思维链是**先花掉的**，正文排在后面。预算只够推理时，正文一个字都写不出来 ——
# 这不是"截断"，是"整条回答根本没开始写"。任何推理型模型都会踩，与提示词质量无关。
DEFAULT_EXTRACT_MAX_TOKENS = 4096

# 提炼输入里「开头」占的预算比例，剩下的全给**最近**的消息。
#
# 原来是**从第一行往后取、取满就 break**：实测一个 81 条的会话，只有第 1~35 条进得了
# 提示词，后面 46 条从来没被看到。而长期偏好恰恰是聊到后面才冒出来的 —— 越新的越抽不到，
# 而且每次触发都从头再取同一段，所以它**永远**抽不到新的。
#
# 但也不能只取最近的：会话开头往往交代了项目背景和整体诉求。所以两头都取，
# 开头只留三分之一，其余全给最近的消息。中间被跳过的部分会插一行省略标记，
# 免得模型以为对话是连续的。
DEFAULT_EXTRACT_HEAD_RATIO = 0.34

MAX_HISTORY_ENTRIES = 200
HISTORY_TRUNCATE_CHARS = 200


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------


def default_core_memory_path():
    return os.path.join(get_runtime_dir(), "data", CORE_MEMORY_FILENAME)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _clean_text(text, limit=None):
    body = " ".join(str(text or "").split())
    if limit and len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return body


def _norm_key(text):
    """用于去重的归一化键：忽略大小写、空白和常见标点。"""
    body = str(text or "").lower()
    body = re.sub(r"[\s，。、；：！？,.;:!?\"'`（）()\[\]【】]+", "", body)
    return body


class CoreMemoryStore:
    """常驻块的持久化：一个 JSON 文档，原子写入，读取走内存缓存。"""

    def __init__(self, path=None):
        self.path = str(path or default_core_memory_path())
        self._lock = threading.RLock()
        self._data = self._load()

    # ---- 读写 ----------------------------------------------------------

    def _empty(self):
        return {
            "version": 0,
            "updated_at": "",
            "fields": {
                name: {"budget_chars": budget, "items": []}
                for name, budget in DEFAULT_FIELD_BUDGETS.items()
            },
            "history": [],
        }

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError):
            logging.warning("读取常驻块失败，改用空块：%s", self.path, exc_info=True)
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        return self._normalize(data)

    def _normalize(self, data):
        result = self._empty()
        result["version"] = int(data.get("version") or 0)
        result["updated_at"] = str(data.get("updated_at") or "")
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        for name, default in DEFAULT_FIELD_BUDGETS.items():
            raw = fields.get(name) if isinstance(fields.get(name), dict) else {}
            items = raw.get("items") if isinstance(raw.get("items"), list) else []
            cleaned = []
            for item in items:
                normalized = self._normalize_item(item)
                if normalized is not None:
                    cleaned.append(normalized)
            try:
                budget = int(raw.get("budget_chars") or default)
            except (TypeError, ValueError):
                budget = default
            result["fields"][name] = {
                "budget_chars": max(60, budget),
                "items": cleaned,
            }
        history = data.get("history") if isinstance(data.get("history"), list) else []
        result["history"] = [entry for entry in history if isinstance(entry, dict)][-MAX_HISTORY_ENTRIES:]
        return result

    @staticmethod
    def _normalize_item(item):
        if not isinstance(item, dict):
            return None
        text = _clean_text(item.get("text"))
        if not text:
            return None
        status = str(item.get("status") or "active").strip().lower()
        if status not in ("active", "superseded", "retired"):
            status = "active"
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "id": str(item.get("id") or uuid.uuid4().hex[:12]),
            "text": text,
            "status": status,
            "confidence": max(0.0, min(1.0, confidence)),
            "source_session": str(item.get("source_session") or ""),
            "source_message_id": item.get("source_message_id"),
            "created_at": str(item.get("created_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
            "superseded_by": item.get("superseded_by"),
        }

    def _save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            temp_path = self.path + ".tmp"
            with open(temp_path, "w", encoding="utf-8", newline="") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temp_path, self.path)
        except OSError:
            logging.warning("写入常驻块失败：%s", self.path, exc_info=True)

    def _append_history(self, action, field, record_id, text):
        self._data["history"].append(
            {
                "at": _now_iso(),
                "action": str(action),
                "field": str(field),
                "id": str(record_id or ""),
                "text": _clean_text(text, HISTORY_TRUNCATE_CHARS),
            }
        )
        if len(self._data["history"]) > MAX_HISTORY_ENTRIES:
            self._data["history"] = self._data["history"][-MAX_HISTORY_ENTRIES:]

    def _bump(self):
        self._data["version"] = int(self._data.get("version") or 0) + 1
        self._data["updated_at"] = _now_iso()

    # ---- 查询 ----------------------------------------------------------

    def version(self):
        with self._lock:
            return int(self._data.get("version") or 0)

    def items(self, field, status="active"):
        with self._lock:
            bucket = (self._data.get("fields") or {}).get(field) or {}
            items = bucket.get("items") or []
            if status is None:
                return [dict(item) for item in items]
            return [dict(item) for item in items if item.get("status") == status]

    def find_by_text(self, field, text, status="active"):
        key = _norm_key(text)
        if not key:
            return None
        for item in self.items(field, status=status):
            if _norm_key(item.get("text")) == key:
                return item
        return None

    def field_budget(self, field):
        with self._lock:
            bucket = (self._data.get("fields") or {}).get(field) or {}
            try:
                return int(bucket.get("budget_chars") or DEFAULT_FIELD_BUDGETS.get(field, 300))
            except (TypeError, ValueError):
                return DEFAULT_FIELD_BUDGETS.get(field, 300)

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    # ---- 变更 ----------------------------------------------------------

    def add_item(self, field, text, source_session="", source_message_id=None, confidence=0.0):
        """新增一条。文本重复（归一化后相同）时返回已有条目，不重复写。"""
        field = str(field or "").strip()
        if field not in DEFAULT_FIELD_BUDGETS:
            return None
        text = _clean_text(text, DEFAULT_EXTRACT_ITEM_CHARS)
        if not text:
            return None
        with self._lock:
            existing = self.find_by_text(field, text)
            if existing is not None:
                return existing
            item = {
                "id": uuid.uuid4().hex[:12],
                "text": text,
                "status": "active",
                "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
                "source_session": str(source_session or ""),
                "source_message_id": source_message_id,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "superseded_by": None,
            }
            self._data["fields"][field]["items"].append(item)
            self._append_history("add", field, item["id"], text)
            self._bump()
            self._save()
            return dict(item)

    def supersede_item(self, field, item_id, new_item_id=None, reason=""):
        """把一条标记为失效（保留原文与来源，供审计），不删除。"""
        field = str(field or "").strip()
        with self._lock:
            bucket = (self._data.get("fields") or {}).get(field)
            if not bucket:
                return False
            for item in bucket.get("items") or []:
                if str(item.get("id")) != str(item_id):
                    continue
                if item.get("status") == "superseded":
                    return True
                item["status"] = "superseded"
                item["superseded_by"] = new_item_id
                item["updated_at"] = _now_iso()
                self._append_history("supersede", field, item.get("id"), item.get("text"))
                self._bump()
                self._save()
                return True
            return False

    def retire_item(self, field, item_id):
        """用户手动删除一条：标记 retired，同样不物理删除。"""
        field = str(field or "").strip()
        with self._lock:
            bucket = (self._data.get("fields") or {}).get(field)
            if not bucket:
                return False
            for item in bucket.get("items") or []:
                if str(item.get("id")) != str(item_id):
                    continue
                item["status"] = "retired"
                item["updated_at"] = _now_iso()
                self._append_history("retire", field, item.get("id"), item.get("text"))
                self._bump()
                self._save()
                return True
            return False

    def set_field_budget(self, field, budget_chars):
        field = str(field or "").strip()
        if field not in DEFAULT_FIELD_BUDGETS:
            return False
        try:
            value = max(60, int(budget_chars))
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._data["fields"][field]["budget_chars"] = value
            self._bump()
            self._save()
            return True

    def clear_field(self, field):
        """整栏退休（例如用户想重置偏好）。"""
        for item in self.items(field, status="active"):
            self.retire_item(field, item.get("id"))


# --------------------------------------------------------------------------
# 拼装
# --------------------------------------------------------------------------


def format_core_block(store, budgets=None):
    """把常驻块渲染成一段文本；内容为空时返回空串（调用方就什么都不注入）。"""
    if store is None:
        return ""
    lines = []
    for field in FIELD_ORDER:
        active = store.items(field, status="active")
        if not active:
            continue
        budget = int((budgets or {}).get(field) or store.field_budget(field))
        chosen = []
        used = 0
        for item in sorted(active, key=lambda entry: -float(entry.get("confidence") or 0.0)):
            text = _clean_text(item.get("text"))
            if not text:
                continue
            cost = len(text) + 3
            if used + cost > budget:
                continue
            chosen.append(text)
            used += cost
        if not chosen:
            continue
        lines.append(f"· {FIELD_TITLES.get(field, field)}：" + "；".join(chosen))
    if not lines:
        return ""
    return BLOCK_HEADER + "\n" + "\n".join(lines) + "\n" + BLOCK_FOOTER


# --------------------------------------------------------------------------
# 偏好提炼（LLM，后台）
# --------------------------------------------------------------------------


def _extract_prompt(lines, max_items, item_chars):
    return (
        "从下面的对话里提炼**用户长期稳定的偏好或约束**。\n"
        "只提炼长期有效的东西，例如：语言与语气偏好、回答格式偏好、工作习惯、明确禁止的做法、"
        "长期项目背景。\n"
        "**不要**提炼：临时状态、当轮任务、一次性的提问内容、助手的结论、寒暄、时间地点等易变信息。\n"
        # 实测真库里混进来过「关注检索阈值 0.7 与 0.55 的合理性」——那是**话题**不是偏好。
        # 光说"不要临时状态"拦不住它，因为话题看着一点都不"临时"。所以给一条可判定的标准。
        "**判断标准**：每一条都必须是「用户要求助手以后怎么做」——换一个话题、换一个项目它照样成立。\n"
        "**最容易写错的一类**：把「用户在关注什么话题」当成偏好。像「关注检索阈值的合理性」"
        "「在改造记忆系统」这种是**话题或当前任务**，不是偏好，不要写。\n"
        "正例：「偏好简洁回答，不喜欢表格」「讲解新概念时保留英文术语」。\n"
        "反例：「关注检索阈值」「正在改记忆系统」「这个会话在讨论 RRF」——全是话题。\n"
        f"最多 {int(max_items)} 条，每条不超过 {int(item_chars)} 个字符，用陈述句，"
        "主语尽量省略（例如「偏好简洁回答，不喜欢表格」）。\n"
        "如果新偏好与某条已有偏好冲突，在 supersedes 里原样抄下那条旧偏好。\n\n"
        "只输出 JSON 数组，不要解释、不要 Markdown 代码围栏。格式：\n"
        '[{"text": "偏好简洁回答", "confidence": 0.8, "supersedes": ""}]\n'
        "没有可提炼的内容时输出 []。\n\n"
        "对话记录：\n" + "\n".join(lines)
    )


def _parse_extraction(raw, max_items, item_chars):
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    result = []
    for entry in payload:
        if isinstance(entry, str):
            entry = {"text": entry}
        if not isinstance(entry, dict):
            continue
        body = _clean_text(entry.get("text"), item_chars)
        if not body:
            continue
        try:
            confidence = float(entry.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        result.append(
            {
                "text": body,
                "confidence": max(0.0, min(1.0, confidence)),
                "supersedes": _clean_text(entry.get("supersedes"), item_chars),
            }
        )
        if len(result) >= max_items:
            break
    return result


def _select_extract_lines(rows, total_chars=None, per_message_chars=None, head_ratio=None):
    """按「开头一小段 + 最近一大段」挑进提示词的消息。

    为什么要两头取：见 `DEFAULT_EXTRACT_HEAD_RATIO` 的注释 —— 只从头取会让新偏好
    永远进不了提示词。两头取之后，老的当背景、新的当素材，都能被看到。
    """
    total_chars = DEFAULT_EXTRACT_INPUT_CHARS if total_chars is None else total_chars
    per_message_chars = (
        DEFAULT_EXTRACT_PER_MESSAGE_CHARS if per_message_chars is None else per_message_chars
    )
    head_ratio = DEFAULT_EXTRACT_HEAD_RATIO if head_ratio is None else head_ratio

    lines = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        role = "用户" if row.get("role") == "user" else "助手"
        content = _clean_text(row.get("content"), per_message_chars)
        if not content:
            continue
        lines.append(f"{role}：{content}")
    if not lines:
        return []

    head_budget = int(total_chars * head_ratio)
    tail_budget = max(0, total_chars - head_budget)

    head = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1

    tail = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()

    if len(head) + len(tail) >= len(lines):
        return lines  # 短会话：全都装得下，不做省略
    omitted = len(lines) - len(head) - len(tail)
    return head + [f"……（此处省略 {omitted} 条较早的消息）……"] + tail


def extract_preferences(rows, config=None, field="preferences"):
    """用配置里的 chat API 提炼偏好。失败抛异常，由调用方决定降级。"""
    if config is None:
        raise RuntimeError("未提供提炼配置")
    api_key = str(getattr(config, "get_api_key", lambda: "")() or "").strip()
    base_url = str(getattr(config, "get_base_url", lambda: "")() or "").strip().rstrip("/")
    model = str(getattr(config, "get_model", lambda: "")() or "").strip()
    if not api_key or not base_url or not model:
        raise RuntimeError("偏好提炼缺少 Base URL、API Key 或模型配置")

    lines = _select_extract_lines(rows)
    if not lines:
        # 「抽出 0 条」有四种完全不同的成因，而它们原本在日志里长得一模一样：
        #   ① 输入阶段就没内容（这一支）—— 连 HTTP 都不发；
        #   ② 模型确实回了 []；
        #   ③ 模型回了非 JSON（散文/解释）；
        #   ④ JSON 被 max_tokens 截断，json.loads 失败。
        # 不打这几条痕，排查就只能靠猜 —— 这个功能就是这么挂了很久没人发现的。
        logging.info(
            "[core-memory-extract] 输入为空：%d 行候选没有一行能取出 content，未发 HTTP",
            len(rows or []),
        )
        return []

    response = http_client.session().post(
        base_url + "/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个只输出 JSON 的记忆提炼器，绝不输出解释或多余文字。",
                },
                {"role": "user", "content": _extract_prompt(lines, DEFAULT_EXTRACT_MAX_ITEMS, DEFAULT_EXTRACT_ITEM_CHARS)},
            ],
            "temperature": 0.0,
            "max_tokens": DEFAULT_EXTRACT_MAX_TOKENS,
            "stream": False,
        },
        timeout=DEFAULT_EXTRACT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"提炼接口返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError("提炼接口返回了非 JSON 内容") from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    raw = message.get("content") if isinstance(message, dict) else ""
    items = _parse_extraction(raw, DEFAULT_EXTRACT_MAX_ITEMS, DEFAULT_EXTRACT_ITEM_CHARS)
    if not items:
        # 见上面「输入为空」那条注释：0 条必须留下**原始响应**才可能定位。
        # 截断是最可疑的一支 —— max_tokens=512 对推理模型来说会被思维链吃光，
        # 留下的半截 JSON 让 json.loads 抛错，然后被 _parse_extraction 静默吞成 []。
        logging.info(
            "[core-memory-extract] 解析出 0 条：响应 %d 字符，开头 %r",
            len(str(raw or "")),
            str(raw or "")[:200],
        )
    return items


class _LlmConfigSnapshot:
    """LLM 调用所需的配置快照：QSettings 不适合跨线程读，所以在调用线程取好再交给后台。"""

    def __init__(self, base_url="", api_key="", model=""):
        self._base_url = base_url
        self._api_key = api_key
        self._model = model

    def get_base_url(self):
        return self._base_url

    def get_api_key(self):
        return self._api_key

    def get_model(self):
        return self._model


def _snapshot_llm_config(config):
    def read(name):
        getter = getattr(config, name, None) if config is not None else None
        try:
            return str(getter() or "").strip() if callable(getter) else ""
        except Exception:
            logging.warning("读取提炼配置 %s 失败", name, exc_info=True)
            return ""

    return _LlmConfigSnapshot(read("get_base_url"), read("get_api_key"), read("get_model"))


# --------------------------------------------------------------------------
# 管理器
# --------------------------------------------------------------------------


class CoreMemoryManager:
    """常驻块的对外门面：读块、按需排一次后台提炼。"""

    def __init__(self, config=None, store=None):
        self.config = config
        self.store = store or CoreMemoryStore()
        self._lock = threading.Lock()
        self._last_extract_at = 0.0
        self._last_extract_count = {}
        self._inflight = set()
        self._seeded = False

    # ---- 配置 ----------------------------------------------------------

    def _setting(self, name, default):
        getter = getattr(self.config, name, None) if self.config is not None else None
        try:
            return getter() if callable(getter) else default
        except Exception:
            logging.warning("读取常驻块配置 %s 失败", name, exc_info=True)
            return default

    def enabled(self):
        return bool(self._setting("get_core_memory_enabled", True))

    def extract_enabled(self):
        return self.enabled() and bool(self._setting("get_core_memory_extract_enabled", True))

    def min_new_messages(self):
        try:
            return max(2, int(self._setting("get_core_memory_extract_min_messages", DEFAULT_EXTRACT_MIN_NEW_MESSAGES)))
        except (TypeError, ValueError):
            return DEFAULT_EXTRACT_MIN_NEW_MESSAGES

    def cooldown_seconds(self):
        try:
            return max(60, int(self._setting("get_core_memory_extract_cooldown", DEFAULT_EXTRACT_COOLDOWN_SECONDS)))
        except (TypeError, ValueError):
            return DEFAULT_EXTRACT_COOLDOWN_SECONDS

    # ---- 读取 ----------------------------------------------------------

    def pull_enabled(self):
        """pull 通道是否开启。只读配置，不去 import 工具层（避免依赖成环）。"""
        try:
            return bool(self._setting("get_memory_pull_enabled", True))
        except Exception:
            return True

    def _capability_hint(self):
        return DEFAULT_CAPABILITY_HINT if self.pull_enabled() else LEGACY_CAPABILITY_HINT

    def _ensure_seed(self):
        """补上「能力索引」的固定说明；pull 上线后把出厂原文升级成点名工具的版本。

        只动「还是出厂原文」的那一条：用户自己写过或改过，就一律不碰。
        """
        if self._seeded:
            return
        self._seeded = True
        try:
            active = self.store.items("capabilities", status="active")
            hint = self._capability_hint()
            if not active:
                self.store.add_item("capabilities", hint, confidence=1.0)
                return
            if len(active) != 1:
                return
            current = active[0]
            if _norm_key(current.get("text")) != _norm_key(LEGACY_CAPABILITY_HINT):
                return
            if _norm_key(hint) == _norm_key(LEGACY_CAPABILITY_HINT):
                return
            # 走 supersede 而不是就地改：改动可审计，原文和来源都留着。
            self.store.supersede_item("capabilities", current.get("id"), reason="pull-upgrade")
            self.store.add_item("capabilities", hint, confidence=1.0)
        except Exception:
            logging.warning("写入默认能力索引失败", exc_info=True)

    def block(self):
        if not self.enabled():
            return ""
        self._ensure_seed()
        try:
            return format_core_block(self.store)
        except Exception:
            logging.warning("渲染常驻块失败", exc_info=True)
            return ""

    def block_version(self):
        """版本标记：给桥接侧做「同一会话不重复注入」的比对键。"""
        if not self.enabled():
            return ""
        try:
            return f"core:{self.store.version()}"
        except Exception:
            return ""

    def describe(self):
        """给日志/界面用的一句话摘要。"""
        active = {field: len(self.store.items(field, status="active")) for field in FIELD_ORDER}
        return "常驻块 v%d（偏好 %d 条 / 能力 %d 条 / 任务 %d 条）" % (
            self.store.version(),
            active.get("preferences", 0),
            active.get("capabilities", 0),
            active.get("task", 0),
        )

    # ---- 提炼 ----------------------------------------------------------

    def note_turn(self, session_id, rows):
        """攒够新消息就排一次后台提炼；不满足条件直接返回。"""
        if not self.extract_enabled():
            return False
        rows = [row for row in (rows or []) if isinstance(row, dict)]
        if not rows:
            return False
        session_key = str(session_id or "")
        total = len(rows)
        last = int(self._last_extract_count.get(session_key, 0))
        if total - last < self.min_new_messages():
            return False
        now = time.monotonic()
        with self._lock:
            if now - self._last_extract_at < self.cooldown_seconds():
                return False
            if session_key in self._inflight:
                return False
            self._inflight.add(session_key)
            self._last_extract_at = now
        snapshot = _snapshot_llm_config(self.config)
        payload = [dict(row) for row in rows]

        def _worker():
            try:
                items = extract_preferences(payload, config=snapshot)
                written = self._apply(items, session_key)
                if written:
                    logging.info("常驻块已更新：写入 %d 条偏好（%s）", written, self.describe())
                else:
                    # 没写入也必须留痕：否则"提炼跑了但一条没抽出来"和"压根没跑"
                    # 在日志里长得一模一样，排查时只能靠猜。
                    logging.info(
                        "常驻块提炼完成但无写入：抽出 %d 条（%s）",
                        len(items or []),
                        self.describe(),
                    )
            except Exception:
                logging.warning("后台偏好提炼失败，保留原常驻块", exc_info=True)
            finally:
                with self._lock:
                    self._inflight.discard(session_key)
                    self._last_extract_count[session_key] = total

        try:
            thread_registry.spawn(_worker, name="core-memory-extract", group="core_memory")
            return True
        except Exception:
            with self._lock:
                self._inflight.discard(session_key)
            logging.warning("偏好提炼线程未能启动", exc_info=True)
            return False

    def _apply(self, items, session_id):
        """把提炼结果落盘：冲突走 supersede，重复直接跳过。"""
        written = 0
        for entry in items or []:
            text = _clean_text(entry.get("text"), DEFAULT_EXTRACT_ITEM_CHARS)
            if not text:
                continue
            supersedes = _clean_text(entry.get("supersedes"), DEFAULT_EXTRACT_ITEM_CHARS)
            new_item = self.store.add_item(
                "preferences",
                text,
                source_session=session_id,
                confidence=entry.get("confidence") or 0.0,
            )
            if new_item is None:
                continue
            written += 1
            if supersedes:
                old = self.store.find_by_text("preferences", supersedes, status="active")
                if old is not None and str(old.get("id")) != str(new_item.get("id")):
                    self.store.supersede_item(
                        "preferences",
                        old.get("id"),
                        new_item_id=new_item.get("id"),
                        reason="新偏好覆盖",
                    )
        return written


_MANAGER_LOCK = threading.Lock()
_MANAGER = None


def get_core_memory(config=None):
    """进程内共享一个管理器，避免多处各读一份文件。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = CoreMemoryManager(config)
        elif config is not None and _MANAGER.config is None:
            _MANAGER.config = config
        return _MANAGER


def reset_core_memory():
    """测试钩子：丢弃缓存的管理器。"""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = None


if __name__ == "__main__":  # 手测入口：python src\core_memory.py
    import tempfile

    logging.basicConfig(level=logging.INFO)
    demo = CoreMemoryStore(os.path.join(tempfile.gettempdir(), "core_memory_demo.json"))
    demo.add_item("preferences", "偏好简洁回答，不喜欢表格", source_session="demo", confidence=0.9)
    demo.add_item("preferences", "用中文回复", source_session="demo", confidence=0.7)
    manager = CoreMemoryManager(config=None, store=demo)
    print(manager.block())
    print(manager.describe())
