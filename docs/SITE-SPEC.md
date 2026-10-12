# SITE-SPEC — GitHub Pages 纯客户端版网页（JS 移植契约）

> 背景：GitHub Pages 只能托管静态文件，跑不了 Python 服务。所以把 arcam 的
> **同一条投影/合成管线移植成浏览器 JS**（原版项目本就是 JS，这是回到本形）。
> 命名/单位/取整契约与 Python 侧 `docs/SPEC.md` 完全一致；原创作者约定不变。

## 0. 硬规则

- 原生 JS（ES2022，`<script type="module">`），零框架、零 CDN、零构建；`docs/` 目录
  整体即 Pages 发布物（Pages 只支持 / 或 /docs，故站点文件与 SPEC 文档同放 docs/）（仓库 Settings → Pages → branch main /site）。
- 数值口径与 Python 逐字一致：`jsRound = (v) => Math.floor(v + 0.5)`（JS Math.round
  本身就是半向 +∞，正好等于 Python 的 js_round）；角度公开 API 用度；坐标 Y 上/Z 朝
  观察者/画布 v 向下。**禁止 `toFixed` 之外的任何取整捷径**（scene.json 的
  `toFixed(2)/(3)` 语义与 Python `js_to_fixed` 一致，直接用）。
- 验收基准：`docs/tests/golden.json`（Python 侧已生成：focal 40 / project 200 /
  place 120 / placement 102 / scene 3 + v1.9.0 起光照五组 38 向量
  （measureRegion 7 / bakeLit 17 / charStats 4 / litScaleBucket 8 / grainTile 2，
  bakeLit 逐字节 0 容差），共 503 向量）。Node 侧夹具 `tools/golden-diff.mjs`
  逐向量跑其中 500 个（scene 组 3 个无 JS 对拍物，只过 Python 侧重算门禁
  `tests/test_golden_recompute.py`）；J1 交付时用 node_repl
  （`mcp__node_repl__js`，纯 Node 内核）加载自己的 JS + golden 做**数值 diff 全过**
  （几何 1e-9 相对误差；光照组 bakeLit 字节全等）。
- 文件互斥：J1 只写 arcam-core.js；J2 只写 arcam-render.js；J3 只写
  index.html/style.css/app.js；J4 只写 matte.js + vendor/。禁止互相写。
- 视觉：`docs/WEB-STYLE.md` tokens（J3 的 style.css 用同一套 :root 变量，深浅色
  媒体查询照抄本地 webui 的即可——本地版已验证可用）。

## 1. J1 `docs/arcam-core.js`（纯数学，无 DOM）

```js
export const K_LONG = 17.3064, K_SHORT = 12.9798;
export const SHOTS = { '全身':1.00,'膝上':0.72,'腰上':0.47,'胸上':0.28,'头肩':0.20 };
export const FRAMES = { '自由':0,'1:1':1,'4:5':0.8,'16:9':16/9,'9:16':9/16,'满幅':-1 };
export function focalPx(W, H, feq)                       // geometry.focal_px
export function project(x, y, d, cam) -> [u,v] | null     // cam={W,H,h,feq,pitch,roll}
export function place(u, v, cam) -> [x,d] | null
export function groundPlacement(charW, charH, subj, cam, opts) -> {u,v,w,h,rot} | null
export function freePlacement(charW, charH, subj, cam, opts) -> {...} | null
export function currentPlacement(charW, charH, subj, cam, opts)               // mode 分派
export function autoFixX(charW, charH, subj, cam, opts) -> [newX, status]      // 'nochar'|'already'|'moved:NN'
export function snapToEdge(c, bar, rect, aspect, rot) -> {u,v,w,h,rot}
export function frameRect(key, W, H) -> {x,y,w,h}          // 满幅/自由/宽高比
export function viewRect(W, H) -> {x:0,y:0,w:W,h:H}
```
- charW/charH 替代 numpy 图（JS 侧只有尺寸参与数学）；subj={hp,d,x}；opts 收
  {shot,charScale,rotBasis,sizePct,rotDeg,freeP,edgeBar,rect}，缺省同 Python。
- 数值公式逐字移植 `src/arcam/geometry.py` / `placement.py`（含 clamp 范围 ±6/±20、
  span<2 早退、相机后方 None）。
- **自验**：node_repl 里 `import('./arcam-core.js')` + 读 golden.json，断言
  focal/project/place/placement 全字段最大偏差 <1e-9；不过就修到过。

## 2. J2 `docs/arcam-render.js`（canvas 合成 + 配方）

