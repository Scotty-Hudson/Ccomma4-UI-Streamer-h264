"""Monkeypatch installer for UI streaming — loaded early via .pth file.

Installs a sys.meta_path import hook that intercepts the import of
openpilot.system.ui.lib.application and wraps GuiApplication methods
to start the WebRTC stream server and capture frames.

NO files inside /data/openpilot/ are modified.  This survives git resets,
overlay swaps, and sunnypilot updates.

DESIGN: This module is imported by EVERY Python process via the .pth file
(including safety-critical ones like controlsd, paramsd, etc.).  Therefore
the code at module level is kept absolutely minimal:
  - No imports beyond os/sys (already loaded)
  - No file I/O beyond two os.path.exists calls
  - No importlib imports (deferred until the target module is found)
  - The find_spec fast-path is a single string comparison + return None

The heavy work (importlib, threading, numpy, pyray, etc.) is deferred
until the one process that actually imports the UI module triggers it.
"""
import os
import sys

# Quick exit if stream files are not deployed
if not os.path.exists("/data/ui_stream.py") or not os.path.exists("/data/ui_frame_bridge.py"):
    pass
else:
    if "/data" not in sys.path:
        sys.path.insert(0, "/data")

    _TARGET_MODULE = "openpilot.system.ui.lib.application"

    class _StreamFinder:
        """Minimal meta_path finder — zero-cost for non-UI processes.

        find_spec returns None immediately for every module except the
        UI application module.  When the target IS found, we do the
        heavy lifting: import importlib, wrap the loader, define all
        the capture/streaming functions, and monkey-patch GuiApplication.
        """

        def find_spec(self, fullname, path, target=None):
            if fullname != _TARGET_MODULE:
                return None  # fast path — single string compare

            # This IS the UI process — remove ourselves and do everything
            sys.meta_path[:] = [f for f in sys.meta_path
                                if not isinstance(f, _StreamFinder)]

            import importlib.util
            spec = importlib.util.find_spec(fullname)
            if spec is None:
                return None

            spec.loader = _PatchingLoader(spec.loader)
            return spec

    # ------------------------------------------------------------------
    # Everything below is ONLY executed when find_spec matches the
    # target module.  In all other processes, none of this runs.
    # ------------------------------------------------------------------

    _stream_started = False
    _capture_counter = 0
    _capture_thread = None
    _capture_queue = None
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
                rgb = arr[::-1, :, :3].copy()
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

            data_size = w * h * 4
            raw = bytes(rl.ffi.buffer(image.data, data_size))
            rl.unload_image(image)

            _ensure_capture_thread()
            try:
                _capture_queue.put_nowait((raw, w, h))
            except Exception:
                pass  # drop frame if queue full — never block UI thread

        except Exception as e:
            import logging
            logging.getLogger("stream_hook").warning("capture error: %s", e, exc_info=True)

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

    def _patch_gui_app(module):
        GuiApp = getattr(module, "GuiApplication", None)
        if GuiApp is None or getattr(GuiApp, "_stream_hooked", False):
            return

        _orig_init_window = GuiApp.init_window

        def _init_window_with_stream(self, *args, **kwargs):
            _orig_init_window(self, *args, **kwargs)
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

    # Install the hook — this is the ONLY thing that runs at import time
    # besides the os.path.exists checks and sys.path append above.
    sys.meta_path.insert(0, _StreamFinder())
