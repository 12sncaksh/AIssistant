"""审计 ActionHandler 的方法绑定表（src/action_control.py 末尾那一段）。

背景
----
action_control 把 ActionHandler 的方法拆进各个 tools_*.py 模块后，在文件末尾用
classmethod(...) / staticmethod(...) 把函数绑回类上。拆模块时如果漏了 def 后面的
cls 参数，classmethod 在属性访问时会把"类"当第一个位置参数传进去，于是运行期报：

    _get_health_check() takes 0 positional arguments but 1 were given

而且只在真正调用那个工具时才炸，平时看不出来。这个脚本只读源码、不改任何东西，
一次性把这类错配全找出来：

1. classmethod 绑定的函数，第一个参数必须是 cls；
2. staticmethod 绑定的函数，函数体不能引用 cls，首参也不该叫 cls。

用法
----
    python scripts/audit_method_bindings.py              # 打印全部绑定
    python scripts/audit_method_bindings.py --problems   # 只打印有问题的

退出码：存在 ERROR 时返回 1，方便接进提交前检查。
"""

from __future__ import annotations

import ast
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
ACTION_CONTROL = os.path.join(SRC_DIR, "action_control.py")
CLASS_NAME = "ActionHandler"

_TREE_CACHE: dict[str, ast.Module] = {}


def read_tree(path: str) -> ast.Module:
    if path not in _TREE_CACHE:
        with open(path, "r", encoding="utf-8") as handle:
            _TREE_CACHE[path] = ast.parse(handle.read(), filename=path)
    return _TREE_CACHE[path]


def collect_import_map(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """本地名 -> (模块名, 模块里的原名)，只认模块级 `from X import Y [as Z]`。"""
    mapping: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                mapping[alias.asname or alias.name] = (node.module, alias.name)
    return mapping


def collect_bindings(tree: ast.Module) -> list[tuple[str, str, str, int]]:
    """[(类属性名, 绑定方式, 被绑定的函数名, 行号)]。"""
    bindings: list[tuple[str, str, str, int]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
            continue
        if target.value.id != CLASS_NAME:
            continue
        value = node.value
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)):
            continue
        if value.func.id not in ("classmethod", "staticmethod"):
            continue
        if len(value.args) != 1 or not isinstance(value.args[0], ast.Name):
            continue
        bindings.append((target.attr, value.func.id, value.args[0].id, node.lineno))
    return bindings


def find_function(module_name: str, func_name: str):
    """在 src/<module>.py 里找同名顶层函数，返回 (函数节点, 文件路径)。"""
    path = os.path.join(SRC_DIR, module_name + ".py")
    if not os.path.isfile(path):
        return None, None
    tree = read_tree(path)
    for node in tree.body:
        candidate = node
        if isinstance(node, (ast.If, ast.Try)):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner.name == func_name:
                    return inner, path
            continue
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)) and candidate.name == func_name:
            return candidate, path
    return None, path


def first_param(func) -> str:
    args = list(getattr(func.args, "posonlyargs", [])) + list(func.args.args)
    return args[0].arg if args else ""


def uses_cls(func) -> bool:
    return any(isinstance(node, ast.Name) and node.id == "cls" for node in ast.walk(func))


def main(argv: list[str]) -> int:
    problems_only = "--problems" in argv
    if not os.path.isfile(ACTION_CONTROL):
        print("找不到 %s" % ACTION_CONTROL)
        return 1

    tree = read_tree(ACTION_CONTROL)
    imports = collect_import_map(tree)
    bindings = collect_bindings(tree)

    errors = 0
    hints = 0
    checked = 0
    lines: list[str] = []

    for attr, kind, func_name, lineno in bindings:
        module_name, original = imports.get(func_name, (None, func_name))
        expr = "%s.%s = %s(%s)" % (CLASS_NAME, attr, kind, func_name)
        head = "[%s] action_control.py:%d  %s" % ("----", lineno, expr)

        if not module_name:
            detail = "不是 from-import 进来的函数（可能定义在 action_control 里），跳过"
            lines.append(" | ".join(["SKIP ", "action_control.py:%d" % lineno, expr, detail]))
            continue

        func, path = find_function(module_name, original or func_name)
        if func is None:
            detail = "在 src/%s.py 里没找到，跳过" % module_name
            lines.append(" | ".join(["SKIP ", "src/%s.py" % module_name, expr, detail]))
            continue

        checked += 1
        location = "%s:%d" % (os.path.relpath(path, PROJECT_ROOT).replace("\\", "/"), func.lineno)
        params = first_param(func)

        if kind == "classmethod":
            if params == "cls":
                lines.append(" | ".join(["OK   ", location, expr, "首参是 cls"]))
            elif uses_cls(func):
                errors += 1
                lines.append(" | ".join([
                    "ERROR", location, expr,
                    "classmethod 绑定，但首参是 %r 且函数体用到了 cls —— 调用时会多传一个类参数（%s）"
                    % (params, "takes 0 positional arguments but 1 were given" if not params else "参数整体错位"),
                ]))
            else:
                hints += 1
                lines.append(" | ".join([
                    "HINT ", location, expr,
                    "classmethod 绑定，但首参是 %r 且未用到 cls，可考虑改成 staticmethod" % params,
                ]))
        else:  # staticmethod
            if uses_cls(func):
                errors += 1
                lines.append(" | ".join([
                    "ERROR", location, expr,
                    "staticmethod 绑定，但函数体引用了 cls —— 调用时会 NameError: name 'cls' is not defined",
                ]))
            elif params == "cls":
                errors += 1
                lines.append(" | ".join([
                    "ERROR", location, expr,
                    "staticmethod 绑定，但首参是 cls —— 调用方不会传类参数，必炸",
                ]))
            else:
                lines.append(" | ".join(["OK   ", location, expr, "首参 %r" % params]))

    if not problems_only:
        for line in lines:
            print(line)
    else:
        for line in lines:
            if not line.startswith("OK"):
                print(line)

    print("")
    print("共检查 %d 个绑定：ERROR %d，HINT %d，其余正常。" % (checked, errors, hints))
    if errors:
        print("修法：给 ERROR 里那些函数补上首参 cls（classmethod 绑定必须收类）。")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
