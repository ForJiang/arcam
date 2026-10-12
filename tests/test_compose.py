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

import math

import numpy as np
import pytest

from arcam.compose import (
    ComposedShot,
    _light_region_rect,
    _placement,
    compose_char_layer,
    compose_shot,
)
from arcam.frames import frame_rect
from arcam.geometry import Camera
from arcam.lighting import LightSample, bake_lit, measure_region
from arcam.numeric import Rect, js_round
from arcam.placement import Subject, current_placement
from arcam import sprite as sprite_mod
import arcam.compose as comp

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
    # 缺省 frame_key = DEFAULT_FRAME('满幅')：与满幅照片的立绘层逐像素相同
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

        def spy_shadow(char, c, ambient, **kw):   # shadow_az/shadow_opacity 不影响本断言
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


def test_bake_lit_receives_target_box(monkeypatch) -> None:
    """bake_lit 的 canvas 参数 = 立绘落在画布上的目标尺寸（颗粒缩放补偿用）。

    照片路径与立绘层路径**共用同一份**烘光产物：bake_lit 是确定性纯函数，两遍
    _engine 构图完全相同（同一摆位、同一套光），相同入参必然相同输出——所以
    只烘一次、第二遍复用（v1.x 性能打磨，逐位相同）。"两遍目标框相同"这条不变，
    只是从"各烘一次比两次"变成"烘一次、两遍都用它"。
    """
    seen: list[tuple] = []
    real = bake_lit

    def spy(char, sample, canvas, **kw):   # grain 门（默认 True）不影响本断言
        seen.append(canvas)
        return real(char, sample, canvas, **kw)

    monkeypatch.setattr(comp, "bake_lit", spy)
    from arcam import sprite as sp
    pc = current_placement(CHAR, SUBJ, CAM, mode="ground", char_scale=1.0, rot_basis="world")
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    assert seen and set(seen) == {(js_round(pc.w), js_round(pc.h))}
    assert len(seen) == 1                          # 只烘一次，照片/立绘层两遍共用
    # 共用必须真的发生：与"每遍各烘一次"的参考出图逐像素相同
    m = got.light
    ref = q(sp.composite_sprite(BG, bake_lit(CHAR, m, (js_round(pc.w), js_round(pc.h))),
                                pc, shadow=True, ambient=m.mean,
                                shadow_layer=sp.contact_shadow_layer(CHAR, pc, m.mean)))
    assert np.array_equal(got.shot, ref)


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


# ───────────────────────── free 分支参数透传（2026-10-09 审计回归） ─────────────────────────
# 历史 bug：compose._placement 的 free 分支不传 rot_basis / rect，导致 --rot-basis
# frame（free）与 --edge-bar（free）在整条渲染链上是空操作。原版依据：charRot
# （index.html:2930）free 模式同样读 S.rotBasis；snapToEdge 需要 snapRect()。
# 对拍基准：docs/arcam-render.js placementFor 两个键都传（rotBasis + 本遍 frameRect）。


def test_free_mode_rot_basis_frame_reaches_placement() -> None:
    """free + rot_basis='frame'：经 compose 分派后 rot 必须是 0（画面竖直）。

    相机 roll=20°：'world' 应得到 rot=20°（弧度 0.349），'frame' 必须是 0。
    """
    roll_cam = Camera(W, H, 1.6, 23.0, -8.0, 20.0)
    world = _placement(CHAR, SUBJ, roll_cam, mode="free", size_pct=50.0,
                       char_scale=1.0, rot_basis="world",
                       rect=frame_rect("1:1", W, H))
    frame = _placement(CHAR, SUBJ, roll_cam, mode="free", size_pct=50.0,
                       char_scale=1.0, rot_basis="frame",
                       rect=frame_rect("1:1", W, H))
    assert world is not None and frame is not None
    assert world.rot == pytest.approx(math.radians(20.0))
    assert frame.rot == 0.0                       # 断链时这里会是 0.349（跟随 roll）


def test_free_mode_rot_basis_frame_changes_rendered_shot() -> None:
    """渲染链：free + rot_basis='frame' 与 'world' 出图必须不同（曾逐像素相同）。"""
    roll_cam = Camera(W, H, 1.6, 23.0, -8.0, 20.0)
    kw = dict(frame_key="1:1", mode="free", size_pct=40.0)
    a = compose_shot(BG, CHAR, SUBJ, roll_cam, rot_basis="frame", **kw)
    b = compose_shot(BG, CHAR, SUBJ, roll_cam, rot_basis="world", **kw)
    assert not np.array_equal(a.shot, b.shot)
    assert not np.array_equal(a.char_layer, b.char_layer)
    # 与「直接按该摆位合成」一致：frame 基准下立绘竖直（外接框不再倾斜）
    pc = current_placement(CHAR, SUBJ, roll_cam, mode="free", rot_basis="frame",
                           size_pct=40.0, rect=frame_rect("1:1", W, H))
    assert pc is not None and pc.rot == 0.0


