"""Embedded JavaScript and timings for the pet window."""

from __future__ import annotations


# 桌宠拖拽演出：被拎起来 -> 道具淡出 -> 摔一下 -> 爬起来 -> 道具淡回
PET_DRAG_STAGE_ENABLED = True


PET_DRAG_FALL_MS = 450


PET_DRAG_RECOVER_MS = 800


PET_DRAG_RESTORE_MS = 450


PET_MOUTH_TICK_MS = 50       # 口型推进间隔


PET_MOUTH_HOLD_MS = 250      # 页面侧超过这个时间没收到更新就闭嘴（与脚本里的 MOUTH_HOLD_MS 保持一致）


PET_DRAG_STAGE_SCRIPT = r"""
// 桌宠「被拎起来」演出驱动：main.py 通过 __petStageSet 推阶段，这里在每个模型帧里改参数与道具透明度
(() => {
  if (window.__petDragStageReady) { return; }
  window.__petDragStageReady = true;

  const PROP_PARTS = { Part18: 1, Part21: 1, Part35: 1, Part109: 1, Part123: 1, Part122: 1, Part5: 1, Part121: 1, Part119: 1, Part120: 1, Part4: 1, Part22: 1, Part114: 1, Part6: 1, Part107: 1, Part106: 1, Part110: 1, Part43: 1, Part10: 1, Part15: 1, Part37: 1, Part112: 1, Part124: 1, Part127: 1, Part129: 1, Part130: 1, Part128: 1, ABXY: 1, M: 1 };

  // ParamCheek75 = 「感叹号」贴图、ParamCheek18 = 「吐魂」贴图
  // （感叹号.exp3.json / 吐魂.exp3.json 各自都只改这一个参数）。
  // 两个都必须放进 NEUTRAL 才会进 PARAM_ORDER，被 writePose 每帧绝对写入：
  // 框架每帧 loadParameters() 会还原参数，没被逐帧写入的会瞬间弹回去，符号会一闪一闪。
  // 没有这些参数的模型（如 Mao）会在 writePose 里因 index < 0 被跳过，不会报错。
  const NEUTRAL = { ParamAngleZ: 0, ParamBodyAngleZ: 0, ParamAngleX: 0, ParamAngleY: 0, ParamEyeLOpen: 1, ParamEyeROpen: 1, ParamEyeBallX: 0, ParamEyeBallY: 0, ParamBrowLForm: 0, ParamBrowRForm: 0, ParamCheek77: 0, ParamCheek22: 0, ParamCheek20: 0, ParamCheek75: 0, ParamCheek18: 0, ParamMouthForm: -0.5, ParamMouthOpenY: 0 };
  // 被拎起来与拖拽中都挂「！」；drag 阶段由 POSE_HOLD 派生，改这里即可同时生效。
  // 松手走 POSE_FALL（继承 NEUTRAL 的 0），所以落地即收。
  const POSE_HOLD = Object.assign({}, NEUTRAL, { ParamEyeLOpen: 0.55, ParamEyeROpen: 0.55, ParamEyeBallX: 0.1, ParamEyeBallY: 0.6, ParamBrowLForm: 1, ParamBrowRForm: 1, ParamCheek77: 1, ParamCheek75: 1, ParamMouthForm: -0.6, ParamMouthOpenY: 0.3 });
  // 松手瞬间「吐魂」就出来，整个坠落阶段（450ms）都吐着；
  // 爬起来时 recover 会从 POSE_FALL 插值回 NEUTRAL，魂便随起身过程慢慢收回。
  const POSE_FALL = Object.assign({}, NEUTRAL, { ParamEyeLOpen: 0, ParamEyeROpen: 0, ParamBrowLForm: -1, ParamBrowRForm: -1, ParamCheek22: 1, ParamCheek18: 1, ParamMouthForm: -1.5, ParamMouthOpenY: 1 });
  const PARAM_ORDER = Object.keys(NEUTRAL);

  const IDLE_MS = 140;
  const FALL_S = 0.45;
  const RECOVER_S = 0.8;
  const FADE_RATE = 9;
  const FADE_PROPS = false;   // 道具只在 true 时淡出；留着手才有地方放

  const stage = { phase: "idle", vx: 0, vy: 0, at: performance.now() };
  window.__petStage = stage;
  window.__petStageSet = function (phase, vx, vy) {
    stage.phase = phase;
    stage.vx = vx || 0;
    stage.vy = vy || 0;
    stage.at = performance.now();
  };

  const MOUTH_HOLD_MS = 250;
  const mouth = { open: 0, jitter: 0, at: 0 };
  window.__petMouthSet = function (open, jitter) {
    mouth.open = clamp(Number(open) || 0, 0, 1);
    mouth.jitter = clamp(Number(jitter) || 0, 0, 1);
    mouth.at = performance.now();
  };
  // 口型状态也挂出去：音乐律动层（pet_music.py）靠它判断「在说话」，好让位。
  window.__petMouthState = mouth;
  let fadeModel = null;
  let fadeList = null;
  let propK = 1;
  let propTarget = 1;
  let rot = 0;
  let lift = 0;
  let squash = 1;
  let dt = 0.016;
  let phaseAt = performance.now();
  let lastPhase = "idle";
  let lastTick = performance.now();
  let baseTransform = null;

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function ease(u) { return u < 0.5 ? 2 * u * u : 1 - 2 * (1 - u) * (1 - u); }
  function stageError(where, err) {
    window.__petStageErr = where + ": " + (err && err.message ? err.message : String(err));
    if (!window.__petStageErrLogged) {
      window.__petStageErrLogged = true;
      console.error("[pet-stage] " + window.__petStageErr);
    }
  }

  function fadeTargets(model) {
    if (fadeModel === model && fadeList) { return fadeList; }
    const core = model._model;
    const names = Array.from(core.parts.ids);
    const parent = core.drawables.parentPartIndices;
    const list = [];
    for (let i = 0; i < core.drawables.count; i++) {
      if (PROP_PARTS[names[parent[i]]]) { list.push(i); }
    }
    fadeModel = model;
    fadeList = list;
    return list;
  }

  let paramModel = null;
  let paramIndex = null;

  function paramMap(model) {
    if (paramModel === model && paramIndex) { return paramIndex; }
    const ids = Array.from(model._model.parameters.ids);
    const map = {};
    for (let i = 0; i < PARAM_ORDER.length; i++) { map[PARAM_ORDER[i]] = ids.indexOf(PARAM_ORDER[i]); }
    paramModel = model;
    paramIndex = map;
    return map;
  }

  // 注意：框架的 setParameterValueById 只认 CubismId 对象（字符串查不到会被静默丢弃），所以这里按参数下标写
  function writePose(model, pose) {
    const map = paramMap(model);
    for (let i = 0; i < PARAM_ORDER.length; i++) {
      const key = PARAM_ORDER[i];
      const index = map[key];
      if (index >= 0) { model.setParameterValueByIndex(index, pose[key]); }
    }
  }

  // 点击互动（框架的 TapBody 动作）不在这里处理。框架的动作一旦起播就停不下来 ——
  // 队列挂在 LAppModel 上，而帧钩子拿到的是 CubismModel；bundle 里也没有任何全局入口。
  // 所以改成整个接管：拦掉框架的 onTap + 自己按 motion3.json 播曲线，见 pet_motion.py。
  // 这里只管拖拽演出，以及它之后的各层。

  window.__petStageProbe = function () {
    const model = paramModel;
    const map = paramIndex;
    const info = { hook: true, phase: stage.phase, err: window.__petStageErr || null, index: {}, value: {}, mouth: { open: mouth.open, jitter: mouth.jitter, age: performance.now() - mouth.at } };
    if (!model || !map) { info.model = false; return info; }
    // 点击互动层（pet_motion.py）的状态：曲线加载好了没、正在播哪个、拖拽有没有把它停掉。
    info.motion = window.__petMotionProbe ? window.__petMotionProbe() : { missing: true };
    // 音乐律动层（pet_music.py）：哪道闸门挡着、包络多大、正在响的是谁。
    info.music = window.__petMusicProbe ? window.__petMusicProbe() : { missing: true };
    const keys = ["ParamEyeLOpen", "ParamCheek77", "ParamMouthOpenY", "ParamAngleZ", "ParamBodyAngleZ"];
    for (let i = 0; i < keys.length; i++) {
      const index = map[keys[i]];
      info.index[keys[i]] = index;
      info.value[keys[i]] = index >= 0 ? model.getParameterValueByIndex(index) : null;
    }
    return info;
  };

  window.__petFramePre = function (model) { try {
    const now = performance.now();
    dt = clamp((now - lastTick) / 1000, 0.001, 0.05);
    lastTick = now;
    // 点击互动层：框架那一份已经被 pet_motion.py 拦掉（松手坐标丢画布外），这里只驱动我们
    // 自己播的那条线。它会在阶段离开 idle 的第一帧把自己停掉 —— 所以拖拽一进入，道具和姿势
    // 立刻回落到框架的基线：没有藏着的动作，也没有优先级死窗口。
    window.__petMotionPre && window.__petMotionPre(model, dt);
    if (stage.phase !== lastPhase) {
      lastPhase = stage.phase;
      phaseAt = now;
    }
    if (now - stage.at > IDLE_MS) {
      stage.vx *= 0.82;
      stage.vy *= 0.82;
      if (Math.abs(stage.vx) < 1) { stage.vx = 0; }
      if (Math.abs(stage.vy) < 1) { stage.vy = 0; }
    }

    const phase = stage.phase;
    const t = (now - phaseAt) / 1000;
    let wantRot = 0;
    let wantLift = 0;
    let wantSquash = 1;
    let pose = null;
    let direct = false;

    if (phase === "hold") {
      propTarget = 0;
      wantRot = Math.sin(now / 620) * 2.5;
      wantLift = -5;
      pose = POSE_HOLD;
    } else if (phase === "drag") {
      propTarget = 0;
      const speed = Math.abs(stage.vx);
      wantRot = clamp(-stage.vx * 0.02, -14, 14);
      wantLift = -5 - Math.min(6, speed * 0.004) + Math.sin(now / 240) * 2;
      pose = Object.assign({}, POSE_HOLD, { ParamMouthOpenY: clamp(0.25 + speed / 2600, 0.25, 0.9) });
    } else if (phase === "fall") {
      propTarget = 0;
      const u = clamp(t / FALL_S, 0, 1);
      wantRot = 11;
      wantLift = -5 + 27 * ease(Math.min(1, u * 2.5));
      wantSquash = 1 - 0.13 * Math.min(1, u * 3);
      pose = POSE_FALL;
      direct = true;
    } else if (phase === "recover") {
      propTarget = 0;
      const u = clamp(t / RECOVER_S, 0, 1);
      const e = ease(u);
      wantRot = 11 * (1 - e);
      wantLift = 22 * (1 - e) - 4 * Math.sin(Math.PI * u);
      wantSquash = 0.87 + 0.13 * e + 0.04 * Math.sin(Math.PI * u);
      pose = {};
      for (let i = 0; i < PARAM_ORDER.length; i++) {
        const key = PARAM_ORDER[i];
        pose[key] = POSE_FALL[key] + (NEUTRAL[key] - POSE_FALL[key]) * e;
      }
      direct = true;
    } else {
      propTarget = 1;
    }

    rot += (wantRot - rot) * Math.min(1, dt * 10);
    if (direct) {
      lift = wantLift;
      squash = wantSquash;
    } else {
      lift += (wantLift - lift) * Math.min(1, dt * 12);
      squash += (wantSquash - squash) * Math.min(1, dt * 14);
    }
    if (pose) {
      pose.ParamAngleZ = clamp(rot, -30, 30);
      pose.ParamBodyAngleZ = clamp(rot * 0.45, -10, 10);
      writePose(model, pose);
    }
    const mouthAge = now - mouth.at;
    if (phase === "idle" && mouth.at > 0 && mouthAge < MOUTH_HOLD_MS) {
      const map = paramMap(model);
      const openIndex = map.ParamMouthOpenY;
      if (openIndex >= 0) {
        model.setParameterValueByIndex(openIndex, clamp(mouth.open + mouth.jitter * Math.sin(now / 47), 0, 1));
      }
      const formIndex = map.ParamMouthForm;
      if (formIndex >= 0) {
        model.setParameterValueByIndex(formIndex, -0.5 + 0.6 * clamp(mouth.open, 0, 1));
      }
    }
    window.__petActivityPre && window.__petActivityPre(model, dt);
    window.__petParamPre && window.__petParamPre(model, dt);
    // 写字层排在注视层之前：它会给 headExtra 补一个「低头」的 y，而注视层是
    // 唯一真正把 headExtra 写进 ParamAngleY 的地方，得让它读到最新值。
    window.__petWritePre && window.__petWritePre(model, dt);
    window.__petGazePre && window.__petGazePre(model, dt);
    // 音乐律动层放最后：它也是加算（见 pet_music.py），跟注视层自然叠在一起。
    window.__petMusicPre && window.__petMusicPre(model, dt);
    } catch (err) { stageError("pre", err); }
  };

  window.__petFramePost = function (model) { try {
    const fadeGoal = FADE_PROPS ? propTarget : 1;
    propK += (fadeGoal - propK) * Math.min(1, dt * FADE_RATE);
    if (propK > 0.995) { propK = 1; }
    if (propK < 1) {
      const list = fadeTargets(model);
      const ops = model._model.drawables.opacities;
      for (let i = 0; i < list.length; i++) { ops[list[i]] = ops[list[i]] * propK; }
    }

    const act = window.__petActivityPose ? window.__petActivityPose() : null;
    if (stage.phase === "idle" && propK === 1) {
      if (Math.abs(rot) < 0.05 && Math.abs(lift) < 0.05 && Math.abs(squash - 1) < 0.002) {
        if (!act && rot === 0 && lift === 0 && squash === 1) { return; }
        rot = 0;
        lift = 0;
        squash = 1;
      }
    }

    const canvas = document.querySelector("canvas");
    if (!canvas) { return; }
    if (baseTransform === null) { baseTransform = canvas.style.transform || ""; }
    canvas.style.transformOrigin = "50% 74%";
    const actLift = act ? act.lift : 0;
    const actRot = act ? act.rot : 0;
    const actScale = act ? act.scaleY : 1;
    canvas.style.transform = baseTransform + " translateY(" + (lift + actLift).toFixed(2) + "px) rotate(" + (rot + actRot).toFixed(2) + "deg) scaleY(" + (squash * actScale).toFixed(3) + ")";
    } catch (err) { stageError("post", err); }
  };
})();

"""


