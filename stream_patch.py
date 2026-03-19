#!/usr/bin/env python3
"""Patch application.py for WebRTC UI streaming (STREAM=1 support).

Idempotent — safe to run multiple times.
Creates a .bak backup before first patch.

This approach directly patches the UI process source code so streaming code
ONLY executes inside selfdrive.ui.ui.  Zero footprint in controlsd, paramsd,
or any other safety-critical process.

Based on peterclampton/Comma4-UI-Streamer, adapted for WebRTC + SSE telemetry.

Usage: python3 /data/stream_patch.py
"""
import sys
import shutil
import re
from pathlib import Path

PATHS = [
    Path("/data/openpilot/system/ui/lib/application.py"),
    Path("/data/openpilot/openpilot/system/ui/lib/application.py"),
]

target = None
for p in PATHS:
    if p.exists():
        target = p
        break

if not target:
    print("ERROR: application.py not found")
    sys.exit(1)

print(f"Target: {target}")
text = target.read_text()

if "ui_stream" in text:
    print("Already patched — nothing to do.")
    sys.exit(0)

# Backup (only first time)
bak = target.with_suffix(".py.bak")
if not bak.exists():
    shutil.copy2(target, bak)
    print(f"Backup: {bak}")

changes = 0

# ---------------------------------------------------------------------------
# PATCH 1: Add self._ui_stream = None in __init__
# Find the line: self._render_texture ... = None  (in the __init__ block)
# ---------------------------------------------------------------------------
init_marker = re.search(r'(self\._render_texture[^=]*=\s*None)', text)
if init_marker:
    text = text.replace(
        init_marker.group(0),
        init_marker.group(0) + "\n    self._ui_stream = None",
        1
    )
    changes += 1
    print("[1/3] _ui_stream init - OK")
else:
    print("[1/3] _ui_stream init - FAILED (no _render_texture init found)")

# ---------------------------------------------------------------------------
# PATCH 2: Import and start ui_stream after set_target_fps
# Only activates when STREAM=1 env var is set.
# Creates render_texture on the fly if needed.
# ---------------------------------------------------------------------------
stream_init = '''
      if os.getenv("STREAM") == "1":
          try:
              import sys as _sys
              _sys.path.insert(0, "/data")
              import ui_stream
              self._ui_stream = ui_stream
              if self._render_texture is None:
                  self._render_texture = rl.load_render_texture(self._width, self._height)
                  rl.set_texture_filter(self._render_texture.texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
              port = int(os.getenv("STREAM_PORT", "8082"))
              ui_stream.start(port)
              cloudlog.warning(f"WebRTC stream on port {port}")
          except Exception as e:
              cloudlog.error(f"Stream init failed: {e}")
              self._ui_stream = None'''

# Find: self._target_fps = fps
target_fps_match = re.search(r'(\n(\s+)self\._target_fps\s*=\s*fps)', text)
if target_fps_match:
    text = text.replace(
        target_fps_match.group(0),
        "\n" + stream_init + target_fps_match.group(0),
        1
    )
    changes += 1
    print("[2/3] ui_stream import + start - OK")
else:
    # Fallback: after set_target_fps line
    stfps = re.search(r'(rl\.set_target_fps\([^)]+\))', text)
    if stfps:
        text = text.replace(
            stfps.group(0),
            stfps.group(0) + "\n" + stream_init,
            1
        )
        changes += 1
        print("[2/3] ui_stream import + start (fallback) - OK")
    else:
        print("[2/3] ui_stream import + start - FAILED")

# ---------------------------------------------------------------------------
# PATCH 3: capture_frame() in render loop before _monitor_fps()
# Guard: only runs if both _ui_stream and _render_texture exist
# ---------------------------------------------------------------------------
capture = '        if self._ui_stream is not None and self._render_texture is not None:\n            self._ui_stream.capture_frame(self, int(os.getenv("STREAM_QUALITY", "50")), int(os.getenv("STREAM_FPS", "10")))\n'

monitor_match = re.search(r'(\n(\s+)self\._monitor_fps\(\))', text)
if monitor_match:
    text = text.replace(
        monitor_match.group(0),
        "\n" + capture + monitor_match.group(0),
        1
    )
    changes += 1
    print("[3/3] capture_frame - OK")
else:
    print("[3/3] capture_frame - FAILED")

if changes == 3:
    target.write_text(text)
    print(f"\nAll patches applied to {target}")
else:
    target.write_text(text)
    print(f"\nPartial: {changes}/3 applied. Check failures above.")
    sys.exit(1)
