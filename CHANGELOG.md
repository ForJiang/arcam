# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.1] — 2026-10-10

补验版本：把 v1.1.0 未覆盖的可选依赖路径（HEIC / EXR 深度、mp4 视频）在本机
真跑一遍，过程中抓到并修掉 2 个真 bug，消掉 1 个测试抖动。

### 修复

- **HEIC 人像深度量程预检把 0 值无效像素误判为「疑似标定差异」**（depth.py）：
  预检在 `_make_frame` 的无效值归一**之前**跑，真机人像 HEIC 的无效像素大量
  编码为 0，直接把 min 打成 0 撞上误导性报错——整份深度图被拒。现在预检前
  先按 §1 口径归一（0/负/非有限 → NaN），与 PNG16/EXR 路径一致。
- **web 服务 listen backlog 只有 5**（webapp.py）：`socketserver` 默认值，
  浏览器一次并发拉 html/css/js/favicon + API 超出即被内核 reset
  （实测 `ConnectionResetError [Errno 54]`）。提到 64——这也让 8 线程并发
  渲染测试从「全套件负载下约 1/3 概率挂」变成 10/10 稳定。

### 测试与工具

- `tests/test_lidar_depth.py` 的 HEIC skip 转正：真 HEIC 容器（RGB 主图过
  libheif 编码）+ 按 pillow-heif 真实返回结构注入 `depth_images`，覆盖
  rep 0/1/2/3 四种表示类型与全部错误路径（pillow-heif 1.8 只暴露读侧 API，
  合成不了带 Apple 辅图的真机文件，辅图提取本身是第三方代码不在覆盖范围）。
- 新增 `tools/lidar-e2e-smoke.py`：可验证的 LiDAR 闭环冒烟——按 `place()`
  逆投影合成已知真值（高 1.5m/俯角 20°/滚转 -8°）的地面深度，过 HEIC 容器 →
  平面拟合 → 外参解算**精确回真值**（1.500m/20.00°/-8.00°，rms 0.49mm）→
  深度驱动合成。合成帧焦距与 `_estimate_pinhole` 对齐（同焦假设的已记录边界）。
- `conftest.py` 加 atexit gc 回收：ORT 会话若被循环引用拖到解释器关机才释放，
  teardown 与 threading 关机竞态会偶发 SIGABRT（退出码 134，测试全绿但看
  退出码的门禁误报）。
- `test_webapp.py` 加 backlog 回归断言，防回退到 socketserver 默认值。
- 全套 440 passed / 1 skipped（1 skip 为 onnxruntime 已装时 CLI 抠图 E2E
  由 P4 覆盖的既定跳过）；可选依赖（onnxruntime / pillow-heif / OpenEXR /
  imageio）本机全装真跑：HEIC/EXR 深度 23 项、mp4 渲染 11 帧 + h264 成片。

### 打包

- 版本 1.1.0 → 1.1.1；wheel/sdist 重建。

## [1.1.0] — 2026-10-10

全项目代码审计版本：五分区并行审查（geometry/placement/numeric · compose/scene ·
cli/video/matting · depth/plane/lidar/webapp · webui/Pages JS），修复 16 个功能障碍
与静默错误、清理死代码、补齐数值回归钉。**未改动任何公开 API 签名**（除删除四个
零调用方的死函数，见下）。

### 修复（功能障碍）

- **free 模式 `rot_basis` / `edge_bar` 整条渲染链断链**（compose.py）：`_placement`
  的 free 分支漏传 `rot_basis` 与 `rect`，导致 `--rot-basis frame` 与 `--edge-bar N`
  在整条合成路径上是空操作——画面永远不滚转对齐、贴边永远不吸附。webapp 的
  `_scene_placement` 同款漏传（scene.json 记的落位与预览/导出图不一致）一并修复。
- **`char-layer --frame`  silently ignored**（cli.py）：`compose_char_layer` 内部固定
  '满幅'，换成 16:9 等画幅时立绘层与出图裁切框错位、盖不严丝合缝。函数新增
  `frame_key` 参数并全链透传。
- **char-layer 参数整组漏传**（cli.py）：`shot` / `char_scale` / `edge_bar` /
  `rot_basis` 四个开关在 CLI 侧从未送达，层与 `render` 同参数输出不一致。
