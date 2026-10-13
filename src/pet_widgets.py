"""Live2D pet web view, drag overlay and speech bubble."""

from __future__ import annotations
from PyQt6.QtCore import QEvent
from PyQt6.QtCore import QRect
from PyQt6.QtCore import QRectF
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtGui import QPainter
from PyQt6.QtGui import QPainterPath
from PyQt6.QtGui import QPen
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QWidget
from chat_markdown import pet_bubble_text



class PetWebView(QWebEngineView):
    def __init__(self, pet_window):
        super().__init__(pet_window)
        self.pet_window = pet_window
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setStyleSheet("background: transparent; border: none;")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.page().setBackgroundColor(Qt.GlobalColor.transparent)


class PetInteractionOverlay(QWidget):
    DRAG_THRESHOLD = 6

    def __init__(self, pet_window, webview):
        super().__init__(pet_window)
        self.pet_window = pet_window
        self.webview = webview
        self._right_press_pos = None
        self._right_dragging = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._forward_mouse_event(event)
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            self._right_press_pos = event.globalPosition().toPoint()
            self._right_dragging = False
            self.pet_window.begin_drag(event.globalPosition().toPoint())
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._forward_mouse_event(event)
            event.accept()
            return

        if event.buttons() & Qt.MouseButton.RightButton and self._right_press_pos is not None:
            if (event.globalPosition().toPoint() - self._right_press_pos).manhattanLength() >= self.DRAG_THRESHOLD:
                if not self._right_dragging:
                    self._right_dragging = True
                    self.pet_window.start_pet_drag_stage()
                self.pet_window.update_drag(event.globalPosition().toPoint())
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._forward_mouse_event(event)
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            self.pet_window.end_drag()
            if self._right_dragging:
                self._right_dragging = False
                self._right_press_pos = None
                event.accept()
                return

            self._right_press_pos = None
            self.pet_window.show_context_menu(event.globalPosition().toPoint())
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _forward_mouse_event(self, event):
        local_point = event.position().toPoint()
        webview_width = max(1, self.width())
        webview_height = max(1, self.height())
        if not (0 <= local_point.x() <= webview_width and 0 <= local_point.y() <= webview_height):
            return

        ratio_x = max(0.0, min(1.0, local_point.x() / webview_width))
        ratio_y = max(0.0, min(1.0, local_point.y() / webview_height))
        buttons = 0
        if event.buttons() & Qt.MouseButton.LeftButton:
            buttons |= 1

        event_type_map = {
            QEvent.Type.MouseButtonPress: "pointerdown",
            QEvent.Type.MouseMove: "pointermove",
            QEvent.Type.MouseButtonRelease: "pointerup",
        }
        event_type = event_type_map.get(event.type())
        if not event_type:
            return

        script = f"""
        (() => {{
            const ratioX = {ratio_x:.8f};
            const ratioY = {ratio_y:.8f};
            const target = document.querySelector('canvas') || document.body;
            if (!target) return;
            const rect = target.getBoundingClientRect();
            const clientX = rect.left + rect.width * ratioX;
            const clientY = rect.top + rect.height * ratioY;
            if (window.__petForwardPointer) {{
                window.__petForwardPointer("{event_type}", clientX, clientY, {buttons});
                return;
            }}
            const evt = new PointerEvent("{event_type}", {{
                bubbles: true,
                cancelable: true,
                composed: true,
                clientX,
                clientY,
                pageX: clientX,
                pageY: clientY,
                button: 0,
                buttons: {buttons},
                pointerType: "mouse",
                isPrimary: true
            }});
            target.dispatchEvent(evt);
        }})();
        """
        self.webview.page().runJavaScript(script)


class PetSpeechBubble(QWidget):
    """桌宠的非交互式说话气泡。"""
    MAX_TEXT_WIDTH = 220
    MAX_BODY_HEIGHT = 126
    MAX_LABEL_HEIGHT = MAX_BODY_HEIGHT - 17
    TAIL_WIDTH = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accent = QColor("#64748B")
        self._side = "right"
        self._display_scale = 1.0
        self._message = ""
        self._text_width = 58
        self._body_height = 42
        self.text_label = QLabel(self)
        self.text_label.setWordWrap(True)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.text_label.setStyleSheet(
            "background: transparent; color: #1F2937; "
            "padding: 0px;"
        )
        self.set_scale(1.0)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.hide()

    def set_message(self, text, accent=None):
        message = pet_bubble_text(text)
        if not message:
            self._message = ""
            self.hide()
            return False
        self._message = message
        if accent:
            self._accent = QColor(accent)

        metrics = QFontMetrics(self.text_label.font())
        bounds = metrics.boundingRect(
            QRect(0, 0, self._max_text_width, 1000),
            int(Qt.TextFlag.TextWordWrap),
            message,
        )
        self._text_width = max(58, min(self._max_text_width, bounds.width() + 4))
        self._body_height = max(42, min(self.MAX_BODY_HEIGHT, bounds.height() + 20))
        self.text_label.setText(message)
        self._update_layout()
        self.update()
        return True

    def set_scale(self, scale):
        self._display_scale = max(0.7, min(1.4, float(scale)))
        self._max_text_width = round(self.MAX_TEXT_WIDTH * self._display_scale)
        font = QFont("Microsoft YaHei")
        font.setPointSizeF(max(11.0, min(18.0, 14.0 * self._display_scale)))
        self.text_label.setFont(font)
        if self._message:
            self.set_message(self._message)

    def set_side(self, side):
        if side not in {"left", "right", "above"} or side == self._side:
            return
        self._side = side
        self._update_layout()
        self.update()

    def _update_layout(self):
        if self._side == "above":
            self.setFixedSize(self._text_width + 28, self._body_height + self.TAIL_WIDTH)
            self.text_label.setGeometry(14, 9, self._text_width, self._body_height - 17)
            return
        bubble_width = self._text_width + 28 + self.TAIL_WIDTH
        body_x = self.TAIL_WIDTH + 1 if self._side == "right" else 1
        self.setFixedSize(bubble_width, self._body_height + 2)
        self.text_label.setGeometry(body_x + 14, 9, self._text_width, self._body_height - 17)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._side == "above":
            body = QRectF(1, 1, self.width() - 2, self.height() - self.TAIL_WIDTH - 2)
        else:
            body_x = self.TAIL_WIDTH + 1 if self._side == "right" else 1
            body = QRectF(body_x, 1, self.width() - self.TAIL_WIDTH - 2, self.height() - 2)
        body_path = QPainterPath()
        body_path.addRoundedRect(body, 12, 12)
        tail_path = QPainterPath()
        if self._side == "above":
            tail_x = body.center().x()
            tail_path.moveTo(tail_x - 9, body.bottom())
            tail_path.lineTo(tail_x, self.height() - 1)
            tail_path.lineTo(tail_x + 9, body.bottom())
        else:
            tail_y = max(body.top() + 16, min(body.bottom() - 16, body.center().y()))
            if self._side == "right":
                tail_path.moveTo(body.left(), tail_y - 9)
                tail_path.lineTo(1, tail_y)
                tail_path.lineTo(body.left(), tail_y + 9)
            else:
                tail_path.moveTo(body.right(), tail_y - 9)
                tail_path.lineTo(self.width() - 1, tail_y)
                tail_path.lineTo(body.right(), tail_y + 9)
        tail_path.closeSubpath()
        painter.setPen(QPen(self._accent, 1.2))
        painter.setBrush(QColor(255, 255, 255, 238))
        painter.drawPath(body_path)
        painter.drawPath(tail_path)
