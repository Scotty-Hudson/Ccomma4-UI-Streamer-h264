#!/usr/bin/env python3
"""DEPRECATED — This file is no longer used.

The new approach uses stream_hook.py installed via a .pth file in Python's
site-packages.  It monkeypatches GuiApplication at import time, so NO files
inside /data/openpilot/ are ever modified.  This survives git resets, overlay
swaps, and sunnypilot updates.

Run ensure_stream.sh to install the new system.
"""
import sys
print("stream_patch.py is DEPRECATED.")
print("The new installer uses a .pth monkeypatch that does not modify application.py.")
print("Run: sudo /data/ensure_stream.sh")
sys.exit(0)
