# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.8.1] — 2026-10-11

功能补齐：取景框读数（原版 ``frameRead``）。

### 新增

- 两个网页版的画幅选择行新增**取景框读数**（原版 index.html:355/1971：
  hud() 里每帧刷「出图 {W}×{H}」）——选完画幅立刻知道出图裁到多大，不用等
  渲染完看尺寸。五个画幅逐个实测（1:1→9×9、4:5→7×9、9:16→5×9、16:9/满幅→
  16×9），中英切换同步刷新。
- 画幅切换**即时刷新**读数（不等 renderNow）：没上传照片时 renderNow 早退，
  读数会停在旧画幅上——原版在 hud() 每帧刷没这个坑，webui/docs 是事件驱动
  渲染，补了 bindSelect 的 frame 分支。

### 备注

- 本轮同时完成全状态核查：完整用户旅程（上传 → 渲染 → 三种导出 → prep →
  reset → batch）本机实测通过；线上与本地 11 个关键文件逐字节一致；线上 mmd/
  子目录 6 个资源与 vendor/ 44MB 模型 206 就绪；HUD 实测绘制 26721 像素且
  构图提示正确触发（pitch 45° → 「人物会被压扁」）。
- 全套 **570 passed / 1 skipped**。

## [1.8.0] — 2026-10-11

全项目审计轮次（五分区并行 + 编排者集成）：修复 12 个问题，收敛两侧分叉。

### 修复（BUG）

- **`arcam prep` 兜底整条漏掉**（cli.py）：动过手（抠或裁）且最终内容 bbox 任一边
  < 8px 时应整条回退原图（index.html:2435-2439），CLI 侧从未实现——5×5 的立绘
  会被输出成 5×5 的"修好"的图，而 webapp `/api/prep` 同图回退原图，**两条入口
  结论相反**。已对齐 + 两条回归测试（回退/保持）。
- **`snap_to_edge` 的 NaN rot 炸 `js_round`**（placement.py）：原版 `if(!rot) rot=0`
  的 JS 假值集含 NaN，Python `not nan` 为 False，NaN 漏进 `_rotate_bar` 把
  `math.floor` 炸成 ValueError（公开 API 的 rot 由调用方数据算出，NaN 可达）。
- **`free_placement(free_p=None)` TypeError**：`current_placement`/`auto_fix_x` 有
  None 兜底而它没有（同一公开 API 家族口径不一致）。
- **`lidar sim --push` 静默丢掉用户给的 `--width/--height/--seed`**：
  `simulate_scene_server` 按固定 256×192/seed 0 合成、不收尺寸参数，命令却把
  `--width 128` 收下——推出去的帧既是 256×192，用户无从得知。改人话报错。
- **`--overlay hud` 的构图提示不带本遍落位**（cli.py）：`draw_hud_text` 没收
  placement/shot/mode，三分线/顶满/偏小/浮空那四条永远不触发（原版
  compositionHints 读的就是当前摆位）。
- **`arcam stats` 版本号在可编辑安装下报旧版**：`importlib.metadata` 停留在安装
  那一刻（实测 pyproject 已 1.7.0、metadata 报 1.2.0）。改为**源码树在就以
  pyproject 为准**，纯 wheel 安装才回落 metadata。
- **webapp 路由表与 404 hint 缺 `/api/batch`**（v1.7 上线未回写文档）。

### 优化

- `sprite._warp_rect` 的预乘 RGB 提到分块循环外（与块无关的常量每块重算）：
  实测 1509×1000 源 + 4K 画布，单次 `composite_sprite` 0.51s → 0.36s。
- `contact_shadow_layer` 的 `sm` 广播统一（az=0 与斜射两支同形）。

### 收敛（两侧同源去分叉）

- `webui/hud.js` 与 `docs/hud.js` 的 `autoFixX`/`currentPlacementOf` 逐字对齐
  （此前同一算法两种写法）。
- `webui/app.js` 的 `bindNumeric`/`applyLang` 对齐 docs 风格，删 `currentLang`
  helper（toggle 处内联）。

### 删除

- `docs/matte-tools.js` 的 `rgba01`（8 行零引用死函数——`cutSolidBg` 等处都直接
  内联取 `px`）。
