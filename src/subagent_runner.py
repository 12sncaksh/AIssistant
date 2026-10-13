"""Sub-agent delegation: workspace prep, resume, budgeted runs."""

from __future__ import annotations
from action_control import ActionHandler
from app_paths import get_runtime_dir
from datetime import datetime
from functools import partial
from subagent_config import SUBAGENT_MAX_RESULT_LENGTH
from subagent_config import SUBAGENT_MAX_ROUNDS
from subagent_config import SUBAGENT_PROMPTS
from subagent_config import SUBAGENT_TIMEOUT_SECONDS
from subagent_config import SUBAGENT_TIMEOUT_SECONDS_BY_TYPE
from subagent_config import SUBAGENT_TOTAL_BUDGET_BY_TYPE
from subagent_config import SUBAGENT_TOTAL_BUDGET_SECONDS
from subagent_config import SUBAGENT_WRAPUP_MARGIN_SECONDS
from subagent_config import SubagentRoundsExhausted
import json
import logging
import os
import sys
import time



def _handle_delegate_tool_call(self, tool_call_id, arguments, depth):
    agent_type = str(arguments.get("agent_type", "")).strip()
    task = str(arguments.get("task", "")).strip()
    self._tool_execution_index += 1
    self._mark_task_state("executing", f"子agent({agent_type})处理中")
    result = self._run_subagent(agent_type, task, depth=depth + 1)
    if result.get("partial"):
        result = dict(result)
        result["message"] = str(result.get("message", "")) + "\n【提示】子agent已尽力完成并返回上述结果，请直接基于它整理最终答复；除非关键信息确实缺失，不要重复调用搜索工具。"
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, ensure_ascii=False)
    }


def _prepare_code_workspace(self):
    """为 code 子agent 创建隔离工作区 + Python 虚拟环境，返回 (workspace, venv_python)。失败返回 (None, "")。"""
    try:
        root = os.path.join(get_runtime_dir(), "agent_workspace")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        workspace = os.path.join(root, f"code_{stamp}")
        os.makedirs(workspace, exist_ok=True)
        venv_dir = os.path.join(workspace, "venv")
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
        if not os.path.exists(venv_python):
            import subprocess
            try:
                subprocess.run(
                    [sys.executable, "-m", "venv", venv_dir],
                    capture_output=True, timeout=120,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as e:
                logging.warning("创建子agent虚拟环境失败：%s", str(e))
        return workspace, (venv_python if os.path.exists(venv_python) else "")
    except Exception as e:
        logging.warning("创建子agent工作区失败：%s", str(e))
        return None, ""


def _build_code_workspace_hint(self, workspace, venv_python):
    lines = [
        "\n\n【工作区约束】",
        f"- 你的工作目录是：{workspace}",
        "- list_dir / read_file / write_file 的相对路径会自动解析到该目录；不要尝试访问工作区以外的路径，也不要使用 .. 穿越到工作区外。",
        "- run_command 默认在工作目录下执行，且不允许在命令中出现工作区外的绝对路径。",
        "- 所有脚本、结果文件必须写在工作区内；禁止把文件写到临时目录（Temp）、桌面、下载、系统目录等任何工作区外位置。",
    ]
    if venv_python:
        lines += [
            f"- 已创建 Python 虚拟环境，python 路径：{venv_python}",
            f"- 运行脚本请用：& \"{venv_python}\" 脚本名.py",
            f"- 安装依赖请用：& \"{venv_python}\" -m pip install 包名",
        ]
    return "\n".join(lines)


def _build_subagent_resume_messages(self, prompt, task, partial_summary):
    resume = (
        f"原始任务：{task}\n\n"
        "你之前执行了一部分，但时间预算耗尽被中断。以下是已经完成的工具结果摘要：\n"
        f"{partial_summary or '（无已完成工具结果）'}\n\n"
        "请基于已有信息继续完成原始任务：优先收尾总结；若确实还缺关键信息，"
        "可以再补充少量工具调用。不要重复已经完成的工作。"
    )
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": resume},
    ]


