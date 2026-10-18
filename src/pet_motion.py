"""桌宠点击互动层：点击这条线由我们自己播。

为什么不接着用框架的 TapBody（这是前面好几轮踩出来的结论）：

1. **框架的动作一旦起播就停不下来。** 动作队列挂在 LAppModel 上，而帧钩子拿到的是
   CubismModel（bundle 里 `new qr(t)`）；bundle 里 `window.XXX =` 一处都没有，管理器
   只活在 delegate 内部（`onTouchesEnded` 里是 `this._subdelegate.getLive2DManager()`）。
   够不到 = 没法提前结束，只能"按住它的样子"（参数压制），于是动作还会藏在拖拽背后演完。
2. **它还有一道优先级门。** 正在播的动作优先级 2，新点击也是 2 → `reserveMotion` 直接
   返回 false，旧动作没演完时点不动（`[APP]can't start motion.`）。

所以这一层把点击线整个接管：

- **松手坐标一律丢到画布外**（-10000）→ 框架 `onTap` 的命中判定必失败 → 框架永远不会起
  TapBody。`onDrag(0,0)` 的复位照常走（它用的是固定 0，不看我们传的坐标）。
- 互动由我们自己按 `motion3.json` 的曲线播（同一个文件、同一批曲线，所以观感一致）。
- **拖拽一进入（阶段离开 idle）立刻停** —— "提前结束"就只是"不再推进曲线"：
  没有队列、没有优先级、没有残留，道具和姿势下一帧就回到框架的基线。
- 拖完回到当前状态：闲置就是闲置（呼吸 + 物理 + 眨眼 + 注视），工作就是工作演出。

曲线数据是页面自己去取的：`model-selector.js` 已经把模型路径放在 URL 查询串里
（`?model=<id>&file=<file>`），所以这里 fetch 一下 model3.json 就知道 TapBody 的 6 个
动作在哪，Python 侧一行都不用改。
"""

from __future__ import annotations


PET_MOTION_ENABLED = True