- `cmd_batch`/`cmd_lidar_sim` 里模块顶部已 import 的重复局部 `from arcam.i18n
  import t`。

### 备注

- i18n 表 108 个"死键"经复核**全部保留**：原版 TXT 160 条逐条搬运是 FEATURE-SPEC
  §3 的契约（完整性本身是验收项），且 option 标签（`shot.全身` 等）被两个页面
  直接当中文下标用。
- `align_to_canvas`/`sample_subject`/`occlusion_mask`/`send_frame` 经全仓扫描
  确认均有真实引用（跨模块/测试/公开 API），非死代码。
- 全套 **570 passed / 1 skipped**；golden 差分 462 向量 0 失败；batch + depth
  混合尺寸照片实测 2 ok / 0 failed；浏览器复验本机版（hud 12 导出、i18n 190 条、
  MMD/live 就绪）。

## [1.7.0] — 2026-10-11

**3D 模型支持进入本机网页版**（此前只有 Pages 版有）——两侧现在功能对等。

### 新增

- 本机网页版（`src/arcam/webui/`）vendor 完整 MMD 运行时：`mmd.js`（原版 1:1 搬运）
  + `mmd/`（three.js / GLTFLoader / TGALoader / mmdparser / three-mmd / controls /
  shaders / poses.json，2.6MB）。index.html 加 importmap（裸模块说明符 ``three``
  的唯一解析方式，必须在任何动态 import 之前）+ mmd.js 经典 script。
- UI 与 Pages 版同一套：3D 开关、导入模型（.zip/.glb/文件夹/.pmx 四入口）、
  模型列表（多 pmx 时可切换/去掉）、姿势行（4 个）、主光方位/高度/对焦/虚化
  四滑块、3D 状态提示。
- **分工**（Python 没有 WebGL）：后端新增 ``base_only`` 参数——3D 就绪时只出
  **照片底**（同裁切框），WebGL 层由客户端合成。scene.json 仍记 2D 落位，客户端
  再挂 ``mmd`` 段（与 Pages 版同字段）。
- 3D 导出（PNG / 素材包）：客户端合成 shot.jpg + scene.json + character.png
  （MMD 层本身就是 alpha 画布，盖回去严丝合缝）。为此把 docs 版的最小 ZIP
  writer（crc32 + store 方式 + 1980 时间戳）同源移植进 webui。

### 验证

- 本机版 3D 引擎浏览器实测：`mmd.js` 加载 20 个导出、poses.json 4 个姿势、
  点开关后 `ready: true`（three.js 动态 import + 场景构建成功）、无模型时提示
  「还没导入模型」。
- `base_only` 后端实测：同参两版渲染差异 616 像素（= 立绘被跳过），scene 的
  character 段照常记录。
- 静态门禁 `test_webui_has_mmd_3d_stack`：vendor 在位 / importmap 顺序 / UI 控件
  / app.js 接线（base_only、drawMmdLayer、attachMmdKey、buildBundleZip）/ docs 侧
  同源，六项全查。
- 全套 **561 passed / 1 skipped**；golden 差分 462 向量 0 失败。

## [1.6.0] — 2026-10-11

功能再扩充：LiDAR 合成器命令、阴影创作控制、网页批量出图。

### 新增（CLI）

- `arcam lidar sim`：按已知真值（相机高/俯角/滚转）合成深度帧——**落盘
  `.depth` 序列 或 推给正在监听的 `arcam lidar serve`**（`--push 端口`）。
  补全了 LiDAR 闭环：sim（合成设备）→ serve（接收）→ inspect（解出真值，
  实测 h/pitch/roll 精确回真值）→ render --depth（合成）。`--json` 只打印
  第一帧 meta；噪声由 (seed, index) 播种，同参同帧可复现。
- 摆放参数组新增 `--shadow-az`（阴影光线方位角，度）与 `--shadow-opacity`
  （浓度倍率）。

### 新增（核心）

- **阴影方向与浓度可调**（`compose_shot(shadow_az=, shadow_opacity=)` →
  `contact_shadow_layer`）：`shadow_az=0` 时影子正落脚下（与原版逐位相同，
  有回归测试钉死）；非 0 = 斜射，影子沿背光方向逐行拖偏（可分离双线性重采样
  + 边界夹取）。原版 2D 阴影旋转对称、没有方向——这是离线侧新增的创作控制
  （MMD 3D 路径一向有 keyAz）。`shadow_opacity` 线性缩放 SH_ALPHA。
