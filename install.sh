#!/usr/bin/env bash
set -e

echo "======================================================"
echo "🚀 Linux Server, Uptime & Mail Monitor — Version 1.0"
echo "👨‍💻 Developed by Eng. Samir Abo El-Khair"
echo "======================================================"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8080

# 1. Install Python3 & venv
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip curl
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip curl
elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip curl
fi

# 2. Setup Python virtual environment
cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. Create systemd service for 24/7 operation
SERVICE_FILE="/etc/systemd/system/server-monitor.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Linux Server & Mail Uptime Monitor (5s Real-Time)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable server-monitor
systemctl restart server-monitor

# 4. Open firewall port if ufw or firewalld is active
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "active"; then
    ufw allow ${PORT}/tcp >/dev/null 2>&1 || true
fi
if command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=${PORT}/tcp >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
fi

SERVER_IP=$(curl -s https://api.ipify.org || hostname -I | awk '{print $1}')

echo "======================================================"
echo "✅ تم تشغيل النظام بنجاح ويعمل الآن في الخلفية 24/7!"
echo "🌐 افتح لوحة التحكم من المتصفح عبر الرابط:"
echo "   http://${SERVER_IP}:${PORT}"
echo "======================================================"
