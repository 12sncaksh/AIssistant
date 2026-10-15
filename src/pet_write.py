"""桌宠「伏案写字」：活动状态进入 working / waiting 时挂上画笔、手在板子上一笔一笔写。

这一层用的参数全是本鱼从模型资源里挖出来的，不是猜的：

    bi        画笔（`画笔.exp3.json` 里只有这一条：bi = 1，Add）
    pi        橡皮（暂时没用）
    point     点菜板本体（开关型，ParamGroup29 里那一排「道具开关」）
    pointX    \
    pointY     | 点菜手 X / Y / Z —— 在 `c_0120.cdi3.json` 的 CombinedParameters 里
    pointY2   /  `["pointX","pointY"]` 是**成对**登记的，所以它们是一组二维坐标
                （手在板子上的位置），而不是 0/1 开关
    pointZ    点菜手按下（`点菜按下.exp3.json` 里是 pointZ = 1，Add，所以是「按压力度」）

范围不硬编码：`c_0120.cdi3.json` 里这些参数**没有 Min/Max**（真实范围只在编译后的
`.moc3` 里），所以这一层在运行时向模型自己问 getParameterMinimumValue /
getParameterMaximumValue / getParameterDefaultValue，并且**以默认值为锚点**摆动 ——
这样无论范围是 0~1 还是 -30~30，手都不会飞出去。

写入方式跟着作者的设定分两种：
    * 位置参数（pointX / pointY / pointY2）用 set，值 = 默认值 + 幅度×权重，裁进真实范围；
    * 开关（point / bi）和按下（pointZ）用 add —— 作者的表情文件就是 Add 语义。

时间线：进入 working/waiting → 权重约 0.3 秒升到 1（笔和板子淡入、手开始写）；
离开 → 约 0.3 秒退回 0（手回原位、笔和板子淡出）。拖拽演出期间整体让位。

所有旋钮都在 PET_WRITE_SCRIPT 顶部（SPEED / RATE / SHOW_BOARD / SHOW_BRUSH /
HEAD_DOWN / BOB / PRESS / WRITE_T / REST_T / STROKE_N / ROW_N / ROW_M / SWEEP /
HOLD / SPAN），觉得哪儿不对只改那几个数就行。

节奏调过两版：
  第一版 3.1 rad/s + 幅度 0.34/0.24 + 满量程下压 = 「用笔点本子」；
  第二版 11.5 rad/s + 0.16/0.07 + 轻压常在 = 够快，但一直在写、而且频率是
    无理数叠加（sin 3.1x + sin 6.7x）永远不重复，看着像乱抖；
  现在 = 有格子有节拍的「写一个字 → 原地停一下 → 挪到下一格」。
"""

from __future__ import annotations
import logging
from PyQt6.QtCore import QTimer


