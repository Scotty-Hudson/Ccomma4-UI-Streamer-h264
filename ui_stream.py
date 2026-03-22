"""WebRTC stream server (H.264 preferred) for sunnypilot UI with telemetry overlay.

Dual-purpose module:
  1. When imported by application.py (UI process):
     - capture_frame() does GPU readback and writes raw RGBA to shared memory
     - start() spawns the server as a separate subprocess
     - Zero encoding work in the UI process
  2. When run as subprocess (--server):
     - Reads frames from shared memory
     - Does numpy downscale + H.264 encoding + WebRTC + SSE telemetry
     - All CPU-intensive work is isolated here

This module is ONLY imported inside the UI process (selfdrive.ui.ui) via the
STREAM=1 env-var gate in the patched application.py.  It never runs in
controlsd, paramsd, or any other safety-critical process.
"""
import fractions
import io
import os
import asyncio
import json
import logging
import struct
import time
import threading

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCRtpSender, MediaStreamTrack
from av import VideoFrame
from PIL import Image

from ui_frame_bridge import wait_for_frame, get_latest_frame, snapshot_jpeg, publish_frame

logger = logging.getLogger("ui_webrtc")


# ---------------------------------------------------------------------------
# Shared memory constants — cross-process frame passing
# ---------------------------------------------------------------------------
# Layout: [seq:4][width:4][height:4][viewers:1][pad:3][rgba_data:...]
#   seq: seqlock counter (odd=writing, even=valid)
#   viewers: set by server subprocess when clients are connected

_SHM_NAME = "comma_stream"
_SHM_HEADER = 16
_SHM_MAX_FRAME = 1920 * 1200 * 4  # ~9.2 MB max
_SHM_SIZE = _SHM_HEADER + _SHM_MAX_FRAME

# ---------------------------------------------------------------------------
# Frame capture — called from the patched render loop in application.py
# ---------------------------------------------------------------------------
# Runs in the UI process only.  Writes raw RGBA to shared memory.
# All numpy / H.264 encoding happens in the server subprocess.

_shm = None          # SharedMemory object (created by start())
_shm_seq = 0         # seqlock write counter
_capture_counter = 0
_capture_logged = False
_server_proc = None
_server_started = False


