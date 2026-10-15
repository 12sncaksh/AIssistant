"""Desktop pet window."""

from __future__ import annotations
from PyQt6.QtCore import QPoint
from PyQt6.QtCore import QRect
from PyQt6.QtCore import QSize
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import QUrl
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QActionGroup
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtGui import QIcon
from PyQt6.QtGui import QLinearGradient
from PyQt6.QtGui import QPainter
from PyQt6.QtGui import QPixmap
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QMenu
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtWidgets import QSystemTrayIcon
from PyQt6.QtWidgets import QVBoxLayout
from PyQt6.QtWidgets import QWidget
from chat_markdown import pet_bubble_text
from app_flags import DEBUG_MODE
from dsh_acp_client import current_connection
from text_protocol import PetSpeechStreamFilter
import json
import logging
import math
import re
import time

from app_theme import get_app_icon, style_popup_menu
from live2d_catalog import get_live2d_all_expressions, get_live2d_expression_params, get_live2d_model_catalog
from permission_modes import PERMISSION_MODE_LABELS
from pet_scripts import PET_ACTIVITY_DONE_MS, PET_ACTIVITY_ENABLED, PET_ACTIVITY_HINT_MIN_GAP, PET_ACTIVITY_HINT_MS, PET_ACTIVITY_SCRIPT, PET_DRAG_FALL_MS, PET_DRAG_RECOVER_MS, PET_DRAG_RESTORE_MS, PET_DRAG_STAGE_ENABLED, PET_DRAG_STAGE_SCRIPT, PET_MOUTH_HOLD_MS, PET_MOUTH_TICK_MS, PET_PARAM_DEFAULT_MS, PET_PARAM_ENABLED, PET_PARAM_SCRIPT
from pet_widgets import PetInteractionOverlay, PetSpeechBubble, PetWebView



# ---- method groups moved into their own modules ----
from pet_speech import (  # noqa: F401
    _speech_text_fits,
    _split_speech_pages,
    get_speech_pages,
    _position_speech_bubble,
    _show_bubble,
    _advance_speech_page,
    _enqueue_speech,
    _enqueue_expression,
    begin_speech_stream,
    prepare_tts_speech,
    show_tts_page,
    finish_tts_speech,
    append_speech_chunk,
    _flush_completed_speech_sentences,
    finish_speech_stream,
    begin_mouth_animation,
    stop_mouth_animation,
    _tick_mouth,
    _mouth_curve,
    cancel_speech,
    set_kaomoji,
)
from pet_live2d import (  # noqa: F401
    _on_pet_page_loaded,
    _verify_pet_model,
    _on_pet_model_probe,
    load_live2d_model,
    _scaled_pet_size,
    _clear_canvas_scale,
    set_model_scale,
    _place_default_position,
    _current_pet_model_name,
    _push_pet_expressions,
    _install_pet_params,
    get_current_worn,
    set_pet_wear,
    clear_pet_wear,
    set_pet_emotion,
    set_pet_params,
    clear_pet_params,
    request_pet_expression,
    has_pending_expression,
    clear_pending_expression,
    activate_pending_expression,
    _set_pet_speaking,
    set_pet_activity,
    show_activity_hint,
    _pet_activity_done,
    _hide_activity_hint,
    _clear_activity_hint,
    _install_pet_activity,
    _push_pet_activity,
)
from pet_poke import (  # noqa: F401
    is_pet_busy,
    refuse_pet_poke,
    _hide_pet_poke_bubble,
)
from pet_drag import (  # noqa: F401
    begin_drag,
    update_drag,
    end_drag,
    start_pet_drag_stage,
    _install_pet_drag_stage,
    _push_pet_drag_stage,
    _probe_pet_drag_stage,
    _log_pet_drag_probe,
    _play_pet_drop_sequence,
    _advance_pet_stage,
)
from pet_gaze import (  # noqa: F401
    install_pet_gaze,
    _install_pet_gaze,
    _push_pet_gaze,
    _tick_pet_gaze,
)
from pet_write import (  # noqa: F401
    _install_pet_write,
    _verify_pet_write,
    _on_pet_write_probe,
)
from pet_tray import (  # noqa: F401
    _add_pet_menu_entries,
    show_context_menu,
    restart_application,
    quit_application,
    _make_tray_icon,
    _build_tray_menu,
    _show_dsh_permission_status,
    _update_tray_menu_labels,
    _setup_tray,
    notify_permission_request,
    _on_tray_message_clicked,
    _on_tray_activated,
    _toggle_pet_visibility,
    _toggle_chat_window,
)