# ======================== 桌宠活动反馈（思考/工作/完成） ========================
PET_ACTIVITY_ENABLED = True


PET_ACTIVITY_DONE_MS = 1600          # 完成庆祝动作时长


PET_ACTIVITY_HINT_MS = 4000          # 工作进度气泡停留时长


PET_ACTIVITY_HINT_MIN_GAP = 1200     # 两条工作气泡之间的最小间隔（毫秒）


PET_ACTIVITY_SCRIPT = r"""
// 桌宠活动反馈层：思考/工作/完成时的轻量动作。
// 只动眼神、眉毛、问号/星星与轻微位移；拖拽演出（phase != idle）时整层让位。
(() => {
  if (window.__petActivityReady) { return; }
  window.__petActivityReady = true;

  const state = { name: "idle", at: performance.now() };
  window.__petActivityState = state;

  window.__petActivitySet = function (name) {
    const next = String(name || "idle");
    if (next === state.name) { return; }
    state.name = next;
    state.at = performance.now();
  };

  const PARAM_IDS = ["ParamEyeBallX", "ParamEyeBallY", "ParamEyeLOpen", "ParamEyeROpen",
                     "ParamBrowLForm", "ParamBrowRForm", "ParamCheek74", "ParamCheek16",
                     // ParamCheek75 = 「感叹号」贴图，用于 done 的完成反馈。
                     // 拖拽演出也在用同一张贴图，但两边阶段互斥（拖拽时本层整体让位），
                     // 且 idle 阶段拖拽脚本不写任何参数，所以不会打架。
                     "ParamCheek75"];
  let indexModel = null;
  let indexMap = null;

  function indexes(model) {
    if (indexModel === model && indexMap) { return indexMap; }
    const ids = Array.from(model._model.parameters.ids);
    const map = {};
    for (let i = 0; i < PARAM_IDS.length; i++) { map[PARAM_IDS[i]] = ids.indexOf(PARAM_IDS[i]); }
    indexModel = model;
    indexMap = map;
    return map;
  }

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function approach(current, target, dt, rate) {
    return current + (target - current) * Math.min(1, dt * rate);
  }

  const eased = { eyeX: 0, eyeY: 0, brow: 0, question: 0, star: 0, squint: 0, exclaim: 0 };
  let lastTick = performance.now();

  function write(model, map, id, value) {
    const index = map[id];
    if (index >= 0) { model.setParameterValueByIndex(index, value); }
  }

  window.__petActivityPre = function (model, dt) { try {
    const now = performance.now();
    dt = clamp(Number(dt) || (now - lastTick) / 1000, 0.001, 0.05);
    lastTick = now;
    const phase = (window.__petStage && window.__petStage.phase) || "idle";
    if (phase !== "idle") { return; }

    const name = state.name;
    if (name === "done" && (now - state.at) / 1000 > 2.5) { window.__petActivitySet("idle"); }

    const thinking = name === "thinking";
    const working = name === "working" || name === "waiting";
    const done = name === "done";
    const wanted = {
      eyeX: thinking ? -0.5 : 0,
      eyeY: thinking ? 0.6 : 0,
      brow: (thinking || done) ? 0.6 : 0,
      // 思考与工作期间都常驻。旧值 (now % 3200) < 1100 是「亮 1.1s / 灭 2.1s」的闪烁，
      // 看起来就像刚冒出来就没了；工作状态闪 1.6s 也让人觉得符号时有时无。
      question: (thinking || working) ? 1 : 0,
      star: done ? 1 : 0,
      // 完成反馈：星星眼 + 感叹号一起挂（同一个 done 窗口 2.5s）
      exclaim: done ? 1 : 0,
      squint: working ? 1 : 0
    };

    // 摇头晃脑改走头部参数（只动头，桌子和道具不跟着转），单位是度。
    // 头部参数必须只有一个写入者，所以这里只登记偏移，由注视层统一相加。
    if (working) {
      window.__petHeadExtra = { x: Math.sin(now / 240) * 5.5, z: Math.sin(now / 340) * 3 };
    } else if (window.__petHeadExtra) {
      window.__petHeadExtra = null;
    }
    eased.eyeX = approach(eased.eyeX, wanted.eyeX, dt, 5);
    eased.eyeY = approach(eased.eyeY, wanted.eyeY, dt, 5);
    eased.brow = approach(eased.brow, wanted.brow, dt, 5);
    eased.question = approach(eased.question, wanted.question, dt, 6);
    eased.star = approach(eased.star, wanted.star, dt, 8);
    eased.exclaim = approach(eased.exclaim, wanted.exclaim, dt, 8);
    eased.squint = approach(eased.squint, wanted.squint, dt, 5);

    const total = Math.abs(eased.eyeX) + Math.abs(eased.eyeY) + eased.brow
      + eased.question + eased.star + eased.exclaim + eased.squint;
    if (total < 0.004) { return; }

    const map = indexes(model);
    write(model, map, "ParamEyeBallX", eased.eyeX);
    write(model, map, "ParamEyeBallY", eased.eyeY);
    write(model, map, "ParamBrowLForm", eased.brow);
    write(model, map, "ParamBrowRForm", eased.brow);
    write(model, map, "ParamCheek74", eased.question);
    write(model, map, "ParamCheek16", eased.star);
    write(model, map, "ParamCheek75", eased.exclaim);
    if (eased.squint > 0.004) {
      const factor = 1 - 0.22 * eased.squint;
      const eyeIds = ["ParamEyeLOpen", "ParamEyeROpen"];
      for (let i = 0; i < eyeIds.length; i++) {
        const index = map[eyeIds[i]];
        if (index >= 0) {
          const current = model.getParameterValueByIndex(index);
          model.setParameterValueByIndex(index, current * factor);
        }
      }
    }
  } catch (err) { window.__petActivityErr = String((err && err.message) ? err.message : err); } };

  let poseActive = false;
  window.__petActivityPose = function () { try {
    const now = performance.now();
    const phase = (window.__petStage && window.__petStage.phase) || "idle";
    const name = phase === "idle" ? state.name : "idle";
    const elapsed = (now - state.at) / 1000;
    // 注意：working/waiting 不再走整身摇摆 —— 那会把桌子和道具一起转，
    // 改为通过 __petHeadExtra 只动头（见 __petActivityPre）。
    // 这条 canvas transform 通道留给以后的多媒体监听（跟音乐律动）。
    let pose = null;
    if (name === "thinking") {
      pose = { lift: -1.5, rot: -2.4 + Math.sin(now / 900) * 1.1, scaleY: 1 };
    } else if (name === "done" && elapsed < 1.6) {
      const decay = Math.pow(1 - Math.min(1, elapsed / 1.6), 2);
      pose = {
        lift: -Math.abs(Math.sin(elapsed * Math.PI * 3.2)) * 8 * decay,
        rot: Math.sin(elapsed * Math.PI * 4.0) * 4 * decay,
        scaleY: 1 + 0.05 * decay * Math.sin(elapsed * Math.PI * 6.4)
      };
    }
    if (pose) { poseActive = true; return pose; }
    if (poseActive) { poseActive = false; return { lift: 0, rot: 0, scaleY: 1 }; }
    return null;
  } catch (err) {
    window.__petActivityErr = String((err && err.message) ? err.message : err);
    return null;
  } };
})();

"""


