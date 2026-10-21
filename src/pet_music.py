"""桌宠音乐律动：WASAPI 会话监听 + 节拍估计 + 自研 sway 层。

为什么不用 sounddevice 录系统输出：
PortAudio 在这台机器上没把 WASAPI 回环设备枚举出来（36 个设备里一个 [Loopback] 都没有），
「录下来算 RMS」那条路走不通。改用 comtypes 直接问 WASAPI 要电平 —— 不录音、不占 CPU、
自动跟随默认输出设备（扬声器 / 蓝牙耳机 / 降噪耳机切换都不用管）。

为什么只认音乐 App：
峰值表是**系统总输出**，她自己的语音（edge-tts）、通知音、视频都会让它动。所以按
会话枚举 → 进程名白名单 → 只取**音乐 App 那一路自己的电平**。另外实测：网易云
「在运行」不等于「在播放」，必须看会话状态（Active），光看进程会误判。

摆动速度跟节拍、不跟音量：
音量不是节拍 —— 慢歌开大音量不该摇得飞快，安静的快歌也不该摇得很慢。所以电平序列
本身被拿来估 BPM，速度跟 BPM 走，幅度才跟音量走。

节拍估计为什么要"稳"和"对齐"（这版新增）：
上一版每 0.5 秒估一次、估到多少就用多少，实测日志里 BPM 每 2.6 秒变一次
（同一首歌在 34~165 之间乱跳）—— 那比"踩不准"更难受。所以这一版：
  1. **倍频折叠**：把估计折进离稳定值最近的倍频（治 34/68/141 乱跳）
  2. **时间投票**：最近 8 秒的估计按强度加权投票取主簇（治"一直在变速"）
  3. **相位锁定**：对选中的周期做 DFT 取起音相位，页面侧把摆动对齐到鼓点上
  4. **半拍摆动**：速度 = BPM/120（两拍晃一次）—— 128BPM 摇 2.1 次/秒太急，
     1.07 次/秒才是"轻轻晃"的舒服区间
  5. **律动曲线**：对称的余弦，极值落在鼓点上 —— 中间过得快、两端停得久，
     左右两侧的幅度和停留时间完全一样（**不能**做成"快出慢回"的非对称曲线：
     那样她会在同一侧待 70% 的时间，看着就是"一直往一边仰着、偶尔弹回来"）

表现层（PET_MUSIC_SCRIPT）用标准 Cubism 参数加算写 ParamAngleZ / ParamBodyAngleZ /
ParamAngleX —— 换任何模型都能用；头发、耳朵、尾巴由物理引擎自己带起来。

优先级（用户定的）：**只有完全空闲才摇** —— 拖拽 / 工作 / 说话 / 点击互动全部让位。
五道闸门都在页面侧判断，Python 侧只推四个数（电平、App、BPM、离下一拍还有多久）。

摇起来的时候会挂一个固定的 🎶 气泡：页面侧才是"真的在摇吗"的权威（五道闸门都在那边），
所以 Python 侧每隔一小会儿问页面一次探针，据此挂/收气泡。
"""

from __future__ import annotations
import json
import logging
import math
import threading
import time
from collections import deque

import comtypes
import psutil
from ctypes import POINTER, c_float, c_int, c_uint, c_void_p, c_wchar_p
from comtypes import COMMETHOD, GUID, HRESULT
from PyQt6.QtCore import QThread, pyqtSignal


PET_MUSIC_ENABLED = True

# 读电平的频率：40Hz。节拍估计要的就是时间分辨率 —— 20Hz 对 120BPM 只有 10 个采样点，
# 鼓点位置会糊；40Hz 有 20 个点，自相关和相位才认得准。
PET_MUSIC_TICK_S = 0.025

# 重扫会话的频率：1Hz。默认输出设备可能被切走（插耳机），会话也可能新起/结束，
# 所以每秒重新枚举一次；两次之间只读缓存的电平表，开销极小。
PET_MUSIC_SCAN_S = 1.0

# 电平变化小于这个值就不打扰页面（静音时完全不发信号）。
PET_MUSIC_EPS = 0.002

