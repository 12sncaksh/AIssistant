"""Text-to-speech worker backed by edge-tts and pygame."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading

import edge_tts
import pygame
from PyQt6.QtCore import QThread, pyqtSignal


TTS_AUDIO_LOCK = threading.RLock()


class TextToSpeechThread(QThread):
    error = pyqtSignal(str)
    page_started = pyqtSignal(int, str, object)

    def __init__(self, pages, voice="zh-CN-XiaoxiaoNeural"):
        super().__init__()
        if isinstance(pages, str):
            pages = [pages]
        self.pages = [str(page).strip() for page in (pages or []) if str(page).strip()]
        self.voice = voice
        self._stop_requested = threading.Event()

    def stop(self):
        # pygame must only be controlled by the worker holding the audio lock.
        self._stop_requested.set()

    async def _synthesize(self, page, tmp_path):
        """Synthesize one page and collect word-boundary marks when available."""
        marks = []
        try:
            communicate = edge_tts.Communicate(page, self.voice, boundary="WordBoundary")
            with open(tmp_path, "wb") as audio:
                async for message in communicate.stream():
                    kind = message.get("type")
                    if kind == "audio":
                        audio.write(message["data"])
                    elif kind in ("WordBoundary", "SentenceBoundary"):
                        marks.append({
                            "start": int(message.get("offset", 0)) // 10000,
                            "duration": max(1, int(message.get("duration", 0)) // 10000),
                            "text": str(message.get("text", "")),
                        })
        except Exception as exc:
            logging.warning("TTS 词边界合成失败，退回普通合成：%s", exc)
            communicate = edge_tts.Communicate(page, self.voice)
            await communicate.save(tmp_path)
            marks = []
        return marks

    def run(self):
        async def _speak():
            tmp_paths = []
            word_marks = []
            try:
                for page in self.pages:
                    if self._stop_requested.is_set():
                        return
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                        tmp_path = tmp.name
                    tmp_paths.append(tmp_path)
                    word_marks.append(await self._synthesize(page, tmp_path))

                for index, (page, tmp_path) in enumerate(zip(self.pages, tmp_paths)):
                    if self._stop_requested.is_set():
                        return
                    with TTS_AUDIO_LOCK:
                        if self._stop_requested.is_set():
                            return
                        if not pygame.mixer.get_init():
                            pygame.mixer.init()
                        try:
                            pygame.mixer.music.stop()
                            pygame.mixer.music.load(tmp_path)
                            pygame.mixer.music.play()
                            self.page_started.emit(
                                index, page,
                                word_marks[index] if index < len(word_marks) else [],
                            )
                            while pygame.mixer.music.get_busy():
                                if self._stop_requested.is_set():
                                    pygame.mixer.music.stop()
                                    break
                                self.msleep(100)
                        finally:
                            try:
                                pygame.mixer.music.unload()
                            except Exception:
                                pass
            except Exception as exc:
                logging.error("TTS 语音合成/播放失败：%s", exc)
                self.error.emit(str(exc))
            finally:
                for tmp_path in tmp_paths:
                    for _ in range(20):
                        try:
                            if os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                            break
                        except OSError:
                            self.msleep(200)

        asyncio.run(_speak())
