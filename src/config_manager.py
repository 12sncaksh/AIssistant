"""Application settings manager."""

from __future__ import annotations
from PyQt6.QtCore import QSettings
from app_paths import get_default_avatar_path
from app_paths import resolve_avatar_path
from app_paths import serialize_avatar_path
from chat_markdown import DEFAULT_CODE_BLOCK_COLOR
from chat_style import DEFAULT_BUBBLE_ASSISTANT_COLOR
from chat_style import DEFAULT_BUBBLE_USER_COLOR
from chat_style import clamp_chat_font_size
from chat_style import clamp_opacity
from chat_style import normalize_hex_color
from config_storage import CredentialConfigMixin
from dsh_options_cache import OptionsCacheStore
from dsh_options_cache import default_options_cache_path
from dsh_options_cache import dsh_options_cache_scope
from dsh_options_cache import options_cache_key
import copy
import json
import logging
import os

from app_prompts import DEFAULT_ROLES, DEFAULT_SYSTEM_PROMPT, normalize_roles
from app_theme import normalize_ui_theme
from permission_modes import DEFAULT_PERMISSION_MODE, FULL_ACCESS_MODE, PERMISSION_MODE_VALUES, READ_ONLY_MODE
from reasoning_options import REASONING_EFFORT_VALUES