PET_WRITE_SCRIPT = r"""
// 桌宠写字层：working / waiting 时把「画笔」挂上，让「点菜手」在板子上一笔一笔写。
// 参数来源、为什么用 set + add 两种写法，见 pet_write.py 顶部的说明。
(() => {
  if (window.__petWriteReady) { return; }
  window.__petWriteReady = true;

  // ---- 旋钮：只改这一块 ----
  const SPEED = 1.0;         // 写字速度倍率（越大越快）
  const RATE = 3.0;          // 权重进出速度（越大越干脆）
  const SHOW_BOARD = true;   // 摆出「点菜板」。⚠️ 实测 point 的默认值就是 1(=max)，
                             // 加 1 会被引擎裁回 1，所以这条目前是**空操作**，见下面的注释。
  const SHOW_BRUSH = true;   // 是否把「画笔」挂到手上
  const HEAD_DOWN = 9;       // 干活时低头角度（度，越大越低）
  const BOB = 1.0;           // 写字时轻轻点头的幅度（度），不想要就设 0
  // 按下量（pointZ 的 Add 量）。实测 pointZ 是 [0, 1, 默认 0] 的**按下开关**，
  // 所以这里用满量程：写字时按到底，停笔时完全松开。
  // 第一版把它当成「力度」，做成每笔 0→1→0 地戳 —— 那是把开关当成了力度，
  // 看着就成了拿笔点本子，而不是写字。
  const PRESS = 1.0;
  const PRESS_IN = 0.26;     // 按下去用多久（秒）。满量程开关行程大，起得太快像砸笔。
  const PRESS_OUT = 0.40;    // 松开用多久（秒），从「写完那一刻」算起，一直延续到停顿里。

  // ---- 写字节奏：写一个字 → 原地真停一下 → 挪到下一格 ----
  // 全部由时间推导，没有任何随机数、也没有无理数频率叠加 —— 所以是「规律」的。
  const WRITE_T = 0.82;      // 写一个字用多久（秒）
  const REST_T = 0.78;       // 写完停多久（秒）
  const STROKE_N = 3;        // 一个「字」写几笔
  const ROW_N = 7;           // 一行写几个字，写满换行
  const ROW_M = 3;           // 往下写几行，写满回到第一行
  const SWEEP = 0.70;        // 一笔里 70% 在划、30% 在回位（回位时抬笔）。
                             // 回位太快（原来 78/22）会让每笔结尾像弹一下，看着就是抖。
  const HOLD = 0.5;          // 停顿的前 50% 原地不动，后 50% 才挪向下一格

  // 摆动幅度：写成「范围的比例」而不是绝对值，这样跟真实范围无关。
  // 手部坐标范围未知是 0~1 还是 -30~30，比例写法两种都不会飞出去。
  // 第一版 0.34 / 0.24 太大，一笔横跨半块板子 —— 现在收成「一个小格」的范围。
  const SPAN = { pointX: 0.16, pointY: 0.07, pointY2: 0.03 };

  const POS_IDS = ["pointX", "pointY", "pointY2"];
  const ADD_IDS = ["point", "bi", "pointZ"];
  const ALL_IDS = POS_IDS.concat(ADD_IDS);
  // 和活动层的「忙」保持一致（pet_scripts.py 里 working 也含 waiting）
  const BUSY = { working: true, waiting: true };

  let indexModel = null;
  let info = null;
  let usable = false;        // 模型上一个相关参数都没有 → 整层失效，一个字节都不写
  let weight = 0;
  let force = null;          // 调试用：window.__petWriteForce(1) 强制进入写字状态

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function smoothstep(a, b, x) {
    const t = clamp((x - a) / (b - a), 0, 1);
    return t * t * (3 - 2 * t);
  }

  // 页面里拿到的是 LAppModel 包装，真正的 CubismModel 挂在 ._model 上。
  // 取值优先问 ._model，包装上也试一下，都没有就交给兜底值。
  function ask(model, name, index) {
    const inner = model ? model._model : null;
    let fn = inner ? inner[name] : null;
    if (typeof fn === "function") { return Number(fn.call(inner, index)); }
    fn = model ? model[name] : null;
    if (typeof fn === "function") { return Number(fn.call(model, index)); }
    return NaN;
  }

  function build(model) {
    if (indexModel === model && info) { return; }
    const ids = Array.from(model._model.parameters.ids);
    const tab = {};
    for (let k = 0; k < ALL_IDS.length; k++) {
      const id = ALL_IDS[k];
      const i = ids.indexOf(id);
      if (i < 0) { continue; }          // 换模型没有这些参数时整层自动失效
      let min = ask(model, "getParameterMinimumValue", i);
      let max = ask(model, "getParameterMaximumValue", i);
      let def = ask(model, "getParameterDefaultValue", i);
      if (!isFinite(min) || !isFinite(max) || max <= min) { min = 0; max = 1; }
      if (!isFinite(def)) { def = clamp(0, min, max); }
      tab[id] = { index: i, min: min, max: max, def: def };
    }
    indexModel = model;
    info = tab;
    usable = Object.keys(tab).length > 0;
  }

  function add(model, id, value) {
    const t = info[id];
    if (t && value !== 0) { model.addParameterValueByIndex(t.index, value); }
  }

  function setPos(model, id, norm) {
    const t = info[id];
    if (!t) { return; }
    const amp = (t.max - t.min) * SPAN[id];
    model.setParameterValueByIndex(t.index, clamp(t.def + amp * norm, t.min, t.max));
  }

  window.__petWriteForce = function (v) {
    force = (v === null || v === undefined) ? null : clamp(Number(v) || 0, 0, 1);
  };

  window.__petWritePre = function (model, dt) { try {
    build(model);
    if (!usable) { return; }

    // 拖拽演出期间让位：她正被拎着甩，不该同时在写字
    const phase = (window.__petStage && window.__petStage.phase) || "idle";
    if (phase !== "idle") { return; }

    const st = window.__petActivityState;
    const name = (st && st.name) || "";
    const want = (force !== null) ? force : (BUSY[name] ? 1 : 0);
    const step = clamp(Number(dt) || 0.016, 0.001, 0.05);
    weight += (want - weight) * Math.min(1, step * RATE);
    if (weight < 0.002) { weight = 0; }
    if (weight <= 0) { return; }        // 闲着的时候一个参数都不碰

    const nowMs = (typeof performance !== "undefined" && performance.now)
      ? performance.now() : Date.now();
    const t = (nowMs / 1000) * SPEED;

    // ---- 节奏：写一个字 → 真停一下 → 挪到下一个字。全部由时间推导 ----
    const CYCLE = WRITE_T + REST_T;
    const cyc = t / CYCLE;
    const idx = Math.floor(cyc);
    const local = (cyc - idx) * CYCLE;           // 本周期内的进度（秒）
    const writing = local < WRITE_T;
    const u = writing ? (local / WRITE_T) : 1;   // 字内进度 0~1

    // 字内：STROKE_N 笔，每笔从左划到右，再快速回位（回位时抬笔）。
    // 用「第几笔 + 这一笔的进度」这种整数节拍，而不是几个正弦叠加 ——
    // 叠正弦会永远不重复，看着就是乱抖。
    const sPos = u * STROKE_N;
    const kIdx = Math.min(STROKE_N - 1, Math.floor(sPos));
    const f = Math.min(1, sPos - kIdx);
    // 最后一笔不回位：收笔就停在右边，接着才是「停一下」——手不用白跑一趟，
    // 也正好让「停笔位置」和「收笔位置」重合，交界处不会有瞬跳。
    const along = (f < SWEEP) ? (f / SWEEP)
      : ((kIdx === STROKE_N - 1) ? 1 : (1 - (f - SWEEP) / (1 - SWEEP)));
    const sxStroke = along * 2 - 1;                                    // -1 → 1（末笔停在 1）
    const syStroke = (STROKE_N > 1) ? (kIdx / (STROKE_N - 1)) * 2 - 1 : 0;

    // 字与字：往右一格一格走，一行写满就换行 —— 规整的格子，不是随机漂。
    const col = ((idx % ROW_N) + ROW_N) % ROW_N;
    const row = ((Math.floor(idx / ROW_N) % ROW_M) + ROW_M) % ROW_M;
    const col2 = (col + 1) % ROW_N;
    const row2 = (col2 === 0) ? ((row + 1) % ROW_M) : row;
    const cellX = (c) => (c / Math.max(1, ROW_N - 1)) * 2 - 1;
    const cellY = (r) => (r / Math.max(1, ROW_M - 1)) * 2 - 1;

    // 停笔：前 HOLD 段原地真停，后段平移到下一个字的起笔位置。
    // 两头都接得上（收笔位置 ↔ 下一字起笔位置），所以不会跳变。
    const glide = writing ? 0 : smoothstep(HOLD, 1, (local - WRITE_T) / REST_T);
    const restX = (cellX(col) * 0.62 + 0.30) * (1 - glide)
                + (cellX(col2) * 0.62 - 0.30) * glide;
    const restY = (cellY(row) * 0.55 + syStroke * 0.32) * (1 - glide)
                + (cellY(row2) * 0.55 - 0.32) * glide;

    const sx = writing ? (cellX(col) * 0.62 + sxStroke * 0.30) : restX;
    const sy = writing ? (cellY(row) * 0.55 + syStroke * 0.32) : restY;

    // 写字包络：只管「手在动」这件事（pointY2 的扭腕也用它）。
    const writeEnv = writing
      ? smoothstep(0, 0.10, local) * (1 - smoothstep(WRITE_T - 0.12, WRITE_T, local))
      : 0;
    // 按下（pointZ）：写字这一整段按着，停笔松开 —— 一次停顿一次开合，不是每笔戳一下。
    // 行程是满量程的，所以起落都放慢：按下 0.26 秒、松开 0.40 秒，而且松开一直延续到
    // 停顿里 —— 看起来才像「写完抬笔」，而不是「啪一下弹起来」。
    const pressEnv = smoothstep(0, PRESS_IN, local)
      * (1 - smoothstep(WRITE_T, WRITE_T + PRESS_OUT, local));
    const penDown = pressEnv;

    setPos(model, "pointX", sx * weight);
    setPos(model, "pointY", sy * weight);
    setPos(model, "pointY2", sxStroke * 0.5 * writeEnv * weight);

    // ⚠️ 实测（日志里的真范围）：point = [0, 1, 默认 1]，也就是说点菜板的默认状态
    // 本来就是「1」，这里 add(+1) 会被引擎裁回 1 —— **等于什么都没写**。板子的显隐
    // 目前完全由模型自己的默认状态决定，本层没参与。想让它参与，得先确定这个开关的
    // 极性（1 是「摆出来」还是「收起来」），那个只能靠眼睛看。
    if (SHOW_BOARD) { add(model, "point", weight); }
    if (SHOW_BRUSH) { add(model, "bi", weight); }
    add(model, "pointZ", PRESS * penDown * weight);

    // 低头；停笔那会儿抬起头一点（平滑过渡，不跳变），写字时跟着每一笔轻轻点头。
    // 活动层的摇头晃脑也在写 headExtra，但它写 x/z，本层只补 y，谁也不覆盖谁。
    const lift = 0.45
      * smoothstep(WRITE_T - 0.08, WRITE_T + 0.12, local)
      * (1 - smoothstep(CYCLE - 0.20, CYCLE, local));
    const extra = window.__petHeadExtra || (window.__petHeadExtra = {});
    extra.y = -HEAD_DOWN * weight * (1 - lift) + Math.sin(u * Math.PI) * BOB * weight;
  } catch (err) { window.__petWriteErr = String((err && err.message) ? err.message : err); } };

  // 给 Python 侧落日志用：能直接看到运行时问到的真实范围，调参不用猜。
  window.__petWriteProbe = function () {
    const out = {
      ready: true,
      weight: weight,
      force: force,
      state: (window.__petActivityState && window.__petActivityState.name) || null,
      err: window.__petWriteErr || null,
      params: null
    };
    if (info) {
      out.params = {};
      for (const id in info) {
        out.params[id] = [info[id].min, info[id].max, info[id].def];
      }
    }
    return out;
  };
})();
"""


