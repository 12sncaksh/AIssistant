"""Task progress and streaming reply placeholder widget."""

from __future__ import annotations

import html
import math
import os
import sys

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QPixmap, QTextOption
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from chat_style import (
    CHAT_LINE_HEIGHT_STYLE_SHEET,
    DEFAULT_BUBBLE_ASSISTANT_COLOR,
    apply_plain_text_line_height,
    chat_font_style,
    clamp_chat_font_size,
    clamp_opacity,
    color_to_rgba,
    contrast_secondary_text_color,
    contrast_text_color,
    make_circular_avatar,
    normalize_hex_color,
)
from chat_widgets import ClickableAvatarLabel


def _default_avatar_path():
    if getattr(sys, "frozen", False):
        runtime_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        runtime_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    filename = os.path.join("assets", "web_resources", "mao.png")
    external_path = os.path.join(runtime_dir, filename)
    if os.path.exists(external_path):
        return external_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


class TaskProgressWidget(QWidget):
    """AI reply placeholder with task progress and streamed text."""

    MIN_BUBBLE_WIDTH = 140
    MAX_BUBBLE_WIDTH = 960
    BUBBLE_HORIZONTAL_PADDING = 28
    BUBBLE_VERTICAL_PADDING = 16
    HEIGHT_SAFETY = 4
    avatar_clicked = pyqtSignal()

    def __init__(
        self,
        opacity=0.85,
        parent=None,
        avatar_path=None,
        font_family="Microsoft YaHei UI",
        font_size=10,
        bubble_color=DEFAULT_BUBBLE_ASSISTANT_COLOR,
    ):
        super().__init__(parent)
        self._opacity = clamp_opacity(opacity)
        self.bubble_color = normalize_hex_color(bubble_color, DEFAULT_BUBBLE_ASSISTANT_COLOR)
        self.font_family = font_family
        self.font_size = clamp_chat_font_size(font_size)
        self._text = ""
        self._task_state = {}
        self.list_item = None
        self._has_progress = False
        self._show_divider = False
        self._sync_pending = False
        self._sync_force_requested = False
        self._last_layout_viewport_width = None
        self._content_size = QSize(320, 64)
        self._progress_h = 0
        self._message_h = 0

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(5, 5, 5, 5)
        self._layout.setSpacing(8)

        self.avatar_label = ClickableAvatarLabel()
        self.avatar_label.setFixedSize(44, 44)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar_label.clicked.connect(self.avatar_clicked.emit)
        self.set_avatar_path(avatar_path or _default_avatar_path())

        self.bubble_container = QWidget()
        self.bubble_container.setObjectName("progressBubble")
        self.bubble_layout = QVBoxLayout(self.bubble_container)
        self.bubble_layout.setContentsMargins(12, 10, 12, 10)
        self.bubble_layout.setSpacing(6)

        self.progress_text = QTextEdit()
        self.progress_text.setReadOnly(True)
        self.progress_text.setFrameShape(QFrame.Shape.NoFrame)
        self.progress_text.setLineWidth(0)
        self.progress_text.setVisible(False)
        self.progress_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.progress_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.progress_text.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.progress_text.document().setDocumentMargin(0)
        self.progress_text.document().setDefaultStyleSheet(CHAT_LINE_HEIGHT_STYLE_SHEET)
        self.progress_text.setFont(QFont(self.font_family, self.font_size))
        self.progress_text.setStyleSheet(
            "QTextEdit { border:none; background:transparent; color:#333333; padding:0px; "
            f"{chat_font_style(self.font_family, self.font_size)}}}"
        )
        self.bubble_layout.addWidget(self.progress_text)

        self.divider = QLabel()
        self.divider.setFixedHeight(1)
        self.divider.setStyleSheet("background-color: #D8D8D8; border: none;")
        self.divider.setVisible(False)
        self.bubble_layout.addWidget(self.divider)

        self.message_text = QTextEdit()
        self.message_text.setReadOnly(True)
        self.message_text.setFrameShape(QFrame.Shape.NoFrame)
        self.message_text.setLineWidth(0)
        self.message_text.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.message_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.message_text.document().setDocumentMargin(0)
        self.message_text.document().setDefaultStyleSheet(CHAT_LINE_HEIGHT_STYLE_SHEET)
        self.message_text.setFont(QFont(self.font_family, self.font_size))
        self.message_text.setStyleSheet(
            "QTextEdit { border:none; background:transparent; color:#000000; padding:0px; "
            f"{chat_font_style(self.font_family, self.font_size)}}}"
        )
        self.bubble_layout.addWidget(self.message_text)

        self._layout.addStretch(1)
        self._layout.addWidget(self.bubble_container, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self._layout.addWidget(self.avatar_label)
        self._apply_bubble_style(self._opacity)

    def _apply_bubble_style(self, opacity=None):
        if opacity is not None:
            self._opacity = clamp_opacity(opacity)
        self._bubble_text_color = contrast_text_color(self.bubble_color)
        self._secondary_text_color = contrast_secondary_text_color(self.bubble_color)
        self.bubble_container.setStyleSheet(
            "QWidget#progressBubble {"
            f"background-color:{color_to_rgba(self.bubble_color, self._opacity)};"
            "border-radius:10px;"
            "}"
        )
        shared_style = chat_font_style(self.font_family, self.font_size)
        text_style = (
            "QTextEdit { border:none; background:transparent; padding:0px; "
            f"color:{self._bubble_text_color}; {shared_style}"
            "}"
        )
        self.progress_text.setFont(QFont(self.font_family, self.font_size))
        self.progress_text.setStyleSheet(text_style)
        self.message_text.setFont(QFont(self.font_family, self.font_size))
        self.message_text.setStyleSheet(text_style)
        self.divider.setStyleSheet(f"background-color:{self._secondary_text_color}; border:none;")

    def update_opacity(self, opacity):
        self._apply_bubble_style(opacity)

    def update_bubble_color(self, bubble_color):
        self.bubble_color = normalize_hex_color(bubble_color, DEFAULT_BUBBLE_ASSISTANT_COLOR)
        self._apply_bubble_style()
        self.update_progress(self._task_state)

    def set_avatar_path(self, avatar_path):
        pixmap = QPixmap(avatar_path)
        circular = make_circular_avatar(pixmap, 44)
        if circular.isNull():
            self.avatar_label.clear()
            self.avatar_label.setText("🤖")
            self.avatar_label.setStyleSheet(
                "font-size:24px; background-color:#0078D7; border-radius:22px; color:white;"
            )
        else:
            self.avatar_label.setStyleSheet("")
            self.avatar_label.setPixmap(circular)

    def refresh_layout(self, force=False):
        self._sync_geometry(force=force)
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())

    def set_font_settings(self, font_family, font_size):
        self.font_family = font_family
        self.font_size = clamp_chat_font_size(font_size)
        self._apply_bubble_style()
        self.refresh_layout(force=True)

    def set_text(self, text):
        self._text = text
        self.message_text.setPlainText(text)
        apply_plain_text_line_height(self.message_text)
        self._sync_geometry(force=True)
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())

    def update_progress(self, task_state):
        self._task_state = task_state or {}
        steps = self._task_state.get("plan_steps", [])
        self._has_progress = bool(steps)
        self._show_divider = bool(steps)
        if steps:
            blocks = []
            for index, step in enumerate(steps):
                status = step.get("status", "")
                icon = {"in_progress": "\u23f3", "completed": "\u2705", "failed": "\u274c"}.get(status, "\u2022")
                title = html.escape(str(step.get("title", "")))
                margin = ' style="margin-top: 6px;"' if index else ""
                line = (
                    f'<div{margin} style="font-size: 15px; font-weight: 600; '
                    f'color: {self._bubble_text_color};">{icon} {title}</div>'
                )
                detail = str(step.get("detail", "") or "")
                if detail and status in {"in_progress", "failed"}:
                    detail = html.escape(detail if len(detail) <= 60 else detail[:60] + "\u2026")
                    line += (
                        '<div style="margin-top: 2px; font-size: 13px; font-weight: 400; '
                        f'color: {self._secondary_text_color};">&nbsp;&nbsp;&nbsp;{detail}</div>'
                    )
                blocks.append(line)
            self.progress_text.setHtml("".join(blocks))
            self.progress_text.setVisible(True)
            self.divider.setVisible(True)
        else:
            self.progress_text.setHtml("")
            self.progress_text.setVisible(False)
            self.divider.setVisible(False)
        self._sync_geometry(force=True)

    def _sync_geometry(self, force=False):
        list_widget = self._find_list_widget()
        viewport_width = list_widget.viewport().width() if list_widget else -1
        if not force and viewport_width == self._last_layout_viewport_width:
            return
        width = self._calculate_bubble_width()
        content_width = max(40, width - 24)
        self.bubble_container.setFixedWidth(width)

        if self._has_progress:
            self.progress_text.setFixedWidth(content_width)
            progress_doc = self.progress_text.document()
            progress_doc.setTextWidth(content_width)
            self._progress_h = math.ceil(progress_doc.documentLayout().documentSize().height()) + self.HEIGHT_SAFETY
            self.progress_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.progress_text.setFixedHeight(self._progress_h)
        else:
            self.progress_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._progress_h = 0

        self.message_text.setFixedWidth(content_width)
        doc = self.message_text.document()
        doc.setTextWidth(content_width)
        self._message_h = math.ceil(doc.documentLayout().documentSize().height()) + self.HEIGHT_SAFETY
        self.message_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setFixedHeight(self._message_h)
        self._update_content_size()
        self._last_layout_viewport_width = viewport_width
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())

    def _update_content_size(self):
        outer = self._layout.contentsMargins()
        bubble_w = self.bubble_container.width()
        divider_h = self.divider.height() if self._show_divider else 0
        margins = self.bubble_layout.contentsMargins()
        bubble_h = (
            margins.top() + self._progress_h + divider_h + self._message_h
            + self.bubble_layout.spacing() * 2 + margins.bottom()
        )
        self._content_size = QSize(
            bubble_w + self.avatar_label.width() + self._layout.spacing() + outer.left() + outer.right(),
            bubble_h + outer.top() + outer.bottom(),
        )

    def sizeHint(self):
        return QSize(self._content_size)

    def minimumSizeHint(self):
        return QSize(self._content_size)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_sync()

    def _schedule_sync(self, force=False):
        self._sync_force_requested = self._sync_force_requested or force
        if self._sync_pending:
            return
        self._sync_pending = True
        QTimer.singleShot(0, self._run_scheduled_sync)

    def _run_scheduled_sync(self):
        self._sync_pending = False
        force = self._sync_force_requested
        self._sync_force_requested = False
        self._sync_geometry(force=force)

    def _calculate_bubble_width(self):
        max_width = self._get_max_bubble_width()
        metrics = QFontMetrics(self.message_text.font())
        progress_plain = self.progress_text.toPlainText() if self._has_progress else ""
        combined = f"{progress_plain}\n{self._text}"
        lines = combined.splitlines() or [""]
        longest_line_width = max(metrics.horizontalAdvance(line) for line in lines) if lines else 0
        suggested_width = longest_line_width + self.BUBBLE_HORIZONTAL_PADDING
        if len(combined) > 80:
            suggested_width = max(suggested_width, int(max_width * 0.72))
        return max(self.MIN_BUBBLE_WIDTH, min(int(suggested_width), max_width))

    def _get_max_bubble_width(self):
        list_widget = self._find_list_widget()
        if not list_widget:
            return self.MAX_BUBBLE_WIDTH
        viewport_width = list_widget.viewport().width()
        reserved = self.avatar_label.width() + self._layout.spacing() + 22
        candidate = viewport_width - reserved
        return max(self.MIN_BUBBLE_WIDTH, min(candidate, self.MAX_BUBBLE_WIDTH))

    def _find_list_widget(self):
        parent = self.parent()
        while parent and not isinstance(parent, QListWidget):
            parent = parent.parent()
        return parent if isinstance(parent, QListWidget) else None
