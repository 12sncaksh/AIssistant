"""Per-tool handlers for ActionHandler.execute_tool.

The bodies are the original if-chain branches, moved verbatim. execute_tool still
owns the read-only gate, the try/except wrapper and the unknown-tool fallback; this
module only maps a tool name to its implementation.
"""

from __future__ import annotations
from tool_exec import COMMAND_TIMEOUT_SECONDS
from tools_memory import handle_search_memory
from vision_input import VisionInputController
import os
import sys
import webbrowser


def _tool_open_app(cls, arguments):
    app_name = str(arguments.get("app_name", "")).strip()
    if not app_name:
        return cls._result(False, "应用名称不能为空。")
    if app_name not in cls._apps:
        return cls._result(False, f"未找到应用“{app_name}”，请检查 config/apps.json。")
    path = cls._apps[app_name]
    try:
        if path.startswith(("http://", "https://")):
            webbrowser.open(path)
        elif sys.platform == "win32":
            os.startfile(path)
        else:
            return cls._result(False, f"当前系统不支持打开 {path}")
        return cls._result(True, f"已打开 {app_name}")
    except Exception as e:
        return cls._result(False, f"打开 {app_name} 失败: {str(e)}")


def _tool_open_url(cls, arguments):
    url = str(arguments.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return cls._result(False, "仅支持以 http:// 或 https:// 开头的完整网址。")
    try:
        webbrowser.open(url)
        return cls._result(True, f"已在默认浏览器中打开 {url}")
    except Exception as e:
        return cls._result(False, f"打开网址失败: {str(e)}")


def _tool_inspect_screen(cls, arguments):
    instruction = str(arguments.get("instruction", "")).strip()
    if not instruction:
        return cls._result(False, "视觉定位目标不能为空。")
    return VisionInputController.inspect_screen(instruction)


def _tool_inspect_foreground_window(cls, arguments):
    return VisionInputController.inspect_foreground_window(arguments.get("instruction", ""))


def _tool_inspect_accessibility(cls, arguments):
    return VisionInputController.inspect_accessibility(arguments.get("query", ""))


def _tool_inspect_screen_region(cls, arguments):
    return VisionInputController.inspect_screen_region(
        arguments.get("instruction", ""),
        arguments.get("x"),
        arguments.get("y"),
        arguments.get("width"),
        arguments.get("height"),
    )


def _tool_mouse_move(cls, arguments):
    return VisionInputController.mouse_move(arguments.get("x"), arguments.get("y"))


def _tool_mouse_click(cls, arguments):
    return VisionInputController.mouse_click(
        arguments.get("x"),
        arguments.get("y"),
        arguments.get("button", "left"),
        arguments.get("clicks", 1),
    )


def _tool_verified_mouse_click(cls, arguments):
    return VisionInputController.verified_mouse_click(
        arguments.get("x"),
        arguments.get("y"),
        arguments.get("target_label", ""),
        arguments.get("button", "left"),
        arguments.get("clicks", 1),
    )


def _tool_keyboard_type(cls, arguments):
    return VisionInputController.keyboard_type(arguments.get("text", ""))


def _tool_keyboard_hotkey(cls, arguments):
    return VisionInputController.keyboard_hotkey(arguments.get("keys", ""))


def _tool_computer_wait(cls, arguments):
    return VisionInputController.wait(arguments.get("seconds", 1))


def _tool_wait_for_user_action(cls, arguments):
    return VisionInputController.wait_for_user_action(arguments.get("seconds", 15))


def _tool_control_pet(cls, arguments):
    return cls._handle_control_pet(arguments)


def _tool_set_live2d_param(cls, arguments):
    return cls._handle_set_live2d_param(arguments)


def _tool_list_live2d_params(cls, arguments):
    return cls._handle_list_live2d_params(arguments)


def _tool_get_current_datetime(cls, arguments):
    success, msg, data = cls._get_current_datetime()
    return cls._result(success, msg, data)


def _tool_search_web(cls, arguments):
    query = str(arguments.get("query", "")).strip()
    user_intent = str(arguments.get("_user_intent", "")).strip()
    if not query:
        return cls._result(False, "搜索关键词不能为空。")
    return cls._search_web(query, user_intent=user_intent or None)


def _tool_read_url(cls, arguments):
    url = str(arguments.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return cls._result(False, "仅支持读取以 http:// 或 https:// 开头的网址。")
    text = cls._fetch_page_text(url, timeout=10, max_chars=3000)
    if not text:
        return cls._result(False, f"网页读取失败或内容为空：{url}")
    return cls._result(True, f"已读取网页正文（{len(text)} 字）：\n{text[:3000]}", {"url": url, "content": text[:3000]})


def _tool_get_weather(cls, arguments):
    city = str(arguments.get("city", "")).strip()
    if not city:
        return cls._result(False, "城市名不能为空。")
    success, msg = cls._get_weather(city)
    return cls._result(success, msg)


def _tool_get_forecast(cls, arguments):
    city = str(arguments.get("city", "")).strip()
    days = int(arguments.get("days", 4))
    if not city:
        return cls._result(False, "城市名不能为空。")
    success, msg = cls._get_forecast(city, days)
    return cls._result(success, msg)


def _tool_get_route(cls, arguments):
    origin = str(arguments.get("origin", "")).strip()
    destination = str(arguments.get("destination", "")).strip()
    mode = str(arguments.get("mode", "drive")).strip().lower() or "drive"
    if not origin or not destination:
        return cls._result(False, "路线规划需要起点和终点。")
    success, msg = cls._get_route(origin, destination, mode)
    return cls._result(success, msg)


def _tool_system_status(cls, arguments):
    success, msg = cls._get_system_status()
    return cls._result(success, msg)


def _tool_health_check(cls, arguments):
    success, msg = cls._get_health_check()
    return cls._result(success, msg)


def _tool_disk_scan(cls, arguments):
    success, msg, data = cls._scan_disk_junk()
    return cls._result(success, msg, data)


def _tool_network_info(cls, arguments):
    return cls._result(True, cls._network_info())


def _tool_speedtest(cls, arguments):
    return cls._result(True, cls._speedtest())


def _tool_ping_host(cls, arguments):
    host = str(arguments.get("host", "")).strip() or "8.8.8.8"
    return cls._result(True, cls._ping_host(host))


def _tool_recommend_food_by_city(cls, arguments):
    city = str(arguments.get("city", "")).strip()
    keyword = str(arguments.get("keyword", "")).strip() or "美食"
    if not city:
        return cls._result(False, "城市名不能为空。")
    success, msg = cls._search_food(city=city, keyword=keyword)
    return cls._result(success, msg)


def _tool_recommend_food_by_address(cls, arguments):
    address = str(arguments.get("address", "")).strip()
    radius = int(arguments.get("radius", 2000))
    keyword = str(arguments.get("keyword", "")).strip() or "美食"
    if not address:
        return cls._result(False, "地址不能为空。")
    lng, lat = cls._geocode(address)
    if lng is None or lat is None:
        return cls._result(False, f"无法解析地址“{address}”，请提供更详细的位置信息。")
    success, msg = cls._search_food(location=f"{lng},{lat}", radius=radius, keyword=keyword)
    return cls._result(success, msg)


def _tool_recommend_food_by_coordinates(cls, arguments):
    lng = arguments.get("lng")
    lat = arguments.get("lat")
    radius = int(arguments.get("radius", 2000))
    keyword = str(arguments.get("keyword", "")).strip() or "美食"
    if lng is None or lat is None:
        return cls._result(False, "经纬度不能为空。")
    success, msg = cls._search_food(location=f"{lng},{lat}", radius=radius, keyword=keyword)
    return cls._result(success, msg)


def _tool_lock_screen(cls, arguments):
    success, msg = cls._lock_screen()
    return cls._result(success, msg)


def _tool_shutdown(cls, arguments):
    success, msg = cls._shutdown()
    return cls._result(success, msg)


def _tool_reboot(cls, arguments):
    success, msg = cls._reboot()
    return cls._result(success, msg)


def _tool_run_command(cls, arguments):
    command = str(arguments.get("command", "")).strip()
    if not command:
        return cls._result(False, "命令不能为空。")
    success, msg, data = cls._run_command(command, COMMAND_TIMEOUT_SECONDS, cwd=cls._agent_workspace or None)
    return cls._result(success, msg, data)


def _tool_read_file(cls, arguments):
    path = str(arguments.get("path", "")).strip()
    if not path:
        return cls._result(False, "文件路径不能为空。")
    path, ws_err = cls._resolve_agent_path(path)
    if ws_err:
        return cls._result(False, ws_err)
    if os.path.basename(path).lower() == "auth.json":
        return cls._result(False, "已阻止读取 config/auth.json（该文件包含密钥配置）。")
    if not os.path.isfile(path):
        return cls._result(False, "文件不存在: {path}".format(path=path))
    try:
        size = os.path.getsize(path)
        if size > 512 * 1024:
            return cls._result(False, "文件过大（{size} 字节），暂不支持读取超过 512KB 的文件。".format(size=size))
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return cls._result(True, "已读取文件（{size} 字节）。".format(size=size), {"content": content, "path": path})
    except Exception as e:
        return cls._result(False, "读取文件失败: {e}".format(e=e))


def _tool_write_file(cls, arguments):
    path = str(arguments.get("path", "")).strip()
    content = str(arguments.get("content", ""))
    if not path:
        return cls._result(False, "文件路径不能为空。")
    path, ws_err = cls._resolve_agent_path(path)
    if ws_err:
        return cls._result(False, ws_err)
    lower_path = os.path.abspath(path).lower()
    # 受保护写入路径在任何权限模式下都拦截：往系统目录和版本库里写基本只有搞破坏才会用到，
    # 属于保命拦截而非沙箱边界（完全权限模式放开的是工作区外的读写范围）。
    for blocked_area in cls.BLOCKED_WRITE_PATHS:
        if lower_path.startswith(blocked_area):
            return cls._result(False, f"已阻止写入受保护路径：{path}（{blocked_area}）。")
    if "\\.git\\" in lower_path or lower_path.endswith("\\.git"):
        return cls._result(False, "已阻止写入 .git 目录（避免破坏版本库）。")
    # 凭证文件同样在所有权限模式下都不允许覆盖
    if lower_path.endswith("auth.json"):
        return cls._result(False, "已阻止覆盖 config/auth.json（该文件包含密钥配置）。")
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return cls._result(True, "已写入文件: {path}（{length} 字符）。".format(path=path, length=len(content)))
    except Exception as e:
        return cls._result(False, "写入文件失败: {e}".format(e=e))


def _tool_list_dir(cls, arguments):
    path = str(arguments.get("path", "")).strip() or "."
    path, ws_err = cls._resolve_agent_path(path)
    if ws_err:
        return cls._result(False, ws_err)
    if not os.path.isdir(path):
        return cls._result(False, "目录不存在: {path}".format(path=path))
    try:
        import time
        entries = []
        for name in sorted(os.listdir(path))[:200]:
            full = os.path.join(path, name)
            is_dir = os.path.isdir(full)
            try:
                size = "" if is_dir else os.path.getsize(full)
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(full)))
            except OSError:
                size, mtime = "", ""
            entries.append({"name": name, "type": "dir" if is_dir else "file", "size": size, "modified": mtime})
        return cls._result(True, "目录共 {n} 项（最多显示 200 项）。".format(n=len(entries)), {"entries": entries, "path": path})
    except Exception as e:
        return cls._result(False, "读取目录失败: {e}".format(e=e))


