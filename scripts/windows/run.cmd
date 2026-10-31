@echo off
rem Runs the Yordamchi personal bot in this terminal (scripts\windows\run.py). Ctrl+C stops it.
rem Exit 3 means the runner updated itself from GitHub: start the new one.
cd /d "%~dp0..\.."
:loop
uv run python scripts\windows\run.py %*
if errorlevel 3 if not errorlevel 4 goto loop
exit /b %errorlevel%
