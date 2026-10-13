"""UI themes, palettes and window chrome helpers."""

from __future__ import annotations
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QIcon
from PyQt6.QtGui import QPalette
from app_paths import get_resource_path
import logging
import os
import sys



# ======================== 界面主题（暗色/明色） ========================
# 暗色取值与旧版硬编码颜色逐字一致，切回暗色观感不变。
UI_THEMES = {
    "dark": {
        "window_bg": "#1f1f1f",
        "panel_bg": "#252525",
        "panel_border": "#444444",
        "brand_text": "#eeeeee",
        "label_text": "#d7d7d7",
        "input_bg": "#303030",
        "input_border": "#4b4b4b",
        "input_focus_border": "#6688aa",
        "item_text": "#eeeeee",
        "item_hover": "#383838",
        "item_selected_bg": "#34495e",
        "item_selected_text": "#ffffff",
        "separator": "#4b4b4b",
        "accent_bg": "#29465f",
        "accent_fg": "#52a9ff",
        "accent_hover": "#345a79",
        "accent_pressed": "#223d53",
        "content_border": "#46515f",
        "composer_bg": "rgba(45, 45, 45, 235)",
        "composer_border": "#555",
        "composer_text": "#eeeeee",
        "placeholder": "#929292",
        "tool_color": "#d8d8d8",
        "tool_hover": "#484848",
        "combo_bg": "#363636",
        "combo_text": "#dddddd",
        "send_bg": "#e7edf2",
        "send_fg": "#252a2e",
        "stop_bg": "#d85c5c",
        "stop_fg": "#ffffff",
        "hint_text": "#aeb8c2",
        "splitter_handle": "#1f1f1f",
        "splitter_hover": "#4c7396",
        "canvas_bg": "#1f1f1f",
        "scroll_track": "#262626",
        "scroll_handle": "#4a4a4a",
        "scroll_handle_hover": "#5f5f5f",
        "menu_bg": "#1e1e1e",
        "menu_text": "#ffffff",
        "menu_border": "#3c3c3c",
    },
    "light": {
        "window_bg": "#f5f6f8",
        "panel_bg": "#ffffff",
        "panel_border": "#d9dde3",
        "brand_text": "#1f2937",
        "label_text": "#4b5563",
        "input_bg": "#ffffff",
        "input_border": "#cbd5e1",
        "input_focus_border": "#3b82f6",
        "item_text": "#1f2937",
        "item_hover": "#eef2f7",
        "item_selected_bg": "#dbeafe",
        "item_selected_text": "#1d4ed8",
        "separator": "#e5e7eb",
        "accent_bg": "#dbeafe",
        "accent_fg": "#1d4ed8",
        "accent_hover": "#c7dbfd",
        "accent_pressed": "#b9d2fb",
        "content_border": "#d9dde3",
        "composer_bg": "rgba(255, 255, 255, 240)",
        "composer_border": "#d9dde3",
        "composer_text": "#1f2937",
        "placeholder": "#9aa3af",
        "tool_color": "#4b5563",
        "tool_hover": "#eceff3",
        "combo_bg": "#ffffff",
        "combo_text": "#374151",
        "send_bg": "#2563eb",
        "send_fg": "#ffffff",
        "stop_bg": "#dc2626",
        "stop_fg": "#ffffff",
        "hint_text": "#6b7280",
        "splitter_handle": "#e5e8ed",
        "splitter_hover": "#b9c6d6",
        "canvas_bg": "#f7f8fa",
        "scroll_track": "#eef1f5",
        "scroll_handle": "#c4cbd6",
        "scroll_handle_hover": "#aab4c3",
        "menu_bg": "#ffffff",
        "menu_text": "#1f2937",
        "menu_border": "#d9dde3",
    },
}


