"""arcam.lidar_sim —— 实时流协议模拟器（docs/LIDAR-SPEC §6，测试/无真机调试用）。

合成场景（对应 LIDAR-SPEC §5 T2 的 (h, pitch, roll) 闭环场景）：相机离地 ``h``、
绕光轴横滚 ``roll_deg``、向下俯仰 ``pitch_deg`` 看向水平地面。约定全部按
LIDAR-SPEC §1 的**深度相机坐标**（y 向下、z 前向）：

- 地面平面（世界 +Y 向上，相机高 h）在深度相机坐标满足 ``n·p = −h``，
  法向 ``n = (sin r, −cos r·cos p, −cos r·sin p)``（r、p 为弧度）——
  等价于世界坐标里的平面方程经 ``arcam.plane._to_arcam_camera_coords`` 所指的
  y 翻转。推导：相机位姿 R = Rz(roll)·Rx(pitch)（arcam y 上 z 前向约定，
  pitch>0 为俯视），世界点 q = (x, 0, z) 投影到相机坐标再翻 y。
- 像素 (u, v) 的射线方向 ``d = ((u−cx)/fx, (v−cy)/fy, 1)``，命中地面的
  射线参数（即 depth 值，= 后投影点的 z 分量）为 ``t = −h/(n·d)``——
  与 ``arcam.plane.backproject(depth)`` 的 ``p = d·depth`` 完全互逆，
  因此 fit → camera_env_from_plane 可闭环恢复 (h, pitch, roll)。
- 无效区：``n·d ≥ 0``（地平线及以上，射线不与地面相交）→ NaN；另加一块
  矩形 NaN 区模拟遮挡吸光体；全部叠加 σ=1cm 高斯噪声。
- 内参按针孔满幅 FOV≈60°（``fx = W/(2·tan30°)``、``fy = H/(2·tan30°)``、
  ``cx = W/2``、``cy = H/2``），与 LIDAR-SPEC §2 对 npy 内参缺省的估计约定一致。

``simulate_scene_server`` 起一个 daemon 线程扮演「设备」：连接已在监听的
``arcam.lidar_live.serve``，按协议（``send_frame``）推 ``frames`` 帧后关闭。
"""

from __future__ import annotations

import contextlib
import math
import socket
import threading
import time

import numpy as np

from arcam.lidar_live import send_frame

_DEPTH_DTYPE = np.dtype('<f4')


def pinhole_defaults(width: int, height: int) -> tuple[float, float, float, float]:
    """针孔满幅 FOV≈60° 内参，与 ``arcam.depth._estimate_pinhole`` **同口径**（方形像素）。

    ``fy = fx``（不按 H 竖 FOV 另算）：模拟帧若经 ``.npy`` 落盘再读回，loader 只能
    按方形像素假设估计内参，两边不一致会让平面拟合系统性失真（真机 ARCD 头带
    fp32 内参、往返无损，不受此约束）。主点 (cx,cy)=(W/2,H/2)。
    """
    half_tan = math.tan(math.radians(30.0))
    fx = width / (2.0 * half_tan)
    return fx, fx, width / 2.0, height / 2.0


def synth_ground_frame(
    h: float = 1.6,
    pitch_deg: float = 12.0,
    roll_deg: float = 8.0,
    width: int = 256,
    height: int = 192,
    index: int = 0,
    seed: int = 0,
) -> tuple[dict, np.ndarray]:
    """合成第 ``index`` 帧深度相机坐标地面点云 → ``(meta, depth)``。

    ``depth``: (height, width) float32 米，无效 = NaN（地平线以上 + 遮挡块）；
    ``meta``: LIDAR-SPEC §6 要求的全部字段（W,H,fx,fy,cx,cy,roll_deg,pitch_deg,
    height_m,timestamp）。噪声由 ``(seed, index)`` 决定播种 → 同参同参同帧可复现。
    """
    pitch = math.radians(pitch_deg)
    roll = math.radians(roll_deg)
    fx, fy, cx, cy = pinhole_defaults(width, height)

    # 地面平面法向（深度相机坐标，y 向下、z 前向），n·p = −h
    normal = np.array([
        math.sin(roll),
        -math.cos(roll) * math.cos(pitch),
        -math.cos(roll) * math.sin(pitch),
    ])

    us = np.arange(width, dtype=np.float64)
    vs = np.arange(height, dtype=np.float64)
    dx = (us - cx) / fx          # 每列的 x 方向分量
    dy = (vs - cy) / fy          # 每行的 y 方向分量
    denom = normal[0] * dx[None, :] + normal[1] * dy[:, None] + normal[2]

    with np.errstate(divide='ignore', invalid='ignore'):
        depth_f = np.where(denom < -1e-9, -h / denom, np.nan)

    # 地平线以上（n·d≥0）已是 NaN；再卡一下 LiDAR 有效量程 (0, 120]
    depth_f[(depth_f <= 0.0) | (depth_f > 120.0)] = np.nan

    # 遮挡/吸光体无效区（固定矩形，跨帧稳定）
    depth_f[height // 3: height // 3 + height // 8,
            width // 4: width // 2] = np.nan

    rng = np.random.default_rng(seed * 1_000_003 + index)
    noise = rng.normal(0.0, 0.01, size=(height, width))
    depth_f = depth_f + noise
    depth_f[~np.isfinite(depth_f) | (depth_f <= 0.05) | (depth_f > 120.0)] = np.nan
    depth = np.ascontiguousarray(depth_f, dtype=_DEPTH_DTYPE)

    meta = {
        'W': int(width),
        'H': int(height),
        'fx': float(fx),
        'fy': float(fy),
        'cx': float(cx),
        'cy': float(cy),
        'roll_deg': float(roll_deg) if roll_deg is not None else None,
        'pitch_deg': float(pitch_deg) if pitch_deg is not None else None,
        'height_m': float(h) if h is not None else None,
        'timestamp': time.time(),
    }
    return meta, depth


def simulate_scene_server(
    port: int,
    h: float = 1.6,
    pitch: float = 12.0,
    roll: float = 8.0,
    frames: int = 5,
    fps: int = 10,
) -> threading.Thread:
    """起 daemon 线程扮演设备：连 ``(127.0.0.1, port)`` 上监听的 serve，
    按 LIDAR-SPEC §6 协议推 ``frames`` 帧后干净关闭。返回该线程（可 join）。"""
    def _run() -> None:
        with contextlib.closing(socket.create_connection(('127.0.0.1', port), timeout=10.0)) as sock:
            for i in range(frames):
                meta, depth = synth_ground_frame(h=h, pitch_deg=pitch, roll_deg=roll, index=i)
                send_frame(sock, meta, depth)
                if i + 1 < frames and fps > 0:
                    time.sleep(1.0 / fps)

    thread = threading.Thread(target=_run, name='arcam-lidar-sim', daemon=True)
    thread.start()
    return thread
