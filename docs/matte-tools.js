// 立绘修图工具链 —— cutout.js 非 AI 线的 JS 移植（FEATURE-SPEC §1）。
// 与 src/arcam/matte_tools.py 逐行对译（同一份算法两个语言）；docs 侧没有
// 后端，纯色底抠图/裁边/柔边/微糊全在浏览器里跑。
(function () {
  "use strict";

  var ALPHA_MIN = 8 / 255;      // contentBounds 的 alphaMin 默认 8（字节口径）
  var CUT_RATIO_MIN = 0.05;
  var CUT_RATIO_MAX = 0.85;
  var CUT_TOL = 32.0;           // 字节口径（与 Python 侧一致，入口处 /255）

  function boxBlur(a, w, h, r) {
    // (2r+1)² 盒式均值，越界不计入分母（与 Python _box_blur 同口径）
    var out = new Float64Array(a.length);
    for (var y = 0; y < h; y++) {
      for (var x = 0; x < w; x++) {
        var s = 0, n = 0;
        for (var dy = -r; dy <= r; dy++) {
          var yy = y + dy;
          if (yy < 0 || yy >= h) continue;
          for (var dx = -r; dx <= r; dx++) {
            var xx = x + dx;
            if (xx < 0 || xx >= w) continue;
            s += a[yy * w + xx]; n++;
          }
        }
        out[y * w + x] = n > 0 ? s / n : a[y * w + x];
      }
    }
    return out;
  }

  // 原版 softenEdge 的 feather 内核（cutout.js:805-817 逐字对译）：
  // 可分离 3 抽头均值、横竖各一遍、每遍 Math.round、**字节域**。
  // ⚠ 与 boxBlur 不是同一个核（等效 5×5 三角形 vs 5×5 盒式）；且 fe 只当
  //   布尔用——半径硬编码 1，feather 值不是半径。a 为字节值 Float64Array。
  function separable3tapMean(a, w, h) {
    var cur = a;
    for (var pass = 0; pass < 2; pass++) {
      var horiz = pass === 0;
      var out = new Float64Array(a.length);
      for (var y = 0; y < h; y++) {
        for (var x = 0; x < w; x++) {
          var s = 0, n = 0;
          for (var k = -1; k <= 1; k++) {
            var xx = horiz ? x + k : x, yy = horiz ? y : y + k;
            if (xx < 0 || yy < 0 || xx >= w || yy >= h) continue;
            s += cur[yy * w + xx]; n++;
          }
          out[y * w + x] = Math.round(s / n);   // 每遍取整（原版同）
        }
      }
      cur = out;
    }
    return cur;
  }

  // ── cutSolidBg（cutout.js:16-98 / matte_tools.py:_flood_solid）──
  // 返回 { canvas, ratio }；不是纯色底 → { canvas: null, ratio: 0 }。
  function cutSolidBg(canvas, opt) {
    var o = Object.assign({ tol: CUT_TOL, feather: 1.2 }, opt || {});
    var w = canvas.width, h = canvas.height;
    if (!w || !h) return { canvas: null, ratio: 0 };
    var im = canvas.getContext("2d", { willReadFrequently: true }).getImageData(0, 0, w, h);
    var px = im.data;
    var tol01 = o.tol / 255.0;

    var cs = [[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1]].map(function (p) {
      var i = (p[1] * w + p[0]) * 4;
      return [px[i], px[i + 1], px[i + 2]];                 // 0-255 字节
    });
    var alphas = [[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1]].map(function (p) {
      return px[(p[1] * w + p[0]) * 4 + 3];
    });
    if (alphas.every(function (a) { return a < 10; })) return { canvas: null, ratio: 0 };
    var avg = [0, 1, 2].map(function (c) {
      return (cs[0][c] + cs[1][c] + cs[2][c] + cs[3][c]) / 4;
    });
    var dev = 0;
    for (var i = 0; i < cs.length; i++) for (var k = 0; k < 3; k++) {
      dev = Math.max(dev, Math.abs(cs[i][k] - avg[k]));
    }
    if (dev / 255.0 > tol01) return { canvas: null, ratio: 0 };
    var lim = tol01 * 3 * 255;                               // 字节空间的 lim

    var seen = new Uint8Array(w * h);
    var isBg = new Uint8Array(w * h);
    var stack = [];
    var x, y;
    for (x = 0; x < w; x++) { stack.push(x); stack.push(x + (h - 1) * w); }
    for (y = 1; y < h - 1; y++) { stack.push(y * w); stack.push(y * w + w - 1); }
    var removed = 0;
    while (stack.length) {
      var p = stack.pop();
      if (seen[p]) continue;
      seen[p] = 1;
      var i4 = p * 4;
      var d = Math.abs(px[i4] - avg[0]) + Math.abs(px[i4 + 1] - avg[1]) + Math.abs(px[i4 + 2] - avg[2]);
      if (d > lim) continue;
      isBg[p] = 1;
      removed++;
      x = p % w; y = (p / w) | 0;
      if (x > 0 && !seen[p - 1]) stack.push(p - 1);
      if (x < w - 1 && !seen[p + 1]) stack.push(p + 1);
      if (y > 0 && !seen[p - w]) stack.push(p - w);
      if (y < h - 1 && !seen[p + w]) stack.push(p + w);
    }
    if (!removed) return { canvas: null, ratio: 0 };

    var a = new Float64Array(w * h);
    for (var q = 0; q < w * h; q++) a[q] = isBg[q] ? 0 : 255;
    if (o.feather > 0) {
      // 盒式均值 + (s/n)|0 字节截断（cutout.js:88，v1.5 与 Python flood 侧对齐）
      var bl = boxBlur(a, w, h, Math.max(1, Math.round(o.feather)));
      for (var t2 = 0; t2 < bl.length; t2++) bl[t2] = Math.floor(bl[t2]);
      a = bl;
    }
    for (var r2 = 0; r2 < w * h; r2++) px[r2 * 4 + 3] = a[r2];
    canvas.getContext("2d").putImageData(im, 0, 0);
    return { canvas: canvas, ratio: removed / (w * h) };
  }

  // ── contentBounds / autoTrim（cutout.js:246-276）──
  function contentBounds(canvas, alphaMin) {
    var th = alphaMin == null ? ALPHA_MIN : alphaMin;
    var w = canvas.width, h = canvas.height;
    var px = canvas.getContext("2d", { willReadFrequently: true }).getImageData(0, 0, w, h).data;
    var x0 = w, y0 = h, x1 = -1, y1 = -1;
    for (var y = 0; y < h; y++) {
      for (var x = 0; x < w; x++) {
        if (px[(y * w + x) * 4 + 3] >= th * 255) {
          if (x < x0) x0 = x;
          if (x > x1) x1 = x;
          if (y < y0) y0 = y;
          if (y > y1) y1 = y;
        }
      }
    }
    if (x1 < 0) return null;
    return { x: x0, y: y0, w: x1 - x0 + 1, h: y1 - y0 + 1 };
  }

  function cropToCanvas(canvas, r) {
    var W = canvas.width, H = canvas.height;
    var x = Math.max(0, Math.min(W - 1, Math.round(r.x)));
    var y = Math.max(0, Math.min(H - 1, Math.round(r.y)));
    var w = Math.max(1, Math.min(W - x, Math.round(r.w)));
    var h = Math.max(1, Math.min(H - y, Math.round(r.h)));
    var c = document.createElement("canvas");
    c.width = w; c.height = h;
    c.getContext("2d").drawImage(canvas, -x, -y);
    return { canvas: c, rect: { x: x, y: y, w: w, h: h } };
  }

  function autoTrim(canvas) {
    var b = contentBounds(canvas);
    if (!b) return null;
    var r = cropToCanvas(canvas, b);
    if (!r || r.canvas.width < 4 || r.canvas.height < 4) return null;
    return r;
  }

  // ── softenEdge / lensBlur（index.html:2384-2423）──
  function softenEdge(canvas, opt) {
    var o = Object.assign({ erode: 1, feather: 2 }, opt || {});
    var w = canvas.width, h = canvas.height;
    var im = canvas.getContext("2d", { willReadFrequently: true }).getImageData(0, 0, w, h);
    var px = im.data;
    var a = new Float64Array(w * h);
    for (var i = 0; i < w * h; i++) a[i] = px[i * 4 + 3];
    if (o.erode > 0) {
      var r = o.erode;
      var out = new Float64Array(a);
      for (var y = 0; y < h; y++) for (var x = 0; x < w; x++) {
        var mn = a[y * w + x];
        for (var dy = -r; dy <= r; dy++) for (var dx = -r; dx <= r; dx++) {
          var xx = x + dx, yy = y + dy;
          var v = (xx < 0 || yy < 0 || xx >= w || yy >= h) ? a[y * w + x] : a[yy * w + xx];
          if (v < mn) mn = v;
        }
        out[y * w + x] = mn;
      }
      a = out;
    }
    // ⚠ feather 走可分离 3 抽头两遍（v1.5 与 Python soften_edge 同内核），
    //   不再是盒式均值；a 此时即字节值（alpha 通道 0..255），无需换算。
    if (o.feather > 0) a = separable3tapMean(a, w, h);
    for (var i2 = 0; i2 < w * h; i2++) px[i2 * 4 + 3] = a[i2];
    canvas.getContext("2d").putImageData(im, 0, 0);
    return { canvas: canvas, changed: true };
  }

  function lensBlur(canvas, k) {
    var w = canvas.width, h = canvas.height;
    if (w < 8 || h < 8) return canvas;
    var tw = Math.max(2, Math.round(w * k)), th = Math.max(2, Math.round(h * k));
    var t = document.createElement("canvas");
    t.width = tw; t.height = th;
    var tg = t.getContext("2d");
    tg.imageSmoothingQuality = "high";
    tg.drawImage(canvas, 0, 0, tw, th);
    var g = canvas.getContext("2d");
    g.imageSmoothingQuality = "high";
    g.clearRect(0, 0, w, h);
    g.drawImage(t, 0, 0, w, h);
    return canvas;
  }

  // ── autoPrepareChar（index.html:2400-2440）──
  function autoPrepareChar(canvas) {
    var out = { cut: null, trimmed: false, half: false, actions: [] };
    if (!canvas) return out;
    var before = copyOf(canvas);
    try {
      var c = copyOf(canvas);
      var r = cutSolidBg(c, { tol: CUT_TOL, feather: 1.2 });
      if (r.canvas && r.ratio >= CUT_RATIO_MIN && r.ratio <= CUT_RATIO_MAX) {
        canvas = c; out.cut = r.ratio; out.actions.push("note.autoCut");
      }
    } catch (e) {}
    try {
      var b = autoTrim(canvas);
      if (b) { canvas = b.canvas; out.trimmed = true; out.actions.push("note.autoTrim"); }
    } catch (e) {}
    try {
      if (softenEdge(canvas, { erode: 1, feather: 2 })) out.actions.push("note.autoSoft");
    } catch (e) {}
    try {
      lensBlur(canvas, 0.62);
      out.actions.push("note.autoBlur");
    } catch (e) {}
    out.half = isHalfBody(canvas);
    // 兜底：动过手且内容 bbox 任一边 < 8 → 回退
    if (out.cut !== null || out.trimmed) {
      var bb = contentBounds(canvas);
      if (!bb || bb.w < 8 || bb.h < 8) {
        return { cut: null, trimmed: false, half: false, actions: [], canvas: before };
      }
    }
    out.canvas = canvas;
    return out;
  }

  // ── 半身判定（index.html:2428-2434）：倒数第二行、alpha>200、>30% ──
  // 行位口径与 Python is_half_body 逐字同：h<1 才无行可读，h==1 退化读唯一的第 0 行
  // （原版 Math.max(0, h-2) 同样落在第 0 行）。
  function isHalfBody(canvas) {
    var w = canvas.width, h = canvas.height;
    if (h < 1 || w < 1) return false;
    var row = canvas.getContext("2d", { willReadFrequently: true })
      .getImageData(0, Math.max(0, h - 2), w, 1).data;
    var solid = 0;
    for (var x = 0; x < w; x++) if (row[x * 4 + 3] > 200) solid++;
    return (solid / w) > 0.3;
  }

  function copyOf(canvas) {
    var c = document.createElement("canvas");
    c.width = canvas.width; c.height = canvas.height;
    c.getContext("2d").drawImage(canvas, 0, 0);
    return c;
  }

  window.arcamMatteTools = {
    cutSolidBg: cutSolidBg,
    contentBounds: contentBounds,
    autoTrim: autoTrim,
    softenEdge: softenEdge,
    lensBlur: lensBlur,
    autoPrepareChar: autoPrepareChar,
    isHalfBody: isHalfBody,
    copyOf: copyOf
  };
})();