def capture_frame(app, quality=50, target_fps=10):
    """Capture a frame and write raw RGBA to shared memory.

    This is the ONLY streaming work in the UI process:
    GPU readback (~0.5ms) + memcpy to shared memory (~0.5ms).
    No numpy, no encoding, no asyncio.

    NO-OP when no browser clients are connected (server subprocess
    clears the viewers byte in shared memory).
    """
    global _capture_counter, _capture_logged, _shm_seq

    if _shm is None:
        return

    # Server subprocess sets byte 12 when clients are connected
    if _shm.buf[12] == 0:
        return

    import pyray as rl

    if app._render_texture is None:
        return

    _capture_counter += 1
    skip = max(1, getattr(app, '_target_fps', 30) // target_fps)
    if _capture_counter % skip != 0:
        return

    try:
        image = rl.load_image_from_texture(app._render_texture.texture)
        w, h = image.width, image.height
        if w <= 0 or h <= 0:
            rl.unload_image(image)
            return

        data_size = w * h * 4
        if data_size + _SHM_HEADER > _shm.size:
            rl.unload_image(image)
            return

        if not _capture_logged:
            logger.info("first capture: %dx%d fmt=%d -> shm", w, h, image.format)
            _capture_logged = True

        raw = rl.ffi.buffer(image.data, data_size)

        # Seqlock write: odd = writing in progress
        _shm_seq += 1
        struct.pack_into('I', _shm.buf, 0, _shm_seq)
        struct.pack_into('II', _shm.buf, 4, w, h)
        _shm.buf[_SHM_HEADER:_SHM_HEADER + data_size] = raw
        # Even = write complete
        _shm_seq += 1
        struct.pack_into('I', _shm.buf, 0, _shm_seq)

        rl.unload_image(image)
    except Exception as e:
        logger.warning("capture error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Server start — called from the patched application.py init
# ---------------------------------------------------------------------------


def start(port=8082):
    """Create shared memory and spawn the server as a separate process.

    All encoding, WebRTC, and telemetry work runs in the subprocess.
    The UI process only writes raw RGBA frames to shared memory.
    Safe to call multiple times.
    """
    global _server_started, _shm, _server_proc
    if _server_started:
        return

    from multiprocessing.shared_memory import SharedMemory
    import subprocess
    import sys

    # Create shared memory for frame passing
    try:
        _shm = SharedMemory(name=_SHM_NAME, create=True, size=_SHM_SIZE)
    except FileExistsError:
        # Clean up stale shm from a previous crash
        try:
            old = SharedMemory(name=_SHM_NAME, create=False)
            old.close()
            old.unlink()
        except Exception:
            pass
        _shm = SharedMemory(name=_SHM_NAME, create=True, size=_SHM_SIZE)

    _shm.buf[:_SHM_HEADER] = b'\x00' * _SHM_HEADER

    fps = int(os.getenv("STREAM_FPS", "5"))
    scale = os.getenv("STREAM_SCALE", "0.5")

    _server_proc = subprocess.Popen(
        [sys.executable, '/data/ui_stream.py', '--server',
         '--port', str(port), '--fps', str(fps), '--scale', scale],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("Server subprocess PID %d (port=%s fps=%s scale=%s)",
                _server_proc.pid, port, fps, scale)
    _server_started = True


# ---------------------------------------------------------------------------
# WebRTC video track fed by ui_frame_bridge
# ---------------------------------------------------------------------------

class CameraTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, fps=10):
        super().__init__()
        self._fps = max(fps, 1)
        self._frame_interval = 1.0 / self._fps
        self._last_sent = 0.0
        self._last_source_ts = 0.0
        self._recv_count = 0
        self._pts = 0
        self._time_base = fractions.Fraction(1, 90000)  # standard RTP clock

    async def recv(self):
        loop = asyncio.get_event_loop()
        while True:
            now = time.time()
            sleep_for = self._frame_interval - (now - self._last_sent)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

            arr, w, h, ts = await loop.run_in_executor(None, wait_for_frame, 2.0)
            if arr is None or w <= 0 or h <= 0:
                continue
            if ts == self._last_source_ts:
                await asyncio.sleep(0.02)
                arr, w, h, ts = await loop.run_in_executor(None, get_latest_frame)
                if arr is None or ts == self._last_source_ts:
                    continue

            # Ensure dimensions are even (required by H.264 yuv420p)
            if h % 2 != 0:
                arr = arr[:h - 1, :, :]
                h -= 1
            if w % 2 != 0:
                arr = arr[:, :w - 1, :]
                w -= 1

            frame = VideoFrame.from_ndarray(arr, format="rgb24")
            frame.pts = self._pts
            frame.time_base = self._time_base
            self._pts += int(90000 / self._fps)

            self._recv_count += 1
            if self._recv_count <= 5:
                logger.info("recv: sending frame #%d %dx%d pts=%s", self._recv_count, w, h, frame.pts)

            self._last_sent = time.time()
            self._last_source_ts = ts
            return frame


# ---------------------------------------------------------------------------
# SDP helper — reorder payload types so H.264 comes first
# ---------------------------------------------------------------------------

def _prefer_h264(sdp):
    lines = sdp.split("\r\n")
    h264_pts = set()
    for line in lines:
        if line.startswith("a=rtpmap:") and "H264" in line:
            h264_pts.add(line.split(":")[1].split(" ")[0])
    if not h264_pts:
        return sdp
    result = []
    for line in lines:
        if line.startswith("m=video "):
            parts = line.split(" ")
            header, pts = parts[:3], parts[3:]
            line = " ".join(
                header
                + [p for p in pts if p in h264_pts]
                + [p for p in pts if p not in h264_pts]
            )
        result.append(line)
    return "\r\n".join(result)


# ---------------------------------------------------------------------------
# Active peer connections
# ---------------------------------------------------------------------------

_pcs = set()
_server_shm = None  # SharedMemory reference in server subprocess


def _update_viewers_shm():
    """Update shared memory viewers byte so UI process knows to capture."""
    if _server_shm is not None:
        _server_shm.buf[12] = 1 if (_pcs or _sse_clients) else 0


# ---------------------------------------------------------------------------
# SSE telemetry infrastructure
# ---------------------------------------------------------------------------
_sse_clients: list[asyncio.Queue] = []  # one queue per connected SSE client
_telemetry_latest: dict = {}            # latest snapshot, shared with SSE handler


# ---------------------------------------------------------------------------
# Inline HTML page — WebRTC viewer with telemetry HUD
# ---------------------------------------------------------------------------

_OVERLAY_HTML = """<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>openpilot live</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;overflow:hidden;height:100vh;width:100vw;margin:0;font-family:-apple-system,sans-serif}
#wrap{position:relative;width:100vw;height:100vh;display:flex;justify-content:center;align-items:center}
#cam{width:82%;height:95%;object-fit:contain;margin-left:auto;margin-right:5%}

/* Overlay container - matches image bounds */
#hud{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none}

/* Set speed - top right */
#set-speed{position:absolute;top:18%;left:1%;background:rgba(0,0,0,0.5);border-radius:12px;padding:6px 14px;text-align:center;border:1px solid rgba(255,255,255,0.15)}
#set-label{font-size:min(2.5vw,11px);color:#888;text-transform:uppercase;letter-spacing:1px}
#set-val{font-size:min(7vw,36px);font-weight:600;color:#fff}
#speed-limit{margin-top:8px;border-top:1px solid rgba(255,255,255,0.15);padding-top:6px}
#sl-label{font-size:min(2vw,9px);color:#888;text-transform:uppercase;letter-spacing:0.5px}
#sl-val{font-size:min(5vw,24px);font-weight:500;color:#ff9800}

/* Lead car info - center top */
#lead-info{position:absolute;top:38%;left:50%;transform:translateX(-50%);text-align:center;opacity:0;transition:opacity 0.3s;background:rgba(0,0,0,0.55);padding:6px 14px;border-radius:8px}
#lead-info.show{opacity:1}
#lead-dist{font-size:min(4.5vw,22px);font-weight:600;color:#fff;text-shadow:0 1px 4px rgba(0,0,0,0.8)}
#lead-gap{font-size:min(3.5vw,16px);font-weight:500;color:#4fc3f7}

/* Status bar - top left */
#status{position:absolute;top:5%;left:1%}
#engage-badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:min(3vw,13px);font-weight:600;letter-spacing:1px;text-transform:uppercase}
#engage-badge.off{background:rgba(100,100,100,0.5);color:#888}
#engage-badge.on{background:rgba(76,175,80,0.3);color:#4caf50;border:1px solid rgba(76,175,80,0.4)}

/* Metrics strip - bottom left/right */
.metric{position:absolute;bottom:4%;font-size:min(3vw,13px);color:#aaa;text-shadow:0 1px 3px rgba(0,0,0,0.8)}
.metric .val{font-size:min(4.5vw,20px);font-weight:600;color:#e0e0e0}
#m-steer{left:4%}
#m-grade{left:1%}
#m-accel{position:absolute;left:50%;top:10%;transform:translateX(-50%);text-align:center;width:min(50vw,220px)}
#accel-label{font-size:min(2.5vw,10px);color:#888;letter-spacing:1px;margin-bottom:2px}
#accel-bar-wrap{display:flex;align-items:center;height:min(2.5vw,12px);background:rgba(255,255,255,0.1);border-radius:6px;overflow:hidden;position:relative}
#accel-bar-neg{height:100%;width:0;background:#f44336;position:absolute;right:50%;border-radius:6px 0 0 6px;transition:width 0.1s}
#accel-bar-pos{height:100%;width:0;background:#4caf50;position:absolute;left:50%;border-radius:0 6px 6px 0;transition:width 0.1s}
#accel-center{position:absolute;left:50%;top:0;bottom:0;width:2px;background:rgba(255,255,255,0.4);transform:translateX(-50%);z-index:1}
#accel-num{font-size:min(4vw,18px);color:#aaa;margin-top:1px}

/* Brake/Gas indicators */
#pedals{position:absolute;bottom:15%;left:1%;display:flex;gap:10px;align-items:flex-end}
.pedal-wrap{display:flex;flex-direction:column;align-items:center;gap:2px}
.pedal-label{font-size:min(2.5vw,10px);color:#888;letter-spacing:1px}
.pedal-bar{width:min(4vw,18px);min-height:2px;border-radius:3px;transition:height 0.15s}
.pedal-val{font-size:min(2.5vw,11px);color:#aaa}
#gas-bar{background:#4caf50}
#brake-bar{background:#f44336}
#perf-strip{position:absolute;bottom:1%;left:50%;transform:translateX(-50%);display:flex;gap:min(3vw,14px);background:rgba(0,0,0,0.5);padding:3px 10px;border-radius:6px}
.pf{text-align:center}
.pf-label{font-size:min(1.8vw,8px);color:#666;letter-spacing:0.5px}
.pf-val{font-size:min(2.5vw,12px);color:#e0e0e0;font-weight:500}
.pf-val.bad{color:#f44336}

</style></head><body>
<div id="wrap">
  <video id="cam" autoplay playsinline muted></video>
  <div id="hud"><div ontouchend="event.preventDefault();event.stopPropagation();toggleFS();" onclick="toggleFS()" style="position:absolute;right:1%;top:5%;background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.2);color:rgba(255,255,255,0.6);font-size:16px;padding:8px 12px;border-radius:8px;pointer-events:auto;z-index:9999;cursor:pointer">&#x26F6;</div>
    <div id="status"><span id="engage-badge" class="off">OFF</span></div>

    <div id="set-speed">
      <div id="set-label">SET</div>
      <div id="set-val">--</div>
      <div id="speed-limit">
        <div id="sl-label">LIMIT</div>
        <div id="sl-val">--</div>
      </div>
    </div>

    <div id="lead-info">
      <div id="lead-dist">--</div>
      <div id="lead-gap">--</div>
    </div>

    <div id="m-accel">
      <div id="accel-label">ACCEL</div>
      <div id="accel-bar-wrap">
        <div id="accel-bar-neg" class="accel-fill"></div>
        <div id="accel-center"></div>
        <div id="accel-bar-pos" class="accel-fill"></div>
      </div>
      <div id="accel-num">0.0</div>
    </div>

    <div class="metric" id="m-grade"><div class="val" id="grade-val">--%</div>grade</div>
    <div id="pedals">
      <div class="pedal-wrap"><div class="pedal-label">GAS</div><div id="gas-bar" class="pedal-bar"></div><div class="pedal-val" id="gas-val">0</div></div>
      <div class="pedal-wrap"><div class="pedal-label">BRK</div><div id="brake-bar" class="pedal-bar"></div><div class="pedal-val" id="brake-val">0</div></div>
    </div>
    <div id="perf-strip">
      <div class="pf"><div class="pf-label">MODEL</div><div class="pf-val" id="pf-model">--</div></div>
      <div class="pf"><div class="pf-label">DROPS</div><div class="pf-val" id="pf-drops">--</div></div>
      <div class="pf"><div class="pf-label">CPU</div><div class="pf-val" id="pf-cpu">--</div></div>
      <div class="pf"><div class="pf-label">MEM</div><div class="pf-val" id="pf-mem">--</div></div>
      <div class="pf"><div class="pf-label">CPU TEMP</div><div class="pf-val" id="pf-temp">--</div></div>
    </div>
  </div>
</div>
<script>
/* ---------- WebRTC ---------- */
let pc = null, retryMs = 1000;
async function startWebRTC() {
  if (pc) { try { pc.close(); } catch(e) {} pc = null; }
  pc = new RTCPeerConnection({ iceServers: [] });
  var tr = pc.addTransceiver('video', { direction: 'recvonly' });
  try {
    var caps = RTCRtpReceiver.getCapabilities('video');
    if (caps && tr.setCodecPreferences) {
      var h264 = caps.codecs.filter(function(c){ return c.mimeType==='video/H264'; });
      var rest = caps.codecs.filter(function(c){ return c.mimeType!=='video/H264'; });
      if (h264.length) tr.setCodecPreferences(h264.concat(rest));
    }
  } catch(e) {}
  pc.ontrack = function(ev) {
    document.getElementById('cam').srcObject = ev.streams[0];
    retryMs = 1000;
  };
  pc.oniceconnectionstatechange = function() {
    if (pc.iceConnectionState==='failed' || pc.iceConnectionState==='disconnected') {
      setTimeout(startWebRTC, retryMs);
      retryMs = Math.min(retryMs * 2, 10000);
    }
  };
  try {
    var offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    var r = await fetch('/offer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sdp: offer.sdp, type: offer.type })
    });
    var answer = await r.json();
    await pc.setRemoteDescription(new RTCSessionDescription(answer));
  } catch(e) {
    setTimeout(startWebRTC, retryMs);
    retryMs = Math.min(retryMs * 2, 10000);
  }
}

/* ---------- Telemetry (SSE) ---------- */
let lastData = null;
function applyTelemetry(d) {
    lastData = d;

    // Set speed
    const sv = document.getElementById('set-val');
    sv.textContent = d.setSpeed > 0 ? d.setSpeed : '--';

    // Speed limit
    const slVal = document.getElementById('sl-val');
    slVal.textContent = (d.speedLimit && d.speedLimit > 0) ? d.speedLimit : '--';

    // Engage status
    const badge = document.getElementById('engage-badge');
    const engaged = d.cruiseEnabled === true || d.driveState === 'active';
    const standby = !engaged && (d.driveState === 'standby' || d.cruiseEnabled === false);
    badge.className = engaged ? 'on' : 'off';
    badge.textContent = engaged ? 'ENGAGED' : standby ? 'STANDBY' : 'OFF';

    // Lead car
    const li = document.getElementById('lead-info');
    const isEngaged = d.cruiseEnabled === true || d.driveState === 'active';
    if (isEngaged && d.leadDist !== undefined && d.leadDist !== null) {
      li.className = 'show';
      const ft = Math.round(d.leadDist * 3.28084);
      const gap = d.vEgo > 0.5 ? (d.leadDist / d.vEgo).toFixed(1) : '--';
      document.getElementById('lead-dist').textContent = ft + ' ft';
      document.getElementById('lead-gap').textContent = gap + ' s';
    } else {
      li.className = '';
    }

    // Accel
    const a = d.aEgo || 0;
    const pct = Math.min(Math.abs(a) / 3.0 * 50, 50);
    document.getElementById('accel-bar-pos').style.width = (a > 0 ? pct : 0) + '%';
    document.getElementById('accel-bar-neg').style.width = (a < 0 ? pct : 0) + '%';
    document.getElementById('accel-num').textContent = a.toFixed(1) + ' m/s2';

    // CPU
    if (d.cpuTemp !== undefined) { var e=document.getElementById("pf-temp"); e.textContent=d.cpuTemp+String.fromCharCode(176); e.className="pf-val"+(d.cpuTemp>80?" bad":""); }

    // Gas/Brake bars
    if (d.grade !== undefined) document.getElementById('grade-val').textContent = d.grade + '%';
    document.getElementById('gas-bar').style.height = Math.max(2, d.gas * 0.6) + 'px';
    document.getElementById('gas-val').textContent = d.gas;
    document.getElementById('brake-bar').style.height = Math.max(2, d.brake * 0.6) + 'px';
    document.getElementById('brake-val').textContent = d.brake;
    if (d.modelExec !== undefined) { var e=document.getElementById("pf-model"); e.textContent=d.modelExec+"ms"; e.className="pf-val"+(d.modelExec>35?" bad":""); }
    if (d.frameDropPerc !== undefined) { var e=document.getElementById("pf-drops"); e.textContent=d.frameDropPerc+"%"; e.className="pf-val"+(d.frameDropPerc>5?" bad":""); }
    if (d.cpuUsage !== undefined) { var e=document.getElementById("pf-cpu"); e.textContent=d.cpuUsage+"%"; e.className="pf-val"+(d.cpuUsage>85?" bad":""); }
    if (d.memUsed !== undefined) { var e=document.getElementById("pf-mem"); e.textContent=d.memUsed+"%"; e.className="pf-val"+(d.memUsed>85?" bad":""); }
}

function startSSE() {
  const es = new EventSource('/telemetry/stream');
  es.onmessage = function(e) {
    try { applyTelemetry(JSON.parse(e.data)); } catch(ex) {}
  };
  es.onerror = function() {
    es.close();
    setTimeout(startSSE, 2000);
  };
}

document.addEventListener("DOMContentLoaded",function(){
  startWebRTC();
  setTimeout(function(){window.scrollTo(0,1);},100);
  setTimeout(function(){window.scrollTo(0,0);},200);
  startSSE();
  if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(function(){});}
});
function toggleFS(){var d=document.documentElement;try{if(!document.fullscreenElement&&!document.webkitFullscreenElement){if(d.requestFullscreen)d.requestFullscreen();else if(d.webkitRequestFullscreen)d.webkitRequestFullscreen(Element.ALLOW_KEYBOARD_INPUT);else if(d.webkitEnterFullscreen)d.webkitEnterFullscreen();else alert('Fullscreen not supported');}else{if(document.exitFullscreen)document.exitFullscreen();else if(document.webkitExitFullscreen)document.webkitExitFullscreen();}}catch(e){alert('FS error: '+e);}}
</script></body></html>"""


# ---------------------------------------------------------------------------
# aiohttp handlers
# ---------------------------------------------------------------------------

async def _handle_index(request):
    return web.Response(text=_OVERLAY_HTML, content_type="text/html")


async def _handle_offer(request):
    try:
        params = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")
    if "sdp" not in params or "type" not in params:
        return web.Response(status=400, text="Missing sdp or type")

    pc = RTCPeerConnection()
    _pcs.add(pc)
    _update_viewers_shm()

    @pc.on("connectionstatechange")
    async def _on_state():
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            _pcs.discard(pc)
            _update_viewers_shm()

    fps = int(os.getenv("STREAM_FPS", "10"))
    sender = pc.addTrack(CameraTrack(fps=fps))

    # Prefer H.264 via codec preferences (but allow fallback to VP8)
    transceiver = next((t for t in pc.getTransceivers() if t.sender == sender), None)
    if transceiver is not None:
        try:
            capabilities = RTCRtpSender.getCapabilities("video")
            if capabilities is not None:
                h264 = [c for c in capabilities.codecs if c.mimeType.lower() == "video/h264"]
                rest = [c for c in capabilities.codecs if c.mimeType.lower() != "video/h264"]
                if h264:
                    transceiver.setCodecPreferences(h264 + rest)
        except Exception:
            pass

    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    answer = RTCSessionDescription(sdp=_prefer_h264(answer.sdp), type=answer.type)
    await pc.setLocalDescription(answer)

    logger.info("offer handled: pc=%s ice=%s", pc.connectionState, pc.iceConnectionState)

    return web.json_response(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )


async def _handle_telemetry(request):
    """One-shot JSON endpoint (kept for debugging / curl)."""
    _HEADERS = {"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"}
    if _telemetry_latest:
        return web.json_response(_telemetry_latest, headers=_HEADERS)
    return web.json_response(
        {"setSpeed": 0, "cruiseEnabled": False, "driveState": "off",
         "aEgo": 0, "vEgo": 0, "gas": 0, "brake": 0,
         "cpuTemp": 0, "cpuUsage": 0, "memUsed": 0},
        headers=_HEADERS,
    )


async def _handle_telemetry_stream(request):
    """SSE endpoint — pushes telemetry JSON as it arrives."""
    resp = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
        },
    )
    await resp.prepare(request)

    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    _sse_clients.append(q)
    logger.info("SSE client connected (%d total)", len(_sse_clients))

    try:
        # Send current state immediately so the UI populates on connect
        if _telemetry_latest:
            await resp.write(f"data: {json.dumps(_telemetry_latest)}\n\n".encode())

        while True:
            data = await q.get()
            await resp.write(f"data: {json.dumps(data)}\n\n".encode())
    except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
        pass
    finally:
        _sse_clients.remove(q)
        logger.info("SSE client disconnected (%d remaining)", len(_sse_clients))

    return resp


