"""Image-crop, font-picker, chat-appearance and avatar settings dialogs."""

from __future__ import annotations

import os

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRubberBand,
    QSlider,
    QSpinBox,
    QVBoxLayout,
)

from app_paths import get_default_avatar_path
from avatar_tools import AVATAR_OUTPUT_SIDE, load_avatar_source_pixmap, save_avatar_pixmap
from chat_markdown import DEFAULT_CODE_BLOCK_COLOR
from chat_style import clamp_chat_font_size, make_circular_avatar, normalize_hex_color

# 裁剪出的背景图长边上限：保留原图分辨率，只在超过这里时等比缩小（3840 够 4K 屏用）
CHAT_BG_MAX_SIDE = 3840


def limit_pixmap_side(pixmap, max_side):
    """只降不升：长边超过上限就等比缩下来，否则原样返回。"""
    if pixmap.isNull() or max_side <= 0:
        return pixmap
    if max(pixmap.width(), pixmap.height()) <= max_side:
        return pixmap
    return pixmap.scaled(
        max_side,
        max_side,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class CropImageLabel(QLabel):
    def __init__(self, pixmap, aspect_ratio=None, parent=None):
        super().__init__(parent)
        self.original_pixmap = pixmap
        self.aspect_ratio = aspect_ratio
        self.display_rect = QRect()
        self.selection_rect = QRect()
        self.origin = QPoint()
        self.selecting = False
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMouseTracking(True)
        self.setStyleSheet(
            "background-color: %s; border: 1px solid %s;"
            % (
                self.palette().color(QPalette.ColorRole.Window).name(),
                self.palette().color(QPalette.ColorRole.Mid).name(),
            )
        )
        self.setMinimumSize(720, 460)
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self):
        if self.original_pixmap.isNull():
            return
        target_size = self.contentsRect().size()
        scaled = self.original_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        self.display_rect = QRect(x, y, scaled.width(), scaled.height())
        if self.selection_rect:
            self.rubber_band.setGeometry(self.selection_rect)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.display_rect.contains(event.position().toPoint()):
            super().mousePressEvent(event)
            return
        self.origin = self._clamp_to_display_rect(event.position().toPoint())
        self.selecting = True
        self.selection_rect = QRect(self.origin, QSize())
        self.rubber_band.setGeometry(self.selection_rect)
        self.rubber_band.show()
        event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self.selecting:
            current = self._clamp_to_display_rect(event.position().toPoint())
            self.selection_rect = self._build_selection_rect(current)
            self.rubber_band.setGeometry(self.selection_rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selecting = False
            if self.selection_rect.width() < 8 or self.selection_rect.height() < 8:
                self.selection_rect = QRect()
                self.rubber_band.hide()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _clamp_to_display_rect(self, point):
        return QPoint(
            max(self.display_rect.left(), min(point.x(), self.display_rect.right())),
            max(self.display_rect.top(), min(point.y(), self.display_rect.bottom())),
        )

    def _build_selection_rect(self, current):
        raw_rect = QRect(self.origin, current).normalized()
        if not self.aspect_ratio or raw_rect.width() == 0 or raw_rect.height() == 0:
            return raw_rect

        direction_x = 1 if current.x() >= self.origin.x() else -1
        direction_y = 1 if current.y() >= self.origin.y() else -1

        width = max(1, abs(current.x() - self.origin.x()))
        height = max(1, abs(current.y() - self.origin.y()))

        if width / height >= self.aspect_ratio:
            width = int(height * self.aspect_ratio)
        else:
            height = int(width / self.aspect_ratio)

        right = self.origin.x() + direction_x * width
        bottom = self.origin.y() + direction_y * height
        adjusted = QRect(self.origin, QPoint(right, bottom)).normalized()
        return adjusted.intersected(self.display_rect)

    def _selection_to_source_rect(self, rect):
        scale_x = self.original_pixmap.width() / max(1, self.display_rect.width())
        scale_y = self.original_pixmap.height() / max(1, self.display_rect.height())
        return QRect(
            int((rect.left() - self.display_rect.left()) * scale_x),
            int((rect.top() - self.display_rect.top()) * scale_y),
            max(1, int(rect.width() * scale_x)),
            max(1, int(rect.height() * scale_y)),
        ).intersected(self.original_pixmap.rect())

    def _center_fit_source_rect(self):
        if not self.aspect_ratio or self.original_pixmap.isNull():
            return self.original_pixmap.rect()

        src_w = self.original_pixmap.width()
        src_h = self.original_pixmap.height()
        src_ratio = src_w / max(1, src_h)

        if src_ratio > self.aspect_ratio:
            target_w = int(src_h * self.aspect_ratio)
            x = (src_w - target_w) // 2
            return QRect(x, 0, target_w, src_h)
        target_h = int(src_w / self.aspect_ratio)
        y = (src_h - target_h) // 2
        return QRect(0, y, src_w, target_h)

    def get_cropped_pixmap(self, use_full_image=False):
        if self.original_pixmap.isNull():
            return self.original_pixmap

        if use_full_image or self.selection_rect.isNull():
            crop_rect = self._center_fit_source_rect()
        else:
            crop_rect = self._selection_to_source_rect(self.selection_rect)

        result = self.original_pixmap.copy(crop_rect) if crop_rect.isValid() else self.original_pixmap
        # 不再压到面板像素尺寸：保留裁剪出来的原生分辨率，只有超过上限才等比缩小
        return limit_pixmap_side(result, CHAT_BG_MAX_SIDE)


class ImageCropDialog(QDialog):
    def __init__(
        self,
        pixmap,
        target_size=None,
        parent=None,
        title="裁剪聊天背景",
        target_label="背景板",
        minimum_size=(860, 620),
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(*minimum_size)
        self._result_pixmap = pixmap
        self.target_size = target_size or QSize()
        self.aspect_ratio = None
        if self.target_size.width() > 0 and self.target_size.height() > 0:
            self.aspect_ratio = self.target_size.width() / self.target_size.height()

        layout = QVBoxLayout(self)
        helper_text = "拖拽框选要显示的区域，不框选则使用整张图片。"
        if self.aspect_ratio:
            helper_text += (
                f" 当前裁剪框会锁定为{target_label}比例 "
                f"{self.target_size.width()}:{self.target_size.height()}。"
            )
        layout.addWidget(QLabel(helper_text))

        self.crop_label = CropImageLabel(pixmap, self.aspect_ratio, self)
        layout.addWidget(self.crop_label, 1)

        button_row = QHBoxLayout()
        self.use_original_btn = QPushButton("使用整张图")
        self.apply_crop_btn = QPushButton("裁剪并应用")
        self.cancel_btn = QPushButton("取消")
        button_row.addWidget(self.use_original_btn)
        button_row.addStretch(1)
        button_row.addWidget(self.apply_crop_btn)
        button_row.addWidget(self.cancel_btn)
        layout.addLayout(button_row)

        self.use_original_btn.clicked.connect(self.use_original)
        self.apply_crop_btn.clicked.connect(self.apply_crop)
        self.cancel_btn.clicked.connect(self.reject)

    def use_original(self):
        self._result_pixmap = self.crop_label.get_cropped_pixmap(use_full_image=True)
        self.accept()

    def apply_crop(self):
        self._result_pixmap = self.crop_label.get_cropped_pixmap()
        self.accept()

    def get_result_pixmap(self):
        return self._result_pixmap


class SimpleFontDialog(QDialog):
    """Font family and size picker without unsupported style/effect options."""

    def __init__(self, current_font, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.family_combo = QFontComboBox()
        self.family_combo.setCurrentFont(current_font)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 32)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setValue(clamp_chat_font_size(current_font.pointSize(), 10))
        form.addRow("\u5b57\u4f53", self.family_combo)
        form.addRow("\u5b57\u53f7", self.size_spin)
        root.addLayout(form)

        self.preview = QLabel("AaBbYyZz\n\u4f60\u597d\uff0cAI \u52a9\u624b")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(110)
        self.preview.setStyleSheet(
            "border:1px solid #cbd5e1; border-radius:6px; "
            "background:#f8fafc; color:#111827;"
        )
        root.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.family_combo.currentFontChanged.connect(self._update_preview)
        self.size_spin.valueChanged.connect(self._update_preview)
        self._update_preview()

    def _update_preview(self, _value=None):
        self.preview.setFont(self.selected_font())

    def selected_font(self):
        font = self.family_combo.currentFont()
        font.setPointSize(self.size_spin.value())
        return font


class ChatAppearanceDialog(QDialog):
    """统一预览并保存聊天背景、气泡、代码块和字体外观。"""

    BACKGROUND_MODES = (
        ("fill", "填充（保持比例，可能裁剪）"),
        ("fit", "适应（保持比例，可能留边）"),
        ("stretch", "拉伸（完整显示）"),
        ("tile", "平铺"),
        ("center", "居中"),
    )

    def __init__(self, owner, parent=None):
        super().__init__(parent or owner)
        self.owner = owner
        self._original = owner._read_chat_appearance_state()
        self.values = dict(self._original)
        self.setWindowTitle("聊天外观")
        self.setMinimumWidth(640)

        root = QVBoxLayout(self)
        tip = QLabel("修改会实时预览；点击“确定”后保存，取消则恢复原来的外观。")
        tip.setStyleSheet("color:#64748b;")
        root.addWidget(tip)

        root.addWidget(self._build_background_group())
        root.addWidget(self._build_bubble_group())
        root.addWidget(self._build_code_group())
        root.addWidget(self._build_text_group())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_background_status()
        for key in (
            "bubble_user_color",
            "bubble_assistant_color",
            "code_block_color",
        ):
            self._refresh_color_button(key)
        self._refresh_font_labels()

    def _build_background_group(self):
        group = QGroupBox("背景")
        form = QFormLayout(group)
        self.background_status = QLabel()
        self.background_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("当前背景", self.background_status)

        background_buttons = QHBoxLayout()
        choose_button = QPushButton("选择背景...")
        reset_button = QPushButton("恢复默认背景")
        choose_button.clicked.connect(self._choose_background)
        reset_button.clicked.connect(self._reset_background)
        background_buttons.addWidget(choose_button)
        background_buttons.addWidget(reset_button)
        background_buttons.addStretch(1)
        form.addRow("", background_buttons)

        self.background_mode_combo = QComboBox()
        for mode, label in self.BACKGROUND_MODES:
            self.background_mode_combo.addItem(label, mode)
        index = self.background_mode_combo.findData(self.values["background_mode"])
        self.background_mode_combo.setCurrentIndex(max(0, index))
        self.background_mode_combo.currentIndexChanged.connect(
            self._on_background_mode_changed
        )
        form.addRow("显示模式", self.background_mode_combo)

        self.background_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.background_opacity_slider.setRange(0, 100)
        self.background_opacity_slider.setValue(
            round(self.values["background_opacity"] * 100)
        )
        self.background_opacity_slider.setToolTip("调整聊天区背景图的不透明度")
        self.background_opacity_label = QLabel(
            f'{self.values["background_opacity"]:.2f}'
        )
        background_opacity_row = QHBoxLayout()
        background_opacity_row.addWidget(self.background_opacity_slider, 1)
        background_opacity_row.addWidget(self.background_opacity_label)
        self.background_opacity_slider.valueChanged.connect(
            self._on_background_opacity_changed
        )
        form.addRow("背景透明度", background_opacity_row)
        return group

    def _build_bubble_group(self):
        group = QGroupBox("气泡")
        form = QFormLayout(group)
        self.color_buttons = {}
        self.color_labels = {}
        form.addRow("我的气泡颜色", self._build_color_row("bubble_user_color"))
        form.addRow(
            "AI 气泡颜色",
            self._build_color_row("bubble_assistant_color"),
        )
        self.bubble_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.bubble_opacity_slider.setRange(0, 100)
        self.bubble_opacity_slider.setValue(round(self.values["bubble_opacity"] * 100))
        self.bubble_opacity_label = QLabel(
            f'{self.values["bubble_opacity"]:.2f}'
        )
        bubble_opacity_row = QHBoxLayout()
        bubble_opacity_row.addWidget(self.bubble_opacity_slider, 1)
        bubble_opacity_row.addWidget(self.bubble_opacity_label)
        self.bubble_opacity_slider.valueChanged.connect(
            self._on_bubble_opacity_changed
        )
        form.addRow("气泡透明度", bubble_opacity_row)
        return group

    def _build_code_group(self):
        group = QGroupBox("代码块")
        form = QFormLayout(group)
        form.addRow("代码块颜色", self._build_color_row("code_block_color"))

        self.code_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.code_opacity_slider.setRange(0, 100)
        self.code_opacity_slider.setValue(round(self.values["code_opacity"] * 100))
        self.code_opacity_label = QLabel(f'{self.values["code_opacity"]:.2f}')
        code_opacity_row = QHBoxLayout()
        code_opacity_row.addWidget(self.code_opacity_slider, 1)
        code_opacity_row.addWidget(self.code_opacity_label)
        self.code_opacity_slider.valueChanged.connect(
            self._on_code_opacity_changed
        )
        form.addRow("代码块透明度", code_opacity_row)

        self.code_font_label = QLabel()
        code_font_button = QPushButton("选择字体...")
        code_font_button.clicked.connect(
            lambda: self._choose_font("code", "选择代码块字体")
        )
        code_font_row = QHBoxLayout()
        code_font_row.addWidget(self.code_font_label, 1)
        code_font_row.addWidget(code_font_button)
        form.addRow("代码字体", code_font_row)
        return group

    def _build_text_group(self):
        group = QGroupBox("正文")
        form = QFormLayout(group)
        self.chat_font_label = QLabel()
        chat_font_button = QPushButton("选择字体...")
        chat_font_button.clicked.connect(
            lambda: self._choose_font("chat", "选择聊天正文字体")
        )
        chat_font_row = QHBoxLayout()
        chat_font_row.addWidget(self.chat_font_label, 1)
        chat_font_row.addWidget(chat_font_button)
        form.addRow("聊天正文字体", chat_font_row)
        return group

    def _build_color_row(self, key):
        row = QHBoxLayout()
        button = QPushButton()
        button.setFixedSize(54, 26)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(
            lambda checked=False, color_key=key: self._choose_color(color_key)
        )
        value_label = QLabel()
        value_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row.addWidget(button)
        row.addWidget(value_label)
        row.addStretch(1)
        self.color_buttons[key] = button
        self.color_labels[key] = value_label
        return row

    def _refresh_color_button(self, key):
        color = normalize_hex_color(self.values[key], DEFAULT_CODE_BLOCK_COLOR)
        self.values[key] = color
        self.color_buttons[key].setStyleSheet(
            "QPushButton {"
            f"background-color:{color};"
            "border:1px solid #64748b; border-radius:5px;"
            "}"
        )
        self.color_buttons[key].setToolTip(f"点击选择颜色，当前 {color}")
        self.color_labels[key].setText(color)

    def _refresh_background_status(self):
        path = str(self.values.get("background_path") or "")
        if path and os.path.isfile(path):
            self.background_status.setText(os.path.basename(path))
            self.background_status.setToolTip(path)
        else:
            self.values["background_path"] = ""
            self.background_status.setText("默认背景")
            self.background_status.setToolTip("")

    def _refresh_font_labels(self):
        self.chat_font_label.setText(
            f'{self.values["chat_font_family"]}  {self.values["chat_font_size"]} pt'
        )
        self.code_font_label.setText(
            f'{self.values["code_font_family"]}  {self.values["code_font_size"]} pt'
        )

    def _choose_color(self, key):
        title = {
            "bubble_user_color": "选择我的气泡颜色",
            "bubble_assistant_color": "选择 AI 气泡颜色",
            "code_block_color": "选择代码块颜色",
        }.get(key, "选择颜色")
        selected = QColorDialog.getColor(QColor(self.values[key]), self, title)
        if not selected.isValid():
            return
        self.values[key] = selected.name().upper()
        self._refresh_color_button(key)
        self._preview()

    def _choose_background(self):
        path = self.owner.choose_chat_background()
        if not path:
            return
        self.values["background_path"] = path
        self._refresh_background_status()
        self._preview()

    def _reset_background(self):
        self.values["background_path"] = ""
        self._refresh_background_status()
        self._preview()

    def _on_background_mode_changed(self, _index):
        self.values["background_mode"] = self.background_mode_combo.currentData()
        self._preview()

    def _on_background_opacity_changed(self, value):
        self.values["background_opacity"] = value / 100.0
        self.background_opacity_label.setText(
            f'{self.values["background_opacity"]:.2f}'
        )
        self._preview()

    def _on_bubble_opacity_changed(self, value):
        self.values["bubble_opacity"] = value / 100.0
        self.bubble_opacity_label.setText(f'{self.values["bubble_opacity"]:.2f}')
        self._preview()

    def _on_code_opacity_changed(self, value):
        self.values["code_opacity"] = value / 100.0
        self.code_opacity_label.setText(f'{self.values["code_opacity"]:.2f}')
        self._preview()

    def _choose_font(self, kind, title):
        if kind == "code":
            current_font = QFont(
                self.values["code_font_family"],
                self.values["code_font_size"],
            )
        else:
            current_font = QFont(
                self.values["chat_font_family"],
                self.values["chat_font_size"],
            )
        dialog = SimpleFontDialog(current_font, title, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        font = dialog.selected_font()
        prefix = "code" if kind == "code" else "chat"
        self.values[f"{prefix}_font_family"] = font.family()
        self.values[f"{prefix}_font_size"] = clamp_chat_font_size(
            font.pointSize(),
            10,
        )
        self._refresh_font_labels()
        self._preview()

    def _preview(self):
        self.owner.preview_chat_appearance(self.values)


class AvatarSettingsDialog(QDialog):
    """集中设置用户和 AI 助手的全局聊天头像。"""
    AVATAR_KINDS = (
        ("user", "我的头像"),
        ("assistant", "AI 助手头像"),
    )

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._pending_pixmaps = {}
        self._reset_kinds = set()
        self.avatar_labels = {}
        self.avatar_status_labels = {}
        self.setWindowTitle("头像与身份")
        self.setMinimumWidth(620)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("用户头像和 AI 助手头像均为全局设置，保存后立即生效。"))

        avatar_row = QHBoxLayout()
        for avatar_kind, title in self.AVATAR_KINDS:
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            preview = QLabel()
            preview.setFixedSize(144, 144)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setStyleSheet(
                "background:#edf2f7; border:1px solid #cbd5e1; border-radius:72px;"
            )
            group_layout.addWidget(preview, 0, Qt.AlignmentFlag.AlignHCenter)
            status = QLabel()
            status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status.setStyleSheet("color:#64748b;")
            group_layout.addWidget(status)

            button_row = QHBoxLayout()
            choose_button = QPushButton("更换图片")
            reset_button = QPushButton("恢复默认")
            choose_button.clicked.connect(
                lambda checked=False, kind=avatar_kind: self._choose_avatar(kind)
            )
            reset_button.clicked.connect(
                lambda checked=False, kind=avatar_kind: self._reset_avatar(kind)
            )
            button_row.addWidget(choose_button)
            button_row.addWidget(reset_button)
            group_layout.addLayout(button_row)
            avatar_row.addWidget(group)
            self.avatar_labels[avatar_kind] = preview
            self.avatar_status_labels[avatar_kind] = status
        root.addLayout(avatar_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh_previews()

    def _choose_avatar(self, avatar_kind):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择头像图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if not file_path:
            return
        pixmap = load_avatar_source_pixmap(file_path)
        if pixmap.isNull():
            QMessageBox.warning(
                self,
                "图片无效",
                "无法加载所选图片，或文件超过 20MB，请换一张试试。",
            )
            return

        title = "裁剪用户头像" if avatar_kind == "user" else "裁剪 AI 助手头像"
        crop_dialog = ImageCropDialog(
            pixmap,
            target_size=QSize(AVATAR_OUTPUT_SIDE, AVATAR_OUTPUT_SIDE),
            parent=self,
            title=title,
            target_label="头像",
            minimum_size=(720, 560),
        )
        if crop_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        cropped = crop_dialog.get_result_pixmap()
        if cropped.isNull():
            QMessageBox.warning(self, "裁剪失败", "裁剪结果为空，请重新选择。")
            return
        self._pending_pixmaps[avatar_kind] = cropped
        self._reset_kinds.discard(avatar_kind)
        self._refresh_previews()

    def _reset_avatar(self, avatar_kind):
        self._pending_pixmaps.pop(avatar_kind, None)
        self._reset_kinds.add(avatar_kind)
        self._refresh_previews()

    def _preview_pixmap(self, avatar_kind):
        if avatar_kind in self._pending_pixmaps:
            return self._pending_pixmaps[avatar_kind]
        if avatar_kind in self._reset_kinds:
            path = get_default_avatar_path(avatar_kind)
        else:
            path = self.config.get_avatar_path(avatar_kind)
        return QPixmap(path)

    def _refresh_previews(self):
        for avatar_kind, title in self.AVATAR_KINDS:
            pixmap = self._preview_pixmap(avatar_kind)
            circular = make_circular_avatar(pixmap, 128)
            label = self.avatar_labels[avatar_kind]
            if circular.isNull():
                label.clear()
                label.setText("无法预览")
            else:
                label.setPixmap(circular)

            if avatar_kind in self._pending_pixmaps:
                status = "新头像将在保存后生效"
            elif avatar_kind in self._reset_kinds:
                status = "将恢复为默认头像"
            elif self.config.get_custom_avatar_path(avatar_kind):
                status = "当前使用自定义头像"
            else:
                status = "当前使用默认头像"
            self.avatar_status_labels[avatar_kind].setText(status)

    def accept(self):
        for avatar_kind, _title in self.AVATAR_KINDS:
            try:
                pending = self._pending_pixmaps.get(avatar_kind)
                if pending is not None:
                    saved_path = save_avatar_pixmap(pending, avatar_kind)
                    self.config.set_avatar_path(avatar_kind, saved_path)
                elif avatar_kind in self._reset_kinds:
                    self.config.clear_avatar_path(avatar_kind)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "保存失败", f"无法保存头像：{error}")
                return
        super().accept()
