"""Markdown / reasoning-tag text processing for chat rendering.

Moved out of main.py so chat widgets can render without importing the app module.
"""

from __future__ import annotations

import html
import re

from PyQt6.QtGui import QColor, QTextDocument

from chat_style import (
    clamp_chat_font_size,
    clamp_opacity,
    color_to_rgba,
    normalize_hex_color,
)

DEFAULT_CODE_BLOCK_COLOR = "#0F172A"

INTERNAL_REASONING_BLOCK_PATTERN = re.compile(
    r"<\s*(thinking|think|analysis|reasoning)\b[^>]*>.*?</\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
INTERNAL_REASONING_OPEN_PATTERN = re.compile(
    r"<\s*(thinking|think|analysis|reasoning)\b[^>]*>", re.IGNORECASE
)
INTERNAL_REASONING_CLOSE_PATTERN = re.compile(
    r"</\s*(thinking|think|analysis|reasoning)\s*>", re.IGNORECASE
)

MARKDOWN_FENCE_PATTERN = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*(?P<language>[^\n]*)\n"
    r"(?P<code>.*?)(?:^(?P=indent)[ \t]*(?P=fence)[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)


def strip_internal_reasoning(text: str) -> str:
    """移除模型内部推理标签，避免 thinking 内容进入聊天、历史记录或 TTS。"""
    text = str(text or "")
    text = INTERNAL_REASONING_BLOCK_PATTERN.sub("", text)
    opening = INTERNAL_REASONING_OPEN_PATTERN.search(text)
    if opening:
        closing = INTERNAL_REASONING_CLOSE_PATTERN.search(text, opening.end())
        if not closing:
            text = text[:opening.start()]
    return text.strip()


def markdown_to_plain_text(text):
    """把 Markdown 转成适合历史记录和 TTS 的纯文本，不读取格式符号。"""
    text = strip_internal_reasoning(str(text or ""))
    text = MARKDOWN_FENCE_PATTERN.sub(lambda match: match.group("code"), text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$", "", text, flags=re.MULTILINE)
    text = text.replace("|", " ")
    text = re.sub(r"(\*\*|__|~~|`|\*|_)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pet_bubble_text(text):
    """Remove fenced code, then convert Markdown to plain pet-bubble text."""
    without_code = MARKDOWN_FENCE_PATTERN.sub("", str(text or ""))
    return markdown_to_plain_text(without_code)


def _sanitize_markdown_segment(markdown_text):
    """移除 Markdown 图片和危险链接，避免 Qt 富文本加载本地资源。"""
    markdown_text = re.sub(
        r"<(https?://[^>\s]+)>",
        lambda match: f"[{match.group(1)}]({match.group(1)})",
        markdown_text,
        flags=re.IGNORECASE,
    )
    markdown_text = re.sub(r"</?[A-Za-z][^>]*>", "", markdown_text)
    markdown_text = re.sub(
        r"!\[([^\]]*)\]\([^)]+\)",
        lambda match: match.group(1),
        markdown_text,
    )

    def replace_link(match):
        label = match.group(1)
        target = match.group(2).strip()
        if re.match(r"^https?://", target, re.IGNORECASE):
            return match.group(0)
        return label

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_link, markdown_text)


def _markdown_segment_to_html(markdown_text):
    if not str(markdown_text or "").strip():
        return ""
    document = QTextDocument()
    document.setMarkdown(_sanitize_markdown_segment(markdown_text))
    rendered = document.toHtml()
    body_match = re.search(r"<body[^>]*>(.*)</body>", rendered, re.IGNORECASE | re.DOTALL)
    return body_match.group(1) if body_match else rendered


def render_markdown_to_html(
    markdown_text,
    code_blocks=None,
    code_font_family="Consolas",
    code_font_size=10,
    code_opacity=0.85,
    code_color=DEFAULT_CODE_BLOCK_COLOR,
):
    """渲染受限 Markdown；围栏代码块由应用统一控制，不交给模型指定样式。"""
    markdown_text = str(markdown_text or "")
    code_blocks = code_blocks if code_blocks is not None else []
    parts = []
    cursor = 0
    code_index = 0
    code_opacity = clamp_opacity(code_opacity)
    base_color = QColor(normalize_hex_color(code_color, DEFAULT_CODE_BLOCK_COLOR))
    luminance = 0.299 * base_color.red() + 0.587 * base_color.green() + 0.114 * base_color.blue()
    if luminance < 150:
        header_color = base_color.lighter(125).name()
        border_color = base_color.lighter(160).name()
        code_text_color = "#E5E7EB"
        language_color = "#93C5FD"
        copy_color = "#DBEAFE"
    else:
        header_color = base_color.darker(112).name()
        border_color = base_color.darker(135).name()
        code_text_color = "#111827"
        language_color = "#1D4ED8"
        copy_color = "#1E3A8A"
    header_rgba = color_to_rgba(header_color, code_opacity)
    body_rgba = color_to_rgba(base_color.name(), code_opacity)
    border_rgba = color_to_rgba(border_color, code_opacity)
    for match in MARKDOWN_FENCE_PATTERN.finditer(markdown_text):
        parts.append(_markdown_segment_to_html(markdown_text[cursor:match.start()]))
        language = re.sub(r"[^A-Za-z0-9_+#.-]+", "", match.group("language").strip())[:24]
        code = match.group("code").rstrip("\n")
        code_blocks.append(code)
        language_label = html.escape((language or "code").upper())
        code_html = html.escape(code)
        code_family = html.escape(str(code_font_family or "Consolas"), quote=True)
        code_size = clamp_chat_font_size(code_font_size)
        copy_url = f"codex-copy:{code_index}"
        parts.append(
            '<div style="margin:10px 0 12px 0;">'
            '<table cellspacing="0" cellpadding="0" width="100%" '
            f'style="background-color:{body_rgba}; border:1px solid {border_rgba}; '
            'border-radius:12px;">'
            '<tr>'
            f'<td style="padding:7px 11px; background-color:{header_rgba}; '
            f'color:{language_color}; font-family:Consolas,monospace; font-size:9pt;">'
            f'{language_label}</td>'
            f'<td align="right" style="padding:7px 11px; background-color:{header_rgba};">'
            f'<a href="{copy_url}" style="color:{copy_color}; text-decoration:none; '
            'font-size:9pt;">复制代码</a></td>'
            '</tr>'
            f'<tr><td colspan="2" style="padding:12px 14px; background-color:{body_rgba};">'
            f'<pre style="font-family:\'{code_family}\',Consolas,monospace; '
            f'font-size:{code_size}pt; color:{code_text_color}; line-height:145%; '
            'white-space:pre-wrap; margin:0;">'
            f'{code_html}</pre></td></tr></table></div>'
        )
        code_index += 1
        cursor = match.end()
    parts.append(_markdown_segment_to_html(markdown_text[cursor:]))
    return "".join(parts)


def build_sources_block(sources) -> str:
    """把结构化来源列表拼成“来源：[n] 标题 / 链接：url”文本块。"""
    if not sources:
        return ""
    lines = ["来源："]
    for index, item in enumerate(sources, 1):
        title = str(item.get("title", "") or "").strip() or "无标题"
        url = str(item.get("url", "") or "").strip()
        lines.append(f"[{index}] {title}")
        lines.append(f"链接：{url}")
    return "\n".join(lines)


def attach_sources_to_text(text, sources) -> str:
    """把来源块追加到正文后面，与实时回答时的拼装格式保持一致。"""
    block = build_sources_block(sources)
    if not block:
        return text
    return f"{text}\n\n{block}" if text else block
