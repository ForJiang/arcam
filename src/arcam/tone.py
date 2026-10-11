"""环境融合 ①：把立绘图层往实拍画面的色域上拉一档（toneMatchedLayer）。

原版只作用在 3D 图层上（``draw3DLayer`` 里 ``TONE_ON = !!src`` 自守卫），
Python 侧按 SPEC 的口径在照片路径上以同一套六步算法处理 2D 立绘层。
算法全部在 **uint8 域**里算（与原版 ``Uint8ClampedArray`` 逐字节语义一致），
进出各做一次 0..1 ↔ 0..255 的量化。

已如实保留的原版行为 / 已知偏离：
- 原版 ``bgCtx`` 是 RGBA 画布、背景环带里带 alpha<8 判据；离线侧背景是 RGB
  照片帧，等价于"整块不透明"，故跳过 alpha 判据（不再判据）。
- 原版返回 canvas；Python 返回 float64 的 RGBA 数组（0..1）。``None`` 语义一致。
- 原版把诊断写进 ``window.__tone``；Python 侧由 :func:`tone_stats` 显式返回。
"""

from __future__ import annotations

import numpy as np

from arcam.numeric import js_round, js_to_fixed

#: index.html:3500 —— `let TONE_ON = false, TONE_K = 0.35;`（均值平移系数）
TONE_K: float = 0.35
#: index.html:3567 —— `kTone = rangeB < 90 ? 0.3 : 0.4`
TONE_K_DARK: float = 0.3
TONE_K_LIT: float = 0.4
#: index.html:3515/3533 —— 模型/背景两个自守卫的样本数下限
TONE_MIN_N: int = 500
#: index.html:3582 —— 全篇统一的 alpha 判据（>= 8 算"有内容"）
TONE_ALPHA_ON: int = 8