class ConfigManager(CredentialConfigMixin):
    """管理API配置和系统设置"""
    def __init__(self):
        self.settings = QSettings("AI_Assistant", "Desktop_AI_Helper")
        self._auth_config = None
        self._file_config = None
        self._permission_mode = None  # 权限模式进程内缓存（见 get_permission_mode）

    def get_base_url(self):
        return str(self._get_setting_or_file("base_url", "https://api.openai.com/v1")).strip()

    def set_base_url(self, url):
        self.settings.setValue("base_url", url)

    def get_model(self):
        return str(self._get_setting_or_file("model", "gpt-3.5-turbo")).strip()

    def set_model(self, model):
        self.settings.setValue("model", model)

    def get_vision_model(self):
        return str(self._get_setting_or_file("vision_model", self.get_model())).strip()

    def set_vision_model(self, model):
        self.settings.setValue("vision_model", str(model or "").strip())

    def get_reasoning_effort(self):
        value = str(self._get_setting_or_file("reasoning_effort", "") or "").strip().lower()
        return value if value in REASONING_EFFORT_VALUES else ""

    def set_reasoning_effort(self, value):
        value = str(value or "").strip().lower()
        self.settings.setValue("reasoning_effort", value if value in REASONING_EFFORT_VALUES else "")

    # --- 权限模式（对齐 DSH 权限预设：沙箱模式 + 审批策略） ---
    def get_permission_mode(self):
        # 进程内缓存优先：QSettings 在受限环境里可能写入失败，权限模式不能因此变成空操作
        if self._permission_mode is not None:
            return self._permission_mode
        value = str(self._get_setting_or_file("permission_mode", DEFAULT_PERMISSION_MODE) or "").strip().lower()
        mode = value if value in PERMISSION_MODE_VALUES else DEFAULT_PERMISSION_MODE
        self._permission_mode = mode
        return mode

    def set_permission_mode(self, value):
        value = str(value or "").strip().lower()
        mode = value if value in PERMISSION_MODE_VALUES else DEFAULT_PERMISSION_MODE
        self._permission_mode = mode
        self.settings.setValue("permission_mode", mode)
        return mode

    def is_full_access_mode(self):
        return self.get_permission_mode() == FULL_ACCESS_MODE

    def is_read_only_mode(self):
        return self.get_permission_mode() == READ_ONLY_MODE

    def get_dsh_bridge_enabled(self):
        value = self._get_setting_or_file("dsh_bridge_enabled", False)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def set_dsh_bridge_enabled(self, enabled):
        self.settings.setValue("dsh_bridge_enabled", "true" if enabled else "false")

    def get_dsh_command(self):
        return str(self._get_setting_or_file("dsh_command", "dsh")).strip() or "dsh"

    def get_dsh_profile(self):
        return str(self._get_setting_or_file("dsh_profile", "acp")).strip() or "acp"

    def get_dsh_provider(self):
        return str(self._get_setting_or_file("dsh_provider", "")).strip()

    def set_dsh_provider(self, value):
        self.settings.setValue("dsh_provider", str(value or "").strip())

    def get_dsh_model(self):
        return str(self._get_setting_or_file("dsh_model", "")).strip()

    def set_dsh_model(self, value):
        self.settings.setValue("dsh_model", str(value or "").strip())

    def get_dsh_reasoning_effort(self):
        """DSH 推理档位（ACP 的 reasoning_effort 选项值）；空串表示跟随 DSH / 模型默认。"""
        return str(self._get_setting_or_file("dsh_reasoning_effort", "")).strip()

    def set_dsh_reasoning_effort(self, value):
        self.settings.setValue("dsh_reasoning_effort", str(value or "").strip())

    def get_dsh_known_providers(self):
        """上次探测确认可用的 DSH provider 列表；空表示尚未探测过。

        用于校验路由覆盖：DSH 的 provider / 模型会经常变，配置里残留的旧值一旦写进
        overlay，DSH 就会在 session/new 直接回 Internal error。
        """
        raw = self.settings.value("dsh_known_providers", "")
        if isinstance(raw, (list, tuple)):
            return [str(item).strip() for item in raw if str(item).strip()]
        return [part.strip() for part in str(raw or "").split(",") if part.strip()]

    def set_dsh_known_providers(self, providers):
        joined = ",".join(
            sorted({str(item).strip() for item in (providers or []) if str(item).strip()})
        )
        self.settings.setValue("dsh_known_providers", joined)

    def get_dsh_rejected_providers(self):
        """被实测判定为失效的 DSH provider；这些值永远不再写进 overlay。

        为什么需要单独一份名单：`set_dsh_provider("")` 在 config.json 里仍有非空值时
        会被文件回退盖回来（见 `_get_setting_or_file`），所以「清掉」必须落在这里。
        """
        raw = self.settings.value("dsh_rejected_providers", "")
        if isinstance(raw, (list, tuple)):
            return [str(item).strip() for item in raw if str(item).strip()]
        return [part.strip() for part in str(raw or "").split(",") if part.strip()]

    def add_dsh_rejected_provider(self, provider):
        value = str(provider or "").strip()
        if not value:
            return
        current = set(self.get_dsh_rejected_providers())
        current.add(value)
        self.settings.setValue("dsh_rejected_providers", ",".join(sorted(current)))

    # --- 本地语义记忆（长期记忆/检索/摘要） ---
    # 所有阈值都刻意放宽：默认值只在极端情况下（单条超长消息、大量重复命中）
    # 才起保护作用，正常使用不会因为预算太小而丢上下文。
    def _memory_bool(self, key, default):
        value = self.settings.value(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(default if value is None else value)

    def _memory_int(self, key, default, minimum=0):
        try:
            value = int(self.settings.value(key, default))
        except (TypeError, ValueError):
            return default
        return max(minimum, value)

    def get_memory_enabled(self):
        return self._memory_bool("memory_enabled", True)

    def set_memory_enabled(self, enabled):
        self.settings.setValue("memory_enabled", bool(enabled))

    def get_memory_embedding_backend(self):
        value = str(self.settings.value("memory_embedding_backend", "auto") or "").strip().lower()
        return value if value in ("auto", "api", "local", "off") else "auto"

    def set_memory_embedding_backend(self, value):
        value = str(value or "").strip().lower()
        self.settings.setValue(
            "memory_embedding_backend",
            value if value in ("auto", "api", "local", "off") else "auto",
        )

    def get_memory_embedding_model(self):
        return str(self.settings.value("memory_embedding_model", "") or "").strip()

    def set_memory_embedding_model(self, value):
        self.settings.setValue("memory_embedding_model", str(value or "").strip())

    def get_memory_embedding_base_url(self):
        return str(self.settings.value("memory_embedding_base_url", "") or "").strip()

    def set_memory_embedding_base_url(self, value):
        self.settings.setValue("memory_embedding_base_url", str(value or "").strip())

    def get_memory_embedding_api_key(self):
        """Embedding 专用 Key；留空表示回落到聊天 API Key。"""
        return str(self.settings.value("memory_embedding_api_key", "") or "").strip()

    def set_memory_embedding_api_key(self, value):
        self.settings.setValue("memory_embedding_api_key", str(value or "").strip())

    def get_memory_index_all_sessions(self):
        return self._memory_bool("memory_index_all_sessions", False)

    def get_memory_retrieval_enabled(self):
        return self._memory_bool("memory_retrieval_enabled", True)

    def set_memory_retrieval_enabled(self, enabled):
        self.settings.setValue("memory_retrieval_enabled", bool(enabled))

    def get_memory_recent_messages(self):
        return self._memory_int("memory_recent_messages", 12, minimum=2)

    def set_memory_recent_messages(self, value):
        self.settings.setValue("memory_recent_messages", self._memory_int("memory_recent_messages", value, minimum=2))

    def get_memory_retrieval_top_k(self):
        return self._memory_int("memory_retrieval_top_k", 12, minimum=1)

    def get_memory_retrieval_group_radius(self):
        return self._memory_int("memory_retrieval_group_radius", 2)

    def get_memory_retrieval_max_groups(self):
        return self._memory_int("memory_retrieval_max_groups", 8, minimum=1)

    def get_memory_retrieval_max_chars(self):
        return self._memory_int("memory_retrieval_max_chars", 24000, minimum=500)

    def get_memory_retrieval_min_score(self):
        """语义检索的绝对相似度下限（0~1，浮点）。只对语义模型生效。

        默认 0.55 来自实测：bge-m3 对无关中文文本的相似度约 0.43~0.51，
        相关片段在 0.55 以上。调低会注入更多（可能不相关）的历史，
        调高会更保守、记忆更"安静"。
        """
        try:
            return max(0.0, min(1.0, float(self.settings.value("memory_retrieval_min_score", 0.55))))
        except (TypeError, ValueError):
            return 0.55

    def set_memory_retrieval_min_score(self, value):
        try:
            clamped = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            clamped = 0.55
        self.settings.setValue("memory_retrieval_min_score", clamped)

    # --- 常驻块（core memory）：永远在场的那部分记忆 ---
    # 它与检索层是两条路：不参与打分，所以没有阈值可调；代价是每一轮都在烧预算，
    # 因此结构上必须字段化并各自限长（每栏预算存在 data/core_memory.json 里）。
    def get_core_memory_enabled(self):
        return self._memory_bool("core_memory_enabled", True)

    def set_core_memory_enabled(self, enabled):
        self.settings.setValue("core_memory_enabled", bool(enabled))

    def get_core_memory_extract_enabled(self):
        return self._memory_bool("core_memory_extract_enabled", True)

    def set_core_memory_extract_enabled(self, enabled):
        self.settings.setValue("core_memory_extract_enabled", bool(enabled))

    def get_core_memory_extract_min_messages(self):
        """攒够多少条新消息才值得花一次 LLM 调用做偏好提炼。"""
        return self._memory_int("core_memory_extract_min_messages", 6, minimum=2)

    def set_core_memory_extract_min_messages(self, value):
        self.settings.setValue(
            "core_memory_extract_min_messages",
            self._memory_int("core_memory_extract_min_messages", value, minimum=2),
        )

    def get_core_memory_extract_cooldown(self):
        """同一会话两次提炼之间的最小间隔（秒），防止连续提问触发多次调用。"""
        return self._memory_int("core_memory_extract_cooldown", 600, minimum=60)

    def set_core_memory_extract_cooldown(self, value):
        self.settings.setValue(
            "core_memory_extract_cooldown",
            self._memory_int("core_memory_extract_cooldown", value, minimum=60),
        )

    def get_memory_query_turns(self):
        """检索 query 拼接的近期轮数（1 轮 = 一问一答）。

        默认刻意很小：query 拼得越长，语义被平均得越厉害。把十几轮全塞进去
        会把查询向量稀释成一锅粥，反而比只用当前消息更差。上限 6 轮。
        """
        return min(6, self._memory_int("memory_query_turns", 2, minimum=1))

    def set_memory_query_turns(self, value):
        self.settings.setValue(
            "memory_query_turns",
            min(6, self._memory_int("memory_query_turns", value, minimum=1)),
        )

    def get_memory_pull_enabled(self):
        """pull 通道开关（search_memory 工具），默认开。

        打开时本地路径的自动向量注入（push）会让位：两条路同时开会让同一段历史
        被注入一次、又被模型查一次，白烧 token。关掉即完全回到改动前的 push 行为。
        常驻块不受这个开关影响 —— 它本来就不参与打分。
        """
        return self._memory_bool("memory_pull_enabled", True)

    def set_memory_pull_enabled(self, enabled):
        self.settings.setValue("memory_pull_enabled", bool(enabled))

    def get_memory_hybrid_enabled(self):
        """混合检索开关（dense + 词法 BM25 + RRF 融合），默认开，**只作用于 pull 路**。

        它只改**排序**、不改取舍：dense 走 min_ratio / min_score，词法走 best×0.5 的
        相对门，RRF 用名次把两路合起来。关掉即回到纯向量检索。

        为什么 push 不跟着开：push 的 dense 被门槛削到只剩几条，而词法路是宽池 40 条，
        融合时词法会压倒 dense —— 真库实测 push 召回从 81.0% 掉到 61.9%，注入字符还
        更多。pull 不设门，两路池宽对称，融合才成立。
        """
        return self._memory_bool("memory_hybrid_enabled", True)

    def set_memory_hybrid_enabled(self, enabled):
        self.settings.setValue("memory_hybrid_enabled", bool(enabled))

    def get_memory_context_max_chars(self):
        return self._memory_int("memory_context_max_chars", 400000, minimum=2000)

    def get_memory_summary_enabled(self):
        return self._memory_bool("memory_summary_enabled", True)

    def set_memory_summary_enabled(self, enabled):
        self.settings.setValue("memory_summary_enabled", bool(enabled))

    def get_memory_summary_llm_enabled(self):
        # 默认开：滚动增量摘要的输入只有"旧摘要 + 新增轮次"，成本低，而 LLM 式
        # 才能产出"话题清单"形态的目录（抽取式只是挑原句）。失败会自动回退本地。
        return self._memory_bool("memory_summary_llm_enabled", True)

    def set_memory_summary_llm_enabled(self, enabled):
        self.settings.setValue("memory_summary_llm_enabled", bool(enabled))

    def get_memory_summary_max_chars(self):
        # 摘要是"目录"不是"文章"：1000 字够装 15~20 个话题，且每轮附加的成本可控。
        return self._memory_int("memory_summary_max_chars", 1000, minimum=200)

    def get_memory_summary_refresh_growth(self):
        # 滚动步长 24 条 = 12 轮，与 DSH 的 seed 窗口（dsh_seed_turns × 2）同宽，
        # 保证摘要边界紧贴 seed 起点、中间不留缝。
        return self._memory_int("memory_summary_refresh_growth", 24, minimum=1)

    # --- 会话切换：UI 分页 ---
    # 每页 10 条：分页首次绘制只构造 10 个气泡，长会话切换更跟手；
    # 更早的内容在上滑时按页补载，功能不受影响。
    def get_chat_history_page_size(self):
        return self._memory_int("chat_history_page_size", 10, minimum=5)

    def set_chat_history_page_size(self, value):
        self.settings.setValue("chat_history_page_size", self._memory_int("chat_history_page_size", value, minimum=5))

    # --- DSH 桥接：本地上下文补给 ---
    def get_dsh_memory_enabled(self):
        return self._memory_bool("dsh_memory_enabled", True)

    def get_dsh_seed_turns(self):
        return self._memory_int("dsh_seed_turns", 12, minimum=1)

    def get_dsh_seed_max_chars(self):
        return self._memory_int("dsh_seed_max_chars", 4000, minimum=100)

    def get_dsh_full_recall_enabled(self):
        """用户明确要求「完整回顾」时，是否允许注入未截断的完整历史。"""
        return self._memory_bool("dsh_full_recall_enabled", True)

    # --- 本地 API：回忆类提问 ---
    def get_local_full_recall_enabled(self):
        """本地 API 路径：用户明确要求「完整回顾」时，是否改为发送完整历史。

        关掉后退回压缩上下文（摘要 + 语义检索 + 最近轮次），超长会话更安全，
        代价是「把完整记录整理一下」这类请求只能得到概述。
        """
        return self._memory_bool("local_full_recall_enabled", True)

    def set_local_full_recall_enabled(self, enabled):
        self.settings.setValue("local_full_recall_enabled", bool(enabled))

    # --- DSH 探测结果缓存（config/dsh_options_cache.json） ---
    def _dsh_options_store(self):
        """进程内单例：探测缓存不需要每次读写都重新解析 JSON 文件。"""
        store = getattr(self, "_dsh_options_cache_store", None)
        if store is None:
            store = OptionsCacheStore(default_options_cache_path())
            self._dsh_options_cache_store = store
        return store

    def get_cached_dsh_options(self, provider="", model=""):
        """按 (provider, model) 取缓存的 configOptions；未命中/过期返回 None。"""
        try:
            return self._dsh_options_store().get(
                options_cache_key(provider, model), scope=dsh_options_cache_scope(self)
            )
        except Exception:
            logging.warning("读取 DSH 探测缓存失败", exc_info=True)
            return None

    def set_cached_dsh_options(self, provider, model, options):
        """写入探测缓存；options 为空时不动旧缓存。"""
        try:
            self._dsh_options_store().set(
                options_cache_key(provider, model),
                list(options or []),
                scope=dsh_options_cache_scope(self),
            )
        except Exception:
            logging.warning("写入 DSH 探测缓存失败", exc_info=True)

    def get_searxng_url(self):
        return str(self._get_setting_or_file("searxng_url", "http://localhost:18080")).strip()

    def set_searxng_url(self, url):
        self.settings.setValue("searxng_url", url)

    def get_chat_background_mode(self):
        mode = str(self.settings.value("chat_bg_mode", "stretch") or "").strip().lower()
        return mode if mode in {"fill", "fit", "stretch", "tile", "center"} else "stretch"

    def set_chat_background_mode(self, mode):
        mode = str(mode or "").strip().lower()
        self.settings.setValue(
            "chat_bg_mode",
            mode if mode in {"fill", "fit", "stretch", "tile", "center"} else "stretch",
        )

    def get_chat_background_opacity(self):
        return clamp_opacity(self.settings.value("chat_bg_opacity", 1.0), 1.0)

    def set_chat_background_opacity(self, value):
        self.settings.setValue("chat_bg_opacity", clamp_opacity(value, 1.0))

    def get_ui_theme(self):
        return normalize_ui_theme(self.settings.value("ui_theme", "dark"))

    def set_ui_theme(self, value):
        self.settings.setValue("ui_theme", normalize_ui_theme(value))

    def get_legacy_system_prompt(self):
        prompt = self.settings.value("system_prompt", DEFAULT_SYSTEM_PROMPT)
        return str(prompt) if prompt is not None else DEFAULT_SYSTEM_PROMPT

    def get_custom_system_prompt(self):
        prompt = self.settings.value("custom_system_prompt", "")
        return str(prompt) if prompt is not None else ""

    def get_system_prompt_mode(self):
        """返回 default、custom 或 none，并兼容旧版 QSettings。

        `none` 现在的意思是"**不附加角色卡和用户提示词**"——硬规则（工具/安全规则）
        任何模式都会下发，`none` 也关不掉。
        """
        mode = str(self.settings.value("system_prompt_mode", "") or "").strip().lower()
        if mode in {"default", "custom", "none"}:
            return mode
        if self.settings.contains("custom_system_prompt"):
            # 键存在但是空串 = 用户写过又清空了 = default，不是 none。
            return "custom" if self.get_custom_system_prompt().strip() else "default"
        return "default"

    def has_custom_system_prompt(self):
        return self.get_system_prompt_mode() == "custom"

    def is_system_prompt_disabled(self):
        return self.get_system_prompt_mode() == "none"

    def get_system_prompt(self):
        """**用户自己写的那一段**（不含角色卡，也不含硬规则）。

        以前它在 default 模式下会返回出厂的小柚文本 —— 那是"系统提示词是一整块"
        时代的遗留。现在提示词是分层的：角色卡由角色卡对话框管、硬规则写死在代码里，
        这里只该返回用户手写的那段。
        """
        if self.get_system_prompt_mode() == "custom":
            return self.get_custom_system_prompt()
        return ""

    def set_system_prompt(self, prompt):
        """写入用户手写的那段。清空 = 回到 default（**不是** none）。

        这里以前把空串写成 `none`，也就是"不用系统提示词"。现在 `none` 只剩
        "不附加角色卡和用户提示词"的意思，而硬规则永远在 —— 清空一个附加框，
        不该顺手把角色卡一起关掉。
        """
        prompt = str(prompt or "")
        if prompt.strip():
            self.settings.setValue("custom_system_prompt", prompt)
            self.settings.setValue("system_prompt_mode", "custom")
        else:
            self.settings.remove("custom_system_prompt")
            self.settings.setValue("system_prompt_mode", "default")

    def reset_system_prompt(self):
        """清空**用户自己写的那段**附加提示词。

        不是"恢复内置提示词"—— 角色卡和硬规则本来就在，而且删不掉。
        """
        self.settings.remove("custom_system_prompt")
        self.settings.setValue("system_prompt_mode", "default")

    def get_roleplay_mode(self):
        return self.settings.value("roleplay_mode", False, type=bool)

    def set_roleplay_mode(self, enabled):
        self.settings.setValue("roleplay_mode", bool(enabled))

    def get_roles(self):
        raw = self.settings.value("roles_json", "")
        if raw:
            try:
                roles = json.loads(str(raw)) if isinstance(raw, str) else raw
                if isinstance(roles, dict):
                    return normalize_roles(roles)
            except (TypeError, json.JSONDecodeError):
                logging.warning("读取角色卡失败，将使用默认角色。")
        return copy.deepcopy(DEFAULT_ROLES)

    def set_roles(self, roles):
        self.settings.setValue("roles_json", json.dumps(normalize_roles(roles), ensure_ascii=False))

    # --- TTS 配置 ---
    def get_tts_enabled(self):
        return self.settings.value("tts_enabled", False, type=bool)

    def set_tts_enabled(self, enabled):
        self.settings.setValue("tts_enabled", enabled)

    def get_tts_voice(self):
        return self.settings.value("tts_voice", "zh-CN-XiaoxiaoNeural")

    def set_tts_voice(self, voice):
        self.settings.setValue("tts_voice", voice)
    # --- 气泡  ---
    def get_bubble_opacity(self):
        return self.settings.value("bubble_opacity", 0.85, type=float)

    def set_bubble_opacity(self, value):
        self.settings.setValue("bubble_opacity", value)

    def get_code_opacity(self):
        if self.settings.contains("code_opacity"):
            return clamp_opacity(self.settings.value("code_opacity", 0.85))
        return clamp_opacity(self.get_bubble_opacity())

    def set_code_opacity(self, value):
        self.settings.setValue("code_opacity", clamp_opacity(value))

    # --- 输入框底  ---
    def get_composer_opacity(self):
        return clamp_opacity(self.settings.value("composer_opacity", 0.92))

    def set_composer_opacity(self, value):
        self.settings.setValue("composer_opacity", clamp_opacity(value))

    def get_bubble_user_color(self):
        return normalize_hex_color(
            self.settings.value("bubble_user_color", DEFAULT_BUBBLE_USER_COLOR),
            DEFAULT_BUBBLE_USER_COLOR,
        )

    def set_bubble_user_color(self, value):
        self.settings.setValue(
            "bubble_user_color",
            normalize_hex_color(value, DEFAULT_BUBBLE_USER_COLOR),
        )

    def get_bubble_assistant_color(self):
        return normalize_hex_color(
            self.settings.value("bubble_assistant_color", DEFAULT_BUBBLE_ASSISTANT_COLOR),
            DEFAULT_BUBBLE_ASSISTANT_COLOR,
        )

    def set_bubble_assistant_color(self, value):
        self.settings.setValue(
            "bubble_assistant_color",
            normalize_hex_color(value, DEFAULT_BUBBLE_ASSISTANT_COLOR),
        )

    def get_code_block_color(self):
        return normalize_hex_color(
            self.settings.value("code_block_color", DEFAULT_CODE_BLOCK_COLOR),
            DEFAULT_CODE_BLOCK_COLOR,
        )

    def set_code_block_color(self, value):
        self.settings.setValue(
            "code_block_color",
            normalize_hex_color(value, DEFAULT_CODE_BLOCK_COLOR),
        )

    # --- 全局头像 ---
    @staticmethod
    def _avatar_setting_key(avatar_kind):
        return "avatar_assistant" if avatar_kind == "assistant" else "avatar_user"

    def get_custom_avatar_path(self, avatar_kind):
        value = self.settings.value(self._avatar_setting_key(avatar_kind), "")
        path = resolve_avatar_path(value)
        return path if path and os.path.isfile(path) else ""

    def get_avatar_path(self, avatar_kind):
        custom_path = self.get_custom_avatar_path(avatar_kind)
        return custom_path or get_default_avatar_path(avatar_kind)

    def set_avatar_path(self, avatar_kind, path):
        self.settings.setValue(
            self._avatar_setting_key(avatar_kind),
            serialize_avatar_path(path),
        )

    def clear_avatar_path(self, avatar_kind):
        self.settings.remove(self._avatar_setting_key(avatar_kind))

    # --- 聊天字体 ---
    def get_chat_font_family(self):
        return str(self.settings.value("chat_font_family", "Microsoft YaHei UI") or "").strip()

    def get_chat_font_size(self):
        return clamp_chat_font_size(self.settings.value("chat_font_size", 10), 10)

    def set_chat_font(self, font):
        self.settings.setValue("chat_font_family", font.family())
        self.settings.setValue("chat_font_size", clamp_chat_font_size(font.pointSize(), 10))

    def get_code_font_family(self):
        return str(self.settings.value("code_font_family", "Consolas") or "").strip()

    def get_code_font_size(self):
        return clamp_chat_font_size(self.settings.value("code_font_size", 10), 10)

    def set_code_font(self, font):
        self.settings.setValue("code_font_family", font.family())
        self.settings.setValue("code_font_size", clamp_chat_font_size(font.pointSize(), 10))

    # --- 桌宠 Live2D ---
    def get_pet_model_scale(self):
        return max(0.5, min(1.4, self.settings.value("pet_model_scale", 1.0, type=float)))

    def set_pet_model_scale(self, value):
        self.settings.setValue("pet_model_scale", max(0.5, min(1.4, float(value))))

    def get_pet_model_name(self):
        return str(self.settings.value("pet_model_name", "Mao") or "Mao").strip()

    def set_pet_model_name(self, value):
        self.settings.setValue("pet_model_name", str(value or "Mao").strip())

    def get_pet_worn(self, model_name):
        """读取某模型已保存的持久穿戴（.exp3.json 名称列表）。"""
        key = "pet_worn_%s" % str(model_name or "").strip()
        raw = self.settings.value(key, "", type=str)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [str(item) for item in data] if isinstance(data, list) else []

    def set_pet_worn(self, model_name, names):
        key = "pet_worn_%s" % str(model_name or "").strip()
        self.settings.setValue(key, json.dumps([str(n) for n in (names or [])], ensure_ascii=False))
