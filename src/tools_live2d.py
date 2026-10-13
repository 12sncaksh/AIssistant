"""Live2D / desktop-pet tool handlers."""

from __future__ import annotations
from typing import Any



def set_pet_command_callback(cls, callback):
    """注册桌宠控制回调。回调应为线程安全的（GUI 侧用信号 emit）。"""
    cls._pet_command_callback = callback


def set_pet_param_catalog(cls, catalog):
    """登记当前桌宠模型可用的 Live2D 参数表。"""
    cls._pet_param_catalog = list(catalog or [])


def _handle_control_pet(cls, arguments: dict[str, Any]) -> dict[str, Any]:
    emotion = str(arguments.get("emotion", "")).strip().lower()
    if emotion not in cls.PET_EMOTION_NAMES:
        return cls._result(False, f"不支持的情绪「{emotion}」，可选：{'、'.join(cls.PET_EMOTION_NAMES)}。")
    try:
        intensity = float(arguments.get("intensity", 1.0))
    except (TypeError, ValueError):
        intensity = 1.0
    intensity = max(0.0, min(1.0, intensity))
    try:
        duration_ms = int(arguments.get("duration_ms", 6000))
    except (TypeError, ValueError):
        duration_ms = 6000
    duration_ms = max(500, min(30000, duration_ms))
    if cls._pet_command_callback is None:
        return cls._result(False, "当前没有可控制的桌宠窗口。")
    try:
        cls._pet_command_callback({
            "kind": "emotion",
            "emotion": emotion,
            "intensity": intensity,
            "duration_ms": duration_ms,
        })
    except Exception as e:
        return cls._result(False, f"桌宠表情指令下发失败: {e}")
    if emotion == "neutral":
        return cls._result(True, "已登记收回桌宠表情，将在本条回复播报时生效。")
    return cls._result(True, f"已登记桌宠情绪「{emotion}」（强度 {intensity:.2f}，持续 {duration_ms}ms），将在本条回复播报/说话时展示。")


def _handle_set_live2d_param(cls, arguments: dict[str, Any]) -> dict[str, Any]:
    raw = arguments.get("parameters") or []
    if not isinstance(raw, list):
        return cls._result(False, "parameters 必须是 {id,value} 列表。")
    known_ids = {item.get("id") for item in cls._pet_param_catalog}
    mapping: dict[str, float] = {}
    unknown: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id", "")).strip()
        if not pid:
            continue
        if known_ids and pid not in known_ids:
            unknown.append(pid)
            continue
        try:
            mapping[pid] = float(item.get("value"))
        except (TypeError, ValueError):
            continue
    if unknown:
        return cls._result(
            False,
            f"以下参数不在当前模型（可用 list_live2d_params 查询）：{'、'.join(unknown[:20])}。",
        )
    if not mapping:
        return cls._result(False, "没有可设置的有效参数。")
    try:
        duration_ms = int(arguments.get("duration_ms", 6000))
    except (TypeError, ValueError):
        duration_ms = 6000
    duration_ms = max(500, min(30000, duration_ms))
    if cls._pet_command_callback is None:
        return cls._result(False, "当前没有可控制的桌宠窗口。")
    try:
        cls._pet_command_callback({
            "kind": "param",
            "parameters": mapping,
            "duration_ms": duration_ms,
        })
    except Exception as e:
        return cls._result(False, f"Live2D 参数下发失败: {e}")
    shown = "、".join(f"{pid}={val:g}" for pid, val in list(mapping.items())[:12])
    return cls._result(True, f"已登记桌宠参数：{shown}（将在本条回复播报时展示，持续 {duration_ms}ms 后回落）。")


def _handle_list_live2d_params(cls, arguments: dict[str, Any]) -> dict[str, Any]:
    keyword = str(arguments.get("keyword", "")).strip().lower()
    catalog = cls._pet_param_catalog
    if not catalog:
        return cls._result(False, "当前模型没有可读取的 Live2D 参数表。")
    rows = [
        item for item in catalog
        if not keyword
        or keyword in str(item.get("id", "")).lower()
        or keyword in str(item.get("name", "")).lower()
        or keyword in str(item.get("group", "")).lower()
    ]
    if not rows:
        return cls._result(True, f"没有匹配「{keyword}」的 Live2D 参数。", {"params": []})
    limit = 200
    lines = [
        f"{item.get('id', '')}（{item.get('name', '') or item.get('group', '')}）"
        for item in rows[:limit]
    ]
    suffix = "" if len(rows) <= limit else f"\n（已截断，仅显示前 {limit} 个）"
    return cls._result(
        True,
        f"当前模型共 {len(catalog)} 个参数，匹配 {len(rows)} 个：\n" + "\n".join(lines) + suffix,
        {"params": rows[:limit]},
    )
