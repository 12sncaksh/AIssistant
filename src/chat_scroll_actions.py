"""Auto-follow / scroll-position handling for the chat list."""

from __future__ import annotations
from PyQt6.QtCore import QPoint
from contextlib import contextmanager
import logging



def _is_chat_at_bottom(self, tolerance=24):
    chat_list = getattr(self, "chat_list", None)
    if chat_list is None:
        return True
    scroll_bar = chat_list.verticalScrollBar()
    return scroll_bar.maximum() - scroll_bar.value() <= tolerance


def _set_auto_follow_output(self, enabled, follow=False):
    """统一维护自动跟随开关与「回到最新输出」药丸的显隐。"""
    self._auto_follow_output = bool(enabled)
    bar = getattr(self, "latest_output_bar", None)
    if self._auto_follow_output:
        if bar is not None:
            bar.hide()
        if follow:
            self._follow_latest_output()
    elif bar is not None:
        self._position_latest_output_bar()
        bar.show()
        bar.raise_()


@contextmanager
def _suppress_chat_scroll_events(self):
    """包住程序性滚动，屏蔽它自己引发的滚动条信号。

    用计数器而不是布尔值，避免嵌套时内层提前把保护关掉。
    """
    self._programmatic_chat_scroll += 1
    try:
        yield
    finally:
        self._programmatic_chat_scroll -= 1


def _on_chat_scroll_changed(self, *_args):
    """滚动条位置变化：只有真正离开底部才算「用户在翻历史」。

    内容变矮时（流式过程块清空、plan_steps 消失、占位气泡被移除等）
    Qt 会把 value 钳制到新的 maximum，同样发出 valueChanged；此时视图
    其实仍停在底部，把它当成用户滚动就会让自动跟随莫名退出。因此这里
    以位置为准，而不是以信号来源为准。
    """
    if self._programmatic_chat_scroll:
        return
    # 滚到顶部时补加载更早的历史消息（分页），加载期间会自己保持滚动位置。
    loader = getattr(self, "_maybe_load_earlier_history", None)
    if callable(loader):
        try:
            if loader():
                return
        except Exception:
            logging.warning("加载更早的历史消息失败", exc_info=True)
    if self._is_chat_at_bottom():
        # 用户自己滚回底部，或内容收缩后仍停在底部：保持/恢复自动跟随。
        if not self._auto_follow_output:
            self._set_auto_follow_output(True)
        return
    if self._auto_follow_output:
        self._set_auto_follow_output(False)


def _position_latest_output_bar(self):
    bar = getattr(self, "latest_output_bar", None)
    if bar is None or not hasattr(self, "chat_list"):
        return
    host = self.chat_background_panel
    list_origin = self.chat_list.mapTo(host, QPoint(0, 0))
    if hasattr(self, "composer"):
        composer_top = self.composer.mapTo(host, QPoint(0, 0)).y()
        y = composer_top - bar.height() - 8
    else:
        y = list_origin.y() + self.chat_list.height() - bar.height() - 8
    x = list_origin.x() + max(8, (self.chat_list.width() - bar.width()) // 2)
    bar.move(x, max(8, y))


def _return_to_latest_output(self):
    self._set_auto_follow_output(True, follow=True)


def _follow_latest_output(self):
    if self._auto_follow_output and not self._auto_follow_scroll_timer.isActive():
        self._auto_follow_scroll_timer.start(0)


def _scroll_latest_output_if_enabled(self):
    if not self._auto_follow_output:
        return
    with self._suppress_chat_scroll_events():
        self.chat_list.doItemsLayout()
        self.chat_list.scrollToBottom()
