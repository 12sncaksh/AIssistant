"""首次启动向导：把"第一次用要做什么"变成一条直线。

设计目标只有一句话：**用户打开程序后，永远知道下一步点哪里**。

页面是一条有分支的直线：

    欢迎（选路径）
      ├─ 本地模型 → 聊天配置（3 必填 + 2 可选，每项写清用途）
      └─ ACP 桥接 → 逐项环境检测（进度受控）→ 手填 Provider 或 Web 本地配置导入
    检索方式（两条路径都会经过；Ollama 能自动填好）
    完成

三条硬规矩：

1. **任何一步都能返回**，包括在 DSH 检测失败后直接改用本地；
2. **绝不猜**：检测结果、探测结果都来自 ``dsh_preflight`` / ``memory_embeddings``
   的真实调用，失败就把原文交给用户；
3. **不写密钥到日志、不联网装软件**：Ollama 只给下载页和复制命令，
   装不装由用户决定。

对外只暴露两个函数，接入时调一行：

    from first_run import show_first_run_if_needed
    show_first_run_if_needed(self, self.config)
"""
from __future__ import annotations

import os
import shutil
import sys

from PyQt6.QtCore import QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

import dsh_preflight
import voice_setup
from acp_provider_config import read_web_provider_config

# 完成标记。QSettings 在不同后端上可能返回 bool / str / int，统一按字符串比较。
FIRST_RUN_FLAG = "first_run_completed"

OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
# 中文/多语言检索明显强于 nomic-embed-text，是这个项目的默认推荐。
OLLAMA_EMBED_MODEL = "bge-m3"


# ---------------------------------------------------------------- 小工具


def _flag_done(config) -> bool:
    try:
        raw = config.settings.value(FIRST_RUN_FLAG, False)
    except Exception:  # noqa: BLE001 - 读不到就当没走过
        return False
    return str(raw).strip().lower() in ("1", "true", "yes")


def needs_first_run(config) -> bool:
    """要不要弹向导。

    只看完成标记：用户点「保存并开始」后才算走过；取消或未完成 ACP 配置都会保留首次引导。
    """
    return not _flag_done(config)


def mark_first_run_done(config, done: bool = True) -> None:
    try:
        config.settings.setValue(FIRST_RUN_FLAG, "true" if done else "false")
        config.settings.sync()
    except Exception:  # noqa: BLE001 - 标记写不进去也不该拦启动
        pass


def reset_first_run(config) -> None:
    """供「重新初始化用户配置」调用。"""
    mark_first_run_done(config, False)


