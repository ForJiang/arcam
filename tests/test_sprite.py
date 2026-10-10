"""sprite.py 单测：接触阴影层与立绘合成（index.html:3044-3166）。

``_P`` 是 placement.SpritePlacement 同属性协议（u/v/w/h/rot）的轻量替身 ——
离线合成只按属性协议取值，测试替身即可，无需构造真实 Subject/Camera。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pytest

from arcam import sprite


@dataclass
class _P:
    """SpritePlacement 的属性协议替身（u/v/w/h/rot）。"""

    u: float
    v: float
    w: float
    h: float
    rot: float


def _char_blob(h: int = 60, w: int = 40) -> np.ndarray:
    """40×60 立绘：中间 20 宽是不透明人形，底部带全不透明（落地轮廓 = 一条）。"""
    char = np.zeros((h, w, 4), dtype=np.uint8)
    char[..., 0:3] = 200
    char[:, 10:30, 3] = 255
    return char


def _char_tapered_bottom(h: int = 60, w: int = 40) -> np.ndarray:
    """底部带 alpha 呈三角形 hump：中心最实、向两侧线性衰减。"""
    char = np.zeros((h, w, 4), dtype=np.uint8)
    char[..., 0:3] = 200
    xs = np.arange(w)
    prof = np.maximum(0.0, 1.0 - np.abs(xs - (w - 1) / 2.0) / ((w - 1) / 2.0))
    char[h - 8:, :, 3] = (prof * 255.0 + 0.5).astype(np.uint8)
    return char


# ── contact_shadow_layer ───────────────────────────────────────────────
def test_shadow_layer_shape_golden():
    """outH = max(4, js_round(w·SH_THICK))，宽 = 立绘宽（index.html:3063）。"""
    layer = sprite.contact_shadow_layer(_char_blob(), _P(100, 100, 40, 60, 0.0), (0.6, 0.6, 0.6))
    # 手推：w=40 → outH = max(4, round(40*0.24)) = max(4, round(9.6)) = 10
    assert layer.shape == (10, 40, 4)
    assert layer.dtype == np.uint8


def test_shadow_layer_center_strongest_decays_to_sides():
    """中心列最强、向两侧衰减（横向被移动平均抹过）。"""
    char = _char_tapered_bottom()
    layer = sprite.contact_shadow_layer(char, _P(100, 100, 40, 60, 0.0), (0.6, 0.6, 0.6))
    al = layer[..., 3]
    row = 2                                     # 峰值行（t=0.25，离 SH_PEAK=0.30 最近）
    cols = [20, 18, 16, 14, 12, 10, 6, 2, 0]
    vals = [int(al[row, c]) for c in cols]
    assert all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)), vals
    assert vals[0] == 100 and vals[-1] == 3
    # 底带轮廓在列 19/20 附近对称（hump 顶）
    assert int(al[row, 19]) == int(al[row, 20])
    assert int(al[row, 0]) < int(al[row, 20]) // 20


def test_shadow_layer_peak_alpha_golden():
    """峰值行 alpha = js_round(sm·fall·SH_ALPHA·255)（index.html:3097-3107）。"""
    char = _char_tapered_bottom()               # bandH = max(2, round(60*0.085)) = 5
    layer = sprite.contact_shadow_layer(char, _P(100, 100, 40, 60, 0.0), (0.6, 0.6, 0.6))
    # 手推：prof[x] = byte(255·(1-|x-19.5|/19.5)) → prof[19] = prof[20] = 248，
    #       prof[21] = 235；平滑半径 r = max(1, round(0.8)) = 1
    #       sm[20] = (248+248+235)/3/255 = 0.955556
    #       峰值行 t=(2+0.5)/10 = 0.25 → dy = -0.05 → fall = exp(-0.0025/0.1352) = 0.981679
    #       a = 0.955556 * 0.981679 * 0.42 = 0.394002 → js_round(0.394002*255)
    #         = js_round(100.47) = 100
    assert layer[2, 20, 3] == 100
    # 次行：t=0.15 → dy=-0.15 → fall=0.846690 → a=0.339864 → js_round(86.67) = 87
    assert layer[1, 20, 3] == 87
    # 颜色：环境 (0.6,0.6,0.6) → lumS = 0.6*0.55 = 0.33 → q(0.33) = round(0.33*42.5)*6 = 84
    assert layer[2, 20, 0:3].tolist() == [84, 84, 84]


def test_shadow_layer_flat_blob_peak_golden():
    """整条不透明底带的峰值 alpha：sm=1 → js_round(fall_max·0.42·255)。"""
    layer = sprite.contact_shadow_layer(_char_blob(), _P(100, 100, 40, 60, 0.0), (0.6, 0.6, 0.6))
    # 手推：outH = 10；|dy| 最小的行是 y=2 与 y=3（t=0.25/0.35，|dy|=0.05）
    #       fall = 0.981677 → a = 1*0.981677*0.42 = 0.412304 → js_round(105.14) = 105
    assert layer[2, 20, 3] == 105
    assert layer[3, 20, 3] == 105
    # 纵向对称（高斯带对峰值线对称）：alpha[1] == alpha[4]、alpha[0] == alpha[5]
    assert layer[1, 20, 3] == layer[4, 20, 3] == 91
    assert layer[0, 20, 3] == layer[5, 20, 3] == 67
    # 底带之外 → 透明（prof 全 0）
    assert int(layer[:, 0, 3].max()) == 0
    assert int(layer[:, 39, 3].max()) == 0


def test_shadow_layer_ignores_alpha_above_bottom_band():
    """只有底部 8.5% 带内的 alpha 算落地轮廓；带以上不透明不产生影子。"""
    char = np.zeros((60, 40, 4), dtype=np.uint8)
    char[..., 0:3] = 200
    char[0:40, :, 3] = 255          # 只有上半身不透明，底部带（55..59）透明
    layer = sprite.contact_shadow_layer(char, _P(100, 100, 40, 60, 0.0), (0.6, 0.6, 0.6))
    assert int(layer[..., 3].max()) == 0


def test_shadow_layer_color_from_ambient_goldens():
    """颜色 = 环境色暗化版，量化到 6 的倍数（index.html:3133-3138）。"""
    char = _char_blob()
    c = _P(100, 100, 40, 60, 0.0)
    # 手推：lum = 0.6 → lumS = 0.6*0.55 = 0.33；rb = 1；q(0.33) = round(0.33*255/6)*6 = 84
    neutral = sprite.contact_shadow_layer(char, c, (0.6, 0.6, 0.6))
    assert neutral[2, 20, 0:3].tolist() == [84, 84, 84]
    # 手推：lum = 0.20962+0.7152*0.9... env=(0.9,0.9,0.3)：lum = 0.85668 → lumS = 0.471174
    #       rb = sqrt(0.9/0.3) = 1.732051
    #       colR = q(0.471174*1.732051) = q(0.816064) = round(34.682)*6 = 210
    #       colG = q(0.471174) = round(20.0249)*6 = 120
    #       colB = q(0.471174/1.732051) = q(0.272039) = round(11.5617)*6 = 72
    warm = sprite.contact_shadow_layer(char, c, (0.9, 0.9, 0.3))
    assert warm[2, 20, 0:3].tolist() == [210, 120, 72]
    # 冷环境：rb = sqrt(0.3/0.9) = 0.577350 → colR = q(0.471174*0.57735) = 30, colB = 84
    cool = sprite.contact_shadow_layer(char, c, (0.3, 0.3, 0.9))
    assert cool[2, 20, 0:3].tolist() == [30, 48, 84]
    # 无环境采样：按原版 LIGHT 初值 0.5 回退 → lumS = 0.275 → q = round(11.6875)*6 = 72
    assert sprite.contact_shadow_layer(char, c, None)[2, 20, 0:3].tolist() == [72, 72, 72]
    # 亮度下限 0.04：lum=0.04 → lumS = 0.022 → q(0.022) = round(0.935)*6 = 6
    dark = sprite.contact_shadow_layer(char, c, (0.04, 0.04, 0.04))
    assert dark[2, 20, 0:3].tolist() == [6, 6, 6]


def test_shadow_layer_color_uint8_wraparound_258_to_2():
    """q(v) 可以吐出 258（clampN(v,0,1)·255/6 半向 +∞ 取整 = 43 → 43·6）：
    numpy uint8 **回绕**成 2，与 JS 端口 q() 的 ``% 256`` 逐位一致
    （docs/arcam-render.js shadowColor：Uint8ClampedArray 会钳成 255，但对拍
    基准是回绕；极暖环境 r=1/b=0.05 → rb=√20，lumS·rb 被 clampN 夹到 1）。

    手推：lum = 0.2126+0.7152+0.0722·0.05 = 0.93141 → lumS = 0.512276
          colR = q(0.512276·4.472136) = q(1.0) = 43·6 = 258 → 2
          colG = q(0.512276) = round(21.7717)·6 = 132
          colB = q(0.512276/4.472136) = q(0.114537) = round(4.86782)·6 = 30
    """
    layer = sprite.contact_shadow_layer(_char_blob(), _P(100, 100, 40, 60, 0.0),
                                        (1.0, 1.0, 0.05))
    assert layer[2, 20, 0:3].tolist() == [2, 132, 30]


def test_shadow_layer_tiny_cases_return_transparent():
    """c.w < 14 / 立绘 w<12 或 h<12 → 原版直接不画（index.html:3118 / 3064）。"""
    tiny = sprite.contact_shadow_layer(_char_blob(), _P(100, 100, 10, 15, 0.0), (0.6,) * 3)
    assert tiny.shape == (1, 1, 4) and int(tiny.max()) == 0
    small_char = np.zeros((20, 10, 4), dtype=np.uint8)
    small_char[..., 3] = 255
    tiny2 = sprite.contact_shadow_layer(small_char, _P(100, 100, 40, 20, 0.0), (0.6,) * 3)
    assert tiny2.shape == (1, 1, 4) and int(tiny2.max()) == 0


# ── composite_sprite ───────────────────────────────────────────────────
def _base(h: int = 200, w: int = 200, value: float = 0.5) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.float64)


def test_composite_sprite_lands_exactly_in_bbox():
    """立绘严格落在 (u±w/2, v±h/2)：界内 = 立绘色、界外 = 底色（index.html:3146-3166）。"""
    out = sprite.composite_sprite(_base(), _char_blob(), _P(100, 100, 40, 60, 0.0), shadow=False)
    # u=100,v=100,w=40,h=60 → 局部矩形 x∈[80,120]、y∈[70,130]，锚点 = 外接矩形中心
    assert out[100, 100].tolist() == pytest.approx([200 / 255] * 3)
    assert out[100, 79].tolist() == pytest.approx([0.5] * 3)     # 左界外
    assert out[100, 120].tolist() == pytest.approx([0.5] * 3)    # 右界外
    assert out[69, 100].tolist() == pytest.approx([0.5] * 3)     # 上界外
    assert out[130, 100].tolist() == pytest.approx([0.5] * 3)    # 下界外
    # 人形只占中间 20 列（立绘 10..29）→ 画布 x = 90..109
    assert out[100, 90].tolist() == pytest.approx([200 / 255] * 3)
    assert out[100, 89].tolist() == pytest.approx([0.5] * 3)


def test_composite_sprite_subpixel_placement():
    """亚像素落位：边界像素值随小数锚点连续变化（双线性 warp，不是取整吸附）。

    左半黑右半白的 20×20 立绘，中心缝在立绘局部 x=10：
      u=100.00 → 画布像素 100 的局部坐标 0.5 → 落在白色侧 → 1.0
      u=100.25 → 0.25 → 75% 白 → 0.75
      u=100.50 → 0.00 → 黑白各半 → 0.50
      u=100.75 → -0.25 → 25% 白 → 0.25
    """
    char = np.zeros((20, 20, 4), dtype=np.uint8)
    char[..., 3] = 255
    char[:, :10, 0:3] = 0
    char[:, 10:, 0:3] = 255
    base = _base()
    got = []
    for u in (100.0, 100.25, 100.5, 100.75):
        out = sprite.composite_sprite(base, char, _P(u, 100.0, 20, 20, 0.0), shadow=False)
        got.append(float(out[100, 100, 0]))
    assert got == [pytest.approx(v) for v in (1.0, 0.75, 0.5, 0.25)]
    # 界外不受锚点小数部分影响
    out_a = sprite.composite_sprite(base, char, _P(100.0, 100.0, 20, 20, 0.0), shadow=False)
    out_b = sprite.composite_sprite(base, char, _P(100.25, 100.0, 20, 20, 0.0), shadow=False)
    assert out_a[100, 95, 0] == out_b[100, 95, 0]


def test_composite_sprite_rotation_keeps_center():
    """锚点中心旋转 90°：立绘仍在中心、宽高互换（rot=π/2）。"""
    char = np.zeros((20, 40, 4), dtype=np.uint8)
    char[..., 0:3] = 180
    char[..., 3] = 255
    out = sprite.composite_sprite(_base(), char, _P(100, 100, 40, 20, math.pi / 2), shadow=False)
    # 旋转后占 x∈[90,110]、y∈[80,120]（原 -x 方向对着 +y？canvas rotate 顺时针）
    assert out[100, 100].tolist() == pytest.approx([180 / 255] * 3)
    a = np.abs(out - 0.5).max(axis=2) > 1e-6
    ys, xs = np.nonzero(a)
    assert abs((xs.min() + xs.max()) / 2 - 100) <= 1.0
    assert abs((ys.min() + ys.max()) / 2 - 100) <= 1.0
    assert (xs.max() - xs.min()) < (ys.max() - ys.min())   # 变竖长了


def test_composite_sprite_shadow_drawn_below_char():
    """阴影在立绘**之前**铺：重叠像素只见立绘色（studio 上一版就栽在这）。"""
    char = _char_blob()
    c = _P(100, 100, 40, 60, 0.0)
    out = sprite.composite_sprite(_base(), char, c, shadow=True, ambient=(0.6, 0.6, 0.6))
    no = sprite.composite_sprite(_base(), char, c, shadow=False)
    # 立绘下沿一带（y ≤ 130）立绘不透明处：两次结果一致（阴影被立绘盖住）
    assert np.allclose(out[100, 100], no[100, 100])
    assert np.allclose(out[129, 100], no[129, 100])


def test_composite_sprite_shadow_darkens_below_char():
    """阴影压在立绘下方接触线一带、只影响人形投影范围。"""
    char = _char_blob()
    c = _P(100, 100, 40, 60, 0.0)
    out = sprite.composite_sprite(_base(), char, c, shadow=True, ambient=(0.6, 0.6, 0.6))
    no = sprite.composite_sprite(_base(), char, c, shadow=False)
    # 手推：top = h/2 - shh*SH_PEAK = 30 - 9.6*0.3 = 27.12 → 画布 y ∈ [127.12, 136.72]
    assert out[132, 100][0] < no[132, 100][0]        # 人形正下方被压暗
    assert out[132, 20][0] == 0.5                    # 投影范围外不动
    assert out[100, 100][0] == no[100, 100][0]       # 立绘身上不叠阴影


def test_composite_sprite_shadow_color_can_lighten_dark_ground():
    """如实保留的偏离：用**平均**地面色代替**局部**地面色 —— 局部比平均暗会被提亮。"""
    char = _char_blob()
    c = _P(100, 100, 40, 60, 0.0)
    base = _base(value=0.05)                          # 很暗的地面
    out = sprite.composite_sprite(base, char, c, shadow=True, ambient=(1.0, 1.0, 1.0))
    # 阴影色 = q(0.55) = 138/255 = 0.541 > 0.05 → 盖上去是提亮（原版注释 3122-3124）
    assert out[132, 100][0] > 0.05


def test_composite_sprite_free_mode_leaves_no_shadow():
    """自由模式（不知道地面在哪）不画阴影：shadow=False 时环境色不产生任何影响。"""
    char = _char_blob()
    c = _P(100, 100, 40, 60, 0.0)
    a = sprite.composite_sprite(_base(), char, c, shadow=False)
    b = sprite.composite_sprite(_base(), char, c, shadow=False, ambient=(0.1, 0.1, 0.1))
    assert np.array_equal(a, b)


def test_composite_sprite_reuses_supplied_shadow_layer():
    """shadow_layer 传入 = 内部现算（原版 footCv 缓存同一张）。"""
    char = _char_blob()
    c = _P(100, 100, 40, 60, 0.0)
    layer = sprite.contact_shadow_layer(char, c, (0.6, 0.6, 0.6))
    a = sprite.composite_sprite(_base(), char, c, shadow=True, ambient=(0.6, 0.6, 0.6))
    b = sprite.composite_sprite(_base(), char, c, shadow=True, shadow_layer=layer)
    assert np.array_equal(a, b)


def test_composite_sprite_rotated_shadow_follows_rotation():
    """阴影跟着立绘一起转（同一 translate→rotate 变换）。

    画布 y 向下、``rotate`` 正向是顺时针：rot=+90° 时立绘的"下方"（局部 +y）
    转到画布的**左**侧 —— 局部 (0, +30) → 画布 (-30, 0)。影子因此落在 x≈[63,73]。
    """
    char = _char_blob()
    c = _P(100, 100, 40, 60, math.pi / 2)
    out = sprite.composite_sprite(_base(), char, c, shadow=True, ambient=(0.6, 0.6, 0.6))
    no = sprite.composite_sprite(_base(), char, c, shadow=False)
    diff = np.abs(out - no).max(axis=2) > 1e-6
    assert diff[100, 68] and not diff[100, 100]     # 左侧有影子、立绘身上没有
    assert not diff[132, 100]                       # 原"下方"方向不再有影子
    ys, xs = np.nonzero(diff)
    assert 60 <= xs.min() and xs.max() <= 75 and 80 <= ys.min() and ys.max() <= 120


def test_composite_sprite_alpha_blend_is_source_over():
    """半透明立绘 = 标准 source-over（Cr = Cs·αs + Cb·(1-αs)）。"""
    char = np.zeros((20, 20, 4), dtype=np.uint8)
    char[..., 0:3] = 100
    char[..., 3] = 128
    out = sprite.composite_sprite(_base(value=0.4), char, _P(100, 100, 20, 20, 0.0), shadow=False)
    expect = (100 / 255) * (128 / 255) + 0.4 * (1 - 128 / 255)
    assert out[100, 100][0] == pytest.approx(expect, abs=1 / 510)


def test_composite_sprite_offcanvas_anchor_is_stable():
    """画布外锚点不崩：不出墨、有限值。"""
    char = _char_blob()
    for u, v in ((-50.0, -50.0), (250.0, 100.0), (100.0, 250.0), (-9999.0, 0.0), (1e6, 1e6)):
        out = sprite.composite_sprite(_base(), char, _P(u, v, 40, 60, 0.37),
                                      shadow=True, ambient=(0.5, 0.5, 0.5))
        assert out.shape == (200, 200, 3)
        assert np.isfinite(out).all()
        if abs(u) > 200 and abs(v) > 200:
            assert np.allclose(out, 0.5)


def test_composite_sprite_degenerate_placement_returns_base():
    """c 为 None / 尺寸退化 → 原样返回画布副本（drawImage 零尺寸等于不画）。"""
    base = _base()
    assert np.array_equal(sprite.composite_sprite(base, _char_blob(), None), base)
    zero_w = sprite.composite_sprite(base, _char_blob(), _P(100, 100, 0, 60, 0.0))
    assert np.array_equal(zero_w, base)


def test_composite_sprite_returns_float64_and_keeps_inputs():
    """返回新画布 float64 [0,1]；入参一个字节都不动。"""
    base_u8 = np.full((200, 200, 3), 128, dtype=np.uint8)
    char = _char_blob()
    base_f = base_u8.astype(np.float64) / 255.0
    snapshot_b, snapshot_c = base_u8.copy(), char.copy()
    out = sprite.composite_sprite(base_u8, char, _P(100, 100, 40, 60, 0.2), shadow=True,
                                  ambient=(0.6, 0.6, 0.6))
    assert out.dtype == np.float64
    assert 0.0 <= out.min() and out.max() <= 1.0
    assert np.array_equal(base_u8, snapshot_b) and np.array_equal(char, snapshot_c)
    # uint8 底与等价 float 底走同一条路径
    out_f = sprite.composite_sprite(base_f, char, _P(100, 100, 40, 60, 0.2), shadow=True,
                                    ambient=(0.6, 0.6, 0.6))
    assert np.array_equal(out, out_f)


def test_shadow_az_zero_is_bit_identical():
    """shadow_az=0 必须与原版行为逐位相同（新功能的默认路径不许漂）。"""
    from arcam.sprite import contact_shadow_layer
    from arcam.placement import SpritePlacement

    rng = np.random.default_rng(5)
    char = np.zeros((300, 200, 4))
    char[..., :3] = rng.random((300, 200, 3))
    char[..., 3] = (rng.random((300, 200)) > 0.3).astype(float)
    c = SpritePlacement(u=960.0, v=540.0, w=200.0, h=300.0, rot=0.0)
    ambient = (0.5, 0.45, 0.4)
    base = contact_shadow_layer(char, c, ambient)
    same = contact_shadow_layer(char, c, ambient, shadow_az=0.0, shadow_opacity=1.0)
    assert np.array_equal(base, same), "默认参数改变了阴影输出"


def test_shadow_az_nonzero_shifts_profile():
    """shadow_az≠0 时影子拖偏：单元级直测 _shear_profile + 端到端抽样。

    端到端只用「有像素变化」这种弱断言——斜射量经 alpha 量化后小角度可能
    逐位相同（量化吞掉），强断言属于测试过拟合；精确行为由下面的单元测试钉。
    """
    from arcam.sprite import _shear_profile

    # 单元级：单峰轮廓，az=45° 时每行右移 shear·(y+0.5) 列（峰位可无歧义读出）
    sm = np.zeros(21, dtype=np.float64)
    sm[10] = 1.0                                 # 峰在 x=10
    out = _shear_profile(sm, 5, 45.0)
    shear = math.tan(math.radians(45.0)) * (5 / 21.0)
    for y in range(5):
        # out[x] = sm[x-s]：sm 的峰落在 out 的 x = 10+s（右移），y 越大移越远
        want_peak = 10.0 + shear * (y + 0.5)
        got_peak = float(np.argmax(out[y]))
        assert abs(got_peak - want_peak) < 1.0, f"y={y}: 峰位 {got_peak} != {want_peak:.2f}"
    assert float(np.argmax(out[4])) > 10.0, "az=45° 应让峰右移"
    assert float(np.argmax(out[0])) >= 10.0, "第 0 行也只右移（半列起）"

    # 端到端：高对比 footprint + 大角度，确保至少有些像素变了
    from arcam.sprite import contact_shadow_layer
    from arcam.placement import SpritePlacement
    rng = np.random.default_rng(6)
    char = np.zeros((300, 200, 4))
    char[..., :3] = 0.6
    char[..., 3] = (rng.random((300, 200)) > 0.02).astype(float)
    char[:, :60, 3] = 0.0                       # 左右留空 → 轮廓有边（拖偏才看得出来）
    char[:, 140:, 3] = 0.0
    c = SpritePlacement(u=960.0, v=540.0, w=200.0, h=300.0, rot=0.0)
    base = contact_shadow_layer(char, c, (0.5, 0.5, 0.5))
    pos = contact_shadow_layer(char, c, (0.5, 0.5, 0.5), shadow_az=75.0)
    neg = contact_shadow_layer(char, c, (0.5, 0.5, 0.5), shadow_az=-75.0)
    assert not np.array_equal(base, pos), "az=75° 没产生拖偏"
    assert not np.array_equal(pos, neg), "az 符号没改变拖偏方向"


def test_shadow_opacity_scales_alpha():
    """shadow_opacity 线性缩放浓度（2.0 ≈ 翻倍，0.5 ≈ 减半；1.0 不变）。"""
    from arcam.sprite import contact_shadow_layer
    from arcam.placement import SpritePlacement

    rng = np.random.default_rng(8)
    char = np.zeros((300, 200, 4))
    char[..., :3] = 0.6
    char[..., 3] = (rng.random((300, 200)) > 0.2).astype(float)
    c = SpritePlacement(u=960.0, v=540.0, w=200.0, h=300.0, rot=0.0)
    one = contact_shadow_layer(char, c, (0.5, 0.5, 0.5)).astype(np.float64)
    half = contact_shadow_layer(char, c, (0.5, 0.5, 0.5), shadow_opacity=0.5).astype(np.float64)
    mask = one[..., 3] > 0
    assert mask.any()
    # 半浓度的 alpha ≈ 原值一半（量化容 1 个字节档：floor(a·255+0.5) 在奇数值上
    # 会差 0.5，如 3 → floor(3/2·0.5+0.5)=2 而 3/2=1.5）
    assert np.abs(half[..., 3][mask] - one[..., 3][mask] / 2).max() <= 1.0 + 1e-9
