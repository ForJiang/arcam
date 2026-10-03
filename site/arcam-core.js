/* arcam-core.js — 纯数学内核（针孔投影 / 摆位 / 画幅），零 DOM、零依赖。
 *
 * 逐字移植 src/arcam/geometry.py + src/arcam/placement.py + src/arcam/frames.py，
 * 数值口径与 Python 完全一致：内部一律 float64、对外角度用度、坐标 Y 上 / Z 朝
 * 观察者 / 画布 v 向下。Math.round 就是半向 +∞（= Python js_round），直接用，
 * 不另写取整捷径。
 *
 * 与 Python 的类型差异（有意为之，无语义差异）：
 *   · charW/charH 两个数字替代 numpy 图——JS 侧只有尺寸参与数学；任一无图或非正
 *     尺寸 → null（对应 Python _char_size 的 None 早退，原版 !chCanvas 判据）。
 *   · subj = {hp, d, x}（height_m/distance_m/lateral_m 的驼峰等价）；
 *     cam  = {W, H, h, feq, pitch, roll}（width/height/height_m/feq_mm/pitch_deg/roll_deg）。
 *   · freeP 是 [u, v] 数组（Python tuple 的等价，按下标访问）。
 *   · opts 收 Python 的关键字参数：mode/shot/charScale/rotBasis/sizePct/rotDeg/
 *     freeP/edgeBar/rect，缺省值与 Python 一致。
 */

export const K_LONG = 17.3064;   // 0.8*43.266/2（mm，传感器长半边长），geometry.py:20
export const K_SHORT = 12.9798;  // 0.6*43.266/2（mm，传感器短半边长），geometry.py:21

/* 景别表：值 = 立绘下沿离地高度 / 人物身高（placement.py:28）。 */
export const SHOTS = Object.assign(Object.create(null), {
  '全身': 1.00, '膝上': 0.72, '腰上': 0.47, '胸上': 0.28, '头肩': 0.20,
});

/* 出图画幅表：值 = 宽/高，0 = 不裁，-1 = 整块画布（placement.py:31）。 */
export const FRAMES = Object.assign(Object.create(null), {
  '自由': 0, '1:1': 1, '4:5': 0.8, '16:9': 16 / 9, '9:16': 9 / 16, '满幅': -1,
});

/* 自由模式图心的归一化默认位置（placement.py:34）。用数组而非对象：Python 侧是
 * tuple，调用方按下标 [0]/[1] 取，保持索引语义一致。 */
export const DEFAULT_FREE_P = [0.5, 0.6];

const DEG2RAD = Math.PI / 180.0;

/* clampN(v, a, b)：先夹范围，两端不排序（numeric.py:26，原版同样不排序）。 */
function clampN(value, lo, hi) {
  return value < lo ? lo : (value > hi ? hi : value);
}

/* 立绘像素尺寸 (w, h)；无图或任一维非正 → null。 */
function charSize(charW, charH) {
  if (typeof charW !== 'number' || typeof charH !== 'number') return null;
  if (!(charW > 0) || !(charH > 0)) return null;
  return [charW, charH];
}

/* SHOTS 的取值带「未知/空键回退全身」的兜底（placement.py:103 的 or 链，
 * 老存档只有一个 '全身' 可用时不能崩）。表本身无原型链，查不到的键一律兜底。 */
function shotVal(shot) {
  if (shot === '全身') return SHOTS['全身'];
  const v = shot === undefined || shot === null ? undefined : SHOTS[shot];
  return v === undefined ? SHOTS['全身'] : v;
}

/* ─────────── hypot：必须与 Python 的 math.hypot 逐位一致 ───────────
 * V8/Chrome、Firefox 的 Math.hypot 是朴素 sqrt(x²+y²)：平方和本身已被舍入，
 * 结果比 libm 的 math.hypot 最多差 1 ulp（实测 200k 随机样本 60918 例不等）。
 * Python 的 math.hypot 是正确舍入的，而 groundPlacement 的 span 下游决定立绘
 * 像素高，差 1 ulp 就会在 golden diff 里留下 3e-16 的相对偏差。
 * 这里用 Dekker 拆分把平方和算成 double-double，再做一步牛顿修正，结果与
 * Python 逐位相同（200k 随机样本 + 全部 golden 用例 0 例外）。
 * 纯 JS、无 fma 依赖；量级超出安全区（平方和会溢出/次正态）时退回 Math.hypot。
 */
