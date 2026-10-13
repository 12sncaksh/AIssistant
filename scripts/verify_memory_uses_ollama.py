"""End-to-end proof that the app's memory pipeline runs on the real Ollama backend.

Uses the REAL ConfigManager (so the saved settings are exercised) against a
temporary database, so the user's real chat history is never touched.

The decisive check is a paraphrase query that shares almost no characters with
the stored message: lexical hashing cannot match it, a semantic model can.
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config_manager import ConfigManager  # noqa: E402
from memory_embeddings import EmbeddingService  # noqa: E402
from memory_runtime import MemoryRuntime  # noqa: E402
from memory_store import MemoryIndexStore  # noqa: E402
from message_database import MessageDatabase  # noqa: E402

STORED = "如何修改窗口大小"
PARAPHRASE = "窗口尺寸怎么调整"
UNRELATED = "今天午饭吃什么"

# 多个互不相关的主题，且彼此隔开：检索器会带回命中片段前后的邻居，
# 只有主题足够分散时，「无关问题不该命中窗口话题」才是有效断言。
TOPICS = [
    ("用户", STORED),
    ("助手", "可以调整窗口的宽高像素。"),
    ("用户", "帮我看看 Seaborn 的配色方案"),
    ("助手", "deep 调色板适合分类数据。"),
    ("用户", "专利申请书里的附图怎么提取"),
    ("助手", "用 python-docx 读取内嵌图片。"),
    ("用户", UNRELATED),
    ("助手", "附近有几家评价不错的川菜馆。"),
    ("用户", "数据库备份多久做一次"),
    ("助手", "建议每天增量、每周全量。"),
]

failures = []


def check(label, ok, detail=""):
    if not ok:
        failures.append(label)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))


def main():
    config = ConfigManager()
    print(f"backend  = {config.get_memory_embedding_backend()}")
    print(f"base_url = {config.get_memory_embedding_base_url()}")
    print(f"model    = {config.get_memory_embedding_model()}")
    print()

    service = EmbeddingService(config)
    provider = service.resolve()
    identity = provider.identity if provider is not None else ""
    check("解析出的后端来自 Ollama（不是本地哈希）", identity.startswith("api:"), identity)
    check("模型名为 bge-m3", "bge-m3" in identity, identity)
    check("向量维度为 1024", provider is not None and provider.dimensions == 1024,
          str(provider.dimensions if provider else ""))

    with tempfile.TemporaryDirectory(prefix="aissistant-ollama-e2e-") as temp:
        db = MessageDatabase(os.path.join(temp, "chat.db"))
        store = MemoryIndexStore(os.path.join(temp, "index.db"))
        runtime = MemoryRuntime(config, message_db=db, store=store, service=service)
        session = "e2e"
        for role, text in TOPICS:
            db.save_message(session, "user" if role == "用户" else "assistant", text)

        runtime.note_session_loaded(session)
        check("索引构建完成", runtime.wait_index_idle(120))
        check(f"{len(TOPICS)} 条消息全部入库",
              store.session_size(session) == len(TOPICS),
              str(store.session_size(session)))
        check("索引记录的 identity 与当前后端一致",
              (store.index_identity() or "") == identity,
              store.index_identity() or "")

        def best_score_for(query, needle):
            """该 query 下，包含 needle 的片段组的最高分（没有则返回 0）。

            注意：这是片段组的分，等于组内最佳命中的分，不代表 needle 本身的分，
            所以只用来判断「有没有召回」，不能用来比较相似度。
            """
            result = runtime.retrieve(session, query)
            scores = [
                float(group.get("score") or 0.0)
                for group in result.get("groups") or []
                if any(needle in (message.get("content") or "") for message in group.get("messages") or [])
            ]
            return max(scores) if scores else 0.0

        # 相似度必须直接比向量：组分数会被同组其他命中带偏，不能用来排序。
        def cosine(left, right):
            dot = sum(a * b for a, b in zip(left, right))
            norm_left = sum(a * a for a in left) ** 0.5
            norm_right = sum(b * b for b in right) ** 0.5
            if norm_left <= 0 or norm_right <= 0:
                return 0.0
            return dot / (norm_left * norm_right)

        stored_vector = service.embed_one(STORED)
        paraphrase_similarity = cosine(service.embed_one(PARAPHRASE), stored_vector)
        unrelated_similarity = cosine(service.embed_one(UNRELATED), stored_vector)
        print(f"       相似度  同义改写 {paraphrase_similarity:.3f}  无关问题 {unrelated_similarity:.3f}")

        check("同义改写与原文的相似度显著高于无关问题",
              paraphrase_similarity > unrelated_similarity + 0.3,
              f"{paraphrase_similarity:.3f} vs {unrelated_similarity:.3f}")
        check("无关问题与原文的相似度处于低位", unrelated_similarity < 0.6,
              f"{unrelated_similarity:.3f}")

        paraphrase_score = best_score_for(PARAPHRASE, STORED)
        check("同义改写能召回原文（纯字面匹配做不到）", paraphrase_score > 0,
              f"group score={paraphrase_score:.3f}")

        result = runtime.retrieve(session, PARAPHRASE)
        texts = [
            message.get("content")
            for group in result.get("groups") or []
            for message in group.get("messages") or []
        ]
        check("召回片段包含窗口话题", any(STORED in text for text in texts), str(texts[:3]))

        runtime.stop()

    print()
    if failures:
        print(f"失败 {len(failures)} 项：" + "；".join(failures))
        return 1
    print("全部通过：程序记忆检索已运行在 Ollama bge-m3 上，且具备真正的语义召回能力。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
