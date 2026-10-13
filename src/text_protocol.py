"""Text protocol helpers: DSML / text tool calls, kaomoji blocks, sources.

Leaf module (no Qt, no app imports) shared by the request layer and the pet UI.
"""

from __future__ import annotations

import json
import re
import uuid

from chat_markdown import MARKDOWN_FENCE_PATTERN, strip_internal_reasoning


KAOMOJI_TAG_PATTERN = re.compile(
    r"<\s*kaomo[a-z]{2,4}\s*>(.*?)</\s*kaomo[a-z]{2,4}\s*>",
    re.IGNORECASE | re.DOTALL
)
KAOMOJI_BROKEN_CLOSING_PATTERN = re.compile(
    r"</\s*kaomo[a-z]{2,4}\s*>([^<\n]{1,80}?)</\s*kaomo[a-z]{2,4}\s*>",
    re.IGNORECASE | re.DOTALL
)
KAOMOJI_TAG_CLEANUP_PATTERN = re.compile(r"</?\s*kaomo[a-z]{2,4}\s*>", re.IGNORECASE)
DSML_TAG_PATTERN = re.compile(r"</?｜｜DSML｜｜[^>]*>", re.IGNORECASE)
DSML_INVOKE_PATTERN = re.compile(
    r"<｜｜DSML｜｜invoke\s+name=\"([^\"]+)\"\s*>(.*?)</｜｜DSML｜｜invoke>",
    re.IGNORECASE | re.DOTALL
)
DSML_PARAMETER_PATTERN = re.compile(
    r"<｜｜DSML｜｜parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</｜｜DSML｜｜parameter>",
    re.IGNORECASE | re.DOTALL
)
TEXT_TOOL_CALL_MARKER_PATTERN = re.compile(
    r"(?:^|\s)to\s*=\s*(?:functions\.)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:代json|json)?\s*",
    re.IGNORECASE,
)


