"""Windows UI Automation helpers used to verify visual click targets."""

from __future__ import annotations

import logging
import math


class AccessibilityController:
    MAX_TREE_NODES = 240
    MAX_TREE_DEPTH = 8

    @staticmethod
    def _read_value(control, name, default=None):
        value = getattr(control, name, default)
        if callable(value):
            try:
                value = value()
            except Exception:
                return default
        return value

    @classmethod
    def _control_info(cls, control, depth):
        name = str(cls._read_value(control, "Name", "") or "").strip()
        control_type = str(cls._read_value(control, "ControlTypeName", "") or "").strip()
        rect = cls._read_value(control, "BoundingRectangle", None)
        if rect is None:
            return None
        try:
            left = int(getattr(rect, "left"))
            top = int(getattr(rect, "top"))
            right = int(getattr(rect, "right"))
            bottom = int(getattr(rect, "bottom"))
        except (AttributeError, TypeError, ValueError):
            return None
        if right <= left or bottom <= top:
            return None
        return {
            "name": name,
            "control_type": control_type,
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
            "enabled": bool(cls._read_value(control, "IsEnabled", True)),
            "offscreen": bool(cls._read_value(control, "IsOffscreen", False)),
            "depth": depth,
        }

    @classmethod
    def _walk_controls(cls, root):
        queue = [(root, 0)]
        visited = 0
        while queue and visited < cls.MAX_TREE_NODES:
            control, depth = queue.pop(0)
            visited += 1
            yield control, depth
            if depth >= cls.MAX_TREE_DEPTH:
                continue
            try:
                children = control.GetChildren()
            except Exception:
                children = []
            for child in children or []:
                queue.append((child, depth + 1))

    @classmethod
    def _foreground_root(cls):
        try:
            import uiautomation as auto
        except ImportError as exc:
            raise RuntimeError("缺少 uiautomation，请先安装 UI 可访问性依赖。") from exc
        root = auto.GetForegroundControl()
        if root is None:
            raise RuntimeError("没有找到当前前台窗口。")
        return root

    @staticmethod
    def _matches(info, query):
        query = str(query or "").strip().lower()
        if not query:
            return True
        haystack = f"{info.get('name', '')} {info.get('control_type', '')}".lower()
        return query in haystack

    @classmethod
    def inspect_tree(cls, query=""):
        root = cls._foreground_root()
        root_info = cls._control_info(root, 0) or {
            "name": "",
            "control_type": "",
            "x": 0,
            "y": 0,
            "width": 0,
            "height": 0,
            "enabled": True,
            "offscreen": False,
            "depth": 0,
        }
        nodes = []
        for control, depth in cls._walk_controls(root):
            info = cls._control_info(control, depth)
            if not info or info["offscreen"] or not cls._matches(info, query):
                continue
            if not info["name"] and query:
                continue
            nodes.append(info)
            if len(nodes) >= 80:
                break
        return {
            "success": True,
            "message": f"UI Automation 找到 {len(nodes)} 个匹配控件。",
            "data": {
                "query": str(query or ""),
                "foreground_window": root_info,
                "nodes": nodes,
            },
        }

    @classmethod
    def get_foreground_window_info(cls):
        root = cls._foreground_root()
        info = cls._control_info(root, 0)
        if not info:
            return {
                "success": False,
                "message": "当前前台窗口没有可用的 UI Automation 边界。",
            }
        return {
            "success": True,
            "message": f"当前前台窗口：{info.get('name') or '未命名窗口'}。",
            "data": {"foreground_window": info},
        }

    @classmethod
    def find_control(cls, query, x=None, y=None):
        root = cls._foreground_root()
        candidates = []
        for control, depth in cls._walk_controls(root):
            info = cls._control_info(control, depth)
            if not info or info["offscreen"] or not info["enabled"]:
                continue
            if not cls._matches(info, query) or not info["name"]:
                continue
            center_x = info["x"] + info["width"] / 2
            center_y = info["y"] + info["height"] / 2
            if x is not None and y is not None:
                x_value = float(x)
                y_value = float(y)
                inside = (
                    info["x"] <= x_value <= info["x"] + info["width"]
                    and info["y"] <= y_value <= info["y"] + info["height"]
                )
                distance = 0.0 if inside else math.hypot(center_x - x_value, center_y - y_value)
            else:
                distance = 0.0
            candidates.append((distance, -info["depth"], control, info))

        if not candidates:
            return {
                "success": False,
                "message": f"UI Automation 没有找到名为“{query}”的可用控件。",
            }

        candidates.sort(key=lambda item: (item[0], item[1]))
        distance, _, control, info = candidates[0]
        max_distance = max(80.0, min(180.0, max(info["width"], info["height"])))
        if x is not None and y is not None and distance > max_distance:
            return {
                "success": False,
                "message": f"视觉坐标与 UIA 控件“{info['name']}”相距 {distance:.0f} 像素，拒绝盲点。",
                "data": {"candidate": info, "distance": round(distance, 1)},
            }
        return {
            "success": True,
            "message": f"已通过 UI Automation 定位控件“{info['name']}”。",
            "data": {"control": control, "info": info, "distance": round(distance, 1)},
        }

    @classmethod
    def verify_click_target(cls, query, x, y):
        result = cls.find_control(query, x=x, y=y)
        if not result.get("success"):
            return result
        info = result["data"]["info"]
        return {
            "success": True,
            "message": f"视觉坐标与 UIA 控件“{info['name']}”一致。",
            "data": {
                "info": info,
                "distance": result["data"].get("distance", 0),
            },
        }
