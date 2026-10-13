"""Task-state creation and update helpers."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime


def now_iso_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_task_state(user_request: str, task_id: str | None = None) -> dict:
    now = now_iso_timestamp()
    return {
        "task_id": task_id or f"task_{uuid.uuid4().hex[:10]}",
        "title": (user_request or "未命名任务")[:40],
        "status": "received",
        "created_at": now,
        "updated_at": now,
        "user_request": user_request,
        "latest_user_message": user_request,
        "current_step": "",
        "plan_steps": [],
        "tool_results": [],
        "retry_count": 0,
        "turn_count": 1,
        "clarification": {
            "required": False,
            "kind": "",
            "reason": "",
            "question": "",
            "options": [],
            "answer": "",
            "resolved_value": "",
            "asked_at": "",
            "resolved_at": "",
        },
        "last_error": "",
        "final_response": "",
    }


def touch_task_state(task_state: dict, status: str | None = None, current_step: str | None = None) -> dict:
    if status is not None:
        task_state["status"] = status
    if current_step is not None:
        task_state["current_step"] = current_step
    task_state["updated_at"] = now_iso_timestamp()
    return task_state


def upsert_task_plan_step(task_state: dict, step_key: str, title: str, status: str, detail: str = "") -> dict:
    steps = task_state.setdefault("plan_steps", [])
    now = now_iso_timestamp()
    for step in steps:
        if step.get("key") != step_key:
            continue
        step["title"] = title
        step["status"] = status
        step["detail"] = detail
        step["updated_at"] = now
        if status == "in_progress" and not step.get("started_at"):
            step["started_at"] = now
        if status in {"completed", "failed"}:
            step["finished_at"] = now
        return step

    step = {
        "key": step_key,
        "title": title,
        "status": status,
        "detail": detail,
        "started_at": now if status == "in_progress" else "",
        "finished_at": now if status in {"completed", "failed"} else "",
        "updated_at": now,
    }
    steps.append(step)
    return step


def append_task_tool_result(task_state: dict, tool_name: str, arguments: dict, result: dict, duration_ms: int) -> None:
    record = {
        "tool_name": tool_name,
        "arguments": copy.deepcopy(arguments),
        "success": bool(result.get("success")),
        "message": str(result.get("message", "")),
        "duration_ms": duration_ms,
        "recorded_at": now_iso_timestamp(),
    }
    if tool_name == "search_web" and result.get("success"):
        data = result.get("data") or {}
        results = data.get("results") or []
        record["details"] = [
            {
                "title": str(item.get("title", ""))[:120],
                "url": str(item.get("url", ""))[:200],
            }
            for item in results[:5]
        ]
    task_state.setdefault("tool_results", []).append(record)
    task_state["updated_at"] = now_iso_timestamp()
