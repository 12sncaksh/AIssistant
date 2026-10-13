"""把评测样本从「关键词判定」升级成「金标消息判定」。

**为什么必须升级**：体检（`eval_sample_health.py`）显示 21 条正例里有 14 条的关键词
在真实会话里**饱和**（「常驻块」df=28、「阈值」df=28、「RRF」df=28、「pull」df=32）。
饱和意味着基线随便捞一段就"命中"，案例永远通过 —— 跑出来的召回率是被"碰巧命中"
抬高的假数字，RRF 上完你也看不出它到底有没有帮上忙。

金标判定的判据换成：**那条真正回答问题的消息（message_id）有没有被捞进返回窗口**。
message_id 在会话里唯一，不存在饱和，也不存在子串误配（`"20"` 命中日期那种）。

**金标怎么来**：取该样本里**词频最低的那个关键词**（df 在 1~max-df 之间）所在的消息
作为正解，并把锚点词和消息位置一起记下来。这是"锚点自动标定"，**不是人工确认** ——
所以脚本会打印每条样本的锚点、词频、消息在会话里的位置，方便你一眼复核；标不出来的
样本会明确列出来，绝不静默跳过。

用法::

    python scripts/eval_gold_init.py --dry-run    # 只看会标成什么，不写文件
    python scripts/eval_gold_init.py              # 写入 data/memory_eval.json（先备份）
    python scripts/eval_gold_init.py --max-df 2   # 更严：只认 df ≤ 2 的锚点
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_store import MemoryIndexStore  # noqa: E402

from eval_memory import LIVE_DB_PATH, TEMPLATE_PATH, WORKING_PATH, session_of  # noqa: E402

GOLD_NOTE = "；带 gold_message_ids 的样本按「金标消息是否被召回」判定，不再看关键词（关键词在长会话里会饱和）"


def cases_container(document):
    """样本文件既可能是 {"cases": [...]}，也可能就是一个数组。"""
    if isinstance(document, dict):
        return document, document.get("cases") or []
    return document, document


def main():
    parser = argparse.ArgumentParser(description="为评测样本标定 gold_message_ids")
    parser.add_argument("--cases", default="", help="样本文件；默认 data/memory_eval.json")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    parser.add_argument("--max-df", type=int, default=4, help="锚点词频上限，默认 4")
    parser.add_argument("--max-gold", type=int, default=3, help="每条样本最多记几条正解消息")
    args = parser.parse_args()

    path = args.cases or (WORKING_PATH if os.path.exists(WORKING_PATH) else TEMPLATE_PATH)
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    container, cases = cases_container(document)
    if not cases:
        print(f"[fail] 没读到样本：{path}")
        return 1
    if not os.path.exists(LIVE_DB_PATH):
        print(f"[fail] 找不到真实索引：{LIVE_DB_PATH}")
        return 1

    store = MemoryIndexStore(LIVE_DB_PATH)
    row_cache = {}

    def rows_of(session_id):
        if session_id not in row_cache:
            try:
                row_cache[session_id] = store.session_rows(session_id)
            except Exception as exc:  # noqa: BLE001 - 一条样本不该拖垮整个标定
                print(f"  ! 读会话失败 {session_id}: {exc}")
                row_cache[session_id] = []
        return row_cache[session_id]

    print(f"样本：{os.path.relpath(path, ROOT)}  锚点词频上限：df ≤ {args.max_df}\n")

    marked = 0
    manual = []
    for case in cases:
        case_id = str(case.get("id"))
        if case.get("expect_none"):
            continue
        keywords = [str(word) for word in (case.get("expect_any") or []) if str(word).strip()]
        if not keywords:
            manual.append((case_id, "没有 expect_any，只能判「有没有召回」"))
            continue
        rows = rows_of(session_of(case))
        if not rows:
            manual.append((case_id, "会话在真库里一条消息都没有"))
            continue

        texts = []
        for position, row in enumerate(rows, start=1):
            texts.append(
                (row.get("message_id"), position, str(row.get("text") or row.get("content") or ""))
            )
        scored = []
        for word in keywords:
            hits = [(mid, pos) for mid, pos, text in texts if word in text]
            scored.append((len(hits), word, hits))
        usable = [item for item in scored if 0 < item[0] <= args.max_df]
        if not usable:
            detail = ", ".join(f"{word!r}df={count}" for count, word, _ in sorted(scored))
            manual.append((case_id, f"没有可用锚点（{detail}）"))
            continue

        # 词频最低者最可能是"那条特定的消息"；同频时取更长的词（子串误配概率低）。
        usable.sort(key=lambda item: (item[0], -len(item[1])))
        df, word, hits = usable[0]
        gold = [mid for mid, _pos in hits[: args.max_gold]]
        positions = [pos for _mid, pos in hits[: args.max_gold]]
        case["gold_message_ids"] = gold
        case["gold_anchor"] = word
        marked += 1
        others = ", ".join(f"{w!r}df={n}" for n, w, _ in sorted(scored) if w != word)
        print(
            f"[{case_id:<10}] 锚点={word!r} df={df} → gold={gold} "
            f"（会话第 {positions} 条 / 共 {len(rows)} 条）"
        )
        if others:
            print(f"{'':<12} 其余关键词：{others}")

    # 标不出来的样本必须显式标记：留在分母里继续按饱和关键词判分，等于制造假数字。
    by_id = {str(case.get("id")): case for case in cases}
    for case_id, reason in manual:
        target = by_id.get(case_id)
        if target is not None:
            target["needs_manual_gold"] = True
            target["needs_manual_gold_reason"] = reason

    positives = sum(1 for case in cases if not case.get("expect_none"))
    print(f"\n===== 标定结果：{marked}/{positives} 条正例拿到了金标 =====")
    if manual:
        print("需要人工处理：")
        for case_id, reason in manual:
            print(f"  {case_id:<10} {reason}")
    if isinstance(container, dict) and "note" in container and GOLD_NOTE not in container["note"]:
        container["note"] = str(container["note"]) + GOLD_NOTE

    if args.dry_run:
        print("\n--dry-run：没有写文件。")
        return 0

    backup = path + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
        print(f"已备份原样本：{os.path.relpath(backup, ROOT)}")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"已写入：{os.path.relpath(path, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
