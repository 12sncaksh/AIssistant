"""Session list rendering and session actions."""

from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QIcon
from PyQt6.QtGui import QPainter
from PyQt6.QtGui import QPen
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog
from PyQt6.QtWidgets import QHBoxLayout
from PyQt6.QtWidgets import QInputDialog
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QListWidget
from PyQt6.QtWidgets import QListWidgetItem
from PyQt6.QtWidgets import QMenu
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtWidgets import QVBoxLayout
from datetime import datetime
import logging
import time
from main_window_helpers import _session_indicator_color



def _get_latest_session_or_new(self):
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    self.session_is_persisted = False
    return session_id


def refresh_session_list(self):
    if not hasattr(self, "session_list"):
        return
    refresh_started = time.perf_counter()
    query = str(self.session_search.text() or "").strip().lower()
    metadata_started = time.perf_counter()
    metadata = self.db.get_session_metadata()
    metadata_ms = (time.perf_counter() - metadata_started) * 1000
    self.session_list.blockSignals(True)
    self.session_list.clear()
    self._session_row_items = {}
    current_item = None
    current_group = None
    visible_count = 0
    ui_started = time.perf_counter()
    for row in metadata:
        title = (row.get("title") or row.get("preview") or "未命名会话").strip()
        preview = (row.get("preview") or "").replace("\n", " ").strip()
        haystack = f"{title} {preview}".lower()
        if query and query not in haystack:
            continue
        group = self._session_group(row.get("updated_at"))
        if group != current_group:
            header = QListWidgetItem(group)
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setForeground(QColor("#888888"))
            self.session_list.addItem(header)
            current_group = group
        display_title = ("★ " if row.get("pinned") else "") + title[:32]
        display = display_title
        if preview and preview != title and not preview.startswith(title):
            display += f"\n{preview[:46]}"
        list_item = QListWidgetItem(display)
        session_id = str(row["session_id"])
        list_item.setData(Qt.ItemDataRole.UserRole, session_id)
        list_item.setData(Qt.ItemDataRole.UserRole + 1, display)
        list_item.setData(Qt.ItemDataRole.UserRole + 2, preview or title)
        self.session_list.addItem(list_item)
        self._session_row_items[session_id] = list_item
        self._update_session_list_entry(session_id)
        visible_count += 1
        if session_id == self.current_session_id:
            current_item = list_item
    self.session_list.blockSignals(False)
    if current_item:
        self.session_list.setCurrentItem(current_item)
    logging.info(
        "[perf] session_list metadata=%d visible=%d query=%s metadata_ms=%.1f ui_ms=%.1f total_ms=%.1f",
        len(metadata),
        visible_count,
        "yes" if query else "no",
        metadata_ms,
        (time.perf_counter() - ui_started) * 1000,
        (time.perf_counter() - refresh_started) * 1000,
    )


def _session_indicator_state(self, session_id):
    thread = self.session_threads.get(session_id)
    if thread is not None and thread.isRunning():
        task_state = self.session_task_states.get(session_id) or {}
        step = str(task_state.get("current_step") or "")
        return "running", step
    notice = self.session_notices.get(session_id) or {}
    return str(notice.get("status") or ""), str(notice.get("step") or "")


def _update_session_list_entry(self, session_id):
    """刷新一条会话的状态图标与提示；标题文本始终与状态无关。"""
    session_id = str(session_id)
    item = self._session_row_items.get(session_id)
    if item is None:
        return
    base_text = item.data(Qt.ItemDataRole.UserRole + 1) or ""
    state, step = self._session_indicator_state(session_id)
    # 关键：标题只写入不带状态的文本。状态画在图标位，宽度固定，
    # 因此无论运行、完成还是失败，标题都不会发生任何左右位移。
    if item.text() != base_text:
        item.setText(base_text)
    item.setIcon(self._session_indicator_icon(state) if state else QIcon())
    if state == "running":
        tooltip = "正在运行" + (f"\n当前步骤：{step}" if step else "")
        if not self._session_breath_timer.isActive():
            self._session_breath_timer.start()
    elif state == "completed":
        tooltip = "已完成，切换到该会话后此提示自动消失" + (f"\n完成步骤：{step}" if step else "")
    elif state == "failed":
        tooltip = "执行失败，切换到该会话后此提示自动消失" + (f"\n失败步骤：{step}" if step else "")
    else:
        tooltip = ""
    if not tooltip:
        tooltip = str(item.data(Qt.ItemDataRole.UserRole + 2) or "")
    item.setToolTip(tooltip)
    if not any(
        self._session_indicator_state(sid)[0] == "running" for sid in self._session_row_items
    ):
        self._session_breath_timer.stop()


