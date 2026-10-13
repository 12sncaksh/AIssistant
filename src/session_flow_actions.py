"""Session lifecycle: history, placeholders, switching."""

from __future__ import annotations
from PyQt6.QtWidgets import QListWidgetItem
from PyQt6.QtWidgets import QMessageBox
from app_prompts import WORLD_CUP_SCOPE_ALIASES
from chat_markdown import attach_sources_to_text
from datetime import datetime
from dsh_acp_client import current_connection
from task_progress_widget import TaskProgressWidget
from task_state import create_task_state
from task_state import now_iso_timestamp
from task_state import touch_task_state
from task_state import upsert_task_plan_step
from text_protocol import extract_kaomoji_tag
from text_protocol import parse_sources_block
from text_protocol import split_reply_and_sources
import copy
import logging
import time



def _history_page_size(self):
    try:
        return max(5, int(self.config.get_chat_history_page_size()))
    except Exception:
        return 10


def _create_history_widgets(self, rows, at_top=False):
    """批量创建历史消息气泡。

    at_top=True 时按时间倒序插入（每次都插到第 0 行），最终顺序才是正序。
    返回插入内容的总像素高度，供「加载更早」保持滚动位置。
    """
    if not rows:
        return 0
    inserted_height = 0
    # 历史加载不能改变「自动跟随」状态：用户上滑补页时正在读旧内容，
    # 若被历史里的用户消息重新打开跟随，下一段流式输出就会把视图拽回底部。
    previous_follow = getattr(self, "_auto_follow_output", True)
    self.chat_list.setUpdatesEnabled(False)
    self.chat_list.blockSignals(True)
    try:
        with self._suppress_chat_scroll_events():
            sequence = list(reversed(rows)) if at_top else list(rows)
            for row in sequence:
                sources = row.get("sources") or []
                self.add_message(
                    row["content"],
                    is_user=(row["role"] == "user"),
                    store=False,
                    sources=sources,
                    follow=False,
                    insert_at_top=at_top,
                )
                if at_top:
                    item = self.chat_list.item(0)
                    if item is not None:
                        inserted_height += item.sizeHint().height() + self.chat_list.spacing()
    finally:
        self.chat_list.blockSignals(False)
        self.chat_list.setUpdatesEnabled(True)
        self._set_auto_follow_output(previous_follow)
    return inserted_height


def reset_history_pagination(self):
    """切换/清空会话时重置分页账本。"""
    self.session_history_rows = []
    self._first_visible_index = 0
    self._loading_earlier = False


def load_history_from_db(self):
    """载入会话历史。

    完整历史仍然进入 conversation_history 和记忆索引；界面只创建最近一页，
    更早的消息在用户上滑到顶部时再补。切换卡顿就出在这里：以前是一次性为
    全部历史消息创建 QTextBrowser 并逐条排版。
    """
    started = time.perf_counter()
    rows = self.db.load_all_session_rows(self.current_session_id)
    db_return_ms = (time.perf_counter() - started) * 1000
    if rows is None:
        # 读取失败 ≠ 空会话：保留现有界面与内存历史，避免一次 DB 抖动被当成
        # 「会话被清空」，从而静默丢掉全部上下文。
        logging.warning("读取本地历史失败，跳过界面重建 session=%s", str(self.current_session_id)[:12])
        self.status_bar.showMessage("读取本地历史失败，已保留当前会话", 5000)
        return
    self.session_history_rows = list(rows)
    self._first_visible_index = len(rows)
    self._loading_earlier = False
    # 无论有没有历史，都先把内存历史重置为「系统提示词」，避免空会话分支
    # 残留上一个会话的对话内容。
    self.conversation_history = self._build_system_prompt_messages()
    if not rows:
        self.session_is_persisted = False
        self.add_message("你好呀！我是你的桌面 AI 助手，有什么想做的吗？(｡•ᴗ•｡)", is_user=False, store=False)
        logging.info(
            "[perf] history_ui session=%s messages=0 rendered=0 db_return_ms=%.1f ui_ms=%.1f layout_ms=0.0 total_ms=%.1f",
            str(self.current_session_id)[:12],
            db_return_ms,
            (time.perf_counter() - started) * 1000 - db_return_ms,
            (time.perf_counter() - started) * 1000,
        )
        return
    self.session_is_persisted = True
    for row in rows:
        sources = row.get("sources") or []
        self.conversation_history.append({
            "role": row["role"],
            "content": attach_sources_to_text(row["content"], sources),
        })
    page_size = self._history_page_size()
    start = max(0, len(rows) - page_size)
    render_started = time.perf_counter()
    self._create_history_widgets(rows[start:], at_top=False)
    render_ms = (time.perf_counter() - render_started) * 1000
    self._first_visible_index = start
    layout_started = time.perf_counter()
    self.chat_list.doItemsLayout()
    self._follow_latest_output()
    layout_ms = (time.perf_counter() - layout_started) * 1000
    text_chars = sum(len(str(row.get("content") or "")) for row in rows)
    logging.info(
        "[perf] history_ui session=%s messages=%d rendered=%d chars=%d db_return_ms=%.1f ui_ms=%.1f layout_ms=%.1f total_ms=%.1f",
        str(self.current_session_id)[:12],
        len(rows),
        len(rows) - start,
        text_chars,
        db_return_ms,
        render_ms,
        layout_ms,
        (time.perf_counter() - started) * 1000,
    )
    if getattr(self, "_memory", None) is not None:
        self._memory.note_session_loaded(self.current_session_id)


