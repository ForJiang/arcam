/**
 * J4 — AI 抠图（ISNet-anime + onnxruntime-web），ESM 模块，零框架零构建。
 *
 * 契约：docs/SITE-SPEC.md §4（GitHub Pages 纯客户端版）；算法逐条对应
 * src/arcam/matting.py（Python 侧等价实现）与上游 cutout.js（只读参考）。
 * 出处标注约定：
 *   cutout.js:NNN  = oss-teardown/repo/cutout.js 行号（原版 JS，IIFE 挂 window.ARCAM_CUT）
 *   matting.py:NNN = 本仓库 src/arcam/matting.py 行号（Python 移植，数值同口径）
 *   A-matting §x.y = oss-teardown/lhso-20261003/artifacts/A-matting-mmd-chars.md
 *
 * 与上游的结构差异（有意为之，非 bug）：
 *   · 上游是普通 script IIFE；本文件是 ESM module（SITE-SPEC §0），vendor/ 目录按
 *     import.meta.url 解析 —— ESM 里 document.currentScript 是 null
 *     （cutout.js:301-302 明确警告过这个坑），不能再照抄 currentScript.src。
 *   · 上游的缩放/合成全走 canvas drawImage；本文件把「预处理→推理→后处理→alpha 回贴」
 *     整条数学路径实现为**不依赖 DOM 的纯函数**（segPreprocess / segPostprocess /
 *     keepLargestBlob / resizeMaskBilinear），node 可直接喂普通数组自验；
 *     DOM 只负责两件事：取原图 ImageData、把结果写成 canvas。缩放滤波取双线性
 *     （上游 imageSmoothingQuality='high' 的实现相关、未指定核，matting.py:20-21
 *     已把这类偏离记入 SPEC §5；Python 侧预处理同取双线性、蒙版回贴取 BICUBIC，
 *     本文件回贴也取双线性，见 resizeMaskBilinear 注释）。
 *
 * 页面挂钩点（SITE-SPEC §3/§4，J3 只判存在性）：
 *   window.arcamMatte = async (imgBitmap或canvas, onProgress?) => canvas
 * 输入：HTMLCanvasElement / ImageBitmap / HTMLImageElement / OffscreenCanvas；
 * 输出：**新的** w×h canvas（RGB 原图不动，只换 alpha，cutout.js:740-744）；
 * fg < 0.02 抛「没有检出前景」（index.html:2703 闸门）；失败一律 throw 人话指引，
 * 不静默返回原图。
 */

// ─────────────────────────────────────────────────────────────────────────
// 常量（逐条对应上游写死值 / Python 参考实现）
// ─────────────────────────────────────────────────────────────────────────

export const SEG_SZ = 1024;                        // cutout.js:310 / matting.py:36
export const SEG_MEAN = [0.485, 0.456, 0.406];     // cutout.js:311 / matting.py:37（ImageNet）
export const SEG_STD = [0.229, 0.224, 0.225];      // cutout.js:312 / matting.py:38
export const SEG_LO = 0.28;                        // cutout.js:712 / matting.py:39（窄带下界）
export const SEG_HI = 0.46;                        // cutout.js:712 / matting.py:40（窄带上界）
export const FG_MIN = 0.02;                        // index.html:2703 / matting.py:41
export const MODEL_FILE = 'isnet-anime-q8.onnx';
export const MODEL_SIZE = 44214375;                // cutout.js:367（进度分母，不信任 content-length）
export const MODEL_SHA256 =
  'cb5f78506887f5250352c04ec8ea242874db3434f0b4a216d041a810b20377e3'; // cutout.js:368 / matting.py:46

/** 仓库统一取整口径（SITE-SPEC §0）：半向 +∞，等于 Python js_round。 */
export const jsRound = (v) => Math.floor(v + 0.5);

// ─────────────────────────────────────────────────────────────────────────
// 纯函数 ①：预处理 segPreprocess（cutout.js:619-664 / matting.py:103-151）
//   不依赖 DOM：rgba 为长度 w*h*4 的 uint8 RGBA（与 ImageData.data 同布局，
//   只取 RGB 三通道——「量化回 uint8 再双线性」的量化时点即输入本身的 8bit，
//   对应 Python 侧 float 输入先 _to_u8 再 resize，matting.py:19,132-135）。
// ─────────────────────────────────────────────────────────────────────────