PET_MOTION_SCRIPT = r"""
// 桌宠点击互动层：点击这条线由我们自己播（框架的 TapBody 起播后停不下来，所以不让它起播）。
(() => {
  if (window.__petMotionReady) { return; }
  window.__petMotionReady = true;

  // 画布外坐标：框架 onTouchesEnded 用它做 onTap 的命中判定，必然不成立。
  const BLOCK = -10000;

  const state = {
    loaded: false, why: "init", names: [], motions: [], fadeIn: 0.6,
    playing: null, index: -1, t: 0, starts: null, model: null, indexMap: null
  };
  window.__petMotionState = state;

  // ---------- motion3.json 解析 ----------
  // Segments 是扁平数组：[t0, v0, 然后每段 = 类型 + 点...]
  //   类型 0 线性 / 2 阶跃 / 3 反阶跃 → 后面 1 个点（2 个数）
  //   类型 1 贝塞尔                    → 后面 3 个点（6 个数：控制点1、控制点2、终点）
  function parseMotion(json) {
    const meta = (json && json.Meta) || {};
    const curves = [];
    const list = (json && json.Curves) || [];
    for (let ci = 0; ci < list.length; ci++) {
      const c = list[ci];
      if (c.Target !== "Parameter" || !c.Id) { continue; }
      const seg = c.Segments || [];
      if (seg.length < 2) { continue; }
      const pts = [{ t: seg[0], v: seg[1], type: 0, c1t: 0, c1v: 0, c2t: 0, c2v: 0 }];
      let i = 2;
      while (i < seg.length) {
        const type = seg[i];
        if (type === 1) {
          pts.push({ t: seg[i + 5], v: seg[i + 6], type: 1,
                     c1t: seg[i + 1], c1v: seg[i + 2], c2t: seg[i + 3], c2v: seg[i + 4] });
          i += 7;
        } else {
          pts.push({ t: seg[i + 1], v: seg[i + 2], type: type, c1t: 0, c1v: 0, c2t: 0, c2v: 0 });
          i += 3;
        }
      }
      curves.push({ id: c.Id, pts: pts });
    }
    return { duration: Number(meta.Duration) || 0, curves: curves };
  }

  function bez(p0, p1, p2, p3, s) {
    const m = 1 - s;
    return m * m * m * p0 + 3 * m * m * s * p1 + 3 * m * s * s * p2 + s * s * s * p3;
  }

  function curveAt(curve, t) {
    const pts = curve.pts;
    const n = pts.length;
    if (n === 0) { return 0; }
    if (t <= pts[0].t) { return pts[0].v; }
    if (t >= pts[n - 1].t) { return pts[n - 1].v; }
    let k = 1;
    while (k < n && pts[k].t < t) { k++; }
    const a = pts[k - 1];
    const b = pts[k];
    const span = b.t - a.t;
    if (span <= 0) { return b.v; }
    if (b.type === 2) { return a.v; }                 // 阶跃：保持前值
    if (b.type === 3) { return b.v; }                 // 反阶跃：保持后值
    if (b.type === 1) {                               // 贝塞尔：对 x 二分求参数
      let lo = 0, hi = 1, s = 0.5;
      for (let it = 0; it < 16; it++) {                // 16 次 ≈ 2^-16，误差远小于 0.01
        s = (lo + hi) * 0.5;
        if (bez(a.t, b.c1t, b.c2t, b.t, s) < t) { lo = s; } else { hi = s; }
      }
      return bez(a.v, b.c1v, b.c2v, b.v, s);
    }
    return a.v + (b.v - a.v) * ((t - a.t) / span);    // 线性
  }

  function modelIndexMap(model) {
    if (state.model === model && state.indexMap) { return state.indexMap; }
    const map = {};
    const ids = model && model._model && model._model.parameters && model._model.parameters.ids;
    if (ids) { for (let i = 0; i < ids.length; i++) { map[ids[i]] = i; } }
    state.model = model;
    state.indexMap = map;
    return map;
  }

  // ---------- 播放 ----------
  function pick() {
    if (!state.model) { state.why = "no-model"; return; }
    const n = state.motions.length;
    if (!n) { state.why = "no-motions"; return; }
    const index = Math.floor(Math.random() * n);
    const m = state.motions[index];
    const map = modelIndexMap(state.model);
    const starts = [];
    for (let i = 0; i < m.curves.length; i++) {
      const idx = map[m.curves[i].id];
      starts.push(idx === undefined ? 0 : state.model.getParameterValueByIndex(idx));
    }
    state.playing = m;                 // 正在播就直接换掉 —— 框架做不到的"随时可点"
    state.index = index;
    state.starts = starts;
    state.t = 0;
    state.why = "play:" + state.names[index];
  }

  function update(model, dt) {
    const phase = (window.__petStage && window.__petStage.phase) || "idle";
    if (phase !== "idle") {            // 拖拽一进入：立刻停，不再推进曲线
      if (state.playing) { state.playing = null; state.why = "stop:drag"; }
      return;
    }
    const m = state.playing;
    if (!m) { return; }
    state.t += dt;
    if (state.t >= m.duration) {       // 演完了：停止写入，框架的基线自然接管
      state.playing = null;
      state.why = "stop:end";
      return;
    }
    const map = modelIndexMap(model);
    const fade = Math.min(1, state.t / Math.max(0.05, state.fadeIn));
    for (let i = 0; i < m.curves.length; i++) {
      const c = m.curves[i];
      const idx = map[c.id];
      if (idx === undefined) { continue; }
      const target = curveAt(c, state.t);
      const from = state.starts[i];
      model.setParameterValueByIndex(idx, from + (target - from) * fade);
    }
  }

  // ---------- 指针钩子 ----------
  // pointerdown 一律放行：框架的 onTouchesBegan 只做 touchManager 记账（拖拽/注视要靠它），
  // 它不会起动作。只有松手要拦 —— 框架的 onTouchesEnded 会用松手坐标走 onTap。
  // 「是不是左键点击」必须在**按下**时记：Qt 的 event.buttons() 只报还按着的键，松手那一刻
  // 已经是 0（pet_widgets.py 就是照这个映射成 1/0 的），拿松手时的 buttons 认点击会一次都不中。
  let pressLeft = false;
  function installHook() {
    if (window.__petMotionHook) { return true; }
    const orig = window.__petForwardPointer;
    if (typeof orig !== "function") { return false; }
    window.__petMotionHook = true;
    window.__petForwardPointer = function (type, x, y, buttons) {
      if (type === "pointerdown") {
        pressLeft = buttons === 1;
        return orig(type, x, y, buttons);
      }
      if (type === "pointerup" || type === "pointercancel") {
        const clicked = pressLeft && type === "pointerup";
        pressLeft = false;
        // 演出期间（拖拽中）不理会点击：那条线此刻归演出层，点了也不该起互动。
        const phase = (window.__petStage && window.__petStage.phase) || "idle";
        if (clicked && phase === "idle") { pick(); }
        return orig(type, BLOCK, BLOCK, buttons);   // 框架那一份永远拦掉
      }
      return orig(type, x, y, buttons);
    };
    return true;
  }

  // ---------- 取曲线数据 ----------
  function load() {
    const q = new URLSearchParams(window.location.search);
    const folder = q.get("model") || "";
    const file = q.get("file") || "";
    if (!folder || !file) { state.why = "no-query"; return; }
    const base = "/Resources/" + encodeURIComponent(folder) + "/";
    fetch(base + encodeURIComponent(file))
      .then(r => r.json())
      .then(json => {
        const refs = ((((json || {}).FileReferences || {}).Motions || {}).TapBody) || [];
        if (!refs.length) { throw new Error("no TapBody group"); }
        return Promise.all(refs.map(ref => {
          const name = String(ref.File || "");
          const path = name.split("/").map(encodeURIComponent).join("/");
          return fetch(base + path)
            .then(r => r.json())
            .then(m => ({ motion: parseMotion(m), name: name, fade: Number(ref.FadeInTime) || 0.6 }));
        }));
      })
      .then(list => {
        state.motions = list.map(x => x.motion);
        state.names = list.map(x => x.name);
        state.fadeIn = list[0].fade;
        state.loaded = true;
        state.why = "loaded:" + list.length;
      })
      .catch(err => { state.why = "load-failed:" + (err && err.message ? err.message : err); });
  }

  // 每帧由演出层调用（跟注视层同一个规矩：子层只暴露 __petXxxPre）。
  window.__petMotionPre = function (model, dt) { try {
    state.model = model;              // 无条件记下：点击可能落在曲线还没加载完的那几毫秒里
    if (!window.__petMotionHook) { installHook(); }
    if (!state.loaded) { return; }
    update(model, Number(dt) || 0.016);
  } catch (err) { state.why = "err:" + (err && err.message ? err.message : err); } };

  window.__petMotionProbe = function () {
    return {
      loaded: state.loaded,
      why: state.why,
      count: state.motions.length,
      names: state.names,
      playing: state.playing ? state.names[state.index] : null,
      t: Math.round(state.t * 100) / 100,
      phase: (window.__petStage && window.__petStage.phase) || "idle"
    };
  };

  installHook();
  load();
})();
"""


def _install_pet_motion(self):
    """页面加载完成后注入点击互动层（幂等：脚本自己带 __petMotionReady 门）。"""
    if not PET_MOTION_ENABLED or self.webview is None:
        return
    self.webview.page().runJavaScript(PET_MOTION_SCRIPT)
