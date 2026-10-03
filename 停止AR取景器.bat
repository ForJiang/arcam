@echo off
rem ============================================================
rem 停止AR取景器.bat —— Windows：停掉占用端口（默认 8765）的网页服务
rem
rem 用法：
rem   双击本文件；或命令行里：
rem     停止AR取景器.bat          # 默认端口 8765
rem     停止AR取景器.bat 9000     # 指定其它端口
rem
rem 做法：netstat 找 LISTENING 该端口的进程 → taskkill /F；
rem   查到没人监听就明说「无需停止」；杀不掉给下一句人话（多半是权限）。
rem 契约：docs/WEB-SPEC.md §4（可选级）。
rem ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

rem ---------- 端口（第一个可选参数，默认 8765） ----------
set "PORT=8765"
if not "%~1"=="" set "PORT=%~1"
echo %PORT%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 (
  echo [停止AR取景器] 出错：端口号只能是数字，收到「%PORT%」。
  echo                  正确用法：停止AR取景器.bat 9000
  pause
  exit /b 1
)

set "FOUND=0"
for /f "tokens=5" %%i in ('netstat -ano ^| findstr /c:":%PORT% " ^| findstr /c:"LISTENING"') do (
  set "FOUND=1"
  echo [停止AR取景器] 正在结束占用端口 %PORT% 的进程（PID %%i）...
  taskkill /F /PID %%i >nul 2>&1
  if !errorlevel!==0 (
    echo [停止AR取景器] 已结束 PID %%i。
  ) else (
    echo [停止AR取景器] 没能结束 PID %%i —— 多半是权限不足：右键本脚本、「以管理员身份运行」再试。
  )
)
if "!FOUND!"=="0" echo [停止AR取景器] 端口 %PORT% 当前没有服务在监听（无需停止）。
echo.
echo [停止AR取景器] 完成。按任意键关闭本窗口。
pause >nul
exit /b 0
