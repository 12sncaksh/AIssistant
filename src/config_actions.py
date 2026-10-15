"""Settings / prompt / voice / role dialogs."""

from __future__ import annotations
from PyQt6.QtWidgets import QDialog
from config_dialog import ConfigDialog
from dsh_acp_client import current_connection
from dsh_acp_client import reset_connection
from permission_modes import PERMISSION_MODE_BUSY_MESSAGE
from role_dialog import RoleManagerDialog
import logging



def show_config_dialog(self):
    before_mode = self.config.get_permission_mode()
    dialog = ConfigDialog(self.config, self, section="config")
    if dialog.exec() == QDialog.DialogCode.Accepted:
        # 对话框是直接写 ConfigManager 的：先回滚到旧档，再走统一入口，规则与快捷切换保持一致
        requested_mode = self.config.get_permission_mode()
        if requested_mode != before_mode:
            self.config.set_permission_mode(before_mode)
            if not self.set_permission_mode(requested_mode, announce=False):
                self.status_bar.showMessage(PERMISSION_MODE_BUSY_MESSAGE, 5000)
        self.refresh_action_handler_config()
        # 记忆检索配置可能变化：重建 embedding provider，新后端在后台生效。
        runtime = getattr(self, "_memory", None)
        if runtime is not None:
            try:
                runtime.reload_embeddings()
            except Exception:
                logging.warning("重载 embedding 配置失败", exc_info=True)
        self.runtime_system_prompt = self._get_effective_system_prompt()
        self.load_system_prompt()
        self._update_model_indicator()
        self._refresh_permission_mode_actions()
        self._update_permission_button()
        # 权限模式可能变化：推给共享的 DSH 连接（有会话在跑时不打断，下次提问生效）
        connection = current_connection()
        if connection is not None:
            try:
                connection.set_permission_mode(self.config.get_permission_mode())
                # 模型 / 推理档位同理：空闲时立刻补写到活着的 ACP 会话，
                # 有回合在跑就只记下来，等下一次提问复用会话时生效。
                changed = connection.set_model_selection(
                    self.config.get_dsh_provider(),
                    self.config.get_dsh_model(),
                    self.config.get_dsh_reasoning_effort(),
                )
                if changed and not self._is_turn_running():
                    connection.sync_model_selection()
            except Exception:
                logging.warning("同步 DSH 权限/模型设置失败", exc_info=True)
        # 路由覆盖（--patch overlay）是 dsh 的启动参数：没有会话在跑时重建连接，
        # 让新的 provider / 模型下次提问生效。
        if (
            self.config.get_dsh_bridge_enabled()
            and not self._is_turn_running()
            and not self.session_threads
        ):
            reset_connection()
        self._update_reasoning_button()
        self._update_mode_button()
        self.status_bar.showMessage("配置已更新", 2000)


def show_prompt_dialog(self):
    # 可编辑的是**用户自己写的那段**，不是整个 runtime_system_prompt ——
    # 角色卡和硬规则在界面里只读展示，改它们要去角色卡对话框或代码里。
    dialog = ConfigDialog(
        self.config,
        self,
        self.config.get_custom_system_prompt(),
        section="prompt",
        builtin_system_prompt_text=self._get_default_system_prompt(),
    )
    if dialog.exec() == QDialog.DialogCode.Accepted:
        self.runtime_system_prompt = self._get_effective_system_prompt()
        self.load_system_prompt()
        self.status_bar.showMessage("提示词已更新", 2000)


def show_voice_config_dialog(self):
    dialog = ConfigDialog(self.config, self, section="voice")
    if dialog.exec() == QDialog.DialogCode.Accepted:
        self.status_bar.showMessage("语音配置已更新", 2000)


def _rebuild_role_menu(self):
    self.role_menu.clear()
    for role_name in self.roles.keys():
        action = self.role_menu.addAction(role_name)
        action.triggered.connect(lambda checked, r=role_name: self.switch_role(r))
    self.role_menu.addSeparator()
    manage_action = self.role_menu.addAction("管理角色")
    manage_action.triggered.connect(self.show_role_manager_dialog)


def show_role_manager_dialog(self):
    dialog = RoleManagerDialog(self.config, self)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    self.roles = self.config.get_roles()
    if self.current_role_name not in self.roles:
        self.current_role_name = next(iter(self.roles), "")
    self.config.settings.setValue("last_role", self.current_role_name)
    self._rebuild_role_menu()
    self.status_bar.showMessage("角色卡已更新", 2000)


def switch_role(self, role_name):
    if role_name not in self.roles:
        return
    if role_name == self.current_role_name:
        return
    self.current_role_name = role_name

    self.runtime_system_prompt = self._get_effective_system_prompt()

    # 更新当前会话的 system 消息
    if self.conversation_history and self.conversation_history[0]["role"] == "system":
        if self.runtime_system_prompt.strip():
            self.conversation_history[0]["content"] = self.runtime_system_prompt
        else:
            self.conversation_history.pop(0)
    elif self.runtime_system_prompt.strip():
        self.conversation_history.insert(0, {"role": "system", "content": self.runtime_system_prompt})

    # 可选：在聊天区显示切换提示
    self.add_message(f"✨ 角色已切换为：{role_name}，请继续聊天~", is_user=False)

    self.status_bar.showMessage(f"已切换到角色：{role_name}", 3000)
    self.config.settings.setValue("last_role", role_name)
