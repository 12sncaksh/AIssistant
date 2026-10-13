"""Semantic retrieval over the local memory index.

A hit is a single message, but a useful memory is a *conversation fragment*:
the surrounding turns carry the meaning. So every hit is expanded into a
window of neighbouring messages, overlapping windows are merged, and the
result is handed back in chronological order.

The limits are deliberately generous. They exist only to stop a pathological
session (one enormous message, or hundreds of near-identical hits) from
swallowing the whole model context.
"""

from __future__ import annotations

import logging

from memory_embeddings import EmbeddingUnavailable, normalize_index_text
from memory_lexical import (
    DEFAULT_LEXICAL_RATIO,
    RRF_K,
    fused_hits,
    lexical_rank,
)
from memory_lexical import DEFAULT_POOL as DEFAULT_HYBRID_POOL


DEFAULT_TOP_K = 12
DEFAULT_GROUP_RADIUS = 2
DEFAULT_MAX_GROUPS = 8
DEFAULT_MAX_CHARS = 24000
DEFAULT_MIN_RATIO = 0.7
# 绝对相似度下限，只用于语义模型（见 EmbeddingProvider.absolute_floor）。
#
# 为什么必须有：min_ratio 是"最高分 ×0.70"的相对阈值。bge-m3 实测对中文
# 无关文本的相似度地板约 0.39~0.48；相对阈值过松（如 0.35，实际只有
# 0.16~0.19，永远低于地板）等于不设阈值，无关问题也会注入满额历史
# （实测 8 组约 7800 字符）。0.70 只保留与最佳命中同一量级的片段，
# 弱相关历史不再占用上下文；本地哈希后端（无绝对下限）同样受益。
#
# 取值依据（实测，与「如何修改窗口大小」对比）：
#   同义改写 0.953 | 相关 0.553~0.655 | 无关 0.425~0.510 | 无意义 0.394
# 0.55 能挡住无关内容，同时保留真相关的片段。
DEFAULT_MIN_SCORE = 0.55


DEFAULT_QUERY_TURNS = 2
DEFAULT_QUERY_PER_MESSAGE_CHARS = 240
DEFAULT_QUERY_MAX_CHARS = 1200


def _content_text(content):
    """从消息 content 里取纯文本（兼容多模态的分片列表形式）。"""
    if isinstance(content, list):
        parts = []
        for part in content or []:
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                text = str(part.get("text") or "").strip()
                if text:
                    parts.append(text)
        return " ".join(parts)
    return str(content or "").strip()


def build_query_text(
    messages,
    turns=DEFAULT_QUERY_TURNS,
    per_message_chars=DEFAULT_QUERY_PER_MESSAGE_CHARS,
    max_chars=DEFAULT_QUERY_MAX_CHARS,
):
    """把最近几轮对话拼成检索用的 query。

    为什么需要：只用当前那条用户消息时，「那这个呢？」「继续」「还有呢」这类
    短问题在向量和词法两路上都等于瞎猜 —— 这是记忆系统最常见的真实失败场景。
    带上最近 1~2 轮，指代就有了可解释的对象。

    为什么不多带：query 越长，查询向量被平均得越厉害。把十几轮全塞进去会退化成
    一锅粥，比只用当前消息更差，所以默认只带 2 轮，且每条消息都截断。

    当前消息永远保留：超预算时从最早的内容开始丢，而不是截尾部。
    """
    try:
        window = max(2, int(turns or DEFAULT_QUERY_TURNS) * 2)
    except (TypeError, ValueError):
        window = DEFAULT_QUERY_TURNS * 2
    try:
        per_message = max(40, int(per_message_chars or DEFAULT_QUERY_PER_MESSAGE_CHARS))
    except (TypeError, ValueError):
        per_message = DEFAULT_QUERY_PER_MESSAGE_CHARS
    try:
        budget = max(120, int(max_chars or DEFAULT_QUERY_MAX_CHARS))
    except (TypeError, ValueError):
        budget = DEFAULT_QUERY_MAX_CHARS

    lines = []
    for message in list(messages or [])[-window:]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        body = " ".join(_content_text(message.get("content")).split())
        if not body:
            continue
        lines.append(_format_message(role, body, limit=per_message))
    if not lines:
        return ""
    # 从最早的一行开始丢，保证当前这条用户消息始终在 query 里。
    while len(lines) > 1 and len("\n".join(lines)) > budget:
        lines.pop(0)
    return "\n".join(lines)[-budget:]


