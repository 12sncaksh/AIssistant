"""记忆检索评测集跑分。

为什么需要它：改阈值、加词法、改 query 拼接这类改动，**凭体感判断几乎必然自欺**
——新东西刚上时总是"感觉可以"。只有用同一批样本在改动前后各跑一次，才有资格说
"变好了"。

两种模式，数字不要混着看：

1. **离线基准（默认）**：用样本自带的语料 + 一段公共干扰语料建临时索引，本地哈希
   向量，完全离线、可复现。它验证的是**机制**：query 带近期上下文有没有用、比例门
   从哪一档开始误杀样本、短指代这一类是不是真的被救回来了。
2. **真实索引（--live）**：在真实 data/memory_index.db 上跑，需要样本里给出
   ``live_session``（真实会话 id）和 embedding 配置。它验证的是**真实质量**。

用例::

    python scripts/eval_memory.py                      # 离线基准 + 阈值扫描 + 分类对比
    python scripts/eval_memory.py --init               # 生成 data/memory_eval.json 供自己改
    python scripts/eval_memory.py --verbose            # 逐条打印命中了什么
    python scripts/eval_memory.py --query-turns 4      # 换近期轮数再跑
    python scripts/eval_memory.py --live --query-turns 0 --embedding-backend api \
        --embedding-base-url https://... --embedding-model bge-m3 --embedding-api-key sk-...
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from memory_embeddings import EmbeddingService, StaticEmbeddingConfig, normalize_index_text  # noqa: E402
from memory_retriever import build_query_text, retrieve  # noqa: E402
from memory_store import IDENTITY_META_KEY, MemoryIndexStore  # noqa: E402

TEMPLATE_PATH = os.path.join(ROOT, "config", "templates", "memory_eval.example.json")
WORKING_PATH = os.path.join(ROOT, "data", "memory_eval.json")
LIVE_DB_PATH = os.path.join(ROOT, "data", "memory_index.db")

DEFAULT_MIN_RATIOS = (0.5, 0.6, 0.7, 0.8)
DEFAULT_MIN_SCORES = (0.0, 0.35, 0.45, 0.55)
DEFAULT_RATIO = 0.7
DEFAULT_SCORE = 0.55


def load_cases(path):
    """返回 (cases, background)；background 是每个会话都会附上的公共干扰语料。

    干扰语料是必要的：只看 case 自带的那几条，窗口一扩就把整个会话覆盖了，
    什么参数都能召回，测不出任何差别。
    """
    with open(path, "r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        raw_cases = payload.get("cases")
        background = [entry for entry in (payload.get("background") or []) if isinstance(entry, dict)]
    else:
        raw_cases, background = payload, []
    cases = [case for case in (raw_cases or []) if isinstance(case, dict)]
    return cases, background


def session_of(case):
    return str(case.get("live_session") or ("eval-" + str(case.get("id") or "case")))


def messages_of(case):
    """把样本里的 context + query 还原成消息列表（build_query_text 的输入）。"""
    messages = []
    for entry in case.get("context") or []:
        if isinstance(entry, dict):
            messages.append({"role": entry.get("role") or "user", "content": entry.get("text") or ""})
    messages.append({"role": "user", "content": case.get("query") or ""})
    return messages


def query_of(case, turns):
    """turns<=0 表示复现改动前的行为：只用当前那条用户消息。"""
    current = str(case.get("query") or "")
    if turns <= 0:
        return current
    return build_query_text(messages_of(case), turns=turns) or current


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------


def build_fixture_store(cases, service, background=()):
    """离线基准：每个会话 = 公共干扰语料 + 该 case 自己的语料。"""
    provider = service.provider()
    directory = tempfile.mkdtemp(prefix="mem-eval-")
    store = MemoryIndexStore(os.path.join(directory, "index.db"))
    # message_id 是全表主键，各样本之间必须连续编号；每个 case 都从 1 开始的话
    # 会互相覆盖，最后只剩一个会话有数据。
    message_id = 0
    for case in cases:
        corpus = list(background) + [entry for entry in (case.get("corpus") or []) if isinstance(entry, dict)]
        if not corpus:
            continue
        session_id = session_of(case)
        texts = [normalize_index_text(entry.get("text") or "") for entry in corpus]
        vectors = service.embed(texts)
        entries = []
        for index, (entry, text, vector) in enumerate(zip(corpus, texts, vectors), start=1):
            message_id += 1
            entries.append(
                {
                    "message_id": message_id,
                    "session_id": session_id,
                    "role": entry.get("role") or "user",
                    "text": entry.get("text") or "",
                    "content_hash": f"{session_id}:{index}",
                    "identity": provider.identity,
                    "embedding": vector,
                }
            )
        store.upsert_entries(entries)
    store.set_meta(IDENTITY_META_KEY, provider.identity)
    return store


# --------------------------------------------------------------------------
# 评分
# --------------------------------------------------------------------------


def fragments_text(result):
    """召回片段的正文。retrieve() 返回的每条是 {"message_id","role","content"}。"""
    parts = []
    for group in result.get("groups") or []:
        for message in group.get("messages") or []:
            parts.append(str(message.get("content") or message.get("text") or ""))
    return "\n".join(parts)


def message_ids_of(result):
    """召回片段里出现过的 message_id 集合。"""
    found = set()
    for group in result.get("groups") or []:
        for message in group.get("messages") or []:
            value = message.get("message_id")
            if value is None:
                continue
            try:
                found.add(int(value))
            except (TypeError, ValueError):
                continue
    return found


# 金标 message_id 来自真实索引，离线合成索引里没有这些 id —— 离线跑分必须关掉金标，
# 否则带金标的 12 条正例会全部变成假 MISS（实测分类表整体掉到 0）。由 live 入口打开。
_GOLD_SCORING = {"on": False}


def use_gold_scoring(on):
    """live 口径开关：打开后（1）按金标判分，（2）滤掉「关键词饱和、金标待标」的样本。

    两件事必须同开同关：离线基准里关键词并不饱和，样本不该被滤；而金标 id 在离线
    合成索引里根本不存在，开着只会把 12 条正例全判成 MISS。
    """
    _GOLD_SCORING["on"] = bool(on)


def gold_ids_of(case):
    """样本标注的「正解消息」id 集合（只在 live 口径下生效）。

    为什么要有它：关键词判定在**专门聊这个话题**的会话里必然失效 —— 主题词在整段
    会话里到处都是（真库实测「常驻块」df=28、「阈值」df=28、「pull」df=32），基线
    随便捞一段就"命中"，案例永远通过，测不出任何差异。所以正例应当记 message_id，
    判据换成"那条真正回答问题的消息有没有被捞进返回窗口"。
    """
    if not _GOLD_SCORING["on"]:
        return set()
    gold = set()
    for value in case.get("gold_message_ids") or []:
        try:
            gold.add(int(value))
        except (TypeError, ValueError):
            continue
    return gold


def score_case(case, result):
    """返回 (是否达标, 是否被门槛误杀)。负例达标 = 什么都没召回。"""
    text = fragments_text(result)
    candidate_hits = int(result.get("hits") or 0)
    groups = result.get("groups") or []
    killed = candidate_hits > 0 and not groups
    if case.get("expect_none"):
        return (not groups), killed
    gold = gold_ids_of(case)
    if gold:
        # 有金标就只认金标：关键词在长会话里会饱和，判定被"碰巧命中"污染。
        return bool(gold & message_ids_of(result)), killed
    keywords = [str(word) for word in (case.get("expect_any") or []) if str(word).strip()]
    if not keywords:
        return bool(groups), killed
    return any(word in text for word in keywords), killed


def scorable_cases(cases):
    """滤掉「关键词饱和、金标待标」的样本。

    这些样本的关键词在真实会话里到处都是（「常驻块」df=28、「阈值」df=28），
    基线随便捞一段就"命中"，把它们算进分母只会得到一个被抬高的假召回率。
    所以宁可分母变小，也不要一个好看但不可信的分数。

    只在 live 口径下滤（见 use_gold_scoring）：离线合成语料里关键词并不饱和，
    而且离线本来就复现不了真实会话，滤掉只会白白缩小分母。
    """
    if not _GOLD_SCORING["on"]:
        return list(cases)
    return [case for case in cases if not case.get("needs_manual_gold")]


def run_grid(cases, store, service, turns, min_ratios, min_scores, verbose=False):
    """跑一遍完整网格，返回每行一个参数组合的汇总。"""
    cases = scorable_cases(cases)
    cache = {}
    for case in cases:
        session_id = session_of(case)
        query = query_of(case, turns)
        for min_ratio in min_ratios:
            for min_score in min_scores:
                cache[(str(case.get("id")), min_ratio, min_score)] = retrieve(
                    session_id,
                    query,
                    store,
                    service,
                    min_ratio=min_ratio,
                    min_score=min_score,
                )

    rows = []
    positives = [case for case in cases if not case.get("expect_none")]
    negatives = [case for case in cases if case.get("expect_none")]
    for min_ratio in min_ratios:
        for min_score in min_scores:
            hit = 0
            killed = 0
            injected = 0
            false_injections = 0
            chars = []
            top_scores = []
            details = []
            for case in cases:
                result = cache[(str(case.get("id")), min_ratio, min_score)]
                ok, was_killed = score_case(case, result)
                if not case.get("expect_none"):
                    hit += 1 if ok else 0
                    killed += 1 if was_killed else 0
                if result.get("groups"):
                    injected += 1
                    chars.append(int(result.get("chars") or 0))
                    top_scores.append(float(result["groups"][0].get("score") or 0.0))
                    if case.get("expect_none"):
                        false_injections += 1
                details.append((case, ok, result))
            rows.append(
                {
                    "min_ratio": min_ratio,
                    "min_score": min_score,
                    "recall": (hit / len(positives)) if positives else 0.0,
                    "hit": hit,
                    "positives": len(positives),
                    "false_inject": (false_injections / len(negatives)) if negatives else 0.0,
                    "false_injections": false_injections,
                    "negatives": len(negatives),
                    "inject_rate": injected / len(cases) if cases else 0.0,
                    "avg_chars": statistics.mean(chars) if chars else 0.0,
                    "killed": killed,
                    "avg_top1": statistics.mean(top_scores) if top_scores else 0.0,
                    "details": details,
                }
            )
    if verbose:
        for row in rows:
            print(f"\n--- min_ratio={row['min_ratio']} min_score={row['min_score']} ---")
            for case, ok, result in row["details"]:
                mark = "OK  " if ok else "MISS"
                print(
                    f"  [{mark}] {str(case.get('id')):<9} hits={result.get('hits')} "
                    f"groups={len(result.get('groups') or [])} chars={result.get('chars')}"
                )
    return rows


def print_table(title, rows, has_absolute_floor, note=""):
    print(f"\n== {title} ==")
    if note:
        print(note)
    header = (
        f"{'min_ratio':>9} {'min_score':>9} {'召回率':>8} {'命中':>7} "
        f"{'误注入率':>9} {'注入率':>8} {'平均注入字符':>13} {'被门槛误杀':>11} {'平均Top1':>9}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        if has_absolute_floor:
            false_inject = f"{row['false_inject'] * 100:>8.1f}%"
        else:
            false_inject = f"{'n/a':>9}"
        print(
            f"{row['min_ratio']:>9.2f} {row['min_score']:>9.2f} "
            f"{row['recall'] * 100:>7.1f}% {row['hit']:>3}/{row['positives']:<3} "
            f"{false_inject} {row['inject_rate'] * 100:>7.1f}% "
            f"{row['avg_chars']:>13.0f} {row['killed']:>11} {row['avg_top1']:>9.3f}"
        )


def category_recall(cases, store, service, turns, min_ratio, min_score):
    """按类别算召回率：短指代那一类的改善才是本次改动的真正证据。"""
    cases = scorable_cases(cases)
    buckets = {}
    for case in cases:
        if case.get("expect_none"):
            continue
        result = retrieve(
            session_of(case),
            query_of(case, turns),
            store,
            service,
            min_ratio=min_ratio,
            min_score=min_score,
        )
        ok, _ = score_case(case, result)
        bucket = buckets.setdefault(str(case.get("category") or "未分类"), [0, 0])
        bucket[0] += 1
        bucket[1] += 1 if ok else 0
    return buckets


def print_category_compare(cases, store, service, turns, min_ratio, min_score):
    before = category_recall(cases, store, service, 0, min_ratio, min_score)
    after = category_recall(cases, store, service, turns, min_ratio, min_score)
    print(f"\n分类召回率对比（min_ratio={min_ratio} min_score={min_score}）：")
    header = f"{'类别':<22} {'只用当前消息':>12} {'带近期上下文':>12} {'变化':>8}"
    print(header)
    print("-" * len(header))
    for key in sorted(after, key=lambda name: -after[name][0]):
        total, hit_after = after[key]
        _, hit_before = before.get(key, (total, 0))
        delta = (hit_after - hit_before) / total * 100 if total else 0.0
        print(
            f"{key:<22} {hit_before:>6}/{total:<5} {hit_after:>6}/{total:<5} {delta:>+7.1f}pt"
        )


def pick(rows, min_ratio, min_score):
    for row in rows:
        if abs(row["min_ratio"] - min_ratio) < 1e-9 and abs(row["min_score"] - min_score) < 1e-9:
            return row
    return None


def main():
    parser = argparse.ArgumentParser(description="记忆检索评测集跑分")
    parser.add_argument("--cases", default="", help="样本文件；默认 data/memory_eval.json，没有就用模板")
    parser.add_argument("--init", action="store_true", help="把模板复制成 data/memory_eval.json")
    parser.add_argument("--live", action="store_true", help="在真实 data/memory_index.db 上跑")
    parser.add_argument("--query-turns", type=int, default=2, help="检索 query 拼带的近期轮数；0 表示只用当前消息")
    parser.add_argument("--min-ratios", default="", help="逗号分隔的比例门，默认 0.5,0.6,0.7,0.8")
    parser.add_argument("--min-scores", default="", help="逗号分隔的绝对下限，默认 0.0,0.35,0.45,0.55")
    parser.add_argument("--embedding-backend", default="local", choices=("local", "api", "auto"))
    parser.add_argument("--embedding-base-url", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--embedding-api-key", default="")
    parser.add_argument("--verbose", action="store_true", help="逐条打印命中了什么")
    args = parser.parse_args()

    if args.init:
        os.makedirs(os.path.dirname(WORKING_PATH), exist_ok=True)
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as src, open(
            WORKING_PATH, "w", encoding="utf-8", newline=""
        ) as dst:
            dst.write(src.read())
        print(f"已生成 {WORKING_PATH}，可以按自己的真实会话改 live_session 了。")
        return 0

    path = args.cases or (WORKING_PATH if os.path.exists(WORKING_PATH) else TEMPLATE_PATH)
    cases, background = load_cases(path)
    if not cases:
        print(f"[fail] 样本为空：{path}")
        return 1

    min_ratios = [float(value) for value in args.min_ratios.split(",") if value.strip()] or list(DEFAULT_MIN_RATIOS)
    min_scores = [float(value) for value in args.min_scores.split(",") if value.strip()] or list(DEFAULT_MIN_SCORES)
    min_scores = sorted(set(min_scores))

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

    has_absolute_floor = bool(getattr(provider, "absolute_floor", True))
    if args.live:
        if not os.path.exists(LIVE_DB_PATH):
            print(f"[fail] 找不到真实索引：{LIVE_DB_PATH}")
            return 1
        # 金标 message_id 只对真实索引有意义：离线合成索引里没有这些 id，
        # 开着会让带金标的正例全部变成假 MISS。
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

    positives = [case for case in cases if not case.get("expect_none")]
    negatives = [case for case in cases if case.get("expect_none")]
    print(f"样本：{len(cases)} 条（正例 {len(positives)} / 负例 {len(negatives)}）  文件：{os.path.relpath(path, ROOT)}")
    print(f"模式：{mode}   后端：{provider.identity}")

    note = ""
    if not has_absolute_floor:
        note = (
            "注意：当前后端没有绝对下限（absolute_floor=False），min_score 这几列不生效，只看 min_ratio；\n"
            "      负例也因此无法被拒绝，误注入率记为 n/a 而不是 0%。"
        )

    baseline_rows = run_grid(cases, store, service, 0, min_ratios, min_scores, verbose=args.verbose)
    enriched_rows = run_grid(cases, store, service, max(0, args.query_turns), min_ratios, min_scores, verbose=args.verbose)

    print_table("query 只用当前消息（改动前的行为）", baseline_rows, has_absolute_floor, note)
    print_table(f"query 带最近 {args.query_turns} 轮（本次改动）", enriched_rows, has_absolute_floor)

    ratio = DEFAULT_RATIO if DEFAULT_RATIO in min_ratios else min_ratios[-1]
    score = DEFAULT_SCORE if DEFAULT_SCORE in min_scores else min_scores[-1]
    before = pick(baseline_rows, ratio, score)
    after = pick(enriched_rows, ratio, score)
    if before and after:
        print(f"\n汇总对比（min_ratio={ratio} min_score={score}）：")
        print(
            f"  召回率      {before['recall'] * 100:.1f}%  ->  {after['recall'] * 100:.1f}%"
            f"   ({after['recall'] * 100 - before['recall'] * 100:+.1f}pt)"
        )
        print(f"  被门槛误杀  {before['killed']}  ->  {after['killed']}")
        print(f"  注入率      {before['inject_rate'] * 100:.1f}%  ->  {after['inject_rate'] * 100:.1f}%")
        print(f"  平均注入字符 {before['avg_chars']:.0f}  ->  {after['avg_chars']:.0f}")
        print_category_compare(cases, store, service, max(0, args.query_turns), ratio, score)

    print("\n提示：离线基准验证的是机制（query 拼接、门槛档位、分类召回），不是真实语料质量；")
    print("      要看真实效果，用 --init 生成样本、填上真实 live_session，再跑 --live。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
