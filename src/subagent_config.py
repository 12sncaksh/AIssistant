"""Sub-agent budgets and prompts."""

from __future__ import annotations


# ======================== 子agent配置 ========================
class SubagentRoundsExhausted(RuntimeError):
    """子agent轮次预算用尽但任务未收尾，用于触发自动续跑。"""


SUBAGENT_MAX_ROUNDS = 8


SUBAGENT_TIMEOUT_SECONDS = 240


SUBAGENT_TIMEOUT_SECONDS_BY_TYPE = {
    "search": 300,
    "code": 180,
}


SUBAGENT_WRAPUP_MARGIN_SECONDS = 30


SUBAGENT_TOTAL_BUDGET_SECONDS = 600


SUBAGENT_TOTAL_BUDGET_BY_TYPE = {
    "search": 1200,
    "code": 600,
}


SUBAGENT_MAX_RESULT_LENGTH = 4000


SUBAGENT_PROMPTS = {
    "search": (
        "你是一个搜索调研子agent，负责帮主助手完成联网调研任务。\n"
        "你必须通过 search_web 工具获取真实信息，禁止编造来源和事实。\n"
        "搜到候选链接后如果摘要不够用，可以用 read_url 打开具体链接细读正文再总结。\n"
        "搜索时用 3~6 个精准关键词，不要整句提问；复杂问题拆成多次小搜索。\n"
        "可以多次搜索交叉验证，最终输出：1) 结论概述；2) 关键事实与数据；3) 信息不完整或不确定的地方要明确说明。\n"
        "使用中文回答，简洁有条理，方便主助手直接引用。"
    ),
    "code": (
        "你是一个代码子agent，负责帮主助手完成代码修改、问题排查和脚本编写任务。\n"
        "你可以用 read_file / list_dir 查看代码，用 run_command 运行命令验证，用 write_file 修改文件。\n"
        "要求：1) 动手前先用只读工具了解现状；2) 每次改动要小，改完说明改了哪些文件、为什么；\n"
        "3) run_command / write_file 是高危操作，需要用户确认，务必先说明你打算做什么；\n"
        "4) 不要动 config/auth.json、src/main.py、src/action_control.py 等关键文件，除非任务明确要求。\n"
        "最后用中文总结：改了什么、验证结果、还有哪些风险或未完成项。"
    ),
}
