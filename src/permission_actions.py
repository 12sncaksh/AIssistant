"""Permission mode buttons, quick switches and composer sizing."""

from __future__ import annotations
from PyQt6.QtGui import QActionGroup
from dsh_acp_client import current_connection
from permission_modes import DEFAULT_PERMISSION_MODE
from permission_modes import FULL_ACCESS_MODE
from permission_modes import PERMISSION_MODE_BUSY_MESSAGE
from permission_modes import PERMISSION_MODE_CHOICES
from permission_modes import PERMISSION_MODE_VALUES
from permission_modes import READ_ONLY_MODE
from reasoning_options import reasoning_effort_label
import logging



def _update_model_indicator(self):
    if not hasattr(self, "model_indicator"):
        return
    if self.config.get_dsh_bridge_enabled():
        acp_provider = self.config.get_acp_provider() or {}
        dsh_model = acp_provider.get("model") or "模型由 ACP 选择"
        provider_name = acp_provider.get("name") or "未配置 Provider"
        label = f"ACP · {provider_name}/{dsh_model}"
        self.model_indicator.setText(label if len(label) <= 30 else label[:27] + "…")
        self.model_indicator.setToolTip(
            "当前处于 DSH 桥接模式：消息交给本机 DSH 运行时（Agent + 工具）处理。\n"
            "本机模型的 Base URL / API Key / 模型设置在此模式下不生效。\n"
            "点击打开配置"
        )
        return
    model = self.config.get_model() or "未配置模型"
    label = model if len(model) <= 24 else model[:21] + "…"
    self.model_indicator.setText(label)
    self.model_indicator.setToolTip(f"当前模型：{model}\n点击打开配置")


def _update_permission_button(self):
    """输入框旁的权限模式快捷按钮：常驻显示当前预设，并随档位变色。"""
    if not hasattr(self, "permission_button"):
        return
    mode = self.config.get_permission_mode()
    if mode == FULL_ACCESS_MODE:
        self.permission_button.setText("⚠ 完全权限")
        self.permission_button.setStyleSheet("color: #ff7b7b; font-weight: bold;")
        tip = (
            "完全权限模式（danger-full-access）：所有工具调用都不再弹确认框，"
            "沙箱不再限制工作区外的读写范围；破坏性命令黑名单与受保护写入路径仍然拦截。"
        )
    elif mode == READ_ONLY_MODE:
        self.permission_button.setText("权限：只读")
        self.permission_button.setStyleSheet("color: #7fb2ff;")
        tip = "只读模式（read-only）：写文件、执行命令、关机重启和键鼠操作会被直接拒绝。"
    else:
        self.permission_button.setText("权限：默认")
        self.permission_button.setStyleSheet("")
        tip = "工作区可写（默认）：执行命令、写文件、关机重启等高风险操作会逐次弹窗确认。"
    self.permission_button.setToolTip(tip + "\n点击选择其它权限模式。")


def _resize_composer_input(self):
    if not hasattr(self, "input_text"):
        return
    line_height = max(20, self.input_text.fontMetrics().lineSpacing())
    document = self.input_text.document()
    document.documentLayout().documentSize()
    visual_lines = 0
    block = document.begin()
    while block.isValid():
        layout = block.layout()
        visual_lines += max(1, layout.lineCount() if layout else 1)
        block = block.next()
    visual_lines = max(1, visual_lines)
    visible_lines = max(2, min(5, visual_lines or 1))
    target_input_height = line_height * visible_lines + 16
    attachment_height = 26 if self.attach_label.isVisible() else 0
    target_composer_height = target_input_height + 42 + 14 + attachment_height
    if visible_lines == self._composer_line_count and self.composer.height() == target_composer_height:
        return
    self._composer_line_count = visible_lines
    self.input_text.setFixedHeight(target_input_height)
    self.composer.setFixedHeight(target_composer_height)
    self.composer.updateGeometry()
    parent_layout = self.composer.parentWidget().layout()
    if parent_layout:
        parent_layout.invalidate()
        parent_layout.activate()
    logging.info(
        "输入框布局更新：visual_lines=%s，input_height=%s，composer_height=%s",
        visible_lines,
        target_input_height,
        target_composer_height,
    )


def _update_mode_button(self):
    roleplay = self.config.get_roleplay_mode()
    self.mode_button.setText("自由角色扮演" if roleplay else "默认模式")
    self.default_mode_action.setChecked(not roleplay)
    self.roleplay_mode_action.setChecked(roleplay)


