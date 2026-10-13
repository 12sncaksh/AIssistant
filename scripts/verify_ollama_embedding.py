"""Verify a local Ollama bge-m3 embedding endpoint end to end.

Checks, in order:
1. ollama.exe is installed and the HTTP service answers.
2. the embedding model is present (pulls it when missing).
3. the OpenAI-compatible /v1/embeddings path works for one and for many inputs.
4. the app's own EmbeddingService resolves this endpoint as the active backend.
5. semantic quality beats the local-hashing fallback on a Chinese paraphrase pair.

Run from the project root:  python scripts/verify_ollama_embedding.py
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

MODEL = "bge-m3"
BASE_URL = "http://localhost:11434/v1"
NATIVE_URL = "http://localhost:11434"
# 首次调用要把模型加载进显存（冷启动），必须给足时间；之后是毫秒级。
TIMEOUT = 300

PASSED = []
FAILED = []


def check(label, ok, detail=""):
    (PASSED if ok else FAILED).append(label)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f"  -> {detail}" if detail else ""))
    return ok


def find_ollama():
    found = shutil.which("ollama")
    if found:
        return found
    candidate = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"
    )
    return candidate if os.path.isfile(candidate) else ""


def http_json(url, payload=None, timeout=TIMEOUT):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def cosine(left, right):
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return dot / (norm_left * norm_right)


def wait_for_service(seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            http_json(f"{NATIVE_URL}/api/tags", timeout=3)
            return True
        except Exception:
            time.sleep(1)
    return False


def main():
    print("=== 1. Ollama 可执行文件 ===")
    exe = find_ollama()
    if not check("找到 ollama 可执行文件", bool(exe), exe or "未找到"):
        print("\n请先安装 Ollama（https://ollama.com/download）。")
        return 1

    print("\n=== 2. HTTP 服务 ===")
    if not check("ollama 服务可访问（/api/tags）", wait_for_service(30)):
        print("服务未就绪。可在新终端运行 `ollama serve`，或从开始菜单启动 Ollama。")
        return 1

    print("\n=== 3. 模型 ===")
    try:
        tags = http_json(f"{NATIVE_URL}/api/tags")
        names = [item.get("name", "") for item in tags.get("models", [])]
    except Exception as exc:
        names = []
        print(f"读取模型列表失败：{exc}")
    if any(name.split(":")[0] == MODEL for name in names):
        check(f"模型 {MODEL} 已存在", True, ", ".join(names))
    else:
        print(f"未找到 {MODEL}，开始拉取（约 1.2 GB，请耐心等待）…")
        code = subprocess.call([exe, "pull", MODEL])
        check(f"模型 {MODEL} 拉取成功", code == 0, f"exit={code}")
        if code != 0:
            return 1

    print("\n=== 4. OpenAI 兼容 /v1/embeddings ===")
    single = http_json(f"{BASE_URL}/embeddings", {"model": MODEL, "input": ["你好"]})
    vector = single["data"][0]["embedding"]
    check("单条输入返回向量", bool(vector), f"{len(vector)} 维")

    batch_inputs = ["如何修改窗口大小", "窗口尺寸怎么调整", "今天午饭吃什么"]
    batch = http_json(f"{BASE_URL}/embeddings", {"model": MODEL, "input": batch_inputs})
    check("批量输入返回等量向量", len(batch["data"]) == len(batch_inputs), f"{len(batch['data'])}/{len(batch_inputs)}")
    ordered = sorted(batch["data"], key=lambda item: item.get("index", 0))

    print("\n=== 5. App 的 EmbeddingService ===")
    from memory_embeddings import EmbeddingService, StaticEmbeddingConfig

    service = EmbeddingService(
        StaticEmbeddingConfig("api", BASE_URL, MODEL, "")
    )
    result = service.probe_api()
    check("probe_api 通过", bool(result.get("ok")), result.get("message", ""))

    print("\n=== 6. 语义质量 vs 本地哈希 ===")
    from memory_embeddings import HashingEmbeddingProvider

    paraphrases = ["如何修改窗口大小", "窗口尺寸怎么调整"]
    unrelated = "今天午饭吃什么"

    api_vectors = [item["embedding"] for item in ordered[:2]]
    semantic_similar = cosine(api_vectors[0], api_vectors[1])
    semantic_unrelated = cosine(api_vectors[0], ordered[2]["embedding"])

    local = HashingEmbeddingProvider()
    local_vectors = local.embed(paraphrases + [unrelated])
    local_similar = cosine(local_vectors[0], local_vectors[1])
    local_unrelated = cosine(local_vectors[0], local_vectors[2])

    print(f"  语义模型  同义句 {semantic_similar:.3f}  vs 无关句 {semantic_unrelated:.3f}"
          f"  （差值 {semantic_similar - semantic_unrelated:+.3f}）")
    print(f"  本地哈希  同义句 {local_similar:.3f}  vs 无关句 {local_unrelated:.3f}"
          f"  （差值 {local_similar - local_unrelated:+.3f}）")
    check(
        "语义模型能把同义句排在无关句之前且区分度更大",
        semantic_similar > semantic_unrelated and (semantic_similar - semantic_unrelated) > (local_similar - local_unrelated),
        f"语义 {semantic_similar - semantic_unrelated:+.3f} > 哈希 {local_similar - local_unrelated:+.3f}",
    )

    print("\n" + "=" * 56)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
        return 1
    print("\n建议在「设置 → 工具与权限 → 记忆检索（Embedding）」中填入：")
    print(f"  后端     : 自动（优先 API，失败回退本地）")
    print(f"  Base URL : {BASE_URL}")
    print(f"  模型     : {MODEL}")
    print(f"  API Key  : 留空（本地 Ollama 无鉴权）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
