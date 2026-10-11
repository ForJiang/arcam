/* arcam-lighting.js — 环境光采样与立绘烘光（src/arcam/lighting.py 的 Pages 纯 JS 移植）。
 *
 * 零 DOM、零依赖（只有 ./arcam-core.js 的 currentPlacement 参与 lightRegionRect），
 * Node 可直接 import —— 黄金向量门禁按这个前提逐位对拍 Python。
 *
 * 三条主线，全部逐字对应 Python：
 *   · measureRegion（lighting.py:155）：区域 → N×N 降采样 → 三通道均值 + 32 级亮度
 *     分布。降采样用**面积平均盒式滤波**（原版 drawImage 的滤波核未指定，见 Python
 *     模块 docstring 的「已知偏离」）；采样框按 clampRegion 的 sx/sy 公式钳制，
 *     源外区域按 canvas 规范的**透明黑零填充**（不 clamp 回可见区）。
 *   · charStats（lighting.py:270）：立绘自身统计（alpha 先量化成字节再比 <8）。
 *   · bakeLit（lighting.py:375）：gamma 明暗曲线 + R/B 白平衡（高光限幅）+ 饱和度
 *     + 固定种子颗粒噪声；三条 256 项 LUT 的边界取整一律 floor(clip+0.5)，最终落
 *     字节用 round-half-even（Uint8ClampedArray 语义）。
 *
 * 数值口径（Python 用 numpy，这里必须逐位复刻，否则字节会漂）：
 *   · 前缀和：numpy cumsum 是**严格顺序累加** → JS 顺序累加逐位一致。
 *   · 均值：numpy mean/add.reduce 走 pairwise_sum（128 块、块内 8 路展开、>128 二分
 *     递归）→ 见 pairwiseSum 的注释；写成朴素 for 会差 1 ulp，虽在 1e-9 容差内，
 *     但 charStats 的 r/b/sat 会经 _lit_luts 进 LUT 取整，1 ulp 就可能翻一个字节。
 *   · 亮度加权和：flat @ LUM_W 与左结合 ``0.2126*r + 0.7152*g + 0.0722*b`` 实测
 *     逐位相同（1024 随机样本 0 例外），直接按左结合写。
 *   · Math.pow：与 numpy 的 pow 在本 venv 构建上逐位相同（LUT 定义域 256×8 组
 *     0 例外，实测）。⚠ 这条绑定**当前 numpy 构建**——换构建/SIMD 路径理论上
 *     可能差 1 ulp 翻字节；golden-diff 的 bakeLit 0 容差就是抓这个的，真红了
 *     先查这里，别忙着放容差。
 *   · pow / sqrt：Math.pow / Math.sqrt 与 Python math.* 实测逐位相同（1792 + 600
 *     样本 0 例外）。唯一例外是 Math.log2：约 2.4% 的入参与 Python 差 1 ulp，只影响
 *     litScaleBucket 的 2^round(log2(s)) —— 落在半点边界才会换档（整数比不可能恰在
 *     边界上），记为已知边界。
 *   · 取整：_lut_bytes 是 floor(clip+0.5)（半向 +∞），np.rint / Uint8ClampedArray
 *     是 round-half-even —— 两者语义不同，各走各的（rintHalfEven）。
 *
 * 入参约定：rgba 一律是 **RGBA 字节**（0..255，长 w*h*4），即 getImageData 的
 * ImageData.data；region / 落位矩形是 {x,y,w,h}（浮点、画布坐标）。Python 侧
 * 「uint8 进出 uint8、float64[0,1] 进出 float64[0,1]」的双族行为在这里不移植：
 * 网页侧像素天生是字节，出参恒为新的 Uint8ClampedArray。
 */

import { currentPlacement } from './arcam-core.js';

/* ── 光照常量（lighting.py:32-43，index.html:1148-1158）────────────────────
 * 这些常量 export 出来是 golden-diff.mjs 的**冻结契约**（门禁要能按名引用、
 * 口径漂移立刻红），不是给页面用的——页面只走 bakeLit/measureRegion 两个入口，
 * 常量在内部已参与计算。别当死代码删。 */
