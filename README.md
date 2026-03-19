# Comma4-UI-Streamer

Stream your comma 4's live sunnypilot/openpilot UI to any browser on your local network via WebRTC (H.264 preferred).

> Forked from [Comma4-UI-Streamer](https://github.com/peterclampton/Comma4-UI-Streamer) by [peterclampton](https://github.com/peterclampton). Original project provided MJPEG streaming of the comma UI. This fork replaces MJPEG with WebRTC (H.264 preferred), adds a real-time telemetry overlay via SSE, cereal-based telemetry, and PWA support.

![comma 4](https://img.shields.io/badge/comma-4-blue) ![openpilot](https://img.shields.io/badge/openpilot-compatible-blue) ![sunnypilot](https://img.shields.io/badge/sunnypilot-compatible-green)

![Screenshot](screenshot.png)

## What you get

Open `http://<comma-ip>:8082` on your phone, infotainment screen, or any browser — and see the comma UI live with full HUD overlay (lane lines, lead car, speed, alerts) plus real-time telemetry data. Video is delivered via WebRTC with H.264 codec preference for low-latency, bandwidth-efficient streaming.

**Live overlay includes:**
- Set speed, speed limit (from map data), engage status (Engaged / Standby / Steering with sunnypilot MADS)
- Lead car distance (ft) and gap time (seconds) — appears when a lead car is detected and self-driving is active
- Acceleration bar, gas/brake output
- Road grade (%)
- Performance monitoring — model exec time, frame drops, CPU temperature, CPU usage & memory usage

**Endpoints:**
- `/` — fullscreen viewer with telemetry overlay (WebRTC video)
- `/offer` — WebRTC signaling (POST SDP offer, receive SDP answer)
- `/telemetry/stream` — Server-Sent Events (SSE) telemetry stream
- `/telemetry` — one-shot telemetry JSON (for debugging / curl)
- `/snapshot` — grab a single JPEG frame
- `/health` — JSON status (frame availability, resolution, timestamp)

---

## Requirements

Your phone/browser must be on the **same wifi network** as your comma (or connected via USB tether). A modern browser with WebRTC support is required (Chrome, Firefox, Safari, Edge).

## Setup

### Step 1 — SSH into your comma

```bash
ssh comma@<your-comma-ip>
```

### Step 2 — Install and enable

```bash
# Download the installer
curl -fsSL https://raw.githubusercontent.com/Scotty-Hudson/Ccomma4-UI-Streamer-h264/main/ensure_stream.sh -o /data/ensure_stream.sh
chmod +x /data/ensure_stream.sh

# Run it (patches application.py, sets env vars, hooks into boot)
sudo /data/ensure_stream.sh

# Reboot to activate
sudo reboot
```

Then open in your browser:

```
http://<comma-ip>:8082
```

### What the installer does

`ensure_stream.sh` handles everything in one shot:

1. **Env vars** — adds `STREAM=1` to `launch_env.sh`
2. **Boot patch** — adds a one-liner to `launch_env.sh` that re-runs `stream_patch.py` every time openpilot starts (idempotent — exits instantly if already patched)
3. **Stream files** — downloads `ui_stream.py`, `ui_frame_bridge.py`, and `stream_patch.py` to `/data/`
4. **Code patch** — patches `application.py` with stream hooks (creates a `.bak` backup first)

The boot-patch line lives in `launch_env.sh` on `/data/`, so it **survives all reboots and overlay resets**. No systemd service needed. When a sunnypilot update replaces `application.py` with stock code, the next openpilot launch re-applies the patch automatically.

The patch only injects code into the UI process (`selfdrive.ui.ui`). **Zero code runs in controlsd, paramsd, or any other safety-critical process.**

---

## Mobile — Add to Home Screen

Works best on phones and tablets when added as a web app. This gives you a fullscreen, app-like experience with no browser bar.

**iOS (Safari):**
1. Open `http://<comma-ip>:8082` in Safari
2. Tap the **Share** button (square with arrow)
3. Scroll down and tap **Add to Home Screen**
4. Tap **Add**

**Android (Chrome):**
1. Open `http://<comma-ip>:8082` in Chrome
2. Tap the **⋮** menu (top right)
3. Tap **Add to Home Screen**
4. Tap **Add**

> **Note:** Chrome's "Install app" option requires HTTPS, which isn't available on a local network device. Use **Add to Home Screen** instead — it works the same way.

The stream will now open as a standalone app — no browser bar, no tabs, just the live UI.

---

## Configuration

Set these environment variables before openpilot starts (e.g., in `launch_env.sh`):

| Variable | Default | Description |
|----------|---------|-------------|
| `STREAM` | `1` | Enabled by default after install |
| `STREAM_PORT` | `8082` | HTTP port for the stream server |
| `STREAM_QUALITY` | `50` | JPEG quality for snapshot endpoint (1–95) |
| `STREAM_FPS` | `10` | Target capture/stream frame rate |

Example with all options:

```bash
export STREAM=1
export STREAM_PORT=8082
export STREAM_QUALITY=50
export STREAM_FPS=10
```

---

## How it works

The installer patches `application.py` to:

1. **Import** `/data/ui_stream.py` when `STREAM=1` is set
2. **Create** a render texture for frame capture (if one doesn't already exist)
3. **Capture** each rendered frame and publish it to a shared buffer (`ui_frame_bridge.py`)

GPU readback happens on the UI thread (~0.5ms), while heavy numpy work (reshape, flip, strip alpha) is offloaded to a background thread so the UI watchdog heartbeat is never delayed.

When a browser connects, it negotiates a WebRTC peer connection via the `/offer` endpoint. Both the SDP answer and codec preferences are set to prefer H.264, giving you low-latency video with minimal bandwidth. The telemetry overlay receives live vehicle and system data via Server-Sent Events (SSE) — a persistent connection that pushes cereal messages as they arrive, with no polling overhead.

The patch **only runs inside the UI process** (`selfdrive.ui.ui`). Zero code touches controlsd, paramsd, or any other safety-critical process.

---

## Troubleshooting

**Connection refused on :8082**
- SSH in and check: `grep ui_stream /data/openpilot/system/ui/lib/application.py` (try also with `openpilot/openpilot/` in the path)
- If empty, the patch didn't apply. Run: `python3 /data/stream_patch.py`
- Check `STREAM=1` is in `launch_env.sh`: `grep STREAM /data/openpilot/launch_env.sh`
- Check port is listening: `ss -tlnp | grep 8082`
- Check health endpoint: `curl http://localhost:8082/health`

**Stream connects but blank/no frames**
- The render texture may not be initializing. Check `/health` — `has_frame` should be `true`
- Check logs: `journalctl -n 50 | grep -i stream`

**After sunnypilot update, stream stopped**
- The boot-patch in `launch_env.sh` should re-apply automatically on the next openpilot start. If it didn't, check that `launch_env.sh` still contains the `stream_patch.py` line: `grep stream_patch /data/openpilot/launch_env.sh`
- If missing, re-run: `sudo /data/ensure_stream.sh`

---

## Uninstall

```bash
# Remove the stream files
rm -f /data/ui_stream.py /data/ui_frame_bridge.py /data/stream_patch.py /data/ensure_stream.sh

# Restore original application.py
APP=$(find /data/openpilot -name "application.py.bak" -path "*/ui/lib/*" 2>/dev/null | head -1)
[ -n "$APP" ] && cp "$APP" "${APP%.bak}"

# Remove STREAM vars and boot-patch from launch_env.sh
sed -i '/^export STREAM/d' /data/openpilot/launch_env.sh
sed -i '/stream_patch\.py/d' /data/openpilot/launch_env.sh

sudo reboot
```

---

## Compatibility

- **Hardware:** comma 4 (Snapdragon 845)
- **Software:** sunnypilot and openpilot — both use the same `pyray`-based UI framework
- **Browsers:** Safari (iOS), Chrome, Firefox, Edge — any browser with WebRTC support

## Tested on

- comma 4
- sunnypilot staging + dev (March 2026)
- 2017 Lexus RX350 (TSS-P)

## Credits

- **[peterclampton](https://github.com/peterclampton)** — original [Comma4-UI-Streamer](https://github.com/peterclampton/Comma4-UI-Streamer) project

## License

MIT