/**
 * 原图 → 1024×1024 NCHW float32 + ImageNet 归一化。
 *
 * 五步（与 matting.py:103-151 逐步对应）：
 *   1. 长边等比缩到 1024：s = 1024/max(w,h)，nw/nh = max(1, jsRound(边·s))
 *   2. 8bit 域双线性缩放到 nw×nh（无 blur/median，cutout.js:141「去噪：无」）
 *   3. 居中贴方图：ox/oy = (1024-n)>>1（cutout.js:623,635）
 *   4. 补边 = **边缘延展**（最外一圈像素往外拉）。17 张评测集实测外圈 5%
 *      误判率中位数：拉伸 11.17% / 填灰 11.17% / **边缘延展 4.01%**
 *      （cutout.js:636-643；letterbox 几何下长边 round 后恰好铺满 1024，
 *      ox/oy 必有一个为 0、四角恒空操作，matting.py:119-121——照样实现，
 *      对任意几何都成立）
 *   5. 归一化 (x/255 - mean)/std，RGB 通道序、NCHW 平面布局，f32
 *      （cutout.js:658-663；无 BGR 翻转、不带 alpha）
 *
 * @returns {{tensor: Float32Array, ox: number, oy: number, nw: number, nh: number}}
 *   tensor 长度 3·1024·1024；crop 是图片区域在方图里的偏移/尺寸，
 *   后处理按它把蒙版裁回图片区域（cutout.js:663 + 681-690）。
 */
export function segPreprocess(rgba, w, h) {
  w = jsRound(w); h = jsRound(h);
  if (!(rgba && rgba.length >= w * h * 4)) {
    throw new Error(`抠图预处理： RGBA 数据长度 ${rgba ? rgba.length : 0} 小于 ${w}×${h}×4`);
  }
  if (!(w > 0 && h > 0)) throw new Error(`抠图预处理： 图像尺寸非法 ${w}×${h}`);
  const scale = SEG_SZ / Math.max(w, h);
  const nw = Math.max(1, jsRound(w * scale));
  const nh = Math.max(1, jsRound(h * scale));
  const ox = (SEG_SZ - nw) >> 1, oy = (SEG_SZ - nh) >> 1;
  const small = resizeRGBBilinear(rgba, w, h, nw, nh);        // 步骤 2（Uint8Array nw·nh·3）

  // 步骤 3+4：方图画布，先居中贴图，再把四条边各拉一圈 + 四角各拉一个像素。
  // 对应上游 8 次 ext()（cutout.js:647-655）；guard 掉 dw/dh<=0 的空操作。
  const cv = new Uint8Array(SEG_SZ * SEG_SZ * 3);
  for (let y = 0; y < nh; y++) {                               // 居中贴方图
    const src = y * nw * 3, dst = ((y + oy) * SEG_SZ + ox) * 3;
    for (let p = 0; p < nw * 3; p++) cv[dst + p] = small[src + p];
  }
  if (oy > 0) {                                               // 上/下：重复首/末行
    const top = new Uint8Array(nw * 3), bot = new Uint8Array(nw * 3);
    for (let p = 0; p < nw * 3; p++) { top[p] = small[p]; bot[p] = small[(nh - 1) * nw * 3 + p]; }
    for (let y = 0; y < oy; y++) {
      cv.set(top, y * SEG_SZ * 3 + ox * 3);
      cv.set(bot, (oy + nh + y) * SEG_SZ * 3 + ox * 3);
    }
  }
  if (ox > 0) {                                               // 左/右：重复首/末列
    for (let y = 0; y < nh; y++) {
      const row = (y + oy) * SEG_SZ;
      for (let x = 0; x < ox; x++) {
        cv[(row + x) * 3] = small[(y * nw) * 3];
        cv[(row + x) * 3 + 1] = small[(y * nw) * 3 + 1];
        cv[(row + x) * 3 + 2] = small[(y * nw) * 3 + 2];
        const lx = ox + nw + x;
        cv[(row + lx) * 3] = small[(y * nw + nw - 1) * 3];
        cv[(row + lx) * 3 + 1] = small[(y * nw + nw - 1) * 3 + 1];
        cv[(row + lx) * 3 + 2] = small[(y * nw + nw - 1) * 3 + 2];
      }
    }
  }
  if (ox > 0 && oy > 0) {                                     // 四角：角像素外拉（letterbox 下恒空）
    const corners = [small[0], small[1], small[2],               // 左上
      small[(nw - 1) * 3], small[(nw - 1) * 3 + 1], small[(nw - 1) * 3 + 2], // 右上
      small[(nh - 1) * nw * 3], small[(nh - 1) * nw * 3 + 1], small[(nh - 1) * nw * 3 + 2], // 左下
      small[(nh - 1) * nw * 3 + (nw - 1) * 3], small[(nh - 1) * nw * 3 + (nw - 1) * 3 + 1],
      small[(nh - 1) * nw * 3 + (nw - 1) * 3 + 2]];                // 右下
    const put = (x, y, c) => {
      const p = (y * SEG_SZ + x) * 3;
      cv[p] = c[0]; cv[p + 1] = c[1]; cv[p + 2] = c[2];
    };
    for (let y = 0; y < oy; y++) {
      for (let x = 0; x < ox; x++) {
        put(x, y, corners.slice(0, 3));
        put(ox + nw + x, y, corners.slice(3, 6));
        put(x, oy + nh + y, corners.slice(6, 9));
        put(ox + nw + x, oy + nh + y, corners.slice(9, 12));
      }
    }
  }

  // 步骤 5：归一化 + NCHW 平面 f32（cutout.js:657-662 同布局同公式）
  const n = SEG_SZ * SEG_SZ;
  const f = new Float32Array(n * 3);
  const m0 = SEG_MEAN[0], m1 = SEG_MEAN[1], m2 = SEG_MEAN[2];
  const s0 = SEG_STD[0], s1 = SEG_STD[1], s2 = SEG_STD[2];
  for (let i = 0, p = 0; i < n; i++, p += 3) {
    f[i] = (cv[p] / 255 - m0) / s0;
    f[i + n] = (cv[p + 1] / 255 - m1) / s1;
    f[i + 2 * n] = (cv[p + 2] / 255 - m2) / s2;
  }
  return { tensor: f, ox, oy, nw, nh };
}

