"""Pure helpers used by MainWindow (no widget state)."""

from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from app_prompts import WORLD_CUP_SCOPE_ALIASES
from app_prompts import WORLD_CUP_STATUS_KEYWORDS
from datetime import datetime
from screen_overlays import ATTACHMENT_MAX_BYTES
from screen_overlays import SCREENSHOT_FALLBACK_MAX_SIDE
import base64
import os
import re
import tempfile



def _session_group(timestamp):
    try:
        value = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        today = datetime.now().date()
        if value.date() == today:
            return "今天"
        if value.date() == today.fromordinal(today.toordinal() - 1):
            return "昨天"
    except (TypeError, ValueError, OverflowError):
        pass
    return "更早"


def _session_indicator_color(state, phase=0):
    """状态色；运行中随呼吸相位在两种色之间渐变，不改变任何几何尺寸。"""
    if state == "running":
        return QColor(90, 170, 255) if phase == 0 else QColor(60, 130, 215)
    if state == "failed":
        return QColor(235, 110, 110)
    return QColor(110, 205, 140)


def _resolve_world_cup_scope(user_text: str) -> str:
    normalized = user_text.lower().replace(" ", "")
    for label, aliases in WORLD_CUP_SCOPE_ALIASES.items():
        if any(alias.lower().replace(" ", "") in normalized for alias in aliases):
            return label
    return ""


def _needs_world_cup_clarification(user_text: str) -> bool:
    normalized = user_text.replace(" ", "")
    if "世界杯" not in normalized:
        return False
    if "世俱杯" in normalized or "女足" in normalized or "预选赛" in normalized or "世预赛" in normalized:
        return False
    if not any(keyword in normalized for keyword in WORLD_CUP_STATUS_KEYWORDS):
        return False
    return not _resolve_world_cup_scope(normalized)


def _compact_user_message(text):
    """把含附件原文的用户消息压缩成仅显示附件名（模型仍收到完整内容）。"""
    if not text or ("【用户上传文件：" not in text and "【用户图片：" not in text):
        return text
    parts = re.split(r"(【用户上传文件：.+?】|【用户图片：.+?】)", text)
    names = []
    for part in parts:
        if part.startswith("【用户上传文件："):
            names.append(part.replace("【用户上传文件：", "").replace("】", ""))
        elif part.startswith("【用户图片："):
            names.append(part.replace("【用户图片：", "").replace("】", "") + "（图片）")
    prefix = parts[0].strip() if parts else ""
    display = "附件：" + "、".join(names)
    if prefix:
        display = prefix + "\n\n" + display
    return display


def _screenshot_to_attachment(pixmap, prefix="截图"):
    """把图片落成临时文件并生成 data URL；超大时降采样转 JPEG。"""
    folder = os.path.join(tempfile.gettempdir(), "aissistant_screenshots")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = os.path.join(folder, f"{prefix}_{stamp}.png")
    if not pixmap.save(path, "PNG"):
        raise OSError("无法写入截图文件")
    with open(path, "rb") as shot_file:
        raw = shot_file.read()
    mime_type = "image/png"
    if len(raw) > ATTACHMENT_MAX_BYTES:
        if max(pixmap.width(), pixmap.height()) > SCREENSHOT_FALLBACK_MAX_SIDE:
            scaled = pixmap.scaled(
                SCREENSHOT_FALLBACK_MAX_SIDE,
                SCREENSHOT_FALLBACK_MAX_SIDE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            scaled = pixmap
        jpeg_path = os.path.splitext(path)[0] + ".jpg"
        if scaled.save(jpeg_path, "JPEG", 88):
            path = jpeg_path
            with open(jpeg_path, "rb") as shot_file:
                raw = shot_file.read()
            mime_type = "image/jpeg"
    if len(raw) > ATTACHMENT_MAX_BYTES:
        return None, path
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{encoded}", path


def _is_text_file(path):
    """粗略判断是否为文本文件：读取前 8KB 尝试 UTF-8 解码。"""
    try:
        with open(path, "rb") as f:
            sample = f.read(8192)
        sample.decode("utf-8")
        return True
    except Exception:
        return False