# ======================== 桌宠 Live2D 参数层（大模型可调） ========================
PET_PARAM_ENABLED = True


PET_PARAM_DEFAULT_MS = 6000


PET_PARAM_MIN_MS = 500


PET_PARAM_MAX_MS = 30000


# 语义情绪名（与 PET_PARAM_SCRIPT 内 EMOTIONS 保持一致，action_control 用它做参数校验）
PET_EMOTION_NAMES = (
    "happy", "sad", "angry", "surprised", "shy", "wink", "cry", "sleepy",
    "love", "sparkle", "magic", "heart", "excited", "confused", "nervous",
    "tongue", "neutral",
)


PET_PARAM_SCRIPT = r"""
// 桌宠 Live2D 情绪层：语义情绪 + 原始参数覆盖，由大模型经工具调用驱动。
// 状态仲裁：工作/思考/等待时整层让位给活动层（活动层独占）；播放 TTS 时与口型共存，
// 但嘴型永远归口型动画（speaking 期间情绪不写嘴）。拖拽演出（phase != idle）时整层让位。
// 每个覆盖项到期后平滑回落到底层默认值，再把控制权交回框架动作/物理。
// 注意：框架每帧 loadParameters() 会用上一帧 saveParameters() 的快照还原参数，
// 所以覆盖项必须自己维护动画值（overrides[id].cur）；若每帧从模型当前值重新逼近，
// 就只会走 rate*dt（约两成）就停在原地，表现是所有表情都半透明发虚。
(() => {
  if (window.__petParamReady) { return; }
  window.__petParamReady = true;

  const DEFAULT_DURATION_MS = 6000;
  const MIN_DURATION_MS = 500;
  const MAX_DURATION_MS = 30000;
  const APPLY_RATE = 12;      // 逼近目标速度
  const RELEASE_RATE = 7;     // 回落默认值速度
  const SWITCH_APPLY_RATE = 26;    // 物料开关（ParamCheek** 这类 0/1 美术件）要快
  const SWITCH_RELEASE_RATE = 22;  // 否则切换时两个物料各半透明地叠在一起
  const CLEAR_EPS = 0.02;

  // 说话期间这些参数归 __petMouthSet 口型动画独占，情绪不得写入
  const MOUTH_PARAMS = {
    ParamMouthForm: 1, ParamMouthOpenY: 1,
    ParamA: 1, ParamI: 1, ParamU: 1, ParamE: 1, ParamO: 1,
  };

  window.__petSpeaking = false;

  // 语义情绪 -> 参数目标。每项 ids 为候选参数名，运行时取当前模型中第一个存在的。
  // 覆盖 Mao / Hiyori / 大肥鱼 三种内置模型的常见参数，跨模型自动降级。
  const EMOTIONS = {
    happy: [
      { ids: ["ParamEyeLSmile"], value: 1 },
      { ids: ["ParamEyeRSmile"], value: 1 },
      { ids: ["ParamMouthForm"], value: 1 },
      { ids: ["ParamCheek"], value: 0.6 },
      { ids: ["ParamCheek76"], value: 1 }
    ],
    sad: [
      // 不用作者 阴暗.exp3 的 Paramhh3：那是整脸蒙一层半透明灰，会让画面发虚
      { ids: ["ParamBrowLY"], value: -1 },
      { ids: ["ParamBrowRY"], value: -1 },
      { ids: ["ParamMouthForm"], value: -1.5 },
      { ids: ["ParamEyeBallY"], value: -0.5 },
      { ids: ["ParamEyeLOpen"], value: 0.6 },
      { ids: ["ParamEyeROpen"], value: 0.6 },
      { ids: ["ParamCheek15"], value: 1 }
    ],
    angry: [
      { ids: ["ParamBrowLX"], value: -1 },
      { ids: ["ParamBrowLY"], value: -0.5 },
      { ids: ["ParamBrowLAngle"], value: -1 },
      { ids: ["ParamBrowLForm"], value: -0.6 },
      { ids: ["ParamBrowRX"], value: -1 },
      { ids: ["ParamBrowRY"], value: -0.5 },
      { ids: ["ParamBrowRAngle"], value: -1 },
      { ids: ["ParamBrowRForm"], value: -0.6 },
      { ids: ["ParamMouthAngry"], value: 1 },
      { ids: ["ParamCheek80"], value: 1 },
      { ids: ["ParamCheek23"], value: 1 }
    ],
    surprised: [
      { ids: ["ParamEyeLOpen"], value: 1.7 },
      { ids: ["ParamEyeROpen"], value: 1.7 },
      { ids: ["ParamBrowLY"], value: 1 },
      { ids: ["ParamBrowRY"], value: 1 },
      { ids: ["ParamMouthOpenY"], value: 0.9 },
      { ids: ["ParamA"], value: 0.9 },
      { ids: ["ParamO"], value: 0.6 },
      { ids: ["ParamCheek75"], value: 1 }
    ],
    shy: [
      { ids: ["ParamCheek"], value: 1 },
      { ids: ["Paramhh2"], value: 1 },
      { ids: ["ParamEyeLSmile"], value: 0.7 },
      { ids: ["ParamEyeRSmile"], value: 0.7 },
      { ids: ["ParamEyeBallX"], value: -0.25 },
      { ids: ["ParamEyeBallY"], value: -0.3 },
      { ids: ["ParamMouthForm"], value: 0.3 }
    ],
    wink: [
      { ids: ["ParamEyeLOpen"], value: 0 },
      { ids: ["ParamEyeRSmile"], value: 1 },
      { ids: ["ParamMouthForm"], value: 0.6 },
      { ids: ["ParamCheek"], value: 0.4 },
      { ids: ["ParamCheek21"], value: 1 }
    ],
    cry: [
      // 眼睛全闭：作者 哭.exp3 自带呆眼物料，留缝会露出底下的眼睛叠影
      { ids: ["ParamEyeLOpen"], value: 0 },
      { ids: ["ParamEyeROpen"], value: 0 },
      { ids: ["ParamEyeBallY"], value: -0.7 },
      { ids: ["ParamBrowLY"], value: -1 },
      { ids: ["ParamBrowRY"], value: -1 },
      { ids: ["ParamMouthForm"], value: -1.5 },
      { ids: ["ParamCheek20"], value: 1 },
      { ids: ["ParamCheek27"], value: 1 }
    ],
    sleepy: [
      // 眼睛全闭：作者 闭眼口水.exp3 的物料自带闭眼美术
      { ids: ["ParamEyeLOpen"], value: 0 },
      { ids: ["ParamEyeROpen"], value: 0 },
      { ids: ["ParamBrowLY"], value: -0.4 },
      { ids: ["ParamBrowRY"], value: -0.4 },
      { ids: ["ParamMouthForm"], value: -0.3 },
      { ids: ["ParamMouthOpenY"], value: 0.15 },
      { ids: ["ParamCheek22"], value: 1 }
    ],
    love: [
      { ids: ["ParamCheek"], value: 0.9 },
      { ids: ["ParamEyeLSmile"], value: 1 },
      { ids: ["ParamEyeRSmile"], value: 1 },
      { ids: ["ParamCheek17"], value: 1 },
      { ids: ["ParamHeartHealOn"], value: 1 },
      { ids: ["ParamHeartColorHeal"], value: 1 },
      { ids: ["ParamHeartDrow"], value: 1 },
      { ids: ["ParamHeartSize"], value: 1 }
    ],
    sparkle: [
      { ids: ["ParamCheek"], value: 0.5 },
      { ids: ["ParamEyeLSmile"], value: 1 },
      { ids: ["ParamEyeRSmile"], value: 1 },
      { ids: ["ParamEyeBallY"], value: 0.2 },
      { ids: ["ParamCheek16"], value: 1 },
      { ids: ["ParamHeartLightOn"], value: 1 },
      { ids: ["ParamHeartLight"], value: 1 }
    ],
    magic: [
      { ids: ["ParamWandInk"], value: 1 },
      { ids: ["ParamWandRotate"], value: 1 },
      { ids: ["ParamAuraOn"], value: 1 },
      { ids: ["ParamAura"], value: 1 },
      { ids: ["ParamEyeLSmile"], value: 0.6 },
      { ids: ["ParamEyeRSmile"], value: 0.6 },
      { ids: ["mozhua"], value: 1 },
      { ids: ["bi"], value: 1 }
    ],
    heart: [
      { ids: ["ParamHeartHealOn"], value: 1 },
      { ids: ["ParamHeartColorHeal"], value: 1 },
      { ids: ["ParamHeartDrow"], value: 1 },
      { ids: ["ParamHeartSize"], value: 1 },
      { ids: ["ParamCheek"], value: 1 },
      { ids: ["ParamCheek73"], value: 1 }
    ],
    excited: [
      { ids: ["ParamEyeLOpen"], value: 1.3 },
      { ids: ["ParamEyeROpen"], value: 1.3 },
      { ids: ["ParamEyeBallY"], value: 0.3 },
      { ids: ["ParamCheek"], value: 0.7 },
      { ids: ["ParamMouthOpenY"], value: 0.5 },
      { ids: ["ParamA"], value: 0.5 },
      { ids: ["ParamCheek76"], value: 1 }
    ],
    confused: [
      { ids: ["ParamBrowLY"], value: 1 },
      { ids: ["ParamBrowRY"], value: -0.4 },
      { ids: ["ParamEyeBallX"], value: 0.4 },
      { ids: ["ParamMouthForm"], value: -0.3 },
      { ids: ["ParamCheek74"], value: 1 }
    ],
    nervous: [
      { ids: ["ParamCheek19"], value: 1 },
      { ids: ["ParamEyeLOpen"], value: 0.9 },
      { ids: ["ParamEyeROpen"], value: 0.9 },
      { ids: ["ParamBrowLY"], value: -0.3 },
      { ids: ["ParamBrowRY"], value: -0.3 },
      { ids: ["ParamMouthForm"], value: -0.4 }
    ],
    tongue: [
      { ids: ["ParamCheek79"], value: 1 },
      { ids: ["ParamMouthOpenY"], value: 0.76 },
      { ids: ["ParamMouthForm"], value: 0.97 },
      { ids: ["ParamEyeRSmile"], value: 0.8 }
    ],
    neutral: []
  };

  window.__petParamEmotions = Object.keys(EMOTIONS);

  let modelRef = null;
  let indexOf = {};
  let ranges = {};
  let pendingOp = null;
  let pendingWear = null;

  const overrides = {};   // id -> { target, rate, release }  瞬时情绪/裸参数
  const worn = {};        // id -> { value, cur }             持久穿戴（配件/表情），与瞬时互不干扰
  let expireAt = 0;

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  function ensureModel(model) {
    if (modelRef === model && modelRef) { return true; }
    const params = model && model._model && model._model.parameters;
    if (!params || !params.ids) { return false; }
    modelRef = model;
    indexOf = {};
    ranges = {};
    const ids = Array.from(params.ids);
    for (let i = 0; i < ids.length; i++) {
      indexOf[ids[i]] = i;
      ranges[ids[i]] = {
        min: params.minimumValues ? params.minimumValues[i] : -10,
        max: params.maximumValues ? params.maximumValues[i] : 10,
        def: params.defaultValues ? params.defaultValues[i] : 0
      };
    }
    if (pendingOp) {
      const op = pendingOp;
      pendingOp = null;
      try { op(); } catch (err) { window.__petParamErr = String((err && err.message) ? err.message : err); }
    }
    if (pendingWear !== null) {
      const names = pendingWear;
      pendingWear = null;
      try { applyWear(names); } catch (err) { window.__petParamErr = String((err && err.message) ? err.message : err); }
    }
    return true;
  }

  function pickId(ids) {
    for (let i = 0; i < ids.length; i++) {
      if (Object.prototype.hasOwnProperty.call(indexOf, ids[i])) { return ids[i]; }
    }
    return null;
  }

  // 参数分组：作者 exp3 已经照顾到的组（眼/眉/嘴）让通用表让位，避免两边各写一份打架。
  function paramGroup(id) {
    if (/^ParamEye/.test(id)) { return "eye"; }
    if (/^ParamBrow/.test(id)) { return "brow"; }
    if (/^ParamMouth/.test(id) || id === "ParamA" || id === "ParamI" || id === "ParamU"
        || id === "ParamE" || id === "ParamO") { return "mouth"; }
    return "";
  }

  // 物料开关：0/1 驱动一整块美术件（大肥鱼 ParamCheek**/Paramhh* 与道具参数），
  // 走快档、且不按强度缩放，绝不停在半透明中间态。
  function isSwitchParam(id) {
    return /^ParamCheek\d+$/.test(id) || /^Paramhh\d+$/.test(id)
      || id === "mozhua" || id === "mozhua2" || id === "love" || id === "aixing"
      || id === "ji" || id === "bi";
  }

  // 作者自带 exp3 的 Delta 折算成绝对值：Add = 默认值 + 增量，Multiply = 默认值 * 增量，
  // Set = 直接取该值（配置文件里的 params 用）。
  function convertEntries(list) {
    if (!list || !list.length) { return null; }
    const out = [];
    for (let i = 0; i < list.length; i++) {
      const item = list[i] || {};
      const id = String(item.id || "");
      const range = ranges[id];
      if (!id || !range) { continue; }
      const delta = Number(item.value) || 0;
      const blend = String(item.blend || "Add").toLowerCase();
      let value = delta;
      if (blend === "multiply") { value = range.def * delta; }
      else if (blend === "add") { value = range.def + delta; }
      // 没真正改动的项（Add 0 / Multiply 1）跳过：覆盖它们等于把参数钉死，连眨眼都会被冻住。
      if (Math.abs(value - range.def) < 1e-6) { continue; }
      out.push({ id: id, value: value });
    }
    return out.length ? out : null;
  }

  // 情绪用：config 解析后的情绪表（作者表情 + 配置 params）。
  function expressionEntries(name) {
    const table = window.__petExprParams;
    return convertEntries(table ? table[name] : null);
  }

  // 穿戴用：模型全部 exp3 原表。
  function wornEntries(name) {
    const table = window.__petAllExpressions;
    return convertEntries(table ? table[name] : null);
  }

  function applyWear(names) {
    for (const id in worn) { delete worn[id]; }
    const list = names || [];
    for (let i = 0; i < list.length; i++) {
      const entries = wornEntries(list[i]);
      if (!entries) { continue; }
      for (let j = 0; j < entries.length; j++) {
        const entry = entries[j];
        const range = ranges[entry.id];
        if (!range) { continue; }
        worn[entry.id] = { value: clamp(entry.value, range.min, range.max), cur: null };
      }
    }
    return Object.keys(worn);
  }

  window.__petWearApply = function (names) {
    if (!modelRef) { pendingWear = names; return true; }
    applyWear(names);
    return true;
  };

  window.__petWearClear = function () {
    for (const id in worn) { delete worn[id]; }
    pendingWear = null;
  };

  function setOverride(id, target, isSwitch) {
    const range = ranges[id];
    if (!range) { return; }
    const keep = overrides[id];
    overrides[id] = {
      target: clamp(target, range.min, range.max),
      cur: keep ? keep.cur : null,
      rate: isSwitch ? SWITCH_APPLY_RATE : APPLY_RATE,
      releaseRate: isSwitch ? SWITCH_RELEASE_RATE : RELEASE_RATE,
      release: false
    };
  }

  function performEmotion(name, intensity, durationMs) {
    const table = EMOTIONS[name];
    if (!table) { return; }
    for (const id in overrides) { overrides[id].release = true; }
    const k = clamp(Number(intensity) || 0, 0, 1);
    // 作者原版表情优先：它才是该模型「正确」的观感（物料之外还带基础五官改动）。
    const expr = expressionEntries(name);
    const claimedId = {};
    const claimedGroup = {};
    if (expr) {
      for (let i = 0; i < expr.length; i++) {
        claimedId[expr[i].id] = true;
        const group = paramGroup(expr[i].id);
        if (group) { claimedGroup[group] = true; }
      }
    }
    for (let i = 0; i < table.length; i++) {
      const entry = table[i];
      const id = pickId(entry.ids);
      if (!id || claimedId[id]) { continue; }
      const group = paramGroup(id);
      if (group && claimedGroup[group]) { continue; }
      setOverride(id, entry.value * k, isSwitchParam(id));
    }
    if (expr) {
      for (let i = 0; i < expr.length; i++) {
        const id = expr[i].id;
        const isSwitch = isSwitchParam(id);
        setOverride(id, isSwitch ? expr[i].value : expr[i].value * k, isSwitch);
      }
    }
    const dur = clamp(Number(durationMs) || DEFAULT_DURATION_MS, MIN_DURATION_MS, MAX_DURATION_MS);
    expireAt = performance.now() + dur;
  }

  function performRaw(mapping, durationMs) {
    const keys = Object.keys(mapping || {});
    for (const id in overrides) { overrides[id].release = true; }
    for (let i = 0; i < keys.length; i++) {
      const id = keys[i];
      if (Object.prototype.hasOwnProperty.call(indexOf, id)) {
        setOverride(id, Number(mapping[id]) || 0, false);
      }
    }
    const dur = clamp(Number(durationMs) || DEFAULT_DURATION_MS, MIN_DURATION_MS, MAX_DURATION_MS);
    expireAt = performance.now() + dur;
  }

  function releaseAll() {
    for (const id in overrides) { overrides[id].release = true; }
  }

  window.__petEmotionSet = function (name, intensity, durationMs) {
    const key = String(name || "").trim().toLowerCase();
    if (!EMOTIONS[key]) { return false; }
    if (!modelRef) { pendingOp = function () { performEmotion(key, intensity, durationMs); }; return true; }
    performEmotion(key, intensity, durationMs);
    return true;
  };

  window.__petParamSet = function (mapping, durationMs) {
    if (!mapping || typeof mapping !== "object") { return false; }
    if (!modelRef) { pendingOp = function () { performRaw(mapping, durationMs); }; return true; }
    performRaw(mapping, durationMs);
    return true;
  };

  window.__petParamClear = function () {
    expireAt = 0;
    releaseAll();
  };

  window.__petParamActive = function () {
    return Object.keys(overrides).length > 0;
  };

  window.__petParamInfo = function () {
    return {
      ready: true,
      model: modelRef ? true : false,
      emotions: Object.keys(EMOTIONS),
      modelExpressions: Object.keys(window.__petExprParams || {}),
      wearables: Object.keys(window.__petAllExpressions || {}),
      active: Object.keys(overrides),
      worn: Object.keys(worn)
    };
  };

  window.__petParamPre = function (model, dt) { try {
    if (!ensureModel(model)) { return; }
    const step = clamp(Number(dt) || 0.016, 0.001, 0.05);
    const speaking = !!window.__petSpeaking;
    // 持久穿戴层：任何状态都在（拖拽/工作也不脱），瞬时情绪可临时盖住同参数
    const wornIds = Object.keys(worn);
    for (let i = 0; i < wornIds.length; i++) {
      const id = wornIds[i];
      if (speaking && MOUTH_PARAMS[id]) { continue; }
      if (overrides[id]) { continue; }   // 瞬时情绪优先
      const index = indexOf[id];
      const range = ranges[id];
      const w = worn[id];
      if (index === undefined || !range || !w) { delete worn[id]; continue; }
      if (w.cur === null || w.cur === undefined) { w.cur = model.getParameterValueByIndex(index); }
      const rate = isSwitchParam(id) ? SWITCH_APPLY_RATE : APPLY_RATE;
      w.cur = w.cur + (w.value - w.cur) * Math.min(1, step * rate);
      model.setParameterValueByIndex(index, w.cur);
    }

    const phase = (window.__petStage && window.__petStage.phase) || "idle";
    if (phase !== "idle") { return; }
    // 状态仲裁：工作/思考/等待时活动层独占，瞬时情绪让位（除非正在说话）
    const activity = (window.__petActivityState && window.__petActivityState.name) || "idle";
    // 例外：工作期间被点击的「生气」是交互反馈，必须看得见。由 pet_poke.py 开一个
    // 限时窗口（__petPokeUntil），窗口内让情绪层照常写参数与回落。
    const pokeForce = Number(window.__petPokeUntil || 0) > performance.now();
    if (!speaking && !pokeForce && (activity === "thinking" || activity === "working" || activity === "waiting")) {
      return;
    }
    const now = performance.now();
    if (expireAt && now > expireAt) { expireAt = 0; releaseAll(); }
    const ids = Object.keys(overrides);
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      // 说话期间嘴型归口型动画独占，情绪跳过所有嘴部参数
      if (speaking && MOUTH_PARAMS[id]) { continue; }
      const index = indexOf[id];
      const range = ranges[id];
      const ov = overrides[id];
      if (index === undefined || !range || !ov) { delete overrides[id]; continue; }
      // 模型上的值每帧都会被框架 loadParameters() 还原，不能拿它当逼近基准；
      // 覆盖项自己维护动画值，逐帧绝对写入（本钩子在框架写完参数之后、update() 之前）。
      if (ov.cur === null || ov.cur === undefined) { ov.cur = model.getParameterValueByIndex(index); }
      const target = ov.release ? clamp(range.def, range.min, range.max) : ov.target;
      const rate = ov.release ? (ov.releaseRate || RELEASE_RATE) : ov.rate;
      ov.cur = ov.cur + (target - ov.cur) * Math.min(1, step * rate);
      model.setParameterValueByIndex(index, ov.cur);
      if (ov.release && Math.abs(ov.cur - target) < CLEAR_EPS) { delete overrides[id]; }
    }
  } catch (err) { window.__petParamErr = String((err && err.message) ? err.message : err); } };
})();

"""
