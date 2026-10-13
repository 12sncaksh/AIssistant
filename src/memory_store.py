"""SQLite storage for the local semantic memory index.

The index lives in its own database file so it never interferes with the chat
history schema or its migrations. It stores:

* ``memory_index``       - one embedded message per row (keyed by message id)
* ``session_summaries``  - the long-term background summary per session
* ``memory_meta``        - the embedding identity the index was built with

Search is per session: a session's vectors are cached in memory as one matrix
and scored with a single dot product, which is fast enough for the thousands of
messages a long conversation can reach.
"""

from __future__ import annotations

import array
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

try:  # optional accelerator
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

from app_paths import get_runtime_dir


DEFAULT_INDEX_FILENAME = "memory_index.db"
MAX_CACHED_SESSIONS = 4
IDENTITY_META_KEY = "embedding_identity"
# 「当前是哪个会话」也存进 meta，而不是留在进程内变量里。
#
# 为什么：search_memory 会同时从三个地方被调用 —— 本地 API 路径的本进程、
# 桥接路径的 MCP 子进程、以及以后免安装版可能走的独立进程。子进程里没有
# build_context 跑过，拿不到进程内变量，只能靠这份落库的记录知道该查哪个会话。
# 它同时还是一个"陈旧值"的兜底：读出来之后会用 session_size() 校验一次。
ACTIVE_SESSION_META_KEY = "active_session_id"


def default_index_path():
    return os.path.join(get_runtime_dir(), "data", DEFAULT_INDEX_FILENAME)


def _serialize(vector):
    return array.array("f", [float(value) for value in vector]).tobytes()


def _deserialize(blob):
    values = array.array("f")
    values.frombytes(blob)
    return values


