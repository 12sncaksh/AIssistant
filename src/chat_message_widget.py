"""Chat message bubble widget."""

from __future__ import annotations

import html
import math
import webbrowser

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap, QTextDocument, QTextOption
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QSizePolicy,
    QTextBrowser,
    QWidget,
)

from app_paths import get_default_avatar_path
from chat_markdown import (
    DEFAULT_CODE_BLOCK_COLOR,
    build_sources_block,
    markdown_to_plain_text,
    render_markdown_to_html,
)
from chat_style import (
    CHAT_LINE_HEIGHT_STYLE_SHEET,
    DEFAULT_BUBBLE_ASSISTANT_COLOR,
    DEFAULT_BUBBLE_USER_COLOR,
    apply_plain_text_line_height,
    chat_font_style,
    clamp_chat_font_size,
    clamp_opacity,
    color_to_rgba,
    contrast_text_color,
    make_circular_avatar,
    normalize_hex_color,
)
from chat_widgets import ClickableAvatarLabel


class ChatMessageWidget(QWidget):
    MIN_BUBBLE_WIDTH = 140
    MAX_BUBBLE_WIDTH = 960
    # 必须与 _apply_bubble_style 里的 QSS "padding:6px 10px" 对齐：10+10 / 6+6
    BUBBLE_HORIZONTAL_PADDING = 20
    BUBBLE_VERTICAL_PADDING = 12
    HEIGHT_SAFETY = 4
    avatar_clicked = pyqtSignal()

    def __init__(
        self,
        message,
        is_user=True,
        opacity=0.85,
        code_opacity=0.85,
        bubble_color=None,
        code_color=None,
        parent=None,
        sources=None,
        avatar_path=None,
        render_markdown=False,
        font_family="Microsoft YaHei UI",
        font_size=10,
        code_font_family="Consolas",
        code_font_size=10,
    ):
        super().__init__(parent)
        self.is_user = is_user
        self._opacity = clamp_opacity(opacity)
        self._code_opacity = clamp_opacity(code_opacity)
        default_bubble = DEFAULT_BUBBLE_USER_COLOR if is_user else DEFAULT_BUBBLE_ASSISTANT_COLOR
        self.bubble_color = normalize_hex_color(bubble_color, default_bubble)
        self.code_color = normalize_hex_color(code_color, DEFAULT_CODE_BLOCK_COLOR)
        self._bubble_text_color = contrast_text_color(self.bubble_color)
        self.sources = sources or []
        self.avatar_path = avatar_path or get_default_avatar_path("user" if is_user else "assistant")
        self.render_markdown = bool(render_markdown)
        self.font_family = font_family
        self.font_size = clamp_chat_font_size(font_size)
        self.code_font_family = code_font_family
        self.code_font_size = clamp_chat_font_size(code_font_size)
        self._code_blocks = []
        self._text = ""
        self._last_layout_viewport_width = None
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(5, 5, 5, 5)
        self._layout.setSpacing(8)

        # 头像（使用图片文件）
        self.avatar_label = ClickableAvatarLabel()
        self.avatar_label.setFixedSize(44, 44)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar_label.clicked.connect(self.avatar_clicked.emit)
        self.set_avatar_path(self.avatar_path)
        self.list_item = None

        # 消息文本 (QTextBrowser 支持可点击链接)
        self.message_text = QTextBrowser()
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
        self._apply_bubble_style(opacity)
        self.message_text.setOpenLinks(False)
        self.message_text.setOpenExternalLinks(False)
        self.message_text.anchorClicked.connect(self._on_anchor_clicked)

        # 布局
        if is_user:
            self._layout.addWidget(self.avatar_label)
            self._layout.addWidget(self.message_text, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self._layout.addStretch(1)
        else:
            self._layout.addStretch(1)
            self._layout.addWidget(self.message_text, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            self._layout.addWidget(self.avatar_label)

        self.list_item = None
        self.set_text(message)

    def update_opacity(self, opacity):
        self._opacity = clamp_opacity(opacity)
        self._apply_bubble_style(self._opacity)

    def update_code_opacity(self, code_opacity):
        self._code_opacity = clamp_opacity(code_opacity)
        if self.render_markdown and self._code_blocks:
            self.set_text(self._text)

    def update_bubble_color(self, bubble_color):
        default_bubble = DEFAULT_BUBBLE_USER_COLOR if self.is_user else DEFAULT_BUBBLE_ASSISTANT_COLOR
        self.bubble_color = normalize_hex_color(bubble_color, default_bubble)
        self._bubble_text_color = contrast_text_color(self.bubble_color)
        self._apply_bubble_style(self._opacity)
        self.set_text(self._text)

    def update_code_color(self, code_color):
        self.code_color = normalize_hex_color(code_color, DEFAULT_CODE_BLOCK_COLOR)
        if self.render_markdown and self._code_blocks:
            self.set_text(self._text)

    def _apply_bubble_style(self, opacity):
        bg_color = color_to_rgba(self.bubble_color, opacity)
        self.message_text.setStyleSheet(
            "QTextEdit {"
            "border-radius:10px; padding:6px 10px;"
            f"color:{self._bubble_text_color};"
            f"background-color:{bg_color}; border:none;"
            f"{chat_font_style(self.font_family, self.font_size)}"
            "}"
        )

    def set_font_settings(
        self,
        font_family,
        font_size,
        code_font_family=None,
        code_font_size=None,
    ):
        self.font_family = font_family
        self.font_size = clamp_chat_font_size(font_size)
        if code_font_family is not None:
            self.code_font_family = code_font_family
        if code_font_size is not None:
            self.code_font_size = clamp_chat_font_size(code_font_size)
        self.message_text.setFont(QFont(self.font_family, self.font_size))
        self._apply_bubble_style(self._opacity)
        self.set_text(self._text)

    def set_avatar_path(self, avatar_path):
        self.avatar_path = avatar_path or get_default_avatar_path("user" if self.is_user else "assistant")
        pixmap = QPixmap(self.avatar_path)
        circular = make_circular_avatar(pixmap, 44)
        if circular.isNull():
            self.avatar_label.clear()
            self.avatar_label.setText("👤" if self.is_user else "🤖")
            self.avatar_label.setStyleSheet(
                "font-size:24px; background-color:#0078D7; border-radius:22px; color:white;"
            )
        else:
            self.avatar_label.setStyleSheet("")
            self.avatar_label.setPixmap(circular)

    def set_text(self, text):
        self._text = text
        self._code_blocks = []
        if self.sources or self.render_markdown:
            self.message_text.setHtml(self._build_html(text, self.sources))
        else:
            self.message_text.setPlainText(text)
            # setPlainText 不经过富文本引擎，默认样式表的行距不生效，这里补块级行距。
            apply_plain_text_line_height(self.message_text)
        self._sync_bubble_geometry(force=True)
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())
        else:
            # 自动查找父级 QListWidgetItem
            parent = self.parent()
            while parent and not isinstance(parent, QListWidget):
                parent = parent.parent()
            if isinstance(parent, QListWidget):
                for i in range(parent.count()):
                    if parent.itemWidget(parent.item(i)) == self:
                        self.list_item = parent.item(i)
                        self.list_item.setSizeHint(self.sizeHint())
                        break

    def _build_html(self, text, sources):
        if self.render_markdown:
            body = render_markdown_to_html(
                text,
                self._code_blocks,
                code_font_family=self.code_font_family,
                code_font_size=self.code_font_size,
                code_opacity=self._code_opacity,
                code_color=self.code_color,
            )
        else:
            body = html.escape(text).replace("\n", "<br>")
        parts = [f'<div style="color:{self._bubble_text_color};">{body}</div>']
        if sources:
            parts.append(
                "<div style='margin-top: 8px; padding-top: 6px; border-top: 1px solid #DDDDDD; "
                f"font-size: 12px; color: {self._bubble_text_color};'>来源：</div>"
            )
            for index, item in enumerate(sources, 1):
                title = html.escape(str(item.get("title", "") or "链接"))
                url = html.escape(str(item.get("url", "") or ""), quote=True)
                parts.append(
                    f"<div style='font-size: 12px;'><a href='{url}' style='color: #1a73e8; "
                    f"text-decoration: none;'>[{index}] {title}</a></div>"
                )
        return "".join(parts)

    def _on_anchor_clicked(self, url):
        if url.scheme() in {"http", "https"}:
            webbrowser.open(url.toString())
            return
        if url.scheme() != "codex-copy":
            return
        try:
            index = int(url.path().strip("/") or url.host())
            code = self._code_blocks[index]
        except (ValueError, IndexError):
            return
        QApplication.clipboard().setText(code)

    def refresh_layout(self):
        self._sync_bubble_geometry()
        self.updateGeometry()
        if self.list_item:
            self.list_item.setSizeHint(self.sizeHint())

    def _sync_bubble_geometry(self, force=False):
        list_widget = self._find_list_widget()
        viewport_width = list_widget.viewport().width() if list_widget else -1
        if not force and viewport_width == self._last_layout_viewport_width:
            return
        target_width = self._calculate_bubble_width(self._text)
        content_width = max(40, target_width - self.BUBBLE_HORIZONTAL_PADDING)
        self.message_text.setFixedWidth(target_width)
        doc = self.message_text.document()
        doc.setTextWidth(content_width)
        # 先按最终宽度完成排版，再读取布局后的实际高度；浮点高度向上取整，避免末行或圆角被列表项裁掉。
        doc_height = math.ceil(doc.documentLayout().documentSize().height())
        total_height = doc_height + self.BUBBLE_VERTICAL_PADDING + self.HEIGHT_SAFETY
        self.message_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.message_text.setFixedHeight(total_height)
        self._last_layout_viewport_width = viewport_width

    def _calculate_bubble_width(self, text):
        max_width = self._get_max_bubble_width()
        measure_text = markdown_to_plain_text(text) if self.render_markdown else text
        if self.sources:
            measure_text += "\n" + build_sources_block(self.sources)
        measure_doc = QTextDocument()
        measure_doc.setDefaultFont(self.message_text.font())
        measure_doc.setPlainText(measure_text)
        document_width = math.ceil(measure_doc.idealWidth())
        suggested_width = document_width + self.BUBBLE_HORIZONTAL_PADDING
        if len(measure_text) > 80:
            suggested_width = max(suggested_width, int(max_width * 0.82))
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
        return parent

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_bubble_geometry()

    def sizeHint(self):
        bubble_width = self.message_text.width()
        bubble_height = self.message_text.height()
        margins = self._layout.contentsMargins()
        total_width = bubble_width + self.avatar_label.width() + self._layout.spacing() + margins.left() + margins.right()
        total_height = max(bubble_height, self.avatar_label.height()) + margins.top() + margins.bottom()
        return QSize(total_width, total_height)
