#!/usr/bin/env bash
# Ensures UI Streamer (STREAM=1) persists across sunnypilot and AGNOS updates
# Lives in /data/ — never wiped by updates
#
# Usage:
#   sudo /data/ensure_stream.sh
#
# What it does:
#   1. Adds STREAM=1 to launch_env.sh if missing
#   2. Installs a systemd service to do this on every boot

LAUNCH_ENV=/data/openpilot/launch_env.sh
SERVICE=/etc/systemd/system/ensure-stream.service

# 1. Inject STREAM vars into launch_env.sh if missing
if [ -f "$LAUNCH_ENV" ]; then
  grep -q '^export STREAM=1' "$LAUNCH_ENV" || echo 'export STREAM=1' >> "$LAUNCH_ENV"
  grep -q '^export STREAM_QUALITY=' "$LAUNCH_ENV" || echo 'export STREAM_QUALITY=100' >> "$LAUNCH_ENV"
  grep -q '^export STREAM_FPS=' "$LAUNCH_ENV" || echo 'export STREAM_FPS=20' >> "$LAUNCH_ENV"
fi

# 2. Recreate systemd service if missing (e.g. after AGNOS update)
if [ ! -f "$SERVICE" ]; then
  mount -o remount,rw / 2>/dev/null
  cat > "$SERVICE" << 'SVC'
[Unit]
Description=Ensure STREAM env vars in launch_env.sh
Before=openpilot.service
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/data/ensure_stream.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVC
  systemctl daemon-reload
  systemctl enable ensure-stream.service
fi
