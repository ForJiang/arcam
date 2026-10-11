# AUDIT-CHARTER — 全项目审计宪章（v1.1.0 打包前）

> 用户指令（2026-10-09）：仔细阅读项目所有代码，排除 bug、修复功能障碍、优化、
> 检查所有功能、删除多余代码（保留必要注释）、打包更新。
> 本宪章是全部审计 Agent 的统一作业标准；上游契约 docs/SPEC.md、docs/LIDAR-SPEC.md、
> docs/WEB-SPEC.md、docs/SITE-SPEC.md 与 oss-teardown/lhso-20261003/artifacts/ 是
> 行为基准（真机/JS 逐字源码）。

## 1. 作业流程（每个 Agent 必须按序执行）

1. **通读**：逐行读完自己分区内每个文件（禁止抽样/ skim；大文件分段读但要全覆盖）。
2. **记账**：每个疑点先记入发现表（id/严重度/file:line/证据/类别），**先有证据后动手**。
3. **验证**：疑点 bug 必须用最小复现证明（测试/REPL/对拍）再修；不能复现的降级为
   「记录不改」并写进报告。
4. **修复**：只修自己分区内的文件；跨分区问题写进报告交编排者，不越界。
5. **清理**：删除死代码（不可达/未引用/并行期残留/stub）；**保留必要注释**：
   带 file:line 溯源的 docstring、解释「为什么」的注释、记录边界/偏离的注释。
   顺带修掉**过期/错误**的注释（引用了不存在文件的、与行为矛盾的）。
6. **回归**：交付前跑 `.venv/bin/python -m pytest tests/ -q` 全量（必须全绿），
   外加自己分区相关的 CLI/浏览器冒烟（能跑就跑）。
7. **报告**：发现表 + 删除清单 + 未修项与理由 + 测试结果。

## 2. 分类口径

| 类别 | 定义 | 处置 |
|---|---|---|
| BUG | 行为与契约/原版不一致、会出错、算错、静默失真 | 必须修 + 回归测试佐证 |
| DEAD | 不可达代码、未引用符号、并行开发期 stub、被替代的旧路径 | 删除 |
| REDUNDANT | 重复实现/重复数据（可收敛且不破坏 API） | 收敛，报告说明 |
| SMELL | 缺校验、错误路径不兜底、注释过期、命名误导 | 修 |
| PERF | 有测量证据的热点（>100ms 级、O(n²) 可避免） | 优化 + 前后对比数字；无证据不动 |
| KEEP | 刻意保留的原版不一致行为（见各 SPEC「如实保留」清单） | **不动**，注释已在的不许删 |

## 3. 红线

- 不改公开 API 签名（要改先报告）；`__init__.py` 的导出只增不减。
- 不许为了让测试通过而弱化断言/删测试；测试本身错了要拿证据修并在报告声明。
- 不许「顺手修」SPEC 里标注的刻意保留项（clamp ±6/±20、零填充语义、双轨兼容…）。
- 只准改自己分区文件；`docs/SPEC*.md`、`.ai/`、启动器、README 归编排者。
- 全部数值口径不变：js_round/toFixed 语义、深度轴向语义、scene.json 字段序。
- 不 commit、不 push、不动 .venv。

## 4. 分区（互斥）

