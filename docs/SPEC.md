# arcam —— Python 移植接口契约（SPEC）

> 本文件是并行实现的唯一宪法。所有人（含 Agent）按本契约写代码；原版事实以
> `oss-teardown/lhso-20261003/artifacts/A-*.md` 为准（含逐字 JS 源码与 file:line）。
> 上游 commit：`4a9ed047943b932dc8c130f59869f9c9a6bf1c24`（main，2026-09-28）。

## 0. 产品定义

浏览器原版是「纯静态、零依赖的 AR 取景合成工具」：把二次元立绘按透视投影方程摆进
摄像头实时画面（桌面端是导入图片）。Python 移植做**同一套投影/合成管线的离线版**：
CLI 把立绘合成进照片/视频帧，并在位姿轨道下逐帧渲染。scene.json 与原版 100% 同构
（同名字段、同顺序、同单位、同取整语义），使「Python 出的图能和原版工具互认配方」。

不做：实时摄像头（Python 侧只接受已给定的 yaw/pitch/roll 参数，姿态数据源与版式
布局 Shell 都是浏览器专属）；MMD 3D 运行时（three.js 生态，Python 侧记录接口契约
与相机矩阵交叉验证，见 §6）。

## 1. 共享约定（违反即打回）

1. **环境**：`.venv/bin/python`（uv + Python 3.14）。测试：`.venv/bin/python -m pytest`。
   依赖只允许 numpy / pillow / pytest；onnxruntime 仅 matting 模块懒加载（`import` 放函数内）。
2. **图像表示**：RGB `uint8 (H,W,3)`、RGBA `uint8 (H,W,4)`；计算用 `float64` 且已归一到
   `[0,1]`。存/取用 `numpy.clip` 后 `*255+0.5` 再 `astype(uint8)`。
3. **角度**：公开 API 一律**度**（对应原版 `S.pitch/S.roll/S.feq`），函数内部转弧度。
4. **取整**：一律 `arcam.numeric.js_round`（JS `Math.round` 半向 +∞）。**禁止用内置
   `round()`**（银行家舍入）。`toFixed` 语义用 `js_to_fixed` / `js_fixed_str`。
5. **坐标约定**（index.html:1048，见 artifact A §1.3）：
   世界 Y 向上、地面 `y=0`、Z 朝观察者；相机 `(0, S.h, 0)`；画布 v 向下、主点
   `(W/2, H/2)`；`pitch>0` 俯视、`roll>0` 画面顺时针。
6. **溯源**：每个公开函数的 docstring 必须注明对应 JS 的 `index.html:<line>` /
   `cutout.js:<line>`（从 artifact 里抄）。
7. **风格**：公开函数完整类型标注；模块级 docstring 一段；不写实现过程注释，只写
   「为什么」与「原版行为约束」；禁止 print 调试；禁止 `# noqa`。
8. **文件互斥**：只创建/修改本契约 §2 分配给你的文件。需要共享类型时 import 自
   `numeric.py` / `geometry.py`，不得自建副本。

## 2. 模块划分与归属（互斥分区）

| 归属 | 文件（src/arcam/ 下） | 测试（tests/ 下） |
|---|---|---|
| 已就绪（编排者） | `numeric.py`、`__init__.py`、`pyproject.toml` | — |
| **Agent P1 投影** | `geometry.py`、`placement.py` | `test_geometry.py`、`test_placement.py` |
| **Agent P2 取景/光照/立绘层** | `frames.py`、`lighting.py`、`sprite.py` | `test_frames.py`、`test_lighting.py`、`test_sprite.py` |
| **Agent P3 合成/配方/色调** | `compose.py`、`scene.py`、`tone.py` | `test_compose.py`、`test_scene.py`、`test_tone.py` |
| **Agent P4 抠图** | `matting.py` | `test_matting.py` |
| **Agent P5 视频/CLI/文档** | `video.py`、`cli.py`、`__main__.py`、`README.md` | `test_video.py`、`test_cli.py` |
| 编排者最后 | — | `test_integration.py` |

## 3. 接口契约（签名冻结，语义以 artifact 为准）

### 3.1 geometry.py（P1）