def _session_indicator_icon(self, state):
    """生成固定尺寸的状态图标，颜色随呼吸相位变化。"""
    size = 12
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    phase = self._session_breath_phase if state == "running" else 0
    color = _session_indicator_color(state, phase)
    if state == "failed":
        painter.setPen(QPen(color, 2))
        painter.drawEllipse(1, 1, size - 2, size - 2)
        painter.drawLine(4, 4, size - 4, size - 4)
        painter.drawLine(size - 4, 4, 4, size - 4)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.end()
    return QIcon(pixmap)


def _advance_session_breath(self):
    """只切换呼吸相位并刷新颜色；不触碰文本，不引起任何布局变化。"""
    self._session_breath_phase = 1 - self._session_breath_phase
    running = False
    for session_id, item in self._session_row_items.items():
        state, _step = self._session_indicator_state(session_id)
        if state != "running":
            continue
        running = True
        item.setIcon(self._session_indicator_icon("running"))
    if not running:
        self._session_breath_timer.stop()


def _set_session_notice(self, session_id, status, step=""):
    session_id = str(session_id)
    if status in ("completed", "failed"):
        self.session_notices[session_id] = {"status": status, "step": str(step or "")}
    else:
        self.session_notices.pop(session_id, None)
    self._update_session_list_entry(session_id)


def _on_session_item_activated(self, item):
    """点击会话行即切换；完成/失败提示由 switch_session 顺带清除。"""
    session_id = item.data(Qt.ItemDataRole.UserRole)
    if not session_id:
        return
    self.switch_session(str(session_id))


def _session_id_at(self, position):
    item = self.session_list.itemAt(position)
    if not item:
        return None, item
    session_id = item.data(Qt.ItemDataRole.UserRole)
    return (str(session_id) if session_id else None), item


def _show_session_context_menu(self, position):
    session_id, item = self._session_id_at(position)
    if not session_id:
        return
    metadata = next((row for row in self.db.get_session_metadata() if row["session_id"] == session_id), None)
    if not metadata:
        return
    menu = QMenu(self)
    open_action = menu.addAction("打开会话")
    rename_action = menu.addAction("重命名")
    pin_action = menu.addAction("取消置顶" if metadata.get("pinned") else "置顶会话")
    menu.addSeparator()
    delete_action = menu.addAction("删除会话")
    action = menu.exec(self.session_list.mapToGlobal(position))
    if action == open_action:
        self.switch_session(session_id)
    elif action == rename_action:
        self._rename_session(session_id, metadata.get("title") or metadata.get("preview") or "")
    elif action == pin_action:
        self.db.set_session_pinned(session_id, not metadata.get("pinned"))
        self.refresh_session_list()
    elif action == delete_action:
        self._delete_session_from_panel(session_id)


def _rename_session(self, session_id, old_title):
    title, accepted = QInputDialog.getText(self, "重命名会话", "会话名称:", text=old_title[:80])
    if accepted and title.strip():
        self.db.rename_session(session_id, title.strip())
        self.refresh_session_list()