# 只认这些播放器进程（进程名小写）。浏览器故意不放：那会把视频也当成音乐。
PET_MUSIC_APPS = frozenset({
    "cloudmusic.exe",     # 网易云音乐
    "qqmusic.exe",        # QQ 音乐
    "spotify.exe",        # Spotify
    "kugou.exe",          # 酷狗
    "kwmusic.exe",        # 酷我
    "foobar2000.exe",     # foobar2000
    "aimp.exe",           # AIMP
    "musicbee.exe",       # MusicBee
})

# 节拍估计：攒 6 秒电平，每 0.5 秒重估一次。搜索 0.30~1.20 秒的滞后
# （40Hz 下是 12~48 个采样点，对应 200~50 BPM）。
PET_BEAT_WINDOW_S = 6.0
PET_BEAT_EVERY_S = 0.5
PET_BEAT_LAG_MIN = 12
PET_BEAT_LAG_MAX = 48
# 自相关峰太弱就不认（环境音乐、纯人声、铺底弦乐都没有明显鼓点）。
# 认不出来时保持上一次的稳定值 —— 这不是兜底，是"没有新信息"。
# 实测真歌的强度大多在 0.15~0.47，所以这里只做粗筛，主要靠投票去噪。
PET_BEAT_MIN_STRENGTH = 0.15

# 稳定化：最近 8 秒的估计一起投票，取加权主簇；同簇 = log2 相差 0.16（约 12%）。
# 换主簇要连续压过旧值 3 次（迟滞），免得在边界上抖。
#
# 为什么是 0.16 而不是更窄：实测日志里同一首歌的估计在**相邻滞后**之间乱窜
# （17/18/20/21/23/24 点 -> 141/133/120/114/104/100 BPM），相邻差值换算成 log2 是
# 0.06~0.15 —— 簇设成 0.06 时它们被切成好几档，"主簇"每隔几秒换一次 = 换节拍 =
# 摆动速度突变 = 肉眼看到的抽搐。簇内是**链式**合并（相邻够近就连上），所以放宽到
# 0.16 正好把这些抖动并成一档，而真正的换歌（差 40% 以上）仍然能识别出来。
PET_BEAT_VOTE_S = 8.0
PET_BEAT_VOTE_MIN = 3
PET_BEAT_CLUSTER = 0.16
PET_BEAT_HYSTERESIS = 3
# 还没有稳定值时，把估计折向这个速度 —— 人耳最舒服的区间，也是"轻轻晃"的甜点。
PET_BEAT_FOLD_TARGET = 120.0

# 摇起来时挂的固定气泡。
PET_MUSIC_BUBBLE_TEXT = "🎶"
PET_MUSIC_BUBBLE_EVERY_S = 0.3


# ---------------------------------------------------------------------------
# WASAPI 的 COM 接口声明
#
# 这些接口没有类型库（在 mmdeviceapi.h / audiopolicy.h 里），comtypes 没法自动生成，
# 只能手写。只声明**用得到的那几个方法**，但顺序必须与 vtable 一致 —— 少声明尾部
# 方法没关系，中间漏一个就会调错槽位。
# ---------------------------------------------------------------------------

class IAudioMeterInformation(comtypes.IUnknown):
    _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "peak")),
    ]


class IMMDevice(comtypes.IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate", (["in"], GUID, "iid"), (["in"], c_uint, "context"),
                  (["in"], c_void_p, "params"),
                  (["out"], POINTER(POINTER(comtypes.IUnknown)), "instance")),
    ]


class IMMDeviceEnumerator(comtypes.IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_uint, "flow"), (["in"], c_uint, "mask"),
                  (["out"], POINTER(POINTER(comtypes.IUnknown)), "collection")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_uint, "flow"), (["in"], c_uint, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "device")),
    ]