/**
 * 双线性缩放（RGB，u8 域，center-aligned 采样 —— 与 PIL resize 的中心对齐一致）。
 * 输出四邻域加权后 jsRound 回 u8；不信任任何插值捷径（SITE-SPEC §0 取整数口径）。
 * 用于 segPreprocess 的「长边→1024」；上游该步走 canvas drawImage
 * （imageSmoothingQuality='high'，实现相关），matting.py:132-135 同取双线性。
 */
export function resizeRGBBilinear(rgba, w, h, dw, dh) {
  const out = new Uint8Array(dw * dh * 3);
  const xr = w / dw, yr = h / dh;
  for (let dy = 0; dy < dh; dy++) {
    const sy = (dy + 0.5) * yr - 0.5;
    const y0 = Math.floor(sy), fy = sy - y0;
    const c0 = Math.min(Math.max(y0, 0), h - 1), c1 = Math.min(Math.max(y0 + 1, 0), h - 1);
    const r0 = c0 * w * 4, r1 = c1 * w * 4;
    for (let dx = 0; dx < dw; dx++) {
      const sx = (dx + 0.5) * xr - 0.5;
      const x0 = Math.floor(sx), fx = sx - x0;
      const d0 = Math.min(Math.max(x0, 0), w - 1), d1 = Math.min(Math.max(x0 + 1, 0), w - 1);
      const p00 = r0 + d0 * 4, p01 = r0 + d1 * 4, p10 = r1 + d0 * 4, p11 = r1 + d1 * 4;
      const w00 = (1 - fx) * (1 - fy), w01 = fx * (1 - fy), w10 = (1 - fx) * fy, w11 = fx * fy;
      const o = (dy * dw + dx) * 3;
      for (let c = 0; c < 3; c++) {
        out[o + c] = jsRound(
          rgba[p00 + c] * w00 + rgba[p01 + c] * w01 + rgba[p10 + c] * w10 + rgba[p11 + c] * w11);
      }
    }
  }
  return out;
}

// ─────────────────────────────────────────────────────────────────────────
// 纯函数 ②：后处理 segPostprocess（cutout.js:681-718 / matting.py:154-185）
// ─────────────────────────────────────────────────────────────────────────

/**
 * 模型软概率输出 → 窄带线性二值化（**不做 sigmoid**，q8 模型已把 sigmoid 烧进图内，
 * cutout.js:697-701：模型输出本身就是 0~1 概率）。
 *
 * 返回与模型输出同尺寸（outH×outW）的 Float64Array：图片区域 =
 * clip((p - 0.28)/0.18, 0, 1) 的窄带线性二值（cutout.js:712-718；下界从 0.42 压到
 * 0.28 防浅色头发/半透衣料被啃，同时收窄带子 HI−LO=0.18 保边缘不发灰），
 * **补边区一律 0**（cutout.js:681-683：不裁/不清的话补边会被当成角色一部分抠出来；
 * matting.py:183-184 同构返回全画布、区外置 0 —— 区外置 0 也让「补边区 0」可被单验）。
 *
 * @param raw 模型第一个输出的一维数据（length = outH*outW，[1,1,H,W] 或 [H,W] 拍平后）
 * @param crop [ox, oy, nw, nh] 来自 segPreprocess
 */
