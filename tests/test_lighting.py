"""lighting.py 单测：measure_region / char_lum / bake_lit / grain_tile 的手工 golden。

所有 golden 都附手推算式（注释）。对应 JS：measureRegion index.html:1212-1224、
clampRegion 1244-1251、charLum 1263-1283、bakeLit 1285-1447、grainTile 1168-1180。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from arcam import lighting
from arcam.numeric import Rect

LightSample = lighting.LightSample


def _gray(h: int, w: int, value: int, alpha: int = 255) -> np.ndarray:
    out = np.zeros((h, w, 4), dtype=np.uint8)
    out[..., 0:3] = value
    out[..., 3] = alpha
    return out


def _ramp_rgb(h: int, w: int) -> np.ndarray:
    """第 x 列 = x/w（R=G=B），整幅水平渐变。"""
    xs = np.arange(w, dtype=np.float64) / w
    return np.repeat(xs[None, :, None], h, axis=0) * np.ones((h, w, 3))


# ── measure_region ─────────────────────────────────────────────────────
def test_measure_region_pure_color():
    """纯色图：均值 = 该色；8 级量化后全部落进同一桶。"""
    img = np.full((8, 8, 3), 0.5, dtype=np.float64)
    s = lighting.measure_region(img, Rect(0, 0, 8, 8), levels=8)
    # 手推：每格均值 = 0.5 → lum = 0.5 → 桶 = floor(0.5*8) = 4 → hist = [0]*4+[1]+[0]*3
    assert s.mean == (0.5, 0.5, 0.5)
    assert len(s.levels) == 8
    assert s.levels[4] == 1.0
    assert sum(s.levels) == pytest.approx(1.0)


def test_measure_region_integer_ramp_golden():
    """整数对齐渐变：目的格 j 恰好覆盖源像素 j → 直方图均匀、均值 = (w-1)/2/w。"""
    img = _ramp_rgb(8, 8)   # 第 x 列值 x/8
    s = lighting.measure_region(img, Rect(0, 0, 8, 8), levels=8)
    # 手推：格 j 覆盖源 [j, j+1) → 值 = j/8 → 桶 floor(j/8*8) = j → 每桶 1/8
    #       均值 = (0+1+…+7)/8/8 = 28/64 = 0.4375
    assert s.mean == pytest.approx((0.4375, 0.4375, 0.4375))
    for j in range(8):
        assert s.levels[j] == pytest.approx(1 / 8)
    # light_levels：累计 1/8…；首个 ≥0.05 的是桶 0 → lo = 0.5/8 = 0.0625
    #              首个 ≥0.95 的是桶 7（7/8=0.875<0.95, 1.0≥0.95）→ hi = 7.5/8 = 0.9375
    lo, hi = lighting.light_levels(s)
    assert lo == pytest.approx(0.5 / 8)
    assert hi == pytest.approx(7.5 / 8)


def test_measure_region_default_levels_is_32():
    """sampleLight 恒定传 32（index.html:1185）：levels 默认 32、占比和为 1。"""
    img = _ramp_rgb(10, 20)
    s = lighting.measure_region(img, Rect(0, 0, 20, 10))
    assert len(s.levels) == 32
    assert sum(s.levels) == pytest.approx(1.0)
    assert all(v >= 0.0 for v in s.levels)


def test_measure_region_block_mean_two_tone():
    """四象限两色图、整数对齐采样框 → 精确块均值。"""
    img = np.zeros((4, 4, 3), dtype=np.float64)
    img[0:2, 0:2] = 0.25
    img[0:2, 2:4] = 0.75
    img[2:4, 0:2] = 1.0
    img[2:4, 2:4] = 0.0
    s = lighting.measure_region(img, Rect(0, 0, 4, 4), levels=4)
    # 手推：均值 = (0.25+0.75+1.0+0.0)/4 = 0.5；4 格 → 每格一块单色
    #       桶：0.25→floor(1)=1, 0.75→floor(3)=3, 1.0→floor(4)→夹 3, 0.0→0
    #       → hist = [1/4, 1/4, 0, 2/4]
    assert s.mean == pytest.approx((0.5, 0.5, 0.5))
    assert s.levels == pytest.approx((0.25, 0.25, 0.0, 0.5))


def test_measure_region_sw_over_width_zero_fills():
    """⚠ sw>W 越界**零填充**（artifact A §2.2.7 / SPEC §5）：越界段按透明黑计入均值。

    逐字 JS 的 ``sw = Math.min(sw, W)`` 预夹会让本分支永不可达；
    契约要求保留"越界→透明黑"而不是把它洗成 clamp，详见 _clamp_region docstring。
    """
    img = np.full((10, 20, 3), 1.0, dtype=np.float64)
    s = lighting.measure_region(img, Rect(-5, 0, 40, 10), levels=8)
    # 手推：sw=40 > W=20 → W-sw = -20 → sx = max(0, min(-20, -5)) = 0，sw 原样 40
    #       窗口 [0, 40) 右半段在图外 → 零填充 → 均值 = 1.0 * 20/40 = 0.5
    assert s.mean == pytest.approx((0.5, 0.5, 0.5))


def test_measure_region_partial_out_of_bounds_zero_fills():
    """右越界 25%：均值 = 1/1.25 = 0.8（零填充比例 = 越界面积/窗口面积）。"""
    img = np.full((10, 20, 3), 1.0, dtype=np.float64)
    s = lighting.measure_region(img, Rect(0, 0, 25, 10), levels=8)
    # 手推：sw=25, W=20 → sx = max(0, min(-5, 0)) = 0；窗口 [0,25) → 5/25 越界 → 0.8
    assert s.mean == pytest.approx((0.8, 0.8, 0.8))
    s2 = lighting.measure_region(img, Rect(0, 0, 20, 16), levels=8)
    # 手推：sh=16, H=10 → sy=0；窗口 [0,16) → 6/16 越界 → 1*10/16 = 0.625
    assert s2.mean == pytest.approx((0.625, 0.625, 0.625))


def test_measure_region_clamps_sx_when_sw_within_width():
    """sw<=W 时与逐字 JS 一致：sx 被夹回 [0, W-sw]，不可能越界。"""
    img = np.full((10, 20, 3), 1.0, dtype=np.float64)
    # Rect(18,0,5,10)：sx = max(0, min(15,18)) = 15 → [15,20) 在图内 → 均值 1.0
    assert lighting.measure_region(img, Rect(18, 0, 5, 10)).mean == pytest.approx((1.0, 1.0, 1.0))
    # Rect(-8,0,20,10)：sx = max(0, min(0,-8)) = 0 → 整幅 → 1.0
    assert lighting.measure_region(img, Rect(-8, 0, 20, 10)).mean == pytest.approx((1.0, 1.0, 1.0))


def test_measure_region_degenerate_region_zero_sample():
    """原版 ``!(sw>0) || !(sh>0)`` 返回 null：离线按「无有效采样」返回全零。"""
    img = np.full((10, 20, 3), 1.0, dtype=np.float64)
    s = lighting.measure_region(img, Rect(5, 5, 0, 10))
    assert s.mean == (0.0, 0.0, 0.0)
    assert set(s.levels) == {0.0}
    assert lighting.light_levels(s) == (0.0, 0.0)


def test_measure_region_none_region_samples_whole_frame():
    """clampRegion 的 ``!r`` 分支：没给区域 → 退化为整帧（index.html:1245）。"""
    img = np.full((10, 20, 3), 0.3, dtype=np.float64)
    assert lighting.measure_region(img, None).mean == pytest.approx((0.3, 0.3, 0.3))


def test_measure_region_rejects_bad_levels():
    with pytest.raises(ValueError):
        lighting.measure_region(np.full((4, 4, 3), 0.5), Rect(0, 0, 4, 4), levels=0)


# ── char_lum ───────────────────────────────────────────────────────────
def test_char_lum_opaque_golden():
    """全不透明灰 200 → 亮度 = 200/255。"""
    # 手推：缩到宽 32 后每个像素 alpha=255 → 不跳过；lum = 0.2126+0.7152+0.0722 的
    #       线性组合 = (0.2126*200+0.7152*200+0.0722*200)/255 = 200/255
    assert lighting.char_lum(_gray(8, 6, 200)) == pytest.approx(200 / 255)


def test_char_lum_skips_alpha_below_8():
    """alpha < 8 跳过（阈值 8/255 不是 0）：半张 alpha=7 的图不能把亮度拉低。"""
    char = _gray(8, 6, 200)
    char[:, 3:, 3] = 7          # 右半 alpha=7 → 全部跳过
    assert lighting.char_lum(char) == pytest.approx(200 / 255)
    char[:, 3:, 3] = 0
    assert lighting.char_lum(char) == pytest.approx(200 / 255)


def test_char_lum_alpha_exactly_8_is_counted():
    """边界：alpha == 8 不跳过（``if (d[i+3] < 8) continue``）。"""
    char = _gray(8, 6, 100)
    char[:, 3:, 3] = 8
    assert lighting.char_lum(char) == pytest.approx(100 / 255)


def test_char_lum_all_transparent_returns_half():
    """n==0 → 原版返回 0.5（index.html:1279）。"""
    assert lighting.char_lum(np.zeros((8, 6, 4), dtype=np.uint8)) == 0.5


def test_char_lum_transparent_pixels_do_not_darken():
    """大半透明立绘不该被当成很暗（原版注释 1262）：一半像素透明 → 亮度仍是白的。"""
    char = _gray(8, 6, 255)
    char[:, 3:, 3] = 0
    assert lighting.char_lum(char) == pytest.approx(1.0)


# ── grain_tile ─────────────────────────────────────────────────────────
def test_grain_tile_seed_goldens():
    """固定种子 0x9E3779B9 的 xorshift32 前 4 个值（两种独立 32 位实现交叉验证过）。"""
    tile = lighting.grain_tile()
    assert tile.shape == (256, 256)
    np.testing.assert_allclose(
        tile[0, 0:4],
        [0.6331870672293007, -0.24858602974563837, 0.9666003258898854, 0.011830414179712534],
        rtol=0, atol=1e-15,
    )


def test_grain_tile_range_is_unit():
    assert tile_min_max() == (-0.9999967543408275, 0.9999884585849941)


def tile_min_max() -> tuple[float, float]:
    t = lighting.grain_tile()
    return float(t.min()), float(t.max())


def test_grain_tile_is_stable_singleton():
    """同一张砖（原版只建一次，缓存）：两次调用同一对象、值不变。"""
    a = lighting.grain_tile()
    b = lighting.grain_tile()
    assert a is b
    snapshot = a.copy()
    lighting.grain_tile()
    np.testing.assert_array_equal(a, snapshot)


def test_grain_tile_matches_independent_int32_implementation():
    """与 numpy int32/uint32 原生位移语义的实现逐位一致（32 位回绕是关键）。"""
    def numpy_int32_tile() -> np.ndarray:
        s = np.int32(-1640531527)        # 0x9E3779B9 | 0
        out = np.empty(256 * 256)
        for i in range(out.shape[0]):
            s = np.int32(s ^ np.int32(np.int32(s << np.int32(13))))
            s = np.int32(s ^ np.int32(np.uint32(s) >> np.uint32(17)))
            s = np.int32(s ^ np.int32(np.int32(s << np.int32(5))))
            out[i] = float(s) / 2147483648.0
        return out.reshape(256, 256)

    np.testing.assert_array_equal(lighting.grain_tile(), numpy_int32_tile())


def test_grain_tile_requires_int32_wraparound():
    """不复刻 32 位回绕（用 Python 无限精度 int）会得到不同序列 —— 这条防止回退。"""
    def no_wrap_first(n: int) -> list[int]:
        s = 0x9E3779B9 - 0x100000000
        out = []
        for _ in range(n):
            s ^= (s << 13)
            s ^= (s & 0xFFFFFFFF) >> 17
            s ^= (s << 5)
            out.append(s)
        return out

    # 不回绕时第一步就是 4.34e14（量级完全不对），与回绕序列必然不同
    no_wrap = no_wrap_first(4)
    assert abs(no_wrap[0]) > 2 ** 32
    assert lighting.grain_tile().flat[0] != no_wrap[0] / 2147483648.0


# ── lit_scale_bucket ───────────────────────────────────────────────────
def test_lit_scale_bucket_goldens():
    """2^round(log2(s))，s>=1 → 1；s 夹到 [0.05, 1]（index.html:1455-1458）。"""
    assert lighting.lit_scale_bucket(1.0) == 1.0
    assert lighting.lit_scale_bucket(1.7) == 1.0        # 大于 1 一律按 1
    assert lighting.lit_scale_bucket(0.99) == 1.0       # clamp 到 1 → 1
    # 手推：log2(0.5)=-1 → round(-1)=-1 → 2^-1 = 0.5
    assert lighting.lit_scale_bucket(0.5) == 0.5
    # 手推：log2(0.49)=-1.0291 → js_round(-1.0291)=floor(-0.5291)=-1 → 0.5
    assert lighting.lit_scale_bucket(0.49) == 0.5
    # 手推：log2(0.26)=-1.9434 → js_round = floor(-1.4434) = -2 → 0.25
    assert lighting.lit_scale_bucket(0.26) == 0.25
    # 手推：log2(0.125)=-3 → 0.125
    assert lighting.lit_scale_bucket(0.125) == 0.125
    # 下限：0.05 → clamp 后仍是 0.05 → log2=-4.3219 → round=-4 → 0.0625
    assert lighting.lit_scale_bucket(0.05) == 0.0625
    assert lighting.lit_scale_bucket(0.001) == 0.0625   # 被夹到 0.05


# ── bakeLit LUT ────────────────────────────────────────────────────────
def test_lut_bytes_js_round_half_up():
    """LUT 边界取整必须 js_round：Math.round(0.5)=1、round(2.5)=3（Python 是 0/2）。"""
    got = lighting._lut_bytes(np.array([0.5, 1.5, 2.5, 3.5, -0.5, -1.5, 254.5, 255.5, 300.0]))
    np.testing.assert_array_equal(got, np.array([1, 2, 3, 4, 0, 0, 255, 255, 255], dtype=np.uint8))


def test_lut_endpoints_and_monotone():
    """LUT(0)=0、LUT(255)=255；三条 LUT 单调不减（gamma 曲线 v↦255·(v/255)^inv）。"""
    lut_r, lut_g, lut_b, _g_l, _inv, _s_k = lighting._lit_luts(
        0.5, 0.5, 0.5, 0.0, 0.0, 0.5, 0.5)
    for lut in (lut_r, lut_g, lut_b):
        assert lut[0] == 0
        assert lut[255] == 255
        assert np.all(np.diff(lut.astype(np.int64)) >= 0)


def test_lut_neutral_environment_is_pure_gamma():
    """环境与立绘都是中性灰 → 无白平衡增益 → 三条 LUT 相同且等于 gamma 曲线。"""
    env_lum, env_sat, ch_sat, ch_r, ch_b = 0.5, 0.0, 0.0, 0.5, 0.5
    lut_r, lut_g, lut_b, g_l, inv, s_k = lighting._lit_luts(
        env_lum, 0.5, 0.5, env_sat, ch_sat, ch_r, ch_b)
    # 手推：gL = clamp(0.5/0.42, 0.6, 1.15) = 1.15；inv = (1/1.15)^0.6
    assert g_l == pytest.approx(1.15)
    assert inv == pytest.approx(math.pow(1 / 1.15, 0.6))
    assert s_k == 1.0
    np.testing.assert_array_equal(lut_r, lut_g)
    np.testing.assert_array_equal(lut_b, lut_g)
    # 手推 v=128：cur = 255*(128/255)^inv = 135.2967 → js_round = 135
    assert lut_g[128] == 135
    assert lut_g[200] == 204        # cur = 203.9468 → 204
    assert lut_g[64] == 72          # cur = 71.5272 → 72


def test_lut_white_balance_highlight_transition_golden():
    """165/215 边界与 gRfull→gRcap 高光限幅过渡（bakeLit index.html:1364-1372）。"""
    # 环境 r=0.9,b=0.3（暖），立绘中性灰 → charRBg=1
    # 手推：inv = (1/1.15)^0.6（lum=0.9 → gL=1.15）；envRB = 3
    #       ratio = 3^0.75 = 2.2795 → gRfull = sqrt = 1.50980 > GMAX=1.18 → gRcap = 1.18
    #       w(v) = 0 (v<=165) / 1 (v>=215) / (v-165)/50
    #       lutR[v] = js_round(255*(v/255)^inv * (gRfull + (gRcap-gRfull)*w))
    #       lutB[v] = js_round(255*(v/255)^inv * (1/gRfull + (1/gRcap-1/gRfull)*w))
    lut_r, lut_g, lut_b, _g_l, inv, s_k = lighting._lit_luts(0.9, 0.9, 0.3, 0.5, 0.0, 0.5, 0.5)
    g_r_full = math.sqrt(3 ** 0.75)
    g_r_cap = 1.18
    for v in (100, 165, 166, 214, 215, 255):
        cur = 255.0 * (v / 255.0) ** inv
        w = 0.0 if v <= 165 else (1.0 if v >= 215 else (v - 165) / 50.0)
        exp_r = math.floor(min(255.0, cur * (g_r_full + (g_r_cap - g_r_full) * w)) + 0.5)
        b_gain = 1 / g_r_full + (1 / g_r_cap - 1 / g_r_full) * w
        exp_b = math.floor(min(255.0, cur * b_gain) + 0.5)
        exp_g = math.floor(min(255.0, cur) + 0.5)
        assert lut_r[v] == exp_r, f"v={v}"
        assert lut_b[v] == exp_b, f"v={v}"
        assert lut_g[v] == exp_g, f"v={v}"
    # 边界两侧实测值（golden）：v=165→B=113，v=166→B=114（w 从 0 跳到 0.02）
    assert lut_b[165] == 113 and lut_b[166] == 114
    assert lut_b[214] == 183 and lut_b[215] == 185
    assert lut_r[255] == 255 and lut_b[255] == 216


def test_lut_gain_product_is_one():
    """立绘色比已经等于环境色比 → 白平衡增益 = 1，颜色一个字节都不动（只剩颗粒）。

    手推：env = (a, a, b) 取 a/b = 3 且 rec709(a,a,b) = 0.42 → a = 0.44123827,
          b = 0.14707942。lum = 0.42 → gL = 1 → inv = 1（gamma 曲线恒等）；
          envRB = 3 = charRB（立绘 192:64）→ charRBg = 3 → ratio = 1 → gRfull = 1；
          env_sat = 2/3 = ch_sat（(192-64)/192）→ sK = 1。LUT/饱和都不起作用，
          输出 = 输入 + 颗粒噪声（gAmp = clamp(6.5*(1-0.42*1.35), 0.7, 6.5) = 2.8145）。
    """
    a, b = 0.42 / 2.8556 * 3, 0.42 / 2.8556
    char = np.zeros((16, 16, 4), dtype=np.uint8)
    char[..., 0], char[..., 1], char[..., 2] = 192, 192, 64
    char[..., 3] = 255
    out = lighting.bake_lit(char, LightSample(levels=(), mean=(a, a, b)), (16, 16))
    assert np.all(out[..., 3] == 255)
    # 颗粒幅度 2.8145 + 取整 0.5
    assert float(np.abs(out[..., 0].astype(np.float64) - 192.0).max()) <= 3.5
    assert float(np.abs(out[..., 0].astype(np.float64) - 192.0).mean()) > 0.5
    # 颜色比被完整保留：R:B = 192:64 = 3:1（只受颗粒微扰）
    ratio = float(out[..., 0].astype(np.float64).mean() / out[..., 2].astype(np.float64).mean())
    assert ratio == pytest.approx(3.0, abs=0.05)


# ── bake_lit ───────────────────────────────────────────────────────────
def test_bake_lit_full_golden_4x4():
    """全链路 golden：4×4 灰 128 立绘 + 灰 0.9 环境 + 画布目标 (4,4)。

    手推（index.html:1319-1410）：
      charLum: 128/255 = 0.50196，chSat = 0，chR = chB = 0.50196
      env: lum = 0.9（rec709(0.9,0.9,0.9)），sat = 0
      gL = clamp(0.9/0.42, 0.6, 1.15) = 1.15 → inv = (1/1.15)^0.6 = 0.9195625920471774
      envRB = 1，charRB = 1，charRBg = 1 → ratio = 1 → gRfull = gRcap = 1（三条 LUT 相同）
      sK = clamp(1+(0-0)*0.3) = 1 → 跳过饱和度混合
      lutG[128] = js_round(255*(128/255)^inv) = js_round(135.2967) = 135
      litScale = canvas[0]/charW = 4/4 = 1 → bucket = 1 → 补偿 = 1
      gAmp = clamp(6.5*(1-0.9*1.35), 0.7, 6.5) * 1 = 0.7（上限把 -2.275 夹到 0.7）
      out[y,x] = rint(135 + tile[y,x]*0.7)（Uint8ClampedArray 的 round-half-even）
    颗粒砖前 4 个值：0.6331870672 / -0.2485860297 / 0.9666003259 / 0.0118304142
    第二行前 4 个：  0.1726524783 /  0.2330028736 / -0.2375066653 / 0.9399389657
    """
    char = _gray(4, 4, 128)
    sample = LightSample(levels=tuple([0.0] * 32), mean=(0.9, 0.9, 0.9))
    out = lighting.bake_lit(char, sample, (4, 4))
    assert out.shape == (4, 4, 4) and out.dtype == np.uint8
    np.testing.assert_array_equal(
        out[..., 0],
        [[135, 135, 136, 135],
         [135, 135, 135, 136],
         [135, 135, 134, 135],
         [136, 134, 135, 135]],
    )
    assert np.all(out[..., 3] == 255)


def test_bake_lit_bright_environment_lifts_dark_char():
    """暗立绘 + 亮环境 → 提亮（gL = 1.15 → inv < 1 → gamma 曲线上移）。"""
    char = _gray(8, 8, 60)
    out = lighting.bake_lit(char, LightSample(levels=(), mean=(0.9, 0.9, 0.9)), (8, 8))
    assert float(out[..., 0].mean()) > 60 + 5


def test_bake_lit_dark_environment_darkens_bright_char():
    """亮立绘 + 暗环境 → 压暗（gL = 0.6 → inv > 1 → gamma 曲线下移）。"""
    char = _gray(8, 8, 220)
    out = lighting.bake_lit(char, LightSample(levels=(), mean=(0.1, 0.1, 0.1)), (8, 8))
    assert float(out[..., 0].mean()) < 220 - 5


def test_bake_lit_white_balance_follows_environment():
    """暖环境 → R 抬 B 压；冷环境 → 反过来（对数空间只挪 K=0.75 那一份）。"""
    char = _gray(16, 16, 128)
    warm = lighting.bake_lit(char, LightSample(levels=(), mean=(0.9, 0.85, 0.3)), (16, 16))
    cool = lighting.bake_lit(char, LightSample(levels=(), mean=(0.3, 0.85, 0.9)), (16, 16))
    assert float(warm[..., 0].mean()) > float(warm[..., 2].mean())
    assert float(cool[..., 0].mean()) < float(cool[..., 2].mean())
    # golden（含颗粒位移）：暖 (0,0) → R=216, B=79；冷 → R=82, B=219
    assert warm[0, 0, 0] == 216 and warm[0, 0, 2] == 79
    assert cool[0, 0, 0] == 82 and cool[0, 0, 2] == 219


def test_bake_lit_transparent_pixels_zeroed_and_alpha_kept():
    """alpha==0 → RGB 清零（createImageData 零初始化）；alpha 原样一个字节不动。"""
    char = _gray(8, 8, 200)
    char[0:4, 0:4, 3] = 0
    char[4:8, 4:8, 3] = 128
    out = lighting.bake_lit(char, LightSample(levels=(), mean=(0.5, 0.5, 0.5)), (8, 8))
    assert np.all(out[0:4, 0:4] == 0)
    assert np.all(out[4:8, 4:8, 3] == 128)


def test_bake_lit_none_sample_returns_char_unchanged():
    """sample=None → 原版「总开关关 / 采样无效」分支：原样输出（index.html:1305）。"""
    char = _gray(6, 6, 137)
    out = lighting.bake_lit(char, None, (6, 6))
    np.testing.assert_array_equal(out, char)


def test_bake_lit_uint8_and_float_families():
    """uint8 进出 uint8、float64[0,1] 进出 float64[0,1]（内部一律按 0..255 算）。"""
    char_u8 = _gray(4, 4, 128)
    char_f = char_u8.astype(np.float64) / 255.0
    sample = LightSample(levels=(), mean=(0.9, 0.9, 0.9))
    out_u8 = lighting.bake_lit(char_u8, sample, (4, 4))
    out_f = lighting.bake_lit(char_f, sample, (4, 4))
    assert out_u8.dtype == np.uint8
    assert out_f.dtype == np.float64
    assert out_f.max() <= 1.0 and out_f.min() >= 0.0
    np.testing.assert_allclose(out_f, out_u8.astype(np.float64) / 255.0, atol=1e-12)


def test_bake_lit_is_deterministic():
    """固定种子颗粒：同一立绘同一环境两次烘制逐字节相同。"""
    char = _gray(16, 16, 90)
    sample = LightSample(levels=(), mean=(0.62, 0.55, 0.4))
    a = lighting.bake_lit(char, sample, (16, 16))
    b = lighting.bake_lit(char, sample, (16, 16))
    np.testing.assert_array_equal(a, b)


def test_bake_lit_grain_adds_positive_noise():
    """默认 GRN 开：输出 = LUT 值 ± gAmp 噪声（幅度在 0.7~6.5 之间）。"""
    char = _gray(32, 32, 128)
    sample = LightSample(levels=(), mean=(0.42, 0.42, 0.42))
    out = lighting.bake_lit(char, sample, (32, 32))
    # gAmp（lum=0.42）= clamp(6.5*(1-0.42*1.35), 0.7, 6.5) = clamp(2.8025,…)=2.8025
    delta = out[..., 0].astype(np.float64) - 128.0
    assert float(np.abs(delta).max()) <= 6.5 + 1e-9
    assert float(np.abs(delta).max()) > 0.0
    assert float(np.abs(delta).mean()) > 0.5


def test_bake_lit_grain_compensation_follows_canvas_size():
    """画布目标越小 → litScale 档位越低 → 补偿越大（缩得越小颗粒越强）。

    环境取中性 0.42 让 gamma 曲线恒等（gL=1, inv=1），这样输出偏离只剩颗粒：
    litScale=1 → 补偿 1（幅度 2.7985）；litScale=0.25 → 补偿 clamp(1/0.25,1,3.2)=3.2。
    """
    char = _gray(64, 64, 128)
    sample = LightSample(levels=(), mean=(0.42, 0.42, 0.42))
    big = lighting.bake_lit(char, sample, (64, 64))     # litScale = 1 → 补偿 1
    small = lighting.bake_lit(char, sample, (16, 16))   # litScale = 0.25 → 补偿 3.2
    amp_big = float(np.abs(big[..., 0].astype(np.float64) - 128.0).mean())
    amp_small = float(np.abs(small[..., 0].astype(np.float64) - 128.0).mean())
    # 手推：gAmp = clamp(6.5*(1-0.42*1.35), 0.7, 6.5) = 2.8145；噪声 |n|∈[0,1] 均匀
    #       → |n*gAmp| 的均值 ≈ 0.5*gAmp = 1.407（实测 1.4087）
    assert amp_big == pytest.approx(2.8145 * 0.5, abs=0.2)
    assert amp_small > amp_big * 2.5
    assert amp_small <= 6.5


def test_bake_lit_saturation_toward_environment():
    """环境比立绘灰 → sK < 1 → 饱和度被压向同亮度灰（index.html:1397-1399）。"""
    color = np.zeros((16, 16, 4), dtype=np.uint8)
    color[..., 0], color[..., 1], color[..., 2] = 200, 60, 60
    color[..., 3] = 255
    # 环境 sat = (0.5-0.3)/0.5 = 0.4 > chSat，sK = clamp(1+(0.4-chSat)*0.3) > 1 → 更艳
    vivid = lighting.bake_lit(color, LightSample(levels=(), mean=(0.5, 0.5, 0.3)), (16, 16))
    # 环境 sat = 0（灰）→ sK = clamp(1 - chSat*0.3) < 1 → 更灰
    flat = lighting.bake_lit(color, LightSample(levels=(), mean=(0.4, 0.4, 0.4)), (16, 16))

    def spread(img: np.ndarray) -> float:
        return float((img[..., 0].astype(np.int64) - img[..., 2].astype(np.int64)).mean())

    assert spread(flat) < spread(vivid)
