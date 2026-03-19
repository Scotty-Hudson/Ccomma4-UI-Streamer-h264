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
    # Uses the same approach as sunnypilot's RECORD mode:
    # rl.load_image_from_texture(render_texture.texture)
    # This avoids the Adreno GPU stride bug with glReadPixels.
    # -----------------------------------------------------------------
    _capture_logged = False

    def _do_capture_frame(app):
        global _capture_counter, _capture_logged
        _capture_counter += 1
        target_fps = int(os.getenv("STREAM_FPS", "10"))
        ui_fps = getattr(app, "target_fps", None) or getattr(app, "_target_fps", 30)
        skip = max(1, ui_fps // target_fps)
        if _capture_counter % skip != 0:
            return
        try:
            import numpy as np
            import pyray as rl
            from ui_frame_bridge import publish_frame

            rt = getattr(app, "_render_texture", None)
            if rt is None:
                return  # No render texture — nothing to capture

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

            # Exact same approach as sunnypilot RECORD:
            # image data is RGBA, w*h*4 bytes
            data_size = w * h * 4
            raw = bytes(rl.ffi.buffer(image.data, data_size))
            rl.unload_image(image)

            arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))
            rgb = arr[:, :, :3].copy()  # drop alpha, no flip needed
            publish_frame(rgb, w, h)
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
                if _stream_started:
                    _do_capture_frame(self)
                return _orig_frame(self, *args, **kwargs)

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
