#!/usr/bin/env python3
"""DSH ACP connectivity probe (stage-0 minimal verification, not product code).

Launches `dsh --profile acp`, runs initialize -> session/new -> session/prompt,
prints every session/update as it arrives, then prints stopReason and timing.

Usage: python scripts\\dsh_acp_probe.py
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = 1
PROBE_PROMPT = "Please reply with exactly the two words: bridge ok. Do not call any tools."
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT_SECONDS = 300


class AcpProbe:
    """Minimal ACP client: NDJSON over stdio, just enough for one prompt."""

    def __init__(self, argv):
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=PROJECT_ROOT,
        )
        self._next_id = 0
        self._pending = {}
        self._write_lock = threading.Lock()
        self.update_count = 0
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _send(self, message):
        with self._write_lock:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def _pump_stdout(self):
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                print("[non-JSON output] " + raw[:200], flush=True)
                continue
            self._dispatch(message)

    def _pump_stderr(self):
        for raw in self.proc.stderr:
            print("[dsh stderr] " + raw.rstrip(), flush=True)

    def _dispatch(self, message):
        if "id" not in message:
            if message.get("method") == "session/update":
                self.update_count += 1
                self._print_update(message.get("params") or {})
            return
        if "method" in message:
            self._answer_reverse_request(message)
            return
        waiter = self._pending.pop(message.get("id"), None)
        if waiter is not None:
            waiter.put(message)

    def _answer_reverse_request(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        if method == "session/request_permission":
            options = params.get("options") or []
            picked = next(
                (o for o in options if str(o.get("kind", "")).startswith("allow")), None
            )
            title = (params.get("toolCall") or {}).get("title", method)
            label = picked["optionId"] if picked else "reject"
            print("\n[permission] " + str(title) + " -> " + str(label), flush=True)
            result = (
                {"outcome": {"outcome": "selected", "optionId": picked["optionId"]}}
                if picked
                else {"outcome": {"outcome": "cancelled"}}
            )
        else:
            print("\n[unimplemented client method] " + str(method), flush=True)
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "probe does not implement " + str(method)},
                }
            )
            return
        self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def request(self, method, params, timeout=TIMEOUT_SECONDS):
        self._next_id += 1
        request_id = self._next_id
        waiter = queue.Queue()
        self._pending[request_id] = waiter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            message = waiter.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(method + " produced no response within " + str(timeout) + "s")
        if "error" in message:
            raise RuntimeError(
                method + " failed: " + json.dumps(message["error"], ensure_ascii=False)
            )
        return message.get("result") or {}

    @staticmethod
    def _print_update(params):
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk", "user_message_chunk"):
            print((update.get("content") or {}).get("text", ""), end="", flush=True)
        elif kind == "tool_call":
            print("\n[tool] " + str(update.get("title") or update.get("toolCallId")), flush=True)
        elif kind == "tool_call_update":
            print("\n[tool update] status=" + str(update.get("status")), flush=True)
        elif kind == "plan":
            entries = [e.get("content") for e in (update.get("entries") or [])]
            print("\n[plan] " + json.dumps(entries, ensure_ascii=False), flush=True)
        else:
            print("\n[" + str(kind) + "] " + json.dumps(update, ensure_ascii=False)[:300], flush=True)

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main():
    if os.name == "nt":
        argv = ["cmd", "/c", "dsh", "--profile", "acp"]
    else:
        argv = ["dsh", "--profile", "acp"]
    print("launch: " + " ".join(argv) + "  (cwd=" + PROJECT_ROOT + ")", flush=True)
    probe = AcpProbe(argv)
    started = time.perf_counter()
    try:
        init = probe.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
        )
        print("\n[initialize] " + json.dumps(init, ensure_ascii=False)[:600] + "\n")

        session = probe.request("session/new", {"cwd": PROJECT_ROOT, "mcpServers": []})
        session_id = session.get("sessionId")
        print("[session/new] sessionId=" + str(session_id))
        options = json.dumps(session.get("configOptions"), ensure_ascii=False)[:400]
        print("[session/new] configOptions=" + options + "\n")

        print("[prompt] " + PROBE_PROMPT + "\n--- stream ---")
        result = probe.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": PROBE_PROMPT}]},
        )
        print("\n--- stream end ---")
        print("[prompt result] " + json.dumps(result, ensure_ascii=False))
        print("[updates] " + str(probe.update_count))
        print("[elapsed] " + format(time.perf_counter() - started, ".1f") + "s")
        print("[verdict] ACP link is UP" if session_id else "[verdict] no sessionId")
        return 0
    except Exception as exc:  # noqa: BLE001 - the probe must surface every failure
        print("\n[FAILED] " + type(exc).__name__ + ": " + str(exc), flush=True)
        print("[dsh exit code] " + str(probe.proc.poll()))
        return 1
    finally:
        probe.close()


if __name__ == "__main__":
    sys.exit(main())