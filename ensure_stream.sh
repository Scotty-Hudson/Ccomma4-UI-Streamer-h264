#!/usr/bin/env bash
# Ensures UI Streamer persists across all sunnypilot updates and reboots.
# Lives in /data/ — never wiped by updates.
#
# How it works (zero application.py patching):
#   1. Downloads stream files to /data/ if missing
#   2. Installs a .pth file in Python site-packages that loads stream_hook.py
#      at Python startup — this monkeypatches GuiApplication at import time
#   3. Installs itself as a systemd service
#
# The .pth file and /data/ files are OUTSIDE the openpilot git repo,
# so they survive git resets, overlay swaps, and sunnypilot updates.

set -e

STREAM_BRANCH="${STREAM_BRANCH:-take_over_bug_fix}"
STREAM_REPO="https://raw.githubusercontent.com/Scotty-Hudson/Ccomma4-UI-Streamer-h264/${STREAM_BRANCH}"

# ---------- 1. Download / update stream files in /data/ ----------
# Always re-download to pick up fixes.  Atomic: write to .tmp then mv,
# so existing file is preserved if curl fails (e.g. no internet).

for f in ui_stream.py ui_frame_bridge.py stream_hook.py; do
  echo "[ensure_stream] Updating $f from $STREAM_BRANCH ..."
  if curl -fsSL "$STREAM_REPO/$f" -o "/data/$f.tmp"; then
    mv "/data/$f.tmp" "/data/$f"
    echo "[ensure_stream] $f updated"
  else
    rm -f "/data/$f.tmp"
    if [ -f "/data/$f" ]; then
      echo "[ensure_stream] $f download failed — keeping existing copy"
    else
      echo "[ensure_stream] ERROR: $f missing and download failed"
      exit 1
    fi
  fi
done

echo "[ensure_stream] stream files OK"

# ---------- 2. Install .pth file in Python site-packages ----------

# Find Python 3 site-packages directory
SITE_DIR=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)

if [ -z "$SITE_DIR" ]; then
  echo "[ensure_stream] ERROR: could not find Python site-packages"
  exit 1
fi

PTH_FILE="$SITE_DIR/comma_stream.pth"

# .pth contents: add /data to path, then import the hook module
PTH_CONTENT="/data
import stream_hook"

if [ ! -f "$PTH_FILE" ] || [ "$(cat "$PTH_FILE" 2>/dev/null)" != "$PTH_CONTENT" ]; then
  echo "[ensure_stream] Installing .pth file to $PTH_FILE..."
  mount -o remount,rw / 2>/dev/null || true
  echo "$PTH_CONTENT" > "$PTH_FILE"
  echo "[ensure_stream] .pth file installed"
else
  echo "[ensure_stream] .pth file exists"
fi

echo "[ensure_stream] Python hook OK"

# ---------- 3. Restore application.py if previously patched ----------

# Clean up old patches from application.py (from the old stream_patch.py approach)
for app_py in /data/openpilot/system/ui/lib/application.py /data/openpilot/openpilot/system/ui/lib/application.py; do
  bak="${app_py}.bak"
  if [ -f "$bak" ]; then
    echo "[ensure_stream] Restoring $app_py from backup (removing old patches)..."
    cp "$bak" "$app_py"
    rm -f "$bak"
  fi
done

# ---------- 4. Install systemd service (if missing) ----------

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
