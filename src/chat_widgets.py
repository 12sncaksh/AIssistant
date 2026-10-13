"""Reusable chat presentation widgets."""

from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QLabel, QWidget


def _clamp_opacity(value, default=0.85):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class ClickableAvatarLabel(QLabel):
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ResizableBackgroundPanel(QWidget):
    """Paint a scalable, cached background image inside a widget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._background_pixmap = QPixmap()
        self._background_mode = "stretch"
        self._background_opacity = 1.0
        self._base_color = QColor("#1f1f1f")
        self._scaled_cache = QPixmap()
        self._scaled_cache_key = None
        self.setAutoFillBackground(False)

    def set_background_image(self, image_path=None):
        self._background_pixmap = QPixmap(image_path) if image_path else QPixmap()
        self._scaled_cache_key = None
        self.update()

    def set_background_mode(self, mode):
        if mode not in {"fill", "fit", "stretch", "tile", "center"}:
            mode = "stretch"
        self._background_mode = mode
        self.update()

    def set_background_opacity(self, opacity):
        self._background_opacity = _clamp_opacity(opacity, 1.0)
        self.update()

    def set_base_color(self, color):
        candidate = QColor(str(color or ""))
        self._base_color = candidate if candidate.isValid() else QColor("#1f1f1f")
        self.update()

    def _scaled_background(self, size, aspect_mode):
        dpr = max(1.0, self.devicePixelRatioF())
        width = max(1, round(size.width() * dpr))
        height = max(1, round(size.height() * dpr))
        key = (width, height, getattr(aspect_mode, "value", aspect_mode),
               self._background_pixmap.cacheKey())
        if self._scaled_cache_key == key and not self._scaled_cache.isNull():
            return self._scaled_cache
        self._scaled_cache = self._background_pixmap.scaled(
            QSize(width, height), aspect_mode, Qt.TransformationMode.SmoothTransformation
        )
        self._scaled_cache_key = key
        return self._scaled_cache

    def paintEvent(self, event):
        painter = QPainter(self)
        target = self.rect()
        painter.fillRect(target, self._base_color)
        if not self._background_pixmap.isNull() and target.width() > 0 and target.height() > 0:
            painter.setOpacity(self._background_opacity)
            mode = self._background_mode
            if mode == "tile":
                painter.drawTiledPixmap(target, self._background_pixmap)
            elif mode == "center":
                x = (target.width() - self._background_pixmap.width()) // 2
                y = (target.height() - self._background_pixmap.height()) // 2
                painter.drawPixmap(x, y, self._background_pixmap)
            else:
                aspect_mode = {
                    "fill": Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    "fit": Qt.AspectRatioMode.KeepAspectRatio,
                    "stretch": Qt.AspectRatioMode.IgnoreAspectRatio,
                }[mode]
                scaled = self._scaled_background(target.size(), aspect_mode)
                dpr = max(1.0, self.devicePixelRatioF())
                if mode == "fit":
                    width = max(1, round(scaled.width() / dpr))
                    height = max(1, round(scaled.height() / dpr))
                    painter.drawPixmap((target.width() - width) // 2, (target.height() - height) // 2,
                                       width, height, scaled)
                elif mode == "fill":
                    width = max(1, round(target.width() * dpr))
                    height = max(1, round(target.height() * dpr))
                    source = QRect(
                        max(0, (scaled.width() - width) // 2),
                        max(0, (scaled.height() - height) // 2),
                        min(width, scaled.width()),
                        min(height, scaled.height()),
                    )
                    painter.drawPixmap(target, scaled, source)
                else:
                    painter.drawPixmap(target, scaled)
            painter.setOpacity(1.0)
        painter.end()
        super().paintEvent(event)
