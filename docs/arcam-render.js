/* arcam-render.js — 立绘落位合成与 scene.json 配方（canvas 侧，五个导出）。
 *
 * 逐字移植 src/arcam/sprite.py（contact_shadow_layer / composite_sprite）、
 * compose.py（composeShot 六步）与 scene.py（buildScene）。原版项目本就是 JS，
 * 本文件即 SITE-SPEC §0 说的「回到本形」：合成直接在 canvas 上做，走浏览器原生
 * 的 source-over 与 imageSmoothingQuality='high' —— Python 侧为此专门写的
 * 黑白底反解 alpha / 逆映射双线性 warp 那一层绕行，在这里不需要。
 *
 * 六步（compose.py:206 _engine，原版 index.html:3673-3701）：
 *   ① rect = frameRect(frame, W, H)
 *   ② 铺帧：photoCanvas 非空 → drawImage 整屏铺满（原版 g.drawImage(src,0,0,W,H)；
 *      照片与画布同尺寸时即 1:1  blit，cv 像素 = 视频像素）
 *   ③ 采光 —— **v1 不做**：light / tone / bakeLit / occlusion 全不在本期范围，
 *      params.light 与 params.tone 接受但忽略，返回值的 light 恒 null
 *   ④ 摆位（core.currentPlacement 按 mode 分派）→ translate(u,v) → rotate(rot)
 *      → 先铺接触阴影 → 再 drawImage 立绘（锚点 = 图心，占局部 (-w/2,-h/2,w,h)）
 *   ⑤ 整数 off 画布裁切：off 尺寸 max(1,jsRound(R.w))×max(1,jsRound(R.h))，
 *      drawImage(full, -jsRound(R.x), -jsRound(R.y))；越界目标像素保持透明黑
 *      （对应导出 JPEG 的黑边）
 *   ⑥ 返回 { shot, rect, scene, light: null }
 *
 * params（composeShot / composeCharLayer / buildScene 共用一个参数袋，全部可选，
 * 缺省同 Python 关键字参数）：
 *   frame     画幅中文 key（默认 '满幅' = compose_char_layer 的 DEFAULT_FRAME；
 *             别名 frameKey 也收）
 *   mode      'ground' 贴地 / 'free' 自由（默认 'ground'；其余值抛错，同 _engine）
 *   shot      景别中文 key（默认 '全身'）
 *   charScale 贴地模式大小系数（默认 1）
 *   rotBasis  'world' 跟随世界竖直 / 'frame' 永远画面竖直（默认 'world'；
 *             两个模式都生效 —— 原版 currentPlacement 分派就是这么分的，
 *             placement.py:155-174）
 *   sizePct   自由模式立绘占画面高度百分比（默认 60）
 *   edgeBar   框条吸附编号 0–4（自由模式生效；吸附目标 rect = 本遍的 frameRect，
 *             即原版 snapRect = 取景框 ∩ 可见区在离线退化下的结果：可见区=整块画布）
 *   ambient   环境光 [r,g,b] —— 只影响接触阴影颜色（默认 null → 原版 LIGHT 初值
 *             0.5，rb=1）
 *   charName  立绘文件名，写 scene.character.name（默认 'character.png'）
 *   char      buildScene 专用：立绘图（鸭子类型，取 .width/.height 写
 *             character.image）；null → character 记 null（同 build_scene 判据）
 *   light / tone  **接受但忽略**（v1 无采光、无色调匹配；README「未移植」注明）
 *
 * 与 Python 侧的已知差异（均为「回到本形」的有意选择，非 bug）：
 *   · 立绘层路径：Python 的入参/出参只有 RGB 画布，没有透明底可铺，于是用黑底 +
 *     白底两次合成反解累加 alpha、再反预乘（compose.py:277-286）；这里直接往透明
 *     canvas 上 source-over，读出来就是 RGBA —— 同一套 source-over 混合语义，
 *     8-bit 量化点位与 Python「最后一次性舍入」略有先后之差。
 *   · compose_char_layer 的裁切框取 params.frame（原版 buildCharCanvas =
 *     composeShot(null, …) 用的就是当前 S.frame，层与出图同框才能严丝合缝地盖在
 *     shot.jpg 上）；Python 侧该入口写死 DEFAULT_FRAME 是它的函数签名没有画幅参数
 *     所致（compose.py:56-57）；换画幅请传 params.frame。
 *   · 铺帧是 drawImage 五参语义（与原版一致）：同尺寸 1:1，不一致拉伸铺满；
 *     Python 离线侧对尺寸不符改为抛 ValueError。
 *   · 阴影层 1×1 是 contactShadowAlpha 的「不画」标记（全透明，对应 Python 的
 *     zeros((1,1,4))）：drawSprite 直接略过该次 no-op drawImage；其余尺寸一律画
 *     （原版也无条件画）。Python 的跳过判据是「层内 alpha 最大值 > 0」（要读像素），
 *     这里用尺寸近似，视觉等价。
 *   · 阴影色量化 q(v) = js_round(clampN(v,0,1)*255/6)*6：numpy uint8 是**回绕**
 *     （258 → 2），Uint8ClampedArray 是钳位（258 → 255）。按 Python 逐字回绕
 *     （% 256），否则极亮/极暖的环境色下阴影色会对不上。
 *   · jsFixed 不直接用 toFixed：toFixed 按二进制真值舍入，当最短回绕串恰为 .5
 *     结尾的 tie 而真值略低时会向下（(2.675).toFixed(2)='2.67'，Python 的
 *     Decimal(repr(v)) 给 2.68）；scene.json 坐标一旦偶合该 tie 就差半个最小位。
 *     改为对 String(v)（与 Python repr 同一「最短回绕」语义）做十进制定点
 *     HALF_UP（半远离零）量化，与 numeric.js_to_fixed 逐位一致。
 */

