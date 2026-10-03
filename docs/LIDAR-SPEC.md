# LIDAR-SPEC — iPhone 激光雷达深度接入 arcam 的接口契约

> 上游：`oss-teardown/lidar-research.md`（调研 Agent 产出，格式/路径以此为准，冲突时以它为准）。
> 本文件冻结签名与语义；实现前必读。总体约定同 `docs/SPEC.md` §1（js_round、类型标注、
> docstring 溯源、文件互斥）。测试用 `.venv/bin/python -m pytest`。

## 0. 需求定义（"支持调用 iPhone 的激光雷达，获取环境数据"的离线等价物）

arcam 原管线用**标称参数**摆人（`--h 1.6 --pitch 0 --d 4`）。LiDAR 接入后，深度数据
可以**测出**这些量，并把遮挡关系补全：

| # | 环境量 | 离线获取方式 | 用途 |
|---|---|---|---|
| E1 | 相机高度 `h` | 地面平面拟合（plane offset） | 覆盖 S.h |
| E2 | 俯仰 `pitch` | 地面平面法向（+ 已知/估计 roll） | 覆盖 S.pitch |
| E3 | 人物距离 `d` | 在立绘足底屏幕位置取深度、按平面几何反解世界距离 | 覆盖 S.d |
| E4 | 遮挡 | 比「立绘公告板深度」与每像素深度，在前者后方的像素从 alpha 抠掉 | 出图遮挡正确性 |
| E5 | 平面质量 | inliers / rms / 覆盖率 | CLI 报告 + scene 诊断 |

**优先级**：显式 CLI 参数 > 深度测出值 > 标称默认值（即 `--h` 给了 1.7 就用 1.7）。

**不做（边界）**：实时 iOS App（Swift）、多帧重建、语义分割、真机标定；
实时流接收（`arcam lidar serve`）仅做最小可用协议 + 模拟器测试。

## 1. 坐标与单位约定

- 深度单位**米**（float32），值域 (0, 120]；**无效值 = 0 或 NaN**（约定 0，读入统一 NaN）。
- 深度相机坐标系（**光学中心坐标，z 前向**）：`p = ((u-cx)/fx * z, (v-cy)/fy * z, z)`。
- **深度值 = 相机平面轴向 z（Apple 语义，2026-10-03 编排者终审）**：ARKit 文档
  "distance from the plane of the camera in meters"（ARFrame.sceneDepth /
  ARDepthData；调研 `oss-teardown/lidar-research.md` §2），官方点云样例反投影
  `worldPoint = (K⁻¹·T)·point(u,v,1)·depth` 也是把深度直接乘在 z=1 的射线上。
  ⚠ 曾有个别实现把 depth 当沿光线长度做 `cosθ` 折半，与真机语义相反——`backproject`
  有 `z ≡ depth` 的回归测试钉死，不许再改回径向。
- arcam 数学坐标系（y 上、z 前向）与深度相机坐标差一个 y 翻转：转换函数统一放
  `depth.py`（`_to_arcam_camera_coords`），**任何模块不得各自推导**。
- 平面在**深度相机坐标**里拟合、存储；`camera_env_from_plane` 内部转 arcam 坐标解
  h/pitch/roll。测试必须用合成场景闭环验证：给定 (h, pitch, roll) 生成地面 →
  backproject → fit → 恢复三者（容差见 §5）。

## 2. 文件格式与读取（`depth.py`）

```python
@dataclass(frozen=True)
class LiDARFrame:
    depth_m: np.ndarray                 # (H, W) float32，米，无效=NaN
    fx: float; fy: float; cx: float; cy: float
    roll_deg: float | None = None       # ARKit 给的横滚（没有就 None）
    pitch_deg: float | None = None      # ARKit 给的俯仰（没有就 None）
    height_m: float | None = None       # 已知相机高（真机世界原点在地面时）
    source: str = ''                    # 来源描述（文件名/URL/stream）

def load_depth(path: str | Path) -> LiDARFrame
def align_to_canvas(frame: LiDARFrame, width: int, height: int) -> LiDARFrame
```

支持格式（按优先级，调研 §7 分级）：
1. `*.png/*.tif` 16-bit：**毫米**（uint16，0=无效）——3D 扫描 App 导出的通行
   惯例（调研 §5：无官方规范，工程惯例如此）。