def find_ollama() -> str:
    """返回 ollama 可执行文件路径；没装返回空串。"""
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidate = os.path.join(local, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(candidate):
            return candidate
    return ""


def pulled_ollama_models() -> list[str]:
    """读 Ollama 的模型清单目录（不启动服务、不发网络请求）。"""
    root = os.path.join(os.path.expanduser("~"), ".ollama", "models", "manifests")
    found: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        if not filenames:
            continue
        # 结构：manifests/registry.ollama.ai/library/<模型名>/<tag>
        name = os.path.basename(dirpath)
        if name and name != "manifests":
            found.add(name)
    return sorted(found)


_CONN_ERROR_MARKERS = (
    "max retries exceeded",
    "newconnectionerror",
    "connection refused",
    "actively refused",
    "积极拒绝",
    "10061",
    "connectionerror",
)


def _friendly_probe_error(message: str) -> str:
    """把连接类异常压成一句人话，原始信息截断后另起一行保留。

    不吞原文：用户提问时那串 host/port 往往就是唯一线索，只是不该当主句。
    """
    text = str(message or "").strip()
    if not text or "未填写" in text or "已关闭" in text:
        return text
    lowered = text.lower()
    if "timed out" in lowered or "超时" in text:
        head = "等了一会儿没有回应 —— 服务可能没启动，或者地址不通。"
    elif any(mark in lowered for mark in _CONN_ERROR_MARKERS):
        head = "连不上这个地址 —— 服务没启动，或者端口 / 路径写错了。"
    else:
        return text
    raw = text if len(text) <= 300 else text[:300] + "…"
    return f"{head}\n原始信息：{raw}"


def _hint(text: str, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setStyleSheet("color: #8a8f98;")
    return label


def _mono_box(initial: str = "") -> QPlainTextEdit:
    box = QPlainTextEdit(initial)
    box.setReadOnly(True)
    box.setMinimumHeight(120)
    box.setStyleSheet(
        "font-family: Consolas, monospace; background-color: #1e1e1e;"
        " color: #d4d4d4; border: 1px solid #3c3c3c; border-radius: 6px;"
    )
    return box


# ---------------------------------------------------------------- 后台探测


class ChatProbeThread(QThread):
    """用一次 GET /models 验证 Base URL + Key，不产生对话 token。"""

    finished_with = pyqtSignal(object)

    def __init__(self, base_url: str, api_key: str, parent=None):
        super().__init__(parent)
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""

    def run(self):
        import json
        import urllib.error
        import urllib.request

        if not self._base_url:
            self.finished_with.emit({"ok": False, "message": "还没填 Base URL。"})
            return
        request = urllib.request.Request(self._base_url + "/models")
        if self._api_key:
            request.add_header("Authorization", f"Bearer {self._api_key}")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
            items = payload.get("data") if isinstance(payload, dict) else None
            names = [str(item.get("id", "")) for item in items or [] if isinstance(item, dict)]
            names = [name for name in names if name]
            if names:
                head = "、".join(names[:5])
                more = f" 等 {len(names)} 个" if len(names) > 5 else ""
                self.finished_with.emit({"ok": True, "message": f"连接成功，这个站提供：{head}{more}"})
            else:
                self.finished_with.emit({"ok": True, "message": "连接成功了（这个站没返回模型列表，直接保存也能用）。"})
        except urllib.error.HTTPError as exc:
            self.finished_with.emit({"ok": False, "message": f"服务器返回 HTTP {exc.code}，Key 或地址可能不对。"})
        except Exception as exc:  # noqa: BLE001 - 原因要原样给用户
            self.finished_with.emit({"ok": False, "message": f"连不上：{exc}"})


class RetrievalProbeThread(QThread):
    """在后台探测当前配置的 Embedding API，避免首次引导界面被网络请求卡住。"""

    finished_with = pyqtSignal(object)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = config

    def run(self):
        try:
            from memory_embeddings import EmbeddingService

            result = EmbeddingService(self._config).probe_api()
            if result.get("ok"):
                self.finished_with.emit(result)
                return
            self.finished_with.emit(result)
        except Exception as exc:  # noqa: BLE001 - 原因要原样交给向导显示
            self.finished_with.emit({"ok": False, "message": f"API 探测失败：{exc}"})


class DshPreflightThread(QThread):
    """逐项执行 DSH 预检，避免同步检查冻结首次启动向导。"""

    check_finished = pyqtSignal(int, object)
    all_finished = pyqtSignal(object)

    def run(self):
        checks = []
        checkers = (
            dsh_preflight.check_dsh_command,
            dsh_preflight.check_node,
            dsh_preflight.check_home_writable,
            dsh_preflight.check_profile,
            dsh_preflight.check_patch,
        )
        for index, checker in enumerate(checkers, 1):
            try:
                result = checker()
            except Exception as exc:  # noqa: BLE001 - 自检结果必须回到界面
                result = dsh_preflight.Check(
                    f"preflight_{index}", dsh_preflight.FAIL,
                    "自检项目执行失败", str(exc), "", ""
                )
            checks.append(result)
            self.check_finished.emit(index, result)
        self.all_finished.emit(checks)




class WelcomePage(QWizardPage):
    PAGE_ID = 0

    def __init__(self, wizard):
        super().__init__(wizard)
        self.wizard_ref = wizard
        self.setTitle("欢迎使用 Aissistant")
        self.setSubTitle("本鱼不知道你以前用过什么，所以先问一句：")

        self.local_radio = QRadioButton("用本地模型开始（没有装过 DeepSeek Harness）")
        self.local_radio.setChecked(True)
        self.dsh_radio = QRadioButton("用我已有的 DeepSeek Harness（需要本机装好 dsh）")

        group = QGroupBox("选一条路")
        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self.local_radio)
        group_layout.addWidget(_hint("自己填一个 API Key 就能聊。大多数人都走这条。"))
        group_layout.addSpacing(8)
        group_layout.addWidget(self.dsh_radio)
        group_layout.addWidget(_hint("已经装过 dsh、配好模型的话走这条，模型和 Key 由 DSH 自己管。"))

        self.choice = QButtonGroup(self)
        self.choice.addButton(self.local_radio, 0)
        self.choice.addButton(self.dsh_radio, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(group)
        layout.addWidget(
            _hint(
                "两条路都能随时改：设置 → DSH 桥接里可以再切换茅；"
                "想再走一遍：设置 →「重新初始化用户配置」。"
            )
        )
        layout.addStretch(1)

    def nextId(self):
        return DshPage.PAGE_ID if self.dsh_radio.isChecked() else LocalPage.PAGE_ID


class LocalPage(QWizardPage):
    PAGE_ID = 10

    def __init__(self, wizard):
        super().__init__(wizard)
        self.wizard_ref = wizard
        self.setTitle("填三个框就能聊了")
        self.setSubTitle("只有第一组是必填的；下面两组先跳过也完全能聊天。")

        # ---- 必填
        required = QGroupBox("① 聊天要用的（必填）")
        required_form = QFormLayout(required)
        self.base_url_edit = QLineEdit(wizard.config.get_base_url())
        self.base_url_edit.setPlaceholderText("例如 https://api.deepseek.com/v1")
        self.api_key_edit = QLineEdit(wizard.config.get_api_key())
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        self.model_edit = QLineEdit(wizard.config.get_model())
        self.model_edit.setPlaceholderText("例如 deepseek-chat")
        required_form.addRow("Base URL", self.base_url_edit)
        required_form.addRow("API Key", self.api_key_edit)
        required_form.addRow("模型", self.model_edit)
        required_form.addRow("", _hint("填的是 OpenAI 兼容接口地址；中转站通常也兼容，末尾带不带 /v1 都行。"))

        self.probe_button = QPushButton("测一下能不能用（不会花钱）")
        self.probe_button.clicked.connect(wizard.probe_chat)
        self.probe_status = _hint("")
        probe_row = QHBoxLayout()
        probe_row.addWidget(self.probe_button)
        probe_row.addWidget(self.probe_status, 1)

        # ---- 可选：搜索
        search = QGroupBox("② 联网搜索：让本鱼能查最新消息（可以跳过）")
        search_form = QFormLayout(search)
        self.tavily_edit = QLineEdit(wizard.config.get_tavily_api_key())
        self.tavily_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.tavily_edit.setPlaceholderText("tvly-...（留空就不启用联网搜索）")
        search_form.addRow("Tavily Key", self.tavily_edit)
        search_form.addRow("", _hint("不填就只能凭训练时的记忆回答；填了才能查新闻、查文档。"))

        layout = QVBoxLayout(self)
        layout.addWidget(required)
        layout.addLayout(probe_row)
        layout.addWidget(search)
        layout.addStretch(1)

    def nextId(self):
        return RetrievalPage.PAGE_ID


class DshPage(QWizardPage):
    PAGE_ID = 20

    def __init__(self, wizard):
        super().__init__(wizard)
        self.wizard_ref = wizard
        self.setTitle("配置 ACP 桥接")
        self.setSubTitle("先完成环境自检；下面两种 Provider 配置方式并列，任选一种导入到独立 ACP 配置。")
        self._checks = []
        self._check_thread = None
        self._check_finished = False
        self._web_providers = []

        self.progress = QProgressBar()
        self.progress.setRange(0, 5)
        self.progress.setValue(0)
        self.progress.setFormat("等待开始自检")
        self.result_box = _mono_box("准备检查 dsh、Node、目录、profile 和权限补丁……")
        self.recheck_button = QPushButton("重新检查")
        self.recheck_button.clicked.connect(self.run_checks)
        self.switch_local_button = QPushButton("改用本地模型")
        self.switch_local_button.clicked.connect(self.switch_to_local)

        manual = QGroupBox("方式一：手动填写 ACP Provider")
        manual_form = QFormLayout(manual)
        current = wizard.config.get_acp_provider() or {}
        self.provider_edit = QLineEdit(current.get("name", ""))
        self.url_edit = QLineEdit(current.get("base_url", ""))
        self.url_edit.setPlaceholderText("例如 https://example.com/v1")
        self.api_key_edit = QLineEdit(current.get("api_key", ""))
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("ACP 专用 API Key")
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        for model in current.get("models") or []:
            self.model_combo.addItem(model, model)
        self.model_combo.setCurrentText(current.get("model", ""))
        manual_form.addRow("Provider 名称", self.provider_edit)
        manual_form.addRow("API URL", self.url_edit)
        manual_form.addRow("API Key", self.api_key_edit)
        manual_form.addRow("模型（可稍后探测）", self.model_combo)
        manual_form.addRow("", _hint("这份配置只属于 Aissistant 的 ACP，不会写回 DSH Web。"))

        web = QGroupBox("方式二：读取 DSH Web 本地配置并导入")
        web_form = QFormLayout(web)
        self.web_provider_combo = QComboBox()
        self.web_provider_combo.setEnabled(False)
        self.web_read_button = QPushButton("读取 Web 配置")
        self.web_import_button = QPushButton("导入选中的 Provider")
        self.web_import_button.setEnabled(False)
        self.web_read_button.clicked.connect(self.read_web_config)
        self.web_import_button.clicked.connect(self.import_selected_web_provider)
        web_buttons = QHBoxLayout()
        web_buttons.addWidget(self.web_read_button)
        web_buttons.addWidget(self.web_import_button)
        web_form.addRow("Web Provider", self.web_provider_combo)
        web_form.addRow("", web_buttons)
        self.web_status = _hint("不会自动读取；请点击按钮后选择要导入的 Provider。")
        web_form.addRow("", self.web_status)

        buttons = QHBoxLayout()
        buttons.addWidget(self.recheck_button)
        buttons.addWidget(self.switch_local_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(_hint("环境自检完成前不能点下一步；存在红色阻塞项时，请修复后重新检查，或改用本地模型。"))
        layout.addWidget(self.progress)
        layout.addWidget(self.result_box, 1)
        layout.addLayout(buttons)
        layout.addWidget(manual)
        layout.addWidget(web)
        self._provider_widgets = (self.provider_edit, self.url_edit, self.api_key_edit, self.model_combo)
        for edit in (self.provider_edit, self.url_edit, self.api_key_edit):
            edit.textChanged.connect(lambda _text: self._emit_complete())

    def initializePage(self):
        if not self._check_finished:
            self.run_checks()

    def isComplete(self):
        return (
            self._check_finished
            and not dsh_preflight.blocking_checks(self._checks)
            and bool(self.provider_edit.text().strip())
            and bool(self.url_edit.text().strip())
            and bool(self.api_key_edit.text().strip())
        )

    def _emit_complete(self):
        self.completeChanged.emit()

    def run_checks(self):
        if self._check_thread is not None and self._check_thread.isRunning():
            return
        self._checks = []
        self._check_finished = False
        self.progress.setValue(0)
        self.progress.setFormat("正在准备自检…")
        self.result_box.setPlainText("正在启动逐项检查……")
        self.recheck_button.setEnabled(False)
        self._emit_complete()
        self._check_thread = DshPreflightThread(self)
        self._check_thread.check_finished.connect(self._on_check_finished)
        self._check_thread.all_finished.connect(self._on_checks_finished)
        self._check_thread.finished.connect(self._on_check_thread_finished)
        self._check_thread.start()

    def _on_check_finished(self, index, result):
        while len(self._checks) < index:
            self._checks.append(None)
        self._checks[index - 1] = result
        self.progress.setValue(index)
        self.progress.setFormat(f"已完成 {index}/5：{result.title}")
        self.result_box.setPlainText(dsh_preflight.format_report([item for item in self._checks if item is not None]))

    def _on_checks_finished(self, checks):
        self._checks = list(checks or [])
        self._check_finished = True
        self.recheck_button.setEnabled(True)
        self.progress.setValue(5)
        self.progress.setFormat("自检完成")
        self.result_box.setPlainText(dsh_preflight.format_report(self._checks))
        self._emit_complete()

    def _on_check_thread_finished(self):
        self._check_thread = None

    def read_web_config(self):
        try:
            self._web_providers = read_web_provider_config()
        except Exception as exc:  # noqa: BLE001 - 原因直接展示给用户
            self.web_status.setText(f"读取失败：{exc}")
            self.web_status.setStyleSheet("color: #e06c75;")
            return
        self.web_provider_combo.blockSignals(True)
        self.web_provider_combo.clear()
        for provider in self._web_providers:
            self.web_provider_combo.addItem(provider.get("name", ""), provider.get("name", ""))
        self.web_provider_combo.blockSignals(False)
        enabled = bool(self._web_providers)
        self.web_provider_combo.setEnabled(enabled)
        self.web_import_button.setEnabled(enabled)
        self.web_status.setText(
            f"已读取 {len(self._web_providers)} 个 Web Provider，请选择后点击导入。"
            if enabled else "Web 本地配置中没有可导入的 Provider。"
        )
        self.web_status.setStyleSheet("color: #4caf50;" if enabled else "color: #e06c75;")

    def import_selected_web_provider(self):
        index = self.web_provider_combo.currentIndex()
        if index < 0 or index >= len(self._web_providers):
            return
        provider = self._web_providers[index]
        self.provider_edit.setText(provider.get("name", ""))
        self.url_edit.setText(provider.get("base_url", ""))
        self.api_key_edit.setText(provider.get("api_key", ""))
        self.model_combo.clear()
        for model in provider.get("models") or []:
            self.model_combo.addItem(model, model)
        if provider.get("models"):
            self.model_combo.setCurrentIndex(0)
        env_hint = provider.get("api_key_env") or "未声明环境变量"
        self.web_status.setText(
            f"已将「{provider.get('name', '')}」填入 ACP 表单；"
            + ("Key 已从环境变量读取。" if provider.get("api_key") else f"未读取到 {env_hint}，请手动填写 Key。")
        )
        self._emit_complete()

    def switch_to_local(self):
        self.wizard_ref.switch_to_local()

    def nextId(self):
        return RetrievalPage.PAGE_ID


class RetrievalPage(QWizardPage):
    PAGE_ID = 30

    def __init__(self, wizard):
        super().__init__(wizard)
        self.wizard_ref = wizard
        self.setTitle("检索（记忆）")
        self.setSubTitle("这一步决定本鱼能不能记住你说过的事。聊天不依赖它，但记忆力差很多。")

        self.ollama_radio = QRadioButton("Ollama 本地（推荐：免费、离线、不用另外申请 Key）")
        self.key_radio = QRadioButton("专用 Embedding Key（你有的话最省事）")
        self.skip_radio = QRadioButton("先跳过（检索会很弱，以后能在设置里补）")

        group = QGroupBox("选一个")
        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self.ollama_radio)
        group_layout.addWidget(self.key_radio)
        group_layout.addWidget(self.skip_radio)
        self.choice = QButtonGroup(self)
        self.choice.addButton(self.ollama_radio, 0)
        self.choice.addButton(self.key_radio, 1)
        self.choice.addButton(self.skip_radio, 2)
        self.ollama_radio.setChecked(True)

        # Ollama 区
        self.ollama_box = QGroupBox("Ollama")
        ollama_layout = QVBoxLayout(self.ollama_box)
        self.ollama_status = _hint("正在检查……")
        self.ollama_download_button = QPushButton("打开 Ollama 下载页")
        self.ollama_download_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(OLLAMA_DOWNLOAD_URL))
        )
        ollama_cmd = QLineEdit(f"ollama pull {OLLAMA_EMBED_MODEL}")
        ollama_cmd.setReadOnly(True)
        ollama_layout.addWidget(self.ollama_status)
        ollama_layout.addWidget(self.ollama_download_button)
        ollama_layout.addWidget(_hint(f"装好后在终端跑这一行（点一下可全选复制），模型约 1.2 GB："))
        ollama_layout.addWidget(ollama_cmd)

        # 专用 Key 区
        self.key_box = QGroupBox("专用 Embedding Key")
        key_form = QFormLayout(self.key_box)
        self.emb_base_edit = QLineEdit(wizard.config.get_memory_embedding_base_url())
        self.emb_base_edit.setPlaceholderText("例如 https://api.siliconflow.cn/v1")
        self.emb_key_edit = QLineEdit(wizard.config.get_memory_embedding_api_key())
        self.emb_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.emb_key_edit.setPlaceholderText("和聊天 Key 不是同一个，要单独注册")
        self.emb_model_edit = QLineEdit(wizard.config.get_memory_embedding_model())
        self.emb_model_edit.setPlaceholderText("例如 BAAI/bge-m3")
        key_form.addRow("Base URL", self.emb_base_edit)
        key_form.addRow("Key", self.emb_key_edit)
        key_form.addRow("模型", self.emb_model_edit)
        self.key_box.setVisible(False)

        self.probe_button = QPushButton("测一下现在能不能用")
        self.probe_button.clicked.connect(wizard.probe_retrieval)
        self.probe_status = _hint("")

        layout = QVBoxLayout(self)
        layout.addWidget(group)
        layout.addWidget(self.ollama_box)
        layout.addWidget(self.key_box)
        probe_row = QHBoxLayout()
        probe_row.addWidget(self.probe_button)
        probe_row.addWidget(self.probe_status, 1)
        layout.addLayout(probe_row)
        layout.addStretch(1)

        # buttonClicked 会带一个按钮参数，这里吞掉，避免签名不匹配。
        self.choice.buttonClicked.connect(lambda *_: wizard.update_retrieval_fields())

    def initializePage(self):
        self.wizard_ref.update_retrieval_fields()

    def nextId(self):
        return VoicePage.PAGE_ID


