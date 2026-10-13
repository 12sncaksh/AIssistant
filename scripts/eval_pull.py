"""pull 通道（search_memory）的跑分：和 push 用同一批样本、同一个判分口径。

**为什么不能拿 eval_memory.py 的数字看 pull 的效果**：那份跑分测的是
「召回 + 排序」（第一层），调的是 retrieve() 的阈值网格。而 pull 动的是
「选择权」（第二层），召回和排序一行没改 —— 所以 push 的那几个数字必然纹丝不动，
拿它看 pull 等于什么都没测到。

这里换一批**真的会被 pull 改变**的量：

* **召回率**：同一批样本、同一个「期望关键词是否出现在返回片段里」的口径。
* **首位组命中**：关键词出现在**排名第一的那个片段组**里的比例 —— 排序层指标。
  pull 动不了它，RRF 才能。故意和召回率并排摆，好知道瓶颈在哪一层。
* **预算**：pull 每次返回多少字符。**这是 pull 与 push 最容易被忽略的差异** ——
  预算不同的话，比召回率是不公平的，所以这里也把预算一起打出来，并提供
  `--max-chars 0 --per-message-chars 0` 来跑"不限预算"的对照。
* **预算损失**：pull 在默认预算下漏掉的样本里，有多少条是**放宽预算就能捞回来**的。
  这一项直接回答"是 pull 更差，还是它只是花得更少"。

跑法::

    python scripts/eval_pull.py --live --embedding-backend api \
        --embedding-base-url http://localhost:11434/v1 --embedding-model bge-m3
    python scripts/eval_pull.py --live --max-chars 0 --per-message-chars 0   # 不限预算对照
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_embeddings import EmbeddingService, StaticEmbeddingConfig  # noqa: E402
from memory_retriever import retrieve  # noqa: E402
from memory_runtime import (  # noqa: E402
    DEFAULT_PULL_LIMIT,
    PULL_MAX_CHARS,
    PULL_PER_MESSAGE_CHARS,
    MemoryRuntime,
)
from memory_store import MemoryIndexStore  # noqa: E402

from eval_memory import (  # noqa: E402
    DEFAULT_RATIO,
    DEFAULT_SCORE,
    LIVE_DB_PATH,
    TEMPLATE_PATH,
    WORKING_PATH,
    build_fixture_store,
    gold_ids_of,
    load_cases,
    message_ids_of,
    query_of,
    scorable_cases,
    score_case,
    session_of,
    use_gold_scoring,
)

NO_BUDGET = 10 ** 9

# 混合检索（dense + 词法 BM25 + RRF）开关。由 --no-hybrid 关掉做纯向量对照 ——
# 没有这个对照，就没法回答"RRF 到底有没有帮上忙"。
HYBRID_ON = True


class EvalConfig:
    """评测用配置：唯一刻意的选择是 recent_messages=0（不排除任何消息）。

    离线基准里「模型已经看过最近几条」这个前提不成立，排除任何消息都是凭空的。
    """

    def _values(self):
        return {
            "get_memory_enabled": True,
            "get_memory_retrieval_enabled": True,
            "get_memory_pull_enabled": True,
            "get_memory_index_all_sessions": False,
            "get_memory_recent_messages": 0,
            "get_memory_query_turns": 2,
            "get_memory_retrieval_top_k": 12,
            "get_memory_retrieval_group_radius": 2,
            "get_memory_retrieval_max_groups": 8,
            "get_memory_retrieval_max_chars": 24000,
            "get_memory_retrieval_min_score": DEFAULT_SCORE,
            "get_memory_hybrid_enabled": HYBRID_ON,
            "get_memory_summary_enabled": False,
            "get_core_memory_enabled": False,
        }

    def __getattr__(self, name):
        values = self._values()
        if name in values:
            return lambda: values[name]
        raise AttributeError(name)


def keywords_of(case):
    return [str(word) for word in (case.get("expect_any") or []) if str(word).strip()]


def fragments(result):
    parts = []
    for group in result.get("groups") or []:
        for message in group.get("messages") or []:
            parts.append(str(message.get("content") or message.get("text") or ""))
    return parts


def group_text(group):
    return "\n".join(
        str(message.get("content") or message.get("text") or "")
        for message in (group.get("messages") or [])
    )


def top_group(case, result):
    """**排序第一**的那个片段组。

    不能拿 `groups[0]`：`retrieve()` 选完组之后会按**时间**重排再交给模型，
    所以 `groups[0]` 是"最早的那个组"，不是"排第一的组"。排序质量必须靠 `rank` 找
    —— 本鱼第一版就是拿 groups[0] 算的，那个数字测的是别的东西。
    """
    groups = result.get("groups") or []
    if not groups:
        return None
    return max(
        groups,
        key=lambda group: float(group.get("rank", group.get("score") or 0.0)),
    )


def top_group_hits(case, result):
    """正解是否出现在排序第一的片段组里（排序层指标）。

    有金标就认金标（message_id 在会话里唯一），没有才退回关键词 ——
    长会话里主题词会饱和（实测「常驻块」df=28），关键词判定会被"碰巧命中"污染。
    """
    group = top_group(case, result)
    if group is None:
        return False
    gold = gold_ids_of(case)
    if gold:
        return bool(gold & message_ids_of({"groups": [group]}))
    keywords = keywords_of(case)
    if not keywords:
        return False
    return any(word in group_text(group) for word in keywords)


def pull_once(
    runtime, case, turns, limit, max_chars, per_message_chars, hybrid_pool=None, lexical_weight=1.0
):
    return runtime.recall(
        session_of(case),
        query_of(case, turns),
        limit=limit,
        max_chars=max_chars,
        per_message_chars=per_message_chars,
        hybrid_pool=hybrid_pool,
        lexical_weight=lexical_weight,
    )


def push_once(store, service, case, turns):
    """push 路**固定纯向量**：混合检索只开在 pull（见 memory_runtime.recall）。

    实测把 RRF 也开给 push 会让召回从 81.0% 掉到 61.9%（dense 被门槛削到几条，
    词法宽池压倒了它）。所以评测里 push 反映的就是线上行为：不受 --no-hybrid 影响。
    """
    return retrieve(
        session_of(case),
        query_of(case, turns),
        store,
        service,
        min_ratio=DEFAULT_RATIO,
        min_score=DEFAULT_SCORE,
        hybrid=False,
    )


def measure(cases, results, label, killed_of=None):
    positives = [case for case in cases if not case.get("expect_none")]
    hits = 0
    top_group = 0
    killed = 0
    chars = []
    injected = 0
    missing = []
    late = []
    for case in cases:
        result = results[str(case.get("id"))]
        ok, was_killed = score_case(case, result)
        if case.get("expect_none"):
            continue
        if ok:
            hits += 1
            if top_group_hits(case, result):
                top_group += 1
            else:
                late.append(str(case.get("id")))
        else:
            missing.append(str(case.get("id")))
        if killed_of is not None and was_killed:
            killed += 1
        if result.get("groups"):
            injected += 1
            chars.append(int(result.get("chars") or 0))
    return {
        "label": label,
        "recall": hits / len(positives) if positives else 0.0,
        "hits": hits,
        "positives": len(positives),
        "top_group": top_group / len(positives) if positives else 0.0,
        "top_group_hits": top_group,
        "killed": killed if killed_of is not None else None,
        "inject": injected / len(cases) if cases else 0.0,
        "avg_chars": (sum(chars) / len(chars)) if chars else 0.0,
        "missing": missing,
        "late": late,
    }


def print_rows(rows):
    print(f"{'检索方式':<30}{'召回率':>9}{'首位组命中':>12}{'被门槛误杀':>12}{'注入率':>9}{'平均字符':>10}")
    for row in rows:
        killed = "—" if row["killed"] is None else f"{row['killed']}/{row['positives']}"
        print(
            f"{row['label']:<30}{row['recall'] * 100:>8.1f}%{row['top_group'] * 100:>11.1f}%"
            f"{killed:>12}{row['inject'] * 100:>8.1f}%{row['avg_chars']:>10.0f}"
        )


def main():
    parser = argparse.ArgumentParser(description="pull 通道跑分（与 push 同口径对比）")
    parser.add_argument("--cases", default="", help="样本文件；默认 data/memory_eval.json")
    parser.add_argument("--live", action="store_true", help="在真实 data/memory_index.db 上跑")
    parser.add_argument("--query-turns", type=int, default=2, help="检索 query 拼带的近期轮数")
    parser.add_argument("--limit", type=int, default=DEFAULT_PULL_LIMIT, help="pull 返回几段")
    parser.add_argument("--max-chars", type=int, default=PULL_MAX_CHARS, help="pull 总预算；0 表示不限")
    parser.add_argument(
        "--per-message-chars", type=int, default=PULL_PER_MESSAGE_CHARS, help="单条上限；0 表示不限"
    )
    parser.add_argument("--embedding-backend", default="local", choices=("local", "api", "auto"))
    parser.add_argument("--embedding-base-url", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--embedding-api-key", default="")
    parser.add_argument("--no-compare", action="store_true", help="不跑 push 与不限预算的对照")
    parser.add_argument("--verbose", action="store_true", help="逐条打印")
    parser.add_argument("--no-hybrid", action="store_true", help="关掉词法+RRF 融合，做纯向量对照")
    parser.add_argument(
        "--lexical-weight",
        type=float,
        default=1.0,
        help="词法那路在 RRF 里的权重（1.0=等权）。短查询上词法会顶噪声，用它验该占多重",
    )
    parser.add_argument(
        "--hybrid-pool", type=int, default=0, help="两路各自的候选池宽度；0 表示用默认值"
    )
    args = parser.parse_args()

    global HYBRID_ON
    HYBRID_ON = not args.no_hybrid

    path = args.cases or (WORKING_PATH if os.path.exists(WORKING_PATH) else TEMPLATE_PATH)
    cases, background = load_cases(path)
    if not cases:
        print(f"[fail] 样本为空：{path}")
        return 1

    service = EmbeddingService(
        StaticEmbeddingConfig(
            backend=args.embedding_backend,
            base_url=args.embedding_base_url,
            model=args.embedding_model,
            api_key=args.embedding_api_key,
        )
    )
    provider = service.resolve()
    if provider is None:
        print("[fail] embedding 后端不可用，无法跑分")
        return 1

    if args.live:
        if not os.path.exists(LIVE_DB_PATH):
            print(f"[fail] 找不到真实索引：{LIVE_DB_PATH}")
            return 1
        # 金标 message_id 只对真实索引有意义；离线合成索引里没有这些 id。
        use_gold_scoring(True)
        store = MemoryIndexStore(LIVE_DB_PATH)
        missing = [case.get("id") for case in cases if not case.get("live_session")]
        if missing:
            print(
                "[warn] 这些样本没有 live_session，会按 eval-<id> 这种不存在的会话检索（必然空召回）："
                + ", ".join(str(item) for item in missing)
            )
        mode = "真实索引"
    else:
        store = build_fixture_store(cases, service, background)
        mode = f"离线基准（{len(background)} 条公共干扰语料 + 样本自带语料）"

    runtime = MemoryRuntime(EvalConfig(), store=store, service=service)

    scorable = scorable_cases(cases)
    skipped = len(cases) - len(scorable)
    if skipped:
        print(
            f"[note] 有 {skipped} 条正例的关键词在真库里饱和（金标待标），已移出分母 —— "
            "留在里面只会得到被「碰巧命中」抬高的假召回率。"
        )
    cases = scorable

    positives = [case for case in cases if not case.get("expect_none")]
    negatives = [case for case in cases if case.get("expect_none")]
    max_chars = args.max_chars if args.max_chars > 0 else NO_BUDGET
    per_message_chars = max(0, args.per_message_chars)
    print(f"样本：{len(cases)} 条（正例 {len(positives)} / 负例 {len(negatives)}）  文件：{os.path.relpath(path, ROOT)}")
    print(f"模式：{mode}   后端：{provider.identity}")
    print(f"pull：limit={args.limit}  单条≤{per_message_chars or '不限'}  总量≤{max_chars if max_chars != NO_BUDGET else '不限'}"
          f"   query 带最近 {args.query_turns} 轮\n")

    default_results = {
        str(case.get("id")): pull_once(
            runtime,
            case,
            args.query_turns,
            args.limit,
            max_chars,
            per_message_chars,
            args.hybrid_pool or None,
            args.lexical_weight,
        )
        for case in cases
    }
    rows = [
        measure(
            cases,
            {str(case.get("id")): push_once(store, service, case, args.query_turns) for case in cases},
            f"push ({DEFAULT_RATIO}/{DEFAULT_SCORE})",
        ),
        measure(cases, default_results, f"pull (limit={args.limit}, 本次预算)"),
    ]
    if not args.no_compare:
        wide_results = {
            str(case.get("id")): pull_once(runtime, case, args.query_turns, args.limit, NO_BUDGET, 0)
            for case in cases
        }
        rows.append(measure(cases, wide_results, f"pull (limit={args.limit}, 不限预算)"))
    print_rows(rows)

    current = rows[1]
    print(f"\n拆开看 pull 的 {current['positives']} 条正例去向：")
    print(f"  首位组就是正解    {current['top_group_hits']}/{current['positives']}"
          f" ({current['top_group'] * 100:.1f}%)   ← 排序层，pull 动不了，RRF 才能")
    print(f"  召回了但不在首位  {len(current['late'])}/{current['positives']}"
          f"   ← RRF 的明确靶子：{('、'.join(current['late'])) if current['late'] else '无'}")
    print(f"  仍然没召回        {len(current['missing'])}/{current['positives']}"
          f"   ← 得靠加宽召回或写入端：{('、'.join(current['missing'])) if current['missing'] else '无'}")

    if not args.no_compare and current["missing"]:
        recovered = []
        for case_id in current["missing"]:
            case = next(item for item in cases if str(item.get("id")) == case_id)
            wide = pull_once(runtime, case, args.query_turns, args.limit, NO_BUDGET, 0)
            if score_case(case, wide)[0]:
                recovered.append(case_id)
        print(f"\n预算诊断：pull 漏掉的 {len(current['missing'])} 条里，"
              f"{len(recovered)} 条**放宽预算就能捞回来** → {('、'.join(recovered)) if recovered else '无'}")
        if recovered:
            print("  也就是说，这部分差距不是「pull 选得差」，而是「pull 给的字符少」。")
            print("  要提召回，先调 --max-chars / --per-message-chars，而不是改门槛。")

    print(f"\n负例 {len(negatives)} 条：pull 不设门槛，所以会返回片段（判断交给模型）。")
    print("  这一项**离线无法评分** —— pull 的设计就是「返回 top-k 让模型自己判」，")
    print("  返回了片段不等于答错；要评得跑端到端（模型看着片段作答）。")

    if args.verbose:
        print("\n逐条：")
        for case in cases:
            result = default_results[str(case.get("id"))]
            ok, _ = score_case(case, result)
            print(f"  [{str(case.get('id')):<10}] {'HIT ' if ok else 'MISS'}"
                  f" groups={len(result.get('groups') or [])} chars={int(result.get('chars') or 0)}"
                  f" 想看={keywords_of(case) or '(空)'}")
            for index, part in enumerate(fragments(result)[:3], start=1):
                print(f"      {index}. {part[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