def _to_u8(arr01: np.ndarray) -> np.ndarray:
    """0..1 float → uint8。SPEC §1.2：clip 后 ``*255+0.5`` 再 astype(uint8)。"""
    return (np.clip(arr01, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _store(value: np.ndarray) -> np.ndarray:
    """JS ``Uint8ClampedArray`` 赋值语义：先夹 [0,255]，再四舍五入（**半数取偶**）。

    原版第 4/5 步写完 RGB 之后由 Uint8ClampedArray 隐式量化，
    ``Math.round`` 的"半向 +∞"在这里**不适用** —— 那是 Math.round 的语义。
    Python 内置 ``round`` 恰是半数取偶，与 ToUint8Clamp 一致。
    """
    v = np.clip(value, 0.0, 255.0)
    return np.rint(v).astype(np.uint8)


def _luma(rgb: np.ndarray) -> np.ndarray:
    """Rec.601 亮度 0.299R+0.587G+0.114B（index.html:3547/3573），float64 不取整。"""
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _percentile(hist: np.ndarray, tot: int) -> tuple[int, int]:
    """直方图 P5/P95：两端各累加到 >=5% 得 (lo, hi)，未触发则 0/255（index.html:3559-3563）。"""
    if tot == 0:
        return 0, 255
    acc = 0
    lo = 0
    for v in range(256):
        acc += int(hist[v])
        if acc >= tot * 0.05:
            lo = v
            break
    acc = 0
    hi = 255
    for v in range(255, -1, -1):
        acc += int(hist[v])
        if acc >= tot * 0.05:
            hi = v
            break
    return lo, hi


def _tone_prefix(layer_rgba01: np.ndarray, bg_rgb01: np.ndarray) -> dict | None:
    """tone_stats / tone_matched_layer 共用的前 5 步（index.html:3506-3570）。

    量化、包围盒、两个均值、亮度 P5/P95 直方图只算**一遍** —— 原先
    :func:`tone_matched_layer` 先调 :func:`tone_stats` 再把整段原样重算
    （3024×4032 照片帧上实测 1.26 s → 0.70 s）。⚠ tone_stats 的返回值是
    **取整后**的诊断量（js_round 过的均值），像素路径不能用，所以这里原样
    返回未取整的中间量，两个入口各取所需。

    三个自守卫（n<500 / 环带<4 / bn<500）任一不过 → None；形状不符 → ValueError。
    """
    layer = _to_u8(layer_rgba01)
    if layer.ndim != 3 or layer.shape[2] != 4:
        raise ValueError("layer_rgba01 必须是 (H,W,4)")
    h, w = layer.shape[0], layer.shape[1]
    bg = _to_u8(bg_rgb01)
    if bg.shape[0] != h or bg.shape[1] != w:
        raise ValueError("bg_rgb01 与 layer_rgba01 尺寸不一致")

    a = layer[..., 3]
    m = a >= TONE_ALPHA_ON
    ys, xs = np.nonzero(m)
    if ys.size == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    n = int(xs.size)
    if n < TONE_MIN_N:
        return None
    rgb = layer[..., :3].astype(np.float64)
    mr, mg, mb = (float(rgb[..., i][m].sum()) / n for i in range(3))

    pad = js_round(max(x1 - x0, y1 - y0) * 0.2) + 8
    rx = max(0, x0 - pad)
    ry = max(0, y0 - pad)
    rw = min(w, x1 + pad) - rx
    rh = min(h, y1 + pad) - ry
    if rw < 4 or rh < 4:
        return None

    gy = np.arange(ry, ry + rh)
    gx = np.arange(rx, rx + rw)
    inside = ((gx[None, :] >= x0) & (gx[None, :] <= x1) &
              (gy[:, None] >= y0) & (gy[:, None] <= y1))
    breg = bg[ry:ry + rh, rx:rx + rw, :].astype(np.float64)
    valid = ~inside
    bn = int(valid.sum())
    if bn < TONE_MIN_N:
        return None
    br, bgc, bb = (float(breg[..., i][valid].sum()) / bn for i in range(3))

    box = rgb[y0:y1 + 1, x0:x1 + 1]
    boxm = m[y0:y1 + 1, x0:x1 + 1]
    hmodel = np.bincount(np.floor(_luma(box[boxm])).astype(np.int64), minlength=256)
    m_hist = np.zeros(256, dtype=np.int64)
    m_hist[:hmodel.size] = hmodel
    hbg = np.bincount(np.floor(_luma(breg[valid])).astype(np.int64), minlength=256)
    b_hist = np.zeros(256, dtype=np.int64)
    b_hist[:hbg.size] = hbg
    mLo, mHi = _percentile(m_hist, n)
    bLo, bHi = _percentile(b_hist, bn)

    range_m = max(8, mHi - mLo)
    range_b = max(8, bHi - bLo)
    k_tone = TONE_K_DARK if range_b < 90 else TONE_K_LIT
    t_range = range_m + (range_b - range_m) * k_tone
    t_lo = mLo + (bLo - mLo) * k_tone
    return {
        "layer": layer, "bg": bg, "m": m, "rgb": rgb, "n": n,
        "mr": mr, "mg": mg, "mb": mb,
        "rx": rx, "ry": ry, "rw": rw, "rh": rh,
        "breg": breg, "valid": valid, "bn": bn, "br": br, "bgc": bgc, "bb": bb,
        "mLo": mLo, "mHi": mHi, "bLo": bLo, "bHi": bHi, "k_tone": k_tone,
        "t_lo": t_lo, "sc": t_range / range_m,
    }


def tone_stats(layer_rgba01: np.ndarray, bg_rgb01: np.ndarray) -> dict | None:
    """toneMatchedLayer 的诊断量（原版 ``window.__tone``，index.html:3617-3619）。

    与 :func:`tone_matched_layer` 走同一套前 5 步计算（:func:`_tone_prefix`），
    只是不改像素、只报数。返回 None 的条件与原版的三个自守卫完全一致。``sc``
    按原版 ``+sc.toFixed(3)`` 的口径走 :func:`arcam.numeric.js_to_fixed`
    （SPEC §1.4：禁止用 Python 的 ``round``/格式化串代替 ``toFixed``）。
    """
    p = _tone_prefix(layer_rgba01, bg_rgb01)
    if p is None:
        return None
    return {
        "model": [js_round(p["mr"]), js_round(p["mg"]), js_round(p["mb"])],
        "bg": [js_round(p["br"]), js_round(p["bgc"]), js_round(p["bb"])],
        "k": TONE_K, "kTone": p["k_tone"], "mLo": p["mLo"], "mHi": p["mHi"],
        "bLo": p["bLo"], "bHi": p["bHi"], "sc": js_to_fixed(p["sc"], 3),
        "n": p["n"], "bn": p["bn"],
    }


def tone_matched_layer(layer_rgba01: np.ndarray, bg_rgb01: np.ndarray) -> np.ndarray | None:
    """把立绘层往实拍背景的色域/明暗包络上拉一档（index.html:3501-3620）。

    六步：
      1. 模型 bbox + RGB 均值（alpha>=8 判据，``n < 500`` → None）；
      2. bbox 外扩 ``pad = js_round(max(w,h)*0.2)+8`` 的背景环带均值（跳过 bbox
         内部，``bn < 500`` → None）；
      3. Rec.601 亮度 P5/P95 直方图 → ``rangeM/rangeB = max(8,·)``、
         ``kTone = 0.3 if rangeB<90 else 0.4``、``tRange/tLo/sc``；
      4. 逐像素：``L0 = 0.299R+0.587G+0.114B``，
         ``g = L0>1 ? ((L0-mLo)*sc+tLo)/L0 : 1``，
         ``C' = clamp(C*g + (μbg-μmodel)*TONE_K, 0, 255)``；
      5. 边缘羽化（3×3 高斯 4/2/1 权重，只降 alpha），alpha 降了的 RGB 按
         ``t = 1-na/255`` 混向背景色；
      6. 返回新 RGBA（0..1 float64）。三个自守卫任一不过 → None。

    Args:
        layer_rgba01: 立绘层，float64 (H,W,4) 归一到 [0,1]。
        bg_rgb01: 实拍背景帧，float64 (H,W,3)，与 layer 同尺寸。

    Returns:
        新 RGBA float64 (H,W,4) 或 None。
    """
    p = _tone_prefix(layer_rgba01, bg_rgb01)
    if p is None:
        return None

    layer = p["layer"]
    a = layer[..., 3]
    m = p["m"]
    rgb = p["rgb"]
    mr, mg, mb = p["mr"], p["mg"], p["mb"]
    rx, ry, rw, rh = p["rx"], p["ry"], p["rw"], p["rh"]
    breg = p["breg"]
    br, bgc, bb = p["br"], p["bgc"], p["bb"]
    mLo = p["mLo"]
    t_lo = p["t_lo"]
    sc = p["sc"]

    out = layer.copy()
    # 4) 均值平移 + 范围重映射（只动 alpha>=8 的像素；alpha 一步都没动）
    l0 = _luma(rgb)
    g = np.where(l0 > 1, ((l0 - mLo) * sc + t_lo) / np.where(l0 > 1, l0, 1.0), 1.0)
    shift = np.array([br - mr, bgc - mg, bb - mb]) * TONE_K
    newrgb = np.empty_like(rgb)
    for i in range(3):
        newrgb[..., i] = rgb[..., i] * g + shift[i]
    stored = _store(newrgb)
    out[m, :3] = stored[m]

    # 5) 边缘羽化：4 邻域有 alpha<8 判边缘；3×3 高斯只降 alpha
    edge = _edge_mask(a)
    if edge.any():
        eys, exs = np.nonzero(edge)
        wgt = np.array([[1.0, 2.0, 1.0], [2.0, 4.0, 2.0], [1.0, 2.0, 1.0]])
        wsum = float(wgt.sum())
        sums = np.zeros(eys.size, dtype=np.float64)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                sums += a[eys + dy, exs + dx].astype(np.float64) * wgt[dy + 1, dx + 1]
        # `before` 是第 4 步之后同一份快照；alpha 在第 4 步未被改动，直接读 a 等价
        oas = a[eys, exs].astype(np.int64)
        na = np.floor(sums / wsum + 0.5).astype(np.int64)   # Math.round：半向 +∞
        drop = na < oas
        if drop.any():
            dyy, dxx = eys[drop], exs[drop]
            dna = na[drop]
            out[dyy, dxx, 3] = dna.astype(np.uint8)
            t = 1.0 - dna.astype(np.float64) / 255.0
            bi_ok = ((dxx >= rx) & (dxx < rx + rw) & (dyy >= ry) & (dyy < ry + rh))
            if bi_ok.any():
                bx = dxx[bi_ok] - rx
                by = dyy[bi_ok] - ry
                tt = t[bi_ok, None]
                cur = out[by, bx, :3].astype(np.float64)
                mixed = cur * (1.0 - tt) + breg[by, bx, :] * tt
                out[by, bx, :3] = _store(mixed)
    return out.astype(np.float64) / 255.0


def _edge_mask(alpha: np.ndarray) -> np.ndarray:
    """index.html:3586-3591：alpha>=8 且 4 邻域里有 alpha<8 → 边缘（边框一圈不判）。"""
    h, w = alpha.shape
    edge = np.zeros((h, w), dtype=bool)
    core = alpha[1:h - 1, 1:w - 1] >= TONE_ALPHA_ON
    if not core.any():
        return edge
    neigh = ((alpha[1:h - 1, 0:w - 2] < TONE_ALPHA_ON) |
             (alpha[1:h - 1, 2:w] < TONE_ALPHA_ON) |
             (alpha[0:h - 2, 1:w - 1] < TONE_ALPHA_ON) |
             (alpha[2:h, 1:w - 1] < TONE_ALPHA_ON))
    edge[1:h - 1, 1:w - 1] = core & neigh
    return edge
