@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
  echo Deployment incomplete. Read the error above, then run deploy.cmd again.
  exit /b 1
)
call "%~dp0start-web.cmd"
exit /b %errorlevel%