export const GRN_MAX = 6.5;          // 颗粒幅度上限（255 里的 6.5）
export const GRN_MIN = 0.7;          // 颗粒幅度下限
export const GRN_COMP_MAX = 3.2;     // 缩放补偿上限
export const LIGHT_NEUTRAL = 0.42;   // 「中性环境」亮度
export const LIGHT_GAIN_MIN = 0.6;   // 明暗增益下限
export const LIGHT_GAIN_MAX = 1.15;  // 明暗增益上限
export const WB_K = 0.75;            // 白平衡对数空间推进比例
export const GMIN = 0.85;            // 高光白平衡限幅下限
export const GMAX = 1.18;            // 高光白平衡限幅上限
export const SAT_K = 0.3;            // 饱和度匹配强度

/** Rec.709 亮度权重（measureRegion / charStats / bakeLit 共用，lighting.py:45）。 */
export const LUM_W = [0.2126, 0.7152, 0.0722];

/* ─────────────────────────── 小工具 ─────────────────────────── */

/** clampN(v, a, b)：先夹范围，两端不排序（numeric.py:26，原版同样不排序）。 */
function clampN(value, lo, hi) {
  return value < lo ? lo : (value > hi ? hi : value);
}

/** JS Math.round 的显式写法：半向 +∞（numeric.py:17 js_round）。 */
const jsRound = (v) => Math.floor(v + 0.5);

/**
 * round-half-even（np.rint / Uint8ClampedArray 的 ToUint8Clamp 语义，
 * lighting.py:291 的 alpha 字节量化与 :439 的出口落字节都用它）。
 * ⚠ 与 Math.round（半向 +∞）、与 floor(x+0.5) 都不同：2.5 → 2、3.5 → 4。
 */
function rintHalfEven(v) {
  const f = Math.floor(v);
  const d = v - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return (f % 2 === 0) ? f : f + 1;      // 恰好在 .5 上：凑到偶数
}

/**
 * numpy mean / add.reduce 的求和顺序（loops_utils.h.src 的 DOUBLE_pairwise_sum）：
 *   n < 8      ：顺序累加（numpy 用 -0 起步以保留 -0，本处入参非负，等价）
 *   n <= 128   ：8 个累加器、块内 8 路展开，再树形合并，剩余尾巴顺序追加
 *   n > 128    ：二分递归，前半截 n2 = floor(n/2) 去掉非 8 倍数部分
 * 实测：朴素 for 累加在 1024 个样本上约 18% 与 numpy 差 1 ulp，而 pairwise 复刻
 * 在 3180 组（连续 + stride 3、n 取 1…1057）上 0 例外。arr 的第 i 个元素在
 * ``arr[off + i*stride]``。
 */
const PW_BLOCKSIZE = 128;
function pairwiseSum(arr, off, n, stride) {
  if (n < 8) {
    let res = -0.0;
    for (let i = 0; i < n; i++) res += arr[off + i * stride];
    return res;
  }
  if (n <= PW_BLOCKSIZE) {
    let r0 = arr[off], r1 = arr[off + stride], r2 = arr[off + 2 * stride],
        r3 = arr[off + 3 * stride], r4 = arr[off + 4 * stride],
        r5 = arr[off + 5 * stride], r6 = arr[off + 6 * stride],
        r7 = arr[off + 7 * stride];
    const end = n - (n % 8);
    let i = 8;
    for (; i < end; i += 8) {
      r0 += arr[off + i * stride];
      r1 += arr[off + (i + 1) * stride];
      r2 += arr[off + (i + 2) * stride];
      r3 += arr[off + (i + 3) * stride];
      r4 += arr[off + (i + 4) * stride];
      r5 += arr[off + (i + 5) * stride];
      r6 += arr[off + (i + 6) * stride];
      r7 += arr[off + (i + 7) * stride];
    }
    let res = ((r0 + r1) + (r2 + r3)) + ((r4 + r5) + (r6 + r7));
    for (; i < n; i++) res += arr[off + i * stride];
    return res;
  }
  const n2 = Math.floor(n / 2) - (Math.floor(n / 2) % 8);
  return pairwiseSum(arr, off, n2, stride)
       + pairwiseSum(arr, off + n2 * stride, n - n2, stride);
}

/* ───────────── 盒式面积加权降采样（drawImage 的离线等价）─────────────── */

