"""轨道插值 / 渲染 + 图片 IO 单测。

断言取向：能离线算的（插值、帧数、schema 往返）手推；要过 composeShot 管线的用
**可观测性质**（帧数、尺寸、立绘随俯角整体上移），不绑定 P1/P3 的内部实现。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from arcam.video import (
    PoseKey,
    PoseTrack,
    frame_count,
    load_track,
    render_track,
    sample_track,
    save_track,
    track_issues,
)

TRACK_KW = dict(width=64, height=96, fps=4.0, height_m=1.6, feq_mm=23.0,
                subject_height_m=1.65, char_scale=1.0, size_pct=60.0,
                mode="ground", frame_key="满幅")


def make_track(keys: list[PoseKey], **kw) -> PoseTrack:
    data = dict(TRACK_KW)
    data.update(kw)
    return PoseTrack(keys=keys, **data)


def make_char(w: int = 20, h: int = 30) -> np.ndarray:
    """品红立绘（alpha 全不透明）—— 色相与中灰背景差异大，便于像素级定位。"""
    rgb = np.zeros((h, w, 4), dtype=np.uint8)
    rgb[..., 0] = 230
    rgb[..., 2] = 200
    rgb[..., 3] = 255
    return rgb.astype(np.float64) / 255.0


def sprite_mask(frame: np.ndarray) -> np.ndarray:
    """品红立绘的色相掩码：(r+b-2g) 大 → 立绘；灰色阴影/背景都进不来。"""
    a = np.asarray(frame)[..., :3].astype(np.int16)
    return (a[..., 0] + a[..., 2] - 2 * a[..., 1]) > 40


def centroid_v(frame: np.ndarray) -> float:
    mask = sprite_mask(frame)
    ys = np.nonzero(mask)[0]
    return float(ys.mean())


# ---------- schema 往返 ----------

def test_save_load_roundtrip(tmp_path: Path) -> None:
    track = make_track([
        PoseKey(t=0.0, lateral_m=0.0, distance_m=4.0, pitch_deg=0.0, roll_deg=0.0),
        PoseKey(t=0.5, lateral_m=1.0, distance_m=5.0, pitch_deg=10.0, roll_deg=-3.0),
        PoseKey(t=1.0, lateral_m=-1.0, distance_m=6.0, pitch_deg=20.0, roll_deg=3.0,
                feq_mm=30.0, mode="free", size_pct=70.0),
    ])
    path = tmp_path / "track.json"
    save_track(track, path)
    back = load_track(path)
    assert back == track
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["width"] == 64 and data["height"] == 96 and data["fps"] == 4.0
    assert data["frame_key"] == "满幅" and data["mode"] == "ground"
    assert len(data["keys"]) == 3
    assert data["keys"][2]["feq_mm"] == 30.0 and data["keys"][2]["mode"] == "free"
    assert "height_m" not in data["keys"][0]      # 没表态的键内字段不落盘
    assert data["keys"][0]["t"] == 0.0


def test_load_minimal_and_defaults(tmp_path: Path) -> None:
    """只写 t + distance_m：其余键内字段走 S 默认，顶层字段走轨道默认。"""
    path = tmp_path / "min.json"
    path.write_text(json.dumps({"keys": [{"t": 0.0, "distance_m": 4.0}]}),
                    encoding="utf-8")
    track = load_track(path)
    assert track.width == 1080 and track.height == 1920 and track.fps == 30.0
    assert track.height_m == 1.6 and track.feq_mm == 23.0
    assert track.subject_height_m == 1.65 and track.char_scale == 1.0
    assert track.size_pct == 60.0 and track.mode == "ground"
    assert track.frame_key == "满幅" and track.shot == "全身"
    key = track.keys[0]
    assert key.lateral_m == 0.0 and key.pitch_deg == 0.0 and key.roll_deg == 0.0
    assert key.height_m is None and key.mode is None


def test_load_errors(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(ValueError, match="不存在"):
        load_track(missing)
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_track(bad)
    typo = tmp_path / "typo.json"
    typo.write_text(json.dumps({"keys": [{"t": 0, "distance_m": 4, "pitc": 1}]}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="pitc"):
        load_track(typo)
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"keys": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="keys"):
        load_track(empty)
    disordered = tmp_path / "dis.json"
    disordered.write_text(json.dumps({"keys": [
        {"t": 0.0, "distance_m": 4.0},
        {"t": 1.0, "distance_m": 4.0},
        {"t": 0.5, "distance_m": 4.0},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="递增"):
        load_track(disordered)
    badframe = tmp_path / "frame.json"
    badframe.write_text(json.dumps({"frame_key": "全幅", "keys": [{"t": 0, "distance_m": 4}]}),
                        encoding="utf-8")
    with pytest.raises(ValueError, match="满幅"):
        load_track(badframe)


# ---------- 插值 ----------

def test_key_interpolation_midpoint() -> None:
    """中点手推：两键 t=0 / t=1，参数线性插值，t=0.5 应正好落在两点正中。"""
    track = make_track([
        PoseKey(t=0.0, lateral_m=0.0, distance_m=4.0, pitch_deg=0.0, roll_deg=0.0),
        PoseKey(t=1.0, lateral_m=2.0, distance_m=8.0, pitch_deg=10.0, roll_deg=-20.0),
    ])
    pose = sample_track(track, 0.5)
    assert pose.lateral_m == pytest.approx(1.0)      # (0+2)/2
    assert pose.distance_m == pytest.approx(6.0)     # (4+8)/2
    assert pose.pitch_deg == pytest.approx(5.0)      # (0+10)/2
    assert pose.roll_deg == pytest.approx(-10.0)     # (0-20)/2
    assert pose.height_m == pytest.approx(1.6)       # 没表态 → 轨道默认
    assert pose.feq_mm == pytest.approx(23.0)
    assert pose.subject_height_m == pytest.approx(1.65)
    assert pose.char_scale == pytest.approx(1.0)
    assert pose.size_pct == pytest.approx(60.0)
    assert pose.mode == "ground"


def test_interpolation_optional_override_and_inherit() -> None:
    track = make_track(
        [PoseKey(t=0.0, distance_m=4.0, feq_mm=20.0),
         PoseKey(t=2.0, distance_m=4.0)],
        feq_mm=23.0,
    )
    assert sample_track(track, 0.0).feq_mm == pytest.approx(20.0)   # 键表态优先
    assert sample_track(track, 2.0).feq_mm == pytest.approx(23.0)   # None → 轨道默认
    assert sample_track(track, 1.0).feq_mm == pytest.approx(21.5)   # 20→23 的中点


def test_interpolation_clamps_outside() -> None:
    track = make_track([
        PoseKey(t=0.25, distance_m=4.0, pitch_deg=5.0),
        PoseKey(t=0.75, distance_m=8.0, pitch_deg=15.0),
    ])
    assert sample_track(track, -1.0).distance_m == pytest.approx(4.0)   # 早于首键 → 首键
    assert sample_track(track, 99.0).distance_m == pytest.approx(8.0)   # 晚于末键 → 末键
    assert sample_track(track, 99.0).pitch_deg == pytest.approx(15.0)


def test_interpolation_quarter_and_mode_switch() -> None:
    track = make_track([
        PoseKey(t=0.0, distance_m=4.0, pitch_deg=0.0),
        PoseKey(t=1.0, distance_m=8.0, pitch_deg=40.0, mode="free"),
    ])
    assert sample_track(track, 0.25).pitch_deg == pytest.approx(10.0)
    assert sample_track(track, 0.25).mode == "ground"        # 过键才切
    assert sample_track(track, 1.0).mode == "free"


def test_frame_count() -> None:
    # 帧数 = js_round(末键时间 × fps) + 1：fps=4、末键 t=1 → 5 帧，末帧正好落在末键上
    track = make_track([PoseKey(t=0.0, distance_m=4.0), PoseKey(t=1.0, distance_m=5.0)])
    assert frame_count(track) == 5
    assert [i / 4.0 for i in range(frame_count(track))][-1] == 1.0
    assert frame_count(make_track([PoseKey(t=0.0, distance_m=4.0)])) == 1
    assert frame_count(make_track([PoseKey(t=0.0, distance_m=4.0),
                                   PoseKey(t=0.5, distance_m=5.0)])) == 3


# ---------- 自检 ----------

def test_track_issues() -> None:
    good = make_track([PoseKey(t=0.0, distance_m=4.0, pitch_deg=0.0, roll_deg=0.0)])
    assert track_issues(good) == []
    bad_pitch = make_track([PoseKey(t=0.0, distance_m=4.0, pitch_deg=75.0)])
    assert track_issues(bad_pitch)
    bad_roll = make_track([PoseKey(t=0.0, distance_m=4.0, roll_deg=-90.0)])
    assert track_issues(bad_roll)
    bad_dist = make_track([PoseKey(t=0.0, distance_m=0.01)])
    assert any("0.01" in m for m in track_issues(bad_dist))


# ---------- 渲染 ----------

def test_render_single_bg_frame_count_and_size(tmp_path: Path) -> None:
    """3 键 + 单图背景：帧数 = 末键×fps+1，每帧尺寸 = 画布满幅。"""
    track = make_track(
        [PoseKey(t=0.0, distance_m=4.0, pitch_deg=0.0),
         PoseKey(t=0.5, distance_m=4.0, pitch_deg=10.0),
         PoseKey(t=1.0, distance_m=4.0, pitch_deg=20.0)],
    )
    bg = np.full((96, 64, 3), 128, dtype=np.uint8)
    frames = render_track(bg, track, make_char(), tmp_path / "out")
    assert len(frames) == 5
    assert [p.name for p in frames] == [f"frame_{i:06d}.png" for i in range(1, 6)]
    for p in frames:
        with Image.open(p) as im:
            assert im.size == (64, 96) and im.mode == "RGB"


def test_render_pitch_moves_sprite_up(tmp_path: Path) -> None:
    """俯角变大（低头看）= 地面上的人物在画面里上移 —— 立绘色心 v 单调变小。

    推导（project，index.html:1048-1057，roll=0）：
    脚 v 偏移 = F·Yc/Zc，Yc = -y·cos p - d·sin p，y=-1.6、d=4；
    p=0°  → Yc=1.6、Zc=4.0 → v = H/2 + F·0.40
    p=20° → Yc≈0.136、Zc≈4.306 → v = H/2 + F·0.0316
    脚点（= 贴地锚点）明显上移，整块立绘随之整体上移。
    """
    track = make_track(
        [PoseKey(t=0.0, distance_m=4.0, pitch_deg=0.0),
         PoseKey(t=1.0, distance_m=4.0, pitch_deg=20.0)],
    )
    bg = np.full((96, 64, 3), 128, dtype=np.uint8)
    frames = render_track(bg, track, make_char(), tmp_path / "out")
    assert len(frames) == 5
    centers = []
    for p in frames:
        with Image.open(p) as im:
            arr = np.asarray(im.convert("RGB"))
        mask = sprite_mask(arr)
        assert mask.sum() > 50, "立绘应当清晰可辨"
        ys = np.nonzero(mask)[0]
        centers.append(float(ys.mean()))
    assert centers[0] > centers[-1] + 5.0
    assert all(b < a for a, b in zip(centers, centers[1:])), centers


def test_render_bg_dir_frames_and_hold(tmp_path: Path) -> None:
    """帧目录：按文件名排序逐帧用；目录帧数不足时 hold 最后一帧。"""
    colors = [(200, 40, 40), (40, 200, 40), (40, 40, 200)]
    bg_dir = tmp_path / "bg"
    bg_dir.mkdir()
    for i, c in enumerate(colors, start=1):
        Image.fromarray(np.full((96, 64, 3), c, dtype=np.uint8)).save(
            bg_dir / f"frame_{i:06d}.png")
    track = make_track([PoseKey(t=0.0, distance_m=4.0), PoseKey(t=0.5, distance_m=4.0)])
    frames = render_track(bg_dir, track, make_char(), tmp_path / "out")
    assert len(frames) == 3
    for p, color in zip(frames, colors):
        with Image.open(p) as im:
            corner = tuple(np.asarray(im.convert("RGB"))[2, 2])
        assert corner == color          # 左上角远离立绘，应是该帧背景原色

    hold_dir = tmp_path / "bg1"
    hold_dir.mkdir()
    Image.fromarray(np.full((96, 64, 3), colors[1], dtype=np.uint8)).save(
        hold_dir / "frame_000001.png")
    frames = render_track(hold_dir, track, make_char(), tmp_path / "out2")
    assert len(frames) == 3
    for p in frames:
        with Image.open(p) as im:
            corner = tuple(np.asarray(im.convert("RGB"))[2, 2])
        assert corner == colors[1]     # 3 帧全部 hold 在唯一那帧背景上

    # 目录帧数多于轨道帧数：多余的忽略（时间轴由轨道定义，不从背景借帧）
    more_dir = tmp_path / "bg2"
    more_dir.mkdir()
    for i, c in enumerate(colors * 2, start=1):
        Image.fromarray(np.full((96, 64, 3), c, dtype=np.uint8)).save(
            more_dir / f"frame_{i:06d}.png")
    frames = render_track(more_dir, track, make_char(), tmp_path / "out3")
    assert len(frames) == 3
    for p, color in zip(frames, colors):
        with Image.open(p) as im:
            corner = tuple(np.asarray(im.convert("RGB"))[2, 2])
        assert corner == color         # 仍按文件名顺序取前 3 帧


def test_render_resizes_mismatched_bg(tmp_path: Path) -> None:
    bg = np.full((48, 32, 3), 100, dtype=np.uint8)     # 与轨道画布不同尺寸
    track = make_track([PoseKey(t=0.0, distance_m=4.0)])
    frames = render_track(bg, track, make_char(), tmp_path / "out")
    assert len(frames) == 1
    with Image.open(frames[0]) as im:
        assert im.size == (64, 96)


def test_render_rejects_bad_track(tmp_path: Path) -> None:
    track = make_track([PoseKey(t=0.0, distance_m=4.0, pitch_deg=75.0)])
    bg = np.full((96, 64, 3), 128, dtype=np.uint8)
    with pytest.raises(ValueError, match="俯角"):
        render_track(bg, track, make_char(), tmp_path / "out")


def test_render_requires_char(tmp_path: Path) -> None:
    track = make_track([PoseKey(t=0.0, distance_m=4.0)])
    bg = np.full((96, 64, 3), 128, dtype=np.uint8)
    with pytest.raises(ValueError, match="立绘"):
        render_track(bg, track, None, tmp_path / "out")


def test_render_missing_bg(tmp_path: Path) -> None:
    track = make_track([PoseKey(t=0.0, distance_m=4.0)])
    with pytest.raises(ValueError, match="背景"):
        render_track(tmp_path / "nope.png", track, make_char(), tmp_path / "out")


def test_render_tone_flag_runs(tmp_path: Path) -> None:
    track = make_track([PoseKey(t=0.0, distance_m=4.0), PoseKey(t=1.0, distance_m=5.0)])
    bg = np.full((96, 64, 3), 128, dtype=np.uint8)
    frames = render_track(bg, track, make_char(), tmp_path / "out", tone=True)
    assert len(frames) == 5
    for p in frames:
        assert p.is_file()


def test_save_png_compress_level_pixels_identical(tmp_path: Path) -> None:
    """compress_level 只影响编码速度/体积，解码像素必须逐位相同（PNG 无损）。

    render_track 的帧序列走 level 3（实测比默认 6 快一半、体积 +13%），这条钉住
    "换级别不改像素"，防止将来有人把它换成有损路径。
    """
    from arcam.video import save_png
    arr = np.zeros((40, 30, 3), np.uint8)
    arr[10:20, 5:25] = (200, 60, 180)
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    save_png(arr, a)
    save_png(arr, b, compress_level=1)
    with Image.open(a) as im1, Image.open(b) as im2:
        assert np.array_equal(np.asarray(im1), np.asarray(im2))
    assert a.stat().st_size > 0 and b.stat().st_size > 0


def test_render_accepts_float01_background(tmp_path: Path) -> None:
    """float64 [0,1] 背景（image_to_rgb01 的产物）也要对：曾静默多除一次 255。

    尺寸一致时旧代码把 0.5 当成 0.5/255 → 整张近黑；尺寸不一致时 PIL 的
    fromarray 根本不认 3 通道 float64 → TypeError。两种都修好。
    """
    track = make_track([PoseKey(t=0.0, distance_m=4.0)])
    char = make_char()

    same = np.full((96, 64, 3), 0.5, dtype=np.float64)
    frames = render_track(same, track, char, tmp_path / "out_same")
    with Image.open(frames[0]) as im:
        corner = np.asarray(im.convert("RGB"))[2, 2]
    assert tuple(corner) == (128, 128, 128), corner        # 0.5 → 128，不再近黑

    small = np.full((48, 32, 3), 0.5, dtype=np.float64)     # 尺寸不一致 → 缩放
    frames = render_track(small, track, char, tmp_path / "out_small")
    with Image.open(frames[0]) as im:
        assert im.size == (64, 96)
        corner = np.asarray(im.convert("RGB"))[2, 2]
    assert tuple(corner) == (128, 128, 128), corner


def test_load_rejects_duplicate_timestamps(tmp_path: Path) -> None:
    """重复 t 会让前一键永远取不到（bisect_right 直接跳过它）——按笔误报错。"""
    path = tmp_path / "dup.json"
    path.write_text(json.dumps({"keys": [
        {"t": 0.0, "distance_m": 4.0},
        {"t": 0.0, "distance_m": 5.0},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="严格递增"):
        load_track(path)
    dup = make_track([PoseKey(t=0.0, distance_m=4.0), PoseKey(t=0.0, distance_m=5.0)])
    assert any("严格递增" in m for m in track_issues(dup))


def test_track_issues_rejects_bad_enums(tmp_path: Path) -> None:
    """PoseTrack 可直接构造：写错的 mode/frame_key/shot 也要被自检拦下。

    漏过去的话 frame_rect 把未知画幅当 0（不裁，静默出错图）、SHOTS[...] 直接
    KeyError —— 都属"不静默失败"要拦的那类。
    """
    good = dict(TRACK_KW)
    for field, bad in (("mode", "floor"), ("frame_key", "全幅"), ("shot", "半身")):
        track = PoseTrack(keys=[PoseKey(t=0.0, distance_m=4.0)],
                          **{**good, field: bad})
        problems = track_issues(track)
        assert any(field in m for m in problems), (field, problems)
        with pytest.raises(ValueError, match="轨道有问题"):
            render_track(np.full((96, 64, 3), 128, dtype=np.uint8), track,
                         make_char(), tmp_path / "never-written")
        assert not (tmp_path / "never-written").exists()