- **`lidar serve` 一帧都收不到**（cli.py）：生成器从未被迭代，命令立即退出。
- **`char_lum` alpha 字节语义差 1 导致 1/3 像素被跳过**（lighting.py）：重采样后
  alpha 门限判断按浮点而非字节口径，区域光测量系统性偏差。
- **视频 `float01` 背景双重除法**（video.py）：0.5 被除了两次，背景接近全黑。
- **`lidar_sim` 旋转序导致法向错误**（lidar_sim.py）：极端位姿下滚转角误差 2.8°。
- **webapp 深度环境竞态**（webapp.py）：返回了新帧配旧环境（静默错位）；空字节路径
  500 且泄漏 stderr。
- **抠图模型损坏时抛裸 traceback**（matting.py）：改为 `ValueError` 人话错误。
- **轨道时间戳允许重复**（video.py）：重复键被静默丢弃；改为严格单调校验。
- **`--feq 0` 直接 ZeroDivisionError**（cli.py）：focal=0 时 `place` 除零；现在
  CLI 入口一次性挡成人话错误（web 侧本就校验 positive）。
- **`focal_px` 零维画布除零**（geometry.py）：按 JS 的比较结果分流，与原版行为一致。
- **Pages 版导出空照片/空立绘裸 TypeError**（docs/app.js）：三个人话校验前置；
  素材包的立绘层改走 `composeCharLayer` 同参路径（原来的
  `composeShot(null, char)` 回退在 JS 侧取不到层，素材包直接坏）。
- **Pages 版抠图二值阈值 1 ulp 翻面**（docs/matte.js）：`×1/(HI-LO)` 改为
  `÷(HI-LO)`——1/0.18 不精确，乘法比除法最多差 1 ulp，而 `keepLargestBlob` 的
  阈值恰是 0.5，实测 18432 像素随机场 95 处不等、下游 63 像素翻转、整块连通域变形。
- **HEIC/EXR 深度解析校验缺口**（depth.py）：补齐格式校验。

### 优化与清理

- 删除四个零调用方死函数：`Rect.clamp_to`（语义与冻结的 `clampRegion` 相反、纯误导）、
  `js_fixed_str`、`to_u8`、`clamp01`（numeric.py 及包导出）。
- 删除 JS 侧死代码：`arcam-core.js` 的 `twoSum`（被 fast2sum 误差项取代）、
  `index.html` 的 10 个无引用 `id`、`composeParams` 的 snake_case 死载荷
  （渲染侧只读 camelCase，已核对）、`style.css` 的 no-op `gap: 0`。
- Pages 版数值输入失焦时把夹取后的值写回控件（与本地 webui 一致：手输超量程时
  输入框还留着 99，会让人以为没夹成功）。
- 本地 webui：上传失败后清理 `depthFile` 残留。

### 测试与门禁

- **golden.json 补 18 条 free 模式 `rot_basis=frame` / `edge_bar` 吸附向量**
  （84 → 102）：这两个参数正是本次断链 bug 的重灾区，此前 golden 零覆盖——
  JS/Python 分叉也发现不了。每条带 `out_nosnap` 对照，证明确实在考吸附。
- 新增 `tests/test_golden_recompute.py`（5 项）：fixture 必须能用当前 Python 实现
  逐向量重算出来，任一侧漂移立刻红——避免 fixture 悄悄过期后浏览器差分反而在
  捍卫一个错误基准。
- 新增 `tools/golden-diff.mjs`：Node 常驻差分夹具（IAB 页面事件循环会被挂起，
   await 类页面脚本跑不动；arcam-core.js 是纯数值 ES module，Node 可直接 import）。
  实测 462 向量 0 失败，最大偏差 5.551e-17。
- 审计期间各分区补回归测试；全套 437 passed / 3 skipped（3 skip 为可选依赖
  onnxruntime / pillow-heif 未装时的跳过）。

### 备注

- `lidar live` 的深度口径为 Apple 轴向 z（"距相机平面的距离"，官方示例
  `point(u,v,1)·depth`），与径向（cosθ）解释的仲裁结论一致，有回归测试钉住。
- scene.json 与原版 1:1 同构（字段序/单位/toFixed 口径），未因本次审计变动。

[1.1.1]: https://github.com/ForJiang/arcam/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/ForJiang/arcam/compare/v1.0.0...v1.1.0