def _run_subagent(self, agent_type, task, depth=1):
    started = time.perf_counter()
    step_key = f"subagent_{agent_type}"
    tool_results_start = 0
    workspace = None
    try:
        prompt = SUBAGENT_PROMPTS.get(agent_type)
        if not prompt:
            return {"success": False, "message": f"未知的子agent类型：{agent_type}"}
        if agent_type == "code":
            workspace, venv_python = self._prepare_code_workspace()
            if workspace:
                ActionHandler.set_agent_workspace(workspace)
                prompt = prompt + self._build_code_workspace_hint(workspace, venv_python)
        tools = ActionHandler.get_subagent_tool_definitions(agent_type)
        if depth >= 1:
            tools = [t for t in tools if t["function"]["name"] != "delegate_to_agent"]
        title = f"子agent({agent_type})：{task[:40]}" + ("…" if len(task) > 40 else "")
        self._set_plan_step(step_key, title, "in_progress", "已启动，正在处理…")
        self.status_update.emit(f"子agent({agent_type})正在处理…")
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": task},
        ]
        timeout = SUBAGENT_TIMEOUT_SECONDS_BY_TYPE.get(agent_type, SUBAGENT_TIMEOUT_SECONDS)
        tool_results_start = len(self.task_state.get("tool_results", []))
        total_budget = SUBAGENT_TOTAL_BUDGET_BY_TYPE.get(agent_type, SUBAGENT_TOTAL_BUDGET_SECONDS)
        deadline_total = time.perf_counter() + total_budget
        segment_index = 0
        result_text = ""
        while True:
            if not self.is_running or time.perf_counter() >= deadline_total:
                break
            segment_index += 1
            remaining = deadline_total - time.perf_counter()
            segment_timeout = max(1, min(timeout, remaining))
            segment_started = time.perf_counter()
            try:
                result_text = self._run_agent_loop(
                    messages, tools,
                    max_rounds=SUBAGENT_MAX_ROUNDS,
                    depth=depth,
                    use_stream=False,
                    step_prefix=step_key,
                    timeout=segment_timeout,
                    wrapup_margin=SUBAGENT_WRAPUP_MARGIN_SECONDS,
                    progress_key=step_key,
                    progress_title=title,
                    resume_on_exhaust=True,
                )
                break
            except SubagentRoundsExhausted:
                if time.perf_counter() >= deadline_total:
                    partial = self._collect_subagent_partial_results(tool_results_start)
                    msg = f"（子agent({agent_type})轮次预算用尽且总预算耗尽，仅完成部分任务）"
                    if partial:
                        msg += "\n" + partial
                    self._set_plan_step(step_key, title, "failed", "轮次与总预算均耗尽，已返回部分结果")
                    return {"success": True, "message": msg[:SUBAGENT_MAX_RESULT_LENGTH], "agent_type": agent_type, "partial": True}
                partial = self._collect_subagent_partial_results(tool_results_start, limit=2500)
                messages = self._build_subagent_resume_messages(prompt, task, partial)
                self.status_update.emit(f"子agent({agent_type})轮次用尽，自动续跑（第 {segment_index} 段）…")
                self._set_plan_step(step_key, title, "in_progress", f"轮次用尽已续跑第 {segment_index} 段 · 剩余 {int(deadline_total - time.perf_counter())}s")
            except RuntimeError as e:
                if "执行超时" not in str(e):
                    raise
                if time.perf_counter() >= deadline_total or (time.perf_counter() - segment_started) < 5:
                    raise
                partial = self._collect_subagent_partial_results(tool_results_start, limit=2500)
                messages = self._build_subagent_resume_messages(prompt, task, partial)
                self.status_update.emit(f"子agent({agent_type})时间预算已用尽，自动续跑（第 {segment_index} 段）…")
                self._set_plan_step(step_key, title, "in_progress", f"已续跑第 {segment_index} 段 · 剩余 {int(deadline_total - time.perf_counter())}s")
        if not self.is_running:
            partial = self._collect_subagent_partial_results(tool_results_start)
            note = "（子agent已停止，仅完成部分任务）"
            result_text = (note + "\n" + partial) if partial else note
            self._set_plan_step(step_key, title, "failed", "已停止，返回部分结果")
            return {"success": True, "message": result_text[:SUBAGENT_MAX_RESULT_LENGTH], "agent_type": agent_type, "partial": True}
        result_text = str(result_text or "").strip()
        if not result_text:
            result_text = "（子agent未返回有效内容）"
        if len(result_text) > SUBAGENT_MAX_RESULT_LENGTH:
            result_text = result_text[:SUBAGENT_MAX_RESULT_LENGTH] + "…（结果过长已截断）"
        duration_ms = int((time.perf_counter() - started) * 1000)
        logging.info("子agent %s 任务=%s 耗时=%dms 成功（续跑%d次）", agent_type, task[:80], duration_ms, max(0, segment_index - 1))
        self._set_plan_step(step_key, title, "completed", f"已完成（耗时 {duration_ms}ms，续跑 {max(0, segment_index - 1)} 次）")
        return {"success": True, "message": result_text, "agent_type": agent_type}
    except SubagentRoundsExhausted as e:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logging.warning("子agent %s 轮次预算用尽：%s", agent_type, str(e))
        partial = self._collect_subagent_partial_results(tool_results_start)
        message = f"（子agent({agent_type})轮次预算用尽，仅完成部分任务）"
        if partial:
            message += "\n" + partial
        self._set_plan_step(step_key, title, "failed", "轮次预算用尽，已返回部分结果")
        return {"success": True, "message": message[:SUBAGENT_MAX_RESULT_LENGTH], "agent_type": agent_type, "partial": True}
    except Exception as e:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logging.warning("子agent %s 执行失败：%s", agent_type, str(e))
        message = f"子agent({agent_type})执行失败: {str(e)}"
        if "执行超时" in str(e):
            partial = self._collect_subagent_partial_results(tool_results_start)
            if partial:
                message = f"（子agent({agent_type})超时，仅完成部分任务）\n{partial}"
                self._set_plan_step(step_key, title, "failed", "执行超时，已返回部分结果")
                return {"success": True, "message": message[:SUBAGENT_MAX_RESULT_LENGTH], "agent_type": agent_type, "partial": True}
        self._set_plan_step(step_key, f"子agent({agent_type})", "failed", str(e)[:200])
        return {"success": False, "message": message}
    finally:
        if workspace:
            ActionHandler.set_agent_workspace(None)


def _collect_subagent_partial_results(self, start_index, limit=3000):
    entries = []
    for item in self.task_state.get("tool_results", [])[start_index:]:
        if not item.get("success"):
            continue
        name = str(item.get("tool_name", "")).strip()
        msg = str(item.get("message", "")).strip()
        if not name or not msg:
            continue
        line = f"[{name}] {msg}"
        details = item.get("details")
        if details:
            detail_lines = [f"    - {d.get('title', '')} | {d.get('url', '')}" for d in details]
            if detail_lines:
                line += "\n" + "\n".join(detail_lines)
        if len(line) > 800:
            line = line[:800] + "…"
        entries.append(line)
    if not entries:
        return ""
    joined = "\n".join(entries)
    if len(joined) > limit:
        joined = joined[:limit] + "…"
    return "已获取的部分信息：\n" + joined
