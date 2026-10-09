# FEATURE-SPEC — 与原版网站功能对等契约（v1.2.0 目标）

原版：https://filomenosnyder-stack.github.io/Let-her-step-out-of-the-screen-and-into-your-world/
（源码 `/Users/jianghaoda/.zcode/workspace/default/oss-teardown/repo/`，index.html 4077 行 +
cutout.js 837 + mmd.js 1107 + mmd/ 2.8MB；上游 commit `4a9ed04`，一切 file:line 绑定该 SHA）。

本契约冻结**新增功能**的接口与语义。既有 SPEC/LIDAR-SPEC/WEB-SPEC/SITE-SPEC 全部继续
有效；本文件与它们冲突时，以「更具体的对等语义」为准，但**不许**改动已冻结的数值口径
（js_round/toFixed/投影公式/scene.json 同构）。

## 0. 差距清单（原版功能 → 现状 → 本版目标）

| # | 原版功能（file:line） | 现状 | 去向 |
|---|---|---|---|
| A1 | 纯色底抠图 `cutSolidBg`（cutout.js:16-98） | 只有 AI 抠图 | `arcam/matte_tools.py` |
| A2 | 内容边界+裁边 `contentBounds`/`cropToCanvas`（cutout.js:246-276） | 无 | 同上 |
| A3 | 装图自动三件套 `autoPrepareChar`（index.html:2400-2440） | 无 | 同上 |
| A4 | 半身判定（底行整条不透明→不能贴地，index.html:2365-2368） | 无 | 同上 |
| A5 | HUD 六件套 `drawFrame/drawGrid/drawHorizon/drawFigure/hud`（index.html:1746-1972） | 无 | `arcam/hud.py` |
| A6 | 构图体检 11 条 `compositionHints`（index.html:1890-1937） | 无 | 同上 |
| A7 | 中英语言表 `TXT`+`t()`（index.html:600-830, 4047-4065） | 无 | `arcam/i18n.py` |
| B1 | 摄像头 `start()`（index.html:3899-3961） | 无 | webui + docs |
| B2 | 陀螺仪 `onOrient`（index.html:1059-1100） | 无 | webui + docs |
| B3 | 拍照快门 + `resumeCam` 三时机（index.html:3469-3493, 3985-3993） | 无 | webui + docs |
| B4 | 真·变焦 `setupZoom`（index.html:3855-3896） | 无 | webui + docs |
| B5 | 中英切换 + `data-t` + localStorage（index.html:4047-4071） | 无 | webui + docs |
| B6 | 全身/半身弹窗 `showKind`/`applyCharKind`（index.html:2500-2507, 2444-2454） | 无 | webui + docs |
| B7 | 拖动提示 `showTip`（index.html:2519-2540） | 无 | webui + docs |
| B8 | 软转屏 `syncTurn`+`rotSafeRange`（index.html:524-588, 4008-4037） | 无 | webui + docs |
| B9 | 修图按钮组（纯色底/AI/裁边/还原，index.html:2783-2790） | 部分（AI 有） | webui + docs |
| C1 | MMD 3D 层（mmd.js 1107 行 + mmd/ 2.8MB） | 无（README 记为"不做"） | Wave 2 |

**形态分工**：Python 核心拿 A1-A7（CLI 可用、可测、无设备依赖）；B* 进两个网页
（本机 webui + Pages docs，浏览器原生能力）；C1 Wave 2 单独排期。

## 1. `arcam/matte_tools.py`（F1 分区，新文件）

纯 numpy/PIL，零新依赖。所有函数**确定性**（同输入同输出），数值断言附手算注释。

```python
def cut_solid_bg(rgba01: np.ndarray, *, tol: float = 32.0,
                 feather: float = 1.2) -> float | None
```
- 语义 1:1 对译 cutout.js:16-98（四角均值容差 → 边框泛洪 → 二值化 → 3×3 均值羽化）。
- `rgba01`：**(H,W,4) float64 [0,1]**（与 arcam 其余模块一致的 01 口径）。
- 返回抠掉像素占比（0~1）；**不是纯色底返回 None**（四角 alpha 全 <10/255 → None；
  四角 RGB 与均值最大偏差 > tol → None；removed==0 → None）。
