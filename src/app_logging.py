"""Rotating run log and crash forensics."""

from __future__ import annotations
from app_paths import get_runtime_dir
from logging.handlers import RotatingFileHandler
import faulthandler
import logging
import os
import sys
import tempfile
import traceback



# ======================== 运行日志 ========================
# 崩溃取证文件句柄：必须由模块级引用持住，否则会被 GC 关闭导致 faulthandler 失效
_CRASH_LOG_HANDLE = None


def setup_logging():
    """初始化运行日志：logs/app.log，滚动 2MB×3，并捕获未处理异常。"""
    log_dir = os.path.join(get_runtime_dir(), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        try:
            log_dir = os.path.join(os.path.expanduser("~"), ".aissistant_logs")
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            log_dir = tempfile.gettempdir()
    log_path = os.path.join(log_dir, "app.log")
    handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
    root_logger.addHandler(handler)

    def _excepthook(exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb)
        logging.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _excepthook

    # 硬崩溃取证：C++ 层的访问违例（如 Qt6Core.dll 的 0xc0000005）不会走 excepthook，
    # app.log 里什么都留不下，只能靠 Windows 事件查看器看到“模块+偏移”。开启 faulthandler 后，
    # 崩溃瞬间会把所有线程的 Python 调用栈写进同目录的 crash.log，便于定位到具体槽函数。
    global _CRASH_LOG_HANDLE
    if _CRASH_LOG_HANDLE is None:
        try:
            _CRASH_LOG_HANDLE = open(os.path.join(log_dir, "crash.log"), "a", encoding="utf-8")
            faulthandler.enable(_CRASH_LOG_HANDLE, all_threads=True)
        except Exception:  # noqa: BLE001 - 取证功能失败不能影响启动
            _CRASH_LOG_HANDLE = None
    return log_path


def _log_safe_args(arguments):
    """工具参数脱敏：长内容截断，避免日志过大。"""
    safe = dict(arguments or {})
    for key in ("command", "content"):
        if key in safe:
            text = str(safe[key])
            safe[key] = text[:200] + ("..." if len(text) > 200 else "")
    return safe