2. `*.npy`：float32 (H,W) **米**；fx/fy/cx/cy 缺省按**针孔满幅 FOV≈60°**估计
   （`fx=W/(2·tan(30°))`，cx=W/2, cy=H/2），并在 docstring 标注这是估计。
3. `*.depth`（magic `ARCD`）：小头 [magic 4B][W u32][H u32][fx f32][fy f32][cx f32]
   [cy f32][roll f32(NaN=无)][pitch f32][height f32][depth float32 H*W 米]——
   实时流与模拟器落盘格式。
4. `*.heic` 人像深度：**可选**（must-support，调研 §7）——pillow-heif ≥1.8 的
   `info['depth_images']` + `representation_type`（0 UNIFORM_INVERSE_Z /
   1 UNIFORM_DISPARITY / 2 UNIFORM_Z / 3 NON_UNIFORM_DISPARITY）判别，
   disparity 用元数据 focalPixelPitch 换算米；不可转 → RuntimeError 带指引。
5. `*.exr`：**可选**（must-support，调研 §7）——OpenEXR 3.5.1（有 macOS ARM64
   wheel）读 `Z`（或首个）通道为 float32 米；行序假定顶行优先（假设项，装 extra
   后首次使用核对真机 EXR）。
读入统一：0/负 → NaN；断言尺寸>0 且有效比例>2%（否则 ValueError 报"没有有效深度"）。

`align_to_canvas`：深度图分辨率通常低于照片（如 256×192 vs 3024×4032）。约定——
- 深度与照片**同一运营商视场**（Apple 人像深度就是照片视场的子采样），按
  最近邻缩放到画布尺寸；`fx,fy,cx,cy` 同比缩放。
- 深度视场 ≠ 照片视场（真机竖拍裁剪差异）属已知偏差，README 记录。

## 3. 平面与环境量（`plane.py`）

```python
@dataclass(frozen=True)
class GroundPlane:
    normal: tuple[float, float, float]   # 单位法向，深度相机坐标（y 向下）
    offset: float                        # normal·p + offset = 0（深度相机坐标，米）
    inliers: int
    rms: float

@dataclass(frozen=True)
class CameraEnv:
    height_m: float      # 相机离地高
    pitch_deg: float     # >0 俯视（arcam 约定）
    roll_deg: float      # 0=从法向分解；或采用外部给定 roll
    plane: GroundPlane   # 溯源

@dataclass(frozen=True)
class DepthStats:
    coverage: float      # 有效像素比例
    inlier_ratio: float

def backproject(depth_m, fx, fy, cx, cy) -> tuple[np.ndarray, np.ndarray]
# 返回 (points (N,3) 深度相机坐标, valid_mask)。仅有效像素。

def fit_ground_plane(frame: LiDARFrame, *, max_slope_deg: float = 60.0,
                     ransac_iters: int = 2000, tol_m: float = 0.02) -> GroundPlane
# RANSAC（固定种子，纯 numpy，无 scipy）。约束：normal·(0,-1,0) >= cos(max_slope)
# ——地面法向在深度坐标里应接近"朝上"（负 y）。点数 <64 → ValueError。

def camera_env_from_plane(plane: GroundPlane, roll_deg: float | None = None) -> CameraEnv
# roll 已知 → 先反旋再解 pitch；未知 → 从法向同时分解（arcam 相机 y 上约定，
# 关系：n_arcam ∝ (-cos p·sin r?, …) 以实现闭环测试为准，见 §5 测试 T2。
# offset → h：平面在 arcam 相机坐标满足 n·p + h = 0（世界+Y 单位向量 = n_arcam）。

def sample_subject(frame: LiDARFrame, env: CameraEnv, u: float, v: float) -> tuple[float, float]
# 在足底屏幕位置解人物世界距离与横移：调用 arcam.placement.place
# （y=-h 的解析逆，index.html:3186）→ (lateral_m, distance_m)。

def occlusion_mask(frame: LiDARFrame, cam: Camera, placement: SpritePlacement,
                   *, margin_m: float = 0.05) -> np.ndarray
# 布尔图 (H,W)：True = 该像素在立绘公告板**前面**（应抠掉立绘）。
# 公告板深度(u,v) = place(足底锚点).d·cos(pitch)（眼平面、公告板立在世界 Z=d 上，
# 全公告板共用一个标量——逐像素 place 会把头边上方的地面交点当公告板深度，导致
# 后方贴地方块被误判，见 plane.occlusion_mask docstring 契约考据）；
# Zc_lidar = 裸 depth（Apple 轴向下 depth 即相机平面 z，§1 终审，
# 不再做 cosθ 折半——那是径向口径，与真机相反）。
# 容差 margin_m 内不算遮挡；公告板外的像素不判（False）。
```

