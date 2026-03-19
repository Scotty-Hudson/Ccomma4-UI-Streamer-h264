#!/usr/bin/env bash
# Ensures UI Streamer persists across all sunnypilot updates and reboots.
# Lives in /data/ — never wiped by updates.
#
# How it works:
#   1. Injects STREAM=1 into launch_env.sh (env vars)
#   2. Adds a boot-patch line to launch_env.sh that re-patches application.py
#      every time openpilot starts (idempotent, survives overlay resets)
#   3. Downloads stream files to /data/ if missing or outdated
#   4. Patches application.py with stream hooks (for this session)
#
# The patch only runs inside the UI process (selfdrive.ui.ui).
# Zero footprint in controlsd, paramsd, or any safety-critical process.

set -e

STREAM_BRANCH="${STREAM_BRANCH:-take_over_bug_fix}"
STREAM_REPO="https://raw.githubusercontent.com/Scotty-Hudson/Ccomma4-UI-Streamer-h264/${STREAM_BRANCH}"

# ---------- 1. Patch launch_env.sh with STREAM env vars + boot patch ----------

# The boot-patch line runs stream_patch.py every time openpilot starts.
# launch_env.sh lives on /data/ so it survives overlay resets.
# stream_patch.py is idempotent — if already patched, it exits instantly.
PATCH_LINE='[ -f /data/stream_patch.py ] && python3 /data/stream_patch.py 2>/dev/null || true'

patch_env() {
  local f="$1"
  [ -f "$f" ] || return
  grep -q '^export STREAM=1' "$f" || echo 'export STREAM=1' >> "$f"
  grep -q '^export STREAM_QUALITY=' "$f" || echo 'export STREAM_QUALITY=50' >> "$f"
  grep -q '^export STREAM_FPS=' "$f" || echo 'export STREAM_FPS=10' >> "$f"
  grep -q 'stream_patch.py' "$f" || echo "$PATCH_LINE" >> "$f"
}

# Patch live copy
patch_env /data/openpilot/launch_env.sh

# Patch any staged update waiting to be swapped in
patch_env /data/safe_staging/finalized/launch_env.sh

echo "[ensure_stream] env vars + boot patch OK"

# ---------- 2. Download / update stream files in /data/ ----------
# Always re-download to pick up fixes.  Atomic: write to .tmp then mv.

for f in ui_stream.py ui_frame_bridge.py stream_patch.py; do
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

# ---------- 3. Remove old .pth hook (if present) ----------
# The .pth approach caused "Take Over Immediately" by running code in
# safety-critical processes.  Clean it up completely.

SITE_DIR=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || true)
if [ -n "$SITE_DIR" ] && [ -f "$SITE_DIR/comma_stream.pth" ]; then
  echo "[ensure_stream] Removing old .pth hook..."
  mount -o remount,rw / 2>/dev/null || true
  rm -f "$SITE_DIR/comma_stream.pth"
  echo "[ensure_stream] .pth hook removed"
fi

# Clean up old hook files (no longer needed)
rm -f /data/stream_hook.py /data/stream_hook_impl.py

# ---------- 4. Patch application.py (idempotent, for this session) ----------

echo "[ensure_stream] Checking application.py patch..."
python3 /data/stream_patch.py

# ---------- 5. Remove old systemd service (no longer needed) ----------
# The boot-patch in launch_env.sh replaces the systemd approach, which
# was unreliable because the overlay filesystem resets the enable symlinks.

SERVICE=/etc/systemd/system/ensure-stream.service
if [ -f "$SERVICE" ]; then
  echo "[ensure_stream] Removing old systemd service..."
  mount -o remount,rw / 2>/dev/null || true
  systemctl disable ensure-stream.service 2>/dev/null || true
  rm -f "$SERVICE"
  systemctl daemon-reload 2>/dev/null || true
  echo "[ensure_stream] systemd service removed"
fi

echo "[ensure_stream] Done"