import { frameRect, currentPlacement, focalPx, project } from './arcam-core.js';

/* ── 接触阴影常量（sprite.py:39-48，index.html:3044-3049）────────────────── */
const SH_BAND = 0.085;    // 取立绘底部 8.5% 当「落地轮廓」
const SH_THICK = 0.24;    // 阴影厚度 ÷ 立绘宽（地面被斜着看，压得很扁）
const SH_ALPHA = 0.42;    // 最深处浓度
const SH_PEAK = 0.30;     // 纵向峰值位置（0 = 上边缘，1 = 下边缘）
const SH_SPREAD = 0.26;   // 纵向高斯宽度
const SH_DARK = 0.55;     // 阴影亮度 = 环境亮度 × 这个系数
const LUM_W = [0.2126, 0.7152, 0.0722];
const LIGHT_DEFAULT = [0.5, 0.5, 0.5];   // index.html:1132 LIGHT 初值（无采样时的回退）

/* scene.json 配方常量（scene.py:31-38）。 */
const SCENE_VERSION = 1;
const SCENE_IMAGE_FILE = 'shot.jpg';

/* 参数缺省（compose.py:57 的 DEFAULT_FRAME；其余同 Python 关键字参数缺省）。 */
const DEFAULT_FRAME = '满幅';
const DEFAULT_SHOT = '全身';
const DEFAULT_CHAR_SCALE = 1.0;
const DEFAULT_CHAR_NAME = 'character.png';

/** JS Math.round 的显式写法（SITE-SPEC §0：半向 +∞，= Python js_round）。 */
const jsRound = (v) => Math.floor(v + 0.5);

/** clampN(v, a, b)：先夹范围，两端不排序（numeric.py:26）。 */
const clampN = (v, lo, hi) => (v < lo ? lo : (v > hi ? hi : v));

/** 数值型参数兜底：undefined/null/''/NaN → 缺省值，否则 Number()。 */
const numOr = (v, d) => (v === undefined || v === null || v === '' || Number.isNaN(Number(v))
  ? d : Number(v));

/* ─────────────────────────── canvas 小工具 ─────────────────────────── */

