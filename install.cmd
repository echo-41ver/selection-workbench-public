@echo off
setlocal
cd /d "%~dp0"
call "%~dp0deploy.cmd" %*
set "result=%errorlevel%"
if not "%result%"=="0" pause
endlocal & exit /b %result%
