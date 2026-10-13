"""Benchmark: history rendering cost before/after pagination.

Run:  python scripts/bench_history_load.py [message_count]

``page_size = count`` reproduces the old behaviour (one widget per historical
message on the GUI thread); ``page_size = 10`` is the current default.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from message_database import MessageDatabase  # noqa: E402
from test_history_paging import HistoryPanel  # noqa: E402

PAGE_SIZES = (10, 20)


def build_messages(db, session_id, count):
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        body = f"第 {index} 条消息：" + ("这是一段用于模拟真实会话长度的内容。" * 8)
        db.save_message(session_id, role, body)


def measure(panel, app, repeats=3):
    panel.current_session_id = "bench"
    best = None
    for _ in range(repeats):
        panel.chat_list.clear()
        panel.reset_history_pagination()
        started = time.perf_counter()
        panel.load_history_from_db()
        app.processEvents()
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 124
    app = QApplication.instance() or QApplication(sys.argv[:1])
    results = []
    more_ms = None
    with tempfile.TemporaryDirectory(prefix="aissistant-bench-") as temp:
        db = MessageDatabase(os.path.join(temp, "chat.db"))
        build_messages(db, "bench", count)

        legacy = HistoryPanel(db, page_size=count)
        legacy.show()
        app.processEvents()
        legacy_ms = measure(legacy, app)
        legacy_items = legacy.chat_list.count()

        for page_size in PAGE_SIZES:
            panel = HistoryPanel(db, page_size=page_size)
            panel.show()
            app.processEvents()
            results.append((page_size, measure(panel, app), panel.chat_list.count()))
            if page_size == PAGE_SIZES[0] and panel.chat_list.count() < count:
                panel.chat_list.clear()
                panel.reset_history_pagination()
                panel.current_session_id = "bench"
                panel.load_history_from_db()
                app.processEvents()
                started = time.perf_counter()
                panel.load_earlier_history()
                app.processEvents()
                more_ms = (time.perf_counter() - started) * 1000

    print(f"messages in session      : {count}")
    print(f"legacy render (all)      : {legacy_ms:8.1f} ms  ({legacy_items} widgets)")
    for page_size, elapsed, items in results:
        print(f"paged first paint ({page_size:>2})  : {elapsed:8.1f} ms  ({items} widgets)")
    if more_ms is not None:
        print(f"paged load earlier page  : {more_ms:8.1f} ms")
    if legacy_ms and results:
        default_ms = results[0][1]
        print(f"first-paint speedup      : {legacy_ms / default_ms:.1f}x  (page size {PAGE_SIZES[0]})")


if __name__ == "__main__":
    main()
