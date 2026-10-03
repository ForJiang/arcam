#!/bin/bash
# ============================================================
# 启动AR取景器.command —— macOS 一键启动 arcam 网页界面
#
# 用法：
#   在 Finder 里双击本文件（系统会用「终端」打开它），或在终端里：
#     ./启动AR取景器.command            # 默认端口 8765
#     ./启动AR取景器.command 9000       # 换端口（第二个可选参数）
#
# 做什么（全程中文提示，失败给人话指引）：
#   1. 找 python3（只读探测，不碰你的全局 Python；顺序：PATH → /usr/bin
#      → Homebrew 三处 → /usr/local → python.org）;
#   2. 建/复用项目根目录里的 .venv（有 uv 用 uv，没有用 python3 -m venv）;
#   3. 装/确认依赖：只装 numpy + pillow，不引入任何第三方 web 框架
#      （pypi 离线则检查本机是否已可导入，通就直接继续）;
#   4. exec 启动网页服务并自动打开浏览器。
#
# 契约：docs/WEB-SPEC.md §4。入口固定 `arcam web --open --port`。
# ============================================================
set -euo pipefail

chmod +x "$0" 2>/dev/null || true          # 解压/clone 后丢了执行权限时自愈
cd "$(dirname "$0")"                        # 不管从哪里被调用，都回到项目根
PROJECT_DIR="$(pwd -P)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"

say() { printf '[AR取景器] %s\n' "$*"; }
die() { printf '[AR取景器] 出错：%s\n' "$*" >&2; exit 1; }

# ---------- 0. 端口（第二个可选参数，默认 8765） ----------
PORT="${1:-8765}"
case "$PORT" in
  ''|*[!0-9]*)
    die "端口号只能是数字，收到「${PORT}」。正确用法：./启动AR取景器.command 9000"
    ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  die "端口号要在 1–65535 之间，收到 ${PORT}。正确用法：./启动AR取景器.command 9000"
fi

# ---------- 1. 找 python3（≥ 3.14；不碰全局环境，只用来建项目内 .venv） ----------
# 顺序（写死，见 WEB-SPEC §4）：PATH → /usr/bin/python3 → Homebrew →
# /usr/local/bin/python3 → python.org 官方包
py_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)' >/dev/null 2>&1
}
PY3=""
# for 列表里的空串元素（command -v 失败时）与无匹配通配都天然被 [ -x ] 过滤掉
for c in "$(command -v python3 2>/dev/null || true)" \
         /usr/bin/python3 \
         /opt/homebrew/bin/python3 \
         /usr/local/bin/python3 \
         /home/linuxbrew/.linuxbrew/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/*/bin/python3 \
         /usr/local/bin/python3 ; do
  [ -n "$c" ] && [ -x "$c" ] || continue
  if py_ok "$c"; then PY3="$c"; break; fi
done
if [ -z "$PY3" ]; then
  die "这台电脑上没找到 Python 3.14 或更新的 python3（项目需要 ≥ 3.14，见 README「安装」）。
    任选其一，弄好后重新双击本脚本：
      1) 官网装一个：https://www.python.org/downloads/
      2) 有 Homebrew：brew install python@3.14
      3) 有 uv：uv python install 3.14"
fi
say "Python：${PY3}（$("$PY3" -V 2>&1)）"

# ---------- 2. .venv（只建在项目里；已存在直接复用，幂等） ----------
if [ -x "$VENV_PY" ]; then
  say "环境：项目里的 .venv 已就绪，跳过创建（重复双击不会重来、不会弄坏它）"
else
  if [ -e "$VENV_DIR" ]; then
    die ".venv 目录存在，但里头的 python 跑不起来（多半是上次创建被中断）。
    删掉本项目的 .venv 文件夹后重新双击本脚本：
      rm -rf \"$VENV_DIR\""
  fi
  if command -v uv >/dev/null 2>&1; then
    say "没有 .venv，正在用 uv 创建（uv venv，很快）…"
    uv venv "$VENV_DIR" --python "$PY3" || die "uv 创建 .venv 失败。
    可以删掉半成品后改用系统 venv 重来：
      rm -rf \"$VENV_DIR\" && \"$PY3\" -m venv \"$VENV_DIR\""
  else
    say "没有 .venv，正在用 python3 -m venv 创建（第一次要点时间）…"
    "$PY3" -m venv "$VENV_DIR" || die "python3 -m venv 创建 .venv 失败（常见原因：系统没装 venv 组件）。
    可以：
      1) 有 brew：brew install python@3.14 后重试；
      2) 或者装个 uv（https://docs.astral.sh/uv/ ），本脚本会优先用它。"
  fi
  [ -x "$VENV_PY" ] || die "创建完 .venv 但找不到 ${VENV_PY}。
    删掉 .venv 后重试，或换用 README「安装」里的手动步骤。"
fi

# ---------- 3. 依赖：numpy + pillow（先试 pip，失败退化为本机导入检查） ----------
say "正在确认依赖（numpy / pillow；只装这两个，不引入任何第三方 web 框架）…"
if "$VENV_PY" -m pip install numpy pillow >/dev/null 2>&1; then
  :
else
  say "pip 这条路没走通（本机 pypi 可能离线，或这个 .venv 里没有 pip）——转为本机导入检查…"
fi
if "$VENV_PY" -c 'import numpy, PIL' >/dev/null 2>&1; then
  say "依赖就绪：numpy / pillow 已可导入"
else
  die "这个 .venv 里既装不上、也没有现成的 numpy / pillow，网页服务的渲染核心就是它们，缺了跑不起来。
    任选其一，弄好后重新双击本脚本：
      1) 联网后执行：\"$VENV_PY\" -m pip install numpy pillow
      2) 有 uv 的话：uv pip install --python \"$VENV_PY\" numpy pillow
      3) 删掉 .venv 换一个装好 numpy/pillow 的 python3 再来：rm -rf \"$VENV_DIR\""
fi

# ---------- 4. 让 python 找得到 arcam 包 ----------
# 无论 .venv 是 pip install -e 装的、还是刚建的空 venv，都保证 src/ 在导入
# 路径上（README「安装」：不 pip install -e 也能 python -m arcam，src/ 上路径即可）。
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# ---------- 5. 端口占用预检（提前给人话，不等服务端崩栈） ----------
if "$VENV_PY" - "$PORT" <<'PYEOF' >/dev/null 2>&1
import socket
import sys

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PYEOF
then
  :
else
  die "端口 $PORT 已被别的程序占用，网页服务起不来。任选其一：
      1) 双击同目录的「停止AR取景器.command」，把上次没退干净的服务停掉；
      2) 换个端口再启动：./启动AR取景器.command 9000"
fi

# ---------- 6. 启动（exec 替换当前进程；关闭这个终端窗口 = 停止服务） ----------
say "网页服务启动中：http://127.0.0.1:${PORT}（浏览器会稍后自动打开）"
exec "$VENV_PY" -m arcam web --open --port "$PORT"