/** 新建画布（浏览器唯一 DOM 入口，集中在这一处，便于 Node 下注入 document stub）。 */
function makeCanvas(w, h) {
  if (typeof document === 'undefined' || typeof document.createElement !== 'function') {
    throw new Error('arcam-render 需要 DOM（document.createElement）；Node 下请先注入 document stub');
  }
  const cv = document.createElement('canvas');
  cv.width = w;
  cv.height = h;
  return cv;
}

/** 取 2d context 并统一滤镜口径：缩放一律 imageSmoothingQuality='high'（与原版一致）。 */
function ctx2d(canvas) {
  const g = canvas.getContext('2d');
  if (g === null || g === undefined) {
    throw new Error('拿不到 2d context（画布被占用或环境不支持）');
  }
  g.imageSmoothingEnabled = true;
  g.imageSmoothingQuality = 'high';
  return g;
}

/** 读图的 RGBA 像素：canvas 直接 getImageData；HTMLImageElement/ImageBitmap 借临时画布。 */
function pixelsOf(img) {
  if (img === null || img === undefined) return null;
  const w = Math.trunc(Number(img.width));
  const h = Math.trunc(Number(img.height));
  if (!(w > 0) || !(h > 0)) return null;
  if (typeof img.getContext === 'function') {
    const g = img.getContext('2d');
    if (g !== null && g !== undefined && typeof g.getImageData === 'function') {
      const id = g.getImageData(0, 0, w, h);
      return { data: id.data, width: w, height: h };
    }
  }
  const cv = makeCanvas(w, h);
  const g = ctx2d(cv);
  g.drawImage(img, 0, 0, w, h);
  const id = g.getImageData(0, 0, w, h);
  return { data: id.data, width: w, height: h };
}

/* ─────────────────────── toFixed 的 Python 等价 ─────────────────────── */

/** 十进制数字串 +1（保留前导零长度：'0000' → '0001'，'999' → '1000'）。 */
function incDigits(str) {
  const a = str.split('');
  let i = a.length - 1;
  for (; i >= 0; i--) {
    if (a[i] === '9') { a[i] = '0'; continue; }
    a[i] = String.fromCharCode(a[i].charCodeAt(0) + 1);
    break;
  }
  if (i < 0) a.unshift('1');
  return a.join('');
}

/**
 * Number.prototype.toFixed(digits) 的数值版 —— 逐字对齐 Python numeric.js_to_fixed：
 * 对「最短回绕十进制串」（JS String(v) 与 Python repr(v) 同一语义）做 Decimal
 * ROUND_HALF_UP（半远离零）量化，返回 Number。
 */
function jsFixed(value, digits) {
  if (typeof value !== 'number' || !isFinite(value)) return value;
  const neg = value < 0 || (value === 0 && 1 / value === -Infinity);
  let s = (neg ? -value : value).toString();
  let exp = 0;
  const ei = s.indexOf('e');
  if (ei >= 0) { exp = parseInt(s.slice(ei + 1), 10); s = s.slice(0, ei); }
  let ip = s, fp = '';
  const dot = s.indexOf('.');
  if (dot >= 0) { ip = s.slice(0, dot); fp = s.slice(dot + 1); }
  const all = ip + fp;                 // 全部十进制位（可带前导零）
  const pointPos = ip.length + exp;    // 小数点在 all 里的逻辑位置 = 整数位数
  const cut = pointPos + digits;       // 保留 digits 位小数后的切点
  let scaled;                          // 舍入后的「×10^digits 整数化」数字串
  if (cut <= 0) {
    scaled = '0';
  } else {
    const head = all.slice(0, cut).padEnd(cut, '0');
    const tail = all.slice(cut);
    scaled = (tail.length > 0 && tail[0] >= '5') ? incDigits(head) : head;
  }
  let out;
  if (digits === 0) {
    out = scaled;
  } else if (scaled.length <= digits) {
    out = '0.' + scaled.padStart(digits, '0');
  } else {
    out = scaled.slice(0, scaled.length - digits) + '.' + scaled.slice(scaled.length - digits);
  }
  return Number((neg ? '-' : '') + out);
}

