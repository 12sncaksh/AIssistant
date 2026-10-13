"""System prompt assembly and action-handler refresh."""

from __future__ import annotations
from action_control import ActionHandler
from app_prompts import SYSTEM_SAFETY_RULES
from chat_markdown import strip_internal_reasoning
from permission_modes import FULL_ACCESS_MODE
from permission_modes import FULL_ACCESS_PROMPT_NOTICE
from permission_modes import READ_ONLY_MODE
from permission_modes import READ_ONLY_PROMPT_NOTICE
from text_protocol import sanitize_reply_text



def _generate_dynamic_system_prompt(self):
    """生成以工具调用为核心的系统提示词。"""
    apps_list = ActionHandler.get_available_apps()
    apps_str = "、".join(apps_list) if apps_list else "无"

    # 角色基本设定
    roleplay_mode = self.config.get_roleplay_mode()
    if roleplay_mode:
        expression_rule = (
            "所有回复都要像日常聊天一样自然，想到哪说到哪，不要暴露自己是模型。"
            "禁止使用 Markdown、列表、标题、表格和代码块等复杂排版格式。"
            "你可以自然地在对话里称呼用户，但称呼不要过于频繁，保持自然即可。"
        )
        final_format_rule = "最终答复使用自然的中文纯文本，不要输出 Markdown 格式符号。"
    else:
        expression_rule = (
            "回答保持自然、清楚，不要暴露自己是模型。"
            "你可以根据内容自由使用 Markdown 排版；代码请使用带语言名称的围栏代码块，"
            "但不要为了排版而堆砌标题、列表或表格。"
            "你可以自然地在对话里称呼用户，但称呼不要过于频繁，保持自然即可。"
        )
        final_format_rule = (
            "最终答复可按需使用 Markdown、表格或带语言名称的围栏代码块，"
            "代码内容不得声称已经执行，除非确实调用过工具。"
        )
    role_description = self.roles.get(
        self.current_role_name,
        "你是一个AI助手，性格友好，乐于助人。",
    ) + "\n\n" + expression_rule
    prompt = role_description + """
    【颜文字输出】
    所有的颜文字，应当使用以下格式输出：<kaomoji>标签</kaomoji>
    例如：“今天的阳光真好呀，心情都变好了～ <kaomoji>(｡•̀ᴗ•́｡)</kaomoji>”
    """
    prompt += f"""

    【工具使用规则】
    - 你可以通过系统提供的工具执行真实操作或联网查询，不要伪造已经执行过的结果。
    - apps.json 中的应用是快捷访问列表，优先使用其中的快捷方式：{apps_str}。
    - 如果目标应用不在快捷访问列表中，可以在用户确认后使用 run_command/PowerShell 启动，或使用视觉键鼠通过开始菜单、桌面或任务栏打开；不要因为 apps.json 没有该应用就直接说无法打开。
    - 用户询问今天几号、星期几、现在几点、当前日期或当前时间时，必须先使用时间工具，不要凭记忆回答。
    - 用户询问实时信息、新闻、价格、最新动态、网页资料时，优先使用联网搜索工具。
    - 涉及体育比赛、世界杯、联赛、比分、赛况、积分榜时，搜索词必须忠实于用户原意，不要擅自改错年份；赛事类型不明确时优先先问清楚。
    - 只有在用户明确给出完整 http:// 或 https:// 链接时，才使用打开网址工具。
    - 如果用户需求缺少关键参数，就先追问，不要猜测。
    - 关机和重启属于高风险操作，只有当用户明确要求时才调用。
    - 不要输出任何工具调用协议、标签或 JSON；需要操作时直接调用工具即可。
    - 用户问电脑卡不卡、要不要清理、电脑状态是否健康时，先用 health_check 工具做体检，再用通俗语言解读并给出具体建议。
    - 用户想清理磁盘、找大文件、看临时文件占用时，用 disk_scan 工具做只读扫描，列举结果并给出建议；不要删除任何文件，也不要建议执行删除命令。
    - 工具执行完成后，再基于工具返回结果自然回复用户。
    - {final_format_rule}
    """
    prompt += """

    【桌宠 Live2D 表现】
    - 你有一只桌面桌宠，可以用 control_pet 工具让它做出情绪表情，这是你与用户互动的一部分，能让交流更生动。
    - 情绪明显、语气有起伏时就可以自然地调用，不必等用户点名要求；一条回复最多一次，别机械地每句都换表情，也别在纯信息型/严肃回复里硬套。
    - 表情会在你本条回复开始播报/朗读时随说话动作一起展示，工作过程中不会抢动画，所以不用担心和任务动画冲突。
    - control_pet 的 emotion 可选：happy、sad、angry、surprised、shy、wink、cry、sleepy、love、sparkle、magic、heart、excited、confused、nervous、tongue、neutral。intensity 为 0~1 的强度，duration_ms 为持续时间（默认 6000，500~30000）。neutral 表示立即收回表情。
    - 需要更精细或模型专属的表现时，可用 set_live2d_param 直接设置参数；不确定参数名时先用 list_live2d_params 查询当前模型支持的参数，数值越界会被自动裁剪，到期后表情会自动回落。
    - 这些工具只影响桌宠外观，不会修改系统；不要因为调用它们而在回复里解释技术细节，保持自然即可。
    """
    return prompt


def _format_assistant_text(self, text):
    text = str(text or "")
    text = strip_internal_reasoning(text)
    return sanitize_reply_text(text) if self.config.get_roleplay_mode() else text


def _get_default_system_prompt(self):
    return self._generate_dynamic_system_prompt() + "\n\n" + SYSTEM_SAFETY_RULES


def _get_permission_mode_notice(self):
    """权限模式会改变模型实际能做的事，必须随系统提示词一起下发，免得它按旧规则反复试探。"""
    mode = self.config.get_permission_mode()
    if mode == FULL_ACCESS_MODE:
        return FULL_ACCESS_PROMPT_NOTICE
    if mode == READ_ONLY_MODE:
        return READ_ONLY_PROMPT_NOTICE
    return ""


def _get_effective_system_prompt(self):
    mode = self.config.get_system_prompt_mode()
    if mode == "none":
        return ""
    if mode == "custom":
        prompt = self.config.get_custom_system_prompt()
    else:
        prompt = self._get_default_system_prompt()
    notice = self._get_permission_mode_notice()
    if not notice:
        return prompt
    if not str(prompt or "").strip():
        return notice
    if notice in prompt:
        return prompt
    return prompt.rstrip() + "\n\n" + notice


def _build_system_prompt_messages(self):
    if not self.runtime_system_prompt.strip():
        return []
    return [{"role": "system", "content": self.runtime_system_prompt}]


def load_system_prompt(self):
    self.conversation_history = self._build_system_prompt_messages()


def refresh_action_handler_config(self):
    # 权限模式先落到工具层沙箱，让内置工具与配置保持一致
    ActionHandler.set_sandbox_mode(self.config.get_permission_mode())
    ActionHandler.configure(
        searxng_url=self.config.get_searxng_url(),
        tavily_api_key=self.config.get_tavily_api_key(),
        amap_api_key=self.config.get_amap_api_key(),
        base_url=self.config.get_base_url(),
        api_key=self.config.get_api_key(),
        vision_model=self.config.get_vision_model(),
    )