def sanitize_reply_text(text: str) -> str:
    """清理 Markdown 或列表符号，强制输出更自然的纯文本。"""
    if not text:
        return ""

    text = strip_internal_reasoning(text)
    text = strip_text_tool_protocol(text)
    text = DSML_TAG_PATTERN.sub("", text)
    text = MARKDOWN_FENCE_PATTERN.sub(lambda match: match.group("code"), text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("*", "").replace("`", "")
    return text.strip()


def parse_text_tool_calls(text: str):
    """解析网关把 function call 作为 to=functions.name 代json 文本返回的格式。"""
    text = str(text or "")
    calls = []
    spans = []
    decoder = json.JSONDecoder()
    for marker in TEXT_TOOL_CALL_MARKER_PATTERN.finditer(text):
        json_start = text.find("{", marker.end())
        if json_start < 0:
            continue
        try:
            arguments, consumed = decoder.raw_decode(text[json_start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(arguments, dict):
            continue
        tool_name = marker.group(1).strip()
        calls.append({
            "id": f"text_{tool_name}_{uuid.uuid4().hex[:10]}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        })
        spans.append((marker.start(), json_start + consumed))
    return calls, spans


def strip_text_tool_protocol(text: str) -> str:
    text = str(text or "")
    _, spans = parse_text_tool_calls(text)
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_kaomoji_tag(text: str) -> tuple[str | None, str]:
    """容错提取颜文字标签，允许 kaomoji 的轻微拼写错误。"""
    if not text:
        return None, ""

    match = KAOMOJI_TAG_PATTERN.search(text)
    if match:
        kaomoji = match.group(1).strip()
        kaomoji = re.sub(r"^\*{1,2}|\*{1,2}$", "", kaomoji).strip()
        kaomoji = re.sub(r"^`|`$", "", kaomoji).strip()
        clean_text = (text[:match.start()] + text[match.end():]).strip()
        clean_text = KAOMOJI_TAG_CLEANUP_PATTERN.sub("", clean_text).strip()
        return kaomoji or None, clean_text

    broken_match = KAOMOJI_BROKEN_CLOSING_PATTERN.search(text)
    if broken_match:
        kaomoji = broken_match.group(1).strip()
        kaomoji = re.sub(r"^\*{1,2}|\*{1,2}$", "", kaomoji).strip()
        kaomoji = re.sub(r"^`|`$", "", kaomoji).strip()
        clean_text = (text[:broken_match.start()] + text[broken_match.end():]).strip()
        clean_text = KAOMOJI_TAG_CLEANUP_PATTERN.sub("", clean_text).strip()
        return kaomoji or None, clean_text

    return None, KAOMOJI_TAG_CLEANUP_PATTERN.sub("", text).strip()


class KaomojiStreamFilter:
    """从流式显示文本中剔除可能被拆分的 kaomoji 协议块。"""
    _opening_pattern = re.compile(r"<\s*kaomo[a-z]{2,4}\s*>", re.IGNORECASE)
    _closing_pattern = re.compile(r"</\s*kaomo[a-z]{2,4}\s*>", re.IGNORECASE)

    def __init__(self):
        self._buffer = ""

    def reset(self):
        self._buffer = ""

    @staticmethod
    def _trailing_prefix_start(text):
        marker = text.rfind("<")
        if marker < 0:
            return len(text)
        suffix = re.sub(r"\s+", "", text[marker + 1:]).lower()
        if suffix.startswith("/"):
            suffix = suffix[1:]
        if not suffix or "kaomo".startswith(suffix):
            return marker
        if re.fullmatch(r"kaomo[a-z]{0,4}", suffix):
            return marker
        return len(text)

    def feed(self, chunk, final=False):
        self._buffer += str(chunk or "")
        visible_parts = []

        while self._buffer:
            opening = self._opening_pattern.search(self._buffer)
            if not opening:
                keep_from = self._trailing_prefix_start(self._buffer)
                visible_parts.append(self._buffer[:keep_from])
                self._buffer = self._buffer[keep_from:]
                break

            visible_parts.append(self._buffer[:opening.start()])
            remainder = self._buffer[opening.end():]
            closing = self._closing_pattern.search(remainder)
            next_opening = self._opening_pattern.search(remainder)
            if closing and (not next_opening or closing.start() <= next_opening.start()):
                self._buffer = remainder[closing.end():]
                continue
            if next_opening:
                # 第二个开标签被当成闭标签时，整个颜文字协议块均不显示。
                self._buffer = remainder[next_opening.end():]
                continue
            if final:
                self._buffer = ""
                break
            self._buffer = self._buffer[opening.start():]
            break

        return "".join(visible_parts)


class PetSpeechStreamFilter:
    """Strip fenced code and kaomoji protocol blocks from pet speech."""

    def __init__(self):
        self._line_buffer = ""
        self._in_fence = False
        self._fence_char = ""
        self._fence_length = 0
        self._kaomoji_filter = KaomojiStreamFilter()

    def reset(self):
        self._line_buffer = ""
        self._in_fence = False
        self._fence_char = ""
        self._fence_length = 0
        self._kaomoji_filter.reset()

    @staticmethod
    def _opening_fence(line):
        return re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)

    @staticmethod
    def _closing_fence(line):
        return re.match(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$", line)

    @staticmethod
    def _could_start_fence(line):
        stripped = line.lstrip(" \t")
        if len(line) - len(stripped) > 3 or not stripped:
            return bool(stripped) or len(line) <= 3
        first = stripped[0]
        if first not in "`~":
            return False
        if stripped.startswith(first * 3):
            return True
        return all(char == first for char in stripped)

    def _consume_line(self, line):
        if self._in_fence:
            closing = self._closing_fence(line)
            if (
                closing
                and closing.group(1)[0] == self._fence_char
                and len(closing.group(1)) >= self._fence_length
            ):
                self._in_fence = False
            return ""

        opening = self._opening_fence(line)
        if opening:
            fence = opening.group(1)
            self._in_fence = True
            self._fence_char = fence[0]
            self._fence_length = len(fence)
            return ""
        return line + "\n"

    def feed(self, chunk, final=False):
        self._line_buffer += str(chunk or "")
        visible_parts = []

        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            visible_parts.append(self._consume_line(line))

        if final:
            if self._line_buffer:
                visible_parts.append(self._consume_line(self._line_buffer))
            self._line_buffer = ""
            self._in_fence = False
        elif (
            not self._in_fence
            and self._line_buffer
            and not self._could_start_fence(self._line_buffer)
        ):
            visible_parts.append(self._line_buffer)
            self._line_buffer = ""

        return self._kaomoji_filter.feed("".join(visible_parts), final=final)


def split_reply_and_sources(text: str) -> tuple[str, str]:
    """将正文和来源区块拆开，便于显示与语音分流。"""
    if not text:
        return "", ""

    marker = "\n\n来源：\n"
    if marker in text:
        body, sources = text.split(marker, 1)
        return body.strip(), f"来源：\n{sources}".strip()
    return text.strip(), ""


def parse_sources_block(sources_block: str) -> list[dict]:
    """解析“来源：[n] 标题 / 链接：url”文本块为结构化列表。"""
    if not sources_block:
        return []
    sources = []
    current = None
    for raw_line in sources_block.splitlines():
        line = raw_line.strip()
        if not line or line == "来源：":
            continue
        title_match = re.match(r"^\[\d+\]\s+(.*)$", line)
        if title_match:
            current = {"title": title_match.group(1).strip(), "url": ""}
            sources.append(current)
            continue
        if current is not None and line.startswith("链接："):
            current["url"] = line[len("链接："):].strip()
    return [source for source in sources if source.get("url")]
