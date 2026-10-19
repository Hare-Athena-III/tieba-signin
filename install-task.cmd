@echo off
chcp 936 >nul 2>&1
setlocal
rem ===========================================================================
rem  贴吧签到：注册计划任务（自动请求管理员权限），并立即试跑一次
rem
rem  这个文件必须是 GBK/ANSI 编码保存 —— cmd.exe 按当前代码页读 .bat，
rem  存成 UTF-8 的话中文全变乱码。
rem ===========================================================================

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

rem --- 没有管理员权限就自己提权重来一遍（会弹 UAC，点“是”）-------------------
net session >nul 2>&1
if errorlevel 1 (
    echo 需要管理员权限，正在请求提权（请在弹窗里点“是”）...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo === 已获得管理员权限 ===
echo.

echo 注册任务：每天 11:00 + 开机时（SYSTEM 身份，开机后最多等 120 秒让网络就绪）
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%\scripts\install-task.ps1" -Time 11:00 -AtStartup -NetworkWait 120
if errorlevel 1 (
    echo.
    echo 注册失败，后面的试跑跳过。按任意键关闭...
    pause >nul
    exit /b 1
)

echo.
echo === 立即试跑一次，确认 SYSTEM 身份下也能跑通（约 40 秒）===
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-ScheduledTask -TaskName TiebaSign"
echo 等待签到完成...
ping -n 76 127.0.0.1 >nul

echo.
echo === 任务状态与最近日志 ===
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%\scripts\status-task.ps1"

echo.
echo 按任意键关闭本窗口...
pause >nul