| Agent | 文件 |
|---|---|
| A1 核心数学 | src/arcam/{numeric,geometry,placement,frames,scene}.py + tests/test_{geometry,placement,frames,scene}.py |
| A2 合成管线 | src/arcam/{lighting,sprite,compose,tone}.py + tests/test_{lighting,sprite,compose,tone}.py |
| A3 外设 CLI | src/arcam/{matting,video,cli,__init__,__main__}.py + tests/test_{matting,video,cli}.py |
| A4 LiDAR/服务 | src/arcam/{webapp,depth,plane,lidar_live,lidar_sim}.py + tests/test_{webapp,lidar_depth,lidar_plane,lidar_live}.py |
| A5 前端 | src/arcam/webui/* + docs/{index.html,style.css,app.js,arcam-core.js,arcam-render.js,matte.js} + tests/test_webui_static.py |

## 5. 交付定义

- 全量 `pytest tests/ -q` 绿（当前基线 393 passed / 3 skipped，只允许增）。
- 每个 Agent 的最终回复 = 发现表（含证据）+ 删除清单 + 优化前后数字 + 未修项。
- 编排者（我）负责：合并复审、跨分区裁决、test_integration、CHANGELOG/版本/
  打包/推送/线上验收。

---

# v1.5.0 审计轮次（2026-10-11）

用户指令同 2026-10-09（通读/排 bug/修障碍/优化/查功能/删多余代码保留必要注释/打包），
本轮项目已从 393 测试长到 **496 passed / 1 skipped**、15 个子命令、22 个 Python 模块、
13 个网页 JS。分区按当前文件清单重划（互斥不变）：

| Agent | 文件 |
|---|---|
| B1 核心数学 | src/arcam/{numeric,geometry,placement,frames,scene}.py + tests/test_{geometry,placement,frames,scene,golden_recompute}.py |
| B2 合成修图 | src/arcam/{lighting,sprite,compose,tone,matting,matte_tools}.py + tests/test_{lighting,sprite,compose,tone,matting,matte_tools}.py |
| B3 CLI 与文案 | src/arcam/{cli,video,__init__,__main__,hud,i18n}.py + tests/test_{cli,video,hud}.py |
| B4 服务 LiDAR | src/arcam/{webapp,depth,plane,lidar_live,lidar_sim}.py + tests/test_{webapp,lidar_depth,lidar_plane,lidar_live,lidar_compose,integration}.py |
| B5 前端站点 | src/arcam/webui/* + docs/{*.js,*.html,*.css,mmd.js,mmd/} + tools/ + tests/test_webui_static.py |

编排者直管（Agent 不许改）：pyproject.toml、README.md、CHANGELOG.md、docs/*.md、
docs/tests/golden.json、.ai/、启动器、src/arcam/__init__.py 的导出表（要增导出写报告）。

本轮新增关注（v1.3/v1.4 新代码是重点）：
- **multi / watch / stats / recipe / batch / contact-sheet** 六个新命令的边界与错误路径；
- **光影三门**（shadow/grain/sample_light）在 compose/_engine/CLI/webapp/网页两侧是否真的
  全程通畅（门加了但某处没透传 = 静默失效，v1.4 已踩过两次这类）；
- **参数记忆 / 全屏 / 拖拽 / 自动摆正** 在 webui 与 docs 两侧是否行为一致；
- **docs 与 webui 的重复代码**（i18n 表、HUD 绘制、live 相机、matte-tools）能否收敛到
  单一来源（至少要有测试钉住两侧一致，已有的是 i18n 逐条比对）；
- v1.3/v1.4 引入的注释是否与行为一致（过期注释一律修）。

---

# v1.8.0 审计轮次（2026-10-11）

用户指令同前两轮。项目现状：23 个 Python 模块 / 9265 行、21 个测试文件 / 10073 行、
14 个网页 JS / 8256 行、15 个子命令、561 passed / 1 skipped。分区在 v1.5.0 基础上
按新增文件重划（互斥不变）：

| Agent | 文件 |
|---|---|
| C1 核心数学 | src/arcam/{numeric,geometry,placement,frames,scene}.py + tests/test_{geometry,placement,frames,scene,golden_recompute}.py |
| C2 合成修图 | src/arcam/{lighting,sprite,compose,tone,matting,matte_tools}.py + tests/test_{lighting,sprite,compose,tone,matting,matte_tools}.py |
| C3 CLI 与文案 | src/arcam/{cli,video,__init__,__main__,hud,i18n}.py + tests/test_{cli,video,hud}.py |
| C4 服务 LiDAR | src/arcam/{webapp,depth,plane,lidar_live,lidar_sim}.py + tests/test_{webapp,lidar_depth,lidar_plane,lidar_live,lidar_compose,integration}.py |
| C5 前端站点 | src/arcam/webui/* + docs/{*.js,*.html,*.css,mmd.js,mmd/} + tools/ + tests/test_webui_static.py |

本轮新增关注（v1.6/v1.7 新代码是重点）：
- **3D 栈两侧一致性**：webui 与 docs 的 mmd.js/mmd/ 是否仍逐字节同源；importmap
  路径（docs 相对 / webui 绝对）是否各自正确；`base_only` 在 webapp schema、
  webui buildParams、docs composeParams 三处是否都真通达（v1.7 新参数）；
  3D 导出路径（doExport3D / buildBundleZip 移植件）在 webui 侧是否与 docs 等价。
- **`lidar sim` / 阴影两参 / batch 端点**（v1.6 新增）的错误路径与 i18n 覆盖。
- **重复代码收敛**：webui/app.js 与 docs/app.js 的 3D 段、zip writer、参数记忆、
  效果开关——哪些是真重复（可收敛/至少加一致性测试），哪些是两侧固有差异
  （ES module vs classic script、后端渲染 vs 客户端合成）。
- **测试文件已 10073 行**（超过源码）：查重复用例、可参数化合并的near-duplicate、
  过期 fixture。测试只增不减，但near-duplicate 合并要在报告里列明。
- 过期注释/文档引用（file:line 偏移、引用了已删函数的注释）。

编排者直管（Agent 不许改）：pyproject.toml、README.md、CHANGELOG.md、docs/*.md、
docs/tests/golden.json、.ai/、启动器、src/arcam/__init__.py 的导出表。
