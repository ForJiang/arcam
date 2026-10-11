"""frames.py 单测：view_rect / frame_rect（index.html:951-1039）的手工 golden。"""

from __future__ import annotations

import math
import random

from arcam import frames
from arcam.numeric import Rect, js_round, js_to_fixed


def test_view_rect_is_whole_canvas_offline():
    """离线无布局外壳：viewRect 退化为整块画布（SPEC §5 documented deviation）。"""
    assert frames.view_rect(1920, 1080) == Rect(0.0, 0.0, 1920.0, 1080.0)
    assert frames.view_rect(1080, 1920) == Rect(0.0, 0.0, 1080.0, 1920.0)
    assert frames.view_rect(0, 0) == Rect(0.0, 0.0, 0.0, 0.0)


def test_frame_rect_full_bleed_takes_whole_canvas():
    """'满幅'(-1)：整块画布，不先缩进可见区（index.html:1031）。"""
    assert frames.frame_rect("满幅", 1920, 1080) == Rect(0.0, 0.0, 1920.0, 1080.0)
    assert frames.frame_rect("满幅", 640, 480) == Rect(0.0, 0.0, 640.0, 480.0)


def test_frame_rect_free_equals_view_rect():
    """'自由'(0)：ar = FRAMES[key] || 0 → 0 → 直接返回 viewRect（index.html:1034-1035）。"""
    for w, h in ((1920, 1080), (1080, 1920), (800, 600)):
        vr = frames.view_rect(w, h)
        assert frames.frame_rect("自由", w, h) == vr


def test_frame_rect_unknown_key_falls_back_to_free():
    """JS 里 FRAMES[未知名] = undefined，``undefined || 0`` → 0 → viewRect。"""
    assert frames.frame_rect("不存在的画幅", 1920, 1080) == frames.view_rect(1920, 1080)
    assert frames.frame_rect("", 1920, 1080) == frames.view_rect(1920, 1080)


def test_frame_rect_1_1_on_landscape_canvas():
    """1:1 在 1920×1080：w=1920 → h=1920 > 1080 → 改钳 h=1080, w=1080，x=(1920-1080)/2=420。"""
    # 手推：V=(0,0,1920,1080), ar=1
    #   w=V.w=1920, h=w/ar=1920；h>V.h → h=V.h=1080, w=h*ar=1080
    #   x=round(V.x+(V.w-w)/2)=round(0+420)=420, y=round(0+(1080-1080)/2)=0
    assert frames.frame_rect("1:1", 1920, 1080) == Rect(420.0, 0.0, 1080.0, 1080.0)


def test_frame_rect_16_9_is_full_landscape():
    """16:9 在 1920×1080：w=1920, h=1920/(16/9)=1080 恰好等于 V.h → 整块。"""
    # 手推：h = 1920/（16/9）= 1080，不触发 h>V.h 分支；x=y=0
    assert frames.frame_rect("16:9", 1920, 1080) == Rect(0.0, 0.0, 1920.0, 1080.0)


def test_frame_rect_9_16_on_landscape_canvas():
    """9:16 在 1920×1080：先 w=1920 → h=3413.33>1080 → 钳 h=1080, w=607.5，x=round(656.25)=656。"""
    # 手推：ar = 9/16 = 0.5625
    #   w=1920, h=1920/0.5625=3413.33… > 1080 → h=1080, w=1080*0.5625=607.5
    #   x=round(0+(1920-607.5)/2)=round(656.25)=656（js_round 半向 +∞）
    r = frames.frame_rect("9:16", 1920, 1080)
    assert r == Rect(656.0, 0.0, 607.5, 1080.0)


def test_frame_rect_4_5_portrait_canvas_centers_vertically():
    """4:5 在 1080×1920：w=1080, h=1350 ≤ 1920 → x=0, y=round((1920-1350)/2)=285。"""
    # 手推：ar = 4:5 = 0.8；w=V.w=1080, h=1080/0.8=1350；h <= V.h=1920 不钳
    #   x=round(0+(1080-1080)/2)=0, y=round(0+(1920-1350)/2)=round(285)=285
    assert frames.frame_rect("4:5", 1080, 1920) == Rect(0.0, 285.0, 1080.0, 1350.0)


def test_frame_rect_4_5_landscape_clamps_height():
    """4:5 在 1920×1080：w=1920 → h=2400>1080 → h=1080, w=864, x=round(528)=528。"""
    # 手推：w=1920, h=2400 > 1080 → h=1080, w=1080*0.8=864
    #   x=round((1920-864)/2)=round(528)=528, y=0
    assert frames.frame_rect("4:5", 1920, 1080) == Rect(528.0, 0.0, 864.0, 1080.0)


