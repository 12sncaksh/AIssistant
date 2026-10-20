# main.py
# 桌面AI助手主程序 - 支持API调用、Live2D模型展示、会话管理、语音合成(PyQt6版本)
import sys
import os

# 必须最早执行：之后启动的 MCP server / DSH 桥接 / 工具命令都不再弹控制台窗口。
# 详见 src/no_console_window.py（MCP server 由第三方库内部 spawn，无法逐点传参）。
from no_console_window import install as _install_no_console_window

_install_no_console_window()

# 调试模式：`python src/main.py --debug` 打开（run_debug.bat 会带上）。
# 定义已移到 app_flags，供拆分出去的模块共用，避免反向导入 main。
from app_flags import DEBUG_MODE  # noqa: F401
import thread_registry
from memory_runtime import get_memory_runtime



# ---- remaining MainWindow method groups ----
from chat_scroll_actions import (  # noqa: F401
    _is_chat_at_bottom,
    _set_auto_follow_output,
    _suppress_chat_scroll_events,
    _on_chat_scroll_changed,
    _position_latest_output_bar,
    _return_to_latest_output,
    _follow_latest_output,
    _scroll_latest_output_if_enabled,
)
from prompt_actions import (  # noqa: F401
    _generate_dynamic_system_prompt,
    _format_assistant_text,
    _get_default_system_prompt,
    _get_permission_mode_notice,
    _get_effective_system_prompt,
    _build_system_prompt_messages,
    load_system_prompt,
    refresh_action_handler_config,
)
from session_flow_actions import (  # noqa: F401
    load_history_from_db,
    _history_page_size,
    _create_history_widgets,
    reset_history_pagination,
    load_earlier_history,
    _maybe_load_earlier_history,
    _get_session_task_state,
    _set_session_task_state,
    _restore_input_controls,
    _discard_current_generation,
    _show_temporary_session,
    _create_task_placeholder,
    _restore_running_session_ui,
    _on_session_thread_finished,
    _stop_session_thread,
    _finish_background_reply,
    _placeholder_row,
    _on_status_update,
    _on_task_state_updated,
    _on_computer_state,
    _sync_pet_task_activity,
    _build_world_cup_clarification,
    _evaluate_clarification_need,
    clear_conversation,
    new_session,
    switch_session,
)
from message_actions import (  # noqa: F401
    add_message,
    _create_message_widget,
    _track_history_row,
    _build_api_messages,
    _fallback_api_messages,
    send_message,
    on_process_chunk,
    on_stream_chunk,
    _stop_generation,
    on_generation_stopped,
    on_response_complete,
    raise_permission_dialog,
    on_api_error,
)
from voice_actions import (  # noqa: F401
    _start_wakeword_listener,
    _on_wakeword_detected,
    _speak_wake_ack,
    _toggle_voice_input,
    _start_voice_input,
    _on_stt_started,
    _on_stt_partial,
    _on_stt_final,
    _on_stt_canceled,
    _on_stt_error,
    _stop_voice_input,
    _stop_tts,
)
from attachment_actions import (  # noqa: F401
    _attach_file,
    _attach_screenshot,
    _open_screenshot_overlay,
    _hide_floating_windows,
    _restore_floating_windows,
    _restore_after_screenshot,
    _on_screenshot_finished,
    _on_image_pasted,
    _attach_images,
    _update_attach_label,
    _clear_attachments,
)
from pet_actions import (  # noqa: F401
    bind_pet_window,
    _apply_pet_command,
    _sync_kaomoji_to_pet,
    _set_kaomoji_display,
    _live2d_page_url,
    set_pet_model,
    init_live2d_server,
    _publish_pet_endpoint,
    reload_live2d_model,
    open_model_directory,
    _update_kaomoji_by_text,
    _show_thinking_kaomoji,
)
from motto_actions import (  # noqa: F401
    fetch_motto,
    on_motto_reply,
    use_fallback_motto,
)
from permission_actions import (  # noqa: F401
    _update_model_indicator,
    _update_permission_button,
    _resize_composer_input,
    _update_mode_button,
    set_permission_mode,
    _is_turn_running,
    _permission_change_blocked,
    _sync_permission_action_enabled,
    _build_permission_actions,
    _refresh_permission_mode_actions,
    _set_quick_mode,
    _set_quick_reasoning,
    _update_reasoning_button,
)
from config_actions import (  # noqa: F401
    show_config_dialog,
    show_prompt_dialog,
    show_voice_config_dialog,
    _rebuild_role_menu,
    show_role_manager_dialog,
    switch_role,
)

# ---- MainWindow method groups moved into their own modules ----
from main_window_helpers import (  # noqa: F401
    _session_group,
    _session_indicator_color,
    _resolve_world_cup_scope,
    _needs_world_cup_clarification,
    _compact_user_message,
    _screenshot_to_attachment,
    _is_text_file,
)
from mcp_manager_dialog import (  # noqa: F401
    show_mcp_manager_dialog,
)
from pet_dialogs import (  # noqa: F401
    _get_selected_live2d_model,
    _refresh_pet_param_catalog,
    show_pet_model_dialog,
    show_pet_wear_dialog,
    show_pet_emotion_dialog,
    show_pet_scale_dialog,
)
from appearance_actions import (  # noqa: F401
    update_all_bubbles_opacity,
    update_all_code_opacity,
    update_all_chat_colors,
    show_opacity_dialog,
    show_code_opacity_dialog,
    update_all_avatar_widgets,
    show_avatar_dialog,
    update_all_text_font_widgets,
    show_chat_font_dialog,
    show_code_font_dialog,
    refresh_all_message_widgets,
    _read_chat_appearance_state,
    _apply_chat_appearance_state,
    preview_chat_appearance,
    _save_chat_appearance_state,
    show_chat_appearance_dialog,
    _refresh_background_mode_actions,
    set_chat_background_mode,
    choose_chat_background,
    set_chat_background,
    reset_chat_background,
    _resolve_saved_chat_background,
    _apply_chat_background,
)
from theme_actions import (  # noqa: F401
    _apply_ui_theme,
    _refresh_theme_actions,
    set_ui_theme,
    _style_latest_output_bar,
)
from session_panel_actions import (  # noqa: F401
    _get_latest_session_or_new,
    refresh_session_list,
    _session_indicator_state,
    _update_session_list_entry,
    _session_indicator_icon,
    _advance_session_breath,
    _set_session_notice,
    _on_session_item_activated,
    _session_id_at,
    _show_session_context_menu,
    _rename_session,
    _delete_session_from_panel,
    _delete_session,
    _on_session_splitter_moved,
    toggle_session_panel,
    show_session_list,
)
from help_dialogs import (  # noqa: F401
    show_speech_config_help,
    show_config_file_help,
    show_about,
)

