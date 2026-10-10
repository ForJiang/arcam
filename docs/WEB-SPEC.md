# WEB-SPEC — arcam 本地网页界面 + 双平台一键启动（接口契约）

> 上游风格参考：`docs/WEB-STYLE.md`（风格 Agent 从 `zcode-2026-10-02.jsonl`
> 会话 sess_0312ae9e 挖掘；不存在时前端用其中性默认，编排者合并时替换）。
> 总体约定同 `docs/SPEC.md` §1 与 `.ai/COLLABORATION.md` 六条门禁。

## 0. 定位与硬约束

- **本地工具**：单用户、本机回环（127.0.0.1）、无账号、不上传任何东西。
- **零新依赖**：只用纯标准库（`http.server`/`json`/`threading`/`webbrowser`/`email`
  等）+ numpy + PIL。**禁止引入 Flask/FastAPI/任何第三方 web 框架**（pypi 离线，
  且原项目就是零依赖气质）。
- 界面「功能全覆盖、不做多余美化」：所有 CLI 能力都要能在网页点到；视觉按
  `docs/WEB-STYLE.md`  tokens 落地，不多做。
- 兼容 chrome/safari/edge 近三年版本即可（原生 fetch + canvas，不引 CDN）。

## 1. 后端 `src/arcam/webapp.py`（标准库实现）

```python
def make_server(host: str, port: int, *, open_browser: bool = True) -> http.server.HTTPServer
def main(argv: list[str] | None = None) -> int      # 'arcam web' 子命令入口
```

路由（单用户内存态，进程重启即清空）：

| 方法/路径 | 作用 | 请求 | 响应 |
|---|---|---|---|
| `GET /` | 单页 | — | `webui/index.html`（正确 MIME） |
| `GET /healthz` | 存活探针 | — | `{ok:true}`（200） |
| `GET /static/*` | 静态 | — | `webui/` 下文件 |
| `POST /api/upload` | 上传照片/立绘/深度 | multipart：字段 `photo`/`char`/`depth` | `{ok, kind, width, height, fg?}`；坏图/超大（>64MB）→ 400 `{ok:false,error}` |
| `POST /api/render` | 预览渲染 | JSON：`{params}`（§2 schema）+ `{scale:≤1}` | `{ok, image:"data:image/png;base64,...", scene:{...}, env:{...}\|null, ms}` |
| `POST /api/export` | 成品下载 | JSON：`{params, target:"png"\|"layer"\|"bundle"}` | 二进制下载（Content-Disposition attachment）；target 语义同 CLI render/char-layer/bundle |
| `POST /api/matte` | 抠图 | JSON：`{source:"char"\|"photo"}` | `{ok, image:dataURI, fg, ms}`；模型缺失 → `{ok:false,error:"…下载指引…"}`（不 500） |
| `POST /api/lidar/inspect` | 深度诊断 | multipart：字段 `depth` | `{ok, env:{height_m,pitch_deg,roll_deg,coverage,inliers,rms}, frame:{width,height}}`；随后 `/api/render` 可用 `params.depth_uploaded=true` 走深度驱动 |
| `POST /api/matte-result-as-char` | 把抠图结果设为当前立绘 | JSON：`{}` | `{ok}` |
| `POST /api/prep` | 立绘自动修图链 | JSON：`{target:"char", steps?:["solid","trim","soften","blur"]}` | `{ok, image:dataURI, cutRatio, trimmed, halfBody, actions:[i18n key], ms}`；steps 缺省四步全跑；类型不对/步骤名不认识/空数组 → 400 带可选值清单（v1.2 上线，v1.5 补契约） |
| `POST /api/trim` | 只裁边（修图按钮组单步入口） | JSON：`{}` | `{ok, image:dataURI, rect:[x,y,w,h], trimmed:true}`；没有透明边距或内容 <4px → 400（v1.2 上线，v1.5 补契约） |
| `POST /api/reset` | 还原到上传时的立绘 | JSON：`{}` | `{ok, image:dataURI, width, height}`；没上传过 → 400（v1.2 上线，v1.5 补契约） |

硬规则：
- 所有异常收敛为 JSON `{ok:false,error, hint?}`，**绝不给浏览器吐 traceback**；
  上传的数据超限/解析失败 → 400。
- `params` 校验复用 CLI 同款默认与 choices（shot/frame/rot_basis/mode/edge_bar），
  错误值 → 400 带 `hint`（哪个参数、可选值）。