def test_frame_rect_origin_uses_js_round_half_up():
    """原点取整必须是 js_round（半向 +∞）：Python 内置 round(0.5)=0 会算出 x=0。"""
    # 手推：100×101、1:1 → w=100, h=100 ≤ 101；y=round((101-100)/2)=round(0.5)=1
    #   JS Math.round(0.5)=1；内置 round(0.5)=0（银行家舍入）——本测试专抓这个差
    assert frames.frame_rect("1:1", 100, 101) == Rect(0.0, 1.0, 100.0, 100.0)


def test_frame_rect_origin_negative_half_rounds_toward_plus_inf():
    """js_round(-0.5) = -0（半向 +∞）：127×126、1:1 → y=round(-0.5)=0 而不是 -1。"""
    # 手推：w=126, h=126 > ... V=(0,0,127,126)：w=127, h=127 > 126 → h=126, w=126
    #   y = round(0 + (126-126)/2) = 0；换 126×127：w=126, h=126 ≤ 127 → y=round(0.5)=1
    #   这里用 125×124：w=124, h=124；y=round((124-124)/2)=0；x=round((125-124)/2)=round(0.5)=1
    assert frames.frame_rect("1:1", 125, 124) == Rect(1.0, 0.0, 124.0, 124.0)


def test_frame_rect_stays_inside_view_rect():
    """所有画幅的最大内接矩形必须整块落在 viewRect 内（框边就是看得见的那条线）。"""
    for key in frames.FRAMES:
        for w, h in ((1920, 1080), (1080, 1920), (800, 800), (333, 777)):
            r = frames.frame_rect(key, w, h)
            assert r.x >= 0.0 and r.y >= 0.0, (key, w, h, r)
            assert r.x2 <= w + 1e-9 and r.y2 <= h + 1e-9, (key, w, h, r)


def test_frame_rect_keeps_exact_aspect_ratio():
    """宽高保持浮点（画幅比例不能因为取整就歪）：w/h == ar。"""
    for key, ar in frames.FRAMES.items():
        if ar == -1.0 or ar == 0.0:
            continue
        r = frames.frame_rect(key, 1920, 1080)
        assert abs(r.w / r.h - ar) < 1e-12, (key, r)


def test_frames_table_matches_spec():
    """FRAMES 画幅表与 SPEC §3.2 逐字一致（含 '满幅': -1.0）。"""
    assert frames.FRAMES == {
        "自由": 0.0, "1:1": 1.0, "4:5": 0.8, "16:9": 16 / 9, "9:16": 9 / 16, "满幅": -1.0,
    }


# ───────────────── js_round（numeric.py，frame_rect 原点的取整入口） ─────────────────
# C1 分区没有 test_numeric.py；js_round 的用户可见效果集中在这里（原点取整），
# 边界语义就近钉住。数值口径本体由 test_golden_recompute 的 fixture 反向兜底。

def test_js_round_matches_ecma_math_round():
    """ECMA-262 Math.round：最近整数、平分向 +∞（负半分也向 +∞ 进）。

    这条口径被 frame_rect 原点、scene crop/image 尺寸全部继承，Python 内置 round()
    是银行家舍入（round(0.5)=0、round(2.5)=2），一律不许用。"""
    for value, want in ((-2.5, -2), (-1.5, -1), (-0.5, 0), (-0.0, 0), (0.0, 0),
                        (0.5, 1), (1.5, 2), (2.5, 3), (3.5, 4), (99.5, 100),
                        (-99.5, -99), (100.49, 100), (100.51, 101)):
        assert js_round(value) == want, value


def test_js_round_trap_value_below_half():
    """0.5 的下一个可表示 double 必须舍到 0，不是 1。

    教科书快捷式 ``math.floor(x + 0.5)`` 在这里会炸：``0.49999999999999994 + 0.5``
    向上溢到 1.0 → 得 1；ECMA-262 规定「大于 0 且小于 0.5 的 x 结果为 +0」，JS 给 0。
    （300 万随机样本 + 全部「整数±0.5」边界扫描：这是两式唯一的分歧输入；再大一档
     ulp 变粗，2.0+0.49999999999999994 直接被吸收成 2.5，仍是平分进 1 得 3。）"""
    assert js_round(0.49999999999999994) == 0
    assert js_round(2.0 + 0.49999999999999994) == 3   # = 2.5，平分向 +∞


# ───────────────── js_to_fixed（numeric.py，scene.json 的取整入口） ─────────────────
# toFixed 只被 scene.py / tone.py 消费（每枪一次，不在逐像素热路径），边界语义就近钉住。
# 判据：node 实跑 ``(x).toFixed(n)`` 的数字值，与 Python 侧逐值比。

