"""Chat request worker thread."""

from __future__ import annotations
from PyQt6.QtCore import QThread
from PyQt6.QtCore import pyqtSignal
from action_control import ActionHandler
from app_paths import get_runtime_dir
from chat_markdown import attach_sources_to_text
from chat_markdown import strip_internal_reasoning
from datetime import datetime
from functools import partial
from mcp_client import MCPManager
from task_state import append_task_tool_result
from task_state import create_task_state
from task_state import touch_task_state
from task_state import upsert_task_plan_step
from text_protocol import DSML_INVOKE_PATTERN
from text_protocol import DSML_PARAMETER_PATTERN
from text_protocol import DSML_TAG_PATTERN
from text_protocol import parse_text_tool_calls
from text_protocol import sanitize_reply_text
from vision_input import VisionInputController
import http_client
import copy
import json
import logging
import os
import re
import requests
import sys
import threading
import time
import uuid

from app_logging import _log_safe_args
from app_prompts import DATETIME_FORCE_KEYWORDS
from permission_modes import API_RESPONSE_MAX_CHARS
from reasoning_options import REASONING_EFFORT_ORDER, error_detail_head, reasoning_effort_error, reasoning_effort_label, reasoning_effort_rank, tools_unsupported_error
from subagent_config import SUBAGENT_MAX_RESULT_LENGTH, SUBAGENT_MAX_ROUNDS, SUBAGENT_PROMPTS, SUBAGENT_TIMEOUT_SECONDS, SUBAGENT_TIMEOUT_SECONDS_BY_TYPE, SUBAGENT_TOTAL_BUDGET_BY_TYPE, SUBAGENT_TOTAL_BUDGET_SECONDS, SUBAGENT_WRAPUP_MARGIN_SECONDS, SubagentRoundsExhausted


# ======================== API 调用线程 ========================
class APICallCancelled(RuntimeError):
    """API 请求被用户的 Q 取消。"""


class ReasoningEffortUnsupported(RuntimeError):
    """模型或网关拒绝当前 reasoning_effort 档位，可以下调档位后重试。"""



# ---- method groups moved into their own modules ----
from subagent_runner import (  # noqa: F401
    _handle_delegate_tool_call,
    _prepare_code_workspace,
    _build_code_workspace_hint,
    _build_subagent_resume_messages,
    _run_subagent,
    _collect_subagent_partial_results,
)

