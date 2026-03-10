#!/usr/bin/env bash
# Ensures UI Streamer persists across all sunnypilot updates and reboots.
# Lives in /data/ — never wiped by updates.
#
# What this does (on every boot):
#   1. Injects STREAM=1 into launch_env.sh (env vars)
#   2. Downloads ui_stream.py if missing
#   3. Patches application.py with stream hooks (if not already patched)
#   4. Installs itself as a systemd service (runs before openpilot)

set -e

STREAM_REPO="https://raw.githubusercontent.com/peterclampton/Comma4-UI-Streamer/main"

# ---------- 1. Patch launch_env.sh with STREAM env vars ----------

patch_env() {
  local f="$1"
  [ -f "$f" ] || return
  grep -q '^export STREAM=1' "$f" || echo 'export STREAM=1' >> "$f"
  grep -q '^export STREAM_QUALITY=' "$f" || echo 'export STREAM_QUALITY=100' >> "$f"
  grep -q '^export STREAM_FPS=' "$f" || echo 'export STREAM_FPS=20' >> "$f"
}

# Patch live copy
patch_env /data/openpilot/launch_env.sh

# Patch any staged update waiting to be swapped in
patch_env /data/safe_staging/finalized/launch_env.sh

echo "[ensure_stream] env vars OK"

# ---------- 2. Ensure ui_stream.py exists ----------

if [ ! -f /data/ui_stream.py ]; then
  echo "[ensure_stream] Downloading ui_stream.py..."
  curl -sL "$STREAM_REPO/ui_stream.py" -o /data/ui_stream.py
  echo "[ensure_stream] ui_stream.py downloaded"
else
  echo "[ensure_stream] ui_stream.py exists"
fi

# ---------- 3. Ensure stream_patch.py exists ----------

if [ ! -f /data/stream_patch.py ]; then
  echo "[ensure_stream] Downloading stream_patch.py..."
  curl -sL "$STREAM_REPO/stream_patch.py" -o /data/stream_patch.py
  echo "[ensure_stream] stream_patch.py downloaded"
else
  echo "[ensure_stream] stream_patch.py exists"
fi

# ---------- 4. Patch application.py (idempotent) ----------

echo "[ensure_stream] Checking application.py patch..."
python3 /data/stream_patch.py

# ---------- 5. Install systemd service (if missing) ----------

SERVICE=/etc/systemd/system/ensure-stream.service
if [ ! -f "$SERVICE" ]; then
  echo "[ensure_stream] Installing systemd service..."
  mount -o remount,rw / 2>/dev/null || true
  cat > "$SERVICE" << 'SVC'
[Unit]
Description=Ensure UI Streamer persists across updates
Before=openpilot.service
After=local-fs.target network.target

[Service]
Type=oneshot
ExecStart=/data/ensure_stream.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVC
  systemctl daemon-reload
  systemctl enable ensure-stream.service
  echo "[ensure_stream] Service installed"
else
  echo "[ensure_stream] Service exists"
fi

echo "[ensure_stream] Done"