/**
 * 沿归约轴做盒式面积加权求和的一列（lighting.py:64 _axis_weighted_sum）。
 * 目的格 k 覆盖源区间 ``[edges[k], edges[k+1])``；源第 i 个元素在
 * ``src[srcBase + i*iStride]``，结果写到 ``out[outBase + k*outStride]``。
 * 越界段按 canvas drawImage 的**透明黑零填充**计入（不 clamp 回可见区）——
 * 体现在前缀和下标被 clip 到 [0, nSrc]、以及两个边缘权重项在越界时整项跳过。
 * ``pre`` 是长度 >= nSrc+1 的前缀和 scratch（boxResample 复用，避免每列新建）。
 */
function axisSum(src, srcBase, iStride, nSrc, edges, nDst, out, outBase,
                 outStride, pre) {
  pre[0] = 0.0;
  for (let i = 0; i < nSrc; i++) pre[i + 1] = pre[i] + src[srcBase + i * iStride];
  for (let k = 0; k < nDst; k++) {
    const a = edges[k], b = edges[k + 1];
    const l0 = Math.floor(a), l1 = Math.floor(b);
    const wl = (l0 + 1) - a;                 // 左边缘的分数权重
    const wr = b - l1;                       // 右边缘的分数权重
    const i1 = l1 < 0 ? 0 : (l1 > nSrc ? nSrc : l1);
    const i0p = l0 + 1;
    const i0 = i0p < 0 ? 0 : (i0p > nSrc ? nSrc : i0p);
    let o = pre[i1] - pre[i0];
    if (l0 >= 0 && l0 < nSrc) o += src[srcBase + l0 * iStride] * wl;
    if (l1 >= 0 && l1 < nSrc) o += src[srcBase + l1 * iStride] * wr;
    out[outBase + k * outStride] = o;
  }
}

/**
 * 把源矩形 (x0, y0, w, h) 面积平均缩到 (nY, nX) 网格（lighting.py:90 _box_resample，
 * 对应 ``g.drawImage(源, sx, sy, sw, sh, 0, 0, N, N)``）。
 * src 是 float01、行主序 (hPx, wPx, nCh)；返回 float01 (nY, nX, nCh)。
 * 通道各自独立重采样（非预乘）；源外区域按透明黑计入分母（零填充）。
 * 退化（nX/nY/w/h/源尺寸任一非正）返回全零 —— 对应 Python 同分支。
 */
function boxResample(src, wPx, hPx, nCh, x0, y0, w, h, nX, nY) {
  const out = new Float64Array(nX * nY * nCh);
  if (nX <= 0 || nY <= 0 || w <= 0 || h <= 0 || wPx <= 0 || hPx <= 0) return out;
  // 目的格边界：numpy 是 x0 + (w*arange)/n，顺序不能换（幂二次数下两者等价，
  // 非 2 的幂会有末位差）
  const xe = new Float64Array(nX + 1);
  const ye = new Float64Array(nY + 1);
  for (let k = 0; k <= nX; k++) xe[k] = x0 + (w * k) / nX;
  for (let k = 0; k <= nY; k++) ye[k] = y0 + (h * k) / nY;
  const rowStride = wPx * nCh;
  // ① 行方向：flat (hPx, wPx, nCh) → (nY, wPx, nCh)
  const rows = new Float64Array(nY * rowStride);
  const pre = new Float64Array(hPx + 1);
  for (let x = 0; x < wPx; x++) {
    for (let ch = 0; ch < nCh; ch++) {
      axisSum(src, x * nCh + ch, rowStride, hPx, ye, nY, rows, x * nCh + ch,
              rowStride, pre);
    }
  }
  // ② 列方向：rows 视作 (wPx, nY, nCh) 的转置 → (nX, nY, nCh)，再转回 (nY, nX, nCh)
  const cols = new Float64Array(nX * nY * nCh);
  const pre2 = new Float64Array(wPx + 1);
  const colStride = nY * nCh;
  for (let k = 0; k < nY; k++) {
    for (let ch = 0; ch < nCh; ch++) {
      axisSum(rows, k * rowStride + ch, nCh, wPx, xe, nX, cols, k * nCh + ch,
              colStride, pre2);
    }
  }
  // ③ 除以目的格面积（Python：grid /= diff(ye)[:,None] * diff(xe)[None,:]）
  for (let k = 0; k < nY; k++) {
    const dy = ye[k + 1] - ye[k];
    for (let j = 0; j < nX; j++) {
      const den = dy * (xe[j + 1] - xe[j]);
      const g = (k * nX + j) * nCh;
      const c = (j * nY + k) * nCh;
      for (let ch = 0; ch < nCh; ch++) out[g + ch] = cols[c + ch] / den;
    }
  }
  return out;
}

