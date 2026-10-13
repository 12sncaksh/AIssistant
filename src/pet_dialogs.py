"""Desktop-pet model, wear and emotion dialogs."""

from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox
from PyQt6.QtWidgets import QDialog
from PyQt6.QtWidgets import QDialogButtonBox
from PyQt6.QtWidgets import QHBoxLayout
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QListWidget
from PyQt6.QtWidgets import QListWidgetItem
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtWidgets import QSlider
from PyQt6.QtWidgets import QTextBrowser
from PyQt6.QtWidgets import QVBoxLayout
from action_control import ActionHandler
from app_paths import get_config_dir
from live2d_catalog import PET_EMOTION_EXPRESSIONS
from live2d_catalog import _live2d_model_folder
from live2d_catalog import _scan_exp3_files
from live2d_catalog import build_live2d_emotion_template
from live2d_catalog import get_live2d_all_expressions
from live2d_catalog import get_live2d_model_catalog
from live2d_catalog import get_live2d_model_expression_names
from live2d_catalog import get_live2d_param_catalog
from live2d_catalog import load_live2d_emotion_config
from live2d_catalog import merge_live2d_emotion_config
import logging
import os
import sys
import webbrowser



def _get_selected_live2d_model(self):
    catalog = get_live2d_model_catalog()
    configured_name = self.config.get_pet_model_name()
    selected = next((item for item in catalog if item["id"] == configured_name), None)
    if selected:
        return selected
    fallback = next((item for item in catalog if item["id"] == "Mao"), None)
    selected = fallback or (catalog[0] if catalog else {
        "id": "Mao",
        "label": "Mao",
        "file": "Mao.model3.json",
        "path": "Mao/Mao.model3.json",
    })
    if configured_name != selected["id"]:
        self.config.set_pet_model_name(selected["id"])
    return selected


def _refresh_pet_param_catalog(self):
    """把当前桌宠模型的参数表同步给工具层，供 list_live2d_params 查询。"""
    try:
        model = self._get_selected_live2d_model()
        ActionHandler.set_pet_param_catalog(get_live2d_param_catalog(model["id"]))
    except Exception:
        logging.debug("刷新 Live2D 参数表失败", exc_info=True)


def show_pet_model_dialog(self):
    catalog = get_live2d_model_catalog()
    if not catalog:
        QMessageBox.warning(self, "没有模型", "Resources 目录中没有可用的 Live2D 模型。")
        return
    current = self._get_selected_live2d_model()["id"]
    dialog = QDialog(self)
    dialog.setWindowTitle("选择桌宠模型")
    dialog.setMinimumWidth(360)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("选择内置 Live2D 模型"))
    combo = QComboBox()
    for item in catalog:
        combo.addItem(item["label"], item["id"])
    combo.setCurrentIndex(max(0, combo.findData(current)))
    layout.addWidget(combo)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        self.set_pet_model(combo.currentData())


def show_pet_wear_dialog(self):
    """独立窗口：勾选当前模型要持久穿戴的配件/表情（.exp3.json），可多选。"""
    model = self._get_selected_live2d_model()
    names = get_live2d_model_expression_names(model["id"])
    worn = set(self.config.get_pet_worn(model["id"]))
    dialog = QDialog(self)
    dialog.setWindowTitle(f"穿戴配件 - {model['label']}")
    dialog.resize(420, 460)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(
        "勾选要佩戴的配件/表情，立即生效并记住。\n"
        "同一部位的建议只选其一（例如眼镜类、耳饰类），避免美术件互相叠加。"
    ))
    listw = QListWidget()
    for name in names:
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if name in worn else Qt.CheckState.Unchecked
        )
        listw.addItem(item)
    if not names:
        empty = QListWidgetItem("（当前模型没有可穿戴的 .exp3.json）")
        empty.setFlags(Qt.ItemFlag.NoItemFlags)
        listw.addItem(empty)
    layout.addWidget(listw, 1)

    btn_layout = QHBoxLayout()
    clear_btn = QPushButton("全部脱下")
    close_btn = QPushButton("关闭")
    btn_layout.addWidget(clear_btn)
    btn_layout.addStretch(1)
    btn_layout.addWidget(close_btn)

    def apply_wear():
        selected = [
            listw.item(i).text()
            for i in range(listw.count())
            if listw.item(i).checkState() == Qt.CheckState.Checked
        ]
        selected = [n for n in selected if n in names]
        self.config.set_pet_worn(model["id"], selected)
        if self.pet_window:
            self.pet_window.set_pet_wear(selected)
        self.status_bar.showMessage(f"已更新穿戴：{len(selected)} 件", 2500)

    def on_item_changed(_item):
        apply_wear()

    def clear_all():
        listw.blockSignals(True)
        for i in range(listw.count()):
            listw.item(i).setCheckState(Qt.CheckState.Unchecked)
        listw.blockSignals(False)
        apply_wear()

    listw.itemChanged.connect(on_item_changed)
    clear_btn.clicked.connect(clear_all)
    close_btn.clicked.connect(dialog.accept)
    layout.addLayout(btn_layout)
    dialog.exec()


