"""Desktop-pet window wiring and Live2D page actions."""

from __future__ import annotations
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QMessageBox
from app_paths import get_resource_path
from app_paths import get_runtime_dir
from datetime import datetime
from live2d_catalog import get_live2d_model_catalog
from live2d_server import Live2DRequestHandler
from live2d_server import LocalHTTPServer
from urllib.parse import quote
import json
import logging
import os
import random
import webbrowser



def bind_pet_window(self, pet_window):
    self.pet_window = pet_window
    self._sync_kaomoji_to_pet()
    self._refresh_pet_param_catalog()


def _apply_pet_command(self, payload):
    """工具线程经信号转到主线程：表情指令只登记，等回复播报/说话时才真正执行。"""
    if not self.pet_window or not isinstance(payload, dict):
        return
    kind = str(payload.get("kind") or "")
    if kind not in ("emotion", "param", "clear"):
        return
    try:
        logging.info("[pet-exp] 收到桌宠指令：%s", payload)
        self.pet_window.request_pet_expression(payload)
    except Exception:
        logging.warning("登记桌宠指令失败：%s", payload, exc_info=True)


def _sync_kaomoji_to_pet(self):
    if self.pet_window:
        self.pet_window.set_kaomoji(self.kaomoji_label.text(), self.kaomoji_label.styleSheet())


def _set_kaomoji_display(self, text=None, style=None):
    if text is not None:
        self.kaomoji_label.setText(text)
    if style is not None:
        self.kaomoji_label.setStyleSheet(style)
    self._sync_kaomoji_to_pet()


def _live2d_page_url(self, page):
    model = self._get_selected_live2d_model()
    query = f"?model={quote(model['id'])}&file={quote(model['file'])}"
    return self.live2d_server.get_url(page) + query


def set_pet_model(self, model_id):
    model = next((item for item in get_live2d_model_catalog() if item["id"] == model_id), None)
    if not model:
        QMessageBox.warning(self, "模型不可用", "没有找到有效的 Live2D 模型资源。")
        return
    self.config.set_pet_model_name(model["id"])
    self._refresh_pet_param_catalog()
    if self.show_live2d_panel and self.webview and self.live2d_server:
        self.webview.setUrl(QUrl(self._live2d_page_url("index.html")))
    if self.pet_window:
        self.pet_window.load_live2d_model()
    self.status_bar.showMessage(f"已切换桌宠模型：{model['label']}", 3000)


def init_live2d_server(self):
    web_dir = get_resource_path("assets", "web_resources")
    dist_dir = os.path.join(web_dir, "dist")
    try:
        Live2DRequestHandler.app = self
        self.live2d_server = LocalHTTPServer(dist_dir, port=8123)
        self.live2d_server.start()
    except Exception as e:
        self.live2d_server = None
        logging.exception("Live2D 本地服务器启动失败：%s", e)
        return
    self._publish_pet_endpoint()
    if self.show_live2d_panel and self.webview:
        QTimer.singleShot(1000, lambda: self.webview.setUrl(QUrl(self._live2d_page_url("index.html"))))