- 网页两侧（webui + Pages）同步：效果开关区加「阴影方位/浓度」两个滑块，
  `normalizeParams` 白名单放行 `shadowAz/shadowOpacity`（漏放 = 滑块静默失效，
  注释里钉住这个坑）。

### 新增（网页）

- **批量出图**：本机网页版「批量出图」钮——多选照片 → 每张转 dataURI →
  `POST /api/batch` → 下载 zip（`shot_001.png …`）。单张失败不拖垮整批：
  跳过的张数进 `X-Arcam-Batch-Failed` 头（**只放数字**——HTTP 头按 latin-1
  编码，中文会 UnicodeEncodeError 让整个响应 500，本版本人踩过），具体原因
  写进 zip 内的 `_errors.txt`（UTF-8）。上限 12 张/次。
- Pages 版同钮：纯客户端逐张合成（setPhoto → composeFull → 收集）再走既有
  的 `buildBundleZip` 打包，失败明细同样进 `_errors.txt`。

### 修复

- `/api/batch` 响应头混入中文导致 500（latin-1 编码限制）——见上，明细改走
  zip 内文本。

### 备注

- 全套 556 → **560 passed / 1 skipped**；golden 差分 462 向量 0 失败；
  真图修图冒烟 7 张全过；LiDAR 闭环（sim → inspect）外参精确回真值；
  `/api/batch` 混合好坏图实测 2 ok / 1 failed 且明细正确。

## [1.5.0] — 2026-10-11

全项目审计轮次（五分区并行 + 编排者集成）：修复 30+ 个问题，其中 8 个是
「开关收了但不生效」类静默失效。

### 修复（按严重度）

**静默失效（开关/参数/端点收了但没到达生效点）**
- `arcam batch --depth-dir` **整批静默失效**：`load_depth` 从未 import，NameError
  被候选循环的 `except Exception: continue` 吞掉——输出与无深度逐位相同、退出码
  仍 0（B3-01）
- `char-layer --no-shadow/--no-grain/--no-light` 收下不生效：手抄 kwargs 漏了
  光影三门（B3-03）
- `contact-sheet --tone` 无效：base_params 另抄一份 compose 开关、漏了 tone
  （B3-04）
- `multi --scene` 裸 traceback（读 `args.char`，multi 的位置参数叫 `args.chars`）、
  `multi` 非满幅画幅全线广播报错（层是裁切后尺寸直接 _over 到整幅）（B3-05/06）
- webui 修图三钮整个点不动：`state.charName/charW/charH` 从未被赋值（5 处读 0 处
  写）→ 恒走「先选一张立绘」；HUD 也恒显示「还没选立绘」（B5 B-01）
- kind 弹窗选的 mode/shot/size_pct/edge_bar 一个都没进请求体：applyCharKind 改
  params 但不 markTouched，而 buildParams 只发 touched 键（B5 B-02）
- Pages 版取景框整块不画：`window.arcamCore` 从来没挂过（arcam-core 是 module），
  `R` 恒 null（B5 B-03）
- `cli._placement` free 分支漏传 `rect=frame_rect(...)`：scene.json 记的落位与
  出图不一致（吸附记不吸；1:1+edge_bar=2 实测差 48px、16:9 差 189px），波及
  render/char-layer/bundle/multi 的 free+非满幅+edge_bar≠0（B1 F-04 + B4 X1 双报）
- `_recipe_from_scene` 把 rot_basis=frame 反推成 world（判据写成 rotationLocked）
  （B3-07）

**其它 bug**
- `js_round(0.49999999999999994)` 得 1、ECMA 应为 0：唯一分歧输入，全定义域对齐
  `Math.round`（B1 F-02，代价 1.33x 慢、单渲染多 2µs）
- `place()` 零焦距/零维画布抛 ZeroDivisionError（同模块 focal_px/project 都不抛）
  （B1 F-01）
- `current_placement(free_p=None)` / `auto_fix_x(free_p=None)` TypeError（kw.get
  不把显式 None 当缺省；JS 孪生有兜底）（B1 F-03）