/** RGBA 字节 → float01 (wPx, hPx, nCh)。nCh=3 取 RGB（照片 = Python 的 rgb01），
 *  nCh=4 连 alpha 一起（立绘 = Python 的 char_rgba01）。uint8 → /255.0 一次舍入。 */
function rgba01(rgba, wPx, hPx, nCh) {
  const n = wPx * hPx;
  const f = new Float64Array(n * nCh);
  for (let i = 0; i < n; i++) {
    const s = i * 4, d = i * nCh;
    for (let c = 0; c < nCh; c++) f[d + c] = rgba[s + c] / 255.0;
  }
  return f;
}

/* ── 采样 ─────────────────────────────────────────────────────────────── */

/**
 * 采样框钳制 —— clampRegion 的 sx/sy 公式（lighting.py:132，index.html:1244-1251）。
 * ⚠ ``sw>W`` 越界**零填充**语义如实保留：这里**不预夹 sw/sh**，只套 JS 的
 *   ``sx = max(0, min(W-sw, sx))`` —— sw>W 时 W-sw<0 → sx 被夹到 0 而 sw 原样
 *   保留，窗口越过图像右/下边界，越界段在降采样里按透明黑计入均值（把 lum 拉低）。
 *   **不要**改成先 ``sw = min(sw, W)`` 再夹 —— 那正是把黑像素洗掉的 clamp 版
 *   （compose.py:496 的 _clamp_region 才是先缩尺寸的那份，lightRegionRect 用它）。
 * region 为 null/undefined 时按原版 ``!r`` 分支退化为整帧。
 */
function clampRegion(region, width, height) {
  if (region === null || region === undefined) {
    return [0.0, 0.0, width, height];
  }
  const sw = region.w, sh = region.h;
  const sx = Math.max(0.0, Math.min(width - sw, region.x));
  const sy = Math.max(0.0, Math.min(height - sh, region.y));
  return [sx, sy, sw, sh];
}

/**
 * 采样框 → N×N 降采样 → 三通道均值 + 逐级亮度分布（lighting.py:155 measure_region，
 * index.html:1212-1231 + clampRegion 1244-1251）。N = levels（Python 侧恒定 32）。
 *
 * ⚠ 直方图桶下标在精确边界上加 1e-9 保护：Rec.709 加权和落在 [i/n, (i+1)/n) 边界
 *   上时，浮点误差（如 0.625*8 → 4.999999999999999）会把亮度分到下一桶。
 * ⚠ 退化分支（region 退化后 sw/sh <= 0、源尺寸为 0）返回全零采样（levels 全 0、
 *   mean 全 0）—— 对应原版返回 null，调用方应按「无有效采样」处理。
 *
 * @param rgba   RGBA 字节（长 w*h*4，只用 RGB 三通道）
 * @param region null | {x,y,w,h}（浮点、画布坐标）
 * @returns {{levels: number[], mean: [number, number, number]}}
 */
export function measureRegion(rgba, w, h, region, levels) {
  const lv = Math.trunc(Number(levels));
  if (!(lv >= 1)) throw new Error(`levels must be >= 1, got ${levels}`);
  const wPx = Math.trunc(Number(w));
  const hPx = Math.trunc(Number(h));
  const [sx, sy, sw, sh] = clampRegion(region, wPx, hPx);
  if (wPx <= 0 || hPx <= 0 || sw <= 0 || sh <= 0) {
    const zero = new Array(lv).fill(0.0);
    return { levels: zero, mean: [0.0, 0.0, 0.0] };
  }
  const grid = boxResample(rgba01(rgba, wPx, hPx, 3), wPx, hPx, 3,
                           sx, sy, sw, sh, lv, lv);
  const n = lv * lv;
  const mean = [0.0, 0.0, 0.0];
  for (let ch = 0; ch < 3; ch++) mean[ch] = pairwiseSum(grid, ch, n, 3) / n;
  const hist = new Float64Array(lv);
  for (let i = 0; i < n; i++) {
    const o = i * 3;
    const lum = LUM_W[0] * grid[o] + LUM_W[1] * grid[o + 1] + LUM_W[2] * grid[o + 2];
    let idx = Math.floor(lum * lv + 1e-9);
    if (idx > lv - 1) idx = lv - 1;
    if (idx < 0) idx = 0;
    hist[idx] += 1.0;
  }
  const out = new Array(lv);
  for (let i = 0; i < lv; i++) out[i] = hist[i] / n;
  return { levels: out, mean: mean };
}