/* ───────────────────────── 接触阴影（sprite.py） ───────────────────────── */

/** 阴影颜色 = 环境色暗化版（_shadow_color，index.html:3133-3138）。 */
function shadowColor(ambient) {
  let lum, rb;
  if (ambient === null || ambient === undefined) {
    lum = LIGHT_DEFAULT[0];
    rb = 1.0;                          // 无采样：按原版 LIGHT 初值 0.5 回退（rb=1）
  } else {
    const envR = Number(ambient[0]);
    const envG = Number(ambient[1]);
    const envB = Number(ambient[2]);
    lum = LUM_W[0] * envR + LUM_W[1] * envG + LUM_W[2] * envB;
    rb = Math.sqrt(Math.max(0.05, envR) / Math.max(0.05, envB));   // 暖冷比只施加一半
  }
  const lumS = clampN(lum, 0.04, 1.0) * SH_DARK;
  // q(v) = js_round(clampN(v,0,1)*255/6)*6（量化到 6 的倍数）；
  // numpy uint8 回绕（258→2），Uint8ClampedArray 钳位（258→255）——按 Python 回绕
  const q = (v) => (jsRound(clampN(v, 0.0, 1.0) * 255.0 / 6.0) * 6) % 256;
  return [q(lumS * rb), q(lumS), q(lumS / rb)];
}

/**
 * 立绘原生分辨率下的接触阴影层（contact_shadow_layer，index.html:3027-3143）。
 *
 * 步骤（逐字对应原版）：
 *   1. c.w < 14 → 不画，返回 1×1 全透明层（立绘像素 w<12 或 h<12 同样）；
 *   2. outH = max(4, jsRound(立绘宽·SH_THICK))；bandH = max(2, jsRound(立绘高·SH_BAND))
 *      取底部 8.5% 那一条带；
 *   3. 逐列 alpha 最大值 → 落地轮廓 prof[x]（那一列有没有踩在地上、踩得多实）；
 *   4. 横向移动平均，半径 r = max(1, jsRound(立绘宽·0.02))，越界样本跳过、按有效
 *      数取均值（不抹的话影子边缘就是脚本身的硬轮廓，比立绘还清楚）；
 *   5. 纵向高斯带：t=(y+0.5)/outH，dy=t-SH_PEAK，fall=exp(-dy²/(2·SH_SPREAD²))；
 *      fall<0.004 整行透明；a = sm[x]·fall·SH_ALPHA，a<=0.002 该像素透明，否则
 *      alpha = jsRound(a·255)，RGB = 阴影色（alpha=0 处也填色，与 Python 一致）。
 *
 * 返回 canvas（宽 = 立绘宽、高 = outH）的 RGBA 层 —— 即原版
 * drawImage(fp, -sw/2, top, sw, shh) 的源 footCv，由 drawSprite 一次 warp 进
 * 旋转矩形（一次重采样，与原版一次 drawImage+rotate 的滤波次数一致）。
 */
