"""Long-term session summary.

The summary is *not* a replacement for semantic retrieval: it is the stable
background that keeps the assistant oriented (what this conversation is about,
what the user has repeatedly asked for, what was concluded), while retrieval
recovers specific old facts on demand.

The default implementation is extractive and completely local. An optional LLM
summarizer can be enabled from the settings dialog; it reuses the configured
chat API and always falls back to the local implementation on failure.

Because an LLM call takes seconds, the LLM path never runs on the calling
thread: it is scheduled in the background and the caller keeps using the cached
summary (or none) until the new one lands. The extractive path stays synchronous
because it only takes milliseconds.
"""

from __future__ import annotations

import logging
import threading

import http_client
import thread_registry

DEFAULT_SUMMARY_MAX_CHARS = 6000
DEFAULT_PER_MESSAGE_CHARS = 400
DEFAULT_SAMPLE_MESSAGES = 40
DEFAULT_REFRESH_GROWTH = 40
DEFAULT_LLM_INPUT_CHARS = 30000
DEFAULT_LLM_TIMEOUT = (5, 60)

DEFAULT_SUMMARY_ENABLED = True
DEFAULT_SUMMARY_LLM_ENABLED = False
DEFAULT_SUMMARY_CHARS = 6000
DEFAULT_SUMMARY_GROWTH = 40


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


def build_extractive_summary(
    rows,
    max_chars=DEFAULT_SUMMARY_MAX_CHARS,
    per_message_chars=DEFAULT_PER_MESSAGE_CHARS,
    sample_messages=DEFAULT_SAMPLE_MESSAGES,
):
    """Compose a stable background summary from ordered message rows."""
    rows = [row for row in (rows or []) if str(row.get("content") or "").strip()]
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
            line = "- " + _clean(item.get("content"), per_message_chars)
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


def build_llm_summary(rows, max_chars=DEFAULT_SUMMARY_MAX_CHARS, config=None):
    """Summarize sampled history through the configured chat-completions API."""
    if config is None:
        raise RuntimeError("未提供 LLM 摘要配置")
    api_key = str(getattr(config, "get_api_key", lambda: "")() or "").strip()
    base_url = str(getattr(config, "get_base_url", lambda: "")() or "").strip().rstrip("/")
    model = str(getattr(config, "get_model", lambda: "")() or "").strip()
    if not api_key or not base_url or not model:
        raise RuntimeError("LLM 摘要缺少 Base URL、API Key 或模型配置")

    lines = []
    remaining = DEFAULT_LLM_INPUT_CHARS
    for row in rows or []:
        role = "用户" if row.get("role") == "user" else "助手"
        content = _clean(row.get("content"), DEFAULT_PER_MESSAGE_CHARS * 2)
        line = f"{role}：{content}"
        if len(line) > remaining:
            break
        lines.append(line)
        remaining -= len(line) + 1
    if not lines:
        return ""

    prompt = (
        "请为下面的长期对话生成一份供后续助手参考的中文摘要。\n"
        "保留用户的目标、已确认的事实、关键技术决策、未完成事项和重要约束；"
        "不要编造内容，不要输出分析过程，不要使用 Markdown 代码围栏。"
        f"摘要最多 {int(max_chars)} 个字符。\n\n对话记录：\n" + "\n".join(lines)
    )
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
            "max_tokens": max(256, min(4096, int(max_chars) // 2)),
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
        """把摘要写入缓存（后台线程也会调用，所以只在 store 内做同步）。"""
        covered = 0
        for row in reversed(rows or []):
            if row.get("id"):
                covered = int(row["id"])
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

    def _schedule_llm_summary(self, session_id, rows, max_chars):
        """后台生成 LLM 摘要；同一会话同时只跑一个。"""
        with self._bg_lock:
            if session_id in self._bg_inflight:
                return False
            self._bg_inflight.add(session_id)
        # 在调用线程取好配置快照，后台线程不碰 QSettings。
        snapshot = _snapshot_llm_config(self.config)
        payload = [dict(row) for row in (rows or [])]

        def _worker():
            summary = ""
            try:
                summary = build_llm_summary(payload, max_chars=max_chars, config=snapshot)
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

    def _build_summary(self, rows, max_chars):
        if self._summarizer is not None:
            return self._summarizer(rows or [], max_chars=max_chars)
        if self._use_llm():
            try:
                return build_llm_summary(rows or [], max_chars=max_chars, config=self.config)
            except Exception:
                logging.warning("LLM 摘要失败，回退到本地摘要", exc_info=True)
        return self._local_summarizer(rows or [], max_chars=max_chars)

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
        """Return the cached summary, rebuilding it when it fell too far behind.

        LLM 摘要在后台生成：本轮先返回缓存内容（可能为空），生成完成后下一轮
        生效。这样发送消息永远不会因为摘要调用而卡住界面。
        """
        if not self.enabled():
            return ""
        session_id = str(session_id or "")
        if not session_id:
            return ""
        info = None
        if not force:
            try:
                info = self.store.get_summary(session_id)
            except Exception:
                logging.warning("读取会话摘要失败 session=%s", session_id, exc_info=True)
        stale = str(info.get("summary") or "") if info else ""
        if stale.strip():
            grown = len(rows or []) - int(info.get("message_count") or 0)
            if grown < self.refresh_growth():
                return stale

        if self._async_llm():
            if self._schedule_llm_summary(session_id, rows, self.max_chars()):
                logging.info("LLM 摘要已排入后台，本轮先用缓存内容 session=%s", session_id)
            return stale

        summary = ""
        try:
            summary = self._build_summary(rows or [], max_chars=self.max_chars())
        except Exception:
            logging.warning("生成会话摘要失败 session=%s", session_id, exc_info=True)
            return stale
        if summary:
            self._store_summary(session_id, summary, rows)
        return summary
