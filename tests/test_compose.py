"""compose.py —— composeShot 六步（index.html:3673-3701）与立绘层路径。

端到端用**程序生成**的 320×240 背景 + 64×128 立绘，断言：
  · 出图尺寸 = ``js_round(R.w/R.h)``、各画幅宽高比、小数偏移裁切的落点；
  · 立绘锚点（中心像素）落在 ``(c.u, c.v)``；
  · 复用光 vs 当场采光 —— 相同输入下逐像素相同（原版"逐像素一致"那条承诺）；
  · 立绘层与照片同尺寸同裁切框，能严丝合缝盖回去（over 往返 ≤1 级）；
  · 照片路径 == 直接 composite_sprite 一次到底（bit-exact）；
  · ``TONE_ON = !!src``：只有照片路径做色调匹配，且立绘层路径恒不匹配；
  · 阴影在立绘之前铺、自由模式不铺阴影（通过探针断言调用参数）。

其余模块（P1 投影/落位、P2 滤波/阴影颜色）的像素语义由它们自己的测试覆盖；
这里只锁 compose 这一层的编排。
"""

from __future__ import annotations

import numpy as np
import pytest

from arcam.compose import (
    ComposedShot,
    compose_char_layer,
    compose_shot,
)
from arcam.frames import frame_rect
from arcam.geometry import Camera
from arcam.lighting import LightSample, bake_lit
from arcam.numeric import Rect, js_round
from arcam.placement import Subject, current_placement
from arcam import sprite as sprite_mod

W, H = 320, 240
CAM = Camera(W, H, 1.6, 23.0, -8.0, 3.0)
SUBJ = Subject(1.65, 4.0, -0.35)


def make_char(h: int = 128, w: int = 64) -> np.ndarray:
    """程序生成立绘：随机底色 + 一个实心方块（alpha 255）+ 右侧半透明带。"""
    rng = np.random.default_rng(42)
    char = np.zeros((h, w, 4))
    char[..., :3] = rng.random((h, w, 3))        # 底噪，确保不是纯色
    x0, y0, x1, y1 = 8, 4, 56, 124
    char[y0:y1, x0:x1, 0] = 0.85
    char[y0:y1, x0:x1, 1] = 0.30
    char[y0:y1, x0:x1, 2] = 0.30
    char[y0:y1, x0:x1, 3] = 1.0
    char[y0:y1, x1:x1 + 6, 3] = 0.25             # 半透明带：alpha 8..254
    char[y0:y1, x1:x1 + 6, :3] = 0.9
    return char


def make_bg(scale: float = 1.0) -> np.ndarray:
    """程序生成照片：横向渐变 + 纵向渐变（保证有内容、各通道不等）。"""
    gy, gx = np.mgrid[0:H, 0:W]
    bg = np.zeros((H, W, 3))
    bg[..., 0] = (0.15 + 0.4 * gx / W) * scale
    bg[..., 1] = (0.55 - 0.2 * gy / H) * scale
    bg[..., 2] = 0.35 * scale
    return bg


CHAR = make_char()
BG = make_bg()


def q(a: np.ndarray) -> np.ndarray:
    return (np.clip(np.asarray(a, dtype=np.float64), 0, 1) * 255 + 0.5).astype(np.uint8)


# ───────────────────────── 尺寸 / 画幅 / 裁切 ─────────────────────────
def test_shot_size_matches_frame_rect_for_every_frame() -> None:
    for key in ("1:1", "4:5", "16:9", "9:16", "满幅", "自由"):
        r = frame_rect(key, W, H)
        got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key=key)
        assert got.shot.shape == (max(1, js_round(r.h)), max(1, js_round(r.w)), 3), key
        assert got.shot.dtype == np.uint8
        assert got.char_layer.shape == (max(1, js_round(r.h)), max(1, js_round(r.w)), 4), key
        assert got.rect == r


