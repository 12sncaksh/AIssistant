"""Pet speech bubble, paging, TTS streaming and mouth animation."""

from __future__ import annotations
from PyQt6.QtCore import QPoint
from PyQt6.QtCore import QRect
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import QApplication
from pet_scripts import PET_DRAG_STAGE_ENABLED
from pet_scripts import PET_DRAG_STAGE_SCRIPT
from pet_scripts import PET_MOUTH_HOLD_MS
import math
import re
import time



def _speech_text_fits(self, text):
    metrics = QFontMetrics(self.speech_bubble.text_label.font())
    bounds = metrics.boundingRect(
        QRect(0, 0, self.speech_bubble._max_text_width, 10000),
        int(Qt.TextFlag.TextWordWrap),
        text,
    )
    return bounds.height() <= self.speech_bubble.MAX_LABEL_HEIGHT


def _split_speech_pages(self, text):
    """按气泡实际像素高度分页，优先保留自然停顿处。"""
    pages = []
    remaining = re.sub(r"\s+", " ", str(text or "").strip())
    while remaining:
        if self._speech_text_fits(remaining):
            pages.append(remaining)
            break

        fitted = ""
        last_break = 0
        for index, char in enumerate(remaining):
            candidate = fitted + char
            if not self._speech_text_fits(candidate):
                break
            fitted = candidate
            if char in "，、；：,;: ":
                last_break = index + 1
        else:
            pages.append(remaining)
            break

        cut = last_break or len(fitted)
        if cut <= 0:
            cut = 1
        while cut < len(remaining) and remaining[cut] in "”’）】》」』":
            if not self._speech_text_fits(remaining[:cut + 1]):
                break
            cut += 1
        page = remaining[:cut].strip()
        if page:
            pages.append(page)
        remaining = remaining[cut:].strip()
    return [page for page in pages if page]


def get_speech_pages(self, text):
    return self._split_speech_pages(text)


def _position_speech_bubble(self):
    if not self.speech_bubble.isVisible():
        return
    face_center = self.mapToGlobal(QPoint(round(self.width() * 0.56), round(self.height() * 0.22)))
    screen = QApplication.screenAt(face_center) or QApplication.primaryScreen()
    if not screen:
        return
    area = screen.availableGeometry()

    # 以模型可见区域，而不是脸部中心，作为气泡的水平避让边界。
    model_left = self.mapToGlobal(QPoint(round(self.width() * 0.12), round(self.height() * 0.08))).x()
    model_right = self.mapToGlobal(QPoint(round(self.width() * 0.88), round(self.height() * 0.08))).x()
    model_top = self.mapToGlobal(QPoint(round(self.width() * 0.50), round(self.height() * 0.08))).y()
    margin = 16

    def bubble_size(side):
        if side == "above":
            return (
                self.speech_bubble._text_width + 28,
                self.speech_bubble._body_height + self.speech_bubble.TAIL_WIDTH,
            )
        return (
            self.speech_bubble._text_width + 28 + self.speech_bubble.TAIL_WIDTH,
            self.speech_bubble._body_height + 2,
        )

    candidates = []
    # 优先放在模型上方，只有上方空间不足时才尝试左右两侧。
    for candidate_side in ("above", "right", "left"):
        bubble_width, bubble_height = bubble_size(candidate_side)
        if candidate_side == "right":
            candidate_x = model_right + margin
            candidate_y = face_center.y() - bubble_height // 2
        elif candidate_side == "left":
            candidate_x = model_left - bubble_width - margin
            candidate_y = face_center.y() - bubble_height // 2
        else:
            candidate_x = face_center.x() - bubble_width // 2
            candidate_y = model_top - bubble_height - margin
        candidates.append((candidate_side, candidate_x, candidate_y, bubble_width, bubble_height))

    side, x, y, bubble_width, bubble_height = candidates[-1]
    x = max(area.left() + 8, min(x, area.right() - bubble_width - 8))
    y = max(area.top() + 8, min(y, area.bottom() - bubble_height - 8))
    for candidate_side, candidate_x, candidate_y, candidate_width, candidate_height in candidates:
        if (
            candidate_x >= area.left() + 8
            and candidate_x + candidate_width <= area.right() - 8
            and candidate_y >= area.top() + 8
            and candidate_y + candidate_height <= area.bottom() - 8
        ):
            side, x, y = candidate_side, candidate_x, candidate_y
            break
    self.speech_bubble.set_side(side)
    self.speech_bubble.move(x, y)
    self.speech_bubble.raise_()