- 泛洪从**四条边框**整圈开始（不从四角点）；`seen`/`isBg` 两个数组（人物外圈不被抠）。
- 容差判定：`|ΔR|+|ΔG|+|ΔB| <= tol*3`（cutout.js:57，tol 是逐通道 tol）。
- 羽化：3×3 均值，半径 `max(1, round(feather))`，边界外像素不计入均值分母。
- ⚠ 与 AI 抠图（matting.anime_cut）**互不调用**；两条线并列，调用方选。

```python
def content_bounds(rgba01: np.ndarray, alpha_min: float = 8/255) -> tuple[int,int,int,int] | None
```
- 1:1 对译 cutout.js:246-263：alpha >= alpha_min 的像素 bbox，返回 `(x, y, w, h)`；
  无有效像素 → None。

```python
def auto_trim(rgba01: np.ndarray, alpha_min: float = 8/255) -> tuple[np.ndarray, tuple[int,int,int,int]] | None
```
- 1:1 对译 cutout.js:267-276 + cropToCanvas：按 content_bounds 裁到新画布；
  返回 `(裁后数组, (x,y,w,h))`； bounds 为 None 或裁后任一边 <4 像素 → None。

```python
def soften_edge(rgba01: np.ndarray, *, erode: int = 1,
                feather: int = 2) -> tuple[np.ndarray, bool]
```
- 对译 cutout.js 的 `softenEdge`：先 erode `erode` px（3×3 最小值，收掉残留底色），
  再 feather `feather` px（3×3 均值）。返回 `(新数组, changed)`。

```python
@dataclass(frozen=True)
class PreparedChar:
    rgba01: np.ndarray          # 处理后的立绘（01 口径）
    cut_ratio: float | None     # 纯色底抠掉的比例（没抠为 None）
    trimmed: bool               # 是否裁过边
    half_body: bool             # 底行整条不透明 → 半身（不能贴地）
    actions: tuple[str, ...]    # 人话动作列表（i18n key，如 'note.autoCut'）

def auto_prepare_char(rgba01: np.ndarray, *, enabled: bool = True) -> PreparedChar
```
- 1:1 对译 index.html:2400-2440 `autoPrepareChar` 的顺序与门控：
  ① `cut_solid_bg(tol=32, feather=1.2)`，占比须在 **[0.05, 0.85]** 才算数；
  ② `auto_trim`（从**上一步结果**出发，不叠加在原图上——原版同款"从基准重算"）；
  ③ 半身判定：**最底下一行**（裁完后）存在 alpha >= 8/255 的像素 → half_body；
  ④ `soften_edge(erode=1, feather=2)`。
- **风控**（原版逐字）：任一步抛错 → 该步静默跳过；抠+裁之后必须还剩内容
  （bbox 非 None 且 w,h >= 4）否则整体回退**原图**；`enabled=False` → 原样返回。
- 半身的用途：`half_body=True` 时**贴地模式无意义**（原版 §4.6：把腰以上当整个人
  放大）——上层（webui/CLI）应提示或拒绝 ground 模式；本函数只报告不拦截。

### webapp.py 新增端点（编排者合并时接线，契约冻结）

| 端点 | 方法 | 请求 | 响应 |
|---|---|---|---|
| `/api/prep` | POST JSON | `{target:"char", steps:["solid","trim","soften"]}` | `{ok, cutRatio, trimmed, halfBody, actions:[i18n key], image:dataURI}` |
| `/api/trim` | POST JSON | `{target:"char"}` | 同上（只裁边） |
| `/api/reset` | POST JSON | `{target:"char"}` | `{ok, image:dataURI}`（回 upload 时的原图） |

- `image` 为处理后的立绘 dataURI（PNG）；处理后立绘**替换会话内的 char**，后续
  render 直接用——与原版"修图线改 chBase/chCanvas"同语义。
- 错误路径统一 `{ok:false,error}` 人话（不 500）。

### CLI 新增（编排者合并时接线，契约冻结）