/* ── 颗粒噪声砖（lighting.py:210，index.html:1168-1180）────────────────── */

let _grainTile = null;

/**
 * 256×256 固定种子噪声砖，值域 [-1, 1)（lighting.py:210 grain_tile）。
 * xorshift32、种子 0x9E3779B9，``s >>> 17`` 是无符号右移、``s / 2147483648``
 * 线性映射到 [-1,1)。⚠ 必须按 32 位回绕实现（JS 位运算天然 int32，``<<`` 回绕、
 * ``>>>`` 逻辑右移，与 Python 手工 & 0xFFFFFFFF 后调符号逐位一致）。
 * **只读**：调用方不得改写（Python 用 setflags(write=False 钉住，JS 侧模块单例
 * 本身就是全进程同一张，误改写会静默污染后续每一帧 —— 靠注释与自检守住）。
 */
export function grainTile() {
  if (_grainTile === null) _grainTile = _grainTileBuild();
  return _grainTile;
}

/** xorshift32 序列本体（lighting.py:236 _grain_tile_build）。 */
function _grainTileBuild() {
  let s = (0x9E3779B9 - 0x100000000) | 0;      // |0 → int32
  const out = new Float64Array(256 * 256);
  for (let i = 0; i < out.length; i++) {
    s ^= (s << 13);                            // int32 回绕
    s ^= (s >>> 17);                           // 无符号右移
    s ^= (s << 5);                             // int32 回绕
    out[i] = s / 2147483648.0;                 // → [-1, 1)
  }
  return out;
}

/**
 * 立绘屏幕/原生宽度比的分档（1 / 0.5 / 0.25 / 0.125 …，lighting.py:257
 * lit_scale_bucket，index.html:1455-1458）：clampN(s, 0.05, 1) 后
 * ``2^jsRound(log2(s))``，s>=1 返回 1。
 */
export function litScaleBucket(scale) {
  const s = clampN(scale, 0.05, 1.0);
  if (s >= 1.0) return 1.0;
  return Math.pow(2.0, jsRound(Math.log2(s)));
}

/* ── 立绘自身统计（charLum，lighting.py:270）──────────────────────────── */

/**
 * 立绘自身 (亮度, 饱和度, R 均值, B 均值) —— charLum 的口径（lighting.py:270）。
 * 缩到宽 32、高 ``max(1, jsRound(H/W*32))``；**alpha < 8 跳过**（阈值 8/255，
 * 不是 0）；n==0 时亮度按原版返回 0.5，其余统计离线无残留态按 0。
 *
 * ⚠ alpha 判据按原版 ``getImageData`` 的**字节**语义先量化再比：drawImage 缩采样后
 *   读回的是 0..255 整数，``d[i+3] < 8`` 比的是字节；浮点 alpha 在精确 8/255 边界
 *   上会因求和顺序落在 7.9999999999998x / 8.0000000000000x 两侧（整幅 alpha=8 的
 *   立绘实测 1/3 像素被误跳过），round-half-even 量化后比较与原版逐位一致。
 *
 * @param rgba RGBA 字节（长 w*h*4）
 * @returns {{lum: number, sat: number, r: number, b: number}}
 */
