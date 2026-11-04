@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%~dp0.venv\Scripts\python.exe" "%~dp0gflow.py" %*
