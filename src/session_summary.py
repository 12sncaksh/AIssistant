"""Long-term session summary.

The summary is *not* a replacement for semantic retrieval: it is the stable
background that keeps the assistant oriented (what this conversation is about,
what the user has repeatedly asked for, what was concluded), while retrieval
recovers specific old facts on demand.

The default implementation is extractive and completely local. An optional LLM
summarizer can be enabled from the settings dialog; it reuses the configured
chat API and always falls back to the local implementation on failure.

The summary is **rolling and incremental**: every time it falls a full window
behind (``refresh_growth`` messages, 24 by default = 12 turns), the previous
summary plus only the *new* turns are merged into an updated one. The window is
deliberately the same width as the DSH history seed, so the summary boundary
always sits flush against the start of the verbatim seed window and no stretch
of conversation can fall between the two.

Because an LLM call takes seconds, the LLM path never runs on the calling
thread: it is scheduled in the background and the caller keeps using the cached
summary (or a millisecond-scale local one) until the new one lands. The
extractive path stays synchronous because it only takes milliseconds.
"""

from __future__ import annotations

import logging
import threading

import http_client
import thread_registry

DEFAULT_SUMMARY_MAX_CHARS = 1000
DEFAULT_PER_MESSAGE_CHARS = 400
DEFAULT_SAMPLE_MESSAGES = 40
# 滚动步长：24 条消息 = 12 轮，与 DSH 的 seed 窗口（dsh_seed_turns × 2）同宽。
# 同宽是有意的 —— 摘要边界因此永远紧贴 seed 起点，中间不会留缝。
DEFAULT_REFRESH_GROWTH = 24
DEFAULT_LLM_INPUT_CHARS = 30000
DEFAULT_LLM_TIMEOUT = (5, 60)
# 生成摘要时的 token 预算，与 max_chars **完全解耦**：
# max_chars 管的是"每轮附加多少字符"，token 预算管的是"模型有多少思考空间"。
# 以前写成 max(256, min(4096, max_chars // 2))，max_chars 一降到 1000 就只剩
# 500 token —— 带推理链的模型会把预算全花在思维链上，正文一个字都写不出来
# （和 core_memory 那次 DEFAULT_EXTRACT_MAX_TOKENS 的坑是同一个机理）。
# 输出长度由下面的 prompt 约束 + summary[:max_chars] 截断来保证，不靠 token 数。
DEFAULT_SUMMARY_MAX_TOKENS = 40960

DEFAULT_SUMMARY_ENABLED = True
DEFAULT_SUMMARY_LLM_ENABLED = True
DEFAULT_SUMMARY_CHARS = 1000
DEFAULT_SUMMARY_GROWTH = 24


def _sample(items, count):
    """Evenly spaced sample that always keeps the first and last element."""
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[-1]]
    step = (len(items) - 1) / (count - 1)
    picked = []
    for index in range(count):
        position = int(round(index * step))
        position = max(0, min(len(items) - 1, position))
        if not picked or picked[-1] is not items[position]:
            picked.append(items[position])
    return picked


def _clean(text, limit):
    body = " ".join(str(text or "").split())
    if limit and len(body) > limit:
        body = body[:limit] + "…"
    return body


# 摘要会被两种来源的消息行喂到，字段名不一样，必须都认：
#   · 本地 API 路径 —— 聊天库的 load_all_session_rows：{"id", "role", "content"}
#   · DSH 桥接路径 —— 记忆索引的 store.session_rows：{"message_id", "role", "text"}
# 只认其中一种的话，另一条路会静默拿到一堆空内容（摘要永远出不来）。
def _row_text(row):
    if not isinstance(row, dict):
        return ""
    value = row.get("content")
    if value is None:
        value = row.get("text")
    return value


def _row_id(row):
    if not isinstance(row, dict):
        return None
    value = row.get("id")
    if value is None:
        value = row.get("message_id")
    return value


