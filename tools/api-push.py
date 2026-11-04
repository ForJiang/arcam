#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ForJiang
# SPDX-License-Identifier: MIT
"""API 分步推送（git 协议不通时的备用路径，见 memory: github-api-push-workaround）。

git 协议失败但 api.github.com 正常时：blob → tree(base_tree) → commit(parent=远端
tip) → PATCH refs。令牌两条来路（先环境变量，后 git 凭据）：
  GH_TOKEN=... python3 tools/api-push.py <owner/repo> <branch>
  或先 git credential approve（git credential fill 能返回 password= 即可）。
"""
import base64
import json
import re
import os
import subprocess
import sys
import urllib.request

ARGS = sys.argv[1:]
FORCE = "--force" in ARGS
FULL = "--full" in ARGS          # 全量同步本地树（远端树从零重建，清历史漂移）
POS = [a for a in ARGS if not a.startswith("--")]
REPO = POS[0] if len(POS) > 0 else "ForJiang/arcam"
BRANCH = POS[1] if len(POS) > 1 else "main"
TOKEN = os.environ.get("GH_TOKEN", "").strip()
if not TOKEN:
    _fill = subprocess.run(
        ["git", "credential", "fill"], input="host=github.com\nprotocol=https\n",
        capture_output=True, text=True).stdout
    # 没配 git 凭据时原来会在这里抛 StopIteration（一串看不懂的调用栈）—— 说人话
    _pw = [l.split("=", 1)[1].strip() for l in _fill.splitlines() if l.startswith("password=")]
    TOKEN = _pw[0] if _pw else ""
if not TOKEN:
    sys.exit("拿不到 GitHub 令牌：GH_TOKEN 环境变量为空，且 git credential fill 没返回 "
             "password=（先 export GH_TOKEN=ghp_... 或 git credential approve；"
             "也可改用 tools/push-to-github.sh 的 GITHUB_TOKEN）")
API = f"https://api.github.com/repos/{REPO}"


def _git(*args, binary=False):
    """git 子进程统一走这里：core.quotepath=false —— 默认转义会把非 ASCII
    文件名（启动AR取景器.command 这类）变成带引号的 "\345..." 串，diff 的
    输出再传回 git show 就成了不存在的路径（v1.13.2 实测）。blob 取内容那步
    要 binary=True：base64 编码需要原始字节，text 模式的解码/回编码虽可经
    surrogateescape 往返，但直取字节更干净。"""
    return subprocess.run(["git", "-c", "core.quotepath=false", *args],
                          capture_output=True, text=not binary)


def api(method, path, body=None):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # 令牌过期/权限不够是这条备用路径最常见的失败，别吐 traceback
        if e.code in (401, 403):
            sys.exit(f"GitHub API {e.code}：令牌无效或权限不足"
                     f"（{API}{path}）——换有效 token 再跑")
        raise


remote = api("GET", f"/git/refs/heads/{BRANCH}")["object"]["sha"]
local = _git("rev-parse", "HEAD").stdout.strip()
parent = _git("rev-parse", "HEAD~1").stdout.strip()
print(f"远端 {BRANCH}: {remote[:8]}  本地 HEAD: {local[:8]}  parent: {parent[:8]}", flush=True)

# 历史对齐：本工具每轮都经 API 重建提交（author/committer 时间戳与本地不同），
# 于是远端 tip 与本地父提交 **SHA 不同但树相同**——第二轮起必现。内容一致就在
# 此收口（新提交仍挂 remote 为 parent，下面照旧）；树也不同才是真分叉，拒绝。
if remote != parent:
    if FORCE:
        print(f"⚠ --force：远端 tip 与本地父提交不同（{remote[:8]} vs {parent[:8]}），"
              f"按你的要求直接覆盖远端——确认本地已包含远端全部内容", flush=True)
    else:
        remote_tree = api("GET", f"/git/commits/{remote}")["tree"]["sha"]
        parent_tree = _git("rev-parse", f"{parent}^{{tree}}").stdout.strip()
        if remote_tree != parent_tree:
            sys.exit(f"远端与本地父提交内容也不同（{remote[:8]} vs {parent[:8]}）"
                     f"——真分叉，先对齐再推（确信本地包含远端全部内容时可用 --force）")
        print("注：远端 tip 是上一轮 API 重建的提交（树与本地父提交相同），"
              "新提交直接挂它", flush=True)

if FULL:
    # 全量同步：远端树从零重建（不带 base_tree）——增量推送只补最新一次提交的
    # diff，早先轮次积累的任何偏差会永远留在远端树里（v1.13.2 实测树哈希对不上
    # 就是这么来的）。--full 让远端树严格等于本地 HEAD 树。
    #
    # blob 直接引用 `ls-files -s` 给出的本地 SHA、不走 /git/blobs 上传：那些 blob
    # 全都已经在 GitHub 的对象库里（vendored 大文件来自早期的 git 直推、其余来自
    # 历轮 API 推送），而 blob API 对 1MB+ 的文件会 422 "input was too large"
    # （docs/mmd/three.module.js 实测）。mode 也照 ls-files -s 的实际值带
    # （100755 的可执行脚本不再硬编码 100644）。
    changed = []
    for l in _git("ls-files", "-s").stdout.splitlines():
        meta, path = l.split("\t", 1)
        mode, sha = meta.split()[:2]
        changed.append((path, mode, sha))
    print(f"--full：全量同步 {len(changed)} 个被跟踪文件（blob 引用本地 SHA，零上传）",
          flush=True)
    tree_entries = [{"path": path, "mode": mode, "type": "blob", "sha": sha}
                    for path, mode, sha in changed]
else:
    changed = _git("diff", "--name-only", f"{parent}..{local}").stdout.split()
    print(f"变更 {len(changed)} 个文件", flush=True)
    tree_entries = []
    for path in changed:
        blob = _git("show", f"HEAD:{path}", binary=True).stdout
        b = api("POST", "/git/blobs", {
            "content": base64.b64encode(blob).decode(), "encoding": "base64"})
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": b["sha"]})
        print(f"  blob {path}", flush=True)

tree_kw = {"tree": tree_entries}
if not FULL:
    tree_kw["base_tree"] = remote
# 建树 + 补传缺失 blob 的循环：--full 直接引用本地 blob SHA，其中可能有「本地新
# 提交、还没上传过」的 blob（比如刚改的 LICENSE），GitHub 会 422 并在报错里点名
# 那个 SHA —— 补传它再试，直到建树成功（一般 1-2 轮；上限 = 文件数，防死循环）。
for _attempt in range(len(tree_entries) + 1):
    try:
        tree = api("POST", "/git/trees", tree_kw)
        break
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        m = re.search(r"tree\.sha ([0-9a-f]{40}) is not a valid blob", body)
        if m is None or not FULL:
            raise
        missing = m.group(1)
        # 按 SHA 反查文件名（本地索引里有它的内容）
        content = _git("cat-file", "blob", missing, binary=True).stdout
        b = api("POST", "/git/blobs", {
            "content": base64.b64encode(content).decode(), "encoding": "base64"})
        assert b["sha"] == missing, "补传的 blob SHA 与本地不一致"
        print(f"  补传缺失 blob {missing[:10]}（{len(content)} 字节）", flush=True)
else:
    sys.exit("建树失败：缺失 blob 超过文件数，不可能——查逻辑")
commit = api("POST", "/git/commits", {
    "message": _git("log", "-1", "--format=%B").stdout.strip(),
    "tree": tree["sha"], "parents": [remote]})
api("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": FORCE})
print(f"已推送 {BRANCH} -> {commit['sha'][:8]}", flush=True)