export function charStats(rgba, w, h) {
  const wPx = Math.trunc(Number(w));
  const hPx = Math.trunc(Number(h));
  if (!(wPx > 0) || !(hPx > 0)) return { lum: 0.5, sat: 0.0, r: 0.0, b: 0.0 };
  const nX = 32;
  const nY = Math.max(1, jsRound(hPx / wPx * 32));
  const grid = boxResample(rgba01(rgba, wPx, hPx, 4), wPx, hPx, 4,
                           0.0, 0.0, wPx, hPx, nX, nY);
  const n = nX * nY;
  // 实心像素的 RGB（0..255）—— 布尔掩码选出的连续数组，numpy 的 mean 走 pairwise
  const rr = new Float64Array(n), gg = new Float64Array(n), bb = new Float64Array(n);
  let cnt = 0;
  for (let i = 0; i < n; i++) {
    const o = i * 4;
    const a = rintHalfEven(clampN(grid[o + 3], 0.0, 1.0) * 255.0);   // 字节量化
    if (a < 8) continue;
    rr[cnt] = grid[o] * 255.0;
    gg[cnt] = grid[o + 1] * 255.0;
    bb[cnt] = grid[o + 2] * 255.0;
    cnt++;
  }
  if (cnt === 0) return { lum: 0.5, sat: 0.0, r: 0.0, b: 0.0 };
  const lumArr = new Float64Array(cnt);
  const satArr = new Float64Array(cnt);
  for (let i = 0; i < cnt; i++) {
    lumArr[i] = LUM_W[0] * rr[i] + LUM_W[1] * gg[i] + LUM_W[2] * bb[i];
    const mx = Math.max(rr[i], gg[i], bb[i]);
    const mn = Math.min(rr[i], gg[i], bb[i]);
    satArr[i] = mx > 0 ? (mx - mn) / mx : 0.0;      // HSV 的 S = (max-min)/max
  }
  return {
    lum: (pairwiseSum(lumArr, 0, cnt, 1) / cnt) / 255.0,
    sat: pairwiseSum(satArr, 0, cnt, 1) / cnt,
    r: (pairwiseSum(rr, 0, cnt, 1) / cnt) / 255.0,
    b: (pairwiseSum(bb, 0, cnt, 1) / cnt) / 255.0,
  };
}

/* ── 烘光 LUT（bakeLit 的三条曲线，lighting.py:315-359）───────────────── */

/** ``Math.round(clamp255(x))``：先夹 [0,255] 再 js_round（半向 +∞）。 */
function lutBytes(x) {
  return Math.floor(clampN(x, 0.0, 255.0) + 0.5);
}

/** measureRegion 的 sat：HSV 的 S = (max-min)/max，max 为 0 时取 0。 */
function envSat(mean) {
  const mx = Math.max(mean[0], mean[1], mean[2]);
  const mn = Math.min(mean[0], mean[1], mean[2]);
  return mx > 0.0 ? (mx - mn) / mx : 0.0;
}

/**
 * 构建 bakeLit 的三条 256 项 LUT，返回 {lutR, lutG, lutB, sK}
 * （lighting.py:327 _lit_luts，index.html:1319-1372）：
 *   gL  = clampN(lum/0.42, 0.6, 1.15)；inv = (1/gL)^0.6（gamma 指数软化）
 *   envRB  = max(.05,LIGHT.r)/max(.05,LIGHT.b)；charRB = max(.05,chR)/max(.05,chB)
 *   charRBg = charRB^inv —— 白平衡基准取"过完曲线之后"的色比
 *   ratio = (envRB/charRBg)^0.75；gRfull = sqrt(ratio)；gRcap 夹 [0.85, 1.18]
 *   sK  = clampN(1 + (env_sat - chSat)*0.3, 0.75, 1.25)
 *   cur = 255*(v/255)^inv；w = 0 / 1 / (v-165)/50（v<=165 / v>=215 / 中间）
 *   lutR = round(clamp255(cur*(gRfull+(gRcap-gRfull)*w)))
 *   lutB = round(clamp255(cur*(1/gRfull+(1/gRcap-1/gRfull)*w)))
 *   lutG = round(clamp255(cur))
 * 三条边界（165 / 215 及 gRfull→gRcap 过渡）的取整一律 js_round（半向 +∞）。
 */
function litLuts(envLum, envR, envB, envSatV, chSat, chR, chB) {
  const gL = clampN(envLum / LIGHT_NEUTRAL, LIGHT_GAIN_MIN, LIGHT_GAIN_MAX);
  const inv = Math.pow(1.0 / gL, 0.6);
  const envRB = Math.max(0.05, envR) / Math.max(0.05, envB);
  const charRB = Math.max(0.05, chR) / Math.max(0.05, chB);
  const charRBg = Math.pow(charRB, inv);
  const ratio = Math.pow(envRB / charRBg, WB_K);
  const gRfull = Math.sqrt(ratio);
  const gRcap = gRfull < GMIN ? GMIN : (gRfull > GMAX ? GMAX : gRfull);
  const sK = clampN(1.0 + (envSatV - chSat) * SAT_K, 0.75, 1.25);
  const lutR = new Uint8Array(256), lutG = new Uint8Array(256),
        lutB = new Uint8Array(256);
  for (let v = 0; v < 256; v++) {
    const cur = Math.pow(v / 255.0, inv) * 255.0;
    const wv = v <= 165 ? 0.0 : (v >= 215 ? 1.0 : (v - 165) / 50.0);
    lutR[v] = lutBytes(cur * (gRfull + (gRcap - gRfull) * wv));
    lutB[v] = lutBytes(cur * (1.0 / gRfull + (1.0 / gRcap - 1.0 / gRfull) * wv));
    lutG[v] = lutBytes(cur);
  }
  return { lutR: lutR, lutG: lutG, lutB: lutB, sK: sK };
}