def _dialogue_lines(rows, per_message_chars=DEFAULT_PER_MESSAGE_CHARS * 2):
    """把消息行压成"用户：… / 助手：…"，只保留有内容的部分。"""
    lines = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        content = _clean(_row_text(row), per_message_chars)
        if not content:
            continue
        role = "用户" if row.get("role") == "user" else "助手"
        lines.append(f"{role}：{content}")
    return lines


def _fit_lines(lines, budget):
    """在字符预算内取用对话行；装不下就均匀采样，**既不只留开头也不丢结尾**。

    旧实现是"从最旧一条往后累计、到预算就 break"，于是一个几万字符的会话里
    LLM 只看得到最前面几十条，后面的全丢。这里改成均匀采样（保首尾），并且
    **给最后一条单独留出位置** —— 采样之后按顺序取，仍可能刚好在结尾前用光预算，
    那正是要避免的丢尾。
    """
    lines = list(lines or [])
    budget = int(budget or 0)
    if not lines or budget <= 0:
        return []
    total = sum(len(line) + 1 for line in lines)
    if total <= budget:
        return lines

    tail = lines[-1]
    tail_cost = len(tail) + 1
    head = lines[:-1]
    head_budget = max(0, budget - tail_cost)
    head_total = sum(len(line) + 1 for line in head)
    keep = max(1, int(len(head) * head_budget / max(1, head_total)))
    out = []
    remaining = head_budget
    for line in _sample(head, min(len(head), keep)):
        if len(line) + 1 > remaining:
            break
        out.append(line)
        remaining -= len(line) + 1
    if tail_cost <= budget:
        out.append(tail)
    return out


# 摘要是给后续助手当"目录"用的：模型看到话题和关键词，才知道该去检索什么。
# 所以格式固定成话题清单，而不是散文 —— 散文在固定字数下会一代代把早期内容
# 挤掉，话题清单则是"合并/追加"，稳定得多。
SUMMARY_PROMPT_TEMPLATE = (
    "你在维护一份长期对话的「话题目录」，供后续助手判断该去检索什么。\n"
    "输出格式固定，不要写成散文：\n"
    "最初目标：<一句话>\n"
    "话题：\n"
    "- <话题关键词> —— <一句结论或进展>\n"
    "当前进展：<一句话>\n\n"
    "要求：\n"
    "1. 每条话题必须带上可检索的关键词（文件名、函数名、参数名、报错、结论）。\n"
    "2. 必须保留「最初目标」，它是整段对话的锚点。\n"
    "3. 上一版摘要里仍有价值的话题要保留；重复的合并；确实过时或无关的可以删。\n"
    "4. 话题优先于细节：宁可多列几个话题，也不要展开长篇描述。\n"
    "5. 不要编造内容，不要输出分析过程，不要使用 Markdown 代码围栏。\n"
    "6. 全文最多 {max_chars} 个字符，越精炼越好。\n"
)


