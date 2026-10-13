"""Live2D model, expression, wear and activity state for the pet."""

from __future__ import annotations
from PyQt6.QtCore import QSize
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication
from chat_markdown import pet_bubble_text
from live2d_catalog import get_live2d_all_expressions
from live2d_catalog import get_live2d_expression_params
from pet_scripts import PET_ACTIVITY_DONE_MS
from pet_scripts import PET_ACTIVITY_ENABLED
from pet_scripts import PET_ACTIVITY_HINT_MIN_GAP
from pet_scripts import PET_ACTIVITY_HINT_MS
from pet_scripts import PET_ACTIVITY_SCRIPT
from pet_scripts import PET_PARAM_DEFAULT_MS
from pet_scripts import PET_PARAM_ENABLED
from pet_scripts import PET_PARAM_SCRIPT
import json
import logging
import time



def _on_pet_page_loaded(self, ok):
    """页面加载完成：装脚本；若引擎还没就绪（首次常发），延迟重试一次。"""
    self._clear_canvas_scale()
    self._install_pet_drag_stage()
    self._install_pet_activity()
    self._install_pet_params()
    if not ok:
        return
    QTimer.singleShot(800, self._verify_pet_model)
    QTimer.singleShot(2200, self._verify_pet_model)


def _verify_pet_model(self):
    if self.webview is None:
        return
    self.webview.page().runJavaScript(
        "JSON.stringify(window.__petParamInfo ? window.__petParamInfo() : {missing:true})",
        self._on_pet_model_probe,
    )


def _on_pet_model_probe(self, result):
    try:
        info = json.loads(result) if isinstance(result, str) else (result or {})
    except (TypeError, ValueError):
        info = {}
    ready = bool(info.get("model")) if isinstance(info, dict) else False
    if ready:
        self._pet_model_loaded = True
        self._pet_load_retry = 0
        return
    if self._pet_model_loaded or self._pet_load_retry >= 1:
        return
    self._pet_load_retry += 1
    logging.warning("[pet] 首次加载未就绪，重试加载模型页面：%s", info)
    self.load_live2d_model()


def load_live2d_model(self):
    if self.live2d_server:
        self.webview.setUrl(QUrl(self.chat_window._live2d_page_url("pet.html")))


def _scaled_pet_size(self):
    return QSize(
        round(self.BASE_SIZE.width() * self.model_scale),
        round(self.BASE_SIZE.height() * self.model_scale),
    )


def _clear_canvas_scale(self):
    script = f"""
    (() => {{
        const clearScale = () => {{
            const canvas = document.querySelector('canvas');
            if (!canvas) {{
                window.setTimeout(clearScale, 100);
                return;
            }}
            window.__petModelScale = 1;
            canvas.style.transformOrigin = '';
            canvas.style.transform = '';
        }};
        clearScale();
    }})();
    """
    self.webview.page().runJavaScript(script)