class APICallThread(QThread):
    response_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    stream_chunk = pyqtSignal(str)
    tool_confirmation_requested = pyqtSignal(str, str, object)
    status_update = pyqtSignal(str)
    computer_state = pyqtSignal(str)
    task_state_updated = pyqtSignal(object)
    stopped = pyqtSignal()

    def __init__(self, messages, config, stream=True, task_state=None):
        super().__init__()
        self.messages = messages
        self.config = config
        self.stream = stream
        self.is_running = True
        self._confirmation_lock = threading.Lock()
        self._confirmation_events = {}
        self._confirmation_answers = {}
        self._latest_datetime_data = None
        self._forced_datetime_route = False
        self._search_sources = []
        self._tool_execution_index = 0
        self._stream_emitted = False
        self._reasoning_effort_known = {}
        self._computer_session_approved = False
        self._computer_session_requested = False
        self._computer_session_started = False
        self._computer_guard_failed = False
        self._computer_step_count = 0
        self._computer_observation_streak = 0
        self.task_state = copy.deepcopy(task_state) if task_state else create_task_state(self._get_latest_user_text(messages))

    def run(self):
        try:
            self._mark_task_state("planning", "分析需求")
            self._set_plan_step("analyze", "分析需求", "in_progress", "正在分析用户需求并准备执行。")
            content = self._run_tool_call_loop()
            if not self.is_running:
                self.stopped.emit()
                return
            if not content.strip():
                content = "我刚才没有组织出合适的回复，你可以再问我一次。"
            if self.config.get_roleplay_mode():
                content = sanitize_reply_text(content)
            content = self._append_search_sources(content)
            self._set_plan_step("finalize", "整理最终答复", "completed", "已生成最终答复。")
            self.task_state["final_response"] = content
            self._mark_task_state("completed", "已完成")
            if self.stream and not self._stream_emitted:
                self._emit_text_stream(content)
            self.response_received.emit(content)
        except RuntimeError as e:
            if isinstance(e, APICallCancelled):
                self.stopped.emit()
                return
            self.task_state["last_error"] = str(e)
            self._set_plan_step("finalize", "整理最终答复", "failed", str(e))
            self._mark_task_state("failed", "执行失败")
            self.error_occurred.emit(str(e))
        except Exception as e:
            self.task_state["last_error"] = str(e)
            self._set_plan_step("finalize", "整理最终答复", "failed", str(e))
            self._mark_task_state("failed", "执行失败")
            self.error_occurred.emit(f"错误: {str(e)}")
        finally:
            if self._computer_guard_failed:
                self.computer_state.emit("guard_failed")
            elif self._computer_session_requested:
                self.computer_state.emit("finished")
            VisionInputController.end_session()
            # 本线程的 HTTP 连接池随回合结束释放，避免长会话里 socket 只增不减。
            http_client.close_current_thread_session()

    def stop(self):
        self.is_running = False
        if self._computer_session_requested:
            self.computer_state.emit("stopping")
            if not VisionInputController.is_cancelled():
                VisionInputController.cancel_session("manual")
        else:
            VisionInputController.cancel_session()

    def _force_cancel_from_input(self):
        self.is_running = False
        if self._computer_session_requested:
            self.computer_state.emit("stopping")

    def set_tool_confirmation(self, call_id, approved):
        with self._confirmation_lock:
            self._confirmation_answers[call_id] = approved
            event = self._confirmation_events.get(call_id)
        if event:
            event.set()

    def _run_tool_call_loop(self):
        api_key = self.config.get_api_key()
        if not api_key:
            raise RuntimeError("请先设置API Key（在菜单 设置->API配置 中）")

        messages = list(self.messages)
        tools = ActionHandler.get_tool_definitions()
        messages = self._inject_forced_datetime_context(messages)
        user_text = self._get_latest_user_text(messages)
        computer_keywords = (
            "视觉", "截图", "屏幕", "鼠标", "键盘", "点击", "浏览器", "网页",
            "棋盘", "排雷", "游戏界面", "电脑界面", "桌面", "窗口", "应用",
            "程序", "文件", "脚本", "命令", "打开", "关闭", "运行",
        )
        max_rounds = None if any(keyword in user_text for keyword in computer_keywords) else 8
        return self._run_agent_loop(messages, tools, max_rounds=max_rounds, depth=0)

    def _run_agent_loop(self, messages, tools, max_rounds=8, depth=0, use_stream=None, step_prefix="", timeout=None, wrapup_margin=30, progress_key="", progress_title="", resume_on_exhaust=False):
        """通用工具循环：主agent(depth=0)与子agent(depth>=1)共用。"""
        use_stream = self.stream if use_stream is None else bool(use_stream)
        used_tools = False
        deadline = time.perf_counter() + timeout if timeout else None
        loop_started = time.perf_counter()

        round_index = 0
        while max_rounds is None or round_index < max_rounds:
            if not self.is_running:
                return ""
            round_index += 1
            if deadline and time.perf_counter() > deadline:
                raise RuntimeError(f"子agent执行超时（超过 {timeout} 秒），已中止。")
            if depth >= 1 and progress_key:
                elapsed = time.perf_counter() - loop_started
                round_text = f"第 {round_index}/{max_rounds} 轮" if max_rounds is not None else f"第 {round_index} 轮"
                self._set_plan_step(progress_key, progress_title, "in_progress", f"{round_text} · 已用 {elapsed:.0f}s")
            if depth == 0:
                self.status_update.emit("AI正在思考...")
            response = self._request_chat_completion(messages, tools=tools, temperature=0.25, stream=use_stream, emit_stream=(depth == 0))
            message = response["choices"][0]["message"]
            tool_calls = self._extract_tool_calls_from_message(message)

            if not tool_calls:
                if depth == 0:
                    self._set_plan_step("analyze", "分析需求", "completed", "无需调用工具，直接生成答复。")
                    self._set_plan_step("finalize", "整理最终答复", "in_progress", "正在整理自然语言答复。")
                return self._annotate_truncation(self._ensure_datetime_consistency(messages, message), response)

            if deadline and depth >= 1 and (deadline - time.perf_counter()) < wrapup_margin:
                if resume_on_exhaust:
                    raise SubagentRoundsExhausted(
                        f"子agent时间预算即将耗尽（剩余 {int(deadline - time.perf_counter())}s）但任务尚未收尾，需要续跑。"
                    )
                messages.append({
                    "role": "system",
                    "content": "时间预算快要用完，不要再调用工具，直接基于已有信息总结回答。"
                })
                response = self._request_chat_completion(messages, temperature=0.5, stream=use_stream, emit_stream=False)
                wrap_message = response["choices"][0]["message"]
                return self._annotate_truncation(self._ensure_datetime_consistency(messages, wrap_message), response)

            used_tools = True
            if depth == 0:
                self._set_plan_step("analyze", "分析需求", "completed", f"模型决定调用 {len(tool_calls)} 个工具。")
            message = self._normalize_tool_call_message(message, tool_calls)
            messages.append(self._build_assistant_followup_message(message))

            pending_notes = []
            for tool_call in tool_calls:
                if not self.is_running:
                    return ""
                user_intent_hint = self._get_latest_user_text(messages)
                tool_message = self._execute_tool_call(tool_call, depth=depth, step_prefix=step_prefix, user_intent_hint=user_intent_hint)
                messages.append(tool_message)
                executed_tool_name = str((tool_call.get("function") or {}).get("name", ""))
                if executed_tool_name == "wait_for_user_action":
                    pending_notes.append({
                        "role": "system",
                        "content": "用户处理窗口的等待已结束。现在重新调用 inspect_screen 或 inspect_screen_region，确认弹窗是否消失后再继续。",
                    })
                if executed_tool_name in {"inspect_screen", "inspect_foreground_window", "inspect_screen_region"}:
                    try:
                        vision_result = json.loads(tool_message.get("content", "{}"))
                    except (TypeError, json.JSONDecodeError):
                        vision_result = {}
                    if not vision_result.get("success", True):
                        pending_notes.append({
                            "role": "system",
                            "content": (
                                "这次视觉读取失败或超时，不能因此结束任务，也不要声称已经完成。"
                                "不要在没有新动作的情况下连续重复视觉读取；请先基于已有结果执行可逆动作、"
                                "使用 inspect_accessibility 或明确说明当前阻塞原因。"
                            ),
                        })
                    blocking_dialog = (vision_result.get("data") or {}).get("vision") or {}
                    blocking_dialog = blocking_dialog.get("blocking_dialog") or {}
                    if blocking_dialog.get("present") is True:
                        self.computer_state.emit("waiting_external")
                        pending_notes.append({
                            "role": "system",
                            "content": (
                                "视觉模型明确识别到外部阻塞确认弹窗。不要要求用户在聊天中回复，"
                                "也不要盲点弹窗坐标；请调用 wait_for_user_action 等待用户在目标窗口完成确认，"
                                "等待结束后重新 inspect_screen 验证弹窗是否消失。"
                            ),
                        })

            # 这些提示必须等本轮全部 tool 结果追加完再插：tool 消息与 assistant.tool_calls
            # 之间一旦夹入其它角色，网关就会整轮拒收这次请求。
            messages.extend(pending_notes)


        if used_tools:
            if resume_on_exhaust and depth >= 1:
                raise SubagentRoundsExhausted(
                    f"子agent已用完 {max_rounds} 轮工具调用预算，任务尚未收尾，需要续跑。"
                )
            messages.append({
                "role": "system",
                "content": "你已经拿到了足够的工具结果。不要继续调用工具，直接基于现有结果回答用户。"
            })
            if depth == 0:
                self._set_plan_step("finalize", "整理最终答复", "in_progress", "工具执行完成，正在归纳结果。")
            response = self._request_chat_completion(messages, temperature=0.5, stream=use_stream, emit_stream=(depth == 0))
            message = response["choices"][0]["message"]
            return self._annotate_truncation(self._ensure_datetime_consistency(messages, message), response)

        return ""

    def _annotate_truncation(self, content, response):
        """服务端因长度上限截断输出时，在回复末尾附一句提示，避免误以为已经写完。"""
        try:
            choice = (response.get("choices") or [{}])[0] or {}
        except AttributeError:
            return content
        finish_reason = str(choice.get("finish_reason") or "").strip().lower()
        if finish_reason not in ("length", "max_tokens"):
            return content
        note = "（输出已达长度上限被截断，回复「继续」我就接着写）"
        return content + "\n\n" + note if content else note

    def _post_json_cancellable(self, url, headers, payload):
        """同步 POST，但取消能真正中断在飞的请求。

        原来的做法是把 requests.post(timeout=300) 丢进一个额外的 daemon 线程，
        自己在上面的队列上轮询：取消只是「不再等结果」，那个线程仍会占着连接最长
        300 秒，反复取消就会不断堆积残留线程。

        改成在本线程里用 stream=True 分块读取，每个分块检查一次取消标志：
          * 不再有额外线程；
          * 取消后最多等一个分块的时间（毫秒级），并立刻 close() 释放连接。
        对外语义不变：返回 (status_code, text)，失败抛 RuntimeError，取消抛 APICallCancelled。
        """
        response = None
        try:
            response = http_client.session().post(
                url, headers=headers, json=payload, timeout=(10, 300), stream=True
            )
            chunks = []
            for chunk in response.iter_content(chunk_size=65536):
                if not self.is_running:
                    raise APICallCancelled()
                if chunk:
                    chunks.append(chunk)
            body = b"".join(chunks).decode("utf-8", errors="replace")
        except APICallCancelled:
            raise
        except Exception as exc:
            raise RuntimeError(f"API请求失败: {exc}") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    logging.debug("关闭响应失败", exc_info=True)

        if len(body) > API_RESPONSE_MAX_CHARS:
            raise RuntimeError(
                "API请求失败: "
                f"响应体异常大（{len(body)} 字符，超过上限 {API_RESPONSE_MAX_CHARS}），"
                "疑似网关返回异常，已中止解析，请重试。"
            )
        return response.status_code, body

    def _request_chat_completion(self, messages, tools=None, tool_choice=None, temperature=0.7, stream=None, emit_stream=True):
        api_key = self.config.get_api_key()
        base_url = self.config.get_base_url()
        model = self.config.get_model()
        use_stream = self.stream if stream is None else bool(stream)

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": messages,
            "stream": use_stream,
            "temperature": temperature
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        url = f"{base_url.rstrip('/')}/chat/completions"
        requested_effort = self.config.get_reasoning_effort()
        attempts = self._reasoning_effort_attempts(model, requested_effort)
        while True:
            effort = attempts.pop(0)
            if effort:
                payload["reasoning_effort"] = effort
            else:
                payload.pop("reasoning_effort", None)
            try:
                if use_stream:
                    result = self._request_chat_completion_stream(url, headers, payload, emit_stream)
                else:
                    result = self._request_chat_completion_sync(url, headers, payload)
            except ReasoningEffortUnsupported:
                if not attempts:
                    raise
                self.status_update.emit(
                    f"{reasoning_effort_label(requested_effort)} 不被当前模型或网关支持，"
                    f"已自动降到 {reasoning_effort_label(attempts[0])}（本次运行内生效）"
                )
                continue
            self._remember_reasoning_effort(model, effort)
            return result

    def _reasoning_effort_attempts(self, model, requested_effort):
        """按「选中档位 → 逐级下调 → 关闭」生成重试顺序，并复用本次运行已知可用的档位上限。"""
        requested_effort = (requested_effort or "").strip().lower()
        if not requested_effort:
            return [""]
        known = self._reasoning_effort_known.get(model)
        # 取两者中更保守的那个：已知上限更低时就从上限制起试，用户选得更低就直接按用户选的来。
        if known is None or reasoning_effort_rank(requested_effort) >= reasoning_effort_rank(known):
            start = requested_effort
        else:
            start = known
        if not start:
            return [""]
        return list(REASONING_EFFORT_ORDER[reasoning_effort_rank(start):]) + [""]

    def _remember_reasoning_effort(self, model, effort):
        """记住本次运行中该模型实际可用的最高档位，避免后续请求反复撞墙。"""
        effort = (effort or "").strip().lower()
        known = self._reasoning_effort_known.get(model)
        if known is None or reasoning_effort_rank(effort) < reasoning_effort_rank(known):
            self._reasoning_effort_known[model] = effort

    def _request_chat_completion_sync(self, url, headers, payload):
        status_code, raw_text = self._post_json_cancellable(url, headers, payload)
        if status_code != 200:
            detail = raw_text
            if tools_unsupported_error(detail):
                raise RuntimeError(
                    "当前模型或中转网关不支持 tools/function calling，请更换支持工具调用的模型或基座。"
                    f"（网关返回：{error_detail_head(detail)}）"
                )
            if reasoning_effort_error(detail, status_code, payload.get("reasoning_effort")):
                raise ReasoningEffortUnsupported(
                    "当前模型或中转网关不支持该推理强度，已自动逐级降级仍失败，请在输入区把推理调低或关闭后重试。"
                    f"（网关返回：{error_detail_head(detail)}）"
                )
            raise RuntimeError(f"API请求失败: {status_code} - {detail}")
        return self._parse_sync_response(raw_text)

    def _parse_sync_response(self, raw_text):
        """解析非流式响应：容忍 BOM；网关无视 stream=false 回 SSE 时按流式拼回来。"""
        text = raw_text or ""
        if text.startswith("\ufeff"):
            text = text[1:]
        stripped = text.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            completion = self._completion_from_sse_text(stripped)
            if completion is not None:
                logging.warning("网关忽略了 stream=false 直接返回 SSE，已按流式解析（%d 字符）", len(text))
                return completion
            raise RuntimeError(
                f"API 返回了无效 JSON：{exc}（已接收 {len(raw_text)} 字符）"
                f"；返回体开头：{error_detail_head(stripped)}"
            )

    def _completion_from_sse_text(self, text):
        """把整段 SSE 文本拼成 completion；开头就不是 SSE 帧时返回 None。"""
        state = {"content": "", "tool_calls": {}}
        finish_reason = None
        seen_frame = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not line.startswith("data:"):
                if not seen_frame:
                    return None
                continue
            seen_frame = True
            data_str = line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            finish_reason = self._merge_stream_chunk(state, data, False) or finish_reason
        if not seen_frame:
            return None
        return self._stream_state_to_completion(state, finish_reason)

    def _merge_stream_chunk(self, state, data, emit_stream):
        """把一帧增量合并进累积状态，返回该帧的 finish_reason（没有则 None）。"""
        choices = data.get("choices") or []
        if not choices:
            return None
        choice = choices[0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            state["content"] += content
            if emit_stream:
                self._stream_emitted = True
                self.stream_chunk.emit(content)
        for tool_call_delta in delta.get("tool_calls") or []:
            index = tool_call_delta.get("index", 0)
            slot = state["tool_calls"].setdefault(index, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""}
            })
            if tool_call_delta.get("id"):
                slot["id"] = tool_call_delta["id"]
            if tool_call_delta.get("type"):
                slot["type"] = tool_call_delta["type"]
            fn_delta = tool_call_delta.get("function") or {}
            if fn_delta.get("name"):
                slot["function"]["name"] = fn_delta["name"]
            if fn_delta.get("arguments"):
                slot["function"]["arguments"] += fn_delta["arguments"]
        return choice.get("finish_reason")

    @staticmethod
    def _stream_state_to_completion(state, finish_reason):
        message = {"role": "assistant", "content": state["content"]}
        if state["tool_calls"]:
            message["tool_calls"] = [state["tool_calls"][index] for index in sorted(state["tool_calls"])]
        return {"choices": [{"message": message, "finish_reason": finish_reason}]}

    def _request_chat_completion_stream(self, url, headers, payload, emit_stream=True):
        """SSE 流式请求：边收边发 stream_chunk，同时累积 content 与增量 tool_calls。"""
        state = {"content": "", "tool_calls": {}}
        finish_reason = None
        try:
            response = http_client.session().post(url, headers=headers, json=payload, timeout=(10, 300), stream=True)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"API请求失败: {str(e)}")

        if response.status_code != 200:
            detail = response.text
            response.close()
            if tools_unsupported_error(detail):
                raise RuntimeError(
                    "当前模型或中转网关不支持 tools/function calling，请更换支持工具调用的模型或基座。"
                    f"（网关返回：{error_detail_head(detail)}）"
                )
            if reasoning_effort_error(detail, response.status_code, payload.get("reasoning_effort")):
                raise ReasoningEffortUnsupported(
                    "当前模型或中转网关不支持该推理强度，已自动逐级降级仍失败，请在输入区把推理调低或关闭后重试。"
                    f"（网关返回：{error_detail_head(detail)}）"
                )
            raise RuntimeError(f"API请求失败: {response.status_code} - {detail}")

        state = {"content": "", "tool_calls": {}}
        finish_reason = None
        try:
            for raw_bytes in response.iter_lines():
                if not self.is_running:
                    break
                if not raw_bytes:
                    continue
                line = raw_bytes.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                finish_reason = self._merge_stream_chunk(state, data, emit_stream) or finish_reason
        finally:
            response.close()

        return self._stream_state_to_completion(state, finish_reason)

    def _tool_failure_response(self, tool_call_id, tool_name, arguments, message, step_prefix=""):
        result = {"success": False, "message": message}
        self._tool_execution_index += 1
        step_key = (f"{step_prefix}_tool_{self._tool_execution_index}_{tool_name}"
                    if step_prefix else f"tool_{self._tool_execution_index}_{tool_name}")
        self._set_plan_step(step_key, f"执行工具：{tool_name}", "failed", message)
        append_task_tool_result(self.task_state, tool_name, arguments, result, 0)
        self._emit_task_state()
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result, ensure_ascii=False),
        }

    def _execute_tool_call(self, tool_call, depth=0, step_prefix="", user_intent_hint=None):
        function_call = tool_call.get("function", {})
        tool_name = function_call.get("name", "")
        arguments = self._parse_tool_arguments(function_call.get("arguments", "{}"))
        if tool_name == "search_web":
            arguments = dict(arguments)
            if user_intent_hint and "_user_intent" not in arguments:
                arguments["_user_intent"] = user_intent_hint
        tool_call_id = tool_call.get("id", "")

        if tool_name == "delegate_to_agent":
            return self._handle_delegate_tool_call(tool_call_id, arguments, depth)

        # 只读模式：这类工具会被沙箱拒绝，先拦下来，避免白弹一次确认框
        if (
            self.config is not None
            and self.config.is_read_only_mode()
            and tool_name in ActionHandler.READ_ONLY_BLOCKED_TOOLS
        ):
            return self._tool_failure_response(
                tool_call_id,
                tool_name,
                arguments,
                "当前处于「只读」权限模式，该操作被沙箱拒绝。请改用只读工具，"
                "或在聊天框旁的权限按钮，或「设置 → 模型与接口 → 工具与权限」里切换到「工作区可写」或「完全权限」。",
                step_prefix,
            )

        needs_user_approval = tool_name in {"shutdown", "reboot", "run_command", "write_file"}
        if not needs_user_approval and MCPManager.is_mcp_tool(tool_name):
            # 外部 MCP 工具默认每次都要用户点头；server 配置里写了 autoApprove 才免
            needs_user_approval = MCPManager.needs_confirmation(tool_name)
        if needs_user_approval:
            approved = self._request_confirmation(tool_call_id, tool_name, arguments)
            if not approved:
                logging.warning("用户取消了高风险操作 %s", tool_name)
                return self._tool_failure_response(
                    tool_call_id, tool_name, arguments, "用户取消了该高风险操作。", step_prefix
                )

        computer_tools = {
            "inspect_screen", "inspect_foreground_window", "inspect_screen_region", "inspect_accessibility", "mouse_move", "mouse_click",
            "verified_mouse_click",
            "keyboard_type", "keyboard_hotkey", "computer_wait", "wait_for_user_action",
        }
        if tool_name in computer_tools:
            observation_tools = {
                "inspect_screen", "inspect_foreground_window", "inspect_screen_region",
            }
            state_change_tools = {
                "mouse_click", "verified_mouse_click", "keyboard_type", "keyboard_hotkey",
                "wait_for_user_action",
            }
            if tool_name in observation_tools:
                if self._computer_observation_streak >= 1:
                    return self._tool_failure_response(
                        tool_call_id,
                        tool_name,
                        arguments,
                        "最近没有发生新的键鼠动作或用户处理，暂时禁止重复读取屏幕；请基于已有观察执行动作，或明确说明当前阻塞原因。",
                        step_prefix,
                    )
                self._computer_observation_streak += 1
            elif tool_name in state_change_tools:
                self._computer_observation_streak = 0
            if not self._computer_session_approved:
                self._computer_session_requested = True
                self.computer_state.emit("waiting_authorization")
                approved = self._request_confirmation(
                    tool_call_id,
                    "computer_session",
                    {"requested_tool": tool_name},
                )
                if not approved:
                    return self._tool_failure_response(
                        tool_call_id, tool_name, arguments, "用户拒绝了视觉键鼠连续操作。", step_prefix
                    )
                self._computer_session_approved = True
                self._computer_session_started = True
                if not VisionInputController.begin_session(cancel_callback=self._force_cancel_from_input):
                    self._computer_guard_failed = True
                    self.computer_state.emit("guard_failed")
                    self.stop()
                    return self._tool_failure_response(
                        tool_call_id,
                        tool_name,
                        arguments,
                        "Windows 键鼠接管钩子启动失败，已拒绝继续操作。",
                        step_prefix,
                    )
                self.computer_state.emit("q_listener_active")
                self.computer_state.emit("input_guard_active")
                self.computer_state.emit("observing")
            self._computer_step_count += 1
            if VisionInputController.is_cancelled():
                self.computer_state.emit("stopping")
                self.stop()
                return self._tool_failure_response(
                    tool_call_id, tool_name, arguments, "视觉键鼠任务已被长按 Q 取消。", step_prefix
                )

        self.status_update.emit(f"正在执行工具：{tool_name}")
        computer_state_map = {
            "inspect_screen": "waiting_vision",
            "inspect_foreground_window": "waiting_vision",
            "inspect_screen_region": "waiting_vision",
            "inspect_accessibility": "reading_accessibility",
            "mouse_move": "moving_mouse",
            "mouse_click": "clicking",
            "verified_mouse_click": "verifying_click",
            "keyboard_type": "typing",
            "keyboard_hotkey": "pressing_hotkey",
            "computer_wait": "waiting_interface",
            "wait_for_user_action": "waiting_external",
        }
        if tool_name in computer_state_map:
            self.computer_state.emit(computer_state_map[tool_name])
        self._tool_execution_index += 1
        step_key = (f"{step_prefix}_tool_{self._tool_execution_index}_{tool_name}"
                    if step_prefix else f"tool_{self._tool_execution_index}_{tool_name}")
        self._mark_task_state("executing", f"执行工具：{tool_name}")
        self._set_plan_step(step_key, f"执行工具：{tool_name}", "in_progress", f"参数：{json.dumps(arguments, ensure_ascii=False)}")
        started = time.perf_counter()
        if MCPManager.is_mcp_tool(tool_name):
            result = MCPManager.call_tool(tool_name, arguments)
        else:
            ActionHandler.set_timeout_confirm_callback(self._request_continue_waiting)
            try:
                result = ActionHandler.execute_tool(tool_name, arguments)
            finally:
                ActionHandler.set_timeout_confirm_callback(None)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if tool_name in {
            "inspect_screen", "inspect_foreground_window", "inspect_screen_region", "inspect_accessibility", "mouse_move", "mouse_click",
            "verified_mouse_click",
            "keyboard_type", "keyboard_hotkey", "computer_wait",
            "wait_for_user_action",
        } and VisionInputController.is_cancelled():
            self.computer_state.emit("stopping")
            self.stop()
        if result.get("success"):
            logging.info("工具调用 %s 参数=%s 耗时=%dms 成功", tool_name, _log_safe_args(arguments), duration_ms)
        else:
            logging.warning("工具调用 %s 参数=%s 耗时=%dms 失败：%s", tool_name, _log_safe_args(arguments), duration_ms, str(result.get("message", ""))[:200])
        append_task_tool_result(self.task_state, tool_name, arguments, result, duration_ms)
        step_status = "completed" if result.get("success") else "failed"
        step_detail = str(result.get("message", "")) or "工具执行完成。"
        self._set_plan_step(step_key, f"执行工具：{tool_name}", step_status, step_detail)
        if tool_name == "get_current_datetime" and result.get("success"):
            self._latest_datetime_data = result.get("data")
        if tool_name == "search_web" and result.get("success"):
            self._collect_search_sources(result)
        self._emit_task_state()
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result, ensure_ascii=False)
        }


    @staticmethod
    def _parse_tool_arguments(raw_arguments):
        if not raw_arguments:
            return {}
        if isinstance(raw_arguments, dict):
            return raw_arguments
        try:
            return json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _build_assistant_followup_message(message):
        if not isinstance(message, dict):
            return {"role": "assistant", "content": str(message)}

        preserved = {"role": message.get("role", "assistant")}
        if "content" in message:
            preserved["content"] = message.get("content")
        elif "tool_calls" not in message:
            preserved["content"] = ""

        for key, value in message.items():
            if key == "role":
                continue
            if key in preserved:
                continue
            if key in {"tool_calls", "function_call", "name", "refusal"}:
                preserved[key] = value
                continue
            if key.startswith("reasoning") or key.startswith("thinking"):
                preserved[key] = value

        return preserved

    def _emit_task_state(self):
        touch_task_state(self.task_state)
        self.task_state_updated.emit(copy.deepcopy(self.task_state))

    def _mark_task_state(self, status: str, current_step: str):
        touch_task_state(self.task_state, status=status, current_step=current_step)
        self._emit_task_state()

    def _set_plan_step(self, step_key: str, title: str, status: str, detail: str = ""):
        upsert_task_plan_step(self.task_state, step_key, title, status, detail)
        self._emit_task_state()

    @staticmethod
    def _strip_dsml_protocol_text(text):
        if not text:
            return ""
        cleaned = DSML_INVOKE_PATTERN.sub("", text)
        cleaned = DSML_TAG_PATTERN.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _extract_tool_calls_from_message(self, message):
        if not isinstance(message, dict):
            return []

        native_tool_calls = message.get("tool_calls") or []
        if native_tool_calls:
            return native_tool_calls

        content = str(message.get("content") or "")
        if not content:
            return []

        text_calls, _ = parse_text_tool_calls(content)
        if text_calls:
            return text_calls

        if "DSML" not in content:
            return []

        parsed_calls = []
        for invoke_match in DSML_INVOKE_PATTERN.finditer(content):
            tool_name = invoke_match.group(1).strip()
            if not tool_name:
                continue
            arguments = {}
            invoke_body = invoke_match.group(2)
            for param_match in DSML_PARAMETER_PATTERN.finditer(invoke_body):
                param_name = param_match.group(1).strip()
                param_value = self._strip_dsml_protocol_text(param_match.group(2))
                if param_name:
                    arguments[param_name] = param_value
            parsed_calls.append({
                "id": f"dsml_{tool_name}_{uuid.uuid4().hex[:10]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False)
                }
            })
        return parsed_calls

    def _normalize_tool_call_message(self, message, tool_calls):
        if not isinstance(message, dict):
            return {"role": "assistant", "content": "", "tool_calls": tool_calls}

        normalized = dict(message)
        normalized["role"] = normalized.get("role", "assistant")
        normalized["tool_calls"] = tool_calls
        original_content = str(normalized.get("content") or "")
        has_text_protocol = bool(parse_text_tool_calls(original_content)[0])
        normalized["content"] = "" if "DSML" in original_content or has_text_protocol else self._strip_dsml_protocol_text(original_content)
        return normalized

    def _request_continue_waiting(self, command, elapsed, timeout):
        """命令超时后询问用户是否继续等待；返回 True 表示再等一个超时周期。"""
        call_id = f"__timeout_wait__{id(self)}_{elapsed}"
        return self._request_confirmation(call_id, "__timeout_wait__", {
            "command": str(command),
            "elapsed": elapsed,
            "timeout": timeout,
        })

    def _request_confirmation(self, call_id, tool_name, arguments):
        # 完全权限模式（danger-full-access）：审批策略为 never，任何工具都直接放行，不弹确认框
        if self.config is not None and self.config.is_full_access_mode():
            logging.info("完全权限模式：自动授权工具 %s", tool_name)
            return True
        event = threading.Event()
        with self._confirmation_lock:
            self._confirmation_events[call_id] = event
        self.tool_confirmation_requested.emit(call_id, tool_name, arguments)

        while self.is_running and not event.wait(0.1):
            pass

        with self._confirmation_lock:
            approved = self._confirmation_answers.pop(call_id, False)
            self._confirmation_events.pop(call_id, None)
        return approved

    def _emit_text_stream(self, content):
        if not content:
            return
        chunk_size = 14
        for start in range(0, len(content), chunk_size):
            if not self.is_running:
                break
            self.stream_chunk.emit(content[start:start + chunk_size])
            self.msleep(20)

    def _collect_search_sources(self, result):
        data = result.get("data") or {}
        for item in data.get("results", [])[:5]:
            title = sanitize_reply_text(str(item.get("title", "无标题"))).strip() or "无标题"
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            if any(existing["url"] == url for existing in self._search_sources):
                continue
            self._search_sources.append({"title": title, "url": url})

    def _append_search_sources(self, content):
        if not self._search_sources:
            return content
        return attach_sources_to_text(content, self._search_sources)

    def _inject_forced_datetime_context(self, messages):
        latest_user_text = self._get_latest_user_text(messages)
        if not self._should_force_current_datetime(latest_user_text):
            return messages

        tool_result = ActionHandler.execute_tool("get_current_datetime", {})
        tool_call_id = f"forced_datetime_{uuid.uuid4().hex[:10]}"
        self._forced_datetime_route = True
        if tool_result.get("success"):
            self._latest_datetime_data = tool_result.get("data")

        forced_messages = list(messages)
        forced_messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "get_current_datetime",
                    "arguments": "{}"
                }
            }]
        })
        forced_messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(tool_result, ensure_ascii=False)
        })
        forced_messages.append({
            "role": "system",
            "content": "用户正在询问当前日期或时间。你必须严格依据刚才的 get_current_datetime 工具结果回答，禁止改写成其他日期、星期或时间，也不要使用 Markdown 符号。"
        })
        return forced_messages

    @staticmethod
    def _get_latest_user_text(messages):
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content", "")).strip()
        return ""

    @staticmethod
    def _should_force_current_datetime(user_text):
        if not user_text:
            return False
        normalized = user_text.strip().replace(" ", "")
        if any(keyword in normalized for keyword in DATETIME_FORCE_KEYWORDS):
            return True
        if re.search(r"(今天|现在|当前).{0,6}(日期|时间|星期|周几|礼拜几|几点)", normalized):
            return True
        if re.search(r"(几号|几月几日|几月几号|星期几|周几|礼拜几|几点|当前时间|当前日期)$", normalized):
            return True
        return False

    def _ensure_datetime_consistency(self, messages, message):
        content = ""
        if isinstance(message, dict):
            content = (message.get("content") or "").strip()
        else:
            content = str(message).strip()

        if not self._latest_datetime_data:
            return content
        if not self._has_datetime_mismatch(content, self._latest_datetime_data):
            return content

        correction_message = (
            "你刚才把当前日期或时间说错了。"
            f"正确值是：{self._latest_datetime_data['year']}年{self._latest_datetime_data['month']}月{self._latest_datetime_data['day']}日，"
            f"{self._latest_datetime_data['weekday']}，{self._latest_datetime_data['time']}。"
            "请只基于这个正确值，重新自然回答用户，不要使用 Markdown 符号，也不要再调用工具。"
        )
        self.task_state["retry_count"] = int(self.task_state.get("retry_count", 0)) + 1
        self._set_plan_step("datetime_retry", "修正日期时间答复", "in_progress", "检测到日期或时间不一致，正在自动重试。")
        retry_messages = list(messages)
        retry_messages.append(self._build_assistant_followup_message(message))
        retry_messages.append({"role": "system", "content": correction_message})
        response = self._request_chat_completion(retry_messages, temperature=0.1, stream=self.stream, emit_stream=False)
        retry_message = response["choices"][0]["message"]
        self._set_plan_step("datetime_retry", "修正日期时间答复", "completed", "已依据工具结果纠正日期或时间。")
        content = (retry_message.get("content") or "").strip()
        return sanitize_reply_text(content) if self.config.get_roleplay_mode() else strip_internal_reasoning(content)

    @staticmethod
    def _has_datetime_mismatch(content, datetime_data):
        if not content:
            return False

        expected_year = int(datetime_data["year"])
        expected_month = int(datetime_data["month"])
        expected_day = int(datetime_data["day"])
        expected_weekday = datetime_data["weekday"]
        expected_weekday_short = expected_weekday.replace("星期", "周", 1)

        full_date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", content)
        if full_date_match:
            year, month, day = map(int, full_date_match.groups())
            if (year, month, day) != (expected_year, expected_month, expected_day):
                return True

        month_day_match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", content)
        if month_day_match:
            month, day = map(int, month_day_match.groups())
            if (month, day) != (expected_month, expected_day):
                return True

        weekday_match = re.search(r"(星期[一二三四五六日天]|周[一二三四五六日天])", content)
        if weekday_match:
            weekday = weekday_match.group(1)
            if weekday not in {expected_weekday, expected_weekday_short}:
                return True

        time_match = re.search(r"(?<!\d)(\d{1,2})[:：](\d{2})(?::(\d{2}))?", content)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            expected_hour, expected_minute, _ = map(int, str(datetime_data["time"]).split(":"))
            if (hour, minute) != (expected_hour, expected_minute):
                return True

        return False


# Bind the moved methods back (identical descriptor semantics).
APICallThread._handle_delegate_tool_call = _handle_delegate_tool_call
APICallThread._prepare_code_workspace = _prepare_code_workspace
APICallThread._build_code_workspace_hint = _build_code_workspace_hint
APICallThread._build_subagent_resume_messages = _build_subagent_resume_messages
APICallThread._run_subagent = _run_subagent
APICallThread._collect_subagent_partial_results = _collect_subagent_partial_results