def test_js_to_fixed_negative_half_matches_ecma():
    """负半分：ECMA-262 先取 ``x = -x`` 再对幅值找最近的 n —— 半**远离零**，
    与 ``ROUND_HALF_UP`` 同向。这一条曾在本模块 docstring 里被误记成「已冻结偏离①」，
    按 node 实测更正（``(-0.125).toFixed(2) === "-0.13"``、``(-0.135).toFixed(2) === "-0.14"``）。"""
    for value, digits, want in ((-0.125, 2, -0.13), (-0.135, 2, -0.14), (-2.5, 0, -3.0),
                                (-0.5, 0, -1.0), (-0.001, 2, -0.0), (0.125, 2, 0.13),
                                (2.5, 0, 3.0), (-99.5, 0, -100.0)):
        got = js_to_fixed(value, digits)
        assert got == want and math.copysign(1.0, got) == math.copysign(1.0, want), (value, digits, got)


def test_js_to_fixed_repr_shortest_string_is_a_frozen_deviation():
    """已冻结偏离（红线：toFixed 口径不许动，SPEC §1.4）：按 ``repr`` 最短串取整，
    不按 double 的精确二进制值。

    ``2.675`` 的二进制实为 2.674999…，规范该给 ``"2.67"``，这里给 2.68。
    受影响字段：2 位的 centerPixel/sizePixel/focalPx、3 位的
    pitchDeg/rollDeg/rotationDeg（实测 ``js_to_fixed(0.1235,3)=0.124`` vs JS 0.123）。
    这条测试是**钉住偏离本身**，谁「顺手修」它就会红。"""
    assert js_to_fixed(2.675, 2) == 2.68          # JS: 2.67
    assert js_to_fixed(-2.675, 2) == -2.68        # JS: -2.67
    assert js_to_fixed(1.005, 2) == 1.01          # JS: 1.00
    assert js_to_fixed(-0.15, 1) == -0.2          # JS: -0.1
    assert js_to_fixed(0.1235, 3) == 0.124        # JS: 0.123
    # 非平分区间与 JS 完全一致（这些是 scene.json 的常见值）
    for value, digits, want in ((0.0625, 3, 0.063), (0.0005, 3, 0.001), (1e-07, 3, 0.0),
                                (8.675, 2, 8.68), (12.345, 2, 12.35), (-0.7875, 2, -0.79)):
        assert js_to_fixed(value, digits) == want, (value, digits)


def test_js_to_fixed_non_finite_passthrough():
    """非有限入参原样返回：JS 的 ``(NaN).toFixed(n)`` 得字符串 ``"NaN"``、
    ``+"NaN"`` 回解析成 NaN（``Infinity`` 同理）；``Decimal('NaN').quantize``
    会抛 ``InvalidOperation``（scene 的 pitchDeg/rotationDeg 上游可能是非有限角）。"""
    assert math.isnan(js_to_fixed(float('nan'), 2))
    assert js_to_fixed(float('inf'), 2) == float('inf')
    assert js_to_fixed(float('-inf'), 3) == float('-inf')
    assert js_to_fixed(1e21, 2) == 1e21           # >= 1e21 时 JS 走 toString，数值相同


# ───────────────── clampn（numeric.py，光照/摆位的钳位入口） ─────────────────

def test_clampn_matches_original_math_min_max_for_sorted_bounds():
    """原版逐字是 ``Math.min(b, Math.max(a, x))``（index.html:1161）。

    两端有序（本项目全部调用点都是）时与比较链逐值相同 —— 这条把等价性钉住，
    免得有人「优化」成另一套口径。"""
    from arcam.numeric import clampn
    rng = random.Random(20261011)
    for _ in range(20000):
        lo = rng.uniform(-50.0, 0.0)
        hi = rng.uniform(0.0, 50.0)          # lo <= hi：有序
        x = rng.uniform(-100.0, 100.0)
        want = min(hi, max(lo, x))           # = 原版 Math.min(b, Math.max(a, x))
        assert clampn(x, lo, hi) == want, (x, lo, hi)
    # NaN 两侧都透传（Math.min/max 与比较链同为 NaN）
    assert math.isnan(clampn(float('nan'), 0.0, 1.0))


def test_clampn_unsorted_bounds_diverges_from_original_is_unreachable():
    """两端无序（a > b）时与原版不同：原版退化成常数 b，本式在 x < a 时给 a。

    当前调用点全部有序（auto_fix_x ±20、free 分支 [0,1]、光照 LIGHT_GAIN_MIN/MAX …），
    该分歧不可达 —— 这条测试是**文档化**这个边界，不是鼓励使用它。"""
    from arcam.numeric import clampn
    assert clampn(0.0, 6.0, 0.5) == 6.0          # 本式：x < a → a
    assert min(0.5, max(6.0, 0.0)) == 0.5        # 原版：恒为 b
    assert clampn(10.0, 6.0, 0.5) == 0.5         # x > b 时两式一致