def _delete_session_from_panel(self, session_id):
    reply = QMessageBox.question(
        self,
        "确认删除",
        "确定删除这个会话及其历史消息吗？",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if reply != QMessageBox.StandardButton.Yes:
        return
    self._delete_session(session_id)


def _delete_session(self, session_id):
    """删除会话；若删的是当前窗口，则切到一个干净的新会话。"""
    is_current = session_id == self.current_session_id
    if is_current:
        self._discard_current_generation()
    self._stop_session_thread(session_id)
    self.db.delete_session(session_id)
    if getattr(self, "_memory", None) is not None:
        self._memory.note_session_deleted(session_id)
    self.session_notices.pop(str(session_id), None)
    self._set_session_task_state(None, session_id=session_id)
    if is_current:
        self._show_temporary_session("当前会话已删除，等待新消息")
        return
    self.refresh_session_list()


def _on_session_splitter_moved(self, position, index):
    if not self.session_panel.isVisible() or not hasattr(self, "session_splitter"):
        return
    sizes = self.session_splitter.sizes()
    if not sizes or sizes[0] <= 0:
        return
    self.session_panel_width = max(220, min(380, sizes[0]))
    self.config.settings.setValue("session_panel_width", self.session_panel_width)


def toggle_session_panel(self, visible):
    visible = bool(visible)
    if visible:
        self.session_panel.show()
        self.session_splitter.setSizes([self.session_panel_width, max(1, self.session_splitter.width() - self.session_panel_width)])
    else:
        sizes = self.session_splitter.sizes()
        if sizes and sizes[0] > 0:
            self.session_panel_width = max(220, min(380, sizes[0]))
            self.config.settings.setValue("session_panel_width", self.session_panel_width)
        self.session_panel.hide()


def show_session_list(self):
    """显示历史会话列表对话框，支持双击切换和右键/按钮删除（含当前会话）"""
    sessions = self.db.get_session_info()
    if not sessions:
        QMessageBox.information(self, "历史会话", "暂无历史会话记录")
        return

    dialog = QDialog(self)
    dialog.setWindowTitle("历史会话")
    dialog.setMinimumWidth(500)
    layout = QVBoxLayout(dialog)

    list_widget = QListWidget()
    for sid, last_time, preview in sessions:
        time_str = datetime.fromisoformat(last_time).strftime("%Y-%m-%d %H:%M") if last_time else "未知"
        preview_text = (preview[:40] + "...") if preview and len(preview) > 40 else (preview or "无消息")
        display_text = f"{sid}\n{time_str}\n{preview_text}"
        item = QListWidgetItem(display_text)
        item.setData(Qt.ItemDataRole.UserRole, sid)
        list_widget.addItem(item)

    layout.addWidget(QLabel("双击会话切换，右键或点击下方按钮删除"))

    btn_layout = QHBoxLayout()
    delete_btn = QPushButton("删除选中会话")
    close_btn = QPushButton("关闭")
    btn_layout.addWidget(delete_btn)
    btn_layout.addWidget(close_btn)
    layout.addLayout(btn_layout)
    layout.addWidget(list_widget)

    def delete_selected():
        current_item = list_widget.currentItem()
        if not current_item:
            QMessageBox.warning(dialog, "提示", "请先选择一个会话")
            return
        session_id = current_item.data(Qt.ItemDataRole.UserRole)
        if session_id == self.current_session_id:
            confirm_text = "确定删除当前正在使用的会话吗？删除后会切换到新的空会话。"
        else:
            confirm_text = f"确定要删除会话 {session_id} 吗？"
        reply = QMessageBox.question(dialog, "确认删除", confirm_text,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._delete_session(session_id)
            row = list_widget.row(current_item)
            list_widget.takeItem(row)
            QMessageBox.information(dialog, "成功", "会话已删除")
            if list_widget.count() == 0:
                QMessageBox.information(dialog, "提示", "所有历史会话已清空")
                dialog.accept()

    def on_double_click(item):
        session_id = item.data(Qt.ItemDataRole.UserRole)
        self.switch_session(session_id)
        dialog.accept()

    def show_context_menu(pos):
        item = list_widget.itemAt(pos)
        if item:
            menu = QMenu()
            menu.addAction("删除此会话")
            action = menu.exec(list_widget.mapToGlobal(pos))
            if action and action.text() == "删除此会话":
                list_widget.setCurrentItem(item)
                delete_selected()

    list_widget.itemDoubleClicked.connect(on_double_click)
    list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    list_widget.customContextMenuRequested.connect(show_context_menu)
    delete_btn.clicked.connect(delete_selected)
    close_btn.clicked.connect(dialog.accept)

    dialog.exec()
