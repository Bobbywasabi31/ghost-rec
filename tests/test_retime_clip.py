"""Logic tests for retime_clip.py (issue #1 repair tool).

Run:  pytest -q

Factor math, log lookup, overwrite/insanity guards, and dry-run are
hermetic (canned probes / canned ffmpeg runs). Two end-to-end tests write
real MP4s with imageio and run the real bundled ffmpeg, emulating the
issue-#1 shape: a short video-only clip re-timed to a longer wall-clock
duration, plus a clip with a short audio track to prove `-c:a copy` is
harmless in both cases.
"""

import io
import csv
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

import retime_clip


def canned_probe(seconds):
    def probe(path):
        return seconds
    return probe


def fake_run_ok():
    proc = mock.Mock()
    proc.returncode = 0
    proc.stderr = ""
    return proc


def make_run(calls):
    def run(cmd, **kwargs):
        calls.append(cmd)
        return fake_run_ok()
    return run


def touch(path: Path):
    path.write_bytes(b"\x00")  # exists; probe/run are canned


def write_log(path: Path, entries):
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )


def entry(file="clip.mp4", duration=60.0):
    return {
        "start": "2026-10-11T12:00:00",
        "stop": "2026-10-11T12:01:00",
        "duration_s": duration,
        "file": file,
        "size_bytes": 1024,
        "audio": False,
    }


# --- factor math -----------------------------------------------------------

def test_retime_factor_is_target_over_encoded():
    assert retime_clip.retime_factor(6.0, 240.0) == pytest.approx(40.0)
    assert retime_clip.retime_factor(120.0, 60.0) == pytest.approx(0.5)


def test_already_realtime_uses_check_playback_tolerance():
    assert retime_clip.already_realtime(57.0, 60.0)      # 5% < 10%
    assert retime_clip.already_realtime(6.0, 240.0) is False
    assert retime_clip.already_realtime(1.5, 1.0)        # 2s floor wins
    assert retime_clip.already_realtime(4.0, 1.0) is False


def test_default_output_never_equals_input(tmp_path):
    clip = tmp_path / "clip.mp4"
    out = retime_clip.resolve_output(clip, None)
    assert out == tmp_path / "clip.retimed.mp4"
    assert out.resolve() != clip.resolve()


# --- log lookup ------------------------------------------------------------

def test_lookup_duration_last_match_wins(tmp_path):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry(duration=60.0), entry(duration=120.0)])
    assert retime_clip.lookup_duration("clip.mp4", log) == 120.0


def test_lookup_duration_skips_garbage(tmp_path):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry(duration=60.0), entry(duration="bogus")])
    assert retime_clip.lookup_duration("clip.mp4", log) is None
    write_log(log, [entry(duration=0.0)])
    assert retime_clip.lookup_duration("clip.mp4", log) is None
    write_log(log, [entry(duration=-5.0)])
    assert retime_clip.lookup_duration("clip.mp4", log) is None


def test_lookup_duration_missing_log_or_entry(tmp_path):
    assert retime_clip.lookup_duration("clip.mp4", tmp_path / "nope.jsonl") is None
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry(file="other.mp4")])
    assert retime_clip.lookup_duration("clip.mp4", log) is None


# --- guards ----------------------------------------------------------------

