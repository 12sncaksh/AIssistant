"""Thread-local ``requests.Session`` factory.

Why: the chat path, tool calls (weather / search / Tavily / 高德) and the vision
path all fire repeated HTTPS requests at a handful of hosts. Calling the
module-level ``requests.get/post`` each time pays a fresh TCP + TLS handshake on
every request, because no connection is ever reused.

Why thread-local: ``requests.Session`` is *not* thread-safe. Each thread gets its
own Session via ``threading.local()``, so connection pooling is per worker thread
and nothing is shared across the Qt threads.
"""

from __future__ import annotations

import threading

import requests

_local = threading.local()

# 连接池按线程给：一个聊天线程 + 若干工具/视觉线程，各自几路并发足够。
POOL_CONNECTIONS = 4
POOL_MAXSIZE = 8


def session() -> requests.Session:
    """Return this thread's Session, creating it on first use."""
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=POOL_CONNECTIONS,
            pool_maxsize=POOL_MAXSIZE,
            # 不自动重试：重发一次聊天请求会重复计费，重发一次工具调用可能重复执行副作用。
            # 需要重试的地方（如 reasoning effort 降档）都在上层显式做。
            max_retries=0,
        )
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _local.session = sess
    return sess


def close_current_thread_session() -> None:
    """Close and drop this thread's Session; safe to call repeatedly.

    Call this before a worker thread exits so its sockets are released promptly
    instead of waiting for the process to reap them.
    """
    sess = getattr(_local, "session", None)
    if sess is None:
        return
    _local.session = None
    try:
        sess.close()
    except Exception:  # noqa: BLE001 - cleanup must never raise
        pass
