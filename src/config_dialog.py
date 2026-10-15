"""Settings dialog."""

from __future__ import annotations
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import Qt
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QCheckBox
from PyQt6.QtWidgets import QComboBox
from PyQt6.QtWidgets import QDialog
from PyQt6.QtWidgets import QDialogButtonBox
from PyQt6.QtWidgets import QFormLayout
from PyQt6.QtWidgets import QFrame
from PyQt6.QtWidgets import QGroupBox
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QLineEdit
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtWidgets import QScrollArea
from PyQt6.QtWidgets import QTabWidget
from PyQt6.QtWidgets import QTextEdit
from PyQt6.QtWidgets import QWidget
from dsh_acp_client import MODEL_OPTION_CATEGORY
from dsh_acp_client import REASONING_OPTION_CATEGORY
from dsh_acp_client import config_option_choices
from dsh_acp_client import find_config_option
from dsh_acp_client import parse_model_selection_value
from dsh_options_probe import DshOptionsProbeThread
from memory_embeddings import StaticEmbeddingConfig
import asyncio
import edge_tts
import thread_registry

from permission_modes import DEFAULT_PERMISSION_MODE, FULL_ACCESS_MODE, PERMISSION_MODE_CHOICES, PERMISSION_MODE_DESCRIPTIONS, READ_ONLY_MODE
from reasoning_options import DSH_REASONING_CHOICES, REASONING_EFFORT_CHOICES
from role_dialog import _track_probe_thread



# ---- method groups moved into their own modules ----
from dsh_settings_ui import (  # noqa: F401
    _fill_dsh_reasoning_combo,
    _current_dsh_reasoning_choice,
    _sync_dsh_provider_from_model,
    _selected_dsh_model,
    _schedule_dsh_options_probe,
    _probe_dsh_options,
    _on_dsh_probe_thread_finished,
    _on_dsh_options_probed,
    _restore_cached_dsh_options,
    _apply_dsh_options,
    _apply_dsh_bridge_state,
    _save_dsh_selection,
)
from core_memory_dialog import (  # noqa: F401
    build_core_memory_tab,
    open_core_memory_dialog,
)

