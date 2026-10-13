"""Pet context menu, system tray icon and application lifecycle actions."""

from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QActionGroup
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QIcon
from PyQt6.QtGui import QLinearGradient
from PyQt6.QtGui import QPainter
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QMenu
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtWidgets import QSystemTrayIcon
from app_flags import DEBUG_MODE
from app_theme import get_app_icon
from app_theme import style_popup_menu
from dsh_acp_client import current_connection
from live2d_catalog import get_live2d_model_catalog
from permission_modes import PERMISSION_MODE_LABELS
import logging



def _add_pet_menu_entries(self, menu, include_window_toggles=True):
    """把「桌宠」菜单项加进任意菜单：右键桌宠与托盘共用同一批入口。"""
    model_menu = menu.addMenu("桌宠模型")
    catalog = get_live2d_model_catalog()
    current = self.chat_window._get_selected_live2d_model()["id"] if catalog else None
    model_group = QActionGroup(model_menu)
    model_group.setExclusive(True)
    for item in catalog:
        action = model_menu.addAction(item["label"])
        action.setCheckable(True)
        action.setChecked(item["id"] == current)
        action.setActionGroup(model_group)
        action.triggered.connect(
            lambda _checked=False, mid=item["id"]: self.chat_window.set_pet_model(mid)
        )
    if not catalog:
        empty = model_menu.addAction("（没有可用模型）")
        empty.setEnabled(False)
    model_menu.addSeparator()
    model_menu.addAction("模型大小...").triggered.connect(
        self.chat_window.show_pet_scale_dialog
    )
    model_menu.addAction("重新加载模型").triggered.connect(
        self.chat_window.reload_live2d_model
    )
    model_menu.addAction("打开模型目录").triggered.connect(
        self.chat_window.open_model_directory
    )
    menu.addAction("配件管理...").triggered.connect(
        self.chat_window.show_pet_wear_dialog
    )
    menu.addAction("动作映射配置...").triggered.connect(
        self.chat_window.show_pet_emotion_dialog
    )
    menu.addSeparator()
    if include_window_toggles:
        if self.chat_window.isVisible():
            menu.addAction("隐藏聊天窗口").triggered.connect(
                self.chat_window.hide_chat_window
            )
        else:
            menu.addAction("打开聊天窗口").triggered.connect(
                self.chat_window.show_chat_window
            )
        menu.addAction("隐藏桌宠").triggered.connect(self.hide)


def show_context_menu(self, global_pos):
    menu = QMenu(self)
    style_popup_menu(menu, getattr(self.chat_window, "ui_theme", "dark"))
    menu.addAction("打开聊天窗口").triggered.connect(self.chat_window.show_chat_window)
    menu.addSeparator()
    self._add_pet_menu_entries(menu, include_window_toggles=True)
    menu.addSeparator()
    menu.addAction("重启").triggered.connect(self.restart_application)
    menu.addAction("退出程序").triggered.connect(self.quit_application)
    menu.exec(global_pos)


def restart_application(self):
    logging.info("请求重启程序")
    self._restart_requested = True
    self.quit_application()


def quit_application(self):
    logging.info("退出程序")
    self._allow_close = True
    if self._tray is not None:
        self._tray.hide()
    self.chat_window.request_close()
    self.close()
    QApplication.instance().quit()


def _make_tray_icon(self):
    """优先使用应用图标；资源缺失时保留内置回退图标。"""
    app_icon = get_app_icon()
    if not app_icon.isNull():
        return app_icon

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gradient = QLinearGradient(4, 4, 60, 60)
    gradient.setColorAt(0.0, QColor("#FFB6C1"))
    gradient.setColorAt(1.0, QColor("#FF8FAB"))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, 60, 60)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Microsoft YaHei", 26, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "柚")
    painter.end()
    return QIcon(pixmap)