```python
K_LONG  = 17.3064   # 0.8*43.266/2，index.html:1041
K_SHORT = 12.9798   # 0.6*43.266/2

@dataclass(frozen=True)
class Camera:
    width: int; height: int      # 画布像素 cv.width/cv.height
    height_m: float              # S.h 相机离地高（米）
    feq_mm: float                # S.feq 等效焦距（毫米）
    pitch_deg: float             # >0 俯视
    roll_deg: float              # >0 画面顺时针

def focal_px(cam: Camera) -> float
# index.html:1043-1047。L=max(W,H), Sd=min, wide = L/Sd >= 4/3
# F = (L/2)*feq/K_LONG if wide else (Sd/2)*feq/K_SHORT

def project(x: float, y: float, d: float, cam: Camera) -> tuple[float, float] | None
# index.html:1048-1057。Zc<=0.05 → None。返回 (u,v) 画布像素。

def place(u: float, v: float, cam: Camera) -> tuple[float, float] | None
# index.html:3186-3198。project 固定 y=-h 的**解析逆**：解 (x, d)（米）。
# 退化（den<=0 或 d<=0）→ None。输出 x 夹 ±6（原版 place 行为，如实保留）。
```

等价公式（artifact A §1 摘要，实现必须逐字一致）：
```
p=pitch·π/180, r=roll·π/180
Zc = -y·sin p + d·cos p
Yc = -y·cos p - d·sin p
Xr =  x·cos r - Yc·sin r
Yr =  x·sin r + Yc·cos r
u = W/2 + F·Xr/Zc ; v = H/2 + F·Yr/Zc
place: A=(u-W/2)/F·cos r + (v-H/2)/F·sin r ; B=-(u-W/2)/F·sin r + (v-H/2)/F·cos r
      d = h·(cos p - B·sin p)/(sin p + B·cos p) ; x = A·(h·sin p + d·cos p)
```

### 3.2 placement.py（P1）

```python
SHOTS: dict[str, float] = {'全身':1.00,'膝上':0.72,'腰上':0.47,'胸上':0.28,'头肩':0.20}
FRAMES: dict[str, float] = {'自由':0.0,'1:1':1.0,'4:5':0.8,'16:9':16/9,'9:16':9/16,'满幅':-1.0}

@dataclass(frozen=True)
class Subject:
    height_m: float   # S.Hp 人物身高（米）
    distance_m: float # S.d
    lateral_m: float  # S.x

@dataclass(frozen=True)
class SpritePlacement:
    u: float; v: float   # 立绘外接矩形中心（画布像素）
    w: float; h: float   # 外接矩形尺寸（像素），w = h*charAspect
    rot: float           # 弧度，绕图心

def ground_placement(char_rgba: np.ndarray, subj: Subject, cam: Camera,
                     char_scale: float = 1.0, rot_basis: str = 'world') -> SpritePlacement | None
# index.html:2789-2840。yb=(1-SHOTS[shot])·Hp；pb=project(x, yb-h, d)；pt=project(x, Hp-h, d)；
# span=hypot(pt-pb)；h=span*char_scale；锚点=pb；rot: rot_basis=='frame' → 0  else atan2(ux,-uy)。
# shot 缺省 '全身'。None 情形：无图/相机后方/span<2。

def free_placement(char_rgba, subj, cam, size_pct: float = 60.0, rot_deg: float = 0.0) -> SpritePlacement | None
# index.html:2945-2981（freeRaw/freePlacement）。按画面高度 size_pct% 摆，位置 freeP。

def current_placement(char_rgba, subj, cam, *, mode='ground', **kw) -> SpritePlacement | None
# index.html:2981。mode 分派。

def auto_fix_x(char_rgba, subj, cam, *, mode='ground', **kw) -> tuple[float, str]
# index.html:2983-3025。返回 (new_x, status)：
# 'nochar'|'already'|'moved:<pct>'；ground 分支用数值导数一步解，clamp ±20（如实保留）。

def snap_to_edge(c: SpritePlacement, bar: int, rect: Rect, aspect: float,
                 rot: float) -> SpritePlacement
# index.html:2874-2895（bar 1上/2下/3左/4右）。edgeGeom 旋转后外接盒 bw/bh；
# fit 钳位两端不排序（图比框大时范围自然反过来）。rot 变化后 rotateBar 换边。
```

