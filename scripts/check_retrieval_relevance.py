"""逐条检查：无关问题是否也会被注入大量历史（只读，不改任何数据）。"""

from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config_manager import ConfigManager  # noqa: E402
from memory_embeddings import EmbeddingService  # noqa: E402
from memory_retriever import DEFAULT_MIN_RATIO, _select_hits, retrieve  # noqa: E402
from memory_store import MemoryIndexStore  # noqa: E402


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def main():
    config = ConfigManager()
    store = MemoryIndexStore()
    service = EmbeddingService(config)
    print("后端:", service.resolve().identity)

    conn = sqlite3.connect(store.db_path)
    sid, _ = conn.execute(
        "SELECT session_id, COUNT(*) c FROM memory_index GROUP BY session_id ORDER BY c DESC LIMIT 1"
    ).fetchone()
    conn.close()
    print("会话:", sid, "| min_ratio =", DEFAULT_MIN_RATIO)

    probes = [
        ("如何修改窗口大小", "相关（与部分历史同主题）"),
        ("米其林餐厅预订流程是什么", "完全无关"),
        ("zzz qqq 1234 无意义字符串", "完全无意义"),
    ]
    from memory_retriever import DEFAULT_GROUP_RADIUS, DEFAULT_MAX_GROUPS, DEFAULT_MAX_CHARS, DEFAULT_TOP_K, DEFAULT_MIN_SCORE

    for query, label in probes:
        result = retrieve(
            sid, query, store, service,
            top_k=DEFAULT_TOP_K, group_radius=DEFAULT_GROUP_RADIUS,
            max_groups=DEFAULT_MAX_GROUPS, max_chars=DEFAULT_MAX_CHARS,
            min_score=DEFAULT_MIN_SCORE,
        )
        groups = result.get("groups") or []
        scores = [round(float(g.get("score") or 0), 3) for g in groups]
        chars = sum(len(str(m.get("content") or "")) for g in groups for m in (g.get("messages") or []))
        print(f"\n[{label}] {query}")
        print(f"  hits={result.get('hits')}  组数={len(groups)}  注入字符≈{chars}")
        print(f"  分数={scores}")
        if scores:
            print(f"  相对阈值={round(scores[0] * DEFAULT_MIN_RATIO, 3)}（最低命中只要高于它就会被注入）")

    # 直接看分数分布：相关 vs 无关
    print("\n=== 分数分布参考 ===")
    v = lambda t: service.embed_one(t)
    base = v("如何修改窗口大小")
    for text, label in [("窗口尺寸怎么调整", "同义改写"), ("米其林餐厅预订流程", "无关"), ("zzz qqq 1234", "无意义")]:
        print(f"  {label:<8} 与基准的相似度 = {cosine(v(text), base):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