class IAudioSessionControl2(comtypes.IUnknown):
    """IAudioSessionControl 的 9 个方法必须全在（GetProcessId 排在它们之后）。"""

    _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_uint), "state")),
        COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_wchar_p), "name")),
        COMMETHOD([], HRESULT, "SetDisplayName", (["in"], c_wchar_p, "name"), (["in"], GUID, "context")),
        COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_wchar_p), "path")),
        COMMETHOD([], HRESULT, "SetIconPath", (["in"], c_wchar_p, "path"), (["in"], GUID, "context")),
        COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "param")),
        COMMETHOD([], HRESULT, "SetGroupingParam", (["in"], GUID, "param"), (["in"], GUID, "context")),
        COMMETHOD([], HRESULT, "RegisterAudioSessionNotification",
                  (["in"], POINTER(comtypes.IUnknown), "client")),
        COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification",
                  (["in"], POINTER(comtypes.IUnknown), "client")),
        COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_wchar_p), "identifier")),
        COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_wchar_p), "identifier")),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(c_uint), "pid")),
    ]


class IAudioSessionEnumerator(comtypes.IUnknown):
    _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "count")),
        COMMETHOD([], HRESULT, "GetSession", (["in"], c_int, "index"),
                  (["out"], POINTER(POINTER(IAudioSessionControl2)), "session")),
    ]


class IAudioSessionManager2(comtypes.IUnknown):
    """GetSessionEnumerator 排在 IAudioSessionManager 那 2 个方法之后。"""

    _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetAudioSessionControl", (["in"], POINTER(GUID), "context"),
                  (["in"], c_uint, "flags"), (["out"], POINTER(POINTER(comtypes.IUnknown)), "control")),
        COMMETHOD([], HRESULT, "GetSimpleAudioVolume", (["in"], POINTER(GUID), "context"),
                  (["in"], c_uint, "flags"), (["out"], POINTER(POINTER(comtypes.IUnknown)), "volume")),
        COMMETHOD([], HRESULT, "GetSessionEnumerator",
                  (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "enumerator")),
    ]


CLSID_MM_DEVICE_ENUMERATOR = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
E_RENDER = 0            # EDataFlow::eRender
E_CONSOLE = 0           # ERole::eConsole
SESSION_ACTIVE = 1      # AudioSessionState::AudioSessionStateActive

# 进程名缓存：会话枚举里同一首歌会反复出现同一个 pid，psutil 查一次就够。
# 每次重扫前清空 —— pid 会被系统回收，长期缓存会串到别的进程上。
_PROCESS_NAMES = {}


def _process_name(pid):
    name = _PROCESS_NAMES.get(pid)
    if name is None:
        try:
            name = (psutil.Process(pid).name() or "").lower()
        except Exception:
            name = ""
        _PROCESS_NAMES[pid] = name
    return name


class _MusicPeek:
    """默认输出设备上「音乐 App 会话」自己的电平（0~1）。"""

    def __init__(self):
        self._enumerator = comtypes.CoCreateInstance(
            CLSID_MM_DEVICE_ENUMERATOR, interface=IMMDeviceEnumerator)
        self._manager = None
        self._meter = None
        self._app = ""
        self._next_scan = 0.0
        self._open_manager()

    def _open_manager(self):
        device = self._enumerator.GetDefaultAudioEndpoint(E_RENDER, E_CONSOLE)
        activated = device.Activate(IAudioSessionManager2._iid_, 0, None)
        self._manager = activated.QueryInterface(IAudioSessionManager2)
        self._meter = None
        self._app = ""

    def _scan(self):
        """重新枚举会话，挑出白名单里正在响的那一路，缓存它的电平表。"""
        self._meter = None
        self._app = ""
        _PROCESS_NAMES.clear()
        try:
            sessions = self._manager.GetSessionEnumerator()
            count = sessions.GetCount()
        except comtypes.COMError:
            self._open_manager()        # 默认设备被切走了，重取
            return
        loudest = 0.0
        for index in range(count):
            try:
                control = sessions.GetSession(index)
                if control.GetState() != SESSION_ACTIVE:
                    continue
                name = _process_name(control.GetProcessId())
                if name not in PET_MUSIC_APPS:
                    continue
                meter = control.QueryInterface(IAudioMeterInformation)
                peak = float(meter.GetPeakValue())
            except comtypes.COMError:
                continue
            if peak >= loudest:
                loudest = peak
                self._meter = meter
                self._app = name

    def read(self):
        """返回 (电平 0~1, App 名)；没有音乐在响时是 (0.0, "")。"""
        now = time.monotonic()
        if now >= self._next_scan:
            self._next_scan = now + PET_MUSIC_SCAN_S
            self._scan()
        if self._meter is None:
            return 0.0, ""
        try:
            return float(self._meter.GetPeakValue()), self._app
        except comtypes.COMError:
            self._meter = None
            return 0.0, ""


