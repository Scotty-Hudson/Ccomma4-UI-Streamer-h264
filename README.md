# comma4-mjpeg-stream

MJPEG stream server for sunnypilot on the comma 4. Streams the live openpilot UI to any browser on your local network.

![comma 4](https://img.shields.io/badge/comma-4-blue) ![sunnypilot](https://img.shields.io/badge/sunnypilot-staging--c4-green)

## What it does

Hooks into sunnypilot's render loop and serves the UI as an MJPEG stream over HTTP. Open it on your phone, infotainment screen, or any browser on the same network.

**Endpoints:**
- `/stream` — live MJPEG stream
- `/snapshot` — single JPEG frame
- `/` — simple HTML viewer

## Setup

### 1. Copy to your comma

```bash
scp ui_stream.py comma@<your-comma-ip>:/data/ui_stream.py
```

### 2. Patch the sunnypilot render loop

Add to `/data/openpilot/system/ui/lib/application.py` in the render loop:

```python
import ui_stream
_stream_state = ui_stream.start(port=8082)

# Inside the render loop:
ui_stream.capture_frame(app, quality=50, target_fps=10)
```

Or enable via environment variable in `launch_env.sh`:

```bash
STREAM=1 python3 -m openpilot.system.ui.ui &
```

And patch `application.py` to check `os.environ.get("STREAM")`.

### 3. Access

```
http://<comma-ip>:8082/stream
```

## Notes

- **Port 8082** by default
- Quality and FPS are tunable (`quality=50`, `target_fps=10`)
- Low resolution on comma 4 due to the smaller display
- No impact on openpilot when `STREAM=1` is not set (lazy import)
- Works best on local wifi or car hotspot

## Tested on

- comma 4
- sunnypilot `staging-c4` branch
