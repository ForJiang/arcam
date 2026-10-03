# 让她走出屏幕 · Python 离线版（arcam）

把二次元立绘按**透视投影方程**摆进你的画面里 —— 浏览器原版的离线同构实现。

原版（[AR 取景器，纯静态零依赖](https://filomenosnyder-stack.github.io/Let-her-step-out-of-the-screen-and-into-your-world/)）
把立绘摆进手机摄像头的实时画面：相机设于 `y = h`、地面为 `y = 0`、立绘置于 `(x, z)`，
屏幕投影高度与足底贴地位置由针孔投影方程解算，俯角/滚转来自空间姿态数据。
本仓库把**同一条投影/合成管线**搬到命令行：喂给人像照片和立绘，按参数出图、
导素材包、抠图，或者在一条位姿轨道下逐帧渲染。

核心承诺：`scene.json` 与原版 **100% 同构**（同名字段、同顺序、同单位、同取整语义），
「Python 出的图能和原版工具互认配方」。

不做：实时摄像头（Python 侧只接受已给定的 yaw/pitch/roll 参数）、MMD 3D 运行时
（three.js 生态；相机矩阵交叉验证的接口契约见 `docs/SPEC.md` §6）。

---

## 安装

需要 Python ≥ 3.14。依赖只有 numpy / pillow（pytest 仅测试用）。

```bash
cd let-her-step-out-py
uv venv
.venv/bin/python -m pip install -e .            # 基础：投影 + 合成 + 出图
.venv/bin/python -m pip install -e ".[matting]" # 可选：onnxruntime 抠图
.venv/bin/python -m pip install -e ".[lidar]"   # 可选：HEIC/EXR 深度输入（pillow-heif/OpenEXR）
.venv/bin/python -m pip install -e ".[video]"   # 可选：imageio 写 mp4
.venv/bin/python -m pip install -e ".[dev]"     # 测试
```

装完就有 `arcam` 命令；不装也能 `python -m arcam`（前提是 `src/` 在路径上，
`conftest.py` 已为 pytest 处理好这件事）。

## 快速开始

```bash
# 1) 出图：立绘贴地放进照片，顺带写 scene.json 配方
arcam render photo.jpg char.png -o shot.png --scene scene.json

# 2) 只要透明立绘层（和照片逐像素对应，事后自己叠）
arcam char-layer char.png -o layer.png --mode free --size-pct 70

# 3) 素材包三件套：shot.jpg(q0.95) + scene.json + character.png（确定性时间戳）
arcam bundle photo.jpg char.png -o pack.zip --pitch 12 --shot 腰上

# 4) 位姿轨道逐帧渲染：bg 给单图（静止背景）或帧目录 frame_000001.png…
arcam video track.json char.png -o frames/ --bg bg/ --tone

# 5) 顺手两个：抠图（ISNet-anime，纯前端同款）/ 轨道自检
arcam matte photo.jpg -o cut.png --model isnet-anime-q8.onnx
arcam check track.json

# 6) LiDAR 环境数据：先诊断深度帧，再深度驱动出图（显式参数仍优先）
arcam lidar inspect scan_0001.png [--json]
arcam render photo.jpg char.png -o shot.png --depth scan_0001.png
```

`--pitch 12 --roll -5 --d 4 --x 0.3 --h 1.6 --hp 1.65 --feq 23 --shot 全身
--char-scale 1.2 --frame 满幅 --rot-basis world --mode ground --size-pct 60 --tone`
这些摆放参数的默认值就是原版 `S` 的默认值（`index.html:921-937`），不填即原版默认。
每个子命令都有完整 `--help`；退出码 **0** 正常 / **1** 运行期失败（缺文件、素材不
合法、轨道有问题）/ **2** 用法错误。

## 轨道文件（track.json）

轨道是离线新增概念 —— 原版真机端把 deviceorientation 的 pitch/roll 实时写进 `S`
（`index.html:1069`），桌面端一次只导一张图。轨道文件把「一段随时间变化的姿态」
落成 JSON，再逐帧走同一条 `composeShot` 管线（`index.html:3673`）。

```json
{
  "width": 1080,
  "height": 1920,
  "fps": 30,
  "height_m": 1.6,
  "feq_mm": 23,
  "subject_height_m": 1.65,
  "char_scale": 1.0,
  "size_pct": 60.0,
  "mode": "ground",
  "frame_key": "满幅",
  "shot": "全身",
  "keys": [
    {"t": 0.0,  "lateral_m": 0.0, "distance_m": 4.0, "pitch_deg": 0.0,  "roll_deg": 0.0},
    {"t": 2.5,  "lateral_m": 0.8, "distance_m": 4.0, "pitch_deg": 12.0, "roll_deg": -4.0},
    {"t": 5.0,  "lateral_m": 0.0, "distance_m": 6.0, "pitch_deg": 24.0, "roll_deg": 0.0,
     "feq_mm": 28.0, "height_m": 1.2, "mode": "ground", "size_pct": 70.0}
  ]
}
```

| 字段 | 缺省 | 含义 |
|---|---|---|
| `width` / `height` | 1080 / 1920 | 画布像素（原版 canvas 像素 = 视频像素） |
| `fps` | 30 | 帧率；帧数 = `round(末键时间 × fps) + 1`，末帧正好落在末键上 |
| `height_m` | 1.6 | 相机离地高 `S.h` |
| `feq_mm` | 23 | 等效 35mm 焦距 `S.feq` |
| `subject_height_m` | 1.65 | 人物身高 `S.Hp` |
| `char_scale` | 1.0 | 立绘倍率 `S.charScale` |
| `size_pct` | 60.0 | 自由模式立绘占画面高度 % `S.sizePct` |
| `mode` | `"ground"` | `ground`（贴地）/ `free`（自由半身） |
| `frame_key` | `"满幅"` | 出图画幅，FRAMES 中文 key（存中文，`scene.json` 同值） |
| `shot` | `"全身"` | 景别，SHOTS 中文 key |
| `keys` | **必填，非空** | 关键帧数组，`t` 必须逐键递增 |

键（`keys[i]`）必需 `t` 与 `distance_m`；`lateral_m` / `pitch_deg` / `roll_deg`
缺省 0；`height_m` / `feq_mm` / `subject_height_m` / `char_scale` / `size_pct` /
`mode` 缺省 `null` = **沿用轨道默认**（对应原版「没拖过的滑块保持 S 默认」）。

数值字段在两键之间线性插值；`mode` 是离散字段，过键即切换、不插值；`t` 早于首键
停在首键、晚于末键停在末键（不外推）。

自检范围（`arcam check` / 渲染前强制过一遍）：`pitch ∈ [-60°, 60°]`、
`roll ∈ [-45°, 45°]`、`distance ∈ (0.05 m, 120 m]`，外加 fps/尺寸/倍率等正数约束。
原版没有轨道，这些阈值是本项目自定的（pitch 上限参考 `compositionHints` 的
40° 压扁提示放宽到 60°，`index.html:1890`）。

## LiDAR 环境数据（iPhone 激光雷达）

原版真机端把 deviceorientation 的 pitch/roll 实时写进 `S`（`index.html:1069`）——相机高
`S.h`、俯仰 `S.pitch`、人物纵深 `S.d` 都是**手拖滑块**的标称值。LiDAR 接入后，这些量可以
从深度数据里**测出来**（契约 `docs/LIDAR-SPEC.md`，优先级：显式摆放参数 > 深度测出值 >
标称默认值）：

| 环境量 | 测法 | 用途 |
|---|---|---|
| E1 相机高 `h` | 地面平面拟合（plane offset） | 覆盖 `S.h` |
| E2 俯仰 `pitch` | 地面平面法向（+ 已知/估计 roll） | 覆盖 `S.pitch` |
| E3 人物距离 `d` | 立绘足底屏幕位置取深度，按平面几何反解 | 覆盖 `S.d` |
| E4 遮挡 | 比「立绘公告板深度」与每像素深度，公告板后方的像素抠掉 | 出图遮挡正确性 |
| E5 平面质量 | inliers / rms / 覆盖率 | `arcam lidar inspect` 报告 |

### 怎么从 iPhone 拿到深度

有激光雷达的机型（调研 2026-10-03，来源 `oss-teardown/lidar-research.md` §1）：**iPhone
12 Pro 及之后所有 Pro 系、iPad Pro 11" (3 代) 及之后 / 12.9" (5 代) 及之后**。现实取数
路径（调研 §2/§3/§7）：

| 来源 | 格式 | 说明 |
|---|---|---|
| 人像模式照片（零成本，人人都有） | `.heic`/`.heif` | pillow-heif ≥1.8 的 `info['depth_images']` 读，`representation_type` 判别：`UNIFORM_Z(2)`=米直接用；`UNIFORM_INVERSE_Z(0)`=1/z→z；`UNIFORM_DISPARITY(1)`=归一化视差，用元数据 focalPixelPitch 换算米；`NON_UNIFORM_DISPARITY(3)` 无逐像素表不可转 → 报错指引。未装 pillow-heif → 带指引的 RuntimeError，**不静默回落到 RGB 主图** |
| 3D 扫描 App 导出 | `.png`/`.tif`（**16-bit 单通道**） | 通行惯例（调研 §5：无官方规范，工程惯例）：**毫米** uint16，0=无效；内参按针孔满幅 FOV≈60° **估计**（不带元数据），docstring 已注明 |
| 离线脚本/第三方工具 | `.exr` | OpenEXR ≥3.3 读 `Z`（或首个）通道为 float32 **米**；行序假定顶行优先（假设项，首次用真机 EXR 时核对） |
| 离线脚本/模拟器 | `.npy` | float32 `(H,W)` **米**；内参同样 FOV 估计 |
| 实时流 / 模拟器落盘 | `.depth`（magic `ARCD`） | 小头二进制：40B 头（宽高、fx/fy/cx/cy、roll/pitch/height）+ 深度 float32 米；内参外参齐备，是唯一带实测内参的格式 |

真机实时流（可选，调研 §7 排期）：**Record3D**（`pip install record3d`，SDK 免费，USB
免费 / Wi-Fi App 内付费，流 RGBD+置信+位姿）是最现成的「录像即得环境量」路径。调研明确
「不做」的：自建 iOS 直播协议/SDK、ARKit 原生格式作内部表示、相机 App JPEG 的 XMP 猜测。

内置实时流命令：`arcam lidar serve --port 7707 [--out DIR]`（协议见 `docs/LIDAR-SPEC.md`
§6；`arcam/lidar_live.py` 接收 + `arcam/lidar_sim.py` 合成场景模拟器，本机回环可自测）。
LiDAR 可选依赖：`.venv/bin/python -m pip install -e ".[lidar]"`（pillow-heif / OpenEXR）。

### 用法

```bash
# 诊断：读一帧深度，拟合地面平面，报告相机环境量（--json 给脚本）
arcam lidar inspect scan_0001.png
arcam lidar inspect scan_0001.depth --json
# 深度驱动出图：h/pitch/roll/d 由深度测出，遮挡区自动抠掉
arcam render photo.jpg char.png -o shot.png --depth scan_0001.png
arcam render photo.jpg char.png -o shot.png --depth scan_0001.png --h 1.9   # 显式优先
```

`--depth` 与显式 `--h/--pitch/--roll/--d` 同时给时打印一行「深度测得 h=… pitch=… roll=…
（显式值优先：…）」；显式判定口径是"开关真被用户给过"（argparse 默认 None，未给才是
默认/测出值）。`scene.json` 记的是**本遍实际落位所用**的有效值，可与原版互认。

### 单位与对齐约定

- 深度单位**米**（float32），值域 (0, 120]；**无效 = 0 或 NaN**（读入统一 NaN）。
- **深度值 = 相机平面轴向 z（Apple 语义）**：ARKit/AVFoundation 官方口径
  "distance from the plane of the camera in meters"，官方点云样例反投影就是
  `point(u,v,1) · depth`。`plane.backproject` 有 `z ≡ depth` 的回归测试钉死，
  不许改回「沿光线长度」口径（径向语义，与真机相反）。
- 深度相机坐标光学中心、z 前向、**y 向下**：`p = ((u-cx)/fx·z, (v-cy)/fy·z, z)`。
  与 arcam 数学坐标（y 上）只差一个 y 翻转，转换只在 `arcam/depth.py` 一处实现。
- `align_to_canvas`：深度分辨率通常低于照片（256×192 vs 3024×4032），约定深度与照片
  **同一运营商视场**（Apple 人像深度即照片视场的子采样），最近邻缩放到画布尺寸、
  内参同比缩放；**深度视场 ≠ 照片视场**（真机竖拍裁剪差异）是已知偏差。
- 焦距**同焦假设**：arcam 侧只有等效 35mm 焦距 `S.feq`，与深度相机 `fx` 之间按针孔
  模型换算（`focal_px`），不另做标定。

### 局限

- **视场/对齐**：深度与照片不同视场（裁剪差异）会系统性地偏置 pitch/h；对齐按同一
  视场约定处理，超出部分属已知偏差（见上）。
- **焦距同焦假设**：无逐台标定，长边缘像素的 `d`/`x` 反解误差随离轴角增长。
- **玻璃与纯黑吸光体**：LiDAR 对镜面/吸光表面无回波，该区域深度无效（NaN），平面拟合
  与遮挡判定在这些区域退化——拟合靠 RANSAC 鲁棒性兜底，不是修好。
- **有效距离**：0–5m 内精度最佳；超出后稀疏噪声增大，`rms`/覆盖率（`lidar inspect`）
  会如实反映，建议先 inspect 再决定要不要信。

## 架构

```
                 ┌──────────────────────────────────────────────┐
   photo/帧序列 ─▶│  compose_shot          （index.html:3673）    │
   char RGBA ────▶│   frame_rect → 铺帧 → 采光 → 画立绘 → 裁切    │
   S(x,d,h,hp,   │        │                                    │
     feq,pitch,  │   geometry.project / placement / sprite /    │
     roll) ─────▶│   lighting.bake_lit / tone / scene.build     │
                 └───────────────┬──────────────────────────────┘
                                 │ shot.jpg / PNG 帧 / scene.json
                 ┌───────────────▼───────────────┐
   位姿轨道 ─────▶│ video.render_track            │  ← 离线新增：键间线性插值，
   (track.json)  │  sample_track → 每帧重跑上面   │    逐帧走同一条管线
                 └───────────────┬───────────────┘
                                 ├─ frame_000001.png …（总是）
                                 └─ video.mp4（装了 imageio 才写）

   LiDAR 深度帧 ─▶ depth.load_depth → plane.fit_ground_plane ─┐
   (.png16/.npy/   → camera_env_from_plane（h/pitch/roll）      │→ 覆盖标称 S 或作
    .depth/.heic/  → sample_subject（人物 d/x）                  │  遮挡抠除（alpha=0）
    .exr)          → occlusion_mask ──────────────────────────┘
```

一条完整调用链：`cli` → `video.render_track` → `compose.compose_shot` →
`frames.frame_rect` + `placement.current_placement` + `sprite.composite_sprite`
+ `lighting.measure_region/bake_lit` + `tone.tone_matched_layer` →
`scene.build_scene` → `video.save_png/encode_jpeg`（PIL）。

## 模块 ↔ 原版 index.html 映射

| 本项目 | 原版位置 | 内容 |
|---|---|---|
| `arcam/numeric.py` | 全文 `Math.round`/`toFixed` 语义 | `js_round` / `js_to_fixed` / `Rect` |
| `arcam/geometry.py` | `index.html:1041-1057`、`3184-3202` | `K_LONG/K_SHORT`、`Camera`、`focal`、`project`、`place` |
| `arcam/placement.py` | `index.html:2789-3025` | `SHOTS/FRAMES`、贴地/自由摆位、三分线摆正、贴边吸附 |
| `arcam/frames.py` | `index.html:951-1039` | `viewRect`（离线取整块画布）、`frameRect` |
| `arcam/lighting.py` | `index.html:1212-1447` | `measureRegion` 32 级量化、`bakeLit` 三条 LUT |
| `arcam/sprite.py` | `index.html:3027-3166` | `drawContactShadow`、`drawCharTo`（锚点=图心） |
| `arcam/compose.py` | `index.html:3673-3712` | `composeShot` 六步、`buildCharCanvas` |
| `arcam/scene.py` | `index.html:3729-3789` | `buildScene` 配方 1:1 同构 |
| `arcam/tone.py` | `index.html:3501-3620` | `toneMatchedLayer`（均值平移 + 范围重映射 + 边缘羽化） |
| `arcam/matting.py` | `cutout.js:619-747` | `animeCut`（ISNet-anime + onnxruntime 懒加载） |
| `arcam/video.py` | —（离线新增，引用 `index.html:3673`） | 轨道 schema、插值、逐帧渲染、图片 IO |
| `arcam/cli.py` | `index.html:3797-3851` 快门/素材包/`zipStore` | 六个子命令 + `lidar inspect/serve`；zip 确定性时间戳 |
| `arcam/depth.py` | —（离线新增，LiDAR 接入 L1，`docs/LIDAR-SPEC.md` §2） | `LiDARFrame`/`load_depth`/`align_to_canvas`/`save_depth`（ARCD） |
| `arcam/plane.py` | —（离线新增，LiDAR 接入 L2，`docs/LIDAR-SPEC.md` §3） | `backproject`/`fit_ground_plane`/`camera_env_from_plane`/`sample_subject`/`occlusion_mask` |
| `arcam/lidar_live.py` | —（离线新增，实时流，LIDAR-SPEC §6，可选） | TCP 帧协议 `serve`/`recv_frame`/`send_frame` |

未移植（浏览器专属，只为 1:1 起见记录几何参数）：取景器装饰
（`drawFrame`/`drawGrid`/`drawHorizon`/`drawFigure`/`compositionHints`/`hud`，
`index.html:1740-1972`）、软转屏轴互换（`screenAxisToFree`）、3D 层
（`draw3DLayer`/`mmd.js`）。

## 已知偏离（如实保留，不许「顺手修」）

- **轨道/逐帧渲染是离线新增**：原版一次只导一张图。帧数定义、`hold` 最后一帧
  背景、帧目录帧数多于轨道时忽略多余帧，都在 `arcam/video.py` 的 docstring 写死。
- **`viewRect` 无布局外壳**：离线侧取整块画布（原版要减两条常驻条的高度，
  `index.html:951-966` 属浏览器版式）。
- **缩放滤波**：原版 `imageSmoothingQuality='high'` 未指定具体核 → Python 用双线性
  （`sprite` 与背景缩放同）。
- **`S.x` 两处 clamp 不一致**：`place` ±6（`index.html:3198`）vs `autoFix` ±20
  （`index.html:3020`）——都保留。
- **`clampRegion` 的 `sw>W` 越界零填充**（`index.html:1244-1251`）不改成 clamp。
- **JS `Math.round` 半向 +∞**：统一走 `arcam.numeric.js_round`（Python 内置是
  银行家舍入）。
- **素材包文件名去时间戳**：原版是 `shot_${stamp}.jpg`，这里固定
  `shot.jpg` / `scene.json` / `character.png`，配合 zip 时间戳写死
  1980-01-01（原版 `zipStore` 同样写死 `0x21`，`index.html:3309`）——同样输入
  永远出同样字节。
- **抠图模型解析顺序**：`--model` → `$ARCAM_ISNET_MODEL` → 仓库 `anime/` →
  `~/.cache/arcam/isnet-anime-q8.onnx`；都缺则报错并给下载指引，不静默失败。
- **mp4 是可选的**：`imageio` / `imageio-ffmpeg` 懒加载，没装只出 PNG 序列并提示，
  不许 crash。

## 网页界面

网页版取景器：把「上传照片/立绘 → 拖参数 → 出图」搬进浏览器。命令行那些能力
（出图 / 立绘层 / 素材包 / 抠图 / LiDAR 诊断 / 深度驱动 / 全部摆放参数）都做成了
按钮和滑块，任何改动 300ms 防抖自动重渲。仍然是**本机回环、单用户**：照片和立绘
只进本机进程内存，不出网（界面里有明示）。

### 用法

```bash
arcam web --open                # 起服务（默认 127.0.0.1:8765）并自动打开浏览器
arcam web --open --port 9000    # 换端口；--open 控制要不要自动开浏览器
```

或者不敲命令 —— 直接用项目根目录的双击启动器（见下）。

### 架构（一段）

后端 `src/arcam/webapp.py` 一个文件、纯标准库 `http.server`（`ThreadingHTTPServer`
+ 渲染锁，防并发抢 numpy），路由契约见 `docs/WEB-SPEC.md` §1；前端
`src/arcam/webui/` 三件（`index.html` / `style.css` / `app.js`），原生 fetch +
canvas、无构建、无 CDN。上传的素材常驻**进程内存**（`POST /api/*` 全走同源回环），
渲染复用 `compose.compose_shot` 同一条管线，`params` 与 CLI 摆放参数逐字同名
（默认值 = 原版 `S`，显式值 > 深度测出 > 标称默认）。预览按 `scale` 降采样
（长边 ≤1280，<1s），`export` 永远全尺寸。

### 双击启动器

| 文件 | 平台 | 作用 |
|---|---|---|
| `启动AR取景器.command` | macOS | 双击即用：找 python3 → 建/复用项目内 `.venv` → 装/确认 numpy+pillow → 起服务并开浏览器 |
| `启动AR取景器.bat` | Windows | 同上（`py -3`/`python` 顺序探测；`chcp 65001` 保中文不乱码） |
| `停止AR取景器.command` | macOS | 停掉占用端口的旧服务（`lsof` + `kill`，不屈服再 `kill -9`） |
| `停止AR取景器.bat` | Windows | 同上（`netstat` + `taskkill /F`；权限不够会提示用管理员身份运行） |

- 端口固定 **8765**，被占用时启动器会提前给人话提示（用停止脚本，或换端口）。
- 换端口：**第二个可选参数** —— `./启动AR取景器.command 9000`、`启动AR取景器.bat 9000`。
- 启动器只建/用**项目里的 `.venv`**，不碰你的全局 Python；`.venv` 已存在就直接复用，
  重复双击幂等（不会重装、不会弄坏 venv）。
- pypi 离线兜底：pip 装不上时，检查本机 `.venv` 里 numpy/PIL 是否已可导入，能导入
  就直接启动（网页服务只依赖 numpy + pillow，不引第三方 web 框架）。
- macOS 从 zip 解压 / 从某些 Git 客户端克隆后 `.command` 可能丢执行权限：启动器
  会自 `chmod +x`；还是不行就在 Finder 里右键「打开」，或 Git Bash 下手动
  `chmod +x '启动AR取景器.command'`。

### 已知边界

- **仅本机、单用户**：服务只听 `127.0.0.1`，无账号、无鉴权，别往公网暴露。
- **重启即清空**：上传的照片/立绘/深度帧都在进程内存里，停掉服务（或关掉终端窗口）
  再起来要从头传。
- 端口被别的程序占用时换端口（启动器第二个可选参数）；老服务没退干净用
  `停止AR取景器.command/.bat`。
- 抠图 / LiDAR 可选依赖（onnxruntime / pillow-heif 等）网页侧同样是懒加载，缺了
  `POST /api/matte` 会回 `{ok:false,error}` 带下载指引，不会 500。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

- `tests/test_video.py`：schema 往返、插值手推（中点）、轨道自检、渲染（帧数/
  尺寸/俯角上移方向）、背景目录与 hold。
- `tests/test_cli.py`：六个子命令的冒烟（render+scene.json、char-layer RGBA、
  bundle 解包三件套且 ZIP_DEFLATED、check 退出码、video 帧数、matte 错误路径）。
- `tests/test_lidar_compose.py`：LiDAR 深度接入合成的闭环——合成深度（地面+噪声+
  遮挡方块）驱动 `compose_shot(depth=…)` 与手算一致、遮挡区 alpha=0、无 depth 路径
  逐位回归、`_merge_env` 三态优先级、CLI `lidar inspect/serve` 与 `render --depth`。
- 其余模块的 golden 用例（投影 12 组公式手推、4×4 矩阵交叉验证、像素级合成性质）
  见 P1–P4 各自的测试文件。

源码溯源：每个公开函数的 docstring 都注明对应 JS 的 `index.html:<line>` /
`cutout.js:<line>`；契约全集在 `docs/SPEC.md`，原版逐字事实在
`oss-teardown/lhso-20261003/artifacts/`。