class _BeatTracker:
    """从电平序列估 BPM，并且**稳住它、对齐它**。

    为什么是"起音差分"：峰值表是瞬时峰值，鼓点来的时候跳一下、然后慢慢衰减。
    直接对电平做自相关，那串长长的衰减尾巴会把周期糊掉；只留"变大"的那部分
    （max(0, x[i]-x[i-1])），鼓点就变成一串干净的脉冲。
    """

    def __init__(self):
        self._levels = deque(maxlen=int(PET_BEAT_WINDOW_S / PET_MUSIC_TICK_S))
        self._next = 0.0
        self._bpm = 0.0
        self._next_beat_s = 0.0
        self._votes = deque()
        self._pending = 0

    @property
    def bpm(self):
        return self._bpm

    @property
    def next_beat_s(self):
        """离下一拍还有多少秒（页面侧拿它把本地节拍时钟对上）。"""
        return self._next_beat_s

    def push(self, level, now):
        self._levels.append(level)
        if now < self._next or len(self._levels) < PET_BEAT_LAG_MAX * 3:
            return                      # 至少攒够 3 拍，估出来才有意义
        self._next = now + PET_BEAT_EVERY_S
        self._estimate(now)

    def _estimate(self, now):
        raw = list(self._levels)
        onset = [max(0.0, raw[i] - raw[i - 1]) for i in range(1, len(raw))]
        energy = sum(value * value for value in onset)
        if energy <= 1e-9:
            return                      # 这一段完全没起伏，没有新信息
        best_lag, best = 0, 0.0
        for lag in range(PET_BEAT_LAG_MIN, PET_BEAT_LAG_MAX + 1):
            total = 0.0
            for i in range(lag, len(onset)):
                total += onset[i] * onset[i - lag]
            if total > best:
                best, best_lag = total, lag
        if not best_lag:
            return
        strength = best / energy
        if strength < PET_BEAT_MIN_STRENGTH:
            return                      # 认不出鼓点：保持上一次的稳定值
        self._vote(self._fold(60.0 / (best_lag * PET_MUSIC_TICK_S)), strength, now)
        if self._bpm > 0:
            period_samples = 60.0 / self._bpm / PET_MUSIC_TICK_S
            self._next_beat_s = self._grid_offset(onset, period_samples)

    def _fold(self, bpm):
        """折进离稳定值最近的倍频（没有稳定值就折向 120 BPM）。

        自相关很容易锁到半拍或双拍（实测同一首歌在 34/68/141 之间跳）。折叠不会
        丢掉"对齐"：2 倍频只是把网格变细，真实鼓点仍然落在细网格上（见 _grid_offset）。
        """
        target = self._bpm if self._bpm > 0 else PET_BEAT_FOLD_TARGET
        chosen, chosen_error = bpm, abs(math.log2(bpm / target))
        for factor in (0.25, 0.5, 2.0, 4.0):
            candidate = bpm * factor
            error = abs(math.log2(candidate / target))
            if error < chosen_error:
                chosen, chosen_error = candidate, error
        return chosen

    def _vote(self, bpm, strength, now):
        """按强度加权投票取主簇；换主簇要连续压过旧值，免得边界抖动。"""
        self._votes.append((bpm, strength, now))
        while self._votes and now - self._votes[0][2] > PET_BEAT_VOTE_S:
            self._votes.popleft()
        if len(self._votes) < PET_BEAT_VOTE_MIN:
            return
        best_rows, best_weight = [], 0.0
        current = []
        for row in sorted(self._votes, key=lambda item: item[0]):
            if current and math.log2(row[0] / current[-1][0]) > PET_BEAT_CLUSTER:
                current = []
            current.append(row)
            weight = sum(item[1] for item in current)
            if weight > best_weight:
                best_weight, best_rows = weight, list(current)
        total = sum(item[1] for item in best_rows)
        if total <= 0:
            return
        consensus = sum(item[0] * item[1] for item in best_rows) / total
        if self._bpm <= 0:
            logging.info("[pet-music] 节拍锁定: %.1f BPM（%d 票，强度 %.2f）",
                         consensus, len(best_rows), best_weight)
            self._bpm = consensus
            return
        if abs(math.log2(consensus / self._bpm)) < PET_BEAT_CLUSTER:
            self._bpm = consensus        # 同一档，跟着微调
            self._pending = 0
            return
        self._pending += 1
        if self._pending >= PET_BEAT_HYSTERESIS:
            logging.info("[pet-music] 换节拍: %.1f -> %.1f BPM", self._bpm, consensus)
            self._bpm = consensus
            self._pending = 0

    def _grid_offset(self, onset, period_samples):
        """起音脉冲落在周期网格的哪个位置；返回"离下一拍还有多少秒"。"""
        count = len(onset)
        omega = 2.0 * math.pi / period_samples
        real = imaginary = 0.0
        for i, value in enumerate(onset):
            if value == 0.0:
                continue
            real += value * math.cos(omega * i)
            imaginary += value * math.sin(omega * i)
        if real == 0.0 and imaginary == 0.0:
            return self._next_beat_s
        base = (math.atan2(imaginary, real) % (2.0 * math.pi)) / omega
        # 折叠会把网格变细（2 倍频），细网格上的相邻两点一真一反 —— 挑起音更重的那个，
        # 否则有一半概率锁到反拍上（摆动就整体挪半拍）。
        best_position, best_energy = base, -1.0
        for candidate in (base, base + period_samples / 2.0):
            energy = self._grid_energy(onset, candidate, period_samples)
            if energy > best_energy:
                best_energy, best_position = energy, candidate
        elapsed = (count - 1) - best_position
        return (period_samples - (elapsed % period_samples)) * PET_MUSIC_TICK_S

    @staticmethod
    def _grid_energy(onset, position, period_samples):
        """网格点附近 ±1 个采样点里的起音总和（脉冲只有 1~2 个点宽）。"""
        count = len(onset)
        total = 0.0
        cursor = position % period_samples
        while cursor < count:
            index = int(round(cursor))
            for offset in (-1, 0, 1):
                if 0 <= index + offset < count:
                    total += onset[index + offset]
            cursor += period_samples
        return total