## 4. 合成接入（`compose.py`，追加参数，禁止破坏现签名）

```python
def compose_shot(..., depth: LiDARFrame | None = None,
                 env_overrides: dict | None = None) -> ComposedShot
# depth 非 None 且为照片路径时：
#   ① fit_ground_plane → CameraEnv；与显式 CLI 参数按 §0 优先级合并出
#      effective Camera/Subject（合并逻辑抽成 compose._merge_env，独立可测）；
#   ② 落位/采光/合成全用 effective 值；
#   ③ occlusion_mask 得到的区域在立绘层 alpha 上置 0（照片路径与立绘层路径都抠）。
# env_overrides 形如 {'h':1.7}：显式值优先于深度测出值。
# ComposedShot 新增字段：env: CameraEnv | None = None（诊断：CLI/测试打印）。
```

## 5. 测试门禁（每个实现 Agent 交付前自跑绿；编排者复跑）

- **T1 读取**：png16 毫米→米、0/负→NaN、npy 针孔估计、.depth magic 头往返、
  heic 未装时的报错文本；align 的等比缩放断言。
- **T2 平面闭环**（核心）：合成场景 `_synth_scene(h=1.55, pitch=12, roll=8)` 生成
  深度相机坐标地面点云（加噪 σ=1cm）→ backproject→fit→camera_env_from_plane →
  恢复 (h,pitch) 误差 <2cm / <1°，roll 从法向分解误差 <2°。
- **T3 距离采样**：T2 场景中在世界 (x=0.6, Z=5.0) 取点投影到屏幕 → sample_subject
  返回 (0.6, 5.0)，误差 <5cm。
- **T4 遮挡**：公告板前放一个方块（近 0.5m）→ mask 覆盖公告板被挡区；方块在公告板后
  → 全 False；公告板外像素全 False；**语义钉子**：backproject 的 z 分量恒等于裸
  depth（Apple 轴向下 depth 即 z；断言防止有人把「折半成光线长度」的实现塞回来）。
- **T5 合成接入**：合成照片+立绘+深度 → compose_shot(depth=…)落位与手算
  （h/pitch/d 全由 T2/T3 管线给出）一致；occlusion 区 alpha=0；无 depth 时行为与
  现版逐位相同（回归）。

## 6. CLI（`cli.py`）

```
arcam lidar inspect DEPTH [--json]         # 平面/h/pitch/roll/覆盖率/inliers，诊断
arcam render ... --depth DEPTH             # 深度驱动（E1-E4 全开；显式参数仍优先）
arcam lidar serve --port 7707 [--out DIR]  # 可选：实时流接收（协议见下）
arcam lidar record TRACK.json --port ...   # 可选：把实时流落成 .depth 帧序列
```

实时流协议（`lidar_live.py`，可选最小实现）：TCP，每帧 = `[u32 BE meta_len][JSON][u32 BE len][depth float32 米]`；
JSON 至少含 W,H,fx,fy,cx,cy,roll_deg,pitch_deg,height_m,timestamp。附 `simulator.py`
（发 T2 合成场景的循环）供测试单测。没网/没 App 不影响本模块单测（localhost 回环）。

## 7. 文档

README 增加「LiDAR 环境数据」节：怎么从 iPhone 拿到深度（人像 HEIC / 扫描 App 导出
16-bit PNG 序列 / .depth magic / 实时流）、`arcam lidar inspect` 与 `arcam render --depth`
用法、单位与对齐约定、局限（对齐视场、有效距离、玻璃/纯黑吸光体失效——以调研
`lidar-research.md` §6 为准）。`.ai/COLLABORATION.md` 追加本次并行分区记录。