- 预览渲染按 `scale` 降采样（长边 ≤1280），保证 <1s；`export` 永远全尺寸。
- LiDAR 上传的深度帧常驻服务端内存（inspect 后 render 复用），`scale` 只影响照片。
- 线程模型：`ThreadingHTTPServer`；渲染锁（`threading.Lock`）防并发抢 numpy。

## 2. params schema（与 CLI 摆放参数逐字同名）

```json
{"x":0.0,"d":4.0,"h":1.6,"hp":1.65,"feq":23.0,"pitch":0.0,"roll":0.0,
 "shot":"全身","char_scale":1.0,"frame":"满幅","rot_basis":"world",
 "mode":"ground","size_pct":60.0,"edge_bar":0,"tone":false,
 "depth_uploaded":false}
```
默认值 = 原版 `S` 默认（`index.html:921-937`）。显式值 > 深度测出 > 标称默认
（LIDAR-SPEC §0：布尔「用户动过没」由前端传显式键，缺键即未动）。

## 3. 前端 `src/arcam/webui/`（index.html / style.css / app.js）

布局（功能优先，风格按 WEB-STYLE tokens）：
- 左栏「输入」：照片上传（必选）、立绘上传（若为普通图，先点「抠图」）、深度帧上传（可选）
  + 三个动作钮（抠图 / LiDAR 诊断 / 重新渲染）。
- 右栏「预览」：canvas 显示最新渲染；下方四个成品钮（下载 PNG / 下载立绘层 /
  下载素材包 zip / 复制 scene.json）。
- 底部「摆放参数」：x/d/h/hp/feq/pitch/roll 数字滑块+输入；shot/frame/mode/
  rot-basis 下拉；char-scale/size-pct 滑块；edge-bar 0–4 下拉（仅 free 模式显示），
  tone 开关。**任何改动 300ms 防抖自动重渲**。
- 状态条：后端消息/错误/耗时；scene.json 折叠展示（等宽字体）。
- 无立绘时允许上传后自动 matte 一次（按钮手动触发，不自动烧 CPU）。
- 上传即本地内存，**不发外网**（同源 POST 而已），UI 明示「仅在本地运行」。

## 4. 启动器（双击即用）

- **macOS `启动AR取景器.command`**：bash；自 `chmod +x`；找 python3（PATH→
  `/usr/bin/python3`→brew→`/Applications/python.org`）；有 `uv` 优先没 `uv` 用
  `python3 -m venv` 建 `.venv`；依赖安装**先试正常 pip，失败则检查 numpy/PIL 是否
  已可导入，可导入就继续**（离线兜底，不装第三方 web 框架所以兜底大概率成立）；
  成功后 `exec .venv/bin/python -m arcam web --open`。全程中文进度输出，失败给出
  人话指引。
- **Windows `启动AR取景器.bat`**：`py -3` → `python` 顺序探测；同逻辑（venv+
  pip+离线兜底）；`start` 打开浏览器由 `--open` 负责；`chcp 65001` 保证中文不乱码。
- 两个脚本必须**幂等**（重复双击不坏）、把 `.venv` 放项目根、不碰用户全局环境。
- 附带 `STOP-AR取景器.command/.bat`（可选）：杀掉占用端口的进程（lsof/netstat），
  不会用就跳过——此项为可选级。

## 5. 测试门禁

- `tests/test_webapp.py`（后端 Agent + 编排者）：ephemeral 端口起服务（线程），
  覆盖：静态页 200+正确 MIME、upload 坏图 400、render 全参数字段往返、scale 生效、
  export 三种 target（PNG 二进制头 / layer RGBA / zip 三件套）、matte 模型缺失 400
  不 500、lidar inspect 合成深度（用 lidar_sim 合成器）→ env 数值、渲染锁不死锁。
- 前端无单测；编排者人工冒烟（截图级）：起服务→curl HTML→确认引用了 style.css/app.js。
- 启动器不做自动化测试（依赖真实 shell/shell 行为），编排者在两台逻辑（bash -n 语法检查）
  + 人工审读。

## 6. 完成定义

1. `arcam web` 起服务，浏览器打开即用；照片+立绘上传后**所有 CLI 能力**可在网页完成
   （出图/立绘层/素材包/抠图/LiDAR 诊断/深度驱动/全部摆放参数）。
2. Mac 与 Windows 双击启动器从干净状态（无 .venv）到出网页，全程无需手敲命令。
3. `.venv/bin/python -m pytest tests/ -q` 全量绿（含新 webapp 测试）。
4. README 增「网页界面」节（用法/架构/启动器说明/已知边界：仅本机、单用户、重启即清空）。