class VoicePage(QWizardPage):
    PAGE_ID = 35

    def __init__(self, wizard):
        super().__init__(wizard)
        self.wizard_ref = wizard
        self.setTitle("语音（都可以跳过）")
        self.setSubTitle("跟走哪条路无关，两条路共用这一份；不配就只出文字、只用键盘。")

        speak_group = QGroupBox("① 让它说话（合成）")
        speak_form = QFormLayout(speak_group)
        self.voice_combo = QComboBox()
        for text, value in voice_setup.VOICE_ENGINE_ITEMS:
            self.voice_combo.addItem(text, value)
        self.voice_combo.setCurrentIndex(1 if wizard.config.get_tts_enabled() else 0)
        speak_form.addRow("引擎", self.voice_combo)
        speak_form.addRow("", _hint(voice_setup.VOICE_ENGINE_HINT))

        input_group = QGroupBox("② " + voice_setup.XFYUN_GROUP_TITLE)
        input_form = QFormLayout(input_group)
        self.xfyun_appid = QLineEdit(wizard.config.get_xfyun_appid())
        self.xfyun_appid.setPlaceholderText("6 位 APPID")
        self.xfyun_key = QLineEdit(wizard.config.get_xfyun_api_key())
        self.xfyun_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.xfyun_secret = QLineEdit(wizard.config.get_xfyun_api_secret())
        self.xfyun_secret.setEchoMode(QLineEdit.EchoMode.Password)
        input_form.addRow("AppID", self.xfyun_appid)
        input_form.addRow("API Key", self.xfyun_key)
        input_form.addRow("API Secret", self.xfyun_secret)
        input_form.addRow("", _hint(voice_setup.XFYUN_HINT))
        input_form.addRow("", _hint(voice_setup.VOICE_INPUT_HINT))
        input_form.addRow("", _hint(voice_setup.WAKE_WORD_HINT))

        layout = QVBoxLayout(self)
        layout.addWidget(speak_group)
        layout.addWidget(input_group)
        layout.addStretch(1)

    def nextId(self):
        return DonePage.PAGE_ID


