"""Sending a turn and consuming its streamed result."""

from __future__ import annotations
from PyQt6.QtWidgets import QListWidgetItem
from PyQt6.QtWidgets import QMessageBox
from api_thread import APICallThread
from chat_markdown import attach_sources_to_text
from chat_markdown import markdown_to_plain_text
from chat_message_widget import ChatMessageWidget
from context_builder import build_context
from dsh_session_thread import DSHSessionThread
import recall_intent
from functools import partial
from text_protocol import extract_kaomoji_tag
from text_protocol import parse_sources_block
from text_protocol import split_reply_and_sources
from tts_thread import TextToSpeechThread
from vision_input import VisionInputController
import logging



def _create_message_widget(self, message, is_user, sources=None, stream_log=None):
    """构建一个消息气泡控件（不加入列表），供正常追加与历史置顶复用。"""
    display_text = self._compact_user_message(message) if is_user else message
    avatar_path = self.config.get_avatar_path("user" if is_user else "assistant")
    render_markdown = not is_user and not self.config.get_roleplay_mode()
    widget = ChatMessageWidget(
        display_text,
        is_user,
        self.bubble_opacity,
        code_opacity=self.code_opacity,
        bubble_color=self.bubble_user_color if is_user else self.bubble_assistant_color,
        code_color=self.code_block_color,
        sources=sources,
        avatar_path=avatar_path,
        render_markdown=render_markdown,
        font_family=self.chat_font_family,
        font_size=self.chat_font_size,
        code_font_family=self.code_font_family,
        code_font_size=self.code_font_size,
        stream_log=stream_log,
    )
    widget.avatar_clicked.connect(self.show_avatar_dialog)
    item = QListWidgetItem()
    item.setSizeHint(widget.sizeHint())
    widget.list_item = item
    return item, widget


def _track_history_row(self, role, message, sources, message_id):
    """把落库的新消息同步进分页账本，保证「加载更早」的边界正确。"""
    rows = getattr(self, "session_history_rows", None)
    if not isinstance(rows, list) or not message_id:
        return
    rows.append({
        "id": int(message_id),
        "role": role,
        "content": message,
        "sources": sources or [],
    })


def add_message(
    self,
    message,
    is_user=True,
    store=True,
    sources=None,
    follow=True,
    insert_at_top=False,
    stream_log=None,
    store_text=None,
):
    # 发消息必然想看到回复；助手消息只在「本来就在底部」时才继续跟随，
    # 用户正在翻历史时不抢他的位置（生成结束时占位气泡换成正式回复属于前者）。
    if is_user or self._is_chat_at_bottom():
        self._set_auto_follow_output(True)
    item, widget = self._create_message_widget(message, is_user, sources, stream_log=stream_log)
    if insert_at_top:
        self.chat_list.insertItem(0, item)
    else:
        self.chat_list.addItem(item)
    self.chat_list.setItemWidget(item, widget)
    widget.refresh_layout()
    if follow:
        self._follow_latest_output()
    if store:
        role = "user" if is_user else "assistant"
        # 显示的和存的不一定是同一份：气泡里放"最终结果"，存储放全量。
        stored_body = store_text if (store_text and not is_user) else message
        history_content = stored_body if is_user else attach_sources_to_text(stored_body, sources)
        self.conversation_history.append({"role": role, "content": history_content})
        if role == "user" or self.session_is_persisted:
            message_id = self.db.save_message(
                self.current_session_id,
                role,
                stored_body,
                sources=sources,
                stream_log=stream_log if not is_user else None,
            )
            self.session_is_persisted = True
            self._track_history_row(role, stored_body, sources, message_id)
            self.refresh_session_list()
            memory = getattr(self, "_memory", None)
            if memory is not None:
                memory.note_message_saved(self.current_session_id)