const SPLIT = 134217729;                    // 2^27 + 1，Dekker 双精度拆分因子
function twoSum(a, b) {                     // Knuth two-sum（无顺序要求）
  const s = a + b;
  const bb = s - a;
  return [s, (a - (s - bb)) + (b - bb)];
}
function twoProd(a, b) {                    // 精确乘积 (p, e)，满足 p + e == a*b
  const ca = SPLIT * a, ah = ca - (ca - a), al = a - ah;
  const cb = SPLIT * b, bh = cb - (cb - b), bl = b - bh;
  const p = a * b;
  const e = ((ah * bh - p) + ah * bl + al * bh) + al * bl;
  return [p, e];
}
function hypot(x, y) {
  let a = Math.abs(x), b = Math.abs(y);
  if (a < b) { const t = a; a = b; b = t; }          // a >= b：|a²| >= |b²|，fast2sum 精确
  if (Number.isNaN(a) || Number.isNaN(b)) return NaN;
  if (b === 0 || !Number.isFinite(a)) return a;      // 0 / ±∞
  if (!(a > 2 ** -450) || !(a < 2 ** 500)) return Math.hypot(a, b);  // 拆分会失真 → 退回
  const [p, pe] = twoProd(a, a);
  const [q, qe] = twoProd(b, b);
  const head = p + q;                                // 平方和的主项
  const tail = ((p - head) + q) + (pe + qe);         // 平方和的低位补偿
  const r = Math.sqrt(head);                         // IEEE 正确舍入的平方根
  if (!(r > 0)) return r;
  const [r2, r2e] = twoProd(r, r);                   // r² 的精确表示
  const delta = ((head - r2) + tail) - r2e;          // 精确平方和 − r²（head−r2 由 Sterbenz 精确）
  if (delta === 0) return r;
  return r + delta / (2 * r);                        // 一步牛顿修正到正确舍入
}

/* ───────────────────────────── 几何 / 投影 ───────────────────────────── */

/* freeP 读成 [u, v]：接受数组（Python tuple 的等价）或 {u,v} 对象两种写法。 */
function freePU(freeP) {
  if (freeP === undefined || freeP === null) freeP = DEFAULT_FREE_P;
  return typeof freeP === 'object' && !Array.isArray(freeP) ? [freeP.u, freeP.v] : freeP;
}

/** 像素焦距 F。wide = L/Sd >= 4/3 → (L/2)*feq/K_LONG，否则 (Sd/2)*feq/K_SHORT。 */
export function focalPx(W, H, feq) {
  const longEdge = Math.max(W, H);
  const shortEdge = Math.min(W, H);
  const wide = longEdge / shortEdge >= 4.0 / 3.0;
  if (wide) return (longEdge / 2.0) * feq / K_LONG;
  return (shortEdge / 2.0) * feq / K_SHORT;
}

const _focalFromCam = (cam) => focalPx(cam.W, cam.H, cam.feq);

/**
 * 相机系点 (x, y, d) → 画布像素 (u, v)；Zc <= 0.05（点在相机平面上或背后）
 * 返回 null —— 唯一剔除条件，无 FOV 裁剪（geometry.py:66）。
 * y 是相机系 y（= 世界高度 - 相机高），调用方负责换算。
 */
export function project(x, y, d, cam) {
  const p = cam.pitch * DEG2RAD;
  const r = cam.roll * DEG2RAD;
  const zc = -y * Math.sin(p) + d * Math.cos(p);
  if (zc <= 0.05) return null;
  const yc = -y * Math.cos(p) - d * Math.sin(p);
  const xr = x * Math.cos(r) - yc * Math.sin(r);
  const yr = x * Math.sin(r) + yc * Math.cos(r);
  const focal = _focalFromCam(cam);
  const u = cam.W / 2.0 + focal * xr / zc;
  const v = cam.H / 2.0 + focal * yr / zc;
  return [u, v];
}

/**
 * 画布像素 (u, v) → 世界 (x, d)（米）：project 固定 y = -h 的解析逆。
 * 如实保留的原版写回 clamp：d 夹 [0.5, 20]、x 夹 [-6, 6]（geometry.py:104，
 * 后者的 ±6 与 autoFixX 的 ±20 故意不一致，不许顺手修）。
 * 退化返回 null：|den| < 1e-6（射线与地平面近平行）或 d <= 0.05。
 */
