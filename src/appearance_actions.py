"""Chat appearance, colours, fonts and background actions."""

from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog
from PyQt6.QtWidgets import QDialogButtonBox
from PyQt6.QtWidgets import QFileDialog
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtWidgets import QSlider
from PyQt6.QtWidgets import QVBoxLayout
from app_paths import get_runtime_dir
from appearance_dialogs import AvatarSettingsDialog
from appearance_dialogs import ChatAppearanceDialog
from appearance_dialogs import ImageCropDialog
from appearance_dialogs import SimpleFontDialog
from chat_markdown import DEFAULT_CODE_BLOCK_COLOR
from chat_message_widget import ChatMessageWidget
from chat_style import DEFAULT_BUBBLE_ASSISTANT_COLOR
from chat_style import DEFAULT_BUBBLE_USER_COLOR
from chat_style import clamp_chat_font_size
from chat_style import clamp_opacity
from chat_style import normalize_hex_color
from task_progress_widget import TaskProgressWidget
import os
import uuid



def update_all_bubbles_opacity(self, opacity):
    for i in range(self.chat_list.count()):
        item = self.chat_list.item(i)
        widget = self.chat_list.itemWidget(item)
        if widget and isinstance(widget, ChatMessageWidget):
            widget.update_opacity(opacity)
            widget.refresh_layout()
        elif isinstance(widget, TaskProgressWidget):
            widget.update_opacity(opacity)
            widget.refresh_layout()


def update_all_code_opacity(self, code_opacity):
    for i in range(self.chat_list.count()):
        item = self.chat_list.item(i)
        widget = self.chat_list.itemWidget(item)
        if widget and isinstance(widget, ChatMessageWidget):
            widget.update_code_opacity(code_opacity)


def update_all_chat_colors(self):
    for i in range(self.chat_list.count()):
        item = self.chat_list.item(i)
        widget = self.chat_list.itemWidget(item)
        if isinstance(widget, ChatMessageWidget):
            widget.update_bubble_color(
                self.bubble_user_color if widget.is_user else self.bubble_assistant_color
            )
            widget.update_code_color(self.code_block_color)
            widget.refresh_layout()
        elif isinstance(widget, TaskProgressWidget):
            widget.update_bubble_color(self.bubble_assistant_color)
            widget.refresh_layout()


def show_opacity_dialog(self):
    current_opacity = self.bubble_opacity
    dialog = QDialog(self)
    dialog.setWindowTitle("透明度设置")
    layout = QVBoxLayout(dialog)

    bubble_label = QLabel("气泡透明度")
    layout.addWidget(bubble_label)

    bubble_slider = QSlider(Qt.Orientation.Horizontal)
    bubble_slider.setRange(0, 100)
    bubble_slider.setValue(int(current_opacity * 100))
    layout.addWidget(bubble_slider)

    bubble_value_label = QLabel(f"{current_opacity:.2f}")
    layout.addWidget(bubble_value_label)

    def on_bubble_slider_changed(val):
        opacity = val / 100.0
        bubble_value_label.setText(f"{opacity:.2f}")
        self.update_all_bubbles_opacity(opacity)

    bubble_slider.valueChanged.connect(on_bubble_slider_changed)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec() == QDialog.DialogCode.Accepted:
        new_opacity = bubble_slider.value() / 100.0
        self.bubble_opacity = new_opacity
        self.config.set_bubble_opacity(new_opacity)
        self.update_all_bubbles_opacity(new_opacity)
    else:
        self.update_all_bubbles_opacity(current_opacity)


def show_code_opacity_dialog(self):
    current_opacity = self.code_opacity
    dialog = QDialog(self)
    dialog.setWindowTitle("代码块透明度")
    layout = QVBoxLayout(dialog)

    layout.addWidget(QLabel("代码块透明度"))
    code_slider = QSlider(Qt.Orientation.Horizontal)
    code_slider.setRange(0, 100)
    code_slider.setValue(int(current_opacity * 100))
    layout.addWidget(code_slider)

    value_label = QLabel(f"{current_opacity:.2f}")
    layout.addWidget(value_label)

    def on_code_slider_changed(value):
        opacity = value / 100.0
        value_label.setText(f"{opacity:.2f}")
        self.update_all_code_opacity(opacity)

    code_slider.valueChanged.connect(on_code_slider_changed)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec() == QDialog.DialogCode.Accepted:
        new_opacity = code_slider.value() / 100.0
        self.code_opacity = new_opacity
        self.config.set_code_opacity(new_opacity)
        self.update_all_code_opacity(new_opacity)
    else:
        self.update_all_code_opacity(current_opacity)


