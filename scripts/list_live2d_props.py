"""列出内置 Live2D 模型的可穿戴 .exp3.json 与参数表，帮助填写 config/live2d_emotions.json。

用法（项目根目录）：
    python scripts/list_live2d_props.py              # 列出所有模型
    python scripts/list_live2d_props.py 大肥鱼        # 只看某个模型

不需要 PyQt，仅读文件。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCES = os.path.join(ROOT, "assets", "web_resources", "dist", "Resources")


def read_json(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def list_model(folder_name, want):
    if want and folder_name != want:
        return
    folder = os.path.join(RESOURCES, folder_name)
    if not os.path.isdir(folder):
        return
    files = os.listdir(folder)
    model_files = [n for n in files if n.lower().endswith(".model3.json")]
    if not model_files:
        return
    print(f"\n================ {folder_name} ================")
    print("模型文件:", model_files[0])

    exp_files = sorted(n for n in files if n.lower().endswith(".exp3.json"))
    print(f"\n-- 可穿戴 .exp3.json（{len(exp_files)} 个）--")
    for name in exp_files:
        base = name[: -len(".exp3.json")]
        try:
            data = read_json(os.path.join(folder, name))
        except (OSError, ValueError):
            print(f"  [读取失败] {base}")
            continue
        params = []
        for item in data.get("Parameters") or []:
            params.append(
                "%s=%s(%s)"
                % (item.get("Id"), item.get("Value"), item.get("Blend", "Add"))
            )
        print(f"  {base}: " + ", ".join(params))

    cdi = [n for n in files if n.lower().endswith(".cdi3.json")]
    if cdi:
        try:
            data = read_json(os.path.join(folder, cdi[0]))
        except (OSError, ValueError):
            return
        params = data.get("Parameters") or []
        print(f"\n-- cdi3 参数表（{len(params)} 个，前 40）--")
        for item in params[:40]:
            print("  %s | %s" % (item.get("Id"), item.get("Name", "")))


def main():
    if not os.path.isdir(RESOURCES):
        print("找不到 Resources 目录：", RESOURCES)
        return 1
    want = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    for name in sorted(os.listdir(RESOURCES), key=str.casefold):
        if os.path.isdir(os.path.join(RESOURCES, name)):
            list_model(name, want)
    return 0


if __name__ == "__main__":
    sys.exit(main())
