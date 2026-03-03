# Comma4-UI-Streamer

Stream your comma 4's live openpilot/sunnypilot UI to any browser on your local network via MJPEG. Runs at 10 FPS by default (configurable).

![comma 4](https://img.shields.io/badge/comma-4-blue) ![openpilot](https://img.shields.io/badge/openpilot-compatible-blue) ![sunnypilot](https://img.shields.io/badge/sunnypilot-compatible-green)

## What you get

Open `http://<comma-ip>:8082/stream` on your phone, infotainment screen, or any browser — and see the comma UI live.

**Endpoints:**
- `/stream` — live MJPEG stream
- `/snapshot` — grab a single frame
- `/` — simple fullscreen viewer page

---

## Setup

### Step 1 — SSH into your comma

```bash
ssh comma@<your-comma-ip>
```

### Step 2 — Download the stream server

```bash
curl -o /data/ui_stream.py https://raw.githubusercontent.com/peterclampton/Comma4-UI-Streamer/main/ui_stream.py
```

### Step 3 — Patch the render loop

This hooks the streamer into sunnypilot's UI. Run this once:

```bash
cd /data/openpilot

# Add import at top of application.py
sed -i '1s/^/import os\n/' system/ui/lib/application.py

# Add stream start after app init (look for the render loop start)
python3 - <<'EOF'
import re

path = 'system/ui/lib/application.py'
with open(path) as f:
    txt = f.read()

inject_start = "    import ui_stream; _stream = ui_stream.start(8082)\n"
inject_frame = "        if os.environ.get('STREAM'): ui_stream.capture_frame(self)\n"

# Add stream start before serve_forever or main loop
txt = txt.replace("    def _run(self", inject_start + "    def _run(self", 1)

with open(path, 'w') as f:
    f.write(txt)
print("Done")
EOF
```

> **Note:** The exact injection point may vary between sunnypilot versions. If the script fails, open `system/ui/lib/application.py` and manually add `import ui_stream; _stream = ui_stream.start(8082)` before the render loop starts.

### Step 4 — Enable on boot

Add to `/data/openpilot/launch_env.sh`:

```bash
STREAM=1
```

Or to run manually without modifying launch_env:

```bash
STREAM=1 PYTHONPATH=/data/openpilot:/usr/local/venv/lib/python3.12/site-packages python3 /data/openpilot/system/ui/ui.py
```

### Step 5 — Reboot and view

```bash
sudo reboot
```

Then open in your browser:

```
http://<comma-ip>:8082
```

---

## Options

| Variable | Default | Description |
|----------|---------|-------------|
| `quality` | `50` | JPEG quality (1-95) |
| `target_fps` | `10` | Stream frame rate |
| `port` | `8082` | HTTP port |

Edit these in `ui_stream.py` → `capture_frame()` and `start()` calls.

---

## Compatibility

Works with both **openpilot** and **sunnypilot** — both use the same `pyray`-based UI framework (`system/ui/lib/application.py`). The hook point is identical.

> The auto-patch script may need minor adjustments depending on your exact branch/version. The manual injection step always works as a fallback.

## Tested on

- comma 4
- sunnypilot `staging-c4`
- 2017 Lexus RX350 (TSS-P)

## Notes

- No performance impact when `STREAM=1` is not set
- Low resolution reflects the comma 4's smaller display (vs comma 3x)
- Works on local wifi or USB tether network
