# COLLABORATION — arcam（let-her-step-out-py）多 Agent 协作 SOP

> 用户固定要求（2026-10-03）：所有项目任务走**多 Agent 并行协作**，目标总耗时最短、效率最高，
> 质量门禁不省。本文件是本仓库的落地 SOP；上游为 project-relay 技能 + `oss-teardown/lhso-20261003/`
> （含 LEARNING_REPORT.md 与三份 artifact）。

## 1. 档位选择

| 档位 | 何时用 | 本仓库实例 |
|---|---|---|
| 快速 | 单文件/<30min | 改一个 clip、补一条测试 → 单 Agent 直接做 |
| 标准（默认） | 多模块横跨实现/测试/文档 | 本次移植：3 拆解 + 5 实现并行（见 §2） |
| 深度 | ≥5 个独立探索面 or 关键路径不确定 | 换渲染后端/接真相机标定时 |

## 2. 互斥分区与文件所有权

拆解期（只读目标仓库，按 index.html 行号切分区，绝不重叠）；实现期按模块与测试文件切分，
**一个 Agent 一个文件集，他人文件只读**。本次实际分区：

| Agent | 读取/写入分区 |
|---|---|
| 分析 A/B/C | index.html 三组行号区间 / cutout.js+mmd.js+chars.js |
| 实现 P1 | geometry.py, placement.py, test_geometry.py, test_placement.py |
| 实现 P2 | frames.py, lighting.py, sprite.py, tests/test_{frames,lighting,sprite}.py |
| 实现 P3 | compose.py, scene.py, tone.py, tests/test_{compose,scene,tone}.py |
| 实现 P4 | matting.py, tests/test_matting.py |
| 实现 P5 | video.py, cli.py, __main__.py, README.md, tests/test_{video,cli}.py |
| 编排者 | numeric.py, __init__.py, pyproject.toml, conftest.py, tests/test_integration.py, docs/SPEC.md, .ai/ |
| **实现 L1–L4（LiDAR 并行）** | 契约 `docs/LIDAR-SPEC.md`（冻结）。L1 depth.py+tests；L2 plane.py+tests；**L3 compose.py 追加 / cli.py 追加 / README 节 / tests/test_lidar_compose.py / 本行**（compose 只追加带默认值参数 `depth`/`env_overrides` + `ComposedShot.env`，无 depth 逐位回归；CLI 加 `lidar inspect`/`lidar serve`（占位）/`render --depth`）；L4 lidar_live.py+lidar_sim.py。`lidar serve` 实现在 L4，L3 只管 CLI 结构。depth.py/plane.py/scene.py/frames 为他人文件只读 |
| **实现 W1–W3（网页界面并行）** | 契约 `docs/WEB-SPEC.md`（冻结；风格 tokens 见 `docs/WEB-STYLE.md`，不存在时用中性默认）。W1：`src/arcam/webapp.py`（纯标准库 http.server，零新依赖）+ `cli.py` 追加 `web` 子命令 + `tests/test_webapp.py`；W2：`src/arcam/webui/` 前端三件（index.html/style.css/app.js，无构建无 CDN）；W3：启动器四件（`启动AR取景器.command/.bat`、`停止AR取景器.command/.bat`，端口 8765 固定、第二可选参数可换）+ README「网页界面」节 + 本行。`src/arcam/` 下他人文件只读；入口固定 `python -m arcam web --open --port 8765`（`web` 子命令由 W1 追加，WEB-SPEC §1 签名冻结） |

## 3. 黑板租约

- 跨 Agent 事实只通过**持久化文件**交换：`docs/SPEC.md`（接口契约）、artifact、README；
  禁止靠聊天转述大段内容。
- 接口先冻结（签名+语义+示例），再并行实现；冻结后追加参数只许**带默认值的关键字**。
- 相同证据（同一 file:line / 同一 URL）去重；冲突结论**并存**交编排者裁决，禁止后写覆盖。

## 4. 六条质量门禁（不可并行化、不可省）

1. 每个 Agent 交付前**自跑绿**自己的 pytest 文件（贴输出），不接受"应该能过"。
2. 编排者合并后跑**全量** `pytest tests/ -q` 并核对关键数字（投影 golden / scene.json / 真模型 fg）。
3. 数值断言必须附手算过程注释；跨实现对拍（如 tone 逐行 JS 转写）优先于自说自话。
4. CLI/端到端必须**真跑**一次（真图片、真模型、真解包），不接受单测覆盖即交付。
5. dead code / 未用 import 复查由编排者在合并后做（本次清掉 4 处并行期 shim）。
6. 发现的真 bug 谁发现谁记录在交付报告；修不越文件边界（编排者修集成点）。

## 5. 真相层级（仲裁序）

运行结果 > 源码/配置（原版 JS 逐字）> 需求/验收（scene.json 同构）> 架构决策（SPEC）>
前任报告 > 自己推测（必须标注）。原版仓库只读；本仓库 `src/arcam/` 只有编排者与 Owner Agent 可写。

## 6. 历史决策存档（换模型/换会话必读）

- 2026-10-03 移植完成：256 passed 1 skipped；真模型（~/.cache/arcam/isnet-anime-q8.onnx，
  软链到上游仓库只读源）可用；onnxruntime/imageio 为 optional extra。
- 2026-10-03 LiDAR 接入完成（4 Agent 并行 + 编排者仲裁）：**334 passed 3 skipped**。
  调研 `oss-teardown/lidar-research.md`（30+ 来源），契约 `docs/LIDAR-SPEC.md`。
- **深度语义终审（不许再翻案）**：depth = 相机平面**轴向 z**（Apple 官方语义
  "distance from the plane of the camera"；官方点云样例 `point(u,v,1)·depth`）。
  曾有实现按「沿光线长度」做 cosθ 折半，与真机相反——`plane.backproject` 有
  `z ≡ depth` 回归测试钉死。
- 编排者修的集成缺口（同类问题复发先查这里）：compose 未把深度帧 `align_to_canvas`
  到画布尺寸（遮挡/E3 两处）、CLI `--shot/--char-scale/--edge-bar` 未透传、
  video 轨道 `shot` 未透传、测试文件并行期 shim 死代码。
- 同焦假设是已记录边界：深度相机 fx 与渲染相机 `S.feq` 按针孔模型换算，不做标定；
  E2E 验收时合成深度帧的 FOV 必须与 `--feq` 匹配（否则 E3 测距系统性偏移）。
- 显式偏离清单在 README「已知偏离」节；新增偏离必须先过门禁 4 再加文档。
- 上游 commit 4a9ed04（main）：一切 file:line 证据绑定该 SHA；上游更新需重绑。