# ---------------------------------------------------------------------------
# PWA assets — manifest, service worker, icons
# ---------------------------------------------------------------------------

_MANIFEST = json.dumps({
    "name": "openpilot live",
    "short_name": "OP Live",
    "start_url": "/",
    "display": "standalone",
    "orientation": "landscape",
    "background_color": "#000000",
    "theme_color": "#000000",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}
    ]
})

_SW_JS = """self.addEventListener('install',function(e){self.skipWaiting();});
self.addEventListener('activate',function(e){e.waitUntil(clients.claim());});
self.addEventListener('fetch',function(e){e.respondWith(fetch(e.request));});
"""

_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="80" fill="#000"/><circle cx="256" cy="220" r="90" fill="none" stroke="#4caf50" stroke-width="18"/><path d="M160 340 Q256 420 352 340" fill="none" stroke="#4fc3f7" stroke-width="14" stroke-linecap="round"/><rect x="220" y="380" width="72" height="24" rx="6" fill="#fff" opacity=".8"/></svg>'


def _generate_icon_png(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    # Draw a simple green circle in the center
    cx, cy, r = size // 2, size * 43 // 100, size * 18 // 100
    for y in range(size):
        for x in range(size):
            dx, dy = x - cx, y - cy
            d = (dx * dx + dy * dy) ** 0.5
            if abs(d - r) < size * 2 // 100:
                img.putpixel((x, y), (76, 175, 80, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


_icon_cache = {}


async def _handle_manifest(request):
    return web.Response(text=_MANIFEST, content_type="application/manifest+json")


async def _handle_sw(request):
    return web.Response(text=_SW_JS, content_type="application/javascript")


async def _handle_icon_svg(request):
    return web.Response(text=_ICON_SVG, content_type="image/svg+xml")


async def _handle_icon_png(request):
    size_str = request.match_info.get("size", "192")
    size = int(size_str)
    if size not in (192, 512):
        return web.Response(status=404)
    if size not in _icon_cache:
        loop = asyncio.get_event_loop()
        _icon_cache[size] = await loop.run_in_executor(None, _generate_icon_png, size)
    return web.Response(body=_icon_cache[size], content_type="image/png")


async def _handle_snapshot(request):
    quality = int(request.query.get("q", "50"))
    jpeg = snapshot_jpeg(quality)
    if jpeg:
        return web.Response(
            body=jpeg,
            content_type="image/jpeg",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    return web.Response(status=503)


async def _handle_health(request):
    arr, w, h, ts = get_latest_frame()
    return web.json_response({
        "ok": True,
        "has_frame": arr is not None and w > 0 and h > 0,
        "width": w,
        "height": h,
        "last_frame_ts": ts,
    })


async def _on_shutdown(app):
    coros = [pc.close() for pc in _pcs]
    await asyncio.gather(*coros)
    _pcs.clear()


# ---------------------------------------------------------------------------
# Telemetry collector — reads cereal, pushes to SSE clients
# ---------------------------------------------------------------------------

def _telemetry_collector():
    """Background thread: subscribe to cereal and push to SSE clients.

    System metrics (CPU temp/usage, memory) come from deviceState and are
    always available — even in standby with the car off.  Driving metrics
    (speed, gas, brake, cruise, lead) only populate when the vehicle is on.
    """
    global _telemetry_latest
    try:
        import cereal.messaging as messaging
    except ImportError:
        logger.warning("cereal not available — telemetry collector disabled")
        return

    topics = ['deviceState', 'carState', 'controlsState', 'modelV2', 'radarState',
              'liveMapDataSP', 'navInstruction']
    sm = None

    while True:
        # Don't poll cereal when nobody is listening — saves CPU + ZMQ overhead
        if not _sse_clients and not _pcs:
            _update_viewers_shm()
            time.sleep(2)
            continue

        if sm is None:
            sm = messaging.SubMaster(topics)
            logger.info("telemetry SubMaster created, topics=%s", topics)

        sm.update(0)  # non-blocking poll
        data = {}

        # ---- System metrics (hardwared/thermald — always running) ----
        try:
            if sm.alive['deviceState']:
                ds = sm['deviceState']
                temps = list(ds.cpuTempC)
                if temps:
                    data['cpuTemp'] = round(max(temps), 1)
                cpus = list(ds.cpuUsagePercent)
                if cpus:
                    data['cpuUsage'] = int(round(sum(cpus) / len(cpus)))
                data['memUsed'] = int(round(ds.memoryUsagePercent))
        except Exception:
            pass

        # ---- Car state (only when vehicle/panda connected) ----
        try:
            if sm.alive['carState']:
                cs = sm['carState']
                data['vEgo'] = round(cs.vEgo, 2)
                data['aEgo'] = round(cs.aEgo, 2)
                data['gas'] = int(round(cs.gas * 100))
                brake_val = getattr(cs, 'brake', 0.0)
                data['brake'] = int(round(brake_val * 100))
                if cs.brakePressed and data['brake'] < 10:
                    data['brake'] = 100
                data['setSpeed'] = int(round(cs.cruiseState.speed * 2.23694))  # m/s → mph
                data['cruiseEnabled'] = bool(cs.cruiseState.enabled)
        except Exception:
            pass

        # ---- Controls state ----
        try:
            if sm.alive['controlsState']:
                ctrl = sm['controlsState']
                data['driveState'] = 'active' if ctrl.enabled else 'standby'
        except Exception:
            pass

        # ---- Model execution time (only while driving) ----
        try:
            if sm.alive['modelV2']:
                mv = sm['modelV2']
                exec_time = getattr(mv, 'modelExecutionTime', 0)
                if exec_time > 0:
                    data['modelExec'] = round(exec_time * 1000, 1)  # s → ms
        except Exception:
            pass

        # ---- Lead vehicle ----
        try:
            if sm.alive['radarState']:
                rs = sm['radarState']
                lead = rs.leadOne
                if lead.status:
                    data['leadDist'] = round(lead.dRel, 1)
        except Exception:
            pass

        # ---- Speed limit (sunnypilot liveMapDataSP or navInstruction) ----
        try:
            sl = 0
            if sm.alive.get('liveMapDataSP', False):
                lmd = sm['liveMapDataSP']
                sl = getattr(lmd, 'speedLimit', 0)
            if sl <= 0 and sm.alive.get('navInstruction', False):
                ni = sm['navInstruction']
                sl = getattr(ni, 'speedLimit', 0)
            if sl > 0:
                data['speedLimit'] = int(round(sl * 2.23694))  # m/s → mph
        except Exception:
            pass

        # Defaults for fields the frontend expects
        data.setdefault('driveState', 'off')
        data.setdefault('cruiseEnabled', False)
        data.setdefault('setSpeed', 0)
        data.setdefault('vEgo', 0)
        data.setdefault('aEgo', 0)
        data.setdefault('gas', 0)
        data.setdefault('brake', 0)

        # Update shared state and push to SSE clients
        _telemetry_latest = data
        if _sse_clients:
            for q in list(_sse_clients):
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    # Drop oldest, push newest — keeps UI current
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(data)
                    except asyncio.QueueFull:
                        pass

        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Shared memory frame reader — runs in the server subprocess
# ---------------------------------------------------------------------------

def _shm_frame_reader(scale_val):
    """Background thread in server subprocess: read frames from shared memory.

    Polls the seqlock counter.  When a new frame arrives, does numpy
    reshape/flip/scale and publishes to the in-process frame bridge
    for CameraTrack to pick up.
    """
    import numpy as np
    from multiprocessing.shared_memory import SharedMemory

    parent_pid = os.getppid()
    log = logging.getLogger("ui_stream.shm_reader")
    shm = SharedMemory(name=_SHM_NAME, create=False)
    last_seq = 0

    log.info("shm reader started (parent PID %d, scale=%.2f)", parent_pid, scale_val)

    try:
        while True:
            # Exit if parent (UI process) died
            if os.getppid() != parent_pid:
                log.info("Parent process exited, shutting down")
                break

            # Read seqlock
            seq = struct.unpack_from('I', shm.buf, 0)[0]
            if seq == last_seq or seq % 2 != 0:
                time.sleep(0.01)
                continue

            w, h = struct.unpack_from('II', shm.buf, 4)
            if w <= 0 or h <= 0:
                time.sleep(0.01)
                continue

            data_size = w * h * 4
            raw = bytes(shm.buf[_SHM_HEADER:_SHM_HEADER + data_size])

            # Verify no torn read
            seq2 = struct.unpack_from('I', shm.buf, 0)[0]
            if seq != seq2:
                continue

            last_seq = seq

            # Numpy processing — all in this subprocess, not in UI process
            arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))
            rgb = arr[::-1, :, :3]  # flip vertically, drop alpha

            if 0 < scale_val < 1:
                step = max(1, int(round(1.0 / scale_val)))
                rgb = rgb[::step, ::step, :]
                h, w = rgb.shape[0], rgb.shape[1]

            if h % 2 != 0:
                rgb = rgb[:h - 1, :, :]
                h -= 1
            if w % 2 != 0:
                rgb = rgb[:, :w - 1, :]
                w -= 1

            rgb = np.ascontiguousarray(rgb)
            publish_frame(rgb, w, h)

            # Update viewers flag for UI process
            _update_viewers_shm()

            time.sleep(0.01)  # ~100Hz poll; actual rate limited by capture side
    finally:
        shm.close()
        log.info("shm reader stopped")


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_server(host="0.0.0.0", port=8082, fps=10, scale=0.5, server_mode=False):
    """Start the aiohttp/WebRTC server (blocking).

    In server_mode (subprocess), also starts the shared memory frame reader.
    """
    global _server_shm
    os.environ["STREAM_FPS"] = str(fps)

    _fh = logging.FileHandler("/tmp/ui_stream.log")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.root.addHandler(_fh)
    logging.root.setLevel(logging.INFO)
    logger.info("Starting UI WebRTC server on %s:%s at %s fps (scale=%s, server_mode=%s)",
                host, port, fps, scale, server_mode)

    if server_mode:
        from multiprocessing.shared_memory import SharedMemory
        _server_shm = SharedMemory(name=_SHM_NAME, create=False)
        # Set viewers=1 initially so UI starts capturing while we set up
        _server_shm.buf[12] = 1
        reader = threading.Thread(
            target=_shm_frame_reader, args=(float(scale),),
            daemon=True, name="shm-reader"
        )
        reader.start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_post("/offer", _handle_offer)
    app.router.add_get("/telemetry", _handle_telemetry)
    app.router.add_get("/telemetry/stream", _handle_telemetry_stream)
    app.router.add_get("/snapshot", _handle_snapshot)
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/manifest.json", _handle_manifest)
    app.router.add_get("/sw.js", _handle_sw)
    app.router.add_get("/icon.svg", _handle_icon_svg)
    app.router.add_get("/icon-{size}.png", _handle_icon_png)
    app.on_shutdown.append(_on_shutdown)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host, port)
    loop.run_until_complete(site.start())

    tc = threading.Thread(target=_telemetry_collector, daemon=True, name="telemetry-collector")
    tc.start()

    loop.run_forever()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Comma UI WebRTC streamer")
    parser.add_argument("--server", action="store_true", help="Run as server subprocess")
    parser.add_argument("--host", default=os.getenv("STREAM_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("STREAM_PORT", "8082")))
    parser.add_argument("--fps", type=int, default=int(os.getenv("STREAM_FPS", "5")))
    parser.add_argument("--scale", type=float, default=float(os.getenv("STREAM_SCALE", "0.5")))
    args = parser.parse_args()
    run_server(args.host, args.port, args.fps, scale=args.scale, server_mode=args.server)
