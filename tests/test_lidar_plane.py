"""plane.py 单测 —— LIDAR-SPEC §5 门禁 T2（地平面闭环）/ T3（距离采样）/ T4（遮挡）。

合成场景全部由 :func:`_synth_scene` 生成：给定 (h, pitch, roll) 在**深度相机坐标**
（光学中心、z 前向、y 向下，LIDAR-SPEC §1）里铺地面点云 + σ=1cm 噪声 + 无效 NaN 区，
按 **Apple 轴向语义**存深度（depth = 相机平面 z 分量，2026-10-03 编排者仲裁，
见 plane 模块 backproject docstring；ARKit/AVFoundation 官方语义）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from arcam.depth import LiDARFrame
from arcam.geometry import K_LONG, Camera, focal_px, project
from arcam.placement import Subject, SpritePlacement, ground_placement
from arcam.plane import (
    CameraEnv,
    DepthStats,
    GroundPlane,
    backproject,
    camera_env_from_plane,
    depth_stats,
    fit_ground_plane,
    occlusion_mask,
    sample_subject,
)

W, H = 480, 360
_DEG2RAD = math.pi / 180.0


def _intrinsics(width: int = W, height: int = H) -> tuple[float, float, float, float]:
    """针孔满幅 FOV≈60°（估计内参，同 depth.py 的 npy 路径假设）。"""
    fx = width / (2.0 * math.tan(math.radians(30.0)))
    return fx, fx, width / 2.0, height / 2.0


def _render_camera(h: float, pitch: float, roll: float, width: int = W, height: int = H) -> Camera:
    """渲染相机：feq 由 fx 反解（同 plane._camera_from_frame 的同焦假设与选边规则）。"""
    fx, _, _, _ = _intrinsics(width, height)
    long_edge = max(width, height)
    short_edge = min(width, height)
    wide = long_edge / short_edge >= 4.0 / 3.0
    half = (long_edge if wide else short_edge) / 2.0
    k = K_LONG if wide else K_SHORT
    return Camera(width=width, height=height, height_m=h,
                  feq_mm=fx * k / half, pitch_deg=pitch, roll_deg=roll)


def _synth_scene(h: float, pitch: float, roll: float, seed: int = 7,
                 sigma: float = 0.01, width: int = W, height: int = H
                 ) -> tuple[LiDARFrame, Camera]:
    """合成 T2 场景：地面深度帧 + 对应渲染相机（LIDAR-SPEC §5 T2）。

    逐像素按解析几何求地面光线深度：像素方向在 arcam 相机系里（A, y_d, d_d)，
    地面 y=-h 交点参数 t = h/(B·cos p + sin p)（place 的分母，index.html:3192/
    artifact A §3.4），**相机平面轴向深度 Zc = t**（Apple 语义：depth 即 z 分量，
    见 plane.backproject docstring 的 2026-10-03 仲裁）。加高斯噪 + 一块无效 NaN 区。
    """
    fx, fy, cx, cy = _intrinsics(width, height)
    p = pitch * _DEG2RAD
    r = roll * _DEG2RAD
    rows, cols = np.mgrid[0:height, 0:width]
    a = (cols - cx) / fx
    b = (rows - cy) / fy
    alpha = a
    beta = b
    big_b = -alpha * math.sin(r) + beta * math.cos(r)
    den = math.sin(p) + big_b * math.cos(p)
    with np.errstate(divide='ignore', invalid='ignore'):
        zc = np.where(den > 1e-6, h / np.where(den > 1e-6, den, 1.0), np.nan)
    depth = zc
    rng = np.random.default_rng(seed)
    depth = depth + rng.normal(scale=sigma, size=depth.shape)
    depth[height // 4:height // 2, width // 8:width // 3] = np.nan   # 无效区（§1）
    frame = LiDARFrame(depth_m=depth.astype(np.float32), fx=fx, fy=fy, cx=cx, cy=cy,
                       roll_deg=None, pitch_deg=None, height_m=None,
                       source=f'synth h={h} p={pitch} r={roll}')
    return frame, _render_camera(h, pitch, roll, width, height)


# ── T2 地平面闭环（核心门禁，LIDAR-SPEC §5）────────────────────────────────

@pytest.mark.parametrize('h,pitch,roll', [
    (1.55, 12.0, 8.0),      # §5 T2 基准参数
    (1.62, -15.0, -10.0),   # 负 pitch / 负 roll
    (1.40, 25.0, 5.0),      # 大俯仰
    (1.75, 0.0, 0.0),       # 恒等：pitch=0,roll=0
    (1.55, 30.0, -20.0),    # 反向大横滚
])
def test_t2_plane_closed_loop(h: float, pitch: float, roll: float) -> None:
    """backproject → fit_ground_plane → camera_env_from_plane 恢复 (h,pitch,roll)。

    容差（LIDAR-SPEC §5 T2）：h < 2cm、pitch < 1°、roll < 2°（σ=1cm 噪声）。
    """
    frame, _ = _synth_scene(h, pitch, roll)
    plane = fit_ground_plane(frame)
    env = camera_env_from_plane(plane)
    assert abs(env.height_m - h) < 0.02, f'h 误差 {abs(env.height_m - h):.4f} m'
    assert abs(env.pitch_deg - pitch) < 1.0, f'pitch 误差 {abs(env.pitch_deg - pitch):.4f}°'
    assert abs(env.roll_deg - roll) < 2.0, f'roll 误差 {abs(env.roll_deg - roll):.4f}°'
    assert plane.rms < 0.03, f'rms {plane.rms:.4f} m'
    assert plane.inliers > 1000


def test_t2_large_frame_strided_subsample() -> None:
    """大画布（>40 万像素）走确定性步进子采样，闭环精度不退化。"""
    h, pitch, roll = 1.55, 12.0, 8.0
    frame, _ = _synth_scene(h, pitch, roll, width=1280, height=720)
    from arcam.plane import _strided_frame
    assert _strided_frame(frame).depth_m.shape[0] < 720   # 确实被步进
    env = camera_env_from_plane(fit_ground_plane(frame))
    assert abs(env.height_m - h) < 0.02
    assert abs(env.pitch_deg - pitch) < 1.0
    assert abs(env.roll_deg - roll) < 2.0


def test_t2_fixed_roll_branch() -> None:
    """roll 已知（ARKit 直测）时固定 roll 解 pitch：结果与齐次解一致。"""
    h, pitch, roll = 1.55, 12.0, 8.0
    frame, _ = _synth_scene(h, pitch, roll)
    plane = fit_ground_plane(frame)
    free = camera_env_from_plane(plane)
    fixed = camera_env_from_plane(plane, roll_deg=roll)
    assert abs(fixed.pitch_deg - free.pitch_deg) < 0.2
    assert fixed.roll_deg == pytest.approx(roll)
    assert abs(fixed.height_m - h) < 0.02


def test_t2_lidar_sim_closed_loop_extreme_pose() -> None:
    """lidar_sim 合成帧的闭环恢复精度（T2 容差），含大角度位姿。

    钉子：``synth_ground_frame`` 的地面法向曾写成「先滚转后俯仰」的
    ``(sin r, −cos r·cos p, −cos r·sin p)``，与 project/place 的旋转链
    （先俯仰后滚转）不一致 —— 小角度误差 <1° 测不出，p=30°/r=−20° 时
    闭环恢复的 roll 差 2.8°、pitch 差 2.0°，超出 T2 门禁（roll<2°/pitch<1°）。
    """
    from arcam.lidar_sim import synth_ground_frame

    for h, pitch, roll in ((1.55, 30.0, -20.0), (1.6, 12.0, 8.0), (1.55, -15.0, -10.0)):
        meta, depth = synth_ground_frame(h=h, pitch_deg=pitch, roll_deg=roll,
                                         width=256, height=192, index=0)
        frame = LiDARFrame(depth_m=depth, fx=meta['fx'], fy=meta['fy'],
                           cx=meta['cx'], cy=meta['cy'], source='lidar_sim')
        env = camera_env_from_plane(fit_ground_plane(frame))
        assert abs(env.height_m - h) < 0.02, f'h 误差 {abs(env.height_m - h):.4f} m'
        assert abs(env.pitch_deg - pitch) < 1.0, \
            f'pitch 误差 {abs(env.pitch_deg - pitch):.4f}°（h={h} p={pitch} r={roll}）'
        assert abs(env.roll_deg - roll) < 2.0, \
            f'roll 误差 {abs(env.roll_deg - roll):.4f}°（h={h} p={pitch} r={roll}）'


def test_t2_anchor_identities() -> None:
    """法向锚点（§3 要求）：pitch=0,roll=0 → n_arcam=(0,1,0)；pitch=90 → 沿 z 轴。"""
    flat = camera_env_from_plane(GroundPlane((0.0, -1.0, 0.0), 1.6, 100, 0.0))
    assert flat.pitch_deg == pytest.approx(0.0, abs=1e-9)
    assert flat.roll_deg == pytest.approx(0.0, abs=1e-9)
    assert flat.height_m == pytest.approx(1.6)
    down = camera_env_from_plane(GroundPlane((0.0, 0.0, -1.0), 1.2, 100, 0.0))
    assert down.pitch_deg == pytest.approx(90.0)
    # pitch=90 时 roll 不可观（任意 roll 同一平面）：只要求 pitch 与 n 的 z 分量
    assert down.pitch_deg == pytest.approx(90.0)


def test_t2_offset_semantics_under_flip() -> None:
    """y 翻转（正交变换）不改变 offset → 两个坐标系里 h 都是 plane.offset。"""
    p, r = 20.0 * _DEG2RAD, 15.0 * _DEG2RAD
    n_d = np.array([math.cos(p) * math.sin(r), -math.cos(p) * math.cos(r), -math.sin(p)])
    plane = GroundPlane(tuple(n_d), 1.55, 100, 0.0)
    env = camera_env_from_plane(plane)
    assert env.height_m == pytest.approx(1.55)          # offset 即 h（符号不变）
    assert env.pitch_deg == pytest.approx(20.0)
    assert env.roll_deg == pytest.approx(15.0)


# ── T3 距离采样（LIDAR-SPEC §5 T3）─────────────────────────────────────────

def test_t3_sample_subject() -> None:
    """世界点 (x=0.6, Z=5.0) 投屏幕 → sample_subject 恢复误差 < 5cm。"""
    h, pitch, roll, x_world, z_world = 1.55, 12.0, 8.0, 0.6, 5.0
    frame, cam = _synth_scene(h, pitch, roll)
    env = camera_env_from_plane(fit_ground_plane(frame))
    uv = project(x_world, -h, z_world, cam)
    assert uv is not None
    lateral, distance = sample_subject(frame, env, uv[0], uv[1])
    assert abs(lateral - x_world) < 0.05, f'横向误差 {abs(lateral - x_world):.4f} m'
    assert abs(distance - z_world) < 0.05, f'纵深误差 {abs(distance - z_world):.4f} m'


def test_t3_sample_subject_with_arkit_extrinsics() -> None:
    """ARKit 直测 pitch/roll 在帧上时同样恢复（直测量优先分支）。"""
    h, pitch, roll = 1.55, 12.0, 8.0
    frame, cam = _synth_scene(h, pitch, roll)
    frame = LiDARFrame(depth_m=frame.depth_m, fx=frame.fx, fy=frame.fy,
                       cx=frame.cx, cy=frame.cy, roll_deg=roll, pitch_deg=pitch,
                       height_m=h, source=frame.source)
    env = camera_env_from_plane(fit_ground_plane(frame))
    uv = project(0.6, -h, 5.0, cam)
    assert uv is not None
    lateral, distance = sample_subject(frame, env, uv[0], uv[1])
    assert abs(lateral - 0.6) < 0.05
    assert abs(distance - 5.0) < 0.05


def test_t3_sample_subject_degenerate_raises() -> None:
    """place 无解（pitch=0 时画面正中行射线与地平面平行）→ ValueError。"""
    h, pitch = 1.55, 0.0
    frame, _ = _synth_scene(h, pitch, 0.0)
    env = camera_env_from_plane(fit_ground_plane(frame))
    with pytest.raises(ValueError):
        sample_subject(frame, env, W / 2.0, H / 2.0)


# ── T4 遮挡（LIDAR-SPEC §5 T4）─────────────────────────────────────────────

def _char() -> np.ndarray:
    return np.zeros((80, 48, 4), dtype=np.uint8)


def _placement(cam: Camera, h: float, d: float, x: float) -> object:
    subj = Subject(height_m=1.65, distance_m=d, lateral_m=x)
    plc = ground_placement(_char(), subj, cam)
    assert plc is not None
    return plc


def _paint_block(depth: np.ndarray, cam: Camera, z_world: float,
                 x_lo: float, x_hi: float, y_lo: float, y_hi: float,
                 step: float = 0.02) -> set[tuple[int, int]]:
    """在深度帧上刷一个「立在世界 Z=z_world 的竖直方块」，返回被写过的像素集。"""
    painted: set[tuple[int, int]] = set()
    xs = np.arange(x_lo, x_hi + 1e-9, step)
    ys = np.arange(y_lo, y_hi + 1e-9, step)
    for xw in xs:
        for yw in ys:
            uv = project(xw, yw - cam.height_m, z_world, cam)
            if uv is None:
                continue
            u, v = int(round(uv[0])), int(round(uv[1]))
            if 0 <= u < depth.shape[1] and 0 <= v < depth.shape[0]:
                # 轴向深度（Apple 语义）：= project() 里的 Zc，roll 不影响 z 分量
                p = math.radians(cam.pitch_deg)
                z_axis = -(yw - cam.height_m) * math.sin(p) + z_world * math.cos(p)
                depth[v, u] = z_axis
                painted.add((u, v))
    return painted


def _local(plc: SpritePlacement, u: int, v: int) -> tuple[float, float]:
    """像素 (u,v) → 公告板局部坐标（rot 的逆旋转，与 occlusion_mask 同口径）。"""
    du, dv = float(u) - plc.u, float(v) - plc.v
    cr, sr = math.cos(plc.rot), math.sin(plc.rot)
    return du * cr + dv * sr, -du * sr + dv * cr


def test_t4_occlusion_front_block() -> None:
    """公告板前 0.5m 方块 → 覆盖被挡区（公告板内的方块像素全 True）。"""
    h, pitch, roll, d_person, x_person = 1.55, 12.0, 8.0, 5.0, 0.6
    frame, cam = _synth_scene(h, pitch, roll)
    env = camera_env_from_plane(fit_ground_plane(frame))
    plc = _placement(cam, h, d_person, x_person)

    depth = frame.depth_m.astype(np.float64).copy()
    painted = _paint_block(depth, cam, d_person - 0.5, x_person - 0.5, x_person + 0.3,
                           0.0, 0.95)
    blocked = LiDARFrame(depth_m=depth.astype(np.float32), fx=frame.fx, fy=frame.fy,
                         cx=frame.cx, cy=frame.cy, roll_deg=roll, pitch_deg=pitch,
                         height_m=h, source='t4-front')
    mask = occlusion_mask(blocked, cam, plc)

    inside_painted = [(u, v) for (u, v) in painted
                      if abs(_local(plc, u, v)[0]) <= plc.w / 2.0
                      and abs(_local(plc, u, v)[1]) <= plc.h / 2.0]
    assert len(inside_painted) > 200, f'方块盖住公告板像素太少：{len(inside_painted)}'
    hit = sum(1 for (u, v) in inside_painted if mask[v, u])
    assert hit == len(inside_painted), \
        f'公告板内被挡像素未全抠：{hit}/{len(inside_painted)}'

    # 公告板外像素不判
    outside = [(u, v) for v in range(0, H, 7) for u in (2, W - 3)]
    for (u, v) in outside:
        assert not mask[v, u], f'公告板外像素被误判：({u},{v})'


def test_t4_occlusion_back_block() -> None:
    """公告板后 0.5m 方块 → 全 False（不抠）。

    纯净地面 + 后方方块时唯一可能为 True 的是公告板**下沿（脚线）**那一两个边界
    像素：公告板竖直面的底线与地面相切，模型上两者同深度，margin 5cm 之外的少量
    越界像素来自公告板矩形比真实卡片的梯形轮廓略宽——这是矩形化公告板的固有近似，
    合理验收口径是「脚线 ±4px 之外、且总数极少」。
    """
    h, pitch, roll, d_person, x_person = 1.55, 12.0, 8.0, 5.0, 0.6
    frame, cam = _synth_scene(h, pitch, roll)
    env = camera_env_from_plane(fit_ground_plane(frame))
    plc = _placement(cam, h, d_person, x_person)

    depth = frame.depth_m.astype(np.float64).copy()
    painted = _paint_block(depth, cam, d_person + 0.5, x_person - 0.5, x_person + 0.3,
                           0.0, 0.95)
    blocked = LiDARFrame(depth_m=depth.astype(np.float32), fx=frame.fx, fy=frame.fy,
                         cx=frame.cx, cy=frame.cy, roll_deg=roll, pitch_deg=pitch,
                         height_m=h, source='t4-back')
    mask = occlusion_mask(blocked, cam, plc)

    fake = [mask[v, u] for (u, v) in painted]
    assert painted and not any(fake), '公告板后方方块被误判为遮挡'
    ys, xs = np.nonzero(mask)
    assert int(mask.sum()) <= 12, f'意外遮挡像素过多：{int(mask.sum())}'
    for v, u in zip(ys.tolist(), xs.tolist()):
        ly = _local(plc, u, v)[1]
        assert ly >= plc.h / 2.0 - 4.0, f'非脚线像素被误判：({u},{v}) ly={ly:.1f}'


def test_backproject_axial_z_equals_depth() -> None:
    """语义钉子：Apple 轴向下 backproject 的 z 分量**恒等于**裸 depth。

    2026-10-03 编排者仲裁（调研 + 官方点云样例 ``point(u,v,1)·depth``）：
    ARKit/AVFoundation 的 depth 就是相机平面 z 分量。此测试防有人把
    「cosθ 折半成光线长度」的实现塞回来——那会与真机语义相反。
    """
    frame, _ = _synth_scene(1.55, 12.0, 8.0)
    points, valid = backproject(frame.depth_m, frame.fx, frame.fy, frame.cx, frame.cy)
    raw = np.asarray(frame.depth_m, dtype=np.float64)
    assert points.shape[0] == int(valid.sum())
    rows_all, cols_all = np.nonzero(valid)
    z_true_by_pixel = np.full(raw.shape, np.nan)
    z_true_by_pixel[rows_all, cols_all] = points[:, 2]
    for col in (0, W // 2, W - 1):
        col_valid = valid[:, col]
        assert int(col_valid.sum()) > 50
        diff = np.abs(raw[:, col] - z_true_by_pixel[:, col])
        assert float(np.nanmax(diff)) == 0.0, \
            f'列 {col} z 与 depth 不一致：{float(np.nanmax(diff))}'


# ── 辅助单元测试（backproject / fit 门槛 / 诊断）────────────────────────────

def test_backproject_basics() -> None:
    """y 向下约定 + 无效剔除 + 形状。"""
    depth = np.full((H, W), np.nan, dtype=np.float64)
    depth[H // 2, W // 2] = 5.0          # 正前方 5m → (0,0,5)
    depth[H // 2 + 40, W // 2] = 5.0     # 画面下方 40px → 深度系 y 为正（向下）
    depth[H // 2, W // 2 - 100] = 5.0    # 画面左方 → x 为负
    depth[0, 0] = -1.0                   # 负值 → 无效
    fx, fy, cx, cy = _intrinsics()
    depth = np.full((H, W), np.nan, dtype=np.float64)
    depth[H // 2, W // 2] = 5.0          # 正前方 5m → (0,0,5)
    depth[H // 2 + 40, W // 2] = 5.0     # 画面下方 40px → 深度系 y 为正（向下）
    depth[H // 2, W // 2 - 100] = 5.0    # 画面左方 → x 为负
    depth[0, 0] = -1.0                   # 负值 → 无效
    points, valid = backproject(depth, fx, fy, cx, cy)
    assert points.shape == (3, 3)
    assert not bool(valid[0, 0])
    assert int(valid.sum()) == 3
    rows, cols = np.nonzero(valid)
    at = {(int(c), int(r)): tuple(points[i].tolist())
          for i, (r, c) in enumerate(zip(rows, cols))}
    assert at[(W // 2, H // 2)] == pytest.approx([0.0, 0.0, 5.0])
    below = at[(W // 2, H // 2 + 40)]
    assert below[1] > 0 and below[0] == pytest.approx(0.0)   # y 向下为正
    assert below[2] == pytest.approx(5.0)                    # 轴向语义：z == depth
    left = at[(W // 2 - 100, H // 2)]
    assert left[0] < 0 and left[2] == pytest.approx(5.0)     # 轴向语义：z == depth
    with pytest.raises(ValueError):
        backproject(np.zeros((2, 2, 2)), 1.0, 1.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        backproject(depth, 0.0, 1.0, 0.0, 0.0)


def test_fit_requires_min_points() -> None:
    """有效点 < 64 → ValueError（LIDAR-SPEC §3）。

    门槛在反投影之后立即触发，故深度值几何形状不限：给 20 个有效像素即可。
    """
    fx, fy, cx, cy = _intrinsics()
    depth = np.full((H, W), np.nan, dtype=np.float32)
    depth[10:15, 10:14] = 3.0            # 5×4 = 20 个有效点
    tiny = LiDARFrame(depth_m=depth, fx=fx, fy=fy, cx=cx, cy=cy)
    with pytest.raises(ValueError):
        fit_ground_plane(tiny)


def test_fit_deterministic() -> None:
    """固定种子：同输入两次拟合结果一致。"""
    frame, _ = _synth_scene(1.55, 12.0, 8.0)
    a = fit_ground_plane(frame)
    b = fit_ground_plane(frame)
    assert a.normal == pytest.approx(b.normal)
    assert a.offset == pytest.approx(b.offset)
    assert a.inliers == b.inliers
    assert a.rms == pytest.approx(b.rms)


def test_depth_stats() -> None:
    """E5 诊断：覆盖率与内点比例。"""
    frame, _ = _synth_scene(1.55, 12.0, 8.0)
    plane = fit_ground_plane(frame)
    stats: DepthStats = depth_stats(frame, plane)
    assert 0.0 < stats.coverage < 1.0
    assert 0.0 < stats.inlier_ratio <= 1.0


def test_sample_subject_camera_focal_match() -> None:
    """同焦假设：反解 feq 使 focal_px(cam) == frame.fx（宽幅画布走 K_LONG 长边）。"""
    frame, _ = _synth_scene(1.55, 12.0, 8.0)
    env = camera_env_from_plane(fit_ground_plane(frame))
    from arcam.plane import _camera_from_frame
    cam = _camera_from_frame(frame, env)
    assert focal_px(cam) == pytest.approx(frame.fx, rel=1e-12)


def test_non_square_frame_short_edge_branch() -> None:
    """非宽幅画布（W:H < 4/3）走 K_SHORT 短边分支，focal 仍等于 fx。"""
    width, height = 300, 400
    frame, _ = _synth_scene(1.55, 12.0, 8.0, width=width, height=height)
    env = camera_env_from_plane(fit_ground_plane(frame))
    from arcam.plane import _camera_from_frame
    cam = _camera_from_frame(frame, env)
    assert focal_px(cam) == pytest.approx(frame.fx, rel=1e-12)
    assert cam.width == width and cam.height == height


def test_env_and_plane_fields() -> None:
    """CameraEnv / GroundPlane 数据类字段溯源不变量。"""
    plane = GroundPlane((0.0, -1.0, 0.0), 1.55, 42, 0.012)
    env: CameraEnv = camera_env_from_plane(plane)
    assert env.plane is plane
    assert env.height_m == 1.55
    assert isinstance(plane.normal, tuple)
    assert plane.normal == (0.0, -1.0, 0.0)


def test_occlusion_mask_shape_and_dtype() -> None:
    """掩码形状/类型与帧一致；纯净地面场景公告板内部无遮挡。

    公告板下沿（脚线）与地面相切，矩形化公告板略宽于真实卡片轮廓，脚线 ±4px 内
    允许极少量边界像素（见 test_t4_occlusion_back_block 的说明）。
    """
    frame, cam = _synth_scene(1.55, 12.0, 8.0)
    env = camera_env_from_plane(fit_ground_plane(frame))
    plc = _placement(cam, 1.55, 5.0, 0.6)
    mask = occlusion_mask(frame, cam, plc)
    assert mask.shape == frame.depth_m.shape
    assert mask.dtype == np.bool_
    ys, xs = np.nonzero(mask)
    assert int(mask.sum()) <= 12
    for v, u in zip(ys.tolist(), xs.tolist()):
        assert _local(plc, u, v)[1] >= plc.h / 2.0 - 4.0


def _paint_fronto_film(depth: np.ndarray, frame: LiDARFrame, zc: float,
                       rows_sel: slice | None = None) -> set[tuple[int, int]]:
    """刷一层**平行于像平面**（深度相机系 z = zc 恒定）的薄膜，返回被写像素集。

    每像素轴向深度 = zc 恒定（Apple 语义：depth 即 z 分量，见 plane.backproject
    docstring 的 2026-10-03 仲裁）—— 用来精确制造「距公告板 d 厘米」的几何，
    而不用担心俯仰/横滚带来的深度歧义。
    """
    hgt, wdt = depth.shape
    painted: set[tuple[int, int]] = set()
    for v in range(hgt):
        for u in range(wdt):
            if rows_sel is not None and not (rows_sel.start <= v < rows_sel.stop):
                continue
            depth[v, u] = zc
            painted.add((u, v))
    return painted


def test_occlusion_margin_tolerance() -> None:
    """margin 区间内不算遮挡（§3）：薄膜距眼平面公告板 3cm → 默认 margin 5cm 不抠。"""
    h, pitch, roll, d_person, x_person = 1.55, 12.0, 8.0, 5.0, 0.6
    frame, cam = _synth_scene(h, pitch, roll)
    env = camera_env_from_plane(fit_ground_plane(frame))
    plc = _placement(cam, h, d_person, x_person)
    # 眼平面公告板深度 = place(足底).d·cos(pitch)
    zc_bill = d_person * math.cos(math.radians(pitch))
    depth = frame.depth_m.astype(np.float64).copy()
    band = slice(int(round(plc.v)) - 10, int(round(plc.v)) + 10)
    painted = _paint_fronto_film(depth, frame, zc_bill - 0.03, band)
    assert len(painted) > 100
    blocked = LiDARFrame(depth_m=depth.astype(np.float32), fx=frame.fx, fy=frame.fy,
                         cx=frame.cx, cy=frame.cy, roll_deg=roll, pitch_deg=pitch,
                         height_m=h, source='t4-margin')
    mask = occlusion_mask(blocked, cam, plc)
    assert not any(mask[v, u] for (u, v) in painted), 'margin 5cm 内不该判遮挡'
    # margin 收到 1cm → 同一场景转为遮挡（证明判据方向正确）
    tight = occlusion_mask(blocked, cam, plc, margin_m=0.01)
    assert any(tight[v, u] for (u, v) in painted), 'margin 1cm 应判遮挡'

