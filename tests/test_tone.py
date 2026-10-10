"""tone.py —— 环境融合 ①（toneMatchedLayer，index.html:3501-3620）。

两个层次：
  · **逐字 JS 转写**做 bit-exact 基准（_tone_ref_js）：算法六步一行不差，
    随机数据下与被测实现逐字节相同，防将来重构漂离原版。
  · 三处自守卫（n<500 / bn<500 / 环带<4）、alpha 判据、kTone 0.3/0.4 分支、
    高斯羽化只降 alpha、RGB 混向背景色，各自独立断言（含手工推导的 golden）。
"""

from __future__ import annotations

import numpy as np
import pytest

from arcam.numeric import js_round, js_to_fixed
from arcam.tone import TONE_K, tone_matched_layer, tone_stats


# ───────────────────────── 逐字 JS 转写（基准） ─────────────────────────
def _to_u8c(v: float) -> int:
    """JS ``Uint8ClampedArray`` 赋值：夹 [0,255] 后四舍五入，**半数取偶**。"""
    if v < 0.0:
        v = 0.0
    elif v > 255.0:
        v = 255.0
    return int(round(v))


def _tone_ref_js(layer_u8: np.ndarray, bg_u8: np.ndarray):
    """index.html:3501-3620 的逐行转写（layer/bg 都是 uint8 数组）。

    返回 ``(out_u8, diag)``；自守卫命中返回 ``(None, reason)``。
    """
    h, w = layer_u8.shape[0], layer_u8.shape[1]
    a = [list(map(int, px)) for px in layer_u8.reshape(-1, 4)]
    bflat = bg_u8.reshape(-1, 3)

    x0, y0, x1, y1 = w, h, -1, -1
    n = 0
    mr = mg = mb = 0
    for y in range(h):
        for x in range(w):
            i = y * w + x
            if a[i][3] < 8:
                continue
            if x < x0:
                x0 = x
            if x > x1:
                x1 = x
            if y < y0:
                y0 = y
            if y > y1:
                y1 = y
            mr += a[i][0]
            mg += a[i][1]
            mb += a[i][2]
            n += 1
    if n < 500:
        return None, "n<500"
    mr /= n
    mg /= n
    mb /= n

    pad = js_round(max(x1 - x0, y1 - y0) * 0.2) + 8
    rx = max(0, x0 - pad)
    ry = max(0, y0 - pad)
    rw = min(w, x1 + pad) - rx
    rh = min(h, y1 + pad) - ry
    if rw < 4 or rh < 4:
        return None, "band<4"
    br = bgc = bb = bn = 0
    for y in range(rh):
        for x in range(rw):
            gx, gy = rx + x, ry + y
            if x0 <= gx <= x1 and y0 <= gy <= y1:
                continue
            px = [int(bflat[gy * w + gx][c]) for c in range(3)]
            br += px[0]
            bgc += px[1]
            bb += px[2]
            bn += 1
    if bn < 500:
        return None, "bn<500"
    br /= bn
    bgc /= bn
    bb /= bn

    def pct(is_model: bool):
        hist = [0] * 256
        tot = 0
        if is_model:
            rng = ((y, x) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))
        else:
            rng = ((ry + y, rx + x) for y in range(rh) for x in range(rw)
                   if not (x0 <= rx + x <= x1 and y0 <= ry + y <= y1))
        for (py, px) in rng:
            src = a if is_model else None
            if is_model:
                if a[py * w + px][3] < 8:
                    continue
                r, g, b = a[py * w + px][:3]
            else:
                r, g, b = (int(bflat[py * w + px][c]) for c in range(3))
            hist[int(r * 0.299 + g * 0.587 + b * 0.114)] += 1
            tot += 1
            del src
        if not tot:
            return [0, 255]
        acc = lo = hi = 0
        lo, hi = 0, 255
        for v in range(256):
            acc += hist[v]
            if acc >= tot * 0.05:
                lo = v
                break
        acc = 0
        for v in range(255, -1, -1):
            acc += hist[v]
            if acc >= tot * 0.05:
                hi = v
                break
        return [lo, hi]

    mLo, mHi = pct(True)
    bLo, bHi = pct(False)
    range_m = max(8, mHi - mLo)
    range_b = max(8, bHi - bLo)
    k_tone = 0.3 if range_b < 90 else 0.4
    t_range = range_m + (range_b - range_m) * k_tone
    t_lo = mLo + (bLo - mLo) * k_tone
    sc = t_range / range_m
    mus = (mr, mg, mb)
    for i in range(w * h):
        if a[i][3] < 8:
            continue
        l0 = a[i][0] * 0.299 + a[i][1] * 0.587 + a[i][2] * 0.114
        g = ((l0 - mLo) * sc + t_lo) / l0 if l0 > 1 else 1
        for c in range(3):
            v = a[i][c] * g + ([br, bgc, bb][c] - mus[c]) * TONE_K
            a[i][c] = _to_u8c(max(0.0, min(255.0, v)))

    def alpha_at(x: int, y: int) -> int:
        return a[y * w + x][3]

    edge = [0] * (w * h)
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            if alpha_at(x, y) < 8:
                continue
            if (alpha_at(x - 1, y) < 8 or alpha_at(x + 1, y) < 8
                    or alpha_at(x, y - 1) < 8 or alpha_at(x, y + 1) < 8):
                edge[y * w + x] = 1
    before = [list(p) for p in a]
    edge_n = 0
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            i = y * w + x
            if not edge[i]:
                continue
            s = 0
            ws = 0
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    k2 = 4 if (dy == 0 and dx == 0) else (2 if (dy == 0 or dx == 0) else 1)
                    s += before[(y + dy) * w + (x + dx)][3] * k2
                    ws += k2
            na = js_round(s / ws)
            oa = before[i][3]
            if na >= oa:
                continue
            a[i][3] = na
            t = 1 - na / 255
            if 0 <= (x - rx) < rw and 0 <= (y - ry) < rh:
                gp = bflat[(ry + (y - ry)) * w + (rx + (x - rx))]
                for c in range(3):
                    a[i][c] = _to_u8c(a[i][c] * (1 - t) + int(gp[c]) * t)
            edge_n += 1
    diag = {"model": [js_round(mr), js_round(mg), js_round(mb)],
            "bg": [js_round(br), js_round(bgc), js_round(bb)], "k": TONE_K,
            "kTone": k_tone, "mLo": mLo, "mHi": mHi, "bLo": bLo, "bHi": bHi,
            "sc": js_to_fixed(sc, 3), "n": n, "bn": bn}
    out = np.array(a, dtype=np.uint8).reshape(h, w, 4)
    return (out, diag)