def load_earlier_history(self):
    """把上一页更早的消息插到列表顶部，并保持用户当前阅读位置不动。"""
    if getattr(self, "_loading_earlier", False):
        return False
    start = int(getattr(self, "_first_visible_index", 0) or 0)
    if start <= 0:
        return False
    rows = getattr(self, "session_history_rows", None) or []
    if not rows:
        return False
    page_size = self._history_page_size()
    new_start = max(0, start - page_size)
    batch = rows[new_start:start]
    if not batch:
        return False
    self._loading_earlier = True
    started = time.perf_counter()
    try:
        scroll_bar = self.chat_list.verticalScrollBar()
        previous_value = scroll_bar.value()
        inserted_height = self._create_history_widgets(batch, at_top=True)
        self._first_visible_index = new_start
        self.chat_list.doItemsLayout()
        with self._suppress_chat_scroll_events():
            scroll_bar.setValue(previous_value + inserted_height)
    finally:
        self._loading_earlier = False
    logging.info(
        "[perf] history_more session=%s added=%d rendered=%d remaining=%d elapsed_ms=%.0f",
        str(self.current_session_id)[:12],
        len(batch),
        len(rows) - new_start,
        new_start,
        (time.perf_counter() - started) * 1000,
    )
    if new_start == 0:
        self.status_bar.showMessage("已加载全部历史记录", 2000)
    return True


def _maybe_load_earlier_history(self):
    """滚动条触顶时补加载更早的历史消息。"""
    if getattr(self, "_loading_earlier", False):
        return False
    if int(getattr(self, "_first_visible_index", 0) or 0) <= 0:
        return False
    if self.chat_list.verticalScrollBar().value() > 0:
        return False
    return self.load_earlier_history()


def _get_session_task_state(self, session_id=None):
    target_session_id = session_id or self.current_session_id
    state = self.session_task_states.get(target_session_id)
    return copy.deepcopy(state) if state else None


def _set_session_task_state(self, task_state, session_id=None):
    target_session_id = session_id or self.current_session_id
    if task_state is None:
        self.session_task_states.pop(target_session_id, None)
        if target_session_id == self.current_session_id:
            self.current_task_state = None
        self._update_session_list_entry(target_session_id)
        return

    snapshot = copy.deepcopy(task_state)
    self.session_task_states[target_session_id] = snapshot
    if target_session_id == self.current_session_id:
        self.current_task_state = copy.deepcopy(snapshot)
    self._update_session_list_entry(target_session_id)


def _restore_input_controls(self):
    self.action_stack.setCurrentWidget(self.send_btn)
    self.input_text.setEnabled(True)
    self.send_btn.setEnabled(True)
    self.attach_btn.setEnabled(True)
    self.input_text.setFocus()


def _discard_current_generation(self):
    """只解绑当前界面，不再打断仍在后台执行的回答。"""
    self.current_api_thread = None
    row = self._placeholder_row()
    if row >= 0:
        self.chat_list.takeItem(row)
    self.placeholder_item = None
    self.placeholder_widget = None
    self.stream_buffer = ""
    self.stream_display_buffer = ""
    self.chat_stream_filter.reset()
    self._restore_input_controls()