- `matte_tools.lens_blur`/`_flood_solid` 用内置 `round()`（半数取偶）而非 js_round：
  w=75/k=0.62 恰好 46.5 → 差一档、364/6000 像素变（B2-01/02，违反 SPEC §1.4 红线）
- `tone_stats` 用 `float(f"{sc:.3f}")`（半数取偶）而非 js_to_fixed（B2-04）
- `is_half_body` 的 h<2 门与原版 `max(0,h-2)` 不符（h==1 时原版读第 0 行）
  （B2-03；JS 侧同问题 B-07 一并修）
- `/api/prep` steps 类型不校验 → 500（应 400）；`{"steps":"solid"}` 文案报
  `['s','o','l','i','d']`（B4 F1）
- `/api/prep` 单步入口丢了整链的「<8px 回退」兜底 → 产出碎立绘（trim-only 对
  5×5 内容返回 5×5，整链/修复后回退 20×20）（B4 F3）
- `lidar_live._write_depth_spec` 兜底路径写 NaN 位型，权威 save_depth 写 0——
  同一帧两条路字节不同（B4 F9）
- `batch` 的 `camera_env_from_plane` 漏传 roll_deg，与 render/inspect/compose
  三处结论不同（同帧 roll 差 12°）（B3-02）
- `do_GET` 不 drain body → 带 body 的 GET 之后同连接 POST 永久损坏（501）
  （B4 F6）
- `docs` 触屏拖动被浏览器手势吃掉（缺 `touch-action:none`）；`.check-grid` 样式
  漏抄；全屏后 canvas 仍受 max-height 限死（B5 B-05）

**保真度（与原版逐字对译的差距）**
- **soften/flood 两处羽化内核**：原版 flood 是盒式均值 + `(s/n)|0` 字节截断、
  soften 是**可分离 3 抽头横竖两遍、每遍 Math.round、半径硬编码 1**（feather 值
  只当布尔）；现码两处都是浮点盒式。实测 24×24 单块最大差 **153/255**、96/576
  像素不同。Python 与 JS 两侧同步修复 + 手算对拍测试钉死（5×5 单像素 → 28）。
  副作用印证：默认素材 00_半身1 的半身判定从「否」翻回「是」——原版作者说过
  默认素材就是下沿被切的半身，修好才与原版一致
- `_write_gif`/`lens_blur`/`tone_stats` 的 round 口径（见上）

### 优化

- `compose_shot` 端到端 3439ms → 2174ms（**1.58×**，bit-exact：177 组参考哈希
  0 mismatch；`_over` 1189→118ms、`_to_float01` 137→15ms、`_alpha_from` 430→323ms、
  `bake_lit` 132→109ms）
- `/api/prep` 单步（trim-only，3000×1500）：3514ms → **57ms**（不再白跑整链）
- `bake_lit` 关颗粒时不再算 lit_scale/g_amp 与两张整幅 meshgrid
- `js_round` 0.139s → 0.186s/200 万次（1.33x，换 ECMA 全定义域一致）

### 删除（死代码，必要注释全留）

- webapp：`_Render.char01`/`notes` 两个写了没人读的字段、`_env_of` 里重复的
  fit/camera_env 两行（收敛到 compose._lidar_env）、HEIC 量程预检恒假的下界条件
- hud：`_blend`（8 行无人调用）、`_to_pil` 恒 True 的第二返回值
- tone：`tone_stats` 算出从不使用的 `t_lo`
- sprite：`composite_sprite` 里 `_to_float01` 已保证新数组后的冗余整幅拷贝
- docs/mmd.js：`pmxMod`/`pmxBuilder` 死变量、`imageToTexture`+`texLoader`（65 行，
  唯一调用方本身是死代码）、`built.official=true` 重复行、死回退 2 行（净 −107 行）
- docs/mmd/three-mmd/materials/ 三个零 import 文件（228 行/9.5KB，删前逐文件确认
  mmdparser/zipread/three.core/utils 都真被加载才动）
- 测试：死绑定常量、重复建数组代码、恒真断言（`abs(x-x)<1e-12`）、未用 import
- cli：手抄 kwargs 9 行 + base_params 6 行（统一走 _compose_kw，顺带修掉两个
  静默失效）、`_recipe_from_scene` 恒 None 的晦涩写法、`_subcommand_names` 死条件、
  `cmd_multi` 不可达的 nargs 校验与空块

