"""Monkeypatch installer for UI streaming — loaded early via .pth file.

Installs a sys.meta_path import hook that intercepts the import of
openpilot.system.ui.lib.application and wraps GuiApplication methods
to start the WebRTC stream server and capture frames.

NO files inside /data/openpilot/ are modified.  This survives git resets,
overlay swaps, and sunnypilot updates.

IMPORTANT: This module is imported by EVERY Python process via the .pth
file.  We must exit immediately in non-UI processes to avoid interfering
with safety-critical processes (controlsd, paramsd, plannerd, etc.).
"""
import os
import sys


def _is_safe_to_hook():
    """Check if it's safe to install the streaming hook in this process."""
    _BLOCKED = (
        "controlsd", "paramsd", "plannerd", "radard",
        "dmonitoringd", "calibrationd", "locationd",
        "hardwared", "thermald", "modeld", "navmodeld",
        "ubloxd", "pandad", "pigeond", "sensord",
        "boardd", "loggerd", "encoderd", "proclogd",
        "logmessaged", "tombstoned", "updated", "uploader",
        "athenad", "manage_athenad", "deleter",
        "rawgpsd", "laikad", "torqued",
    )
    try:
        with open("/proc/self/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", errors="replace").lower()
        blocked = any(b in cmdline for b in _BLOCKED)
        # Debug: log every process decision to help diagnose issues
        try:
            with open("/tmp/stream_hook_guard.log", "a") as dbg:
                dbg.write(f"pid={os.getpid()} blocked={blocked} cmdline={cmdline!r}\n")
        except Exception:
            pass
        return not blocked
    except Exception:
        return False