class MemoryIndexStore:
    """Persistence plus in-memory vector cache for the memory index."""

    def __init__(self, db_path=None, max_cached_sessions=MAX_CACHED_SESSIONS):
        if db_path is None:
            db_path = default_index_path()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._max_cached_sessions = max(1, int(max_cached_sessions))
        self._init_db()

    # ---- plumbing -------------------------------------------------------

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_index (
                    message_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_session ON memory_index(session_id, message_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    covered_message_id INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()

    # ---- meta / identity ------------------------------------------------

    def get_meta(self, key, default=""):
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
                return row[0] if row else default
        except sqlite3.Error:
            logging.warning("读取记忆索引元信息失败 key=%s", key, exc_info=True)
            return default

    def set_meta(self, key, value):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memory_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
            conn.commit()

    def index_identity(self):
        return str(self.get_meta(IDENTITY_META_KEY, "") or "")

    def clear_index(self, identity=""):
        """Drop every vector (used when the embedding model changes)."""
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM memory_index")
                conn.commit()
            self._cache.clear()
            if identity:
                self.set_meta(IDENTITY_META_KEY, identity)

    def stats(self):
        try:
            with self._connect() as conn:
                indexed = conn.execute("SELECT COUNT(*) FROM memory_index").fetchone()[0]
                sessions = conn.execute("SELECT COUNT(DISTINCT session_id) FROM memory_index").fetchone()[0]
            return {"indexed": int(indexed), "sessions": int(sessions), "identity": self.index_identity()}
        except sqlite3.Error:
            return {"indexed": 0, "sessions": 0, "identity": ""}

    # ---- writing --------------------------------------------------------

    def upsert_entries(self, entries):
        """Insert or refresh embedded messages. ``entries`` carry raw vectors."""
        prepared = []
        for entry in entries or []:
            vector = entry.get("embedding") or []
            if not vector:
                continue
            prepared.append((
                int(entry["message_id"]),
                str(entry.get("session_id") or ""),
                str(entry.get("role") or ""),
                str(entry.get("text") or ""),
                str(entry.get("content_hash") or ""),
                str(entry.get("identity") or ""),
                len(vector),
                _serialize(vector),
            ))
        if not prepared:
            return 0
        touched = {row[1] for row in prepared}
        with self._lock:
            with self._connect() as conn:
                conn.executemany("""
                    INSERT INTO memory_index
                        (message_id, session_id, role, text, content_hash, identity, dim, embedding, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(message_id) DO UPDATE SET
                        session_id = excluded.session_id,
                        role = excluded.role,
                        text = excluded.text,
                        content_hash = excluded.content_hash,
                        identity = excluded.identity,
                        dim = excluded.dim,
                        embedding = excluded.embedding,
                        updated_at = CURRENT_TIMESTAMP
                """, prepared)
                conn.commit()
            for session_id in touched:
                self._cache.pop(session_id, None)
        return len(prepared)

    def delete_messages(self, message_ids):
        ids = [int(value) for value in message_ids or []]
        if not ids:
            return
        with self._lock:
            with self._connect() as conn:
                conn.executemany("DELETE FROM memory_index WHERE message_id = ?", [(value,) for value in ids])
                conn.commit()
            self._cache.clear()

    def delete_session(self, session_id):
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM memory_index WHERE session_id = ?", (str(session_id),))
                conn.execute("DELETE FROM session_summaries WHERE session_id = ?", (str(session_id),))
                conn.commit()
            self._cache.pop(str(session_id), None)

    # ---- reading --------------------------------------------------------

    def indexed_hashes(self, session_id=None):
        """message_id -> content_hash, so unchanged messages are never re-embedded."""
        query = "SELECT message_id, content_hash FROM memory_index"
        params = ()
        if session_id is not None:
            query += " WHERE session_id = ?"
            params = (str(session_id),)
        try:
            with self._connect() as conn:
                return {int(row[0]): str(row[1]) for row in conn.execute(query, params).fetchall()}
        except sqlite3.Error:
            logging.warning("读取已索引消息失败", exc_info=True)
            return {}

    def session_rows(self, session_id):
        """Ordered (message_id, role, text) for one session, from cache when warm."""
        cache = self._cache_entry(session_id)
        return [dict(row) for row in cache["rows"]]

    def session_size(self, session_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_index WHERE session_id = ?", (str(session_id),)
            ).fetchone()
        return int(row[0]) if row else 0

    def _load_rows(self, session_id):
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT message_id, role, text, embedding
                FROM memory_index
                WHERE session_id = ?
                ORDER BY message_id ASC
            """, (str(session_id),)).fetchall()
        return rows

    def _cache_entry(self, session_id):
        session_id = str(session_id)
        with self._lock:
            cached = self._cache.get(session_id)
            if cached is not None:
                self._cache.move_to_end(session_id)
                return cached
        raw = self._load_rows(session_id)
        rows = []
        vectors = []
        for message_id, role, text, blob in raw:
            rows.append({"message_id": int(message_id), "role": str(role), "text": str(text)})
            try:
                vectors.append(_deserialize(blob))
            except Exception:
                vectors.append(array.array("f"))
        matrix = None
        if vectors and _np is not None:
            matrix = _np.asarray([_np.frombuffer(v.tobytes(), dtype=_np.float32) for v in vectors], dtype=_np.float32)
        entry = {
            "rows": rows,
            "vectors": vectors,
            "matrix": matrix,
            "positions": {row["message_id"]: index for index, row in enumerate(rows)},
        }
        with self._lock:
            self._cache[session_id] = entry
            self._cache.move_to_end(session_id)
            while len(self._cache) > self._max_cached_sessions:
                self._cache.popitem(last=False)
        return entry

    def invalidate(self, session_id=None):
        with self._lock:
            if session_id is None:
                self._cache.clear()
            else:
                self._cache.pop(str(session_id), None)

    def search(self, session_id, query_vector, limit=12, exclude_message_ids=None):
        """Cosine similarity search inside one session."""
        if not query_vector:
            return []
        entry = self._cache_entry(session_id)
        rows = entry["rows"]
        if not rows:
            return []
        excluded = {int(value) for value in (exclude_message_ids or [])}
        scores = self._score(entry, query_vector)
        if scores is None:
            return []
        index_limit = min(len(rows), max(int(limit) * 4, int(limit) + len(excluded) + 8))
        if _np is not None and entry["matrix"] is not None:
            order = _np.argsort(-scores)[:index_limit]
        else:
            order = sorted(range(len(scores)), key=lambda i: -scores[i])[:index_limit]
        hits = []
        for position in order:
            position = int(position)
            row = rows[position]
            if row["message_id"] in excluded:
                continue
            hits.append({
                "message_id": row["message_id"],
                "role": row["role"],
                "text": row["text"],
                "score": float(scores[position]),
                "position": position,
            })
            if len(hits) >= int(limit):
                break
        return hits

    @staticmethod
    def _score(entry, query_vector):
        matrix = entry["matrix"]
        if matrix is not None and _np is not None:
            query = _np.asarray(query_vector, dtype=_np.float32)
            if query.shape[0] != matrix.shape[1]:
                logging.warning(
                    "查询向量维度 %d 与索引维度 %d 不一致，跳过本次检索",
                    query.shape[0],
                    matrix.shape[1],
                )
                return None
            return matrix @ query
        vectors = entry["vectors"]
        if not vectors:
            return None
        if len(query_vector) != len(vectors[0]):
            return None
        return [sum(a * b for a, b in zip(vector, query_vector)) for vector in vectors]

    # ---- summaries ------------------------------------------------------

    def get_summary(self, session_id):
        try:
            with self._connect() as conn:
                row = conn.execute("""
                    SELECT summary, covered_message_id, message_count, updated_at
                    FROM session_summaries WHERE session_id = ?
                """, (str(session_id),)).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return {
            "summary": str(row[0] or ""),
            "covered_message_id": int(row[1] or 0),
            "message_count": int(row[2] or 0),
            "updated_at": str(row[3] or ""),
        }

    def set_summary(self, session_id, summary, covered_message_id=0, message_count=0):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO session_summaries
                    (session_id, summary, covered_message_id, message_count, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    summary = excluded.summary,
                    covered_message_id = excluded.covered_message_id,
                    message_count = excluded.message_count,
                    updated_at = CURRENT_TIMESTAMP
            """, (str(session_id), str(summary or ""), int(covered_message_id), int(message_count)))
            conn.commit()

    def stale(self, session_id):
        info = self.get_summary(session_id)
        return info is None or not str(info.get("summary") or "").strip()