def set_permission_mode(self, mode, announce=True):
    """切换权限模式；任务运行期间禁止切换任何档位。"""
    mode = str(mode or "").strip().lower()
    if mode not in PERMISSION_MODE_VALUES:
        mode = DEFAULT_PERMISSION_MODE
    current = self.config.get_permission_mode()
    if self._permission_change_blocked(mode):
        logging.info("任务进行中，拒绝切换到 %s（当前 %s）", mode, current)
        if announce:
            self.status_bar.showMessage(PERMISSION_MODE_BUSY_MESSAGE, 5000)
        return False
    changed = mode != current
    self.config.set_permission_mode(mode)
    self.refresh_action_handler_config()
    # DSH 子进程靠环境变量 DSH_PERMISSION_MODE 决定沙箱与审批策略，改模式后标记重启，下一次提问生效
    connection = current_connection()
    if connection is not None:
        try:
            connection.set_permission_mode(mode)
        except Exception:
            logging.warning("切换 DSH 权限模式失败", exc_info=True)
    if changed:
        self.runtime_system_prompt = self._get_effective_system_prompt()
        if self.conversation_history and self.conversation_history[0].get("role") == "system":
            if self.runtime_system_prompt.strip():
                self.conversation_history[0]["content"] = self.runtime_system_prompt
            else:
                self.conversation_history.pop(0)
        elif self.runtime_system_prompt.strip():
            self.conversation_history.insert(
                0,
                {"role": "system", "content": self.runtime_system_prompt},
            )
    self._refresh_permission_mode_actions()
    self._update_permission_button()
    if not announce:
        return True
    if mode == FULL_ACCESS_MODE:
        self.status_bar.showMessage(
            "⚠ 已开启完全权限模式：所有工具调用不再弹确认框，请谨慎下达指令", 8000
        )
    elif mode == READ_ONLY_MODE:
        self.status_bar.showMessage("已切换到只读模式：写入与命令会被沙箱拒绝", 4000)
    else:
        self.status_bar.showMessage("已切换到工作区可写模式（默认）", 3000)
    return True


def _is_turn_running(self):
    """是否有回合正在跑：本地 APICallThread 与桥接 DSHSessionThread 都登记在 session_threads 里。"""
    for thread in list(self.session_threads.values()):
        try:
            if thread is not None and thread.isRunning():
                return True
        except RuntimeError:  # 底层 C++ 对象已释放
            continue
    return False


def _permission_change_blocked(self, mode):
    return mode != self.config.get_permission_mode() and self._is_turn_running()


def _sync_permission_action_enabled(self):
    """任务运行时仅保留当前档位，避免切换导致运行中的 DSH 进程退出。"""
    for attr in ("permission_mode_actions", "permission_quick_actions"):
        actions = getattr(self, attr, None)
        if not actions:
            continue
        for value, action in actions.items():
            action.setEnabled(not self._permission_change_blocked(value))


def _build_permission_actions(self, menu):
    """按权限预设表给一个菜单建一组互斥动作，返回 ({value: QAction}, QActionGroup)。"""
    group = QActionGroup(self)
    group.setExclusive(True)
    actions = {}
    for label, value, description in PERMISSION_MODE_CHOICES:
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setActionGroup(group)
        action.setToolTip(description)
        action.triggered.connect(
            lambda checked=False, mode=value: self.set_permission_mode(mode)
        )
        actions[value] = action
    return actions, group


def _refresh_permission_mode_actions(self):
    """输入框快捷菜单的勾选状态跟随当前权限配置。"""
    current = self.config.get_permission_mode()
    for attr in ("permission_mode_actions", "permission_quick_actions"):
        actions = getattr(self, attr, None)
        if not actions:
            continue
        for value, action in actions.items():
            action.setChecked(value == current)


def _set_quick_mode(self, roleplay):
    self.config.set_roleplay_mode(bool(roleplay))
    self.runtime_system_prompt = self._get_effective_system_prompt()
    if self.conversation_history and self.conversation_history[0].get("role") == "system":
        if self.runtime_system_prompt.strip():
            self.conversation_history[0]["content"] = self.runtime_system_prompt
        else:
            self.conversation_history.pop(0)
    elif self.runtime_system_prompt.strip():
        self.conversation_history.insert(
            0,
            {"role": "system", "content": self.runtime_system_prompt},
        )
    self._update_mode_button()
    self.status_bar.showMessage(
        "已切换到自由角色扮演模式" if roleplay else "已切换到默认模式",
        2000,
    )


def _set_quick_reasoning(self, value):
    """菜单里选中某个推理档位：写配置、同步菜单勾选与按钮文字。"""
    value = str(value or "")
    self.config.set_reasoning_effort(value)
    self._update_reasoning_button()
    self.status_bar.showMessage(f"推理强度：{reasoning_effort_label(value)}", 1800)


def _update_reasoning_button(self):
    """推理档位按钮：常驻显示当前档位，并同步菜单里的勾选项。"""
    if not hasattr(self, "reasoning_button"):
        return
    current = self.config.get_reasoning_effort()
    for value, action in getattr(self, "reasoning_actions", {}).items():
        action.setChecked(value == current)
    label = reasoning_effort_label(current)
    self.reasoning_button.setText(f"推理：{label}")
