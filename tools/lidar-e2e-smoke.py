"""arcam LiDAR E2E 冒烟：可验证合成地面深度 → HEIC 容器 → 平面拟合 → 外参解算 → 深度驱动合成。

真值已知（相机高 1.5m / 俯角 20° / 滚转 -8°），解出值必须回到真值附近，
否则链路某环有系统性偏差。用法：.venv/bin/python tools/lidar-e2e-smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pillow_heif                                     # noqa: E402
pillow_heif.register_heif_opener()

import arcam.depth as D                                # noqa: E402
from arcam.compose import compose_shot                 # noqa: E402
from arcam.geometry import Camera                      # noqa: E402
from arcam.placement import Subject                   # noqa: E402
from arcam.plane import camera_env_from_plane, fit_ground_plane  # noqa: E402

W, H = 640, 480
H_CAM, PITCH, ROLL = 1.5, 20.0, -8.0
OUT = Path("/tmp/arcam-heic")
OUT.mkdir(parents=True, exist_ok=True)


def synth_ground_depth() -> np.ndarray:
    """按 place()（project 的解析逆）逐像素取世界地面点的 Apple 轴向深度。

    place(u,v,cam) 给地面点世界 (x, d)；代回 project 的 zc 公式（y=-h 地面点）：
    z_c = h·sin(p) + d·cos(p) —— 与真机 sceneDepth 的轴向语义逐字一致。
    返回 (H, W) float64 米，逐行线性插值补稀疏洞。

    ⚠ 焦距必须与 HEIC 加载器的 ``_estimate_pinhole`` 对齐（同焦假设，见
    .ai/COLLABORATION.md「已记录边界」）：这里反解出使 focal_px == fx_est 的
    feq，否则拟合解出的高/俯角会有系统性偏移（roll 不受影响）。
    """
    from arcam.geometry import place
    fx_est = D._estimate_pinhole(W, H)[0]
    # wide 支：F = (W/2)·feq/K_LONG → feq = 2F·K_LONG/W
    from arcam.geometry import K_LONG
    feq_match = 2.0 * fx_est * K_LONG / W
    cam = Camera(W, H, H_CAM, feq_match, PITCH, ROLL)
    assert abs(D_focal := __import__("arcam.geometry", fromlist=["focal_px"]).focal_px(cam) - fx_est) < 1e-9, D_focal
    p = np.radians(PITCH)
    depth = np.full((H, W), np.nan)
    for iv in range(H):
        for iu in range(W):
            hit = place(iu + 0.0, iv + 0.0, cam)
            if hit is None:
                continue
            _x, d = hit
            if not (0.5 <= d <= 20.0):
                continue
            depth[iv, iu] = H_CAM * np.sin(p) + d * np.cos(p)
    valid = np.isfinite(depth)
    if not valid.any():
        raise SystemExit("合成失败：place() 逆投影没产出任何有效像素")
    filled = depth.copy()
    for row in range(H):
        cols = np.where(valid[row])[0]
        if cols.size:
            filled[row] = np.interp(np.arange(W), cols, depth[row, cols])
    return filled


class FakeDepth:
    """pillow-heif HeifDepthImage 的字段级同构（.info['metadata'] + .to_pillow()）。"""

    def __init__(self, arr: np.ndarray, metadata: dict):
        self.info = {"metadata": metadata}
        self._arr = arr

    def to_pillow(self) -> Image.Image:
        im = Image.fromarray(self._arr.astype(np.float32))
        im.info = dict(self.info)
        return im


class FakeHeif:
    """Image.open 返回同构：.info['depth_images'] + 上下文管理协议。"""

    def __init__(self, depths):
        self.info = {"depth_images": depths}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def main() -> int:
    depth = synth_ground_depth()
    print("合成深度: 有效 %.1f%%，范围 [%.2f, %.2f]m" % (
        100 * np.isfinite(depth).mean(), float(np.nanmin(depth)), float(np.nanmax(depth))))

    # 真 HEIC 容器（RGB 主图过 libheif），深度按 pillow-heif 真实结构注入
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[..., 0], rgb[..., 1], rgb[..., 2] = 130, 150, 200
    rgb[H // 2:] = (100, 90, 80)
    heic = OUT / "ground.heic"
    Image.fromarray(rgb, "RGB").save(heic, format="HEIF")

    real_open = Image.open

    def fake_open(path, *a, **kw):
        if str(path).endswith((".heic", ".heif")):
            return FakeHeif([FakeDepth(depth, {"representation_type": 2})])
        return real_open(path, *a, **kw)

    Image.open = fake_open
    try:
        frame = D._load_heic_depth(heic)
    finally:
        Image.open = real_open
    print("HEIC 加载: %s fx=%.1f（辅图尺寸估计，非实测）" % (frame.depth_m.shape, frame.fx))

    plane = fit_ground_plane(frame)
    env = camera_env_from_plane(plane)
    print("解出: 高=%.3fm（真值 %.2f）俯角=%.2f°（真值 %.1f）滚转=%.2f°（真值 %.1f）" % (
        env.height_m, H_CAM, env.pitch_deg, PITCH, env.roll_deg, ROLL))
    print("平面: 内点=%d rms=%.2fmm" % (plane.inliers, plane.rms * 1000))

    ok_h = abs(env.height_m - H_CAM) < 0.05
    ok_p = abs(env.pitch_deg - PITCH) < 1.0
    ok_r = abs(env.roll_deg - ROLL) < 1.0
    if not (ok_h and ok_p and ok_r):
        print("!! 外参解算偏离真值超限（合成数据/链路问题）")
        return 1

    # 深度驱动合成：立绘按解出外参贴地，遮挡 mask 由深度帧算出
    char = np.zeros((H, W, 4), np.uint8)
    for y in range(120, 460):
        t = (y - 120) / 340.0
        half = int(20 + 90 * np.sin(np.pi * min(t * 1.1, 1.0)) ** 0.8)
        char[y, 320 - half:320 + half] = (200, 160, 130, 255)
    cam = Camera(W, H, env.height_m, 23.0, env.pitch_deg, env.roll_deg)
    subj = Subject(1.65, 4.0, 0.0)
    shot = compose_shot(
        np.asarray(Image.open(heic).convert("RGB")).astype(np.float64) / 255.0,
        char.astype(np.float64) / 255.0, subj, cam, frame_key="满幅", depth=frame)
    out = (np.clip(shot.shot, 0, 1) * 255).astype(np.uint8)
    Image.fromarray(out, "RGB").save(OUT / "e2e.jpg", quality=92)
    print("深度驱动合成 OK: %s → %s" % (out.shape, OUT / "e2e.jpg"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