def _mk_sprite(rng, h, w, soft: bool = False):
    """立绘层：画面中缝一块随机色 + alpha 255；soft 时右侧加半透明带。"""
    layer = np.zeros((h, w, 4))
    x0, y0 = max(4, w // 6), max(6, h // 6)
    x1, y1 = min(w - 4, w - w // 6), min(h - 2, h - h // 6)
    layer[y0:y1, x0:x1, :3] = rng.random((y1 - y0, x1 - x0, 3))
    layer[y0:y1, x0:x1, 3] = 1.0
    if soft and x1 + 4 <= w:
        layer[y0:y1, x1:x1 + 4, 3] = rng.integers(8, 255, (y1 - y0, 4)) / 255.0
        layer[y0:y1, x1:x1 + 4, :3] = 190 / 255
    return layer


# ───────────────────────── 与逐字转写逐位一致 ─────────────────────────
@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("soft", [False, True])
def test_bit_exact_against_js_transcription(seed: int, soft: bool) -> None:
    rng = np.random.default_rng(100 + seed)
    h, w = 60, 44
    layer01 = _mk_sprite(np.random.default_rng(200 + seed), h, w, soft)
    layer_u8 = (np.clip(layer01, 0, 1) * 255 + 0.5).astype(np.uint8)
    bg = np.random.default_rng(300 + seed).integers(0, 256, (h, w, 3)).astype(np.uint8)

    ref, diag = _tone_ref_js(layer_u8, bg)
    got = tone_matched_layer(layer01, bg.astype(np.float64) / 255.0)
    assert ref is not None and got is not None
    got_u8 = (np.clip(got, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    assert np.array_equal(got_u8, ref), f"seed={seed} soft={soft} 与原版转写有偏差"
    st = tone_stats(layer01, bg.astype(np.float64) / 255.0)
    assert st is not None
    for k in ("kTone", "mLo", "mHi", "bLo", "bHi", "sc", "n", "bn"):
        assert st[k] == diag[k], k
    assert st["model"] == diag["model"] and st["bg"] == diag["bg"]


# ───────────────────────── 手工 golden：均值平移 + 范围重映射 ─────────────────────────
def test_golden_mean_shift_and_range_remap() -> None:
    """单色立绘 + 单色背景的 golden（手推，不调用被测码）。

    立绘 (200,100,50) 恒定、背景 (60,60,60) 恒定：
      L0 = 0.299·200 + 0.587·100 + 0.114·50 = 124.2 → 直方图档 124
      rangeM = rangeB = max(8, 0) = 8 → kTone = 0.3（rangeB < 90）
      tRange = 8 + (8-8)·0.3 = 8 ; tLo = 124 + (60-124)·0.3 = 104.8 ; sc = 8/8 = 1
      g = ((124.2-124)·1 + 104.8)/124.2 = 105.0/124.2
      shift = ((60-200),(60-100),(60-50))·0.35 = (-49, -14, 3.5)
      C' = C·g + shift → (120.08, 70.54, 45.77) → ToUint8Clamp → (120, 71, 46)
    """
    h, w = 48, 64
    layer = np.zeros((h, w, 4))
    layer[6:42, 10:54, 0] = 200 / 255
    layer[6:42, 10:54, 1] = 100 / 255
    layer[6:42, 10:54, 2] = 50 / 255
    layer[6:42, 10:54, 3] = 1.0
    bg = np.zeros((h, w, 3))
    bg[:] = 60 / 255

    out = tone_matched_layer(layer, bg)
    assert out is not None
    assert tuple((np.clip(out[20, 30], 0, 1) * 255 + 0.5).astype(int)[:3]) == (120, 71, 46)
    assert out[20, 30, 3] == pytest.approx(1.0)
    # 方向：只开增益（无均值平移）会是 pure；实测与 pure 之差正好是均值平移项
    # (μbg - μmodel)·TONE_K —— R/G 往下降、B 反而往上升（42.27 → 45.77）。
    model = (200, 100, 50)
    pure = tuple(model[c] * 105.0 / 124.2 for c in range(3))
    got = tuple(float(out[20, 30, c]) * 255 for c in range(3))
    for c in range(3):
        assert (got[c] - pure[c]) == pytest.approx((60 - model[c]) * TONE_K, abs=0.6), c


def test_mean_shift_direction_is_toward_background() -> None:
    """灰立绘 + 灰背景：均值平移就是 |μbg-μmodel|×TONE_K（范围重映射此时是恒等）。

    立绘 204 恒值、背景 51 恒值：L0 = 204（Rec.601 权重和为 1）→ 直方图档 203
    （0.8·255 的浮点累加落在 203.999…，``|0`` 截断取 203 —— 原版逐字节语义）；
    背景同理 51 → bLo = 50（同样的浮点截断）→ rangeB = 8 → kTone = 0.3；
    g = ((204-203)·1 + (203 + (50-203)·0.3)) / 204 = 158.1/204；
    平移项 = (51-204)·0.35 = -53.55。故 C' = 204·0.775 - 53.55 = 104.55 → 105，
    落在"只开增益"的 158 与背景 51 之间（朝背景走了一半多）。
    """
    h, w = 48, 64
    layer = np.zeros((h, w, 4))
    layer[6:42, 10:54, :3] = 204 / 255
    layer[6:42, 10:54, 3] = 1.0
    bg = np.full((h, w, 3), 51 / 255)
    out = tone_matched_layer(layer, bg)
    assert out is not None
    assert int(np.clip(out[20, 30, 0], 0, 1) * 255 + 0.5) == 105
    assert 51 < 105 < 158
    st = tone_stats(layer, bg)
    assert st is not None and st["kTone"] == 0.3 and st["mLo"] == 203 and st["bLo"] == 50


def test_k_tone_dark_vs_lit_branch() -> None:
    """"rangeB < 90 → 0.3" 才不被 toon 分档压平；宽域背景 → 0.4。"""
    h, w = 48, 64

    def run(bg_two: bool) -> dict:
        layer = _mk_sprite(np.random.default_rng(7), h, w)
        bg = np.full((h, w, 3), 60 / 255)
        if bg_two:
            bg[:, :w // 2] = 0.0
            bg[:, w // 2:] = 200 / 255
        st = tone_stats(layer, bg)
        assert st is not None
        return st

    dark = run(False)
    lit = run(True)
    assert dark["kTone"] == 0.3 and dark["bLo"] == 60 and dark["bHi"] == 60
    assert lit["kTone"] == 0.4
    assert lit["bLo"] == 0 and lit["bHi"] >= 190


# ───────────────────────── 自守卫 ─────────────────────────
def test_returns_none_when_too_few_model_pixels() -> None:
    h, w = 48, 64
    layer = np.zeros((h, w, 4))
    layer[10:20, 10:20, :3] = 0.8          # 100 px < 500
    layer[10:20, 10:20, 3] = 1.0
    bg = np.full((h, w, 3), 0.4)
    assert tone_matched_layer(layer, bg) is None
    assert tone_stats(layer, bg) is None


def test_returns_none_when_background_band_empty() -> None:
    """立绘铺满整张画布 → 背景环带一个像素都不剩（立绘层路径的自守卫）。"""
    h, w = 48, 64
    layer = np.zeros((h, w, 4))
    layer[..., :3] = 0.7
    layer[..., 3] = 1.0
    bg = np.full((h, w, 3), 0.3)
    assert tone_matched_layer(layer, bg) is None


def test_returns_none_when_layer_fully_transparent() -> None:
    layer = np.zeros((48, 64, 4))
    bg = np.full((48, 64, 3), 0.3)
    assert tone_matched_layer(layer, bg) is None


# ───────────────────────── alpha 判据 / 羽化 ─────────────────────────
def test_alpha_below_8_pixels_untouched() -> None:
    h, w = 48, 64
    layer = np.zeros((h, w, 4))
    layer[6:42, 10:54, :3] = 0.8
    layer[6:42, 10:54, 3] = 1.0
    # 内部挖三个 alpha < 8 的洞，RGB 给一个显眼的值
    holes = [(20, 30), (25, 40), (33, 15)]
    for (y, x) in holes:
        layer[y, x, 3] = 4 / 255
        layer[y, x, :3] = (200 / 255, 10 / 255, 10 / 255)
    bg = np.full((h, w, 3), 0.4)
    before = layer.copy()
    out = tone_matched_layer(layer, bg)
    assert out is not None
    for (y, x) in holes:
        assert tuple((out[y, x] * 255 + 0.5).astype(int)) == \
            tuple((before[y, x] * 255 + 0.5).astype(int)), (y, x)
    # 画布外沿（alpha 0）自然也是原样
    assert out[0, 0, 3] == 0.0


def test_feather_only_lowers_alpha_and_mixes_rgb_toward_bg() -> None:
    """手推：矩形模型第一行 (6,30) 的 3×3 里有 12/16 权重是不透明 → na = 191。

    t = 1 - 191/255；RGB 按 t 混向背景色 (60,60,60)。配色 (120,71,46)（见上一条 golden）：
      R = 120 - 60t = 120 - 15.06 = 104.94 → 105
      G = 71 - 11t  = 71 -  4.83 =  66.17 → 66
      B = 46 + 14t  = 46 +  6.15 =  52.15 → 52
    """
    h, w = 48, 64
    layer = np.zeros((h, w, 4))
    layer[6:42, 10:54, 0] = 200 / 255
    layer[6:42, 10:54, 1] = 100 / 255
    layer[6:42, 10:54, 2] = 50 / 255
    layer[6:42, 10:54, 3] = 1.0
    bg = np.full((h, w, 3), 60 / 255)
    out = tone_matched_layer(layer, bg)
    assert out is not None
    px = (np.clip(out[6, 30], 0, 1) * 255 + 0.5).astype(int)
    assert tuple(px) == (105, 68, 50, 191)
    # 角上 (6,10)：3×3 里只有 5/16 权重不透明 → na = round(2295/16) = 143
    px2 = (np.clip(out[6, 10], 0, 1) * 255 + 0.5).astype(int)
    assert tuple(px2) == (94, 66, 52, 143)
    # 内部（7,30）的 3×3 全不透明 → na = oa，不降
    assert out[7, 30, 3] == pytest.approx(1.0)
    assert tuple((np.clip(out[7, 30], 0, 1) * 255 + 0.5).astype(int)[:3]) == (120, 71, 46)


def test_feather_never_raises_alpha() -> None:
    """半透明带（alpha 8..254）不该被羽化抬高。"""
    h, w = 48, 64
    layer = _mk_sprite(np.random.default_rng(11), h, w, soft=True)
    bg = np.full((h, w, 3), 0.45)
    out = tone_matched_layer(layer, bg)
    assert out is not None
    before = layer[..., 3]
    after = out[..., 3]
    # 只在边缘判据内才可能变，且只降
    diff = after - before
    assert float(diff.max()) <= 1e-12
    assert float((diff < -1e-6).sum()) > 0          # 确实有像素被降
    assert float(after.min()) >= 0.0 and float(after.max()) <= 1.0


def test_bbox_at_canvas_corner_is_handled() -> None:
    """bbox 贴左上角时 rx/ry 夹到 0，外圈一圈不判（原版循环 1..h-2）。"""
    h, w = 80, 70
    layer = np.zeros((h, w, 4))
    layer[:40, :35, :3] = 0.8
    layer[:40, :35, 3] = 1.0
    bg = np.full((h, w, 3), 100 / 255)
    out = tone_matched_layer(layer, bg)
    assert out is not None
    q = (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)
    assert q[0, 0, 3] == 255                        # 最外一圈不羽化（循环 1..h-2）
    assert q[39, 20, 3] < 255                       # 内侧边界才判边缘
    ref, _ = _tone_ref_js((layer * 255 + 0.5).astype(np.uint8),
                          (bg * 255 + 0.5).astype(np.uint8))
    assert np.array_equal(q, ref)


def test_empty_layer_and_bad_shapes_raise() -> None:
    with pytest.raises(ValueError):
        tone_matched_layer(np.zeros((10, 10, 3)), np.zeros((10, 10, 3)))
    with pytest.raises(ValueError):
        tone_matched_layer(np.zeros((10, 10, 4)), np.zeros((12, 10, 3)))