def test_aspect_ratio_exact() -> None:
    """frame='1:1' 出图必须是正方（320×240 画布里取 240×240）。"""
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="1:1")
    assert got.shot.shape[0] == got.shot.shape[1] == 240
    assert got.rect.x == 40 and got.rect.y == 0
    # 16:9 → 320×180，居中是 y=30
    got2 = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="16:9")
    assert got2.shot.shape == (180, 320, 3) and got2.rect == Rect(0.0, 30.0, 320.0, 180.0)
    # 9:16 → 135×240，居中 x = round((320-135)/2)
    got3 = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="9:16")
    assert got3.shot.shape == (240, 135, 3) and got3.rect.x == js_round((320 - 135) / 2)


def test_crop_offset_preserves_photo_pixels() -> None:
    """小数偏移裁切：出图像素 == 源帧在 (R.x, R.y) 起的逐像素拷贝（无立绘时）。"""
    got = compose_shot(BG, None, SUBJ, CAM, frame_key="满幅")
    assert got.char_layer is not None and int(got.char_layer[..., 3].max()) == 0
    assert np.array_equal(got.shot, q(BG))
    # 1:1：左边 40 列被裁掉
    g1 = compose_shot(BG, None, SUBJ, CAM, frame_key="1:1")
    assert np.array_equal(g1.shot, q(BG[:, 40:280, :]))


def test_source_size_mismatch_raises() -> None:
    small = make_bg()[:200, :300, :]
    with pytest.raises(ValueError):
        compose_shot(small, CHAR, SUBJ, CAM, frame_key="满幅")


def test_bad_mode_raises() -> None:
    with pytest.raises(ValueError):
        compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅", mode="tilt")


# ───────────────────────── 锚点 ─────────────────────────
def test_sprite_centre_pixel_is_placement_centre() -> None:
    """立绘外接矩形中心就是落位中心 (c.u, c.v)：落点是实心色块的中心。"""
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    pc = current_placement(CHAR, SUBJ, CAM, mode="ground", char_scale=1.0, rot_basis="world")
    assert pc is not None
    cx, cy = int(round(pc.u)), int(round(pc.v))
    # 该点必须不再是背景色
    assert not np.allclose(got.shot[cy, cx], q(BG)[cy, cx], atol=6)
    # 且与"合成到该点"的结果一致：直接 composite 到同一画布再比
    from arcam.lighting import bake_lit
    from arcam import sprite as sp
    m = got.light
    drawn = bake_lit(CHAR, m, (js_round(pc.w), js_round(pc.h)))
    ref = q(sp.composite_sprite(BG, drawn, pc, shadow=True,
                                ambient=m.mean if m else None,
                                shadow_layer=sp.contact_shadow_layer(CHAR, pc, m.mean)))
    assert np.array_equal(got.shot, ref)


def test_sprite_lands_where_placement_says() -> None:
    """立绘覆盖率集中在 (c.u,c.v) 附近：以锚点为中心取一小块，Alpha 覆盖率高。"""
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    pc = current_placement(CHAR, SUBJ, CAM, mode="ground", char_scale=1.0, rot_basis="world")
    assert pc is not None
    lay = got.char_layer[..., 3]
    cx, cy = int(round(pc.u)), int(round(pc.v))
    box = lay[cy - 12:cy + 12, cx - 12:cx + 12]
    assert float(box.mean()) > 0.6                    # 中心几乎全被覆盖
    # 同一块在离锚点很远的地方覆盖率低
    far = lay[10:40, 10:40]
    assert float(far.mean()) < 0.05


# ───────────────────────── 采光 ─────────────────────────
def test_light_reuse_equals_fresh_measurement() -> None:
    """复用光 vs 当场采光：相同输入必须逐像素相同（原版"逐像素一致"承诺）。"""
    fresh = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="1:1")
    reused = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="1:1", light=fresh.light)
    assert fresh.light is not None
    assert np.array_equal(fresh.shot, reused.shot)
    assert np.array_equal(fresh.char_layer, reused.char_layer)


