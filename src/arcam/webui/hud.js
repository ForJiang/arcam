// HUD 取景装饰层 —— index.html:1746-1972 的浏览器侧移植（FEATURE-SPEC §2）。
// 几何与 Python 侧 arcam/hud.py 同源（focal/project 公式逐字对译
// index.html:1043-1057），供 webui 预览浮层实时绘制；出图不带 HUD。
(function () {
  "use strict";

  var K_LONG = 17.3064;   // 0.8*43.266/2（mm）
  var K_SHORT = 12.9798;  // 0.6*43.266/2（mm）

  function focalPx(W, H, feq) {
    var L = Math.max(W, H), Sd = Math.min(W, H);
    var wide = L / Sd >= 4 / 3;
    return wide ? (L / 2) * feq / K_LONG : (Sd / 2) * feq / K_SHORT;
  }

  // project(x, y, d, cam)：y 是相机系 y（世界高度 - 相机高）。Zc <= 0.05 → null。
  function project(x, y, d, cam) {
    var p = cam.pitch * Math.PI / 180, r = cam.roll * Math.PI / 180;
    var zc = -y * Math.sin(p) + d * Math.cos(p);
    if (zc <= 0.05) return null;
    var yc = -y * Math.cos(p) - d * Math.sin(p);
    var xr = x * Math.cos(r) - yc * Math.sin(r);
    var yr = x * Math.sin(r) + yc * Math.cos(r);
    var F = focalPx(cam.W, cam.H, cam.feq);
    return { u: cam.W / 2 + F * xr / zc, v: cam.H / 2 + F * yr / zc };
  }

  // tt(key, ...)：语言表取文案（i18n.js 先于本文件加载，window.arcamT 必在；
  // 缺 key 时回落裸 key —— 与 Python 侧 t() 同口径，立刻现形不静默漏翻）。
  function tt(key) {
    return window.arcamT ? window.arcamT.apply(null, arguments) : key;
  }

  // HUD 画在 canvas 上（fillText 不渲染 HTML）：语言表里 hud.* 几条带 <b> 标签
  // 是给网页 innerHTML 用的，这里先取文案再剥标签（与 Python hud.py 的
  // _strip_tags 同口径）。
  function stripTags(s) {
    return String(s).replace(/<\/?b>/g, "");
  }

  // ── place() 反投影（geometry.py:85 的 JS 对译）────────────────────
  // 画布像素 (u,v) → 世界 (x, d)：人立在地面上（Y_world=0）的解析逆。
  // 与 Python place() 逐字同式：d 夹 [0.5,20]、x 夹 [-6,6]（SPEC §5 冻结项）。
  function placeAt(u, v, cam) {
    var F = focalPx(cam.W, cam.H, cam.feq);
    if (!F) return null;
    var p = cam.pitch * Math.PI / 180, r = cam.roll * Math.PI / 180;
    var up = u - cam.W / 2, vp = v - cam.H / 2;
    var a = (up / F) * Math.cos(r) + (vp / F) * Math.sin(r);
    var b = -(up / F) * Math.sin(r) + (vp / F) * Math.cos(r);
    var den = Math.sin(p) + b * Math.cos(p);
    if (Math.abs(den) < 1e-6) return null;
    var d = cam.h * (Math.cos(p) - b * Math.sin(p)) / den;
    if (d <= 0.05) return null;
    d = Math.max(0.5, Math.min(20, d));
    var x = a * (cam.h * Math.sin(p) + d * Math.cos(p));
    x = Math.max(-6, Math.min(6, x));
    return { x: x, d: d };
  }

  // ── autoFixX（placement.py:240 的 JS 对译）──────────────────────
  // 把立绘横向吸到最近的三分线。ground：project 非线性 → 数值导数一步解
  //（dx=0.05，clamp ±20，与 Python 逐字同）；free：直接改归一化 freeP.u。
  function autoFixX(cam, subj, mode, sizePct, opts) {
    opts = opts || {};
    var targets = [1 / 3, 0.5, 2 / 3];
    var c0 = currentPlacementOf(cam, subj, mode, sizePct, opts);
    if (!c0) return null;
    var xr = c0.u / cam.W;
    var best = targets[0];
    for (var i = 1; i < targets.length; i++) {
      if (Math.abs(xr - targets[i]) < Math.abs(xr - best)) best = targets[i];
    }
    var targetU = best * cam.W;
    if (mode === "free") {
      return { freeU: Math.max(0, Math.min(1, targetU / cam.W)), snapped: best };
    }
    // ground：数值导数一步解（与 Python 的 dx=0.05 同）
    var plus = project(subj.x + 0.05, -cam.h, subj.d, cam);
    var minus = project(subj.x - 0.05, -cam.h, subj.d, cam);
    if (!plus || !minus) return { x: subj.x, snapped: best };
    var grad = (plus.u - minus.u) / 0.1;
    if (!isFinite(grad) || Math.abs(grad) < 1e-6) return { x: subj.x, snapped: best };
    return { x: Math.max(-20, Math.min(20, subj.x - (c0.u - targetU) / grad)), snapped: best };
  }

  // 当前摆位（webui 没有 arcam-core，用一份最小落位对译）
  function currentPlacementOf(cam, subj, mode, sizePct, opts) {
    if (mode === "free") {
      var h = sizePct / 100 * cam.H;
      return { u: (opts.freeP || [0.5, 0.6])[0] * cam.W, v: (opts.freeP || [0.5, 0.6])[1] * cam.H,
               w: h * (opts.charAspect || 0.6), h: h };
    }
    // ground：足底 pb 与头顶 pt 的投影（与 groundPlacement 同式）
    var k = SHOTS[opts.shot || "全身"];
    var yb = (1.0 - k) * subj.hp;
    var pb = project(subj.x, yb - cam.h, subj.d, cam);
    var pt = project(subj.x, subj.hp - cam.h, subj.d, cam);
    if (!pb || !pt) return null;
    var span = Math.hypot(pb.u - pt.u, pb.v - pt.v);
    if (span < 2) return null;
    var hh = span * (opts.charScale || 1);
    var ux = (pt.u - pb.u) / span, uy = (pt.v - pb.v) / span;
    return { u: pb.u + ux * hh / 2, v: pb.v + uy * hh / 2, w: hh * (opts.charAspect || 0.6), h: hh };
  }

  // ── drawFrame（index.html:1746-1772）─────────────────────────────
  function drawFrame(ctx, R, V, W, H) {
    if (R.x <= V.x + 0.5 && R.y <= V.y + 0.5 &&
        R.x + R.w >= V.x + V.w - 0.5 && R.y + R.h >= V.y + V.h - 0.5) return;
    ctx.save();
    ctx.fillStyle = "rgba(0,0,0,.58)";
    if (R.y > 0.5) ctx.fillRect(0, 0, W, R.y);
    if (R.y + R.h < H - 0.5) ctx.fillRect(0, R.y + R.h, W, H - (R.y + R.h));
    if (R.x > 0.5) ctx.fillRect(0, R.y, R.x, R.h);
    if (R.x + R.w < W - 0.5) ctx.fillRect(R.x + R.w, R.y, W - (R.x + R.w), R.h);
    ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.lineWidth = 2;
    ctx.strokeRect(R.x + 1, R.y + 1, R.w - 2, R.h - 2);
    ctx.strokeStyle = "rgba(255,255,255,.30)"; ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (var i = 1; i < 3; i++) {
      var x = R.x + R.w * i / 3, y = R.y + R.h * i / 3;
      ctx.moveTo(x, R.y); ctx.lineTo(x, R.y + R.h);
      ctx.moveTo(R.x, y); ctx.lineTo(R.x + R.w, y);
    }
    ctx.stroke();
    ctx.restore();
  }

  // ── gridPaths / drawGrid（index.html:1774-1824）──────────────────
  function gridPaths(cam) {
    var out = [];
    var x, i, d, q, pts, pen;
    for (x = -6; x <= 6; x++) {
      pts = []; pen = false;
      for (i = 0; i <= 300; i++) {
        d = 0.3 + (i / 300) * 26;
        q = project(x, -cam.h, d, cam);
        if (!q) { pen = false; continue; }
        pen ? pts.push(q) : (pts.push(null, q), pen = true);
      }
      out.push({ pts: pts, major: x === 0 });
    }
    for (var k = 0; k < 10; k++) {
      d = [1, 2, 3, 4, 5, 6, 8, 10, 14, 20][k];
      pts = [];
      for (i = 0; i <= 60; i++) {
        q = project(-7 + (i / 60) * 14, -cam.h, d, cam);
        if (q) pts.push(q);
      }
      out.push({ pts: pts, major: d === 5, label: d });
    }
    return out;
  }

  function drawGrid(ctx, cam) {
    var paths = gridPaths(cam);
    ctx.save(); ctx.lineCap = "round"; ctx.lineJoin = "round";
    for (var pass = 0; pass < 2; pass++) {
      for (var n = 0; n < paths.length; n++) {
        var p = paths[n];
        ctx.beginPath();
        var start = true;
        for (var i = 0; i < p.pts.length; i++) {
          var q = p.pts[i];
          if (q === null) { start = true; continue; }
          start ? ctx.moveTo(q.u, q.v) : ctx.lineTo(q.u, q.v);
          start = false;
        }
        if (pass === 0) { ctx.strokeStyle = "rgba(0,0,0,.6)"; ctx.lineWidth = p.major ? 7 : 5; }
        else { ctx.strokeStyle = p.major ? "rgba(120,255,255,1)" : "rgba(90,225,255,.9)";
               ctx.lineWidth = p.major ? 3 : 1.8; }
        ctx.stroke();
      }
    }
    for (var m = 0; m < paths.length; m++) {
      if (paths[m].label === undefined) continue;
      var lab = project(-1.4, -cam.h, paths[m].label, cam);
      if (!lab || lab.v > cam.H || lab.v < 0) continue;
      ctx.font = "bold 20px sans-serif";
      ctx.lineWidth = 4; ctx.strokeStyle = "rgba(0,0,0,.75)";
      ctx.strokeText(paths[m].label + "m", lab.u + 8, lab.v - 8);
      ctx.fillStyle = "rgba(120,255,255,1)";
      ctx.fillText(paths[m].label + "m", lab.u + 8, lab.v - 8);
    }
    ctx.restore();
  }

  // ── drawHorizon（index.html:1826-1837）───────────────────────────
  function drawHorizon(ctx, cam, label) {
    var F = focalPx(cam.W, cam.H, cam.feq);
    var p = cam.pitch * Math.PI / 180, r = cam.roll * Math.PI / 180;
    var tp = Math.tan(p);
    var pt = function (t) {
      return { u: cam.W / 2 + F * (t * Math.cos(r) + tp * Math.sin(r)),
               v: cam.H / 2 + F * (t * Math.sin(r) - tp * Math.cos(r)) };
    };
    var a = pt(-12), b = pt(12), lab = pt(-0.22);
    ctx.save();
    ctx.strokeStyle = "rgba(255,120,120,.8)"; ctx.setLineDash([12, 8]); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(a.u, a.v); ctx.lineTo(b.u, b.v); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,120,120,.95)"; ctx.font = "22px sans-serif";
    ctx.fillText(label || tt("horizon"), lab.u + 10, lab.v - 10);
    ctx.restore();
  }

  // ── drawFigure（index.html:1839-1876）────────────────────────────
  function drawFigure(ctx, cam, subj) {
    var Hp = subj.hp;
    var P = function (lx, ly) { return project(subj.x + lx, ly - cam.h, subj.d, cam); };
    var TOP = 1.00, HEAD = 0.9335, CHIN = 0.867, NECK = 0.855, SHLD = 0.80,
        WAIST = 0.61, HIP = 0.53, KNEE = 0.28, ANKLE = 0.045, SOLE = 0.0;
    var SW = 0.105 * Hp, HW = 0.072 * Hp, FW = 0.048 * Hp;
    var J = {
      soleL: P(-FW, SOLE), soleR: P(FW, SOLE),
      ankleL: P(-FW, ANKLE * Hp), ankleR: P(FW, ANKLE * Hp),
      kneeL: P(-FW * 0.9, KNEE * Hp), kneeR: P(FW * 0.9, KNEE * Hp),
      hipL: P(-HW, HIP * Hp), hipR: P(HW, HIP * Hp), waist: P(0, WAIST * Hp),
      shldL: P(-SW, SHLD * Hp), shldR: P(SW, SHLD * Hp), neck: P(0, NECK * Hp),
      chin: P(0, CHIN * Hp), head: P(0, HEAD * Hp), top: P(0, TOP * Hp),
      elbowL: P(-SW * 1.18, 0.635 * Hp), elbowR: P(SW * 1.18, 0.635 * Hp),
      wristL: P(-SW * 1.30, 0.445 * Hp), wristR: P(SW * 1.30, 0.445 * Hp)
    };
    var seg = function (a, b, w, c) {
      if (!a || !b) return;
      ctx.strokeStyle = c; ctx.lineWidth = w; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(a.u, a.v); ctx.lineTo(b.u, b.v); ctx.stroke();
    };
    ctx.save();
    var B = "rgba(255,212,121,.95)", L = "rgba(255,212,121,.75)";
    seg(J.hipL, J.hipR, 7, B); seg(J.shldL, J.shldR, 7, B); seg(J.waist, J.neck, 9, B);
    seg(J.hipL, J.kneeL, 9, L); seg(J.kneeL, J.ankleL, 7, L); seg(J.ankleL, J.soleL, 5, L);
    seg(J.hipR, J.kneeR, 9, L); seg(J.kneeR, J.ankleR, 7, L); seg(J.ankleR, J.soleR, 5, L);
    seg(J.shldL, J.elbowL, 7, L); seg(J.elbowL, J.wristL, 6, L);
    seg(J.shldR, J.elbowR, 7, L); seg(J.elbowR, J.wristR, 6, L);
    seg(J.neck, J.chin, 6, B);
    if (J.head && J.top && J.chin) {
      var ry = Math.abs(J.top.v - J.chin.v) / 2;
      ctx.strokeStyle = B; ctx.lineWidth = 4;
      ctx.beginPath(); ctx.ellipse(J.head.u, J.head.v, ry * 0.72, ry, 0, 0, Math.PI * 2); ctx.stroke();
    }
    for (var i = 0; i < 2; i++) {
      var s = i ? J.soleR : J.soleL;
      if (!s) continue;
      ctx.fillStyle = "rgba(120,255,180,.95)";
      ctx.beginPath(); ctx.arc(s.u, s.v, 7, 0, Math.PI * 2); ctx.fill();
    }
    ctx.restore();
  }

  // ── compositionHints（index.html:1890-1937，11 条全并列）─────────
  // 景别表全文件只此一份（currentPlacementOf 共用；webui 没有 arcam-core，
  // docs 侧那份改为从 arcam-core.js import —— 两边都不许再出现第二份）
  var SHOTS = { "全身": 1.00, "膝上": 0.72, "腰上": 0.47, "胸上": 0.28, "头肩": 0.20 };

  // 提示文案走语言表 hints.*（中文列与 Python hud.py composition_hints 的现文案
  // 逐字一致；SHOTS 的键是景别参数值，不翻）。
  function compositionHints(cam, placement, W, H, shot, mode) {
    if (W < 8 || H < 8) return [];
    var F = focalPx(cam.W, cam.H, cam.feq);
    var hv = H / 2 - F * Math.tan(cam.pitch * Math.PI / 180);
    var hr = hv / H;
    var out = [];
    if (hr >= 0.42 && hr <= 0.58) out.push(tt("hints.horizonMid"));
    if (cam.pitch > 40) out.push(tt("hints.pitchHigh", cam.pitch.toFixed(0)));
    if (cam.pitch < -35) out.push(tt("hints.pitchLow"));
    if (placement && placement.w > 0) {
      var hw = placement.w / 2, hh = placement.h / 2;
      if (placement.u + hw < 0 || placement.u - hw > W ||
          placement.v + hh < 0 || placement.v - hh > H) {
        out.push(tt("hints.outOfFrame"));
        return out;
      }
      var xr = placement.u / W;
      var near = Math.min(Math.abs(xr - 1 / 3), Math.abs(xr - 2 / 3), Math.abs(xr - 0.5));
      if (near > 0.11) out.push(tt("hints.thirds"));
      var fill = placement.h / H;
      if (fill > 0.92) out.push(tt("hints.tooBig"));
      if (fill < 0.10) out.push(tt("hints.tooSmall"));
      var cover = SHOTS[shot] || 1;
      if (cover >= 0.95 && mode === "ground") {
        var cutV = placement.v + placement.h / 2;
        if (cutV < hv - H * 0.03) out.push(tt("hints.floating"));
      }
    }
    return out;
  }

  // ── hud 读数（index.html:1957-1972）─────────────────────────────
  // 四行读数走语言表 hud.*（与 Python hud.py 的 t("hud.*") 同键同名）；表里带
  // <b> 标签，canvas 侧先剥。warns 平行数组记录警告行（黄）—— 不能再用中文
  // 前缀判色（切英文后前缀就对不上了）。
  function drawHud(ctx, cam, subj, hasChar, placement, shot, mode) {
    var a = project(subj.x, -cam.h, subj.d, cam);
    var b = project(subj.x, subj.hp - cam.h, subj.d, cam);
    var px = (a && b) ? Math.abs(a.v - b.v) : 0;
    var F = focalPx(cam.W, cam.H, cam.feq);
    var lines = [], warns = [];
    lines.push(stripTags(tt("hud.line1", cam.pitch.toFixed(1), cam.roll.toFixed(1))));
    warns.push(false);
    lines.push(stripTags(tt("hud.line2", cam.W, cam.H, F.toFixed(0))));
    warns.push(false);
    if (hasChar) {
      if (a && b) {
        lines.push(stripTags(tt("hud.person", (px / cam.H * 100).toFixed(1))));
        warns.push(false);
      } else {
        lines.push(stripTags(tt("hud.behind")));
        warns.push(true);
      }
    } else {
      lines.push(stripTags(tt("hud.nochar")));
      warns.push(true);
    }
    var hints = compositionHints(cam, placement, cam.W, cam.H, shot, mode);
    if (hints.length) { lines.push("⚠ " + hints.join("　·　")); warns.push(true); }
    ctx.save();
    ctx.font = "13px sans-serif";
    ctx.textBaseline = "top";
    for (var i = 0; i < lines.length; i++) {
      var y = 8 + i * 17;
      ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.75)";
      ctx.strokeText(lines[i], 8, y);
      ctx.fillStyle = warns[i] ? "#ffd54f" : "#fff";
      ctx.fillText(lines[i], 8, y);
    }
    ctx.restore();
    return hints;
  }

  window.arcamHud = {
    focalPx: focalPx,
    project: project,
    placeAt: placeAt,
    autoFixX: autoFixX,
    currentPlacementOf: currentPlacementOf,
    drawFrame: drawFrame,
    drawGrid: drawGrid,
    drawHorizon: drawHorizon,
    drawFigure: drawFigure,
    compositionHints: compositionHints,
    drawHud: drawHud,
    gridPaths: gridPaths
  };
})();
