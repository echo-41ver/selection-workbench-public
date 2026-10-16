@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "selection_ps=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "selection_ps=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%selection_ps%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
  echo Deployment incomplete. Read the error above, then run deploy.cmd again.
  exit /b 1
)
call "%~dp0start-web.cmd"
exit /b %errorlevel%
