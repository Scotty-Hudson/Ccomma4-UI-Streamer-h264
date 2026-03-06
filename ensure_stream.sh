#!/usr/bin/env bash
# Ensures UI Streamer (STREAM=1) persists across all updates and reboots
# Lives in /data/ — never wiped

patch_file() {
  local f="$1"
  [ -f "$f" ] || return
  grep -q '^export STREAM=1' "$f" || echo 'export STREAM=1' >> "$f"
  grep -q '^export STREAM_QUALITY=' "$f" || echo 'export STREAM_QUALITY=100' >> "$f"
  grep -q '^export STREAM_FPS=' "$f" || echo 'export STREAM_FPS=20' >> "$f"
}

# Patch live copy
patch_file /data/openpilot/launch_env.sh

# Patch any staged update waiting to be swapped in
patch_file /data/safe_staging/finalized/launch_env.sh

# Recreate systemd service if missing (e.g. after AGNOS update)
SERVICE=/etc/systemd/system/ensure-stream.service
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