/* ── 立绘烘光（bakeLit，lighting.py:375，index.html:1285-1447）────────── */

/**
 * 把立绘按采样到的环境亮度/色温烘一版（gamma 曲线 + 白平衡 + 饱和 + 颗粒）。
 * 对应 bakeLit + charLum + grainTile + litScaleBucket。
 *
 * ``sample`` 为 null/undefined 时按原版「总开关关 / 采样无效」分支**原样返回立绘**
 * （字节直通，仍是新数组，不改入参）。``grain`` 为假只门控颗粒那一段（原版
 * LIGHT_GRN 开关）—— 曲线/白平衡/饱和的数值一个字节都不动，关掉颗粒不等于关掉光。
 * ``grainScale`` 是颗粒强度倍率，乘在原版 gAmp 上；1.0 时与原版逐位一致。
 * ``canvasW`` = 立绘落在画布上的目标宽（Python 的 canvas[0]），只用于颗粒缩放
 * 补偿：litScale = canvasW/wPx，补偿 = clampN(1/litScaleBucket(litScale), 1, 3.2)；
 * 传 0/非正值时补偿退化为 1（不补偿）。
 *
 * LUT 语义三行摘要：
 *   1. 明暗走**曲线**不走乘法：out = 255·(v/255)^inv，inv = (1/gL)^0.6，
 *      gL = clamp(环境亮度/0.42, 0.6, 1.15) —— 亮环境提亮、暗环境压暗但保亮部反差。
 *   2. 白平衡在曲线**之后**按线性增益乘：R 增益 gRfull→gRcap（高光限幅 [0.85,1.18]），
 *      B 增益取倒数（gR·gB=1，不动整体明暗）；增益基准是过完曲线的色比 charRB^inv。
 *   3. 逐字节出口：LUT 内 js_round，饱和度混合与颗粒在浮点里做，最终落字节用
 *      round-half-even；透明像素 RGB 清零、alpha 原样（0 仍是 0）。
 *
 * @param rgba      RGBA 字节（长 w*h*4），**不被修改**
 * @param sample    null | measureRegion 的产物（{levels, mean}）
 * @returns {Uint8ClampedArray} 新的 RGBA 字节（长 w*h*4）
 */
