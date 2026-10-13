"""DSH bridge settings: model/reasoning probing and persistence."""

from __future__ import annotations
from dsh_acp_client import MODEL_OPTION_CATEGORY
from dsh_acp_client import REASONING_OPTION_CATEGORY
from dsh_acp_client import config_option_choices
from dsh_acp_client import find_config_option
from dsh_acp_client import parse_model_selection_value
from dsh_options_probe import DshOptionsProbeThread
from reasoning_options import DSH_REASONING_CHOICES
from role_dialog import _track_probe_thread



def _fill_dsh_reasoning_combo(self, current=""):
    """填充 DSH 推理档位下拉框；current 不在预置档位里时原样保留（适配器可用自定义词汇）。"""
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
    self.config.set_cached_dsh_options(requested_provider, requested_model, options)
    self.dsh_probe_status.setText(self._apply_dsh_options(options))


def _restore_cached_dsh_options(self):
    """用上次探测的缓存回填下拉框；没有可用缓存时保持「只有当前值」的原状。

    命中缓存就不再自动探测，用户想刷新仍可点「探测」按钮。
    """
    # 用与 _probe_dsh_options 完全相同的取值方式算键，否则写缓存与读缓存
    # 会因 provider 一个来自下拉 data、一个来自输入框而对不上。
    provider, model = self._selected_dsh_model()
    cached = self.config.get_cached_dsh_options(provider, model)
    if not cached:
        return
    self._apply_dsh_options(cached)
    self.dsh_probe_status.setText(
        f"已从缓存载入 DSH 的 {len(self._dsh_model_values)} 个模型与推理档位"
        "（点「探测」可刷新）。"
    )


def _apply_dsh_options(self, options):
    """把一份 configOptions 渲染进模型 / 推理档位下拉框。

    探测结果与缓存读到的结构完全相同，所以两条路径共用这里；
    source_hint 只影响状态栏文案前缀（「已读取」/「缓存」）。
    """
    options = list(options or [])
    model_option = find_config_option(options, "", MODEL_OPTION_CATEGORY)
    reasoning_option = find_config_option(options, "", REASONING_OPTION_CATEGORY)
    if model_option is not None:
        choices = config_option_choices(model_option)
        current_provider, _ = parse_model_selection_value(model_option.get("currentValue"))
        typed_model = self.dsh_model_combo.currentText().strip()
        self.dsh_model_combo.blockSignals(True)
        self.dsh_model_combo.clear()
        self._dsh_model_values = {}
        for item in choices:
            _, item_model = parse_model_selection_value(item["value"])
            if not item_model:
                continue
            label = f"{item['group']} / {item['name']}" if item["group"] else item["name"]
            self.dsh_model_combo.addItem(label, item["value"])
            self._dsh_model_values[item_model] = item["value"]
        index = self.dsh_model_combo.findData(str(model_option.get("currentValue") or ""))
        if index < 0 and typed_model:
            index = self.dsh_model_combo.findData(self._dsh_model_values.get(typed_model, ""))
        self.dsh_model_combo.setCurrentIndex(max(0, index))
        self.dsh_model_combo.blockSignals(False)
        if current_provider:
            self.dsh_provider_edit.setText(current_provider)
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
            f"已读取 DSH：{len(self._dsh_model_values)} 个模型、"
            f"{len(config_option_choices(reasoning_option))} 档推理。选好后点「确定」保存。"
        )
    # 模型没声明 reasoningEfforts：DSH 不公布档位，此时也必须保住用户/已保存的
    # 选择，不能直接重置成「跟随默认」——否则用户手填的档位会被探测结果抹掉。
    self._fill_dsh_reasoning_combo(
        self._current_dsh_reasoning_choice()
        or str(self.config.get_dsh_reasoning_effort() or "").strip()
    )
    return (
        f"已读取 DSH：{len(self._dsh_model_values)} 个模型。当前模型没有声明 "
        "reasoningEfforts，DSH 不提供推理档位（可在 DSH 的 settings.yaml 里给该模型补上）。"
    )


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
        self.dsh_provider_edit,
        self.dsh_model_combo,
        self.dsh_reasoning_combo,
        self.dsh_probe_button,
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
    """保存 DSH 的 provider / 模型 / 推理档位。

    模型下拉的 data 是探测得来的 ACP ["provider","model"] JSON 串；手填时退回
    「Provider 输入框 + 下拉框文本」的组合。
    """
    model_value = self.dsh_model_combo.currentData()
    if model_value is None:
        model_value = self.dsh_model_combo.currentText().strip()
    provider, model = parse_model_selection_value(model_value)
    if not provider:
        provider = self.dsh_provider_edit.text().strip()
        model = self.dsh_model_combo.currentText().strip() or model
    self.config.set_dsh_provider(provider)
    self.config.set_dsh_model(model)
    # 推理档位同样要兼容手输：currentData() 在手输时不更新，直接用会丢掉用户填写
    self.config.set_dsh_reasoning_effort(self._current_dsh_reasoning_choice())
