"""Built-in Live2D model, expression and emotion catalogue."""

from __future__ import annotations
from app_paths import get_config_dir
from app_paths import get_resource_path
import json
import logging
import os



# 情绪名 -> 模型作者自带的 *.exp3.json 文件名候选（取第一个存在的）。
# 作者原版表情才是该模型「正确」的观感：物料开关之外往往还带基础五官改动
# （例如大肥鱼 开心兴奋 会同时把 ParamEyeLOpen/ParamEyeROpen 关掉），
# 只写物料开关会让物料叠在没被关掉的原脸上。
PET_EMOTION_EXPRESSIONS = {
    "happy": ("开心兴奋", "happy", "exp_02"),
    "sad": ("悲伤", "sad", "exp_05"),
    "angry": ("生气", "angry", "exp_08"),
    "surprised": ("感叹号", "surprised", "exp_07"),
    "shy": ("脸红", "shy", "exp_06"),
    "wink": ("调皮", "wink"),
    "cry": ("哭", "cry", "exp_05"),
    "sleepy": ("闭眼口水", "sleepy"),
    "love": ("爱心眼", "love"),
    "sparkle": ("星星眼", "sparkle", "exp_04"),
    "magic": ("魔爪", "magic"),
    "heart": ("心跳", "heart"),
    "excited": ("开心兴奋", "excited", "exp_04"),
    "confused": ("问号", "confused"),
    "nervous": ("流汗", "nervous"),
    "tongue": ("吐舌", "tongue"),
}


def get_live2d_model_catalog():
    """扫描内置 Live2D 模型，返回可安全切换的 model3 配置。"""
    resources_dir = get_resource_path("assets", "web_resources", "dist", "Resources")
    catalog = []
    if not os.path.isdir(resources_dir):
        return catalog
    for folder_name in sorted(os.listdir(resources_dir), key=str.casefold):
        folder_path = os.path.join(resources_dir, folder_name)
        if not os.path.isdir(folder_path):
            continue
        model_files = sorted(
            name for name in os.listdir(folder_path)
            if name.lower().endswith(".model3.json")
        )
        for model_file in model_files:
            model_path = os.path.join(folder_path, model_file)
            try:
                with open(model_path, "r", encoding="utf-8") as handle:
                    model_data = json.load(handle)
                file_refs = model_data.get("FileReferences") or {}
                if not file_refs.get("Moc") or not file_refs.get("Textures"):
                    continue
            except (OSError, ValueError, TypeError):
                logging.warning("跳过无效 Live2D 模型：%s", model_path)
                continue
            catalog.append({
                "id": folder_name,
                "label": folder_name,
                "file": model_file,
                "path": os.path.relpath(model_path, resources_dir).replace(os.sep, "/"),
            })
            break
    return catalog