def _install_pet_write(self):
    """页面加载完成后注入写字层（随页面重载重装，和另外几层脚本一样）。"""
    if self.webview is None:
        return
    self.webview.page().runJavaScript(PET_WRITE_SCRIPT)


PET_WRITE_PROBE_TRIES = 8       # 探针最多试几次
PET_WRITE_PROBE_GAP_MS = 1500   # 每次间隔


def _verify_pet_write(self, attempt=0):
    """探一次，把运行时取到的参数范围写进日志，方便调参。

    `c_0120.cdi3.json` 里这些参数没有 Min/Max，真实范围只有模型自己知道。
    之所以要**重试**：第一次实测发现 2.6 秒时模型常常还没建好（`params:null`），
    探针太早打出去等于白打。所以拿到真范围之前会隔 1.5 秒再试，最多 8 次。
    """
    if self.webview is None:
        return
    self._pet_write_probe_try = attempt
    self.webview.page().runJavaScript(
        "JSON.stringify(window.__petWriteProbe ? window.__petWriteProbe() : {missing:true})",
        self._on_pet_write_probe,
    )


def _on_pet_write_probe(self, payload):
    logging.info("[pet-write] probe %s", payload)
    text = payload if isinstance(payload, str) else str(payload)
    if '"params":null' not in text and "missing" not in text:
        return          # 真范围拿到了，不用再探
    n = getattr(self, "_pet_write_probe_try", 0)
    if n + 1 < PET_WRITE_PROBE_TRIES:
        QTimer.singleShot(PET_WRITE_PROBE_GAP_MS, lambda: self._verify_pet_write(n + 1))