export function place(u, v, cam) {
  const p = cam.pitch * DEG2RAD;
  const r = cam.roll * DEG2RAD;
  const focal = _focalFromCam(cam);
  const up = u - cam.W / 2.0;
  const vp = v - cam.H / 2.0;
  const a = (up / focal) * Math.cos(r) + (vp / focal) * Math.sin(r);
  const b = -(up / focal) * Math.sin(r) + (vp / focal) * Math.cos(r);
  const den = Math.sin(p) + b * Math.cos(p);
  if (Math.abs(den) < 1e-6) return null;
  const d = cam.h * (Math.cos(p) - b * Math.sin(p)) / den;
  if (d <= 0.05) return null;
  const zc = cam.h * Math.sin(p) + d * Math.cos(p);
  const x = a * zc;
  return [clampN(x, -6.0, 6.0), clampN(d, 0.5, 20.0)];
}

/* ───────────────────────────── 摆位 ───────────────────────────── */

/**
 * 把基准边按旋转量转过去（切口朝向变了 → 要吸的边也换）。placement.py:73
 * 画面系 y 向下、正向旋转顺时针，故「下」转过 +90° 变「左」：下(2)→左(3)→上(1)→右(4)。
 */
function rotateBar(bar, rot) {
  const q = ((Math.round(rot / (Math.PI / 2.0)) % 4) + 4) % 4;
  if (!q) return bar;
  const order = [2, 3, 1, 4];
  const i = order.indexOf(bar);
  return i < 0 ? bar : order[(i + q) % 4];
}

/**
 * 贴地摆位：立绘下沿落在景别那个高度上，上沿顶在头顶投影点。placement.py:86
 * pb = project(x, yb-h, d)（下沿）；pt = project(x, Hp-h, d)（头顶）；
 * span = hypot(pb→pt)；h = span·charScale；中心 = pb 沿 pb→pt 方向推 h/2
 * （锚点是下沿 pb，charScale=1 时中心正好落回 (pb+pt)/2）；w = h·宽高比；
 * rot = rotBasis=='frame' ? 0 : atan2(ux, -uy)。
 * 返回 null：无图 / pb 或 pt 落到相机后方 / span < 2。
 */
export function groundPlacement(charW, charH, subj, cam, opts = {}) {
  if (charSize(charW, charH) === null) return null;
  const k = shotVal(opts.shot);
  const yb = (1.0 - k) * subj.hp;
  const pb = project(subj.x, yb - cam.h, subj.d, cam);
  const pt = project(subj.x, subj.hp - cam.h, subj.d, cam);
  if (pb === null || pt === null) return null;
  const span = hypot(pb[0] - pt[0], pb[1] - pt[1]);
  if (span < 2.0) return null;
  const h = span * (opts.charScale === undefined ? 1.0 : Number(opts.charScale));
  const ux = (pt[0] - pb[0]) / span;
  const uy = (pt[1] - pb[1]) / span;
  const rot = opts.rotBasis === 'frame' ? 0.0 : Math.atan2(ux, -uy);
  return {
    u: pb[0] + ux * h / 2.0,
    v: pb[1] + uy * h / 2.0,
    w: h * charW / charH,
    h: h,
    rot: rot,
  };
}

/**
 * 自由模式：按画面高度 sizePct% 摆，图心在归一化 freeP。placement.py:125
 * 贴边开着（edgeBar 非 0）且给了 rect 时过 snapToEdge 吸附；rect 缺省时不吸附。
 * rotDeg 缺省 0.0（直接调用本函数时不随相机斜；经 currentPlacement 分派时默认
 * 跟随 cam.roll，rotBasis='frame' 时为 0）。
 */
