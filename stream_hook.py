"""Ultra-minimal .pth bootstrap — loaded in EVERY Python process.

CRITICAL: This file must be as small as possible.  Every byte is read,
compiled, and executed in ~30+ openpilot processes at startup (controlsd,
paramsd, torqued, etc.).  Any overhead here can cause timing violations
that trigger "Take Over Immediately" disengagements.

What this file does:
  - Imports only sys (already loaded by the interpreter)
  - Defines a tiny class with a single-method string comparison
  - Inserts it into sys.meta_path

What this file does NOT do:
  - No 'import os' (avoids loading os module if not already loaded)
  - No file I/O (no os.path.exists checks)
  - No function definitions beyond find_spec
  - No try/except blocks

Total cost per non-UI process: ~1 class definition + 1 list insert,
then ~0.5μs per import (string != comparison + return None).
"""
import sys


class _SF:
    """8-line meta_path finder.  Returns None for everything except
    the one UI module.  When that module IS imported, delegates to
    stream_hook_impl which contains all the heavy code."""

    def find_spec(self, name, path=None, target=None):
        if name != "openpilot.system.ui.lib.application":
            return None
        # This is the UI process — remove ourselves, load full impl
        sys.meta_path[:] = [x for x in sys.meta_path if not isinstance(x, _SF)]
        from stream_hook_impl import handle_ui_import
        return handle_ui_import(name)


sys.meta_path.insert(0, _SF())