def split_stored_stream_log(content, stream_log):
    """把库里存的 content 拆成「显示正文」和「折叠日志」。

    存储约定：`content` = 正文 + "\\n\\n" + 过程日志。正文在前，和气泡里的顺序一致
    （实时是"最终结果在上、折叠块在下"，重载后必须同序，否则会读成"过程话是答复"）。

    所以日志必定是 content 的**后缀**，用 endswith 精确切开，不用猜分隔符。

    对不上就老实全平铺：用户编辑过消息、或者这是加列之前写的老数据。宁可少一个
    折叠入口，也不能把正文截错。
    """
    body = str(content or "")
    log = str(stream_log or "")
    if not log or not body.endswith(log):
        return body, ""
    head = body[: len(body) - len(log)].rstrip()
    return (head, log) if head else (body, "")


def _latest_user_text(history):
    """取会话里最后一条用户消息的纯文本。"""
    for message in reversed(history or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return recall_intent.message_text(message.get("content"))
    return ""


def _insert_system_block(messages, text):
    """把一段系统块插到所有 system 消息之后、对话之前。"""
    insert_at = 0
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "system":
            insert_at = index + 1
        else:
            break
    return list(messages[:insert_at]) + [{"role": "system", "content": text}] + list(messages[insert_at:])


def _local_full_recall_enabled(config):
    getter = getattr(config, "get_local_full_recall_enabled", None)
    try:
        return bool(getter()) if callable(getter) else True
    except Exception:
        logging.warning("读取本地完整回顾开关失败", exc_info=True)
        return True


def _recall_message_chars(config):
    """会话开头片段里每条消息的截断上限；与 DSH 补给共用同一个配置。"""
    getter = getattr(config, "get_dsh_seed_max_chars", None)
    try:
        return max(100, int(getter())) if callable(getter) else 4000
    except Exception:
        return 4000


def _build_api_messages(self):
    """本次请求真正发给模型的上下文。

    默认是「摘要 + 语义记忆 + 最近轮次」这套压缩上下文。但用户**明确在回忆历史**
    时必须走特殊通道 —— 否则模型只拿到压缩内容，答不出「把完整记录整理一下」
    或「第一句话是什么」这类问题。

    这两条通道原先只存在于 DSH（_full_recall_block / _session_start_block），
    本地 API 一直没有，导致同一个提问在两条路径上表现不一致。判断逻辑现在与
    DSH 共用 recall_intent，两边不会再漂移。
    """
    latest = _latest_user_text(self.conversation_history)

    if latest and recall_intent.wants_full_recall(latest) and _local_full_recall_enabled(self.config):
        logging.info(
            "本地 API：用户要求完整回顾，改用完整历史（%d 条）",
            len(self.conversation_history),
        )
        return self.conversation_history.copy()

    messages = _compact_api_messages(self)

    if latest and recall_intent.wants_session_start(latest):
        opening = recall_intent.format_session_start_block(
            self.conversation_history, per_message_chars=_recall_message_chars(self.config)
        )
        if opening:
            logging.info("本地 API：位置型提问，已在上下文里附加会话开头原文")
            messages = _insert_system_block(messages, opening)
    return messages


def _compact_api_messages(self):
    """压缩上下文：摘要 + 语义记忆 + 最近轮次（含两级降级）。"""
    memory = getattr(self, "_memory", None)
    if memory is None:
        return _fallback_api_messages(self)
    try:
        built = memory.build_context(
            self.current_session_id,
            self.conversation_history,
            rows=getattr(self, "session_history_rows", None),
        )
        messages = built.get("messages") or []
        if messages:
            return messages
    except Exception:
        logging.warning("构建记忆上下文失败，回退到最近轮次", exc_info=True)
    return _fallback_api_messages(self)


def _fallback_api_messages(self):
    """记忆层不可用时的安全降级：仍然只发最近轮次，不发全量历史。"""
    try:
        recent = max(2, int(self.config.get_memory_recent_messages()))
        built = build_context(
            self._build_system_prompt_messages(),
            self.conversation_history,
            recent_messages=recent,
            max_chars=self.config.get_memory_context_max_chars(),
        )
        return built["messages"]
    except Exception:
        logging.warning("最近轮次上下文构建失败，回退到完整历史", exc_info=True)
        return self.conversation_history.copy()


def send_message(self):
    user_msg = self.input_text.toPlainText().strip()
    attachments = getattr(self, "_pending_attachments", None) or []
    image_attachments = getattr(self, "_pending_image_attachments", None) or []
    if not user_msg and not attachments and not image_attachments:
        return
    # 新一轮输入应打断当前播报，避免旧语音与后续回复争用同一设备。
    self._stop_tts()

    if attachments or image_attachments:
        attachment_text = ""
        for att in attachments:
            attachment_text += f"【用户上传文件：{att['name']}】\n{att['content']}\n\n"
        for image in image_attachments:
            attachment_text += f"【用户图片：{image['name']}】\n\n"
        if user_msg:
            full_msg = (user_msg + "\n\n" + attachment_text).strip()
        else:
            full_msg = attachment_text.strip()
        task_state = None
    else:
        clarification_result = self._evaluate_clarification_need(user_msg)
        if clarification_result["action"] == "ask":
            self.add_message(user_msg, is_user=True)
            self._set_session_task_state(clarification_result["task_state"])
            self.input_text.clear()
            self.add_message(clarification_result["question"], is_user=False)
            self.status_bar.showMessage("等待你补充关键信息...", 3000)
            self._set_kaomoji_display(text="( •̀ ω •́ )✧")
            self._restore_input_controls()
            return
        full_msg = user_msg
        task_state = clarification_result["task_state"]

    dsh_bridge_enabled = self.config.get_dsh_bridge_enabled()
    if not dsh_bridge_enabled and not self.config.get_api_key():
        QMessageBox.warning(self, "缺少配置", "请先在 设置->API配置 中设置API Key")
        return

    self._set_session_notice(self.current_session_id, "")
    self._set_session_task_state(task_state)
    self.add_message(full_msg, is_user=True)

    def _apply_image_attachments(messages):
        if not image_attachments or not messages:
            return
        image_content = [{
            "type": "text",
            "text": user_msg or "请分析我上传的图片。",
        }]
        image_content.extend({
            "type": "image_url",
            "image_url": {"url": image["data_url"]},
        } for image in image_attachments)
        messages[-1]["content"] = image_content

    self.input_text.clear()
    self._clear_attachments()
    self.input_text.setEnabled(False)
    self.send_btn.setEnabled(False)
    self.attach_btn.setEnabled(False)
    self.stop_btn.setEnabled(True)
    self.action_stack.setCurrentWidget(self.stop_btn)
    self.status_bar.showMessage("AI正在思考...", 0)
    self.stream_buffer = ""
    self.stream_display_buffer = ""
    self.chat_stream_filter.reset()
    if self.pet_window:
        self.pet_window.begin_speech_stream()
    self._show_thinking_kaomoji()
    if self.pet_window:
        self.pet_window.set_pet_activity("thinking")

    if dsh_bridge_enabled:
        # DSH 自己维护会话上下文：这里仍然给它完整的本地历史，好让它在
        # 新建/重连时能做背景补给，并按需做语义检索。
        dsh_messages = self.conversation_history.copy()
        _apply_image_attachments(dsh_messages)
        self.current_api_thread = DSHSessionThread(
            dsh_messages,
            self.config,
            task_state=task_state,
            session_id=self.current_session_id
        )
    else:
        # 本地 API：不再发送全量历史，改为「摘要 + 语义记忆 + 最近轮次」。
        api_messages = self._build_api_messages()
        _apply_image_attachments(api_messages)
        self.current_api_thread = APICallThread(
            api_messages,
            self.config,
            stream=False,
            task_state=task_state
        )
    active_session = self.current_session_id
    self.session_threads[active_session] = self.current_api_thread
    self.session_stream[active_session] = {"display": "", "pending": True}
    self.current_api_thread.stream_chunk.connect(
        lambda chunk, sid=active_session: self.on_stream_chunk(chunk, sid)
    )
    process_signal = getattr(self.current_api_thread, "process_chunk", None)
    if process_signal is not None:
        process_signal.connect(
            lambda chunk, sid=active_session: self.on_process_chunk(chunk, sid)
        )
    self.current_api_thread.response_received.connect(
        lambda text, sid=active_session: self.on_response_complete(text, sid)
    )
    self.current_api_thread.error_occurred.connect(
        lambda msg, sid=active_session: self.on_api_error(msg, sid)
    )
    self.current_api_thread.tool_confirmation_requested.connect(self.on_tool_confirmation_requested)
    self.current_api_thread.status_update.connect(
        lambda text, sid=active_session: self._on_status_update(text, sid)
    )
    self.current_api_thread.computer_state.connect(
        lambda state, sid=active_session: self._on_computer_state(state, sid)
    )
    self.current_api_thread.task_state_updated.connect(
        lambda state, sid=active_session: self._on_task_state_updated(state, sid)
    )
    self.current_api_thread.stopped.connect(
        lambda sid=active_session: self.on_generation_stopped(sid)
    )
    self.current_api_thread.finished.connect(self._on_session_thread_finished)
    self.current_api_thread.start()
    self._update_session_list_entry(active_session)

    self._create_task_placeholder(task_state)


def on_process_chunk(self, chunk, sid=None):
    """显示 DSH 的临时过程块；正式回复首块到达后由回复流覆盖。"""
    sid = sid or self.current_session_id
    state = self.session_stream.setdefault(sid, {"display": "", "process": "", "pending": True})
    if not chunk:
        state["process"] = ""
    else:
        state["process"] = state.get("process", "") + str(chunk)
    if sid != self.current_session_id:
        return
    placeholder = getattr(self, "placeholder_widget", None)
    try:
        placeholder.set_text(state["process"])
    except RuntimeError:
        pass
    self._follow_latest_output()


def on_stream_chunk(self, chunk, sid=None):
    sid = sid or self.current_session_id
    state = self.session_stream.setdefault(sid, {"display": "", "process": "", "pending": True})
    if sid != self.current_session_id:
        state["display"] = state.get("display", "") + chunk
        state["process"] = ""
        return
    self.stream_buffer += chunk
    self.stream_display_buffer += self.chat_stream_filter.feed(chunk)
    state["display"] = self.stream_display_buffer
    state["process"] = ""
    if self.pet_window:
        self.pet_window.append_speech_chunk(chunk)
    placeholder = getattr(self, "placeholder_widget", None)
    try:
        placeholder.set_text(self.stream_display_buffer)
    except RuntimeError:
        pass
    self._follow_latest_output()


def _stop_generation(self):
    had_running = False
    active = self.session_threads.get(self.current_session_id) or self.current_api_thread
    if active is not None and active.isRunning():
        active.stop()
        had_running = True
    if any(thread.isRunning() for thread in self._tts_threads):
        self._stop_tts()
        had_running = True
    self.action_stack.setCurrentWidget(self.send_btn)
    if had_running:
        self.status_bar.showMessage("正在停止…（等待当前操作返回）", 0)


def on_generation_stopped(self, sid=None):
    sid = sid or self.current_session_id
    self._set_session_notice(sid, "")
    # 先取再 pop：桥接路径的内容全在 process 里（stream_chunk 只在兜底时才发），
    # 只看 stream_display_buffer 的话，中途停止会把整轮已经生成的内容丢掉。
    stopped_state = self.session_stream.get(sid) or {}
    self.session_stream.pop(sid, None)
    if sid != self.current_session_id:
        return
    if self.pet_window:
        self.pet_window.clear_pending_expression()
        self.pet_window.set_pet_activity("idle")
    stopped_thread = self.current_api_thread
    partial = (
        (stopped_state.get("process") or "")
        + self.stream_display_buffer
        + self.chat_stream_filter.feed("", final=True)
    ).strip()
    if self.pet_window:
        self.pet_window.finish_speech_stream(
            partial,
            self.kaomoji_label.text(),
            append_expression=False,
        )
    row = self._placeholder_row()
    if row >= 0:
        self.chat_list.takeItem(row)
        if partial:
            self.add_message(partial, is_user=False, store=True)
    self.placeholder_item = None
    self.placeholder_widget = None
    if getattr(stopped_thread, "_computer_session_requested", False):
        cancel_reason = VisionInputController.get_cancel_reason()
        if cancel_reason == "q":
            self.add_message(
                "视觉接管已取消，键鼠控制权已恢复。",
                is_user=False,
                store=False,
            )
        elif cancel_reason == "manual":
            self.add_message(
                "视觉键鼠任务已停止，键鼠控制权已恢复。",
                is_user=False,
                store=False,
            )
    self._restore_input_controls()
    self.status_bar.showMessage("已停止生成", 3000)
    self.current_api_thread = None
    self.stream_buffer = ""
    self.stream_display_buffer = ""
    self.chat_stream_filter.reset()


def on_response_complete(self, full_response, sid=None):
    sid = sid or self.current_session_id
    if sid != self.current_session_id:
        self._finish_background_reply(sid, full_response)
        return
    task_state = self.session_task_states.get(sid) or {}
    self._set_session_notice(sid, "completed", task_state.get("current_step") or "已完成")
    self.session_stream.pop(sid, None)
    clean_response = full_response   # 先初始化

    # 1. 提取并移除 kaomoji 标签
    kaomoji, clean_response = extract_kaomoji_tag(clean_response)

    clean_response = self._format_assistant_text(clean_response)
    final_reply = clean_response

    # 2. 本轮被折叠起来的那几段（DSH 一个回合可能发多条 agent message）。
    #    过滤必须放在**存储之前**：折叠只是收起，用户点开就看得见 ——
    #    内部推理不能靠"折起来"兜底，那是硬规则。
    stream_log = str(task_state.get("stream_log") or "").strip()
    if stream_log:
        _, stream_log = extract_kaomoji_tag(stream_log)
        stream_log = self._format_assistant_text(stream_log).strip()
    body, sources_block = split_reply_and_sources(final_reply)
    sources = parse_sources_block(sources_block)
    spoken_reply = markdown_to_plain_text(body)

    # 移除占位消息
    row = self._placeholder_row()
    if row < 0:
        # 生成期间用户已清空/切换会话：丢弃本次回复，只恢复界面
        self.placeholder_item = None
        self.placeholder_widget = None
        self._restore_input_controls()
        self.status_bar.showMessage("已停止生成", 3000)
        self.current_api_thread = None
        self.stream_buffer = ""
        self.stream_display_buffer = ""
        self.chat_stream_filter.reset()
        return
    self.chat_list.takeItem(row)
    self.placeholder_item = None

    # 添加最终回复。气泡里只放"最终结果"，被折叠的那几段挂成 stream_log；
    # 而**存储用全量** —— 分开是因为"给人看的"和"给记忆用的"不是一回事：
    # 只存最终结果的话，摘要和 search_memory 永远看不到前面那几段。
    # 顺序是「正文 + 过程日志」：重载后要跟实时视图同序（答复在上、折叠在下），
    # 反过来会让人把过程话当成答复读（`split_stored_stream_log` 靠这个后缀约定切开）。
    self.add_message(
        body,
        is_user=False,
        sources=sources,
        stream_log=stream_log or None,
        store_text=f"{body}\n\n{stream_log}" if stream_log else None,
    )
    print(f"[DEBUG] AI回复原文: {clean_response}")

    # 更新颜文字：优先使用 AI 输出的标签
    if kaomoji:
        self._set_kaomoji_display(text=kaomoji)
    elif spoken_reply:
        self._update_kaomoji_by_text(spoken_reply)   # 后备方案：关键词匹配
    else:
        self._set_kaomoji_display(
            text="(｡•ᴗ•｡)",
            style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;"
        )  # 回复为空时显示默认表情

    # 语音输出
    has_pending_expression = bool(
        self.pet_window and self.pet_window.has_pending_expression()
    )
    if self.config.get_tts_enabled() and spoken_reply:
        self._stop_tts()
        voice = self.config.get_tts_voice()
        speech_pages = self.pet_window.get_speech_pages(spoken_reply) if self.pet_window else [spoken_reply]
        if self.pet_window:
            self.pet_window.prepare_tts_speech(speech_pages, self.kaomoji_label.text())
        tts_thread = TextToSpeechThread(speech_pages, voice)
        self.tts_thread = tts_thread
        self._tts_threads.append(tts_thread)
        tts_thread.page_started.connect(self._on_tts_page_started)
        tts_thread.error.connect(self._on_tts_error)
        tts_thread.finished.connect(self._on_tts_finished)
        if self.wakeword_thread:
            self.wakeword_thread.set_paused(True)
            self.wakeword_thread.suspend()
        tts_thread.start()
    elif self.pet_window:
        self.pet_window.finish_speech_stream(spoken_reply, self.kaomoji_label.text())
        # 没有 TTS 时，回复显示即播放缓存表情
        self.pet_window.activate_pending_expression()

    if self.pet_window:
        if has_pending_expression:
            # 本回合有表情：跳过「完成」庆祝，让情绪 + 说话主导
            self.pet_window.set_pet_activity("idle")
        else:
            self.pet_window.set_pet_activity("done")

    # 恢复输入控件
    self._restore_input_controls()
    self.status_bar.showMessage("就绪", 2000)
    self.current_api_thread = None
    self.stream_buffer = ""
    self.stream_display_buffer = ""
    self.chat_stream_filter.reset()


def raise_permission_dialog(self):
    dialog = self._permission_dialog
    if dialog is None:
        return
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def on_api_error(self, error_msg, sid=None):
    sid = sid or self.current_session_id
    task_state = self.session_task_states.get(sid) or {}
    self._set_session_notice(sid, "failed", task_state.get("current_step") or "任务执行")
    self.session_stream.pop(sid, None)
    if sid != self.current_session_id:
        logging.error("后台会话 API 调用错误（session=%s）：%s", sid, error_msg)
        return
    failed_session_id = self.current_session_id
    preserved_messages = len(self.conversation_history)
    logging.error("API 调用错误：%s", error_msg)
    row = self._placeholder_row()
    if row >= 0:
        self.chat_list.takeItem(row)
    self.placeholder_item = None
    self.placeholder_widget = None
    QMessageBox.critical(self, "API调用错误", error_msg)
    self.add_message(
        f"这次回复没有成功，但当前会话上下文已保留。你可以直接继续输入。\n原因：{error_msg}",
        is_user=False,
        store=False,
    )
    logging.info(
        "API失败后保留会话上下文：session=%s，消息数=%d",
        failed_session_id,
        preserved_messages,
    )
    self._restore_input_controls()
    self.status_bar.showMessage("错误: " + error_msg, 5000)
    self.current_api_thread = None
    self.stream_buffer = ""
    self.stream_display_buffer = ""
    self.chat_stream_filter.reset()
    if self.pet_window:
        self.pet_window.cancel_speech()
        self.pet_window.set_pet_activity("idle")
    self._set_kaomoji_display(text="(｡•́︿•̀｡)")