- `arcam prep CHAR [-o out.png] [--no-solid] [--no-trim] [--no-soften]`：
  跑 auto_prepare_char，打印 actions 与 half_body 结论（i18n：默认跟随
  `--lang`，缺省 zh）。
- 既有子命令新增 `--lang {zh,en}`（影响所有人话输出；缺省 zh，**不改数值行为**）。

## 2. `arcam/hud.py`（F2 分区，新文件）

PIL 绘制（`ImageDraw`），输出 **(H,W,3) uint8** 或直接画到传入 image 上。
所有几何走 `arcam.geometry.project`（不自己实现投影）；语义 1:1 对译原版。

```python
def frame_overlay(img: np.ndarray, rect: Rect, canvas_size: tuple[int,int]) -> np.ndarray
def grid_paths(cam: Camera, subject_x: float) -> list[GridPath]   # 供测试与 JS 侧对拍
def draw_grid(img, cam, subject_x) -> np.ndarray
def draw_horizon(img, cam, label: str) -> np.ndarray
def draw_figure(img, cam, subject: Subject) -> np.ndarray
def draw_hud_text(img, cam, subject, *, has_char: bool,
                  canvas_size, lang: str = 'zh') -> tuple[np.ndarray, list[str]]
def composition_hints(cam, placement, *, canvas_size) -> list[str]
```

冻结语义（原版逐字）：
- **drawFrame**（index.html:1746-1772）：框≈可见区时**不画**（早退条件逐字）；
  框外压暗 `rgba(0,0,0,.58)`；框线白 .9 / 2px；三分线白 .30 / 1.5px。
- **drawGrid**（1774-1824）：纵线 x∈[-6,6] 步长 1（d 从 0.3 到 26，301 点），
  横线 d∈[1,2,3,4,5,6,8,10,14,20]（x 从 -7 到 7，61 点）；x==0 与 d==5 是 major；
  两遍绘制（先黑描边 .6：major 7px/其余 5px；再青色：major `rgb(120,255,255)` 3px、
  其余 `rgba(90,225,255,.9)` 1.8px）；横线标签 `bold 20px`、位置 `project(-1.4,-h,d)`
  偏移 (+8,-8)，黑描边 4px；`q.v` 出画布不画。
- **drawHorizon**（1826-1837）：`F·(t·cos r + tan p·sin r)` / `F·(t·sin r − tan p·cos r)`
  （t=±12），红 `rgba(255,120,120,.8)` 虚线 [12,8] 2px；标签在 t=−0.22 处 +10,−10。
- **drawFigure**（1839-1876）：关节比例 TOP/HEAD/CHIN/NECK/SHLD/WAIST/HIP/KNEE/
  ANKLE/SOLE 逐字；SW=0.105Hp、HW=0.072Hp、FW=0.048Hp；骨色 B=.95/L=.75
  `rgb(255,212,121)`；头椭圆 rx=ry*0.72；足底圆点 `rgb(120,255,180,.95)` r=7。
- **compositionHints**（1890-1937，11 条，全部并列无优先级）：
  地平线位置 hr=hv/H∈[0.42,0.58]；pitch>40 压扁；pitch<−35 拉长；整块出画（早退）；
  三分线 near>0.11；fill>0.92 顶满；fill<0.10 偏小；cover≥0.95 且 ground 且
  cutV<hv−H*0.03 浮空。**删掉的 4 条（反解身高/眼高差）不许复活**（1939-1955 有据）。
- **hud**（1957-1972）：三行读数（pitch/roll；W×H/focal；人物占画面高%或
  behind/nochar 警告）+ hints 行；警告色与原版 `.warn` 一致。
- ⚠ 出图链路（compose_shot）**不画** HUD——原版"那些只在实时预览的 draw() 里画"
  （index.html:3487-3490 注释）。HUD 只服务预览/`--overlay` 显式开关。

## 3. `arcam/i18n.py`（F2 分区，新文件）

