"""Facade that both the GUI and the DSH worker use to reach local memory.

Everything expensive (embedding the query, rebuilding the summary) happens
here, behind generous defaults and a very high safety ceiling. When anything
in the memory layer is unavailable the caller silently falls back to plain
recent-turns context, so chat never breaks because of memory.
"""

from __future__ import annotations

import logging
import threading

from context_builder import (
    DEFAULT_CONTEXT_MAX_CHARS,
    DEFAULT_RECENT_MESSAGES,
    build_context,
    recent_dialogue,
    split_history,
)
from core_memory import get_core_memory
from memory_embeddings import get_embedding_service, reset_embedding_service
from memory_indexer import MemoryIndexer
from memory_retriever import (
    DEFAULT_GROUP_RADIUS,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_GROUPS,
    DEFAULT_MIN_SCORE,
    DEFAULT_QUERY_TURNS,
    DEFAULT_TOP_K,
    build_query_text,
    format_memory_block,
    retrieve,
)
from memory_store import ACTIVE_SESSION_META_KEY, MemoryIndexStore
from session_summary import SessionSummaryManager

DEFAULT_RETRIEVAL_ENABLED = True

# pull 通道（search_memory 工具）默认开启。
#
# 为什么：真库实测（880 条 / bge-m3）显示 push 的两道门是坏的 —— 0.7 相对门在
# 真库上 16 组参数扫下来完全不生效（候选全挤在最高分的 80% 以内），真正管事的
# 0.55 一边误杀正解（低于门槛的命中连窗带邻整组消失），一边放过句式相近的噪声。
# 噪声分和正解分是反着来的，所以这不是调参能修的问题，要把裁判换掉。
# 关掉这个开关即回到改动前的纯 push 行为。
DEFAULT_PULL_ENABLED = True
DEFAULT_PULL_LIMIT = 8
# pull 返回的总字符上限：工具结果同样要进上下文，也是烧预算的。
#
# 30000 是实测选出来的：真库 25 条样本上，召回率随着预算单调上升、没有拐点 ——
# 4000→42.9%、8000→71.4%、16000→85.7%、24000→90.5%、不限→100%。而 pull 漏掉的
# 样本**全部**是"放宽预算就能捞回来"的，说明这一层不是选得差，是给得少。
#
# 注意它和 src/mcp_runner.py 的 MAX_RESULT_CHARS 是一对：search_memory 已迁到
# MCP，工具结果走客户端包装时会被那个上限硬裁，那边必须 ≥ 这个值，否则这里多要的
# 字符在本地路径上是白花的（还会被截出一句"结果过长已截断"）。
PULL_MAX_CHARS = 30000
# 单条历史的字符上限。真实库里的消息可以上万字符（长回复整段存着），
# 而分组预算对"第一个组"是网开一面的（见 memory_retriever.retrieve），
# 所以不截断单条的话，一次工具调用就能吐回两万多字符。
#
# 2000 是实测选出来的：真库 25 条样本、总预算 30000 下 ——
#   单条≤800 → 召回 90.5%、首位组命中 47.6%、平均 17064 字符
#   单条≤2000 → 召回 95.2%、首位组命中 61.9%、平均 17545 字符
#   单条不限 → 召回 95.2%、首位组命中 61.9%、平均 17443 字符
# 也就是说 2000 已经等于"不限"（数字完全一致），却仍能挡住单条巨型消息独占预算。
# 800 会把正解从长消息里截掉 —— 这是排序命中率被压低的主因，不是排序本身差。
PULL_PER_MESSAGE_CHARS = 2000


def _trim_to_budget(groups, max_chars):
    """把分组裁到字符预算以内，返回 (分组, 实际字符数)。

    为什么需要单独一道：memory_retriever.retrieve 的预算是"分组级"的，而且对
    第一个组网开一面（``if groups and total + group_chars > budget``）——单条超长、
    或者相邻命中把窗口合并成一个巨型片段时，它拦不住。真库实测见过一次工具调用
    吐回 25000 字符。工具结果直接进上下文，必须在这里硬裁。

    每条消息整条保留（宁可少给几条），最后至少留住第一条，避免"找到了却什么都不给"。
    """
    try:
        budget = max(200, int(max_chars))
    except (TypeError, ValueError):
        budget = PULL_MAX_CHARS
    kept = []
    total = 0
    for group in groups or []:
        messages = []
        for message in group.get("messages") or []:
            length = len(str(message.get("content") or ""))
            if messages and total + length > budget:
                break
            messages.append(message)
            total += length
        if messages:
            kept.append({"score": group.get("score", 0.0), "messages": messages})
        if total >= budget:
            break
    return kept, total