def test_char_layer_uses_the_same_light_as_the_photo() -> None:
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="1:1")
    layer, rect = compose_char_layer(CHAR, SUBJ, CAM, light=got.light, mode="ground")
    # 满幅下两者逐像素相同（compose_char_layer 固定'满幅'）
    full = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    assert np.array_equal(layer, compose_char_layer(CHAR, SUBJ, CAM, light=full.light)[0])
    assert layer.shape == (H, W, 4) and rect == Rect(0.0, 0.0, float(W), float(H))
    # 立绘层路径自己采不到光
    assert compose_shot(None, CHAR, SUBJ, CAM, frame_key="满幅").light is None


def test_dark_photo_gives_dimmer_shot() -> None:
    lit = compose_shot(make_bg(1.0), CHAR, SUBJ, CAM, frame_key="满幅")
    dark = compose_shot(make_bg(0.3), CHAR, SUBJ, CAM, frame_key="满幅")
    assert float(dark.shot.mean()) < float(lit.shot.mean())
    assert float(dark.light.mean[0]) < float(lit.light.mean[0])


def test_light_levels_is_32() -> None:
    assert len(compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅").light.levels) == 32


# ───────────────────────── 立绘层 ─────────────────────────
def test_char_layer_overlays_seamlessly_on_photo() -> None:
    """立绘层盖回照片 == 照片本身（≤1 级量化差），这就是素材包的用法。"""
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    lay = got.char_layer.astype(np.float64) / 255.0
    back = q(BG * (1 - lay[..., 3:4]) + lay[..., :3] * lay[..., 3:4])
    diff = np.abs(back.astype(int) - got.shot.astype(int))
    assert int(diff.max()) <= 1


def test_char_layer_is_not_tone_matched() -> None:
    """原版 TONE_ON = !!src：立绘层路径恒不匹配，只有照片路径匹配。"""
    plain = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅", tone=False)
    toned = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅", tone=True)
    assert not np.array_equal(plain.shot, toned.shot)          # 照片变了
    assert np.array_equal(plain.char_layer, toned.char_layer)  # 立绘层没变
    # 立绘层路径上 tone=True 也 inert
    a = compose_shot(None, CHAR, SUBJ, CAM, frame_key="满幅", tone=True)
    b = compose_shot(None, CHAR, SUBJ, CAM, frame_key="满幅", tone=False)
    assert np.array_equal(a.shot, b.shot)


def test_compose_shot_none_equals_compose_char_layer() -> None:
    a = compose_shot(None, CHAR, SUBJ, CAM, frame_key="满幅")
    b, rect = compose_char_layer(CHAR, SUBJ, CAM)
    assert np.array_equal(a.shot, b[..., :3])
    assert a.light is None and rect == Rect(0.0, 0.0, float(W), float(H))
    assert b.dtype == np.uint8


def test_compose_char_layer_free_mode() -> None:
    lay, rect = compose_char_layer(CHAR, SUBJ, CAM, mode="free", size_pct=40)
    assert lay.shape == (H, W, 4)
    assert rect == Rect(0.0, 0.0, float(W), float(H))
    assert int(lay[..., 3].max()) == 255
    # 自由模式：摆到画面中心
    cy, cx = H // 2, W // 2
    assert lay[cy, cx, 3] > 200


def test_no_char_gives_empty_shot_and_layer() -> None:
    got = compose_shot(BG, None, SUBJ, CAM, frame_key="1:1")
    assert np.array_equal(got.shot, q(BG[:, 40:280, :]))
    assert got.char_layer is not None
    assert int(got.char_layer[..., 3].max()) == 0
    assert got.light is not None                        # 仍按整帧采光（lightRegion 退化）


def test_no_placement_returns_base() -> None:
    """相机在人物正后方时 project 返回 None → 不画立绘，照片原样出。"""
    behind = Camera(W, H, 1.6, 23.0, 0.0, 0.0)
    beh_subj = Subject(1.65, -4.0, 0.0)
    got = compose_shot(BG, CHAR, beh_subj, behind, frame_key="满幅")
    assert np.array_equal(got.shot, q(BG))
    assert int(got.char_layer[..., 3].max()) == 0


# ───────────────────────── 编排细节：阴影 / 顺序 ─────────────────────────
class _Spy:
    """记录 composite_sprite / contact_shadow_layer 的调用参数。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict] = []
        self.sprite_calls: list[dict] = []
        real = sprite_mod.composite_sprite
        real_shadow = sprite_mod.contact_shadow_layer

        def spy_shadow(char, c, ambient):
            self.calls.append({"char": char is not None, "c": c, "ambient": ambient})
            return real_shadow(char, c, ambient)

        def spy_sprite(base, char, c, **kw):
            self.sprite_calls.append({"base": base is not None, "char": char, "c": c, **kw})
            return real(base, char, c, **kw)

        monkeypatch.setattr(sprite_mod, "contact_shadow_layer", spy_shadow)
        monkeypatch.setattr("arcam.compose.contact_shadow_layer", spy_shadow)
        monkeypatch.setattr("arcam.compose.composite_sprite", spy_sprite)
        self.real = real
        self.real_shadow = real_shadow


def test_shadow_is_laid_before_sprite_and_only_in_ground_mode(monkeypatch) -> None:
    spy = _Spy(monkeypatch)
    compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    assert spy.calls, "contact_shadow_layer 没被调用"
    assert spy.sprite_calls, "composite_sprite 没被调用"
    for call in spy.sprite_calls:
        assert call["shadow"] is True
        assert call["shadow_layer"] is not None          # 阴影先铺（同一次调用里先画阴影）
        assert call["char"] is not None
    # 自由模式不铺阴影
    spy2 = _Spy(monkeypatch)
    compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅", mode="free", size_pct=50)
    assert spy2.calls == []
    for call in spy2.sprite_calls:
        assert call["shadow"] is False
        assert call["shadow_layer"] is None


def test_shadow_color_uses_measured_light(monkeypatch) -> None:
    spy = _Spy(monkeypatch)
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    assert spy.calls and spy.calls[0]["ambient"] == got.light.mean


def test_bake_lit_receives_target_box() -> None:
    """bake_lit 的 canvas 参数 = 立绘落在画布上的目标尺寸（颗粒缩放补偿用）。

    照片路径与立绘层路径各烘一次，两次的目标框必须相同。
    """
    seen: list[tuple] = []
    real = bake_lit

    def spy(char, sample, canvas):
        seen.append(canvas)
        return real(char, sample, canvas)

    import arcam.compose as comp
    saved = comp.bake_lit
    comp.bake_lit = spy
    try:
        pc = current_placement(CHAR, SUBJ, CAM, mode="ground", char_scale=1.0, rot_basis="world")
        compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    finally:
        comp.bake_lit = saved
    assert seen and set(seen) == {(js_round(pc.w), js_round(pc.h))}
    assert len(seen) == 2                          # 照片一遍 + 立绘层一遍


def test_composed_shot_fields() -> None:
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="1:1")
    assert isinstance(got, ComposedShot)
    assert got.shot.ndim == 3 and got.shot.shape[2] == 3
    assert isinstance(got.rect, Rect)
    assert isinstance(got.light, LightSample)
    assert got.char_layer.ndim == 3 and got.char_layer.shape[2] == 4


def test_focal_used_in_placement_is_camera_focal() -> None:
    """换焦距必须改变落位（project 走 focal_px）。"""
    wide = Camera(W, H, 1.6, 50.0, -8.0, 3.0)
    a = compose_shot(BG, CHAR, SUBJ, wide, frame_key="满幅")
    b = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    assert not np.array_equal(a.shot, b.shot)
    pa = current_placement(CHAR, SUBJ, wide, mode="ground")
    pb = current_placement(CHAR, SUBJ, CAM, mode="ground")
    assert pa is not None and pb is not None and pa.h > pb.h
