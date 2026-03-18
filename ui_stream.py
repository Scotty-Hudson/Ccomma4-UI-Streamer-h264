"""WebRTC stream server (H.264 preferred) for sunnypilot UI with telemetry overlay.
Imported lazily when STREAM=1."""
import threading
import io
import os
import asyncio

import json

import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import VideoFrame
from PIL import Image
import pyray as rl


# ---------------------------------------------------------------------------
# Frame relay: thread-safe bridge between sync render loop and async WebRTC
# ---------------------------------------------------------------------------

class FrameRelay:
    def __init__(self):
        self._arr = None
        self._cond = threading.Condition()

    def push(self, rgb_array):
        with self._cond:
            self._arr = rgb_array
            self._cond.notify_all()

    def wait_and_get(self, timeout=2.0):
        with self._cond:
            self._cond.wait(timeout=timeout)
            return self._arr

    def get(self):
        with self._cond:
            return self._arr

    def snapshot_jpeg(self, quality=50):
        arr = self.get()
        if arr is None:
            return None
        img = Image.fromarray(arr)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# WebRTC video track fed by the FrameRelay
# ---------------------------------------------------------------------------

class CameraTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, relay):
        super().__init__()
        self._relay = relay

    async def recv(self):
        loop = asyncio.get_event_loop()
        arr = await loop.run_in_executor(None, self._relay.wait_and_get, 2.0)
        if arr is None:
            frame = VideoFrame(width=1920, height=1080, format="rgb24")
        else:
            frame = VideoFrame.from_ndarray(arr, format="rgb24")
        pts, time_base = await self.next_timestamp()
        frame.pts = pts
        frame.time_base = time_base
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

/* ---------- Telemetry polling ---------- */
let lastData = null;
function poll() {
  fetch('/telemetry').then(r => r.json()).then(d => {
    lastData = d;

    // Set speed
    const sv = document.getElementById('set-val');
    sv.textContent = d.setSpeed > 0 ? d.setSpeed : '--';

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

  }).catch(() => {});
  setTimeout(poll, 250);
}

document.addEventListener("DOMContentLoaded",function(){
  startWebRTC();
  setTimeout(function(){window.scrollTo(0,1);},100);
  setTimeout(function(){window.scrollTo(0,0);},200);
  poll();
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

    @pc.on("connectionstatechange")
    async def _on_state():
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            _pcs.discard(pc)

    relay = request.app["relay"]
    pc.addTrack(CameraTrack(relay))

    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    answer = RTCSessionDescription(sdp=_prefer_h264(answer.sdp), type=answer.type)
    await pc.setLocalDescription(answer)

    return web.json_response(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )


async def _handle_telemetry(request):
    try:
        with open("/tmp/telemetry.json", "r") as fh:
            data = fh.read()
        return web.Response(
            text=data,
            content_type="application/json",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        return web.Response(status=503)


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
    relay = request.app["relay"]
    quality = int(request.query.get("q", "50"))
    jpeg = relay.snapshot_jpeg(quality)
    if jpeg:
        return web.Response(
            body=jpeg,
            content_type="image/jpeg",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    return web.Response(status=503)


async def _on_shutdown(app):
    coros = [pc.close() for pc in _pcs]
    await asyncio.gather(*coros)
    _pcs.clear()


# ---------------------------------------------------------------------------
# Background server (runs in its own thread with a new event loop)
# ---------------------------------------------------------------------------

def _run_server(relay, port):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app["relay"] = relay
    app.router.add_get("/", _handle_index)
    app.router.add_post("/offer", _handle_offer)
    app.router.add_get("/telemetry", _handle_telemetry)
    app.router.add_get("/snapshot", _handle_snapshot)
    app.router.add_get("/manifest.json", _handle_manifest)
    app.router.add_get("/sw.js", _handle_sw)
    app.router.add_get("/icon.svg", _handle_icon_svg)
    app.router.add_get("/icon-{size}.png", _handle_icon_png)
    app.on_shutdown.append(_on_shutdown)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", port)
    loop.run_until_complete(site.start())
    loop.run_forever()


# ---------------------------------------------------------------------------
# Public API (called from openpilot render loop)
# ---------------------------------------------------------------------------

_relay = None
_counter = 0


def start(port=8082):
    """Start the WebRTC signaling server in a background thread."""
    global _relay
    _relay = FrameRelay()
    t = threading.Thread(target=_run_server, args=(_relay, port), daemon=True)
    t.start()


def capture_frame(app, quality=50, target_fps=10):
    """Capture a frame from the render texture for WebRTC streaming."""
    global _counter
    if _relay is None or app._render_texture is None:
        return
    _counter += 1
    skip = max(1, app._target_fps // target_fps)
    if _counter % skip != 0:
        return
    si = rl.load_image_from_texture(app._render_texture.texture)
    raw = bytes(rl.ffi.buffer(si.data, si.width * si.height * 4))
    w, h = si.width, si.height
    rl.unload_image(si)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))
    arr = arr[::-1, :, :3].copy()
    _relay.push(arr)
