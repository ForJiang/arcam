"""几何与投影核心 —— 原版 AR 取景器的针孔相机模型（index.html:1041-1057 / 3184-3202）。

所有角度对外用**度**（``Camera.pitch_deg`` / ``Camera.roll_deg``，对应原版 ``S.pitch`` /
``S.roll``），函数内部转弧度；内部一律 float64（原版声明往返误差 3.6e-15，float32 会把它
抬到 1e-3 px 量级）。

已知偏离/如实保留项（见 docs/SPEC.md §5 与 artifact A-projection-core.md §6.5）：
- ``project`` 只有 ``Zc <= 0.05`` 一条剔除，**没有 FOV 剔除**（artifact §3.2 末注：
  加了 FOV clip 会与桌面验证器的往返测试对不上）。
- ``place`` 写回时 ``S.d`` 夹 [0.5, 20]（index.html:3197）、``S.x`` 夹 [-6, 6]
  （index.html:3198）两处 clamp 范围与 ``autoFix`` 的 ±20 不一致，如实保留。
- 主点固定在画布中心 ``(W/2, H/2)``，无主点偏移（artifact §1.6）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

K_LONG = 17.3064   # 0.8*43.266/2（mm，传感器长半边长），index.html:1042
K_SHORT = 12.9798  # 0.6*43.266/2（mm，传感器短半边长），index.html:1042

_DEG2RAD = math.pi / 180.0


@dataclass(frozen=True)
class Camera:
    """针孔相机参数。字段即原版 ``S`` 里的对应量（index.html:921-937）。

    ``pitch_deg`` > 0 俯视、``roll_deg`` > 0 画面顺时针（artifact A §1.4 / §1.5）。
    """

    width: int
    height: int
    height_m: float
    feq_mm: float
    pitch_deg: float
    roll_deg: float


def focal_px(cam: Camera) -> float:
    """像素焦距 F。index.html:1043-1047。

    L = max(W,H)，Sd = min(W,H)，wide = L/Sd >= 4/3；
    wide → (L/2)*feq/K_LONG，否则 (Sd/2)*feq/K_SHORT（针孔模型
    F_px = 像素半边长 × f_mm / 传感器半边长_mm）。
    """
    long_edge = max(cam.width, cam.height)
    short_edge = min(cam.width, cam.height)
    wide = long_edge / short_edge >= 4.0 / 3.0
    if wide:
        return (long_edge / 2.0) * cam.feq_mm / K_LONG
    return (short_edge / 2.0) * cam.feq_mm / K_SHORT


def project(x: float, y: float, d: float, cam: Camera) -> tuple[float, float] | None:
    """相机系点 (x, y, d) → 画布像素 (u, v)。index.html:1048-1057。

    y 是**相机系 y**（= 世界高度 - 相机高，调用方负责换算，见 artifact A §1.3：
    groundPlacement 传的是 ``Y_world - S.h``）。Zc <= 0.05（点在相机平面上或背后）
    返回 None —— 唯一剔除条件，无 FOV 裁剪。
    """
    p = cam.pitch_deg * _DEG2RAD
    r = cam.roll_deg * _DEG2RAD
    zc = -y * math.sin(p) + d * math.cos(p)
    if zc <= 0.05:
        return None
    yc = -y * math.cos(p) - d * math.sin(p)
    xr = x * math.cos(r) - yc * math.sin(r)
    yr = x * math.sin(r) + yc * math.cos(r)
    focal = focal_px(cam)
    u = cam.width / 2.0 + focal * xr / zc
    v = cam.height / 2.0 + focal * yr / zc
    return (u, v)


def place(u: float, v: float, cam: Camera) -> tuple[float, float] | None:
    """画布像素 (u, v) → 世界 (x, d)（米）。index.html:3184-3202。

    project 固定 y = -cam.height_m（人立在地面上，Y_world = 0）的解析逆：
    A = (u-W/2)/F·cos r + (v-H/2)/F·sin r；B = -(u-W/2)/F·sin r + (v-H/2)/F·cos r；
    d = h·(cos p - B·sin p)/(sin p + B·cos p)；x = A·(h·sin p + d·cos p)。

    如实保留的原版写回语义（docs/SPEC.md §5 第 1 条）：d 夹 [0.5, 20]（3197）、
    x 夹 [-6, 6]（3198）；后者的 ±6 与 auto_fix_x 的 ±20 故意不一致，不许"顺手修"。
    退化返回 None：|den| < 1e-6（3193，射线与地平面近平行）或 d <= 0.05（3195）。
    SPEC §3.1 摘要只提 "x 夹 ±6"，这里连 d 的 clamp 一起按原版逐字保留。
    """
    p = cam.pitch_deg * _DEG2RAD
    r = cam.roll_deg * _DEG2RAD
    focal = focal_px(cam)
    up = u - cam.width / 2.0
    vp = v - cam.height / 2.0
    a = (up / focal) * math.cos(r) + (vp / focal) * math.sin(r)
    b = -(up / focal) * math.sin(r) + (vp / focal) * math.cos(r)
    den = math.sin(p) + b * math.cos(p)
    if abs(den) < 1e-6:
        return None
    d = cam.height_m * (math.cos(p) - b * math.sin(p)) / den
    if d <= 0.05:
        return None
    zc = cam.height_m * math.sin(p) + d * math.cos(p)
    x = a * zc
    return (min(6.0, max(-6.0, x)), min(20.0, max(0.5, d)))