class MemoryRuntime:
    """Owns the index, the embedding service, the retriever and summaries."""

    def __init__(self, config=None, message_db=None, store=None, service=None, message_db_factory=None):
        self.config = config
        self.message_db = message_db
        self._message_db_factory = message_db_factory
        self.store = store or MemoryIndexStore()
        self.service = service or get_embedding_service(config)
        self.indexer = MemoryIndexer(self.message_db, self.store, self.service) if message_db is not None else None
        self.summaries = SessionSummaryManager(self.store, config)
        self._started = False
        # 当前会话 id。工具处理器是静态方法（ActionHandler 拿不到 GUI 侧对象），
        # 所以由 build_context 在这里记一笔，供 search_memory 使用。
        self.active_session_id = ""

    # ---- config ---------------------------------------------------------

    def _setting(self, name, default):
        getter = getattr(self.config, name, None) if self.config is not None else None
        try:
            return getter() if callable(getter) else default
        except Exception:
            logging.warning("读取记忆配置 %s 失败", name, exc_info=True)
            return default

    def enabled(self):
        return bool(self._setting("get_memory_enabled", True))

    def retrieval_enabled(self):
        return self.enabled() and bool(self._setting("get_memory_retrieval_enabled", DEFAULT_RETRIEVAL_ENABLED))

    def pull_enabled(self):
        """pull 通道（search_memory 工具）是否可用。

        它同时决定本地路径的 push 是否让位：两条路都开着的话，同一段历史会被
        注入一次、又被模型查一次，白烧 token 还容易让模型困惑两份是什么关系。
        """
        return self.retrieval_enabled() and bool(
            self._setting("get_memory_pull_enabled", DEFAULT_PULL_ENABLED)
        )

    def recent_messages(self):
        try:
            return max(2, int(self._setting("get_memory_recent_messages", DEFAULT_RECENT_MESSAGES)))
        except (TypeError, ValueError):
            return DEFAULT_RECENT_MESSAGES

    def context_max_chars(self):
        try:
            return int(self._setting("get_memory_context_max_chars", DEFAULT_CONTEXT_MAX_CHARS))
        except (TypeError, ValueError):
            return DEFAULT_CONTEXT_MAX_CHARS

    def query_turns(self):
        """检索 query 拼带的近期轮数（见 memory_retriever.build_query_text）。"""
        try:
            return max(1, int(self._setting("get_memory_query_turns", DEFAULT_QUERY_TURNS)))
        except (TypeError, ValueError):
            return DEFAULT_QUERY_TURNS

    # ---- 常驻块（core memory） -------------------------------------------

    def core_manager(self):
        """常驻块管理器：懒加载，任何失败都不影响检索与发送路径。"""
        manager = getattr(self, "_core", None)
        if manager is None:
            try:
                manager = get_core_memory(self.config)
            except Exception:
                logging.warning("常驻块初始化失败", exc_info=True)
                manager = None
            self._core = manager
        return manager

    def core_block(self):
        manager = self.core_manager()
        if manager is None:
            return ""
        try:
            return manager.block()
        except Exception:
            logging.warning("常驻块渲染失败", exc_info=True)
            return ""

    def _describe_core(self):
        manager = self.core_manager()
        if manager is None:
            return "unavailable"
        try:
            return manager.describe()
        except Exception:
            return "error"

    def note_turn(self, session_id, rows):
        """公开入口：桥接路径通过它调度偏好提炼。

        为什么必须有这个公开名：桥接（dsh_session_thread）属于另一个模块，调的是
        `runtime.note_turn(...)`。这个名字一度**不存在** —— 实际实现是私有的
        `_note_extraction` —— 于是 AttributeError 被桥接那边的 except 吞掉，日志里
        只留一行 warning，偏好一条都没提炼出来过（真库 data/core_memory.json 里
        preferences 长期是 0 条，根因就在这里）。名字对齐比"少写一层包装"重要得多。
        """
        return self._note_extraction(session_id, rows)

    def _note_extraction(self, session_id, rows):
        """攒够新消息就把偏好提炼排进后台；失败静默，绝不影响这一轮。"""
        manager = self.core_manager()
        if manager is None:
            return
        try:
            manager.note_turn(session_id, rows)
        except Exception:
            logging.warning("偏好提炼调度失败", exc_info=True)

    def _retrieval_params(self):
        return {
            "top_k": self._setting("get_memory_retrieval_top_k", DEFAULT_TOP_K),
            "group_radius": self._setting("get_memory_retrieval_group_radius", DEFAULT_GROUP_RADIUS),
            "max_groups": self._setting("get_memory_retrieval_max_groups", DEFAULT_MAX_GROUPS),
            "max_chars": self._setting("get_memory_retrieval_max_chars", DEFAULT_MAX_CHARS),
            "min_score": self._setting("get_memory_retrieval_min_score", DEFAULT_MIN_SCORE),
            # 注意这里**没有** hybrid：混合检索只开在 pull 路（见 recall()）。push 保持
            # 纯向量 —— 实测带上 RRF 后 push 召回从 81.0% 掉到 61.9%，原因见 recall()。
        }

    # ---- lifecycle ------------------------------------------------------

    def _ensure_indexer(self):
        if self.indexer is None:
            if self.message_db is None and self._message_db_factory is not None:
                self.message_db = self._message_db_factory()
            if self.message_db is not None:
                self.indexer = MemoryIndexer(self.message_db, self.store, self.service)
        return self.indexer

    def start(self):
        if self._started:
            return
        self._started = True
        if not self.enabled():
            logging.info("本地语义记忆已关闭（配置 memory_enabled=false）")
            return
        self._ensure_indexer()
        self.service.ensure_async()
        if bool(self._setting("get_memory_index_all_sessions", False)):
            indexer = self._ensure_indexer()
            if indexer is not None:
                indexer.request_all()

    def note_session_loaded(self, session_id):
        """Index a session the user just opened, in the background."""
        if not self.enabled():
            return
        indexer = self._ensure_indexer()
        if indexer is not None and session_id:
            indexer.request_session(session_id)

    def note_message_saved(self, session_id):
        """A new message landed: index it and let the summary catch up later."""
        if not self.enabled():
            return
        indexer = self._ensure_indexer()
        if indexer is not None and session_id:
            indexer.request_session(session_id)

    def note_session_deleted(self, session_id):
        try:
            self.store.delete_session(session_id)
        except Exception:
            logging.warning("删除会话记忆索引失败 session=%s", session_id, exc_info=True)

    def wait_index_idle(self, timeout=30.0):
        indexer = self._ensure_indexer()
        if indexer is None:
            return True
        return indexer.wait_idle(timeout)

    def stop(self, timeout=1.5):
        """关闭时停止后台索引线程，避免留下未结束的后台线程。"""
        if self.indexer is not None:
            try:
                self.indexer.stop(timeout)
            except Exception:
                logging.warning("停止记忆索引线程失败", exc_info=True)

    def reload_embeddings(self):
        """设置变更后重建 embedding provider。

        索引里记录了 provider 的 identity，换后端（例如从本地哈希切到 bge-m3）
        会被索引线程识别为 identity 变化并自动重建，不需要手动清库。
        """
        reset_embedding_service()
        self.service = get_embedding_service(self.config)
        if self.indexer is not None:
            self.indexer.service = self.service
        if self.enabled():
            self.service.ensure_async()
        logging.info("已按新配置重载 embedding 后端")
        return self.service

    def status(self):
        stats = self.store.stats()
        indexer = self.indexer.status() if self.indexer is not None else {"phase": "disabled"}
        provider = self.service.provider()
        return {
            "enabled": self.enabled(),
            "retrieval_enabled": self.retrieval_enabled(),
            "identity": provider.identity if provider is not None else "",
            "embedding_error": self.service.last_error,
            "index": stats,
            "indexer": indexer,
        }

    # ---- retrieval ------------------------------------------------------

    def retrieve(self, session_id, query, exclude_message_ids=None, exclude_texts=None):
        if not self.retrieval_enabled():
            return {"groups": [], "chars": 0, "hits": 0, "identity": "", "error": "disabled"}
        # 当前跑在回退后端上时顺手安排一次重试：本地 embedding 服务恢复后
        # 无需重启程序即可回到语义检索。
        try:
            self.service.retry_if_degraded()
        except Exception:
            logging.warning("安排 embedding 重试失败", exc_info=True)
        try:
            if self.store.session_size(session_id) <= 0:
                return {"groups": [], "chars": 0, "hits": 0, "identity": "", "error": ""}
        except Exception:
            logging.warning("读取记忆索引规模失败", exc_info=True)
            return {"groups": [], "chars": 0, "hits": 0, "identity": "", "error": ""}
        params = self._retrieval_params()
        try:
            result = retrieve(
                session_id,
                query,
                self.store,
                self.service,
                exclude_message_ids=exclude_message_ids,
                **params,
            )
        except Exception:
            logging.warning("语义检索失败 session=%s", session_id, exc_info=True)
            return {"groups": [], "chars": 0, "hits": 0, "identity": "", "error": "检索异常"}
        if exclude_texts:
            normalised = {" ".join(str(text or "").split()) for text in exclude_texts}
            kept = []
            for group in result.get("groups") or []:
                messages = [
                    message for message in group.get("messages") or []
                    if " ".join(str(message.get("content") or "").split()) not in normalised
                ]
                if messages:
                    kept.append({"score": group.get("score", 0.0), "messages": messages})
            result["groups"] = kept
        return result

    def _recent_message_ids(self, session_id):
        """最近若干条消息的 id：它们已经在上下文里了，pull 时排除掉。"""
        try:
            rows = self.store.session_rows(session_id)
        except Exception:
            logging.warning("读取会话消息失败 session=%s", str(session_id)[:12], exc_info=True)
            return []
        if not rows:
            return []
        try:
            window = int(self.recent_messages() or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            # 0 是"一条都不排除"的意思：离线评测要复现"模型还没看到任何历史"
            # 这个前提，那时排除任何消息都是凭空的。
            return []
        window = max(2, window)
        return [row.get("message_id") for row in rows[-window:] if row.get("message_id")]

    def recall(
        self,
        session_id,
        query,
        limit=DEFAULT_PULL_LIMIT,
        max_chars=PULL_MAX_CHARS,
        per_message_chars=PULL_PER_MESSAGE_CHARS,
        # 下面两个是**调参实验**的口子（默认值与出货行为完全一致）：
        #   hybrid_pool=None  → 用检索层的 DEFAULT_HYBRID_POOL（两路各自多宽）
        #   lexical_weight=1.0 → 词法路与 dense 等权
        hybrid_pool=None,
        lexical_weight=1.0,
    ):
        """pull 通道：返回 top-k，**不设任何分数门槛**。

        与 retrieve() 的差别只在取舍：push 必须由系统替模型划一条线，所以有
        min_ratio / min_score；pull 把这条线交给调用方（模型自己判断哪几条有用），
        所以这里刻意把两道门都关掉 —— 排序仍由向量分决定，但不再有"进不进得来"。

        查不到时返回空 groups 且 error 为空：调用方据此明确回复"没有找到相关记录"，
        绝不能回空字符串（模型会把"空"读成"用户确实没说过"）。
        """
        result = {"groups": [], "chars": 0, "hits": 0, "identity": "", "error": ""}
        if not self.pull_enabled():
            result["error"] = "disabled"
            return result
        session_id = str(session_id or "")
        if not session_id:
            # 跨进程兜底：MCP 子进程里没有 build_context 跑过，只能读落库的记录。
            session_id = self._stored_active_session()
            if session_id and not self._session_has_rows(session_id):
                # 陈旧记录（会话已被删）：清掉，免得后面每次都踩空。
                logging.info("[memory-pull] 清掉陈旧会话记录 session=%s", session_id[:12])
                try:
                    self.store.set_meta(ACTIVE_SESSION_META_KEY, "")
                except Exception:
                    logging.warning("清理陈旧会话记录失败", exc_info=True)
                session_id = ""
        if not session_id:
            result["error"] = "no-session"
            return result
        # 把**真正查的那个会话**回报给调用方。子进程里传进来的 session_id 是空的，
        # 靠上面那步兜底才填上；调用方（工具痕迹）要记的是这个，而不是空串 ——
        # 否则"它到底查了哪个会话"永远看不出来。
        result["session"] = session_id
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = DEFAULT_PULL_LIMIT
        if not self._session_has_rows(session_id):
            return result
        # 子进程冷启动：MCP server 那个进程里没人调过 ensure_async()，provider 还是
        # None，直接检索会空手而归。工具调用是"模型明确在问"，值得同步等一次后端
        # 就绪（resolve 内部只在必要时探一次），而不是敷衍一句"没查到"。
        try:
            if self.service.provider() is None:
                self.service.resolve()
        except Exception:
            logging.warning("pull 前解析 embedding 后端失败", exc_info=True)
        if self.service.provider() is None:
            # 后端真的起不来：明确回"不可用"，绝不回"没找到"——后者会被当成
            # "用户确实没说过"，是最危险的错答。
            result["error"] = "unavailable"
            return result
        params = self._retrieval_params()
        try:
            result = retrieve(
                session_id,
                query,
                self.store,
                self.service,
                top_k=max(int(limit) * 3, int(limit)),
                group_radius=params["group_radius"],
                max_groups=int(limit),
                max_chars=max_chars,
                min_ratio=0.0,
                min_score=0.0,
                per_message_chars=per_message_chars,
                exclude_message_ids=self._recent_message_ids(session_id),
                # 混合检索（词法 BM25 + RRF）**只开在 pull 路**，push 保持纯向量。
                #
                # 真库金标实测（21 条正例，bge-m3）：
                #   pull          排序第一组命中 76.2% → 85.7%；不限预算召回 95.2% → 100%
                #   push          召回 81.0% → 61.9%，而注入字符还变多了
                #
                # push 变差不是巧合：它的 dense 被 min_ratio/min_score 削到只剩几条，
                # 而词法路是宽池 40 条 —— 融合时词法直接压倒 dense，排序被词法主导，
                # 结果既多灌了内容又挤掉了正解所在的窗口。pull 不设门，两路池宽对称，
                # 融合的前提才成立。这正是"小 push + 大 pull"该有的不对称。
                hybrid=bool(self._setting("get_memory_hybrid_enabled", True)),
                hybrid_pool=hybrid_pool,
                lexical_weight=lexical_weight,
            )
        except Exception:
            logging.warning("pull 检索失败 session=%s", session_id[:12], exc_info=True)
            return {
                "groups": [],
                "chars": 0,
                "hits": 0,
                "identity": "",
                "error": "检索异常",
                "session": session_id,
            }
        result["session"] = session_id
        result["groups"], result["chars"] = _trim_to_budget(
            result.get("groups") or [], max_chars
        )
        logging.info(
            "[memory-pull] session=%s query=%dchars limit=%d budget=%d/%d hits=%d groups=%d chars=%d",
            session_id[:12],
            len(str(query or "")),
            limit,
            per_message_chars,
            max_chars,
            int(result.get("hits") or 0),
            len(result.get("groups") or []),
            int(result.get("chars") or 0),
        )
        return result

    def memory_block(self, session_id, query, exclude_message_ids=None, exclude_texts=None):
        result = self.retrieve(
            session_id,
            query,
            exclude_message_ids=exclude_message_ids,
            exclude_texts=exclude_texts,
        )
        if not result.get("groups"):
            return "", result
        return format_memory_block(result, max_chars=int(result.get("chars") or 0) or None), result

    # ---- summary --------------------------------------------------------

    def summary_for(self, session_id, rows, force=False):
        if not self.enabled():
            return ""
        try:
            return self.summaries.summary_for(session_id, rows, force=force)
        except Exception:
            logging.warning("生成会话摘要失败 session=%s", session_id, exc_info=True)
            return ""

    # ---- context --------------------------------------------------------

    def note_active_session(self, session_id):
        """记下"当前会话是谁"：既记在进程内，也落进存储层。

        落库这一步是给别的进程用的 —— 桥接把 search_memory 暴露给 DSH 之后，
        工具是在 MCP 子进程里执行的，那里没有 build_context，只能读这条记录。
        值没变就不写，省掉每轮一次 fsync。
        """
        session_id = str(session_id or "")
        self.active_session_id = session_id
        if not session_id:
            return
        try:
            if str(self.store.get_meta(ACTIVE_SESSION_META_KEY, "") or "") != session_id:
                self.store.set_meta(ACTIVE_SESSION_META_KEY, session_id)
        except Exception:
            logging.warning("记录当前会话失败", exc_info=True)

    def _stored_active_session(self):
        """跨进程兜底：从存储层读当前会话。读不到就返回空。"""
        try:
            return str(self.store.get_meta(ACTIVE_SESSION_META_KEY, "") or "")
        except Exception:
            logging.warning("读取当前会话失败", exc_info=True)
            return ""

    def _session_has_rows(self, session_id):
        try:
            return self.store.session_size(session_id) > 0
        except Exception:
            logging.warning("读取会话索引规模失败", exc_info=True)
            return False

    def build_context(self, session_id, history, rows=None, logger=None):
        """Build the outgoing messages for one local API turn."""
        # search_memory 可能在别的进程里执行（MCP 子进程），拿不到这里的对象，
        # 所以"当前会话是谁"必须落库，不能只留在 self 上。
        self.note_active_session(session_id)
        system_messages, dialogue = split_history(history)
        recent = recent_dialogue(dialogue, self.recent_messages())
        recent_texts = [message.get("content") for message in recent]

        recent_ids = []
        if rows:
            sample = recent_dialogue(rows, self.recent_messages())
            recent_ids = [row.get("id") for row in sample if row.get("id")]
            for row in reversed(rows):
                if row.get("id"):
                    recent_ids.append(row.get("id"))
                    break

        current_user = ""
        for message in reversed(dialogue):
            if message.get("role") == "user":
                current_user = message.get("content")
                break

        summary = self.summary_for(session_id, rows or []) if self.enabled() else ""
        core_block = self.core_block() if self.enabled() else ""

        # 检索 query 带上最近几轮：短问题（「那这个呢？」）只靠当前那条消息，在向量
        # 和词法两路上都等于瞎猜。近期轮次同时被排除在结果之外，避免同一段话注入两次。
        query_text = build_query_text(dialogue, turns=self.query_turns()) or current_user
        memory_text = ""
        memory_groups = 0
        pull_on = self.pull_enabled()
        # pull 开着时 push 让位：同一段历史不重复灌两遍。模型仍能自己调
        # search_memory 取回细节，而"最重要的东西"本来就在常驻块里，不依赖这条线。
        if query_text and not pull_on:
            memory_text, result = self.memory_block(
                session_id,
                query_text,
                exclude_message_ids=recent_ids,
                exclude_texts=recent_texts,
            )
            if isinstance(result, dict):
                memory_groups = len(result.get("groups") or [])
        built = build_context(
            system_messages,
            dialogue,
            summary=summary,
            memory_block=memory_text,
            core_block=core_block,
            recent_messages=self.recent_messages(),
            max_chars=self.context_max_chars(),
            logger=logger,
        )
        stats = built["stats"]
        logging.info(
            "[perf] context_build session=%s messages=%d recent=%d summary_chars=%d memory_chars=%d total_chars=%d trimmed=%s",
            str(session_id or "")[:12],
            len(built["messages"]),
            stats["recent_count"],
            stats["summary_chars"],
            stats["memory_chars"],
            stats["total_chars"],
            stats["trimmed"],
        )
        # 一行说清「这一轮到底注入了什么」：之后排查注入类问题全靠它。
        # 常驻块在裁剪链之外，所以这里单独报字符数；core 为空说明偏好栏还是空的。
        logging.info(
            "[memory-inject] path=local session=%s push=%s core=%dchars(%s) query=%dchars(turns=%d) memory=%dchars(groups=%d) excluded=%dids/%dtexts",
            str(session_id or "")[:12],
            "off(pull)" if pull_on else "on",
            len(core_block),
            self._describe_core(),
            len(query_text or ""),
            self.query_turns(),
            len(memory_text or ""),
            memory_groups,
            len(recent_ids),
            len(recent_texts),
        )
        self._note_extraction(session_id, rows or dialogue)
        return built


_RUNTIME_LOCK = threading.Lock()
_RUNTIME = None


def get_memory_runtime(config=None, message_db=None, message_db_factory=None):
    """Process-wide runtime so GUI and DSH threads share one index."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = MemoryRuntime(
                config,
                message_db,
                message_db_factory=message_db_factory,
            )
        else:
            if config is not None and _RUNTIME.config is None:
                _RUNTIME.config = config
                _RUNTIME.summaries.config = config
            if message_db is not None and _RUNTIME.message_db is None:
                _RUNTIME.message_db = message_db
        return _RUNTIME


def reset_memory_runtime():
    """Testing hook: drop the cached runtime."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
        _RUNTIME = None
    if runtime is not None and runtime.indexer is not None:
        try:
            runtime.indexer.stop()
        except Exception:
            pass
    reset_embedding_service()
