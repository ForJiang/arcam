"""matte_tools 单测 —— FEATURE-SPEC §1 门禁。

数值断言附手算过程注释（docs/SPEC.md §4.3）；对译证据绑定原版
oss-teardown repo cutout.js / index.html 的 file:line。
"""

from __future__ import annotations

import numpy as np
import pytest

from arcam.matte_tools import (
    ALPHA_MIN_01,
    CUT_RATIO_MAX,
    CUT_RATIO_MIN,
    auto_prepare_char,
    auto_trim,
    content_bounds,
    cut_solid_bg,
    is_half_body,
    soften_edge,
)


def _solid(h: int, w: int, rgb: tuple[float, float, float]) -> np.ndarray:
    """全不透明纯色图（(h,w,4) float64 01）。"""
    a = np.zeros((h, w, 4), np.float64)
    a[..., :3] = rgb
    a[..., 3] = 1.0
    return a


def test_cut_solid_bg_white_bg_red_block_hand_computed():
    """64×48 纯白底 + 16×16 红方块（不碰四角）。

    手算：背景 3072-256=2816 像素，占比 2816/3072=0.916666…；
    红块内 alpha=1；块外 alpha=0；紧贴块边的 1px 羽化带 = 3×3 窗口内 6 个
    不透明 / 9 = 0.6667（窗口跨块边：3 列×2 行在内）。
    """
    a = _solid(48, 64, (1.0, 1.0, 1.0))
    a[16:32, 24:40, 0] = 1.0
    a[16:32, 24:40, 1] = 0.0
    a[16:32, 24:40, 2] = 0.0
    ratio = cut_solid_bg(a)
    assert ratio == pytest.approx(1.0 - 256.0 / 3072.0)

    from arcam.matte_tools import _flood_solid
    out, _ = _flood_solid(a, 32.0, 1.2)
    assert out[24, 32, 3] == 1.0            # 红块中心
    assert out[0, 0, 3] == 0.0              # 白底
    assert out[16, 32, 3] == pytest.approx(6.0 / 9.0)   # 上边缘羽化带
    # bbox 含 1px 羽化带：cols 23..40（18 宽）、rows 15..32（18 高）
    assert content_bounds(out) == (23, 15, 18, 18)


def test_cut_solid_bg_rejects_non_solid():
    """四角 RGB 与均值最大偏差 > tol/255 → None（cutout.js:52-54）。"""
    d = _solid(32, 32, (0.0, 0.0, 0.0))
    d[0, 0, :3] = 1.0                        # 一个角白、其余黑：dev=1 > 32/255
    assert cut_solid_bg(d) is None


def test_cut_solid_bg_rejects_already_transparent():
    """四角 alpha 全 < 10/255 → None（cutout.js:37-40，不误报"抠掉 40%"）。"""
    t = _solid(32, 32, (1.0, 1.0, 1.0))
    for y, x in ((0, 0), (0, 31), (31, 0), (31, 31)):
        t[y, x, 3] = 0.0
    assert cut_solid_bg(t) is None


def test_cut_solid_bg_tol_is_byte_units():
    """tol 按原版字节口径（默认 32=32/255）——01 空间直接收 32 会整图放行。

    手算：32×32 图上 16×16 的 0.9 块（与 0.5 底偏差 0.4 > 3·32/255≈0.376），
    四角一致（都在底上）→ 泛洪从边框进、块前被挡，抠除 (1024-256)/1024=0.75。
    """
    a = _solid(32, 32, (0.5, 0.5, 0.5))
    a[8:24, 8:24, :3] = 0.9
    assert cut_solid_bg(a) == pytest.approx(0.75)
    from arcam.matte_tools import _flood_solid
    out, _ = _flood_solid(a, 32.0, 1.2)
    assert out[16, 16, 3] == 1.0             # 块内不透明
    assert out[0, 0, 3] == 0.0               # 底被抠


def test_content_bounds_inclusive():
    """bbox 含边界像素（w = x1-x0+1，cutout.js:263）。"""
    a = _solid(20, 30, (0.0, 0.0, 0.0))
    a[..., 3] = 0.0
    a[5:8, 10:13, 3] = 1.0                   # 3×3 块
    assert content_bounds(a) == (10, 5, 3, 3)
    a2 = a.copy()
    a2[..., 3] = 0.0
    assert content_bounds(a2) is None