def build_extractive_summary(
    rows,
    max_chars=DEFAULT_SUMMARY_MAX_CHARS,
    per_message_chars=DEFAULT_PER_MESSAGE_CHARS,
    sample_messages=DEFAULT_SAMPLE_MESSAGES,
):
    """Compose a stable background summary from ordered message rows."""
    rows = [row for row in (rows or []) if str(_row_text(row) or "").strip()]
    if not rows:
        return ""
    users = [row for row in rows if row.get("role") == "user"]
    assistants = [row for row in rows if row.get("role") == "assistant"]

    lines = [f"【会话长期背景摘要】本会话共 {len(rows)} 条消息。"]
    budget = max(0, int(max_chars))

    def add(header, items, limit):
        nonlocal budget
        if not items or budget <= 0:
            return
        block = [header]
        for item in items:
            line = "- " + _clean(_row_text(item), per_message_chars)
            if not line.strip("- "):
                continue
            if len(line) > budget:
                block.append("- …（摘要预算已满，后续内容省略）")
                break
            block.append(line)
            budget -= len(line)
        if len(block) > 1:
            lines.extend(block)

    if users:
        add("最初诉求：", users[:2], per_message_chars)
    sampled_users = _sample(users, max(2, int(sample_messages)))
    add("历次用户诉求（按时间顺序，已截断）：", sampled_users, per_message_chars)
    if len(users) > 2:
        add("最近诉求：", users[-2:], per_message_chars)
    if assistants:
        add("助手给出的关键结论（抽样，已截断）：", _sample(assistants, max(2, int(sample_messages) // 4)), per_message_chars)

    summary = "\n".join(lines).strip()
    if len(summary) > max_chars:
        summary = summary[:max_chars]
    return summary


def build_llm_summary(
    rows,
    max_chars=DEFAULT_SUMMARY_MAX_CHARS,
    config=None,
    previous="",
    previous_count=0,
):
    """增量合成：把「已有摘要」和「之后的新对话」并成一份更新后的摘要。

    `rows` 是**尚未进过摘要**的那一段（调用方按 covered 切好），不是整个会话：
    增量输入天然很小，所以既撞不上输入预算（"只看开头"的根因），成本也低得多。
    """
    if config is None:
        raise RuntimeError("未提供 LLM 摘要配置")
    api_key = str(getattr(config, "get_api_key", lambda: "")() or "").strip()
    base_url = str(getattr(config, "get_base_url", lambda: "")() or "").strip().rstrip("/")
    model = str(getattr(config, "get_model", lambda: "")() or "").strip()
    if not api_key or not base_url or not model:
        raise RuntimeError("LLM 摘要缺少 Base URL、API Key 或模型配置")

    previous = str(previous or "").strip()
    rows = list(rows or [])
    # 旧摘要本身占掉一部分输入预算，剩下的才留给新对话。
    lines = _fit_lines(_dialogue_lines(rows), DEFAULT_LLM_INPUT_CHARS - len(previous))
    if not lines:
        return previous

    total = max(0, int(previous_count or 0)) + len(rows)
    prompt = SUMMARY_PROMPT_TEMPLATE.format(max_chars=int(max_chars))
    prompt += f"\n本会话目前共 {total} 条消息。\n\n"
    if previous:
        prompt += f"已有摘要（覆盖到第 {int(previous_count or 0)} 条消息）：\n{previous}\n\n"
        prompt += "之后的新对话：\n"
    else:
        prompt += "这是本会话的第一份摘要，请从头整理。\n\n对话记录：\n"
    prompt += "\n".join(lines)
    response = http_client.session().post(
        base_url + "/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个严谨的会话摘要器，只返回摘要正文。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            # 与 max_chars 解耦：预算给足思考空间，输出长度靠 prompt + 下面的截断。
            "max_tokens": DEFAULT_SUMMARY_MAX_TOKENS,
            "stream": False,
        },
        timeout=DEFAULT_LLM_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"摘要接口返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError("摘要接口返回了非 JSON 内容") from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    summary = message.get("content") if isinstance(message, dict) else ""
    summary = str(summary or "").strip()
    if not summary:
        raise RuntimeError("摘要接口返回空内容")
    return summary[:int(max_chars)].strip()


class _LlmConfigSnapshot:
    """LLM 调用所需配置的线程安全快照。

    QSettings 不适合跨线程读取，所以在调用线程取值后把纯数据交给后台线程。
    """

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
            logging.warning("读取 LLM 摘要配置 %s 失败", name, exc_info=True)
            return ""

    return _LlmConfigSnapshot(read("get_base_url"), read("get_api_key"), read("get_model"))


class SessionSummaryManager:
    """Caches the summary in the index database and refreshes it lazily."""

    def __init__(self, store, config=None):
        self.store = store
        self.config = config
        self._local_summarizer = build_extractive_summary
        self._summarizer = None
        self._bg_lock = threading.Lock()
        self._bg_inflight = set()

    def _setting(self, name, default):
        getter = getattr(self.config, name, None) if self.config is not None else None
        try:
            return getter() if callable(getter) else default
        except Exception:
            logging.warning("读取摘要配置 %s 失败", name, exc_info=True)
            return default

    def set_summarizer(self, fn):
        """Install a custom summarizer; passing None restores built-in selection."""
        self._summarizer = fn if callable(fn) else None

    def _use_llm(self):
        return bool(self._setting("get_memory_summary_llm_enabled", DEFAULT_SUMMARY_LLM_ENABLED))

    def _async_llm(self):
        """LLM 摘要是否应该丢到后台（自定义摘要器由调用方自己决定）。"""
        return self._summarizer is None and self._use_llm()

    def _store_summary(self, session_id, summary, rows):
        """把摘要写入缓存（后台线程也会调用，所以只在 store 内做同步）。

        `rows` 必须是**整个会话**的消息列表：message_count 就是从这里来的，它要
        如实反映"这份摘要覆盖到了第几条"。增量更新时旧摘要覆盖 [0, covered)、新
        轮次覆盖 [covered, len(rows))，合起来正好是整段，所以记 len(rows) 是真的。
        """
        covered = 0
        for row in reversed(rows or []):
            value = _row_id(row)
            if value:
                covered = int(value)
                break
        try:
            self.store.set_summary(
                session_id,
                summary,
                covered_message_id=covered,
                message_count=len(rows or []),
            )
        except Exception:
            logging.warning("保存会话摘要失败 session=%s", session_id, exc_info=True)

    def _schedule_llm_summary(self, session_id, rows, pending, max_chars, previous="", previous_count=0):
        """后台增量生成 LLM 摘要；同一会话同时只跑一个。

        LLM 只看 `pending`（尚未进过摘要的那一段），本地兜底看全量 `rows`
        —— 兜底必须覆盖整段会话，否则 message_count 又会变成谎报。
        """
        with self._bg_lock:
            if session_id in self._bg_inflight:
                return False
            self._bg_inflight.add(session_id)
        # 在调用线程取好配置快照，后台线程不碰 QSettings。
        snapshot = _snapshot_llm_config(self.config)
        payload = [dict(row) for row in (rows or [])]
        new_rows = [dict(row) for row in (pending or [])]
        previous = str(previous or "")

        def _worker():
            summary = ""
            try:
                summary = build_llm_summary(
                    new_rows,
                    max_chars=max_chars,
                    config=snapshot,
                    previous=previous,
                    previous_count=previous_count,
                )
            except Exception:
                logging.warning(
                    "后台 LLM 摘要失败 session=%s，改用本地摘要兜底", session_id, exc_info=True
                )
            if not summary:
                # 兜底不能丢：LLM 不可用时仍要有一份摘要，否则该会话会永远没有长期背景。
                try:
                    summary = self._local_summarizer(payload, max_chars=max_chars)
                    if summary:
                        logging.info("已用本地摘要兜底 session=%s", session_id)
                except Exception:
                    logging.warning("本地摘要兜底失败 session=%s", session_id, exc_info=True)
            if summary:
                self._store_summary(session_id, summary, payload)
            else:
                logging.info("摘要未产出内容 session=%s，保留原摘要", session_id)
            with self._bg_lock:
                self._bg_inflight.discard(session_id)

        try:
            thread_registry.spawn(
                _worker, name="memory-summary", group="memory_summary"
            )
            return True
        except Exception:
            with self._bg_lock:
                self._bg_inflight.discard(session_id)
            logging.warning("后台摘要线程未能启动 session=%s", session_id, exc_info=True)
            return False

    def _build_summary(self, rows, pending, max_chars, previous="", previous_count=0):
        """同步生成：LLM 走增量，自定义摘要器与本地兜底走全量。"""
        if self._summarizer is not None:
            return self._summarizer(rows or [], max_chars=max_chars)
        if self._use_llm():
            try:
                return build_llm_summary(
                    pending or [],
                    max_chars=max_chars,
                    config=self.config,
                    previous=previous,
                    previous_count=previous_count,
                )
            except Exception:
                logging.warning("LLM 摘要失败，回退到本地摘要", exc_info=True)
        return self._local_summarizer(rows or [], max_chars=max_chars)

    def _local_fallback(self, rows, max_chars):
        """后台摘要还没落地时先给一份毫秒级的本地摘要，别让这一轮空着。"""
        try:
            return self._local_summarizer(rows or [], max_chars=max_chars) or ""
        except Exception:
            logging.warning("本地摘要兜底失败", exc_info=True)
            return ""

    def enabled(self):
        return bool(self._setting("get_memory_summary_enabled", DEFAULT_SUMMARY_ENABLED))

    def max_chars(self):
        try:
            return int(self._setting("get_memory_summary_max_chars", DEFAULT_SUMMARY_CHARS))
        except (TypeError, ValueError):
            return DEFAULT_SUMMARY_CHARS

    def refresh_growth(self):
        try:
            return max(1, int(self._setting("get_memory_summary_refresh_growth", DEFAULT_SUMMARY_GROWTH)))
        except (TypeError, ValueError):
            return DEFAULT_SUMMARY_GROWTH

    def invalidate(self, session_id):
        try:
            self.store.set_summary(session_id, "", 0, 0)
        except Exception:
            logging.warning("清理会话摘要失败 session=%s", session_id, exc_info=True)

    def summary_for(self, session_id, rows, force=False):
        """返回会话摘要；落后整整一个窗口时滚动更新一次。

        滚动规则（和 DSH 的固定轮次补给严格对齐）：

        - 步长 = refresh_growth（默认 24 条 = 12 轮），正好等于 seed 窗口宽度，
          所以"摘要的边界"永远紧贴"seed 的起点"，中间不会留缝；
        - **首次也走同一个门槛**：攒够一个窗口才生成第一份，而不是第一轮就生成
          一份几乎空的摘要；
        - covered（就是 message_count）按会话落库，会话中断重建后接着算，不重置。

        绝大多数轮次走"直接返回缓存"，零计算；只有落后够多才更新。LLM 摘要在
        后台生成，本轮先给缓存（没有缓存时给一份毫秒级的本地摘要），生成完成后
        下一轮生效 —— 发送消息永远不会被摘要调用卡住。
        """
        if not self.enabled():
            return ""
        session_id = str(session_id or "")
        if not session_id:
            return ""
        rows = list(rows or [])
        info = None
        if not force:
            try:
                info = self.store.get_summary(session_id)
            except Exception:
                logging.warning("读取会话摘要失败 session=%s", session_id, exc_info=True)
        stale = str(info.get("summary") or "") if info else ""
        if not rows:
            # 没有消息可依据时，缓存仍然是最佳答案，别把它当成"覆盖数不符"丢掉。
            return stale
        try:
            covered = int(info.get("message_count") or 0) if info else 0
        except (TypeError, ValueError):
            covered = 0
        if covered < 0 or covered > len(rows):
            # 覆盖数比现有消息还多：历史被清理或改写过了，退化成全量重建。
            logging.info(
                "会话摘要覆盖数与消息数不符（covered=%s rows=%s），改为全量重建 session=%s",
                covered,
                len(rows),
                session_id,
            )
            covered = 0
            stale = ""
        if not force and (len(rows) - covered) < self.refresh_growth():
            return stale

        pending = [
            row
            for row in rows[covered:]
            if isinstance(row, dict) and str(_row_text(row) or "").strip()
        ]
        if not pending:
            return stale

        max_chars = self.max_chars()
        if self._async_llm():
            if self._schedule_llm_summary(
                session_id,
                rows,
                pending,
                max_chars,
                previous=stale,
                previous_count=covered,
            ):
                logging.info(
                    "LLM 摘要已排入后台 session=%s（新增 %d 条，本轮先用现有摘要）",
                    session_id,
                    len(pending),
                )
            if stale:
                return stale
            # 后台那份下一轮才生效；这一轮先给一份本地兜底，有总比空着好。
            return self._local_fallback(rows, max_chars)

        summary = ""
        try:
            summary = self._build_summary(
                rows, pending, max_chars, previous=stale, previous_count=covered
            )
        except Exception:
            logging.warning("生成会话摘要失败 session=%s", session_id, exc_info=True)
            return stale
        if summary:
            self._store_summary(session_id, summary, rows)
        return summary