def test_free_mode_edge_bar_snaps_in_compose_shot() -> None:
    """渲染链：free + edge_bar 必须真的吸附（曾与不吸附逐像素相同 = 空操作）。"""
    r = frame_rect("1:1", W, H)                   # 吸附目标 rect = 本遍 frameRect
    kw = dict(frame_key="1:1", mode="free", size_pct=30.0, rot_basis="world")
    no_snap = compose_shot(BG, CHAR, SUBJ, CAM, edge_bar=0, **kw)
    snapped = compose_shot(BG, CHAR, SUBJ, CAM, edge_bar=1, **kw)   # 1 = 上边
    assert not np.array_equal(no_snap.shot, snapped.shot)
    assert not np.array_equal(no_snap.char_layer, snapped.char_layer)
    # 吸附后图心落在 rect 上边：v = rect.y + bh/2（旋转后外接盒）
    p = _placement(CHAR, SUBJ, CAM, mode="free", size_pct=30.0, char_scale=1.0,
                   rot_basis="world", edge_bar=1, rect=r)
    assert p is not None
    bh = (64 / 128 * p.h) * abs(math.sin(p.rot)) + p.h * abs(math.cos(p.rot))
    assert p.v == pytest.approx(r.y + bh / 2.0)
    # 采光框跟着吸附后的立绘走（lightRegion 读的也是吸附后的落位）
    region = _light_region_rect(CHAR, SUBJ, CAM, mode="free", size_pct=30.0,
                                char_scale=1.0, rot_basis="world", edge_bar=1,
                                rect=r)
    assert region is not None and region.y >= 0.0
    assert snapped.light is not None and no_snap.light is not None
    assert snapped.light.mean != no_snap.light.mean


def test_free_mode_edge_bar_ignores_unknown_bar_numbers() -> None:
    """edge_bar=2/3/4 同样生效（下/左/右），且互不相同。"""
    r = frame_rect("1:1", W, H)
    outs = {}
    for bar in (1, 2, 3, 4):
        p = _placement(CHAR, SUBJ, CAM, mode="free", size_pct=30.0,
                       char_scale=1.0, rot_basis="world", edge_bar=bar, rect=r)
        assert p is not None
        outs[bar] = (round(p.u, 6), round(p.v, 6))
    assert len(set(outs.values())) == 4
    assert outs[1][1] < outs[2][1]                    # 上边比下边高
    assert outs[3][0] < outs[4][0]                    # 左边比右边左


def test_ground_mode_ignores_rect_and_edge_bar() -> None:
    """贴地分支不收 rect / edge_bar（groundPlacement 没有这些形参）。"""
    r = frame_rect("1:1", W, H)
    a = _placement(CHAR, SUBJ, CAM, mode="ground", size_pct=50.0, char_scale=1.0,
                   rot_basis="world", edge_bar=1, rect=r)
    b = _placement(CHAR, SUBJ, CAM, mode="ground", size_pct=50.0, char_scale=1.0,
                   rot_basis="world")
    assert a == b


def test_compose_char_layer_frame_key_passthrough() -> None:
    """compose_char_layer 收 frame_key：层与同画幅 compose_shot 的 char_layer 逐像素相同。

    历史 bug：签名里没有 frame_key、内部固定 DEFAULT_FRAME('满幅')，导致
    char-layer --frame X 静默无效、层与 shot.jpg 错位（原版 buildCharCanvas =
    composeShot(null, LAST_SHOT_LIGHT) 用的就是当前 S.frame，index.html:3709）。
    """
    for key in ("1:1", "4:5", "16:9"):
        shot = compose_shot(BG, CHAR, SUBJ, CAM, frame_key=key)
        layer, rect = compose_char_layer(CHAR, SUBJ, CAM, light=shot.light,
                                         frame_key=key)
        assert rect == frame_rect(key, W, H)
        assert layer.shape == shot.char_layer.shape
        assert np.array_equal(layer, shot.char_layer), key


def test_compose_char_layer_frame_key_default_is_full_frame() -> None:
    """缺省 frame_key = DEFAULT_FRAME('满幅')：旧调用点行为不变（红线：签名兼容）。"""
    a, rect_a = compose_char_layer(CHAR, SUBJ, CAM, light=compose_shot(
        BG, CHAR, SUBJ, CAM, frame_key="满幅").light)
    b, rect_b = compose_char_layer(CHAR, SUBJ, CAM, frame_key="满幅", light=compose_shot(
        BG, CHAR, SUBJ, CAM, frame_key="满幅").light)
    assert rect_a == rect_b == Rect(0.0, 0.0, float(W), float(H))
    assert np.array_equal(a, b)


