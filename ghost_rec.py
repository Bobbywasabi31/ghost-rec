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
    Ctrl+Shift+A .... toggle audio capture (system audio via WASAPI loopback)
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

Audio notes (added 2026-10-08; NOT yet tested on Windows):
- Audio is OFF by default (opt-in). Ctrl+Shift+A flips it; the choice is
  persisted in OUTPUT_DIR/audio.enabled so it survives restarts.
- Capture is WASAPI loopback of the default output device via the
  `sounddevice` package -- i.e. whatever the speakers play (calls,
  videos, notification sounds). There is no microphone capture.
- Audio is written as 16-bit PCM WAV alongside the video, then muxed
  into the MP4 when the session ends (video stream copied, audio as
  AAC). If the mux fails, both files are kept and the failure is logged.
- The ffmpeg mux runs with CREATE_NO_WINDOW so no console ever flashes.
"""

import os
import queue
import socket
import subprocess
import threading
import time
import wave
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
HOTKEY_AUDIO = "ctrl+shift+a"  # toggle system-audio capture (off by default)
HOTKEY_QUIT = "ctrl+shift+q"
CRF = 20                     # video quality: 18 = near-lossless/big, 23 = small
SINGLE_INSTANCE_PORT = 53917  # localhost only; change if it clashes
AUDIO_SAMPLE_FORMAT = "<i2"   # 16-bit PCM in the temp WAV
# ------------------------------------------------------------------------

_state_lock = threading.Lock()
_recording = False
_quit = threading.Event()
_frames: "queue.Queue | None" = None  # current session's queue; None when idle
_session_thread: threading.Thread | None = None
_session_path: Path | None = None
_session_start_t = 0.0  # time.time() when the current session started
_wake = threading.Event()  # set when a session starts; lets the idle grab
                           # loop wake promptly instead of sleeping blind

# Audio state. At most ONE audio segment per session is kept for the mux:
# enabling audio mid-session discards any earlier partial segment (logged),
# disabling mid-session keeps the partial segment for the session-end mux.
_audio_enabled = False                    # preference, persisted to audio.enabled
_audio_thread: threading.Thread | None = None
_audio_stop: threading.Event | None = None
_audio_seg: "tuple[Path, float] | None" = None  # (temp wav, delay vs video start)

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


def _audio_flag_path() -> Path:
    return OUTPUT_DIR / "audio.enabled"


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


# ----------------------------- AUDIO ----------------------------------
def _discard_wav(path: Path, why: str) -> None:
    try:
        path.unlink()
    except OSError:
        pass
    _log(f"audio segment {path.name} discarded ({why})")


def _start_audio(wav_path: Path, delay: float) -> bool:
    """Start WASAPI-loopback audio capture into `wav_path`.

    `delay` is seconds between video-session start and audio start (0 when
    audio was on from the beginning); it is applied at mux time so the
    track lines up with the video. Replaces any earlier partial segment of
    this session (logged). Never raises; returns False on any failure and
    the session continues video-only.
    """
    global _audio_thread, _audio_stop, _audio_seg
    try:
        import sounddevice as sd  # noqa: PLC0415 - optional dep, Windows only
    except ImportError:
        _log("audio capture requested but the 'sounddevice' package is not "
             "installed; recording video only (pip install -r requirements.txt)")
        return False
    if getattr(sd, "WasapiSettings", None) is None:
        _log("audio capture needs WASAPI loopback, which this platform does "
             "not provide; recording video only")
        return False
    try:
        dev = sd.query_devices(kind="output")
        samplerate = int(dev.get("default_samplerate") or 48000)
        stream = sd.InputStream(
            samplerate=samplerate,
            channels=2,
            dtype="float32",
            device=dev["index"],
            blocksize=4800,  # ~100 ms at 48 kHz; keeps stop responsive
            extra_settings=sd.WasapiSettings(loopback=True),
        )
    except Exception as e:  # noqa: BLE001 - PortAudio/device errors vary
        _log(f"audio capture unavailable ({e}); recording video only")
        return False

    stop = threading.Event()

    def _run() -> None:
        try:
            with stream, wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(samplerate)
                while not stop.is_set():
                    try:
                        data, overflow = stream.read(4800)
                    except Exception as e:  # noqa: BLE001
                        _log(f"audio read failed ({e}); audio stops here, "
                             "session continues video-only")
                        return
                    if overflow:
                        _log("audio input overflowed; some audio dropped")
                    pcm = np.clip(np.asarray(data, dtype=np.float32), -1.0, 1.0)
                    wav.writeframes((pcm * 32767).astype(AUDIO_SAMPLE_FORMAT).tobytes())
        except Exception as e:  # noqa: BLE001
            _log(f"audio capture died ({e}); session continues video-only")

    thread = threading.Thread(target=_run, daemon=True, name="ghost-audio")
    with _state_lock:
        if _audio_thread is not None and _audio_thread.is_alive():
            _log("audio capture already running; not starting a second stream")
            stream.close()
            return False
        old = _audio_seg
        _audio_seg = (wav_path, delay)
        _audio_thread, _audio_stop = thread, stop
    if old is not None:
        _discard_wav(old[0], "replaced by a newer audio segment")
    thread.start()
    _log(f"audio capture started -> {wav_path.name} ({samplerate} Hz stereo)")
    return True


def _stop_audio_capture() -> None:
    """Stop the live audio thread. The WAV is kept: _end_session muxes it
    into the video (or a mid-session toggle already banked it)."""
    global _audio_thread, _audio_stop
    with _state_lock:
        thread, stop = _audio_thread, _audio_stop
        _audio_thread, _audio_stop = None, None
    if thread is not None:
        stop.set()
        thread.join(timeout=5.0)  # reads block ~100 ms max, so this is generous
        if thread.is_alive():
            _log("WARNING: audio thread still alive after 5s; abandoning it")


def _mux_audio(video: Path, wav: Path, delay: float) -> None:
    """Mux the session's audio WAV into its MP4 (video copied, audio AAC).

    On any failure the video is left untouched and the WAV is kept as a
    sidecar -- audio must never cost Alex his video.
    """
    try:
        size = wav.stat().st_size
    except OSError:
        return  # nothing captured; nothing to mux
    if size < 4096:  # header plus essentially no samples
        _log(f"audio track {wav.name} too short ({size} B); keeping video only")
        _discard_wav(wav, "no usable audio captured")
        return
    try:
        import imageio_ffmpeg  # noqa: PLC0415 - already a hard dependency
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        _log(f"ffmpeg unavailable for audio mux ({e}); keeping {wav.name} as sidecar")
        try:
            wav.rename(video.with_name(video.stem + "_audio.wav"))
        except OSError:
            pass
        return
    out = video.with_name(video.stem + "_mux.mp4")
    cmd = [exe, "-y", "-v", "error", "-i", str(video)]
    if delay > 0.05:
        # Audio started after the video; shift it so the track lines up.
        cmd += ["-itsoffset", f"{delay:.3f}"]
    cmd += ["-i", str(wav), "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(out)]
    run_kwargs: dict = {}
    if os.name == "nt":
        # Never flash a console window: that would break ghost's whole point.
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(cmd, capture_output=True, timeout=180, check=True,
                       **run_kwargs)
    except Exception as e:  # noqa: BLE001
        err = ""
        if hasattr(e, "stderr") and e.stderr:
            err = e.stderr.decode(errors="replace")[-400:]
        _log(f"audio mux failed ({e}); keeping video + {wav.name} sidecar. {err}")
        try:
            wav.rename(video.with_name(video.stem + "_audio.wav"))
        except OSError:
            pass
        return
    try:
        os.replace(out, video)  # atomic: the MP4 name never changes
        wav.unlink()
        _log(f"audio muxed into {video.name} (offset {delay:.2f}s)")
    except OSError as e:
        _log(f"mux finalize failed ({e}); files left: {video.name}, {out.name}")


def toggle_audio() -> None:
    """Flip the audio-capture preference (persisted). Applies immediately:
    enabling mid-session starts audio from now; disabling mid-session
    stops it but keeps the partial track for the session-end mux."""
    global _audio_enabled
    with _state_lock:
        _audio_enabled = not _audio_enabled
        on = _audio_enabled
        recording = _recording
        path = _session_path
        started = _session_start_t
    try:
        if on:
            _audio_flag_path().touch(exist_ok=True)
        else:
            _audio_flag_path().unlink(missing_ok=True)
    except OSError as e:
        _log(f"could not persist audio preference ({e})")
    if on:
        _log("audio capture ENABLED" + ("" if recording
                                        else " (applies to the next session)"))
        if recording and path is not None:
            _start_audio(path.with_name(path.stem + "_audio.tmp.wav"),
                         max(0.0, time.time() - started))
        elif recording:
            _log("audio enabled while a session was ending; applies to the next session")
    else:
        _log("audio capture DISABLED" + ("" if recording
                                         else " (applies to the next session)"))
        if recording:
            _stop_audio_capture()
            _log("audio stopped for this session; the partial track is kept for the mux")


# --------------------------- SESSION ----------------------------------
def _start_session() -> bool:
    """Spin up the writer thread for a new session.

    Returns True once the writer is actually open. On failure returns
    False and leaves no session behind (so toggle() can reset cleanly).
    Must be called with _state_lock held.
    """
    global _frames, _session_thread, _session_path, _session_start_t
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
    _session_start_t = time.time()
    _wake.set()
    _log(f"recording started -> {path.name}")
    return True


def _end_session(timeout: float = 15.0) -> None:
    """Signal the writer to finish via sentinel, then wait for it.

    The writer owns its queue and file, so abandoning it after `timeout`
    can't corrupt the next session -- it just finishes its own file late.
    The join happens outside _state_lock (callers release it first) so
    hotkeys stay responsive while the encoder flushes. Audio, if any, is
    stopped first and muxed into the video afterwards.
    """
    global _frames, _session_thread, _session_path, _audio_thread, _audio_stop, _audio_seg
    with _state_lock:
        frames, thread, path = _frames, _session_thread, _session_path
        _frames, _session_thread, _session_path = None, None, None
        _wake.clear()
        audio_thread, audio_stop = _audio_thread, _audio_stop
        _audio_thread, _audio_stop = None, None
    # Stop audio capture before the mux so the WAV is fully written.
    if audio_thread is not None:
        audio_stop.set()
        audio_thread.join(timeout=5.0)
        if audio_thread.is_alive():
            _log("WARNING: audio thread still alive after 5s; abandoning it")
    with _state_lock:
        audio_seg = _audio_seg
        _audio_seg = None
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
    if audio_seg is not None:
        _mux_audio(path, audio_seg[0], audio_seg[1])


def toggle() -> None:
    global _recording
    stopping = False
    started_path: Path | None = None
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
                    started_path = _session_path
            else:
                _recording = False
                stopping = True
    except Exception as e:  # noqa: BLE001 - lock acquisition itself failed
        _log(f"toggle failed: {e}")
        return
    if stopping:
        _end_session()  # join outside the lock; hotkeys stay responsive
    elif started_path is not None and _audio_enabled:
        # Audio requested: capture starts with the session (delay ~0).
        # _start_audio never raises; a failure just logs and stays video-only.
        _start_audio(started_path.with_name(started_path.stem + "_audio.tmp.wav"),
                     max(0.0, time.time() - _session_start_t))


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
    global _audio_enabled
    if _already_running():
        return  # silent: the other copy is already doing the job
    # keep the guard socket alive
    _guard = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _audio_enabled = _audio_flag_path().exists()
    _log(f"ghost-rec started (audio capture "
         f"{'ENABLED' if _audio_enabled else 'disabled'}; "
         f"{HOTKEY_AUDIO} toggles)")

    threading.Thread(target=_grab_supervisor, daemon=True).start()
    keyboard.add_hotkey(HOTKEY_TOGGLE, toggle)
    keyboard.add_hotkey(HOTKEY_AUDIO, toggle_audio)
    keyboard.add_hotkey(HOTKEY_QUIT, quit_app)

    _quit.wait()  # block until the quit hotkey fires
    keyboard.unhook_all()
    _guard.close()
    _log("ghost-rec stopped")


if __name__ == "__main__":
    main()