export function contactShadowAlpha(charImg, placement, ambient) {
  const c = placement;
  const px = pixelsOf(charImg);
  const wPx = px === null ? 0 : px.width;
  const hPx = px === null ? 0 : px.height;
  if (c === null || c === undefined || !(c.w >= 14) || wPx < 12 || hPx < 12) {
    return makeCanvas(1, 1);           // 1×1 全透明 = 原版「不画」标记
  }
  const [colR, colG, colB] = shadowColor(ambient);
  const outH = Math.max(4, jsRound(wPx * SH_THICK));
  const bandH = Math.max(2, jsRound(hPx * SH_BAND));
  const data = px.data;
  // ③ 逐列 alpha 最大值 → 落地轮廓
  const prof = new Float64Array(wPx);
  for (let x = 0; x < wPx; x++) {
    let mx = 0;
    for (let y = hPx - bandH; y < hPx; y++) {
      const a = data[(y * wPx + x) * 4 + 3];
      if (a > mx) mx = a;
    }
    prof[x] = mx / 255.0;
  }
  // ④ 横向移动平均（窗口 ±r，越界样本跳过、按有效数取均值）
  const r = Math.max(1, jsRound(wPx * 0.02));
  const sm = new Float64Array(wPx);
  for (let x = 0; x < wPx; x++) {
    const lo = x - r < 0 ? 0 : x - r;
    const hi = x + r >= wPx ? wPx - 1 : x + r;
    let sum = 0;
    for (let k = lo; k <= hi; k++) sum += prof[k];
    sm[x] = sum / (hi - lo + 1);
  }
  // ⑤ 纵向高斯带 + alpha 量化
  const layer = makeCanvas(wPx, outH);
  const g = ctx2d(layer);
  const img = g.createImageData(wPx, outH);
  const out = img.data;
  for (let i = 0; i < out.length; i += 4) {
    out[i] = colR;
    out[i + 1] = colG;
    out[i + 2] = colB;
    out[i + 3] = 0;
  }
  for (let y = 0; y < outH; y++) {
    const t = (y + 0.5) / outH;
    const dy = t - SH_PEAK;
    const fall = Math.exp(-(dy * dy) / (2.0 * SH_SPREAD * SH_SPREAD));
    if (fall < 0.004) continue;                     // 整行透明
    for (let x = 0; x < wPx; x++) {
      const a = sm[x] * fall * SH_ALPHA;
      if (a <= 0.002) continue;                     // 该像素透明
      out[(y * wPx + x) * 4 + 3] = Math.floor(a * 255.0 + 0.5);
    }
  }
  g.putImageData(img, 0, 0);
  return layer;
}

/* ───────────────── 立绘落位合成（composite_sprite / drawCharTo） ───────────────── */

/**
 * 把立绘（可带阴影层）按摆位合成进 base 画布（composite_sprite，index.html:3146-3166）。
 *
 * translate(c.u, c.v) → rotate(c.rot) → 先铺阴影 → 再 drawImage 立绘；锚点 = 立绘
 * 外接矩形中心，立绘占局部矩形 (-c.w/2, -c.h/2, c.w, c.h)。阴影层局部矩形
 * (-c.w/2, c.h/2 - c.w·SH_THICK·SH_PEAK, c.w, c.w·SH_THICK) —— 峰值那条线压在
 * 「立绘下沿」（脚踩地面的地方）。
 *
 * opts：
 *   shadow      是否铺阴影（默认 true）；**自由模式调用方应传 false**
 *               （原版 index.html:3117 只在贴地模式画，离线签名没有 mode）
 *   ambient     环境光 [r,g,b] 或 null（只在现场算阴影层时用到）
 *   shadowLayer 预制的 contactShadowAlpha 结果（原版 footCv 缓存）；缺省且
 *               shadow 为真时现场算一份
 * · c 为 null 或尺寸退化（w/h <= 0）→ 不动画布、原样返回 base。
 * · 就地画（canvas 语义，原版 drawCharTo 也是就地画），返回同一个 base。
 */
export function drawSprite(base, charImg, c, opts) {
  const o = opts || {};
  const shadow = o.shadow === undefined ? true : !!o.shadow;
  const ambient = o.ambient === undefined ? null : o.ambient;
  let layer = o.shadowLayer === undefined ? null : o.shadowLayer;
  if (c === null || c === undefined || !(c.w > 0) || !(c.h > 0)) return base;
  if (shadow && (layer === null || layer === undefined)) {
    layer = contactShadowAlpha(charImg, c, ambient);
  }
  const g = ctx2d(base);
  g.save();
  g.translate(c.u, c.v);
  g.rotate(c.rot);
  // 1×1 是 contactShadowAlpha 的「不画」标记（全透明）——略过一次 no-op drawImage
  if (shadow && layer !== null && layer !== undefined
      && !(layer.width === 1 && layer.height === 1)) {
    const thick = c.w * SH_THICK;
    g.drawImage(layer, -c.w / 2, c.h / 2 - thick * SH_PEAK, c.w, thick);
  }
  g.drawImage(charImg, -c.w / 2, -c.h / 2, c.w, c.h);
  g.restore();
  return base;
}