# 工具名 -> 实现。execute_tool 只通过这张表分发，权限门仍在外层。
TOOL_DISPATCH = {
    "open_app": _tool_open_app,
    "open_url": _tool_open_url,
    "inspect_screen": _tool_inspect_screen,
    "inspect_foreground_window": _tool_inspect_foreground_window,
    "inspect_accessibility": _tool_inspect_accessibility,
    "inspect_screen_region": _tool_inspect_screen_region,
    "mouse_move": _tool_mouse_move,
    "mouse_click": _tool_mouse_click,
    "verified_mouse_click": _tool_verified_mouse_click,
    "keyboard_type": _tool_keyboard_type,
    "keyboard_hotkey": _tool_keyboard_hotkey,
    "computer_wait": _tool_computer_wait,
    "wait_for_user_action": _tool_wait_for_user_action,
    "control_pet": _tool_control_pet,
    "set_live2d_param": _tool_set_live2d_param,
    "list_live2d_params": _tool_list_live2d_params,
    "get_current_datetime": _tool_get_current_datetime,
    "search_web": _tool_search_web,
    "read_url": _tool_read_url,
    "get_weather": _tool_get_weather,
    "get_forecast": _tool_get_forecast,
    "get_route": _tool_get_route,
    "system_status": _tool_system_status,
    "health_check": _tool_health_check,
    "disk_scan": _tool_disk_scan,
    "network_info": _tool_network_info,
    "speedtest": _tool_speedtest,
    "ping_host": _tool_ping_host,
    "recommend_food_by_city": _tool_recommend_food_by_city,
    "recommend_food_by_address": _tool_recommend_food_by_address,
    "recommend_food_by_coordinates": _tool_recommend_food_by_coordinates,
    "lock_screen": _tool_lock_screen,
    "shutdown": _tool_shutdown,
    "reboot": _tool_reboot,
    "run_command": _tool_run_command,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "list_dir": _tool_list_dir,
    "search_memory": handle_search_memory,
}
