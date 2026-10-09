"""Logic tests for ghost_rec.py (session lifecycle + audio fallback).

Run:  pytest -q        (deps: pip install -r requirements.txt pytest)

The stubs from conftest.py stand in for mss/keyboard/sounddevice, so these
run headless on Linux CI. They exercise the real encoder (imageio-ffmpeg)
and the real session/mux/toggle logic; Windows-only paths (hotkeys,
WASAPI, pythonw) remain Alex-machine territory per docs/.
"""

import json
import wave
from datetime import datetime
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
import pytest

import ghost_rec


@pytest.fixture()
def outdir(tmp_path, monkeypatch):
    """Point the recorder's output at a fresh temp dir per test."""
    monkeypatch.setattr(ghost_rec, "OUTPUT_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def clean_state(monkeypatch):
    """Reset module-level session/audio state around each test."""
    for name in ("_recording", "_audio_enabled"):
        monkeypatch.setattr(ghost_rec, name, False)
    monkeypatch.setattr(ghost_rec, "_session_meta", None)
    ghost_rec._quit.clear()
    yield
    ghost_rec._quit.clear()


def _frame(h=24, w=32):
    return np.zeros((h, w, 3), dtype=np.uint8)


def _decode_frame_count(path: Path) -> int:
    with imageio.get_reader(str(path)) as reader:
        return sum(1 for _ in reader)


# --- import / defaults --------------------------------------------------------


def test_import_defaults():
    assert ghost_rec.FPS == 60
    assert ghost_rec._audio_enabled is False
    assert ghost_rec._recording is False
    assert ghost_rec._SENTINEL is ghost_rec._SENTINEL  # identity-compared in _write_loop


def test_already_running_false_when_port_free():
    # 53917 is free on CI; the guard socket is released immediately.
    assert ghost_rec._already_running() is False


def test_log_writes(outdir):
    ghost_rec._log("hello-test")
    assert (outdir / "ghost.log").read_text(encoding="utf-8").endswith("hello-test\n")


# --- session lifecycle ----------------------------------------------------------


def test_toggle_cycle_produces_valid_mp4(outdir, clean_state):
    ghost_rec.toggle()  # start
    assert ghost_rec._recording is True
    assert ghost_rec._frames is not None
    for _ in range(10):
        ghost_rec._frames.put_nowait(_frame())
    path = ghost_rec._session_path
    ghost_rec.toggle()  # stop
    assert ghost_rec._recording is False
    assert path is not None and path.suffix == ".mp4"
    assert path.stat().st_size > 0
    assert _decode_frame_count(path) >= 5  # encoder may merge, never invent
    assert path.name in (p.name for p in outdir.glob("ghost_*.mp4"))


def test_filename_collision_bumps_to_2(outdir, clean_state):
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    (outdir / f"ghost_{ts}.mp4").touch()  # squat the plain name this second
    with ghost_rec._state_lock:
        assert ghost_rec._start_session() is True
        path = ghost_rec._session_path
    assert path.name.endswith("_2.mp4"), path.name
    ghost_rec.toggle()  # stop via the public path (drains + finalizes)


def test_stop_with_empty_queue_leaves_no_file_but_resets(outdir, clean_state):
    ghost_rec.toggle()  # start, push nothing
    path = ghost_rec._session_path
    ghost_rec.toggle()  # stop: imageio opens the file lazily on first frame
    assert ghost_rec._recording is False
    assert ghost_rec._session_path is None
    assert not path.exists()  # zero frames -> no file, no crash
    ghost_rec.toggle()  # next session starts cleanly
    assert ghost_rec._recording is True
    ghost_rec.toggle()


def test_quit_app_sets_quit_flag(clean_state):
    assert ghost_rec._quit.is_set() is False
    ghost_rec.quit_app()  # not recording: just signals
    assert ghost_rec._quit.is_set() is True


# --- audio preference -----------------------------------------------------------


def test_audio_toggle_persists(outdir, clean_state):
    flag = outdir / "audio.enabled"
    ghost_rec.toggle_audio()
    assert ghost_rec._audio_enabled is True
    assert flag.exists()
    ghost_rec.toggle_audio()
    assert ghost_rec._audio_enabled is False
    assert not flag.exists()


def test_start_audio_without_wasapi_fails_gracefully(outdir, clean_state):
    # sounddevice stub has no WasapiSettings -> "not this platform" path.
    wav = outdir / "seg.wav"
    assert ghost_rec._start_audio(wav, 0.0) is False
    assert ghost_rec._audio_thread is None
    assert not wav.exists()


def test_toggle_with_audio_enabled_stays_video_only(outdir, clean_state):
    ghost_rec.toggle_audio()  # enable preference (no real device on CI)
    ghost_rec.toggle()  # start session
    for _ in range(6):
        ghost_rec._frames.put_nowait(_frame())
    path = ghost_rec._session_path
    ghost_rec.toggle()  # stop; _start_audio already no-op'd, nothing to mux
    assert ghost_rec._audio_seg is None
    assert path.stat().st_size > 0
    assert list(outdir.glob("*_audio.wav")) == []  # no sidecar left behind


# --- mux edge cases ---------------------------------------------------------------


def _write_wav(path: Path, seconds: float, samplerate=48000):
    frames = int(samplerate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(b"\x00\x00" * 2 * frames)


def test_mux_discards_too_short_wav(outdir):
    video = outdir / "v.mp4"
    video.write_bytes(b"fake-video")
    wav = outdir / "seg.wav"
    _write_wav(wav, 0.01)  # ~1KB < 4096 threshold
    ghost_rec._mux_audio(video, wav, 0.0)
    assert not wav.exists()
    assert video.read_bytes() == b"fake-video"  # untouched


def test_mux_failure_keeps_video_and_sidecar(outdir):
    video = outdir / "v.mp4"
    video.write_bytes(b"fake-video")
    wav = outdir / "seg.wav"
    _write_wav(wav, 1.0)  # big enough to attempt, not real audio -> ffmpeg fails
    with open(wav, "r+b") as f:
        f.seek(44)
        f.write(b"not-real-audio-data" * 300)
    ghost_rec._mux_audio(video, wav, 0.0)
    sidecar = outdir / "v_audio.wav"
    assert sidecar.exists()  # audio must never cost the video
    assert video.read_bytes() == b"fake-video"


# --- sessions.jsonl -------------------------------------------------------------


def _read_session_log(outdir):
    raw = (outdir / "sessions.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines()]


def test_session_log_records_completed_session(outdir, clean_state):
    ghost_rec.toggle()  # start
    for _ in range(10):
        ghost_rec._frames.put_nowait(_frame())
    path = ghost_rec._session_path
    ghost_rec.toggle()  # stop
    (e,) = _read_session_log(outdir)
    assert e["file"] == path.name
    assert e["size_bytes"] == path.stat().st_size
    assert e["size_bytes"] > 0
    assert e["audio"] is False
    assert e["duration_s"] >= 0
    # Local ISO-8601 timestamps that round-trip and order correctly.
    assert datetime.fromisoformat(e["start"]) <= datetime.fromisoformat(e["stop"])
    assert set(e) == {"start", "stop", "duration_s", "file", "size_bytes", "audio"}


def test_session_log_zero_frame_session(outdir, clean_state):
    ghost_rec.toggle()  # start, push nothing
    path = ghost_rec._session_path
    ghost_rec.toggle()  # stop: no file, but the session is still logged
    (e,) = _read_session_log(outdir)
    assert e["file"] == path.name
    assert e["size_bytes"] == 0
    assert e["audio"] is False


def test_session_log_audio_true_when_track_muxed(outdir, clean_state):
    ghost_rec.toggle()  # start
    for _ in range(10):
        ghost_rec._frames.put_nowait(_frame())
    path = ghost_rec._session_path
    wav = outdir / "seg.wav"
    _write_wav(wav, 1.0)
    ghost_rec._audio_seg = (wav, 0.0)  # pretend a session's audio segment exists
    ghost_rec.toggle()  # stop -> mux runs on real ffmpeg
    (e,) = _read_session_log(outdir)
    assert e["audio"] is True
    assert not wav.exists()  # consumed by the mux
    assert list(outdir.glob("*_audio.wav")) == []  # no sidecar: the mux worked


def test_session_log_failed_start_writes_nothing(outdir, clean_state, monkeypatch):
    # Writer never opens -> _start_session returns False -> no session, no entry.
    monkeypatch.setattr(ghost_rec, "_start_session", lambda: False)
    ghost_rec.toggle()
    assert ghost_rec._recording is False
    assert not (outdir / "sessions.jsonl").exists()


def test_session_log_append_is_json_lines(outdir, clean_state):
    ghost_rec._session_log_append({"a": 1})
    ghost_rec._session_log_append({"b": 2})
    raw = (outdir / "sessions.jsonl").read_text(encoding="utf-8")
    assert raw.count("\n") == 2
    assert [json.loads(line) for line in raw.splitlines()] == [{"a": 1}, {"b": 2}]


def test_session_log_rotation(outdir, clean_state, monkeypatch):
    monkeypatch.setattr(ghost_rec, "SESSION_LOG_MAX_LINES", 3)
    for i in range(5):
        ghost_rec._session_log_append({"n": i})
    entries = _read_session_log(outdir)
    assert [e["n"] for e in entries] == [2, 3, 4]  # oldest dropped, order kept
    assert list(outdir.glob("*.tmp")) == []  # atomic rewrite left no temp file