/* ───────────────────────── composeShot 六步（compose.py） ───────────────────────── */

/** mode 校验（_engine 开头，抛错信息同 Python）。 */
function checkMode(mode) {
  if (mode !== 'ground' && mode !== 'free') {
    throw new Error(`mode 必须是 ('ground', 'free') 之一，收到 ${JSON.stringify(mode)}`);
  }
}

/** 相机画布尺寸取整 + 校验（_engine：W/H < 1 抛错）。 */
function camDims(cam) {
  const W = Math.trunc(Number(cam.W));
  const H = Math.trunc(Number(cam.H));
  if (!(W >= 1) || !(H >= 1)) {
    throw new Error(`相机画布尺寸非法：${cam.W}×${cam.H}`);
  }
  return { W, H };
}

/** 参数袋归一化（缺省同 Python 关键字参数）。 */
function normalizeParams(params) {
  const p = params || {};
  const frameRaw = (p.frame !== undefined && p.frame !== null) ? p.frame : p.frameKey;
  return {
    frame: (frameRaw === undefined || frameRaw === null) ? DEFAULT_FRAME : String(frameRaw),
    mode: (p.mode === undefined || p.mode === null) ? 'ground' : String(p.mode),
    shot: (p.shot === undefined || p.shot === null) ? DEFAULT_SHOT : String(p.shot),
    charScale: numOr(p.charScale, DEFAULT_CHAR_SCALE),
    rotBasis: (p.rotBasis === undefined || p.rotBasis === null) ? 'world' : String(p.rotBasis),
    sizePct: numOr(p.sizePct, 60.0),
    edgeBar: Math.trunc(numOr(p.edgeBar, 0)),
    ambient: p.ambient === undefined ? null : p.ambient,
    charName: (p.charName === undefined || p.charName === null) ? DEFAULT_CHAR_NAME : String(p.charName),
    char: (p.char === undefined || p.char === null) ? null : p.char,
    // 3D 立绘挂钩点（app.js 的 draw3DInto）：函数原样透传，缺省 null。
    // ⚠ 必须在这儿放行 —— 少了这一条，调用方传进来的 charDraw 会被 normalizeParams
    //   丢掉，composeShot/composeCharLayer 里的分支永远不生效（静默失效）。
    charDraw: typeof p.charDraw === 'function' ? p.charDraw : null,
    shadow: p.shadow === undefined ? true : !!p.shadow,
  };
}

/** 本遍落位（_placement + _scene_placement 合一：core.currentPlacement 按 mode 分派，
 *  各分支只取自己收得下的键；自由模式的吸附目标 rect 传本遍 frameRect）。 */
function placementFor(charImg, subj, cam, p, rect) {
  if (charImg === null || charImg === undefined) return null;
  const cw = Math.trunc(Number(charImg.width));
  const ch = Math.trunc(Number(charImg.height));
  if (!(cw > 0) || !(ch > 0)) return null;
  return currentPlacement(cw, ch, subj, cam, {
    mode: p.mode,
    shot: p.shot,
    charScale: p.charScale,
    rotBasis: p.rotBasis,
    sizePct: p.sizePct,
    edgeBar: p.edgeBar,
    rect: rect,
  });
}

/** ⑤ 整数 off 画布裁切（_crop_offset：drawImage(full, -R.x, -R.y)）。 */
function cropOffset(full, rect) {
  const ow = Math.max(1, jsRound(rect.w));
  const oh = Math.max(1, jsRound(rect.h));
  const off = makeCanvas(ow, oh);
  const g = ctx2d(off);
  g.drawImage(full, -jsRound(rect.x), -jsRound(rect.y));
  return off;
}