def _build_tray_menu(self):
    menu = QMenu(self)
    self._tray_pet_action = menu.addAction("显示桌宠")
    self._tray_chat_action = menu.addAction("打开聊天窗口")
    menu.addSeparator()
    self._add_pet_menu_entries(menu, include_window_toggles=False)
    menu.addSeparator()
    if DEBUG_MODE:
        emotion_menu = menu.addMenu("测试桌宠表情")
        for emotion, label in (
            ("happy", "开心 happy"),
            ("love", "心动 love"),
            ("sparkle", "星星眼 sparkle"),
            ("shy", "害羞 shy"),
            ("angry", "生气 angry"),
            ("sleepy", "犯困 sleepy"),
            ("neutral", "收回表情"),
        ):
            action = emotion_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, name=emotion: (
                    self.set_pet_activity("idle"),
                    self.set_pet_emotion(name, 1.0, 8000),
                )
            )
        param_action = menu.addAction("测试裸参数 ParamAngleZ")
        param_action.triggered.connect(
            lambda: self.set_pet_params({"ParamAngleZ": 25}, 5000)
        )
        permission_action = menu.addAction("查看 DSH 权限状态")
        permission_action.triggered.connect(self._show_dsh_permission_status)
        menu.addSeparator()
    restart_action = menu.addAction("重启")
    restart_action.triggered.connect(self.restart_application)
    quit_action = menu.addAction("退出程序")
    self._tray_pet_action.triggered.connect(self._toggle_pet_visibility)
    self._tray_chat_action.triggered.connect(self._toggle_chat_window)
    quit_action.triggered.connect(self.quit_application)
    menu.aboutToShow.connect(self._update_tray_menu_labels)
    return menu


def _show_dsh_permission_status(self):
    configured_mode = self.chat_window.config.get_permission_mode()
    connection = current_connection()

    def describe(mode):
        if not mode:
            return "未启动"
        return f"{PERMISSION_MODE_LABELS.get(mode, mode)}（{mode}）"

    if connection is None:
        connection_mode = "无 DSH 连接"
        process_mode = "未启动"
        process_state = "未创建连接"
    else:
        connection_mode = describe(connection.permission_mode)
        proc = connection._proc
        running = proc is not None and proc.poll() is None
        process_mode = describe(connection._process_permission_mode) if running else "未运行"
        process_state = "待重启" if connection._needs_restart else ("运行中" if running else "已停止")

    QMessageBox.information(
        self,
        "DSH 权限状态",
        f"本地配置：{describe(configured_mode)}\n"
        f"连接目标：{connection_mode}\n"
        f"子进程启动档位：{process_mode}\n"
        f"子进程状态：{process_state}",
    )


def _update_tray_menu_labels(self):
    tray_menu = self._tray.contextMenu() if self._tray is not None else None
    if tray_menu is not None:
        style_popup_menu(tray_menu, getattr(self.chat_window, "ui_theme", "dark"))
    if self._tray_pet_action is None or self._tray_chat_action is None:
        return
    self._tray_pet_action.setText("隐藏桌宠" if self.isVisible() else "显示桌宠")
    self._tray_chat_action.setText("隐藏聊天窗口" if self.chat_window.isVisible() else "打开聊天窗口")


def _setup_tray(self):
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return
    self._tray = QSystemTrayIcon(self._make_tray_icon(), self)
    self._tray.setToolTip("小柚桌面助手")
    self._tray.setContextMenu(self._build_tray_menu())
    self._tray.activated.connect(self._on_tray_activated)
    self._tray.messageClicked.connect(self._on_tray_message_clicked)
    self._tray.show()


def notify_permission_request(self, message):
    if self._tray is not None:
        self._tray.showMessage(
            "AI 等待操作授权",
            str(message),
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )


def _on_tray_message_clicked(self):
    self.chat_window.show_chat_window()
    self.chat_window.raise_()
    self.chat_window.activateWindow()
    self.chat_window.raise_permission_dialog()


def _on_tray_activated(self, reason):
    if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
        self._toggle_pet_visibility()


def _toggle_pet_visibility(self):
    if self.isVisible():
        self.hide()
    else:
        self.show()
        self.raise_()
        self.activateWindow()


def _toggle_chat_window(self):
    if self.chat_window.isVisible():
        self.chat_window.hide_chat_window()
    else:
        self.chat_window.show_chat_window()