### 测试与门禁

- 496 → **550 passed / 1 skipped**（+54：光影三门端到端、prep/trim/reset 三端点
  13 例、LiDAR 闭环、placement 对拍 40 组、soften 内核手算、死状态字段守卫
  `test_state_fields_all_written` 等）
- golden 差分 462 向量 0 失败（worst 5.551e-17）；真图修图冒烟 7 张全过；
  LiDAR E2E 外参精确回真值；wheel 两种模式 stats 正常
- 新增 `test_no_python_style_comments_in_js`：JS 行首 `#` 一律红 + 有 node 就
  对所有 JS 做真语法检查（v1.3 踩过：一行 `#` 让整个 module 加载失败）

### 契约回写

- WEB-SPEC §1 路由表补 `/api/prep|trim|reset` 与 `GET /healthz`（上线三个版本
  没回写）
- FEATURE-SPEC §1 半身判定描述修正（原写「最底下一行 + >= 8/255」，实现与原版
  都是「倒数第二行 + >200 + >30%」）
- AUDIT-CHARTER 增补 v1.5.0 分区表与新代码关注点

## [1.4.0] — 2026-10-11

功能再扩充：三个新 CLI 命令 + 网页全屏取景与参数记忆。

### 新增（CLI）

- `arcam watch PHOTO CHAR -o OUT`：监视素材变化自动重出图（轮询式，跨平台最稳；
  `--once` 只跑一遍；`--interval` 调轮询）。调参时不用反复敲命令。
- `arcam multi PHOTO CHAR [CHAR...] --spec "x=0.6;d=3.2;scale=0.8"`：**多立绘合成**——
  一张照片里站多个人，每张各带各的站位。实现是逐张出立绘层再顺序 source-over
  （数学上与"一气画多个人"等价，且不动冻结的单主体六步管线）；scene.json 记
  第一张的落位（多主体无原版格式，如实注明）。
- `arcam stats [--json]`：项目自报——模块/测试/网页 JS 行数、公开 API 数、
  语言表条数、子命令清单（子命令名从 parser 现抓，新增命令自动进表）。

### 新增（网页）

- **全屏取景**：预览区推入 fullscreen（原版手机端就是整屏取景）；HUD 浮层与
  拖拽摆位在全屏下照旧，Esc 退出。
- **参数记忆**：摆放参数存 localStorage，下次打开自动恢复并写回所有控件
  （原版只存语言；调好一套站位关掉页面不再丢）。改动 500ms 防抖存盘，
  `beforeunload` 兜底。

### 修复

- webui/app.js 缺 `t()` 薄封装（i18n.js 挂 window.arcamT）——全屏/记忆接线里
  直接调 `t()` 会在 init 中抛 ReferenceError，把后半段接线全部 abort。补上封装
  （与 docs 版一致），浏览器实测恢复提示"已恢复上次的参数"正常显示。

### 备注

- 全套 496 passed / 1 skipped；golden 差分 462 向量 0 失败；浏览器实测参数记忆
  全链路（改 d=7.2 → 防抖存盘 → 重载 → 控件与提示恢复）。

## [1.3.0] — 2026-10-11

功能扩充版：三个新 CLI 子命令、光影开关组、网页拖拽摆位、GIF 导出。

### 新增（CLI）

- `arcam batch PHOTO_DIR CHAR -o OUTDIR`：批量出图——一个文件夹的照片 ×
  同一立绘同一组参数。深度帧按 stem 逐张匹配（匹配不到走无深度路径，不因
  一张缺深度整批失败）；单张失败记一条继续跑，汇总退出码。
- `arcam recipe save|list|show|apply NAME`：摆放配方库（存 `~/.arcam/recipes/`，
  `$ARCAM_RECIPES` 可改根目录）。`save --from scene.json` 从既有配方提摆放段；
  `apply` 打印可直接粘的 CLI 旗标，`--set K=V` 临时覆盖单项。
- `arcam contact-sheet PHOTO CHAR -o sheet.png --vary K=V,V`：参数对比网格——
  笛卡尔积逐格渲染拼成带标注总览（上限 24 格防手滑），每格标注该格非默认取值。
