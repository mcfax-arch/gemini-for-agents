@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHONUTF8=1"
cd /d "%ROOT%"
python launch_gemini_web2api.py
exit /b %ERRORLEVEL%
