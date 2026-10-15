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



def _role_prompt(self):
    """角色卡层：角色是谁 + 表达风格 + 颜文字格式。

    和硬规则分开，是因为**所有权不同**：这一段由用户在角色卡对话框里编辑，
    硬规则写在代码里、用户看不见也删不掉。
    """
    roleplay_mode = self.config.get_roleplay_mode()
    if roleplay_mode:
        expression_rule = (
            "所有回复都要像日常聊天一样自然，想到哪说到哪，不要暴露自己是模型。"
            "禁止使用 Markdown、列表、标题、表格和代码块等复杂排版格式。"
            "你可以自然地在对话里称呼用户，但称呼不要过于频繁，保持自然即可。"
        )
    else:
        expression_rule = (
            "回答保持自然、清楚，不要暴露自己是模型。"
            "你可以根据内容自由使用 Markdown 排版；代码请使用带语言名称的围栏代码块，"
            "但不要为了排版而堆砌标题、列表或表格。"
            "你可以自然地在对话里称呼用户，但称呼不要过于频繁，保持自然即可。"
        )
    role_description = self.roles.get(
        self.current_role_name,
        "你是一个AI助手，性格友好，乐于助人。",
    ) + "\n\n" + expression_rule
    # 用户提示词里可以指定临时情境（扮演剧本之类）。没有这一句，角色卡的"必须长期保持"
    # 会把它直接压死 —— 而用户想要的恰恰是"不破坏本角色的前提下玩一局扮演"。
    role_description += (
        "\n用户在提示词里明确指定的临时情境或扮演设定除外："
        "此时保留本角色的自称与语气风格，情境按其指定执行。"
    )
    return role_description + """
    【颜文字输出】
    所有的颜文字，应当使用以下格式输出：<kaomoji>标签</kaomoji>
    例如：“今天的阳光真好呀，心情都变好了～ <kaomoji>(｡•̀ᴗ•́｡)</kaomoji>”
    """


def _hard_rules_prompt(self):
    """硬规则层：工具规则 + 桌宠规则 + 安全规则。**写在代码里，用户删不掉。**

    以前这些和角色卡一起拼进"动态提示词"，而动态提示词只在 default 模式生效 ——
    用户一旦自定义提示词，安全规则 13 条就整段消失。`app_prompts.py` 的注释里写着
    "自定义或关闭模式不会自动追加"，是明知故犯。于是"硬规则"一点都不硬：
    随手清空提示词框，它们就全没了。

    现在这一层**任何模式都追加**，`none` 最多只能关掉角色卡和用户提示词。
    """
    apps_list = ActionHandler.get_available_apps()
    apps_str = "、".join(apps_list) if apps_list else "无"
    if self.config.get_roleplay_mode():
        final_format_rule = "最终答复使用自然的中文纯文本，不要输出 Markdown 格式符号。"
    else:
        final_format_rule = (
            "最终答复可按需使用 Markdown、表格或带语言名称的围栏代码块，"
            "代码内容不得声称已经执行，除非确实调用过工具。"
        )
    prompt = f"""

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
    return prompt.strip() + "\n\n" + SYSTEM_SAFETY_RULES


def _builtin_system_prompt(self):
    """程序自带的两层：角色卡 + 硬规则。不含用户自己写的那段。

    **注意这里是模块内直接调用，不走 `self.`**：main.py 只把旧名字挂到 MainWindow 上，
    `self._role_prompt` 在实例上根本不存在。
    """
    return _role_prompt(self) + "\n\n" + _hard_rules_prompt(self)


def _generate_dynamic_system_prompt(self):
    """旧名字保留：main.py 会把它挂到 MainWindow 上，改名会打断那条挂载。"""
    return _builtin_system_prompt(self)


def _format_assistant_text(self, text):
    text = str(text or "")
    text = strip_internal_reasoning(text)
    return sanitize_reply_text(text) if self.config.get_roleplay_mode() else text


def _get_default_system_prompt(self):
    """旧名字保留（同上）。现在它等于「内置部分」，不再含用户自己写的那段。"""
    return _builtin_system_prompt(self)


def _get_permission_mode_notice(self):
    """权限模式会改变模型实际能做的事，必须随系统提示词一起下发，免得它按旧规则反复试探。"""
    mode = self.config.get_permission_mode()
    if mode == FULL_ACCESS_MODE:
        return FULL_ACCESS_PROMPT_NOTICE
    if mode == READ_ONLY_MODE:
        return READ_ONLY_PROMPT_NOTICE
    return ""


def _get_effective_system_prompt(self):
    """最终下发的系统提示词 = 角色卡 → 用户提示词 → 硬规则 → 权限提示。

    顺序是有意的：
      · 角色卡在前、用户提示词紧随其后 —— 用户想演一局别的，改写对象就是紧邻的那段；
      · 硬规则压在最后，**任何模式都追加**，用户删不掉。

    旧行为是"自定义模式直接用用户文本，安全规则整段不追加"，所以清空提示词框
    等于把安全规则一起关掉 —— 那正是"硬规则一点都不硬"的来源。
    """
    mode = self.config.get_system_prompt_mode()
    parts = []
    if mode != "none":
        parts.append(_role_prompt(self))
        if mode == "custom":
            parts.append(self.config.get_custom_system_prompt())
    parts.append(_hard_rules_prompt(self))
    notice = self._get_permission_mode_notice()
    if notice:
        parts.append(notice)
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


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