def test_compose_char_layer_free_mode_frame_key() -> None:
    """free 模式立绘层同样按 frame_key 裁（吸附矩形也来自该画幅）。"""
    shot = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="1:1", mode="free",
                        size_pct=30.0, edge_bar=1)
    layer, rect = compose_char_layer(CHAR, SUBJ, CAM, light=shot.light,
                                     frame_key="1:1", mode="free",
                                     size_pct=30.0, edge_bar=1)
    assert rect == frame_rect("1:1", W, H)
    assert np.array_equal(layer, shot.char_layer)


# ───────────────────────── 光影三门透传（v1.3 新增门的回归） ─────────────────────────
# 历史教训：门加了但某一层没透传 = 静默失效（本项目已踩过两次）。下面四条把
# compose_shot / compose_char_layer → _engine 的 shadow / grain / sample_light
# 逐个钉死：门必须在 _engine 里真的被读到，且在三个调用点（compose_shot 的
# 照片一遍 + 立绘层一遍、compose_char_layer 一遍）真的透传。只断言"出图变了"
# 不够 —— 那一横竖也可能是别处坏了，所以每条都同时记录门本身的实参。


def _grain_spy(monkeypatch: pytest.MonkeyPatch) -> list:
    """记录每次 bake_lit 实际拿到的 grain 实参。"""
    seen: list = []
    real = comp.bake_lit

    def spy(char, sample, canvas, **kw):
        seen.append(kw.get("grain", "<缺省>"))
        return real(char, sample, canvas, **kw)

    monkeypatch.setattr(comp, "bake_lit", spy)
    return seen


def test_grain_gate_passthrough_and_effect(monkeypatch) -> None:
    seen = _grain_spy(monkeypatch)
    base = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    # 照片/立绘层两遍共用一份烘光产物（bake_lit 确定性纯函数，v1.x 性能打磨），
    # 门仍然真的透传进 bake_lit —— 只是不再透传两遍
    assert seen == [True]
    seen.clear()
    off = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅", grain=False)
    assert seen == [False]
    assert not np.array_equal(base.shot, off.shot)
    assert not np.array_equal(base.char_layer, off.char_layer)
    # compose_char_layer 那一遍也透传（层路径只有一个 _engine 调用点）
    seen.clear()
    compose_char_layer(CHAR, SUBJ, CAM, light=base.light, grain=False)
    assert seen == [False]


def test_shadow_gate_passthrough_and_effect(monkeypatch) -> None:
    spy = _Spy(monkeypatch)
    compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅", shadow=False)
    assert spy.calls == []                          # 阴影层根本不建
    assert spy.sprite_calls
    for call in spy.sprite_calls:
        assert call["shadow"] is False
        assert call["shadow_layer"] is None
    # 端到端：关掉阴影出图必须真的少一层
    base = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    off = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅", shadow=False)
    assert not np.array_equal(base.shot, off.shot)
    assert not np.array_equal(base.char_layer, off.char_layer)
    # 立绘层入口同样透传
    spy2 = _Spy(monkeypatch)
    compose_char_layer(CHAR, SUBJ, CAM, light=base.light, shadow=False)
    assert spy2.calls == []
    for call in spy2.sprite_calls:
        assert call["shadow"] is False


def test_sample_light_gate_passthrough_and_effect(monkeypatch) -> None:
    measured: list = []
    baked: list = []
    real_measure = comp.measure_region
    real_bake = comp.bake_lit

    def spy_measure(img, region, levels):
        measured.append(levels)
        return real_measure(img, region, levels)

    def spy_bake(char, sample, canvas, **kw):
        baked.append(sample is not None)
        return real_bake(char, sample, canvas, **kw)

    monkeypatch.setattr(comp, "measure_region", spy_measure)
    monkeypatch.setattr(comp, "bake_lit", spy_bake)

    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅", sample_light=False)
    assert measured == []                           # 门在读：压根不采光
    assert got.light is None
    assert baked == []                              # 没光 → 原版同分支不烘
    # 显式给的光优先于开关（原版 lightM || null 的语义）
    base = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅")
    again = compose_shot(BG, CHAR, SUBJ, CAM, frame_key="满幅",
                         light=base.light, sample_light=False)
    assert np.array_equal(again.shot, base.shot)
    assert np.array_equal(again.char_layer, base.char_layer)
    # 立绘层路径本来采不到光（opaque=False），开关在那一遍上是空操作
    lay_a, _ = compose_char_layer(CHAR, SUBJ, CAM, light=base.light)
    lay_b, _ = compose_char_layer(CHAR, SUBJ, CAM, light=base.light, sample_light=False)
    assert np.array_equal(lay_a, lay_b)


# ─────────────────────── 渲染黄金字节（v1.12.0 对抗复核补） ───────────────────────