export function segPostprocess(raw, crop, outH, outW) {
  const [ox, oy, nw, nh] = crop.map(Number);
  outH = Number(outH); outW = Number(outW);
  if (!(outH > 0 && outW > 0)) throw new Error(`抠图后处理： 模型输出尺寸非法 ${outH}×${outW}`);
  if (ox < 0 || oy < 0 || nw < 1 || nh < 1 || ox + nw > outW || oy + nh > outH) {
    throw new Error(`抠图后处理： crop (${ox},${oy},${nw},${nh}) 超出模型输出尺寸 ${outW}×${outH}`);
  }
  if (raw.length !== outH * outW) {
    throw new Error(`抠图后处理： 数据 ${raw.length} 个与输出尺寸 ${outW}×${outH} 不符`);
  }
  const inv = 1 / (SEG_HI - SEG_LO);
  const out = new Float64Array(outH * outW);          // 补边区 0
  for (let y = 0; y < nh; y++) {
    const src = (y + oy) * outW + ox, dst = src;      // out 与 raw 同布局，dst=src
    for (let x = 0; x < nw; x++) {
      let a = (raw[src + x] - SEG_LO) * inv;
      out[dst + x] = a < 0 ? 0 : a > 1 ? 1 : a;
    }
  }
  return out;
}

// ─────────────────────────────────────────────────────────────────────────
// 纯函数 ③：keepLargestBlob（cutout.js:554-606 / matting.py:188-221）
// ─────────────────────────────────────────────────────────────────────────

/**
 * 去杂点 + 补洞：海报标题/logo/水印/背景碎片在蒙版上都是孤立小块，角色永远是
 * 最大的一整块（cutout.js:539-546：鸣潮海报实测碎片把前景占到 74~75%，
 * 正常立绘只 20~40%）。两步洪水填充，1024² 几十毫秒：
 *   ① **8 连通**标连通域只留最大那块（4 连会把斜着的细线——发丝、描边——
 *      判成两块丢掉，cutout.js:552-553；并列最大取扫描顺序第一块，与 Python
 *      argmax 同口径）
 *   ② 从四边往里 **4 连通**泛洪「外部背景」，泛不到的就是角色内部的洞
 *      （闭着的眼睛、衣服镂空），补上。⚠ 被丢弃的小连通域对泛洪是障碍：
 *      整个被最大域包住的小块会被当成洞补回来（cutout.js:588-592 同样行为，如实保留）
 * ⚠ 保下来的像素**用回原来的软值**，不要统统写满值——写死会把二值化的
 *   锯齿边暴露出来（cutout.js:602-604）。
 * 二值阈值取 0.5：与上游 u8 域的 >=128 逐位等价（jsRound(v*255)>=128 ⟺ v>=0.5，
 * matting.py:191 同口径）。
 *
 * @param mask Float64Array/Array（W*H，[0,1] 软值；本函数不改入参）
 * @returns {{mask: Float64Array, kept: number}} kept = 最大域+被补洞的像素数
 *   （cutout.js:721,746 的 fg 分子；全背景输入返回全 0 + kept=0，对应 cutout.js:583）
 */
