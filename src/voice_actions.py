"""Wake word, speech-to-text and text-to-speech actions."""

from __future__ import annotations
from PyQt6.QtWidgets import QMessageBox
from app_paths import get_resource_path
from app_prompts import WAKE_ACK_SPEECH
from tts_thread import TextToSpeechThread
from voice_threads import HAS_AUDIO_DEPS
from voice_threads import WakeWordThread
from voice_threads import XfyunSTTThread
import logging
import queue



# ======================== 语音交互 ========================
def _start_wakeword_listener(self):
    try:
        self.wakeword_thread = WakeWordThread(resource_path=get_resource_path)
        self.wakeword_thread.wakeword_detected.connect(self._on_wakeword_detected)
        self.wakeword_thread.error_occurred.connect(lambda msg: self.status_bar.showMessage(str(msg), 6000))
        self.wakeword_thread.start()
        print("[语音] 唤醒词监听已启动")
    except Exception as e:
        print("[语音] 唤醒词监听启动失败: %s" % e)


def _on_wakeword_detected(self):
    # 录音中再喊「小柚小柚」= 取消本次录音
    if self.stt_thread and self.stt_thread.isRunning():
        self.stt_thread.cancel()
        self.status_bar.showMessage("已取消本次录音", 3000)
        return
    # TTS 播报中（兜底；通常监听已被暂停）
    if self.tts_thread and self.tts_thread.isRunning():
        self._stop_tts()
        self._set_kaomoji_display(text="在呢~ (｡•ᴗ•｡)")
        self.status_bar.showMessage("在呢~", 2000)
        return
    self._set_kaomoji_display(text="在呢~ (｡•ᴗ•｡)")
    self.status_bar.showMessage("在呢~ 请说…", 3000)
    if self.config.get_tts_enabled():
        # 开着语音回复就出声应答；播完再拾音，否则麦克风会把「我在」当成用户输入
        self._speak_wake_ack()
        return
    self._start_voice_input()


def _speak_wake_ack(self):
    """唤醒后播报应答语，并交给 _on_tts_finished 在播完后开始拾音。"""
    voice = self.config.get_tts_voice()
    pages = [WAKE_ACK_SPEECH]
    if self.pet_window:
        self.pet_window.prepare_tts_speech(pages, self.kaomoji_label.text())
    tts_thread = TextToSpeechThread(pages, voice)
    self.tts_thread = tts_thread
    self._pending_voice_input_thread = tts_thread
    self._tts_threads.append(tts_thread)
    tts_thread.page_started.connect(self._on_tts_page_started)
    tts_thread.error.connect(self._on_tts_error)
    tts_thread.finished.connect(self._on_tts_finished)
    if self.wakeword_thread:
        self.wakeword_thread.set_paused(True)
        self.wakeword_thread.suspend()
    tts_thread.start()


def _toggle_voice_input(self):
    if self.stt_thread and self.stt_thread.isRunning():
        self.stt_thread.stop()   # 手动结束，保留已识别文本
        self.status_bar.showMessage("正在识别…", 3000)
        return
    self._start_voice_input()


def _start_voice_input(self):
    if not HAS_AUDIO_DEPS:
        QMessageBox.warning(self, "缺少依赖", "未安装 sounddevice / websocket-client，无法使用语音输入。")
        return
    if self.current_api_thread and self.current_api_thread.isRunning():
        self.status_bar.showMessage("等AI回答完再说吧…", 3000)
        return
    cfg = self.config.get_xfyun_config()
    if not all((cfg["appid"], cfg["api_key"], cfg["api_secret"])):
        QMessageBox.warning(
            self,
            "缺少讯飞配置",
            "请在设置界面或 auth.json 中填写 xfyun_appid / xfyun_api_key / xfyun_api_secret 后重试。",
        )
        return
    if self.wakeword_thread is None:
        QMessageBox.warning(self, "唤醒词未启动", "麦克风唤醒监听未启动，无法使用语音输入。")
        return
    if self.stt_thread and self.stt_thread.isRunning():
        return
    # 开始录音前打断正在播报的 TTS
    self._stop_tts()
    audio_queue = queue.Queue(maxsize=300)
    self.stt_thread = XfyunSTTThread(cfg["appid"], cfg["api_key"], cfg["api_secret"], audio_queue)
    self.stt_thread.partial_text.connect(self._on_stt_partial)
    self.stt_thread.final_text.connect(self._on_stt_final)
    self.stt_thread.canceled.connect(self._on_stt_canceled)
    self.stt_thread.error_occurred.connect(self._on_stt_error)
    self.stt_thread.recording_started.connect(self._on_stt_started)
    self.wakeword_thread.set_listener(audio_queue)
    self.mic_btn.setText("\u23FA 聆听中…")
    self.status_bar.showMessage("聆听中…（再喊「小柚小柚」可取消）", 0)
    self.stt_thread.start()


def _on_stt_started(self):
    self.mic_btn.setText("\u23FA 聆听中…")
    self.status_bar.showMessage("聆听中…（再喊「小柚小柚」可取消）", 0)


def _on_stt_partial(self, text):
    short = str(text).strip()
    if len(short) > 40:
        short = short[-40:]
    self.status_bar.showMessage("识别中: %s" % short, 0)


def _on_stt_final(self, text):
    self._stop_voice_input()
    text = (text or "").strip()
    if not text:
        return
    self.input_text.setPlainText(text)
    self.input_text.setFocus()
    self.send_message()


def _on_stt_canceled(self):
    self._stop_voice_input()
    self.status_bar.showMessage("已取消本次录音", 3000)


def _on_stt_error(self, msg):
    logging.error("语音识别错误：%s", msg)
    self._stop_voice_input()
    self.status_bar.showMessage(str(msg), 6000)


def _stop_voice_input(self):
    if self.wakeword_thread:
        self.wakeword_thread.set_listener(None)
    if self.stt_thread and self.stt_thread.isRunning():
        self.stt_thread.stop()
        if not self.stt_thread.wait(3000):
            self.stt_thread.terminate()
            self.stt_thread.wait()
    self.stt_thread = None
    self.mic_btn.setText("\U0001F3A4")


def _stop_tts(self, wait_ms=500):
    active_threads = [thread for thread in self._tts_threads if thread.isRunning()]
    for thread in active_threads:
        thread.stop()
    if active_threads and self.pet_window:
        self.pet_window.cancel_speech()
    for thread in active_threads:
        thread.wait(wait_ms)
    self._tts_threads = [thread for thread in self._tts_threads if thread.isRunning()]
    if self.tts_thread and not self.tts_thread.isRunning():
        self.tts_thread = None
    if self.wakeword_thread:
        self.wakeword_thread.set_paused(False)
        self.wakeword_thread.resume()