def _show_bubble(self, text):
    if not self.speech_bubble.set_message(text, self._bubble_accent):
        self.speech_bubble.hide()
        return
    if not self.isVisible():
        self.speech_bubble.hide()
        return
    self.speech_bubble.show()
    self._position_speech_bubble()


def _advance_speech_page(self):
    if self._speech_queue:
        page, page_kind = self._speech_queue.pop(0)
        self._speech_playing = True
        self._show_bubble(page)
        duration = 2600 if page_kind == "expression" else max(2400, min(5600, 1500 + len(page) * 150))
        self.speech_timer.start(duration)
        return
    self._speech_playing = False
    self._speech_stream_received = False
    if self._activity_hint_text and self.speech_bubble._message == self._activity_hint_text:
        return
    if getattr(self, "_music_bubble_on", False) and self.speech_bubble._message == "🎶":
        return
    self.speech_bubble.hide()


def _enqueue_speech(self, text):
    pages = self._split_speech_pages(text)
    if not pages:
        return
    self._speech_queue.extend((page, "speech") for page in pages)
    if not self._speech_playing:
        self.speech_timer.stop()
        self._advance_speech_page()


def _enqueue_expression(self, kaomoji):
    expression = str(kaomoji or "").strip()
    if not expression:
        return
    self._speech_queue.append((expression, "expression"))
    if not self._speech_playing:
        self.speech_timer.stop()
        self._advance_speech_page()


def begin_speech_stream(self):
    self.speech_timer.stop()
    self._speech_queue.clear()
    self._speech_stream_buffer = ""
    self._speech_markup_filter.reset()
    self._speech_stream_received = False
    self._speech_playing = False
    self._tts_speech_active = False
    self._pending_expression = None   # 新一轮开始，丢弃上一轮未播放的表情


def prepare_tts_speech(self, pages, kaomoji=None):
    self.speech_timer.stop()
    self.stop_mouth_animation()
    self._speech_queue.clear()
    self._speech_stream_buffer = ""
    self._speech_markup_filter.reset()
    self._speech_stream_received = False
    self._speech_playing = False
    self._tts_speech_active = True
    if kaomoji:
        self._kaomoji = str(kaomoji).strip()
    self._show_bubble("...")


def show_tts_page(self, index, text, words=None):
    if not self._tts_speech_active:
        return
    self.speech_timer.stop()
    self._speech_playing = True
    self._show_bubble(text)
    self.begin_mouth_animation(words)


def finish_tts_speech(self):
    if not self._tts_speech_active:
        return
    self._tts_speech_active = False
    self.stop_mouth_animation()
    self._speech_playing = False
    self._show_bubble(self._kaomoji)
    self.speech_timer.start(2600)


def append_speech_chunk(self, chunk):
    if not chunk:
        return
    self._speech_stream_received = True
    self._speech_stream_buffer += self._speech_markup_filter.feed(chunk)
    self._flush_completed_speech_sentences()


def _flush_completed_speech_sentences(self, final=False):
    """按句末切分，并把紧随其后的引号、括号归入同一页。"""
    text = self._speech_stream_buffer
    sentence_endings = "。！？!?…\n"
    trailing_closers = "”’）】》」』"
    start = 0
    index = 0

    while index < len(text):
        if text[index] not in sentence_endings:
            index += 1
            continue

        end = index + 1
        while end < len(text) and text[end] in sentence_endings:
            end += 1
        while end < len(text) and text[end] in trailing_closers:
            end += 1

        # 流式分块可能刚好结束在句号后，先保留，等待下个分块的右引号。
        if end == len(text) and not final:
            break

        sentence = text[start:end].strip()
        if sentence:
            self._enqueue_speech(sentence)
        start = end
        index = end

    if start:
        self._speech_stream_buffer = text[start:]