def _format_message(role, text, limit=None):
    label = "用户" if role == "user" else ("助手" if role == "assistant" else role)
    body = str(text or "").strip()
    if limit and len(body) > limit:
        body = body[:limit] + "…（截断）"
    return f"{label}：{body}"


def _select_hits(hits, min_ratio, min_score=0.0):
    """Drop clearly irrelevant hits.

    两道门槛同时生效，取较高者：

    * 相对阈值 ``最高分 × min_ratio``：随问题自适应；
    * 绝对下限 ``min_score``：挡住"最高分本身就不高"的情况 —— 相对阈值
      在这种情况下形同虚设。

    全部低于门槛时返回空列表：宁可这一轮不注入历史记忆，也不要把无关内容
    塞给模型（调用方会跳过 memory block）。
    """
    if not hits:
        return []
    best = max((hit["score"] for hit in hits), default=0.0)
    threshold = best * float(min_ratio)
    if min_score:
        threshold = max(threshold, float(min_score))
    if threshold <= 0:
        return hits
    return [hit for hit in hits if hit["score"] >= threshold]


def retrieve(
    session_id,
    query,
    store,
    service,
    top_k=DEFAULT_TOP_K,
    group_radius=DEFAULT_GROUP_RADIUS,
    max_groups=DEFAULT_MAX_GROUPS,
    max_chars=DEFAULT_MAX_CHARS,
    min_ratio=DEFAULT_MIN_RATIO,
    min_score=DEFAULT_MIN_SCORE,
    exclude_message_ids=None,
    per_message_chars=0,
    hybrid=False,
    lexical_ratio=DEFAULT_LEXICAL_RATIO,
    hybrid_pool=None,  # None = 用 DEFAULT_HYBRID_POOL；给调参实验留的口子
    rrf_k=RRF_K,
    # 词法那路在 RRF 里的权重。1.0 = 与 dense 等权（原行为）。
    # 加这个参数是为了能**实测**短查询下"词法把噪声顶上来"到底有多严重。
    lexical_weight=1.0,
):
    """Return the most relevant historical fragments for ``query``.

    Result shape::

        {
          "groups": [{"score": float, "messages": [{"role", "text", "message_id"}]}],
          "chars": int, "hits": int, "identity": str, "error": str,
        }
    """
    result = {"groups": [], "chars": 0, "hits": 0, "identity": "", "error": ""}
    query_text = normalize_index_text(query)
    if not query_text or not str(session_id or "").strip():
        return result
    provider = service.provider()
    if provider is None:
        result["error"] = "embedding 后端尚未就绪"
        return result
    result["identity"] = provider.identity
    # 身份校验：索引里的向量和当前后端必须同源。
    # local-hashing 与 bge-m3 都是 1024 维，维度检查拦不住这种混用，
    # 但两者的向量空间完全不同，混着算相似度只会得到噪声。
    try:
        stored_identity = store.index_identity()
    except Exception:
        stored_identity = ""
    if stored_identity and stored_identity != provider.identity:
        result["error"] = "索引与当前 embedding 后端不一致，已跳过检索以免返回错误结果"
        logging.warning(
            "语义检索已跳过：索引身份 %s 与当前后端 %s 不一致",
            stored_identity,
            provider.identity,
        )
        return result
    try:
        query_vector = service.embed_one(query_text)
    except EmbeddingUnavailable as exc:
        result["error"] = str(exc)
        return result
    if not query_vector:
        return result

    # 绝对下限只对语义模型成立：本地哈希的相似度尺度完全不同（同义句实测仅
    # 0.152），套用同一个数值会让本地后端一条都召回不到。由 provider 自己声明。
    effective_min_score = min_score if getattr(provider, "absolute_floor", True) else 0.0
    rows = None

    if hybrid:
        # 混合检索：两路各自保留自己的门槛，RRF **只负责排序**。
        # 融合分只有 0.01~0.03 量级，绝不能拿去和 min_score 比 —— 一比全杀光。
        rows = store.session_rows(session_id)
        if not rows:
            return result
        pool = max(1, int(DEFAULT_HYBRID_POOL if hybrid_pool is None else hybrid_pool))
        dense_hits = store.search(
            session_id,
            query_vector,
            # 召回宽度是 RRF 的前提：两路都只取 12 条再融合，融合改变不了那 12 条是谁。
            limit=max(int(top_k), pool),
            exclude_message_ids=exclude_message_ids,
        )
        result["hits"] = len(dense_hits)
        dense_hits = _select_hits(dense_hits, min_ratio, effective_min_score)
        lex_hits = lexical_rank(
            query_text,
            rows,
            exclude_message_ids=exclude_message_ids,
            limit=pool,
            # pull 传 min_ratio=0（刻意不设门），词法路也跟着不设门。
            ratio=float(lexical_ratio) if float(min_ratio or 0.0) > 0 else 0.0,
        )
        result["lexical_hits"] = len(lex_hits)
        result["fused"] = True
        hits = fused_hits(
            dense_hits, lex_hits, rrf_k=rrf_k, lexical_weight=lexical_weight
        )
        if not hits:
            logging.info(
                "混合检索未找到相关历史（dense %d 条 / 词法 %d 条均低于门槛），本轮不注入记忆",
                result["hits"],
                len(lex_hits),
            )
            return result
    else:
        hits = store.search(
            session_id,
            query_vector,
            limit=int(top_k),
            exclude_message_ids=exclude_message_ids,
        )
        result["hits"] = len(hits)
        hits = _select_hits(hits, min_ratio, effective_min_score)
        if not hits:
            logging.info(
                "语义检索未找到足够相关的历史（%d 个候选均低于阈值 %.2f），本轮不注入记忆",
                result["hits"],
                effective_min_score,
            )
            return result

    if rows is None:
        rows = store.session_rows(session_id)
        if not rows:
            return result

    # Expand each hit into a window, then merge overlapping windows.
    windows = []
    radius = max(0, int(group_radius))
    for hit in hits:
        position = int(hit["position"])
        windows.append({
            "start": max(0, position - radius),
            "end": min(len(rows) - 1, position + radius),
            "score": float(hit["score"]),
            # 排序用 rank（混合检索下是 RRF 分），显示用 score（dense 余弦）。
            "rank": float(hit.get("rank_score", hit["score"])),
        })
    windows.sort(key=lambda item: (item["start"], item["end"]))
    merged = []
    for window in windows:
        if merged and window["start"] <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], window["end"])
            merged[-1]["score"] = max(merged[-1]["score"], window["score"])
            merged[-1]["rank"] = max(merged[-1]["rank"], window["rank"])
        else:
            merged.append(dict(window))

    merged.sort(key=lambda item: item.get("rank", item["score"]), reverse=True)
    merged = merged[:max(0, int(max_groups))]
    merged.sort(key=lambda item: item["start"])

    total_chars = 0
    budget = int(max_chars)
    groups = []
    for window in merged:
        messages = []
        group_chars = 0
        for row in rows[window["start"]:window["end"] + 1]:
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            body = text
            if per_message_chars and len(body) > per_message_chars:
                body = body[:per_message_chars] + "…（截断）"
            messages.append({
                "message_id": row.get("message_id"),
                "role": row.get("role"),
                "content": body,
            })
            group_chars += len(body)
        if not messages:
            continue
        if groups and total_chars + group_chars > budget:
            continue
        groups.append({
            "score": window["score"],
            # 排序名次（混合检索下是 RRF 分）。**注意**：groups 最终按时间重排后交给
            # 模型，所以"排第一的组"必须靠这个字段找，不能拿 groups[0] —— 那是"最早
            # 的组"。评测里的"排序第一组命中"就是踩过这个坑。
            "rank": window.get("rank", window["score"]),
            "messages": messages,
        })
        total_chars += group_chars
    result["groups"] = groups
    result["chars"] = total_chars
    if groups:
        logging.info(
            "[perf] memory_retrieve session=%s hits=%d groups=%d chars=%d identity=%s",
            str(session_id)[:12],
            len(hits),
            len(groups),
            total_chars,
            result["identity"],
        )
    return result


def format_memory_block(result, max_chars=DEFAULT_MAX_CHARS):
    """Render retrieved fragments as a clearly-labelled background block."""
    groups = (result or {}).get("groups") or []
    if not groups:
        return ""
    # 混合检索下片段顺序由 RRF 决定，那不是相似度 —— 标一个假的"相关度 0.02"
    # 只会误导模型，所以那种情况下不显示分值。
    fused = bool((result or {}).get("fused"))
    limit = int(max_chars) if max_chars else None
    lines = []
    total = 0
    for index, group in enumerate(groups, 1):
        if fused:
            lines.append(f"—— 片段 {index} ——")
        else:
            lines.append(f"—— 片段 {index}（相关度 {group.get('score', 0):.2f}）——")
        for message in group.get("messages") or []:
            line = _format_message(message.get("role"), message.get("content"))
            if limit is not None and total + len(line) > limit and len(lines) > 1:
                break
            lines.append(line)
            total += len(line)
        if limit is not None and total >= limit:
            break
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)