class _MusicReader(QThread):
    """COM 读写放子线程：音频驱动万一卡住，冻的是这里，不是界面。

    电平只在「变化够大」「换 App」「节拍变了」时发信号，静音时完全不打扰主线程。
    节拍每 0.5 秒重估一次，所以页面侧最少 2Hz 会拿到一次"下一拍还有多久"。
    """

    level = pyqtSignal(float, str, float, float)      # 电平, App, BPM, 下一拍还有多久

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = threading.Event()
        self._last_level = -1.0
        self._last_app = None
        self._last_bpm = -1.0
        self._last_beat = -1.0

    def stop(self):
        self._stop.set()
        self.wait(1000)

    def run(self):
        comtypes.CoInitialize()
        try:
            peek = _MusicPeek()
            beat = _BeatTracker()
            while not self._stop.is_set():
                level, app = peek.read()
                beat.push(level, time.monotonic())
                if app != self._last_app:
                    logging.info("[pet-music] 音乐会话: %s", app or "（无）")
                    self._last_app = app
                    self._last_level = -1.0
                    self._last_bpm = -1.0
                    self._last_beat = -1.0
                    beat = _BeatTracker()       # 换歌了，重新估节拍
                if (abs(level - self._last_level) >= PET_MUSIC_EPS
                        or beat.bpm != self._last_bpm
                        or beat.next_beat_s != self._last_beat):
                    self._last_level = level
                    self._last_bpm = beat.bpm
                    self._last_beat = beat.next_beat_s
                    self.level.emit(level, app, beat.bpm, beat.next_beat_s)
                self._stop.wait(PET_MUSIC_TICK_S)
        except Exception as exc:
            logging.info("[pet-music] 监听线程退出: %s", exc)
        finally:
            comtypes.CoUninitialize()


