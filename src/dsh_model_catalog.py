"""Read the model catalog shipped inside the installed DSH (pi-ai) package.

DSH 的 ACP 会话只会公布「已配置 Provider」的模型，所以用临时 ACP 连接去问
官方 Provider 的目录并不可靠；而 DSH 自带的 pi-ai 目录本身就是它用来判断
模型能力（含 reasoningEfforts / thinkingLevelMap）的真源。

这个模块只读本地 JSON：没有网络、不启动 dsh 子进程，也不修改任何 DSH 文件。
读不到就返回空列表，调用方自行回退到端点探测。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

CATALOG_SUBPATH = Path(
    "@deepseek-ai", "dsh", "node_modules", "@earendil-works", "pi-ai",
    "dist", "providers", "data",
)
_CACHE = {}


def _candidate_dirs():
    """按可靠性排列可能存放 pi-ai 目录数据的目录。"""
    candidates = []
    override = str(os.environ.get("AISSISTANT_DSH_CATALOG_DIR") or "").strip()
    if override:
        candidates.append(Path(override))

    executable = shutil.which("dsh")
    if executable:
        # npm 的 shim（dsh.cmd / dsh.ps1）就放在 npm 前缀根目录，
        # 所以从它的父目录往上试几层就能拼出 node_modules 路径。
        shim = Path(executable).resolve()
        for parent in (shim.parent, shim.parent.parent, shim.parent.parent.parent):
            candidates.append(parent / "node_modules" / CATALOG_SUBPATH)

    appdata = str(os.environ.get("APPDATA") or "").strip()
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / CATALOG_SUBPATH)

    localappdata = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if localappdata:
        candidates.append(
            Path(localappdata) / "Programs" / "DeepSeek Harness" / "resources"
            / "app.asar.unpacked" / "dsh" / "node_modules" / "@earendil-works" / "pi-ai"
            / "dist" / "providers" / "data"
        )

    result = []
    seen = set()
    for item in candidates:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def catalog_dir():
    """返回可用的 pi-ai 模型目录目录；找不到时返回 None。"""
    for candidate in _candidate_dirs():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _reasoning_levels(entry):
    """把 pi-ai 的 thinkingLevelMap 翻译成可用档位列表（不含 off）。"""
    if not entry.get("reasoning"):
        return []
    mapping = entry.get("thinkingLevelMap")
    if not isinstance(mapping, dict) or not mapping:
        return ["low", "medium", "high"]
    levels = []
    for level in ("minimal", "low", "medium", "high", "xhigh", "max"):
        value = mapping.get(level)
        if value is None or value == "":
            continue
        levels.append(level)
    return levels


def provider_catalog_models(provider_id):
    """读某个 Provider 在 DSH 内置目录中的模型。

    返回 [{"id", "name", "api", "base_url", "reasoning_levels"}]；读不到返回 []。
    """
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        return []
    cache_key = provider_id.lower()
    if cache_key in _CACHE:
        return [dict(item) for item in _CACHE[cache_key]]

    directory = catalog_dir()
    if directory is None:
        return []
    path = directory / f"{provider_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        _CACHE[cache_key] = []
        return []
    if not isinstance(payload, dict):
        return []

    models = {}
    for api_block in payload.values():
        if not isinstance(api_block, dict):
            continue
        for model_id, entry in api_block.items():
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or model_id or "").strip()
            if not model_id or model_id in models:
                continue
            models[model_id] = {
                "id": model_id,
                "name": str(entry.get("name") or model_id).strip(),
                "api": str(entry.get("api") or "").strip(),
                "base_url": str(entry.get("baseUrl") or "").strip().rstrip("/"),
                "reasoning_levels": _reasoning_levels(entry),
            }
    result = list(models.values())
    _CACHE[cache_key] = result
    return [dict(item) for item in result]


def provider_reasoning_map(provider_id):
    """返回 {model_id: [档位...]}，只包含确实声明了推理的模型。"""
    return {
        item["id"]: list(item["reasoning_levels"])
        for item in provider_catalog_models(provider_id)
        if item.get("reasoning_levels")
    }
