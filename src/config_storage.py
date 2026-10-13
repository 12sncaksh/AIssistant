"""File-backed and credential settings shared by ConfigManager."""

from __future__ import annotations

import json
import os
import sys


def _config_dir():
    if getattr(sys, "frozen", False):
        runtime_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        runtime_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(runtime_dir, "config")


class CredentialConfigMixin:
    def get_api_key(self):
        key = str(self.settings.value("api_key", "") or "").strip()
        if key:
            return key
        return self._load_auth_config().get("api_key", "")

    def _load_auth_config(self) -> dict:
        """Read optional credentials from the ignored auth.json file."""
        config = {}
        auth_path = os.path.join(_config_dir(), "auth.json")
        try:
            with open(auth_path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                config = {
                    "api_key": str(data.get("api_key", "") or "").strip(),
                    "amap_api_key": str(data.get("amap_api_key", "") or "").strip(),
                    "tavily_api_key": str(data.get("tavily_api_key", "") or "").strip(),
                    "xfyun_appid": str(data.get("xfyun_appid", "") or "").strip(),
                    "xfyun_api_key": str(data.get("xfyun_api_key", "") or "").strip(),
                    "xfyun_api_secret": str(data.get("xfyun_api_secret", "") or "").strip(),
                }
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"警告: 读取 auth.json 失败: {exc}")
        return config

    def _load_file_config(self) -> dict:
        """Read optional non-sensitive defaults from config.json."""
        if self._file_config is not None:
            return self._file_config
        self._file_config = {}
        config_path = os.path.join(_config_dir(), "config.json")
        try:
            with open(config_path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._file_config = data
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"警告: 读取 config.json 失败: {exc}")
        return self._file_config

    def _get_setting_or_file(self, key, default):
        value = self.settings.value(key, None)
        if value is not None and str(value).strip():
            return value
        value = self._load_file_config().get(key, default)
        return value if value is not None and str(value).strip() else default

    def set_api_key(self, key):
        self.settings.setValue("api_key", key)

    def get_tavily_api_key(self):
        key = self.settings.value("tavily_api_key", "")
        if key and str(key).strip():
            return str(key).strip()
        return self._load_auth_config().get("tavily_api_key", "")

    def set_tavily_api_key(self, key):
        self.settings.setValue("tavily_api_key", key)

    def get_xfyun_appid(self):
        value = str(self.settings.value("xfyun_appid", "") or "").strip()
        return value or self._load_auth_config().get("xfyun_appid", "")

    def set_xfyun_appid(self, value):
        self.settings.setValue("xfyun_appid", value)

    def get_xfyun_api_key(self):
        value = str(self.settings.value("xfyun_api_key", "") or "").strip()
        return value or self._load_auth_config().get("xfyun_api_key", "")

    def set_xfyun_api_key(self, value):
        self.settings.setValue("xfyun_api_key", value)

    def get_xfyun_api_secret(self):
        value = str(self.settings.value("xfyun_api_secret", "") or "").strip()
        return value or self._load_auth_config().get("xfyun_api_secret", "")

    def set_xfyun_api_secret(self, value):
        self.settings.setValue("xfyun_api_secret", value)

    def get_xfyun_config(self):
        return {
            "appid": self.get_xfyun_appid(),
            "api_key": self.get_xfyun_api_key(),
            "api_secret": self.get_xfyun_api_secret(),
        }

    def get_amap_api_key(self):
        value = str(self.settings.value("amap_api_key", "") or "").strip()
        return value or self._load_auth_config().get("amap_api_key", "")

    def set_amap_api_key(self, key):
        self.settings.setValue("amap_api_key", key)
