@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "selection_ps=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "selection_ps=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%selection_ps%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1" %*
set "result=%errorlevel%"
if not "%result%"=="0" pause
endlocal & exit /b %result%