export function keepLargestBlob(mask, W, H) {
  W = jsRound(W); H = jsRound(H);
  const N = W * H;
  if (mask.length !== N) throw new Error(`keepLargestBlob: 数据 ${mask.length} ≠ ${W}×${H}`);
  const on = new Uint8Array(N);
  for (let i = 0; i < N; i++) on[i] = mask[i] >= 0.5 ? 1 : 0;
  const lab = new Int32Array(N);
  const stack = new Int32Array(N);
  let best = 0, bestSize = 0, cur = 0;
  for (let p0 = 0; p0 < N; p0++) {                     // ① 8 连通标域
    if (!on[p0] || lab[p0]) continue;
    cur++;
    let sp = 0, size = 0;
    stack[sp++] = p0; lab[p0] = cur;
    while (sp > 0) {
      const q = stack[--sp];
      size++;
      const x = q % W, y = (q / W) | 0;
      for (let dy = -1; dy <= 1; dy++) {
        const yy = y + dy; if (yy < 0 || yy >= H) continue;
        for (let dx = -1; dx <= 1; dx++) {
          if (!dx && !dy) continue;
          const xx = x + dx; if (xx < 0 || xx >= W) continue;
          const r = yy * W + xx;
          if (on[r] && !lab[r]) { lab[r] = cur; stack[sp++] = r; }
        }
      }
    }
    if (size > bestSize) { bestSize = size; best = cur; }
  }
  if (!best) return { mask: new Float64Array(N), kept: 0 };   // 一个前景都没有
  for (let i = 0; i < N; i++) on[i] = lab[i] === best ? 1 : 0;
  const outside = new Uint8Array(N);                   // ② 边界泛洪外部背景（4 连通）
  let sp = 0;
  const push = (i) => { if (!outside[i] && !on[i]) { outside[i] = 1; stack[sp++] = i; } };
  for (let x = 0; x < W; x++) { push(x); push((H - 1) * W + x); }
  for (let y = 1; y < H - 1; y++) { push(y * W); push(y * W + W - 1); }
  while (sp > 0) {
    const q = stack[--sp];
    const x = q % W, y = (q / W) | 0;
    if (x > 0) push(q - 1);
    if (x < W - 1) push(q + 1);
    if (y > 0) push(q - W);
    if (y < H - 1) push(q + W);
  }
  const res = new Float64Array(N);
  let kept = 0;
  for (let i = 0; i < N; i++) {
    if (on[i] || !outside[i]) { res[i] = mask[i]; kept++; }   // 最大域 or 被补的洞：保原软值
  }
  return { mask: res, kept };
}

// ─────────────────────────────────────────────────────────────────────────
// alpha 回贴的纯数学部分（cutout.js:728-744 / matting.py:91-96,239-244）
// ─────────────────────────────────────────────────────────────────────────

/** 从全画布蒙版里按 crop 取图片区域（cutout.js:684-690 的裁回）。 */
export function cropMaskRegion(mask, crop, MW) {
  const [ox, oy, nw, nh] = crop.map(Number);
  const r = new Float64Array(nw * nh);
  for (let y = 0; y < nh; y++) {
    const src = (y + oy) * MW + ox, dst = y * nw;
    for (let x = 0; x < nw; x++) r[dst + x] = mask[src + x];
  }
  return r;
}

/**
 * 蒙版缩回原尺寸（cutout.js:728-735 的「1024→原图大幅下/上采样」）。
 * float64 域先插值再量化回 u8（matting.py:243-244 同：避免二次量化损失）；
 * 上游该步走 canvas imageSmoothingQuality='high'，Python 侧取 BICUBIC 近似它 ——
 * **契约外偏离（如实记录）**：本文件回贴取双线性，与上游/Python 三者互不等同，
 *   属同一类「浏览器高质量平滑的实现相关」偏离（matting.py:20-21 已记）；
 *   预处理双线性则与 Python 完全同口径。
 */
export function resizeMaskBilinear(region, sw, sh, dw, dh) {
  const out = new Float64Array(dw * dh);
  const xr = sw / dw, yr = sh / dh;
  for (let dy = 0; dy < dh; dy++) {
    const sy = (dy + 0.5) * yr - 0.5;
    const y0 = Math.floor(sy), fy = sy - y0;
    // 边界：floor 不 clamp、只在取采样时 clamp —— sx<0（上采样左/上缘）时与
    // PIL 一样做边缘复制，而不是朝内点外插（与 resizeRGBBilinear 同一修正）。
    const c0 = Math.min(Math.max(y0, 0), sh - 1), c1 = Math.min(Math.max(y0 + 1, 0), sh - 1);
    for (let dx = 0; dx < dw; dx++) {
      const sx = (dx + 0.5) * xr - 0.5;
      const x0 = Math.floor(sx), fx = sx - x0;
      const d0 = Math.min(Math.max(x0, 0), sw - 1), d1 = Math.min(Math.max(x0 + 1, 0), sw - 1);
      out[dy * dw + dx] =
        region[c0 * sw + d0] * (1 - fx) * (1 - fy) +
        region[c0 * sw + d1] * fx * (1 - fy) +
        region[c1 * sw + d0] * (1 - fx) * fy +
        region[c1 * sw + d1] * fx * fy;
    }
  }
  return out;
}

// ─────────────────────────────────────────────────────────────────────────
// ORT 运行时 / 模型的懒加载（cutout.js:497-537 的 ESM 等价改写）
// ─────────────────────────────────────────────────────────────────────────

const _state = { ort: null, buf: null, sess: null, loading: null };

