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

# 字段名 -> 默认字符预算。模型维护的三栏总量刻意压在 1.3k 字符以内：
# 它是每一轮都在烧的预算，贵。
#
# `custom` 是**用户手写**的一栏，0 表示**不限制**：用户想写多少写多少，和系统提示词
# 一个待遇。它不参与提炼、不参与淘汰、条目内部也不截断 —— 用户亲口写的东西，
# 程序不该替它做主。
DEFAULT_FIELD_BUDGETS = {
    "custom": 0,
    "preferences": 600,
    "capabilities": 300,
    "task": 400,
}

# 两栏偏好必须**分开**，因为生命周期完全不同：
#   custom      —— 用户手写，永不改写、永不淘汰、永不截断
#   preferences —— 模型提炼，滚动重写（合并 + 淘汰）
#
# 混在一栏会出两个问题：① 压缩会把用户手写的条目一起"合并同类项"，等于程序改用户的话；
# ② 模型分不清"用户要求"和"助手猜的"——两样东西并排躺着、语气一样、权威性也一样。
# 标题里把权威性写死，模型才知道冲突时听谁的。
FIELD_TITLES = {
    "custom": "用户自定义（以此为准）",
    "preferences": "模型提炼（供参考，与上面冲突时以用户自定义为准）",
    "capabilities": "能力索引",
    "task": "当前任务",
}

# 界面上的标签页标题：块内标题要写清权威性（模型才知道冲突时听谁的），
# 标签页只需要短 —— 上面那两串在 tab 上会挤成两行。
FIELD_TAB_TITLES = {
    "custom": "用户自定义",
    "preferences": "模型提炼",
    "capabilities": "能力索引",
    "task": "当前任务",
}

FIELD_ORDER = ("custom", "preferences", "capabilities", "task")

# 预算 0 = 不限制。渲染和写入两侧都要认这个值。
UNLIMITED_BUDGET = 0

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

