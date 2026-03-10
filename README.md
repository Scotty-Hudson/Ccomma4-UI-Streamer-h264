# Comma4-UI-Streamer

Stream your comma 4's live sunnypilot/openpilot UI to any browser on your local network via MJPEG.

![comma 4](https://img.shields.io/badge/comma-4-blue) ![openpilot](https://img.shields.io/badge/openpilot-compatible-blue) ![sunnypilot](https://img.shields.io/badge/sunnypilot-compatible-green)

![Screenshot](screenshot.png)

## What you get

Open `http://<comma-ip>:8082` on your phone, infotainment screen, or any browser — and see the comma UI live with full HUD overlay (lane lines, lead car, speed, alerts) plus real-time telemetry data.

**Live overlay includes:**
- Set speed, engage status (Engaged / Standby / Steering with sunnypilot MADS)
- Lead car distance (ft) and gap time (seconds) — appears when a lead car is detected and self-driving is active
- Acceleration bar, gas/brake output
- Road grade (%)
- Performance monitoring — model exec time, frame drops, CPU temperature & memory usage

**Endpoints:**
- `/` — fullscreen viewer with telemetry overlay
- `/stream` — raw MJPEG stream
- `/snapshot` — grab a single frame
- `/telemetry` — live telemetry JSON

---

## Requirements

Your phone/browser must be on the **same wifi network** as your comma (or connected via USB tether).

## Setup

### Step 1 — SSH into your comma

```bash
ssh comma@<your-comma-ip>
```

### Step 2 — Install and enable

```bash
# Download all files
curl -sL https://raw.githubusercontent.com/peterclampton/Comma4-UI-Streamer/main/ui_stream.py -o /data/ui_stream.py
curl -sL https://raw.githubusercontent.com/peterclampton/Comma4-UI-Streamer/main/stream_patch.py -o /data/stream_patch.py
curl -sL https://raw.githubusercontent.com/peterclampton/Comma4-UI-Streamer/main/ensure_stream.sh -o /data/ensure_stream.sh
chmod +x /data/ensure_stream.sh

# Run it (patches application.py, sets env vars, installs boot service)
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
2. **Stream server** — downloads `ui_stream.py` if missing
3. **Code patch** — patches `application.py` with stream hooks (creates a `.bak` backup first)
4. **Boot service** — installs a systemd service that re-runs on every boot, **before** openpilot starts

This means your stream **survives sunnypilot updates automatically**. When an update replaces `application.py` with stock code, the next reboot re-applies the patch.

### After an AGNOS update

AGNOS updates (rare — a few times a year) wipe the systemd service. Just re-run:

```bash
sudo /data/ensure_stream.sh
```

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
3. Tap **Add to Home Screen** (or **Install app**)
4. Tap **Add**

The stream will now open as a standalone app — no browser bar, no tabs, just the live UI.

---

## Configuration

Set these environment variables in `launch_env.sh` (or edit the defaults in `ensure_stream.sh`):

| Variable | Default | Description |
|----------|---------|-------------|
| `STREAM` | `1` | Enabled by default after install |
| `STREAM_PORT` | `8082` | HTTP port for the stream server |
| `STREAM_QUALITY` | `50` | JPEG quality (1–95) |
| `STREAM_FPS` | `20` | Target frame rate |

Example with all options:

```bash
export STREAM=1
export STREAM_PORT=8082
export STREAM_QUALITY=60
export STREAM_FPS=15
```

---

## How it works

The installer patches `application.py` to:
1. Import `/data/ui_stream.py` when `STREAM=1` is set
2. Create a render texture for frame capture (if one doesn't already exist)
3. Capture each rendered frame as JPEG and serve it over HTTP as an MJPEG stream

The telemetry overlay reads live vehicle data from `/tmp/telemetry.json` (written by a small patch to `ui_state.py`) and displays it on top of the video feed in the browser.

---

## Troubleshooting

**Connection refused on :8082**
- SSH in and check: `grep ui_stream /data/openpilot/system/ui/lib/application.py` (try also with `openpilot/openpilot/` in the path)
- If empty, the patch didn't apply. Run: `python3 /data/stream_patch.py`
- Check `STREAM=1` is in `launch_env.sh`: `grep STREAM /data/openpilot/launch_env.sh`

**Stream connects but blank/no frames**
- The render texture may not be initializing. The patch creates one automatically, but check logs: `journalctl -u openpilot -n 50 | grep -i stream`

**After sunnypilot update, stream stopped**
- Run `sudo /data/ensure_stream.sh` and reboot. The boot service should do this automatically, but if it's missing, this restores it.

---

## Uninstall

```bash
# Remove the stream files
rm /data/ui_stream.py /data/stream_patch.py /data/ensure_stream.sh

# Restore original application.py
APP=$(find /data/openpilot -name "application.py.bak" -path "*/ui/lib/*" 2>/dev/null | head -1)
[ -n "$APP" ] && cp "$APP" "${APP%.bak}"

# Remove STREAM vars from launch_env.sh
sed -i '/^export STREAM/d' /data/openpilot/launch_env.sh

# Remove the boot service
sudo mount -o remount,rw /
sudo systemctl disable ensure-stream.service
sudo rm -f /etc/systemd/system/ensure-stream.service
sudo systemctl daemon-reload

sudo reboot
```

---

## Compatibility

- **Hardware:** comma 4 (Snapdragon 845)
- **Software:** sunnypilot and openpilot — both use the same `pyray`-based UI framework
- **Browsers:** Safari (iOS), Chrome, Firefox — any browser that supports MJPEG

## Tested on

- comma 4
- sunnypilot staging + dev (March 2026)
- 2017 Lexus RX350 (TSS-P)

## License

MIT
