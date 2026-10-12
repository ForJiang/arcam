# 部署到 GitHub Pages（两分钟自助版）

`docs/` 目录就是完整的纯客户端网页：GitHub Pages 只伺服静态文件，所以同一条
投影/合成管线被移植成了原生 JS，访客浏览器里直接跑，照片立绘不出设备。

## 方式 A：一条命令（需要 token）

```bash
export GITHUB_TOKEN=ghp_你的token      # Settings → Developer settings → Personal access tokens，勾 repo
tools/push-to-github.sh                # 默认建 public 仓库 arcam；可传参：tools/push-to-github.sh 仓库名 public|private
```

脚本依次做：读 token 取用户名 → 建仓（已存在跳过）→ 推送 `main` →
开 Pages（`main` 分支 `/docs` 目录）→ 打印网址。幂等，重复跑只是再推一次。
推送完成后会把 remote URL 里的 token 撤掉（只留 `https://github.com/你/仓库.git`）。

## 方式 B：网页点按（不装任何东西）

1. 打开 https://github.com/new ，仓库名随意（如 `arcam`），**Public**，不要勾
   "Add a README"（我们本地已有提交，避免冲突）。
2. 在本项目目录执行（把 `你的用户名`/`仓库名` 换掉）：
   ```bash
   git remote add origin https://github.com/你的用户名/仓库名.git
   git branch -M main
   git push -u origin main
   ```
   弹登录框时用 GitHub 用户名 + Personal Access Token（密码已不支持）。
3. 仓库页面 **Settings → Pages → Source 选 `Deploy from a branch`**，
   Branch 选 `main`、目录选 **`/docs`**，Save。
4. 等 1–2 分钟，网页地址：`https://你的用户名.github.io/仓库名/`

## 方式 C：本地先看效果（不部署）

```bash
cd docs && python3 -m http.server 8770
# 浏览器打开 http://127.0.0.1:8770/ （必须 http 伺服，file:// 打不开 ES 模块）
```

## 常见问题

- **404 了**：确认 Pages 目录选的是 `/docs` 不是 `/`（仓库根）；或 Actions 构建还没跑完。
- **页面白屏**：`docs/` 里 12 个网页文件都要在——`index.html` / `style.css` /
  `app.js` / `i18n.js` / `hud.js` / `live.js` / `matte-tools.js` / `mmd.js` /
  `arcam-core.js` / `arcam-render.js` / `arcam-lighting.js` / `matte.js`
  （引用关系见 `index.html` 的 script 标签与 `app.js` 顶部的动态 import），
  外加 `mmd/` 运行时目录；`vendor/` 是抠图模型，缺了只是抠图按钮不出现，
  合成功能不受影响。
- **想换主题/加功能**：页面三件套是 `docs/index.html` / `style.css` / `app.js`；
  数学内核 `arcam-core.js` 与合成 `arcam-render.js` 与 Python 侧同契约，
  改完用 `docs/tests/golden.json` 对拍（见 `docs/SITE-SPEC.md`）。
- **仓库带约 56MB 模型**：`vendor/` 合计 55,931,309 字节（isnet-anime-q8.onnx
  44,214,375 + ort-wasm 11,246,032 + ort.min.js 446,284 + wasm loader 24,618），
  其中 onnx 是抠图用的 ISNet-anime，sha256 `cb5f7850…77e3` 与上游写死值一致
  （`src/arcam/matting.py:46` `MODEL_SHA256`）；不想要抠图功能就删掉 `docs/vendor/`
  再推（页面会自动隐藏抠图按钮）。