def _publish_pet_endpoint(self):
    """把 Live2D 本地服务的实际监听地址写给桌宠 MCP server。

    端口不是固定的：LocalHTTPServer 从 8123 起顺延找可用端口，所以必须运行时发布。
    主机名写 localhost 与 HTTPServer 的绑定名保持一致（写 127.0.0.1 在只绑到 ::1 时会连不上）。
    """
    server = getattr(self, "live2d_server", None)
    if server is None:
        return
    path = os.path.join(get_runtime_dir(), "data", "runtime_endpoint.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "pet": {"host": "localhost", "port": int(server.port)},
            "pid": os.getpid(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        logging.info("桌宠本地接口已发布：%s", path)
    except Exception:
        logging.warning("写 runtime_endpoint.json 失败：%s", path, exc_info=True)


def reload_live2d_model(self):
    if self.webview and self.live2d_server and self.show_live2d_panel:
        self.webview.setUrl(QUrl(self._live2d_page_url("index.html")))
    if self.pet_window:
        self.pet_window.load_live2d_model()
    if self.show_live2d_panel or self.pet_window:
        self.status_bar.showMessage("重新加载Live2D模型...", 2000)


def open_model_directory(self):
    model_dir = get_resource_path("assets", "web_resources", "dist", "Resources")
    os.makedirs(model_dir, exist_ok=True)
    webbrowser.open(f"file://{model_dir}")


def _update_kaomoji_by_text(self, text: str):
    """根据 AI 回复的内容分析情绪，并更新顶部颜文字标签"""
    if not text:
        self._set_kaomoji_display(
            text="(｡•ᴗ•｡)",
            style="font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: #333333;"
        )
        return

    # 情绪关键词（可根据喜好扩展）
    happy_keywords = ["开心", "高兴", "喜欢", "爱", "棒", "好", "有趣", "笑", "愉快", "美好", "幸福", "耶", "嘻嘻", "哈哈"]
    sad_keywords = ["难过", "伤心", "悲伤", "生气", "愤怒", "讨厌", "烦", "痛苦", "郁闷", "失望", "唉", "呜呜"]
    angry_keywords = ["气死", "火大", "炸了", "可恶", "恨", "暴怒"]
    surprised_keywords = ["哇", "哦", "真的吗", "惊讶", "神奇", "没想到", "居然", "竟然"]

    # 颜文字池
    happy_kaomoji = ["(｡•̀ᴗ•́｡)", "(◕‿◕✿)", "(＾▽＾)", "(´｡• ᵕ •｡`)", "(｡♥‿♥｡)", "٩(◕‿◕｡)۶", "(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧"]
    sad_kaomoji = ["(；ω；)", "(｡•́︿•̀｡)", "(╥﹏╥)", "(T_T)", "(>_<)", "(｡╯︵╰｡)", "(´;ω;`)"]
    angry_kaomoji = ["(╯°□°）╯︵ ┻━┻", "(｀Д´)", "(ノಠ益ಠ)ノ", "(>:-<)", "(＃`Д´)", "(ꐦ°᷄д°᷅)", "(◣_◢)"]
    surprised_kaomoji = ["(°ロ°) !", "(⊙ˍ⊙)", "(⊙_⊙)", "(°o°)", "(º﹃º)", "(⊙ω⊙)", "Σ(°△°|||)"]
    neutral_kaomoji = ["(｡•ᴗ•｡)", "(￣ω￣)", "(´-ω-`)", "(•_•)", "(._.)", "(｡-ω-｡)", "(-ω-)"]

    text_lower = text.lower()
    happy_score = sum(1 for kw in happy_keywords if kw in text_lower or kw in text)
    sad_score = sum(1 for kw in sad_keywords if kw in text_lower or kw in text)
    angry_score = sum(1 for kw in angry_keywords if kw in text_lower or kw in text)
    surprised_score = sum(1 for kw in surprised_keywords if kw in text_lower or kw in text)

    scores = {
        "happy": happy_score,
        "sad": sad_score,
        "angry": angry_score,
        "surprised": surprised_score,
        "neutral": 1   # 基础分，避免无匹配时随机到空
    }

    max_emotion = max(scores, key=scores.get)
    if max_emotion == "happy":
        chosen = random.choice(happy_kaomoji)
    elif max_emotion == "sad":
        chosen = random.choice(sad_kaomoji)
    elif max_emotion == "angry":
        chosen = random.choice(angry_kaomoji)
    elif max_emotion == "surprised":
        chosen = random.choice(surprised_kaomoji)
    else:
        chosen = random.choice(neutral_kaomoji)

    # 根据情绪设置颜色
    if max_emotion == "happy":
        color = "#FF69B4"      # 粉红
    elif max_emotion == "sad":
        color = "#4A90E2"      # 淡蓝
    elif max_emotion == "angry":
        color = "#E94F4F"      # 红色
    elif max_emotion == "surprised":
        color = "#FFB347"      # 橙色
    else:
        color = "#000000"      # 黑色
    self._set_kaomoji_display(
        text=chosen,
        style=f"font-weight: bold; background-color: #f5f5f5; padding: 5px; font-size: 18px; color: {color};"
    )


def _show_thinking_kaomoji(self):
    """显示思考中的颜文字"""
    thinking = ["( •_• )?", "( ˘•ω•˘ )", "(｡•́︿•̀｡)", "(*´･ω･)", "(・・?)"]
    self._set_kaomoji_display(text=random.choice(thinking))
