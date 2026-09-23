"""Windows low-level input guard for exclusive computer-use sessions."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes


class InputGuard:
    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    HC_ACTION = 0
    WM_QUIT = 0x0012
    LLKHF_INJECTED = 0x00000010
    LLMHF_INJECTED = 0x00000001
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105

    _lock = threading.RLock()
    _active = False
    _thread = None
    _thread_id = 0
    _ready = threading.Event()
    _keyboard_hook = None
    _mouse_hook = None
    _keyboard_callback_ref = None
    _mouse_callback_ref = None
    _user32 = None
    _kernel32 = None
    _last_error = ""
    _q_down = threading.Event()
    _mouse_physical_logged = False
    _mouse_injected_logged = False
    _mouse_callback_seen = False
    _mouse_callback_error_logged = False

    class _KeyboardHookStruct(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _MouseHookStruct(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    @classmethod
    def start(cls):
        if sys.platform != "win32":
            return True
        logging.info("输入钩子注册开始")
        for attempt in range(2):
            with cls._lock:
                if cls._active:
                    logging.info("输入钩子已在运行，继续使用现有接管")
                    return bool(cls._keyboard_hook and cls._mouse_hook)
                cls._active = True
                cls._ready.clear()
                cls._last_error = ""
                cls._mouse_physical_logged = False
                cls._mouse_injected_logged = False
                cls._mouse_callback_seen = False
                cls._mouse_callback_error_logged = False
                cls._thread = threading.Thread(
                    target=cls._hook_thread_main,
                    name="AissistInputGuard",
                    daemon=True,
                )
                cls._thread.start()
            if cls._ready.wait(1.5) and cls._keyboard_hook and cls._mouse_hook:
                logging.info("键盘钩子注册成功")
                logging.info("鼠标钩子注册成功")
                logging.info("输入接管已启用")
                return True
            if attempt == 0:
                logging.warning("Windows 输入钩子第 1 次启动失败：%s，准备重试", cls._last_error or "未知错误")
            cls.stop()
            time.sleep(0.15)
        logging.warning("Windows 输入钩子安装失败，无法启用完整接管：%s", cls._last_error or "未知错误")
        return False

    @classmethod
    def stop(cls):
        if sys.platform != "win32":
            return
        was_active = cls._active or bool(cls._keyboard_hook or cls._mouse_hook)
        if was_active:
            logging.info("输入接管释放开始")
        with cls._lock:
            cls._active = False
            thread_id = cls._thread_id
        if thread_id:
            try:
                user32 = cls._user32 or ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW(thread_id, cls.WM_QUIT, 0, 0)
            except Exception:
                pass
        thread = cls._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(1.0)
        with cls._lock:
            cls._thread = None
            cls._thread_id = 0
            cls._keyboard_hook = None
            cls._mouse_hook = None
            cls._q_down.clear()
            cls._mouse_physical_logged = False
            cls._mouse_injected_logged = False
            cls._mouse_callback_seen = False
            cls._mouse_callback_error_logged = False
        if was_active:
            logging.info("输入接管已释放，用户操作权限已恢复")

    @classmethod
    def is_q_down(cls):
        return cls._q_down.is_set()

    @classmethod
    def _keyboard_callback(cls, n_code, w_param, l_param):
        if n_code == cls.HC_ACTION:
            info = ctypes.cast(l_param, ctypes.POINTER(cls._KeyboardHookStruct)).contents
            injected = bool(info.flags & cls.LLKHF_INJECTED)
            if not injected and info.vkCode == 0x51:
                if w_param in {cls.WM_KEYDOWN, cls.WM_SYSKEYDOWN}:
                    cls._q_down.set()
                    logging.info("低级键盘钩子捕获 Q 按下")
                elif w_param in {cls.WM_KEYUP, cls.WM_SYSKEYUP}:
                    cls._q_down.clear()
                    logging.info("低级键盘钩子捕获 Q 松开")
            if not injected:
                return 1
        return cls._user32.CallNextHookEx(None, n_code, w_param, l_param)

    @classmethod
    def _mouse_callback(cls, n_code, w_param, l_param):
        if n_code == cls.HC_ACTION:
            try:
                if not cls._mouse_callback_seen:
                    cls._mouse_callback_seen = True
                    logging.info("低级鼠标钩子首次收到事件（消息=%s）", w_param)
                info = ctypes.cast(l_param, ctypes.POINTER(cls._MouseHookStruct)).contents
                injected = bool(info.flags & cls.LLMHF_INJECTED)
                if injected:
                    if not cls._mouse_injected_logged:
                        cls._mouse_injected_logged = True
                        logging.info("程序注入鼠标事件已被钩子识别并放行")
                else:
                    if not cls._mouse_physical_logged:
                        cls._mouse_physical_logged = True
                        logging.info("低级鼠标钩子已捕获物理鼠标事件并拦截")
                    return 1
            except Exception:
                if not cls._mouse_callback_error_logged:
                    cls._mouse_callback_error_logged = True
                    logging.exception("低级鼠标钩子解析事件失败")
                return 1
        return cls._user32.CallNextHookEx(None, n_code, w_param, l_param)

    @classmethod
    def _hook_thread_main(cls):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        cls._user32 = user32
        cls._kernel32 = kernel32
        hhook = ctypes.c_void_p
        hinstance = ctypes.c_void_p
        lresult = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, ctypes.c_void_p, hinstance, wintypes.DWORD
        ]
        user32.SetWindowsHookExW.restype = hhook
        user32.UnhookWindowsHookEx.argtypes = [hhook]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.CallNextHookEx.argtypes = [
            hhook, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.CallNextHookEx.restype = lresult
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = hinstance
        cls._thread_id = kernel32.GetCurrentThreadId()
        message = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        keyboard_proc_type = ctypes.WINFUNCTYPE(lresult, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        mouse_proc_type = ctypes.WINFUNCTYPE(lresult, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        cls._keyboard_callback_ref = keyboard_proc_type(cls._keyboard_callback)
        cls._mouse_callback_ref = mouse_proc_type(cls._mouse_callback)
        module_handle = kernel32.GetModuleHandleW(None)
        if not module_handle:
            cls._last_error = f"GetModuleHandleW 失败，错误码 {ctypes.get_last_error()}"
            cls._ready.set()
            return
        ctypes.set_last_error(0)
        cls._keyboard_hook = user32.SetWindowsHookExW(
            cls.WH_KEYBOARD_LL, cls._keyboard_callback_ref, module_handle, 0
        )
        keyboard_error = ctypes.get_last_error()
        if cls._keyboard_hook:
            logging.info("键盘钩子注册成功（句柄=%s）", cls._keyboard_hook)
        else:
            logging.warning("键盘钩子注册失败，错误码 %s", keyboard_error)
        ctypes.set_last_error(0)
        cls._mouse_hook = user32.SetWindowsHookExW(
            cls.WH_MOUSE_LL, cls._mouse_callback_ref, module_handle, 0
        )
        mouse_error = ctypes.get_last_error()
        if cls._mouse_hook:
            logging.info("鼠标钩子注册成功（句柄=%s）", cls._mouse_hook)
        else:
            logging.warning("鼠标钩子注册失败，错误码 %s", mouse_error)
        if not cls._keyboard_hook or not cls._mouse_hook:
            failed_parts = []
            if not cls._keyboard_hook:
                failed_parts.append(f"键盘钩子错误码 {keyboard_error}")
            if not cls._mouse_hook:
                failed_parts.append(f"鼠标钩子错误码 {mouse_error}")
            cls._last_error = "；".join(failed_parts) or "未知错误"
            logging.warning("Windows 输入钩子安装失败：%s", cls._last_error)
            if cls._keyboard_hook:
                user32.UnhookWindowsHookEx(cls._keyboard_hook)
            if cls._mouse_hook:
                user32.UnhookWindowsHookEx(cls._mouse_hook)
            cls._keyboard_hook = None
            cls._mouse_hook = None
            cls._ready.set()
            return

        cls._ready.set()
        try:
            while cls._active and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if cls._keyboard_hook:
                user32.UnhookWindowsHookEx(cls._keyboard_hook)
            if cls._mouse_hook:
                user32.UnhookWindowsHookEx(cls._mouse_hook)
            cls._keyboard_hook = None
            cls._mouse_hook = None