### 3.3 frames.py（P2）

```python
def view_rect(width: int, height: int) -> Rect
# index.html:951-997 的离线等价：无布局外壳时 = 整块画布（documented deviation）。

def frame_rect(frame_key: str, width: int, height: int) -> Rect
# index.html:998-1039。'满幅'(-1) → 整块画布；'自由'(0) → view_rect；
# 其它按宽高比在 viewRect 内取最大内接矩形（宽高浮点、原点 js_round 取整）。
```

### 3.4 lighting.py（P2）

```python
@dataclass(frozen=True)
class LightSample:
    levels: tuple[float, ...]   # 逐级亮度（measureRegion 的 N 级）
    mean: tuple[float, float, float]  # RGB 均值

def measure_region(rgb01: np.ndarray, region: Rect, levels: int = 32) -> LightSample
# index.html:1212-1231。32 级亮度量化（注意 clampRegion 的 sw>W 越界零填充语义，
# 见 artifact A §2.2.7 注释：不要"改进"成 clamp）。

def light_levels(sample: LightSample) -> tuple[float, float]        # (lo, hi) 或按源码
def char_lum(char_rgba: np.ndarray) -> float                        # index.html:1262-1283
def bake_lit(char_rgba: np.ndarray, sample: LightSample, canvas: tuple[int, int]) -> np.ndarray
# index.html:1285-1447：按立绘自身亮度分桶，把立绘映射到采样到的环境亮度。
# 三条 LUT 边界的取整必须 js_round。
```

### 3.5 sprite.py（P2）

```python
def contact_shadow_layer(char_rgba: np.ndarray, c: SpritePlacement,
                         ambient: tuple[float,float,float] | None) -> np.ndarray
# index.html:3027-3145。SH_BAND=0.085（取底部 8.5% alpha 逐列最大值=落地轮廓）、
# SH_THICK=0.24、SH_ALPHA=0.42；纵向高斯带、峰值压在接触线；颜色=环境色暗化版。
# 返回与画布同尺寸的 RGBA 层（Python 侧无画布句柄，改为返回层，由 composite 叠加）。

def composite_sprite(base_rgb01: np.ndarray, char_rgba01: np.ndarray, c: SpritePlacement,
                     *, shadow: bool = True, ambient: tuple[float,float,float] | None = None,
                     shadow_layer: np.ndarray | None = None) -> np.ndarray
# index.html:3146-3166 drawCharTo 的 Python 等价：平移→旋转→先铺阴影→再画立绘
# （锚点=立绘外接矩形中心，translate(c.u,c.v) 后 drawImage 于 (-c.w/2,-c.h/2,c.w,c.h)）。
# 缩放用双线性（PIL BICUBIC 亦可，记录与原版 imageSmoothingQuality='high' 的已知偏离）。
# 返回新画布（不改入参）。
```

### 3.6 compose.py（P3）

```python
@dataclass(frozen=True)
class ComposedShot:
    shot: np.ndarray      # RGB uint8 出图
    rect: Rect            # 实际裁切框
    light: LightSample | None
    char_layer: np.ndarray | None   # RGBA uint8 立绘层（照片路径也有；素材包用）

def compose_shot(src_rgb01: np.ndarray | None, char_rgba01: np.ndarray | None,
                 subj: Subject, cam: Camera, *, frame_key: str,
                 light: LightSample | None = None, mode: str = 'ground',
                 size_pct: float = 60.0, char_scale: float = 1.0,
                 rot_basis: str = 'world', tone: bool = False) -> ComposedShot
# index.html:3673-3701 composeShot 六步：R=frameRect → 铺源帧 → 采光（light 优先）
# → drawCharTo（TONE_ON=!!src 时先 tone 匹配）→ 小数偏移裁切 → 返回。
# src 为 None（仅立绘层路径）：不铺帧、不采现场光（light 参数给才用）。

def compose_char_layer(char_rgba01: np.ndarray, subj: Subject, cam: Camera,
                       *, light: LightSample | None = None, mode: str = 'ground',
                       size_pct: float = 60.0, char_scale: float = 1.0,
                       tone: bool = False) -> tuple[np.ndarray, Rect]
# index.html:3709-3712 buildCharCanvas：同变换、不铺帧的透明立绘层（RGBA uint8）。
```