class DesktopPetWindow(QWidget):
    BASE_SIZE = QSize(360, 480)

    def __init__(self, chat_window, live2d_server, parent=None):
        super().__init__(parent)
        self._restart_requested = False
        self.setWindowIcon(get_app_icon())
        self.chat_window = chat_window
        self.live2d_server = live2d_server
        self._drag_offset = None
        self._drag_last_pos = None
        self._drag_last_time = 0.0
        self._drag_vx = 0.0
        self._drag_vy = 0.0
        self._drag_active = False
        self._pet_stage_gen = 0
        self._allow_close = False
        self._tray = None
        self._tray_pet_action = None
        self._tray_chat_action = None
        self.model_scale = self.chat_window.config.get_pet_model_scale()
        self._speech_queue = []
        self._speech_stream_buffer = ""
        self._speech_markup_filter = PetSpeechStreamFilter()
        self._speech_stream_received = False
        self._speech_playing = False
        self._tts_speech_active = False
        self._activity = "idle"
        self._activity_owner = None   # 这份活动状态属于哪个会话（见 set_pet_activity）
        self._activity_done_timer = QTimer(self)
        self._activity_done_timer.setSingleShot(True)
        self._activity_done_timer.timeout.connect(self._pet_activity_done)
        self._activity_hint_timer = QTimer(self)
        self._activity_hint_timer.setSingleShot(True)
        self._activity_hint_timer.timeout.connect(self._hide_activity_hint)
        self._activity_hint_text = ""
        self._activity_hint_at = 0.0
        self._kaomoji = "(｡•ᴗ•｡)"
        self._bubble_accent = "#64748B"
        self._mouth_words = []
        self._mouth_until = 0.0
        self._mouth_started = 0.0
        self._mouth_timer = QTimer(self)
        self._mouth_timer.setInterval(PET_MOUTH_TICK_MS)
        self._mouth_timer.timeout.connect(self._tick_mouth)
        self._pending_expression = None   # 大模型表情指令缓存：等到回复播报/说话时再执行
        self._setup_tray()

        self.setWindowTitle("桌宠")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent;")
        self.resize(self._scaled_pet_size())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.webview = PetWebView(self)
        self.webview.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.webview.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.webview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.webview, 1)

        self.speech_bubble = PetSpeechBubble()
        self.speech_bubble.set_scale(self.model_scale)
        self._bubble_was_visible = False
        self.speech_timer = QTimer(self)
        self.speech_timer.setSingleShot(True)
        self.speech_timer.timeout.connect(self._advance_speech_page)

        self.interaction_overlay = PetInteractionOverlay(self, self.webview)
        self.interaction_overlay.raise_()
        install_pet_gaze(self)

        self._place_default_position()
        self._pet_model_loaded = False
        self._pet_load_retry = 0
        if self.live2d_server:
            QTimer.singleShot(400, self.load_live2d_model)
        self.webview.loadFinished.connect(self._on_pet_page_loaded)


    def closeEvent(self, event):
        if not self._allow_close:
            self.quit_application()
            event.ignore()
            return
        self.speech_bubble.close()
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.interaction_overlay.setGeometry(self.webview.geometry())
        self._position_speech_bubble()
        self.interaction_overlay.raise_()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._position_speech_bubble()

    def hideEvent(self, event):
        self._bubble_was_visible = self.speech_bubble.isVisible()
        self.speech_bubble.hide()
        self._gaze_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._gaze_timer.start()
        should_restore = (
            self._bubble_was_visible
            and (
                self._tts_speech_active
                or self._speech_playing
                or bool(self._speech_queue)
                or self.speech_timer.isActive()
            )
        )
        self._bubble_was_visible = False
        if should_restore:
            self._show_bubble(self.speech_bubble._message)


