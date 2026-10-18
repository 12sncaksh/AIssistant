"""UI theme application and the latest-output pill styling."""

from __future__ import annotations
from PyQt6.QtWidgets import QApplication
from app_theme import UI_THEMES
from app_theme import apply_native_window_frame_theme
from app_theme import build_scrollbar_stylesheet
from app_theme import build_ui_palette
from app_theme import get_ui_theme_palette
from app_theme import normalize_ui_theme
from chat_style import color_to_rgba



def _apply_ui_theme(self):
    """按当前主题重刷主窗口配色；暗色取值与改动前逐字一致。"""
    theme = normalize_ui_theme(getattr(self, "ui_theme", "dark"))
    palette = get_ui_theme_palette(theme)
    app = QApplication.instance()
    if app is not None:
        app.setPalette(build_ui_palette(theme))
        app.setStyleSheet(build_scrollbar_stylesheet(palette))
    apply_native_window_frame_theme(self, theme)
    self.central_widget.setStyleSheet(
        "#mainCentral { background-color: %s; }" % palette["window_bg"]
    )
    self._style_latest_output_bar()
    self.session_panel.setStyleSheet(
        "#sessionPanel { background-color: %s; border-right: 1px solid %s; }"
        % (palette["panel_bg"], palette["panel_border"])
    )
    self.brand_title.setStyleSheet(
        "color: %s; font-size: 19px; font-weight: 700;" % palette["brand_text"]
    )
    self.session_label.setStyleSheet(
        "color: %s; font-size: 14px; font-weight: 600;" % palette["label_text"]
    )
    self.session_search.setStyleSheet(
        "QLineEdit { background: %s; color: %s; border: 1px solid %s; "
        "border-radius: 4px; padding: 4px 7px; }"
        "QLineEdit:focus { border: 1px solid %s; }"
        % (
            palette["input_bg"],
            palette["item_text"],
            palette["input_border"],
            palette["input_focus_border"],
        )
    )
    self.session_list.setStyleSheet(
        "QListWidget { background: transparent; color: %s; border: none; padding: 2px 0; }"
        "QListWidget::item { padding: 8px 9px; border-radius: 5px; }"
        "QListWidget::item:hover { background: %s; }"
        "QListWidget::item:selected { background: %s; color: %s; }"
        % (
            palette["item_text"],
            palette["item_hover"],
            palette["item_selected_bg"],
            palette["item_selected_text"],
        )
    )
    self.session_separator.setStyleSheet(
        "background-color: %s;" % palette["separator"]
    )
    self.new_session_bottom_button.setStyleSheet(
        "QPushButton { background: %s; color: %s; border: none; border-radius: 20px; "
        "font-size: 15px; font-weight: 600; }"
        "QPushButton:hover { background: %s; }"
        "QPushButton:pressed { background: %s; }"
        % (
            palette["accent_bg"],
            palette["accent_fg"],
            palette["accent_hover"],
            palette["accent_pressed"],
        )
    )
    self.content_panel.setStyleSheet(
        "#contentPanel { border: 1px solid %s; border-radius: 16px; background: transparent; }"
        % palette["content_border"]
    )
    # 输入框底透明度：首次从配置读入，之后由「聊天外观」的实时预览改写
    if not hasattr(self, "composer_opacity"):
        self.composer_opacity = self.config.get_composer_opacity()
    self.composer.setStyleSheet(
        "#composer { background: %s; border: 1px solid %s; border-radius: 12px; }"
        "QTextEdit { background: transparent; border: none; color: %s; "
        "font-size: 15px; padding: 8px 10px; }"
        "QTextEdit::placeholder { color: %s; }"
        "QToolButton { color: %s; border: none; border-radius: 18px; padding: 5px 8px; }"
        "QToolButton:hover { background: %s; }"
        "QComboBox { background: %s; color: %s; border: 1px solid %s; "
        "border-radius: 5px; padding: 4px 8px; }"
        % (
            color_to_rgba(palette["composer_base"], self.composer_opacity),
            palette["composer_border"],
            palette["composer_text"],
            palette["placeholder"],
            palette["tool_color"],
            palette["tool_hover"],
            palette["combo_bg"],
            palette["combo_text"],
            palette["composer_border"],
        )
    )
    self.attach_label.setStyleSheet(
        "color: %s; font-size: 12px; padding: 0 8px;" % palette["hint_text"]
    )
    self.attach_btn.setStyleSheet(
        "QToolButton { color: %s; font-size: 22px; border: none; border-radius: 18px; "
        "padding: 2px 9px; }" % palette["tool_color"]
    )
    self.send_btn.setStyleSheet(
        "QToolButton { background: %s; color: %s; border: none; border-radius: 21px; }"
        % (palette["send_bg"], palette["send_fg"])
    )
    self.stop_btn.setStyleSheet(
        "QToolButton { background: %s; color: %s; border: none; border-radius: 21px; }"
        % (palette["stop_bg"], palette["stop_fg"])
    )
    self.session_splitter.setStyleSheet(
        "QSplitter { background: transparent; }"
        "QSplitter::handle { background: %s; }"
        "QSplitter::handle:hover { background: %s; }"
        % (palette["splitter_handle"], palette["splitter_hover"])
    )
    panel = getattr(self, "chat_background_panel", None)
    if panel is not None:
        panel.set_base_color(palette["canvas_bg"])


def _refresh_theme_actions(self):
    for theme_key, action in getattr(self, "theme_actions", {}).items():
        action.setChecked(theme_key == getattr(self, "ui_theme", "dark"))


def set_ui_theme(self, name):
    """切换明色/暗色主题并记住选择。"""
    theme = str(name or "").strip().lower()
    if theme not in UI_THEMES:
        return
    if theme == getattr(self, "ui_theme", None):
        self._refresh_theme_actions()
        return
    self.ui_theme = theme
    self.config.set_ui_theme(theme)
    self._apply_ui_theme()
    self._refresh_theme_actions()
    status_bar = getattr(self, "status_bar", None)
    if status_bar is not None:
        status_bar.showMessage(
            "已切换到%s主题" % ("暗色" if theme == "dark" else "明色"), 2000
        )


def _style_latest_output_bar(self):
    """「回到最新输出」药丸：浅灰底 + 深灰字，跟随当前主题。

    半透明用样式表 rgba 实现，不要用 WA_TranslucentBackground——
    后者会让这个子 QFrame 的背景和边框整个不绘制，只剩文字。
    """
    bar = getattr(self, "latest_output_bar", None)
    if bar is None:
        return
    if normalize_ui_theme(getattr(self, "ui_theme", "dark")) == "light":
        fill = "rgba(228, 231, 236, 246)"
        border = "rgba(201, 208, 217, 255)"
        text = "#374151"
        hover_fill = "rgba(213, 219, 228, 250)"
        hover_text = "#1d4ed8"
    else:
        fill = "rgba(56, 63, 71, 240)"
        border = "rgba(255, 255, 255, 64)"
        text = "#E8ECF1"
        hover_fill = "rgba(72, 80, 90, 246)"
        hover_text = "#8ED8FF"
    bar.setStyleSheet(
        "QFrame#latestOutputBar {"
        f"background-color: {fill};"
        f"border: 1px solid {border};"
        "border-radius: 20px;"
        "}"
        f"QToolButton {{ color: {text}; background: transparent; border: none;"
        " padding: 6px 12px; font-weight: 600; }"
        f"QToolButton:hover {{ color: {hover_text}; background-color: {hover_fill};"
        " border-radius: 16px; }"
    )