def test_auto_trim_min_side_gate():
    """裁后任一边 < 4 → None（index.html:2413 的 >= 4 门限）。"""
    a = _solid(20, 30, (0.0, 0.0, 0.0))
    a[..., 3] = 0.0
    a[5:8, 10:13, 3] = 1.0                   # 3×3 → 小于 4
    assert auto_trim(a) is None
    a[5:10, 10:15, 3] = 1.0                  # 5×5 → 可以
    res = auto_trim(a)
    assert res is not None
    arr, rect = res
    assert rect == (10, 5, 5, 5)
    assert arr.shape == (5, 5, 4)


def test_soften_edge_erode_then_blur():
    """erode 1px 收轮廓 + feather 2px 柔化；3×3 全不透明 → 均值不变。"""
    a = _solid(10, 10, (0.5, 0.5, 0.5))
    out, changed = soften_edge(a, erode=1, feather=2)
    assert changed is True
    # 全 1.0 场：erode/blur 都不改变（最小值与均值都是 1）
    assert np.allclose(out[..., 3], 1.0)
    # 单个不透明像素在 5×5 零场：erode 1 → 全没（最小值被 0 拉走）
    b = _solid(10, 10, (0.5, 0.5, 0.5))
    b[..., 3] = 0.0
    b[5, 5, 3] = 1.0
    out2, _ = soften_edge(b, erode=1, feather=0)
    assert out2[5, 5, 3] == 0.0              # 被 3×3 最小值擦掉


def test_is_half_body_second_last_row_30pct():
    """半身判定三口径（index.html:2428-2434）：倒数第二行、alpha>200/255、>30%。

    手算：宽 10 的行，>200/255 的像素 4 个 → 0.4 > 0.3 → True；3 个 → 0.3
    不大于 0.3 → False（严格大于）。最后一行不计（原版取 height-2）。
    """
    a = _solid(40, 10, (0.0, 0.0, 0.0))
    a[..., 3] = 0.0                          # 先全透明，再只点亮要测的那行
    a[-2, :4, 3] = 1.0                       # 倒数第二行 4/10 实
    assert is_half_body(a) is True
    a[-2, 3, 3] = 0.0                        # 改 3/10 = 0.3，不大于
    assert is_half_body(a) is False
    a[-2, :4, 3] = 1.0
    a[-1, :, 3] = 1.0                        # 最后一行全实也不看它
    assert is_half_body(a) is True           # 仍只看 -2 行
    # 阈值口径：200/255 是"实"的分界（200 不算、201 算）
    a2 = _solid(40, 10, (0.0, 0.0, 0.0))
    a2[..., 3] = 0.0
    a2[-2, :5, 3] = 200.0 / 255.0
    assert is_half_body(a2) is False
    a2[-2, :5, 3] = 201.0 / 255.0
    assert is_half_body(a2) is True


def test_is_half_body_single_row_image():
    """h==1 时原版读 ``max(0, h-2)`` = 第 0 行（唯一那一行），不是一律 False。

    行下标照抄原版的 clamp：只有 h==0（没有行可读）才返回 False。
    """
    a = _solid(1, 10, (0.0, 0.0, 0.0))
    a[0, :, 3] = 1.0                         # 唯一一行整条不透明 → 1.0 > 0.3
    assert is_half_body(a) is True
    a[0, :6, 3] = 0.0                        # 剩 4/10 = 0.4 > 0.3
    assert is_half_body(a) is True
    a[0, :, 3] = 0.0
    assert is_half_body(a) is False


def test_lens_blur_premultiplied():
    """镜头微糊：预乘 alpha 空间往返，透明处的黑不被拉进边缘（index.html:2384-2398）。

    手算：全不透明纯色场 → 糊完还是那块纯色（插值不动常数场）；单像素不透明点
    在零场里 → 糊完半径扩大、值衰减（软边），且**不出现负值/黑渗**（预乘保证）。
    """
    from arcam.matte_tools import lens_blur
    a = _solid(40, 40, (0.8, 0.4, 0.2))
    out = lens_blur(a, 0.62)
    assert out.shape == a.shape
    assert np.allclose(out[..., :3], a[..., :3], atol=2.0 / 255.0)
    assert np.allclose(out[..., 3], 1.0, atol=2.0 / 255.0)

    b = _solid(40, 40, (0.0, 0.0, 0.0))
    b[..., 3] = 0.0
    b[20, 20, 3] = 1.0
    b[20, 20, :3] = 1.0
    out2 = lens_blur(b, 0.62)
    # 预乘口径：半透明边的 RGB 应仍接近白（不是被透明黑拉灰）。
    # 单像素经 0.62² 往返峰值约 0.14（衰减是 roundtrip 的固有行为），阈值取 0.05。
    ring = out2[17:24, 17:24]
    solidish = ring[ring[..., 3] > 0.05]
    assert solidish.size > 0
    assert float(solidish[..., :3].mean()) > 0.5
    assert float(out2[..., 3].max()) < 0.3          # 单点被摊薄，不会重新变实