### 3.7 scene.py（P3）

```python
def build_scene(rect: Rect, placement: SpritePlacement | None, subj: Subject, cam: Camera,
                *, frame_key: str, char_rgba01: np.ndarray | None, char_name: str,
                mode: str, edge_bar: int = 0) -> dict
# index.html:3729-3789 buildScene 的 1:1 同构：version/image/frame/camera/subject/
# ground/shot/charScale/edge/character/mmd。JSON key 顺序一致；mmd=None。
# 取整语义：image.width=js_round(R.w) 等；camera.focalPx=js_to_fixed(...,2)；
# centerPixel/sizePixel=toFixed(2)；rotationDeg=rot*180/π toFixed(3)。

def save_scene(scene: dict, path, *, indent: int = 2) -> None   # json.dump(..., indent=2)
def load_scene(path) -> dict                                 # 只读 json.load + 基本校验
```

### 3.8 tone.py（P3）

```python
def tone_matched_layer(layer_rgba01: np.ndarray, bg_rgb01: np.ndarray) -> np.ndarray | None
# index.html:3501-3620。六步：模型 bbox+均值(n<500→None) / 背景环带均值
# (pad=round(max(w,h)*0.2)+8) / Rec.601 亮度 P5/P95 直方图 / 均值平移(TONE_K=0.35)
# +范围重映射(kTone=0.3/0.4) / 边缘 3×3 高斯只降 alpha 羽化+RGB 混向背景 /
# 返回新层。只动 alpha>=8 的像素。
```

### 3.9 matting.py（P4）

```python
@dataclass(frozen=True)
class MattingResult:
    rgba: np.ndarray     # uint8 (H,W,4)，RGB 保留原图、alpha=蒙版
    fg: float            # 前景占比
    ms: float            # 推理耗时

def anime_cut(rgb01: np.ndarray, model_path: str | None = None,
              *, intra_op: int = 1, simd: bool = True) -> MattingResult
# cutout.js:670-747 animeCut。onnxruntime 懒加载；默认模型按顺序解析：
# 1) 参数 2) 环境 $ARCAM_ISNET_MODEL 3) 仓库 anime/isnet-anime-q8.onnx（若存在）
# 4) ~/.cache/arcam/isnet-anime-q8.onnx。都缺 → 抛 FileNotFoundError（带下载指引）。
# fg<0.02 → ValueError('没有检出前景')（对齐 index.html:2703 行为）。
# 预/后处理拆成模块级函数供单测：
def seg_preprocess(rgb01) -> tuple[np.ndarray, tuple[int,int,int,int]]  # (ox,oy,nw,nh)
def seg_postprocess(mask: np.ndarray, crop: tuple[int,int,int,int], shape: tuple[int,int]) -> np.ndarray
def keep_largest_blob(mask01: np.ndarray) -> np.ndarray
# cutout.js:619-747：长边等比→1024、居中贴方 + **边缘延展补边**（ext() 8 次 drawImage）、
# RGB、NCHW float32、/255 + ImageNet mean/std；lo=0.28/hi=0.46 窄带线性二值化；
# JS 不 sigmoid（q8 模型自带）；软概率图回贴；erode1/feather2 在调用方（softenEdge）。
# 模型输入名 'img'（用 sess.get_inputs() 读）。模型 sha256 cb5f7850…77e3（文档记录用）。
```

### 3.10 video.py（P5）

```python
@dataclass(frozen=True)
class PoseKey:
    t: float; lateral_m: float; distance_m: float; pitch_deg: float; roll_deg: float
    height_m: float | None = None    # 相机高，None 沿用轨道默认
    feq_mm: float | None = None
    subject_height_m: float | None = None
    char_scale: float | None = None
    size_pct: float | None = None    # 自由模式
    mode: str | None = None

@dataclass(frozen=True)
class PoseTrack:
    width: int; height: int; fps: float
    height_m: float; feq_mm: float; subject_height_m: float
    char_scale: float; size_pct: float; mode: str
    frame_key: str
    keys: list[PoseKey]

def load_track(path) -> PoseTrack       # JSON schema 见 README
def save_track(track: PoseTrack, path) -> None
def render_track(bg_source, track: PoseTrack, char_rgba01: np.ndarray,
                 out_dir, *, tone: bool = False) -> list[Path]
# bg_source: 单张 RGB（静止背景）或帧目录（逐帧命名 frame_000001.png…）。
# 键之间线性插值；每帧走 compose_shot；写 PNG 序列 frame_%06d.png。
# 轨道超出帧数：bg 不足时最后一帧/背景循环由实现定义并写进 docstring。
```

