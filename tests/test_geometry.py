"""geometry 模块测试：投影 golden（手工推导）、place 反投影、round-trip、mmd 矩阵交叉验证。

golden 用例的期望值全部按 artifact A-projection-core.md §3.1/§3.2 的公式**手工推导**
（推导中间量写在每条断言的注释里），实现必须逐位复现原版 index.html:1041-1057 / 3184-3202。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from arcam.geometry import K_LONG, K_SHORT, Camera, focal_px, place, project


def _cam(w=1920, h=1080, feq=23.0, pitch=0.0, roll=0.0, height=1.6) -> Camera:
    return Camera(width=w, height=h, height_m=height, feq_mm=feq,
                  pitch_deg=pitch, roll_deg=roll)


# ─────────────────────────── focal（index.html:1043-1047） ───────────────────────────

def test_focal_wide_uses_long_edge():
    """1920×1080、feq=23：L=1920、Sd=1080、L/Sd=1.7778 ≥ 4/3 → wide。
    F = (L/2)·feq/K_LONG = 960·23/17.3064 = 22080/17.3064 = 1275.828595201775 px。
    （artifact §3.1 数值示例 ≈1277，逐位值以本文为准。）"""
    assert focal_px(_cam(1920, 1080, 23.0)) == pytest.approx(1275.828595201775, abs=1e-9)


def test_focal_non_wide_uses_short_edge():
    """1200×1080：L/Sd = 1.1111 < 4/3 → 非 wide。
    F = (Sd/2)·feq/K_SHORT = 540·23/12.9798 = 12420/12.9798 = 956.8714464013314 px。"""
    assert focal_px(_cam(1200, 1080, 23.0)) == pytest.approx(956.8714464013314, abs=1e-9)


def test_focal_square_canvas():
    """900×900：L/Sd = 1 < 4/3 → 非 wide。F = 450·23/12.9798 = 10350/12.9798
    = 797.3928720011095 px。"""
    assert focal_px(_cam(900, 900, 23.0)) == pytest.approx(797.3928720011095, abs=1e-9)


def test_focal_portrait_uses_long_edge():
    """1080×1920（竖画布）：L=1920 → F = 960·28/17.3064 = 26880/17.3064
    = 1553.1826376369436 px（wide 判据是长宽比，与方向无关）。"""
    assert focal_px(_cam(1080, 1920, 28.0)) == pytest.approx(1553.1826376369436, abs=1e-9)


def test_focal_continuous_at_4_3_boundary():
    """恰好 4/3 走 wide（>=）：1600×1200 → 800·23/17.3064；1599×1200 → 600·23/12.9798。
    K_LONG/K_SHORT = 0.8/0.6 = 4/3，两支在边界上数值连续（各 1063.1904960014792）。"""
    at_boundary = focal_px(_cam(1600, 1200, 23.0))
    below = focal_px(_cam(1599, 1200, 23.0))
    assert at_boundary == pytest.approx(1063.1904960014792, abs=1e-9)
    assert below == pytest.approx(at_boundary, abs=1e-9)


def test_focal_linear_in_feq():
    """F ∝ feq：46mm 恰好是 23mm 的两倍（两侧分支同比例）。"""
    assert focal_px(_cam(1920, 1080, 46.0)) == pytest.approx(
        2.0 * focal_px(_cam(1920, 1080, 23.0)), abs=1e-9)


def test_focal_zero_short_edge_matches_js_no_crash():
    """退化画布（某一维为 0）不许抛 ZeroDivisionError。

    原版 JS 的 ``L/Sd`` 在 Sd=0 时得 Infinity（L>0）→ Infinity >= 4/3 → 走 wide 支；
    0×0 得 NaN → 非 wide，但两支都得 0.0。Python 直接 ``/0`` 会崩 —— 回归防护。
    F(1920×0) = 960·23/17.3064 = 22080/17.3064 = 1275.828595201775（wide 支）；
    F(0×1080) 的 L = max(0,1080) = 1080 → 540·23/17.3064 = 717.6535848009985。"""
    assert focal_px(_cam(1920, 0, 23.0)) == pytest.approx(1275.828595201775, abs=1e-9)
    assert focal_px(_cam(0, 1080, 23.0)) == pytest.approx(717.6535848009985, abs=1e-9)
    assert focal_px(_cam(0, 0, 23.0)) == 0.0
    # 下游 project/place 在退化画布上同样不能崩（focal 有限即可算）。
    # place 取 (960, 200)：b = 200/F ≈ 0.157 → d = 1.6/b ≈ 10.2（在 [0.5,20] 内）；
    # 若取主点 (960, 0) 则 b=0 → |den|<1e-6 → None（射线与地平面平行，正常退化）。
    assert project(0.0, 0.0, 4.0, _cam(1920, 0, 23.0)) is not None
    assert place(960.0, 200.0, _cam(1920, 0, 23.0)) is not None


def test_place_zero_focal_returns_none_not_crash():
    """F=0（0×0 画布或 feq=0）时 place 必须返回 None，不许抛 ZeroDivisionError。

    原版 JS 的 ``/0`` 得 Infinity/NaN 传播（不抛异常），Python 直接除会崩 —— 按
    focal_px 对退化画布既定的「不抛」策略收口为 None。同相机下 project 仍不崩
    （F=0 时全部塌缩到主点）。"""
    assert place(960.0, 200.0, _cam(0, 0, 23.0)) is None
    assert place(960.0, 540.0, _cam(1920, 1080, 0.0)) is None
    assert place(0.0, 0.0, _cam(0, 0, 0.0)) is None
    # project 在同两款退化相机下照旧返回主点（与 JS 一致，不抛）
    assert project(0.0, 0.0, 4.0, _cam(0, 0, 23.0)) == (0.0, 0.0)
    assert project(0.0, 0.0, 4.0, _cam(1920, 1080, 0.0)) == (960.0, 540.0)


def test_sensor_constants():
    """K_LONG = 0.8·43.266/2 = 17.3064；K_SHORT = 0.6·43.266/2 = 12.9798（index.html:1042）。"""
    assert K_LONG == pytest.approx(0.8 * 43.266 / 2, abs=0.0)
    assert K_SHORT == pytest.approx(0.6 * 43.266 / 2, abs=0.0)


# ───────────────────── project golden（index.html:1048-1057） ─────────────────────
# 公式：p=pitch·π/180, r=roll·π/180
#   Zc = -y·sin p + d·cos p ；Yc = -y·cos p - d·sin p
#   Xr =  x·cos r - Yc·sin r ；Yr =  x·sin r + Yc·cos r
#   u = W/2 + F·Xr/Zc ；v = H/2 + F·Yr/Zc

def test_project_golden_a_wide_pitch0_roll0():
    """A. 1920×1080、feq=23、pitch=0、roll=0；project(x=1.2, y=-0.6, d=4.0)。
    F = 1275.828595201775（wide）。p=0 → Zc = d = 4.0，Yc = -y = 0.6，Xr = 1.2，Yr = 0.6。
    u = 960 + F·1.2/4 = 960 + 382.7485785605325 = 1342.7485785605325
    v = 540 + F·0.6/4 = 540 + 191.37428928026624 = 731.3742892802662"""
    got = project(1.2, -0.6, 4.0, _cam())
    assert got == pytest.approx((1342.7485785605325, 731.3742892802662), abs=1e-9)


def test_project_golden_b_non_wide():
    """B. 1200×1080（非 wide）、pitch=0、roll=0；project(x=0, y=-1.6, d=3.0)。
    F = 956.8714464013314。Zc = 3.0，Yc = 1.6，Xr = 0，Yr = 1.6。
    u = 600 + 0 = 600.0（主点在画布中心）
    v = 540 + 956.8714464013314·1.6/3 = 540 + 510.3314380807101 = 1050.33143808071"""
    got = project(0.0, -1.6, 3.0, _cam(1200, 1080, 23.0))
    assert got == pytest.approx((600.0, 1050.33143808071), abs=1e-9)


def test_project_golden_c_pitch_positive():
    """C. 1920×1080、feq=23、pitch=+10（俯视）；project(x=0.5, y=-0.8, d=4.0)。
    sin10° = 0.17364817766693033、cos10° = 0.984807753012208。
    Zc = 0.8·sin10 + 4·cos10 = 0.13891854213354426 + 3.939231012048832 = 4.078149554182376
    Yc = 0.8·cos10 - 4·sin10 = 0.7878462024097664 - 0.6945927106677213 = 0.0932534917420451
    俯看使 Yc 变小（天上移）：v = 540 + F·0.0932534917420451/Zc = 569.1738862898977
    u = 960 + F·0.5/Zc = 1116.42248748496"""
    got = project(0.5, -0.8, 4.0, _cam(pitch=10.0))
    assert got == pytest.approx((1116.42248748496, 569.1738862898977), abs=1e-9)


def test_project_golden_d_pitch_negative():
    """D. pitch=-20（仰视）；project(x=-0.5, y=-1.0, d=5.0)。
    sin(-20°) = -0.3420201433256687、cos(-20°) = 0.9396926207859084。
    Zc = 1.0·(-0.3420201433256687) + 5·0.9396926207859084 = 4.356442960603873
    Yc = 1.0·0.9396926207859084 - 5·(-0.3420201433256687) = 2.649793337414252
    Xr = -0.5、Yr = 2.649793337414252
    u = 960 - F·0.5/Zc = 813.5699185391235（x<0 → 画面左）
    v = 540 + F·2.649793337414252/Zc = 1316.0189085041138（仰视把地面压到画面下缘之下附近）"""
    got = project(-0.5, -1.0, 5.0, _cam(pitch=-20.0))
    assert got == pytest.approx((813.5699185391235, 1316.0189085041138), abs=1e-9)


def test_project_golden_e_pitch_and_roll():
    """E. pitch=+15、roll=+30；project(x=0.8, y=-0.4, d=3.5)。
    sin15 = 0.25881904510252074、cos15 = 0.9659258262890683、
    sin30 = 0.5、cos30 = 0.8660254037844387。
    Zc = 0.4·sin15 + 3.5·cos15 = 0.10352761804100829 + 3.380740392011739 = 3.4842680100527474
    Yc = 0.4·cos15 - 3.5·sin15 = 0.3863703305156273 - 0.9058666578588226 = -0.5194963273431953
    Xr = 0.8·cos30 - Yc·sin30 = 0.6928203230275509 + 0.25974816367159765 = 0.9525684866991486
    Yr = 0.8·sin30 + Yc·cos30 = 0.4 - 0.449892... = -0.049890...
    u = 960 + F·Xr/Zc = 1308.8004110798747
    v = 540 + F·Yr/Zc = 521.7292927880081（roll>0 使相机系 +X 下移 → v 增大、画面顺时针）"""
    got = project(0.8, -0.4, 3.5, _cam(pitch=15.0, roll=30.0))
    assert got == pytest.approx((1308.8004110798747, 521.7292927880081), abs=1e-9)


def test_project_golden_f_roll_90():
    """F. roll=+90、pitch=0；project(x=1.0, y=-0.5, d=4.0)。
    cos90° = 6.123233995736766e-17、sin90° = 1。Zc = 4、Yc = 0.5。
    Xr = 1.0·cos90 - 0.5·sin90 ≈ -0.5；Yr = 1.0·sin90 + 0.5·cos90 ≈ 1.0。
    u = 960 + F·(-0.5)/4 = 800.5214255997781
    v = 540 + F·1.0/4 = 858.9571488004437（相机系 +X 转到画面下方，+Y 转到画面左方）"""
    got = project(1.0, -0.5, 4.0, _cam(roll=90.0))
    assert got == pytest.approx((800.5214255997781, 858.9571488004437), abs=1e-9)


def test_project_golden_g_feq_doubles_offset():
    """G. feq=46（F 翻倍）→ 主点偏移翻倍：project(1.2, -0.6, 4.0)。
    u = 960 + 2·382.7485785605325 = 1725.497157121065
    v = 540 + 2·191.37428928026624 = 922.7485785605325"""
    got = project(1.2, -0.6, 4.0, _cam(feq=46.0))
    assert got == pytest.approx((1725.497157121065, 922.7485785605325), abs=1e-9)


def test_project_golden_h_no_fov_clip():
    """H. 无 FOV 剔除（artifact §3.2 末注：加了 clip 往返测试就挂）。project(6, 0, 2)：
    Zc = 2、Yc = 0、Xr = 6、Yr = 0 → u = 960 + F·6/2 = 960 + 3827.485785605325
    = 4787.485785605325（远在 1920 画布外，原版照样返回）、v = 540.0。"""
    got = project(6.0, 0.0, 2.0, _cam())
    assert got == pytest.approx((4787.485785605325, 540.0), abs=1e-9)


def test_project_golden_i_pitch40_roll_neg15():
    """I. pitch=+40、roll=-15；project(x=-1.5, y=-2.2, d=6.0)。
    sin40 = 0.6427876096865393、cos40 = 0.766044443118978、
    sin(-15°) = -0.25881904510252074、cos(-15°) = 0.9659258262890683。
    Zc = 2.2·sin40 + 6·cos40 = 1.4141327413103865 + 4.596266658713868 = 6.010399400024255
    Yc = 2.2·cos40 - 6·(-sin40) = 1.6852977748617516 + 3.856725658119236 = 5.542023432980988
    Xr = -1.5·cos(-15) - Yc·sin(-15) = -1.4488887394336024 + 1.434388... = -0.0145...
    Yr = -1.5·sin(-15) + Yc·cos(-15) = 0.3882285676537811 + 5.353... = 5.7415...
    u = 960 + F·Xr/Zc = 533.1468115768143
    v = 540 + F·Yr/Zc = 177.18573902840762（强俯视 + 负滚转，点被推到画面上缘）"""
    got = project(-1.5, -2.2, 6.0, _cam(pitch=40.0, roll=-15.0))
    assert got == pytest.approx((533.1468115768143, 177.18573902840762), abs=1e-9)


def test_project_returns_none_behind_camera():
    """J. Zc <= 0.05 → None。pitch=80、project(x=0.5, y=0.5, d=1.0)：
    Zc = -0.5·sin80 + 1.0·cos80 = -0.492403876506104 + 0.17364817766693033
       = -0.31875569883917367 <= 0.05 → None（相机平面之后）。"""
    assert project(0.5, 0.5, 1.0, _cam(pitch=80.0)) is None


def test_project_zc_boundary_exactly_005():
    """K. 边界：d=0.05、y=0 → Zc = 0.05 恰等于阈值，判据是 <=  → None；
    d=0.051 时 Zc = 0.051 > 0.05，且 Xr=Yr=0（相机原点）→ 正好落在主点 (960, 540)。"""
    assert project(0.0, 0.0, 0.05, _cam()) is None
    assert project(0.0, 0.0, 0.051, _cam()) == pytest.approx((960.0, 540.0), abs=1e-9)


def test_project_camera_origin_is_principal_point():
    """L. pitch=roll=0 时相机系原点 (0,0,d>0.05) → Xr=Yr=0 → 主点（W/2, H/2）。"""
    assert project(0.0, 0.0, 7.0, _cam(1200, 900, 23.0)) == pytest.approx((600.0, 450.0), abs=1e-9)


# ───────────────── place golden（index.html:3184-3202） ─────────────────
# place 是 project 固定 y=-h 的解析逆：
#   A = (u-W/2)/F·cos r + (v-H/2)/F·sin r ；B = -(u-W/2)/F·sin r + (v-H/2)/F·cos r
#   d = h·(cos p - B·sin p)/(sin p + B·cos p) ；x = A·(h·sin p + d·cos p)
#   clamp：S.d∈[0.5,20]（3197）、S.x∈[-6,6]（3198）

def test_place_golden_pitch0_roll0():
    """P1/P2. pitch=0、roll=0、h=1.6、F=1275.828595201775。
    A = (u-960)/F、B = (v-540)/F；den = B；d = 1.6/B；Zc = d；x = A·d = 1.6·A/B。
    P1: u = 960+0.1F、v = 540+0.2F → A=0.1、B=0.2 → d = 1.6/0.2 = 8.0、x = 0.1·8 = 0.8
    P2: v = 540+1.0F → B=1.0 → d = 1.6、x = 0.1·1.6 = 0.16"""
    assert place(960 + 0.1 * focal_px(_cam()), 540 + 0.2 * focal_px(_cam()), _cam()) \
        == pytest.approx((0.8, 8.0), abs=1e-9)
    assert place(960 + 0.1 * focal_px(_cam()), 540 + 1.0 * focal_px(_cam()), _cam()) \
        == pytest.approx((0.16, 1.6), abs=1e-9)


def test_place_clamps_x_and_d():
    """P3/P4. 原版写回 clamp：S.x ∈ [-6,6]（3198）、S.d ∈ [0.5,20]（3197），如实保留。
    P3: u = 960+10F、v = 540+0.2F → A=10、d=8 → x = 80 → 夹到 6.0 → (6.0, 8.0)
    P4: v = 540+0.01F → B=0.01 → d = 160 → 夹到 20；x = 0.1·160 = 16 → 夹到 6 → (6.0, 20.0)"""
    f = focal_px(_cam())
    assert place(960 + 10 * f, 540 + 0.2 * f, _cam()) == pytest.approx((6.0, 8.0), abs=1e-9)
    assert place(960 + 0.1 * f, 540 + 0.01 * f, _cam()) == pytest.approx((6.0, 20.0), abs=1e-9)


def test_place_degenerate_returns_none():
    """P5/P6/P7. 三条早退：|den| < 1e-6（3193）、d <= 0.05（3195）。
    P5: v = 540+32F → B=32 → d = 1.6/32 = 0.05 → d<=0.05 → None
    P6: 主点 (960,540) pitch=0 → 视线沿地平面（den = sin0 + 0·cos0 = 0）→ None
    P7: pitch=30、v = 540+1.73F → B=1.73 → den = sin30+1.73·cos30 = 0.5+1.49818… = 1.99818…
        d = 1.6·(cos30-1.73·sin30)/den = 1.6·(0.8660254-0.865)/1.99818 ≈ 0.00082 <= 0.05 → None"""
    f = focal_px(_cam())
    assert place(960 + 0.1 * f, 540 + 32 * f, _cam()) is None
    assert place(960.0, 540.0, _cam()) is None
    assert place(960.0, 540 + 1.73 * f, _cam(pitch=30.0)) is None


def test_place_golden_roll30():
    """P8. roll=30、pitch=0；u = 960+0.05F、v = 540+0.15F。
    A = 0.05·cos30 + 0.15·sin30 = 0.04330127018922193 + 0.075 = 0.11830127018922193
    B = -0.05·sin30 + 0.15·cos30 = -0.025 + 0.12990381056766582 = 0.10490381056766582
    den = B（pitch=0）→ d = 1.6/0.10490381056766582 = 15.25206750204709
    Zc = d、x = A·Zc = 1.8043389585039233（均未触及 clamp）"""
    f = focal_px(_cam())
    assert place(960 + 0.05 * f, 540 + 0.15 * f, _cam(roll=30.0)) \
        == pytest.approx((1.8043389585039233, 15.25206750204709), abs=1e-9)


# ───────────────────────── round-trip（place→project） ─────────────────────────

def test_round_trip_sweep_under_1e_9px():
    """place(project(x,-h,d)) 再投回去，误差 < 1e-9 px。

    覆盖 pitch ∈ [-30, 40]、roll ∈ {-45,-15,0,20,90}、多组 (x,d)、wide/非-wide/竖画布。
    全扫描最差 1.5e-10 px，出现在 |u| ~ 5e4 的离画布极端点上（float64 在该量级 ulp≈1e-11，
    且 place 的 den 接近 0 时条件数放大）——原版注释（index.html:1041）宣称的 3.6e-15
    是画幅内典型点的解析可逆性，见下一档更严的断言。"""
    worst = 0.0
    for pitch in (-30.0, -12.5, 0.0, 7.5, 25.0, 40.0):
        for roll in (-45.0, -15.0, 0.0, 20.0, 90.0):
            for x, d in ((-5.0, 1.0), (0.5, 3.0), (2.0, 6.0), (-1.5, 12.0), (3.5, 0.6)):
                for w, h, feq in ((1920, 1080, 23.0), (1200, 1080, 23.0), (1080, 1920, 28.0)):
                    cam = _cam(w, h, feq, pitch, roll)
                    uv = project(x, -cam.height_m, d, cam)
                    if uv is None:
                        continue  # 仰角 + 近距离时该点本就在相机平面之后（Zc<=0.05）
                    xd = place(uv[0], uv[1], cam)
                    assert xd is not None
                    uv2 = project(xd[0], -cam.height_m, xd[1], cam)
                    assert uv2 is not None
                    worst = max(worst, math.hypot(uv2[0] - uv[0], uv2[1] - uv[1]))
    assert worst < 1e-9


def test_round_trip_in_canvas_subset_under_1e_11px():
    """画布内典型点（离掠射角足够远）误差 < 1e-11 px：实测最差 2.27e-13 px，
    与原版声明 3.6e-15（index.html:1041 注释）同量级。"""
    worst = 0.0
    for pitch in (-30.0, -10.0, 0.0, 15.0, 40.0):
        for roll in (0.0, 30.0, -20.0):
            for x, d in ((0.0, 4.0), (1.0, 3.0), (-2.0, 5.0), (3.0, 6.0)):
                cam = _cam(1920, 1080, 23.0, pitch, roll)
                uv = project(x, -cam.height_m, d, cam)
                if uv is None or not (0.0 < uv[0] < 1920.0 and 0.0 < uv[1] < 1080.0):
                    continue
                xd = place(uv[0], uv[1], cam)
                uv2 = project(xd[0], -cam.height_m, xd[1], cam)
                worst = max(worst, math.hypot(uv2[0] - uv[0], uv2[1] - uv[1]))
    assert worst < 1e-11


# ───────────── mmd 4×4 矩阵交叉验证（mmd.js:836-858，artifact C §3.2） ─────────────

def _view_matrix(pitch_deg: float, roll_deg: float, cam_h: float) -> np.ndarray:
    """mmd.js:836-848 viewMatrix 逐字矩阵（THREE.Matrix4.set 行主序，可原样搬进 numpy）。"""
    p, r = math.radians(pitch_deg), math.radians(roll_deg)
    sp, cp, sr, cr = math.sin(p), math.cos(p), math.sin(r), math.cos(r)
    return np.array([
        [cr, cp * sr, -sp * sr, -cam_h * cp * sr],
        [-sr, cp * cr, -sp * cr, -cam_h * cp * cr],
        [0.0, sp, cp, -cam_h * sp],
        [0.0, 0.0, 0.0, 1.0],
    ], dtype=np.float64)


def _proj_matrix(f: float, w: int, h: int, near: float = 0.05, far: float = 120.0) -> np.ndarray:
    """mmd.js:849-858 projMatrix 逐字矩阵（near=0.05/far=120 为 mmd.js:985 写死值）。"""
    return np.array([
        [2.0 * f / w, 0.0, 0.0, 0.0],
        [0.0, 2.0 * f / h, 0.0, 0.0],
        [0.0, 0.0, (far + near) / (near - far), 2.0 * far * near / (near - far)],
        [0.0, 0.0, -1.0, 0.0],
    ], dtype=np.float64)


def mmd_project_px(x: float, y_world: float, z_world: float, cam: Camera) -> tuple[float, float]:
    """世界点 (X, Y, Z) 经 mmd 矩阵管线得到的屏幕像素。

    mmd.js:979 把人物摆到 z' = -d（three 世界 Z 与 arcam 世界 Z 朝观察者相反），故传入
    -z_world；project() 的 y 参数 = Y_world - camH（相机系 y，artifact A §1.3）。
    NDC → 屏幕：u = (ndc_x+1)·W/2、v = (1-ndc_y)·H/2（three/OpenGL 约定 y 向上、屏幕 y 向下）。
    """
    f = focal_px(cam)
    view = _view_matrix(cam.pitch_deg, cam.roll_deg, cam.height_m)
    proj = _proj_matrix(f, cam.width, cam.height)
    world = np.array([x, y_world, -z_world, 1.0], dtype=np.float64)
    clip = proj @ (view @ world)
    ndc = clip[:3] / clip[3]
    return (float((ndc[0] + 1.0) * 0.5 * cam.width), float((1.0 - ndc[1]) * 0.5 * cam.height))


def test_mmd_matrix_matches_project():
    """同一世界点走 4×4 view/proj 矩阵与走 project() 的偏差 < 1e-9 px
    （原版实测 2.27e-13，mmd.js:13-16 声明；artifact C §3.3 要求以 project 为权威）。

    扫描 wide/非-wide/竖画布 × pitch∈[-30,40] × roll≠0 × 多个世界点，实测最差 4.9e-13。"""
    worst = 0.0
    for w, h, feq in ((1920, 1080, 23.0), (1200, 1080, 23.0), (1080, 1920, 28.0), (900, 900, 23.0)):
        for pitch, roll in ((0.0, 0.0), (10.0, 0.0), (-20.0, 5.0), (15.0, 30.0),
                            (25.0, -10.0), (0.0, 90.0), (35.0, 20.0), (-30.0, -45.0), (40.0, 15.0)):
            cam = _cam(w, h, feq, pitch, roll)
            for x, y, z in ((0.0, 0.0, 4.0), (1.2, 0.0, 3.0), (-0.5, 1.65, 6.0),
                            (0.3, 0.8, 2.5), (2.0, 0.2, 8.0)):
                via_project = project(x, y - cam.height_m, z, cam)
                if via_project is None:
                    continue
                via_mmd = mmd_project_px(x, y, z, cam)
                worst = max(worst, math.hypot(via_project[0] - via_mmd[0],
                                              via_project[1] - via_mmd[1]))
    assert worst < 1e-9


def test_mmd_matrix_degenerates_to_pinhole():
    """pitch=0、roll=0、camH=h → viewMatrix = translate(0,-h,0)、R=I（artifact C §3.3
    退化验证）。此时矩阵管线必须与最简针孔 u = W/2 + F·X/d、v = H/2 - F·(Y-h)/d 一致。
    世界点 (1.2, 0, 3.0)、h=1.6、cam 1920×1080 feq=23：
    F = 1275.828595201775；u = 960 + F·1.2/3 = 1470.33143808071
    v = 540 - F·(0-1.6)/3 = 540 + 680.4419174409467 = 1220.4419174409468"""
    cam = _cam()
    view = _view_matrix(0.0, 0.0, cam.height_m)
    np.testing.assert_allclose(view, [[1, 0, 0, 0], [0, 1, 0, -1.6], [0, 0, 1, 0], [0, 0, 0, 1]])
    assert project(1.2, -1.6, 3.0, cam) == pytest.approx((1470.33143808071, 1220.4419174409468), abs=1e-9)
    assert mmd_project_px(1.2, 0.0, 3.0, cam) == pytest.approx((1470.33143808071, 1220.4419174409468),
                                                              abs=1e-9)


def test_camera_is_frozen():
    """Camera 是 frozen dataclass（SPEC §3.1），字段顺序 = width,height,height_m,feq_mm,
    pitch_deg,roll_deg。"""
    cam = _cam()
    with pytest.raises(Exception):
        cam.pitch_deg = 5.0  # type: ignore[misc]