def finish_speech_stream(self, full_text, kaomoji=None, append_expression=True):
    if kaomoji:
        self._kaomoji = kaomoji
    if self._speech_stream_received:
        self._speech_stream_buffer += self._speech_markup_filter.feed("", final=True)
        self._flush_completed_speech_sentences(final=True)
        self._enqueue_speech(self._speech_stream_buffer)
    else:
        self._enqueue_speech(full_text)
    self._speech_stream_buffer = ""
    if append_expression:
        self._enqueue_expression(self._kaomoji)


def begin_mouth_animation(self, words=None):
    """语音播放期间按词边界驱动口型；拿不到词边界就走固定节律。"""
    self.stop_mouth_animation()
    if not PET_DRAG_STAGE_ENABLED or self.webview is None:
        return
    marks = []
    for item in (words or []):
        try:
            start = float(item.get("start", 0.0))
            duration = float(item.get("duration", 0.0))
        except Exception:
            continue
        if duration > 0:
            chars = min(3, max(1, len(str(item.get("text", "")))))
            marks.append((start, duration, chars))
    marks.sort(key=lambda row: row[0])
    self._mouth_words = marks
    self._mouth_until = (marks[-1][0] + marks[-1][1]) if marks else 0.0
    self._mouth_started = time.monotonic()
    self.webview.page().runJavaScript(PET_DRAG_STAGE_SCRIPT)
    self._mouth_timer.start()
    self._set_pet_speaking(True)
    # 说话开始：真正播放本回合缓存的表情，与口型一起
    self.activate_pending_expression()


def stop_mouth_animation(self):
    self._mouth_timer.stop()
    self._mouth_words = []
    self._mouth_until = 0.0
    self._mouth_started = 0.0
    if PET_DRAG_STAGE_ENABLED and self.webview is not None:
        self.webview.page().runJavaScript("window.__petMouthSet && window.__petMouthSet(0, 0);")
    self._set_pet_speaking(False)


def _tick_mouth(self):
    if not self._tts_speech_active or self.webview is None:
        self.stop_mouth_animation()
        return
    elapsed = (time.monotonic() - self._mouth_started) * 1000.0
    if self._mouth_words and elapsed > self._mouth_until + PET_MOUTH_HOLD_MS:
        self.stop_mouth_animation()
        return
    opening, jitter = self._mouth_curve(elapsed)
    script = "window.__petMouthSet && window.__petMouthSet(%s, %s);" % (
        round(opening, 3), round(jitter, 3))
    self.webview.page().runJavaScript(script)


def _mouth_curve(self, elapsed_ms):
    """返回 (张合幅度, 抖动幅度)；有词边界按词走，没有就按固定节律开合。"""
    marks = self._mouth_words
    if not marks:
        return 0.16 + 0.42 * abs(math.sin(elapsed_ms / 165.0)), 0.05
    for start, duration, chars in marks:
        if elapsed_ms < start or elapsed_ms > start + duration:
            continue
        u = (elapsed_ms - start) / duration
        return 0.2 + 0.5 * abs(math.sin(math.pi * chars * u)), 0.05
    return 0.08, 0.03


def cancel_speech(self):
    self.speech_timer.stop()
    self._speech_queue.clear()
    self._speech_stream_buffer = ""
    self._speech_markup_filter.reset()
    self._speech_stream_received = False
    self._speech_playing = False
    self._tts_speech_active = False
    self._pending_expression = None
    self.stop_mouth_animation()
    self.speech_bubble.hide()


def set_kaomoji(self, text, style=None):
    self._kaomoji = str(text or "(｡•ᴗ•｡)")
    if style:
        color_match = re.search(r"color:\s*([^;]+)", style)
        if color_match:
            self._bubble_accent = color_match.group(1).strip()
    if (
        getattr(self, "_music_bubble_on", False)
        and self.speech_bubble._message == "🎶"
    ):
        return
    if (
        not self._tts_speech_active
        and not self._speech_playing
        and not self._speech_queue
        and not self._speech_stream_received
    ):
        self.speech_timer.stop()
        self._show_bubble(self._kaomoji)
        self.speech_timer.start(2800)
