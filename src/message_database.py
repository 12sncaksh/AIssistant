"""SQLite persistence for chat messages and sessions."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import contextmanager


def _default_database_path():
    if getattr(sys, "frozen", False):
        runtime_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        runtime_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(runtime_dir, "data", "chat_history.db")


class MessageDatabase:
    """Persistence API for messages and their session metadata."""

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = _default_database_path()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        try:
            with self._connect() as conn:
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
                if "stream_log" not in existing_columns:
                    # 折叠用的「完整流式输出」。content 仍然是全量，这一列只是让界面
                    # 重载后还能把过程那几段折起来；老行是 NULL，按「没有日志」处理。
                    cursor.execute("ALTER TABLE messages ADD COLUMN stream_log TEXT")
                    print("数据库迁移：messages 表已添加 stream_log 列")
                conn.commit()
            print("数据库初始化成功")
        except Exception as exc:
            print(f"数据库初始化失败: {exc}")

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
                    ORDER BY timestamp ASC, id ASC LIMIT 1
                ), ''), 1, 50)
                WHERE session_id = ? AND title = ''
            """, (session_id, session_id))
        conn.commit()

    def get_session_metadata(self):
        try:
            with self._connect() as conn:
                self._sync_session_metadata(conn)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT s.session_id, s.title, s.updated_at, s.pinned,
                        COALESCE((SELECT content FROM messages m
                                  WHERE m.session_id = s.session_id
                                    AND m.role = 'user' AND m.is_system = 0
                                  ORDER BY m.timestamp ASC, m.id ASC LIMIT 1), '') AS preview
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
        with self._connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO sessions(session_id, title)
                VALUES (?, ?)
            """, (session_id, str(title or "")[:50]))
            conn.commit()

    def rename_session(self, session_id, title):
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ?",
                (str(title or "")[:80], session_id),
            )
            conn.commit()

    def set_session_pinned(self, session_id, pinned):
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET pinned = ? WHERE session_id = ?",
                (1 if pinned else 0, session_id),
            )
            conn.commit()

    def get_session_info(self):
        try:
            return [
                (item["session_id"], item["updated_at"], item["preview"])
                for item in self.get_session_metadata()
            ]
        except Exception:
            return []

    def save_message(self, session_id, role, content, is_system=False, sources=None, stream_log=None):
        """写入一条消息；返回新消息的自增 id（失败返回 None）。"""
        sources_json = None
        if sources:
            try:
                sources_json = json.dumps(sources, ensure_ascii=False)
            except (TypeError, ValueError):
                sources_json = None
        try:
            message_id = None
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT OR IGNORE INTO sessions(session_id) VALUES (?)", (session_id,))
                # 按库里**实际存在**的列拼 INSERT：老库可能还没有 sources / stream_log，
                # 硬写六列会直接报错。列名全部来自本文件的字面量白名单，不拼用户输入。
                columns = ["session_id", "role", "content", "is_system"]
                values = [session_id, role, content, 1 if is_system else 0]
                if self._sources_available(conn):
                    columns.append("sources")
                    values.append(sources_json)
                if self._stream_log_available(conn):
                    columns.append("stream_log")
                    values.append(stream_log)
                cursor.execute(
                    "INSERT INTO messages (%s) VALUES (%s)"
                    % (", ".join(columns), ", ".join("?" for _ in columns)),
                    tuple(values),
                )
                message_id = cursor.lastrowid
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
            return int(message_id) if message_id else None
        except sqlite3.OperationalError as exc:
            print(f"保存消息失败: {exc}")
            return None

    @staticmethod
    def _decode_sources(raw):
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
        started = time.perf_counter()
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                columns = {
                    row[1] for row in cursor.execute("PRAGMA table_info(messages)").fetchall()
                }
                select_columns = "role, content, sources" if "sources" in columns else "role, content"
                if "stream_log" in columns:
                    select_columns += ", stream_log"
                query = f"""
                    SELECT {select_columns} FROM messages
                    WHERE session_id = ? AND is_system = 0
                    ORDER BY timestamp ASC, id ASC
                """
                if limit:
                    query += f" LIMIT {limit}"
                query_started = time.perf_counter()
                cursor.execute(query, (session_id,))
                rows = cursor.fetchall()
                query_ms = (time.perf_counter() - query_started) * 1000
                has_sources = "sources" in columns
                has_stream_log = "stream_log" in columns
                decode_started = time.perf_counter()
                messages = []
                for row in rows:
                    index = 2
                    sources = []
                    if has_sources:
                        sources = self._decode_sources(row[index])
                        index += 1
                    messages.append({
                        "role": row[0],
                        "content": row[1],
                        "sources": sources,
                        "stream_log": row[index] if has_stream_log else None,
                    })
                decode_ms = (time.perf_counter() - decode_started) * 1000
                text_chars = sum(len(str(item.get("content") or "")) for item in messages)
                logging.info(
                    "[perf] history_db session=%s messages=%d chars=%d query_ms=%.1f decode_ms=%.1f total_ms=%.1f",
                    str(session_id)[:12],
                    len(messages),
                    text_chars,
                    query_ms,
                    decode_ms,
                    (time.perf_counter() - started) * 1000,
                )
                return messages
        except sqlite3.OperationalError:
            logging.warning(
                "[perf] history_db session=%s failed total_ms=%.1f",
                str(session_id)[:12],
                (time.perf_counter() - started) * 1000,
            )
            return []

    def _sources_available(self, conn):
        """进程内缓存 schema 探测结果，分页查询不必每次重跑 PRAGMA。"""
        cached = getattr(self, "_sources_column", None)
        if cached is None:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            cached = "sources" in columns
            self._sources_column = cached
        return cached

    def _stream_log_available(self, conn):
        """进程内缓存 schema 探测结果（老库可能还没有这一列）。"""
        cached = getattr(self, "_stream_log_column", None)
        if cached is None:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            cached = "stream_log" in columns
            self._stream_log_column = cached
        return cached

    def _fetch_rows(self, conn, sql, params):
        has_sources = self._sources_available(conn)
        has_stream_log = self._stream_log_available(conn)
        columns = "id, role, content"
        if has_sources:
            columns += ", sources"
        if has_stream_log:
            columns += ", stream_log"
        rows = conn.execute(sql.format(columns=columns), params).fetchall()
        decoded = []
        for row in rows:
            index = 3
            sources = []
            if has_sources:
                sources = self._decode_sources(row[index])
                index += 1
            stream_log = row[index] if has_stream_log else None
            decoded.append({
                "id": int(row[0]),
                "role": row[1],
                "content": row[2],
                "sources": sources,
                "stream_log": stream_log,
            })
        return decoded

    def count_session_messages(self, session_id):
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ? AND is_system = 0",
                    (session_id,),
                ).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return 0

    def load_recent_session_rows(self, session_id, limit):
        """最近 limit 条消息，按时间正序返回（含 id，供 UI 分页与索引使用）。"""
        limit = int(limit or 0)
        if limit <= 0:
            return []
        try:
            with self._connect() as conn:
                return self._fetch_rows(conn, """
                    SELECT {columns} FROM (
                        SELECT {columns} FROM messages
                        WHERE session_id = ? AND is_system = 0
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                """, (session_id, limit))
        except sqlite3.OperationalError:
            return []

    def load_session_rows_before(self, session_id, before_id, limit):
        """比 before_id 更早的一批消息，按时间正序返回。"""
        limit = int(limit or 0)
        if limit <= 0:
            return []
        try:
            with self._connect() as conn:
                return self._fetch_rows(conn, """
                    SELECT {columns} FROM (
                        SELECT {columns} FROM messages
                        WHERE session_id = ? AND is_system = 0 AND id < ?
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                """, (session_id, int(before_id), limit))
        except sqlite3.OperationalError:
            return []

    def load_all_session_rows(self, session_id):
        """整个会话的消息（含 id），按时间正序；用于建立完整内存历史和索引。"""
        try:
            with self._connect() as conn:
                return self._fetch_rows(conn, """
                    SELECT {columns} FROM messages
                    WHERE session_id = ? AND is_system = 0
                    ORDER BY id ASC
                """, (session_id,))
        except sqlite3.OperationalError:
            # 返回 None（而不是 []）以区分「查询失败」与「会话确实没有消息」：
            # 调用方若把失败当空会话，会重置分页/持久化状态并静默丢掉上下文。
            logging.warning("读取会话全部消息失败 session=%s", str(session_id)[:12], exc_info=True)
            return None

    def iter_indexable_messages(self, session_id=None, after_id=0, batch_size=200):
        """按 id 顺序分批产出可索引消息，避免一次性载入整张表。"""
        batch_size = max(1, int(batch_size or 200))
        cursor_id = int(after_id or 0)
        while True:
            try:
                with self._connect() as conn:
                    if session_id is None:
                        rows = conn.execute("""
                            SELECT id, session_id, role, content FROM messages
                            WHERE is_system = 0 AND id > ?
                            ORDER BY id ASC LIMIT ?
                        """, (cursor_id, batch_size)).fetchall()
                    else:
                        rows = conn.execute("""
                            SELECT id, session_id, role, content FROM messages
                            WHERE is_system = 0 AND id > ? AND session_id = ?
                            ORDER BY id ASC LIMIT ?
                        """, (cursor_id, str(session_id), batch_size)).fetchall()
            except sqlite3.OperationalError:
                return
            if not rows:
                return
            batch = [
                {
                    "id": int(row[0]),
                    "session_id": str(row[1]),
                    "role": str(row[2]),
                    "content": str(row[3] or ""),
                }
                for row in rows
            ]
            cursor_id = batch[-1]["id"]
            yield batch
            if len(batch) < batch_size:
                return

    def delete_session(self, session_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
