#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${APP_ROOT:-/boot/config/plugins/rclone-mount-manager}"
APP_DIR="$BASE_DIR/app"
ENV_FILE="$BASE_DIR/manager.env"
START_MANAGER="$BASE_DIR/start-manager.sh"
STOP_MANAGER="$BASE_DIR/stop-manager.sh"
START_MOUNTS="$BASE_DIR/start-mounts.sh"
STOP_MOUNTS="$BASE_DIR/stop-mounts.sh"
PORT="${RCLONE_MANAGER_PORT:-5573}"
WEB_USER="${RCLONE_MANAGER_USER:-admin}"
WEB_PASS="${RCLONE_MANAGER_PASS:-ChangeMeNow!}"

if [[ -f /boot/config/plugins/rclone/.rclone.conf ]]; then
  RCLONE_CONF_DEFAULT="/boot/config/plugins/rclone/.rclone.conf"
else
  RCLONE_CONF_DEFAULT="/boot/config/rclone/rclone.conf"
fi
RCLONE_CONF_PATH="${RCLONE_CONFIG_FILE:-$RCLONE_CONF_DEFAULT}"

mkdir -p "$APP_DIR" "$BASE_DIR/logs" "$BASE_DIR/mounts" "$BASE_DIR/pids"
cp -rf "$SCRIPT_DIR"/. "$APP_DIR"
chmod +x "$APP_DIR/app.py" "$APP_DIR/install.sh" "$APP_DIR/install-linux.sh" "$APP_DIR/install-unraid.sh"

cat > "$ENV_FILE" <<EOF
RCLONE_MANAGER_PLATFORM=unraid
RCLONE_MANAGER_PORT=$PORT
RCLONE_MANAGER_USER=$WEB_USER
RCLONE_MANAGER_PASS=$WEB_PASS
RCLONE_MANAGER_UNRAID_DIR=$BASE_DIR
RCLONE_CONFIG_FILE=$RCLONE_CONF_PATH
EOF
chmod 600 "$ENV_FILE"

cat > "$START_MANAGER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$BASE_DIR/manager.env"
PID_FILE="$BASE_DIR/pids/manager.pid"
LOG_FILE="$BASE_DIR/logs/manager.log"
APP_DIR="$BASE_DIR/app"
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    exit 0
  fi
fi
nohup /usr/bin/python3 "$APP_DIR/app.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
EOF

cat > "$STOP_MANAGER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$BASE_DIR/pids/manager.pid"
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${PID:-}" ]]; then
    kill "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi
EOF

cat > "$START_MOUNTS" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$BASE_DIR/manager.env"
APP_DIR="$BASE_DIR/app"
/usr/bin/python3 "$APP_DIR/app.py" --start-all >> "$BASE_DIR/logs/mounts-bootstrap.log" 2>&1 || true
EOF

cat > "$STOP_MOUNTS" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$BASE_DIR/manager.env"
APP_DIR="$BASE_DIR/app"
/usr/bin/python3 "$APP_DIR/app.py" --stop-all >> "$BASE_DIR/logs/mounts-bootstrap.log" 2>&1 || true
EOF

chmod +x "$START_MANAGER" "$STOP_MANAGER" "$START_MOUNTS" "$STOP_MOUNTS"

GO_FILE="/boot/config/go"
BEGIN_MARKER="# BEGIN RCLONE MOUNT MANAGER"
END_MARKER="# END RCLONE MOUNT MANAGER"
BLOCK=$(cat <<EOF
$BEGIN_MARKER
bash $START_MANAGER
(
  for _ in \$(seq 1 90); do
    /bin/mountpoint -q /mnt/user && break
    sleep 2
  done
  bash $START_MOUNTS
) &
$END_MARKER
EOF
)

touch "$GO_FILE"
TMP_GO="$(mktemp)"
awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0==begin {skip=1; next}
  $0==end {skip=0; next}
  !skip {print}
' "$GO_FILE" > "$TMP_GO"
printf '\n%s\n' "$BLOCK" >> "$TMP_GO"
cp "$TMP_GO" "$GO_FILE"
rm -f "$TMP_GO"

bash "$START_MANAGER"
(
  for _ in $(seq 1 20); do
    /bin/mountpoint -q /mnt/user && break
    sleep 1
  done
  bash "$START_MOUNTS"
) &

echo "Installed Unraid mode into $BASE_DIR"
echo "Web UI startup is now added to /boot/config/go"
echo "If you use the User Scripts plugin, you can also call:"
echo "  $START_MANAGER"
echo "  $START_MOUNTS"
