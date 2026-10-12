"""Logic tests for check_playback.py (issue #1 follow-up).

Run:  pytest -q

Drift math and ffmpeg-output parsing are hermetic (canned probes / canned
ffmpeg stderr). One end-to-end test writes real MP4s with imageio and runs
main() against them, so the whole log->probe->verdict path is exercised on
both CI runners (imageio + imageio-ffmpeg are ghost-rec dependencies).
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

import check_playback


def entry(file="clip.mp4", duration=60.0):
    return {
        "start": "2026-10-11T12:00:00",
        "stop": "2026-10-11T12:01:00",
        "duration_s": duration,
        "file": file,
        "size_bytes": 1024,
        "audio": False,
    }


def canned_probe(seconds):
    def probe(path):
        return seconds
    return probe


def touch(path: Path):
    path.write_bytes(b"\x00")  # exists, contents never read (probe is canned)


# --- drift math -----------------------------------------------------------

def test_ok_within_tolerance(tmp_path):
    touch(tmp_path / "clip.mp4")
    row = check_playback.check_entry(entry(duration=60.0), tmp_path,
                                     probe=canned_probe(57.0))
    assert row["status"] == "OK"
    assert row["video_s"] == 57.0


def test_sped_up_is_the_issue1_shape(tmp_path):
    # Alex's issue #1: minutes of screen time squeezed into seconds of video.
    touch(tmp_path / "clip.mp4")
    row = check_playback.check_entry(entry(duration=240.0), tmp_path,
                                     probe=canned_probe(6.0))
    assert row["status"] == "SPED_UP"
    assert "2%" in row["note"] or "3%" in row["note"]  # ~2.5% of real time


def test_stretched(tmp_path):
    touch(tmp_path / "clip.mp4")
    row = check_playback.check_entry(entry(duration=5.0), tmp_path,
                                     probe=canned_probe(20.0))
    assert row["status"] == "STRETCHED"


def test_absolute_floor_protects_short_sessions(tmp_path):
    # 10% of a 1s session is 0.1s -- the 2s floor must win instead.
    touch(tmp_path / "clip.mp4")
    row = check_playback.check_entry(entry(duration=1.0), tmp_path,
                                     probe=canned_probe(2.5))
    assert row["status"] == "OK"
    row = check_playback.check_entry(entry(duration=5.0), tmp_path,
                                     probe=canned_probe(1.0))
    assert row["status"] == "SPED_UP"


def test_missing_file(tmp_path):
    row = check_playback.check_entry(entry(file="gone.mp4"), tmp_path,
                                     probe=canned_probe(60.0))
    assert row["status"] == "MISSING"


def test_no_file_is_informational_not_drift(tmp_path):
    row = check_playback.check_entry(entry(file=None), tmp_path,
                                     probe=canned_probe(60.0))
    assert row["status"] == "NO_FILE"


def test_unreadable_video(tmp_path):
    touch(tmp_path / "clip.mp4")
    row = check_playback.check_entry(entry(), tmp_path,
                                     probe=canned_probe(None))
    assert row["status"] == "UNREADABLE"


@pytest.mark.parametrize("duration", [None, 0, -5, "soon", float("nan")])
def test_no_usable_duration(tmp_path, duration):
    touch(tmp_path / "clip.mp4")
    row = check_playback.check_entry(entry(duration=duration), tmp_path,
                                     probe=canned_probe(60.0))
    assert row["status"] == "NO_DURATION"


# --- duration parsing (pure; no mocking needed) -------------------------------

FFMPEG_STDERR = (
    "ffmpeg version 7.0.2 Copyright (c) 2000-2024 the FFmpeg developers\n"
    "  Duration: 00:04:12.50, start: 0.000000, bitrate: 812 kb/s\n"
    "  Stream #0:0(und): Video: h264 (Main) (avc1 / 0x31637661), "
    "yuv420p, 3840x2160, 60 fps, 60 tbr, 15360 tbn\n"
)


def test_parse_duration_ok():
    assert check_playback._parse_duration(FFMPEG_STDERR) == pytest.approx(252.5)


def test_parse_duration_garbage_is_none():
    assert check_playback._parse_duration("not an ffmpeg output\n") is None


def test_parse_duration_na_is_none():
    assert check_playback._parse_duration(
        "  Duration: N/A, start: 0.000000\n") is None


def test_probe_none_when_ffmpeg_unavailable():
    with mock.patch.object(check_playback, "_ffmpeg_stderr",
                           return_value=None):
        assert check_playback.probe_video_duration(Path("clip.mp4")) is None


# --- end to end with real clips --------------------------------------------

def write_clip(path: Path, frames: int, fps: int):
    import imageio.v2 as imageio
    import numpy as np
    writer = imageio.get_writer(str(path), fps=fps, macro_block_size=1)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    for _ in range(frames):
        writer.append_data(frame)
    writer.close()


def write_log(path: Path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")


def test_e2e_real_clips(tmp_path):
    write_clip(tmp_path / "good.mp4", frames=60, fps=60)     # 1.0 s
    write_clip(tmp_path / "long.mp4", frames=600, fps=30)    # 20.0 s
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        entry(file="good.mp4", duration=1.0),     # OK
        entry(file="good.mp4", duration=240.0),   # SPED_UP (issue #1 shape)
        entry(file="long.mp4", duration=1.0),     # STRETCHED
        entry(file="gone.mp4", duration=60.0),    # MISSING
        entry(file=None, duration=60.0),          # NO_FILE
    ])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = check_playback.main(
            ["--log", str(log), "--video-dir", str(tmp_path), "--json"])
    assert code == 1
    payload = json.loads(buf.getvalue())
    by_file = {}
    for row in payload["sessions"]:
        by_file.setdefault(row["file"], []).append(row["status"])
    assert by_file["good.mp4"] == ["OK", "SPED_UP"]
    assert by_file["long.mp4"] == ["STRETCHED"]
    assert by_file["gone.mp4"] == ["MISSING"]
    assert by_file[None] == ["NO_FILE"]
    assert payload["drifted"] == 2


def test_e2e_clean_log_exits_0(tmp_path):
    write_clip(tmp_path / "good.mp4", frames=60, fps=60)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry(file="good.mp4", duration=1.0)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = check_playback.main(
            ["--log", str(log), "--video-dir", str(tmp_path)])
    assert code == 0
    assert "[        OK]" in buf.getvalue()


def test_main_missing_log_exits_0(tmp_path):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = check_playback.main(
            ["--log", str(tmp_path / "nope.jsonl"),
             "--video-dir", str(tmp_path)])
    assert code == 0
    assert "0 session(s) checked" in buf.getvalue()


def test_main_bad_tolerance_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        check_playback.main(["--tolerance", "-1"])
    assert excinfo.value.code == 2
    assert "tolerance" in capsys.readouterr().err


def test_format_text_summary_counts():
    rows = [
        {"file": "a.mp4", "duration_s": 60.0, "video_s": 60.0,
         "status": "OK", "note": ""},
        {"file": "b.mp4", "duration_s": 60.0, "video_s": 2.0,
         "status": "SPED_UP",
         "note": "video is 2.0s for 60.0s of capture "
                 "(3% of real time) -- sped-up playback"},
    ]
    text = check_playback.format_text(rows, Path("sessions.jsonl"), skipped=1)
    assert "2 session(s) checked: 1 drifted, 1 ok" in text
    assert "skipped 1 unreadable log line(s)" in text
    assert "sped-up playback" in text


# --- --from/--to date-range filtering -------------------------------------

from datetime import date


def dated_entry(day, file, duration=60.0):
    return {
        "start": f"{day}T12:00:00",
        "stop": f"{day}T12:01:00",
        "duration_s": duration,
        "file": file,
        "size_bytes": 1024,
        "audio": False,
    }


def write_log_with(tmp_path, rows):
    log = tmp_path / "sessions.jsonl"
    write_log(log, rows)
    return log


def test_check_log_date_range_filters(tmp_path):
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        touch(tmp_path / name)
    log = write_log_with(tmp_path, [
        dated_entry("2026-10-10", "a.mp4"),
        dated_entry("2026-10-11", "b.mp4"),
        dated_entry("2026-10-12", "c.mp4"),
    ])
    results, skipped, out_of_range, undated = check_playback.check_log(
        log, tmp_path, probe=canned_probe(60.0),
        date_from=date(2026, 10, 11), date_to=date(2026, 10, 11))
    assert [r["file"] for r in results] == ["b.mp4"]
    assert skipped == 0
    assert out_of_range == 2
    assert undated == 0


def test_check_log_date_range_one_sided(tmp_path):
    for name in ("a.mp4", "b.mp4"):
        touch(tmp_path / name)
    log = write_log_with(tmp_path, [
        dated_entry("2026-10-10", "a.mp4"),
        dated_entry("2026-10-12", "b.mp4"),
    ])
    results, skipped, out_of_range, undated = check_playback.check_log(
        log, tmp_path, probe=canned_probe(60.0),
        date_from=date(2026, 10, 11))
    assert [r["file"] for r in results] == ["b.mp4"]
    assert out_of_range == 1
    assert undated == 0


def test_check_log_date_range_counts_undated(tmp_path):
    touch(tmp_path / "a.mp4")
    undated = dict(dated_entry("2026-10-12", "a.mp4"))
    del undated["start"]  # cannot be placed in the range
    log = write_log_with(tmp_path, [undated])
    results, skipped, out_of_range, undated_excluded = (
        check_playback.check_log(log, tmp_path, probe=canned_probe(60.0),
                                 date_from=date(2026, 10, 12),
                                 date_to=date(2026, 10, 12)))
    assert results == []
    assert out_of_range == 0
    assert undated_excluded == 1


def test_check_log_no_range_is_passthrough(tmp_path):
    undated = {"file": "a.mp4", "duration_s": 60.0}  # no start key at all
    touch(tmp_path / "a.mp4")
    log = write_log_with(tmp_path, [dated_entry("2026-10-10", "a.mp4"),
                                    undated])
    results, skipped, out_of_range, undated_excluded = (
        check_playback.check_log(log, tmp_path, probe=canned_probe(60.0)))
    assert len(results) == 2  # nothing filtered
    assert out_of_range == 0
    assert undated_excluded == 0


def test_format_text_shows_range_and_exclusions():
    rows = [{"file": "b.mp4", "duration_s": 60.0, "video_s": 60.0,
             "status": "OK", "note": ""}]
    text = check_playback.format_text(
        rows, Path("sessions.jsonl"), skipped=0,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 12),
        out_of_range=2, undated_excluded=1)
    assert "range: 2026-10-12 -> 2026-10-12" in text
    assert "excluded 2 session(s) outside the range" in text
    assert "excluded 1 session(s) with no usable date" in text


def test_main_from_to_e2e(tmp_path, capsys):
    # Files are absent on purpose: MISSING needs no real ffmpeg probing,
    # so this stays hermetic on both CI runners.
    undated = {"file": "c.mp4", "duration_s": 60.0}
    log = write_log_with(tmp_path, [
        dated_entry("2026-10-10", "a.mp4"),
        dated_entry("2026-10-12", "b.mp4"),
        undated,
    ])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = check_playback.main(
            ["--log", str(log), "--video-dir", str(tmp_path),
             "--from", "2026-10-12", "--to", "2026-10-12"])
    assert code == 0  # MISSING is informational, not drift
    out = buf.getvalue()
    assert "range: 2026-10-12 -> 2026-10-12" in out
    assert "excluded 1 session(s) outside the range" in out
    assert "excluded 1 session(s) with no usable date" in out
    assert "b.mp4" in out and "a.mp4" not in out and "c.mp4" not in out
    assert "1 session(s) checked: 0 drifted, 0 ok" in out


def test_main_from_to_json_carries_bounds(tmp_path):
    log = write_log_with(tmp_path, [dated_entry("2026-10-10", "a.mp4")])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = check_playback.main(
            ["--log", str(log), "--video-dir", str(tmp_path),
             "--from", "2026-10-01", "--json"])
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload["date_from"] == "2026-10-01"
    assert payload["date_to"] is None
    assert payload["out_of_range"] == 0
    assert payload["undated_excluded"] == 0
    # in range: checked (file absent -> MISSING, informational, no drift)
    assert len(payload["sessions"]) == 1
    assert payload["sessions"][0]["status"] == "MISSING"


def test_main_garbage_from_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        check_playback.main(["--from", "not-a-date"])
    assert excinfo.value.code == 2
    assert "bad date" in capsys.readouterr().err


def test_main_from_after_to_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        check_playback.main(["--from", "2026-10-12", "--to", "2026-10-10"])
    assert excinfo.value.code == 2
    assert "--from" in capsys.readouterr().err
