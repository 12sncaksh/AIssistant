"""诊断脚本：验证 no_console_window 补丁是否真的消除了子进程控制台窗口。

判定方法：对子进程用 AttachConsole 挂上它的控制台，再问 GetConsoleWindow。
返回 0 表示这个控制台没有窗口 —— 这才是"看不见"的确凿证据。
（只看 conhost.exe 是不够的：CREATE_NO_WINDOW 也会创建无窗口的控制台宿主。）

用法：python scripts/_check_console_window.py
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

OUT = os.path.join(os.environ.get("TEMP", "."), "console_window_pids.txt")

kernel32 = ctypes.windll.kernel32


def console_window_handle(pid):
    """挂到 pid 的控制台上，返回它的窗口句柄；0 表示无窗口。"""
    if not kernel32.FreeConsole():
        # 本来就没挂控制台也继续，AttachConsole 才是关键
        pass
    if not kernel32.AttachConsole(int(pid)):
        return None  # 该进程没有控制台
    try:
        return int(kernel32.GetConsoleWindow())
    finally:
        kernel32.FreeConsole()


def main():
    if os.name != "nt":
        print("非 Windows，跳过")
        return 0

    # 保持存活约 10 秒，方便检查
    command = ["cmd", "/c", "ping -n 10 127.0.0.1 >nul"]

    before = subprocess.Popen(command)
    time.sleep(1.0)
    before_handle = console_window_handle(before.pid)

    from no_console_window import install

    print("install() 返回:", install())

    after = subprocess.Popen(command)
    time.sleep(1.0)
    after_handle = console_window_handle(after.pid)

    with open(OUT, "w", encoding="utf-8") as handle:
        handle.write(
            f"{before.pid} {after.pid} {before_handle} {after_handle}\n"
        )

    print(f"补丁前 PID={before.pid} 控制台窗口句柄={before_handle}")
    print(f"补丁后 PID={after.pid} 控制台窗口句柄={after_handle}")
    print(f"结果已写入 {OUT}")

    for proc in (before, after):
        try:
            proc.kill()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