```js
export function contactShadowAlpha(charImg, placement, ambient) -> HTMLCanvasElement  // sprite.py 等价
export function drawSprite(base, charImg, c, {shadow, ambient, shadowLayer})          // drawCharTo 等价
export function composeShot(photoCanvas, charImg, subj, cam, params)                  // compose.py 六步
  // → { shot: HTMLCanvasElement, rect, scene: Object, light: null }  // light 恒 null，采样走内部
export function composeCharLayer(charImg, subj, cam, params) -> { layer, rect }
export function sampleLightFor(photoCanvas, charImg, subj, cam, params) -> sample|null
  // composeShot ③ 采光的单独入口：素材包的 character.png 要与 shot.jpg 同一套光，
  // 由 doExportBundle 采一份经 params.sample 传给 composeCharLayer（Python
  // compose_shot 第二遍传 light_used 的 JS 对译）
export function buildScene(rect, placement, subj, cam, params) -> Object              // scene.py 1:1
```
- 六步照 `docs/SPEC.md` §3.6：frameRect → 铺帧（drawImage 全画布）→ 画立绘
  （translate→rotate→先阴影后图，锚点图心）→ 裁切（整数 off 画布，js 偏移取整）。
- 环境光与颗粒烘焙（`docs/arcam-lighting.js`，v1.9.0 起）：照片路径按 `params.light`
  门采光（32×32 降采样 + 直方图），`bakeLit` 过曲线/白平衡/饱和/颗粒四段，
  `params.grain` 只门控颗粒段、`params.grainScale` 是强度倍率（1.0 = 原版逐位
  一致，golden 里有逐字节向量）。立绘层路径自己不采光（透明底没有照片），
  由调用方经 `params.sample` 把采好的光传进来才烘 —— 素材包的 character.png
  与 shot.jpg 因此同一套光（`sampleLightFor` + doExportBundle，Python
  compose_shot 第二遍传 light_used 的 JS 对译）。色调（tone）与 LiDAR 遮挡仍
  **不在范围**（tone 参数接受但忽略）；`light` 返回值恒 null（冻结的返回形状，
  采样值走内部消费，对外入口是 `sampleLightFor`）。
- `buildScene` 字段名/顺序/单位/toFixed 与 Python `scene.py` 逐字一致（golden 里有
  3 组完整样例，字段顺序用 `Object.keys()` 逐项比）。
- 缩放用 `imageSmoothingQuality='high'`（与原版浏览器一致）。

## 3. J3 `docs/index.html|style.css|app.js`（页面，功能对齐本地 webui）

- 布局/控件 id 与本地 `webui/` 保持一致（照片/立绘上传、参数面板、预览、四个成品
  钮、状态条、scene.json 折叠、「仅在本地运行」chip 改成「页面在 GitHub Pages 运行，
  运算全在你的浏览器里」）。
- 调 `arcam-core.js` + `arcam-render.js`；`window.arcamMatte` 存在才显示「抠图」钮
  （J4 的挂钩点；不存在则隐藏并在 hint 说明本地版有此功能）。
- 下载 PNG/立绘层/zip（zip 用 JS 手写 store 式最小 zip 或 store-only：三件套
  shot.jpg+scene.json+character.png——与 CLI bundle 语义一致，文件名带时间戳）。
- 无参数防抖 300ms 自动重渲；无照片/立绘时给状态条提示。
- 上传支持 drag&drop + 点击；立绘无 alpha 时提示「请用本地版抠图」或存在 matte 时
  引导点抠图。

## 4. J4 `docs/matte.js` + `docs/vendor/`（可选抠图，懒加载）

- 从 `oss-teardown/repo/anime/` **只读复制** ort.min.js / ort-wasm-simd-threaded.mjs /
  .wasm / isnet-anime-q8.onnx 到 `docs/vendor/`（这是原版自带的运行时与模型，
  上游 README 声明该目录属本项目自身文件）。
- 按 artifact A-matting-mmd-chars.md §1 的算法重写 `window.arcamMatte =
  async (imgEl, onProgress) => canvas`：segPreprocess（长边→1024、边缘延展补边、
  RGB、NCHW f32、/255+ImageNet 归一）→ ort 会话（env: wasm, numThreads:1, simd:true，
  输入名 'img'）→ 裁回 + LO/HI=0.28/0.46 窄带二值（不做 sigmoid）→ keepLargestBlob
  （8 连通最大域+边界泛洪补洞，保原软值）→ alpha 回贴（RGB 不动）。
  **以自己的实现为准**（可参考上游 cutout.js 的常量，注释注明出处 file:line）。
- onnx 44MB 入库可接受；init 懒加载，进度条用 onProgress。
- 自验：node_repl 只能验预处理/后处理的纯函数部分（DOM 部分留给编排者浏览器验收）。

## 5. 交付定义（编排者验收）

1. `docs/` 可直接被 Pages 伺服：index.html + 上述 JS 全在同目录或相对路径，无绝对
   路径、无外链。
2. 浏览器差分：J1/J2 的数值/几何与 golden.json 全过；scene.json 字段与顺序一致；
   合成图与本地 webui 出图结构一致（立绘落位/尺寸偏差 <2px，alpha 覆盖率差 <3%）。
3. README.md 增加「GitHub Pages 版」节（链接占位、与本地版的差异：光/色调/遮挡/
   抠图模型已含、LiDAR/视频本地专属）。
4. 仓库可推送：`git status` 干净、.gitignore 忽略 .venv/__pycache__。
