"""Chat motto fetching and display."""

from __future__ import annotations
from PyQt6.QtCore import QUrl
from PyQt6.QtNetwork import QNetworkReply
from PyQt6.QtNetwork import QNetworkRequest
import json
import random



def fetch_motto(self):
    """异步请求每日一言"""
    # 使用 hitokoto.cn API，返回 JSON 格式
    url = QUrl("https://v1.hitokoto.cn/?c=a&c=b&c=c&c=d&c=e&c=f&c=g&c=h&c=i&c=j&c=k&encode=json&charset=utf-8")
    # 或者使用更简单的：https://api.btstu.cn/yan/api.php?format=json
    request = QNetworkRequest(url)
    self.nam.get(request)


def on_motto_reply(self, reply: QNetworkReply):
    """处理网络返回的每日一言"""
    if reply.error() == QNetworkReply.NetworkError.NoError:
        data = reply.readAll().data()
        try:
            # 解析 JSON
            text = json.loads(data.decode('utf-8')).get('hitokoto', '')
            if text:
                self.setWindowTitle(f"AI桌面助手 - {text}")
                # 可选：保存到 QSettings，以便下次启动时显示，但实时更新即可
                reply.deleteLater()
                return
        except Exception as e:
            print(f"解析一言失败: {e}")
    # 网络错误或解析失败，使用本地备用句子
    self.use_fallback_motto()
    reply.deleteLater()


def use_fallback_motto(self):
    """本地备用句子（网络不可用时）"""
    fallbacks = [
        "每一天都是新的开始 ✨",
        "今天也要加油鸭！ 🦆",
        "保持微笑，好运常在 😊",
        "别急，好事正在路上 🚗"
    ]
    import random
    self.setWindowTitle(f"AI桌面助手 - {random.choice(fallbacks)}")
