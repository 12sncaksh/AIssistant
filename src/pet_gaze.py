"""桌宠注视：全屏轮询 + 自研 gaze 层。

为什么不用官方那条注视通道：
官方 `CubismLook` 把「光标位置」变成 ParamAngleX/Y/Z、ParamBodyAngleX、
ParamEyeBallX/Y 的**加算**增量（系数 30/30/-30/10/1/1），但它的输入是拖拽管理器里
那个「视角空间 ±1」的值 —— 归一化的原点、增益、裁剪全得由我们这边猜。
桌宠窗口只占屏幕一角，猜出来的结果就是「盯着看却对不上」。

所以这一层自己算：
  1. Python 侧 `_tick_pet_gaze` 以**桌宠窗口中心**为原点，取光标在屏幕上的真实像素
     偏移，用 atan2 反解方向、按距离做柔和衰减，得到 ±1 的归一化向量；
  2. 页面侧 `PET_GAZE_SCRIPT` 用与官方相同的系数把它加到那 6 个参数上。

官方那一路不再喂位置（左键拖动不再转发 pointermove），它的目标停在构造时的
(0, 0)，贡献恒为 0，不会和自研层叠加。
"""

from __future__ import annotations
import math
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCursor


# 轮询间隔：30Hz。眼睛本来就是缓动跟随，33ms 足够顺，且最多每秒 30 次 runJavaScript。
PET_GAZE_TICK_MS = 33

# 距离衰减尺度（像素）：光标离她这么远时，偏转达到一半。
# 这是唯一的灵敏度旋钮 —— 调小更敏感、调大更沉稳。
PET_GAZE_REACH_PX = 260.0

PET_GAZE_SCRIPT = r"""
// 桌宠注视层：把 ±1 的归一化方向写成头部/眼球的偏转。
// 系数与官方 CubismLookUpdater 保持一致（AngleX/Y ±30、AngleZ 是 x*y 交叉项、
// BodyAngleX ±10、眼球 ±1），差别只在映射由我们自己掌握。
// 写入用 addParameterValueByIndex（加算），跟官方一样，所以不会吃掉待机动作、
// 物理、以及活动层（思考时眼神上飘）已经写进去的贡献。
(() => {
  if (window.__petGazeReady) { return; }
  window.__petGazeReady = true;

  const IDS = ["ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamBodyAngleX",
               "ParamEyeBallX", "ParamEyeBallY"];
  const DEG_X = 30;      // ParamAngleX 满偏角度
  const DEG_Y = 30;
  const DEG_Z = -30;     // 斜向时的轻微歪头，官方就是 x*y 的交叉项
  const BODY_X = 10;
  const RATE = 10;       // 缓动速度：越大越跟手

  let indexModel = null;
  let indexMap = null;
  let targetX = 0;
  let targetY = 0;
  let curX = 0;
  let curY = 0;

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

  // 活动层在忙（思考 / 执行命令 / 等待）时注视让位 —— 那是她自己的节目，
  // 不该一边干活一边瞟鼠标。注意只是「不跟鼠标」而不是「停止输出」：
  // headExtra（摇头晃脑）照常写，头回正前方本来也是干活时该有的样子。
  const BUSY = { thinking: true, working: true, waiting: true };
  function busyNow() {
    const st = window.__petActivityState;
    const name = st && st.name;
    return !!(name && BUSY[name]);
  }

  // 由 Python 侧推进来：x 右为正、y 下为正，各自 ±1
  window.__petGazeSet = function (x, y) {
    targetX = clamp(Number(x) || 0, -1, 1);
    targetY = clamp(Number(y) || 0, -1, 1);
  };

  window.__petGazePre = function (model, dt) { try {
    const phase = (window.__petStage && window.__petStage.phase) || "idle";
    if (phase !== "idle") { return; }     // 拖拽演出期间整体让位，别跟它抢 ParamAngleZ
    // 忙的时候缓动目标归零（回正前方），但推进来的目标值照旧存着，
    // 所以一旦回到 idle，视线会立刻缓动回光标那边。
    const busy = busyNow();
    const goalX = busy ? 0 : targetX;
    const goalY = busy ? 0 : targetY;
    const step = clamp(Number(dt) || 0.016, 0.001, 0.05);
    curX += (goalX - curX) * Math.min(1, step * RATE);
    curY += (goalY - curY) * Math.min(1, step * RATE);

    // 其他层往这里塞「额外的头部偏移」，单位是度（活动层的摇头晃脑就走这条）。
    const extra = window.__petHeadExtra || null;
    const ex = extra ? (Number(extra.x) || 0) : 0;
    const ey = extra ? (Number(extra.y) || 0) : 0;
    const ez = extra ? (Number(extra.z) || 0) : 0;

    const map = indexes(model);
    add(model, map, "ParamAngleX", DEG_X * curX + ex);
    add(model, map, "ParamAngleY", DEG_Y * curY + ey);
    add(model, map, "ParamAngleZ", DEG_Z * curX * curY + ez);
    add(model, map, "ParamBodyAngleX", BODY_X * curX);
    add(model, map, "ParamEyeBallX", curX);
    add(model, map, "ParamEyeBallY", curY);
  } catch (err) { window.__petGazeErr = String((err && err.message) ? err.message : err); } };

  window.__petGazeProbe = function () {
    return {
      ready: true,
      target: [targetX, targetY],
      eased: [curX, curY],
      extra: window.__petHeadExtra || null,
      err: window.__petGazeErr || null
    };
  };
})();
"""


