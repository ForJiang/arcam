#!/bin/bash
# ============================================================
# 停止AR取景器.command —— macOS：停掉占用端口（默认 8765）的网页服务
#
# 用法：
#   双击，或终端里：
#     ./停止AR取景器.command            # 默认端口 8765
#     ./停止AR取景器.command 9000       # 指定其它端口
#
# 做法：lsof 找 LISTEN 该端口的进程 → kill（SIGTERM）→ 不屈服再 kill -9；
#   查到没人监听就明说「无需停止」；杀不掉给下一句人话。
# 契约：docs/WEB-SPEC.md §4（可选级）。
# ============================================================
set -euo pipefail

chmod +x "$0" 2>/dev/null || true
cd "$(dirname "$0")"

PORT="${1:-8765}"
case "$PORT" in
  ''|*[!0-9]*)
    printf '[停止AR取景器] 出错：端口号只能是数字，收到「%s」。正确用法：./停止AR取景器.command 9000\n' "$PORT" >&2
    exit 1
    ;;
esac

# 只看 LISTEN 的进程；-sTCP:LISTEN 在老 lsof 上不灵时退回不过滤 state
list_pids() {
  local pids
  pids="$(lsof -nP -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -z "$pids" ]; then
    pids="$(lsof -nP -ti "tcp:$PORT" 2>/dev/null | grep -E '^[0-9]+$' || true)"
  fi
  printf '%s' "$pids"
}

PIDS="$(list_pids)"
if [ -z "$PIDS" ]; then
  echo "[停止AR取景器] 端口 $PORT 当前没有服务在监听（无需停止）。"
  exit 0
fi

for pid in $PIDS; do
  echo "[停止AR取景器] 正在结束占用端口 $PORT 的进程（PID ${pid}）…"
  kill "$pid" 2>/dev/null || true
done
sleep 1

LEFT="$(list_pids)"
if [ -n "$LEFT" ]; then
  echo "[停止AR取景器] 普通结束没成功，再强制一次（kill -9）…"
  for pid in $LEFT; do kill -9 "$pid" 2>/dev/null || true; done
  sleep 1
  LEFT="$(list_pids)"
fi

if [ -n "$LEFT" ]; then
  echo "[停止AR取景器] 仍没能结束这些 PID：$LEFT" >&2
  echo "    它们可能不属于当前用户（比如当初是用 sudo 起的）。手动处理：" >&2
  echo "      1) 看是谁占着：lsof -nP -iTCP:$PORT" >&2
  echo "      2) 结束它：    sudo kill -9 $LEFT" >&2
  echo "      3) 或者别跟它抢：换个端口启动 ./启动AR取景器.command 9000" >&2
  exit 1
fi

echo "[停止AR取景器] 已停止端口 $PORT 上的服务。"
