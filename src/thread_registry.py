"""Process-wide registry for background worker threads.

Three problems this solves, all of which previously had ad-hoc handling:

1. **Keep-alive.** A QThread wrapped by sip is aborted by Qt ("Destroyed while
   thread is still running") if Python drops the last reference while the C++
   side still runs — and because that is a C++ abort, ``sys.excepthook`` never
   sees it. Holding a strong reference until the thread finishes prevents it.
2. **Bounded concurrency.** Requests cannot actually be interrupted once the
   socket call is in flight, so repeatedly cancelling a turn used to leave a
   growing pile of live workers. Each group can now be capped.
3. **One place to reap.** Shutdown joins whatever is still tracked, within a
   total time budget, instead of every feature re-implementing it.

The older per-feature keep-alive set (``_LIVE_PROBE_THREADS``) now delegates
here so behaviour is unchanged where it matters.
"""

from __future__ import annotations

import logging
import threading
import time

_lock = threading.RLock()
_keepalive = {}          # id(token) -> (token, thread)
_group_of = {}           # id(token) -> group
_counts = {}             # group -> live count
_caps = {}               # group -> max live count (0/absent = uncapped)


class ConcurrencyLimit(RuntimeError):
    """Raised by spawn() when a group already has its maximum live workers."""


# Group -> default concurrency cap, kept here so the whole policy is visible in
# one place. 0 or absent means uncapped.
#
#   api_request     每个在跑的聊天回合一个；多出来的是取消后仍在飞行中的残留
#   vision_request  视觉请求由工具调用串行触发
#   probe_fetch     设置对话框里的探测抓取
#   live2d_stop     关窗时停本地 HTTP 服务
DEFAULT_CAPS = {
    "probe_fetch": 2,
    "live2d_stop": 2,
    "dsh_reverse": 4,
    "mcp_ready": 8,
    "mcp_stop": 8,
    "memory_index": 1,
    "memory_summary": 2,
}


def set_cap(group, limit):
    """Cap the live workers of ``group``; 0 disables the cap."""
    if limit is None or int(limit) < 0:
        raise ValueError("limit must be >= 0 (0 means uncapped)")
    with _lock:
        _caps[group] = int(limit)
    return int(limit)


def cap_of(group):
    with _lock:
        return _caps.get(group, 0)


def live_count(group):
    with _lock:
        return _counts.get(group, 0)


def at_cap(group):
    limit = cap_of(group)
    return bool(limit) and live_count(group) >= limit


def _acquire(token, thread, group):
    with _lock:
        _keepalive[id(token)] = (token, thread)
        _group_of[id(token)] = group
        _counts[group] = _counts.get(group, 0) + 1


def _release(token, group):
    with _lock:
        _keepalive.pop(id(token), None)
        _group_of.pop(id(token), None)
        if group in _counts:
            _counts[group] = max(0, _counts[group] - 1)


def spawn(target, *, name, group, daemon=True, args=(), kwargs=None, cap=None):
    """Start a tracked worker thread.

    Raises ConcurrencyLimit when the group is already at its cap *before*
    starting anything, so callers fail fast with a clear message instead of
    silently piling up workers.
    """
    if cap is not None:
        set_cap(group, cap)
    elif group not in _caps:
        set_cap(group, DEFAULT_CAPS.get(group, 0))
    if at_cap(group):
        raise ConcurrencyLimit(
            f"{group} 已有 {live_count(group)} 个任务在跑（上限 {cap_of(group)}），已拒绝新任务"
        )

    token = object()

    def _runner():
        try:
            target(*args, **dict(kwargs or {}))
        except BaseException:  # noqa: BLE001 - worker failures must not leak the slot
            logging.warning("后台线程 %s 异常退出", name, exc_info=True)
        finally:
            _release(token, group)

    thread = threading.Thread(target=_runner, name=name, daemon=daemon)
    _acquire(token, thread, group)
    thread.start()
    return thread


def track(thread, *, group):
    """Track an existing thread object (typically a QThread).

    Releases the reference when the thread reports ``finished``. A QThread that
    is already finished releases immediately.
    """
    token = object()
    _acquire(token, thread, group)

    released = threading.Event()

    def _done(*_args):
        if not released.is_set():
            released.set()
            _release(token, group)

    finished = getattr(thread, "finished", None)
    if finished is not None:
        try:
            finished.connect(_done)
        except Exception:  # noqa: BLE001 - not a signal, or already gone
            _done()
        else:
            # A thread that already ran to completion never emits finished again.
            # Use isFinished(), NOT `not isRunning()`: a thread that has not been
            # started yet is also "not running" and must keep its slot.
            is_finished = getattr(thread, "isFinished", None)
            if callable(is_finished):
                try:
                    if is_finished():
                        _done()
                except Exception:  # noqa: BLE001
                    pass
    else:
        _done()
    return thread


def snapshot():
    """Live worker counts per group (groups with zero workers are omitted)."""
    with _lock:
        return {group: count for group, count in sorted(_counts.items()) if count}


def join_all(timeout_ms=2000, per_thread_ms=None):
    """Best-effort reap of every tracked thread inside a total time budget.

    Returns the threads that did not finish in time. Workers are daemons, so
    giving up simply lets the process exit -- it never blocks shutdown forever.
    """
    budget = max(0, int(timeout_ms))
    deadline = time.monotonic() + budget / 1000.0
    with _lock:
        threads = [thread for _token, thread in _keepalive.values()]

    remaining = []
    for thread in threads:
        left_ms = int((deadline - time.monotonic()) * 1000)
        if left_ms <= 0:
            remaining.append(thread)
            continue
        slice_ms = left_ms if per_thread_ms is None else min(left_ms, int(per_thread_ms))
        done = False
        wait = getattr(thread, "wait", None)
        if wait is not None:
            try:
                done = bool(wait(slice_ms))
            except Exception:  # noqa: BLE001
                done = False
        else:
            join = getattr(thread, "join", None)
            if join is not None:
                try:
                    join(slice_ms / 1000.0)
                    done = not thread.is_alive()
                except Exception:  # noqa: BLE001
                    done = False
        if not done:
            remaining.append(thread)
    return remaining
