"""
ghost-rec : private screen recorder for Windows.

- No window, no tray icon, no notifications, no preview. Nothing on screen
  ever indicates recording is happening.
- Hotkey controlled. Video files stay on this machine only.
- Makes zero network connections (one localhost UDP bind, used only to
  prevent launching a second copy of itself).

Run with:
    pythonw.exe ghost_rec.py        <- pythonw = no console window, ever

Hotkeys (change in CONFIG below):
    Ctrl+Shift+R .... start / stop recording
    Ctrl+Shift+Q .... quit the recorder entirely

Files land in  %USERPROFILE%\\Videos\\ghost\\  as ghost_YYYY-MM-DD_HH-MM-SS.mp4
A tiny ghost.log in the same folder records start/stop times (no content).
"""

import os
import queue
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import imageio.v2 as imageio
import keyboard
import mss
import numpy as np

# ----------------------------- CONFIG ---------------------------------
FPS = 30
MONITOR_INDEX = 1            # 1 = primary monitor, 0 = all monitors at once
OUTPUT_DIR = Path.home() / "Videos" / "ghost"
HOTKEY_TOGGLE = "ctrl+shift+r"
HOTKEY_QUIT = "ctrl+shift+q"
CRF = 20                     # video quality: 18 = near-lossless/big, 23 = small
SINGLE_INSTANCE_PORT = 53917  # localhost only; change if it clashes
# ------------------------------------------------------------------------

_state_lock = threading.Lock()
_recording = False
_quit = threading.Event()
_frames: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=90)
_session_stop: threading.Event | None = None
_session_thread: threading.Thread | None = None


def _log(msg: str) -> None:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_DIR / "ghost.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def _grab_loop() -> None:
    """Capture frames as fast as configured; drop rather than lag."""
    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            idx = MONITOR_INDEX if 0 <= MONITOR_INDEX < len(monitors) else 1
            mon = monitors[idx]
            while not _quit.is_set():
                if _recording:
                    shot = sct.grab(mon)
                    # BGRA -> RGB, contiguous for the encoder
                    frame = np.ascontiguousarray(np.array(shot)[:, :, 2::-1])
                    try:
                        _frames.put(frame, timeout=0.05)
                    except queue.Full:
                        pass
                else:
                    # drain anything stale while idle
                    try:
                        while True:
                            _frames.get_nowait()
                    except queue.Empty:
                        pass
                    time.sleep(0.05)
    except Exception as e:  # noqa: BLE001 - must never kill the hotkey thread
        _log(f"grab loop died: {e}")


def _write_loop(path: Path, done: threading.Event) -> None:
    """Encode one session to MP4. Runs on its own thread per recording."""
    try:
        writer = imageio.get_writer(
            str(path),
            fps=FPS,
            codec="libx264",
            macro_block_size=1,  # allow any resolution, no padding surprises
            ffmpeg_params=["-crf", str(CRF), "-preset", "veryfast",
                           "-pix_fmt", "yuv420p"],
        )
    except Exception as e:  # noqa: BLE001
        _log(f"could not open writer: {e}")
        return
    try:
        with writer:
            while not done.is_set() or not _frames.empty():
                try:
                    frame = _frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    writer.append_data(frame)
                except Exception as e:  # noqa: BLE001
                    _log(f"encode error: {e}")
                    break
    finally:
        _log(f"saved {path.name}")


def _start_session() -> None:
    global _session_stop, _session_thread
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = OUTPUT_DIR / f"ghost_{ts}.mp4"
    _session_stop = threading.Event()
    _session_thread = threading.Thread(
        target=_write_loop, args=(path, _session_stop), daemon=True)
    _session_thread.start()
    _log(f"recording started -> {path.name}")


def _end_session() -> None:
    global _session_stop, _session_thread
    if _session_stop is not None:
        _session_stop.set()
    if _session_thread is not None:
        _session_thread.join(timeout=15)
        _session_thread = None
    _session_stop = None


def toggle() -> None:
    global _recording
    try:
        with _state_lock:
            if not _recording:
                _recording = True
                _start_session()
            else:
                _recording = False
                _end_session()
    except Exception as e:  # noqa: BLE001
        _log(f"toggle failed: {e}")


def quit_app() -> None:
    global _recording
    try:
        with _state_lock:
            if _recording:
                _recording = False
                _end_session()
    finally:
        _quit.set()


def _already_running() -> bool:
    """Single instance via localhost UDP bind. OS releases it on death,
    so no stale lockfiles ever."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        return True
    return False  # socket stays open for the life of the process


def main() -> None:
    if _already_running():
        return  # silent: the other copy is already doing the job
    # keep the guard socket alive
    _guard = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _log("ghost-rec started")

    threading.Thread(target=_grab_loop, daemon=True).start()
    keyboard.add_hotkey(HOTKEY_TOGGLE, toggle)
    keyboard.add_hotkey(HOTKEY_QUIT, quit_app)

    _quit.wait()  # block until the quit hotkey fires
    keyboard.unhook_all()
    _guard.close()
    _log("ghost-rec stopped")


if __name__ == "__main__":
    main()