def show_pet_emotion_dialog(self):
    """独立窗口：情绪映射配置文件（生成模板 / 打开编辑 / 重载）。"""
    model = self._get_selected_live2d_model()
    path = os.path.join(get_config_dir(), "live2d_emotions.json")
    folder, _ = _live2d_model_folder(model["id"])
    available = set(_scan_exp3_files(folder).keys()) if folder else set()

    dialog = QDialog(self)
    dialog.setWindowTitle("动作映射配置")
    dialog.resize(600, 560)
    layout = QVBoxLayout(dialog)
    info = QTextBrowser()
    layout.addWidget(info, 1)

    def spec_label(spec):
        if not isinstance(spec, dict):
            return "未配置"
        names = spec.get("expressions") or []
        if names:
            hits = [n for n in names if str(n).casefold() in available]
            if hits:
                return "、".join(hits)
            return "文件名无效：%s（该模型没有此 exp3）" % "、".join(str(n) for n in names)
        if spec.get("params"):
            return "参数覆盖 %d 项" % len(spec["params"])
        return "未配置"

    def refresh():
        config = load_live2d_emotion_config(model["id"])
        rows = []
        matched = 0
        for emotion in PET_EMOTION_EXPRESSIONS:
            label = spec_label(config.get(emotion))
            if label not in ("未配置",) and not label.startswith("文件名无效"):
                matched += 1
            rows.append("  · %s：%s" % (emotion, label))
        all_expr = get_live2d_all_expressions(model["id"])
        if all_expr:
            rows.append("")
            rows.append("本模型可用 .exp3 文件（把文件名填到上面的 expressions 里）：")
            for name, entries in all_expr.items():
                detail = "、".join(
                    "%s=%s" % (e.get("id"), e.get("value")) for e in entries[:5]
                )
                rows.append("  · %s（%s）" % (name, detail))
        else:
            rows.append("")
            rows.append("本模型没有任何 .exp3 文件：只能靠 params 直接指定参数。")
            rows.append("  可用参数见「打开模型目录」里的 .cdi3.json，或运行")
            rows.append("  scripts/list_live2d_props.py。")
        lines = [
            "配置文件：%s" % path,
            "当前模型：%s" % model["label"],
            "已配置情绪：%d / %d（读取当前配置，含手改内容）" % (
                matched, len(PET_EMOTION_EXPRESSIONS)),
            "说明：expressions 填上面的 .exp3 文件名；params 可直接写参数绝对值。",
        ] + rows
        info.setPlainText("\n".join(lines))

    def open_path(target):
        try:
            if sys.platform.startswith("win"):
                os.startfile(target)
            else:
                webbrowser.open("file://" + target)
        except Exception as exc:
            QMessageBox.warning(dialog, "打开失败", str(exc))

    def generate():
        template = build_live2d_emotion_template(model["id"])
        try:
            added = merge_live2d_emotion_config(template, path)
        except OSError as exc:
            QMessageBox.warning(dialog, "生成失败", f"无法写入：{exc}")
            return
        refresh()
        QMessageBox.information(
            dialog, "已生成映射模板",
            "文件：%s\n已为模型「%s」生成/补全空模板（新增 %d 项），"
            "已有填写不会覆盖。\n点击「打开配置文件」填写，保存后点「重载生效」。"
            % (path, model["label"], added),
        )

    btn_layout = QHBoxLayout()
    gen_btn = QPushButton("生成/补全映射模板")
    open_btn = QPushButton("打开配置文件")
    reload_btn = QPushButton("重载生效")
    close_btn = QPushButton("关闭")
    btn_layout.addWidget(gen_btn)
    btn_layout.addWidget(open_btn)
    btn_layout.addWidget(reload_btn)
    btn_layout.addStretch(1)
    btn_layout.addWidget(close_btn)
    layout.addLayout(btn_layout)

    def reload_config():
        if self.pet_window:
            self.pet_window._install_pet_params()
        refresh()
        self.status_bar.showMessage("已重载动作映射配置", 2500)

    gen_btn.clicked.connect(generate)
    open_btn.clicked.connect(lambda: open_path(path))
    reload_btn.clicked.connect(reload_config)
    close_btn.clicked.connect(dialog.accept)
    refresh()
    dialog.exec()


def show_pet_scale_dialog(self):
    if not self.pet_window:
        QMessageBox.information(self, "桌宠模型大小", "桌宠窗口尚未准备好。")
        return

    original_scale = self.config.get_pet_model_scale()
    dialog = QDialog(self)
    dialog.setWindowTitle("桌宠模型大小")
    layout = QVBoxLayout(dialog)

    scale_value = QLabel()
    scale_slider = QSlider(Qt.Orientation.Horizontal)
    scale_slider.setRange(50, 140)
    scale_slider.setValue(round(original_scale * 100))
    scale_slider.setToolTip("调整桌宠 Live2D 模型大小")

    def preview(value):
        scale_value.setText(f"{value}%")
        self.pet_window.set_model_scale(value / 100, persist=False)

    scale_slider.valueChanged.connect(preview)
    preview(scale_slider.value())

    reset_btn = QPushButton("恢复默认")
    reset_btn.clicked.connect(lambda: scale_slider.setValue(100))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)

    layout.addWidget(QLabel("模型大小"))
    layout.addWidget(scale_slider)
    layout.addWidget(scale_value, 0, Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(reset_btn, 0, Qt.AlignmentFlag.AlignRight)
    layout.addWidget(buttons)

    if dialog.exec() == QDialog.DialogCode.Accepted:
        self.pet_window.set_model_scale(scale_slider.value() / 100, persist=True)
    else:
        self.pet_window.set_model_scale(original_scale, persist=False)
