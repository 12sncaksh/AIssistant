"""词法检索（BM25）+ RRF 融合：给"排序"这一层加第二路信号。

**为什么需要**：现在只有一路 dense（bge-m3 余弦）。真库金标实测的问题不是"召不回来"，
而是"正解排不到前几名"——21 条金标正例里 11 条属于"召回了但不在首位"。单路信号的
排序能力有上限，融合两路是**唯一不动判据就能提排序**的办法。

**为什么不建 FTS5**：中文用 FTS5 默认的 unicode61 分词器会把整串汉字当成一个 token，
等于没用；换 trigram 又要求 SQLite ≥3.34。而项目里 `HashingEmbeddingProvider._tokens`
已经在产「单字 + 二元 + 三元 + 英文词」，**直接复用零新依赖**，还跟"离线可用"的既有
设计一致。本模块刻意调用它而不是自己再写一套分词，免得两路信号哪天走偏。

**RRF 为什么合适**：它只用**名次**，不用分数。dense 是余弦（0~1），BM25 是无界分值，
两者量纲不可比，任何"加权求和"都得先标定；RRF 用 1/(k+rank) 天然免疫量纲差异。

**RRF 最大的坑**：融合后的分值只有 0.01~0.03 量级。如果拿它去和 `min_score=0.55`
比，**所有命中会被一次杀光**（比不改更糟）。所以本模块只产出排序，**进不进得来仍由
各路的门槛决定**，融合分永远不参与取舍。
"""

from __future__ import annotations

import math

from memory_embeddings import HashingEmbeddingProvider

# RRF 的平滑常数：60 是原论文取值，作用是压平"第 1 名"和"第 3 名"的差距，
# 让两路的共识比单路的极端名次更重要。
RRF_K = 60
BM25_K1 = 1.5
BM25_B = 0.75
# 每路各取多宽。RRF 的收益来自"两路各自看得很宽"——两路都只取 12 条再融合，
# 融合几乎改变不了那 12 条是谁。
# 两路各自的候选池宽度。**40 太窄** —— 真库金标实测（21 条正例，bge-m3，pull limit=8）：
#   池 40  → 召回 85.7% / 首位组命中 85.7%
#   池 80  → 召回 100%  / 首位组命中 100%   ← 靶子和漏网同时清零，平均字符 21901→26470
#   池 120 → 与 80 完全一致（收益已吃满）
# 卡住召回的**不是融合权重，是候选池**：正解压根没进候选时，融合排得再对也没用。
# 权重扫描的旁证：词法权重 0.5 和 1.5 都掉到 81%，只有等权 1.0 最好 —— 所以权重不动。
DEFAULT_POOL = 80
# 词法路的相对门（best × ratio）。和 dense 的 min_ratio 是同一个思路：
# 只保留与最佳命中同一量级的文档，免得单字这种高频 token 把整段会话捞进来。
DEFAULT_LEXICAL_RATIO = 0.5


def tokenize(text):
    """复用哈希后端的同一套分词（单字/二元/三元/英文词），保证两路口径一致。"""
    return [token for token, _weight in HashingEmbeddingProvider._tokens(str(text or ""))]


def bm25(query_tokens, documents, k1=BM25_K1, b=BM25_B):
    """对一批文档算 BM25。``documents`` 是 token 列表的列表，返回等长分值列表。"""
    total = len(documents)
    if not total or not query_tokens:
        return [0.0] * total
    lengths = [len(document) for document in documents]
    average = sum(lengths) / total if total else 0.0
    scores = [0.0] * total
    for token in set(query_tokens):
        df = sum(1 for document in documents if token in document)
        if df <= 0:
            continue
        idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
        for index, document in enumerate(documents):
            tf = document.count(token)
            if not tf:
                continue
            normal = lengths[index] / average if average else 0.0
            denominator = tf + k1 * (1.0 - b + b * normal)
            scores[index] += idf * (tf * (k1 + 1.0)) / denominator
    return scores


def lexical_rank(query, rows, exclude_message_ids=None, limit=DEFAULT_POOL, ratio=0.0):
    """按 BM25 给会话内的消息排名。

    返回 ``[(message_id, position, score), ...]``，按分值降序。

    ``ratio`` > 0 时只保留 ``best × ratio`` 以上的文档 —— 这是词法路的"相对门"，
    和 dense 的 ``min_ratio`` 同一个思路。pull 路径传 0（不设门）。
    """
    excluded = set()
    for value in exclude_message_ids or []:
        try:
            excluded.add(int(value))
        except (TypeError, ValueError):
            continue

    documents = []
    index = []
    for position, row in enumerate(rows or []):
        try:
            message_id = int(row.get("message_id"))
        except (TypeError, ValueError):
            continue
        if message_id in excluded:
            continue
        documents.append(tokenize(row.get("text") or row.get("content") or ""))
        index.append((message_id, position))

    scores = bm25(tokenize(query), documents)
    ranked = sorted(
        ((message_id, position, score) for (message_id, position), score in zip(index, scores)),
        key=lambda item: item[2],
        reverse=True,
    )
    ranked = [item for item in ranked if item[2] > 0.0]
    if ratio > 0 and ranked:
        floor = ranked[0][2] * float(ratio)
        ranked = [item for item in ranked if item[2] >= floor]
    return ranked[: max(1, int(limit))] if ranked else []


def fuse(dense_hits, lexical_hits, rrf_k=RRF_K, dense_weight=1.0, lexical_weight=1.0):
    """RRF 融合两路名次，返回 ``{message_id: {"rrf", "position", "score"}}``。

    ``score`` 保留 **dense 的余弦**（给注入块显示用），``rrf`` 只用于排序 ——
    两者绝不混用：融合分不是相似度，拿去显示或过门槛都是错的。

    两个 ``*_weight`` 是给**调参实验**用的：真库上发现词法那路在短查询里会把
    "字面像但主题不对"的片段顶上来，挤掉正确的那组。加权就能验它到底该占多重。
    """
    fused = {}
    for rank, hit in enumerate(dense_hits or [], start=1):
        message_id = hit.get("message_id")
        entry = fused.setdefault(
            message_id,
            {"rrf": 0.0, "position": hit.get("position"), "score": 0.0},
        )
        entry["rrf"] += float(dense_weight) / (rrf_k + rank)
        entry["score"] = max(entry["score"], float(hit.get("score") or 0.0))
    for rank, item in enumerate(lexical_hits or [], start=1):
        message_id, position, _score = item
        entry = fused.setdefault(message_id, {"rrf": 0.0, "position": position, "score": 0.0})
        entry["rrf"] += float(lexical_weight) / (rrf_k + rank)
    return fused


def fused_hits(dense_hits, lexical_hits, rrf_k=RRF_K, dense_weight=1.0, lexical_weight=1.0):
    """把融合结果整理成 retrieve() 内部用的 hit 列表（按 RRF 降序）。"""
    fused = fuse(dense_hits, lexical_hits, rrf_k, dense_weight, lexical_weight)
    hits = [
        {
            "message_id": message_id,
            "position": entry["position"],
            "score": entry["score"],
            "rank_score": entry["rrf"],
        }
        for message_id, entry in fused.items()
    ]
    hits.sort(key=lambda item: item["rank_score"], reverse=True)
    return hits