# 应用级调色板：之前没写样式表的控件（菜单栏、状态栏、滚动条、右键菜单、
# 提示气泡、各类对话框）全部吃系统调色板，系统是深色主题时明色界面就会留一圈深色。
UI_THEME_PALETTES = {
    "dark": {
        "WindowText": "#ffffff",
        "Button": "#3c3c3c",
        "Light": "#787878",
        "Midlight": "#5a5a5a",
        "Dark": "#1e1e1e",
        "Mid": "#282828",
        "Text": "#ffffff",
        "BrightText": "#256bb1",
        "ButtonText": "#ffffff",
        "Base": "#2d2d2d",
        "AlternateBase": "#071421",
        "Window": "#1e1e1e",
        "Shadow": "#000000",
        "Highlight": "#163f69",
        "HighlightedText": "#ffffff",
        "Link": "#256bb1",
        "LinkVisited": "#1e5993",
        "ToolTipBase": "#3c3c3c",
        "ToolTipText": "#d4d4d4",
        "PlaceholderText": "#969696",
    },
    "light": {
        "WindowText": "#1f2937",
        "Button": "#ffffff",
        "Light": "#ffffff",
        "Midlight": "#f2f4f7",
        "Dark": "#c7ced8",
        "Mid": "#d9dde3",
        "Text": "#1f2937",
        "BrightText": "#ffffff",
        "ButtonText": "#1f2937",
        "Base": "#ffffff",
        "AlternateBase": "#f3f4f6",
        "Window": "#f5f6f8",
        "Shadow": "#9aa3af",
        "Highlight": "#dbeafe",
        "HighlightedText": "#1d4ed8",
        "Link": "#2563eb",
        "LinkVisited": "#7c3aed",
        "ToolTipBase": "#ffffff",
        "ToolTipText": "#1f2937",
        "PlaceholderText": "#9aa3af",
    },
}


def normalize_ui_theme(name):
    key = str(name or "").strip().lower()
    return key if key in UI_THEMES else "dark"


def get_ui_theme_palette(name):
    return UI_THEMES[normalize_ui_theme(name)]


def build_ui_palette(name):
    """按主题构建应用级调色板，避免系统深色主题渗进菜单栏、状态栏和滚动条。"""
    values = UI_THEME_PALETTES[normalize_ui_theme(name)]
    palette = QPalette()
    for role_name, color in values.items():
        role = getattr(QPalette.ColorRole, role_name, None)
        if role is not None:
            palette.setColor(role, QColor(color))
    return palette


def build_scrollbar_stylesheet(palette):
    """统一下拉条外观：不写样式时滚动条会跟着系统主题画成黑条。"""
    return (
        "QScrollBar:vertical { background: %(scroll_track)s; width: 10px; margin: 0px; border: none; }"
        "QScrollBar:horizontal { background: %(scroll_track)s; height: 10px; margin: 0px; border: none; }"
        "QScrollBar::handle { background: %(scroll_handle)s; border-radius: 5px; }"
        "QScrollBar::handle:vertical { min-height: 30px; }"
        "QScrollBar::handle:horizontal { min-width: 30px; }"
        "QScrollBar::handle:hover { background: %(scroll_handle_hover)s; }"
        "QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; background: none; border: none; }"
        "QScrollBar::add-page, QScrollBar::sub-page { background: none; }"
    ) % palette


def style_popup_menu(menu, theme="dark"):
    """弹出菜单要显式上色：桌宠窗口的 background:transparent 会顺着父样式表把菜单刷黑。"""
    menu.setStyleSheet(
        "QMenu { background: %(menu_bg)s; color: %(menu_text)s; "
        "border: 1px solid %(menu_border)s; padding: 4px 2px; }"
        "QMenu::item { padding: 6px 22px 6px 14px; border-radius: 4px; }"
        "QMenu::item:selected { background: %(item_selected_bg)s; color: %(item_selected_text)s; }"
        "QMenu::separator { height: 1px; background: %(separator)s; margin: 4px 8px; }"
        % get_ui_theme_palette(theme)
    )
    return menu


def apply_native_window_frame_theme(window, theme):
    """让 Windows 原生标题栏和窗口边框跟随界面主题；非 Windows 直接跳过。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        hwnd = int(window.winId())
        dark = ctypes.c_int(1 if normalize_ui_theme(theme) == "dark" else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE，旧系统用 19
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attribute),
                ctypes.byref(dark),
                ctypes.sizeof(dark),
            )
            if result == 0:
                break
    except Exception:
        logging.debug("设置窗口边框主题失败", exc_info=True)


def get_app_icon():
    """读取应用图标；开发运行和 one-dir 打包都从程序资源目录加载。"""
    icon_path = get_resource_path("assets", "Aissistant.ico")
    if not os.path.exists(icon_path):
        return QIcon()
    icon = QIcon(icon_path)
    return icon if not icon.isNull() else QIcon()