def test_noop_when_already_realtime(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    code, msg = retime_clip.retime(
        clip, 60.0, tmp_path / "out.mp4",
        probe=canned_probe(58.0), run=make_run(calls),
    )
    assert code == 0
    assert "nothing to do" in msg
    assert calls == []  # ffmpeg never ran


def test_refuses_overwrite_without_force(tmp_path):
    clip = tmp_path / "clip.mp4"
    out = tmp_path / "out.mp4"
    touch(clip)
    touch(out)
    calls = []
    code, msg = retime_clip.retime(
        clip, 600.0, out, probe=canned_probe(6.0), run=make_run(calls)
    )
    assert code == 2
    assert "--force" in msg
    assert calls == []


def test_force_allows_overwrite(tmp_path):
    clip = tmp_path / "clip.mp4"
    out = tmp_path / "out.mp4"
    touch(clip)
    touch(out)
    calls = []
    code, _msg = retime_clip.retime(
        clip, 600.0, out, force=True,
        probe=canned_probe(6.0), run=make_run(calls),
    )
    assert code == 0
    assert len(calls) == 1
    assert "setpts=100.000000*PTS" in " ".join(calls[0])


def test_refuses_to_write_over_input_even_with_force(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    code, _msg = retime_clip.retime(
        clip, 600.0, clip, force=True,
        probe=canned_probe(6.0), run=make_run(calls),
    )
    assert code == 2
    assert calls == []


def test_insane_factor_requires_force(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    code, msg = retime_clip.retime(
        clip, 10000.0, tmp_path / "out.mp4",
        probe=canned_probe(1.0), run=make_run(calls),
    )
    assert code == 2
    assert "insane" in msg
    assert calls == []
    code, _msg = retime_clip.retime(
        clip, 10000.0, tmp_path / "out.mp4", force=True,
        probe=canned_probe(1.0), run=make_run(calls),
    )
    assert code == 0
    assert len(calls) == 1


def test_dry_run_prints_command_without_running(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    code, msg = retime_clip.retime(
        clip, 240.0, tmp_path / "out.mp4", dry_run=True,
        probe=canned_probe(6.0), run=make_run(calls),
    )
    assert code == 0
    assert "setpts=40.000000*PTS" in msg
    assert "-c:a" in msg and "copy" in msg
    assert calls == []


def test_unprobable_clip_is_failure(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    code, _msg = retime_clip.retime(
        clip, 60.0, tmp_path / "out.mp4",
        probe=canned_probe(None), run=make_run(calls),
    )
    assert code == 1
    assert calls == []


def test_ffmpeg_failure_surfaces_stderr_tail(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    proc = mock.Mock()
    proc.returncode = 1
    proc.stderr = "line1\nline2\nline3\nline4\nline5\nline6\nboom\n"
    code, msg = retime_clip.retime(
        clip, 600.0, tmp_path / "out.mp4",
        probe=canned_probe(6.0), run=lambda cmd, **k: proc,
    )
    assert code == 1
    assert "boom" in msg
    assert "line1" not in msg  # only the last 5 lines are kept


# --- main() wiring ---------------------------------------------------------

def test_main_resolves_duration_from_log(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry(file="clip.mp4", duration=60.0)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = retime_clip.main(
            [str(clip), "--log", str(log), "--dry-run"],
            probe=canned_probe(6.0),
        )
    assert code == 0
    assert "setpts=10.000000*PTS" in buf.getvalue()


def test_main_explicit_duration_wins_over_log(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry(file="clip.mp4", duration=60.0)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = retime_clip.main(
            [str(clip), "--log", str(log), "--duration", "600", "--dry-run"],
            probe=canned_probe(6.0),
        )
    assert code == 0
    assert "setpts=100.000000*PTS" in buf.getvalue()


def test_main_no_log_entry_exits_2(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [])
    with pytest.raises(SystemExit) as exc:
        retime_clip.main([str(clip), "--log", str(log), "--dry-run"])
    assert exc.value.code == 2


def test_main_bad_duration_exits_2(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    with pytest.raises(SystemExit) as exc:
        retime_clip.main([str(clip), "--duration", "-5"])
    assert exc.value.code == 2


def test_main_missing_clip_exits_2(tmp_path):
    with pytest.raises(SystemExit) as exc:
        retime_clip.main([str(tmp_path / "nope.mp4")])
    assert exc.value.code == 2


# --- --json output ---------------------------------------------------------

def _run_json(tmp_path, argv, **main_kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = retime_clip.main(argv, **main_kwargs)
    return code, json.loads(buf.getvalue())


def test_json_success_carries_fields(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    out = tmp_path / "fixed.mp4"
    calls = []
    code, payload = _run_json(
        tmp_path,
        [str(clip), "--duration", "600", "--output", str(out), "--json"],
        probe=canned_probe(6.0), run=make_run(calls),
    )
    assert code == 0
    assert payload == {
        "clip": str(clip),
        "output": str(out),
        "target_s": 600.0,
        "dry_run": False,
        "exit_code": 0,
        "encoded_s": 6.0,
        "stretch_factor": pytest.approx(100.0),
        "message": payload["message"],
    }
    assert len(calls) == 1


def test_json_dry_run_marks_dry_run_and_factor(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    code, payload = _run_json(
        tmp_path,
        [str(clip), "--duration", "240", "--json", "--dry-run"],
        probe=canned_probe(6.0),
    )
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["encoded_s"] == 6.0
    assert payload["stretch_factor"] == pytest.approx(40.0)
    assert payload["exit_code"] == 0
    assert "setpts=40.000000*PTS" in payload["message"]


def test_json_already_realtime_has_null_factor(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    code, payload = _run_json(
        tmp_path,
        [str(clip), "--duration", "60", "--json"],
        probe=canned_probe(58.0),
    )
    assert code == 0
    assert payload["exit_code"] == 0
    assert payload["encoded_s"] == 58.0
    assert payload["stretch_factor"] is None
    assert "nothing to do" in payload["message"]


def test_json_failure_payload_is_still_parseable(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    code, payload = _run_json(
        tmp_path,
        [str(clip), "--duration", "60", "--json"],
        probe=canned_probe(None),
    )
    assert code == 1
    assert payload["exit_code"] == 1
    assert payload["encoded_s"] is None
    assert payload["stretch_factor"] is None
    assert payload["message"]


def test_json_insane_factor_guard_payload(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    code, payload = _run_json(
        tmp_path,
        [str(clip), "--duration", "10000", "--json"],
        probe=canned_probe(1.0), run=make_run(calls),
    )
    assert code == 2
    assert payload["exit_code"] == 2
    assert payload["encoded_s"] == 1.0
    assert "insane" in payload["message"]
    assert calls == []


def test_retime_without_details_keeps_two_tuple_contract(tmp_path):
    # retime_all unpacks (code, message); the details out-param must stay
    # opt-in only.
    clip = tmp_path / "clip.mp4"
    touch(clip)
    result = retime_clip.retime(
        clip, 240.0, tmp_path / "out.mp4", dry_run=True,
        probe=canned_probe(6.0),
    )
    assert isinstance(result, tuple) and len(result) == 2


def test_json_stdout_is_pure_json(tmp_path, capsys):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    code = retime_clip.main(
        [str(clip), "--duration", "60", "--json"], probe=canned_probe(58.0)
    )
    assert code == 0
    out, err = capsys.readouterr()
    json.loads(out)  # raises if anything non-JSON reached stdout
    assert err == ""


# --- --csv -----------------------------------------------------------------

def _run_csv(tmp_path, argv, **main_kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = retime_clip.main(argv, **main_kwargs)
    rows = list(csv.reader(io.StringIO(buf.getvalue())))
    return code, rows


def test_csv_success_carries_fields(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    out = tmp_path / "fixed.mp4"
    calls = []
    code, rows = _run_csv(
        tmp_path,
        [str(clip), "--duration", "600", "--output", str(out), "--csv"],
        probe=canned_probe(6.0), run=make_run(calls),
    )
    assert code == 0
    assert rows[0] == ["clip", "output", "exit_code", "target_s", "encoded_s",
                       "stretch_factor", "dry_run", "message"]
    header, row = rows
    assert row[:7] == [str(clip), str(out), "0", "600.0", "6.0", "100.000",
                       "false"]
    assert row[7]  # the human-readable success message rides along
    assert len(calls) == 1


def test_csv_dry_run_marks_dry_run_and_factor(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    code, rows = _run_csv(
        tmp_path,
        [str(clip), "--duration", "240", "--csv", "--dry-run"],
        probe=canned_probe(6.0),
    )
    assert code == 0
    row = rows[1]
    assert row[5] == "40.000"
    assert row[6] == "true"
    assert "setpts=40.000000*PTS" in row[7]


def test_csv_already_realtime_has_empty_factor(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    code, rows = _run_csv(
        tmp_path,
        [str(clip), "--duration", "60", "--csv"],
        probe=canned_probe(58.0),
    )
    assert code == 0
    row = rows[1]
    assert row[2] == "0"
    assert row[4] == "58.0"
    assert row[5] == ""  # no stretch applied
    assert "nothing to do" in row[7]


def test_csv_failure_payload_is_still_parseable(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    code, rows = _run_csv(
        tmp_path,
        [str(clip), "--duration", "60", "--csv"],
        probe=canned_probe(None),
    )
    assert code == 1
    row = rows[1]
    assert row[2] == "1"
    assert row[4] == "" and row[5] == ""
    assert row[7]


def test_csv_json_conflict_exits_2(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    with pytest.raises(SystemExit) as exc:
        retime_clip.main([str(clip), "--duration", "60", "--json", "--csv"],
                         probe=canned_probe(6.0))
    assert exc.value.code == 2


def test_csv_stdout_is_pure_csv(tmp_path, capsys):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    code = retime_clip.main(
        [str(clip), "--duration", "60", "--csv"], probe=canned_probe(58.0)
    )
    assert code == 0
    out, err = capsys.readouterr()
    rows = list(csv.reader(io.StringIO(out)))
    assert len(rows) == 2  # header + exactly one data row
    assert err == ""


def test_format_csv_quotes_commas_in_message():
    payload = {
        "clip": "a.mp4",
        "output": "a.retimed.mp4",
        "target_s": 60.0,
        "dry_run": False,
        "exit_code": 1,
        "encoded_s": None,
        "stretch_factor": None,
        "message": 'hello, "world"',
    }
    rows = list(csv.reader(io.StringIO(retime_clip.format_csv(payload))))
    assert rows[1][7] == 'hello, "world"'


# --- end to end (real bundled ffmpeg) --------------------------------------

def _write_issue1_shape_clip(path: Path, with_audio: bool):
    """Emulate the issue #1 artifact: 0.5s of video for 5s of real time.

    30 frames at 60fps = 0.5s of encoded video. With audio, a 0.5s silent
    track is muxed in (also short -- the exact pre-fix shape where the
    probe sees a sped-up container).
    """
    import numpy as np
    import imageio.v2 as imageio

    frames = [(np.zeros((32, 32, 3), np.uint8) + i) for i in range(30)]
    imageio.mimsave(str(path), frames, fps=60, codec="libx264", quality=8)
    if with_audio:
        from imageio_ffmpeg import get_ffmpeg_exe
        exe = get_ffmpeg_exe()
        with_audio_path = path.with_name(path.stem + "_av.mp4")
        subprocess.run(
            [exe, "-y", "-i", str(path),
             "-f", "lavfi", "-i", "anullsrc=d=0.5:r=44100:cl=stereo",
             "-c:v", "copy", "-c:a", "aac", str(with_audio_path)],
            capture_output=True, check=True,
        )
        return with_audio_path
    return path


def test_e2e_video_only_clip_retimed_to_real_time(tmp_path):
    clip = _write_issue1_shape_clip(tmp_path / "clip.mp4", with_audio=False)
    before = retime_clip.probe_video_duration(clip)
    assert before == pytest.approx(0.5, abs=0.2)
    out = tmp_path / "fixed.mp4"
    code = retime_clip.main([str(clip), "--duration", "5", "--output", str(out)])
    assert code == 0
    assert out.is_file()
    after = retime_clip.probe_video_duration(out)
    assert after == pytest.approx(5.0, abs=2.0)  # same floor check_playback uses
    # The input is never modified.
    assert retime_clip.probe_video_duration(clip) == pytest.approx(0.5, abs=0.2)


def test_e2e_audio_track_survives_untouched(tmp_path):
    clip = _write_issue1_shape_clip(tmp_path / "clip.mp4", with_audio=True)
    out = tmp_path / "fixed.mp4"
    code = retime_clip.main([str(clip), "--duration", "5", "--output", str(out)])
    assert code == 0
    from check_playback import _ffmpeg_stderr
    stderr = _ffmpeg_stderr(out)
    assert stderr is not None and "Audio:" in stderr  # track still present
    assert retime_clip.probe_video_duration(out) == pytest.approx(5.0, abs=2.0)