def update_all_avatar_widgets(self):
    user_avatar = self.config.get_avatar_path("user")
    assistant_avatar = self.config.get_avatar_path("assistant")
    for i in range(self.chat_list.count()):
        item = self.chat_list.item(i)
        widget = self.chat_list.itemWidget(item)
        if isinstance(widget, ChatMessageWidget):
            widget.set_avatar_path(user_avatar if widget.is_user else assistant_avatar)
            widget.refresh_layout()
        elif isinstance(widget, TaskProgressWidget):
            widget.set_avatar_path(assistant_avatar)
            widget.refresh_layout()


def show_avatar_dialog(self):
    dialog = AvatarSettingsDialog(self.config, self)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    self.update_all_avatar_widgets()
    self.status_bar.showMessage("头像已更新", 2000)


def update_all_text_font_widgets(self):
    for i in range(self.chat_list.count()):
        item = self.chat_list.item(i)
        widget = self.chat_list.itemWidget(item)
        if isinstance(widget, ChatMessageWidget):
            widget.set_font_settings(
                self.chat_font_family,
                self.chat_font_size,
                self.code_font_family,
                self.code_font_size,
            )
        elif isinstance(widget, TaskProgressWidget):
            widget.set_font_settings(self.chat_font_family, self.chat_font_size)


