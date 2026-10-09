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
    ctx.fillText(label || "地平线", lab.u + 10, lab.v - 10);
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
  var SHOTS = { "全身": 1.00, "膝上": 0.72, "腰上": 0.47, "胸上": 0.28, "头肩": 0.20 };

  function compositionHints(cam, placement, W, H, shot, mode) {
    if (W < 8 || H < 8) return [];
    var F = focalPx(cam.W, cam.H, cam.feq);
    var hv = H / 2 - F * Math.tan(cam.pitch * Math.PI / 180);
    var hr = hv / H;
    var out = [];
    if (hr >= 0.42 && hr <= 0.58) out.push("地平线在正中，抬一点或压一点");
    if (cam.pitch > 40) out.push("俯角 " + cam.pitch.toFixed(0) + "°，人物会被压扁");
    if (cam.pitch < -35) out.push("仰角偏大，人物会被拉长");
    if (placement && placement.w > 0) {
      var hw = placement.w / 2, hh = placement.h / 2;
      if (placement.u + hw < 0 || placement.u - hw > W ||
          placement.v + hh < 0 || placement.v - hh > H) {
        out.push("人物出画了 —— 抬/压一点镜头，或把距离调远");
        return out;
      }
      var xr = placement.u / W;
      var near = Math.min(Math.abs(xr - 1 / 3), Math.abs(xr - 2 / 3), Math.abs(xr - 0.5));
      if (near > 0.11) out.push("角色挪到三分线上更稳");
      var fill = placement.h / H;
      if (fill > 0.92) out.push("角色快顶满画面");
      if (fill < 0.10) out.push("角色偏小");
      var cover = SHOTS[shot] || 1;
      if (cover >= 0.95 && mode === "ground") {
        var cutV = placement.v + placement.h / 2;
        if (cutV < hv - H * 0.03) out.push("脚在地平线以上，浮空了");
      }
    }
    return out;
  }

  // ── hud 读数（index.html:1957-1972）─────────────────────────────
  function drawHud(ctx, cam, subj, hasChar, placement, shot, mode) {
    var a = project(subj.x, -cam.h, subj.d, cam);
    var b = project(subj.x, subj.hp - cam.h, subj.d, cam);
    var px = (a && b) ? Math.abs(a.v - b.v) : 0;
    var F = focalPx(cam.W, cam.H, cam.feq);
    var lines = [];
    lines.push("俯角 " + cam.pitch.toFixed(1) + "°　滚转 " + cam.roll.toFixed(1) + "°");
    lines.push("预览 " + cam.W + "×" + cam.H + "　f=" + F.toFixed(0) + "px");
    if (hasChar) {
      lines.push((a && b)
        ? "人在画面高 " + (px / cam.H * 100).toFixed(1) + "%"
        : "人物在相机后方");
    } else {
      lines.push("还没选立绘");
    }
    var hints = compositionHints(cam, placement, cam.W, cam.H, shot, mode);
    if (hints.length) lines.push("⚠ " + hints.join("　·　"));
    ctx.save();
    ctx.font = "13px sans-serif";
    ctx.textBaseline = "top";
    for (var i = 0; i < lines.length; i++) {
      var y = 8 + i * 17;
      ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.75)";
      ctx.strokeText(lines[i], 8, y);
      ctx.fillStyle = (lines[i].indexOf("⚠") === 0 || lines[i].indexOf("人物在相机后方") === 0 ||
                       lines[i].indexOf("还没选立绘") === 0) ? "#ffd54f" : "#fff";
      ctx.fillText(lines[i], 8, y);
    }
    ctx.restore();
    return hints;
  }

  window.arcamHud = {
    focalPx: focalPx,
    project: project,
    drawFrame: drawFrame,
    drawGrid: drawGrid,
    drawHorizon: drawHorizon,
    drawFigure: drawFigure,
    compositionHints: compositionHints,
    drawHud: drawHud,
    gridPaths: gridPaths
  };
})();
