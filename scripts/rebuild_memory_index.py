"""Rebuild the whole local memory index with the configured embedding backend.

Why this exists: switching the embedding model changes the vector space, so the
old vectors are useless. The app only rebuilds a session when you open it, which
leaves gaps (clear_index drops *every* session's vectors, then only the requested
one comes back). This script rebuilds all sessions in one task instead.

Run:  python scripts/rebuild_memory_index.py
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config_manager import ConfigManager  # noqa: E402
from memory_embeddings import EmbeddingService  # noqa: E402
from memory_runtime import MemoryRuntime  # noqa: E402
from memory_store import MemoryIndexStore  # noqa: E402
from message_database import MessageDatabase  # noqa: E402

TIMEOUT_SECONDS = 1800


def main():
    config = ConfigManager()
    db = MessageDatabase()
    store = MemoryIndexStore()

    before = store.stats()
    print("=== 重建前 ===")
    print(f"  已索引消息 : {before['indexed']} 条")
    print(f"  覆盖会话   : {before['sessions']} 个")
    print(f"  向量身份   : {before['identity'] or '(空)'}")

    backend = config.get_memory_embedding_backend()
    print(f"\n配置后端     : {backend}")

    # 安全闸门：API 后端不可达时必须中止。
    # 否则 resolve() 会静默回退到本地哈希，把索引按"降级模型"重建一遍，
    # 而身份字符串前后都是 local-hashing，看上去还像成功了。
    if backend in ("auto", "api"):
        probe = EmbeddingService(config).probe_api()
        if not probe.get("ok"):
            print(f"\n已中止：Embedding API 不可用。\n  {probe.get('message')}")
            print("\n请先启动 Ollama 服务（或修正 Base URL）后重试。索引未被修改。")
            return 1
        print(f"API 探测     : {probe['message']}")

    service = EmbeddingService(config)
    provider = service.resolve()
    if provider is None:
        print(f"\n无法解析 embedding 后端：{service.last_error}")
        return 1
    identity = provider.identity
    print(f"实际将使用   : {identity}")

    if backend in ("auto", "api") and not identity.startswith("api:"):
        print(
            "\n已中止：配置要求 API 后端，但实际解析到 "
            f"{identity}（发生了降级）。索引未被修改。"
        )
        return 1

    if identity == before["identity"] and before["indexed"]:
        print("\n身份与索引一致，且索引非空 —— 无需重建（只会跳过未变消息）。")

    runtime = MemoryRuntime(config, message_db=db, store=store, service=service)
    print("\n开始全量重建（后台任务，可能需要几分钟）…")
    started = time.perf_counter()
    runtime.indexer.request_all()
    finished = runtime.wait_index_idle(TIMEOUT_SECONDS)
    elapsed = time.perf_counter() - started
    state = runtime.indexer.status()

    print(f"\n任务结束   : {'完成' if finished else '超时'}")
    print(f"本轮耗时   : {elapsed:.1f} 秒")
    print(f"新索引     : {state.get('indexed', 0)} 条，跳过 {state.get('skipped', 0)} 条")
    if state.get("error"):
        print(f"错误       : {state['error']}")

    after = store.stats()
    print("\n=== 重建后 ===")
    print(f"  已索引消息 : {after['indexed']} 条")
    print(f"  覆盖会话   : {after['sessions']} 个")
    print(f"  向量身份   : {after['identity']}")

    runtime.stop()

    # 成功标准必须对齐"目标身份"，而不是"解析出来的身份"：
    # 降级时两者可能同为一个错误的本地后端。
    ok = after["identity"] == identity and after["indexed"] > 0
    print()
    if ok:
        print(f"重建成功：{after['indexed']} 条消息已运行在 {after['identity']} 上。")
        return 0
    print(f"重建未达到预期（期望身份 {identity}，实际 {after['identity']}）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
