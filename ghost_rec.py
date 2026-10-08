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

Reliability notes (logic audit 2026-10-08):
- The capture loop is paced to FPS: every appended frame is stamped
  1/FPS apart, so grabbing must not run faster than FPS or the video
  plays back sped up. Slow machines drop frames instead of lagging.
- Each session gets its own frame queue and a stop sentinel, so ending
  one session and starting the next can never mix frames between files,
  even if the encoder is still flushing the old one.
- A supervisor restarts the capture loop if it dies, so a transient
  grab failure can't silently stop recording mid-session.
"""

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
FPS = 60                     # capture is paced to this rate (see above)
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
_frames: "queue.Queue | None" = None  # current session's queue; None when idle
_session_thread: threading.Thread | None = None
_session_path: Path | None = None
_wake = threading.Event()  # set when a session starts; lets the idle grab
                           # loop wake promptly instead of sleeping blind

# End-of-session marker placed on a session's queue. It is a unique object,
# never a real frame, so the writer can't mistake image data for it.
_SENTINEL = object()


def _log(msg: str) -> None:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_DIR / "ghost.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass  # log disk unwritable; nothing else we can do about it


def _safe_append(writer, frame: np.ndarray, path: Path) -> bool:
    """Append one frame; on encoder failure log it and report False so the
    caller stops the session instead of looping on a dead writer."""
    try:
        writer.append_data(frame)
        return True
    except Exception as e:  # noqa: BLE001 - encoder errors must not kill us
        _log(f"encode error in {path.name}: {e}; session aborted, file may be short")
        return False


def _grab_loop() -> None:
    """Capture frames paced to FPS; drop rather than lag.

    Raises on failure so the supervisor can restart it -- this must never
    die quietly mid-session.
    """
    with mss.mss() as sct:
        monitors = sct.monitors
        idx = MONITOR_INDEX if 0 <= MONITOR_INDEX < len(monitors) else 1
        mon = monitors[idx]
        frame_interval = 1.0 / FPS
        while not _quit.is_set():
            # Plain attribute read; the reference is only swapped under
            # _state_lock, and GIL makes the read itself atomic.
            q = _frames
            if q is None:
                # Idle: wait to be woken when a session starts instead of
                # spin-waiting. The event is only a hint -- _frames is the
                # authority and is re-read every iteration, so a missed or
                # stale wake just costs one short sleep.
                _wake.wait(timeout=0.25)
                _wake.clear()
                continue
            t0 = time.perf_counter()
            shot = sct.grab(mon)
            # BGRA -> RGB, contiguous for the encoder
            frame = np.ascontiguousarray(np.array(shot)[:, :, 2::-1])
            try:
                q.put(frame, timeout=0.05)
            except queue.Full:
                pass  # drop rather than lag
            # Pace to FPS: each frame is stamped 1/FPS apart, so grabbing
            # faster would make the video play back sped up.
            spare = frame_interval - (time.perf_counter() - t0)
            if spare > 0:
                time.sleep(spare)


def _grab_supervisor() -> None:
    """Keep the capture loop alive. A transient grab failure (resolution
    change, driver hiccup) restarts the loop with backoff instead of
    silently ending recording."""
    backoff = 1.0
    while not _quit.is_set():
        try:
            _grab_loop()
        except Exception as e:  # noqa: BLE001 - log and restart, never die quietly
            _log(f"grab loop died ({e}); restarting capture")
        if _quit.wait(timeout=min(backoff, 30.0)):
            break
        backoff *= 2


def _write_loop(path: Path, frames: "queue.Queue", ready: threading.Event) -> None:
    """Encode one session to MP4 on its own thread.

    Owns `frames` and `path` exclusively: even if a new session starts
    while this one is still flushing, no frame can cross between files.
    Exits only on the sentinel (posted by _end_session) or encoder failure,
    so every queued frame is written before the file is finalized.
    """
    global _session_thread, _frames, _session_path, _recording
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
        _log(f"could not open writer for {path.name}: {e}; session aborted")
        return
    ready.set()  # writer is open; the session may now accept frames
    try:
        with writer:
            ok = True
            while ok:
                try:
                    frame = frames.get(timeout=1.0)
                except queue.Empty:
                    continue  # session still open, just no frames yet
                if frame is _SENTINEL:
                    # Drain stragglers grabbed concurrently with the stop.
                    # Nothing new can arrive: _frames was swapped to None
                    # before the sentinel was posted.
                    while ok:
                        try:
                            extra = frames.get_nowait()
                        except queue.Empty:
                            break
                        if extra is _SENTINEL:
                            continue
                        ok = _safe_append(writer, extra, path)
                    break
                ok = _safe_append(writer, frame, path)
    finally:
        _log(f"saved {path.name}")
        # If the encoder died on its own (not via _end_session), clear the
        # session state so the next toggle starts fresh instead of
        # "stopping" a dead session. Non-blocking: never stall the writer
        # thread on the state lock.
        if _state_lock.acquire(blocking=False):
            try:
                if _session_thread is threading.current_thread():
                    _session_thread = None
                    _frames = None
                    _session_path = None
                    if _recording:
                        _recording = False
                        _log(f"{path.name}: encoder failed; recording auto-stopped")
            finally:
                _state_lock.release()


def _start_session() -> bool:
    """Spin up the writer thread for a new session.

    Returns True once the writer is actually open. On failure returns
    False and leaves no session behind (so toggle() can reset cleanly).
    Must be called with _state_lock held.
    """
    global _frames, _session_thread, _session_path
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = OUTPUT_DIR / f"ghost_{ts}.mp4"
    n = 1
    while path.exists():
        # Same-second double-toggle would otherwise write two sessions
        # into one file.
        n += 1
        path = OUTPUT_DIR / f"ghost_{ts}_{n}.mp4"
    frames: "queue.Queue" = queue.Queue(maxsize=90)
    ready = threading.Event()
    thread = threading.Thread(
        target=_write_loop, args=(path, frames, ready), daemon=True)
    thread.start()
    if not ready.wait(timeout=5.0):
        thread.join(timeout=2.0)
        _log(f"writer never became ready for {path.name}; session aborted")
        return False
    # Publish only after the writer is open, so the grab loop can only
    # feed a live encoder. Set the wake AFTER publishing: the grab loop
    # re-reads _frames every iteration, so ordering is what wakes it.
    _frames = frames
    _session_thread = thread
    _session_path = path
    _wake.set()
    _log(f"recording started -> {path.name}")
    return True


def _end_session(timeout: float = 15.0) -> None:
    """Signal the writer to finish via sentinel, then wait for it.

    The writer owns its queue and file, so abandoning it after `timeout`
    can't corrupt the next session -- it just finishes its own file late.
    The join happens outside _state_lock (callers release it first) so
    hotkeys stay responsive while the encoder flushes.
    """
    global _frames, _session_thread, _session_path
    with _state_lock:
        frames, thread, path = _frames, _session_thread, _session_path
        _frames, _session_thread, _session_path = None, None, None
        _wake.clear()
    if frames is None or thread is None:
        return
    if not thread.is_alive():
        return  # writer already exited (e.g. encode error); nothing to wait for
    # Post the sentinel; make room first if the queue is momentarily full.
    # (Only possible while the writer is still draining; the grab loop only
    # ever drops, never blocks, so this terminates.)
    for _ in range(1000):
        try:
            frames.put_nowait(_SENTINEL)
            break
        except queue.Full:
            try:
                frames.get_nowait()  # drop one tail frame to make room
            except queue.Empty:
                pass
    else:
        _log(f"could not post stop sentinel for {path.name}; abandoning writer")
        return
    thread.join(timeout=timeout)
    if thread.is_alive():
        _log(f"WARNING: encoder for {path.name} still alive after "
             f"{timeout:.0f}s; left to finish its own file")


def toggle() -> None:
    global _recording
    stopping = False
    try:
        with _state_lock:
            if not _recording:
                _recording = True
                try:
                    started = _start_session()
                except Exception as e:  # noqa: BLE001
                    _log(f"session start crashed: {e}")
                    started = False
                if not started:
                    _recording = False
                    _log("toggle: writer failed to start, staying idle")
            else:
                _recording = False
                stopping = True
    except Exception as e:  # noqa: BLE001 - lock acquisition itself failed
        _log(f"toggle failed: {e}")
        return
    if stopping:
        _end_session()  # join outside the lock; hotkeys stay responsive


def quit_app() -> None:
    global _recording
    stopping = False
    with _state_lock:
        if _recording:
            _recording = False
            stopping = True
    if stopping:
        _end_session(timeout=60.0)  # generous: this is the file Alex keeps
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

    threading.Thread(target=_grab_supervisor, daemon=True).start()
    keyboard.add_hotkey(HOTKEY_TOGGLE, toggle)
    keyboard.add_hotkey(HOTKEY_QUIT, quit_app)

    _quit.wait()  # block until the quit hotkey fires
    keyboard.unhook_all()
    _guard.close()
    _log("ghost-rec stopped")


if __name__ == "__main__":
    main()
