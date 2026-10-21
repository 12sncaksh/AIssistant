"""DSH bridge settings: model/reasoning probing and persistence."""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidget
from PyQt6.QtWidgets import QListWidgetItem
from PyQt6.QtWidgets import QComboBox
from PyQt6.QtWidgets import QDialog
from PyQt6.QtWidgets import QDialogButtonBox
from PyQt6.QtWidgets import QFormLayout
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QLineEdit
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtWidgets import QVBoxLayout
from dsh_acp_client import MODEL_OPTION_CATEGORY
from dsh_acp_client import REASONING_OPTION_CATEGORY
from dsh_acp_client import config_option_choices
from dsh_acp_client import find_config_option
from dsh_acp_client import parse_model_selection_value
from dsh_options_probe import DshOptionsProbeThread
from dsh_options_probe import ProviderModelsProbeThread
from reasoning_options import DSH_REASONING_CHOICES
from role_dialog import _track_probe_thread
import dsh_preflight
from acp_provider_config import read_web_provider_config
from acp_provider_config import provider_presets
from dsh_model_catalog import provider_catalog_models
from dsh_model_catalog import provider_reasoning_map


class AcpProviderDialog(QDialog):
    """新增 ACP Provider：连接信息只在窗口内填写，成功探测后才保存。"""

    def __init__(self, config, parent=None, provider=None):
        super().__init__(parent)
        self.config = config
        self._probe_thread = None
        self._models = []
        provider = provider or {}
        self._current_model = str(provider.get("model") or "").strip()
        self._presets = provider_presets()
        self._provider_kind = str(provider.get("provider_kind") or "custom").strip().lower()
        self._preset_id = str(provider.get("preset_id") or "").strip()
        self._official_probe = False
        self._reasoning_by_model = dict(provider.get("reasoning_by_model") or {})
        self.setWindowTitle("新增 ACP Provider")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("官方 Provider 预设", "official")
        self.kind_combo.addItem("自定义 Provider", "custom")
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(self._provider_kind)))
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        form.addRow("类型:", self.kind_combo)
        self.preset_combo = QComboBox()
        for preset_id, preset in self._presets.items():
            self.preset_combo.addItem(preset["label"], preset_id)
        self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findData(self._preset_id)))
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.preset_label = QLabel("官方预设:")
        form.addRow(self.preset_label, self.preset_combo)
        self.name_edit = QLineEdit(str(provider.get("name") or ""))
        form.addRow("供应商:", self.name_edit)
        self.url_edit = QLineEdit(str(provider.get("base_url") or ""))
        self.url_edit.setPlaceholderText("https://api.example.com/v1")
        form.addRow("API URL:", self.url_edit)
        self.key_edit = QLineEdit(str(provider.get("api_key") or ""))
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("API Key")
        form.addRow("API Key:", self.key_edit)
        self.model_list = QListWidget()
        self.model_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.model_list.setMinimumHeight(150)
        self.model_list.setEnabled(False)
        form.addRow("加载模型:", self.model_list)
        layout.addLayout(form)
        self.probe_button = QPushButton("探测模型")
        self.probe_button.clicked.connect(self._probe)
        layout.addWidget(self.probe_button)
        self.status = QLabel("填写供应商、API URL 和 API Key 后，点击「探测模型」。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setEnabled(False)
        self._on_kind_changed()
        if self._provider_kind == "official" and self._preset_id:
            self._on_preset_changed()
        layout.addWidget(buttons)

    def _on_kind_changed(self):
        official = self.kind_combo.currentData() == "official"
        self.preset_combo.setEnabled(official)
        self.preset_combo.setVisible(official)
        self.preset_label.setVisible(official)
        self.name_edit.setReadOnly(official)
        self.url_edit.setReadOnly(official)
        if official:
            self._on_preset_changed()
        else:
            self.name_edit.setReadOnly(False)
            self.url_edit.setReadOnly(False)

    def _on_preset_changed(self):
        preset_id = str(self.preset_combo.currentData() or "").strip()
        preset = self._presets.get(preset_id)
        if not preset or self.kind_combo.currentData() != "official":
            return
        self._preset_id = preset_id
        self.name_edit.setText(preset["provider"])
        self.url_edit.setText(preset["base_url"])
        if not self.key_edit.text().strip() and preset.get("api_key_env"):
            self.key_edit.setPlaceholderText(f"API Key（或环境变量 {preset['api_key_env']}）")


    def _probe(self):
        name = self.name_edit.text().strip()
        base_url = self.url_edit.text().strip().rstrip("/")
        api_key = self.key_edit.text().strip()
        if not name or not base_url:
            self.status.setText("探测失败：供应商名称和 API URL 不能为空。")
            return
        if self._probe_thread is not None and self._probe_thread.isRunning():
            return
        is_official = self.kind_combo.currentData() == "official"
        provider_info = {
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "provider_kind": "official" if is_official else "custom",
            "preset_id": str(self.preset_combo.currentData() or "") if is_official else "",
            "api": str((self._presets.get(str(self.preset_combo.currentData() or "")) or {}).get("api") or "openai-responses"),
            "api_key_env": str((self._presets.get(str(self.preset_combo.currentData() or "")) or {}).get("api_key_env") or "") if is_official else "",
            "model": "",
            "models": [],
        }
        self.probe_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.model_list.setEnabled(False)
        self.status.setText("正在探测模型列表，请稍候……")
        if is_official:
            self._official_probe = True
            catalog = provider_catalog_models(provider_info.get("preset_id") or name)
            if catalog:
                # DSH 自带目录是权威数据：模型 ID 与推理档位都从这里来，
                # 不必再启动一次 ACP（那只会拿到「已配置 Provider」的目录）。
                self._reasoning_by_model = provider_reasoning_map(
                    provider_info.get("preset_id") or name
                )
                self.probe_button.setEnabled(True)
                self._on_models_probe(catalog, "")
                return
            # 目录里没有这个 Provider（DSH 版本较旧或未内置）：退回端点探测，
            # 仍比临时起 ACP 可靠——ACP 只会公布「已配置 Provider」的模型。
            thread = ProviderModelsProbeThread(provider_info)
            thread.finished_ok.connect(self._on_models_probe)
        else:
            self._official_probe = False
            thread = ProviderModelsProbeThread(provider_info)
            thread.finished_ok.connect(self._on_models_probe)
        thread.finished.connect(self._on_probe_finished)
        _track_probe_thread(thread)
        thread.finished.connect(thread.deleteLater)
        self._probe_thread = thread
        thread.start()

    def _on_probe_finished(self):
        if self._probe_thread is self.sender():
            self._probe_thread = None
        self.probe_button.setEnabled(True)

    def _on_models_probe(self, models, error):
        if error:
            self.status.setText(f"探测失败：{error}")
            return
        models = [
            str(item.get("id") or item.get("name") or "").strip()
            for item in (models or [])
            if isinstance(item, dict) and str(item.get("id") or item.get("name") or "").strip()
        ]
        models = list(dict.fromkeys(models))
        if not models:
            self.status.setText("探测完成，但没有返回可加载的模型。")
            return
        self._models = models
        self.model_list.clear()
        for model in models:
            item = QListWidgetItem(model, self.model_list)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
        self.model_list.setEnabled(True)
        self.save_button.setEnabled(True)
        self.status.setText(f"探测成功：找到 {len(models)} 个模型，请勾选要加载的模型后点击「保存」。")

    def _save(self):
        models = [
            self.model_list.item(index).text().strip()
            for index in range(self.model_list.count())
            if self.model_list.item(index).checkState() == Qt.CheckState.Checked
        ]
        if not models:
            self.status.setText("请至少勾选一个要加载的模型。")
            return
        model = self._current_model if self._current_model in models else models[0]
        self.result = {
            "name": self.name_edit.text().strip(),
            "base_url": self.url_edit.text().strip().rstrip("/"),
            "api_key": self.key_edit.text().strip(),
            "provider_kind": "official" if self.kind_combo.currentData() == "official" else "custom",
            "preset_id": str(self.preset_combo.currentData() or "") if self.kind_combo.currentData() == "official" else "",
            "api": str((self._presets.get(str(self.preset_combo.currentData() or "")) or {}).get("api") or "openai-responses"),
            "api_key_env": str((self._presets.get(str(self.preset_combo.currentData() or "")) or {}).get("api_key_env") or "") if self.kind_combo.currentData() == "official" else "",
            "model": model,
            "models": models,
            "reasoning_by_model": dict(self._reasoning_by_model),
        }
        self.accept()


def _provider_form_data(self):
    """读取当前编辑器内容，不触碰 Web 配置。"""
    provider = self.dsh_provider_edit.text().strip()
    model_value = self.dsh_model_combo.currentData()
    parsed_provider, parsed_model = parse_model_selection_value(model_value)
    model = str(parsed_model or model_value or self.dsh_model_combo.currentText().strip()).strip()
    models = []
    for index in range(self.dsh_model_combo.count()):
        raw_value = self.dsh_model_combo.itemData(index) or self.dsh_model_combo.itemText(index)
        _, value = parse_model_selection_value(raw_value)
        value = str(value or raw_value or "").strip()
        if value and value not in models:
            models.append(value)
    if model and model not in models:
        models.append(model)
    return {
        "name": provider or parsed_provider,
        "base_url": self.dsh_url_edit.text().strip(),
        "api_key": self.dsh_api_key_edit.text().strip(),
        "api_key_env": next(
            (
                str(item.get("api_key_env") or "").strip()
                for item in self.config.get_acp_providers()
                if item.get("name") == (provider or parsed_provider)
            ),
            "",
        ),
        "model": model,
        "models": models,
    }


def _refresh_acp_provider_combo(self, selected_name=""):
    """刷新本地 Provider 列表；刷新后只回填本地数据。"""
    selected_name = str(selected_name or "").strip()
    providers = self.config.get_acp_providers()
    self.dsh_saved_provider_combo.blockSignals(True)
    self.dsh_saved_provider_combo.clear()
    for provider in providers:
        self.dsh_saved_provider_combo.addItem(provider.get("name", ""), provider.get("name", ""))
    index = self.dsh_saved_provider_combo.findData(selected_name)
    if index < 0 and providers:
        index = 0
    self.dsh_saved_provider_combo.setCurrentIndex(index)
    self.dsh_saved_provider_combo.blockSignals(False)
    self._select_acp_provider(index)


def _new_acp_provider(self):
    dialog = AcpProviderDialog(self.config, self)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    provider = getattr(dialog, "result", None)
    if not provider:
        return
    self.config.set_acp_provider(provider)
    self._refresh_acp_provider_combo(provider["name"])
    self._schedule_dsh_options_probe()
    self.dsh_probe_status.setText(
        f"已保存 Provider「{provider['name']}」及 {len(provider.get('models') or [])} 个模型。"
    )


def _copy_acp_provider(self):
    provider = self._provider_form_data()
    if not provider.get("name"):
        self.dsh_probe_status.setText("当前没有可复制的 Provider。")
        return
    existing = {item.get("name") for item in self.config.get_acp_providers()}
    base = provider["name"] + " 副本"
    name = base
    number = 2
    while name in existing:
        name = f"{base} {number}"
        number += 1
    provider["name"] = name
    self.dsh_provider_edit.setText(name)
    self.dsh_probe_status.setText(f"已复制为「{name}」，修改后点「保存 Provider」。")


def _edit_acp_provider(self):
    name = str(self.dsh_saved_provider_combo.currentData() or "").strip()
    provider = next(
        (item for item in self.config.get_acp_providers() if item.get("name") == name),
        None,
    )
    if not provider:
        self.dsh_probe_status.setText("当前没有可编辑的 Provider。")
        return
    dialog = AcpProviderDialog(self.config, self, provider=provider)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    result = getattr(dialog, "result", None)
    if not result:
        return
    result["api_key_env"] = provider.get("api_key_env", "")
    self.config.set_acp_provider(result)
    self._refresh_acp_provider_combo(result["name"])
    self._schedule_dsh_options_probe()
    self.dsh_probe_status.setText(
        f"已更新 Provider「{result['name']}」及 {len(result.get('models') or [])} 个模型。"
    )

def _delete_acp_provider(self):
    name = str(self.dsh_saved_provider_combo.currentData() or self.dsh_provider_edit.text()).strip()
    if not name:
        self.dsh_probe_status.setText("当前没有可删除的 Provider。")
        return
    providers = self.config.remove_acp_provider(name)
    selected = providers[0]["name"] if providers else ""
    self._refresh_acp_provider_combo(selected)
    if not providers:
        self.dsh_provider_edit.clear()
        self.dsh_url_edit.clear()
        self.dsh_api_key_edit.clear()
        self.dsh_model_combo.clear()
        self.dsh_model_combo.setCurrentIndex(-1)
    self.dsh_probe_status.setText(f"已删除本地 ACP Provider「{name}」。")


def _save_acp_provider(self, silent=False):
    provider = self._provider_form_data()
    if not provider["name"] or not provider["base_url"]:
        if not silent:
            self.dsh_probe_status.setText("保存失败：Provider 名称和 API URL 不能为空。")
        return False
    self.config.set_acp_provider(provider)
    self._refresh_acp_provider_combo(provider["name"])
    if not silent:
        self.dsh_probe_status.setText(f"已保存本地 ACP Provider「{provider['name']}」，并设为当前。")
    return True


def _import_web_acp_providers(self):
    """一次性把 Web Provider 加入本地列表，不改写当前编辑器。"""
    try:
        imported = read_web_provider_config()
    except Exception as exc:
        self.dsh_probe_status.setText(f"读取 DSH Web 配置失败：{exc}")
        return
    if not imported:
        self.dsh_probe_status.setText("未在 DSH Web 本地配置中找到可导入的 Provider。")
        return
    existing = self.config.get_acp_providers()
    names = {item.get("name") for item in existing}
    added = []
    skipped = []
    for provider in imported:
        name = provider.get("name", "")
        if not name or name in names:
            skipped.append(name or "(无名称)")
            continue
        existing.append(provider)
        names.add(name)
        added.append(name)
    current = self.dsh_saved_provider_combo.currentData() or self.dsh_provider_edit.text().strip()
    self.config.save_acp_providers(existing)
    if not current and added:
        current = added[0]
        self.config.set_acp_selected_provider(current)
    self._refresh_acp_provider_combo(current)
    detail = f"已导入 {len(added)} 个 Provider"
    missing_keys = [
        provider.get("name", "")
        for provider in imported
        if provider.get("name") in added and not provider.get("api_key")
    ]
    if missing_keys:
        detail += f"；{len(missing_keys)} 个 Provider 未读取到 API Key，请在上方 API Key 栏补填"
    if skipped:
        detail += f"；跳过已存在的 {len(skipped)} 个"
    self.dsh_probe_status.setText(detail + "。Web 配置已复制到本地 ACP 列表，之后两边独立。")


def _select_acp_provider(self, index):
    name = str(self.dsh_saved_provider_combo.itemData(index) or "").strip()
    if not name:
        return
    provider = next((item for item in self.config.get_acp_providers() if item.get("name") == name), None)
    if not provider:
        return
    self.dsh_provider_edit.setText(provider.get("name", ""))
    self.dsh_url_edit.setText(provider.get("base_url", ""))
    self.dsh_api_key_edit.setText(provider.get("api_key", ""))
    self.dsh_model_combo.blockSignals(True)
    self.dsh_model_combo.clear()
    current_model = provider.get("model", "")
    for model in provider.get("models") or []:
        self.dsh_model_combo.addItem(model, model)
    if current_model and self.dsh_model_combo.findData(current_model) < 0:
        self.dsh_model_combo.addItem(current_model, current_model)
    self.dsh_model_combo.setCurrentIndex(max(0, self.dsh_model_combo.findData(current_model)))
    self.dsh_model_combo.blockSignals(False)
    self.config.set_acp_selected_provider(name)
    levels = (provider.get("reasoning_by_model") or {}).get(current_model) or []
    if levels and hasattr(self, "dsh_reasoning_combo"):
        self._fill_reasoning_levels(
            levels,
            self._current_dsh_reasoning_choice()
            or str(self.config.get_dsh_reasoning_effort() or "").strip(),
        )


def _fill_reasoning_levels(self, levels, current=""):
    """按 DSH 目录声明的档位填充推理下拉框，保留用户已有选择。"""
    current = str(current or "").strip()
    labels = dict(DSH_REASONING_CHOICES)
    self.dsh_reasoning_combo.blockSignals(True)
    self.dsh_reasoning_combo.clear()
    self.dsh_reasoning_combo.addItem("跟随 DSH / 模型默认", "")
    for level in levels:
        level = str(level).strip()
        if not level:
            continue
        self.dsh_reasoning_combo.addItem(labels.get(level, level), level)
    index = self.dsh_reasoning_combo.findData(current)
    self.dsh_reasoning_combo.setCurrentIndex(max(0, index))
    self.dsh_reasoning_combo.blockSignals(False)


def _fill_dsh_reasoning_combo(self, current=""):
    """填充 DSH 推理档位下拉框；current 不在预置档位里时原样保留。"""
    current = str(current or "").strip()
    self.dsh_reasoning_combo.clear()
    self.dsh_reasoning_combo.addItem("跟随 DSH / 模型默认", "")
    for label, value in DSH_REASONING_CHOICES:
        self.dsh_reasoning_combo.addItem(label, value)
    index = self.dsh_reasoning_combo.findData(current)
    if index < 0 and current:
        self.dsh_reasoning_combo.addItem(current, current)
        index = self.dsh_reasoning_combo.count() - 1
    self.dsh_reasoning_combo.setCurrentIndex(max(0, index))


def _current_dsh_reasoning_choice(self):
    """取推理下拉框当前真正代表的值。

    下拉是 setEditable(True)：用户手输后 currentData() 仍返回旧条目 data，
    只有 currentText() 是新文本，所以两者不一致时以文本为准。
    """
    combo = getattr(self, "dsh_reasoning_combo", None)
    if combo is None:
        return ""
    text = combo.currentText().strip()
    data = combo.currentData()
    if text:
        index = combo.findText(text)
        if index >= 0:
            item_value = combo.itemData(index)
            if item_value is not None:
                return str(item_value).strip()
        # 手输值：不在候选项里，直接把文本当档位值（与 _fill_dsh_reasoning_combo 一致）
        return text
    return str(data or "").strip()


def _sync_dsh_provider_from_model(self, index):
    """从下拉框选中的模型反填 provider，保证两栏一致。"""
    value = self.dsh_model_combo.itemData(index)
    if value:
        provider, _ = parse_model_selection_value(value)
        if provider:
            self.dsh_provider_edit.setText(provider)


def _selected_dsh_model(self):
    value = self.dsh_model_combo.currentData()
    if value:
        provider, model = parse_model_selection_value(value)
        if model:
            return provider or self.dsh_provider_edit.text().strip(), model
    return self.dsh_provider_edit.text().strip(), self.dsh_model_combo.currentText().strip()


def _schedule_dsh_options_probe(self, *_args):
    if not self.dsh_bridge_checkbox.isChecked():
        return
    if self._dsh_probe_thread is not None and self._dsh_probe_thread.isRunning():
        self._dsh_probe_pending = True
        return
    self._dsh_probe_timer.start(450)


def _probe_dsh_options(self):
    """后台探测 DSH 公布的模型 / 推理档位（临时起一个 ACP 连接，不发任何消息）。"""
    if not self.dsh_bridge_checkbox.isChecked():
        self.dsh_probe_status.setText("请先勾选「启用 DSH 桥接」，模型列表由 DSH 运行时提供。")
        return
    if self._dsh_probe_thread is not None and self._dsh_probe_thread.isRunning():
        self._dsh_probe_pending = True
        return
    provider, model = self._selected_dsh_model()
    stored = next(
        (item for item in self.config.get_acp_providers() if item.get("name") == provider),
        None,
    )
    levels = ((stored or {}).get("reasoning_by_model") or {}).get(model) or []
    if levels:
        # DSH 目录已经声明了该模型的档位：直接用，不再起 ACP，
        # 既不会卡住，也不会把已加载的模型列表冲掉。
        self._dsh_probe_pending = False
        self._fill_reasoning_levels(
            levels,
            self._current_dsh_reasoning_choice()
            or str(self.config.get_dsh_reasoning_effort() or "").strip(),
        )
        self.dsh_probe_status.setText(
            f"已从 DSH 目录读取当前模型的 {len(levels)} 档推理："
            + "、".join(levels)
            + "。选好后点「确定」保存。"
        )
        return
    if provider and self.dsh_url_edit.text().strip():
        self.config.set_acp_provider({
            "name": provider,
            "base_url": self.dsh_url_edit.text().strip(),
            "api_key": self.dsh_api_key_edit.text().strip(),
            "model": model,
            "models": [
                str(self.dsh_model_combo.itemData(index) or self.dsh_model_combo.itemText(index)).strip()
                for index in range(self.dsh_model_combo.count())
                if str(self.dsh_model_combo.itemData(index) or self.dsh_model_combo.itemText(index)).strip()
            ],
        })
    self._dsh_probe_pending = False
    self.dsh_probe_button.setEnabled(False)
    self.dsh_probe_status.setText(
        "正在连接本机 DSH 读取可用模型 / 推理档位（首次启动可能要十几秒）..."
    )
    # 不传父对象：见 _track_probe_thread 的说明，避免对话框关闭时连坐销毁运行中的线程。
    thread = DshOptionsProbeThread(self.config, provider=provider, model=model)
    thread.finished_ok.connect(self._on_dsh_options_probed)
    thread.finished.connect(self._on_dsh_probe_thread_finished)
    _track_probe_thread(thread)          # 先挂“释放保活引用”，保证它早于 deleteLater 执行
    thread.finished.connect(thread.deleteLater)
    self._dsh_probe_thread = thread
    thread.start()


def _on_dsh_probe_thread_finished(self):
    """线程真正结束后再清引用：这样连点「探测」也不会丢掉仍在运行的那一个。"""
    thread = self.sender()
    if self._dsh_probe_thread is thread:
        self._dsh_probe_thread = None
    if self._dsh_probe_pending and self.dsh_bridge_checkbox.isChecked():
        self._dsh_probe_pending = False
        self._dsh_probe_timer.start(0)


def _on_dsh_options_probed(self, options, error):
    """探测结果回填：模型下拉按 provider 分组，推理档位用 DSH 公布的原始档位名。"""
    self.dsh_probe_button.setEnabled(self.dsh_bridge_checkbox.isChecked())
    # 回填会 clear()/addItem() 整个下拉；若用户此时正开着下拉弹窗，先收起，
    # 避免在弹窗视图存活期间重建模型项。
    self.dsh_model_combo.hidePopup()
    requested_provider, requested_model = self._dsh_probe_thread.provider, self._dsh_probe_thread.model
    if (requested_provider, requested_model) != self._selected_dsh_model():
        self._dsh_probe_pending = True
        return
    if error:
        self.dsh_probe_status.setText(f"探测失败：{error}")
        return
    options = list(options or [])
    # 探测成功：把这一份 configOptions 存下来，下次打开对话框直接渲染，
    # 不必再起一次 dsh --profile acp（首次十几秒）。
    base_url = self.dsh_url_edit.text().strip()
    self.config.set_cached_dsh_options(requested_provider, requested_model, options, base_url)
    self.dsh_probe_status.setText(self._apply_dsh_reasoning(options))


def _restore_cached_dsh_options(self):
    """用上次探测的缓存回填下拉框；没有可用缓存时保持「只有当前值」的原状。

    命中缓存就不再自动探测，用户想刷新仍可点「探测」按钮。
    """
    # 用与 _probe_dsh_options 完全相同的取值方式算键，否则写缓存与读缓存
    # 会因 provider 一个来自下拉 data、一个来自输入框而对不上。
    provider, model = self._selected_dsh_model()
    cached = self.config.get_cached_dsh_options(provider, model, self.dsh_url_edit.text().strip())
    if not cached:
        return
    self._apply_dsh_reasoning(cached)
    self.dsh_probe_status.setText("已从缓存载入当前模型的 DSH 推理档位（点探测可刷新）。")


def _apply_dsh_reasoning(self, options):
    """Apply only the selected model's reasoning options; never touch the model list."""
    options = list(options or [])
    reasoning_option = find_config_option(options, "", REASONING_OPTION_CATEGORY)
    if reasoning_option is not None:
        # 重填下拉会把用户已经选好的档位冲掉，所以先把「当前选择」记下来。
        # 优先级：用户本次显式选择 > 已保存的配置 > DSH 公布的 currentValue。
        # currentValue 只代表 DSH 会话此刻的实际值（常常还是 off），
        # 用它覆盖用户选择正是「选了高挡位、确定后却又变回 off」的原因。
        keep_value = self._current_dsh_reasoning_choice()
        self.dsh_reasoning_combo.clear()
        self.dsh_reasoning_combo.addItem("跟随 DSH / 模型默认", "")
        for item in config_option_choices(reasoning_option):
            label = item["name"]
            if item["description"]:
                label = f"{item['name']}（{item['description']}）"
            self.dsh_reasoning_combo.addItem(label, item["value"])
        candidates = (
            keep_value,
            str(self.config.get_dsh_reasoning_effort() or "").strip(),
            str(reasoning_option.get("currentValue") or "").strip(),
        )
        index = -1
        for candidate in candidates:
            if not candidate:
                continue
            index = self.dsh_reasoning_combo.findData(candidate)
            if index >= 0:
                break
        self.dsh_reasoning_combo.setCurrentIndex(max(0, index))
        return (
            f"已读取当前模型的 {len(config_option_choices(reasoning_option))} 档推理。"
            "选好后点「确定」保存。"
        )
    # 模型没声明 reasoningEfforts：DSH 不公布档位，此时也必须保住用户/已保存的
    # 选择，不能直接重置成「跟随默认」——否则用户手填的档位会被探测结果抹掉。
    self._fill_dsh_reasoning_combo(
        self._current_dsh_reasoning_choice()
        or str(self.config.get_dsh_reasoning_effort() or "").strip()
    )
    return (
        "当前模型没有声明 reasoningEfforts，DSH 不提供可读取的推理档位；"
        "已保留当前选择。请在 DSH Provider 配置中为该模型声明 reasoningEfforts。"
    )


def _run_dsh_preflight(self):
    """跑五项环境自检，把结果原样贴到设置页（可选中复制）。

    只读检查：不写文件、不联网、不动任何配置。自检自己出问题也只影响这一块
    提示，不能把整个设置窗带崩。
    """
    try:
        checks = dsh_preflight.run_checks()
    except Exception as exc:  # noqa: BLE001 - 自检失败只该影响这块提示
        self.dsh_preflight_status.setText(f"自检没能跑完：{exc}")
        self.dsh_preflight_status.setVisible(True)
        return
    marks = {
        dsh_preflight.OK: "[ OK ]",
        dsh_preflight.WARN: "[WARN]",
        dsh_preflight.FAIL: "[FAIL]",
    }
    lines = []
    for item in checks:
        lines.append(f"{marks.get(item.level, '[????]')} {item.title}")
        if item.fix:
            lines.append(f"         $ {item.fix}")
    blocking = dsh_preflight.blocking_checks(checks)
    lines.append("")
    if blocking:
        lines.append(f"结论：有 {len(blocking)} 项挡住桥接。按上面的命令处理完，点按钮重跑一次。")
    else:
        lines.append("结论：可以开 DSH 桥接。")
    self.dsh_preflight_status.setText("\n".join(lines))
    self.dsh_preflight_status.setVisible(True)


def _auto_run_dsh_preflight(self, enabled):
    """勾上桥接时顺手自检一次；已经显示过就不再打扰。"""
    if enabled and not self.dsh_preflight_status.isVisible():
        self._run_dsh_preflight()


def _apply_dsh_bridge_state(self, enabled):
    """桥接开启时，本地模型相关字段不再参与，直接置灰并标注，避免看不出区别。"""
    disabled_style = (
        "color: #6f747a; background-color: #23262a;"
        " border: 1px solid #34383d; border-radius: 4px; padding: 2px 6px;"
    )
    for widget in (
        self.api_key_edit,
        self.base_url_edit,
        self.model_edit,
        self.vision_model_edit,
        self.reasoning_combo,
    ):
        widget.setEnabled(not enabled)
        widget.setStyleSheet(disabled_style if enabled else "")
    # DSH 专属项反过来：桥接开着才有意义，关闭时禁用并置暗（与本地模型栏对称）
    for widget in (
        self.dsh_saved_provider_combo,
        self.dsh_provider_edit,
        self.dsh_url_edit,
        self.dsh_api_key_edit,
        self.dsh_model_combo,
        self.dsh_reasoning_combo,
        self.dsh_probe_button,
        self.dsh_new_provider_button,
        self.dsh_edit_provider_button,
        self.dsh_delete_provider_button,
        self.dsh_import_web_button,
    ):
        widget.setEnabled(enabled)
        widget.setStyleSheet("" if enabled else disabled_style)
    self.dsh_probe_status.setEnabled(True)
    self.dsh_bridge_checkbox.setText(
        "启用 DSH 桥接（交给本机 DSH 运行时）"
        if not enabled
        else "启用 DSH 桥接（已开启：由本机 DSH 运行时接管）"
    )
    self.dsh_bridge_hint.setVisible(enabled)


def _save_dsh_selection(self):
    """只保存当前 ACP Provider / 模型选择，不从隐藏编辑框重建 Provider。"""
    name = str(self.dsh_saved_provider_combo.currentData() or "").strip()
    providers = self.config.get_acp_providers()
    if not name:
        self.config.set_acp_selected_provider("")
    else:
        model_value = self.dsh_model_combo.currentData()
        _, model = parse_model_selection_value(model_value)
        model = str(model or model_value or self.dsh_model_combo.currentText()).strip()
        for provider in providers:
            if provider.get("name") == name:
                if model:
                    provider["model"] = model
                self.config.save_acp_providers(providers)
                self.config.set_acp_selected_provider(name)
                break
    self.config.set_dsh_reasoning_effort(self._current_dsh_reasoning_choice())
