"""On-disk cache of DSH-probed session config options."""

from __future__ import annotations
import datetime
import json
import os
import threading
from dsh_acp_client import PROJECT_ROOT



OPTIONS_CACHE_FILENAME = "dsh_options_cache.json"


# 探测结果缓存的有效期（天）。DSH 侧的模型/档位会变，过期后自动重探一次。
OPTIONS_CACHE_MAX_AGE_DAYS = 7


def default_options_cache_path():
    """探测结果缓存文件：config/dsh_options_cache.json。"""
    return os.path.join(PROJECT_ROOT, "config", OPTIONS_CACHE_FILENAME)


def options_cache_key(provider="", model="", base_url=""):
    """缓存键：Provider、URL、模型共同决定候选项集合。"""
    return f"{str(provider or '').strip()}|{str(base_url or '').strip().rstrip('/')}|{str(model or '').strip()}"


def dsh_options_cache_scope(config):
    """决定缓存有效性的一组环境指纹。

    dsh_command / dsh_profile 会换掉整个 DSH 运行时；权限模式会改变 DSH 公布的
    沙箱与审批相关能力。三者任一变化，缓存里的候选项都不再可信。
    """
    if config is None:
        return ""
    command = config.get_dsh_command() if hasattr(config, "get_dsh_command") else ""
    profile = config.get_dsh_profile() if hasattr(config, "get_dsh_profile") else ""
    mode = config.get_permission_mode() if hasattr(config, "get_permission_mode") else ""
    return f"{command}|{profile}|{mode}"


class OptionsCacheStore:
    """探测结果缓存：把 DSH 公布的 configOptions 按 (provider, model) 存盘。

    探测要临时起一个 `dsh --profile acp` 子进程，首次十几秒起步。缓存下来后，
    再次打开配置对话框就能直接渲染完整的模型 / 推理档位列表，不必重探。
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        # 环境指纹与写入时间：整份缓存级别的元数据，set 时统一刷新。
        self.scope = ""
        self.updated_at = ""
        self._data = self._load()
        self._read_meta()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return {}
        return {
            str(key): value
            for key, value in entries.items()
            if isinstance(value, dict) and isinstance(value.get("configOptions"), list)
        }

    def _save(self):
        payload = {
            "version": 1,
            "scope": self.scope,
            "updated_at": self.updated_at,
            "entries": self._data,
        }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temp_path = self.path + ".tmp"
            with open(temp_path, "w", encoding="utf-8", newline="") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temp_path, self.path)
        except OSError:
            pass

    # 环境指纹与写入时间：整份缓存级别的元数据，set 时统一刷新。
    def _read_meta(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.scope = str(data.get("scope") or "")
            self.updated_at = str(data.get("updated_at") or "")

    def _is_fresh(self):
        """缓存是否仍在有效期内。时间戳无法解析时按过期处理（宁可重探）。"""
        if not self.updated_at:
            return False
        try:
            stamp = datetime.datetime.fromisoformat(self.updated_at)
        except ValueError:
            return False
        age = datetime.datetime.now() - stamp
        return age <= datetime.timedelta(days=OPTIONS_CACHE_MAX_AGE_DAYS)

    def get(self, key, scope=""):
        """取一个缓存的 configOptions 列表；未命中、指纹不符或已过期时返回 None。"""
        key = str(key or "")
        if not key:
            return None
        with self._lock:
            if self.scope != str(scope or ""):
                return None
            if not self._is_fresh():
                return None
            entry = self._data.get(key)
        if not entry:
            return None
        return list(entry.get("configOptions") or [])

    def set(self, key, options, scope=""):
        key = str(key or "")
        if not key or not isinstance(options, (list, tuple)) or not options:
            return
        with self._lock:
            # 换了 DSH 运行时：旧条目全部作废，避免混入不同 profile 的候选项
            if self.scope != str(scope or ""):
                self._data = {}
                self.scope = str(scope or "")
            self._data[key] = {"configOptions": list(options)}
            self.updated_at = datetime.datetime.now().isoformat(timespec="seconds")
            self._save()

    def clear(self):
        with self._lock:
            self._data = {}
            self.updated_at = ""
            self._save()
