"""工作时被点击：不打断手上的活，改成生气 + 气泡提醒。

为什么单独一层：
- 左键点击原本会被转发给页面，页面就播作者的 TapBody 互动动作（自拍/番茄酱/
  开瓶/喷水…）。干活干到一半突然自拍一张显然不对，所以工作期间**不再转发**点击。
- 但也不能「点了没反应」，那会让人以为卡住了 —— 于是给一个固定反馈：
  生气表情 + 气泡「正在工作，请勿打扰！」。
- 手上动作（写字层）完全不受影响：这一层只碰表情和气泡，**不碰活动状态**，
  所以写字层照常写，只是被戳的那一下不理会。

只读、不落盘、不联网。
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QTimer

# 这些状态下点击算「打扰」。和活动层、写字层认定的忙碌集合保持一致。
PET_POKE_BUSY = ("thinking", "working", "waiting")
PET_POKE_REFUSE_TEXT = "正在工作，请勿打扰！"
PET_POKE_REFUSE_MS = 2600      # 生气表情与气泡的停留时长
PET_POKE_MIN_GAP_MS = 400      # 连点节流：这段时间内重复点击只算一次


def is_pet_busy(self):
    """桌宠当前是否在忙（思考 / 工作 / 等待确认）。"""
    return str(getattr(self, "_activity", "idle") or "idle") in PET_POKE_BUSY


def _pet_poke_timer(self):
    """懒创建气泡计时器，省得去改 DesktopPetWindow.__init__。"""
    timer = getattr(self, "_poke_refuse_timer", None)
    if timer is None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._hide_pet_poke_bubble)
        self._poke_refuse_timer = timer
    return timer


def _push_pet_poke_force(self, duration_ms):
    """给情绪层开一个限时窗口：工作期间也放行。

    活动层在工作时独占参数（pet_scripts.py 的状态仲裁），瞬时情绪会被整层挡掉 ——
    所以生气表情必须先打开这个窗口，否则压根不会出现（不是「没生效」，是被让位了）。
    窗口比表情时长多留一点，好让回落缓动也走完，不然到点会硬切回默认脸。
    """
    webview = getattr(self, "webview", None)
    if webview is None:
        return
    script = (
        "window.__petPokeUntil = "
        "(window.performance && performance.now ? performance.now() : Date.now()) + %d;"
        % int(duration_ms)
    )
    webview.page().runJavaScript(script)


def refuse_pet_poke(self):
    """工作时被戳：生气 + 提示气泡。返回是否真的回绝了。

    刻意不动 `_activity`：状态归活动层管，这里插一脚会把「正在工作」提前掐掉。
    """
    if not is_pet_busy(self):
        return False
    now = time.monotonic()
    if now - float(getattr(self, "_poke_refuse_at", 0.0)) < PET_POKE_MIN_GAP_MS / 1000.0:
        return True
    self._poke_refuse_at = now
    # 先开窗口再下发：情绪层在工作期间被让位规则挡着，不开口只会「闪一下」或干脆没有。
    _push_pet_poke_force(self, PET_POKE_REFUSE_MS + 600)
    try:
        self.set_pet_emotion("angry", 1.0, PET_POKE_REFUSE_MS)
    except Exception as exc:      # 表情层出问题也不能让一次点击把界面搞崩
        logging.warning("[pet-poke] 生气表情下发失败：%s", exc)
    try:
        self._show_bubble(PET_POKE_REFUSE_TEXT)
    except Exception as exc:
        logging.warning("[pet-poke] 气泡显示失败：%s", exc)
    _pet_poke_timer(self).start(PET_POKE_REFUSE_MS)
    logging.info(
        "[pet-poke] 工作期间被点击，已回绝（activity=%s）",
        getattr(self, "_activity", None),
    )
    return True


def _hide_pet_poke_bubble(self):
    """只收自己那条气泡，不误伤进度播报 / 说话 / 颜文字气泡。"""
    bubble = getattr(self, "speech_bubble", None)
    if bubble is None:
        return
    try:
        if bubble._message == PET_POKE_REFUSE_TEXT:
            bubble.hide()
    except RuntimeError:
        pass
