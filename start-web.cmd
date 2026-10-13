@echo off
setlocal
cd /d "%~dp0"
echo Keep this window open while using the local console.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1" %*
set "result=%errorlevel%"
if not "%result%"=="0" pause
endlocal & exit /b %result%
