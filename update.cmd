@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\apply_update.ps1" %*
set "selection_update_exit=%errorlevel%"
if not "%selection_update_exit%"=="0" pause
exit /b %selection_update_exit%