def _show_temporary_session(self, status_message=""):
    """显示未持久化的新会话，直到用户发送消息才创建数据库记录。"""
    self._set_auto_follow_output(True)
    self.current_session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    self.session_is_persisted = False
    self._set_session_task_state(None, session_id=self.current_session_id)
    self.chat_list.clear()
    self.reset_history_pagination()
    self.load_system_prompt()
    self.add_message(
        "你好呀！我是你的桌面 AI 助手，有什么想做的吗？(｡•ᴗ•｡)",
        is_user=False,
        store=False,
    )
    self.refresh_session_list()
    if status_message:
        self.status_bar.showMessage(status_message, 3000)
    self._set_kaomoji_display(
        text="(｡•ᴗ•｡)",
        style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;",
    )


def _create_task_placeholder(self, task_state=None):
    """创建任务占位气泡（当前会话正在执行时使用）。"""
    self.placeholder_item = QListWidgetItem()
    self.placeholder_widget = TaskProgressWidget(
        opacity=self.bubble_opacity,
        avatar_path=self.config.get_avatar_path("assistant"),
        font_family=self.chat_font_family,
        font_size=self.chat_font_size,
        bubble_color=self.bubble_assistant_color,
    )
    self.placeholder_widget.avatar_clicked.connect(self.show_avatar_dialog)
    self.placeholder_item.setSizeHint(self.placeholder_widget.sizeHint())
    self.placeholder_widget.list_item = self.placeholder_item
    self.chat_list.addItem(self.placeholder_item)
    self.chat_list.setItemWidget(self.placeholder_item, self.placeholder_widget)
    self.placeholder_widget.refresh_layout()
    if task_state:
        self.placeholder_widget.update_progress(task_state)
    self._follow_latest_output()


def _restore_running_session_ui(self, session_id):
    """切回一个仍在后台执行的会话：恢复停止按钮与流式气泡。"""
    thread = self.session_threads.get(session_id)
    if thread is None or not thread.isRunning():
        return
    self.current_api_thread = thread
    state = self.session_stream.setdefault(session_id, {"display": "", "pending": True})
    self.stream_buffer = ""
    self.stream_display_buffer = state.get("display", "")
    self.chat_stream_filter.reset()
    self.input_text.setEnabled(False)
    self.send_btn.setEnabled(False)
    self.attach_btn.setEnabled(False)
    self.stop_btn.setEnabled(True)
    self.action_stack.setCurrentWidget(self.stop_btn)
    self._create_task_placeholder(self.current_task_state)
    if self.placeholder_widget is not None:
        temporary_text = self.stream_display_buffer or state.get("process", "")
        if temporary_text:
            try:
                self.placeholder_widget.set_text(temporary_text)
            except RuntimeError:
                pass
    self.status_bar.showMessage("该会话仍在后台执行…", 0)
    self._follow_latest_output()


def _on_session_thread_finished(self):
    thread = self.sender()
    for session_id, active_thread in list(self.session_threads.items()):
        if active_thread is thread:
            self.session_threads.pop(session_id, None)
            self._update_session_list_entry(session_id)


def _stop_session_thread(self, session_id):
    """删除会话时确保它的后台线程停下，并释放它占用的 DSH 会话。"""
    thread = self.session_threads.pop(session_id, None)
    self.session_stream.pop(session_id, None)
    # 先停线程再 forget：forget 会清掉本地会话键到 DSH sessionId 的映射，
    # 之前放在 stop 之前，导致取消通知查不到会话、暂停静默失效。
    if thread is not None:
        try:
            thread.stop()
            if not thread.wait(1500):
                thread.terminate()
                thread.wait()
        except RuntimeError:
            pass
    try:
        connection = current_connection()
        if connection is not None:
            connection.forget_session(session_id)
    except Exception:
        pass


def _finish_background_reply(self, sid, full_response):
    """后台会话完成：只落库，不动当前界面、桌宠与语音。"""
    task_state = self.session_task_states.get(sid) or {}
    self._set_session_notice(sid, "completed", task_state.get("current_step") or "已完成")
    self.session_stream.pop(sid, None)
    _kaomoji, clean = extract_kaomoji_tag(full_response or "")
    clean = self._format_assistant_text(clean)
    body, sources_block = split_reply_and_sources(clean)
    sources = parse_sources_block(sources_block)
    if not body:
        return
    try:
        self.db.save_message(sid, "assistant", body, sources=sources)
    except Exception as exc:
        print(f"[WARN] 后台会话落库失败: {exc}")
    self.refresh_session_list()


def _placeholder_row(self):
    """占位气泡当前所在行；已被清空/删除时返回 -1。"""
    item = getattr(self, "placeholder_item", None)
    if item is None:
        return -1
    try:
        return self.chat_list.row(item)
    except RuntimeError:
        return -1


