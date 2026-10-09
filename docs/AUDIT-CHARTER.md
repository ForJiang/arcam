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