def test_lens_blur_target_size_uses_js_round():
    """降/升采样的目标尺寸取整必须走 js_round（原版 Math.round 半向 +∞）。

    SPEC §1.4 红线「禁止用内置 round()」的最小复现：w=75、k=0.62 时
    ``75*0.62`` 在 float64 里**恰好**是 46.5，内置 round=46（半数取偶）、
    js_round=47。tw 差一档 → 往返插值的采样网格不同 → 实测 364/6000 像素变、
    最大差 2.16/255。这条断言把两种口径的结果钉成不相交。
    """
    from PIL import Image

    from arcam.matte_tools import lens_blur
    from arcam.numeric import js_round

    a = _solid(80, 75, (0.5, 0.5, 0.5))
    a[10:70, 5:70, :3] = 0.9
    got = lens_blur(a, 0.62)
    tw, th = max(2, js_round(75 * 0.62)), max(2, js_round(80 * 0.62))
    assert tw == 47 and th == 50                   # 内置 round 会给 46 / 50
    assert int(round(75 * 0.62)) == 46              # 确认旧口径确实分叉

    def _rs(img01: np.ndarray, size: tuple[int, int]) -> np.ndarray:
        im = Image.fromarray((np.clip(img01, 0.0, 1.0) * 255.0).round().astype(np.uint8))
        return np.asarray(im.resize(size, Image.BILINEAR), dtype=np.float64) / 255.0

    pm = a[..., :3] * a[..., 3:4]
    sp, sa = _rs(pm, (tw, th)), _rs(a[..., 3], (tw, th))
    bp, ba = _rs(sp, (75, 80)), _rs(sa, (75, 80))
    rgb = np.where(ba[..., None] > 1e-6, bp / np.maximum(ba[..., None], 1e-6), 0.0)
    np.testing.assert_allclose(got, np.dstack([rgb, ba]), atol=1e-12)
    # 旧口径（内置 round）出来的图确实不一样 —— 不然这条断言锁不住任何东西
    stale_tw = max(2, int(round(75 * 0.62)))
    assert stale_tw != tw


def test_flood_solid_feather_radius_uses_js_round():
    """柔化半径取整同样走 js_round（cutout.js:81 ``Math.round(o.feather)``）。

    feather=2.5：内置 round=2、js_round=3。半径差一档，羽化带宽度就不同 ——
    块左上角 (10,10) 的窗口里不透明像素数 9/25（r=2）vs 16/49（r=3）。
    """
    from arcam.matte_tools import _flood_solid
    from arcam.numeric import js_round

    assert js_round(2.5) == 3 and int(round(2.5)) == 2      # 两种口径确实分叉
    a = _solid(40, 40, (1.0, 1.0, 1.0))
    a[10:30, 10:30, :3] = (0.9, 0.2, 0.2)
    out, _ = _flood_solid(a, 32.0, 2.5)
    # 原版写回 Uint8Array：(s/n)|0 字节截断 → 16/49×255=83.06 → 83
    assert out[10, 10, 3] == pytest.approx(83.0 / 255.0)     # r=3 的 7×7 窗口


def test_auto_prepare_char_full_chain():
    """整链五步（cut→trim→soften→blur→half）与 actions 对手算（index.html:2400-2440）。

    40×60 黑底 + 20×20 人物块（占比 400/2400=0.1667 ∈ [0.05,0.85]）；
    裁后 = 人物 bbox ±1px 羽化带（22×22，>= 8 兜底门限过）；
    半身判定看倒数第二行：裁后底行是羽化带（alpha≈0.33·255=85 < 200）→ False。
    """
    a = _solid(60, 40, (0.0, 0.0, 0.0))
    a[10:30, 10:30, :3] = (0.9, 0.2, 0.2)
    prep = auto_prepare_char(a)
    assert prep.cut_ratio == pytest.approx(1.0 - 400.0 / 2400.0)
    assert prep.trimmed is True
    assert prep.half_body is False           # 倒数第二行是羽化带，不到 200
    assert prep.actions == ("note.autoCut", "note.autoTrim",
                            "note.autoSoft", "note.autoBlur")
    assert prep.rgba01.shape == (22, 22, 4)


def test_auto_prepare_char_ratio_gate_rejects_over_cut():
    """抠除占比 > 0.85 → 不算数（太大=把人也吃了，index.html:2411）。"""
    a = _solid(60, 40, (1.0, 1.0, 1.0))
    a[10:14, 10:14, :3] = (0.9, 0.2, 0.2)    # 只有 16 像素内容 → 抠除 99.3%
    prep = auto_prepare_char(a)
    assert prep.cut_ratio is None
    assert "note.autoCut" not in prep.actions


