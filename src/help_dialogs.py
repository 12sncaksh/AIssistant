"""Small help/about dialogs."""

from __future__ import annotations
from PyQt6.QtWidgets import QMessageBox
from version import __version__ as APP_VERSION



def show_speech_config_help(self):
    QMessageBox.information(
        self,
        "语音使用说明",
        "语音听写使用讯飞开放平台「语音听写（流式版）」接口。\n\n"
        "在「设置 → 语音」中填写讯飞配置，或直接在 config/auth.json 中填写：\n"
        "  xfyun_appid\n  xfyun_api_key\n  xfyun_api_secret\n\n"
        "点击输入框旁的 🎤 开始语音对话；喊「小柚」可唤醒或取消录音。",
    )


def show_config_file_help(self):
    QMessageBox.information(
        self,
        "配置文件说明",
        "config/config.json 保存非敏感的 API 配置，例如 API Base URL、聊天模型、视觉模型、推理强度和 SearXNG URL。\n\n"
        "config/auth.json 只保存 API Key、Tavily、高德和讯飞密钥。\n\n"
        "config/apps.json 保存常用应用快捷方式，模板为 config/templates/apps.example.json。列表外应用可以在确认后通过命令或视觉键鼠打开。\n\n"
        "聊天输入区支持图片和文本文件附件；图片需要配置支持图片输入的视觉模型。\n\n"
        "输入区可以快捷切换默认/自由角色扮演模式和推理强度，其他完整配置仍在设置窗口中。\n\n"
        "读取优先级为：设置窗口中已保存的值 → config/config.json 或 config/auth.json → 程序默认值。\n"
        "设置窗口中的用户值优先；配置文件缺少某项时，会按字段自动回退。\n\n"
        "两个文件都应放在程序目录的 config 文件夹中。config/auth.json 不应提交到代码仓库。",
    )


def show_about(self):
    QMessageBox.about(self, "关于AI助手",
                      f"Aissistant v{APP_VERSION} (fast版 / PyQt6)\n"
                      "功能：多会话聊天、图片/文件附件、视觉键鼠、UI Automation、Live2D、语音输入与回复\n"
                      "技术栈：Python + PyQt6 + OpenAI兼容API + Live2D + edge-tts")