/** 立绘层与 composeShot 共用的绘制段（②之后的透明底/照片底 + ④画立绘）。 */
function drawInto(full, charImg, subj, cam, p, rect) {
  const c = placementFor(charImg, subj, cam, p, rect);
  if (c !== null && c !== undefined) {
    // shadow 门（原版 LIGHT_SHD；v1.3.0 与 Python/本地 web 同语义）。
    // ⚠ JS 侧没有 bakeLit（v1 起环境光烘焙就是 no-op，light 恒 null），
    //   所以 light/grain 两开关在网页版只影响**状态显示**、不改像素——
    //   这一点在 UI 提示里写明，不假装生效。
    const wantShadow = p.mode === 'ground' && p.shadow !== false;
    drawSprite(full, charImg, c, {
      shadow: wantShadow,                  // 阴影是「场景」的一部分：自由模式不铺
      ambient: p.ambient,
      shadowLayer: wantShadow ? contactShadowAlpha(charImg, c, p.ambient) : null,
    });
  }
  return c;
}

/**
 * 合成入口（compose_shot，原版 index.html:3673-3701 六步，见文件头）。
 * photoCanvas 为 null/undefined 时走「不铺帧」的透明底路径（同 _engine 的
 * src=None 分支；立绘层路径请优先用 composeCharLayer）。
 *
 * @returns {{shot: HTMLCanvasElement, rect: Object, scene: Object, light: null}}
 *   shot —— 按 R 裁好的出图（照片路径取 RGB；透明底路径带 alpha）；
 *   rect —— 实际裁切框 {x,y,w,h}（frameRect 的产物）；
 *   scene —— buildScene 的产物（version 1，与原版 scene.json 同构）；
 *   light —— v1 不做采光，恒 null。
 */
export function composeShot(photoCanvas, charImg, subj, cam, params) {
  const p = normalizeParams(params);
  checkMode(p.mode);
  const { W, H } = camDims(cam);
  // ① 取裁切框
  const rect = frameRect(p.frame, W, H);
  // ② 铺源帧：有照片整屏铺满（同尺寸即 1:1 blit）；否则整块透明
  const full = makeCanvas(W, H);
  const g = ctx2d(full);
  if (photoCanvas !== null && photoCanvas !== undefined) {
    g.drawImage(photoCanvas, 0, 0, W, H);
  }
  // ③ 采光：v1 不做（params.light / params.tone 接受但忽略，light 恒 null）
  // ④ 画立绘（先阴影后图）。params.charDraw 是 3D 层的挂钩点（FEATURE-SPEC §5）：
  //    给了就用它画（mmd.js 的 WebGL 层），返回的落位对象仍进 scene.json；
  //    没给走原 drawInto。钩子签名 (full, rect) -> placement。
  const c = (typeof p.charDraw === 'function')
    ? p.charDraw(full, rect)
    : drawInto(full, charImg, subj, cam, p, rect);
  // ⑤ 小数偏移裁切（越界目标像素保持透明黑）
  const shot = cropOffset(full, rect);
  // ⑥ 配方 + 光（恒 null）
  const scene = buildScene(rect, c, subj, cam, Object.assign({}, p, { char: charImg }));
  return { shot: shot, rect: rect, scene: scene, light: null };
}

/**
 * 立绘层路径（compose_char_layer，原版 buildCharCanvas = composeShot(null, …)，
 * index.html:3709）：不铺帧的透明底上出同一套 (阴影+立绘)，再按**同一个裁切框**
 * 裁出来 —— 直接盖在 composeShot 的 shot.jpg 上严丝合缝（素材包 character.png）。
 * 裁切框取 params.frame（原版用的就是当前 S.frame；Python 侧该入口写死
 * DEFAULT_FRAME，见文件头「已知差异」）。
 *
 * @returns {{layer: HTMLCanvasElement, rect: Object}} layer 尺寸 = rect 取整尺寸。
 */