#: compose_shot 的冻结字节基线（md5 前 16 位）。为什么需要它：v1.12.0 的性能
#: 打磨（clip 融合、原地化、双线性重写）声称「输出逐位相同」，但当时只有一次性的
#: A/B artifact 背书——没有门禁守着，将后再改渲染管线漂了字节没人知道。对抗复核
#: 的变异实测证明：单元级 golden（bakeLit 那 17 个向量）抓不住 compose 层的编排
#: 漂移（把颗粒加法换序、双线性换等价代数式，输出字节完全相同、全套测试全绿），
#: 所以这里按**整条 compose_shot** 冻一批代表性配置。
#:
#: fixture 就是本模块的 CHAR/BG/SUBJ/CAM（程序生成、无外部依赖、确定性）。
#: 改动渲染管线后若本测试红：先判断是「有意变更」还是「意外漂移」——有意变更必须
#: 连带更新 docs/tests/golden.json 的光照组与 JS 孪生，并在这里重算基线；
#: 意外漂移则回退。⚠ 只认确定性路径：别把随机/时间/哈希序混进出图输入。
_COMPOSE_GOLDEN = {
    "default":         ("0a5f08a46e9926da", "4c626e1060e79021"),
    "no_light":        ("7d5c9ec4b1521fbf", "32dbdafea11fd0e6"),
    "no_grain":        ("3131f93593e2b92f", "3521554ee7f0cd11"),
    "gs0":             ("3131f93593e2b92f", "3521554ee7f0cd11"),
    "gs25":            ("8f3efc3892e693af", "776d481580eadb81"),
    "az45":            ("1cbb5b41e42f3a83", "08a11c95afc8dabf"),
    "op05":            ("a9d760e475db98ed", "92d78ac0f8784d44"),
    "no_shadow":       ("0b86f455cca1f390", "9bda3aae536e5e43"),
    "tone":            ("8e23539b0c1a5d47", "4c626e1060e79021"),
    "free":            ("35e61c9b9ddb88a5", "35b2c7b1e202eb13"),
    "bust":            ("625b59fb59b89e81", "35ad04284f19082a"),
    "16x9":            ("a0d4861d277b346b", "7671643afe685259"),
    "char_layer_only": ("", "32dbdafea11fd0e6"),
}

_COMPOSE_GOLDEN_KW = {
    "default":         dict(frame_key="满幅"),
    "no_light":        dict(frame_key="满幅", sample_light=False),
    "no_grain":        dict(frame_key="满幅", grain=False),
    "gs0":             dict(frame_key="满幅", grain_scale=0.0),
    "gs25":            dict(frame_key="满幅", grain_scale=2.5),
    "az45":            dict(frame_key="满幅", shadow_az=45.0),
    "op05":            dict(frame_key="满幅", shadow_opacity=0.5),
    "no_shadow":       dict(frame_key="满幅", shadow=False),
    "tone":            dict(frame_key="满幅", tone=True),
    "free":            dict(frame_key="满幅", mode="free", size_pct=55.0, edge_bar=2),
    "bust":            dict(frame_key="满幅", shot="胸上"),
    "16x9":            dict(frame_key="16:9"),
    "char_layer_only": dict(frame_key="满幅"),
}


@pytest.mark.parametrize("case", sorted(_COMPOSE_GOLDEN))
def test_compose_golden_bytes(case: str) -> None:
    """整条 compose_shot / compose_char_layer 的冻结字节基线。

    覆盖 12 组开关组合 + 立绘层直出：默认、关光、关颗粒、颗粒强度 0/2.5、阴影
    方位/浓度、关阴影、色调、自由模式+贴边、非全身景别、非满幅画幅。
    任一字节漂了立刻红——这是渲染性能优化的「逐位相同」承诺的常驻门禁。
    """
    import hashlib

    want_shot, want_layer = _COMPOSE_GOLDEN[case]
    kw = _COMPOSE_GOLDEN_KW[case]
    if case == "char_layer_only":
        layer, _rect = compose_char_layer(CHAR, SUBJ, CAM, **kw)
        got_shot, got_layer = "", hashlib.md5(np.ascontiguousarray(layer)).hexdigest()[:16]
    else:
        r = compose_shot(BG, CHAR, SUBJ, CAM, **kw)
        got_shot = hashlib.md5(np.ascontiguousarray(r.shot)).hexdigest()[:16]
        got_layer = hashlib.md5(np.ascontiguousarray(r.char_layer)).hexdigest()[:16]
    assert got_shot == want_shot, \
        f"{case}: shot 字节漂移（{got_shot} != 冻结值 {want_shot}）——" \
        "渲染管线改动必须有意识地同步 golden.json 光照组与 JS 孪生"
    assert got_layer == want_layer, \
        f"{case}: char_layer 字节漂移（{got_layer} != 冻结值 {want_layer}）"
