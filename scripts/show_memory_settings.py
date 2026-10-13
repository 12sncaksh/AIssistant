"""Print the effective history/context settings for both paths (read-only)."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config_manager import ConfigManager  # noqa: E402


def main():
    c = ConfigManager()

    rows = [
        ("开关", "记忆总开关", "get_memory_enabled"),
        ("开关", "检索开关", "get_memory_retrieval_enabled"),
        ("开关", "LLM 摘要", "get_memory_summary_llm_enabled"),
        ("向量", "后端", "get_memory_embedding_backend"),
        ("向量", "Base URL", "get_memory_embedding_base_url"),
        ("向量", "模型", "get_memory_embedding_model"),
        ("本地API", "最近消息条数", "get_memory_recent_messages"),
        ("本地API", "上下文上限(字符)", "get_memory_context_max_chars"),
        ("检索", "top_k", "get_memory_retrieval_top_k"),
        ("检索", "相邻扩展半径", "get_memory_retrieval_group_radius"),
        ("检索", "最多片段组", "get_memory_retrieval_max_groups"),
        ("检索", "检索字符上限", "get_memory_retrieval_max_chars"),
        ("检索", "相似度下限(绝对)", "get_memory_retrieval_min_score"),
        ("摘要", "摘要开关", "get_memory_summary_enabled"),
        ("摘要", "摘要字符上限", "get_memory_summary_max_chars"),
        ("摘要", "刷新增长阈值", "get_memory_summary_refresh_growth"),
        ("UI", "每页条数", "get_chat_history_page_size"),
        ("DSH", "memory 开关", "get_dsh_memory_enabled"),
        ("DSH", "seed 轮数", "get_dsh_seed_turns"),
        ("DSH", "seed 每条上限", "get_dsh_seed_max_chars"),
        ("DSH", "完整回顾通道", "get_dsh_full_recall_enabled"),
    ]
    for group, label, getter in rows:
        try:
            value = getattr(c, getter)()
        except Exception as exc:
            value = f"<{exc}>"
        print(f"  [{group:<7}] {label:<18} = {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