export function composeCharLayer(charImg, subj, cam, params) {
  const p = normalizeParams(params);
  checkMode(p.mode);
  const { W, H } = camDims(cam);
  const rect = frameRect(p.frame, W, H);
  const full = makeCanvas(W, H);          // 透明底：原版空 canvas 天然带 alpha
  // ④ 画立绘（先阴影后图）。params.charDraw 是 3D 层的挂钩点（同 composeShot）：
  //    3D 开着时这一层就是 MMD 的透明层（模型自身的 alpha），同一个裁切框。
  if (typeof p.charDraw === 'function') p.charDraw(full, rect);
  else drawInto(full, charImg, subj, cam, p, rect);
  const layer = cropOffset(full, rect);
  return { layer: layer, rect: rect };
}

/* ───────────────────────── scene.json 配方（scene.py） ───────────────────────── */

/** project(S.x, y, S.d) 的 dict 化：相机后方（Zc<=0.05）→ null（_project_point）。 */
function projectPoint(x, y, d, cam) {
  const q = project(x, y, d, cam);
  return q === null ? null : { u: q[0], v: q[1] };
}

/**
 * 配方 dict（build_scene，index.html:3729-3789 的 1:1 同构）：字段顺序、单位、
 * 取整语义全部照抄原版 —— version / image{width,height,file} /
 * frame{aspect,crop,source} / camera{focalPx,focal35mm,pitchDeg,rollDeg,heightM} /
 * subject{heightM,distanceM,lateralM,footPixel,headPixel} / ground{y,normal} /
 * shot / charScale / edge{bar,rotationLocked} / character / mmd。
 *
 * params（除 rect/placement/subj/cam 外的全部，缺省同 Python 关键字参数）：
 *   frame → frame.aspect；char → character.image（鸭子类型 .width/.height）；
 *   charName → character.name；mode → character.mode；shot → shot；
 *   charScale → charScale；edgeBar → edge.bar / edge.rotationLocked。
 *
 * placement 为 null 或 params.char 为 null → character 记 null（原版「下游别当成
 * 有立绘但位置是 0」）。mmd 恒 null（离线没有 three.js 3D 运行时，SPEC §0）。
 */
export function buildScene(rect, placement, subj, cam, params) {
  const p = normalizeParams(params);
  const W = Math.trunc(Number(cam.W));
  const H = Math.trunc(Number(cam.H));
  const feq = cam.feq;
  const bar = p.edgeBar;
  let character = null;
  if ((placement !== null && placement !== undefined) && p.char !== null) {
    character = {
      name: p.charName,
      mode: p.mode,
      image: {
        width: Math.trunc(Number(p.char.width)),
        height: Math.trunc(Number(p.char.height)),
      },
      centerPixel: [jsFixed(placement.u, 2), jsFixed(placement.v, 2)],
      sizePixel: [jsFixed(placement.w, 2), jsFixed(placement.h, 2)],
      rotationDeg: jsFixed(placement.rot * 180.0 / Math.PI, 3),
    };
  }
  return {
    version: SCENE_VERSION,
    image: {
      width: jsRound(rect.w),
      height: jsRound(rect.h),
      file: SCENE_IMAGE_FILE,
    },
    frame: {
      aspect: p.frame,
      crop: [jsRound(rect.x), jsRound(rect.y), jsRound(rect.w), jsRound(rect.h)],
      source: { width: W, height: H },
    },
    camera: {
      focalPx: jsFixed(focalPx(W, H, feq), 2),
      focal35mm: feq,
      pitchDeg: jsFixed(cam.pitch, 3),
      rollDeg: jsFixed(cam.roll, 3),
      heightM: cam.h,
    },
    subject: {
      heightM: subj.hp,
      distanceM: jsFixed(subj.d, 3),
      lateralM: jsFixed(subj.x, 3),
      footPixel: projectPoint(subj.x, -cam.h, subj.d, cam),
      headPixel: projectPoint(subj.x, subj.hp - cam.h, subj.d, cam),
    },
    ground: { y: 0, normal: [0, 1, 0] },
    shot: p.shot,
    charScale: p.charScale,
    edge: { bar: bar, rotationLocked: bar !== 0 },
    character: character,
    // 3D 立绘（MMD）：离线没有运行时，这一枪恒是 2D 渲的
    mmd: null,
  };
}
