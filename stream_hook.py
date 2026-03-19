"""DEPRECATED — .pth hook approach abandoned.

The .pth hook ran code in EVERY Python process at startup, including
safety-critical ones (controlsd, paramsd, torqued, etc.).  Even with
ultra-minimal bootstraps, sys.meta_path insertion caused timing
violations that triggered "Take Over Immediately" disengagements.

The current approach uses stream_patch.py to directly patch
application.py — streaming code ONLY runs inside the UI process.

This file is kept in the repo for historical reference only.
ensure_stream.sh removes it from /data/ during installation.
"""