```python
LANGS = ("zh", "en")
DEFAULT_LANG = "zh"
def t(key: str, *args, lang: str = DEFAULT_LANG) -> str   # {0} {1} 占位符
def table() -> dict[str, tuple[str, str]]                 # 全表（测试覆盖率用）
```
- 表 = 原版 `TXT`（index.html:600-830）**逐条搬运**（约 120 条，数组形式
  [中文, English]）；缺 key → 返回裸 key（原版行为，不静默漏翻）。
- `t()` 的 `{0}` 替换用 str.format 语义等价手写（不许引异常）。
- 数字与单位（1.60m / 23mm）不翻——表里就不含它们。

## 4. webui / docs 网页功能（F3/F4 分区）

两个前端**同一份功能清单**，各自实现（webui 调 /api/*，docs 纯 JS）：

| 功能 | 契约 |
|---|---|
| 摄像头 | `start()` 1:1：iOS `DeviceOrientationEvent.requestPermission()` **第一个** await（手势上下文，index.html:3902-3909）；`getUserMedia({video:{facingMode:{ideal:'environment'},width:{ideal:1920}},audio:false})`；fit() 用 **cover**（max 缩放）铺满**物理视口**；三种失败分开报（insecure/denied/other） |
| 陀螺仪 | `onOrient` 1:1：beta/gamma → pitch/roll 换算 + 平滑 + `screen.orientation.angle` 补偿；无传感器时保留手拖 |
| 拍照 | 快门 = 当前视频帧进合成（`buildShotCanvas` 语义：先铺帧再叠立绘，同取景框）；`resumeCam` 挂 **visibilitychange/focus/pageshow** 三时机 |
| 变焦 | `setupZoom` 1:1：caps.zoom 缺失（iOS）→ 整格消失；min/max/step 全按设备；applyConstraints 失败 → 禁用+提示 |
| 语言 | `data-t`/`data-t-title`/`data-t-ph` 三属性 + `applyLang()` 重渲 JS 生成按钮行；localStorage `arcam.lang`；`?lang=` 只影响当次 |
| 全身/半身 | 导入立绘后弹（自动装载不问）；全身→ground 景别默认，半身→free；半身禁贴地提示 |
| HUD | 六件套画在预览 canvas 浮层；**每帧**随传感器/参数更新；出图不带 HUD |
| 提示 | `showTip`：TIP_MS=3600 / TIP_FADE=600；贴边与否换文案（tip.slide/tip.move） |
| 软转屏 | `syncTurn`：系统 angle=0 且 |roll|>45 → body rotate(±90) + 世界层 rotate(-TURN)；`rotSafeRange` 只在 TURN≠0 接管拖动 |
| 修图 | 纯色底/AI/裁边/还原四按钮 + autoPrepareChar 自动链（webui 走 /api/prep） |
| 既有 | 300ms 防抖、scale 预览、export 全尺寸、zip 三件套——不动 |

## 5. Wave 2（本契约不含，排期另立）：MMD 3D

范围：mmd.js 1107 行 + mmd/（libs 312K / loaders 180K / controls 40K / shaders /
poses.json / three-mmd）vendor 进两个网页；相机矩阵按 arcam 投影公式拼
（mmd.js:13 注释）；四入口（zip/glb/文件夹/pmx）；az/el/focus/dof 控件；
draw3DLayer 合成与未就绪回退。Python 核心**不做** 3D（README「已知偏离」保留，
但网页两侧补上后该条改为"网页版已支持"）。

## 6. 质量门禁（不省）

1. 每分区自测绿（贴输出）；数值断言附手算注释；跨实现对拍优先（cut_solid_bg 与
   cutout.js 逐行对译 + 合成图对拍）。
2. CLI/端到端**真跑**：真立绘（含纯黑底 JPG 场景）过 prep；webui 真浏览器点一遍
   每个新按钮（IAB）。
3. 编排者合并后跑全量 `pytest tests/ -q` + `node tools/golden-diff.mjs` +
   `tools/lidar-e2e-smoke.py`；新增 golden 向量（若 hud/matte 有可钉数值）。
4. 死代码/未用 import 复查（编排者）。
5. 跨分区问题写报告交编排者，不越界改文件。
6. 不提交 commit（编排者统一提）。
