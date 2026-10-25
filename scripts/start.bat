@echo off
REM 一键启动：后端 (8000) + 前端 (5173)，各开一个窗口。
REM
REM   scripts\start.bat           默认用回放缓存（开发调试 ¥0、结果确定）
REM   scripts\start.bat --real    先用真实语料初始化知识库
REM   scripts\start.bat --live    每次真调模型
REM
REM 用 Windows 的 Git Bash / WSL 时，scripts\start.sh 更好用
REM （可以 Ctrl-C 一次停掉两个服务，这里是两个独立窗口）。

setlocal
set ROOT=%~dp0..
set MODE=auto
set CORPUS=

if "%1"=="--real" set CORPUS=--real
if "%1"=="--live" set MODE=live
if "%2"=="--live" set MODE=live

pushd "%ROOT%"

where python >nul 2>&1
if errorlevel 1 (
    echo 未找到 python，请先安装并加入 PATH
    popd & exit /b 1
)

if not exist "frontend\node_modules" (
    echo 缺少前端依赖，请先执行：cd frontend ^&^& npm install
    popd & exit /b 1
)

REM 知识库为空时用自带语料兜底，否则检索全空、正文全是「资料未涉及」
pushd backend
python -m app.cli corpus init %CORPUS% --docs "%ROOT%\data\docs" >nul 2>&1
popd

echo 后端 http://127.0.0.1:8000    前端 http://localhost:5173    LLM_MODE=%MODE%
echo.

start "ReportFlow 后端" cmd /k "cd /d "%ROOT%\backend" && set LLM_MODE=%MODE% && python -m app.api --port 8000"
start "ReportFlow 前端" cmd /k "cd /d "%ROOT%\frontend" && npm run dev"

echo 已分别在新窗口启动。关闭对应窗口即可停止服务。
popd
endlocal