def get_live2d_param_catalog(model_id=None):
    """读取指定（默认当前）模型的 .cdi3.json，返回可读参数表供大模型查询。"""
    catalog = get_live2d_model_catalog()
    model = None
    if model_id:
        model = next((item for item in catalog if item["id"] == model_id), None)
    if model is None:
        model = catalog[0] if catalog else None
    if not model:
        return []
    resources_dir = get_resource_path("assets", "web_resources", "dist", "Resources")
    model_path = os.path.join(resources_dir, *model["path"].split("/"))
    folder = os.path.dirname(model_path)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    cdi_name = next((name for name in names if name.lower().endswith(".cdi3.json")), None)
    if not cdi_name:
        return []
    try:
        with open(os.path.join(folder, cdi_name), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        logging.warning("读取 Live2D 参数表失败：%s", cdi_name)
        return []
    params = []
    for item in data.get("Parameters", []):
        pid = str(item.get("Id", "")).strip()
        if not pid:
            continue
        params.append({
            "id": pid,
            "name": str(item.get("Name", "") or ""),
            "group": str(item.get("GroupId", "") or ""),
        })
    return params


def _live2d_model_folder(model_id=None):
    """返回 (模型文件夹绝对路径, 模型id)；找不到返回 (None, None)。"""
    catalog = get_live2d_model_catalog()
    model = next((item for item in catalog if item["id"] == model_id), None) if model_id else None
    if model is None:
        model = catalog[0] if catalog else None
    if not model:
        return None, None
    resources_dir = get_resource_path("assets", "web_resources", "dist", "Resources")
    model_path = os.path.join(resources_dir, *model["path"].split("/"))
    return os.path.dirname(model_path), model["id"]


def _read_exp3_entries(folder, file_name):
    """读取一个 .exp3.json，返回 [{id, value, blend}] 原始 Delta。"""
    try:
        with open(os.path.join(folder, file_name), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        logging.warning("读取 Live2D 表情文件失败：%s", file_name)
        return []
    params = []
    for item in data.get("Parameters") or []:
        pid = str(item.get("Id", "") or "").strip()
        if not pid:
            continue
        try:
            value = float(item.get("Value", 0.0))
        except (TypeError, ValueError):
            continue
        params.append({
            "id": pid,
            "value": value,
            "blend": str(item.get("Blend", "") or "Add"),
        })
    return params


def load_live2d_emotion_config(model_id=None):
    """读取 config/live2d_emotions.json 中指定模型段（不再有 default 段）。

    结构示例见 config/templates/live2d_emotions.example.json：
        { "大肥鱼": { "happy": {"expressions": ["开心兴奋","happy"],
                              "params": [{"id":"ParamMouthForm","value":1}] } } }
    只认以模型文件夹名为键的段；未命中时内置 PET_EMOTION_EXPRESSIONS 兜底。
    """
    path = os.path.join(get_config_dir(), "live2d_emotions.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        logging.warning("读取 %s 失败：%s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    part = data.get(str(model_id or ""))
    if not isinstance(part, dict):
        return {}
    merged = {}
    for emotion, spec in part.items():
        if isinstance(spec, dict):
            merged[str(emotion).strip().lower()] = spec
    return merged


def _scan_exp3_files(folder):
    """递归收集模型目录下的 .exp3.json，返回 {文件名(不含扩展, 小写键): 相对路径}。

    多数模型把表情放在 expressions/ 子目录（如 Mao 的 exp_01..exp_08），
    也有直接放在模型根目录的（如大肥鱼），这里两种都覆盖。
    """
    found = {}
    if not folder:
        return found
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if not name.lower().endswith(".exp3.json"):
                continue
            base = name[: -len(".exp3.json")]
            rel = os.path.relpath(os.path.join(root, name), folder)
            found.setdefault(base.casefold(), rel)
    return found


def _config_explicit_params(spec):
    """把配置里的 params（绝对数值）转成 blend=Set 的条目。"""
    entries = []
    for item in (spec or {}).get("params") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id", "") or "").strip()
        if not pid:
            continue
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        entries.append({"id": pid, "value": value, "blend": "Set"})
    return entries


def get_live2d_expression_params(model_id=None):
    """返回 {情绪: [参数条目]}。

    优先级：config/live2d_emotions.json 指定的 expressions/params →
    内置 PET_EMOTION_EXPRESSIONS。exp3 返回原始 Delta（Add/Multiply），
    配置 params 返回 blend=Set 的绝对值，均由前端折算成最终数值。
    """
    folder, _ = _live2d_model_folder(model_id)
    if not folder:
        return {}
    available = _scan_exp3_files(folder)
    config = load_live2d_emotion_config(model_id)
    emotions = [e for e in PET_EMOTION_EXPRESSIONS]
    emotions += [e for e in config if e not in PET_EMOTION_EXPRESSIONS]
    table = {}
    for emotion in emotions:
        spec = config.get(emotion) or {}
        candidates = spec.get("expressions") or PET_EMOTION_EXPRESSIONS.get(emotion, ())
        entries = []
        for candidate in candidates:
            rel_path = available.get(str(candidate).casefold())
            if not rel_path:
                continue
            entries = _read_exp3_entries(folder, rel_path)
            if entries:
                break
        entries = list(entries) + _config_explicit_params(spec)
        if entries:
            table[emotion] = entries
    return table


def get_live2d_all_expressions(model_id=None):
    """扫描模型目录全部 .exp3.json，返回 {文件名(不含扩展): [条目]}，供穿戴菜单用。"""
    folder, _ = _live2d_model_folder(model_id)
    if not folder:
        return {}
    found = _scan_exp3_files(folder)
    out = {}
    for base in sorted(found, key=str.casefold):
        entries = _read_exp3_entries(folder, found[base])
        if entries:
            # 用原始大小写文件名展示
            display = os.path.basename(found[base])[: -len(".exp3.json")]
            out[display] = entries
    return out


def get_live2d_model_expression_names(model_id=None):
    """返回模型可穿戴/可播放的 .exp3.json 名称列表（用于菜单）。

    过滤掉三类不适合单独穿戴的条目：
    1) 含非 0/1 选择器参数（眼镜/头箍/情绪花花等互斥组代表），单独穿会残影；
    2) 会覆盖多处基础五官的「整体表情」（如 开心兴奋/生气/悲伤/哭/调皮/晕晕/吐舌），
       那是情绪层的活，穿戴会冲突；
    3) 名称带「动画/贴纸」且含多参数的复合件。
    """
    all_expr = get_live2d_all_expressions(model_id)
    return [name for name, entries in all_expr.items() if _is_wearable_prop(name, entries)]


def _is_wearable_prop(name, entries):
    entries = entries or []
    # 含非 0/1 值：互斥组的选择器参数，不能单独穿戴
    for item in entries:
        try:
            num = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if abs(num) not in (0.0, 1.0):
            return False
    # 驱动多处基础五官（眼/眉/嘴）的，是整体表情而非配件
    face_groups = set()
    for item in entries:
        pid = str(item.get("id", ""))
        if pid.startswith("ParamEye") or pid.startswith("ParamBrow"):
            face_groups.add("face")
        if pid.startswith("ParamMouth") or pid in ("ParamA", "ParamI", "ParamU", "ParamE", "ParamO"):
            face_groups.add("face")
    if face_groups:
        return False
    return True


def build_live2d_emotion_template(model_id=None):
    """按当前模型的 .exp3.json 生成只含该模型段的空模板（供用户填写）。

    不再生成 default 段：每个情绪给一个占位（expressions / params 均为空），
    能按内置候选名自动命中的顺手填好；另附 `_可用exp3` 列出该模型全部
    .exp3.json 文件名，供用户对照填写。
    """
    folder, _ = _live2d_model_folder(model_id)
    available = {}
    if folder:
        for _base_lower, rel in _scan_exp3_files(folder).items():
            display = os.path.basename(rel)[: -len(".exp3.json")]
            available[display.casefold()] = display
    section = {}
    for emotion, candidates in PET_EMOTION_EXPRESSIONS.items():
        matched = [
            available[str(candidate).casefold()]
            for candidate in candidates
            if str(candidate).casefold() in available
        ]
        section[emotion] = {
            "expressions": matched[:1],
            "params": [],
        }
    exp_list = sorted(set(available.values()), key=str.casefold)
    section["_可用exp3"] = exp_list
    if not exp_list:
        section["_提示"] = (
            "该模型没有任何 .exp3.json。请改用 params 直接指定参数"
            "（可在「动作映射配置」里查看模型参数，或运行 "
            "scripts/list_live2d_props.py 查看）。"
        )
    _, mid = _live2d_model_folder(model_id)
    template = {}
    if mid:
        template[mid] = section
    return template


def merge_live2d_emotion_config(template, path):
    """把模板合并进已有配置：模型段不存在则整体写入；已存在则只补缺失情绪，
    并始终刷新 `_可用exp3`/`_提示`。不覆盖用户已填的 expressions/params。"""
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                existing = json.load(handle)
        except (OSError, ValueError, TypeError):
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    meta_keys = ("_可用exp3", "_提示", "_说明")
    changed = 0
    for model_key, section in template.items():
        if not isinstance(section, dict):
            continue
        target = existing.get(model_key)
        if not isinstance(target, dict):
            existing[model_key] = section
            changed += 1
            continue
        for sub_key, sub_value in section.items():
            if sub_key in meta_keys:
                if target.get(sub_key) != sub_value:
                    target[sub_key] = sub_value
                    changed += 1
            elif sub_key not in target:
                target[sub_key] = sub_value
                changed += 1
        # 清理历史遗留的 default 段（新结构不再需要）
        existing.pop("default", None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(existing, handle, ensure_ascii=False, indent=2)
    return changed