/** vendor/ 的绝对 URL（含末尾斜杠）。
 *  ⚠ ESM 里没有 document.currentScript（cutout.js:301-302 的坑：module 下它是
 *  null，照抄就 404），改用 import.meta.url 解「本脚本邻居目录」——对
 *  GitHub Pages 项目子路径 /<仓库名>/matte.js 与站点根两种布局都成立。 */
const vendorURL = (name) => new URL('vendor/' + name, import.meta.url).href;
const vendorDirURL = () => new URL('vendor/', import.meta.url).href;

/** 注入一次 <script src>（幂等；与 cutout.js:315-325 loadScriptOnce 同结构）。
 *  ★ 必须走经典 script 注入而不是 dynamic import：ort.min.js 是 UMD 打包
 *  （尾部 typeof exports=="object"&&...(module.exports=ort)，v1.20.1），
 *  以模块方式 import() 时 exports/module 均未定义 → 不挂全局、命名空间为空，
 *  拿不到 ort；经典 script 下顶层 var ort 才落到 window.ort（cutout.js:503 同）。 */
function loadScriptOnce(src) {
  return new Promise((resolve, reject) => {
    const old = document.querySelector('script[data-arcam-vendor="' + src + '"]');
    if (old) {
      if (old.dataset.done) resolve();
      else { old.addEventListener('load', resolve, { once: true }); old.addEventListener('error', () => reject(new Error('加载失败: ' + src)), { once: true }); }
      return;
    }
    const s = document.createElement('script');
    s.src = src; s.async = true; s.dataset.arcamVendor = src;
    s.onload = () => { s.dataset.done = '1'; resolve(); };
    s.onerror = () => reject(new Error('加载失败: ' + src));
    document.head.appendChild(s);
  });
}

async function loadOrt(onProgress) {
  if (_state.ort) return _state.ort;
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    throw new Error('AI 抠图只能在浏览器里跑（当前环境没有 DOM）。'
      + '本模块导出的 segPreprocess/segPostprocess/keepLargestBlob 是不依赖 DOM 的纯函数，可在 node 单测。');
  }
  onProgress && onProgress(0, '载入运行时…');
  try {
    await loadScriptOnce(vendorURL('ort.min.js'));
  } catch (e) {
    throw new Error('AI 抠图运行时（onnxruntime-web）加载失败：' + vendorURL('ort.min.js')
      + '\n请确认 site/vendor/ 下四个文件齐全（ort.min.js / ort-wasm-simd-threaded.mjs /'
      + ' ort-wasm-simd-threaded.wasm / isnet-anime-q8.onnx，SITE-SPEC §4）。');
  }
  const ort = window.ort;
  if (!ort) throw new Error('onnxruntime 没挂上来（vendor/ort.min.js 加载后 window.ort 为空）。');
  ort.env.wasm.wasmPaths = vendorDirURL();   // 绝对 URL、末尾斜杠不能少（cutout.js:505）
  // ⚠ 多线程要 COOP/COEP 跨源隔离头，GitHub Pages 给不了 → 钉死单线程（cutout.js:506-507）
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.simd = true;                  // cutout.js:508
  _state.ort = ort;
  return ort;
}

/** 整块下载模型 + 校验。onProgress(ratio01, stage, gotBytes, totalBytes)。 */
async function fetchModel(url, onProgress) {
  let r;
  try {
    r = await fetch(url);
  } catch (e) {
    throw new Error(`模型文件拉取失败：${url}\n（${e && e.message}）\n`
      + '如果页面是用 file:// 直接打开的：AI 抠图要 http(s) 环境'
      + '（GitHub Pages，或本地起个 localhost 静态服务）——动态 import wasm 会被 file:// 拦死。');
  }
  if (!r.ok) {
    throw new Error(`模型文件下载失败：${url} → HTTP ${r.status}。`
      + '请确认 site/vendor/ 下四个文件齐全（SITE-SPEC §4；本站模型随页面分发，正常情况下不该 404）。');
  }
  // ★★ 绝不能按 content-length 预分配：Pages 对 .onnx 发 Content-Encoding: gzip，
  //   头是压缩后的假数字、reader 吐的是解压后的字节，预分配再 set 会 RangeError
  //   （cutout.js:333-339 踩过：本地怎么测都是好的，一上线就炸）。进度分母一律用
  //   写死的 MODEL_SIZE（cutout.js:522-525 同）。
  const chunks = [];
  let got = 0;
  const prog = () => onProgress && onProgress(
    0.02 + 0.86 * Math.min(got / MODEL_SIZE, 1), '下载模型…', got, MODEL_SIZE);
  if (r.body && typeof r.body.getReader === 'function') {
    const reader = r.body.getReader();
    for (; ;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      got += value.length;
      prog();
    }
  } else {
    const b = new Uint8Array(await r.arrayBuffer());
    chunks.push(b); got = b.length; prog();
  }
  const buf = new Uint8Array(got);
  let off = 0;
  for (const c of chunks) { buf.set(c, off); off += c.length; }
  if (buf.length !== MODEL_SIZE) {
    throw new Error(`模型大小不符：收到 ${buf.length} 字节，应为 ${MODEL_SIZE}。下到一半断了，再点一次。`);
  }
  onProgress && onProgress(0.9, '校验模型…');
  // sha256：crypto.subtle 只在安全上下文有，没有就跳过校验（cutout.js:391-392 同）
  if (typeof crypto !== 'undefined' && crypto.subtle && crypto.subtle.digest) {
    const d = await crypto.subtle.digest('SHA-256', buf);
    const hex = Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, '0')).join('');
    if (hex !== MODEL_SHA256) {
      throw new Error('模型 sha256 不符（下载途中被改坏了），再点一次重新下载。');
    }
  }
  return buf;
}

