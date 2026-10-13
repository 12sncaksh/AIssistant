"""Read-only report of the local memory index state.

Run:  python scripts/inspect_memory_index.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

DB = os.path.join(ROOT, "data", "memory_index.db")


def main():
    if not os.path.exists(DB):
        print(f"索引库不存在：{DB}")
        return 1

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    print("=== 各表行数 ===")
    tables = [
        row[0]
        for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    for name in tables:
        count = cur.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        print(f"  {name:<20} {count} 行")

    print("\n=== memory_meta ===")
    for key, value in cur.execute("SELECT key, value FROM memory_meta").fetchall():
        print(f"  {key} = {value}")

    print("\n=== 已索引会话数 ===")
    rows = cur.execute(
        "SELECT session_id, COUNT(*) FROM memory_index GROUP BY session_id"
    ).fetchall()
    if not rows:
        print("  （空：还没有任何消息被索引）")
    for session_id, count in rows:
        print(f"  {session_id}: {count} 条")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
