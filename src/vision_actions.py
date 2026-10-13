"""Mouse / keyboard actions and input-guard handling."""

from __future__ import annotations
from accessibility_input import AccessibilityController
from input_guard import InputGuard
import logging
import re
import time



def _require_session(cls):
    if not cls._session_active:
        return cls._result(False, "视觉键鼠会话尚未授权。")
    if cls._cancel_event.is_set():
        return cls._result(False, "视觉键鼠任务已被长按 Q 取消。")
    return None


def _result(cls, success, message, data=None):
    result = {"success": bool(success), "message": str(message)}
    if data is not None:
        result["data"] = data
    return result


def _pyautogui():
    try:
        import pyautogui
    except ImportError as exc:
        raise RuntimeError("缺少 pyautogui，请先安装视觉键鼠依赖。") from exc
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.12
    return pyautogui


def _validate_point(cls, x, y):
    left, top, width, height = cls._screen_bounds()
    x = int(x)
    y = int(y)
    if not (left <= x < left + width and top <= y < top + height):
        raise ValueError(f"坐标 ({x}, {y}) 超出当前桌面范围。")
    return x, y


def mouse_move(cls, x, y):
    denied = cls._require_session()
    if denied:
        return denied
    x, y = cls._validate_point(x, y)
    pyautogui = cls._pyautogui()
    pyautogui.moveTo(x, y, duration=0.12)
    return cls._result(True, f"鼠标已移动到 ({x}, {y})。", {"x": x, "y": y})


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


def suspend_input_guard_for_user_confirmation(cls):
    if not cls._session_active:
        return True
    InputGuard.stop()
    cls._input_guard_suspended = True
    logging.info("已临时释放键鼠接管，等待用户确认授权")
    return True


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