def _on_status_update(self, text, sid=None):
    if (sid or self.current_session_id) != self.current_session_id:
        return
    self.status_bar.showMessage(text, 0)


def _on_task_state_updated(self, task_state, sid=None):
    if sid is None and self.sender() is not self.current_api_thread:
        return
    self._set_session_task_state(task_state, session_id=sid)
    if sid is not None and sid != self.current_session_id:
        return
    self._sync_pet_task_activity(task_state)
    placeholder = getattr(self, "placeholder_widget", None)
    if placeholder is None:
        return
    try:
        if isinstance(placeholder, TaskProgressWidget):
            placeholder.update_progress(task_state)
    except RuntimeError:
        pass
    self._follow_latest_output()


def _on_computer_state(self, state, sid=None):
    if sid is not None and sid != self.current_session_id:
        return
    if self.pet_window:
        if str(state) in ("waiting_authorization", "waiting_external"):
            self.pet_window.set_pet_activity("waiting", "等你确认一下…")
        elif str(state) not in ("finished", "guard_failed"):
            self.pet_window.set_pet_activity("working")
    labels = {
        "waiting_authorization": "等待用户授权视觉键鼠操作…",
        "q_listener_active": "Q 取消监听已启动，AI 正在准备接管…",
        "input_guard_active": "输入接管已启用，AI 正在操作屏幕…",
        "waiting_vision": "正在等待视觉模型返回…",
        "observing": "AI 正在读取屏幕…",
        "observing_region": "AI 正在读取局部区域…",
        "locating_window": "AI 正在定位前台窗口…",
        "reading_accessibility": "AI 正在读取 UI 可访问性树…",
        "moving_mouse": "AI 正在移动鼠标…",
        "clicking": "AI 正在点击…",
        "verifying_click": "AI 正在验证点击目标…",
        "typing": "AI 正在输入…",
        "pressing_hotkey": "AI 正在执行快捷键…",
        "waiting_interface": "等待界面更新…",
        "waiting_external": "等待你处理目标窗口中的外部确认…",
        "stopping": "正在停止视觉接管并释放控制权…",
    }
    if state == "finished":
        self.computer_overlay.show_finished()
        return
    if state == "guard_failed":
        self.computer_overlay.show_finished("输入接管失败，任务已停止")
        return
    self.computer_overlay.show_status(labels.get(state, "AI 正在操作屏幕…"))


def _sync_pet_task_activity(self, task_state):
    """把任务状态映射成桌宠活动反馈，工作期间用气泡播报当前步骤。"""
    if not self.pet_window:
        return
    state = dict(task_state or {})
    status = str(state.get("status") or "")
    step = str(state.get("current_step") or "").strip()
    if status == "executing":
        self.pet_window.set_pet_activity("working", step or "正在处理…")
    elif status == "planning":
        self.pet_window.set_pet_activity("thinking")
    elif status == "failed":
        self.pet_window.set_pet_activity("idle")


def _build_world_cup_clarification(self, user_text: str, task_state: dict) -> dict:
    question = "你想查哪一种世界杯赛况？请直接告诉我是 2026 世界杯正赛、2026 世界杯预选赛，还是国际足联世俱杯。"
    clarification = {
        "required": True,
        "kind": "world_cup_scope",
        "reason": "世界杯相关请求缺少赛事类型，无法直接联网检索。",
        "question": question,
        "options": list(WORLD_CUP_SCOPE_ALIASES.keys()),
        "answer": "",
        "resolved_value": "",
        "asked_at": now_iso_timestamp(),
        "resolved_at": ""
    }
    task_state["latest_user_message"] = user_text
    task_state["clarification"] = clarification
    touch_task_state(task_state, status="needs_clarification", current_step="等待用户补充赛事类型")
    upsert_task_plan_step(task_state, "clarify", "澄清关键信息", "awaiting_user", clarification["reason"])
    return task_state


