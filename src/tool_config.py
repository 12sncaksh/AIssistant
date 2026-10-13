"""App list, auth/config files and search-endpoint configuration."""

from __future__ import annotations
from vision_input import VisionInputController
import json
import logging
import os



def load_apps(cls, config_path=None):
    if config_path:
        if not os.path.isabs(config_path):
            config_path = os.path.join(cls._base_dir, config_path)
        cls._config_path = config_path
    if not os.path.exists(cls._config_path):
        logging.warning("配置文件 %s 不存在，应用列表为空", cls._config_path)
        cls._apps = {}
        return
    with open(cls._config_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        apps_list = data.get("apps", [])
        cls._apps = {app["name"]: app["path"] for app in apps_list}
    logging.info("已加载 %s 个应用配置", len(cls._apps))


def load_auth_config(cls, config_path=None):
    """从 config/auth.json 读取第三方 API Key（该文件已被 .gitignore 忽略）。"""
    if config_path:
        if not os.path.isabs(config_path):
            config_path = os.path.join(cls._base_dir, config_path)
        cls._auth_config_path = config_path
    cls._auth_config = {}
    try:
        with open(cls._auth_config_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        if isinstance(data, dict):
            cls._auth_config = {
                "amap_api_key": str(data.get("amap_api_key", "") or "").strip(),
                "tavily_api_key": str(data.get("tavily_api_key", "") or "").strip()
            }
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning("读取 %s 失败: %s", cls._auth_config_path, e)


def configure(cls, searxng_url=None, tavily_api_key=None, amap_api_key=None,
              base_url=None, api_key=None, vision_model=None):
    if searxng_url is not None:
        searxng_url = searxng_url.strip()
        cls._search_config["searxng_url"] = searxng_url.rstrip("/") or "http://localhost:18080"
    if tavily_api_key is not None:
        cls._search_config["tavily_api_key"] = tavily_api_key.strip()
    if amap_api_key is not None:
        cls._amap_api_key = amap_api_key.strip()
    VisionInputController.configure(
        base_url=base_url,
        api_key=api_key,
        vision_model=vision_model,
    )


def get_available_apps(cls) -> list[str]:
    if cls._apps is None:
        cls.load_apps()
    return list(cls._apps.keys())


def _get_amap_api_key(cls) -> str:
    """获取高德 API Key：显式配置 > 环境变量 AMAP_API_KEY > auth.json。"""
    if cls._amap_api_key:
        return cls._amap_api_key
    env_key = os.getenv("AMAP_API_KEY", "").strip()
    if env_key:
        return env_key
    cls.load_auth_config()
    return cls._auth_config.get("amap_api_key", "").strip()