def install_pet_gaze(self):
    """在 DesktopPetWindow.__init__ 里调用，创建定时器（不启动）。"""
    self._gaze_timer = QTimer(self)
    self._gaze_timer.setInterval(PET_GAZE_TICK_MS)
    self._gaze_timer.timeout.connect(self._tick_pet_gaze)
    self._last_gaze_pos = None


def _install_pet_gaze(self):
    """页面加载完成后注入注视层（随页面重载重装，和另外三层脚本一样）。"""
    if self.webview is None:
        return
    self.webview.page().runJavaScript(PET_GAZE_SCRIPT)


def _push_pet_gaze(self, x, y):
    if self.webview is None:
        return
    self.webview.page().runJavaScript(
        "window.__petGazeSet && window.__petGazeSet(%.5f, %.5f);" % (x, y)
    )


def _tick_pet_gaze(self):
    if self.webview is None or not self.isVisible():
        return
    if self._drag_active:
        return          # 右键拖窗口时由拖拽演出驱动，轮询别插一脚

    pos = QCursor.pos()
    if pos == self._last_gaze_pos:
        return          # 光标没动就不发，省下 runJavaScript
    self._last_gaze_pos = pos

    # 原点必须是桌宠自己的中心，而不是屏幕中心：桌宠默认摆在右下角，
    # 拿屏幕中心当原点等于把「屏幕正中」当成她的正前方，光标贴着她也会看别处。
    center = self.frameGeometry().center()
    dx = float(pos.x() - center.x())
    dy = float(pos.y() - center.y())
    dist = math.hypot(dx, dy)
    if dist < 1e-3:
        return

    # 距离衰减：越远越偏向，但永远不硬裁。线性 + 裁剪会在屏幕外侧留下一整片
    # 「她不再分辨方向」的死区，那正是之前感觉盯不准的原因之一。
    mag = dist / (dist + PET_GAZE_REACH_PX)
    # Y 必须取反：屏幕/窗口坐标向下为正，而 ParamAngleY / ParamEyeBallY 是
    # 「向上为正」。官方那条路是在 transformViewY 的矩阵里带负号换过去的，
    # 我们绕开了它，就得自己翻这一下，否则左右对、上下反。
    self._push_pet_gaze(dx / dist * mag, -dy / dist * mag)
