"""Speech-recognition and wake-word audio workers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import sys
import time
from urllib.parse import urlencode

try:
    import numpy as np
    import sounddevice as sd
    import websocket
    HAS_AUDIO_DEPS = True
except ImportError:
    np = None
    sd = None
    websocket = None
    HAS_AUDIO_DEPS = False

from PyQt6.QtCore import QThread, pyqtSignal


class XfyunSTTThread(QThread):
    partial_text = pyqtSignal(str)
    final_text = pyqtSignal(str)
    canceled = pyqtSignal()
    error_occurred = pyqtSignal(str)
    recording_started = pyqtSignal()

    def __init__(self, appid, api_key, api_secret, audio_queue, parent=None):
        super().__init__(parent)
        self.appid = appid
        self.api_key = api_key
        self.api_secret = api_secret
        self.audio_queue = audio_queue
        self._running = True
        self._cancel = False
        self._last_partial = ""

    def stop(self):
        self._running = False

    def cancel(self):
        self._cancel = True
        self._running = False

    def _build_auth_url(self):
        host = "iat-api.xfyun.cn"
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        request_line = "GET /v2/iat HTTP/1.1"
        signature_origin = f"host: {host}\ndate: {date}\n{request_line}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        authorization = base64.b64encode(signature).decode("utf-8")
        auth_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{authorization}"'
        )
        auth_base64 = base64.b64encode(auth_origin.encode("utf-8")).decode("utf-8")
        params = {"host": host, "date": date, "authorization": auth_base64}
        return f"wss://{host}/v2/iat?{urlencode(params)}"

    def _send_data(self, ws, status, audio_bytes=b"", first=False):
        frame = {
            "data": {
                "status": status,
                "format": "audio/L16;rate=16000",
                "encoding": "raw",
                "audio": base64.b64encode(audio_bytes).decode("utf-8"),
            }
        }
        if first:
            frame["common"] = {"app_id": self.appid}
            frame["business"] = {
                "language": "zh_cn",
                "domain": "iat",
                "accent": "mandarin",
                "vad_eos": 2000,
                "ptt": 1,
            }
        ws.send(json.dumps(frame))

    def _drain_ws(self, ws):
        try:
            while True:
                ws.settimeout(0.05)
                message = ws.recv()
                data = json.loads(message)
                code = data.get("code", 0)
                if code != 0:
                    self.error_occurred.emit("讯飞听写错误: %s" % data.get("message", code))
                    return True
                result = data.get("data", {})
                if not result or not result.get("result"):
                    continue
                text = self._parse_result(result)
                self._full_text += text
                if result.get("status") == 2:
                    self.final_text.emit(self._full_text)
                    return True
                if text:
                    self._last_partial = self._full_text
                    self.partial_text.emit(self._full_text)
        except websocket.WebSocketTimeoutException:
            return False
        except (websocket.WebSocketConnectionClosedException, OSError, ValueError):
            return True
        except Exception:
            return True

    @staticmethod
    def _parse_result(result):
        parts = []
        for item in result.get("result", {}).get("ws", []) or []:
            for candidate in item.get("cw", []) or []:
                parts.append(candidate.get("w", ""))
        return "".join(parts)

    def run(self):
        if not (self.appid and self.api_key and self.api_secret):
            self.error_occurred.emit("未配置讯飞听写密钥，请在设置界面或 auth.json 中填写 xfyun_appid / xfyun_api_key / xfyun_api_secret")
            return
        if not HAS_AUDIO_DEPS:
            self.error_occurred.emit("缺少音频依赖: sounddevice / websocket-client")
            return
        self._running = True
        self._cancel = False
        self._last_partial = ""
        self._full_text = ""
        ws = None
        try:
            ws = websocket.create_connection(self._build_auth_url(), timeout=30)
            self.recording_started.emit()
            buf = b""
            got_final = False
            first_sent = False
            while self._running and not self._cancel:
                try:
                    block = self.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                buf += block.tobytes()
                while len(buf) >= 1280:
                    frame, buf = buf[:1280], buf[1280:]
                    if not first_sent:
                        self._send_data(ws, 0, frame, first=True)
                        first_sent = True
                    else:
                        self._send_data(ws, 1, frame)
                    if self._drain_ws(ws):
                        got_final = True
                        self._running = False
                        break
                if not self._running:
                    break
            if not first_sent:
                self._send_data(ws, 0, first=True)
            if self._cancel:
                try:
                    self._send_data(ws, 2)
                except Exception:
                    pass
                self.canceled.emit()
                return
            if not got_final:
                try:
                    self._send_data(ws, 2)
                except Exception:
                    pass
                deadline = time.time() + 3.0
                while time.time() < deadline and not got_final:
                    if self._drain_ws(ws):
                        got_final = True
                    time.sleep(0.05)
            if got_final:
                return
            if self._last_partial:
                self.final_text.emit(self._last_partial)
            else:
                self.canceled.emit()
        except Exception as exc:
            self.error_occurred.emit("讯飞听写失败: %s" % exc)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass


def _default_resource_path(*parts):
    if getattr(sys, "frozen", False):
        runtime_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        runtime_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    external_path = os.path.join(runtime_dir, *parts)
    if os.path.exists(external_path):
        return external_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


class WakeWordThread(QThread):
    wakeword_detected = pyqtSignal()
    error_occurred = pyqtSignal(str)

    SAMPLE_RATE = 16000
    BLOCK_SIZE = 320

    def __init__(self, parent=None, resource_path=None):
        super().__init__(parent)
        self._resource_path = resource_path or _default_resource_path
        self._running = True
        self._paused = False
        self._suspend_requested = False
        self._spotter = None
        self._listener_queue = None
        self._audio_queue = queue.Queue(maxsize=300)

    def _init_spotter(self):
        if self._resource_path is None:
            self.error_occurred.emit("未配置唤醒词资源路径解析器")
            return
        base = self._resource_path("generated", "kws")
        files = {
            "tokens": os.path.join(base, "tokens.txt"),
            "encoder": os.path.join(base, "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            "decoder": os.path.join(base, "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            "joiner": os.path.join(base, "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
            "keywords": os.path.join(base, "keywords.txt"),
        }
        if not all(os.path.isfile(path) for path in files.values()):
            self.error_occurred.emit("唤醒词模型缺失: %s" % base)
            return
        try:
            import sherpa_onnx
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=files["tokens"],
                encoder=files["encoder"],
                decoder=files["decoder"],
                joiner=files["joiner"],
                keywords_file=files["keywords"],
                num_threads=2,
                provider="cpu",
            )
        except Exception as exc:
            self.error_occurred.emit("唤醒词模型加载失败: %s" % exc)

    def set_paused(self, paused):
        self._paused = paused

    def suspend(self):
        self._suspend_requested = True

    def resume(self):
        self._suspend_requested = False

    def set_listener(self, audio_queue):
        self._listener_queue = audio_queue

    def _on_audio(self, indata, frames, time_info, status):
        try:
            self._audio_queue.put_nowait(np.copy(indata[:, 0]))
        except queue.Full:
            pass

    def run(self):
        if not HAS_AUDIO_DEPS:
            self.error_occurred.emit("缺少音频依赖: sounddevice / numpy")
            return
        if self._spotter is None:
            self._init_spotter()
        if self._spotter is None:
            return
        stream = None
        try:
            stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=self.BLOCK_SIZE,
                callback=self._on_audio,
            )
            stream.start()
        except Exception as exc:
            self.error_occurred.emit("麦克风打开失败: %s" % exc)
            return
        spot_stream = self._spotter.create_stream()
        stream_started = True
        try:
            while self._running:
                if self._suspend_requested and stream_started:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                    stream_started = False
                elif not self._suspend_requested and not stream_started:
                    try:
                        stream.start()
                    except Exception:
                        pass
                    stream_started = True
                try:
                    block = self._audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._listener_queue is not None:
                    try:
                        self._listener_queue.put_nowait(block)
                    except queue.Full:
                        pass
                if self._paused:
                    continue
                samples = block.astype(np.float32) / 32768.0
                if self._spotter.is_ready(spot_stream):
                    self._spotter.decode_stream(spot_stream)
                    result = self._spotter.get_result(spot_stream)
                    if result:
                        self._spotter.reset_stream(spot_stream)
                        self.wakeword_detected.emit()
                else:
                    spot_stream.accept_waveform(self.SAMPLE_RATE, samples)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    def stop(self):
        self._running = False
