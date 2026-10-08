"""Test doubles for ghost-rec's Windows-only / hardware dependencies.

ghost_rec.py runs on Windows with a real screen, hotkeys and (optionally)
WASAPI audio. The logic tests below run on headless CI, so this module
installs lightweight stub modules into sys.modules BEFORE ghost_rec is
imported:

- mss ........ fake screen grabber returning tiny synthetic BGRA frames
- keyboard ... no-op hotkey registration
- sounddevice  present but WITHOUT WasapiSettings -> exercises the
               "no WASAPI on this platform" graceful-fallback path
"""

import os
import sys
import types

# Make the repo root (where ghost_rec.py lives) importable even when the
# tests are run from elsewhere, e.g. plain `pytest` in CI.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


# --- mss stub ---------------------------------------------------------------
mss_stub = types.ModuleType("mss")


class _FakeShot:
    """What sct.grab() returns: behaves like an mss screenshot."""

    def __init__(self, w=32, h=24):
        self._data = np.zeros((h, w, 4), dtype=np.uint8)
        self._data[:, :, 0] = 255  # blue channel; arbitrary, only shape matters

    def __array__(self, dtype=None, copy=None):
        arr = self._data.astype(dtype) if dtype else self._data
        return arr.copy() if copy else arr


class _FakeMSS:
    def __init__(self):
        self.monitors = [
            {"left": 0, "top": 0, "width": 32, "height": 24},  # 0: all monitors
            {"left": 0, "top": 0, "width": 32, "height": 24},  # 1: primary
        ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def grab(self, monitor):
        return _FakeShot()


mss_stub.mss = _FakeMSS
sys.modules.setdefault("mss", mss_stub)


# --- keyboard stub ------------------------------------------------------------
keyboard_stub = types.ModuleType("keyboard")
keyboard_stub.add_hotkey = lambda *a, **k: None
keyboard_stub.unhook_all = lambda: None
sys.modules.setdefault("keyboard", keyboard_stub)


# --- sounddevice stub (no WASAPI -> platform-fallback path) -------------------
sounddevice_stub = types.ModuleType("sounddevice")
sounddevice_stub.__doc__ = "test stub: no WasapiSettings, like non-Windows hosts"
sys.modules.setdefault("sounddevice", sounddevice_stub)
