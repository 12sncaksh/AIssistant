"""识别「用户在回忆历史」的意图，并据此构造历史片段。

DSH 路径和本地 API 路径**必须共用这一套判断**。两边各写一份触发词的话，迟早
会漂移成「在 DSH 里能回忆、切到本地 API 就回忆不了」这种看不见的不一致 ——
这正是这个模块存在的唯一理由。

两种意图要分开，因为它们对应完全不同的处理：

* ``wants_full_recall``   —— 「把完整聊天记录整理一下」
  用户要的是全部原文，压缩上下文答不出来，必须提供未截断的历史。

* ``wants_session_start`` —— 「第一句话是什么」
  问的是**位置**，不是语义。向量检索对"最早/开头"这类位置概念本来就不擅长，
  所以直接按顺序取开头几条，比检索更准也更便宜。
"""

from __future__ import annotations

# 「要全部」+「聊的是记录」两个条件必须同时成立才触发完整回顾。
# 只靠"全部"会把「全部删掉」这类普通句子误判；只靠"记录"会把
# 「记录一下这个待办」误判。
FULL_RECALL_CUES = ("完整", "全部", "所有", "整个", "从头到尾", "每一句")
FULL_RECALL_TARGETS = ("聊天记录", "对话记录", "历史记录", "历史", "对话", "记录")

# 位置型提问：问的是"会话最开头说了什么"。
SESSION_START_CUES = (
    "第一句话", "第一句", "第一条消息", "第一条",
    "最早说", "最早的消息", "最开始说", "会话开始", "开头说",
)

# 位置型提问最多给开头几条：够回答"第一句是什么"，又不会把整个会话塞进去。
SESSION_START_LIMIT = 4


def wants_full_recall(text):
    """用户是否明确要求完整回顾历史。"""
    body = str(text or "")
    if not body:
        return False
    return any(cue in body for cue in FULL_RECALL_CUES) and any(
        target in body for target in FULL_RECALL_TARGETS
    )


def wants_session_start(text):
    """用户是否在问会话开头（位置型提问）。"""
    body = str(text or "")
    if not body:
        return False
    return any(cue in body for cue in SESSION_START_CUES)


def dialogue_lines(messages, per_message_chars=0):
    """把消息列表渲染成「用户：…／助手：…」文本，跳过空内容与非对话角色。"""
    lines = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = message_text(message.get("content")).strip()
        if not text:
            continue
        if per_message_chars and len(text) > per_message_chars:
            text = text[:per_message_chars] + "…（截断）"
        lines.append(("用户：" if role == "user" else "助手：") + text)
    return lines


def message_text(content):
    """把消息内容（可能是多模态列表）压成纯文本。

    与 dsh_acp_client._message_text 保持逐字节一致的语义，这样 DSH 改用本模块
    渲染历史片段时行为不变。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts)
    return ""


def format_session_start_block(messages, per_message_chars=0, limit=SESSION_START_LIMIT):
    """会话开头的原文片段；没有内容时返回空串。"""
    lines = dialogue_lines(messages, per_message_chars)[: max(1, int(limit))]
    if not lines:
        return ""
    return (
        "【按需检索：以下是本地会话最早保存的几条原始记录。】\n"
        + "\n".join(lines)
        + "\n【以上是历史记录，请据此回答用户关于会话开头的问题。】"
    )


def format_full_recall_block(messages):
    """未截断的完整历史；没有内容时返回空串。"""
    lines = dialogue_lines(messages)
    if not lines:
        return ""
    return (
        "【按需检索：以下是本地会话保存的原始聊天记录，按时间顺序完整提供，未做轮数或字符截断。"
        "回答关于旧对话的问题时，以此记录为准；若原文中找不到答案，明确说明。】\n"
        + "\n".join(lines)
    )
