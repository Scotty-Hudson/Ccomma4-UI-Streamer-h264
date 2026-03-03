"""MJPEG stream server for sunnypilot UI. Imported lazily when STREAM=1."""
import threading
import io
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image
import pyray as rl


class StreamState:
    def __init__(self):
        self.frame = b""
        self.lock = threading.Lock()
        self.event = threading.Event()

    def update(self, jpeg):
        with self.lock:
            self.frame = jpeg
        self.event.set()

    def get(self):
        with self.lock:
            return self.frame

    def wait(self, t=2.0):
        self.event.wait(t)
        self.event.clear()


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=--frame")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                while True:
                    self.server._state.wait(2.0)
                    f = self.server._state.get()
                    if f:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(f)).encode() + b"\r\n\r\n")
                        self.wfile.write(f)
                        self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/snapshot":
            f = self.server._state.get()
            if f:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(f)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(f)
            else:
                self.send_response(503)
                self.end_headers()
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<!DOCTYPE html><html><head><title>openpilot</title><style>body{margin:0;background:#000;display:flex;justify-content:center;align-items:center;height:100vh}img{max-width:100%;max-height:100vh}</style></head><body><img src="/stream"></body></html>')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


_state = None
_counter = 0


def start(port=8082):
    """Start the MJPEG HTTP server in a background thread."""
    global _state
    _state = StreamState()
    srv = HTTPServer(("0.0.0.0", port), StreamHandler)
    srv._state = _state
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return _state


def capture_frame(app, quality=50, target_fps=10):
    """Call this from the render loop to capture a frame."""
    global _counter
    if _state is None or app._render_texture is None:
        return
    _counter += 1
    skip = max(1, app._target_fps // target_fps)
    if _counter % skip != 0:
        return
    si = rl.load_image_from_texture(app._render_texture.texture)
    raw = bytes(rl.ffi.buffer(si.data, si.width * si.height * 4))
    rl.unload_image(si)
    img = Image.frombytes("RGBA", (si.width, si.height), raw).transpose(Image.FLIP_TOP_BOTTOM).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    _state.update(buf.getvalue())
