"""LiDAR 平面与环境量 —— 地平面拟合 → 相机高度/俯仰/横滚 + 采样 + 遮挡（LIDAR-SPEC §3）。

本模块无原版 JS 对应物，是 iPhone 激光雷达深度接入 arcam 的新功能，接口契约以
``docs/LIDAR-SPEC.md`` §1/§3/§5 为准；相机投影/反投影公式沿用 ``arcam.geometry``
的 ``project``/``place``（index.html:1041-1057 / 3184-3202，artifact A-projection-core.md
§1、§3.2-3.4），本文件只做「深度相机坐标 ↔ arcam 相机坐标」的外参数学。

坐标约定（LIDAR-SPEC §1，三条硬约定）：

1. **深度相机坐标**：光学中心为原点、``z`` 前向、``y`` **向下**，
   ``p = ((u-cx)/fx·z, (v-cy)/fy·z, z)``；无效深度 = 0/NaN。
2. **arcam 相机坐标**：``y`` **向上**、``z`` 前向；与深度相机坐标只差一个 y 翻转，
   翻转换算统一 import ``arcam.depth._to_arcam_camera_coords``（全仓唯一实现，任何
   模块不得各自推导，LIDAR-SPEC §1）。
3. 平面在**深度相机坐标**里拟合、存储（:class:`GroundPlane`）；
   :func:`camera_env_from_plane` 内部翻到 arcam 相机坐标解 h/pitch/roll。

核心恒等式（推导见各函数 docstring；T2 合成闭环自证容差 pitch<1° / roll<2° / h<2cm）：

- 深度相机坐标里的**地面朝天法向**（单位、深度系）::

      n_d = (cos p·sin r,  −cos p·cos r,  −sin p)
      offset = h          # n_d·p + offset = 0，offset 即相机离地高

  其中 ``p = pitch``（arcam 约定，>0 俯视）、``r = roll``（arcam 约定，>0 画面顺时针，
  与 ``project`` 的旋转方向同源）。arcam 相机坐标（翻 y 后）的法向::

      n_arcam = (cos p·sin r,  cos p·cos r,  −sin p)

  - ``p=0, r=0`` → ``n_arcam = (0, 1, 0)``（世界 +Y）；
  - ``p=90``（直视地面）→ ``n_arcam = (0, 0, −1)``（朝天方向背对相机 = 深度系 −z，
    即「指向相机 z 轴」的反向：相机看向 −y，地面法向朝天 = +y，在光学系里是 −z）。

数值口径：内部一律 float64（SPEC §1.2）；纯 numpy（禁 scipy/sklearn）；RANSAC 固定
种子 ``np.random.default_rng(0)``。角度对外一律**度**（与 ``Camera`` 字段一致）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from arcam.depth import LiDARFrame, _to_arcam_camera_coords
from arcam.geometry import K_LONG, K_SHORT, Camera, place
from arcam.placement import SpritePlacement

_DEG2RAD = math.pi / 180.0
_RAD2DEG = 180.0 / math.pi

#: RANSAC 最少点数（LIDAR-SPEC §3：点数 <64 → ValueError）
_MIN_POINTS = 64
#: RANSAC 候选采样子集上限（确定性步进抽样，控制大画布深度图的耗时/内存）
_SEARCH_CAP = 20000
#: 子集像素数上限（fit 前的整幅步进，避免 backproject 物化千万点）
_FRAME_PIXEL_CAP = 400_000
#: 深度相机坐标里「朝天」方向（y 向下 → 负 y 为天）：地面法向约束的基准（§3）
_UP_DEPTH = (0.0, -1.0, 0.0)
#: 立绘公告板深度容差默认值（LIDAR-SPEC §3 / §5 T4 口径）
_DEFAULT_MARGIN_M = 0.05


@dataclass(frozen=True)
class GroundPlane:
    """地平面，**深度相机坐标**（y 向下、z 前向，LIDAR-SPEC §3）。

    - ``normal``：单位法向（深度相机坐标）；朝向约定为「朝天」——即
      ``normal·(0,-1,0) >= cos(max_slope_deg)``（拟合时强制，见 :func:`fit_ground_plane`）；
    - ``offset``：平面方程 ``normal·p + offset = 0``（深度相机坐标，米）的常数项。
      法向朝天时相机在平面正侧，故 ``offset`` = 相机离地高 ``h``（推导见
      :func:`camera_env_from_plane`：**正交变换（含 y 翻转）不改变 offset**）；
    - ``inliers`` / ``rms``：拟合内点数 / 内点残差 RMS（米，E5 平面质量诊断）。

    统计口径：``inliers``/``rms`` 在 :func:`fit_ground_plane` 的**确定性抽样子集**
    上统计（大画布步进，见该函数 docstring），不是全幅点数。
    """

    normal: tuple[float, float, float]
    offset: float
    inliers: int
    rms: float


@dataclass(frozen=True)
class CameraEnv:
    """从地平面解出的相机环境量（LIDAR-SPEC §3 / §0 E1-E2）。

    - ``height_m``：相机离地高（覆盖 ``S.h``）；
    - ``pitch_deg``：俯仰，arcam 约定 >0 俯视（覆盖 ``S.pitch``）；
    - ``roll_deg``：横滚（0 = 从法向分解；或采用外部给定 roll——传入值原样回带）；
    - ``plane``：溯源（:class:`GroundPlane`，深度相机坐标）。
    """

    height_m: float
    pitch_deg: float
    roll_deg: float
    plane: GroundPlane


@dataclass(frozen=True)
class DepthStats:
    """深度帧统计（LIDAR-SPEC §3 / §0 E5：CLI 报告 + scene 诊断）。

    - ``coverage``：有效像素比例（finite 且 > 0）；
    - ``inlier_ratio``：平面内点数 / 有效像素数（口径同 :class:`GroundPlane`
      的抽样子集统计，见 :func:`depth_stats`）。
    """

    coverage: float
    inlier_ratio: float


def backproject(depth_m: np.ndarray, fx: float, fy: float, cx: float, cy: float
                ) -> tuple[np.ndarray, np.ndarray]:
    """深度图 → 点云（**深度相机坐标**，光学中心、z 前向、y 向下）。

    LIDAR-SPEC §3：``p = ((u-cx)/fx·z, (v-cy)/fy·z, z)``，返回 ``(points (N,3),
    valid_mask (H,W) bool)``，**仅有效像素**（finite 且 > 0；0/负/NaN 一律无效，§1）。
    像素坐标取整数下标（u=列、v=行），与 ``arcam.geometry.project`` 的画布像素口径
    一致（主点 ``cx=W/2`` 即画布中心，无主点偏移，artifact A §1.6）。

    ⚠ **depth 值就是相机平面轴向距离 z**（Apple 语义）。ARKit 文档原文
    "distance from the plane of the camera in meters"
    （ARFrame.sceneDepth / ARDepthData，2026-10-03 调研见
    ``oss-teardown/lidar-research.md`` §2），官方点云样例反投影
    ``worldPoint = (K⁻¹·T) · point(u,v,1) · depth`` 也是把深度直接乘在 z=1
    的射线上。2026-10-03 编排者仲裁：早先实现把 depth 当沿光线长度做
    ``cosθ`` 折半，与 Apple 语义相反，已改回轴向——合成器（test、lidar_sim）
    全部按轴向口径出数。无有效像素时返回空 ``(0,3)`` 点云与全 False 掩码
    （不抛异常——门槛检查归 fit）。
    """
    depth = np.asarray(depth_m, dtype=np.float64)
    if depth.ndim != 2:
        raise ValueError(f'期望 (H,W) 深度图，得到形状 {depth.shape}')
    fx = float(fx)
    fy = float(fy)
    if not (math.isfinite(fx) and fx > 0.0) or not (math.isfinite(fy) and fy > 0.0):
        raise ValueError(f'内参 fx/fy 须为有限正数，得到 fx={fx} fy={fy}')
    valid = np.isfinite(depth) & (depth > 0.0)
    rows, cols = np.nonzero(valid)
    depth_v = depth[rows, cols]
    a = (cols.astype(np.float64) - float(cx)) / fx
    b = (rows.astype(np.float64) - float(cy)) / fy
    z = depth_v                       # depth 即相机平面 z（Apple 语义，见 docstring）
    x = a * z
    y = b * z
    points = np.stack([x, y, z], axis=1)
    return points, valid


def _strided_frame(frame: LiDARFrame) -> LiDARFrame:
    """大深度图的确定性整幅步进（内参同步缩放），把像素数压到 *_FRAME_PIXEL_CAP* 内。

    步进 ``s`` 的每个像素是原图像素的**精确子采样**（不改数值，最近邻之外的任何
    滤波都会把平面抹糊/把障碍边缘混进背景，§2 对齐约定同精神）。像素坐标随之缩放：
    原图像素 ``(u,v) = (s·u', s·v')``，故 ``(u-cx)/fx = (u'-cx/s)/(fx/s)``——
    传入缩放后的 ``fx/s, fy/s, cx/s, cy/s`` 即可复现同一批三维点（等比缩放不变量）。
    帧本身小于上限时原样返回。depth float32→float64 数值口径不变。
    """
    height, width = int(frame.depth_m.shape[0]), int(frame.depth_m.shape[1])
    pixels = height * width
    if pixels <= _FRAME_PIXEL_CAP:
        return frame
    stride = int(math.ceil(math.sqrt(pixels / float(_FRAME_PIXEL_CAP))))
    stride = max(1, stride)
    return LiDARFrame(
        depth_m=frame.depth_m[::stride, ::stride],
        fx=frame.fx / stride,
        fy=frame.fy / stride,
        cx=frame.cx / stride,
        cy=frame.cy / stride,
        roll_deg=frame.roll_deg,
        pitch_deg=frame.pitch_deg,
        height_m=frame.height_m,
        source=frame.source,
    )


def _rms_of(resid: np.ndarray, mask: np.ndarray) -> float:
    """内点残差 RMS。掩码为空返回 ``inf``（防御分支：调用方保证 ≥3 内点，不可达）。

    不返回 NaN：任何与 NaN 的比较都静默为假（``rms < 门限`` 恒不成立），
    「没有内点」会被下游当成"拟合成功但残差可疑"或者反过来静默放行；用 inf
    表达至少让所有门限显式失败。（顺带：``json.dumps`` 对 NaN 和 inf 都吐非
    标准字面量，但调用方保证 ≥3 内点，这条分支实际不可达——防御性口径。）
    """
    if not mask.any():
        return float("inf")
    return float(np.sqrt(np.mean(resid[mask] ** 2)))


def _refine_plane(points: np.ndarray, normal: np.ndarray, d: float, tol_m: float,
                  up: np.ndarray) -> tuple[np.ndarray, float, int, float]:
    """内点集 SVD 精修：返回 ``(法向, offset, 内点数, rms)``。

    - 候选模型内点 <3 → 原样返回（无法定义平面）；
    - **精修后内点 <3 → 回退 RANSAC 候选模型**（:func:`fit_ground_plane`
      docstring 承诺的兜底，旧实现漏写）：SVD 对近共线内点集给出的法向可能把
      内点全甩出 ``tol_m``，此时续用精修模型会让 rms 变成空集均值（NaN）。
      候选模型是 2000 次迭代的最优者、其内点必然 ≥3（三个采样点自身即在平面
      上），回退它至少保证四元组自洽。实际数据上该分支不可达（地面 RANSAC
      的最优模型内点常以千计），是防御性守卫。
    """
    resid = points @ normal + d
    mask = np.abs(resid) <= tol_m
    sel = points[mask]
    if int(sel.shape[0]) < 3:
        return normal, d, int(mask.sum()), _rms_of(resid, mask)
    center = sel.mean(axis=0)
    _, _, vt = np.linalg.svd(sel - center, full_matrices=False)
    n = vt[2]
    n = n / np.linalg.norm(n)
    if float(n @ up) < 0.0:
        n = -n
    d2 = -float(n @ center)
    resid2 = points @ n + d2
    mask2 = np.abs(resid2) <= tol_m
    if int(mask2.sum()) < 3:                      # 精修把内点甩没了 → 回退候选模型
        return normal, d, int(mask.sum()), _rms_of(resid, mask)
    return n, d2, int(mask2.sum()), _rms_of(resid2, mask2)


def fit_ground_plane(frame: LiDARFrame, *, max_slope_deg: float = 60.0,
                     ransac_iters: int = 2000, tol_m: float = 0.02) -> GroundPlane:
    """RANSAC 拟合地平面（深度相机坐标，y 向下、z 前向），LIDAR-SPEC §3。

    流程（纯 numpy，无 scipy；固定种子 ``np.random.default_rng(0)``，同输入必同输出）：

    1. :func:`backproject` 反投影有效像素（大画布先经 :func:`_strided_frame` 确定性
       步进，内参同步缩放，点集仍是精确子采样）；有效点 ``< 64`` → ``ValueError``。
    2. 每次迭代随机取 3 点（``replace=False``），两点边叉积得法向：模长 ~0 视为共线
       （退化样本，跳过不占模型分）；归一化后**定向为朝天**（``n·(0,-1,0) > 0``，深度
       系 y 向下故负 y 为天），并施加斜坡约束
       ``n·(0,-1,0) >= cos(max_slope_deg)``——不满足的候选直接作废（§3）。
    3. 按 ``|n·p + d| <= tol_m`` 数内点，保留最优（严格大于才替换 → 并列留先者，
       确定性 tie-break）。
    4. 在全量点上用最优模型的内点集做 SVD 精修（均值中心化后取最小奇异向量为法向），
       重定向、重算内点与 ``rms = sqrt(mean(res²))``（精修后内点 <3 → 回退 RANSAC 模型）。

    返回 :class:`GroundPlane`：法向**朝天**（故 ``offset`` = 相机离地高 h，见
    :func:`camera_env_from_plane`）、内点数与 RMS（E5 平面质量，抽样子集口径）。

    ⚠ ``max_slope_deg`` 约束的是「平面相对水平面的倾角」，不是相机俯仰本身：
    平坦地面上该倾角 = ``arccos(cos p·cos r)``（相机俯仰+横滚的合成），俯角大时
    天花板/墙面会被正确地拒绝。
    """
    search = _strided_frame(frame)
    points, _ = backproject(search.depth_m, search.fx, search.fy, search.cx, search.cy)
    total = int(points.shape[0])
    if total < _MIN_POINTS:
        raise ValueError(
            f'有效点仅 {total} 个（< {_MIN_POINTS}），无法可靠拟合地平面'
            f'（{frame.source or "未标注来源"}）'
        )
    if points.shape[0] <= _SEARCH_CAP:
        subset = points
    else:
        idx = np.linspace(0, points.shape[0] - 1, _SEARCH_CAP).astype(np.int64)
        subset = points[idx]

    cos_max = math.cos(float(max_slope_deg) * _DEG2RAD)
    up = np.asarray(_UP_DEPTH, dtype=np.float64)
    rng = np.random.default_rng(0)                 # 固定种子（§3）
    count = int(subset.shape[0])
    best_n: np.ndarray | None = None
    best_d = 0.0
    best_inliers = -1
    for _ in range(int(ransac_iters)):
        tri = rng.choice(count, size=3, replace=False)
        p0, p1, p2 = subset[tri[0]], subset[tri[1]], subset[tri[2]]
        normal = np.cross(p1 - p0, p2 - p0)
        norm = math.sqrt(float(normal @ normal))
        if norm <= 1e-12:                          # 三点共线/重合：退化候选
            continue
        normal = normal / norm
        if float(normal @ up) < 0.0:               # 定向为朝天
            normal = -normal
        if float(normal @ up) < cos_max:           # 斜坡约束（§3）
            continue
        d = -float(normal @ p0)
        residuals = subset @ normal + d
        inliers = int(np.count_nonzero(np.abs(residuals) <= tol_m))
        if inliers > best_inliers:
            best_inliers = inliers
            best_n = normal.copy()
            best_d = d
    if best_n is None:
        raise ValueError(
            f'RANSAC 未找到满足斜坡约束（max_slope_deg={max_slope_deg}）的候选平面'
            f'（{frame.source or "未标注来源"}）'
        )

    normal, d, inliers, rms = _refine_plane(points, best_n, best_d, tol_m, up)
    return GroundPlane(
        normal=(float(normal[0]), float(normal[1]), float(normal[2])),
        offset=float(d),
        inliers=int(inliers),
        rms=float(rms),
    )


def depth_stats(frame: LiDARFrame, plane: GroundPlane) -> DepthStats:
    """深度帧统计（E5：CLI 报告 + scene 诊断；LIDAR-SPEC §3，附加便捷函数）。

    ``coverage`` = 有效像素（finite 且 > 0）比例；``inlier_ratio`` =
    ``plane.inliers`` / 有效像素数（与 :class:`GroundPlane` 同口径——fit 的抽样子集，
    故 ratio 是**子集内点比例**，全幅内点比例需另行统计）。附加函数而非契约签名，
    供 CLI/compose 复用；契约冻结的只是 :class:`DepthStats` 数据类本身。
    """
    depth = np.asarray(frame.depth_m, dtype=np.float64)
    if depth.ndim != 2:
        raise ValueError(f'期望 (H,W) 深度图，得到形状 {depth.shape}')
    valid = np.isfinite(depth) & (depth > 0.0)
    total = int(valid.sum())
    coverage = float(valid.mean()) if valid.size else 0.0
    inlier_ratio = float(plane.inliers) / float(total) if total else 0.0
    return DepthStats(coverage=coverage, inlier_ratio=inlier_ratio)


def _camera_from_frame(frame: LiDARFrame, env: CameraEnv) -> Camera:
    """由深度帧内参 + 环境量构造渲染用 :class:`~arcam.geometry.Camera`。

    ``feq_mm`` 从 ``frame.fx`` 反解（LIDAR-SPEC §3 备注的「深度相机与渲染相机同焦
    假设」）：arcam 的针孔像素焦距 ``F_px = (max(W,H)/2)·feq/K_LONG``（宽幅，长边）
    或 ``F_px = (min(W,H)/2)·feq/K_SHORT``（非宽幅，短边）
    （``focal_px``，index.html:1043-1047 / artifact A §1.6）。反解即
    ``feq = F_px·K_edge/(half_edge)``，宽幅判定按画布长/短边比例 >= 4/3
    （与 ``focal_px`` 逐字同规则）。**同焦假设**：令渲染相机的像素焦距等于深度相机
    的 fx，即假定两者是同一颗（或同焦距）镜头——否则 place() 的水平/纵深会整体缩放
    失真。fx≠fy 的非方形像素场景只用 fx（place 只有单一 F_px），如实记录为假设。
    """
    height, width = int(frame.depth_m.shape[0]), int(frame.depth_m.shape[1])
    if width < 1 or height < 1:
        raise ValueError(f'深度帧尺寸非法：{width}x{height}')
    long_edge = max(width, height)
    short_edge = min(width, height)
    wide = long_edge / short_edge >= 4.0 / 3.0
    half_edge = (long_edge if wide else short_edge) / 2.0
    k_edge = K_LONG if wide else K_SHORT
    feq = float(frame.fx) * k_edge / half_edge
    # ARKit 直测量（pitch/roll/height）优先于平面解算量：同为深度侧输入，陀螺仪/
    # IMU 直测比几何拟合少一层假设；None 时回落到 env（平面解算）。
    pitch = frame.pitch_deg if frame.pitch_deg is not None else env.pitch_deg
    roll = frame.roll_deg if frame.roll_deg is not None else env.roll_deg
    height_m = frame.height_m if frame.height_m is not None else env.height_m
    return Camera(width=width, height=height, height_m=float(height_m),
                  feq_mm=float(feq), pitch_deg=float(pitch), roll_deg=float(roll))


def sample_subject(frame: LiDARFrame, env: CameraEnv, u: float, v: float) -> tuple[float, float]:
    """足底屏幕位置 (u, v) → (lateral_m, distance_m)（LIDAR-SPEC §3，E3）。

    构造临时渲染相机（:func:`_camera_from_frame`：feq 由 frame.fx 反解 +
    pitch/roll/h 取 ARKit 直测量优先、平面解算兜底），再调
    ``arcam.placement.place``（project 固定 ``y=-h`` 的解析逆，index.html:3184-3202 /
    artifact A §3.4）解出人物世界横移与纵深。place 返回 None（射线与地平面近平行或
    d<=0.05）→ :class:`ValueError`。返回值保留 place 的既有 clamp（d∈[0.5,20]、
    x∈[-6,6]，SPEC §5 如实保留项）——调用方需知这是"测得值"的口径。
    """
    cam = _camera_from_frame(frame, env)
    got = place(float(u), float(v), cam)
    if got is None:
        raise ValueError(
            f'place 在 ({float(u):.2f}, {float(v):.2f}) 无解'
            '（射线与地平面近平行或人到相机距离 <= 0.05m）'
        )
    return (float(got[0]), float(got[1]))


def occlusion_mask(frame: LiDARFrame, cam: Camera, placement: SpritePlacement,
                   *, margin_m: float = _DEFAULT_MARGIN_M) -> np.ndarray:
    """遮挡掩码：True = 该像素的真实几何在立绘公告板**之前**（应把立绘抠掉）。

    LIDAR-SPEC §3 / §5 T4（E4）。逐像素比较两个**沿光轴的深度（z 分量）**：

    - **公告板深度**：LIDAR-SPEC §3 冻结公式
      ``Zc_bill = place(足底像素, cam).d · cos(pitch)``——「眼平面」模型：把立绘
      当作贴在**眼高平面**（相机系 y=0）上、立于 place 解出的世界纵深 ``d`` 处的
      公告板。渲染相机为准：place 的内参（主点 ``(W/2, H/2)``、焦距 ``focal_px``）与
      姿态（pitch/roll/h）全部取自透传的 ``cam``（compose 侧已在 ``_merge_env``
      吸收 frame 的 ARKit 直测量，§0 优先级「显式 > 深度测出 > 标称」）；place 的
      既有 clamp 逐字保留（``d`` 夹 [0.5, 20]，index.html:3197）。**整个公告板共
      用一个标量深度**（足底锚点处那一个），不是逐像素重算——见下方「契约考据」。

      契约考据（§3 原文写 ``place(u,v).d·cos(pitch)``，即逐像素）：逐像素取
      place 会把*该像素视线与地面的交点*当作公告板深度，而公告板上方的像素，其
      地面交点比人物实际距离远（视线下探过头顶落到更远的草地），眼平面深度随之
      线性变深 —— 于是**公告板后方任何贴地立方体都会被判成遮挡**（方块自身深度
      恒小于其像素的地面交点眼平面深度），直接违反 §5 T4「方块在公告板后 → 全
      False」。故取 (u,v) = **立绘足底锚点**（贴地摆位时即 ``ground_placement``
      的 pb）：此时 place 解出的正是人物自身纵深 D，全公告板共用
      ``Zc_bill = D·cos(pitch)``，且脚底地面（轴深 ``h·sin p + D·cos p``）恰在其
      后 → 脚底地面不误判为遮挡，与 T4 三个子项全部自洽。此取舍在交付报告里向
      Orchestrator 报备。
    - **实测深度**：裸 ``depth`` 值本身。Apple 语义（2026-10-03 仲裁，见
      :func:`backproject` docstring）：depth 即相机平面轴向 z 分量，无需也不该做
      ``cosθ`` 折半——那是沿光线长度（径向）口径，与真机相反。

    判据：像素在公告板矩形内（按 ``placement.rot`` 做精确旋转矩形测试——公告板外
    的像素不判）且深度有效，且 ``Zc_lidar < Zc_bill - margin_m`` → True。
    margin 区间内不算遮挡（§3）。足底无解 / 极端仰角（cos(pitch)<=0，公告板整体
    在相机上方）→ 不判（全 False）。

    ⚠ 前置条件：``frame`` 须已 ``align_to_canvas`` 到 ``cam.width/height``（深度
    与渲染同画布像素坐标）；渲染相机（focal_px / 主点 / pitch / roll）用于公告板
    几何，深度帧只提供逐像素 z，两者像素坐标对齐后即可比。
    """
    depth = np.asarray(frame.depth_m, dtype=np.float64)
    if depth.ndim != 2:
        raise ValueError(f'期望 (H,W) 深度图，得到形状 {depth.shape}')
    height, width = int(depth.shape[0]), int(depth.shape[1])
    rows, cols = np.mgrid[0:height, 0:width]
    z_lidar = np.where(np.isfinite(depth) & (depth > 0.0), depth, np.nan)

    # 公告板矩形（精确旋转矩形）：局部 (0, +h/2) = 图底边 = 足底锚点
    rot = float(placement.rot)
    cr, sr = math.cos(rot), math.sin(rot)
    du = cols - float(placement.u)
    dv = rows - float(placement.v)
    lx = du * cr + dv * sr
    ly = -du * sr + dv * cr
    inside = (np.abs(lx) <= float(placement.w) / 2.0 + 0.5) & \
             (np.abs(ly) <= float(placement.h) / 2.0 + 0.5)

    # 公告板深度 Zc_bill = place(足底像素).d·cos(pitch)（LIDAR-SPEC §3 冻结公式）。
    # 渲染相机为准：place 的内参（主点 (W/2, H/2)、焦距 focal_px）与姿态
    # （pitch/roll/h）全部取自透传的 cam —— compose 侧已在 _merge_env 把 frame 的
    # ARKit pitch/roll/h 直测量并进 effective Camera（§0「显式 > 深度测出 > 标称」），
    # 故此处不再重复取 frame.*。place 的既有 clamp 逐字保留（d 夹 [0.5, 20]）。
    p = float(cam.pitch_deg) * _DEG2RAD
    feet_u = float(placement.u) - (float(placement.h) / 2.0) * sr
    feet_v = float(placement.v) + (float(placement.h) / 2.0) * cr
    feet = place(feet_u, feet_v, cam)
    if feet is None or math.cos(p) <= 0.0:
        return np.zeros((height, width), dtype=bool)
    world_d = float(feet[1])                       # place 的既有 clamp [0.5, 20]
    zc_bill = world_d * math.cos(p)                # 眼平面深度：全公告板同一标量

    occluded = inside & np.isfinite(z_lidar) & (z_lidar < zc_bill - float(margin_m))
    return np.asarray(occluded, dtype=bool)


def camera_env_from_plane(plane: GroundPlane, roll_deg: float | None = None) -> CameraEnv:
    """地平面 → 相机高度 / 俯仰 / 横滚（LIDAR-SPEC §3，T2 闭环核心）。

    **平面 ↔ 姿态的数学**（全部在单位法向 + 平面方程上闭合，T2 合成闭环自证）：

    深度相机坐标（y 下、z 前、光轴 = z）里，设 arcam 相机的 pitch ``p``、roll ``r``
    （>0 俯视 / 画面顺时针，与 ``project`` 同源，index.html:1048-1057）。把世界 +Y
    （地面朝天方向）经 project 的旋转链（先俯仰、后滚转，绕相机系 x 轴与 z 轴）
    映到光学系，得**地面法向（深度系）**::

        n_d = (cos p·sin r,  −cos p·cos r,  −sin p)

    锚点校验：p=r=0 → n_d=(0,−1,0)（朝天，与 §3 的 RANSAC 约束基准一致）；p=90
    （直视地面）→ n_d=(0,0,−1)（相机看向 −y，朝天方向在光学系里是 −z）。

    **h 的导出**：平面在深度系满足 ``n_d·p + h = 0``。翻到 arcam 相机坐标（y 上，
    ``_to_arcam_camera_coords`` 只翻 y=x,z 不变，LIDAR-SPEC §1）后
    ``n_arcam·p_a + h = 0``，其中 ``n_arcam = (cos p·sin r, cos p·cos r, −sin p)``
    ——即**世界 +Y 在 arcam 相机坐标的单位向量**（§3）。**y 翻转只改 y 符号、是正交
    变换，平面方程常数项 offset 不变**（``n·p + offset = 0`` 经正交变换后写成
    ``(Rn)·(Rp) + offset = 0``，常数项不动），故两个坐标系里 h 都是同一个
    ``plane.offset``：朝天法向下相机位于平面正侧，垂距（= 离地高）= ``offset``。

    **pitch/roll 的分解**：由 ``n_arcam`` 的两个三角关系：:

        roll  = atan2(n_arcam.x, n_arcam.y)                    # 已知 roll 时跳过
        pitch = atan2(−n_arcam.z,  a.y)                        # a = 反旋 roll 后的法向
        a.y = n_arcam.x·sin r + n_arcam.y·cos r                # 绕 z 反旋（y 上约定）

    齐次解（roll 未知）与固定 roll 解（先反旋再解 pitch）在无噪时完全一致；固定
    roll 时 roll 原样回带（CameraEnv.roll_deg = 传入值），pitch 只用剩余分量解。
    """
    n = np.asarray(plane.normal, dtype=np.float64).reshape(3)
    norm = float(np.linalg.norm(n))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError(f'平面法向非单位/非有限，无法解环境量：{plane.normal!r}')
    n = n / norm
    # 深度相机坐标 → arcam 相机坐标（只翻 y）：全仓唯一翻转点（LIDAR-SPEC §1）
    n_arcam = _to_arcam_camera_coords(n.reshape(1, 3))[0]
    # offset 对正交变换不变 → 两个坐标系里 h 都是 plane.offset（见 docstring 推导）
    height = float(plane.offset)
    if roll_deg is None:
        roll = math.atan2(float(n_arcam[0]), float(n_arcam[1]))
    else:
        roll = float(roll_deg) * _DEG2RAD
    cr, sr = math.cos(roll), math.sin(roll)
    a_y = float(n_arcam[0]) * sr + float(n_arcam[1]) * cr   # 反旋 roll 后的 y 分量
    a_z = float(n_arcam[2])
    pitch = math.atan2(-a_z, a_y)
    return CameraEnv(
        height_m=height,
        pitch_deg=pitch * _RAD2DEG + 0.0,
        roll_deg=roll * _RAD2DEG + 0.0,
        plane=plane,
    )
