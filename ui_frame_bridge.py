"""Thread-safe shared frame buffer between UI render loop and WebRTC server."""
import threading
import time
import io

import numpy as np
from PIL import Image

_lock = threading.Lock()
_latest_frame = None
_latest_width = 0
_latest_height = 0
_latest_ts = 0.0
_cond = threading.Condition(_lock)


def publish_frame(frame_rgb, width, height):
    """Push a new RGB numpy array from the render loop."""
    global _latest_frame, _latest_width, _latest_height, _latest_ts
    if frame_rgb is None:
        return
    with _cond:
        _latest_frame = frame_rgb
        _latest_width = width
        _latest_height = height
        _latest_ts = time.time()
        _cond.notify_all()


def get_latest_frame():
    """Return (frame_rgb, width, height, timestamp) or (None, 0, 0, 0.0)."""
    with _lock:
        if _latest_frame is None:
            return None, 0, 0, 0.0
        return _latest_frame.copy(), _latest_width, _latest_height, _latest_ts


def wait_for_frame(timeout=2.0):
    """Block until a new frame arrives, then return it."""
    with _cond:
        _cond.wait(timeout=timeout)
        if _latest_frame is None:
            return None, 0, 0, 0.0
        return _latest_frame.copy(), _latest_width, _latest_height, _latest_ts


def snapshot_jpeg(quality=50):
    """Return current frame as JPEG bytes, or None."""
    with _lock:
        if _latest_frame is None:
            return None
        img = Image.fromarray(_latest_frame)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()