- `arcam video --gif`：轨道渲染追加动画 GIF（纯 PIL 零依赖；256 色逐帧自适应
  调色板，比全局调色板耐得住光线变化大的轨道）。
- 摆放参数组新增 `--no-light / --no-grain / --no-shadow`（原版 LIGHT_ON/GRN/SHD
  三开关的 CLI 侧）。

### 新增（核心与网页）

- compose 层开三个门（`shadow` / `grain` / `sample_light`，默认全 True = 原行为）：
  门只控整段开关，曲线/白平衡/饱和的数值一个字节不动。webapp params schema 同步
  收 `light/grain/shadow` 三键。
- 网页两侧新增「效果开关」折叠区：光影三件 + HUD 三件（网格/地平线/人体）+
  「自动摆正」按钮。
- 网页新增**拖拽摆位**（原版核心交互）：预览画布上拖动即改站位——ground 模式
  经 `placeAt` 反投影解世界 (x, d)，round-trip 实测闭合（du=0, dv=0）。
- hud.js 新增 `placeAt`（place() 的 JS 对译，含 d∈[0.5,20]/x∈[-6,6] 夹取）与
  `autoFixX`（三分线吸附，ground 走数值导数一步解，与 Python 同算法）。

### 备注

- 网页版（Pages）的 light/grain 两开关**只影响状态显示**：JS 侧自 v1 起环境光
  烘焙就是 no-op（没有 bakeLit），UI 提示里写明，不假装生效；shadow 真生效。
- 抓修两个静默失效（v1.3.0 收尾）：① docs/hud.js 直接用了 arcam-core 的
  ``project`` 而它返回 **[u,v] 数组**（本文件内部按 {u,v} 对象用）——Pages 版
  整个 HUD 一直在静默不画；包一层转换修复，浏览器实测网格 14.3 万像素。
  ② arcam-render.js 混入一行 Python 风格 ``#`` 注释 → module 语法错误 →
  动态 import 失败 → **Pages 全部功能不可用**；修为 ``//`` 并加
  ``test_no_python_style_comments_in_js`` 门禁（行首 ``#`` 一律红 + 有 node
  就真语法检查）。
- 全套 490 passed / 1 skipped；golden 差分 462 向量 0 失败。

## [1.2.0] — 2026-10-11

功能对等版：把原版网站的交互能力补进项目（契约 `docs/FEATURE-SPEC.md`）。
Python 核心拿无设备依赖的部分，两个网页拿浏览器原生能力。

### 新增（Python 核心）

- `arcam/matte_tools.py`：立绘修图工具链（cutout.js 非 AI 线的 1:1 对译）——
  `cut_solid_bg`（纯色底泛洪，tol 字节口径、边框起手、seen/isBg 双数组）、
  `content_bounds`/`auto_trim`（bbox 裁边，<4px 门限）、`soften_edge`
  （erode+feather）、`lens_blur`（预乘 alpha 降采样往返，k=0.62）、
  `auto_prepare_char`（装图自动五步：抠底 5%~85% 门控 → 裁边 → 柔边 →
  微糊 → 半身判定，任一步失败静默跳过、动过手且内容 <8px 整体回退）、
  `is_half_body`（倒数第二行 alpha>200 且 >30%）。15 项单测 + 真图 E2E
  （`tools/matte-tools-e2e.py`）。
- `arcam/hud.py`：HUD 取景装饰层 PIL 对译——`frame_overlay`（框外压暗+三分线，
  铺满早退）、`grid_paths`/`draw_grid`（23 条线、major 标记、米数标签）、
  `draw_horizon`（参数式地平线+虚线）、`draw_figure`（人体骨架，关节比例逐字）、
  `composition_hints`（11 条构图体检，出画早退；删掉的 4 条不复活）、
  `draw_hud_text`（三行读数+hints）。22 项单测。
- `arcam/i18n.py`：中英语言表（原版 TXT 160 条逐条搬运 + webui 17 条新增，
  共 177 条）+ `t()` 占位符渲染；缺 key 回落裸 key。
- CLI：新增 `arcam prep` 子命令（修图链四步可关断，`--lang` 双语输出）。
- webapp：新增 `POST /api/prep|trim|reset`（修图结果替换会话立绘；reset 回
  上传原图）。

### 新增（两个网页）

