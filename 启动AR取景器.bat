@echo off
rem ============================================================
rem 启动AR取景器.bat —— Windows 一键启动 arcam 网页界面
rem
rem 用法：
rem   双击本文件；或命令行里：
rem     启动AR取景器.bat          # 默认端口 8765
rem     启动AR取景器.bat 9000     # 换端口（第二个可选参数）
rem
rem 做什么（全程中文提示，失败给人话指引）：
rem   1. 找 python（py -3 优先，其次 python；只读探测，不碰你的全局 Python）;
rem   2. 校验端口（第二个可选参数，默认 8765）;
rem   3. 建/复用项目根目录里的 .venv（有 uv 用 uv，没有用 venv 模块）;
rem   4. 装/确认依赖：只装 numpy + pillow，不引入任何第三方 web 框架
rem      （pypi 离线则检查本机是否已可导入，通就直接继续）;
rem   5. 启动网页服务（浏览器由 --open 负责打开），端口默认 8765。
rem
rem 契约：docs/WEB-SPEC.md §4。入口固定 `arcam web --open --port`。
rem chcp 65001：保证中文提示不乱码（本文件为 UTF-8）。
rem ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"
set "PROJECT_DIR=%CD%"

rem ---------- 1. 找 python（py -3 → python） ----------
set "PY="
where py >nul 2>&1
if !errorlevel!==0 (set "PY=py -3")
if not defined PY (
  where python >nul 2>&1
  if !errorlevel!==0 (set "PY=python")
)
if not defined PY (
  echo [AR取景器] 出错：这台电脑上没找到 Python。
  echo             装一个 Python 3.14 或更新版后重新双击本脚本：
  echo               - 官网：https://www.python.org/downloads/ ^(安装时勾选 "Add python.exe to PATH"^)
  echo               - 或者先装 uv：https://docs.astral.sh/uv/ ^(再双击本脚本^)
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,14) else 1)" >nul 2>&1
if !errorlevel! neq 0 (
  echo [AR取景器] 出错：找到的 Python 版本低于 3.14，项目需要 3.14 或更新（见 README「安装」）。
  echo             当前版本：
  %PY% -V
  pause
  exit /b 1
)
echo [AR取景器] Python：%PY%

rem ---------- 2. 端口（第二个可选参数，默认 8765） ----------
set "PORT=8765"
if not "%~1"=="" set "PORT=%~1"
echo %PORT%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 goto badport
%PY% -c "import sys; p=int(sys.argv[1]); sys.exit(0 if 1<=p<=65535 else 1)" %PORT% >nul 2>&1
if errorlevel 1 goto badport
goto portok
:badport
echo [AR取景器] 出错：端口号要是 1-65535 之间的数字，收到「%PORT%」。
echo             正确用法：启动AR取景器.bat 9000
pause
exit /b 1
:portok

rem ---------- 3. .venv（只建在项目里；已存在直接复用，幂等） ----------
set "VENV_PY=.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
  echo [AR取景器] 环境：项目里的 .venv 已就绪，跳过创建（重复双击不会重来、不会弄坏它）
) else (
  if exist ".venv" (
    echo [AR取景器] 出错：.venv 目录存在，但里头的 python 跑不起来（多半是上次创建被中断）。
    echo             删掉项目里的 .venv 文件夹后重新双击本脚本。
    pause
    exit /b 1
  )
  where uv >nul 2>&1
  if !errorlevel!==0 (
    echo [AR取景器] 没有 .venv，正在用 uv 创建（uv venv，很快）...
    uv venv .venv
  ) else (
    echo [AR取景器] 没有 .venv，正在用 python -m venv 创建（第一次要点时间）...
    %PY% -m venv .venv
  )
  if errorlevel 1 (
    echo [AR取景器] 出错：创建 .venv 失败。
    echo             常见原因：这个 Python 没装 venv 组件 —— 重装 Python 时勾选 "pip" 与 "venv"；
    echo             或者改用 uv（https://docs.astral.sh/uv/ ）后再双击本脚本。
    pause
    exit /b 1
  )
  if not exist "%VENV_PY%" (
    echo [AR取景器] 出错：创建完 .venv 但没找到 .venv\Scripts\python.exe。
    echo             删掉 .venv 后重试，或换用 README「安装」里的手动步骤。
    pause
    exit /b 1
  )
)

rem ---------- 4. 依赖：numpy + pillow（先试 pip，失败退化为本机导入检查） ----------
echo [AR取景器] 正在确认依赖（numpy / pillow；只装这两个，不引入任何第三方 web 框架）...
"%VENV_PY%" -m pip install numpy pillow >nul 2>&1
if errorlevel 1 (
  echo [AR取景器] pip 这条路没走通（本机 pypi 可能离线，或这个 .venv 里没有 pip）——转为本机导入检查...
)
"%VENV_PY%" -c "import numpy, PIL" >nul 2>&1
if errorlevel 1 (
  echo [AR取景器] 出错：这个 .venv 里既装不上、也没有现成的 numpy / pillow，网页服务的渲染核心就是它们，缺了跑不起来。
  echo             任选其一，弄好后重新双击本脚本：
  echo               1^) 联网后执行："%VENV_PY%" -m pip install numpy pillow
  echo               2^) 有 uv 的话：uv pip install --python "%VENV_PY%" numpy pillow
  echo               3^) 删掉 .venv 换一个装好 numpy/pillow 的 Python 再来
  pause
  exit /b 1
)
echo [AR取景器] 依赖就绪：numpy / pillow 已可导入

rem ---------- 5. 让 python 找得到 arcam 包 ----------
rem 无论 .venv 是 pip install -e 装的、还是刚建的空 venv，都保证 src\ 在导入
rem 路径上（README「安装」：不 pip install -e 也能 python -m arcam，src 上路径即可）。
if defined PYTHONPATH (set "PYTHONPATH=%PROJECT_DIR%\src;%PYTHONPATH%") else (set "PYTHONPATH=%PROJECT_DIR%\src")

rem ---------- 6. 端口占用预检（提前给人话，不等服务端崩栈） ----------
"%VENV_PY%" -c "import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('0.0.0.0', int(sys.argv[1]))); s.close()" %PORT% >nul 2>&1
if errorlevel 1 (
  echo [AR取景器] 出错：端口 %PORT% 已被别的程序占用，网页服务起不来。任选其一：
  echo               1^) 双击同目录的「停止AR取景器.bat」，把上次没退干净的服务停掉；
  echo               2^) 换个端口再启动：启动AR取景器.bat 9000
  pause
  exit /b 1
)

rem ---------- 7. 启动（关闭本窗口 = 停止服务；浏览器由 --open 负责打开） ----------
echo [AR取景器] 网页服务启动中：http://127.0.0.1:%PORT%（浏览器会稍后自动打开）
"%VENV_PY%" -m arcam web --open --port %PORT%
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo [AR取景器] 服务停止了（退出码 %RC%）。上面若有报错，按提示处理后重新双击本脚本。
  pause
)
exit /b %RC%
