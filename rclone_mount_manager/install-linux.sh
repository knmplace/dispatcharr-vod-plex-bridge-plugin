#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-/opt/rclone-mount-manager}"
APP_USER="${APP_USER:-root}"
APP_GROUP="${APP_GROUP:-root}"
PORT="${RCLONE_MANAGER_PORT:-5573}"
WEB_USER="${RCLONE_MANAGER_USER:-admin}"
WEB_PASS="${RCLONE_MANAGER_PASS:-ChangeMeNow!}"

mkdir -p "$APP_ROOT"
if [[ "$SCRIPT_DIR" != "$APP_ROOT" ]]; then
  cp -rf "$SCRIPT_DIR"/. "$APP_ROOT"
fi
chmod +x "$APP_ROOT/app.py"

cat > /etc/default/rclone-mount-manager <<EOF
RCLONE_MANAGER_PLATFORM=linux
RCLONE_MANAGER_PORT=$PORT
RCLONE_MANAGER_USER=$WEB_USER
RCLONE_MANAGER_PASS=$WEB_PASS
EOF
chmod 600 /etc/default/rclone-mount-manager

cat > /etc/systemd/system/rclone-mount-manager.service <<EOF
[Unit]
Description=Rclone Mount Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_ROOT
EnvironmentFile=/etc/default/rclone-mount-manager
ExecStart=/usr/bin/python3 $APP_ROOT/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rclone-mount-manager.service
systemctl restart rclone-mount-manager.service
systemctl status rclone-mount-manager.service --no-pager
