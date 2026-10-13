"""Embedding backends for the local semantic memory index.

Two providers ship with the app:

* ``ApiEmbeddingProvider`` - real semantic vectors from any OpenAI-compatible
  ``/embeddings`` endpoint. It reuses the chat ``base_url``/``api_key`` so no
  extra credential is required.
* ``HashingEmbeddingProvider`` - dependency-free local fallback built from
  character n-grams. It is lexical rather than semantic, but it keeps the
  memory layer working offline, without a network round trip, and gives tests
  a deterministic provider.

Vectors from different providers are not comparable, so every provider exposes
an ``identity`` string. The index records that identity and rebuilds itself
when it changes.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time

try:  # numpy only accelerates scoring; the module works without it
    import numpy as _np
except Exception:  # pragma: no cover - optional dependency
    _np = None


EMBEDDING_DIMENSIONS = 1024
EMBEDDING_BACKENDS = ("auto", "api", "local", "off")
DEFAULT_API_EMBEDDING_MODEL = "text-embedding-3-small"
MAX_EMBED_CHARS = 4000
API_BATCH_SIZE = 32
# 回退状态下重试首选后端的最小间隔（秒）。
DEGRADED_RETRY_SECONDS = 120

_ASCII_WORD_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(?:`{3,}|~{3,}).*$", re.MULTILINE)
_HSPACE_RE = re.compile(r"[ \t\u3000]+")
_LOCAL_ENDPOINT_RE = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?(?:/|$)",
    re.IGNORECASE,
)


def is_local_endpoint(url):
    """True for loopback endpoints such as the local Ollama server."""
    return bool(_LOCAL_ENDPOINT_RE.match(str(url or "").strip()))


# ---- 本地 embedding 服务的自我拉起 --------------------------------------
#
# 背景：Ollama 反复"消失"并不是崩溃 —— 事件日志里没有任何 crash 记录，
# 它自己的 server.log 也没有 shutdown 记录。真实原因是它被当成临时子进程
# 启动（命令行 / 安装器），父进程一退出它就被回收；而"启动文件夹"只在登录
# 时触发。只要 Aissistant 是长期运行的，就让程序自己负责把它拉起来。
LOCAL_SERVICE_START_COOLDOWN_SECONDS = 60
LOCAL_SERVICE_START_GRACE_SECONDS = 6
_local_service_lock = threading.Lock()
_local_service_last_attempt = 0.0


def find_ollama_executable():
    """定位 ollama.exe：先看 PATH，再看默认安装位置。"""
    found = shutil.which("ollama")
    if found:
        return found
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        candidate = os.path.join(local_appdata, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(candidate):
            return candidate
    return ""


def ensure_local_embedding_service(base_url):
    """如果 embedding 指向本机服务但它没在跑，尝试把它启动起来。

    只对本机地址生效；有冷却时间，避免每次探测失败都反复拉进程。
    返回 True 表示本轮确实启动了服务。
    """
    if not is_local_endpoint(base_url):
        return False
    global _local_service_last_attempt
    with _local_service_lock:
        now = time.monotonic()
        if now - _local_service_last_attempt < LOCAL_SERVICE_START_COOLDOWN_SECONDS:
            return False
        _local_service_last_attempt = now

    executable = find_ollama_executable()
    if not executable:
        logging.info("本地 embedding 服务未运行，但未找到 ollama 可执行文件，跳过自动启动")
        return False
    try:
        creationflags = 0
        if os.name == "nt":
            # 脱离父进程：Aissistant 退出后服务仍然可用，也不会跟着被杀。
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [executable, "serve"],
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        logging.info("检测到本地 embedding 服务未运行，已自动启动：%s", executable)
        return True
    except Exception:
        logging.warning("自动启动本地 embedding 服务失败", exc_info=True)
        return False


class EmbeddingUnavailable(RuntimeError):
    """Raised when a vector is requested before any provider is resolved."""


def normalize_index_text(text, limit=MAX_EMBED_CHARS):
    """Strip markdown noise so the embedded text is mostly real content."""
    value = str(text or "")
    if not value:
        return ""
    value = _FENCE_LINE_RE.sub(" ", value)
    value = _HSPACE_RE.sub(" ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if limit and len(value) > limit:
        value = value[:limit]
    return value


class EmbeddingProvider:
    """Minimal interface every backend implements."""

    name = "base"
    # 该后端的相似度是否可以套用"绝对下限"。
    # 语义模型的无关文本相似度有约 0.4 的地板，所以绝对阈值有意义；
    # 哈希向量的尺度完全不同，只能用相对阈值。
    absolute_floor = True

    def __init__(self, dimensions):
        self.dimensions = int(dimensions)

    @property
    def identity(self):
        return f"{self.name}:{self.dimensions}"

    def embed(self, texts):
        raise NotImplementedError

    def embed_one(self, text):
        vectors = self.embed([text])
        return vectors[0] if vectors else []


class HashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic local vectors from character n-grams and words.

    Chinese text has no spaces, so character bigrams carry most of the signal;
    ASCII gets word tokens. Signed hashing keeps collisions from always adding
    up in the same direction.
    """

    name = "local-hashing"
    # 哈希向量的绝对尺度与语义模型不可比（同义句实测仅 0.152），
    # 因此只使用相对阈值，不套用语义模型的绝对下限。
    absolute_floor = False

    def __init__(self, dimensions=EMBEDDING_DIMENSIONS):
        super().__init__(dimensions)

    @staticmethod
    def _tokens(text):
        lowered = text.lower()
        tokens = []
        for word in _ASCII_WORD_RE.findall(lowered):
            tokens.append(("w:" + word, 1.0))
        for run in _CJK_RUN_RE.findall(lowered):
            chars = list(run)
            for char in chars:
                tokens.append(("c:" + char, 0.8))
            for first, second in zip(chars, chars[1:]):
                tokens.append(("b:" + first + second, 1.6))
            for first, second, third in zip(chars, chars[1:], chars[2:]):
                tokens.append(("t:" + first + second + third, 0.6))
        return tokens

    def _vector(self, text):
        vector = [0.0] * self.dimensions
        for token, weight in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0.0:
            return vector
        return [value / norm for value in vector]

    def embed(self, texts):
        return [self._vector(normalize_index_text(text)) for text in texts]


class ApiEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible ``/embeddings`` client (works with any gateway)."""

    name = "api"

    def __init__(self, base_url, api_key, model, dimensions=EMBEDDING_DIMENSIONS, timeout=30):
        super().__init__(dimensions)
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip() or DEFAULT_API_EMBEDDING_MODEL
        self.timeout = timeout
        self.detected_dimensions = 0

    @property
    def identity(self):
        dim = self.detected_dimensions or self.dimensions
        return f"api:{self.model}:{self.base_url}:{dim}"

    def _endpoint(self):
        if self.base_url.endswith("/embeddings"):
            return self.base_url
        return self.base_url + "/embeddings"

    def headers(self):
        """Auth header only when a key exists: 本地 Ollama 等无鉴权服务不需要它。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, payload):
        try:
            import requests
        except Exception as exc:  # pragma: no cover - requests is a hard dep elsewhere
            raise EmbeddingUnavailable(f"requests 不可用：{exc}") from exc
        response = requests.post(
            self._endpoint(),
            headers=self.headers(),
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            snippet = (response.text or "")[:200]
            hint = ""
            if response.status_code == 404:
                hint = "（该地址没有 /embeddings 接口，请检查 Base URL 是否包含 /v1）"
            elif response.status_code == 401 and not self.api_key:
                hint = "（该服务需要 API Key，请在设置里填写 Embedding API Key）"
            raise EmbeddingUnavailable(f"embedding 接口返回 {response.status_code}：{snippet}{hint}")
        try:
            return response.json()
        except ValueError as exc:
            raise EmbeddingUnavailable("embedding 接口返回了非 JSON 内容") from exc

    def embed(self, texts):
        prepared = [normalize_index_text(text) or " " for text in texts]
        if not prepared:
            return []
        vectors = []
        for start in range(0, len(prepared), API_BATCH_SIZE):
            batch = prepared[start:start + API_BATCH_SIZE]
            document = self._post({"model": self.model, "input": batch})
            data = document.get("data") if isinstance(document, dict) else None
            if not isinstance(data, list) or len(data) != len(batch):
                raise EmbeddingUnavailable("embedding 接口返回的数据条数与请求不一致")
            ordered = sorted(
                data,
                key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0,
            )
            for item in ordered:
                vector = item.get("embedding") if isinstance(item, dict) else None
                if not isinstance(vector, list) or not vector:
                    raise EmbeddingUnavailable("embedding 接口返回了空向量")
                vectors.append([float(value) for value in vector])
        if vectors:
            self.detected_dimensions = len(vectors[0])
            self.dimensions = self.detected_dimensions
        return vectors

    def probe(self):
        """One tiny request so a misconfigured endpoint is discovered up front."""
        self.embed(["probe"])
        return True


class EmbeddingService:
    """Resolves a backend once and shares it between GUI and worker threads."""

    def __init__(self, config=None):
        self._config = config
        self._lock = threading.Lock()
        self._provider = None
        self._error = ""
        self._resolving = False
        self._resolved_signature = ""
        self._degraded = False
        self._last_retry = 0.0

    # ---- config helpers -------------------------------------------------

    def _setting(self, name, default=None):
        getter = getattr(self._config, name, None) if self._config is not None else None
        try:
            return getter() if callable(getter) else default
        except Exception:
            logging.warning("读取 embedding 配置 %s 失败", name, exc_info=True)
            return default

    def backend(self):
        value = str(self._setting("get_memory_embedding_backend", "auto") or "").strip().lower()
        return value if value in EMBEDDING_BACKENDS else "auto"

    def api_key(self):
        """Embedding 专用 Key 优先，没填才回落到聊天 Key（兼容旧配置）。

        本地回环地址（Ollama）不回落到聊天 Key：把云端聊天凭证发给本机服务
        没有意义，还会把 Key 平白暴露给本地进程的抓包/日志。
        """
        key = str(self._setting("get_memory_embedding_api_key", "") or "").strip()
        if key:
            return key
        if is_local_endpoint(self.embedding_base_url()):
            return ""
        return str(self._setting("get_api_key", "") or "").strip()

    def embedding_base_url(self):
        url = str(self._setting("get_memory_embedding_base_url", "") or "").strip()
        if not url:
            url = str(self._setting("get_base_url", "") or "").strip()
        return url

    def embedding_model(self):
        return str(self._setting("get_memory_embedding_model", "") or "").strip()

    def signature(self):
        return "|".join([
            self.backend(),
            str(self._setting("get_memory_embedding_base_url", "") or ""),
            str(self._setting("get_base_url", "") or ""),
            str(self._setting("get_memory_embedding_model", "") or ""),
            "1" if self._setting("get_memory_embedding_api_key", "") else "0",
            "1" if self._setting("get_api_key", "") else "0",
        ])

    # ---- provider lifecycle --------------------------------------------

    @property
    def identity(self):
        with self._lock:
            return self._provider.identity if self._provider is not None else ""

    @property
    def last_error(self):
        with self._lock:
            return self._error

    def provider(self):
        """Return the resolved provider, or None. Never blocks on the network."""
        with self._lock:
            return self._provider

    def _build_candidates(self):
        backend = self.backend()
        if backend == "off":
            return []
        local = HashingEmbeddingProvider()
        if backend == "local":
            return [local]
        base_url = self.embedding_base_url()
        model = self.embedding_model()
        # 只要填了 base_url 就构造 API 候选：本地 Ollama 这类无鉴权服务
        # 不带 Key 也必须能用；云端缺 Key 会在 probe 时被拒。
        api = ApiEmbeddingProvider(base_url, self.api_key(), model) if base_url else None
        if backend == "api":
            # 用户显式选「仅用 API」时不回退本地哈希：否则 API 一挂就悄悄降级成
            # 字面匹配，用户以为在用语义检索，实际不是。
            return [api] if api is not None else []
        return [api, local] if api is not None else [local]

    def probe_api(self):
        """Probe only the configured API endpoint, for the settings dialog.

        Unlike resolve() this reports the API result even when a local fallback
        would have masked it, so a wrong Base URL is visible to the user.
        """
        backend = self.backend()
        if backend == "off":
            return {"ok": False, "kind": "disabled", "identity": "", "message": "Embedding 已关闭（off）。"}
        if backend == "local":
            return {"ok": False, "kind": "disabled", "identity": "", "message": "当前后端为「仅本地哈希」，不会调用 API。"}
        base_url = self.embedding_base_url()
        if not base_url:
            return {"ok": False, "kind": "api", "identity": "", "message": "未填写 Embedding Base URL。"}
        provider = ApiEmbeddingProvider(base_url, self.api_key(), self.embedding_model(), timeout=20)
        try:
            provider.probe()
        except Exception as exc:  # noqa: BLE001 - the message goes straight to the UI
            return {"ok": False, "kind": "api", "identity": "", "message": f"API 探测失败：{exc}"}
        return {
            "ok": True,
            "kind": "api",
            "identity": provider.identity,
            "message": f"API 可用：{provider.model}（{provider.dimensions} 维）",
        }

    def resolve(self, force=False):
        """Pick a backend, probing API reachability once. Safe to call anywhere."""
        signature = self.signature()
        with self._lock:
            if not force and self._provider is not None:
                return self._provider
            if not force and self._resolved_signature == signature and self._provider is None and self._error:
                return None
            self._resolving = True
        provider = None
        error = ""
        degraded = False
        try:
            for index, candidate in enumerate(self._build_candidates()):
                if candidate is None:
                    continue
                try:
                    if isinstance(candidate, ApiEmbeddingProvider):
                        candidate.probe()
                    provider = candidate
                    # 第一个候选就是首选后端；取到后面的候选说明发生了回退。
                    degraded = index > 0
                    break
                except Exception as exc:  # noqa: BLE001 - try the next candidate
                    error = str(exc)
                    logging.warning("embedding 后端 %s 不可用，尝试下一个：%s", candidate.name, exc)
                    if isinstance(candidate, ApiEmbeddingProvider):
                        # 指向本机服务却没连上：大概率是 Ollama 没在跑，顺手拉起来。
                        if ensure_local_embedding_service(candidate.base_url):
                            self._schedule_reresolve()
        finally:
            with self._lock:
                self._provider = provider
                self._degraded = degraded
                self._error = "" if provider is not None else (error or "没有可用的 embedding 后端")
                self._resolved_signature = signature
                self._resolving = False
        if provider is None:
            logging.warning("本地记忆索引已禁用：%s", self._error)
        elif degraded:
            logging.warning(
                "embedding 后端已回退到 %s（首选后端不可用）；将保留已有索引，不写入降级向量",
                provider.identity,
            )
        else:
            logging.info("本地记忆索引 embedding 后端：%s", provider.identity)
        return provider

    @property
    def degraded(self):
        """当前 provider 是否为回退结果，而不是首选后端。

        索引层据此判断"该不该清空重建"：把降级向量写进按真实模型建立的索引，
        会毁掉整库数据。
        """
        with self._lock:
            return self._degraded

    def _schedule_reresolve(self, delay=LOCAL_SERVICE_START_GRACE_SECONDS):
        """刚拉起本地服务后，隔几秒重新探测一次，不必等下一个重试周期。"""
        def _later():
            time.sleep(delay)
            self.resolve(force=True)

        threading.Thread(target=_later, name="memory-embedding-recheck", daemon=True).start()

    def ensure_async(self):
        """Kick off resolution in the background; never blocks the caller."""
        with self._lock:
            if self._provider is not None or self._resolving:
                return
        threading.Thread(target=self.resolve, name="memory-embedding-resolve", daemon=True).start()

    def retry_if_degraded(self, interval=DEGRADED_RETRY_SECONDS):
        """回退状态下定期重试首选后端。

        没有这个的话，Ollama 挂掉再恢复后，进程内缓存的降级 provider 不会失效，
        用户必须重启整个程序才能恢复语义检索。
        """
        with self._lock:
            if not self._degraded or self._resolving:
                return False
            now = time.monotonic()
            if now - self._last_retry < interval:
                return False
            self._last_retry = now
        threading.Thread(
            target=self.resolve,
            kwargs={"force": True},
            name="memory-embedding-retry",
            daemon=True,
        ).start()
        return True

    def embed(self, texts):
        provider = self.provider()
        if provider is None:
            provider = self.resolve()
        if provider is None:
            raise EmbeddingUnavailable(self._error or "embedding 后端不可用")
        return provider.embed(texts)

    def embed_one(self, text):
        vectors = self.embed([text])
        return vectors[0] if vectors else []


_SERVICE_LOCK = threading.Lock()
_SERVICE = None


class StaticEmbeddingConfig:
    """Fixed-value config snapshot.

    The settings dialog must not hand widget-reading getters to a worker thread
    (Qt widgets belong to the GUI thread), so it snapshots the fields into this
    object and passes that instead.
    """

    def __init__(self, backend="auto", base_url="", model="", api_key=""):
        self.backend = str(backend or "auto").strip().lower()
        self.base_url = str(base_url or "").strip()
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "").strip()

    def get_memory_embedding_backend(self):
        return self.backend

    def get_memory_embedding_base_url(self):
        return self.base_url

    def get_memory_embedding_model(self):
        return self.model

    def get_memory_embedding_api_key(self):
        return self.api_key

    # 探针只关心 embedding 这一侧：聊天配置与 embedding 同源，直接回填。
    def get_base_url(self):
        return self.base_url

    def get_api_key(self):
        return self.api_key


def get_embedding_service(config=None):
    """Process-wide service so GUI and worker threads share one provider."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = EmbeddingService(config)
        elif config is not None and _SERVICE._config is None:
            _SERVICE._config = config
        return _SERVICE


def reset_embedding_service():
    """Testing hook: drop the cached provider."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None