# ---- re-exported from the modules split out of this file ----
from app_theme import (  # noqa: F401
    UI_THEMES,
    UI_THEME_PALETTES,
    normalize_ui_theme,
    get_ui_theme_palette,
    build_ui_palette,
    build_scrollbar_stylesheet,
    style_popup_menu,
    apply_native_window_frame_theme,
    get_app_icon,
)
from live2d_catalog import (  # noqa: F401
    PET_EMOTION_EXPRESSIONS,
    get_live2d_model_catalog,
    get_live2d_param_catalog,
    _live2d_model_folder,
    _read_exp3_entries,
    load_live2d_emotion_config,
    _scan_exp3_files,
    _config_explicit_params,
    get_live2d_expression_params,
    get_live2d_all_expressions,
    get_live2d_model_expression_names,
    _is_wearable_prop,
    build_live2d_emotion_template,
    merge_live2d_emotion_config,
)
from app_prompts import (  # noqa: F401
    WAKE_ACK_SPEECH,
    DATETIME_FORCE_KEYWORDS,
    WORLD_CUP_STATUS_KEYWORDS,
    WORLD_CUP_SCOPE_ALIASES,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_ROLES,
    normalize_roles,
    SYSTEM_SAFETY_RULES,
)
from subagent_config import (  # noqa: F401
    SubagentRoundsExhausted,
    SUBAGENT_MAX_ROUNDS,
    SUBAGENT_TIMEOUT_SECONDS,
    SUBAGENT_TIMEOUT_SECONDS_BY_TYPE,
    SUBAGENT_WRAPUP_MARGIN_SECONDS,
    SUBAGENT_TOTAL_BUDGET_SECONDS,
    SUBAGENT_TOTAL_BUDGET_BY_TYPE,
    SUBAGENT_MAX_RESULT_LENGTH,
    SUBAGENT_PROMPTS,
)
from app_logging import (  # noqa: F401
    _CRASH_LOG_HANDLE,
    setup_logging,
    _log_safe_args,
)
from reasoning_options import (  # noqa: F401
    REASONING_EFFORT_CHOICES,
    REASONING_EFFORT_VALUES,
    REASONING_EFFORT_ORDER,
    REASONING_EFFORT_LABELS,
    REASONING_EFFORT_ERROR_HINTS,
    DSH_REASONING_CHOICES,
    reasoning_effort_rank,
    reasoning_effort_label,
    reasoning_effort_error,
    error_detail_head,
    TOOLS_UNSUPPORTED_PATTERN,
    tools_unsupported_error,
)
from permission_modes import (  # noqa: F401
    PERMISSION_MODE_CHOICES,
    PERMISSION_MODE_VALUES,
    PERMISSION_MODE_LABELS,
    PERMISSION_MODE_DESCRIPTIONS,
    DEFAULT_PERMISSION_MODE,
    FULL_ACCESS_MODE,
    READ_ONLY_MODE,
    PERMISSION_MODE_RANK,
    PERMISSION_MODE_BUSY_MESSAGE,
    READ_ONLY_PROMPT_NOTICE,
    FULL_ACCESS_PROMPT_NOTICE,
    API_RESPONSE_MAX_CHARS,
)
from config_manager import (  # noqa: F401
    ConfigManager,
)
from api_thread import (  # noqa: F401
    APICallCancelled,
    ReasoningEffortUnsupported,
    APICallThread,
)
from role_dialog import (  # noqa: F401
    RoleManagerDialog,
    _LIVE_PROBE_THREADS,
    _track_probe_thread,
)
from config_dialog import (  # noqa: F401
    ConfigDialog,
)
from pet_widgets import (  # noqa: F401
    PetWebView,
    PetInteractionOverlay,
    PetSpeechBubble,
)
from pet_scripts import (  # noqa: F401
    PET_DRAG_STAGE_ENABLED,
    PET_DRAG_FALL_MS,
    PET_DRAG_RECOVER_MS,
    PET_DRAG_RESTORE_MS,
    PET_MOUTH_TICK_MS,
    PET_MOUTH_HOLD_MS,
    PET_DRAG_STAGE_SCRIPT,
    PET_ACTIVITY_ENABLED,
    PET_ACTIVITY_DONE_MS,
    PET_ACTIVITY_HINT_MS,
    PET_ACTIVITY_HINT_MIN_GAP,
    PET_ACTIVITY_SCRIPT,
    PET_PARAM_ENABLED,
    PET_PARAM_DEFAULT_MS,
    PET_PARAM_MIN_MS,
    PET_PARAM_MAX_MS,
    PET_EMOTION_NAMES,
    PET_PARAM_SCRIPT,
)
from desktop_pet_window import (  # noqa: F401
    DesktopPetWindow,
)
from chat_input_widgets import (  # noqa: F401
    ChatInputTextEdit,
)
from screen_overlays import (  # noqa: F401
    SCREENSHOT_FALLBACK_MAX_SIDE,
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_MB,
    ScreenCaptureOverlay,
    ComputerControlOverlay,
)
import json
import copy
import html
import threading
import time
import webbrowser
import tempfile
import asyncio
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import random
import re
import mimetypes
import traceback
import faulthandler
import requests
import edge_tts
import pygame
import uuid
import math
from urllib.parse import quote
import queue
import base64
import logging
from contextlib import contextmanager
from functools import partial
from logging.handlers import RotatingFileHandler

