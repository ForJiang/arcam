#!/bin/bash
# 推送到 GitHub 并开启 GitHub Pages（/docs 目录）。
#
# 用法：
#   GITHUB_TOKEN=ghp_xxx tools/push-to-github.sh [仓库名] [public|private]
#
# Token 需要 repo 权限（Settings → Developer settings → Personal access tokens）。
# 不传 GITHUB_TOKEN 时脚本会给出指引并不做任何修改。幂等：重复跑只是再推一次。
set -euo pipefail

cd "$(dirname "$0")/.."

TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  cat >&2 <<'MSG'
未检测到 GITHUB_TOKEN。请先导出再跑：
  export GITHUB_TOKEN=ghp_你的token
  tools/push-to-github.sh [仓库名] [public|private]
（token 需要 repo 权限；用完即撤）
MSG
  exit 1
fi

REPO="${1:-arcam}"
VIS="${2:-public}"
API=https://api.github.com
AUTH="Authorization: Bearer $TOKEN"
VER=2022-11-28

echo "[push] 取用户名…"
USER=$(curl -sf -H "$AUTH" -H "X-GitHub-Api-Version: $VER" "$API/user" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["login"])')
[ -n "$USER" ] || { echo "token 无效或网络不通" >&2; exit 1; }
echo "[push] 用户：$USER"

echo "[push] 建仓库（已存在则跳过）…"
curl -sf -H "$AUTH" -H "X-GitHub-Api-Version: $VER" \
  -d "{\"name\":\"$REPO\",\"private\":$([ "$VIS" = private ] && echo true || echo false),\"has_issues\":true}" \
  "$API/user/repos" >/dev/null 2>&1 || echo "（仓库可能已存在，继续）"

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "https://github.com/$USER/$REPO.git"
fi
git remote set-url origin "https://x-access-token:$TOKEN@github.com/$USER/$REPO.git"

echo "[push] 推送 main…"
git branch -M main
git push -u origin main --quiet
git remote set-url origin "https://github.com/$USER/$REPO.git"   # 撤掉 URL 里的 token

echo "[push] 开启 Pages（main 分支 /docs）…"
curl -sf -X POST -H "$AUTH" -H "X-GitHub-Api-Version: $VER" \
  -d '{"source":{"branch":"main","path":"/docs"}}' \
  "$API/repos/$USER/$REPO/pages" >/dev/null 2>&1 \
  || curl -sf -X PUT -H "$AUTH" -H "X-GitHub-Api-Version: $VER" \
       -d '{"source":{"branch":"main","path":"/docs"}}' \
       "$API/repos/$USER/$REPO/pages" >/dev/null 2>&1 \
  || echo "（Pages 开启失败就手动：Settings → Pages → main /docs）"

cat <<MSG

[push] 完成。
  仓库：https://github.com/$USER/$REPO
  网页：https://$USER.github.io/$REPO/ （Pages 首次构建约 1-2 分钟）

MSG
