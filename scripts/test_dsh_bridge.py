#!/usr/bin/env python3
"""Headless self-test for the DSH bridge (drives the real DSHSessionThread).

Usage: python scripts\\test_dsh_bridge.py ["optional prompt"]
"""
from __future__ import annotations

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from PyQt6.QtCore import QCoreApplication, QTimer  # noqa: E402

from dsh_acp_client import DSHSessionThread, reset_connection, sync_launch_overlay  # noqa: E402


class FileConfig:
    """Minimal stand-in for ConfigManager: reads the same config/config.json keys."""

    def __init__(self, root):
        self.root = root
        with open(os.path.join(root, "config", "config.json"), "r", encoding="utf-8-sig") as handle:
            self.data = json.load(handle)

    def _get(self, key, default=""):
        return str(self.data.get(key, default) or "").strip()

    def get_dsh_command(self):
        return self._get("dsh_command", "dsh") or "dsh"

    def get_dsh_profile(self):
        return self._get("dsh_profile", "acp") or "acp"

    def get_dsh_provider(self):
        return self._get("dsh_provider")

    def get_dsh_model(self):
        return self._get("dsh_model")


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Reply with exactly: bridge ok"
    app = QCoreApplication(sys.argv[:1])  # noqa: F841 - QThread needs a QCoreApplication
    config = FileConfig(PROJECT_ROOT)
    print("provider=%r model=%r profile=%r" % (
        config.get_dsh_provider(), config.get_dsh_model(), config.get_dsh_profile()))
    overlay = sync_launch_overlay(config)
    print("overlay=%s" % overlay)
    if overlay and os.path.isfile(overlay):
        with open(overlay, "r", encoding="utf-8") as handle:
            print("--- overlay ---\n%s---" % handle.read())

    thread = DSHSessionThread([{"role": "user", "content": prompt}], config)
    chunks = []
    thread.stream_chunk.connect(lambda text: chunks.append(text))
    thread.status_update.connect(lambda text: print("[status] %s" % text))
    thread.tool_confirmation_requested.connect(
        lambda call_id, name, arguments: (
            print("[permission auto-allow] %s %s" % (name, arguments)),
            thread.set_tool_confirmation(call_id, True),
        )
    )
    final = {}
    thread.response_received.connect(lambda text: final.setdefault("text", text))
    thread.error_occurred.connect(lambda text: final.setdefault("error", text))
    wait_timer = QTimer()
    wait_timer.setSingleShot(True)
    wait_timer.timeout.connect(app.quit)
    wait_timer.start(600000)
    thread.stopped.connect(lambda: final.setdefault("stopped", True))

    # start() + event loop mirrors the real application: streaming chunks are
    # delivered through queued connections, exactly as MainWindow receives them.
    thread.finished.connect(app.quit)
    thread.start()
    app.exec()
    reset_connection()

    print("\n[streamed chars] %d" % len("".join(chunks)))
    if "error" in final:
        print("[FAILED] %s" % final["error"])
        return 1
    print("[final reply]\n%s" % final.get("text", ""))
    print("[verdict] DSH BRIDGE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())