"""Monkeypatch installer for UI streaming — loaded early via .pth file.

Installs a sys.meta_path import hook that intercepts the import of
openpilot.system.ui.lib.application and wraps GuiApplication methods
to start the WebRTC stream server and capture frames.

NO files inside /data/openpilot/ are modified.  This survives git resets,
overlay swaps, and sunnypilot updates.
"""
import os
import sys

# Quick exit if stream files are not deployed
if not os.path.exists("/data/ui_stream.py") or not os.path.exists("/data/ui_frame_bridge.py"):
    pass  # module imported but does nothing
else:
    # Ensure /data is on the path so ui_stream and ui_frame_bridge can be imported
    if "/data" not in sys.path:
        sys.path.insert(0, "/data")

    _TARGET_MODULE = "openpilot.system.ui.lib.application"
    _stream_started = False
    _capture_counter = 0

    # -----------------------------------------------------------------
    # Frame capture helper (called every render frame, throttled)
    # -----------------------------------------------------------------
    def _do_capture_frame(app):
        global _capture_counter
        rt = getattr(app, "_render_texture", None)
        if rt is None:
            return
        _capture_counter += 1
        target_fps = int(os.getenv("STREAM_FPS", "10"))
        ui_fps = getattr(app, "_target_fps", 30)
        skip = max(1, ui_fps // target_fps)
        if _capture_counter % skip != 0:
            return
        try:
            import numpy as np
            import pyray as rl
            from ui_frame_bridge import publish_frame

            image = rl.load_image_from_texture(rt.texture)
            w, h = image.width, image.height
            raw = bytes(rl.ffi.buffer(image.data, w * h * 4))
            rl.unload_image(image)
            arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))
            rgb = arr[::-1, :, :3].copy()
            publish_frame(rgb, w, h)
        except Exception:
            pass

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
        _orig_monitor_fps = GuiApp._monitor_fps

        def _init_window_with_stream(self, *args, **kwargs):
            _orig_init_window(self, *args, **kwargs)
            # Ensure render texture exists for frame capture
            if self._render_texture is None:
                import pyray as rl
                self._render_texture = rl.load_render_texture(self._width, self._height)
                rl.set_texture_filter(
                    self._render_texture.texture,
                    rl.TextureFilter.TEXTURE_FILTER_BILINEAR,
                )
            _start_stream()

        def _monitor_fps_with_capture(self):
            if _stream_started:
                _do_capture_frame(self)
            _orig_monitor_fps(self)

        GuiApp.init_window = _init_window_with_stream
        GuiApp._monitor_fps = _monitor_fps_with_capture
        GuiApp._stream_hooked = True

    # -----------------------------------------------------------------
    # sys.meta_path import hook
    # -----------------------------------------------------------------
    class _StreamFinder:
        """Intercepts import of the UI application module to inject streaming."""

        def find_module(self, fullname, path=None):
            if fullname == _TARGET_MODULE:
                return self
            return None

        def load_module(self, fullname):
            # Remove ourselves FIRST to avoid recursion
            sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _StreamFinder)]

            # Let Python do the real import
            __import__(fullname)
            module = sys.modules[fullname]

            # Monkeypatch
            _patch_gui_app(module)
            return module

    # Install the hook
    sys.meta_path.insert(0, _StreamFinder())