class ConfigDialog(QDialog):
    voices_loaded = pyqtSignal(list)   # 信号用于传递声音列表
    embedding_probe_finished = pyqtSignal(bool, str)  # (是否可用, 说明文字)

    def __init__(self, config, parent=None, system_prompt_text=None, section="config", builtin_system_prompt_text=None):
        super().__init__(parent)
        self.config = config
        self.section = section
        self.initial_system_prompt_mode = config.get_system_prompt_mode()
        self._secret_initial_values = {}
        title_map = {"config": "配置", "prompt": "提示词", "voice": "语音"}
        self.setWindowTitle(title_map.get(section, "配置"))
        self.setMinimumWidth(450)
        layout = QFormLayout(self)

        if section == "config":
            # 配置页原先是一个 22 行的单页 QFormLayout，既没有 Tab 也没有滚动区，
            # 任何屏幕都放不全。这里只重组容器：控件名、信号与行为完全不变，
            # _apply_dsh_bridge_state 仍按属性名跨 Tab 生效。
            self.tabs = QTabWidget()
            self.tabs.setDocumentMode(True)
            base_tab, base_layout = self._make_tab_page()
            dsh_tab, dsh_layout = self._make_tab_page()
            tools_tab, tools_layout = self._make_tab_page()
            self.tabs.addTab(base_tab, "基础模型")
            self.tabs.addTab(dsh_tab, "DSH 桥接")
            self.tabs.addTab(tools_tab, "工具与权限")
            # 常驻块：每轮都注入、且不过任何阈值，所以"能改"是它唯一的安全阀。
            # 之前唯一的改法是手工编辑 data/core_memory.json。
            self.tabs.addTab(self.build_core_memory_tab(), "常驻块")
            layout.addRow(self.tabs)
            self.resize(700, 460)

            self._secret_initial_values = {
                key: str(config.settings.value(key, "") or "").strip()
                for key in ("api_key", "tavily_api_key", "amap_api_key", "memory_embedding_api_key")
            }
            self.dsh_bridge_checkbox = QCheckBox("启用 DSH 桥接（交给本机 DSH 运行时）")
            self.dsh_bridge_checkbox.setChecked(config.get_dsh_bridge_enabled())
            self.dsh_bridge_checkbox.setToolTip(
                "勾选后：消息转交本机 DSH（dsh --profile acp）处理，模型与 API Key 由 DSH 自己管理；\n"
                "下面的 Base URL / API Key / 模型 / 推理强度将不再生效。\n"
                "取消勾选：回到本地直连模型。"
            )
            dsh_layout.addRow(self.dsh_bridge_checkbox)
            self.dsh_bridge_hint = QLabel(
                "DSH 桥接已启用：请先在 DSH 网页端配置 Provider，再用本页探测并选择模型。"
                "对话与工具执行全部交给本机 DSH 运行时。"
                "「基础模型」页的本地 API 设置此时不生效。"
            )
            self.dsh_bridge_hint.setWordWrap(True)
            self.dsh_bridge_hint.setStyleSheet(
                "color: #ffb454; font-weight: bold; background-color: #3a2d17;"
                " border: 1px solid #d9822b; border-radius: 6px; padding: 6px 8px;"
            )
            self.dsh_bridge_hint.setVisible(config.get_dsh_bridge_enabled())
            dsh_layout.addRow("", self.dsh_bridge_hint)

            # DSH 桥接专属：模型与推理档位。候选项由 DSH 的 ACP 会话公布，用「探测」拉取；
            # 探测失败也能手填，桥接侧只在 DSH 确实提供该模型 / 档位时才下发。
            self.dsh_provider_edit = QLineEdit(config.get_dsh_provider())
            self.dsh_provider_edit.setPlaceholderText("DSH provider id，例如 opencode-ds")
            self.dsh_provider_edit.setToolTip(
                "对应 config/dsh_acp_overlay.yml 里的 provider；留空表示不改动 DSH 原有路由。"
            )
            dsh_layout.addRow("DSH Provider:", self.dsh_provider_edit)

            self._dsh_model_values = {}
            self.dsh_model_combo = QComboBox()
            self.dsh_model_combo.setEditable(True)
            self.dsh_model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            current_dsh_model = config.get_dsh_model()
            self.dsh_model_combo.addItem(current_dsh_model or "", current_dsh_model or "")
            self.dsh_model_combo.setToolTip(
                "点下面的「探测」可从 DSH 拉取真实模型列表（按 provider 分组）；也可直接填模型 id。"
            )
            dsh_layout.addRow("DSH 模型:", self.dsh_model_combo)

            self.dsh_reasoning_combo = QComboBox()
            self.dsh_reasoning_combo.setEditable(True)
            self.dsh_reasoning_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            self._fill_dsh_reasoning_combo(config.get_dsh_reasoning_effort())
            self.dsh_reasoning_combo.setToolTip(
                "ACP 的 reasoning_effort 档位，与 DSH 网页端 composer 的推理强度同源；\n"
                "只有所选模型声明了 reasoningEfforts 时 DSH 才会公布档位，否则本项会被忽略。"
            )
            dsh_layout.addRow("DSH 推理深度:", self.dsh_reasoning_combo)

            self.dsh_probe_button = QPushButton("探测 DSH 可用模型 / 推理档位")
            self.dsh_probe_button.clicked.connect(self._probe_dsh_options)
            dsh_layout.addRow("", self.dsh_probe_button)

            self.dsh_probe_status = QLabel(
                "模型列表与推理档位由 DSH 的 ACP 会话公布，点上面的按钮读取；"
                "探测会临时启动一次 dsh --profile acp，不发送任何消息。"
            )
            self.dsh_probe_status.setWordWrap(True)
            dsh_layout.addRow("", self.dsh_probe_status)
            self._dsh_probe_thread = None
            self._dsh_probe_pending = False
            self._dsh_probe_timer = QTimer(self)
            self._dsh_probe_timer.setSingleShot(True)
            self._dsh_probe_timer.timeout.connect(self._probe_dsh_options)

            # 缓存回填必须在接线之前：_apply_dsh_options 会重建下拉项，
            # 若此时已连上 _schedule_dsh_options_probe，回填本身就会触发一次探测，
            # 缓存也就白做了。
            self._restore_cached_dsh_options()
            self.dsh_model_combo.currentIndexChanged.connect(self._sync_dsh_provider_from_model)
            self.dsh_model_combo.currentIndexChanged.connect(self._schedule_dsh_options_probe)
            self.dsh_model_combo.lineEdit().textEdited.connect(self._schedule_dsh_options_probe)

            # 权限模式：与 DSH 网页端的权限预设选择器同源（沙箱边界 + 审批策略捆绑）
            self.permission_combo = QComboBox()
            for label, value, description in PERMISSION_MODE_CHOICES:
                self.permission_combo.addItem(label, value)
                self.permission_combo.setItemData(
                    self.permission_combo.count() - 1, description, Qt.ItemDataRole.ToolTipRole
                )
            current_permission = config.get_permission_mode()
            self.permission_combo.setCurrentIndex(
                max(0, self.permission_combo.findData(current_permission))
            )
            self.permission_combo.setToolTip(
                "权限预设同时决定「沙箱边界」和「是否需要弹窗确认」，与 DSH 网页端的权限选择器一致。\n"
                "默认是工作区可写；完全权限模式请只在信任的任务里临时开启。"
            )
            tools_layout.addRow("权限模式:", self.permission_combo)

            self.permission_hint = QLabel("")
            self.permission_hint.setWordWrap(True)
            tools_layout.addRow("", self.permission_hint)
            self.permission_combo.currentIndexChanged.connect(self._on_permission_mode_changed)
            self._on_permission_mode_changed(self.permission_combo.currentIndex())

            self.llm_summary_checkbox = QCheckBox("允许使用 LLM 生成会话摘要")
            self.llm_summary_checkbox.setChecked(config.get_memory_summary_llm_enabled())
            self.llm_summary_checkbox.setToolTip(
                "开启后，会话摘要每滚动更新一次（默认每 12 轮）才调用一次基础模型 API，"
                "输入只是「旧摘要 + 新增轮次」，成本很低。关闭、未配置或调用失败时使用本地摘要。"
            )
            tools_layout.addRow(self.llm_summary_checkbox)
            self.llm_summary_hint = QLabel(
                "摘要是一份「话题目录」（默认 1000 字以内），每轮都会附在上下文里，"
                "让模型知道此前聊过什么；需要细节时它自己用 search_memory 检索。"
                "仅影响长期记忆摘要，不会发送完整历史；摘要接口失败时自动回退到本地摘要。"
            )
            self.llm_summary_hint.setWordWrap(True)
            self.llm_summary_hint.setStyleSheet("color: #9aa0a6;")
            tools_layout.addRow("", self.llm_summary_hint)

            self.api_key_edit = self._make_secret_edit(config.get_api_key(), "sk-...")
            base_layout.addRow("API Key:", self.api_key_edit)

            self.base_url_edit = QLineEdit(config.get_base_url())
            self.base_url_edit.setPlaceholderText("https://api.openai.com/v1")
            base_layout.addRow("API Base URL:", self.base_url_edit)

            self.model_edit = QLineEdit(config.get_model())
            self.model_edit.setPlaceholderText("gpt-3.5-turbo")
            base_layout.addRow("模型名称:", self.model_edit)

            self.vision_model_edit = QLineEdit(config.get_vision_model())
            self.vision_model_edit.setPlaceholderText("支持图片输入的视觉模型；留空则使用聊天模型")
            self.vision_model_edit.setToolTip("视觉键鼠实验会使用这个模型分析屏幕截图。")
            base_layout.addRow("视觉模型:", self.vision_model_edit)

            self.reasoning_combo = QComboBox()
            for label, value in REASONING_EFFORT_CHOICES:
                self.reasoning_combo.addItem("关闭（默认）" if not value else label, value)
            current_reasoning = config.get_reasoning_effort()
            reasoning_index = max(0, self.reasoning_combo.findData(current_reasoning))
            self.reasoning_combo.setCurrentIndex(reasoning_index)
            self.reasoning_combo.setToolTip(
                "共七档：关闭 + 低 / 中 / 高 / 极高 / 最高 / 极限。"
                "仅对支持 reasoning_effort 的模型生效，不支持时请关闭。"
            )
            base_layout.addRow("推理强度:", self.reasoning_combo)

            self.searxng_url_edit = QLineEdit(config.get_searxng_url())
            self.searxng_url_edit.setPlaceholderText("http://localhost:18080")
            tools_layout.addRow("SearXNG URL:", self.searxng_url_edit)

            self.tavily_api_key_edit = self._make_secret_edit(config.get_tavily_api_key(), "tvly-...")
            tools_layout.addRow("Tavily Key:", self.tavily_api_key_edit)

            self.amap_api_key_edit = self._make_secret_edit(config.get_amap_api_key(), "高德开放平台 Web服务 Key")
            tools_layout.addRow("高德 Key:", self.amap_api_key_edit)

            tools_layout.addRow(self._make_embedding_group(config))

            self.dsh_bridge_checkbox.toggled.connect(self._apply_dsh_bridge_state)
            self._apply_dsh_bridge_state(self.dsh_bridge_checkbox.isChecked())

        elif section == "prompt":
            # 这一页分两块：上面**只读**的内置部分（角色卡 + 工具/安全规则），
            # 下面**可编辑**的附加提示词。分开的理由是所有权：
            # 内置部分由程序维护、用户删不掉；附加提示词是用户自己的，随便写。
            self.initial_system_prompt_text = str(
                (
                    system_prompt_text
                    if system_prompt_text is not None
                    else config.get_system_prompt()
                )
                or ""
            )
            self.builtin_system_prompt_text = str(builtin_system_prompt_text or "")
            self._prompt_reset_requested = False

            layout.addRow(QLabel("内置部分（角色卡 + 工具/安全规则；只读，程序维护）:"))
            self.builtin_prompt_view = QTextEdit()
            self.builtin_prompt_view.setPlainText(self.builtin_system_prompt_text)
            self.builtin_prompt_view.setReadOnly(True)
            self.builtin_prompt_view.setMinimumHeight(200)
            layout.addRow(self.builtin_prompt_view)

            layout.addRow(QLabel("附加提示词（可编辑；插在角色卡之后、工具规则之前）:"))
            self.system_prompt_edit = QTextEdit()
            self.system_prompt_edit.setPlainText(self.initial_system_prompt_text)
            self.system_prompt_edit.setMinimumHeight(160)
            self.system_prompt_edit.setPlaceholderText(
                "留空即可。这里适合放这一段时间的临时情境 —— 例如角色扮演的剧本、"
                "本次合作的特别约定。长期偏好请写进「常驻块 → 用户自定义」。"
            )
            self.system_prompt_edit.textChanged.connect(self._on_prompt_text_changed)
            layout.addRow(self.system_prompt_edit)

            self.restore_prompt_button = QPushButton("清空附加提示词")
            self.restore_prompt_button.clicked.connect(self._restore_default_system_prompt)
            layout.addRow("", self.restore_prompt_button)
            layout.addRow(
                QLabel("清空它不影响角色卡和上面的内置规则 —— 那些每一轮都会下发，删不掉。")
            )

        elif section == "voice":
            self._secret_initial_values = {
                key: str(config.settings.value(key, "") or "").strip()
                for key in ("xfyun_appid", "xfyun_api_key", "xfyun_api_secret")
            }
            xfyun_group = QGroupBox("语音识别（讯飞）")
            xfyun_layout = QFormLayout(xfyun_group)

            self.xfyun_appid_edit = QLineEdit(config.get_xfyun_appid())
            self.xfyun_appid_edit.setPlaceholderText("6位APPID")
            xfyun_layout.addRow("APPID:", self.xfyun_appid_edit)

            self.xfyun_api_key_edit = self._make_secret_edit(config.get_xfyun_api_key(), "讯飞开放平台 APIKey")
            xfyun_layout.addRow("API Key:", self.xfyun_api_key_edit)

            self.xfyun_api_secret_edit = self._make_secret_edit(config.get_xfyun_api_secret(), "讯飞开放平台 APISecret")
            xfyun_layout.addRow("API Secret:", self.xfyun_api_secret_edit)
            layout.addRow(xfyun_group)

            tts_group = QGroupBox("语音合成")
            tts_layout = QFormLayout(tts_group)
            self.tts_checkbox = QCheckBox("启用语音回复")
            self.tts_checkbox.setChecked(config.get_tts_enabled())
            tts_layout.addRow(self.tts_checkbox)

            self.voice_combo = QComboBox()
            self.voice_combo.setEnabled(config.get_tts_enabled())
            self.tts_checkbox.toggled.connect(self.voice_combo.setEnabled)
            tts_layout.addRow("AI音色:", self.voice_combo)
            layout.addRow(tts_group)

        # ========== 按钮 ==========
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        # ========== 连接信号 ==========
        if section == "voice":
            self.voices_loaded.connect(self.update_voice_combo)

        # ========== 异步加载声音列表 ==========
        if section == "voice":
            QTimer.singleShot(100, self.load_voices_async)

    def _make_tab_page(self):
        """建一个配置 Tab 页，返回 (页控件, 页内 QFormLayout)。

        内容套在 QScrollArea 里：窗口被拖小时出现滚动条，而不是把控件裁掉。
        否则中文 QLabel 在 wordWrap 下的 minimumSizeHint 等于整句宽度
        （中文没有空格，Qt 找不到可断行的「单词」），会把窗口硬顶到 700px 以上。
        """
        page = QWidget()
        page_layout = QFormLayout(page)
        page_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll, page_layout

    def _make_secret_edit(self, value, placeholder):
        edit = QLineEdit(value)
        edit.setPlaceholderText(placeholder)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        return edit

    # ---- 记忆检索（Embedding） -------------------------------------------

    EMBEDDING_BACKEND_CHOICES = (
        ("自动（优先 API，失败回退本地）", "auto"),
        ("仅用 API（不回退，失败则暂停记忆检索）", "api"),
        ("仅本地哈希（离线，无需网络）", "local"),
        ("关闭（不做记忆检索）", "off"),
    )

    def _make_embedding_group(self, config):
        """记忆检索配置区：backend / Base URL / 模型 / 独立 Key / 测试连接。"""
        group = QGroupBox("记忆检索（Embedding）")
        layout = QFormLayout(group)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # 检索方式：pull 与 push 是同一件事的两个档位（本地路径上 pull 一开，
        # push 就让位），所以做成二选一而不是两个勾选框 —— 两个勾选框会诱导用户
        # 都打开，然后困惑"为什么会重复注入"。
        self.memory_mode_combo = QComboBox()
        self.memory_mode_combo.addItem("模型自主查询（推荐）", "pull")
        self.memory_mode_combo.addItem("每轮自动注入（旧行为）", "push")
        current_mode = "pull" if config.get_memory_pull_enabled() else "push"
        self.memory_mode_combo.setCurrentIndex(
            max(0, self.memory_mode_combo.findData(current_mode))
        )
        self.memory_mode_combo.setToolTip(
            "模型自主查询：把历史检索做成 search_memory 工具，由模型按需调用，不设相关度门槛。\n"
            "　• 本地对话：不再每轮自动注入检索结果（同一段历史不重复灌两遍）。\n"
            "　• DSH 桥接：同一个开关，自动注入也让位 —— 桥上的历史细节改由模型自己调工具取。\n"
            "　　　代价（两条路一样）：模型若没想到要查，这一轮就没有历史细节。\n"
            "每轮自动注入：改动前的旧行为，按相关度门槛（0.55 / 0.7）自动注入；\n"
            "　　　两条路都每轮带一段历史，稳定，但内容多、可能答非所问。"
        )
        layout.addRow("检索方式:", self.memory_mode_combo)

        self.memory_mode_hint = QLabel(
            "这个开关同时作用于本地对话和 DSH 桥接，两边行为保持一致。\n"
            "选「模型自主查询」会多出一次工具往返；切回「每轮自动注入」即完全恢复旧行为。"
        )
        self.memory_mode_hint.setWordWrap(True)
        self.memory_mode_hint.setStyleSheet("color: #9aa0a6;")
        layout.addRow("", self.memory_mode_hint)

        self.embedding_backend_combo = QComboBox()
        for label, value in self.EMBEDDING_BACKEND_CHOICES:
            self.embedding_backend_combo.addItem(label, value)
        current_backend = config.get_memory_embedding_backend()
        self.embedding_backend_combo.setCurrentIndex(
            max(0, self.embedding_backend_combo.findData(current_backend))
        )
        self.embedding_backend_combo.setToolTip(
            "「仅本地哈希」不需要任何网络与模型，但只做字面相似度匹配；\n"
            "要获得真正的语义检索，请用「自动」并填写下面支持 /embeddings 的地址。"
        )
        layout.addRow("后端:", self.embedding_backend_combo)

        self.embedding_base_url_edit = QLineEdit(config.get_memory_embedding_base_url())
        self.embedding_base_url_edit.setPlaceholderText("http://localhost:11434/v1  或  https://api.siliconflow.cn/v1")
        self.embedding_base_url_edit.setToolTip(
            "必须指向提供 OpenAI 兼容 /embeddings 接口的地址，通常要带 /v1 后缀。\n"
            "留空表示回落到「基础模型」页的 API Base URL。"
        )
        layout.addRow("Base URL:", self.embedding_base_url_edit)

        self.embedding_model_edit = QLineEdit(config.get_memory_embedding_model())
        self.embedding_model_edit.setPlaceholderText("bge-m3  或  BAAI/bge-m3")
        self.embedding_model_edit.setToolTip(
            "Ollama 本地填 bge-m3；硅基流动填 BAAI/bge-m3。留空使用内置默认模型名。"
        )
        layout.addRow("模型:", self.embedding_model_edit)

        self.embedding_api_key_edit = self._make_secret_edit(
            config.get_memory_embedding_api_key(),
            "留空 = 复用聊天 API Key（本地 Ollama 可留空）",
        )
        self.embedding_api_key_edit.setToolTip(
            "单独给 Embedding 用另一个平台的 Key。\n"
            "本地 Ollama 无鉴权，可以留空并忽略聊天 Key。"
        )
        layout.addRow("API Key:", self.embedding_api_key_edit)

        self.embedding_probe_button = QPushButton("测试连接")
        self.embedding_probe_button.setToolTip("只发一次极小的探测请求，不会索引任何数据。")
        self.embedding_probe_button.clicked.connect(self._probe_embedding)
        layout.addRow("", self.embedding_probe_button)

        self.embedding_probe_status = QLabel("尚未测试。")
        self.embedding_probe_status.setWordWrap(True)
        self.embedding_probe_status.setStyleSheet("color: #9aa0a6;")
        layout.addRow("", self.embedding_probe_status)

        self.embedding_probe_finished.connect(self._on_embedding_probe_finished)
        self.embedding_backend_combo.currentIndexChanged.connect(self._apply_embedding_state)
        self._apply_embedding_state()
        return group

    def _apply_embedding_state(self):
        """「仅本地哈希 / 关闭」时灰掉网络字段，避免填了以为生效。"""
        backend = self.embedding_backend_combo.currentData()
        needs_api = backend in ("auto", "api")
        for widget in (
            self.embedding_base_url_edit,
            self.embedding_model_edit,
            self.embedding_api_key_edit,
            self.embedding_probe_button,
        ):
            widget.setEnabled(needs_api)
        if not needs_api:
            self.embedding_probe_status.setText(
                "当前后端不调用 API，无需测试连接。"
            )
            self.embedding_probe_status.setStyleSheet("color: #9aa0a6;")

    def _probe_embedding(self):
        self.embedding_probe_button.setEnabled(False)
        self.embedding_probe_status.setText("正在测试…")
        self.embedding_probe_status.setStyleSheet("color: #9aa0a6;")
        # 在 GUI 线程取好控件值，再把纯数据交给后台线程：Qt 控件不能跨线程读。
        snapshot = StaticEmbeddingConfig(
            backend=self.embedding_backend_combo.currentData(),
            base_url=self.embedding_base_url_edit.text().strip(),
            model=self.embedding_model_edit.text().strip(),
            api_key=self.embedding_api_key_edit.text().strip(),
        )
        try:
            thread_registry.spawn(
                lambda: self._run_embedding_probe(snapshot),
                name="embedding-probe",
                group="probe_fetch",
            )
        except Exception as exc:
            self.embedding_probe_finished.emit(False, f"探测任务未能启动：{exc}")

    def _run_embedding_probe(self, snapshot):
        from memory_embeddings import EmbeddingService

        try:
            result = EmbeddingService(snapshot).probe_api()
            ok = bool(result.get("ok"))
            message = str(result.get("message") or "")
        except Exception as exc:
            ok, message = False, f"探测异常：{exc}"
        self.embedding_probe_finished.emit(ok, message)

    def _on_embedding_probe_finished(self, ok, message):
        self.embedding_probe_button.setEnabled(True)
        self.embedding_probe_status.setText(message)
        self.embedding_probe_status.setStyleSheet(
            "color: #7ee787;" if ok else "color: #ff7b7b;"
        )

    def load_voices_async(self):
        """在后台线程获取声音列表，完成后发射信号"""
        def fetch():
            try:
                import asyncio
                import edge_tts
                voices = asyncio.run(edge_tts.list_voices())
                # 提取中文声音（ShortName 包含 zh-CN）
                chinese_voices = [v['ShortName'] for v in voices if 'zh-CN' in v.get('ShortName', '')]
                if not chinese_voices:
                    chinese_voices = ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"]
            except Exception as e:
                print(f"获取声音列表失败: {e}")
                chinese_voices = ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"]
            # 发射信号（线程安全）
            self.voices_loaded.emit(chinese_voices)

        # 交给注册表：对话框可能先于抓取结束就关闭，注册表持有的强引用
        # 保证线程不会在运行中被回收，关窗时也能统一收尾。
        thread_registry.spawn(fetch, name="AissistantVoiceFetch", group="probe_fetch")

    def update_voice_combo(self, voice_list):
        """主线程中更新下拉框"""
        self.voice_combo.clear()
        current = self.config.get_tts_voice()
        idx = 0
        for i, v in enumerate(voice_list):
            self.voice_combo.addItem(v, v)
            if v == current:
                idx = i
        self.voice_combo.setCurrentIndex(idx)

    def _on_prompt_text_changed(self):
        self._prompt_reset_requested = False

    def _restore_default_system_prompt(self):
        """清空附加提示词。

        不是"恢复内置提示词"—— 内置部分本来就在，而且删不掉，没有"恢复"这回事。
        """
        self._prompt_reset_requested = True
        self.system_prompt_edit.blockSignals(True)
        self.system_prompt_edit.setPlainText("")
        self.system_prompt_edit.blockSignals(False)


    def _on_permission_mode_changed(self, index):
        """权限模式提示随选择变色：完全权限用红色警告，只读用蓝色说明。"""
        value = str(self.permission_combo.itemData(index) or DEFAULT_PERMISSION_MODE)
        self.permission_hint.setText(PERMISSION_MODE_DESCRIPTIONS.get(value, ""))
        if value == FULL_ACCESS_MODE:
            self.permission_hint.setStyleSheet(
                "color: #ff7b7b; font-weight: bold; background-color: #3a1d1d;"
                " border: 1px solid #d94848; border-radius: 6px; padding: 6px 8px;"
            )
        elif value == READ_ONLY_MODE:
            self.permission_hint.setStyleSheet(
                "color: #7fb2ff; background-color: #1b2735;"
                " border: 1px solid #3d6ea8; border-radius: 6px; padding: 6px 8px;"
            )
        else:
            self.permission_hint.setStyleSheet(
                "color: #9aa0a6; background-color: #23262a;"
                " border: 1px solid #34383d; border-radius: 6px; padding: 6px 8px;"
            )

    def accept(self):
        if self.section == "config":
            self._save_secret_override("api_key", self.api_key_edit)
            self.config.set_base_url(self.base_url_edit.text().strip())
            self.config.set_model(self.model_edit.text().strip())
            self.config.set_vision_model(self.vision_model_edit.text().strip())
            self.config.set_reasoning_effort(self.reasoning_combo.currentData())
            self.config.set_searxng_url(self.searxng_url_edit.text().strip())
            self._save_secret_override("tavily_api_key", self.tavily_api_key_edit)
            self._save_secret_override("amap_api_key", self.amap_api_key_edit)
            self.config.set_dsh_bridge_enabled(self.dsh_bridge_checkbox.isChecked())
            self.config.set_permission_mode(self.permission_combo.currentData())
            self.config.set_memory_summary_llm_enabled(self.llm_summary_checkbox.isChecked())
            self.config.set_memory_pull_enabled(self.memory_mode_combo.currentData() == "pull")
            self.config.set_memory_embedding_backend(self.embedding_backend_combo.currentData())
            self.config.set_memory_embedding_base_url(self.embedding_base_url_edit.text().strip())
            self.config.set_memory_embedding_model(self.embedding_model_edit.text().strip())
            self._save_secret_override("memory_embedding_api_key", self.embedding_api_key_edit)
            self._save_dsh_selection()
        elif self.section == "prompt":
            current_prompt = self.system_prompt_edit.toPlainText()
            if self._prompt_reset_requested or not current_prompt.strip():
                # 清空 → default（角色卡 + 硬规则仍在），**不是** none。
                self.config.reset_system_prompt()
            elif current_prompt != self.initial_system_prompt_text:
                self.config.set_system_prompt(current_prompt)
        elif self.section == "voice":
            self._save_secret_override("xfyun_appid", self.xfyun_appid_edit)
            self._save_secret_override("xfyun_api_key", self.xfyun_api_key_edit)
            self._save_secret_override("xfyun_api_secret", self.xfyun_api_secret_edit)
            self.config.set_tts_enabled(self.tts_checkbox.isChecked())
            self.config.set_tts_voice(self.voice_combo.currentData())
        super().accept()


    def _save_secret_override(self, key, edit):
        value = edit.text().strip()
        initial_local = self._secret_initial_values.get(key, "")
        if not initial_local and value == self._effective_secret_value(key):
            return
        if value:
            self.config.settings.setValue(key, value)
        elif initial_local:
            self.config.settings.remove(key)

    def _effective_secret_value(self, key):
        getters = {
            "api_key": self.config.get_api_key,
            "tavily_api_key": self.config.get_tavily_api_key,
            "amap_api_key": self.config.get_amap_api_key,
            "memory_embedding_api_key": self.config.get_memory_embedding_api_key,
            "xfyun_appid": self.config.get_xfyun_appid,
            "xfyun_api_key": self.config.get_xfyun_api_key,
            "xfyun_api_secret": self.config.get_xfyun_api_secret,
        }
        return str(getters[key]() or "").strip()


# Bind the moved methods back (identical descriptor semantics).
ConfigDialog._fill_dsh_reasoning_combo = _fill_dsh_reasoning_combo
ConfigDialog._current_dsh_reasoning_choice = _current_dsh_reasoning_choice
ConfigDialog._sync_dsh_provider_from_model = _sync_dsh_provider_from_model
ConfigDialog._selected_dsh_model = _selected_dsh_model
ConfigDialog._schedule_dsh_options_probe = _schedule_dsh_options_probe
ConfigDialog._probe_dsh_options = _probe_dsh_options
ConfigDialog._on_dsh_probe_thread_finished = _on_dsh_probe_thread_finished
ConfigDialog._on_dsh_options_probed = _on_dsh_options_probed
ConfigDialog._restore_cached_dsh_options = _restore_cached_dsh_options
ConfigDialog._apply_dsh_options = _apply_dsh_options
ConfigDialog._apply_dsh_bridge_state = _apply_dsh_bridge_state
ConfigDialog._save_dsh_selection = _save_dsh_selection
ConfigDialog.build_core_memory_tab = build_core_memory_tab
ConfigDialog.open_core_memory_dialog = open_core_memory_dialog
