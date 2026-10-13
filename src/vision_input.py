"""Screen vision and virtual mouse/keyboard helpers for Windows experiments."""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import sys
import threading
import time

import requests

from accessibility_input import AccessibilityController
from input_guard import InputGuard
import http_client
import thread_registry


# 推理型视觉模型会先输出思考，思考 token 同样占用 max_tokens；
# 上限过小会让 content 变成空字符串（finish_reason=length）。实测思考最多吃掉 1700+ token。
VISION_MAX_TOKENS = 10000



# ---- method groups moved into their own modules ----
from vision_actions import (  # noqa: F401
    _require_session,
    _result,
    _pyautogui,
    _validate_point,
    mouse_move,
    mouse_click,
    verified_mouse_click,
    keyboard_type,
    keyboard_hotkey,
    wait,
    wait_for_user_action,
    suspend_input_guard_for_user_confirmation,
    resume_input_guard,
)
from vision_capture import (  # noqa: F401
    _capture_screen,
    _screen_bounds,
    _request_vision_json,
)

class VisionInputController:
    _base_url = ""
    _api_key = ""
    _vision_model = ""
    _session_lock = threading.RLock()
    _session_active = False
    _cancel_event = threading.Event()
    _cancel_reason = ""
    _q_hold_progress = 0.0
    _escape_thread = None
    _screen_bounds_cache = None
    _cancel_callback = None
    _cancel_callback_invoked = False
    _input_guard_suspended = False

    @classmethod
    def configure(cls, base_url=None, api_key=None, vision_model=None):
        if base_url is not None:
            cls._base_url = str(base_url or "").strip().rstrip("/")
        if api_key is not None:
            cls._api_key = str(api_key or "").strip()
        if vision_model is not None:
            cls._vision_model = str(vision_model or "").strip()

    @classmethod
    def begin_session(cls, cancel_callback=None):
        with cls._session_lock:
            cls._cancel_event.clear()
            cls._cancel_reason = ""
            cls._q_hold_progress = 0.0
            cls._cancel_callback = cancel_callback
            cls._cancel_callback_invoked = False
            cls._input_guard_suspended = False
            cls._session_active = True
            cls._screen_bounds_cache = None
            guard_started = InputGuard.start()
            if sys.platform == "win32" and (
                cls._escape_thread is None or not cls._escape_thread.is_alive()
            ):
                # 长驻取消监听线程：登记保活，关窗时与其它后台线程一起收尾
                cls._escape_thread = thread_registry.spawn(
                    cls._watch_q,
                    name="AissistantQWatcher",
                    group="vision_escape",
                )
                logging.info("Q 取消监听线程已启动，需持续按住 Q 3 秒")
            elif sys.platform == "win32":
                logging.info("Q 取消监听线程已在运行")
            return guard_started

    @classmethod
    def end_session(cls):
        with cls._session_lock:
            cls._session_active = False
            cls._cancel_event.set()
            cls._screen_bounds_cache = None
            cls._q_hold_progress = 0.0
            cls._cancel_callback = None
            cls._cancel_callback_invoked = False
            cls._input_guard_suspended = False
        InputGuard.stop()

    @classmethod
    def cancel_session(cls, reason=""):
        if reason and not cls._cancel_reason:
            cls._cancel_reason = str(reason)
        cls._cancel_event.set()
        InputGuard.stop()
        callback = cls._cancel_callback
        if callback and not cls._cancel_callback_invoked:
            cls._cancel_callback_invoked = True
            try:
                logging.info("Q 取消回调已触发，通知 API 线程停止")
                callback()
            except Exception:
                logging.exception("视觉键鼠取消回调执行失败")

    @classmethod
    def get_cancel_reason(cls):
        return cls._cancel_reason

    @classmethod
    def get_q_hold_progress(cls):
        return cls._q_hold_progress

    @classmethod
    def is_session_active(cls):
        return cls._session_active


    @classmethod
    def is_cancelled(cls):
        return cls._cancel_event.is_set()

    @classmethod
    def _watch_q(cls):
        logging.info("Q 取消监听线程进入轮询")
        pressed_at = None
        notified = False
        while cls._session_active:
            pressed = InputGuard.is_q_down() or cls._get_physical_q_state()
            if pressed and pressed_at is None:
                pressed_at = time.monotonic()
                logging.info("检测到 Q，持续按住 3 秒可取消视觉键鼠任务")
            if pressed and pressed_at is not None:
                cls._q_hold_progress = max(0.0, min(1.0, (time.monotonic() - pressed_at) / 3.0))
            if pressed and pressed_at is not None and not notified:
                if time.monotonic() - pressed_at >= 3.0:
                    cls._q_hold_progress = 1.0
                    cls.cancel_session("q")
                    logging.info("视觉键鼠任务被长按 Q 取消")
                    notified = True
                    return
            if not pressed:
                if pressed_at is not None and not notified:
                    logging.info("Q 未持续按满 3 秒，取消蓄力")
                pressed_at = None
                notified = False
                cls._q_hold_progress = 0.0
            time.sleep(0.05)

    @staticmethod
    def _get_physical_q_state():
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x51) & 0x8000)
        except Exception:
            return False


    @classmethod
    def inspect_screen(cls, instruction, region=None):
        denied = cls._require_session()
        if denied:
            return denied
        if not cls._api_key:
            return cls._result(False, "未配置 API Key，无法调用视觉模型。")
        if not cls._base_url:
            return cls._result(False, "未配置 API Base URL，无法调用视觉模型。")
        if not cls._vision_model:
            return cls._result(False, "未配置视觉模型。请在配置中填写 vision_model。")

        try:
            image_url, screen_info = cls._capture_screen(region=region)
            prompt = (
                "你是桌面视觉定位器。请分析这张当前桌面截图，并严格围绕用户要求定位目标。\n"
                f"用户要求：{str(instruction or '').strip()}\n"
                "截图坐标必须使用原始桌面坐标，而不是缩放后的图片坐标。"
                f"本次截图区域原始尺寸为 {screen_info['width']}x{screen_info['height']}，"
                f"截图区域左上角全局坐标为 ({screen_info['origin_x']}, {screen_info['origin_y']})。\n"
                "只返回简短 JSON，不要 Markdown，不要解释推理过程。格式："
                '{"summary":"当前界面概况", "targets":[{"label":"目标名称",'
                '"x":0,"y":0,"width":0,"height":0,"confidence":0.0}],'
                '"recommended_action":"下一步动作",'
                '"blocking_dialog":{"present":false,"label":"","reason":""}}。'
                "blocking_dialog 只有在清楚看到阻塞目标应用的外部确认弹窗时才设为 present=true；"
                "不要把 Aissistant 自己的 AI 操作授权弹窗当成目标，也不要在看不清时猜测。"
                "如果找不到目标，targets 返回空数组，并说明原因。"
            )
            payload = {
                "model": cls._vision_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }],
                "temperature": 0.1,
                "max_tokens": VISION_MAX_TOKENS,
            }
            data, error = cls._request_vision_json(payload, read_timeout=60 if region else 75)
            if error == "cancelled":
                return cls._result(False, "视觉分析已被长按 Q 取消。")
            if error:
                return cls._result(False, f"视觉模型请求失败：{error}")
            choices = data.get("choices") or [{}]
            choice = choices[0] or {}
            finish_reason = str(choice.get("finish_reason") or "").strip().lower()
            content = (choice.get("message") or {}).get("content", "")
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            content = str(content or "").strip()
            if not content:
                usage = data.get("usage") or {}
                logging.warning(
                    "视觉模型返回空内容：finish_reason=%s usage=%s", finish_reason or "-", usage
                )
                if finish_reason in ("length", "max_tokens"):
                    return cls._result(
                        False,
                        "视觉模型把输出预算全用在思考上了"
                        f"（finish_reason=length，max_tokens={VISION_MAX_TOKENS}），"
                        "没有留下分析结果，请重试。",
                    )
                return cls._result(False, "视觉模型没有返回分析结果。")
            visual_data = None
            try:
                candidate = content.strip()
                if candidate.startswith("```"):
                    candidate = candidate.strip("`").strip()
                    if candidate.lower().startswith("json"):
                        candidate = candidate[4:].strip()
                visual_data = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                visual_data = None
            return cls._result(
                True,
                f"视觉分析完成：\n{content}",
                {"analysis": content, "screen": screen_info, "vision": visual_data},
            )
        except requests.exceptions.RequestException as exc:
            return cls._result(False, f"视觉模型网络请求失败：{exc}")
        except Exception as exc:
            logging.exception("视觉分析失败")
            return cls._result(False, f"视觉分析失败：{exc}")

    @classmethod
    def inspect_screen_region(cls, instruction, x, y, width, height):
        return cls.inspect_screen(
            instruction,
            region={
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
            },
        )

    @classmethod
    def inspect_accessibility(cls, query=""):
        denied = cls._require_session()
        if denied:
            return denied
        try:
            return AccessibilityController.inspect_tree(query)
        except RuntimeError as exc:
            return cls._result(False, str(exc))
        except Exception as exc:
            logging.exception("UI Automation 检查失败")
            return cls._result(False, f"UI Automation 检查失败：{exc}")

    @classmethod
    def inspect_foreground_window(cls, instruction):
        denied = cls._require_session()
        if denied:
            return denied
        try:
            window_result = AccessibilityController.get_foreground_window_info()
            if not window_result.get("success"):
                return window_result
            window = window_result["data"]["foreground_window"]
            region = {
                "x": window["x"],
                "y": window["y"],
                "width": window["width"],
                "height": window["height"],
            }
            result = cls.inspect_screen(instruction, region=region)
            data = result.setdefault("data", {})
            data["foreground_window"] = window
            return result
        except RuntimeError as exc:
            return cls._result(False, str(exc))
        except Exception as exc:
            logging.exception("前台窗口识别失败")
            return cls._result(False, f"前台窗口识别失败：{exc}")


# Bind the moved methods back (identical descriptor semantics).
VisionInputController._require_session = classmethod(_require_session)
VisionInputController._result = classmethod(_result)
VisionInputController._pyautogui = staticmethod(_pyautogui)
VisionInputController._validate_point = classmethod(_validate_point)
VisionInputController.mouse_move = classmethod(mouse_move)
VisionInputController.mouse_click = classmethod(mouse_click)
VisionInputController.verified_mouse_click = classmethod(verified_mouse_click)
VisionInputController.keyboard_type = classmethod(keyboard_type)
VisionInputController.keyboard_hotkey = classmethod(keyboard_hotkey)
VisionInputController.wait = classmethod(wait)
VisionInputController.wait_for_user_action = classmethod(wait_for_user_action)
VisionInputController.suspend_input_guard_for_user_confirmation = classmethod(suspend_input_guard_for_user_confirmation)
VisionInputController.resume_input_guard = classmethod(resume_input_guard)
VisionInputController._capture_screen = classmethod(_capture_screen)
VisionInputController._screen_bounds = classmethod(_screen_bounds)
VisionInputController._request_vision_json = classmethod(_request_vision_json)