PET_MUSIC_SCRIPT = r"""
// 桌宠音乐律动层：只有「完全空闲 + 音乐 App 在响」时才轻轻摇晃。
// 用标准 Cubism 参数加算写入（ParamAngleZ / ParamBodyAngleZ / ParamAngleX），
// 换任何模型都能用；头发、耳朵、尾巴由物理引擎自己带起来。
//
// 幅度跟**音量**走，速度跟**节拍**走，而且**相位锁在鼓点上**：
// Python 侧推出（BPM + 离下一拍还有多久），页面侧据此维护一个本地节拍时钟，
// 摆动的相位直接从时钟推出来 —— 所以不是"自由跑"，是"每一下都踩在鼓点上"。
// 摆动是**半拍**的（两拍晃一次）：128BPM 摇 2.1 次/秒太急，1.07 次/秒才舒服。
//
// 五道闸门（任何一条不满足就让位，包络缓收而不是硬切）：
//   ① 没在拖拽  ② 没在思考/工作/等待  ③ 点击互动没在播  ④ 没在说话  ⑤ 音乐电平够大
(() => {
  if (window.__petMusicReady) { return; }
  window.__petMusicReady = true;

  const DEG_Z = 8;          // 头歪 ±8°
  const BODY_Z = 3;         // 身体 ±3°
  const DEG_X = 3;          // 左右 ±3°
  const ATTACK = 30;        // 起音速度（每秒）：快
  const RELEASE = 4;        // 释放速度（每秒）：慢，停音乐时是「摇完这一下」
  const LEVEL_MIN = 0.02;   // 静音阈值
  const NEUTRAL_HZ = 1.1;   // 还没锁上节拍时（前几秒 / 认不出鼓点）的中性摆动速度
  const PHASE_PULL = 0.25;      // 拿到新估计时把相位往真相推多少（0~1），慢慢收敛不跳
  const PHASE_MAX_STEP = 0.02;  // 单次相位修正的上限（占一个摆动周期的比例）—— 估计再离谱也拽不动
  const PERIOD_SLEW = 0.5;      // 周期每秒最多变多少比例 —— 换拍时速度渐变，不突变

  const IDS = ["ParamAngleZ", "ParamBodyAngleZ", "ParamAngleX"];

  const state = { on: false, level: 0, env: 0, app: "", bpm: 0, swayHz: 0,
                  frac: 0, locked: false, gate: "init", writes: 0, err: null };
  window.__petMusicState = state;

  let level = 0;
  let app = "";
  let bpm = 0;
  let beatPeriod = 0;       // 一拍多少秒（实际值，每帧朝 beatTarget 滑动）
  let beatTarget = 0;       // 估计给出的目标拍长
  let swayFrac = 0;         // 摆动相位（0~1）：自己累加，换拍只会改速度、绝不跳相位
  let phaseReady = false;   // 相位有没有对过节拍
  let lastTempo = 0;        // 上一次收到的 BPM
  let lastBeatArg = 0;      // 上一次收到的"离下一拍还有多久"
  let env = 0;
  let indexModel = null;
  let indexMap = null;

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  function indexes(model) {
    if (indexModel === model && indexMap) { return indexMap; }
    const ids = Array.from(model._model.parameters.ids);
    const map = {};
    for (let i = 0; i < IDS.length; i++) { map[IDS[i]] = ids.indexOf(IDS[i]); }
    indexModel = model;
    indexMap = map;
    return map;
  }

  function add(model, map, id, value) {
    const index = map[id];
    if (index >= 0 && value !== 0) { model.addParameterValueByIndex(index, value); }
  }

  // 律动曲线：对称的余弦 —— frac=0 时正好在 +1（相位锁定保证它落在鼓点上），
  // 中间过得快、两端停得久，左右两侧的幅度和停留时间完全一样。
  //
  // 这里**故意不做"快出慢回"**：那种非对称曲线会让她在同一侧待 70% 的周期，
  // 看起来就是"一直往一边仰着、偶尔弹回来一下"。摆动要左右对称才像晃。
  function groove(frac) {
    return Math.cos(2 * Math.PI * frac);
  }

  // 由 Python 侧推进来：电平、App 名、BPM、离下一拍还有多久（秒）
  //
  // 电平最快每 25ms 推一次，而 BPM / 相位每 0.5 秒才更新一次 —— 所以**只有拿到
  // 新估计时才动节拍时钟**。否则那串高频推送会拿着同一个（已经过时的）"离下一拍
  // 还有多久"，反复把时钟拽到"此刻减去固定偏移"上：相位被钉死，摆动原地卡住，
  // 每 0.5 秒来一个新估计就跳一下 —— 看起来就是抽搐。
  window.__petMusicSet = function (value, name, tempo, nextBeat) {
    level = clamp(Number(value) || 0, 0, 1);
    app = name ? String(name) : "";
    const speed = Number(tempo) || 0;
    const offset = Number(nextBeat) || 0;
    const fresh = (speed !== lastTempo || offset !== lastBeatArg);
    lastTempo = speed;
    lastBeatArg = offset;
    bpm = speed > 0 ? speed : 0;
    if (bpm <= 0 || !isFinite(offset)) {
      beatTarget = 0;
      beatPeriod = 0;
      phaseReady = false;
      return;
    }
    // 只记目标周期，真正的 beatPeriod 由 __petMusicPre 每帧"滑"过去 ——
    // 这样即使估计层换了一档，摆动速度也是渐变而不是"咔"地跳一下。
    beatTarget = 60 / bpm;
    if (!fresh) { return; }             // 电平推送：只更新电平，一个字都不碰相位
    const period = beatTarget;
    // 估计说"上一拍发生在 (period - offset) 秒之前"，换算成摆动相位（一个摆动 = 两拍）
    const target = (period - clamp(offset, 0, period)) / (period * 2);
    if (!phaseReady) {
      beatPeriod = period;              // 第一次锁定：周期和相位都直接对上
      swayFrac = target;
      phaseReady = true;
      return;
    }
    // 相位误差按半圈取模（差半圈等于没差），再轻轻推一点，而且**单次修正有上限** ——
    // 于是哪怕估计层给了个完全错的相位，摆动也不可能被拽出肉眼可见的跳变。
    let error = target - swayFrac;
    if (error > 0.5) { error -= 1; } else if (error < -0.5) { error += 1; }
    if (error > PHASE_MAX_STEP) { error = PHASE_MAX_STEP; }
    else if (error < -PHASE_MAX_STEP) { error = -PHASE_MAX_STEP; }
    swayFrac += error * PHASE_PULL;
  };

  // 活动层自己会说在忙什么（thinking / working / waiting / done），不是 idle 就全部让位
  function busyNow() {
    const st = window.__petActivityState;
    const name = st && st.name;
    return !!name && name !== "idle";
  }

  function gateNow() {
    const phaseName = (window.__petStage && window.__petStage.phase) || "idle";
    if (phaseName !== "idle") { return "drag"; }
    if (busyNow()) { return "activity"; }
    if (window.__petMotionState && window.__petMotionState.playing) { return "tap"; }
    // 说话用说话层自己推的窗口（TTS 起播 → 播完），所以句子之间的空档也算在说话里，
    // 不会从口型的间隙里偷摇。
    if (window.__petSpeaking === true) { return "speech"; }
    if (!(level > LEVEL_MIN)) { return "quiet"; }
    return "";
  }

  window.__petMusicPre = function (model, dt) { try {
    const step = clamp(Number(dt) || 0.016, 0.001, 0.05);
    const gate = gateNow();
    state.gate = gate || "on";
    state.level = level;
    state.app = app;
    state.bpm = bpm;

    const goal = gate ? 0 : level;
    env += (goal - env) * Math.min(1, step * (goal > env ? ATTACK : RELEASE));
    if (env < 0.002) {
      // 安静时一个字都不写：不加算 0 就没有任何副作用
      env = 0;
      state.on = false;
      state.env = 0;
      return;
    }

    // 摆动相位：自己按当前速度累加（半拍 = 两拍晃一次）。
    // 关键：相位是**累加**出来的，不是从"绝对时间 ÷ 周期"算出来的 —— 所以换拍
    // 只会改速度，绝不会让相位跳一下（以前换一次拍就抽一下，根子就在那个除法）。
    let frac;
    let swayHz;
    if (beatPeriod > 0) {
      // 周期滑动：换拍时速度在几百毫秒里渐变过去
      if (beatTarget > 0) {
        const diff = beatTarget - beatPeriod;
        const cap = beatPeriod * PERIOD_SLEW * step;
        beatPeriod += Math.abs(diff) <= cap ? diff : Math.sign(diff) * cap;
      }
      swayHz = 1 / (beatPeriod * 2);
      swayFrac = (swayFrac + step * swayHz) % 1;
      if (swayFrac < 0) { swayFrac += 1; }
      frac = swayFrac;
      state.locked = true;
    } else {
      swayFrac = (swayFrac + step * NEUTRAL_HZ) % 1;
      frac = swayFrac;
      swayHz = NEUTRAL_HZ;
      state.locked = false;
    }
    state.frac = frac;
    state.swayHz = swayHz;

    const swing = groove(frac);
    const map = indexes(model);
    add(model, map, "ParamAngleZ", DEG_Z * env * swing);
    add(model, map, "ParamBodyAngleZ", BODY_Z * env * swing);
    add(model, map, "ParamAngleX", DEG_X * env * Math.sin(2 * Math.PI * frac));
    state.on = true;
    state.env = env;
    state.writes++;
  } catch (err) { state.err = String((err && err.message) ? err.message : err); } };

  window.__petMusicProbe = function () {
    return { on: state.on, level: state.level, env: state.env, app: state.app,
             bpm: state.bpm, swayHz: state.swayHz, frac: state.frac, locked: state.locked,
             gate: state.gate, writes: state.writes, err: state.err };
  };
})();
"""

