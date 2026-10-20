"""语音配置的共用定义：向导与设置页共用，避免两处说法分叉。

只放纯数据与纯函数，不导入 Qt —— 向导、设置页、摘要谁都能直接用。

事实来源（写死在这里，改之前先看代码）：
- 合成（TTS）只有 edge-tts 一种实现，见 tts_thread.py；
  讯飞没有合成实现，只提供听写（voice_threads.py 的 iat-api.xfyun.cn）。
  所以「讯飞音色」绝不能作为引擎选项出现 —— 那会让用户以为换了音色。
- 听写（ASR）只有讯飞一家，靠 AppID / API Key / API Secret 三个值。
- 唤醒词在本机离线跑（sherpa-onnx KeywordSpotter），不需要任何配置。
"""

from __future__ import annotations

# 合成引擎选项：(显示文字, 配置值)。只有 off / edge 两种真实状态。
VOICE_ENGINE_ITEMS = (
    ("不启用（只出文字）", "off"),
    ("edge-tts（微软语音，联网即用，不需要 Key）", "edge"),
)

VOICE_ENGINE_HINT = (
    "合成只有 edge-tts 一种，不需要任何 Key；具体音色可以在设置里再挑。"
)

XFYUN_GROUP_TITLE = "讯飞语音听写（把你说的话变成文字）"

XFYUN_HINT = (
    "三个值来自讯飞开放平台「语音听写（流式版）」。"
    "它们只用于听写，不是聊天 Key，也不会用于合成。"
)

WAKE_WORD_HINT = (
    "唤醒词「小柚小柚」在本机离线运行，不需要配置，装好就有。"
)

VOICE_INPUT_HINT = (
    "语音输入 = 喊「小柚小柚」唤醒 + 讯飞听写。"
    "不填这三个值也能点麦克风，但会提示未配置听写密钥。"
)


def engine_of(config) -> str:
    """当前合成引擎。底层只有 edge，开关关掉就是 off。"""
    return "edge" if config.get_tts_enabled() else "off"


def xfyun_ready(config) -> bool:
    """讯飞听写的三个值是否都填了。"""
    return all(
        (
            str(config.get_xfyun_appid() or "").strip(),
            str(config.get_xfyun_api_key() or "").strip(),
            str(config.get_xfyun_api_secret() or "").strip(),
        )
    )


def describe(config) -> str:
    """一行摘要，向导完成页与以后的设置页共用。"""
    tts = "开（edge-tts）" if config.get_tts_enabled() else "关"
    asr = "已配讯飞听写" if xfyun_ready(config) else "未配（麦克风会提示缺密钥）"
    return f"语音回复 {tts}；语音输入 {asr}"
