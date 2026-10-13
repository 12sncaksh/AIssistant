"""Role manager dialog and probe-thread tracking."""

from __future__ import annotations
from PyQt6.QtWidgets import QDialog
from PyQt6.QtWidgets import QDialogButtonBox
from PyQt6.QtWidgets import QFormLayout
from PyQt6.QtWidgets import QHBoxLayout
from PyQt6.QtWidgets import QLineEdit
from PyQt6.QtWidgets import QListWidget
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtWidgets import QTextEdit
from PyQt6.QtWidgets import QVBoxLayout
from PyQt6.QtWidgets import QWidget
import copy
import thread_registry



# ======================== 角色管理 ========================
class RoleManagerDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.roles = copy.deepcopy(config.get_roles())
        self._active_name = ""
        self.setWindowTitle("角色管理")
        self.resize(760, 520)

        root = QHBoxLayout(self)
        left_layout = QVBoxLayout()
        self.role_list = QListWidget()
        self.role_list.currentRowChanged.connect(self._on_role_selected)
        left_layout.addWidget(self.role_list, 1)

        role_buttons = QHBoxLayout()
        self.add_role_button = QPushButton("新增")
        self.remove_role_button = QPushButton("删除")
        self.help_role_button = QPushButton("说明")
        role_buttons.addWidget(self.add_role_button)
        role_buttons.addWidget(self.remove_role_button)
        role_buttons.addWidget(self.help_role_button)
        left_layout.addLayout(role_buttons)
        root.addLayout(left_layout, 1)

        editor = QWidget()
        editor_layout = QFormLayout(editor)
        self.role_name_edit = QLineEdit()
        self.role_name_edit.setPlaceholderText("角色名称")
        editor_layout.addRow("名称:", self.role_name_edit)
        self.role_persona_edit = QTextEdit()
        self.role_persona_edit.setMinimumHeight(320)
        self.role_persona_edit.setPlaceholderText("角色性格、背景、说话方式和关系设定")
        editor_layout.addRow("角色卡:", self.role_persona_edit)
        root.addWidget(editor, 2)

        self.add_role_button.clicked.connect(self._add_role)
        self.remove_role_button.clicked.connect(self._remove_role)
        self.help_role_button.clicked.connect(self._show_help)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        editor_layout.addRow(buttons)
        self._refresh_role_list()

    def _show_help(self):
        QMessageBox.information(
            self,
            "角色卡说明",
            "角色卡描述“角色是谁”：姓名、性格、背景和说话习惯。\n\n"
            "提示词描述“这次如何扮演”：当前关系、场景、表达风格和示例对话。\n\n"
            "默认系统提示词会自动读取当前角色卡。删除角色不会删除提示词；"
            "删除全部角色后，程序仍可使用通用角色设定。",
        )

    def _refresh_role_list(self, selected_name=""):
        names = list(self.roles)
        self.role_list.blockSignals(True)
        self.role_list.clear()
        self.role_list.addItems(names)
        self.role_list.blockSignals(False)
        if names:
            row = names.index(selected_name) if selected_name in names else 0
            self.role_list.setCurrentRow(row)
        else:
            self._clear_editor()

    def _clear_editor(self):
        self._active_name = ""
        self.role_name_edit.clear()
        self.role_persona_edit.clear()

    def _commit_active_role(self, row=None):
        if not self._active_name or self._active_name not in self.roles:
            return True
        name = self.role_name_edit.text().strip()
        persona = self.role_persona_edit.toPlainText().strip()
        if not name or not persona or (name != self._active_name and name in self.roles):
            return False
        if name != self._active_name:
            self.roles = {
                (name if key == self._active_name else key): value
                for key, value in self.roles.items()
            }
            self._active_name = name
        self.roles[name] = persona
        item = self.role_list.item(self.role_list.currentRow() if row is None else row)
        if item:
            item.setText(name)
        return True

    def _on_role_selected(self, row):
        previous_row = list(self.roles).index(self._active_name) if self._active_name in self.roles else -1
        if not self._commit_active_role(previous_row):
            return
        names = list(self.roles)
        if not 0 <= row < len(names):
            self._clear_editor()
            return
        self._active_name = names[row]
        self.role_name_edit.setText(self._active_name)
        self.role_persona_edit.setPlainText(self.roles[self._active_name])

    def _add_role(self):
        if not self._commit_active_role():
            QMessageBox.warning(self, "角色未保存", "请先填写当前角色的名称和角色卡。")
            return
        name = "新角色"
        index = 2
        while name in self.roles:
            name = f"新角色{index}"
            index += 1
        self.roles[name] = "请填写这个角色的性格、背景、说话方式和关系设定。"
        self._refresh_role_list(name)

    def _remove_role(self):
        if not self._active_name:
            return
        if not self._commit_active_role():
            QMessageBox.warning(self, "角色未保存", "请先填写当前角色的名称和角色卡。")
            return
        self.roles.pop(self._active_name, None)
        self._active_name = ""
        names = list(self.roles)
        self._refresh_role_list(names[0] if names else "")

    def _save(self):
        if not self._commit_active_role():
            QMessageBox.warning(self, "保存失败", "角色名称和角色卡不能为空，且角色名称不能重复。")
            return
        self.config.set_roles(self.roles)
        self.accept()


# ======================== 配置对话框 ========================
# 探测线程不挂在对话框上：探测要跑十几秒到几分钟，这期间用户若关掉对话框，
# 作为其子对象的 QThread 会随父对象一起销毁，Qt 随即以
# “QThread: Destroyed while thread is still running” 直接 abort —— 程序闪退、
# 且因为走的是 C++ abort，Python 侧的 sys.excepthook / app.log 一条异常都留不下。
# 这里用进程级强引用把线程保活到它自己结束，与任何窗口的生命周期解耦。
_LIVE_PROBE_THREADS = set()


def _track_probe_thread(thread):
    """保活探测线程，并在它结束后释放引用（结束后才允许 deleteLater 销毁）。

    集合保留给本模块的历史调用方；跨模块的保活与关窗回收现在统一交给
    进程级线程注册表（thread_registry），关窗时能和其它后台线程一起收尾。
    """
    thread_registry.track(thread, group="dsh_probe")
    _LIVE_PROBE_THREADS.add(thread)

    def _release(*_args):
        # 只做集合操作，不碰 C++ 侧状态：即使包装对象已被销毁也不会抛错
        _LIVE_PROBE_THREADS.discard(thread)

    thread.finished.connect(_release)