def show_chat_font_dialog(self):
    current_font = QFont(self.chat_font_family, self.chat_font_size)
    dialog = SimpleFontDialog(
        current_font,
        "\u9009\u62e9\u804a\u5929\u6b63\u6587\u5b57\u4f53",
        self,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    font = dialog.selected_font()
    self.chat_font_family = font.family()
    self.chat_font_size = clamp_chat_font_size(font.pointSize(), 10)
    self.config.set_chat_font(font)
    self.update_all_text_font_widgets()
    self.status_bar.showMessage("聊天正文字体已更新", 2000)


def show_code_font_dialog(self):
    current_font = QFont(self.code_font_family, self.code_font_size)
    dialog = SimpleFontDialog(
        current_font,
        "\u9009\u62e9\u4ee3\u7801\u5757\u5b57\u4f53",
        self,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    font = dialog.selected_font()
    self.code_font_family = font.family()
    self.code_font_size = clamp_chat_font_size(font.pointSize(), 10)
    self.config.set_code_font(font)
    self.update_all_text_font_widgets()
    self.status_bar.showMessage("代码块字体已更新", 2000)


def refresh_all_message_widgets(self):
    for i in range(self.chat_list.count()):
        item = self.chat_list.item(i)
        widget = self.chat_list.itemWidget(item)
        if widget and isinstance(widget, ChatMessageWidget):
            widget.refresh_layout()


def _read_chat_appearance_state(self):
    background_path = str(self._resolve_saved_chat_background() or "")
    return {
        "background_path": background_path,
        "background_mode": self.chat_background_mode,
        "bubble_user_color": self.bubble_user_color,
        "bubble_assistant_color": self.bubble_assistant_color,
        "bubble_opacity": self.bubble_opacity,
        "background_opacity": self.background_opacity,
        "composer_opacity": self.composer_opacity,
        "code_block_color": self.code_block_color,
        "code_opacity": self.code_opacity,
        "chat_font_family": self.chat_font_family,
        "chat_font_size": self.chat_font_size,
        "code_font_family": self.code_font_family,
        "code_font_size": self.code_font_size,
    }


def _apply_chat_appearance_state(self, values):
    self.chat_background_mode = (
        values.get("background_mode")
        if values.get("background_mode") in {"fill", "fit", "stretch", "tile", "center"}
        else "stretch"
    )
    self.bubble_user_color = normalize_hex_color(
        values.get("bubble_user_color"), DEFAULT_BUBBLE_USER_COLOR
    )
    self.bubble_assistant_color = normalize_hex_color(
        values.get("bubble_assistant_color"), DEFAULT_BUBBLE_ASSISTANT_COLOR
    )
    self.bubble_opacity = clamp_opacity(values.get("bubble_opacity"), 0.85)
    self.code_block_color = normalize_hex_color(
        values.get("code_block_color"), DEFAULT_CODE_BLOCK_COLOR
    )
    self.code_opacity = clamp_opacity(values.get("code_opacity"), 0.85)
    self.background_opacity = clamp_opacity(values.get("background_opacity"), 1.0)
    self.composer_opacity = clamp_opacity(values.get("composer_opacity"), 0.92)
    self.chat_font_family = str(
        values.get("chat_font_family") or "Microsoft YaHei UI"
    )
    self.chat_font_size = clamp_chat_font_size(values.get("chat_font_size"), 10)
    self.code_font_family = str(values.get("code_font_family") or "Consolas")
    self.code_font_size = clamp_chat_font_size(values.get("code_font_size"), 10)

    background_path = str(values.get("background_path") or "")
    if not background_path or not os.path.isfile(background_path):
        background_path = ""
    self._apply_chat_background(background_path or None)
    self._refresh_background_mode_actions()
    self.update_all_chat_colors()
    self.update_all_bubbles_opacity(self.bubble_opacity)
    self.update_all_code_opacity(self.code_opacity)
    self.update_all_text_font_widgets()
    self._apply_ui_theme()


def preview_chat_appearance(self, values):
    self._apply_chat_appearance_state(values)


def _save_chat_appearance_state(self, values):
    self._apply_chat_appearance_state(values)
    self.config.set_bubble_user_color(self.bubble_user_color)
    self.config.set_bubble_assistant_color(self.bubble_assistant_color)
    self.config.set_bubble_opacity(self.bubble_opacity)
    self.config.set_code_block_color(self.code_block_color)
    self.config.set_code_opacity(self.code_opacity)
    self.config.set_chat_background_mode(self.chat_background_mode)
    self.config.set_chat_background_opacity(self.background_opacity)
    self.config.set_composer_opacity(self.composer_opacity)
    self.config.set_chat_font(QFont(self.chat_font_family, self.chat_font_size))
    self.config.set_code_font(QFont(self.code_font_family, self.code_font_size))
    background_path = str(values.get("background_path") or "")
    if background_path and os.path.isfile(background_path):
        self.config.settings.setValue(
            "chat_bg_image", background_path.replace("\\", "/")
        )
    else:
        self.config.settings.remove("chat_bg_image")


def show_chat_appearance_dialog(self):
    dialog = ChatAppearanceDialog(self, self)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        self._save_chat_appearance_state(dialog.values)
        self.status_bar.showMessage("聊天外观已更新", 2000)
    else:
        self._apply_chat_appearance_state(dialog._original)


def _refresh_background_mode_actions(self):
    for mode, action in getattr(self, "background_mode_actions", {}).items():
        action.setChecked(mode == self.chat_background_mode)


def set_chat_background_mode(self, mode):
    if mode not in {"fill", "fit", "stretch", "tile", "center"}:
        return
    self.chat_background_mode = mode
    self.config.set_chat_background_mode(mode)
    self._refresh_background_mode_actions()
    self._apply_chat_background(self._resolve_saved_chat_background())


def choose_chat_background(self):
    file_path, _ = QFileDialog.getOpenFileName(
        self,
        "选择背景图片",
        "",
        "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)",
    )
    if not file_path:
        return ""

    pixmap = QPixmap(file_path)
    if pixmap.isNull():
        QMessageBox.warning(self, "图片无效", "无法加载所选图片，请换一张试试。")
        return ""

    crop_dialog = ImageCropDialog(
        pixmap,
        target_size=self.chat_background_panel.size(),
        parent=self,
    )
    if crop_dialog.exec() != QDialog.DialogCode.Accepted:
        return ""

    cropped_pixmap = crop_dialog.get_result_pixmap()
    if cropped_pixmap.isNull():
        QMessageBox.warning(self, "裁剪失败", "裁剪结果为空，请重新选择。")
        return ""

    output_dir = os.path.join(get_runtime_dir(), "generated", "chat_backgrounds")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"chat_bg_{uuid.uuid4().hex[:8]}.png")
    if not cropped_pixmap.save(output_path, "PNG"):
        QMessageBox.warning(self, "保存失败", "裁剪后的背景图保存失败。")
        return ""

    return output_path.replace("\\", "/")


def set_chat_background(self):
    output_path = self.choose_chat_background()
    if not output_path:
        return
    self.config.settings.setValue("chat_bg_image", output_path)
    self._apply_chat_background(output_path)


def reset_chat_background(self):
    self.config.settings.remove("chat_bg_image")
    self._apply_chat_background(None)


def _resolve_saved_chat_background(self):
    """读取已保存的背景图；原路径失效时用同名文件在生成目录里兜底。"""
    saved = str(self.config.settings.value("chat_bg_image", "") or "")
    if not saved:
        return None
    if os.path.isfile(saved):
        return saved
    fallback = os.path.join(
        get_runtime_dir(), "generated", "chat_backgrounds", os.path.basename(saved)
    )
    if os.path.isfile(fallback):
        fixed = fallback.replace("\\", "/")
        self.config.settings.setValue("chat_bg_image", fixed)
        return fixed
    return None


def _apply_chat_background(self, image_path=None):
    """绘制聊天区背景；消息列表和列表项保持透明。"""
    self.chat_background_panel.set_background_mode(self.chat_background_mode)
    self.chat_background_panel.set_background_opacity(
        getattr(self, "background_opacity", 1.0)
    )
    self.chat_background_panel.set_background_image(image_path)
    self.chat_list.setStyleSheet(
        "QListWidget { border: none; background: transparent; }"
        "QListWidget::item { border: none; padding: 0px; background: transparent; }"
        "QListWidget::item:hover { background: transparent; }"
    )
    self.chat_background_panel.update()
