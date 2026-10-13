"""让本进程启动的所有子进程都不弹出控制台窗口（仅 Windows 需要）。

## 为什么需要这个模块

Windows 上，当一个**没有控制台可继承**的进程去启动一个控制台程序时，系统会为
子进程**新建一个控制台窗口**。桌面上看到的就是「连续弹出好几个黑窗口」。

本程序启动时会拉起不少子进程：

* 多个自带的 MCP server（经 `mcp` SDK 的 `stdio_client` 内部 spawn）
* DSH 桥接（`cmd /c dsh ...`）
* 工具调用（`powershell -Command ...`）

其中 MCP server 那条路径由第三方库内部创建进程，**没有地方可以逐个传
`creationflags`**。所以这里统一在 `subprocess.Popen` 上补 `CREATE_NO_WINDOW`：
`mcp` / `anyio` 在 Windows 上最终都会走到 `subprocess.Popen`（asyncio 的
`windows_utils.Popen` 继承自它），因此这一处即可覆盖全部子进程。

## 行为

* 幂等，重复调用只生效一次
* 非 Windows 平台直接跳过
* 调用方**显式**要求新控制台或脱离控制台（`CREATE_NEW_CONSOLE` /
  `DETACHED_PROCESS`）时不叠加 —— 那些标志本身已经决定了窗口行为
"""

from __future__ import annotations

import logging
import os
import subprocess

# 见 CreateProcess 文档：控制台程序以无窗口方式运行。
CREATE_NO_WINDOW_FALLBACK = 0x08000000

_installed = False


def install():
    """给 subprocess.Popen 统一补上 CREATE_NO_WINDOW。返回是否本次生效。"""
    global _installed
    if os.name != "nt":
        return False
    if _installed:
        return False

    original_init = subprocess.Popen.__init__
    if getattr(original_init, "_aissistant_no_window", False):
        _installed = True
        return False

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW_FALLBACK)
    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)

    def patched_init(self, *args, **kwargs):
        flags = int(kwargs.get("creationflags") or 0)
        if not (flags & (new_console | detached)):
            kwargs["creationflags"] = flags | no_window
        return original_init(self, *args, **kwargs)

    patched_init._aissistant_no_window = True
    subprocess.Popen.__init__ = patched_init
    _installed = True
    logging.info("已启用子进程无窗口模式（CREATE_NO_WINDOW），不会再弹出控制台窗口")
    return True
