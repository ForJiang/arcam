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
import os
import subprocess
import sys
import urllib.request

REPO = sys.argv[1] if len(sys.argv) > 1 else "ForJiang/arcam"
BRANCH = sys.argv[2] if len(sys.argv) > 2 else "main"
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
    remote_tree = api("GET", f"/git/commits/{remote}")["tree"]["sha"]
    parent_tree = _git("rev-parse", f"{parent}^{{tree}}").stdout.strip()
    if remote_tree != parent_tree:
        sys.exit(f"远端与本地父提交内容也不同（{remote[:8]} vs {parent[:8]}）"
                 f"——真分叉，先对齐再推")
    print("注：远端 tip 是上一轮 API 重建的提交（树与本地父提交相同），"
          "新提交直接挂它", flush=True)

changed = _git("diff", "--name-only", f"{parent}..{local}").stdout.split()
print(f"变更 {len(changed)} 个文件", flush=True)

tree_entries = []
for path in changed:
    blob = _git("show", f"HEAD:{path}", binary=True).stdout
    b = api("POST", "/git/blobs", {
        "content": base64.b64encode(blob).decode(), "encoding": "base64"})
    tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": b["sha"]})
    print(f"  blob {path}", flush=True)

tree = api("POST", "/git/trees", {"base_tree": remote, "tree": tree_entries})
commit = api("POST", "/git/commits", {
    "message": _git("log", "-1", "--format=%B").stdout.strip(),
    "tree": tree["sha"], "parents": [remote]})
api("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": False})
print(f"已推送 {BRANCH} -> {commit['sha'][:8]}", flush=True)