def test_auto_prepare_char_disabled_is_identity():
    """enabled=False → 原样返回（只报 half_body）。"""
    a = _solid(20, 20, (0.5, 0.5, 0.5))
    prep = auto_prepare_char(a, enabled=False)
    assert np.array_equal(prep.rgba01, a)
    assert prep.cut_ratio is None
    assert prep.trimmed is False
    assert prep.actions == ()


def test_auto_prepare_char_step_error_never_raises(monkeypatch):
    """任一步抛错 → 该步静默跳过（自动处理不能把装图搞挂，index.html:2427）。"""
    import arcam.matte_tools as mt

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(mt, "_flood_solid", boom)
    a = _solid(20, 20, (0.5, 0.5, 0.5))
    prep = auto_prepare_char(a)               # 不抛
    assert prep.cut_ratio is None
    assert "note.autoCut" not in prep.actions


def test_auto_prepare_char_falls_back_when_content_too_small():
    """动过手且最终内容 bbox 任一边 < 8 → 整条回退原图（index.html:2435-2439）。

    构造：已透明素材（PNG 常态）上 5×5 内容——cut 因"四角全透明"直接 None
    （不走门控），trim 把 5×5 裁出来（>= 4 过），随后兜底查 bbox 5 < 8 →
    回退。⚠ 不能用"小块+大画布"构造：抠除率会超 0.85 被 ① 的门控拒掉，
    根本走不到兜底（那是另一条已测路径）。
    """
    def make(side: int) -> np.ndarray:
        a = _solid(60, 40, (0.9, 0.2, 0.2))
        a[..., 3] = 0.0                        # 已透明素材
        a[10:10 + side, 10:10 + side, 3] = 1.0
        return a

    back = auto_prepare_char(make(5))          # 裁后 5×5 < 8 → 回退
    assert np.array_equal(back.rgba01, make(5))
    assert back.actions == () and back.cut_ratio is None and back.trimmed is False
    # 8×8 恰好等于门限（>= 8）→ 不回退
    keep = auto_prepare_char(make(8))
    assert keep.trimmed is True and keep.rgba01.shape[:2] == (8, 8)


def test_gate_constants_match_original():
    """门控常量与原版逐字一致（index.html:2411/2413、cutout.js:23/26/250）。"""
    assert CUT_RATIO_MIN == 0.05
    assert CUT_RATIO_MAX == 0.85
    assert ALPHA_MIN_01 == 8.0 / 255.0


def test_soften_feather_matches_original_kernel():
    """soften 的 feather 内核 = 原版可分离 3 抽头两遍 + 每遍 Math.round（v1.5 对译）。

    手算（5×5 零场、中心一个 255、erode=0、feather=2）：
      横遍：中心行 [0,0,255,0,0] → [0,85,85,85,0]（(0+0+255)/3=85，端部 2 样本）
      竖遍：中心列 [0,0,85,0,0] → 28（(0+85+0)/3=28.33 → Math.round=28）
      结果：rows 1-3 × cols 1-3 = 28/255，其余 0。
    ⚠ 与 5×5 盒式均值**不同核**（盒式会给 36/255 的软团）——原版
    cutout.js:805-817 是两遍 3 抽头、半径硬编码 1（feather 值只当布尔）。
    """
    a = np.zeros((5, 5, 4))
    a[..., :3] = 0.5
    a[2, 2, 3] = 1.0
    out, changed = soften_edge(a, erode=0, feather=2)
    assert changed is True
    got = np.rint(out[..., 3] * 255).astype(int)
    expect = np.zeros((5, 5), dtype=int)
    expect[1:4, 1:4] = 28
    assert np.array_equal(got, expect), f"\n{got}"
    # feather=0 → 不柔（布尔语义的关闭档）
    out0, _ = soften_edge(a, erode=0, feather=0)
    assert np.array_equal(np.rint(out0[..., 3] * 255).astype(int),
                          np.rint(a[..., 3] * 255).astype(int))


def test_soften_enters_byte_domain():
    """入口量化到字节域（原版 getImageData 口径）：0.5 的 alpha 先进 128 再算。"""
    a = np.zeros((5, 5, 4))
    a[..., :3] = 0.5
    a[..., 3] = 0.5                       # 全 128（0.5×255=127.5 → 128）
    out, _ = soften_edge(a, erode=0, feather=2)
    # 常数场均值不变；出口应是 128/255 而不是浮点 0.5 的再模糊
    assert np.allclose(out[..., 3], 128.0 / 255.0)