export function bakeLit(rgba, w, h, sample, canvasW, grain, grainScale) {
  const wPx = Math.trunc(Number(w));
  const hPx = Math.trunc(Number(h));
  const n = wPx * hPx;
  // 后两个开关按 Python 关键字缺省兜底（grain=True / grain_scale=1.0）：
  // grainScale 漏传会算出 NaN → gAmp>0 恒假 → 颗粒整段静默消失
  const useGrain = grain === undefined ? true : !!grain;
  const gs = grainScale === undefined ? 1.0 : Number(grainScale);
  const out = new Uint8ClampedArray(n * 4);
  for (let i = 0; i < n * 4; i++) out[i] = rgba[i];
  if (n <= 0 || wPx <= 0 || hPx <= 0) return out;
  if (sample === null || sample === undefined) return out;      // 字节直通
  const st = charStats(rgba, wPx, hPx);
  const mean = sample.mean;
  const envLum = LUM_W[0] * mean[0] + LUM_W[1] * mean[1] + LUM_W[2] * mean[2];
  const luts = litLuts(envLum, mean[0], mean[2], envSat(mean), st.sat, st.r, st.b);
  // 颗粒幅度：关掉颗粒时整段算法都不算（原版 gAmp 直接置 0）
  let gAmp = 0.0;
  if (useGrain) {
    const litScale = (canvasW > 0) ? (canvasW / wPx) : 1.0;
    gAmp = clampN(GRN_MAX * (1.0 - envLum * 1.35), GRN_MIN, GRN_MAX)
         * clampN(1.0 / litScaleBucket(litScale), 1.0, GRN_COMP_MAX)
         * gs;
  }
  const tile = (useGrain && gAmp > 0.0) ? grainTile() : null;
  const { lutR, lutG, lutB, sK } = luts;
  for (let y = 0; y < hPx; y++) {
    for (let x = 0; x < wPx; x++) {
      const i = (y * wPx + x) * 4;
      let r = lutR[rgba[i] | 0];
      let g = lutG[rgba[i + 1] | 0];
      let b = lutB[rgba[i + 2] | 0];
      if (sK !== 1.0) {
        const yy = LUM_W[0] * r + LUM_W[1] * g + LUM_W[2] * b;
        r = yy + (r - yy) * sK;
        g = yy + (g - yy) * sK;
        b = yy + (b - yy) * sK;
      }
      if (tile !== null) {
        const nl = tile[(y & 255) * 256 + (x & 255)] * gAmp;
        const nc = nl * 0.35;
        r = r + nl;
        g = g + nl - nc;
        b = b + nl + nc;
      }
      const a = rgba[i + 3] | 0;
      if (a !== 0) {
        out[i] = rintHalfEven(clampN(r, 0.0, 255.0));
        out[i + 1] = rintHalfEven(clampN(g, 0.0, 255.0));
        out[i + 2] = rintHalfEven(clampN(b, 0.0, 255.0));
        out[i + 3] = a;
      } else {
        out[i] = 0;                     // 透明像素：RGB 清零、alpha 原样（0）
        out[i + 1] = 0;
        out[i + 2] = 0;
      }
    }
  }
  return out;
}

/* ── 采光框（lightRegion + clampRegion，compose.py:470-503）───────────── */

/** clampRegion（index.html:1244-1251，compose.py:496）：先缩尺寸、再夹原点，
 *  顺序不能换。与上面 lighting.py:132 的那份是两个口径（这份预夹 sw/sh），
 *  lightRegionRect 用的是这份 —— 原版 sampleLight 按视频尺寸 clamp 的那句。 */
function clampRegionSnap(sx, sy, sw, sh, width, height) {
  sw = Math.min(sw, width);
  sh = Math.min(sh, height);
  sx = Math.max(0.0, Math.min(width - sw, sx));
  sy = Math.max(0.0, Math.min(height - sh, sy));
  return { x: sx, y: sy, w: sw, h: sh };
}

/**
 * 画布坐标的采样矩形（compose.py:470 _light_region_rect = lightRegion +
 * clampRegion）。无立绘（currentPlacement 为 null）时退化成整帧（原版 clampRegion
 * 的 ``!r`` 分支）。内部走 arcam-core.js 的 currentPlacement（与 composeShot 的
 * 落位同源同一次计算结果），不另抄一份。
 *
 * opts（同 Python 关键字参数）：
 *   mode / sizePct / charScale / rotBasis / shot / edgeBar —— 原样转给落位；
 *   rect —— 自由模式吸附后的目标矩形（本遍的 frameRect）。原版 lightRegion 读的
 *   也是吸附后的 currentPlacement，所以 rect 必须传，否则自由模式采光框会偏。
 *
 * @returns {{x: number, y: number, w: number, h: number} | null}
 */
export function lightRegionRect(charW, charH, subj, cam, opts) {
  const o = opts || {};
  const W = Math.trunc(Number(cam.W));
  const H = Math.trunc(Number(cam.H));
  const c = currentPlacement(charW, charH, subj, cam, {
    mode: o.mode === undefined ? 'ground' : o.mode,
    shot: o.shot,
    charScale: o.charScale,
    rotBasis: o.rotBasis,
    sizePct: o.sizePct,
    edgeBar: o.edgeBar,
    rect: o.rect,
  });
  if (c === null || c === undefined) {
    return { x: 0.0, y: 0.0, w: W, h: H };
  }
  const mx = c.w * 0.35;
  const sx = c.u - c.w / 2 - mx;
  const sy = c.v - c.h / 2 - mx;
  const sw = c.w + mx * 2;
  const sh = c.h + mx * 2;
  return clampRegionSnap(sx, sy, sw, sh, W, H);
}