from action_control import ActionHandler  # 导入外部应用控制模块
from mcp_client import MCPManager  # MCP 工具（工具表合并 + 调用分发 + 自带 server 配置）
from vision_input import VisionInputController
from dsh_acp_client import (  # DSH（DeepSeek Harness）桥接
    current_connection,
    reset_connection,
    config_option_choices,
    find_config_option,
    parse_model_selection_value,
    MODEL_OPTION_CATEGORY,
    REASONING_OPTION_CATEGORY,
)
from dsh_session_thread import DSHSessionThread
from dsh_options_cache import (
    OptionsCacheStore,
    default_options_cache_path,
    options_cache_key,
    dsh_options_cache_scope,
)
from chat_list_widget import SmoothChatListWidget
from chat_widgets import ClickableAvatarLabel, ResizableBackgroundPanel
from chat_style import (
    CHAT_LINE_HEIGHT_PERCENT,
    CHAT_LINE_HEIGHT_STYLE_SHEET,
    DEFAULT_BUBBLE_ASSISTANT_COLOR,
    DEFAULT_BUBBLE_USER_COLOR,
    apply_plain_text_line_height,
    chat_font_style,
    clamp_chat_font_size,
    clamp_opacity,
    color_to_rgba,
    contrast_secondary_text_color,
    contrast_text_color,
    make_circular_avatar,
    normalize_hex_color,
)
from task_progress_widget import TaskProgressWidget
from app_paths import (
    get_avatar_storage_dir,
    get_config_dir,
    get_default_avatar_path,
    get_resource_path,
    get_runtime_dir,
    resolve_avatar_path,
    serialize_avatar_path,
)
from chat_markdown import (
    DEFAULT_CODE_BLOCK_COLOR,
    INTERNAL_REASONING_BLOCK_PATTERN,
    INTERNAL_REASONING_CLOSE_PATTERN,
    INTERNAL_REASONING_OPEN_PATTERN,
    MARKDOWN_FENCE_PATTERN,
    attach_sources_to_text,
    build_sources_block,
    markdown_to_plain_text,
    pet_bubble_text,
    render_markdown_to_html,
    strip_internal_reasoning,
)
from chat_message_widget import ChatMessageWidget
from text_protocol import (
    DSML_INVOKE_PATTERN,
    DSML_PARAMETER_PATTERN,
    DSML_TAG_PATTERN,
    KAOMOJI_BROKEN_CLOSING_PATTERN,
    KAOMOJI_TAG_CLEANUP_PATTERN,
    KAOMOJI_TAG_PATTERN,
    TEXT_TOOL_CALL_MARKER_PATTERN,
    KaomojiStreamFilter,
    PetSpeechStreamFilter,
    extract_kaomoji_tag,
    parse_sources_block,
    parse_text_tool_calls,
    sanitize_reply_text,
    split_reply_and_sources,
    strip_text_tool_protocol,
)
from live2d_server import Live2DRequestHandler, LocalHTTPServer
from avatar_tools import (
    AVATAR_MAX_FILE_BYTES,
    AVATAR_MAX_SOURCE_SIDE,
    AVATAR_OUTPUT_SIDE,
    load_avatar_source_pixmap,
    save_avatar_pixmap,
)
from appearance_dialogs import (
    CHAT_BG_MAX_SIDE,
    AvatarSettingsDialog,
    ChatAppearanceDialog,
    CropImageLabel,
    ImageCropDialog,
    SimpleFontDialog,
    limit_pixmap_side,
)
from message_database import MessageDatabase
from config_storage import CredentialConfigMixin
from tts_thread import TTS_AUDIO_LOCK, TextToSpeechThread
from voice_threads import HAS_AUDIO_DEPS, WakeWordThread, XfyunSTTThread
from dsh_options_probe import DshOptionsProbeThread
from task_state import (
    append_task_tool_result,
    create_task_state,
    now_iso_timestamp,
    touch_task_state,
    upsert_task_plan_step,
)
from version import __version__ as APP_VERSION
import json
# PyQt6 导入
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtCore import QUrl
from PyQt6.QtCore import (
    QThread, pyqtSignal, pyqtSlot, QUrl, QSettings, Qt, QTimer, QSize,
    QPointF, QEvent, QPoint, QRect, QRectF, QPropertyAnimation, QEasingCurve,
    QAbstractAnimation,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QSlider,
    QDialogButtonBox,
    QLabel, QSplitter, QMessageBox, QDialog, QFormLayout, QDialogButtonBox,QFileDialog, QInputDialog, QListView, QRubberBand,
    QMenuBar, QMenu, QStatusBar, QSizePolicy, QGroupBox, QComboBox, QCheckBox, QTextBrowser, QAbstractItemView, QProgressBar,
    QToolButton, QStackedWidget, QFrame, QStyle, QTabWidget, QScrollArea,
    QSystemTrayIcon, QFontComboBox, QSpinBox, QColorDialog,
)
from PyQt6.QtGui import (
    QTextOption, QIcon, QPixmap, QImage, QImageReader, QTextDocument,
    QPainter, QColor, QFont, QLinearGradient, QPainterPath, QPen, QPalette, QActionGroup,
    QTextCursor, QTextBlockFormat, QCursor,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtCore import QMetaObject   # 如果你保留 invokeMethod 方式则需要，改用信号则不需要
from PyQt6.QtGui import QPixmap, QFontMetrics


class MainWindow(QMainWindow):
    pet_command_requested = pyqtSignal(object)

    def __init__(self, show_live2d_panel=True, managed_by_pet=False):
        super().__init__()
        self.setWindowIcon(get_app_icon())
        self.show_live2d_panel = show_live2d_panel
        self.managed_by_pet = managed_by_pet
        self.allow_close = not managed_by_pet
        self.pet_window = None
        self.pet_command_requested.connect(self._apply_pet_command)
        ActionHandler.set_pet_command_callback(self.pet_command_requested.emit)
        self.config = ConfigManager()
        self.db = MessageDatabase()
        ActionHandler.load_apps()
        self.refresh_action_handler_config()
        try:
            MCPManager.ensure_builtin_servers()
        except Exception:
            logging.warning("自动配置自带 MCP server 失败", exc_info=True)
        try:
            MCPManager.ensure_started()
        except Exception:
            logging.warning("应用启动时启动 MCP server 失败", exc_info=True)
        print(f"已加载应用: {ActionHandler._apps}")

        self.roles = self.config.get_roles()
        saved_role_name = str(self.config.settings.value("last_role", "") or "")
        self.current_role_name = saved_role_name if saved_role_name in self.roles else next(iter(self.roles), "")
        self.runtime_system_prompt = self._get_effective_system_prompt()
        self.session_is_persisted = False
        self.current_session_id = self._get_latest_session_or_new()
        self.conversation_history = []
        self.session_task_states = {}
        self.current_task_state = None
        self.current_api_thread = None
        self.session_threads = {}
        self.session_stream = {}
        self.session_notices = {}
        self._session_row_items = {}
        # 会话历史分页账本：完整历史留在 conversation_history 与记忆索引里，
        # 界面只创建最近一页；_first_visible_index 指向已渲染的第一条。
        self.session_history_rows = []
        self._first_visible_index = 0
        self._loading_earlier = False
        # 本地语义记忆（embedding + 向量索引 + 摘要）。初始化失败不影响聊天，
        # 只会退化成「最近轮次」上下文。
        self._memory = None
        try:
            self._memory = get_memory_runtime(self.config, self.db)
            self._memory.start()
        except Exception:
            logging.warning("初始化本地语义记忆失败，将只使用最近轮次上下文", exc_info=True)
            self._memory = None
        # 会话状态提示：状态画在条目右侧的固定状态区，标题文本永不被状态改写，
        # 因此不会有任何宽度变化或左右位移；运行中只做低频颜色呼吸，不改变字符。
        self._session_indicator_icon_size = 18
        self._session_breath_phase = 0
        self._session_breath_timer = QTimer(self)
        self._session_breath_timer.setInterval(180)
        self._session_breath_timer.timeout.connect(self._advance_session_breath)
        self._permission_dialog = None
        self._pending_permission = None
        self.computer_overlay = ComputerControlOverlay()
        self.live2d_server = None
        self.stream_buffer = ""
        self.stream_display_buffer = ""
        self._auto_follow_output = True
        self._programmatic_chat_scroll = 0  # 计数器：>0 时忽略滚动条信号
        self._auto_follow_scroll_timer = QTimer(self)
        self._auto_follow_scroll_timer.setSingleShot(True)
        self._auto_follow_scroll_timer.timeout.connect(self._scroll_latest_output_if_enabled)
        self.chat_stream_filter = KaomojiStreamFilter()

        self.bubble_opacity = self.config.get_bubble_opacity()
        self.code_opacity = self.config.get_code_opacity()
        self.background_opacity = self.config.get_chat_background_opacity()
        self.bubble_user_color = self.config.get_bubble_user_color()
        self.bubble_assistant_color = self.config.get_bubble_assistant_color()
        self.code_block_color = self.config.get_code_block_color()
        self.chat_background_mode = self.config.get_chat_background_mode()
        self.chat_font_family = self.config.get_chat_font_family()
        self.chat_font_size = self.config.get_chat_font_size()
        self.code_font_family = self.config.get_code_font_family()
        self.code_font_size = self.config.get_code_font_size()
        self.tts_thread = None
        self._tts_threads = []
        self.stt_thread = None
        self.wakeword_thread = None
        # 唤醒应答播完后要自动开始拾音（播报期间开麦会把应答本身录进去）。
        # 记线程对象而不是布尔标记：应答若被别处 _stop_tts() 打断，标记会漏清，
        # 结果变成下一次回复播完才突然开始录音。
        self._pending_voice_input_thread = None
        self._pending_attachments = []
        self._pending_image_attachments = []
        self._screenshot_overlay = None
        self._hidden_floating = []


        self.init_ui()
        self.setWindowTitle("AI桌面助手 - 加载一言中...")
        # 每日一言网络获取
        self.nam = QNetworkAccessManager(self)
        self.nam.finished.connect(self.on_motto_reply)
        self.fetch_motto()  # 立即获取一次
        # 每隔 1 小时刷新一次（3600000 毫秒）
        self.motto_timer = QTimer(self)
        self.motto_timer.timeout.connect(self.fetch_motto)
        self.motto_timer.start(3600000)
        self.init_menu()
        self.init_live2d_server()
        self._apply_chat_background(self._resolve_saved_chat_background())
        self.load_system_prompt()
        self.load_history_from_db()
        # 启动唤醒词常驻监听（麦克风）
        QTimer.singleShot(0, self._start_wakeword_listener)


    def init_ui(self):
        self.setWindowTitle("AI桌面助手 - 支持Live2D模型")
        self.setMinimumSize(1000, 700)
        self.ui_theme = normalize_ui_theme(self.config.get_ui_theme())

        central = QWidget()
        central.setObjectName("mainCentral")
        self.central_widget = central
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(0)

        self.session_panel = QWidget()
        self.session_panel.setObjectName("sessionPanel")
        self.session_panel.setMinimumWidth(220)
        self.session_panel.setMaximumWidth(380)
        session_layout = QVBoxLayout(self.session_panel)
        session_layout.setContentsMargins(12, 12, 12, 12)
        session_layout.setSpacing(10)

        brand_layout = QHBoxLayout()
        brand_layout.setSpacing(9)
        brand_icon = QLabel()
        app_icon = get_app_icon().pixmap(QSize(28, 28))
        if not app_icon.isNull():
            brand_icon.setPixmap(app_icon)
        brand_icon.setFixedSize(28, 28)
        brand_layout.addWidget(brand_icon)
        self.brand_title = QLabel("Aissistant")
        brand_layout.addWidget(self.brand_title)
        brand_layout.addStretch(1)
        session_layout.addLayout(brand_layout)

        session_header = QHBoxLayout()
        self.session_label = QLabel("会话")
        session_header.addWidget(self.session_label)
        session_header.addStretch(1)
        session_layout.addLayout(session_header)
        self.session_search = QLineEdit()
        self.session_search.setPlaceholderText("搜索会话")
        self.session_search.textChanged.connect(self.refresh_session_list)
        session_layout.addWidget(self.session_search)
        self.session_list = QListWidget()
        self.session_list.setSpacing(2)
        # 固定图标尺寸，使图标列宽度可预测（否则 iconSize 为 -1x-1，热区无法准确计算）
        self.session_list.setIconSize(QSize(self._session_indicator_icon_size, self._session_indicator_icon_size))
        self.session_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._show_session_context_menu)
        self.session_list.itemClicked.connect(self._on_session_item_activated)
        session_layout.addWidget(self.session_list, 1)

        self.session_separator = QWidget()
        self.session_separator.setFixedHeight(1)
        session_layout.addWidget(self.session_separator)
        self.new_session_bottom_button = QPushButton("＋  新对话")
        self.new_session_bottom_button.setMinimumHeight(40)
        self.new_session_bottom_button.clicked.connect(self.new_session)
        session_layout.addWidget(self.new_session_bottom_button)

        # 会话栏右侧的内容区，背景只绘制在这里，不覆盖会话栏
        self.content_panel = QWidget()
        self.content_panel.setObjectName("contentPanel")
        content_layout = QHBoxLayout(self.content_panel)
        content_layout.setContentsMargins(14, 14, 14, 14)
        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        content_splitter.setStyleSheet(
            "QSplitter { background: transparent; } QSplitter::handle { background: transparent; }"
        )

        # 左侧聊天区域：自定义背景由面板绘制，消息列表透明叠加在其上。
        self.chat_background_panel = ResizableBackgroundPanel()
        left = self.chat_background_panel
        left.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.chat_list = SmoothChatListWidget()
        self.chat_list.setUniformItemSizes(False)
        self.chat_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.chat_list.setSpacing(5)
        self.chat_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.chat_list.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.chat_list.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.chat_list.viewport().setAutoFillBackground(False)
        self.chat_list.setAutoFillBackground(False)
        self.chat_list.setStyleSheet("""
            QListWidget { border: none; background: transparent; }
            QListWidget::item { border: none; padding: 0px; background: transparent; }
        """)
        left_layout.addWidget(self.chat_list)
        # 只监听位置变化：滑块拖动、键盘翻页、点击滚动槽都会改 value，
        # 而内容增删/变矮引起的被动变化由 _on_chat_scroll_changed 里的
        # 底部判定过滤掉。
        self.chat_list.verticalScrollBar().valueChanged.connect(
            self._on_chat_scroll_changed
        )

        self.latest_output_bar = QFrame(self.chat_background_panel)
        self.latest_output_bar.setObjectName("latestOutputBar")
        self.latest_output_bar.setFixedSize(176, 42)
        # 不能设 WA_TranslucentBackground：它会让这个子 QFrame 的样式表背景与边框
        # 完全不绘制（只剩文字），浅色主题下看起来就是一块纯白底。
        self.latest_output_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._style_latest_output_bar()
        latest_output_layout = QHBoxLayout(self.latest_output_bar)
        latest_output_layout.setContentsMargins(8, 3, 8, 3)
        self.latest_output_button = QToolButton(self.latest_output_bar)
        self.latest_output_button.setText("回到最新输出")
        self.latest_output_button.setToolTip("恢复自动跟随最新回复")
        self.latest_output_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.latest_output_button.clicked.connect(self._return_to_latest_output)
        latest_output_layout.addWidget(self.latest_output_button)
        self.latest_output_bar.hide()
        self.chat_list.resized.connect(self._position_latest_output_bar)
        self._position_latest_output_bar()

        # 输入区：消息输入、附件菜单、模式、推理强度、模型状态和发送/停止按钮。
        self.composer = QFrame()
        self.composer.setObjectName("composer")
        self.composer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.composer.setMinimumHeight(116)
        self.composer.setMaximumHeight(210)
        composer_layout = QVBoxLayout(self.composer)
        composer_layout.setContentsMargins(8, 6, 8, 8)
        composer_layout.setSpacing(4)
        self.input_text = ChatInputTextEdit()
        self.input_text.setPlaceholderText("输入消息…（按 Enter 发送）")
        self.input_text.setAcceptRichText(False)
        self.input_text.setMinimumHeight(56)
        self.input_text.setMaximumHeight(136)
        self.input_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._composer_line_count = 0
        self.input_text.send_requested.connect(self.send_message)
        self.input_text.image_pasted.connect(self._on_image_pasted)
        self.input_text.textChanged.connect(self._resize_composer_input)
        composer_layout.addWidget(self.input_text)

        self.attach_label = QLabel("")
        self.attach_clear_btn = QToolButton()
        self.attach_clear_btn.setText("×")
        self.attach_clear_btn.setToolTip("清除附件")
        self.attach_clear_btn.clicked.connect(self._clear_attachments)
        attachment_row = QHBoxLayout()
        attachment_row.setContentsMargins(4, 0, 4, 0)
        attachment_row.addWidget(self.attach_label, 1)
        attachment_row.addWidget(self.attach_clear_btn)
        composer_layout.addLayout(attachment_row)
        self.attach_label.hide()
        self.attach_clear_btn.hide()

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(2, 0, 2, 0)
        self.attach_btn = QToolButton()
        self.attach_btn.setText("+")
        self.attach_btn.setToolTip("截图、添加图片或选择文件")
        attach_menu = QMenu(self.attach_btn)
        screenshot_action = attach_menu.addAction("截图")
        screenshot_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DesktopIcon))
        screenshot_action.triggered.connect(self._attach_screenshot)
        image_action = attach_menu.addAction("添加图片")
        image_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
        image_action.triggered.connect(self._attach_images)
        file_action = attach_menu.addAction("选择文件")
        file_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        file_action.triggered.connect(self._attach_file)
        self.attach_btn.setMenu(attach_menu)
        self.attach_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(self.attach_btn)

        self.mode_button = QToolButton()
        self.mode_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        mode_menu = QMenu(self.mode_button)
        self.default_mode_action = mode_menu.addAction("默认模式")
        self.default_mode_action.setCheckable(True)
        self.roleplay_mode_action = mode_menu.addAction("自由角色扮演")
        self.roleplay_mode_action.setCheckable(True)
        self.default_mode_action.triggered.connect(lambda: self._set_quick_mode(False))
        self.roleplay_mode_action.triggered.connect(lambda: self._set_quick_mode(True))
        self.mode_button.setMenu(mode_menu)
        toolbar.addWidget(self.mode_button)

        # 权限模式快捷切换：对齐 DSH 网页端，权限选择器就放在输入框旁边（常驻可点）
        self.permission_button = QToolButton()
        self.permission_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.permission_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        permission_quick_menu = QMenu(self.permission_button)
        self.permission_quick_actions, self.permission_quick_group = self._build_permission_actions(
            permission_quick_menu
        )
        permission_quick_menu.aboutToShow.connect(self._sync_permission_action_enabled)
        self.permission_button.setMenu(permission_quick_menu)
        toolbar.addWidget(self.permission_button)

        self.mic_btn = QToolButton()
        self.mic_btn.setText("🎙")
        self.mic_btn.setToolTip("语音输入")
        self.mic_btn.clicked.connect(self._toggle_voice_input)
        toolbar.addWidget(self.mic_btn)
        toolbar.addStretch(1)

        self.reasoning_button = QToolButton()
        self.reasoning_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.reasoning_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        reasoning_menu = QMenu(self.reasoning_button)
        self.reasoning_actions = {}
        self.reasoning_group = QActionGroup(self)
        self.reasoning_group.setExclusive(True)
        for label, value in REASONING_EFFORT_CHOICES:
            action = reasoning_menu.addAction(f"推理：{label}")
            action.setCheckable(True)
            action.setActionGroup(self.reasoning_group)
            action.triggered.connect(
                lambda checked=False, v=value: self._set_quick_reasoning(v)
            )
            self.reasoning_actions[value] = action
        self.reasoning_button.setMenu(reasoning_menu)
        self.reasoning_button.setToolTip("快捷调整当前模型的推理强度（关闭 + 六档）")
        toolbar.addWidget(self.reasoning_button)

        self.model_indicator = QToolButton()
        self.model_indicator.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.model_indicator.setToolTip("当前模型；点击打开配置")
        self.model_indicator.clicked.connect(self.show_config_dialog)
        toolbar.addWidget(self.model_indicator)

        self.action_stack = QStackedWidget()
        self.action_stack.setFixedSize(48, 42)
        self.send_btn = QToolButton()
        self.send_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self.send_btn.setIconSize(QSize(21, 21))
        self.send_btn.setToolTip("发送")
        self.send_btn.clicked.connect(self.send_message)
        self.stop_btn = QToolButton()
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_btn.setIconSize(QSize(18, 18))
        self.stop_btn.setToolTip("停止生成")
        self.stop_btn.clicked.connect(self._stop_generation)
        self.action_stack.addWidget(self.send_btn)
        self.action_stack.addWidget(self.stop_btn)
        self.action_stack.setCurrentWidget(self.send_btn)
        toolbar.addWidget(self.action_stack)
        composer_layout.addLayout(toolbar)
        left_layout.addWidget(self.composer)
        self._update_model_indicator()
        self._refresh_permission_mode_actions()
        self._update_permission_button()
        self._update_mode_button()
        self._update_reasoning_button()
        self._resize_composer_input()

        # 右侧 Live2D 区域
        right = QWidget()
        right.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.right_panel = right
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.kaomoji_label = QLabel("(｡•ᴗ•｡)")
        self.kaomoji_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.kaomoji_label.setStyleSheet("font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;")
        right_layout.addWidget(self.kaomoji_label)

        self.webview = QWebEngineView()
        self.webview.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.webview.setMinimumWidth(350)
        self.webview.setMinimumHeight(500)
        right_layout.addWidget(self.webview)

        content_splitter.addWidget(left)
        content_splitter.addWidget(right)
        if self.show_live2d_panel:
            content_splitter.setSizes([650, 350])
        else:
            right.hide()
            content_splitter.setSizes([780, 0])
        content_layout.addWidget(content_splitter)

        self.session_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.session_splitter.setChildrenCollapsible(False)
        self.session_splitter.setHandleWidth(4)
        self.session_splitter.addWidget(self.session_panel)
        self.session_splitter.addWidget(self.content_panel)
        self.session_splitter.setCollapsible(0, False)
        self.session_splitter.setCollapsible(1, False)
        saved_session_width = self.config.settings.value("session_panel_width", 270, type=int)
        self.session_panel_width = max(220, min(380, int(saved_session_width or 270)))
        self.session_splitter.setSizes([self.session_panel_width, 900])
        self.session_splitter.splitterMoved.connect(self._on_session_splitter_moved)
        main_layout.addWidget(self.session_splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(f"就绪 | 当前会话: {self.current_session_id[:8]}...")
        self.refresh_session_list()
        self._apply_ui_theme()


    def init_menu(self):
        menubar = self.menuBar()

        session_menu = menubar.addMenu("会话")
        session_menu.addAction("新建会话").triggered.connect(self.new_session)
        session_menu.addAction("历史会话").triggered.connect(self.show_session_list)
        session_menu.addSeparator()
        session_menu.addAction("清空当前会话").triggered.connect(self.clear_conversation)

        self.role_menu = menubar.addMenu("角色")
        self._rebuild_role_menu()

        view_menu = menubar.addMenu("视图")
        view_menu.addAction("聊天外观...").triggered.connect(
            self.show_chat_appearance_dialog
        )
        theme_menu = view_menu.addMenu("主题")
        self.theme_actions = {}
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for theme_key, theme_label in (("dark", "暗色"), ("light", "明色")):
            action = theme_menu.addAction(theme_label)
            action.setCheckable(True)
            action.setActionGroup(theme_group)
            action.triggered.connect(
                lambda checked=False, key=theme_key: self.set_ui_theme(key)
            )
            self.theme_actions[theme_key] = action
        view_menu.addSeparator()

        layout_menu = view_menu.addMenu("布局")
        self.session_panel_action = layout_menu.addAction("会话栏")
        self.session_panel_action.setCheckable(True)
        self.session_panel_action.setChecked(True)
        self.session_panel_action.toggled.connect(self.toggle_session_panel)

        pet_menu = view_menu.addMenu("桌宠")
        pet_menu.addAction("选择模型").triggered.connect(self.show_pet_model_dialog)
        pet_menu.addAction("模型大小").triggered.connect(self.show_pet_scale_dialog)
        pet_menu.addAction("配件管理...").triggered.connect(self.show_pet_wear_dialog)
        pet_menu.addAction("动作映射配置...").triggered.connect(self.show_pet_emotion_dialog)
        pet_menu.addSeparator()
        pet_menu.addAction("重新加载模型").triggered.connect(self.reload_live2d_model)
        pet_menu.addAction("打开模型目录").triggered.connect(self.open_model_directory)

        settings_menu = menubar.addMenu("设置")

        model_menu = settings_menu.addMenu("模型与接口")
        model_menu.addAction("模型与 API").triggered.connect(self.show_config_dialog)
        model_menu.addAction("提示词").triggered.connect(self.show_prompt_dialog)

        interaction_menu = settings_menu.addMenu("交互与身份")
        interaction_menu.addAction("语音").triggered.connect(
            self.show_voice_config_dialog
        )
        interaction_menu.addAction("头像与身份").triggered.connect(
            self.show_avatar_dialog
        )

        extension_menu = settings_menu.addMenu("扩展")
        extension_menu.addAction("MCP 工具管理").triggered.connect(
            self.show_mcp_manager_dialog
        )

        self.background_mode_actions = {}

        help_menu = menubar.addMenu("帮助")
        guide_menu = help_menu.addMenu("使用说明")
        guide_menu.addAction("语音使用说明").triggered.connect(
            self.show_speech_config_help
        )
        guide_menu.addAction("配置文件说明").triggered.connect(
            self.show_config_file_help
        )
        help_menu.addSeparator()
        help_menu.addAction("关于").triggered.connect(self.show_about)
        self._refresh_theme_actions()


    @pyqtSlot(int, str, object)
    def _on_tts_page_started(self, index, text, words=None):
        tts_thread = self.sender()
        if tts_thread is not self.tts_thread:
            return
        if self.pet_window:
            self.pet_window.show_tts_page(index, text, words)

    @pyqtSlot(str)
    def _on_tts_error(self, message):
        tts_thread = self.sender()
        if tts_thread is not self.tts_thread:
            return
        logging.error("TTS 播放线程错误：%s", message)
        if self.pet_window:
            self.pet_window.cancel_speech()

    @pyqtSlot()
    def _on_tts_finished(self):
        finished_thread = self.sender()
        if finished_thread in self._tts_threads:
            self._tts_threads.remove(finished_thread)
        # 唤醒应答：不管正常播完还是被中途打断，接着都该开麦。要在下面的
        # 「不是当前 TTS 线程」提前返回之前处理，否则应答被 _stop_tts() 打断时
        # 会漏掉这次拾音。
        if finished_thread is self._pending_voice_input_thread:
            self._pending_voice_input_thread = None
            if self.tts_thread is finished_thread:
                if self.pet_window:
                    self.pet_window.finish_tts_speech()
                self.tts_thread = None
            if self.wakeword_thread:
                self.wakeword_thread.set_paused(False)
                self.wakeword_thread.resume()
            self._start_voice_input()
            return
        if finished_thread is not self.tts_thread:
            return
        if self.pet_window:
            self.pet_window.finish_tts_speech()
        self.tts_thread = None
        if self.wakeword_thread:
            self.wakeword_thread.set_paused(False)
            self.wakeword_thread.resume()


    @pyqtSlot(str, str, object)
    def on_tool_confirmation_requested(self, call_id, tool_name, arguments):
        prompt_map = {
            "shutdown": "确定要执行关机吗？\n注意：系统将延时10秒执行，可在命令行输入 shutdown /a 取消。",
            "reboot": "确定要执行重启吗？\n注意：系统将延时10秒执行，可在命令行输入 shutdown /a 取消。",
            "run_command": "确定要执行以下命令吗？\n\n{command}\n\n注意：命令将以你的账户权限直接执行，请确认命令内容。",
            "write_file": "确定要写入文件吗？\n\n路径：{path}\n\n内容：\n{content}\n\n注意：该操作会覆盖已存在的文件。",
            "computer_session": "AI 请求启动视觉键鼠连续操作。\n\n"
                               "授权后，AI 可以连续截图、移动鼠标、点击、输入文字和执行快捷键。\n"
                               "任务执行期间持续按住 Q 3 秒可取消。\n\n"
                               "点击 Yes 后任务会自动继续，不需要再回聊天框回复确认。\n\n"
                               "确认开始本次视觉键鼠任务吗？",
        }
        is_timeout = tool_name == "__timeout_wait__"
        if is_timeout:
            prompt = (
                f"命令执行超过 {arguments.get('timeout', 0)} 秒仍无响应"
                f"（已累计等待 {arguments.get('elapsed', 0)} 秒）。\n\n"
                f"命令：\n{arguments.get('command', '')}\n\n"
                f"是否继续等待？继续将再等 {arguments.get('timeout', 0)} 秒；结束将终止该命令。"
            )
        elif MCPManager.is_mcp_tool(tool_name):
            # 外部 MCP 工具的来源和参数都摊给用户看，别只丢一个英文函数名
            _mcp_server = MCPManager.server_of(tool_name) or "未知"
            prompt = (
                f"AI 请求调用外部 MCP 工具「{tool_name}」（来源 server：{_mcp_server}）。\n\n"
                f"参数：\n{json.dumps(arguments, ensure_ascii=False, indent=2)}\n\n"
                "该工具由第三方 MCP server 提供，执行前请确认内容无误。"
            )
        else:
            prompt = prompt_map.get(tool_name, f"确定要执行工具「{tool_name}」吗？")
            if tool_name not in prompt_map and arguments:
                prompt += "\n\n参数：\n" + json.dumps(arguments, ensure_ascii=False, indent=2)
        if tool_name == "run_command":
            prompt = prompt.replace("{command}", str(arguments.get("command", "")))
        elif tool_name == "write_file":
            prompt = prompt.replace("{path}", str(arguments.get("path", ""))).replace("{content}", str(arguments.get("content", "")))
        title = "命令超时" if is_timeout else "AI 操作授权"
        if self.pet_window:
            self.pet_window.set_pet_activity("waiting", "等你确认一下…")
        self._pending_permission = {"title": title, "message": prompt}
        if self.pet_window:
            self.pet_window.notify_permission_request(prompt.split("\n", 1)[0])

        computer_session_active = VisionInputController.is_session_active()
        if computer_session_active:
            VisionInputController.suspend_input_guard_for_user_confirmation()

        dialog = QMessageBox()
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle(title)
        dialog.setText(prompt)
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if is_timeout:
            dialog.button(QMessageBox.StandardButton.Yes).setText("继续等待")
            dialog.button(QMessageBox.StandardButton.No).setText("结束")
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setWindowFlags(
            dialog.windowFlags()
            | Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self._permission_dialog = dialog
        QApplication.beep()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        reply = QMessageBox.StandardButton.No
        try:
            reply = dialog.exec()
        finally:
            self._permission_dialog = None
            self._pending_permission = None
        if self.pet_window:
            self.pet_window.set_pet_activity("working")
        guard_restored = True
        if computer_session_active:
            guard_restored = VisionInputController.resume_input_guard()
        requesting_thread = self.sender() or self.current_api_thread
        if requesting_thread:
            approved = reply == QMessageBox.StandardButton.Yes and guard_restored
            requesting_thread.set_tool_confirmation(call_id, approved)


    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.refresh_all_message_widgets)
        self.chat_background_panel.update()
        self._resize_composer_input()


    def closeEvent(self, event):
        if self.managed_by_pet and not self.allow_close:
            event.ignore()
            self.hide()
            return
        self.computer_overlay.hide()
        # 关掉所有外部 MCP server 子进程，避免关窗后留下孤儿进程
        MCPManager.shutdown()
        # 会话完成信号可能先于 run() 返回到达界面；关闭时遍历所有保活线程并等待退出。
        api_threads = list(self.session_threads.values())
        if self.current_api_thread is not None and all(
            thread is not self.current_api_thread for thread in api_threads
        ):
            api_threads.append(self.current_api_thread)
        for thread in api_threads:
            if thread is None or not thread.isRunning():
                continue
            if thread is self.current_api_thread:
                try:
                    thread.stopped.disconnect(self.on_generation_stopped)
                except (TypeError, RuntimeError):
                    pass
            thread.stop()
            if not thread.wait(1000):
                thread.terminate()
                thread.wait()
        # 停止语音相关线程
        self._stop_tts(wait_ms=3000)
        for thread in list(self._tts_threads):
            if thread.isRunning():
                thread.terminate()
                thread.wait()
        if self.stt_thread and self.stt_thread.isRunning():
            self.stt_thread.stop()
            if not self.stt_thread.wait(3000):
                self.stt_thread.terminate()
                self.stt_thread.wait()
            self.stt_thread = None
        if self.wakeword_thread and self.wakeword_thread.isRunning():
            self.wakeword_thread.stop()
            if not self.wakeword_thread.wait(2000):
                self.wakeword_thread.terminate()
                self.wakeword_thread.wait()
            self.wakeword_thread = None
        if self.live2d_server:
            thread_registry.spawn(
                self.live2d_server.stop, name="Live2D-stop", group="live2d_stop"
            )
        # 停掉本地记忆索引线程：它平时阻塞在队列上，必须显式唤醒才会退出。
        if getattr(self, "_memory", None) is not None:
            self._memory.stop()
        # 统一回收：注册表里仍在跑的短命线程（请求 worker、探测抓取、服务停止等）
        # 尽力 join，超出总预算就放弃 —— 它们都是 daemon，不会拖住进程退出。
        # per_thread_ms 限制单个线程的等待上限，避免长驻 watcher 吃掉整个预算。
        leftover = thread_registry.join_all(timeout_ms=2000, per_thread_ms=250)
        if leftover:
            logging.info(
                "关闭时仍有 %s 个后台线程未在预算内结束（daemon，随进程退出）：%s",
                len(leftover),
                "、".join(str(getattr(thread, "name", thread)) for thread in leftover),
            )
        event.accept()

    def show_chat_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_chat_window(self):
        self.hide()

    def request_close(self):
        self.allow_close = True
        self.close()


# ======================== 程序入口 ========================
def main():
    log_path = setup_logging()
    logging.info("程序启动，日志文件：%s", log_path)
    app = QApplication(sys.argv)
    app_icon = get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    app.setStyle("Fusion")
    app.setApplicationName("AI桌面助手")
    app.setOrganizationName("AI_Assistant")
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow(show_live2d_panel=False, managed_by_pet=True)
    pet_window = DesktopPetWindow(window, window.live2d_server)
    window.bind_pet_window(pet_window)
    pet_window.show()
    window.show_chat_window()
    logging.info("桌宠与聊天窗口已显示，进入主循环")
    # 首次启动向导：等主窗口画完再弹，免得用户先看到一屏空白再被表单糊住。
    # 引导本身出错也不能让程序起不来，所以整段兜住异常。
    def _maybe_show_first_run():
        try:
            from first_run import show_first_run_if_needed

            show_first_run_if_needed(window, window.config)
        except Exception:
            logging.exception("首次启动向导异常，已跳过")

    # 冒烟测试只跑主循环，弹一个模态向导会把它堵在里面，所以跳过。
    if not os.environ.get("AISSISTANT_SMOKE_EXIT_MS", "").strip():
        QTimer.singleShot(300, _maybe_show_first_run)
    # 自动化冒烟测试用：设了就进入事件循环后自动退出，因此走的是**正常关闭路径**
    # （closeEvent → MCP 关闭 → 线程回收），而不是被强杀。不设则完全无副作用。
    smoke_exit_ms = os.environ.get("AISSISTANT_SMOKE_EXIT_MS", "").strip()
    if smoke_exit_ms.isdigit():
        def _smoke_shutdown():
            # allow_close 必须置位，否则 managed_by_pet 的 closeEvent 只会把窗口藏起来
            window.allow_close = True
            window.close()
            QTimer.singleShot(0, app.quit)

        logging.info("冒烟测试模式：%s ms 后自动退出", smoke_exit_ms)
        QTimer.singleShot(int(smoke_exit_ms), _smoke_shutdown)
    exit_code = app.exec()
    logging.info("程序退出，退出码 %s", exit_code)
    if pet_window._restart_requested:
        if getattr(sys, "frozen", False):
            restart_args = [sys.executable, *sys.argv[1:]]
        else:
            restart_args = [sys.executable, *sys.argv]
        logging.info("重新启动程序：%s", restart_args)
        os.execv(sys.executable, restart_args)
    sys.exit(exit_code)


# Bind the moved methods back: descriptor semantics are unchanged, so every
# existing call site keeps working exactly as before.
MainWindow._session_group = staticmethod(_session_group)
MainWindow._session_indicator_color = staticmethod(_session_indicator_color)
MainWindow._resolve_world_cup_scope = staticmethod(_resolve_world_cup_scope)
MainWindow._needs_world_cup_clarification = staticmethod(_needs_world_cup_clarification)
MainWindow._compact_user_message = staticmethod(_compact_user_message)
MainWindow._screenshot_to_attachment = staticmethod(_screenshot_to_attachment)
MainWindow._is_text_file = staticmethod(_is_text_file)
MainWindow.show_mcp_manager_dialog = show_mcp_manager_dialog
MainWindow._get_selected_live2d_model = _get_selected_live2d_model
MainWindow._refresh_pet_param_catalog = _refresh_pet_param_catalog
MainWindow.show_pet_model_dialog = show_pet_model_dialog
MainWindow.show_pet_wear_dialog = show_pet_wear_dialog
MainWindow.show_pet_emotion_dialog = show_pet_emotion_dialog
MainWindow.show_pet_scale_dialog = show_pet_scale_dialog
MainWindow.update_all_bubbles_opacity = update_all_bubbles_opacity
MainWindow.update_all_code_opacity = update_all_code_opacity
MainWindow.update_all_chat_colors = update_all_chat_colors
MainWindow.show_opacity_dialog = show_opacity_dialog
MainWindow.show_code_opacity_dialog = show_code_opacity_dialog
MainWindow.update_all_avatar_widgets = update_all_avatar_widgets
MainWindow.show_avatar_dialog = show_avatar_dialog
MainWindow.update_all_text_font_widgets = update_all_text_font_widgets
MainWindow.show_chat_font_dialog = show_chat_font_dialog
MainWindow.show_code_font_dialog = show_code_font_dialog
MainWindow.refresh_all_message_widgets = refresh_all_message_widgets
MainWindow._read_chat_appearance_state = _read_chat_appearance_state
MainWindow._apply_chat_appearance_state = _apply_chat_appearance_state
MainWindow.preview_chat_appearance = preview_chat_appearance
MainWindow._save_chat_appearance_state = _save_chat_appearance_state
MainWindow.show_chat_appearance_dialog = show_chat_appearance_dialog
MainWindow._refresh_background_mode_actions = _refresh_background_mode_actions
MainWindow.set_chat_background_mode = set_chat_background_mode
MainWindow.choose_chat_background = choose_chat_background
MainWindow.set_chat_background = set_chat_background
MainWindow.reset_chat_background = reset_chat_background
MainWindow._resolve_saved_chat_background = _resolve_saved_chat_background
MainWindow._apply_chat_background = _apply_chat_background
MainWindow._apply_ui_theme = _apply_ui_theme
MainWindow._refresh_theme_actions = _refresh_theme_actions
MainWindow.set_ui_theme = set_ui_theme
MainWindow._style_latest_output_bar = _style_latest_output_bar
MainWindow._get_latest_session_or_new = _get_latest_session_or_new
MainWindow.refresh_session_list = refresh_session_list
MainWindow._session_indicator_state = _session_indicator_state
MainWindow._update_session_list_entry = _update_session_list_entry
MainWindow._session_indicator_icon = _session_indicator_icon
MainWindow._advance_session_breath = _advance_session_breath
MainWindow._set_session_notice = _set_session_notice
MainWindow._on_session_item_activated = _on_session_item_activated
MainWindow._session_id_at = _session_id_at
MainWindow._show_session_context_menu = _show_session_context_menu
MainWindow._rename_session = _rename_session
MainWindow._delete_session_from_panel = _delete_session_from_panel
MainWindow._delete_session = _delete_session
MainWindow._on_session_splitter_moved = _on_session_splitter_moved
MainWindow.toggle_session_panel = toggle_session_panel
MainWindow.show_session_list = show_session_list
MainWindow.show_speech_config_help = show_speech_config_help
MainWindow.show_config_file_help = show_config_file_help
MainWindow.show_about = show_about


# Bind the moved methods back: descriptor semantics are unchanged, so every
# existing call site keeps working exactly as before.
MainWindow._is_chat_at_bottom = _is_chat_at_bottom
MainWindow._set_auto_follow_output = _set_auto_follow_output
MainWindow._suppress_chat_scroll_events = _suppress_chat_scroll_events
MainWindow._on_chat_scroll_changed = _on_chat_scroll_changed
MainWindow._position_latest_output_bar = _position_latest_output_bar
MainWindow._return_to_latest_output = _return_to_latest_output
MainWindow._follow_latest_output = _follow_latest_output
MainWindow._scroll_latest_output_if_enabled = _scroll_latest_output_if_enabled
MainWindow._generate_dynamic_system_prompt = _generate_dynamic_system_prompt
MainWindow._format_assistant_text = _format_assistant_text
MainWindow._get_default_system_prompt = _get_default_system_prompt
MainWindow._get_permission_mode_notice = _get_permission_mode_notice
MainWindow._get_effective_system_prompt = _get_effective_system_prompt
MainWindow._build_system_prompt_messages = _build_system_prompt_messages
MainWindow.load_system_prompt = load_system_prompt
MainWindow.refresh_action_handler_config = refresh_action_handler_config
MainWindow.load_history_from_db = load_history_from_db
MainWindow._history_page_size = _history_page_size
MainWindow._create_history_widgets = _create_history_widgets
MainWindow.reset_history_pagination = reset_history_pagination
MainWindow.load_earlier_history = load_earlier_history
MainWindow._maybe_load_earlier_history = _maybe_load_earlier_history
MainWindow._get_session_task_state = _get_session_task_state
MainWindow._set_session_task_state = _set_session_task_state
MainWindow._restore_input_controls = _restore_input_controls
MainWindow._discard_current_generation = _discard_current_generation
MainWindow._show_temporary_session = _show_temporary_session
MainWindow._create_task_placeholder = _create_task_placeholder
MainWindow._restore_running_session_ui = _restore_running_session_ui
MainWindow._on_session_thread_finished = _on_session_thread_finished
MainWindow._stop_session_thread = _stop_session_thread
MainWindow._finish_background_reply = _finish_background_reply
MainWindow._placeholder_row = _placeholder_row
MainWindow._on_status_update = _on_status_update
MainWindow._on_task_state_updated = _on_task_state_updated
MainWindow._on_computer_state = _on_computer_state
MainWindow._sync_pet_task_activity = _sync_pet_task_activity
MainWindow._build_world_cup_clarification = _build_world_cup_clarification
MainWindow._evaluate_clarification_need = _evaluate_clarification_need
MainWindow.clear_conversation = clear_conversation
MainWindow.new_session = new_session
MainWindow.switch_session = switch_session
MainWindow.add_message = add_message
MainWindow._create_message_widget = _create_message_widget
MainWindow._track_history_row = _track_history_row
MainWindow._build_api_messages = _build_api_messages
MainWindow._fallback_api_messages = _fallback_api_messages
MainWindow.send_message = send_message
MainWindow.on_process_chunk = on_process_chunk
MainWindow.on_stream_chunk = on_stream_chunk
MainWindow._stop_generation = _stop_generation
MainWindow.on_generation_stopped = on_generation_stopped
MainWindow.on_response_complete = on_response_complete
MainWindow.raise_permission_dialog = raise_permission_dialog
MainWindow.on_api_error = on_api_error
MainWindow._start_wakeword_listener = _start_wakeword_listener
MainWindow._on_wakeword_detected = _on_wakeword_detected
MainWindow._speak_wake_ack = _speak_wake_ack
MainWindow._toggle_voice_input = _toggle_voice_input
MainWindow._start_voice_input = _start_voice_input
MainWindow._on_stt_started = _on_stt_started
MainWindow._on_stt_partial = _on_stt_partial
MainWindow._on_stt_final = _on_stt_final
MainWindow._on_stt_canceled = _on_stt_canceled
MainWindow._on_stt_error = _on_stt_error
MainWindow._stop_voice_input = _stop_voice_input
MainWindow._stop_tts = _stop_tts
MainWindow._attach_file = _attach_file
MainWindow._attach_screenshot = _attach_screenshot
MainWindow._open_screenshot_overlay = _open_screenshot_overlay
MainWindow._hide_floating_windows = _hide_floating_windows
MainWindow._restore_floating_windows = _restore_floating_windows
MainWindow._restore_after_screenshot = _restore_after_screenshot
MainWindow._on_screenshot_finished = _on_screenshot_finished
MainWindow._on_image_pasted = _on_image_pasted
MainWindow._attach_images = _attach_images
MainWindow._update_attach_label = _update_attach_label
MainWindow._clear_attachments = _clear_attachments
MainWindow.bind_pet_window = bind_pet_window
MainWindow._apply_pet_command = _apply_pet_command
MainWindow._sync_kaomoji_to_pet = _sync_kaomoji_to_pet
MainWindow._set_kaomoji_display = _set_kaomoji_display
MainWindow._live2d_page_url = _live2d_page_url
MainWindow.set_pet_model = set_pet_model
MainWindow.init_live2d_server = init_live2d_server
MainWindow._publish_pet_endpoint = _publish_pet_endpoint
MainWindow.reload_live2d_model = reload_live2d_model
MainWindow.open_model_directory = open_model_directory
MainWindow._update_kaomoji_by_text = _update_kaomoji_by_text
MainWindow._show_thinking_kaomoji = _show_thinking_kaomoji
MainWindow.fetch_motto = fetch_motto
MainWindow.on_motto_reply = on_motto_reply
MainWindow.use_fallback_motto = use_fallback_motto
MainWindow._update_model_indicator = _update_model_indicator
MainWindow._update_permission_button = _update_permission_button
MainWindow._resize_composer_input = _resize_composer_input
MainWindow._update_mode_button = _update_mode_button
MainWindow.set_permission_mode = set_permission_mode
MainWindow._is_turn_running = _is_turn_running
MainWindow._permission_change_blocked = _permission_change_blocked
MainWindow._sync_permission_action_enabled = _sync_permission_action_enabled
MainWindow._build_permission_actions = _build_permission_actions
MainWindow._refresh_permission_mode_actions = _refresh_permission_mode_actions
MainWindow._set_quick_mode = _set_quick_mode
MainWindow._set_quick_reasoning = _set_quick_reasoning
MainWindow._update_reasoning_button = _update_reasoning_button
MainWindow.show_config_dialog = show_config_dialog
MainWindow.show_prompt_dialog = show_prompt_dialog
MainWindow.show_voice_config_dialog = show_voice_config_dialog
MainWindow._rebuild_role_menu = _rebuild_role_menu
MainWindow.show_role_manager_dialog = show_role_manager_dialog
MainWindow.switch_role = switch_role



if __name__ == "__main__":
    main()
