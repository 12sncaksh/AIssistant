"""Smoke-check semantic retrieval against the real chat history (read-only).

Picks the longest session, takes one of its earlier messages as a query, and
reports what the rebuilt index retrieves. Writes nothing.
"""

from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config_manager import ConfigManager  # noqa: E402
from memory_embeddings import EmbeddingService  # noqa: E402
from memory_retriever import retrieve  # noqa: E402
from memory_store import MemoryIndexStore  # noqa: E402


def main():
    config = ConfigManager()
    store = MemoryIndexStore()
    service = EmbeddingService(config)
    provider = service.resolve()
    print(f"后端: {provider.identity if provider else '不可用'}")
    print(f"索引: {store.stats()}")

    conn = sqlite3.connect(store.db_path)
    row = conn.execute(
        "SELECT session_id, COUNT(*) c FROM memory_index GROUP BY session_id ORDER BY c DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        print("索引为空")
        return 1
    session_id, count = row
    print(f"\n最长会话: {session_id}（{count} 条）")

    from message_database import MessageDatabase

    db = MessageDatabase()
    rows = db.load_all_session_rows(session_id)
    if len(rows) < 3:
        print("消息太少，跳过")
        return 0

    # 取中间一条当作"旧问题"，验证能否被检索回来
    probe = rows[len(rows) // 3]
    query = str(probe.get("content") or "")[:80]
    print(f"\n用这条历史消息作为查询（前 80 字）：\n  {query}\n")

    result = retrieve(
        session_id,
        query,
        store,
        service,
        top_k=8,
        group_radius=1,
        max_groups=4,
        max_chars=24000,
    )
    groups = result.get("groups") or []
    print(f"命中片段组: {len(groups)}  总字符: {result.get('chars', 0)}")
    for index, group in enumerate(groups[:3], 1):
        print(f"\n  组 {index}  相似度 {float(group.get('score') or 0):.3f}")
        for message in (group.get("messages") or [])[:3]:
            text = str(message.get("content") or "").replace("\n", " ")[:70]
            print(f"    [{message.get('role')}] {text}")

    hit = any(
        str(probe.get("content") or "")[:40] in str(m.get("content") or "")
        for group in groups
        for m in (group.get("messages") or [])
    )
    print()
    print("自查询能召回原消息:" , "是" if hit else "否")
    return 0


if __name__ == "__main__":
    sys.exit(main())
