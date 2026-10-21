"""Independent ACP provider configuration and DSH Web import helpers."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

WEB_PROFILE_PATCH = Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml"
ACP_PROVIDERS_SETTINGS_KEY = "dsh_acp_providers"
ACP_PROVIDER_NAME_SETTINGS_KEY = "dsh_acp_provider"

ACP_PROVIDER_PRESETS = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api": "openai-completions",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "label": "OpenAI 官方",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api": "openai-responses",
        "api_key_env": "OPENAI_API_KEY",
    },
    "openrouter": {
        "label": "OpenRouter",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api": "openai-completions",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "anthropic": {"label": "Anthropic 官方", "provider": "anthropic", "base_url": "https://api.anthropic.com", "api": "anthropic-messages", "api_key_env": "ANTHROPIC_API_KEY"},
    "google": {"label": "Google Gemini", "provider": "google", "base_url": "https://generativelanguage.googleapis.com/v1beta", "api": "google-generative-ai", "api_key_env": "GEMINI_API_KEY"},
    "groq": {"label": "Groq", "provider": "groq", "base_url": "https://api.groq.com/openai/v1", "api": "openai-completions", "api_key_env": "GROQ_API_KEY"},
    "mistral": {"label": "Mistral", "provider": "mistral", "base_url": "https://api.mistral.ai/v1", "api": "mistral-conversations", "api_key_env": "MISTRAL_API_KEY"},
    "xai": {"label": "xAI Grok", "provider": "xai", "base_url": "https://api.x.ai/v1", "api": "openai-completions", "api_key_env": "XAI_API_KEY"},
    "together": {"label": "Together AI", "provider": "together", "base_url": "https://api.together.xyz/v1", "api": "openai-completions", "api_key_env": "TOGETHER_API_KEY"},
    "fireworks": {"label": "Fireworks AI", "provider": "fireworks", "base_url": "https://api.fireworks.ai/inference/v1", "api": "openai-completions", "api_key_env": "FIREWORKS_API_KEY"},
    "moonshotai": {"label": "Moonshot AI", "provider": "moonshotai", "base_url": "https://api.moonshot.ai/v1", "api": "openai-completions", "api_key_env": "MOONSHOT_API_KEY"},
    "minimax": {"label": "MiniMax", "provider": "minimax", "base_url": "https://api.minimax.io/v1", "api": "openai-completions", "api_key_env": "MINIMAX_API_KEY"},
    "zai": {"label": "智谱 GLM", "provider": "zai", "base_url": "https://api.z.ai/api/paas/v4", "api": "openai-completions", "api_key_env": "ZAI_API_KEY"},
    "ollama": {"label": "Ollama 本地", "provider": "ollama", "base_url": "http://localhost:11434/v1", "api": "openai-completions", "api_key_env": ""},
}


def provider_presets():
    return {key: dict(value) for key, value in ACP_PROVIDER_PRESETS.items()}


def normalize_provider(item):
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or item.get("id") or "").strip()
    base_url = str(item.get("base_url") or item.get("baseURL") or "").strip().rstrip("/")
    api_key = str(item.get("api_key") or item.get("apiKey") or "").strip()
    model = str(item.get("model") or "").strip()
    models = item.get("models") or []
    if isinstance(models, dict):
        models = list(models.values())
    models = [str(model.get("id") if isinstance(model, dict) else model).strip() for model in models]
    models = [model for model in models if model]
    reasoning = item.get("reasoning_by_model") or {}
    if not isinstance(reasoning, dict):
        reasoning = {}
    reasoning = {
        str(key).strip(): [str(level).strip() for level in (levels or []) if str(level).strip()]
        for key, levels in reasoning.items()
        if str(key).strip() and isinstance(levels, (list, tuple))
    }
    if not name or not base_url:
        return None
    return {"name": name, "base_url": base_url, "api_key": api_key, "api_key_env": str(item.get("api_key_env") or item.get("apiKeyEnv") or "").strip(), "provider_kind": str(item.get("provider_kind") or item.get("kind") or "custom").strip().lower(), "preset_id": str(item.get("preset_id") or "").strip(), "api": str(item.get("api") or "").strip(), "model": model, "models": models, "reasoning_by_model": reasoning}


def load_acp_providers(settings):
    try:
        raw = settings.value(ACP_PROVIDERS_SETTINGS_KEY, "")
        data = json.loads(str(raw or "")) if raw else []
    except (TypeError, ValueError):
        data = []
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        provider = normalize_provider(item)
        if provider:
            result.append(provider)
    return result


def save_acp_providers(settings, providers):
    normalized = [normalize_provider(item) for item in (providers or [])]
    normalized = [item for item in normalized if item]
    settings.setValue(ACP_PROVIDERS_SETTINGS_KEY, json.dumps(normalized, ensure_ascii=False))
    return normalized


def selected_acp_provider(settings, providers=None):
    providers = providers if providers is not None else load_acp_providers(settings)
    selected = str(settings.value(ACP_PROVIDER_NAME_SETTINGS_KEY, "") or "").strip()
    for provider in providers:
        if provider["name"] == selected:
            return provider
    return providers[0] if providers else None


def set_selected_acp_provider(settings, name):
    settings.setValue(ACP_PROVIDER_NAME_SETTINGS_KEY, str(name or "").strip())


def merge_acp_provider(settings, provider):
    provider = normalize_provider(provider)
    if not provider:
        raise ValueError("Provider 名称和 URL 不能为空")
    providers = load_acp_providers(settings)
    replaced = False
    for index, item in enumerate(providers):
        if item["name"] == provider["name"]:
            providers[index] = provider
            replaced = True
            break
    if not replaced:
        providers.append(provider)
    save_acp_providers(settings, providers)
    set_selected_acp_provider(settings, provider["name"])
    return provider


def remove_acp_provider(settings, name):
    name = str(name or "").strip()
    providers = [item for item in load_acp_providers(settings) if item["name"] != name]
    save_acp_providers(settings, providers)
    if str(settings.value(ACP_PROVIDER_NAME_SETTINGS_KEY, "") or "").strip() == name:
        set_selected_acp_provider(settings, providers[0]["name"] if providers else "")
    return providers


def _yaml_scalar(text):
    text = str(text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def read_web_provider_config(path=WEB_PROFILE_PATCH):
    """Read the stable provider fields from DSH Web's patch without executing YAML."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise RuntimeError(f"无法读取 DSH Web 配置：{exc}") from exc
    providers = []
    current = None
    in_providers = False
    in_models = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "providers:":
            in_providers = True
            in_models = False
            continue
        if in_providers and indent == 6 and stripped.endswith(":") and not stripped.startswith("-"):
            if current:
                providers.append(current)
            current = {"name": stripped[:-1].strip(), "models": []}
            in_models = False
            continue
        if not in_providers or current is None:
            continue
        if indent <= 6 and stripped != "providers:":
            if current:
                providers.append(current)
            current = None
            in_providers = False
            in_models = False
            continue
        if stripped.startswith("apiKeyEnv:"):
            current["api_key_env"] = _yaml_scalar(stripped.split(":", 1)[1])
        elif stripped.startswith("baseURL:"):
            current["base_url"] = _yaml_scalar(stripped.split(":", 1)[1])
        elif stripped == "models:":
            in_models = True
        elif in_models and stripped.startswith("- id:"):
            current["models"].append(_yaml_scalar(stripped.split(":", 1)[1]))
        elif in_models and stripped.startswith("id:"):
            current["models"].append(_yaml_scalar(stripped.split(":", 1)[1]))
    if current:
        providers.append(current)
    result = []
    for item in providers:
        provider = normalize_provider(item)
        if provider:
            provider["api_key_env"] = str(item.get("api_key_env") or "").strip()
            provider["api_key"] = os.environ.get(provider["api_key_env"], "").strip()
            result.append(provider)
    return result




def provider_env_name(name):
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(name or "ACP")).strip("_").upper() or "ACP"
    return f"AISSISTANT_ACP_{token}_API_KEY"


def discover_provider_models(provider_info, timeout=30):
    """Fetch models from an OpenAI-compatible provider endpoint."""
    provider_info = provider_info or {}
    base_url = str(provider_info.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("API URL 不能为空")
    api_key = str(provider_info.get("api_key") or "").strip()
    if not api_key:
        env_name = str(provider_info.get("api_key_env") or "").strip()
        api_key = str(os.environ.get(env_name, "") or "").strip() if env_name else ""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{base_url}/models", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"模型列表请求失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"模型列表请求失败：{exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"模型列表读取失败：{exc}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        entries = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("models"), dict):
        entries = [{"id": key, **value} for key, value in payload["models"].items() if isinstance(value, dict)]
    else:
        raise RuntimeError("模型列表响应不是 data 数组或 models 对象")
    result = []
    seen = set()
    for item in entries:
        if isinstance(item, str):
            model_id, name = item.strip(), item.strip()
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
            name = str(item.get("name") or model_id).strip()
        else:
            continue
        if model_id and model_id not in seen:
            result.append({"id": model_id, "name": name or model_id})
            seen.add(model_id)
    return result
