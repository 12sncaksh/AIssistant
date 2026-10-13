"""评测样本体检：把「永远不可能命中」和「必然假命中」的样本挑出来。

为什么需要它：跑分器判「命中」靠的是关键词有没有出现在召回片段里。于是样本会坏在
三个方向，而且**都不会报错**，只会给你一个好看或难看的假数字：

* **df = 0**：关键词在目标会话里根本不存在 → 这条样本永远 MISS。
  测的不是检索能力，是巧合（真库上 `tail-01` 的「分段」就是这样）。
* **df 很大**：关键词在会话里到处都是（比如「继续」「0.7」）→ 基线随便捞一段就
  「命中」→ 这条样本永远 HIT，把真实差异整个盖住。
* **子串误配**：`"20"` 会匹配到日期 `2026-10-13` 里的 `20` → 看着命中，文不对题。
  体检只能报出「df 偏高」这种信号，具体是不是误配得人看一眼上下文。

用法::

    python scripts/eval_sample_health.py              # 只看样本自带语料（离线口径）
    python scripts/eval_sample_health.py --live       # 再查真实会话里的词频（真库口径）
    python scripts/eval_sample_health.py --live --saturated 3
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_store import MemoryIndexStore  # noqa: E402

from eval_memory import (  # noqa: E402
    LIVE_DB_PATH,
    TEMPLATE_PATH,
    WORKING_PATH,
    load_cases,
    session_of,
)

# 短关键词容易被子串误配（"20" 命中日期里的 20），单独标出来提醒人工看。
SHORT_KEYWORD_LEN = 3


def texts_of_case(case, background):
    texts = [str(entry.get("text") or "") for entry in background]
    texts += [
        str(entry.get("text") or "")
        for entry in (case.get("corpus") or [])
        if isinstance(entry, dict)
    ]
    return texts


def texts_of_session(store, session_id):
    try:
        rows = store.session_rows(session_id)
    except Exception as exc:  # noqa: BLE001 - 体检脚本不该因为一条样本崩掉
        return None, str(exc)
    return [str(row.get("text") or row.get("content") or "") for row in rows], ""


def verdict(df, saturated_at):
    if df == 0:
        return "不存在"
    if df >= saturated_at:
        return "饱和"
    return "ok"


def main():
    parser = argparse.ArgumentParser(description="评测样本关键词体检")
    parser.add_argument("--cases", default="", help="样本文件；默认 data/memory_eval.json")
    parser.add_argument("--live", action="store_true", help="再查真实会话里的词频")
    parser.add_argument("--saturated", type=int, default=5, help="命中多少条算「饱和」，默认 5")
    parser.add_argument("--min-live-rows", type=int, default=0, help="跳过行数少于这个值的会话")
    args = parser.parse_args()

    path = args.cases or (WORKING_PATH if os.path.exists(WORKING_PATH) else TEMPLATE_PATH)
    cases, background = load_cases(path)
    if not cases:
        print(f"[fail] 样本为空：{path}")
        return 1

    store = None
    if args.live:
        if not os.path.exists(LIVE_DB_PATH):
            print(f"[fail] 找不到真实索引：{LIVE_DB_PATH}")
            return 1
        store = MemoryIndexStore(LIVE_DB_PATH)

    print(f"样本：{len(cases)} 条  文件：{os.path.relpath(path, ROOT)}")
    print(f"口径：{'离线语料 + 真实会话' if args.live else '样本自带语料'}  饱和线：df ≥ {args.saturated}\n")

    problems = []
    for case in cases:
        case_id = str(case.get("id"))
        keywords = [str(word) for word in (case.get("expect_any") or []) if str(word).strip()]
        if case.get("expect_none"):
            print(f"[{case_id:<10}] 负例（expect_none）：不靠关键词判定，跳过")
            continue
        if not keywords:
            print(f"[{case_id:<10}] ⚠ 没有 expect_any：只能判「有没有召回」，判不了「对不对」")
            problems.append((case_id, "无关键词"))
            continue

        offline_texts = texts_of_case(case, background)
        live_texts = None
        live_note = ""
        if store is not None:
            session_id = session_of(case)
            live_texts, live_note = texts_of_session(store, session_id)
            if live_texts is not None and len(live_texts) < args.min_live_rows:
                live_texts = None
                live_note = f"会话只有 {len(live_texts or [])} 行，已跳过"

        parts = []
        for word in keywords:
            offline_df = sum(1 for text in offline_texts if word in text)
            cell = f"{word!r} 离线df={offline_df}"
            bad = verdict(offline_df, args.saturated)
            if live_texts is not None:
                live_df = sum(1 for text in live_texts if word in text)
                cell += f" 真库df={live_df}"
                if verdict(live_df, args.saturated) != "ok":
                    bad = verdict(live_df, args.saturated)
                if live_df > 0 and len(word) <= SHORT_KEYWORD_LEN:
                    cell += "（短词，防子串误配）"
            if bad != "ok":
                cell += f"  ← {bad}"
                problems.append((case_id, f"{word}:{bad}"))
            parts.append(cell)

        session_id = session_of(case)
        print(f"[{case_id:<10}] session={session_id:<26} {'; '.join(parts)}")
        if live_note:
            print(f"{'':<12} ！{live_note}")

    print(f"\n===== 有问题的样本：{len(problems)} 处 =====")
    for case_id, reason in problems:
        print(f"  {case_id:<10} {reason}")
    if not problems:
        print("  没有。")
    print("\n提示：「不存在」的样本必须改关键词或换会话，否则它永远 MISS；")
    print("      「饱和」的样本要么换关键词，要么升级成 gold_message_id 判定，否则它永远 HIT。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
