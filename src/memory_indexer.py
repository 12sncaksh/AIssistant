"""Background indexer that keeps the semantic memory index in sync.

Embedding is far too slow to run on the GUI thread, so a single daemon worker
consumes index requests and works through the message table in batches. Every
task is idempotent: a message is only re-embedded when its text changed or the
embedding backend changed.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time

from memory_embeddings import EmbeddingUnavailable, normalize_index_text


def content_digest(text):
    return hashlib.sha1(str(text or "").encode("utf-8", "ignore")).hexdigest()


def _chunks(items, size):
    size = max(1, int(size))
    for start in range(0, len(items), size):
        yield items[start:start + size]


class MemoryIndexer:
    """Serial, idempotent, resumable index builder."""

    def __init__(self, message_db, store, service, embed_batch=16, max_stored_chars=20000):
        self.message_db = message_db
        self.store = store
        self.service = service
        self.embed_batch = max(1, int(embed_batch))
        self.max_stored_chars = int(max_stored_chars)
        self._queue = queue.Queue()
        self._thread = None
        self._thread_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._busy = False
        self._stopped = False
        self._state = {"phase": "idle", "session_id": "", "indexed": 0, "skipped": 0, "error": ""}

    # ---- lifecycle ------------------------------------------------------

    def start(self):
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopped = False
            try:
                # 交给进程级注册表：关窗时统一回收，避免留下未结束的后台线程。
                import thread_registry
                self._thread = thread_registry.spawn(
                    self._work, name="memory-indexer", group="memory_index"
                )
            except Exception:
                logging.warning("记忆索引线程未能注册到 thread_registry，退回普通 daemon 线程", exc_info=True)
                self._thread = threading.Thread(target=self._work, name="memory-indexer", daemon=True)
                self._thread.start()

    def stop(self, timeout=3.0):
        self._stopped = True
        self._queue.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def status(self):
        with self._state_lock:
            state = dict(self._state)
        state["queued"] = self._queue.qsize()
        state["busy"] = self._busy
        return state

    def wait_idle(self, timeout=30.0):
        return self._idle.wait(timeout)

    # ---- requests -------------------------------------------------------

    def request_session(self, session_id):
        if not session_id:
            return
        self.start()
        # 先清空闲标志再入队：否则 wait_idle 会在 worker 取走任务之前就返回。
        self._idle.clear()
        self._queue.put({"session_id": str(session_id)})

    def request_all(self):
        self.start()
        self._idle.clear()
        self._queue.put({"session_id": None})

    # ---- worker ---------------------------------------------------------

    def _work(self):
        while not self._stopped:
            task = self._queue.get()
            if task is None:
                break
            self._idle.clear()
            self._busy = True
            try:
                self._run_task(task)
            except Exception:  # noqa: BLE001 - the indexer must never kill the app
                logging.warning("记忆索引任务失败", exc_info=True)
                self._set_state(phase="error", error="索引任务异常，详见日志")
            finally:
                self._busy = False
                if self._queue.empty():
                    self._idle.set()

    def _set_state(self, **kwargs):
        with self._state_lock:
            self._state.update(kwargs)

    def _is_degraded(self):
        """当前服务是否跑在回退后端上（拿不到 degraded 时保守认为不是）。"""
        try:
            return bool(self.service.degraded)
        except Exception:
            return False

    def _run_task(self, task):
        session_id = task.get("session_id")
        provider = self.service.resolve()
        if provider is None:
            self._set_state(
                phase="unavailable",
                session_id=str(session_id or ""),
                error=self.service.last_error or "embedding 后端不可用",
            )
            return
        identity = provider.identity
        stored_identity = self.store.index_identity()
        if stored_identity and stored_identity != identity:
            # 当前 provider 只是回退结果（例如 API 暂时不可达）时，绝不能清空：
            # 那会把按真实模型建立的整库向量删掉，还要在 API 恢复后全部重算。
            # 此时跳过本轮索引，保留原样，等首选后端恢复后继续增量。
            if self._is_degraded():
                logging.warning(
                    "embedding 后端当前为回退结果（%s），与已有索引（%s）不一致；"
                    "已跳过本轮索引以保护现有向量",
                    identity,
                    stored_identity,
                )
                self._set_state(
                    phase="degraded",
                    session_id=str(session_id or "all"),
                    error="embedding 首选后端不可用，已跳过索引以保护现有向量",
                )
                # 顺手安排一次重试：服务恢复后自动继续增量索引，无需重启程序。
                try:
                    self.service.retry_if_degraded()
                except Exception:
                    logging.warning("安排 embedding 重试失败", exc_info=True)
                return
            logging.info("记忆索引的 embedding 标识已变化（%s → %s），重建索引", stored_identity, identity)
            self.store.clear_index(identity)
        elif not stored_identity:
            self.store.set_meta("embedding_identity", identity)

        known = self.store.indexed_hashes(session_id)
        self._set_state(
            phase="indexing",
            session_id=str(session_id or "all"),
            indexed=0,
            skipped=0,
            error="",
        )
        indexed_total = 0
        skipped_total = 0
        started = time.perf_counter()
        for batch in self.message_db.iter_indexable_messages(session_id=session_id):
            if self._stopped:
                return
            pending = []
            for message in batch:
                text = str(message.get("content") or "")
                if not text.strip():
                    continue
                digest = content_digest(text)
                if known.get(message["id"]) == digest:
                    skipped_total += 1
                    continue
                pending.append((message, digest))
            if not pending:
                continue
            for chunk in _chunks(pending, self.embed_batch):
                if self._stopped:
                    return
                try:
                    vectors = self.service.embed([item[0].get("content") for item in chunk])
                except EmbeddingUnavailable as exc:
                    self._set_state(phase="unavailable", error=str(exc))
                    logging.warning("记忆索引中断：%s", exc)
                    return
                entries = []
                for (message, digest), vector in zip(chunk, vectors):
                    text = str(message.get("content") or "")
                    entries.append({
                        "message_id": message["id"],
                        "session_id": message.get("session_id") or "",
                        "role": message.get("role") or "",
                        "text": text[:self.max_stored_chars],
                        "content_hash": digest,
                        "identity": identity,
                        "embedding": vector,
                    })
                written = self.store.upsert_entries(entries)
                indexed_total += written
                self._set_state(indexed=indexed_total, skipped=skipped_total)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._set_state(phase="idle", indexed=indexed_total, skipped=skipped_total, error="")
        if indexed_total or skipped_total:
            logging.info(
                "[perf] memory_index session=%s indexed=%d skipped=%d elapsed_ms=%.0f",
                str(session_id or "all")[:12],
                indexed_total,
                skipped_total,
                elapsed_ms,
            )