PET_MUSIC_PROBE_JS = "window.__petMusicProbe ? JSON.stringify(window.__petMusicProbe()) : ''"


def install_pet_music(self):
    """在 DesktopPetWindow.__init__ 里调用，创建监听线程（不启动）。"""
    self._music_reader = _MusicReader(self)
    self._music_reader.level.connect(self._on_pet_music_level)
    self._music_bubble_on = False
    self._music_bubble_at = 0.0


def _install_pet_music(self):
    """页面加载完成后注入摇摆层，并启动监听线程（随页面重载重装）。"""
    if not PET_MUSIC_ENABLED or self.webview is None:
        return
    self.webview.page().runJavaScript(PET_MUSIC_SCRIPT)
    if not self._music_reader.isRunning():
        self._music_reader.start()


def _on_pet_music_level(self, level, app, bpm, next_beat_s):
    if not PET_MUSIC_ENABLED or self.webview is None:
        return
    page = self.webview.page()
    page.runJavaScript(
        "window.__petMusicSet && window.__petMusicSet(%.4f, %s, %.1f, %.3f);"
        % (level, json.dumps(app), bpm, next_beat_s)
    )
    _sync_music_bubble(self, page, level)


def _sync_music_bubble(self, page, level):
    """音乐在响时，隔一小会儿问一次页面「真的在摇吗」，据此挂/收 🎶 气泡。

    页面才是权威：五道闸门都在那边，Python 侧只看得到电平和 App。
    """
    if level <= PET_MUSIC_EPS:
        _set_music_bubble(self, False)      # 音乐停了，气泡跟着收
        return
    now = time.monotonic()
    if now - self._music_bubble_at < PET_MUSIC_BUBBLE_EVERY_S:
        return
    self._music_bubble_at = now
    page.runJavaScript(PET_MUSIC_PROBE_JS, lambda result: _apply_music_probe(self, result))


def _apply_music_probe(self, result):
    """页面探针的回调（跨 JS 边界，拿到的可能是空串或坏 JSON）。"""
    try:
        probe = json.loads(result) if result else {}
    except (TypeError, ValueError):
        return
    _set_music_bubble(self, bool(probe.get("on")))


def _set_music_bubble(self, on):
    """只收自己挂的那条气泡，不误伤说话 / 活动播报（跟活动层的规矩一致）。"""
    if on == self._music_bubble_on:
        return
    self._music_bubble_on = on
    if on:
        self.speech_timer.stop()
        self._show_bubble(PET_MUSIC_BUBBLE_TEXT)
    elif self.speech_bubble._message == PET_MUSIC_BUBBLE_TEXT:
        self.speech_bubble.hide()