class DonePage(QWizardPage):
    PAGE_ID = 40

    def __init__(self, wizard):
        super().__init__(wizard)
        self.wizard_ref = wizard
        self.setTitle("好了，可以开始了")
        self.setSubTitle("下面这些随时能在设置里改，不会锁死。")

        self.summary = _mono_box("")
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(
            _hint(
                "想再走一遍：设置 →「重新初始化用户配置」。"
                "本向导不会替你装任何软件，也不会悄悄改你的配置。"
            )
        )

    def initializePage(self):
        self.summary.setPlainText(self.wizard_ref.summary_text())


# ---------------------------------------------------------------- 向导本体


class FirstRunWizard(QWizard):
    """首启向导。所有写入都在 finish 时一次性完成。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._chat_probe = None
        self._retrieval_probe = None

        self.setWindowTitle("Aissistant 首次使用引导")
        self.resize(700, 620)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.WizardButton.NextButton, "下一步")
        self.setButtonText(QWizard.WizardButton.BackButton, "← 返回")
        self.setButtonText(QWizard.WizardButton.FinishButton, "保存并开始")
        self.setButtonText(QWizard.WizardButton.CancelButton, "先不设置")

        self.welcome = WelcomePage(self)
        self.local_page = LocalPage(self)
        self.dsh_page = DshPage(self)
        self.retrieval_page = RetrievalPage(self)
        self.voice_page = VoicePage(self)
        self.done_page = DonePage(self)
        # 页号必须显式注册，否则 nextId() 的返回值无处可去。
        self.setPage(WelcomePage.PAGE_ID, self.welcome)
        self.setPage(LocalPage.PAGE_ID, self.local_page)
        self.setPage(DshPage.PAGE_ID, self.dsh_page)
        self.setPage(RetrievalPage.PAGE_ID, self.retrieval_page)
        self.setPage(VoicePage.PAGE_ID, self.voice_page)
        self.setPage(DonePage.PAGE_ID, self.done_page)

        self.update_retrieval_fields()

    # ---- 页面切换

    def switch_to_local(self):
        """从 DSH 检测页退回本地配置页（用户点错了 / 环境修不好）。"""
        self.welcome.local_radio.setChecked(True)
        self.welcome.dsh_radio.setChecked(False)
        self.setCurrentId(LocalPage.PAGE_ID)

    # ---- 检索

    def update_retrieval_fields(self):
        page = self.retrieval_page
        page.ollama_box.setVisible(page.ollama_radio.isChecked())
        page.key_box.setVisible(page.key_radio.isChecked())
        if page.ollama_radio.isChecked():
            self._fill_ollama_status()

    def _fill_ollama_status(self):
        page = self.retrieval_page
        exe = find_ollama()
        if not exe:
            page.ollama_status.setText(
                "本机还没装 Ollama。点下面的按钮去下载，装完回来点「测一下」即可。\n"
                f"（装好后地址会自动填成 {OLLAMA_BASE_URL}，模型选 {OLLAMA_EMBED_MODEL}）"
            )
            return
        models = pulled_ollama_models()
        if OLLAMA_EMBED_MODEL in models:
            page.ollama_status.setText(f"已装 Ollama ✓，而且已经拉过 {OLLAMA_EMBED_MODEL} ✓，点「测一下」就行。")
        elif models:
            page.ollama_status.setText(
                f"已装 Ollama ✓，现有模型：{'、'.join(models[:6])}。\n"
                f"还差 {OLLAMA_EMBED_MODEL}：在终端跑 `ollama pull {OLLAMA_EMBED_MODEL}`。"
            )
        else:
            page.ollama_status.setText(
                f"已装 Ollama ✓，但还没拉过任何模型。\n"
                f"在终端跑 `ollama pull {OLLAMA_EMBED_MODEL}`（约 1.2 GB）。"
            )

    # ---- 探测

    def probe_chat(self):
        page = self.local_page
        if self._chat_probe is not None and self._chat_probe.isRunning():
            return
        page.probe_status.setText("正在连接……")
        page.probe_button.setEnabled(False)
        self._chat_probe = ChatProbeThread(page.base_url_edit.text().strip(), page.api_key_edit.text().strip(), self)
        self._chat_probe.finished_with.connect(self._on_chat_probed)
        self._chat_probe.start()

    def _on_chat_probed(self, result):
        page = self.local_page
        page.probe_button.setEnabled(True)
        message = str(result.get("message", ""))
        page.probe_status.setText(message if result.get("ok") else _friendly_probe_error(message))
        page.probe_status.setStyleSheet("color: #4caf50;" if result.get("ok") else "color: #e06c75;")

    def probe_retrieval(self):
        page = self.retrieval_page
        if self._retrieval_probe is not None and self._retrieval_probe.isRunning():
            return
        self._apply_retrieval_to_config(force_backend=True)
        page.probe_status.setText("正在探测……")
        page.probe_button.setEnabled(False)
        self._retrieval_probe = RetrievalProbeThread(self.config, self)
        self._retrieval_probe.finished_with.connect(self._on_retrieval_probed)
        self._retrieval_probe.start()

    def _on_retrieval_probed(self, result):
        page = self.retrieval_page
        page.probe_button.setEnabled(True)
        message = str(result.get("message", ""))
        page.probe_status.setText(message if result.get("ok") else _friendly_probe_error(message))
        page.probe_status.setStyleSheet("color: #4caf50;" if result.get("ok") else "color: #e06c75;")

    # ---- 写入

    def _apply_retrieval_to_config(self, force_backend: bool = False):
        page = self.retrieval_page
        if page.skip_radio.isChecked():
            if force_backend:
                self.config.set_memory_embedding_backend("auto")
            return
        if page.ollama_radio.isChecked():
            self.config.set_memory_embedding_backend("api")
            self.config.set_memory_embedding_base_url(OLLAMA_BASE_URL)
            self.config.set_memory_embedding_model(OLLAMA_EMBED_MODEL)
            # 本地回环不需要 Key；显式清空，避免把云端聊天 Key 发给本机服务。
            self.config.set_memory_embedding_api_key("")
            return
        self.config.set_memory_embedding_backend("api")
        self.config.set_memory_embedding_base_url(page.emb_base_edit.text().strip())
        self.config.set_memory_embedding_model(page.emb_model_edit.text().strip())
        self.config.set_memory_embedding_api_key(page.emb_key_edit.text().strip())

    def _apply_dsh_to_config(self):
        page = self.dsh_page
        models = []
        for index in range(page.model_combo.count()):
            value = str(page.model_combo.itemData(index) or page.model_combo.itemText(index)).strip()
            if value and value not in models:
                models.append(value)
        self.config.set_acp_provider({
            "name": page.provider_edit.text().strip(),
            "base_url": page.url_edit.text().strip(),
            "api_key": page.api_key_edit.text().strip(),
            "model": page.model_combo.currentText().strip(),
            "models": models,
        })

        page = self.local_page
        base_url = page.base_url_edit.text().strip()
        if base_url:
            self.config.set_base_url(base_url)
        if page.model_edit.text().strip():
            self.config.set_model(page.model_edit.text().strip())
        self.config.set_api_key(page.api_key_edit.text().strip())
        self.config.set_tavily_api_key(page.tavily_edit.text().strip())

    def _apply_voice_to_config(self):
        """语音是两条路共用的，所以不放 local / dsh 分支里。

        空值不覆盖已有配置：用户没填，就不该把原来的密钥清掉。
        """
        page = self.voice_page
        self.config.set_tts_enabled(page.voice_combo.currentData() != "off")
        for setter, edit in (
            (self.config.set_xfyun_appid, page.xfyun_appid),
            (self.config.set_xfyun_api_key, page.xfyun_key),
            (self.config.set_xfyun_api_secret, page.xfyun_secret),
        ):
            value = edit.text().strip()
            if value:
                setter(value)

    def summary_text(self) -> str:
        lines = []
        if self.welcome.dsh_radio.isChecked():
            provider = self.dsh_page.provider_edit.text().strip() or "(未填)"
            model = self.dsh_page.model_combo.currentText().strip() or "(稍后探测)"
            lines.append("路径：ACP 桥接")
            lines.append(f"  · Provider：{provider}")
            lines.append(f"  · 模型：{model}")
            lines.append("  · URL / API Key：由 Aissistant 独立保存并注入 ACP")
        else:
            base = self.local_page.base_url_edit.text().strip() or "(未填)"
            lines.append("路径：本地模型")
            lines.append(f"  · Base URL：{base}")
            lines.append(f"  · 模型：{self.local_page.model_edit.text().strip() or '(未填)'}")
            has_tavily = bool(self.local_page.tavily_edit.text().strip())
            lines.append(f"  · 联网搜索：{'已填 Tavily Key' if has_tavily else '未启用'}")
        lines.append(f"语音：{self._voice_summary()}")
        if self.retrieval_page.skip_radio.isChecked():
            lines.append("检索：先跳过（记忆检索会很弱）")
        elif self.retrieval_page.ollama_radio.isChecked():
            lines.append(f"检索：Ollama 本地（{OLLAMA_EMBED_MODEL}）")
        else:
            lines.append("检索：专用 Embedding Key")
        return "\n".join(lines)

    def _voice_summary(self) -> str:
        """摘要看控件当前值，不看已保存的配置（用户还没点保存）。"""
        page = self.voice_page
        tts = "开（edge-tts）" if page.voice_combo.currentData() != "off" else "关"
        ready = all(
            edit.text().strip()
            for edit in (page.xfyun_appid, page.xfyun_key, page.xfyun_secret)
        )
        asr = "已配讯飞听写" if ready else "未配（麦克风会提示缺密钥）"
        return f"语音回复 {tts}；语音输入 {asr}"

    def accept(self):
        """用户点「保存并开始」：先落盘，再关闭。"""
        try:
            if self.welcome.dsh_radio.isChecked():
                if not self.dsh_page.isComplete():
                    self.dsh_page._emit_complete()
                    return
                self._apply_dsh_to_config()
                self.config.set_dsh_bridge_enabled(True)
            else:
                self.config.set_dsh_bridge_enabled(False)
                self._apply_local_to_config()
            self._apply_voice_to_config()
            self._apply_retrieval_to_config()
        except Exception:  # noqa: BLE001 - 保存失败不该让窗口卡死，但要让用户知道
            import logging

            logging.exception("首次引导保存失败")
            raise
        mark_first_run_done(self.config)
        super().accept()


# ---------------------------------------------------------------- 接入入口


def show_first_run_if_needed(parent, config) -> bool:
    """需要时弹向导；返回是否真的弹了。

    接在 MainWindow 显示之后调用一次即可。用户在向导里点「先不设置」不算走过，
    下次启动还会问；点「保存并开始」才会写完成标记。
    """
    if not needs_first_run(config):
        return False
    wizard = FirstRunWizard(config, parent)
    wizard.exec()
    if wizard.result() == QWizard.DialogCode.Accepted:
        mark_first_run_done(config)
    return True


def restart_first_run(parent, config) -> bool:
    """「重新初始化用户配置」：清掉标记后立刻重走一遍引导。

    全程序只有这一个入口（主窗口 设置 → 重新初始化用户配置）。
    """
    reset_first_run(config)
    return show_first_run_if_needed(parent, config)


def main(argv: list[str] | None = None) -> int:
    """单独跑一遍向导（调试用，不依赖主程序）。"""
    from PyQt6.QtWidgets import QApplication

    import config_manager

    app = QApplication(sys.argv[:1])
    config = config_manager.ConfigManager()
    wizard = FirstRunWizard(config)
    wizard.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
