"""人工金标标定的助手：把候选正解消息按"逐步放宽的条件"列出来。

`eval_gold_init.py` 只能处理关键词不饱和的样本；剩下那些（「常驻块」df=28 这类）
必须靠人读一遍会话才能定正解 —— 但会话有几十上百条，全读不现实。

本脚本按三步缩小候选，人只需要在候选里挑：

1. **同时包含全部关键词**的消息 —— 饱和词的交集通常只剩几条；
2. 退一步：包含 **≥2 个**关键词的消息；
3. 再退：包含**最稀有**那个关键词的消息（最多列 `--limit` 条）。

位置型样本（问「第一句话」「最开始聊的是什么」）额外列出会话开头几条，因为
它们的正解由**位置**决定，不由关键词决定。

输出带 message_id、会话内位置、角色和正文片段，可直接填进 `gold_message_ids`。

用法::

    python scripts/eval_gold_probe.py                      # 只看待标样本
    python scripts/eval_gold_probe.py --ids short-03 pos-01
    python scripts/eval_gold_probe.py --all --limit 8
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_store import MemoryIndexStore  # noqa: E402

from eval_memory import LIVE_DB_PATH, TEMPLATE_PATH, WORKING_PATH, session_of  # noqa: E402

SNIPPET = 110


def cases_of(document):
    if isinstance(document, dict):
        return document.get("cases") or []
    return document


def snippet(text):
    flat = " ".join(str(text or "").split())
    return flat[:SNIPPET] + ("…" if len(flat) > SNIPPET else "")


def main():
    parser = argparse.ArgumentParser(description="列出待标样本的候选正解消息")
    parser.add_argument("--cases", default="", help="样本文件；默认 data/memory_eval.json")
    parser.add_argument("--ids", nargs="*", default=[], help="只探这些样本 id")
    parser.add_argument("--all", action="store_true", help="探全部正例，不只是待标的")
    parser.add_argument("--limit", type=int, default=6, help="第 3 步最多列几条，默认 6")
    parser.add_argument("--head", type=int, default=3, help="额外列出会话开头几条，默认 3")
    args = parser.parse_args()

    path = args.cases or (WORKING_PATH if os.path.exists(WORKING_PATH) else TEMPLATE_PATH)
    with open(path, encoding="utf-8") as handle:
        cases = cases_of(json.load(handle))
    if not os.path.exists(LIVE_DB_PATH):
        print(f"[fail] 找不到真实索引：{LIVE_DB_PATH}")
        return 1

    wanted = set(args.ids)
    targets = [
        case
        for case in cases
        if not case.get("expect_none")
        and (str(case.get("id")) in wanted if wanted else (args.all or case.get("needs_manual_gold")))
    ]
    if not targets:
        print("没有要探的样本。")
        return 0

    store = MemoryIndexStore(LIVE_DB_PATH)
    cache = {}

    def rows_of(session_id):
        if session_id not in cache:
            try:
                cache[session_id] = store.session_rows(session_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! 读会话失败 {session_id}: {exc}")
                cache[session_id] = []
        return cache[session_id]

    for case in targets:
        case_id = str(case.get("id"))
        keywords = [str(word) for word in (case.get("expect_any") or []) if str(word).strip()]
        rows = rows_of(session_of(case))
        indexed = [
            (row.get("message_id"), position, str(row.get("role") or ""),
             str(row.get("text") or row.get("content") or ""))
            for position, row in enumerate(rows, start=1)
        ]
        print(f"\n===== {case_id} =====")
        print(f"查询：{case.get('query')}    关键词：{keywords}")
        print(f"会话 {session_of(case)}  共 {len(indexed)} 条")

        print(f"  [会话开头 {args.head} 条 —— 位置型样本的正解在这里]")
        for mid, position, role, text in indexed[: args.head]:
            print(f"    #{position:<3} id={mid:<6} {role:<9} {snippet(text)}")

        if not keywords:
            continue
        per_word = {word: [(mid, pos, role, text) for mid, pos, role, text in indexed if word in text]
                    for word in keywords}
        df_line = "  ".join(f"{word}×{len(per_word[word])}" for word in keywords)
        print(f"  词频：{df_line}")

        all_of = [item for item in indexed if all(word in item[3] for word in keywords)]
        some_of = [item for item in indexed if sum(word in item[3] for word in keywords) >= 2]
        rarest = min(keywords, key=lambda word: len(per_word[word]))
        rare_of = per_word[rarest]

        for label, bucket in (
            (f"① 含全部关键词（{len(all_of)} 条）", all_of),
            (f"② 含 ≥2 个关键词（{len(some_of)} 条）", some_of),
            (f"③ 含最稀有词 {rarest}（{len(rare_of)} 条）", rare_of),
        ):
            if not bucket:
                continue
            print(f"  {label}：")
            for mid, position, role, text in bucket[: args.limit]:
                print(f"    #{position:<3} id={mid:<6} {role:<9} {snippet(text)}")
            if len(bucket) > args.limit:
                print(f"    …另有 {len(bucket) - args.limit} 条未列出")
            if label.startswith("①") and bucket:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
