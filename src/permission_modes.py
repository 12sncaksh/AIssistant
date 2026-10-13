"""Permission presets and the notices they inject."""

from __future__ import annotations


# ======================== 权限模式（对齐 DSH 网页端权限预设） ========================
# 与 DSH 内置预设表一一对应：每个预设把「沙箱边界」和「审批策略」捆绑在一起。
#   read-only          → sandbox: read-only          | approval: ask
#   workspace-write    → sandbox: workspace-write    | approval: ask   （默认）
#   danger-full-access → sandbox: danger-full-access | approval: never （完全权限模式）
PERMISSION_MODE_CHOICES = (
    (
        "只读（沙箱拒绝一切写入）",
        "read-only",
        "沙箱：read-only ｜ 审批：ask。只能读取文件和查询信息；写文件、执行命令、关机重启、键鼠操作会被沙箱直接拒绝。",
    ),
    (
        "工作区可写（默认）",
        "workspace-write",
        "沙箱：workspace-write ｜ 审批：ask。可读写工作区；执行命令、写文件、关机重启、外部 MCP 工具仍会逐次弹窗确认。",
    ),
    (
        "完全权限（免确认，等同 DSH danger-full-access）",
        "danger-full-access",
        "沙箱：danger-full-access ｜ 审批：never。所有工具调用不再弹确认框，沙箱不再限制工作区外的"
        "读写范围，DSH 桥接也以 danger-full-access 启动；破坏性命令黑名单与受保护写入路径"
        "（C:\\Windows、Program Files、.git 等）仍然拦截。请只在信任的任务里开启。",
    ),
)


PERMISSION_MODE_VALUES = frozenset(value for _, value, _ in PERMISSION_MODE_CHOICES)


PERMISSION_MODE_LABELS = {value: label for label, value, _ in PERMISSION_MODE_CHOICES}


PERMISSION_MODE_DESCRIPTIONS = {value: desc for _, value, desc in PERMISSION_MODE_CHOICES}


DEFAULT_PERMISSION_MODE = "workspace-write"


FULL_ACCESS_MODE = "danger-full-access"


READ_ONLY_MODE = "read-only"


# 档位高低序：只读 < 工作区可写 < 完全权限。数值变大 = 提权（模型能做的事变多）。
PERMISSION_MODE_RANK = {
    READ_ONLY_MODE: 0,
    "workspace-write": 1,
    FULL_ACCESS_MODE: 2,
}


PERMISSION_MODE_BUSY_MESSAGE = "任务进行中：请等本轮结束后再切换权限模式。"


# 权限模式会改变模型可做的事，必须在系统提示词里明确告知，否则模型会按旧规则反复试探。
READ_ONLY_PROMPT_NOTICE = (
    "【只读权限模式已开启】当前沙箱为 read-only：写文件、执行命令、关机重启和键鼠操作都会被系统直接拒绝。"
    "请只用只读工具（read_file / list_dir / 各类查询工具）完成任务；确实需要写入时，先说明原因并请用户"
    "在聊天框旁的权限按钮，或「设置 → 模型与接口 → 工具与权限」切换到「工作区可写」或「完全权限」。"
)


FULL_ACCESS_PROMPT_NOTICE = (
    "【完全权限模式已开启】用户已选择 danger-full-access 权限预设：沙箱不再限制工作区外的读写范围，"
    "所有工具调用都不再弹确认框，因此你的每一条命令都会立即真实执行。"
    "两道拦截不随权限模式放开，命中会直接报错，不要尝试绕过："
    "①破坏性命令黑名单（Remove-Item -Recurse/-Force、rm -rf、del /s、diskpart、format、bcdedit、"
    "Clear-Disk、reg delete、vssadmin delete、-EncodedCommand 等）；"
    "②受保护写入路径与凭证文件（C:\\Windows、Program Files、ProgramData、$Recycle.Bin、"
    "Recovery、System Volume Information、.git 目录、config/auth.json）。"
    "需要删除或覆盖时请改用不含这些模式的做法，并先向用户说明。"
    "请只做用户明确要求的事，绝不能在用户没明确要求时动用户的数据。"
)


# 单次响应体上限：正常回复远小于此值；超过视为网关异常，直接报错而不是把半截 JSON 丢给解析器。
API_RESPONSE_MAX_CHARS = 5_000_000
