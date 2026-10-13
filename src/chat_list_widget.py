"""Scrollable chat list widget with animated mouse-wheel handling."""

from __future__ import annotations

import math

from PyQt6.QtCore import QAbstractAnimation, QEasingCurve, QPropertyAnimation, Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidget


class SmoothChatListWidget(QListWidget):
    """Animate wheel scrolling while preserving native touchpad pixel scrolling.

    这里刻意不提供「用户滚动了」信号：滚轮事件发出时滚动位置还没更新，
    带着旧位置判断是否离开底部必然出错。是否离开底部统一由滚动条的
    valueChanged + 位置判定决定（见主窗口 _on_chat_scroll_changed）。
    """

    resized = pyqtSignal()
    WHEEL_PIXELS_PER_NOTCH = 72
    MIN_ANIMATION_MS = 90
    MAX_ANIMATION_MS = 180

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wheel_pixel_remainder = 0.0
        self._scroll_animation = QPropertyAnimation(
            self.verticalScrollBar(),
            b"value",
            self,
        )
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.verticalScrollBar().setSingleStep(24)
        self.verticalScrollBar().sliderPressed.connect(self._stop_smooth_scroll)

    def _stop_smooth_scroll(self):
        self._scroll_animation.stop()
        self._wheel_pixel_remainder = 0.0

    def scrollToBottom(self):
        self._stop_smooth_scroll()
        super().scrollToBottom()

    def wheelEvent(self, event):
        blocked_modifiers = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.AltModifier
        )
        if event.modifiers() & blocked_modifiers:
            super().wheelEvent(event)
            return

        pixel_delta = event.pixelDelta().y()
        if pixel_delta:
            self._stop_smooth_scroll()
            scroll_bar = self.verticalScrollBar()
            target = max(
                scroll_bar.minimum(),
                min(scroll_bar.maximum(), scroll_bar.value() - pixel_delta),
            )
            if target != scroll_bar.value():
                scroll_bar.setValue(target)
            event.accept()
            return

        angle_delta = event.angleDelta().y()
        if not angle_delta:
            super().wheelEvent(event)
            return

        self._wheel_pixel_remainder += (
            -angle_delta * self.WHEEL_PIXELS_PER_NOTCH / 120.0
        )
        pixel_step = math.trunc(self._wheel_pixel_remainder)
        if not pixel_step:
            event.accept()
            return
        self._wheel_pixel_remainder -= pixel_step
        self._animate_scroll_by(pixel_step)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def _animate_scroll_by(self, pixel_step):
        scroll_bar = self.verticalScrollBar()
        current_value = scroll_bar.value()
        animation_running = (
            self._scroll_animation.state() == QAbstractAnimation.State.Running
        )
        if animation_running:
            base_value = int(self._scroll_animation.endValue())
            if (base_value - current_value) * pixel_step < 0:
                base_value = current_value
        else:
            base_value = current_value

        target_value = max(
            scroll_bar.minimum(),
            min(scroll_bar.maximum(), base_value + pixel_step),
        )
        if animation_running and target_value == base_value:
            return
        if not animation_running and target_value == current_value:
            return

        distance = abs(target_value - current_value)
        duration = min(
            self.MAX_ANIMATION_MS,
            max(self.MIN_ANIMATION_MS, int(70 + distance * 0.7)),
        )
        self._scroll_animation.stop()
        self._scroll_animation.setDuration(duration)
        self._scroll_animation.setStartValue(current_value)
        self._scroll_animation.setEndValue(target_value)
        self._scroll_animation.start()
