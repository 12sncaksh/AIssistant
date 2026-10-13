"""Runtime path resolution shared by the whole application.

Kept free of Qt imports so that low-level modules (config storage, database)
can reuse it without pulling in the GUI stack.
"""

from __future__ import annotations

import os
import sys


def get_runtime_dir():
    """配置/资源/日志的根目录：打包后是 exe 目录，源码运行时是项目根目录（src 的上一级）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_config_dir():
    """配置文件目录：根目录下的 config。"""
    return os.path.join(get_runtime_dir(), "config")


def get_resource_path(*parts):
    """优先读取 exe 同级外置资源，兼容旧版内部资源布局。"""
    external_path = os.path.join(get_runtime_dir(), *parts)
    if os.path.exists(external_path):
        return external_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


def get_default_avatar_path(avatar_kind):
    """返回用户或 AI 助手的内置默认头像。"""
    filename = "user.png" if avatar_kind == "user" else "mao.png"
    return get_resource_path("assets", "web_resources", filename)


def get_avatar_storage_dir():
    """头像属于运行时数据，不能写入只读的 assets 目录。"""
    path = os.path.join(get_runtime_dir(), "data", "avatars")
    os.makedirs(path, exist_ok=True)
    return path


def resolve_avatar_path(value):
    """把 QSettings 中的项目相对路径解析为绝对路径。"""
    value = str(value or "").strip()
    if not value:
        return ""
    if not os.path.isabs(value):
        value = os.path.join(get_runtime_dir(), value)
    return os.path.abspath(value)


def serialize_avatar_path(path):
    """项目目录内的头像保存为相对路径，方便便携版整体移动。"""
    abs_path = os.path.abspath(str(path or ""))
    runtime_dir = os.path.abspath(get_runtime_dir())
    try:
        relative = os.path.relpath(abs_path, runtime_dir)
    except ValueError:
        return abs_path
    if relative != ".." and not relative.startswith(".." + os.sep):
        return relative.replace(os.sep, "/")
    return abs_path