# 模型提炼那一栏用**滚动重写**维护：每次提炼 = [最近对话] + [旧清单] → 重新产出整份清单。
#
# 为什么不是"追加，满了再压缩"：
#   真库实测 v68：偏好栏 57 条 / 1217 字符，预算 600。format_core_block 按置信度挑到
#   装不下为止，所以每轮只有 26 条进得了上下文，**另外 31 条每轮都被丢掉**，不留痕迹。
#   根子是 add_item 只做**精确**去重（_norm_key 去空白和标点），近义重复照样入库：
#   「偏好用中文交流」/「偏好中文交流」、「不要用流式讲解」/「不要用流式输出讲解」、
#   「公开代码时绝不泄露…」/「公开代码时严禁泄露…」都是两条。攒到 57 条之后，一半进不了
#   上下文，而那半个还未必是冗余的那半个 —— 只有一条的独有偏好照样被丢掉。
#
#   更致命的是：追加模式下**没有任何"最近还提到过"的痕迹**。一条偏好被反复重申，
#   add_item 命中去重后直接 return，什么都不记 —— 于是"该淘汰谁"根本无从判断。
#   而这一栏是**跨对话收集**的（实测 57 条来自 3 个会话），不淘汰就一定会撑爆。
#
# 滚动重写一次解决四件事：去重、淘汰、预算、近期性。代价是模型一次生成坏结果可能整栏
# 丢失，所以护栏必须有：输出为空 / 解析失败 → 不落盘，保留原样。
# 淘汰**不是删除**：走 supersede，原文与来源留在文件里，界面勾"显示已失效"可查。
#
# 目标留 10% 余量，免得下次新增一条就立刻又溢出。
DEFAULT_REWRITE_TARGET_RATIO = 0.9
# 整份清单的条数上限（防模型抽风吐一百条）。
DEFAULT_REWRITE_MAX_ITEMS = 60
# 和提炼同一个坑：预算给足思考空间，正文才写得出来（见 DEFAULT_EXTRACT_MAX_TOKENS）。
DEFAULT_REWRITE_MAX_TOKENS = 40960
DEFAULT_REWRITE_TIMEOUT = (5, 60)
DEFAULT_REWRITE_TRIES = 1

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
                budget = int(raw.get("budget_chars"))
            except (TypeError, ValueError):
                budget = default
            if budget != UNLIMITED_BUDGET:
                budget = max(60, budget)
            result["fields"][name] = {
                "budget_chars": budget,
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
        created_at = str(item.get("created_at") or "")
        try:
            seen_count = max(0, int(item.get("seen_count")))
        except (TypeError, ValueError):
            seen_count = 0
        return {
            "id": str(item.get("id") or uuid.uuid4().hex[:12]),
            "text": text,
            "status": status,
            "confidence": max(0.0, min(1.0, confidence)),
            # 用户手写：永不改写、永不淘汰、永不截断。
            "locked": bool(item.get("locked")),
            "source_session": str(item.get("source_session") or ""),
            "source_message_id": item.get("source_message_id"),
            "created_at": created_at,
            "updated_at": str(item.get("updated_at") or ""),
            # 「上一次被重申」——滚动重写决定淘汰谁的唯一客观依据。老数据没这个字段，
            # 退回 created_at（至少能排出先后，比空着强）。
            "last_seen_at": str(item.get("last_seen_at") or created_at or ""),
            "seen_count": seen_count,
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
        """这一栏的字符预算。**0 表示不限制**，所以不能用 `or` 兜底 —— 0 是合法值。"""
        with self._lock:
            bucket = (self._data.get("fields") or {}).get(field) or {}
            raw = bucket.get("budget_chars")
            if raw is None:
                return DEFAULT_FIELD_BUDGETS.get(field, 300)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return DEFAULT_FIELD_BUDGETS.get(field, 300)

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    # ---- 变更 ----------------------------------------------------------

    def add_item(
        self,
        field,
        text,
        source_session="",
        source_message_id=None,
        confidence=0.0,
        locked=False,
    ):
        """新增一条。文本重复（归一化后相同）时返回已有条目，不重复写。

        `locked=True` 是**用户手写**的条目：条目内部不截断。模型提炼的条目仍然截到
        60 字符（那是 prompt 约束的兜底），但用户写的话不该被程序砍掉半句。
        """
        field = str(field or "").strip()
        if field not in DEFAULT_FIELD_BUDGETS:
            return None
        text = _clean_text(text, None if locked else DEFAULT_EXTRACT_ITEM_CHARS)
        if not text:
            return None
        with self._lock:
            existing = self.find_by_text(field, text)
            if existing is not None:
                return existing
            now = _now_iso()
            item = {
                "id": uuid.uuid4().hex[:12],
                "text": text,
                "status": "active",
                "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
                "locked": bool(locked),
                "source_session": str(source_session or ""),
                "source_message_id": source_message_id,
                "created_at": now,
                "updated_at": now,
                "last_seen_at": now,
                "seen_count": 1,
                "superseded_by": None,
            }
            self._data["fields"][field]["items"].append(item)
            self._append_history("add", field, item["id"], text)
            self._bump()
            self._save()
            return dict(item)

    def touch_item(self, field, item_id, confidence=None):
        """标记「这一条本轮又被提到了」：刷 last_seen_at、seen_count +1。

        滚动重写要淘汰谁，全靠这个字段。没有它，「反复被强调」和「提过一次就忘了」
        在库里长得一模一样 —— 原来 add_item 命中去重后直接 return，什么都不记。
        """
        field = str(field or "").strip()
        with self._lock:
            bucket = (self._data.get("fields") or {}).get(field)
            if not bucket:
                return False
            for item in bucket.get("items") or []:
                if str(item.get("id")) != str(item_id):
                    continue
                if item.get("status") != "active":
                    return False
                item["last_seen_at"] = _now_iso()
                item["seen_count"] = int(item.get("seen_count") or 0) + 1
                item["updated_at"] = item["last_seen_at"]
                if confidence is not None:
                    try:
                        item["confidence"] = max(0.0, min(1.0, float(confidence)))
                    except (TypeError, ValueError):
                        pass
                self._bump()
                self._save()
                return True
            return False

    def supersede_item(self, field, item_id, new_item_id=None, reason="", allow_locked=False):
        """把一条标记为失效（保留原文与来源，供审计），不删除。

        用户手写的条目（locked）**默认拒绝**：模型提炼出来的猜测，不该有权推翻用户
        亲口写下的要求。确实需要覆盖用户条目时，调用方必须显式传 allow_locked=True。
        """
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
                if item.get("locked") and not allow_locked:
                    logging.info("拒绝覆盖用户手写条目（locked）：%s", item.get("text"))
                    return False
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
        """设置这一栏的字符预算。**0 表示不限制**；其余值最小 60。"""
        field = str(field or "").strip()
        if field not in DEFAULT_FIELD_BUDGETS:
            return False
        try:
            value = int(budget_chars)
        except (TypeError, ValueError):
            return False
        if value != UNLIMITED_BUDGET:
            value = max(60, value)
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
    """把常驻块渲染成一段文本；内容为空时返回空串（调用方就什么都不注入）。

    预算 0（`custom` 栏）表示不限制：用户手写的东西不参与"装不下就丢"。

    正常路径下**一条都不会丢** —— 模型提炼那一栏由滚动重写保证装得下。真到了装不下的
    地步（比如用户把预算调小了），也会在行尾写明丢了几条，不再像原来那样静默跳过：
    原来那里是 `continue`，31 条偏好每轮消失，日志里一个字都没有。
    """
    if store is None:
        return ""
    overrides = budgets or {}
    lines = []
    for field in FIELD_ORDER:
        active = store.items(field, status="active")
        if not active:
            continue
        raw_budget = overrides.get(field)
        budget = store.field_budget(field) if raw_budget is None else int(raw_budget)
        chosen = []
        dropped = 0
        used = 0
        for item in sorted(active, key=lambda entry: -float(entry.get("confidence") or 0.0)):
            text = _clean_text(item.get("text"))
            if not text:
                continue
            cost = len(text) + 3
            if budget != UNLIMITED_BUDGET and used + cost > budget:
                dropped += 1
                continue
            chosen.append(text)
            used += cost
        if not chosen:
            continue
        line = f"· {FIELD_TITLES.get(field, field)}：" + "；".join(chosen)
        if dropped:
            line += f"；（另有 {dropped} 条未展开）"
        lines.append(line)
    if not lines:
        return ""
    return BLOCK_HEADER + "\n" + "\n".join(lines) + "\n" + BLOCK_FOOTER


# --------------------------------------------------------------------------
# 偏好提炼（LLM，后台）
# --------------------------------------------------------------------------


REWRITE_SYSTEM_PROMPT = "你是一个只输出 JSON 的记忆维护器，绝不输出解释或多余文字。"


def _items_usage(items):
    """这批条目渲染出来要占多少字符 —— 和 format_core_block 用同一套算法。"""
    return sum(len(_clean_text(entry.get("text"))) + 3 for entry in items or [])


def _rewrite_prompt(lines, existing, budget, target, item_chars):
    """滚动重写的提示词：[最近对话] + [旧清单] → 整份新清单。

    `existing` 是 [(文本, 上次重申时间)]。把时间喂进去，模型才有依据判断"这条是老任务的
    残留"还是"这条一直在被重申"——否则它只能瞎猜，而这一栏的淘汰就是瞎删。
    """
    known = ""
    if existing:
        rows = "\n".join(
            f"- {text}（上次重申 {stamp or '未知'}）" for text, stamp in existing
        )
        known = f"**现有清单**（共 {len(existing)} 条）：\n{rows}\n\n"
    return (
        "维护**用户长期稳定的偏好清单**。\n"
        "只保留长期有效的东西，例如：语言与语气偏好、回答格式偏好、工作习惯、明确禁止的做法、"
        "长期项目背景。\n"
        "**不要**保留：临时状态、当轮任务、一次性的提问内容、助手的结论、寒暄、时间地点等易变信息。\n"
        # 实测真库里混进来过「关注检索阈值 0.7 与 0.55 的合理性」——那是**话题**不是偏好。
        # 光说"不要临时状态"拦不住它，因为话题看着一点都不"临时"。所以给一条可判定的标准。
        "**判断标准**：每一条都必须是「用户要求助手以后怎么做」——换一个话题、换一个项目它照样成立。\n"
        "**最容易写错的一类**：把「用户在关注什么话题」当成偏好。像「关注检索阈值的合理性」"
        "「在改造记忆系统」这种是**话题或当前任务**，不是偏好，不要写。\n"
        "正例：「偏好简洁回答，不喜欢表格」「讲解新概念时保留英文术语」。\n"
        "反例：「关注检索阈值」「正在改记忆系统」「这个会话在讨论 RRF」——全是话题。\n\n"
        + known
        + "**你要输出的是整份新清单，不是新增项**：\n"
        "1. 现有清单里**依然成立**的条目，**原文照抄**保留 —— 不要改写措辞，改了会丢追踪。\n"
        "2. 本轮对话里**重申过**的条目，reaffirmed 标 true；没重申的标 false。\n"
        "3. 意思重复或高度重叠的条目，**合并成一条**。\n"
        "4. 已经过时、被用户推翻、或明显只是旧任务残留的条目，**直接不写进结果**（等于淘汰）。\n"
        "5. **拿不准的一律保留**：淘汰错了比留着占字符更糟。只在有明确理由时才淘汰。\n"
        f"6. 每条不超过 {int(item_chars)} 个字符，用陈述句，主语尽量省略。\n"
        f"7. 整份清单不超过 {int(target)} 个字符（硬上限 {int(budget)}）。\n\n"
        "只输出 JSON 数组，不要解释、不要 Markdown 代码围栏。格式：\n"
        '[{"text": "偏好简洁回答", "confidence": 0.8, "reaffirmed": true}]\n'
        "现有清单和对话里都没有可提炼的内容时输出 []。\n\n"
        "对话记录：\n" + "\n".join(lines)
    )


def _tighten_prompt(items, budget, target):
    """打回重写：清单还是太长，让模型再合并一轮。

    输入只有清单本身（几百字符），**不带对话** —— 重发一遍 8000 字符的对话既贵又没用，
    要压的是清单，不是对话。
    """
    rows = "\n".join(f"- {_clean_text(entry.get('text'))}" for entry in items or [])
    return (
        f"下面这份偏好清单还是太长，超过了 {int(budget)} 个字符的硬上限。\n"
        f"请**进一步合并压缩到 {int(target)} 个字符以内**，只输出 JSON 数组：\n"
        "1. 继续合并意思重叠的条目，能并的都并。\n"
        "2. 不许新增：每一条都要能对应回下面的原文。\n"
        f"3. 每条不超过 {DEFAULT_EXTRACT_ITEM_CHARS} 个字符。\n"
        "4. 保持原有的 reaffirmed 值不变。\n"
        "5. 不要解释，不要 Markdown 代码围栏。\n\n"
        '[{"text": "偏好简洁回答", "confidence": 0.8, "reaffirmed": false}]\n\n'
        "清单：\n" + rows
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
                # 滚动重写专用：这一条在本轮对话里有没有被重申。用来刷 last_seen_at。
                "reaffirmed": bool(entry.get("reaffirmed")),
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


def _request_chat(prompt, config, system, max_tokens, timeout, what="提炼"):
    """一次 chat/completions 调用，返回正文文本。失败抛异常，由调用方决定降级。"""
    if config is None:
        raise RuntimeError(f"未提供{what}配置")
    api_key = str(getattr(config, "get_api_key", lambda: "")() or "").strip()
    base_url = str(getattr(config, "get_base_url", lambda: "")() or "").strip().rstrip("/")
    model = str(getattr(config, "get_model", lambda: "")() or "").strip()
    if not api_key or not base_url or not model:
        raise RuntimeError(f"{what}缺少 Base URL、API Key 或模型配置")
    response = http_client.session().post(
        base_url + "/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "stream": False,
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"{what}接口返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{what}接口返回了非 JSON 内容") from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    return message.get("content") if isinstance(message, dict) else ""


def rewrite_preferences(rows, config=None, existing=None, field="preferences"):
    """用 [最近对话] + [旧清单] 重新产出整份偏好清单。

    返回 (items, over_budget)。over_budget=True 表示打回重写之后还是装不下，
    由调用方决定怎么裁 —— 这里不替它做决定。

    「产出 0 条」**不是失败**，是合法结果（这段对话确实没有可维护的偏好），
    由调用方的护栏处理：不落盘，保留原清单。
    """
    lines = _select_extract_lines(rows)
    if not lines:
        # 「产出 0 条」有四种完全不同的成因，而它们原本在日志里长得一模一样：
        #   ① 输入阶段就没内容（这一支）—— 连 HTTP 都不发；
        #   ② 模型确实回了 []；
        #   ③ 模型回了非 JSON（散文/解释）；
        #   ④ JSON 被 max_tokens 截断，json.loads 失败。
        # 不打这几条痕，排查就只能靠猜 —— 这个功能就是这么挂了很久没人发现的。
        logging.info(
            "[core-memory-rewrite] 输入为空：%d 行候选没有一行能取出 content，未发 HTTP",
            len(rows or []),
        )
        return [], False

    budget = int(DEFAULT_FIELD_BUDGETS.get(field, 600))
    target = int(budget * DEFAULT_REWRITE_TARGET_RATIO)
    prompt = _rewrite_prompt(lines, existing or [], budget, target, DEFAULT_EXTRACT_ITEM_CHARS)
    raw = _request_chat(
        prompt,
        config,
        REWRITE_SYSTEM_PROMPT,
        DEFAULT_REWRITE_MAX_TOKENS,
        DEFAULT_REWRITE_TIMEOUT,
    )
    items = _parse_extraction(raw, DEFAULT_REWRITE_MAX_ITEMS, DEFAULT_EXTRACT_ITEM_CHARS)
    if not items:
        # 0 条必须留下**原始响应**才可能定位。截断是最可疑的一支：思维链是**先花掉的**，
        # 预算只够推理时正文一个字都写不出来，半截 JSON 让 json.loads 抛错，
        # 然后被 _parse_extraction 静默吞成 []（见 DEFAULT_EXTRACT_MAX_TOKENS）。
        logging.info(
            "[core-memory-rewrite] 解析出 0 条：响应 %d 字符，开头 %r",
            len(str(raw or "")),
            str(raw or "")[:200],
        )
        return [], False

    used = _items_usage(items)
    for _ in range(DEFAULT_REWRITE_TRIES):
        if used <= budget:
            break
        raw = _request_chat(
            _tighten_prompt(items, budget, target),
            config,
            REWRITE_SYSTEM_PROMPT,
            DEFAULT_REWRITE_MAX_TOKENS,
            DEFAULT_REWRITE_TIMEOUT,
            what="清单压缩",
        )
        tighter = _parse_extraction(raw, DEFAULT_REWRITE_MAX_ITEMS, DEFAULT_EXTRACT_ITEM_CHARS)
        if not tighter:
            break
        items, used = tighter, _items_usage(tighter)
    return items, used > budget


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
        return "常驻块 v%d（自定义 %d 条 / 提炼 %d 条 / 能力 %d 条 / 任务 %d 条）" % (
            self.store.version(),
            active.get("custom", 0),
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
                self.run_rewrite(payload, snapshot, session_key)
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

    def run_rewrite(self, rows, config, session_id=""):
        """一次滚动重写的完整流程：读旧清单 → 让模型重写 → 落盘。

        从 `note_turn` 的线程体里抽出来，是为了**能同步测** —— 跑在线程里的代码没法在
        测试里等结果，而这一段恰恰最需要钉住：淘汰逻辑写错会真的丢用户的偏好。

        返回 (保留条数, 新增条数, 淘汰的原文列表)。
        """
        existing = [
            (_clean_text(item.get("text")), str(item.get("last_seen_at") or ""))
            for item in self.store.items("preferences", status="active")
        ]
        items, over_budget = rewrite_preferences(rows, config=config, existing=existing)
        if not items:
            # 没落盘也必须留痕：否则"跑了但一条没产出"和"压根没跑"
            # 在日志里长得一模一样，排查时只能靠猜。
            logging.info("常驻块滚动重写未落盘（产出 0 条，保留原清单）：%s", self.describe())
            return 0, 0, []
        kept, added, dropped = self._apply_rewrite(items, session_id, over_budget)
        logging.info(
            "常驻块滚动重写：产出 %d 条 / 保留 %d 条 / 新增 %d 条 / 淘汰 %d 条（%s）",
            len(items),
            kept,
            added,
            len(dropped),
            self.describe(),
        )
        return kept, added, dropped

    def _apply_rewrite(self, items, session_id, over_budget=False):
        """把滚动重写的结果落盘：保留的刷「又被提到」，没保留的走 supersede 淘汰。

        淘汰**不是删除**：原文、来源、时间都留在文件里，界面勾"显示已失效"可查。

        返回 (保留条数, 新增条数, 淘汰的原文列表)。
        """
        field = "preferences"
        budget = int(self.store.field_budget(field))
        if over_budget and budget != UNLIMITED_BUDGET:
            # 打回重写之后还是装不下：按置信度裁到装得下为止。
            # 这一步仍然不是"静默丢" —— 淘汰写进历史、原文留着、而且有日志。
            items, overflow = self._fit_items(items, budget)
            logging.warning(
                "常驻块重写后仍超预算，按置信度裁到 %d 字符：再丢 %d 条（%s）",
                budget,
                len(overflow),
                " / ".join(overflow[:4]) + ("…" if len(overflow) > 4 else ""),
            )

        old = self.store.items(field, status="active")
        by_key = {}
        for item in old:
            by_key.setdefault(_norm_key(item.get("text")), item)

        kept_ids = set()
        added = 0
        for entry in items or []:
            text = _clean_text(entry.get("text"), DEFAULT_EXTRACT_ITEM_CHARS)
            if not text:
                continue
            existing = by_key.get(_norm_key(text))
            if existing is not None:
                kept_ids.add(str(existing.get("id")))
                # 本轮被重申过才刷时间；没重申的保持原样 —— 那正是"这条最近没被提到"
                # 的信号，也是下一轮淘汰的依据。
                if entry.get("reaffirmed"):
                    self.store.touch_item(
                        field, existing.get("id"), confidence=entry.get("confidence")
                    )
                continue
            new_item = self.store.add_item(
                field,
                text,
                source_session=session_id,
                confidence=entry.get("confidence") or 0.0,
            )
            if new_item is not None:
                kept_ids.add(str(new_item.get("id")))
                added += 1

        dropped = []
        for item in old:
            if str(item.get("id")) in kept_ids:
                continue
            if item.get("locked"):
                continue  # 用户手写的条目永远不淘汰
            if self.store.supersede_item(field, item.get("id"), reason="提炼淘汰"):
                dropped.append(_clean_text(item.get("text")))
        if dropped:
            logging.info(
                "常驻块淘汰 %d 条（原文保留，界面可查）：%s",
                len(dropped),
                " / ".join(dropped[:6]) + ("…" if len(dropped) > 6 else ""),
            )
        return len(kept_ids), added, dropped

    @staticmethod
    def _fit_items(items, budget):
        """按置信度保留到装得下为止。返回 (保留的条目, 被丢掉的原文列表)。"""
        chosen = []
        overflow = []
        used = 0
        for entry in sorted(items or [], key=lambda item: -float(item.get("confidence") or 0.0)):
            text = _clean_text(entry.get("text"), DEFAULT_EXTRACT_ITEM_CHARS)
            if not text:
                continue
            cost = len(text) + 3
            if used + cost > budget:
                overflow.append(text)
                continue
            chosen.append(entry)
            used += cost
        return chosen, overflow

    # ---- 水位 ----------------------------------------------------------

    def _field_usage(self, field):
        """这一栏渲染出来要占多少字符 —— 和 format_core_block 用同一套算法。"""
        texts = []
        for item in self.store.items(field, status="active"):
            text = _clean_text(item.get("text"))
            if text:
                texts.append(text)
        return texts, sum(len(text) + 3 for text in texts)


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