/** 幂等拿会话：运行时 + 模型 + InferenceSession 全部懒加载，失败可重试
 *  （cutout.js:498-536：_segSess/_segLoading 缓存 + finally 清 _segLoading）。 */
function ensureSession(onProgress) {
  if (_state.sess) return Promise.resolve(_state);
  if (_state.loading) return _state.loading;
  _state.loading = (async () => {
    const ort = await loadOrt(onProgress);
    if (!_state.buf) {
      _state.buf = await fetchModel(vendorURL(MODEL_FILE), onProgress);
    }
    onProgress && onProgress(0.93, '载入模型（建会话）…');
    _state.sess = await ort.InferenceSession.create(_state.buf, {
      executionProviders: ['wasm'],                       // cutout.js:532
    });
    onProgress && onProgress(0.97, '模型就绪');
    return _state;
  })();
  _state.loading.catch(() => { _state.loading = null; });  // 失败要能重试（cutout.js:535）
  return _state.loading;
}

// ─────────────────────────────────────────────────────────────────────────
// 主入口 arcamMatte（cutout.js:670-747 animeCut / matting.py:59-100 anime_cut）
// ─────────────────────────────────────────────────────────────────────────

/**
 * AI 抠图主入口（SITE-SPEC §4 挂钩点，J3 只判 window.arcamMatte 存在性）。
 *
 * @param input HTMLCanvasElement | ImageBitmap | HTMLImageElement | OffscreenCanvas
 * @param onProgress (ratio01, stage, gotBytes, totalBytes) => void
 *   ratio01 ∈ [0,1]：0 载运行时 → [0.02,0.88] 下载模型（分母=写死 MODEL_SIZE，
 *   不是 content-length）→ 0.9 校验 → 0.93 建会话 → 0.97 推理 → 1 完成。
 *   （签名是上游 onProgress(stage, ratio|null, got, total) 的 0..1 变体：
 *   SITE-SPEC §4/任务书要求「onProgress(0..1)」；stage/字节数作为附加参数带上，
 *   想用细信息的调用方从第二三参读。）
 * @returns HTMLCanvasElement 新的 w×h 画布（RGB=原图，alpha=蒙版）；
 *   附加只读信息 canvas.arcamMatte = {fg, ms}（契约只要求返回 canvas）。
 * @throws fg < 0.02 → 「没有检出前景」（index.html:2703 / matting.py:98-99）；
 *   模型/运行时任何失败都 throw 带人话指引，**不静默返回原图**。
 */