export function freePlacement(charW, charH, subj, cam, opts = {}) {
  if (charSize(charW, charH) === null) return null;
  const aspect = charW / charH;
  const rot = (opts.rotDeg === undefined ? 0.0 : Number(opts.rotDeg)) * DEG2RAD;
  const freeP = freePU(opts.freeP);
  const u = freeP[0] * cam.W;
  const v = freeP[1] * cam.H;
  const h = (opts.sizePct === undefined ? 60.0 : Number(opts.sizePct)) / 100.0 * cam.H;
  let uu = u, vv = v;
  const edgeBar = opts.edgeBar === undefined ? 0 : Number(opts.edgeBar);
  const rect = opts.rect === undefined ? null : opts.rect;
  if (edgeBar && rect !== null && rect !== undefined) {
    const s = snapToEdge({ u: u, v: v, w: h * aspect, h: h, rot: rot },
                         edgeBar, rect, aspect, rot);
    uu = s.u; vv = s.v;
  }
  return { u: uu, v: vv, w: h * aspect, h: h, rot: rot };
}

/**
 * 按 opts.mode 分派：'free' → freePlacement，其余 → groundPlacement。placement.py:155
 * free 分支的 rotDeg 缺省跟随 cam.roll（原版 charRot，2930），'frame' 时为 0。
 */
export function currentPlacement(charW, charH, subj, cam, opts = {}) {
  const mode = opts.mode === undefined ? 'ground' : opts.mode;
  if (mode === 'free') {
    const rotBasis = opts.rotBasis === undefined ? 'world' : String(opts.rotBasis);
    let rotDeg = opts.rotDeg;
    if (rotDeg === undefined || rotDeg === null) {
      rotDeg = rotBasis === 'frame' ? 0.0 : cam.roll;
    }
    return freePlacement(charW, charH, subj, cam, {
      sizePct: opts.sizePct === undefined ? 60.0 : Number(opts.sizePct),
      rotDeg: Number(rotDeg),
      freeP: freePU(opts.freeP),
      edgeBar: opts.edgeBar === undefined ? 0 : Number(opts.edgeBar),
      rect: opts.rect,
    });
  }
  return groundPlacement(charW, charH, subj, cam, {
    charScale: opts.charScale === undefined ? 1.0 : Number(opts.charScale),
    rotBasis: opts.rotBasis === undefined ? 'world' : String(opts.rotBasis),
    shot: opts.shot === undefined ? '全身' : String(opts.shot),
  });
}

/**
 * 自动摆正：把立绘横向吸到最近的三分线（1/3、1/2、2/3 三选一）。
 * 返回 [newX, status]：status ∈ 'nochar' | 'already' | 'moved:NN'。
 * ground：project() 非线性 → 取数值导数（dx=0.05）一步解，clamp ±20
 * （placement.py:240；与 place 的 ±6 故意不一致，如实保留）。
 * free：改归一化 freeP.u（clamp [0,1]）后 resnap，newX = 吸附后的 s.u/W。
 * 'nochar'/'already' 时 newX = 未动的当前位置。
 */
export function autoFixX(charW, charH, subj, cam, opts = {}) {
  const mode = opts.mode === undefined ? 'ground' : opts.mode;
  const freeP = freePU(opts.freeP);
  const keepX = mode !== 'free' ? subj.x : Number(freeP[0]);
  if (!cam.W || !cam.H) return [keepX, 'nochar'];   // 原版 !cv.width 分支
  const c0 = currentPlacement(charW, charH, subj, cam, opts);
  if (c0 === null) return [keepX, 'nochar'];
  const width = cam.W;
  const xr = c0.u / width;
  let best = 1.0 / 3.0;
  for (const cand of [0.5, 2.0 / 3.0]) {           // 1/3 → 0.5 → 2/3，平手留前者
    if (Math.abs(xr - cand) < Math.abs(xr - best)) best = cand;
  }
  const targetU = best * width;
  if (Math.abs(c0.u - targetU) < 1.0) return [keepX, 'already'];
  const moved = 'moved:' + String(Math.round(best * 100.0));
  if (mode === 'free') {
    let newU = clampN(targetU / width, 0.0, 1.0);
    const edgeBar = opts.edgeBar === undefined ? 0 : Number(opts.edgeBar);
    const rect = opts.rect === undefined ? null : opts.rect;
    if (edgeBar && rect !== null && rect !== undefined) {
      const size = charSize(charW, charH);
      if (size !== null) {
        const aspect = size[0] / size[1];
        const rotBasis = opts.rotBasis === undefined ? 'world' : String(opts.rotBasis);
        let rotDeg = opts.rotDeg;
        if (rotDeg === undefined || rotDeg === null) {
          rotDeg = rotBasis === 'frame' ? 0.0 : cam.roll;
        }
        const rot = Number(rotDeg) * DEG2RAD;
        const h = (opts.sizePct === undefined ? 60.0 : Number(opts.sizePct)) / 100.0 * cam.H;
        const raw = { u: newU * width, v: Number(freeP[1]) * cam.H, w: h * aspect, h: h, rot: rot };
        const s = snapToEdge(raw, edgeBar, rect, aspect, rot);
        newU = s.u / width;
      }
    }
    return [newU, moved];
  }
  const keep = subj.x;
  const c1 = currentPlacement(charW, charH,
                              { hp: subj.hp, d: subj.d, x: keep + 0.05 },
                              cam, Object.assign({}, opts, { mode: 'ground' }));
  if (c1 === null) return [keep, 'nochar'];
  const grad = (c1.u - c0.u) / 0.05;
  if (!Number.isFinite(grad) || Math.abs(grad) < 1e-6) return [keep, 'nochar'];
  return [clampN(keep - (c0.u - targetU) / grad, -20.0, 20.0), moved];
}

