"""Chat input text edit."""

from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QTextEdit



# ======================== 主窗口 ========================
class ChatInputTextEdit(QTextEdit):
    send_requested = pyqtSignal()
    image_pasted = pyqtSignal(object)

    def insertFromMimeData(self, source):
        """剪贴板里只有图片时（如 Win+Shift+S 截图），转交附件流程而不是插进正文。"""
        if self._try_emit_pasted_image(source):
            return
        super().insertFromMimeData(source)

    def _try_emit_pasted_image(self, source):
        if not source.hasImage() or source.hasUrls():
            return False
        # 同时带文本时（例如从 Excel 复制单元格）优先按文本粘贴，别抢走正常粘贴
        if source.hasText() and source.text().strip():
            return False
        image = source.imageData()
        if isinstance(image, QImage):
            if image.isNull():
                return False
            self.image_pasted.emit(image)
            return True
        if isinstance(image, QPixmap):
            if image.isNull():
                return False
            self.image_pasted.emit(image.toImage())
            return True
        return False

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.send_requested.emit()
            return
        super().keyPressEvent(event)