### 3.11 cli.py（P5）

```
arcam render PHOTO CHAR -o OUT.png [--x --d --h --hp --feq --pitch --roll --shot
        --char-scale --frame --rot-basis --mode --size-pct --tone --scene PATH]
arcam char-layer CHAR -o OUT.png [同上放置参数]            # RGBA 立绘层
arcam bundle PHOTO CHAR -o OUT.zip [同上放置参数]           # shot.jpg(q0.95)+scene_*.json+character_*.png
arcam matte IMG -o OUT.png [--model PATH]
arcam video TRACK.json CHAR -o OUTDIR [--bg IMG_OR_DIR --tone]
arcam check TRACK.json                                     # 轨道合法性自检
```
- JPEG q0.95；zip 用 ZIP_DEFLATED 且 date_time=(1980,1,1,0,0,0) 固定（确定性复现）。
- 所有命令缺立绘/缺背景给出人话报错；退出码 0/1/2。

## 4. 测试要求（每个 Agent 自证）

1. 先跑 `.venv/bin/python -m pytest tests/<你自己的文件> -q`，**必须全绿**再交付。
2. P1 投影 golden：手工按 §3.1 公式推导 ≥12 组用例（覆盖 pitch>0/<0、roll≠0、
   wide/非 wide 画布、相机后方 None、span<2 分支），断言写计算过程注释。
3. P1 交叉验证：实现 4×4 view/proj 矩阵管线（artifact C §3.2 逐字矩阵）投同一世界点，
   与 `project` 偏差 <1e-9 px（这是原版作者 2.27e-13 声明的等价复现）。
4. P1 round-trip：`place(u,v)` 解出 (x,d) 再 `project` 回投，误差 <1e-9 px（原版声明 3.6e-15）。
5. P2/P3 合成测试：用程序生成立绘（几何图形+alpha）与背景，断言像素级性质：
   锚点在 (c.u,c.v)、尺寸 (c.w,c.h)、阴影在立绘之前、frame 裁切偏移、light 复用。
6. P3 scene 断言：字段全存在、顺序一致（list(d.keys()) 逐项比）、数值 toFixed 语义。
7. P4：预处理/后处理/连通域单测不依赖模型；模型存在时跑端到端（skipif 无模型）。
8. P5：CLI 冒烟（render/bundle/matte-skip/video）+ 轨道插值单测 + zip 可解包且三件套齐全。
9. 任何测试不得写临时文件到仓库外；用 tmp_path。

## 5. 已知偏离与如实保留项（写进各模块 docstring，不许"顺手修"）

- `S.x` clamp：`place` ±6（index.html:3198）vs `autoFix` ±20（index.html:3020）——都保留。
- `lightRegion` 返回画布坐标而 `sampleLight` 用视频尺寸 clamp（index.html:1195）——离线
  同尺寸，实现里写成显式断言，不静默通用化。
- `clampRegion` 的 `sw>W` 越界零填充语义（index.html:1244-1251）不得改成 clamp。
- JS `Math.round` 半向 +∞ vs Python 银行家舍入 → 已由 `js_round` 统一。
- 缩放滤波：原版 `imageSmoothingQuality='high'` 未指定具体核 → 双线性，README 记录。
- viewRect 的布局外壳（滑块条遮挡、转屏）属浏览器版式，离线取整块画布。

## 6. camera 数学交叉验证参考（artifact C §3）

原版 3D 运行时（mmd.js:837-858）用 4×4 view/proj 矩阵拼相机，作者实测与 2D `project()`
偏差 2.27e-13 px、与 buildPlacement 落位差 0.00 px。P1 的 test_geometry.py 应包含这条
等价性测试：世界点 (X,Y,Z) → 相机系 (X, Y-h, Z) → 矩阵 → NDC → 屏幕像素，与
`project(X, Y-h, Z, cam)` 同参数比对。
