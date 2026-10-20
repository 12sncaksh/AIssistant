"""DSH 桥接的环境预检与错误翻译。

这个模块只回答两个问题：

1. **现在这台机器，能不能开 DSH 桥接？**  —— ``run_checks()``
2. **这条报错，用户下一步该做什么？**    —— ``translate_error()``

两条设计约束，都是踩过坑才有的：

- **只报能证明的事**。检查项要么有确凿证据（文件存在、命令返回），要么明说
  "没查到"；翻译表只对**特征串精确命中**的错误下结论，认不出来的就原样透出
  并建议上报，绝不猜——猜错比不说更耽误排查。
- **不碰任何密钥**。这里既不读 ``config/auth.json``，也不打印路径以外的内容。

依赖只有标准库，外加本项目里同样只依赖标准库的 ``dsh_acp_permission_patch``
（复用它那套 ``@deepseek-ai`` 子树搜索，避免两处找包逻辑各自漂移）。因此它既能被
主程序调用，也能单独跑：

    python src/dsh_preflight.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dsh_acp_permission_patch import _package_candidates

# 检查结果的三档。用 ASCII 标记而不是 emoji：Windows 控制台是 GBK，emoji 会乱码，
# 图形界面自己按 level 上色即可。
OK = "ok"
WARN = "warn"
FAIL = "fail"

LEVEL_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}

MIN_NODE_MAJOR = 22
DSH_PACKAGE = "@deepseek-ai/dsh"
INSTALL_COMMAND = f"npm i -g {DSH_PACKAGE}"
UPGRADE_COMMAND = f"npm i -g {DSH_PACKAGE}@latest"
# 补丁生效后这个符号一定出现在 dsh-acp 的 lib/index.js 里（见 dsh_acp_permission_patch）。
PATCH_MARKER = "setSessionPermissionPreset"

TIMEOUT_SECONDS = 20


@dataclass
class Check:
    """一项预检结果。

    ``fix`` 是可以原样复制到终端执行的命令；``fix_label`` 是给界面用的按钮文案。
    没有 ``fix`` 的条目表示"这一步没有一键命令"，界面只展示说明。
    """

    key: str
    level: str
    title: str
    detail: str = ""
    fix: str = ""
    fix_label: str = ""

    @property
    def ok(self) -> bool:
        return self.level == OK

    @property
    def blocking(self) -> bool:
        return self.level == FAIL


# ---------------------------------------------------------------- 小工具


def _run(argv: list[str]) -> subprocess.CompletedProcess | None:
    """跑一个小命令并收集输出；失败/超时/找不到可执行文件一律返回 None。"""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _dsh_home() -> Path:
    """DSH 的 home 目录：优先 ``$DSH_HOME``，否则 ``~/.dsh``。"""
    override = os.environ.get("DSH_HOME", "").strip()
    return Path(override) if override else Path.home() / ".dsh"


def _writable(path: Path) -> bool:
    """目标目录（或它最近的已存在祖先）是否可写。"""
    candidate = path
    while True:
        if candidate.exists():
            return os.access(candidate, os.W_OK)
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def node_major_version() -> int | None:
    """返回本机 node 的主版本号；没装或认不出来返回 None。"""
    exe = shutil.which("node")
    if not exe:
        return None
    proc = _run([exe, "--version"])
    if proc is None or proc.returncode != 0:
        return None
    match = re.match(r"v?(\d+)", (proc.stdout or "").strip())
    return int(match.group(1)) if match else None


def find_patched_module() -> tuple[Path | None, bool]:
    """找 dsh-acp 的 lib/index.js，并判断补丁是否已经打上。

    返回 ``(module_path, patched)``；找不到包时 module_path 为 None。
    """
    for candidate in _package_candidates():
        module = candidate / "lib" / "index.js"
        if not module.is_file():
            continue
        try:
            head = module.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return module, PATCH_MARKER in head
    return None, False


# ---------------------------------------------------------------- 五项检查


def check_dsh_command() -> Check:
    exe = shutil.which("dsh")
    if not exe:
        return Check(
            "dsh",
            FAIL,
            "找不到 dsh 命令",
            "本机 PATH 里没有 dsh，桥接无法启动。装好之后不用重启 Aissistant，点「重新自检」即可。",
            INSTALL_COMMAND,
            "复制安装命令",
        )
    return Check("dsh", OK, "已找到 dsh", str(exe))


def check_node() -> Check:
    version = node_major_version()
    if version is None:
        return Check(
            "node",
            FAIL,
            "找不到可用的 node",
            f"DSH 需要 Node {MIN_NODE_MAJOR}+。装好 Node 后重跑一次自检。",
            "winget install OpenJS.NodeJS.LTS",
            "复制安装命令",
        )
    if version < MIN_NODE_MAJOR:
        return Check(
            "node",
            WARN,
            f"Node 版本偏低（v{version}）",
            f"DSH 要求 Node {MIN_NODE_MAJOR}+，低版本可能装不上或跑不起来。",
            "winget upgrade OpenJS.NodeJS.LTS",
            "复制升级命令",
        )
    return Check("node", OK, f"Node v{version}", "")


def check_home_writable() -> Check:
    home = _dsh_home()
    if _writable(home):
        state = "已存在" if home.exists() else "尚未创建，首次启动会自动创建"
        return Check("dsh_home", OK, "DSH 目录可写", f"{home}（{state}）")
    return Check(
        "dsh_home",
        FAIL,
        "DSH 目录不可写",
        f"{home} 写不进去。常见原因：安装在受保护目录、或上一份 DSH 以管理员身份创建过。",
        "",
        "",
    )


def check_profile() -> Check:
    profile = _dsh_home() / "profiles" / "acp"
    manifest = profile / "package.json"
    if manifest.is_file():
        return Check("profile", OK, "acp profile 已就绪", str(profile))
    return Check(
        "profile",
        WARN,
        "还没有 acp profile",
        "首次 `dsh --profile acp` 会自动创建，不需要手动处理；这里只做提示。",
        "dsh --profile acp --dump-config",
        "复制（顺便预建）",
    )


def check_patch() -> Check:
    module, patched = find_patched_module()
    if module is None:
        return Check(
            "patch",
            WARN,
            "没找到 dsh-acp 包",
            "可能 dsh 还没装好，或安装位置不在常见路径。桥接启动时会再找一次。",
            "",
            "",
        )
    if patched:
        return Check("patch", OK, "权限接口补丁已生效", str(module))
    if _writable(module.parent):
        return Check(
            "patch",
            WARN,
            "权限接口补丁尚未写入",
            f"文件可写，Aissistant 下次连接 DSH 时会自动补上（会先备份为 index.js.aissistant-backup）。\n{module}",
            "",
            "",
        )
    return Check(
        "patch",
        FAIL,
        "权限接口补丁写不进去",
        f"缺少写入权限，权限三档功能将不可用。用管理员身份运行一次 Aissistant 即可。\n{module}",
        "",
        "",
    )


def run_checks() -> list[Check]:
    """跑完整预检。顺序固定：先决条件在前，依赖它的在后。"""
    dsh = check_dsh_command()
    results = [dsh]
    # dsh 都没找到时，后面几项少了它就没有意义，但仍然给出结果，
    # 这样界面能一次把"缺什么"看全，而不是修一个冒一个。
    results.append(check_node())
    results.append(check_home_writable())
    results.append(check_profile())
    results.append(check_patch())
    return results


def blocking_checks(checks: list[Check]) -> list[Check]:
    return [item for item in checks if item.blocking]


def format_report(checks: list[Check]) -> str:
    """给控制台 / 日志用的纯文本报告。"""
    lines = []
    for item in checks:
        lines.append(f"{LEVEL_MARK.get(item.level, '[????]')} {item.title}")
        if item.detail:
            lines.append(f"         {item.detail}")
        if item.fix:
            lines.append(f"         $ {item.fix}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 错误翻译


@dataclass
class Diagnosis:
    """一条报错的"人话"版本：标题 + 说明 + 可复制命令。"""

    title: str
    detail: str = ""
    fix: str = ""

    def as_message(self) -> str:
        parts = [self.title]
        if self.detail:
            parts.append(self.detail)
        if self.fix:
            parts.append(f"$ {self.fix}")
        return "\n".join(parts)


# 只按特征串精确命中的翻译表。每一条都对应一个真实出现过的错误，
# 认不出来的一律原样透出（见 translate_error 的兜底）。
_TRANSLATIONS: tuple[tuple[tuple[str, ...], Diagnosis], ...] = (
    (
        (
            "no such file or directory",
            "not found",
            "找不到 dsh",
            "不是内部或外部命令",
            # Windows 上"启动不了子进程"最常见的形态：命令本身就不存在。
            "winerror 2",
            "找不到指定的文件",
        ),
        Diagnosis(
            "没有找到 dsh 命令",
            "桥接要调用本机的 DSH 运行时，但 PATH 里没有它。",
            INSTALL_COMMAND,
        ),
    ),
    (
        ("enotfound", "econnrefused", "econnreset", "getaddrinfo"),
        Diagnosis(
            "连不上网络",
            "这一步要访问外网（安装或下载）。检查代理 / 防火墙后重试；已经装好的话可以忽略。",
        ),
    ),
    (
        ("eacces", "eperm", "access is denied", "permission denied"),
        Diagnosis(
            "没有写入权限",
            "目标目录不允许写入。用管理员身份运行一次 Aissistant，或把它装到用户目录下。",
        ),
    ),
    (
        ("cannot change permissions while a prompt is in flight",),
        Diagnosis(
            "上一轮还没结束，这次权限改动被跳过了",
            "这是正常的时序竞争：回合结束后会自动同步，不影响这次对话。",
        ),
    ),
    (
        ("is incompatible with dsh", "failed to import", "did not activate"),
        Diagnosis(
            "DSH 插件和当前版本不匹配",
            "通常是某个插件把旧版核心包拖进了 profile。先摘掉最近装的插件，再重试。",
        ),
    ),
    (
        ("context overflow", "context length exceeded", "maximum context length", "prompt is too long"),
        Diagnosis(
            "这一轮的上下文太长了",
            "开一个新会话，或把这次任务拆小一点再让它继续。",
        ),
    ),
)


def translate_error(error: object) -> Diagnosis | None:
    """把一条异常 / 报错文本翻译成「用户下一步做什么」。

    认不出来返回 ``None`` —— 调用方应当把原文交给用户，而不是硬套一个说法。
    """
    text = str(error or "").strip()
    if not text:
        return None
    lowered = text.lower()

    # 超时单独处理：要把"等了多久"说出来，用户才知道是慢还是断。
    if "超时" in text or "timeout" in lowered or "timed out" in lowered:
        waited = re.search(r"(\d+)\s*s", text)
        detail = "这不是报错，是等太久了还没有任何进展。"
        if waited:
            detail = f"等满 {waited.group(1)} 秒仍没有任何进展，才判定为卡住。"
        return Diagnosis(
            "DSH 长时间没有响应",
            detail + "可以重试；如果反复出现，把这条提示连同 logs/ 目录下的日志一起发出来。",
        )

    for needles, diagnosis in _TRANSLATIONS:
        if any(needle in lowered for needle in needles):
            return diagnosis
    return None


def explain(error: object) -> str:
    """``translate_error`` 的便捷版：认不出来就原样返回，绝不吞掉原文。"""
    diagnosis = translate_error(error)
    if diagnosis is None:
        text = str(error or "未知错误").strip()
        return f"{text}\n\n（这条本鱼没认出来，把上面的原文发给作者看看。）"
    return diagnosis.as_message()


# ---------------------------------------------------------------- 命令行入口


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    checks = run_checks()
    print("DSH 桥接环境自检")
    print("-" * 46)
    print(format_report(checks))
    print("-" * 46)
    bad = blocking_checks(checks)
    if bad:
        print(f"结论：有 {len(bad)} 项挡住桥接，按上面的命令处理后再跑一次。")
        return 1
    print("结论：可以开 DSH 桥接。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