export async function arcamMatte(input, onProgress) {
  if (!input) throw new Error('抠图输入为空。');
  // ⚠ file:// 显式检测（上游只对 MediaPipe 那条线做了，cutout.js:106-109；ISNet 这条
  //   没有，但 ORT 1.20.1 即便 numThreads=1 也会动态 import vendor/ort-wasm-simd-threaded.mjs
  //   —— file:// 下浏览器会把它连 WASM 一起拦死，报出来的是 ORT 内部错，不如先说人话）
  if (typeof location !== 'undefined' && location.protocol === 'file:') {
    throw new Error('AI 抠图要在 http(s) 下跑（file:// 会拦掉 wasm 的动态 import）。'
      + '在 site 目录起个静态服务（如 python3 -m http.server）后开 http://localhost:8000/，'
      + '或直接用 GitHub Pages 版的页面。');
  }
  // ── DOM 之一：取原图像素（RGB 原图不动，最后只换 alpha） ──
  const w = (input.naturalWidth || input.width) | 0;
  const h = (input.naturalHeight || input.height) | 0;
  if (!(w > 0 && h > 0)) throw new Error(`抠图输入尺寸非法：${w}×${h}`);
  const sc = document.createElement('canvas');
  sc.width = w; sc.height = h;
  const sg = sc.getContext('2d', { willReadFrequently: true });
  sg.drawImage(input, 0, 0);
  const img = sg.getImageData(0, 0, w, h);               // RGBA u8，w*h*4

  // ── 懒加载：首次调用才拉 54MB（42MB 模型 + 12MB 运行时，cutout.js:286） ──
  const { sess, ort } = await ensureSession(onProgress);
  onProgress && onProgress(0.97, '推理中（慢是正常的）…');   // cutout.js:673（单线程 WASM 约几秒）
  const t0 = (typeof performance !== 'undefined' ? performance : Date).now();

  // ── 预处理（纯函数） ──
  const pre = segPreprocess(img.data, w, h);
  const tensor = new ort.Tensor('float32', pre.tensor, [1, 3, SEG_SZ, SEG_SZ]); // cutout.js:663

  // ── 推理：输入/输出名从会话读，不写死（matting.py:87 同口径；上游写死 'img'） ──
  const inName = (sess.inputNames && sess.inputNames[0]) || 'img';            // cutout.js:676
  const out = await sess.run({ [inName]: tensor });
  const firstName = (sess.outputNames && sess.outputNames[0]) || Object.keys(out)[0]; // cutout.js:677
  const first = out[firstName];
  const dims = first.dims;                        // [1,1,1024,1024]
  const MH = dims[dims.length - 2], MW = dims[dims.length - 1];               // cutout.js:679
  const raw = first.data;
  if (raw.length !== MH * MW) {
    throw new Error(`模型输出尺寸异常：dims=[${dims.join(',')}]，数据 ${raw.length} 个。`);
  }

  // ── 后处理（纯函数）：裁回 + 窄带二值 → 最大连通域 + 补洞 ──
  const mask = segPostprocess(raw, [pre.ox, pre.oy, pre.nw, pre.nh], MH, MW);
  const { mask: kept, kept: keptCount } = keepLargestBlob(mask, MW, MH);
  const fg = keptCount / (pre.nw * pre.nh);       // cutout.js:746 fg = kept/(W*H)
  if (fg < FG_MIN) {                              // index.html:2703 闸门
    throw new Error(`没有检出前景（前景占比 ${(fg * 100).toFixed(1)}%，低于 2%）。`
      + '可能图里没有角色/主体，或背景特别杂乱——换一张立绘试试；纯色底的图用本地版「抠纯色底」更快。');
  }

  // ── alpha 回贴：蒙版缩回原尺寸（纯函数） → 新画布，RGB 保留原图 ──
  const region = cropMaskRegion(kept, [pre.ox, pre.oy, pre.nw, pre.nh], MW);
  const alpha01 = resizeMaskBilinear(region, pre.nw, pre.nh, w, h);
  const o = document.createElement('canvas');
  o.width = w; o.height = h;
  const og = o.getContext('2d', { willReadFrequently: true });
  const outImg = og.createImageData(w, h);
  outImg.data.set(img.data);                      // RGB 原图（+原 alpha，马上覆写）
  for (let i = 0; i < w * h; i++) {
    const v = alpha01[i];
    outImg.data[i * 4 + 3] = v < 0 ? 0 : v > 1 ? 255 : jsRound(v * 255);   // cutout.js:743 只换 alpha
  }
  og.putImageData(outImg, 0, 0);
  const ms = Math.round((typeof performance !== 'undefined' ? performance : Date).now() - t0);
  onProgress && onProgress(1, '抠图完成');
  o.arcamMatte = { fg, ms };                      // 附注：契约只要求返回 canvas
  return o;
}

// ── 页面挂钩（SITE-SPEC §4；ESM 下无条件定义也安全，node 里自动跳过） ──
if (typeof window !== 'undefined') {
  window.arcamMatte = arcamMatte;
  // 通知页面钩子已就位（J3 app.js 监听的 'arcam:matte-ready'，覆盖
  // 「matte.js 由 index.html 独立以 module 方式加载、早于/晚于 app.js」两种时序）
  try {
    window.dispatchEvent(new Event('arcam:matte-ready'));
  } catch (e) { /* 老浏览器没有 Event 构造器：页面还有 load 兜底 */ }
}
