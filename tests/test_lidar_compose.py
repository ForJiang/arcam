"""LiDAR 深度接入 compose（LIDAR-SPEC §4/§5 T5）。

plane.py（L2）未落地时本文件整体 skip（落地后自动生效）。覆盖：

- T5 合成闭环：合成照片 + 立绘 + 合成深度（地面平面 + 噪声，可选遮挡方块）→
  compose_shot(depth=…) 落位与手算一致（h/pitch/roll/d 全由深度管线给出）；
- 遮挡区 alpha=0 且 RGB 清零（照片路径露出原照片、立绘层路径 alpha=0）；
- 无 depth 路径与改动前逐位相同（同输入跑两遍：无 depth vs 显式 env=None）；
- _merge_env 三态优先级（显式 > 深度测出 > 标称默认）单测；
- CLI ``lidar inspect``（文本/--json）与 ``lidar serve`` 占位冒烟、
  ``render --depth`` 的「深度测得…（显式值优先）」提示与 scene.json 记 effective 值。

合成器的坐标推导（与本测试手算对照的基础）：深度相机坐标 p（z 前向、y 向下，
LIDAR-SPEC §1）与 arcam 数学坐标 q=(x, Y-h, Z) 的关系是
``p = R_z(roll)·R_x(pitch)·diag(1,-1,1)·q``（R 与 geometry.project 同一套
角约定：pitch>0 俯视、roll>0 画面顺时针）。于是地面平面在深度坐标的法向
``n = R·(0,-1,0)``、``n·p + h = 0``；像素 (u,v) 的地面深度
``t = -h/(n·d)``、``d = ((u-cx)/fx, (v-cy)/fy, 1)``。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from arcam.cli import main
from arcam.compose import _lidar_env, _merge_env, compose_shot
from arcam.depth import LiDARFrame
from arcam.geometry import Camera, project
from arcam.placement import Subject

pytest.importorskip("arcam.plane", reason="arcam.plane（L2）未落地，跳过 LiDAR 合成测试")
from arcam.plane import CameraEnv, GroundPlane  # noqa: E402

W, H = 320, 240
HALF_FOV = math.radians(30.0)
FX = FY = W / (2.0 * math.tan(HALF_FOV))        # depth.py 的针孔估计同式（§2）
CX, CY = W / 2.0, H / 2.0
SYNTH_H, SYNTH_PITCH, SYNTH_ROLL = 1.55, 12.0, 8.0

CAM = Camera(W, H, 1.6, 23.0, 0.0, 0.0)        # 标称相机（默认值那一份）
SUBJ = Subject(1.65, 4.0, 0.0)                 # 标称人物


# ───────────────────────── 合成器 ─────────────────────────
def _rot_x(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def _rot_z(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _device_rot(pitch: float, roll: float) -> np.ndarray:
    """arcam 相机姿态（pitch>0 俯视、roll>0 画面顺时针，geometry.project 同约定）。"""
    return _rot_z(roll) @ _rot_x(pitch)


def make_char(h: int = 128, w: int = 64) -> np.ndarray:
    """程序生成立绘：实心方块 + 右侧半透明带（与 tests/test_compose.py 同型）。"""
    rng = np.random.default_rng(42)
    char = np.zeros((h, w, 4))
    char[..., :3] = rng.random((h, w, 3))
    x0, y0, x1, y1 = 8, 4, 56, 124
    char[y0:y1, x0:x1, 0] = 0.85
    char[y0:y1, x0:x1, 1] = 0.30
    char[y0:y1, x0:x1, 2] = 0.30
    char[y0:y1, x0:x1, 3] = 1.0
    char[y0:y1, x1:x1 + 6, 3] = 0.25
    char[y0:y1, x1:x1 + 6, :3] = 0.9
    return char


def make_bg() -> np.ndarray:
    """程序生成照片：横向 + 纵向渐变（保证有内容、各通道不等）。"""
    gy, gx = np.mgrid[0:H, 0:W]
    bg = np.zeros((H, W, 3))
    bg[..., 0] = 0.15 + 0.4 * gx / W
    bg[..., 1] = 0.55 - 0.2 * gy / H
    bg[..., 2] = 0.35
    return bg


CHAR = make_char()
BG = make_bg()


def q(a: np.ndarray) -> np.ndarray:
    return (np.clip(np.asarray(a, dtype=np.float64), 0, 1) * 255 + 0.5).astype(np.uint8)


def synth_depth(*, h: float = SYNTH_H, pitch: float = SYNTH_PITCH,
                roll: float = SYNTH_ROLL, width: int = W, height: int = H,
                noise: float = 0.005, seed: int = 7,
                occluder: tuple[float, float, float, float, float] | None = None,
                roll_deg: float | None = None):
    """合成一帧 LiDAR 深度：地面平面 + 高斯噪声 +（可选）世界坐标遮挡方块。

    Args:
        h/pitch/roll: 合成真值的相机高 / 俯仰 / 滚转（arcam 约定，度）。
        occluder: 世界坐标 AABB ``(x0, x1, ytop, z0, z1)``——只存"在相机与地面之间
            可见"的深度（盒子比地面近的像素取盒面深度），用来测遮挡。
        roll_deg: 写进 :class:`LiDARFrame` 的 ARKit 横滚（None = 不提供，走法向分解）。

    Returns:
        ``(frame, diag)``：diag 有 ``box_hit``（遮挡盒比地面近的像素掩码，供
        断言遮挡区域不越界）与真值三元组 ``truth``。
    """
    fx = fy = width / (2.0 * math.tan(HALF_FOV))
    cx, cy = width / 2.0, height / 2.0
    us = np.arange(width, dtype=np.float64)
    vs = np.arange(height, dtype=np.float64)
    ddx = (us[None, :] - cx) / fx                  # (1,W)
    ddy = (vs[:, None] - cy) / fy                  # (H,1)
    r = _device_rot(pitch, roll)
    normal = r @ np.array([0.0, -1.0, 0.0])        # 地面法向（深度坐标，y 向下）
    denom = normal[0] * ddx + normal[1] * ddy + normal[2]
    with np.errstate(divide="ignore", invalid="ignore"):
        t_ground = -h / denom
    ground = np.where((denom < -1e-9) & (t_ground > 0.0) & (t_ground <= 120.0),
                      t_ground, np.nan)

    # 世界坐标射线 P(t) = (t·wx, h + t·wy, t·wz)，t = 深度 z
    m = np.diag([1.0, -1.0, 1.0]) @ r.T
    wx = m[0, 0] * ddx + m[0, 1] * ddy + m[0, 2]
    wy = m[1, 0] * ddx + m[1, 1] * ddy + m[1, 2]
    wz = m[2, 0] * ddx + m[2, 1] * ddy + m[2, 2]

    box_hit = np.zeros((height, width), dtype=bool)
    depth = ground
    if occluder is not None:
        x0, x1, ytop, z0, z1 = occluder

        def slab(c: np.ndarray, base: float, lo: float, hi: float):
            """t·c + base ∈ [lo,hi] 的 t 区间；c≈0 时按 base 在区间内外给全集/空集。"""
            # c≈0 的像素先置 1 规避除零警告（结果随后被 where 覆盖，不参与判定）
            safe = np.where(np.abs(c) < 1e-12, 1.0, c)
            ta = (lo - base) / safe
            tb = (hi - base) / safe
            tlo, thi = np.minimum(ta, tb), np.maximum(ta, tb)
            deg = np.abs(c) < 1e-12
            outside = (base < lo) | (base > hi)
            return (np.where(deg, np.where(outside, np.inf, -np.inf), tlo),
                    np.where(deg, np.where(outside, -np.inf, np.inf), thi))

        tlo, thi = slab(wx, 0.0, x0, x1)
        l2, h2 = slab(wy, h, 0.0, ytop)
        tlo, thi = np.maximum(tlo, l2), np.minimum(thi, h2)
        l3, h3 = slab(wz, 0.0, z0, z1)
        tlo, thi = np.maximum(tlo, l3), np.minimum(thi, h3)
        box_hit = (tlo <= thi) & (thi > 0.0)
        t_box = np.where(box_hit, np.maximum(tlo, 0.0), np.inf)
        depth = np.where(box_hit & (t_box < ground), t_box, ground)

    rng = np.random.default_rng(seed)
    finite = np.isfinite(depth)
    noisy = depth + rng.normal(0.0, noise, size=depth.shape)
    holes = finite & (rng.random(depth.shape) < 0.02)      # 2% 随机空洞
    depth = np.where(finite & ~holes, noisy, np.nan)
    frame = LiDARFrame(depth_m=depth.astype(np.float32), fx=fx, fy=fy, cx=cx, cy=cy,
                       roll_deg=roll_deg, source='synth')
    diag = {'box_hit': box_hit & finite,
            'truth': {'h': h, 'pitch': pitch, 'roll': roll}}
    return frame, diag


def ray_ground_world(u: float, v: float, *, h: float, pitch: float, roll: float,
                     fx: float = FX, fy: float = FY, cx: float = CX,
                     cy: float = CY) -> tuple[float, float]:
    """屏幕像素 (u,v) 的射线与世界地面的交点 → (lateral, distance)（独立手算）。

    与 :func:`synth_depth` 同一套坐标推导的逆：q = t·diag(1,-1,1)·Rᵀ·d，
    t = -h/w_y（= 该像素的地面深度），P = (t·w_x, t·w_z) 加相机高。
    这就是 sample_subject 在「内参用深度相机针孔」时应解出的世界点。
    """
    d = np.array([(u - cx) / fx, (v - cy) / fy, 1.0])
    w = np.diag([1.0, -1.0, 1.0]) @ _device_rot(pitch, roll).T @ d
    t = -h / w[1]
    return (t * w[0], t * w[2])


def env_stub(h: float, pitch: float, roll: float) -> CameraEnv:
    """单元测试用的 CameraEnv（plane 是溯源字段，这里给个占位平面）。"""
    plane = GroundPlane(normal=(0.0, -1.0, 0.0), offset=h, inliers=100, rms=0.001)
    return CameraEnv(height_m=h, pitch_deg=pitch, roll_deg=roll, plane=plane)


# ───────────────────────── T5：合成闭环 ─────────────────────────
def test_depth_env_recovered_through_compose() -> None:
    """无遮挡盒时 compose_shot(depth=…) 的 env ≈ 合成真值（T2/T3 管线的端到端证明）。"""
    frame, _ = synth_depth()
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key='满幅', depth=frame)
    assert got.env is not None
    assert got.env.height_m == pytest.approx(SYNTH_H, abs=0.02)
    assert got.env.pitch_deg == pytest.approx(SYNTH_PITCH, abs=1.0)
    assert got.env.roll_deg == pytest.approx(SYNTH_ROLL, abs=2.0)


def test_depth_env_recovered_with_arkit_roll() -> None:
    """frame.roll_deg 给了（ARKit 横滚）：先反旋再解 pitch，roll 原值带回。"""
    frame, _ = synth_depth(roll_deg=SYNTH_ROLL)
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key='满幅', depth=frame)
    assert got.env is not None
    assert got.env.height_m == pytest.approx(SYNTH_H, abs=0.02)
    assert got.env.pitch_deg == pytest.approx(SYNTH_PITCH, abs=1.0)
    assert got.env.roll_deg == pytest.approx(SYNTH_ROLL, abs=0.01)


def test_compose_with_depth_equals_manual_effective_values() -> None:
    """落位与手算一致：compose(depth=…) == compose(手算 effective cam/subj, depth=None)。

    手算值 = 对同一帧跑 fit→env→_merge_env（即 T2/T3 管线给出的 h/pitch/d）。
    无遮挡盒 → 遮挡层无操作，两条路应逐位相同。
    """
    frame, _ = synth_depth()
    env = _lidar_env(frame)
    cam_eff, subj_eff, notes = _merge_env(CAM, SUBJ, None, env, depth=frame)
    assert notes['d']['source'] == 'lidar' and notes['x']['source'] == 'lidar'

    manual = compose_shot(BG, CHAR, subj_eff, cam_eff, frame_key='满幅')
    auto = compose_shot(BG, CHAR, SUBJ, CAM, frame_key='满幅', depth=frame)
    assert np.array_equal(auto.shot, manual.shot)
    assert np.array_equal(auto.char_layer, manual.char_layer)
    # 深度测得的环境量确实与标称不同（否则这条测试是空转）
    assert cam_eff.height_m != CAM.height_m
    assert cam_eff.pitch_deg != CAM.pitch_deg


def test_measured_distance_matches_independent_hand_calculation() -> None:
    """E3：足底屏幕位置取深度反解 (lateral, d)，与测试自己的逆投影手算一致。"""
    frame, _ = synth_depth()
    env = _lidar_env(frame)
    cam_eff, subj_eff, notes = _merge_env(CAM, SUBJ, None, env, depth=frame)
    # 足底屏幕位置：标称人物在世界接地点 (x,0,d) 于合并后相机下的投影
    uv = project(SUBJ.lateral_m, -cam_eff.height_m, SUBJ.distance_m, cam_eff)
    assert uv is not None
    exp_x, exp_d = ray_ground_world(uv[0], uv[1], h=SYNTH_H, pitch=SYNTH_PITCH,
                                    roll=SYNTH_ROLL)
    assert subj_eff.distance_m == pytest.approx(exp_d, abs=0.02)
    assert subj_eff.lateral_m == pytest.approx(exp_x, abs=0.02)
    # 标称 d=4 与测出值确有差别（场景里足底正前方地面比 4m 远）
    assert subj_eff.distance_m != SUBJ.distance_m


# ───────────────────────── E4：遮挡 ─────────────────────────
OCCLUDER = (-0.25, 0.25, 1.8, 2.8, 3.5)   # 公告板前 ~1m 的方块（世界坐标 AABB）


def test_occlusion_knocks_out_alpha_in_both_paths() -> None:
    """公告板前放方块：mask 覆盖区 alpha=0 且 RGB 清零；照片露出原照片；不越界。"""
    frame_occ, diag = synth_depth(occluder=OCCLUDER, seed=11)
    env_occ = _lidar_env(frame_occ)
    cam_eff, subj_eff, _notes = _merge_env(CAM, SUBJ, None, env_occ, depth=frame_occ)
    # 参考：同一帧解出的 effective 值、但不接深度（无遮挡）→ 唯一差别就是遮挡
    manual = compose_shot(BG, CHAR, subj_eff, cam_eff, frame_key='满幅')
    got = compose_shot(BG, CHAR, SUBJ, CAM, frame_key='满幅', depth=frame_occ)
    a_ref = manual.char_layer[..., 3]
    a = got.char_layer[..., 3]
    knocked = (a_ref > 0) & (a == 0)
    assert knocked.any(), "遮挡盒没有抠掉任何公告板像素"
    assert int(got.char_layer[knocked, :3].max()) == 0, "alpha=0 处 RGB 没清零"
    assert knocked.sum() <= int(diag['box_hit'].sum()), "遮挡区越出遮挡盒屏幕范围"
    # 未抠区域与「同 effective 值、无遮挡」的参考逐位相同
    keep = ~knocked
    assert np.array_equal(got.char_layer[keep], manual.char_layer[keep])
    # 照片路径：被抠处露出原照片
    assert np.array_equal(got.shot[knocked], q(BG)[knocked])


def test_occlusion_layer_only_path_also_knocked() -> None:
    """立绘层直出路径（compose_char_layer 带 depth）同样抠（SPEC §4③）。"""
    from arcam.compose import compose_char_layer
    frame_occ, _ = synth_depth(occluder=OCCLUDER, seed=11)
    env_occ = _lidar_env(frame_occ)
    cam_eff, subj_eff, _notes = _merge_env(CAM, SUBJ, None, env_occ, depth=frame_occ)
    layer_occ, _ = compose_char_layer(CHAR, SUBJ, CAM, depth=frame_occ)
    layer_plain, _ = compose_char_layer(CHAR, subj_eff, cam_eff)   # 同落位、无遮挡
    a_occ, a_ref = layer_occ[..., 3], layer_plain[..., 3]
    knocked = (a_ref > 0) & (a_occ == 0)
    assert knocked.any()
    assert int(layer_occ[knocked, :3].max()) == 0
    assert np.array_equal(layer_occ[~knocked], layer_plain[~knocked])


# ───────────────────────── 回归：无 depth 逐位相同 ─────────────────────────
def test_no_depth_path_is_bit_identical() -> None:
    """同输入跑两遍：无 depth / 显式 env=None / 空 override，三条路逐位相同。"""
    ref = compose_shot(BG, CHAR, SUBJ, CAM, frame_key='1:1')
    none_shot = compose_shot(BG, CHAR, SUBJ, CAM, frame_key='1:1', depth=None,
                             env_overrides=None)
    empty_shot = compose_shot(BG, CHAR, SUBJ, CAM, frame_key='1:1', env_overrides={})
    assert np.array_equal(ref.shot, none_shot.shot)
    assert np.array_equal(ref.char_layer, none_shot.char_layer)
    assert np.array_equal(ref.shot, empty_shot.shot)
    assert np.array_equal(ref.char_layer, empty_shot.char_layer)
    assert none_shot.env is None and empty_shot.env is None


# ───────────────────────── _merge_env 三态优先级 ─────────────────────────
def test_merge_env_nominal_only() -> None:
    cam, subj, notes = _merge_env(CAM, SUBJ, None, None)
    assert cam == CAM and subj == SUBJ
    assert all(n['source'] == 'nominal' and n['used'] is not None
               for n in notes.values())
    assert notes['h']['lidar'] is None and notes['h']['explicit'] is None


def test_merge_env_lidar_measured_values_win_over_nominal() -> None:
    env = env_stub(1.55, 12.0, 8.0)
    cam, subj, notes = _merge_env(CAM, SUBJ, None, env)
    assert cam.height_m == pytest.approx(1.55)
    assert cam.pitch_deg == pytest.approx(12.0)
    assert cam.roll_deg == pytest.approx(8.0)
    assert cam.feq_mm == CAM.feq_mm and (cam.width, cam.height) == (CAM.width, CAM.height)
    assert subj.distance_m == SUBJ.distance_m       # 无 depth → E3 不动 d/x
    assert notes['h'] == {'used': 1.55, 'source': 'lidar', 'lidar': 1.55, 'explicit': None}
    assert notes['d']['source'] == 'nominal'


def test_merge_env_explicit_wins_over_lidar() -> None:
    env = env_stub(1.55, 12.0, 8.0)
    cam, subj, notes = _merge_env(CAM, SUBJ, {'h': 1.7, 'roll': None}, env)
    assert cam.height_m == pytest.approx(1.7)          # 显式优先
    assert cam.pitch_deg == pytest.approx(12.0)        # 没显式 → 测出
    assert cam.roll_deg == pytest.approx(8.0)          # None 视为没给 → 测出
    assert notes['h']['source'] == 'explicit' and notes['h']['explicit'] == 1.7
    assert notes['h']['lidar'] == 1.55                  # 测出值仍记录（诊断）
    assert notes['pitch']['source'] == 'lidar'


def test_merge_env_subject_overrides() -> None:
    frame, _ = synth_depth()
    env = _lidar_env(frame)
    cam, subj, notes = _merge_env(CAM, SUBJ, {'d': 6.5}, env, depth=frame)
    assert subj.distance_m == pytest.approx(6.5)       # 显式 d 优先于 E3 测出值
    assert notes['d']['source'] == 'explicit' and notes['d']['lidar'] is not None
    assert notes['x']['source'] == 'lidar'             # x 没显式 → 仍用测出
    assert cam.height_m == pytest.approx(SYNTH_H, abs=0.02)


def test_merge_env_unknown_key_raises() -> None:
    with pytest.raises(ValueError, match="env_overrides"):
        _merge_env(CAM, SUBJ, {'hp': 1.8}, None)


def test_merge_env_env_without_depth_keeps_subject() -> None:
    """只给 env 不给 depth：E3 无从采样，d/x 保持标称（显式除外）。"""
    cam, subj, notes = _merge_env(CAM, SUBJ, None, env_stub(1.4, 5.0, 1.0))
    assert subj == SUBJ
    assert (cam.height_m, cam.pitch_deg, cam.roll_deg) == (1.4, 5.0, 1.0)


# ───────────────────────── CLI 冒烟 ─────────────────────────
def _write_depth_npy(frame: LiDARFrame, path: Path) -> Path:
    np.save(path, np.asarray(frame.depth_m, dtype=np.float32))
    return path


def test_cli_lidar_inspect_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    frame, _ = synth_depth()
    p = _write_depth_npy(frame, tmp_path / 'scan.npy')
    code = main(['lidar', 'inspect', str(p), '--json'])
    assert code == 0
    info = json.loads(capsys.readouterr().out)
    for key in ('height_m', 'pitch_deg', 'roll_deg', 'coverage', 'inliers', 'rms'):
        assert key in info, key
    assert info['height_m'] == pytest.approx(SYNTH_H, abs=0.05)
    assert info['pitch_deg'] == pytest.approx(SYNTH_PITCH, abs=1.5)
    assert info['roll_deg'] == pytest.approx(SYNTH_ROLL, abs=2.5)
    assert info['inliers'] > 64 and info['rms'] < 0.05
    assert 0.5 < info['coverage'] <= 1.0


def test_cli_lidar_inspect_text(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    frame, _ = synth_depth()
    p = _write_depth_npy(frame, tmp_path / 'scan.npy')
    code = main(['lidar', 'inspect', str(p)])
    assert code == 0
    out = capsys.readouterr().out
    for token in ('coverage', 'inliers', 'rms', 'h=', 'pitch=', 'roll='):
        assert token in out, token


def test_cli_lidar_inspect_bad_inputs(tmp_path: Path) -> None:
    assert main(['lidar', 'inspect', str(tmp_path / 'nope.npy')]) == 1
    empty = tmp_path / 'empty.npy'
    np.save(empty, np.zeros((8, 8), dtype=np.float32))
    assert main(['lidar', 'inspect', str(empty)]) == 1
    junk = tmp_path / 'junk.bin'
    junk.write_bytes(b'not a depth file at all')
    assert main(['lidar', 'inspect', str(junk)]) == 1


def test_cli_lidar_serve_module_missing(capsys: pytest.CaptureFixture,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """lidar_live 不可导入：占位提示"实时流模块未就绪"，退出码 0，不崩。

    ``import arcam.lidar_live as live`` 取的是**父包属性**（不是 sys.modules），
    所以两个都要打掉，否则全量跑时真模块已被别处导入过、delattr 不干净。
    """
    monkeypatch.delattr('arcam.lidar_live', raising=False)
    monkeypatch.setitem(sys.modules, 'arcam.lidar_live', None)
    code = main(['lidar', 'serve', '--port', '7707'])
    assert code == 0
    assert '实时流模块未就绪' in capsys.readouterr().out


def test_cli_lidar_serve_forwards_port_and_out_dir(monkeypatch: pytest
                                                   .MonkeyPatch) -> None:
    """模块已落地：只透传 --port/--out（形参名是 lidar_live 的 out_dir，不是 out）。"""
    import types
    seen: dict = {}

    def fake_serve(**kw):
        seen.update(kw)
        return iter(())

    mod = types.ModuleType('arcam.lidar_live')
    mod.serve = fake_serve                       # type: ignore[attr-defined]
    # import 语句取父包属性优先：直接替换属性才稳（sys.modules 只作兜底）
    monkeypatch.setattr('arcam.lidar_live', mod, raising=False)
    monkeypatch.setitem(sys.modules, 'arcam.lidar_live', mod)
    code = main(['lidar', 'serve', '--port', '7788'])
    assert code == 0 and seen == {'port': 7788}
    code = main(['lidar', 'serve', '--port', '7788', '--out', '/tmp/frames'])
    assert code == 0 and seen == {'port': 7788, 'out_dir': '/tmp/frames'}


def _write_png_char(path: Path) -> Path:
    arr = np.zeros((60, 40, 4), dtype=np.uint8)
    arr[..., 0] = 230
    arr[..., 2] = 200
    arr[..., 3] = 255
    Image.fromarray(arr, 'RGBA').save(path)
    return path


def _write_png_photo(path: Path) -> Path:
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[..., 0], img[..., 1], img[..., 2] = 120, 90, 70
    img[:80, :80] = 60
    Image.fromarray(img).save(path)
    return path


def test_cli_render_with_depth(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """render --depth：退出 0、scene.json 记**测出**的相机高、提示行给测得值。"""
    frame, _ = synth_depth()
    depth_p = _write_depth_npy(frame, tmp_path / 'scan.npy')
    photo = _write_png_photo(tmp_path / 'photo.png')
    char = _write_png_char(tmp_path / 'char.png')
    out = tmp_path / 'shot.png'
    scene = tmp_path / 'scene.json'
    code = main(['render', str(photo), str(char), '-o', str(out), '--depth', str(depth_p),
                 '--scene', str(scene)])
    assert code == 0 and out.is_file() and scene.is_file()
    text = capsys.readouterr().out
    assert '深度测得' in text and '已用于摆位' in text
    assert '显式值优先' not in text
    data = json.loads(scene.read_text(encoding='utf-8'))
    assert data['camera']['heightM'] == pytest.approx(SYNTH_H, abs=0.05)
    assert data['camera']['pitchDeg'] == pytest.approx(SYNTH_PITCH, abs=1.5)
    with Image.open(out) as im:
        assert im.size == (W, H)


def test_cli_render_depth_with_explicit_overrides(tmp_path: Path,
                                                  capsys: pytest.CaptureFixture) -> None:
    """--depth 与显式 --h/--d 同给：一行"深度测得…（显式值优先）"，scene 记显式值。"""
    frame, _ = synth_depth()
    depth_p = _write_depth_npy(frame, tmp_path / 'scan.npy')
    photo = _write_png_photo(tmp_path / 'photo.png')
    char = _write_png_char(tmp_path / 'char.png')
    out = tmp_path / 'shot.png'
    scene = tmp_path / 'scene.json'
    code = main(['render', str(photo), str(char), '-o', str(out), '--depth', str(depth_p),
                 '--scene', str(scene), '--h', '1.9', '--d', '5.5'])
    assert code == 0
    text = capsys.readouterr().out
    assert '深度测得' in text and '显式值优先' in text
    data = json.loads(scene.read_text(encoding='utf-8'))
    assert data['camera']['heightM'] == 1.9          # 显式值进配方
    assert data['subject']['distanceM'] == 5.5
    assert data['camera']['pitchDeg'] == pytest.approx(SYNTH_PITCH, abs=1.5)  # 没显式 → 测出


def test_cli_render_depth_missing_file(tmp_path: Path) -> None:
    photo = _write_png_photo(tmp_path / 'photo.png')
    char = _write_png_char(tmp_path / 'char.png')
    code = main(['render', str(photo), str(char), '-o', str(tmp_path / 'o.png'),
                 '--depth', str(tmp_path / 'nope.npy')])
    assert code == 1