def _evaluate_clarification_need(self, user_text: str) -> dict:
    pending_state = self._get_session_task_state()
    if pending_state and pending_state.get("status") == "needs_clarification":
        clarification = pending_state.get("clarification", {})
        if clarification.get("kind") == "world_cup_scope":
            resolved_scope = self._resolve_world_cup_scope(user_text)
            if resolved_scope:
                pending_state["turn_count"] = int(pending_state.get("turn_count", 1)) + 1
                pending_state["latest_user_message"] = user_text
                pending_state["clarification"]["required"] = False
                pending_state["clarification"]["answer"] = user_text
                pending_state["clarification"]["resolved_value"] = resolved_scope
                pending_state["clarification"]["resolved_at"] = now_iso_timestamp()
                touch_task_state(pending_state, status="planning", current_step="整合补充信息")
                upsert_task_plan_step(
                    pending_state,
                    "clarify",
                    "澄清关键信息",
                    "completed",
                    f"已确认赛事类型：{resolved_scope}"
                )
                return {"action": "continue", "task_state": pending_state}

            if len(user_text.strip()) <= 10:
                pending_state["turn_count"] = int(pending_state.get("turn_count", 1)) + 1
                pending_state["latest_user_message"] = user_text
                touch_task_state(pending_state, status="needs_clarification", current_step="等待用户补充赛事类型")
                upsert_task_plan_step(
                    pending_state,
                    "clarify",
                    "澄清关键信息",
                    "awaiting_user",
                    "回复仍然不够明确，继续等待用户确认是正赛、预选赛还是世俱杯。"
                )
                return {
                    "action": "ask",
                    "task_state": pending_state,
                    "question": f"我还没分清具体赛事类型哦。{clarification.get('question', '')}".strip()
                }

    if self._needs_world_cup_clarification(user_text):
        task_state = create_task_state(user_text)
        task_state = self._build_world_cup_clarification(user_text, task_state)
        return {
            "action": "ask",
            "task_state": task_state,
            "question": task_state["clarification"]["question"]
        }

    if pending_state and pending_state.get("status") == "needs_clarification":
        return {"action": "continue", "task_state": create_task_state(user_text)}

    return {"action": "continue", "task_state": create_task_state(user_text)}


def clear_conversation(self):
    reply = QMessageBox.question(self, "确认清空", "确定要清空当前会话的所有对话记录吗？",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    if reply == QMessageBox.StandardButton.Yes:
        self._stop_generation()
        self.chat_list.clear()
        self.reset_history_pagination()
        self._stop_session_thread(self.current_session_id)
        self.db.delete_session(self.current_session_id)
        if getattr(self, "_memory", None) is not None:
            self._memory.note_session_deleted(self.current_session_id)
        self.session_is_persisted = False
        self._set_session_task_state(None)
        self.load_system_prompt()
        self.add_message("对话历史已清空。有什么我可以帮助你的吗？", is_user=False, store=False)
        self.refresh_session_list()
        self.status_bar.showMessage("当前会话已清空", 2000)
        self._set_kaomoji_display(
            text="(｡•ᴗ•｡)",
            style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;"
        )


def new_session(self):
    self._discard_current_generation()
    self._show_temporary_session("新会话已准备好，等待新消息")


def switch_session(self, session_id):
    if session_id == self.current_session_id:
        return
    switch_started = time.perf_counter()
    self._set_auto_follow_output(True)
    # 会话的完成/失败提示在「切进去看」时就已送达，因此切换即视为已读并清除。
    # 这比按坐标判断点击位置可靠，也符合提示的语义。
    if session_id in self.session_notices:
        self.session_notices.pop(session_id, None)
    discard_started = time.perf_counter()
    self._discard_current_generation()
    discard_ms = (time.perf_counter() - discard_started) * 1000
    self.current_session_id = session_id
    self.current_task_state = self._get_session_task_state(session_id)
    self.chat_list.clear()
    prompt_started = time.perf_counter()
    self.load_system_prompt()
    prompt_ms = (time.perf_counter() - prompt_started) * 1000
    history_started = time.perf_counter()
    self.load_history_from_db()
    history_ms = (time.perf_counter() - history_started) * 1000
    refresh_started = time.perf_counter()
    self.refresh_session_list()
    refresh_ms = (time.perf_counter() - refresh_started) * 1000
    restore_started = time.perf_counter()
    self._restore_running_session_ui(session_id)
    restore_ms = (time.perf_counter() - restore_started) * 1000
    logging.info(
        "[perf] switch_session session=%s discard_ms=%.1f prompt_ms=%.1f history_ms=%.1f refresh_ms=%.1f restore_ms=%.1f total_ms=%.1f",
        str(session_id)[:12],
        discard_ms,
        prompt_ms,
        history_ms,
        refresh_ms,
        restore_ms,
        (time.perf_counter() - switch_started) * 1000,
    )
    self.status_bar.showMessage(f"已切换到会话: {session_id[:8]}...", 3000)
    self._set_kaomoji_display(
        text="(｡•ᴗ•｡)",
        style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;"
    )
