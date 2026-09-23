"""Screen vision and virtual mouse/keyboard helpers for Windows experiments."""

from __future__ import annotations

import base64
import io
import json
import logging
import queue
import re
import sys
import threading
import time

import requests

from accessibility_input import AccessibilityController
from input_guard import InputGuard


# 推理型视觉模型会先输出思考，思考 token 同样占用 max_tokens；
# 上限过小会让 content 变成空字符串（finish_reason=length）。实测思考最多吃掉 1700+ token。
VISION_MAX_TOKENS = 10000


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
                cls._escape_thread = threading.Thread(
                    target=cls._watch_q,
                    name="AissistQWatcher",
                    daemon=True,
                )
                cls._escape_thread.start()
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
    def suspend_input_guard_for_user_confirmation(cls):
        if not cls._session_active:
            return True
        InputGuard.stop()
        cls._input_guard_suspended = True
        logging.info("已临时释放键鼠接管，等待用户确认授权")
        return True

    @classmethod
    def resume_input_guard(cls):
        if not cls._session_active or cls.is_cancelled():
            return False
        started = InputGuard.start()
        cls._input_guard_suspended = not started
        if started:
            logging.info("用户授权完成，已恢复键鼠接管")
        else:
            logging.warning("用户授权完成，但键鼠接管恢复失败")
        return started

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
    def _require_session(cls):
        if not cls._session_active:
            return cls._result(False, "视觉键鼠会话尚未授权。")
        if cls._cancel_event.is_set():
            return cls._result(False, "视觉键鼠任务已被长按 Q 取消。")
        return None

    @classmethod
    def _result(cls, success, message, data=None):
        result = {"success": bool(success), "message": str(message)}
        if data is not None:
            result["data"] = data
        return result

    @staticmethod
    def _pyautogui():
        try:
            import pyautogui
        except ImportError as exc:
            raise RuntimeError("缺少 pyautogui，请先安装视觉键鼠依赖。") from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.12
        return pyautogui

    @classmethod
    def _capture_screen(cls, region=None):
        try:
            import mss
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("缺少 mss 或 Pillow，请先安装视觉截图依赖。") from exc

        with mss.mss() as capture:
            monitor = capture.monitors[0]
            if region:
                monitor_left = int(monitor.get("left", 0))
                monitor_top = int(monitor.get("top", 0))
                monitor_right = monitor_left + int(monitor.get("width", 0))
                monitor_bottom = monitor_top + int(monitor.get("height", 0))
                left = max(monitor_left, int(region.get("x", monitor_left)))
                top = max(monitor_top, int(region.get("y", monitor_top)))
                right = min(monitor_right, left + int(region.get("width", 0)))
                bottom = min(monitor_bottom, top + int(region.get("height", 0)))
                if right <= left or bottom <= top:
                    raise ValueError("截图区域无效或超出当前桌面范围。")
                capture_area = {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                }
            else:
                capture_area = monitor
            raw = capture.grab(capture_area)
            image = Image.frombytes("RGB", raw.size, raw.rgb)
            original_width, original_height = image.size
            max_dimension = 1280 if region else 1600
            scale = min(1.0, max_dimension / max(original_width, original_height))
            if scale < 1.0:
                image = image.resize(
                    (round(original_width * scale), round(original_height * scale)),
                    Image.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=82, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            screen_info = {
                "width": original_width,
                "height": original_height,
                "origin_x": capture_area.get("left", 0),
                "origin_y": capture_area.get("top", 0),
                "image_width": image.width,
                "image_height": image.height,
                "scale": scale,
                "region": bool(region),
            }
            return f"data:image/jpeg;base64,{encoded}", screen_info

    @classmethod
    def _request_vision_json(cls, payload, read_timeout=60):
        """在可被 Q 长按取消的后台请求中调用视觉模型。"""
        result_queue = queue.Queue(maxsize=1)

        def request_worker():
            response = None
            try:
                response = requests.post(
                    f"{cls._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {cls._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=(10, read_timeout),
                )
                result_queue.put((response.status_code, response.text[:12000], None))
            except Exception as exc:
                result_queue.put((None, "", exc))
            finally:
                if response is not None:
                    response.close()

        threading.Thread(target=request_worker, name="AissistVisionRequest", daemon=True).start()
        while True:
            if cls.is_cancelled():
                return None, "cancelled"
            try:
                status_code, raw_text, error = result_queue.get(timeout=0.1)
                if error:
                    return None, error
                if status_code != 200:
                    return None, f"HTTP {status_code} - {raw_text[:1000]}"
                try:
                    return json.loads(raw_text), None
                except json.JSONDecodeError as exc:
                    return None, f"视觉模型返回了无效 JSON：{exc}"
            except queue.Empty:
                continue

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
                "不要把 Aissist 自己的 AI 操作授权弹窗当成目标，也不要在看不清时猜测。"
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

    @classmethod
    def wait_for_user_action(cls, seconds=15):
        denied = cls._require_session()
        if denied:
            return denied
        seconds = max(1.0, min(60.0, float(seconds)))
        deadline = time.monotonic() + seconds
        InputGuard.stop()
        try:
            while time.monotonic() < deadline:
                if cls._cancel_event.wait(0.1):
                    return cls._result(False, "视觉键鼠任务已被长按 Q 取消。")
            return cls._result(True, f"已等待用户处理外部弹窗 {seconds:.1f} 秒。")
        finally:
            if cls._session_active and not cls.is_cancelled():
                cls._screen_bounds_cache = None
                InputGuard.start()

    @classmethod
    def _screen_bounds(cls):
        if cls._screen_bounds_cache:
            return cls._screen_bounds_cache
        try:
            import mss
            with mss.mss() as capture:
                monitor = capture.monitors[0]
                bounds = (
                    int(monitor.get("left", 0)),
                    int(monitor.get("top", 0)),
                    int(monitor.get("width", 0)),
                    int(monitor.get("height", 0)),
                )
                cls._screen_bounds_cache = bounds
                return bounds
        except ImportError as exc:
            raise RuntimeError("缺少 mss，请先安装视觉截图依赖。") from exc

    @classmethod
    def _validate_point(cls, x, y):
        left, top, width, height = cls._screen_bounds()
        x = int(x)
        y = int(y)
        if not (left <= x < left + width and top <= y < top + height):
            raise ValueError(f"坐标 ({x}, {y}) 超出当前桌面范围。")
        return x, y

    @classmethod
    def mouse_move(cls, x, y):
        denied = cls._require_session()
        if denied:
            return denied
        x, y = cls._validate_point(x, y)
        pyautogui = cls._pyautogui()
        pyautogui.moveTo(x, y, duration=0.12)
        return cls._result(True, f"鼠标已移动到 ({x}, {y})。", {"x": x, "y": y})

    @classmethod
    def mouse_click(cls, x, y, button="left", clicks=1):
        denied = cls._require_session()
        if denied:
            return denied
        x, y = cls._validate_point(x, y)
        button = str(button or "left").strip().lower()
        if button not in {"left", "right", "middle"}:
            return cls._result(False, "鼠标按键只支持 left、right 或 middle。")
        clicks = int(clicks)
        if clicks not in {1, 2}:
            return cls._result(False, "点击次数只支持 1 或 2。")
        pyautogui = cls._pyautogui()
        pyautogui.moveTo(x, y, duration=0.12)
        pyautogui.click(x=x, y=y, clicks=clicks, interval=0.12, button=button)
        return cls._result(True, f"已在 ({x}, {y}) 执行 {clicks} 次 {button} 点击。")

    @classmethod
    def verified_mouse_click(cls, x, y, target_label, button="left", clicks=1):
        denied = cls._require_session()
        if denied:
            return denied
        target_label = str(target_label or "").strip()
        if not target_label:
            return cls._result(False, "交叉验证点击需要目标名称。")
        try:
            verification = AccessibilityController.verify_click_target(target_label, x, y)
        except RuntimeError as exc:
            return cls._result(False, str(exc))
        except Exception as exc:
            logging.exception("UI Automation 点击验证失败")
            return cls._result(False, f"UI Automation 点击验证失败：{exc}")
        if not verification.get("success"):
            return verification
        info = verification["data"]["info"]
        click_result = cls.mouse_click(
            info["x"] + info["width"] // 2,
            info["y"] + info["height"] // 2,
            button=button,
            clicks=clicks,
        )
        if click_result.get("success"):
            click_result["message"] = (
                f"已通过视觉坐标与 UIA 控件“{info['name']}”交叉验证，"
                f"点击控件中心 ({info['x'] + info['width'] // 2}, {info['y'] + info['height'] // 2})。"
            )
            click_result["data"] = {"accessibility": info, "verification": verification["data"]}
        return click_result

    @classmethod
    def keyboard_type(cls, text):
        denied = cls._require_session()
        if denied:
            return denied
        text = str(text or "")
        if not text:
            return cls._result(False, "输入内容不能为空。")
        if len(text) > 2000:
            return cls._result(False, "单次输入最多 2000 个字符。")
        pyautogui = cls._pyautogui()
        try:
            import pyperclip
            previous = pyperclip.paste()
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.15)
            pyperclip.copy(previous)
        except ImportError:
            if not text.isascii():
                return cls._result(False, "输入中文或其他 Unicode 文本需要 pyperclip。")
            pyautogui.write(text, interval=0.02)
        return cls._result(True, f"已输入 {len(text)} 个字符。")

    @classmethod
    def keyboard_hotkey(cls, keys):
        denied = cls._require_session()
        if denied:
            return denied
        raw_keys = str(keys or "").strip().lower()
        parts = [part for part in re.split(r"[+ ]+", raw_keys) if part]
        allowed = {
            "ctrl", "alt", "shift", "win", "enter", "tab", "esc", "escape",
            "backspace", "delete", "home", "end", "pageup", "pagedown",
            "left", "right", "up", "down", "f1", "f2", "f3", "f4", "f5",
            "f6", "f7", "f8", "f9", "f10", "f11", "f12",
        }
        allowed.update(chr(code) for code in range(ord("a"), ord("z") + 1))
        allowed.update(str(number) for number in range(10))
        if not parts or len(parts) > 5 or any(part not in allowed for part in parts):
            return cls._result(False, "快捷键包含不支持的按键。")
        pyautogui = cls._pyautogui()
        pyautogui.hotkey(*parts)
        return cls._result(True, f"已执行快捷键：{'+'.join(parts)}。")

    @classmethod
    def wait(cls, seconds):
        denied = cls._require_session()
        if denied:
            return denied
        seconds = float(seconds)
        if seconds < 0.1 or seconds > 15:
            return cls._result(False, "等待时间必须在 0.1 到 15 秒之间。")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if cls._cancel_event.wait(min(0.1, deadline - time.monotonic())):
                    return cls._result(False, "视觉键鼠任务已被长按 Q 取消。")
        return cls._result(True, f"已等待 {seconds:.1f} 秒。")
