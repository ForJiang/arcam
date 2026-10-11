"""scene.py —— scene.json 配方（buildScene，index.html:3729-3789）。

三条主线：
  · **字段顺序**逐项断言（``list(d)`` 与嵌套 dict 都查）—— 与原版 JSON 同序；
  · **footPixel / headPixel** 用 SPEC §3.1 的等价公式**独立手算**两组，验算不调
    ``arcam.project``（避免"用被测码验被测码"）；
  · **取整语义**逐字段验：``js_round``（image/crop）、``js_to_fixed(n)``
    （focalPx 2 位 / pitchDeg·rollDeg·rotationDeg 3 位 / centerPixel·sizePixel·distanceM
    ·lateralM 2~3 位）、裸浮点（heightM/focal35mm）、null 分支。
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from arcam.geometry import Camera
from arcam.geometry import K_LONG, K_SHORT
from arcam.numeric import Rect, js_round, js_to_fixed
from arcam.placement import SpritePlacement, Subject
from arcam.scene import (
    SCENE_IMAGE_FILE,
    build_scene,
    load_scene,
    save_scene,
)

# ───────────── 独立参考实现（SPEC §3.1 等价公式，不 import arcam.project） ─────────────
def ref_focal(w: int, h: int, feq: float) -> float:
    """index.html:1043-1047。L=max(W,H)、Sd=min(W,H)；L/Sd>=4/3 走长边。"""
    L, Sd = max(w, h), min(w, h)
    return (L / 2) * feq / K_LONG if (L / Sd) >= 4 / 3 else (Sd / 2) * feq / K_SHORT


def ref_project(x: float, y: float, d: float, cam: Camera):
    """index.html:1048-1057。返回 (u,v) 或 None（Zc<=0.05）。"""
    p = cam.pitch_deg * math.pi / 180.0
    r = cam.roll_deg * math.pi / 180.0
    zc = -y * math.sin(p) + d * math.cos(p)
    yc = -y * math.cos(p) - d * math.sin(p)
    if zc <= 0.05:
        return None
    xr = x * math.cos(r) - yc * math.sin(r)
    yr = x * math.sin(r) + yc * math.cos(r)
    f = ref_focal(cam.width, cam.height, cam.feq_mm)
    return (cam.width / 2.0 + f * xr / zc, cam.height / 2.0 + f * yr / zc)


# 用例一：宽画布（wide 分支）、负俯角、正滚转
CAM_A = Camera(320, 240, 1.6, 23.0, -8.0, 3.0)
SUBJ_A = Subject(1.65, 4.0, -0.35)
RECT_A = Rect(40.0, 0.0, 240.0, 240.0)
PLACE_A = SpritePlacement(u=136.79661073267874, v=193.36845123657918,
                          w=47.30716781076159, h=94.61433562152318,
                          rot=0.06453692198575127)

# 用例二：高画布（非 wide 分支）、正俯角、负滚转、正值横向
CAM_B = Camera(540, 960, 1.45, 35.0, 12.5, -6.25)
SUBJ_B = Subject(1.72, 2.75, 0.62)
RECT_B = Rect(0.0, 174.0, 540.0, 960.0)
PLACE_B = SpritePlacement(u=300.5, v=520.25, w=210.125, h=380.75, rot=-0.0872664626)


def scene_a(**kw):
    return build_scene(RECT_A, PLACE_A, SUBJ_A, CAM_A, frame_key="1:1",
                       char_rgba01=np.zeros((128, 64, 4)), char_name="her.png",
                       mode="ground", **kw)


def scene_b(**kw):
    return build_scene(RECT_B, PLACE_B, SUBJ_B, CAM_B, frame_key="9:16",
                       char_rgba01=np.zeros((96, 48, 4)), char_name="b.png",
                       mode="free", **kw)


# ───────────────────────── 字段顺序 ─────────────────────────
def test_top_level_field_order_is_identical_to_original() -> None:
    d = scene_a()
    assert list(d) == ["version", "image", "frame", "camera", "subject", "ground",
                       "shot", "charScale", "edge", "character", "mmd"]
    assert list(d["image"]) == ["width", "height", "file"]
    assert list(d["frame"]) == ["aspect", "crop", "source"]
    assert list(d["frame"]["source"]) == ["width", "height"]
    assert list(d["camera"]) == ["focalPx", "focal35mm", "pitchDeg", "rollDeg", "heightM"]
    assert list(d["subject"]) == ["heightM", "distanceM", "lateralM",
                                  "footPixel", "headPixel"]
    assert list(d["ground"]) == ["y", "normal"]
    assert list(d["edge"]) == ["bar", "rotationLocked"]
    assert list(d["character"]) == ["name", "mode", "image", "centerPixel",
                                    "sizePixel", "rotationDeg"]
    assert list(d["character"]["image"]) == ["width", "height"]
    assert list(d["subject"]["footPixel"]) == ["u", "v"]
    assert list(d["subject"]["headPixel"]) == ["u", "v"]


def test_version_image_and_mmd() -> None:
    d = scene_a()
    assert d["version"] == 1
    assert d["image"] == {"width": 240, "height": 240, "file": SCENE_IMAGE_FILE}
    assert d["image"]["file"] == "shot.jpg"
    assert d["mmd"] is None                       # 离线没有 3D 运行时，恒 null


def test_ground_segment() -> None:
    d = scene_a()
    assert d["ground"] == {"y": 0, "normal": [0, 1, 0]}


# ───────────────────────── footPixel / headPixel 手算 ─────────────────────────
def test_foot_and_head_pixel_hand_computed_group_a() -> None:
    """用例一：宽画布 wide 分支。

    focal：L=320、Sd=240，320/240 = 1.3333 < 4/3 → 非 wide，
      F = (240/2)·23/12.9798 = 2760/12.9798
    project(-0.35, -1.6, 4.0)，pitch=-8°、roll=3°：
      Zc = -y·sin p + d·cos p = -(-1.6)·(-0.1391731) + 4·0.9902681 = 3.7383953
      Yc = -y·cos p - d·sin p = 1.6·0.9902681 - 4·(-0.1391731) = 2.1411213
      Xr = x·cos r - Yc·sin r ；Yr = x·sin r + Yc·cos r
      u = 160 + F·Xr/Zc ; v = 120 + F·Yr/Zc
    """
    d = scene_a()
    p, r = math.radians(-8.0), math.radians(3.0)
    f = (240 / 2) * 23.0 / K_SHORT
    zc = -(-1.6) * math.sin(p) + 4.0 * math.cos(p)
    yc = -(-1.6) * math.cos(p) - 4.0 * math.sin(p)
    xr = -0.35 * math.cos(r) - yc * math.sin(r)
    yr = -0.35 * math.sin(r) + yc * math.cos(r)
    foot = (160.0 + f * xr / zc, 120.0 + f * yr / zc)
    assert d["subject"]["footPixel"] == {"u": pytest.approx(foot[0], abs=1e-12),
                                        "v": pytest.approx(foot[1], abs=1e-12)}
    # 头部：y = 1.65 - 1.6 = 0.05
    zc2 = -0.05 * math.sin(p) + 4.0 * math.cos(p)
    yc2 = -0.05 * math.cos(p) - 4.0 * math.sin(p)
    xr2 = -0.35 * math.cos(r) - yc2 * math.sin(r)
    yr2 = -0.35 * math.sin(r) + yc2 * math.cos(r)
    head = (160.0 + f * xr2 / zc2, 120.0 + f * yr2 / zc2)
    assert d["subject"]["headPixel"] == {"u": pytest.approx(head[0], abs=1e-12),
                                        "v": pytest.approx(head[1], abs=1e-12)}
    # 独立参考实现复核
    assert d["subject"]["footPixel"] == {"u": ref_project(-0.35, -1.6, 4.0, CAM_A)[0],
                                        "v": ref_project(-0.35, -1.6, 4.0, CAM_A)[1]}
    assert d["subject"]["headPixel"]["v"] == pytest.approx(
        ref_project(-0.35, 0.05, 4.0, CAM_A)[1], abs=1e-12)


def test_foot_and_head_pixel_hand_computed_group_b() -> None:
    """用例二：竖画布非 wide 分支、正俯角、负滚转。

    focal：L=960、Sd=540，960/540 = 1.7778 >= 4/3 → wide，
      F = (960/2)·35/17.3064 = 480·35/17.3064
    project(0.62, -1.45, 2.75)，pitch=12.5°、roll=-6.25°。
    """
    d = build_scene(RECT_B, PLACE_B, SUBJ_B, CAM_B, frame_key="9:16",
                    char_rgba01=np.zeros((96, 48, 4)), char_name="b.png",
                    mode="free")
    p, r = math.radians(12.5), math.radians(-6.25)
    f = (960 / 2) * 35.0 / K_LONG
    for (name, y) in (("footPixel", -1.45), ("headPixel", 1.72 - 1.45)):
        zc = -y * math.sin(p) + 2.75 * math.cos(p)
        yc = -y * math.cos(p) - 2.75 * math.sin(p)
        xr = 0.62 * math.cos(r) - yc * math.sin(r)
        yr = 0.62 * math.sin(r) + yc * math.cos(r)
        want = (270.0 + f * xr / zc, 480.0 + f * yr / zc)
        assert d["subject"][name] == {"u": pytest.approx(want[0], abs=1e-12),
                                     "v": pytest.approx(want[1], abs=1e-12)}, name


def test_project_null_becomes_json_null() -> None:
    """相机身后（Zc<=0.05）→ null，下游别当成 0。"""
    back = Camera(320, 240, 1.6, 23.0, 0.0, 0.0)
    d = build_scene(RECT_A, None, Subject(1.65, -4.0, 0.0), back, frame_key="满幅",
                    char_rgba01=None, char_name="", mode="ground")
    assert d["subject"]["footPixel"] is None
    assert d["subject"]["headPixel"] is None
    assert d["character"] is None


def test_project_null_for_non_finite_angles_is_json_null() -> None:
    """非有限 pitch/roll → project 收口 None → footPixel/headPixel 落 null。

    原版 JS 的 project() 在非有限角下得 {u:NaN,v:NaN}，``JSON.stringify`` 把 NaN
    写成 null；Python 的 math.sin/cos 对 ±inf 会抛 ValueError，已按「不抛」策略收口
    （geometry.py:80-88）。这条同时钉住 json.dumps 不吐非标准 ``NaN`` 字面量。"""
    cam = Camera(320, 240, 1.6, 23.0, float("nan"), float("inf"))
    d = build_scene(RECT_A, None, SUBJ_A, cam, frame_key="满幅",
                    char_rgba01=None, char_name="", mode="ground")
    assert d["subject"]["footPixel"] is None
    assert d["subject"]["headPixel"] is None
    assert d["character"] is None
    # 有限角对照：footPixel 是真 {u,v}
    ok = build_scene(RECT_A, None, SUBJ_A, CAM_A, frame_key="满幅",
                     char_rgba01=None, char_name="", mode="ground")
    assert set(ok["subject"]["footPixel"]) == {"u", "v"}


def test_rect_public_surface_is_x_y_w_h_x2_y2() -> None:
    """Rect 的对外面：x/y/w/h + x2/y2 两个派生边界。

    cx/cy（中心点）已删 —— 全仓库（src / tests / docs / webui，含 .py/.js/.mjs/.md/
    .html/.json）零引用，属死代码。中心一律由调用方写 ``R.x + R.w/2``（原版 JS 同）。"""
    r = Rect(10.0, 20.0, 100.0, 50.0)
    assert (r.x, r.y, r.w, r.h) == (10.0, 20.0, 100.0, 50.0)
    assert (r.x2, r.y2) == (110.0, 70.0)
    assert not hasattr(r, "cx") and not hasattr(r, "cy")
    assert set(Rect.__dataclass_fields__) == {"x", "y", "w", "h"}


# ───────────────────────── character 分支 ─────────────────────────
def test_character_none_when_no_sprite() -> None:
    d = build_scene(RECT_A, None, SUBJ_A, CAM_A, frame_key="1:1",
                    char_rgba01=None, char_name="x.png", mode="ground")
    assert d["character"] is None


def test_character_none_when_placement_none() -> None:
    d = build_scene(RECT_A, None, SUBJ_A, CAM_A, frame_key="1:1",
                    char_rgba01=np.zeros((128, 64, 4)), char_name="x.png",
                    mode="ground")
    assert d["character"] is None            # 原版要求 pc 和 chCanvas 都在


def test_character_fields() -> None:
    d = scene_a()
    ch = d["character"]
    assert ch is not None
    assert ch["name"] == "her.png"
    assert ch["mode"] == "ground"
    assert ch["image"] == {"width": 64, "height": 128}
    assert ch["centerPixel"] == [js_to_fixed(136.79661073267874, 2),
                                 js_to_fixed(193.36845123657918, 2)]
    assert ch["sizePixel"] == [js_to_fixed(47.30716781076159, 2),
                               js_to_fixed(94.61433562152318, 2)]
    # rotationDeg = rot·180/π toFixed(3)
    assert ch["rotationDeg"] == js_to_fixed(0.06453692198575127 * 180.0 / math.pi, 3)
    assert ch["rotationDeg"] == 3.698


# ───────────────────────── 取整语义 ─────────────────────────
def test_focal_px_is_to_fixed_two_decimals_half_up() -> None:
    """focalPx 两位小数、round-half-up。"""
    assert scene_a()["camera"]["focalPx"] == js_to_fixed(ref_focal(320, 240, 23.0), 2)
    assert scene_b()["camera"]["focalPx"] == js_to_fixed(ref_focal(540, 960, 35.0), 2)
    # 半向上取：0.125 → 0.13（不是银行家的 0.12）
    assert js_to_fixed(0.125, 2) == 0.13
    assert js_to_fixed(2.675, 2) == 2.68


def test_pitch_roll_three_decimals() -> None:
    d = scene_a()
    assert d["camera"]["pitchDeg"] == js_to_fixed(-8.0, 3) == -8.0
    assert d["camera"]["rollDeg"] == js_to_fixed(3.0, 3) == 3.0
    b = scene_b()
    assert b["camera"]["pitchDeg"] == 12.5
    assert b["camera"]["rollDeg"] == -6.25
    # 位置本身是浮点，toFixed 只影响小数位
    assert b["camera"]["focal35mm"] == 35.0 and b["camera"]["heightM"] == 1.45


def test_image_and_crop_use_js_round() -> None:
    r = Rect(3.6, 7.5, 100.49, 99.51)
    d = build_scene(r, None, SUBJ_A, CAM_A, frame_key="满幅",
                    char_rgba01=None, char_name="", mode="ground")
    assert d["image"]["width"] == 100 and d["image"]["height"] == 100
    assert d["frame"]["crop"] == [js_round(3.6), js_round(7.5), js_round(100.49),
                                  js_round(99.51)]
    assert d["frame"]["crop"] == [4, 8, 100, 100]
    assert d["frame"]["aspect"] == "满幅"
    assert d["frame"]["source"] == {"width": 320, "height": 240}


def test_distance_and_lateral_three_decimals() -> None:
    d = build_scene(RECT_A, None, Subject(1.65, 4.0004, -0.3496), CAM_A,
                    frame_key="满幅", char_rgba01=None, char_name="", mode="ground")
    assert d["subject"]["distanceM"] == js_to_fixed(4.0004, 3) == 4.0
    assert d["subject"]["lateralM"] == js_to_fixed(-0.3496, 3) == -0.35
    assert d["subject"]["heightM"] == 1.65                 # 裸值


def test_shot_char_scale_and_edge() -> None:
    d = scene_a(shot="腰上", char_scale=1.5, edge_bar=3)
    assert d["shot"] == "腰上"
    assert d["charScale"] == 1.5
    assert d["edge"] == {"bar": 3, "rotationLocked": True}
    d0 = scene_a(edge_bar=0)
    assert d0["edge"] == {"bar": 0, "rotationLocked": False}
    assert d0["shot"] == "全身" and d0["charScale"] == 1.0


# ───────────────────────── 落盘 / 读回 ─────────────────────────
def test_save_and_load_roundtrip(tmp_path) -> None:
    d = scene_a(shot="胸上", char_scale=0.8, edge_bar=2)
    p = tmp_path / "scene.json"
    save_scene(d, p)
    text = p.read_text(encoding="utf-8")
    # indent=2 + ensure_ascii=False（中文画幅原样落盘）
    assert '\n  "version"' in text and '"aspect": "1:1"' in text
    assert "\\u" not in text
    assert load_scene(p) == d
    # JSON 解析回来字段顺序不变
    assert list(json.loads(text)) == list(d)
    assert json.loads(text)["character"]["centerPixel"] == d["character"]["centerPixel"]


def test_save_scene_non_finite_floats_become_json_null(tmp_path) -> None:
    """非有限浮点落 ``null`` —— ``JSON.stringify`` 的既定行为，Python 默认吐
    ``NaN``/``Infinity`` 字面量（不合法 JSON，原版工具读不回）。有限值逐字节不动。"""
    from arcam.scene import _json_safe
    d = scene_a()
    d["camera"]["pitchDeg"] = float("nan")
    d["subject"]["footPixel"] = {"u": float("inf"), "v": float("nan")}
    d["character"]["centerPixel"] = [float("nan"), 12.5]
    safe = _json_safe(d)
    assert safe["camera"]["pitchDeg"] is None
    assert safe["subject"]["footPixel"] == {"u": None, "v": None}
    assert safe["character"]["centerPixel"] == [None, 12.5]
    # 有限值一个字节都不动（键序也保持）
    assert [k for k in safe] == [k for k in d]
    assert safe["image"] == d["image"] and safe["ground"] == d["ground"]
    assert safe["character"]["sizePixel"] == d["character"]["sizePixel"]
    # 落盘文本里不出现 NaN / Infinity 字面量
    p = tmp_path / "nan.json"
    save_scene(d, p)
    text = p.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    assert '"pitchDeg": null' in text and '"u": null' in text
    # 读得回来（json.load 不炸）
    assert load_scene(p)["camera"]["pitchDeg"] is None


def test_save_scene_finite_roundtrip_is_byte_identical(tmp_path) -> None:
    """有限配方的落盘文本与 json.dumps 原样一致：_json_safe 对有限值是恒等变换。"""
    d = scene_a()
    p = tmp_path / "ok.json"
    save_scene(d, p)
    assert p.read_text(encoding="utf-8") == json.dumps(d, indent=2, ensure_ascii=False)


def test_save_scene_chinese_keys_are_not_escaped(tmp_path) -> None:
    d = scene_a(shot="头肩")
    p = tmp_path / "s2.json"
    save_scene(d, p)
    assert "头肩" in p.read_text(encoding="utf-8")


def test_save_scene_custom_indent(tmp_path) -> None:
    d = scene_a()
    p4 = tmp_path / "i4.json"
    save_scene(d, p4, indent=4)
    assert '\n    "version"' in p4.read_text(encoding="utf-8")


def test_load_scene_rejects_bad_version(tmp_path) -> None:
    good = scene_a()
    p = tmp_path / "v.json"
    save_scene(good, p)
    bad = dict(good)
    bad["version"] = 2
    save_scene(bad, p)
    with pytest.raises(ValueError):
        load_scene(p)
    save_scene({"x": 1}, tmp_path / "o.json")     # 有键但 version 不是 1
    save_scene([1, 2, 3], tmp_path / "a.json")    # 顶层不是 object
    for f in ("o.json", "a.json"):
        with pytest.raises(ValueError):
            load_scene(tmp_path / f)


def test_scene_is_json_serialisable() -> None:
    d = scene_a()
    assert json.loads(json.dumps(d, ensure_ascii=False)) == d


# ───────────────────────── 真实链路：compose_shot → build_scene ─────────────────────────
def _sprite_and_photo():
    """程序生成的 320×240 照片 + 64×128 立绘。"""
    rng = np.random.default_rng(7)
    gy, gx = np.mgrid[0:240, 0:320]
    photo = np.zeros((240, 320, 3))
    photo[..., 0] = 0.15 + 0.4 * gx / 320
    photo[..., 1] = 0.55 - 0.2 * gy / 240
    photo[..., 2] = 0.3
    chh, chw = 128, 64
    char = np.zeros((chh, chw, 4))
    char[..., :3] = rng.random((chh, chw, 3))
    yy, xx = np.mgrid[0:chh, 0:chw]
    m = (xx >= 8) & (xx < 56) & (yy >= 4) & (yy < 124)
    char[..., 0][m], char[..., 1][m], char[..., 2][m] = 0.85, 0.30, 0.30
    char[..., 3][m] = 1.0
    return char, photo


def test_scene_follows_a_real_compose_shot() -> None:
    """rect / placement 必须来自同一次合成：image 尺寸与 rect、crop 一致。"""
    from arcam.compose import compose_shot
    from arcam.placement import current_placement
    char, photo = _sprite_and_photo()
    cam, subj = CAM_A, SUBJ_A
    got = compose_shot(photo, char, subj, cam, frame_key="1:1", char_scale=1.5)
    pc = current_placement(char, subj, cam, mode="ground", char_scale=1.5,
                           rot_basis="world")
    d = build_scene(got.rect, pc, subj, cam, frame_key="1:1",
                    char_rgba01=char, char_name="her.png", mode="ground",
                    char_scale=1.5)
    assert d["image"]["width"] == got.shot.shape[1] == js_round(got.rect.w)
    assert d["image"]["height"] == got.shot.shape[0] == js_round(got.rect.h)
    assert d["frame"]["crop"] == [js_round(got.rect.x), js_round(got.rect.y),
                                  js_round(got.rect.w), js_round(got.rect.h)]
    assert d["frame"]["source"] == {"width": cam.width, "height": cam.height}
    assert d["character"]["image"] == {"width": 64, "height": 128}
    assert d["charScale"] == 1.5


def test_size_pixel_scales_with_char_scale() -> None:
    """charScale 只乘投影跨度：sizePixel 就该严格按倍率涨，position 一动都不动。"""
    from arcam.compose import compose_shot
    from arcam.placement import current_placement
    char, photo = _sprite_and_photo()
    cam, subj = CAM_A, SUBJ_A
    base = current_placement(char, subj, cam, mode="ground", char_scale=1.0,
                             rot_basis="world")
    big = current_placement(char, subj, cam, mode="ground", char_scale=1.5,
                            rot_basis="world")
    assert base is not None and big is not None
    d0 = build_scene(RECT_A, base, subj, cam, frame_key="1:1",
                     char_rgba01=char, char_name="her.png", mode="ground",
                     char_scale=1.0)
    d1 = build_scene(RECT_A, big, subj, cam, frame_key="1:1",
                     char_rgba01=char, char_name="her.png", mode="ground",
                     char_scale=1.5)
    assert d1["character"]["sizePixel"] == pytest.approx(
        [v * 1.5 for v in d0["character"]["sizePixel"]], abs=0.01)
    # charScale 是"纯大小系数"：sizePixel 严格按倍率涨，人物位姿与相机参数没动
    assert d1["camera"] == d0["camera"]
    assert d1["subject"] == d0["subject"]
    assert d1["frame"]["crop"] == d0["frame"]["crop"]
    # 合成确实变大了
    s0 = compose_shot(photo, char, subj, cam, frame_key="满幅", char_scale=1.0)
    s1 = compose_shot(photo, char, subj, cam, frame_key="满幅", char_scale=1.5)
    assert int((s1.char_layer[..., 3] > 0).sum()) > int((s0.char_layer[..., 3] > 0).sum())


def test_full_pipeline_roundtrip(tmp_path) -> None:
    """compose_shot → build_scene → save_scene → load_scene 全程不丢字段。"""
    from arcam.compose import compose_shot
    from arcam.placement import current_placement
    char, photo = _sprite_and_photo()
    got = compose_shot(photo, char, SUBJ_A, CAM_A, frame_key="4:5", mode="ground",
                       char_scale=1.25)
    pc = current_placement(char, SUBJ_A, CAM_A, mode="ground", char_scale=1.25,
                           rot_basis="world")
    d = build_scene(got.rect, pc, SUBJ_A, CAM_A, frame_key="4:5",
                    char_rgba01=char, char_name="her.png", mode="ground",
                    char_scale=1.25, shot="腰上", edge_bar=4)
    p = tmp_path / "scene.json"
    save_scene(d, p)
    back = load_scene(p)
    assert back == d and list(back) == list(d)
    assert d["frame"]["aspect"] == "4:5"
    assert d["shot"] == "腰上" and d["edge"] == {"bar": 4, "rotationLocked": True}