# Quick exit: only proceed if stream files are deployed AND we're not a blocked process
if (os.path.exists("/data/ui_stream.py")
        and os.path.exists("/data/ui_frame_bridge.py")
        and _is_safe_to_hook()):

    # Ensure /data is on the path so ui_stream and ui_frame_bridge can be imported
    if "/data" not in sys.path:
        sys.path.insert(0, "/data")

    _TARGET_MODULE = "openpilot.system.ui.lib.application"
    _stream_started = False
    _capture_counter = 0
    _capture_thread = None
    _capture_queue = None

    # -----------------------------------------------------------------
    # Frame capture helper (called every render frame, throttled)
    # Uses the same approach as sunnypilot's RECORD mode:
    # rl.load_image_from_texture(render_texture.texture)
    # This avoids the Adreno GPU stride bug with glReadPixels.
    #
    # IMPORTANT: The GPU readback (load_image_from_texture + buffer copy)
    # must happen on the UI thread, but the heavy numpy work (reshape,
    # flip, strip alpha) is offloaded to a background thread so we never
    # delay the UI process watchdog heartbeat.
    # -----------------------------------------------------------------
    _capture_logged = False

    def _capture_worker():
        """Background thread that processes raw frame buffers."""
        import numpy as np
        from ui_frame_bridge import publish_frame
        import logging
        log = logging.getLogger("stream_hook")

        while True:
            try:
                raw, w, h = _capture_queue.get()
                arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))
                rgb = arr[::-1, :, :3].copy()  # flip vertically, drop alpha
                publish_frame(rgb, w, h)
            except Exception as e:
                log.warning("capture worker error: %s", e)

    def _ensure_capture_thread():
        global _capture_thread, _capture_queue
        if _capture_thread is not None:
            return
        import threading
        import queue
        _capture_queue = queue.Queue(maxsize=2)
        _capture_thread = threading.Thread(
            target=_capture_worker, daemon=True, name="stream-capture"
        )
        _capture_thread.start()

    def _do_capture_frame(app):
        """Grab raw pixels on UI thread (fast), queue heavy work to background."""
        global _capture_counter, _capture_logged
        _capture_counter += 1
        target_fps = int(os.getenv("STREAM_FPS", "10"))
        ui_fps = getattr(app, "target_fps", None) or getattr(app, "_target_fps", 30)
        skip = max(1, ui_fps // target_fps)
        if _capture_counter % skip != 0:
            return
        try:
            import pyray as rl

            rt = getattr(app, "_render_texture", None)
            if rt is None:
                return

            image = rl.load_image_from_texture(rt.texture)
            w, h = image.width, image.height
            if w <= 0 or h <= 0:
                rl.unload_image(image)
                return

            if not _capture_logged:
                import logging
                logging.getLogger("stream_hook").info(
                    "first capture: %dx%d fmt=%d", w, h, image.format,
                )
                _capture_logged = True

            # Copy raw bytes on UI thread (fast ~0.5ms for 536x240)
            data_size = w * h * 4
            raw = bytes(rl.ffi.buffer(image.data, data_size))
            rl.unload_image(image)

            # Queue heavy numpy work to background thread
            _ensure_capture_thread()
            try:
                _capture_queue.put_nowait((raw, w, h))
            except Exception:
                pass  # drop frame if queue full — never block UI thread

        except Exception as e:
            import logging
            logging.getLogger("stream_hook").warning("capture error: %s", e, exc_info=True)

    # -----------------------------------------------------------------
    # Start the WebRTC server (once)
    # -----------------------------------------------------------------
    def _start_stream():
        global _stream_started
        if _stream_started:
            return
        try:
            import threading
            import ui_stream

            port = int(os.getenv("STREAM_PORT", "8082"))
            fps = int(os.getenv("STREAM_FPS", "10"))
            t = threading.Thread(
                target=ui_stream.run_server,
                kwargs={"port": port, "fps": fps},
                daemon=True,
                name="ui-webrtc",
            )
            t.start()
            _stream_started = True
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Apply monkeypatch to GuiApplication
    # -----------------------------------------------------------------
    def _patch_gui_app(module):
        GuiApp = getattr(module, "GuiApplication", None)
        if GuiApp is None or getattr(GuiApp, "_stream_hooked", False):
            return

        _orig_init_window = GuiApp.init_window

        def _init_window_with_stream(self, *args, **kwargs):
            _orig_init_window(self, *args, **kwargs)
            # Create render texture if not already present (e.g. RECORD mode).
            # The render() method already supports _render_texture — when it
            # exists, it renders to the texture then draws it to screen.
            # We capture frames from this texture (same as RECORD mode).
            if getattr(self, "_render_texture", None) is None:
                import pyray as rl
                sw = getattr(self, "_scaled_width", None) or self.width
                sh = getattr(self, "_scaled_height", None) or self.height
                self._render_texture = rl.load_render_texture(int(sw), int(sh))
                rl.set_texture_filter(
                    self._render_texture.texture,
                    rl.TextureFilter.TEXTURE_FILTER_BILINEAR,
                )
            _start_stream()

        # Try wrapping _monitor_fps (per-frame). If it doesn't exist,
        # try wrapping 'render' or 'paint' as alternatives.
        _frame_method = None
        for name in ("_monitor_fps", "render", "paint"):
            if hasattr(GuiApp, name):
                _frame_method = name
                break

        if _frame_method:
            _orig_frame = getattr(GuiApp, _frame_method)

            def _frame_with_capture(self, *args, **kwargs):
                result = _orig_frame(self, *args, **kwargs)  # heartbeat FIRST
                if _stream_started:
                    _do_capture_frame(self)
                return result

            setattr(GuiApp, _frame_method, _frame_with_capture)

        GuiApp.init_window = _init_window_with_stream
        GuiApp._stream_hooked = True

    # -----------------------------------------------------------------
    # sys.meta_path import hook  (Python 3.12+ uses find_spec only)
    # -----------------------------------------------------------------
    import importlib
    import importlib.abc
    import importlib.util

    class _PatchingLoader:
        """Wraps the real loader to apply monkeypatch after module exec."""

        def __init__(self, original_loader):
            self._original = original_loader

        def create_module(self, spec):
            if hasattr(self._original, "create_module"):
                return self._original.create_module(spec)
            return None

        def exec_module(self, module):
            self._original.exec_module(module)
            _patch_gui_app(module)

    class _StreamFinder(importlib.abc.MetaPathFinder):
        """Intercepts import of the UI application module to inject streaming."""

        def find_spec(self, fullname, path, target=None):
            if fullname != _TARGET_MODULE:
                return None

            # Remove ourselves FIRST to avoid recursion
            sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _StreamFinder)]

            # Find the real module spec using the remaining finders
            spec = importlib.util.find_spec(fullname)
            if spec is None:
                return None

            # Wrap the loader so we can patch after exec
            spec.loader = _PatchingLoader(spec.loader)
            return spec

    # Install the hook
    sys.meta_path.insert(0, _StreamFinder())