- **实时相机**（`live.js`）：getUserMedia + iOS 运动权限手势序 + deviceorientation
  → pitch/roll（平滑 + screen.orientation 补偿）+ 快门（视频帧进合成）+
  caps.zoom 变焦（iOS 无则整格消失）+ resumeCam 三时机 + 软转屏（body rotate
  ±90、世界层反转、rotSafeRange 拖动接管）。
- **HUD 浮层**（`hud.js`）：六件套画在预览 canvas 上的透明浮层，随参数/传感器
  每帧重画；出图不带 HUD。docs 侧从 arcam-core.js import 同一份几何。
- **中英切换**：`data-t` 三属性 + localStorage + `?lang=` 当次生效；两侧表由
  `tests/test_webui_static.py::test_i18n_tables_match_between_python_and_js`
  逐条钉死。
- **修图线**：纯色底/自动裁边/还原三按钮 + 装图后自动修图链（docs 侧为
  `matte-tools.js` 同算法 JS 移植）+ 全身/半身弹窗 + 拖动提示 tip。

### 新增（MMD 3D 层，Wave 2）

- **docs（Pages）版新增 3D 立绘**：vendor `mmd/`（three.js + loaders + controls +
  poses.json，2.8MB）+ `mmd.js`（1107 行，原版 1:1 搬运）。`composeShot` 开
  `charDraw` 钩子（加法式，不动冻结 API），3D 激活时 WebGL 层画进 full 画布
  （帧坐标 0,0），裁切/落位/scene.json/导出全部原样走——"预览 = 出图"照旧。
- 四入口模型导入（.zip / .glb / 文件夹 / 单个 .pmx，贴图池自动配）、多模型列表
  切换、4 个姿势、主光方位/高度/对焦/虚化四滑块；相机矩阵按 arcam 投影公式拼
  （mmd.js 文件头：与 project() 偏差 2.27e-13 px）。
- 引擎懒加载（首次点 3D 才下 three.js）；file:// 下 CORS 拦截给人话提示；
  无模型/渲染失败不静默失败也不叠 2D 图。
- 接线门禁 `tests/test_webui_static.py::test_mmd_3d_wired`（vendor 在位、UI
  齐全、真实 API 调用、charDraw 钩子接通）；Node 打桩验 API 表面（14 方法 +
  6 getter、poses.json 4 个姿势）；浏览器实测开关/滑块/无模型降级不抛错。
- 能力矩阵：3D 属网页版专属（与 LiDAR 属本地版对偶）——Python 核心不做 WebGL。

### 备注

- 半身判定的微糊内核差异记为 SPEC §5 已知微偏差（刀锋阈值，flag 仅选默认模式）。

## [1.1.2] — 2026-10-10

v1.1.1 之后的稳定性收尾：全量套件批量压测（每轮 12+ 遍）定位并修掉最后两个
偶发失败，均为连接层竞态。

### 修复

- **`lidar serve` 测试辅助函数违反 serve() 的调用契约**（test_lidar_live.py）：
  `serve()` 的 bind+listen 发生在**调用时**（yield 在内层 `_frames()`，外层是
  普通函数，lidar_live.py:219 的文档契约），但辅助函数把调用放在后台线程里、
  主线程立即连接 → 偶发 `ConnectionRefused`。改为调用线程建 generator（监听
  就绪后再连），后台线程只迭代。
- **`lidar_live` listen backlog 从 1 提到 8**：协议是单客户端顺序帧，但客户端
  重连/探测连接常与上一个连接的关闭竞态，backlog=1 时新连接直接被拒。与
  v1.1.1 的 webapp backlog 修复同类。

### 备注

- 两处修后：目标测试定向 30 次 0 失败；全量套件连续 12 遍全绿（修复前合计
  约 3% 概率挂）。
- 版本 1.1.1 → 1.1.2；wheel/sdist 重建。

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

[1.8.1]: https://github.com/ForJiang/arcam/compare/v1.8.0...v1.8.1
[1.8.0]: https://github.com/ForJiang/arcam/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/ForJiang/arcam/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/ForJiang/arcam/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/ForJiang/arcam/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/ForJiang/arcam/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/ForJiang/arcam/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/ForJiang/arcam/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/ForJiang/arcam/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/ForJiang/arcam/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/ForJiang/arcam/compare/v1.0.0...v1.1.0
