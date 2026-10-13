"""Screenshot selection and computer-control overlays."""

from __future__ import annotations
from PyQt6.QtCore import QPoint
from PyQt6.QtCore import QRect
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import Qt
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QPainter
from PyQt6.QtGui import QPen
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QProgressBar
from PyQt6.QtWidgets import QVBoxLayout
from PyQt6.QtWidgets import QWidget
from vision_input import VisionInputController



SCREENSHOT_FALLBACK_MAX_SIDE = 1600


ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024


ATTACHMENT_MAX_MB = ATTACHMENT_MAX_BYTES // (1024 * 1024)


class ScreenCaptureOverlay(QWidget):
    """全屏框选截图浮层：拖拽选择区域，松手确认，Esc 取消。"""

    finished = pyqtSignal(object)   # 裁剪后的 QPixmap；取消时为 None

    def __init__(self, screenshot, virtual_rect, device_ratio=1.0, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._screenshot = screenshot
        self._virtual_rect = QRect(virtual_rect)
        self._device_ratio = float(device_ratio or 1.0)
        self._origin = QPoint()
        self._current = QPoint()
        self._dragging = False
        self._closed = False

    @staticmethod
    def grab_desktop():
        """抓取整个虚拟桌面，返回 (QPixmap, 逻辑坐标矩形, 物理/逻辑缩放比)。"""
        screens = QApplication.screens()
        if not screens:
            return None, QRect(), 1.0
        virtual_rect = QRect()
        for screen in screens:
            virtual_rect = virtual_rect.united(screen.geometry())
        if virtual_rect.isEmpty():
            return None, QRect(), 1.0

        ratio = max(float(screen.devicePixelRatio() or 1.0) for screen in screens)
        canvas = QPixmap(
            max(1, round(virtual_rect.width() * ratio)),
            max(1, round(virtual_rect.height() * ratio)),
        )
        canvas.fill(QColor(0, 0, 0))
        painter = QPainter(canvas)
        try:
            for screen in screens:
                shot = screen.grabWindow(0)
                if shot.isNull():
                    continue
                shot.setDevicePixelRatio(1.0)
                geometry = screen.geometry()
                painter.drawPixmap(
                    round((geometry.x() - virtual_rect.x()) * ratio),
                    round((geometry.y() - virtual_rect.y()) * ratio),
                    shot,
                )
        finally:
            painter.end()
        # 统一按物理像素处理：裁剪区间与实际保存的像素一一对应
        canvas.setDevicePixelRatio(1.0)
        return canvas, virtual_rect, ratio

    def begin(self):
        self.setGeometry(self._virtual_rect)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _selection_rect(self):
        if not self._dragging:
            return QRect()
        return QRect(self._origin, self._current).normalized()

    def _to_device_rect(self, rect):
        ratio = self._device_ratio
        return QRect(
            round(rect.x() * ratio),
            round(rect.y() * ratio),
            max(1, round(rect.width() * ratio)),
            max(1, round(rect.height() * ratio)),
        ).intersected(self._screenshot.rect())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(self.rect(), self._screenshot)

        selection = self._selection_rect()
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
        if not selection.isNull():
            source = self._to_device_rect(selection)
            if not source.isEmpty():
                painter.drawPixmap(selection, self._screenshot, source)
            painter.setPen(QPen(QColor(86, 168, 232), 2))
            painter.drawRect(selection.adjusted(0, 0, -1, -1))

        font = QFont()
        font.setPointSize(12)
        painter.setFont(font)
        painter.setPen(QPen(QColor(255, 255, 255, 230)))
        if selection.isNull():
            hint = "拖动鼠标框选要发送的区域，Esc 取消"
        else:
            hint = "%d × %d  松手即添加，Esc 取消" % (
                round(selection.width() * self._device_ratio),
                round(selection.height() * self._device_ratio),
            )
        painter.drawText(24, 42, hint)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._origin = event.position().toPoint()
        self._current = self._origin
        self._dragging = True
        self.update()

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._current = event.position().toPoint()
        # 先取选区再清 _dragging：_selection_rect() 只在拖拽中才返回实际矩形
        selection = self._selection_rect()
        self._dragging = False
        if selection.width() < 5 or selection.height() < 5:
            self._finish(None)
            return
        source = self._to_device_rect(selection)
        if source.isEmpty():
            self._finish(None)
            return
        cropped = self._screenshot.copy(source)
        cropped.setDevicePixelRatio(1.0)
        self._finish(cropped)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if not self._closed:
            self._closed = True
            self.finished.emit(None)
        super().closeEvent(event)

    def _finish(self, pixmap):
        if self._closed:
            return
        self._closed = True
        self.hide()
        self.finished.emit(pixmap)
        self.close()


class ComputerControlOverlay(QWidget):
    """独立置顶的视觉键鼠状态条，不依附聊天窗口，也不拦截鼠标。"""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(480, 96)
        panel = QWidget(self)
        panel.setObjectName("computerControlPanel")
        panel.setGeometry(self.rect())
        panel.setStyleSheet(
            "#computerControlPanel { background-color: #1c222a; border: 1px solid #6a91b2; "
            "border-radius: 10px; }"
            "QLabel { color: #f3f6f8; font-size: 13px; }"
            "QLabel#computerControlHint { color: #b7c8d5; font-size: 11px; }"
            "QProgressBar { border: 1px solid #52697a; border-radius: 4px; "
            "background: #20262c; height: 8px; text-align: center; }"
            "QProgressBar::chunk { background: #56a8e8; border-radius: 3px; }"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(6)
        self.status_label = QLabel("AI 正在接管屏幕")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.hint_label = QLabel("持续按住 Q 3 秒可停止接管")
        self.hint_label.setObjectName("computerControlHint")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.status_label)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.progress_bar)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._q_progress_timer = QTimer(self)
        self._q_progress_timer.setInterval(50)
        self._q_progress_timer.timeout.connect(self._update_q_progress)

    def _place_on_screen(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 24)

    def show_status(self, text):
        self._hide_timer.stop()
        self.status_label.setText(str(text))
        self.hint_label.setText("持续按住 Q 3 秒可停止接管")
        self.progress_bar.setRange(0, 0)
        self._q_progress_timer.start()
        self._place_on_screen()
        self.show()
        self.raise_()

    def show_finished(self, text="视觉任务已结束"):
        self._hide_timer.stop()
        self._q_progress_timer.stop()
        self.status_label.setText(str(text))
        self.hint_label.setText("键鼠控制权已恢复")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self._place_on_screen()
        self.show()
        self.raise_()
        self._hide_timer.start(1600)

    def _update_q_progress(self):
        progress = max(0.0, min(1.0, VisionInputController.get_q_hold_progress()))
        if progress <= 0:
            self.progress_bar.setRange(0, 0)
            self.hint_label.setText("持续按住 Q 3 秒可停止接管")
            return
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(round(progress * 100))
        self.hint_label.setText(f"持续按住 Q 3 秒可停止接管（{round(progress * 100)}%）")