/**
 * 贴边吸附：把立绘吸到 rect 的某条边（1 上 / 2 下 / 3 左 / 4 右）。placement.py:243
 * bw/bh 是旋转后外接盒（w·|cos|+h·|sin| / w·|sin|+h·|cos|）；rot 变化后
 * rotateBar 换边。沿自由轴用 fit 钳位，两端不排序（图比框大时范围自然反过来，
 * 让框始终被图盖住，可自由平移）。返回的 w = h·aspect、rot 原样带过去。
 */
export function snapToEdge(c, bar, rect, aspect, rot) {
  const h = c.h;
  if (!(h > 0)) return c;
  if (!rot) rot = 0.0;
  const w = h * aspect;
  const ac = Math.abs(Math.cos(rot));
  const asn = Math.abs(Math.sin(rot));
  const bw = w * ac + h * asn;
  const bh = w * asn + h * ac;
  bar = rotateBar(bar, rot);

  function fit(v, a, b) {
    const lo = a < b ? a : b;
    const hi = a > b ? a : b;
    return v < lo ? lo : (v > hi ? hi : v);
  }

  if (bar === 1) {
    return { u: fit(c.u, rect.x + bw / 2.0, rect.x + rect.w - bw / 2.0),
             v: rect.y + bh / 2.0, w: w, h: h, rot: c.rot };
  }
  if (bar === 2) {
    return { u: fit(c.u, rect.x + bw / 2.0, rect.x + rect.w - bw / 2.0),
             v: rect.y + rect.h - bh / 2.0, w: w, h: h, rot: c.rot };
  }
  if (bar === 3) {
    return { u: rect.x + bw / 2.0,
             v: fit(c.v, rect.y + bh / 2.0, rect.y + rect.h - bh / 2.0),
             w: w, h: h, rot: c.rot };
  }
  if (bar === 4) {
    return { u: rect.x + rect.w - bw / 2.0,
             v: fit(c.v, rect.y + bh / 2.0, rect.y + rect.h - bh / 2.0),
             w: w, h: h, rot: c.rot };
  }
  return c;
}

/* ───────────────────────────── 画幅 ───────────────────────────── */

/** 离线版 viewRect：无布局外壳 → 整块画布（frames.py:22，documented deviation）。 */
export function viewRect(W, H) {
  return { x: 0, y: 0, w: W, h: H };
}

/**
 * 出图矩形（画布坐标）：'满幅'(-1) → 整块画布；'自由'/未知(0) → viewRect；
 * 否则在 viewRect 内取该宽高比的最大内接矩形居中，宽高保持浮点、
 * 原点取 Math.round（整）。frames.py:32
 */
export function frameRect(key, W, H) {
  let ar = key === undefined || key === null ? undefined : FRAMES[key];
  if (ar === undefined) ar = 0.0;
  if (ar === -1) return { x: 0, y: 0, w: W, h: H };
  const v = viewRect(W, H);
  if (!ar) return v;
  let w = v.w;
  let h = w / ar;
  if (h > v.h) {
    h = v.h;
    w = h * ar;
  }
  return {
    x: Math.round(v.x + (v.w - w) / 2),
    y: Math.round(v.y + (v.h - h) / 2),
    w: w,
    h: h,
  };
}