def set_model_scale(self, scale, persist=True):
    self.model_scale = max(0.5, min(1.4, float(scale)))
    if persist:
        self.chat_window.config.set_pet_model_scale(self.model_scale)
    old_center_x = self.frameGeometry().center().x()
    old_bottom = self.frameGeometry().bottom()
    self.resize(self._scaled_pet_size())
    screen = QApplication.screenAt(self.pos()) or QApplication.primaryScreen()
    if screen:
        area = screen.availableGeometry()
        x = max(area.left(), min(old_center_x - self.width() // 2, area.right() - self.width() + 1))
        y = max(area.top(), min(old_bottom - self.height() + 1, area.bottom() - self.height() + 1))
        self.move(x, y)
    self._clear_canvas_scale()
    self.speech_bubble.set_scale(self.model_scale)
    self._position_speech_bubble()


def _place_default_position(self):
    screen = QApplication.primaryScreen()
    if not screen:
        return
    geo = screen.availableGeometry()
    x = geo.right() - self.width() - 32
    y = geo.bottom() - self.height() - 48
    self.move(max(0, x), max(0, y))


# --- 大模型可调的 Live2D 参数层 ---
def _current_pet_model_name(self):
    try:
        return self.chat_window.config.get_pet_model_name()
    except AttributeError:
        return None


def _push_pet_expressions(self):
    """把当前模型作者自带的 *.exp3.json 下发到页面（情绪层按作者原版还原表情，
    并附全量清单供穿戴菜单使用）。"""
    if self.webview is None:
        return
    model_name = self._current_pet_model_name()
    table = get_live2d_expression_params(model_name)
    all_expr = get_live2d_all_expressions(model_name)
    self.webview.page().runJavaScript(
        "window.__petExprParams = %s;" % json.dumps(table, ensure_ascii=False))
    self.webview.page().runJavaScript(
        "window.__petAllExpressions = %s;" % json.dumps(all_expr, ensure_ascii=False))


def _install_pet_params(self):
    if not PET_PARAM_ENABLED or self.webview is None:
        return
    self._push_pet_expressions()
    self.webview.page().runJavaScript(PET_PARAM_SCRIPT)
    # 页面就绪后恢复该模型已保存的穿戴
    self.set_pet_wear(self.get_current_worn())


def get_current_worn(self):
    model_name = self._current_pet_model_name()
    try:
        return self.chat_window.config.get_pet_worn(model_name)
    except AttributeError:
        return []


def set_pet_wear(self, names):
    """持久穿戴：列出要佩戴的 .exp3.json 名称，未列出的自动脱下。"""
    if not PET_PARAM_ENABLED or self.webview is None:
        return
    names = [str(n) for n in (names or [])]
    script = "window.__petWearApply && window.__petWearApply(%s);" % json.dumps(
        names, ensure_ascii=False)
    logging.info("[pet-wear] 应用穿戴：%s", names)
    self.webview.page().runJavaScript(script)


def clear_pet_wear(self):
    if not PET_PARAM_ENABLED or self.webview is None:
        return
    self.webview.page().runJavaScript("window.__petWearClear && window.__petWearClear();")


def set_pet_emotion(self, name, intensity=1.0, duration_ms=None):
    """按语义情绪驱动桌宠表情（happy/shy/angry/love/...）。"""
    if not PET_PARAM_ENABLED or self.webview is None:
        return
    duration_ms = PET_PARAM_DEFAULT_MS if duration_ms is None else int(duration_ms)
    self._install_pet_params()
    try:
        intensity = max(0.0, min(1.0, float(intensity)))
    except (TypeError, ValueError):
        intensity = 1.0
    script = "window.__petEmotionSet && window.__petEmotionSet(%s, %s, %s);" % (
        json.dumps(str(name or "neutral")), round(intensity, 3), int(duration_ms))
    logging.info("[pet-exp] JS 情绪：%s", script)
    self.webview.page().runJavaScript(script)


def set_pet_params(self, mapping, duration_ms=None):
    """直接覆盖一个或多个 Live2D 参数，到期后自动回落默认值。"""
    if not PET_PARAM_ENABLED or self.webview is None:
        return
    if not isinstance(mapping, dict) or not mapping:
        return
    duration_ms = PET_PARAM_DEFAULT_MS if duration_ms is None else int(duration_ms)
    try:
        clean = {str(k): float(v) for k, v in mapping.items()}
    except (TypeError, ValueError):
        return
    self._install_pet_params()
    script = "window.__petParamSet && window.__petParamSet(%s, %s);" % (
        json.dumps(clean, ensure_ascii=False), int(duration_ms))
    self.webview.page().runJavaScript(script)


def clear_pet_params(self):
    if not PET_PARAM_ENABLED or self.webview is None:
        return
    self.webview.page().runJavaScript("window.__petParamClear && window.__petParamClear();")


# --- 情绪层调度：工作期间只跑活动层，情绪缓存到回复播报/说话时才播 ---
def request_pet_expression(self, payload):
    """登记一次大模型表情指令，等工作结束、开始播报/说话时再执行。"""
    if not isinstance(payload, dict):
        return
    self._pending_expression = dict(payload)
    logging.info("[pet-exp] 登记表情指令：%s", self._pending_expression)


def has_pending_expression(self):
    return bool(self._pending_expression)


def clear_pending_expression(self):
    if self._pending_expression:
        logging.info("[pet-exp] 清空未播放的表情：%s", self._pending_expression)
    self._pending_expression = None


def activate_pending_expression(self):
    """把缓存的表情指令真正下发到页面；返回是否执行了指令。"""
    payload = self._pending_expression
    self._pending_expression = None
    if not payload:
        logging.info("[pet-exp] 无可播放的表情（pending 为空）")
        return False
    kind = str(payload.get("kind") or "")
    logging.info("[pet-exp] 播放缓存表情：%s", payload)
    if kind == "emotion":
        self.set_pet_emotion(
            payload.get("emotion", "neutral"),
            payload.get("intensity", 1.0),
            payload.get("duration_ms"),
        )
    elif kind == "param":
        self.set_pet_params(payload.get("parameters") or {}, payload.get("duration_ms"))
    elif kind == "clear":
        self.clear_pet_params()
    else:
        logging.warning("[pet-exp] 未知的表情指令类型：%s", kind)
        return False
    return True


def _set_pet_speaking(self, speaking):
    if not PET_PARAM_ENABLED or self.webview is None:
        return
    logging.info("[pet-exp] speaking=%s", speaking)
    self.webview.page().runJavaScript(
        "window.__petSpeaking = %s;" % ("true" if speaking else "false"))


def set_pet_activity(self, name, hint=None):
    """设置桌宠活动反馈：idle / thinking / working / waiting / done。"""
    name = str(name or "idle").strip() or "idle"
    self._activity_done_timer.stop()
    if name != self._activity:
        self._activity = name
        self._push_pet_activity(name)
    if hint:
        self.show_activity_hint(hint)
    elif name not in ("working", "waiting"):
        self._clear_activity_hint()
    if name == "done":
        self._activity_done_timer.start(PET_ACTIVITY_DONE_MS)


def show_activity_hint(self, text):
    """工作进度用气泡播报；说话或口型进行中不抢气泡。"""
    text = str(text or "").strip()
    shown = pet_bubble_text(text) if text else ""
    if not shown or not self.isVisible():
        return
    if (
        self._tts_speech_active
        or self._speech_playing
        or self._speech_queue
        or self._speech_stream_received
    ):
        return
    if shown == self._activity_hint_text and self.speech_bubble.isVisible():
        return
    now = time.monotonic()
    if now - self._activity_hint_at < PET_ACTIVITY_HINT_MIN_GAP / 1000.0:
        return
    self._activity_hint_text = shown
    self._activity_hint_at = now
    self._show_bubble(text)
    self._activity_hint_timer.start(PET_ACTIVITY_HINT_MS)


def _pet_activity_done(self):
    self.set_pet_activity("idle")


def _hide_activity_hint(self):
    self._clear_activity_hint()


def _clear_activity_hint(self):
    """只收自己播报的那条气泡，不误伤说话/颜文字气泡。"""
    self._activity_hint_timer.stop()
    owns_bubble = (
        bool(self._activity_hint_text)
        and self.speech_bubble._message == self._activity_hint_text
    )
    self._activity_hint_text = ""
    if not owns_bubble:
        return
    if self._tts_speech_active or self._speech_playing or self._speech_queue:
        return
    self.speech_bubble.hide()


def _install_pet_activity(self):
    """思考/工作/完成反馈脚本：随页面加载重装，并重放当前状态。"""
    if not PET_ACTIVITY_ENABLED or self.webview is None:
        return
    self.webview.page().runJavaScript(PET_ACTIVITY_SCRIPT)
    self._push_pet_activity(self._activity)


def _push_pet_activity(self, name):
    if not PET_ACTIVITY_ENABLED or self.webview is None:
        return
    script = "window.__petActivitySet && window.__petActivitySet(%s);" % json.dumps(name)
    self.webview.page().runJavaScript(script)
