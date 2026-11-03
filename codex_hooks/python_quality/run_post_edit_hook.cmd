@echo off
setlocal
set "CODEX_PYTHON=%~1"
if not defined CODEX_PYTHON exit /b 2
if not exist "%CODEX_PYTHON%" exit /b 2
"%CODEX_PYTHON%" -B "%~dp0post_edit_checks.py"
exit /b %ERRORLEVEL%
