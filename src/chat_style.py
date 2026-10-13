"""Shared color, typography, and avatar helpers for chat widgets."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QTextBlockFormat, QTextCursor


DEFAULT_BUBBLE_USER_COLOR = "#E3F2FD"
DEFAULT_BUBBLE_ASSISTANT_COLOR = "#F0F0F0"
CHAT_LINE_HEIGHT_PERCENT = 150
CHAT_LINE_HEIGHT_STYLE_SHEET = "p, li, div { line-height: %d%%; }" % CHAT_LINE_HEIGHT_PERCENT


def make_circular_avatar(pixmap, size):
    size = max(1, int(size))
    if pixmap.isNull():
        return QPixmap()
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    clip_path = QPainterPath()
    clip_path.addEllipse(0.5, 0.5, max(1.0, size - 1.0), max(1.0, size - 1.0))
    painter.setClipPath(clip_path)
    scaled = pixmap.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def clamp_chat_font_size(value, default=10):
    try:
        return max(8, min(32, int(float(value))))
    except (TypeError, ValueError):
        return default


def clamp_opacity(value, default=0.85):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def normalize_hex_color(value, default):
    color = QColor(str(value or "").strip())
    return color.name().upper() if color.isValid() else str(default).upper()


def color_to_rgba(value, opacity):
    color = QColor(normalize_hex_color(value, "#000000"))
    alpha = clamp_opacity(opacity)
    return f"rgba({color.red()},{color.green()},{color.blue()},{alpha:.3f})"


def contrast_text_color(value):
    color = QColor(normalize_hex_color(value, "#FFFFFF"))
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return "#111827" if luminance >= 150 else "#F8FAFC"


def contrast_secondary_text_color(value):
    color = QColor(normalize_hex_color(value, "#FFFFFF"))
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return "#475569" if luminance >= 150 else "#CBD5E1"


def chat_font_style(font_family, font_size):
    family = str(font_family or "").strip().replace('"', "")
    family_rule = f'font-family:"{family}";' if family else ""
    return f"{family_rule}font-size:{clamp_chat_font_size(font_size)}pt;"


def apply_plain_text_line_height(text_edit, percent=CHAT_LINE_HEIGHT_PERCENT):
    cursor = QTextCursor(text_edit.document())
    cursor.select(QTextCursor.SelectionType.Document)
    block_format = QTextBlockFormat()
    block_format.setLineHeight(
        percent, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
    )
    cursor.mergeBlockFormat(block_format)