# Bind the moved methods back (identical descriptor semantics).
DesktopPetWindow._speech_text_fits = _speech_text_fits
DesktopPetWindow._split_speech_pages = _split_speech_pages
DesktopPetWindow.get_speech_pages = get_speech_pages
DesktopPetWindow._position_speech_bubble = _position_speech_bubble
DesktopPetWindow._show_bubble = _show_bubble
DesktopPetWindow._advance_speech_page = _advance_speech_page
DesktopPetWindow._enqueue_speech = _enqueue_speech
DesktopPetWindow._enqueue_expression = _enqueue_expression
DesktopPetWindow.begin_speech_stream = begin_speech_stream
DesktopPetWindow.prepare_tts_speech = prepare_tts_speech
DesktopPetWindow.show_tts_page = show_tts_page
DesktopPetWindow.finish_tts_speech = finish_tts_speech
DesktopPetWindow.append_speech_chunk = append_speech_chunk
DesktopPetWindow._flush_completed_speech_sentences = _flush_completed_speech_sentences
DesktopPetWindow.finish_speech_stream = finish_speech_stream
DesktopPetWindow.begin_mouth_animation = begin_mouth_animation
DesktopPetWindow.stop_mouth_animation = stop_mouth_animation
DesktopPetWindow._tick_mouth = _tick_mouth
DesktopPetWindow._mouth_curve = _mouth_curve
DesktopPetWindow.cancel_speech = cancel_speech
DesktopPetWindow.set_kaomoji = set_kaomoji
DesktopPetWindow._on_pet_page_loaded = _on_pet_page_loaded
DesktopPetWindow._verify_pet_model = _verify_pet_model
DesktopPetWindow._on_pet_model_probe = _on_pet_model_probe
DesktopPetWindow.load_live2d_model = load_live2d_model
DesktopPetWindow._scaled_pet_size = _scaled_pet_size
DesktopPetWindow._clear_canvas_scale = _clear_canvas_scale
DesktopPetWindow.set_model_scale = set_model_scale
DesktopPetWindow._place_default_position = _place_default_position
DesktopPetWindow._current_pet_model_name = _current_pet_model_name
DesktopPetWindow._push_pet_expressions = _push_pet_expressions
DesktopPetWindow._install_pet_params = _install_pet_params
DesktopPetWindow.get_current_worn = get_current_worn
DesktopPetWindow.set_pet_wear = set_pet_wear
DesktopPetWindow.clear_pet_wear = clear_pet_wear
DesktopPetWindow.set_pet_emotion = set_pet_emotion
DesktopPetWindow.set_pet_params = set_pet_params
DesktopPetWindow.clear_pet_params = clear_pet_params
DesktopPetWindow.request_pet_expression = request_pet_expression
DesktopPetWindow.has_pending_expression = has_pending_expression
DesktopPetWindow.clear_pending_expression = clear_pending_expression
DesktopPetWindow.activate_pending_expression = activate_pending_expression
DesktopPetWindow._set_pet_speaking = _set_pet_speaking
DesktopPetWindow.set_pet_activity = set_pet_activity
DesktopPetWindow.show_activity_hint = show_activity_hint
DesktopPetWindow._pet_activity_done = _pet_activity_done
DesktopPetWindow._hide_activity_hint = _hide_activity_hint
DesktopPetWindow._clear_activity_hint = _clear_activity_hint
DesktopPetWindow._install_pet_activity = _install_pet_activity
DesktopPetWindow._push_pet_activity = _push_pet_activity
DesktopPetWindow.is_pet_busy = is_pet_busy
DesktopPetWindow.refuse_pet_poke = refuse_pet_poke
DesktopPetWindow._hide_pet_poke_bubble = _hide_pet_poke_bubble
DesktopPetWindow.begin_drag = begin_drag
DesktopPetWindow.update_drag = update_drag
DesktopPetWindow.end_drag = end_drag
DesktopPetWindow.start_pet_drag_stage = start_pet_drag_stage
DesktopPetWindow._install_pet_drag_stage = _install_pet_drag_stage
DesktopPetWindow._push_pet_drag_stage = _push_pet_drag_stage
DesktopPetWindow._probe_pet_drag_stage = _probe_pet_drag_stage
DesktopPetWindow._log_pet_drag_probe = _log_pet_drag_probe
DesktopPetWindow._play_pet_drop_sequence = _play_pet_drop_sequence
DesktopPetWindow._advance_pet_stage = _advance_pet_stage
DesktopPetWindow._install_pet_gaze = _install_pet_gaze
DesktopPetWindow._push_pet_gaze = _push_pet_gaze
DesktopPetWindow._tick_pet_gaze = _tick_pet_gaze
DesktopPetWindow._install_pet_write = _install_pet_write
DesktopPetWindow._verify_pet_write = _verify_pet_write
DesktopPetWindow._on_pet_write_probe = _on_pet_write_probe
DesktopPetWindow._add_pet_menu_entries = _add_pet_menu_entries
DesktopPetWindow.show_context_menu = show_context_menu
DesktopPetWindow.restart_application = restart_application
DesktopPetWindow.quit_application = quit_application
DesktopPetWindow._make_tray_icon = _make_tray_icon
DesktopPetWindow._build_tray_menu = _build_tray_menu
DesktopPetWindow._show_dsh_permission_status = _show_dsh_permission_status
DesktopPetWindow._update_tray_menu_labels = _update_tray_menu_labels
DesktopPetWindow._setup_tray = _setup_tray
DesktopPetWindow.notify_permission_request = notify_permission_request
DesktopPetWindow._on_tray_message_clicked = _on_tray_message_clicked
DesktopPetWindow._on_tray_activated = _on_tray_activated
DesktopPetWindow._toggle_pet_visibility = _toggle_pet_visibility
DesktopPetWindow._toggle_chat_window = _toggle_chat_window
