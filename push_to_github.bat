@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ======================================================
echo   Pushing Server Monitor v1.0 to GitHub:
echo   https://github.com/Samirlinux/server-monitor.git
echo ======================================================
echo.

if not exist .git (
    git init
    git branch -M main
)

git config user.name "Eng. Samir Abo El-Khair"
git config user.email "samirlinux@users.noreply.github.com"

git add .
git commit -m "Release v1.0: Linux Server, Uptime & Mail Monitor by Eng. Samir Abo El-Khair"

git remote remove origin 2>nul
git remote add origin https://github.com/Samirlinux/server-monitor.git
git push -u origin main --force

echo.
echo ======================================================
echo ✅ Done! Check your repository at:
echo    https://github.com/Samirlinux/server-monitor
echo.
echo To install on your Ubuntu server, run:
echo git clone https://github.com/Samirlinux/server-monitor.git /opt/server-monitor ^&^& cd /opt/server-monitor ^&^& sed -i "s/\r$//" install.sh ^&^& chmod +x install.sh ^&^& sudo ./install.sh
echo ======================================================
pause
