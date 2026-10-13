"""search_memory：把长期记忆检索做成模型可以自己调用的工具（pull 通道）。

为什么需要它：push 模式下，系统必须在模型看到问题**之前**，用一个余弦分加阈值
替它决定「哪段历史有用」。真库实测（880 条 / bge-m3）暴露出两个方向相反的错：

* **正解被误杀**：低于 0.55 的命中连窗带邻整组消失，一轮一个字都注入不进来；
* **噪声越过门**：语义无关但句式相近的短句拿到 0.62，一次注入上万字符。

噪声分和正解分是反着来的 —— 这意味着调阈值永远修不好，必须换裁判。pull 把
相关性判断交回给能读到整段对话的一方：工具返回 top-k，**不设分数门**，由模型
自己决定用不用。

三条与 push 刻意不同的约定：

1. **不给绝对下限，也不做相对截断**：排序仍由向量分决定，但"进不进得来"这个
   判断不再由分数做。阈值那道题在这里被整个取消，而不是被调小。
2. **查不到必须明确回「没有找到相关记录」**，绝不回空。回空会让模型直接编出
   「你确实没提过」——那是比注入噪声更危险的失败模式，而且用户很难察觉。
3. **描述里写清用法，常驻块里只留菜单索引**：详细说明写在工具描述里（模型只在
   需要时才读），常驻块每轮都在烧预算，只写一句「有这个能力」。
"""

from __future__ import annotations

import logging

import pull_trace
from memory_retriever import format_memory_block
from memory_runtime import (
    DEFAULT_PULL_LIMIT,
    PULL_MAX_CHARS,
    get_memory_runtime,
)

MEMORY_SEARCH_TOOL = "search_memory"
MAX_PULL_LIMIT = 20

NO_RESULT_TEXT = (
    "没有找到相关记录。本地记忆里没有与这个查询匹配的历史片段。"
    "注意：「没有找到」只代表这条查询没检索到，不能据此断定用户从未提过；"
    "也不要在回答里补编相关细节，如实告诉用户你没查到即可。"
)
DISABLED_TEXT = (
    "本地记忆检索当前不可用（可能已在设置里关闭，或 embedding 后端尚未就绪），"
    "这次无法查询历史。"
)
NO_SESSION_TEXT = "当前没有可检索的会话上下文，无法查询历史记忆。"

TOOL_DESCRIPTION = (
    "在本地历史对话记忆里检索细节。当用户说「之前说过」「上次那个」「你还记得吗」，"
    "或你需要确认某件事是否在历史里出现过时，调用本工具，不要凭印象回答。"
    "返回按相关度排序的对话片段，不设相关度门槛，由你自己判断哪几条真正有用。"
    "query 要写成完整的语义描述 —— 把「那这个」这类指代展开成它实际指的内容，"
    "否则检索不到。如果返回「没有找到相关记录」，说明记忆里确实没有，"
    "如实说明即可，不要臆造。"
)


def clamp_limit(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PULL_LIMIT
    return max(1, min(MAX_PULL_LIMIT, number))


def _resolve_runtime():
    """取本进程的记忆运行时。

    工具可能在三种进程里跑：GUI 主进程、桥接的 MCP 子进程、以及以后的独立进程。
    后两种里没有任何人建过 runtime —— 如果就这么 `get_memory_runtime()`（config=None），
    它会退回全套默认配置：embedding 后端变成"本地哈希"，与索引里记录的
    `api:bge-m3:...` 身份对不上，被身份守卫直接跳过，表现就是"永远查不到"。

    所以这里自己兜一份 ConfigManager：QSettings 存在进程之外，任何进程读到的都是
    同一份设置。GUI 里已经建过 runtime 时，传进去的 config 会被忽略（不覆盖）。
    """
    try:
        from config_manager import ConfigManager

        return get_memory_runtime(ConfigManager())
    except Exception:
        logging.warning("读取配置失败，改用默认配置构建记忆运行时", exc_info=True)
        return get_memory_runtime()


def handle_search_memory(cls, arguments):
    """tool_dispatch 的处理函数：签名与其它工具一致（cls 是 ActionHandler）。"""
    arguments = arguments or {}
    query = str(arguments.get("query") or "").strip()
    if not query:
        # 空查询也是"模型试图调用过"——这个事件本身就很有诊断价值，照样留痕。
        pull_trace.record({"query": "", "limit": 0, "groups": 0, "chars": 0, "error": "empty-query"})
        return cls._result(False, "检索词不能为空：请把要查的内容写成一句完整的描述。")

    runtime = _resolve_runtime()
    limit = clamp_limit(arguments.get("limit"))
    result = runtime.recall(getattr(runtime, "active_session_id", ""), query, limit=limit)

    # 落盘痕迹：桥接走 MCP 子进程，它的 logging 不进 app.log，所以"模型到底调没调"
    # 只能靠这个文件回答。放在 recall 之后，好把命中数一起记下来。
    #
    # 会话记的是 recall **兜底之后**真正查的那个：子进程里 runtime.active_session_id
    # 永远是空串，记它等于没记 —— 而"它查的是哪个会话"恰恰是这个痕迹最该回答的问题。
    resolved_session = str(
        result.get("session") or getattr(runtime, "active_session_id", "") or ""
    )
    pull_trace.record(
        {
            "session": resolved_session[:24],
            "query": query[: pull_trace.QUERY_CHARS],
            "limit": limit,
            "groups": len(result.get("groups") or []),
            "chars": int(result.get("chars") or 0),
            "error": str(result.get("error") or ""),
        }
    )

    error = str(result.get("error") or "")
    if error in ("disabled", "unavailable"):
        return cls._result(False, DISABLED_TEXT)
    if error == "no-session":
        return cls._result(False, NO_SESSION_TEXT)
    if error:
        return cls._result(False, f"查询本地记忆失败：{error}")

    groups = result.get("groups") or []
    block = format_memory_block(result, max_chars=PULL_MAX_CHARS) if groups else ""
    if not block:
        # 「没查到」也要有明确回执，不能回空。
        return cls._result(True, NO_RESULT_TEXT, {"count": 0, "chars": 0, "items": []})

    logging.info(
        "[memory-pull-tool] query=%dchars groups=%d chars=%d",
        len(query),
        len(groups),
        int(result.get("chars") or 0),
    )
    message = (
        f"本地记忆命中 {len(groups)} 段历史（按相关度排序，仅供参考，请自己判断哪几条有用）："
        f"\n\n{block}"
    )
    items = [
        {
            "score": round(float(group.get("score") or 0.0), 4),
            "messages": [
                {"role": message_item.get("role"), "text": message_item.get("content")}
                for message_item in group.get("messages") or []
            ],
        }
        for group in groups
    ]
    return cls._result(
        True,
        message,
        {"count": len(groups), "chars": int(result.get("chars") or 0), "items": items},
    )


def memory_tool_definitions():
    """工具表条目。只暴露 query：limit 由实现侧定默认值，模型不需要操心。"""
    return [
        {
            "type": "function",
            "function": {
                "name": MEMORY_SEARCH_TOOL,
                "description": TOOL_DESCRIPTION,
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "要检索的内容，写成完整语义描述；"
                                "有指代时先展开（例如把「那这个呢」写成「检索阈值的取舍」）。"
                            ),
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
    ]
