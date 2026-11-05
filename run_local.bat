@echo off
chcp 65001 >nul
echo ======================================================
echo Starting Linux Server & Mail Monitor Locally on Port 8080
echo ======================================================
cd /d "%~dp0"
if not exist venv (
    python -m venv venv
    call venv\Scripts\pip install -r requirements.txt
)
start http://localhost:8080
call venv\Scripts\python app.py
pause
