"""Logic tests for retime_all.py (issue #1 batch repair tool).

Run:  pytest -q

Everything is hermetic: canned probes stand in for ffmpeg duration
probing, canned runs stand in for ffmpeg itself, so no real video files
are needed. The encode path itself is covered by test_retime_clip.py's
end-to-end tests; these cover the batch glue -- SPED_UP selection,
per-clip outcomes, existing-output skips, --force, --dry-run, batch
continuation after a failure, and the CLI exit codes.
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

import retime_all


def canned_probe(durations):
    """Probe returning a per-filename duration (None = unprobable)."""
    def probe(path):
        return durations.get(Path(path).name)
    return probe


def fake_run_ok():
    proc = mock.Mock()
    proc.returncode = 0
    proc.stderr = ""
    return proc


def make_run(calls, fail_on=()):
    def run(cmd, **kwargs):
        calls.append(cmd)
        proc = fake_run_ok()
        for name in fail_on:
            # scan the whole command: cmd[-1] is <name>.retimed.mp4,
            # the input clip name appears in the -i argument
            if any(name in str(part) for part in cmd):
                proc = mock.Mock()
                proc.returncode = 1
                proc.stderr = "mock ffmpeg boom"
        return proc
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


def run_main(argv, probe, run):
    out = io.StringIO()
    with redirect_stdout(out):
        code = retime_all.main(argv, probe=probe, run=run)
    return code, out.getvalue()


# --- SPED_UP selection ------------------------------------------------------

def test_find_sped_up_filters_only_sped_up(tmp_path):
    clip, ok, gone = tmp_path / "clip.mp4", tmp_path / "ok.mp4", "gone.mp4"
    touch(clip)
    touch(ok)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0),
                    entry("ok.mp4", 60.0),
                    entry("gone.mp4", 60.0)])
    probe = canned_probe({"clip.mp4": 6.0, "ok.mp4": 59.0})
    results, skipped = retime_all.find_sped_up(log, tmp_path, probe=probe)
    assert skipped == 0
    assert [r["file"] for r in results] == ["clip.mp4"]
    assert results[0]["status"] == "SPED_UP"


def test_find_sped_up_counts_bad_log_lines(tmp_path):
    log = tmp_path / "sessions.jsonl"
    log.write_text('{"file": "clip.mp4", "duration_s": 60.0}\nnot json\n',
                   encoding="utf-8")
    results, skipped = retime_all.find_sped_up(
        log, tmp_path, probe=canned_probe({}))
    assert skipped == 1
    assert results == []  # clip.mp4 never probed: file missing -> MISSING


def test_find_sped_up_leaves_stretched_clips_alone(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    results, _ = retime_all.find_sped_up(
        log, tmp_path, probe=canned_probe({"clip.mp4": 120.0}))
    assert results == []  # STRETCHED is a different symptom; not ours


def test_find_sped_up_missing_log_is_empty(tmp_path):
    results, skipped = retime_all.find_sped_up(
        tmp_path / "nope.jsonl", tmp_path, probe=canned_probe({}))
    assert results == []
    assert skipped == 0


# --- batch repair -----------------------------------------------------------

def _sped_up_result(file="clip.mp4", duration=60.0):
    return {"file": file, "duration_s": duration, "video_s": 6.0,
            "status": "SPED_UP", "note": ""}


def test_repair_all_writes_retimed_output(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    outcomes = retime_all.repair_all(
        [_sped_up_result()], tmp_path,
        probe=canned_probe({"clip.mp4": 6.0}), run=make_run(calls))
    assert len(outcomes) == 1
    (outcome,) = outcomes
    assert outcome["code"] == 0
    assert outcome["file"] == "clip.mp4"
    assert outcome["output"] == str(tmp_path / "clip.retimed.mp4")
    assert len(calls) == 1  # ffmpeg ran once
    assert "10.000000*PTS" in " ".join(calls[0])  # 60/6 = 10x stretch


def test_repair_all_factor_math(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    retime_all.repair_all(
        [_sped_up_result(duration=240.0)], tmp_path,
        probe=canned_probe({"clip.mp4": 6.0}), run=make_run(calls))
    assert "40.000000*PTS" in " ".join(calls[0])  # 240/6 = 40x


def test_repair_all_skips_existing_output_without_force(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    touch(tmp_path / "clip.retimed.mp4")  # already repaired before
    calls = []
    outcomes = retime_all.repair_all(
        [_sped_up_result()], tmp_path,
        probe=canned_probe({"clip.mp4": 6.0}), run=make_run(calls))
    assert outcomes[0]["code"] == 2
    assert "already exists" in outcomes[0]["message"]
    assert calls == []


def test_repair_all_force_overwrites_existing(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    touch(tmp_path / "clip.retimed.mp4")
    calls = []
    outcomes = retime_all.repair_all(
        [_sped_up_result()], tmp_path, force=True,
        probe=canned_probe({"clip.mp4": 6.0}), run=make_run(calls))
    assert outcomes[0]["code"] == 0
    assert len(calls) == 1


def test_repair_all_dry_run_calls_nothing(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    calls = []
    outcomes = retime_all.repair_all(
        [_sped_up_result()], tmp_path, dry_run=True,
        probe=canned_probe({"clip.mp4": 6.0}), run=make_run(calls))
    assert outcomes[0]["code"] == 0
    assert "setpts" in outcomes[0]["message"]  # the would-be ffmpeg command
    assert calls == []


def test_repair_all_continues_after_failure(tmp_path):
    good, bad = tmp_path / "good.mp4", tmp_path / "bad.mp4"
    touch(good)
    touch(bad)
    calls = []
    outcomes = retime_all.repair_all(
        [_sped_up_result("good.mp4"), _sped_up_result("bad.mp4")], tmp_path,
        probe=canned_probe({"good.mp4": 6.0, "bad.mp4": 6.0}),
        run=make_run(calls, fail_on=("bad.mp4",)))
    assert [o["code"] for o in outcomes] == [0, 1]
    assert "ffmpeg failed" in outcomes[1]["message"]
    assert len(calls) == 2  # both clips attempted


def test_repair_all_unexpected_probe_error_is_recorded(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)

    def bad_probe(path):
        raise RuntimeError("probe exploded")

    outcomes = retime_all.repair_all(
        [_sped_up_result()], tmp_path, probe=bad_probe, run=make_run([]))
    assert outcomes[0]["code"] == 1
    assert "unexpected error" in outcomes[0]["message"]


def test_repair_all_empty_results(tmp_path):
    assert retime_all.repair_all([], tmp_path) == []


def test_repair_all_honors_batch_tolerance(tmp_path):
    # 55s video vs 60s logged: inside the default 10% gate -> no-op, but a
    # stricter batch tolerance (the one that flagged it SPED_UP) repairs it.
    clip = tmp_path / "clip.mp4"
    touch(clip)
    result = {"file": "clip.mp4", "duration_s": 60.0, "video_s": 55.0,
              "status": "SPED_UP", "note": ""}
    calls = []
    outcomes = retime_all.repair_all(
        [result], tmp_path,
        probe=canned_probe({"clip.mp4": 55.0}), run=make_run(calls))
    assert outcomes[0]["code"] == 0 and calls == []
    assert "already plays at real time" in outcomes[0]["message"]
    calls = []
    outcomes = retime_all.repair_all(
        [result], tmp_path, rel_tol=0.0,
        probe=canned_probe({"clip.mp4": 55.0}), run=make_run(calls))
    assert outcomes[0]["code"] == 0 and len(calls) == 1


# --- summarize / format -----------------------------------------------------

def test_summarize_buckets_by_code():
    outcomes = [
        {"code": 0}, {"code": 0}, {"code": 1}, {"code": 2},
    ]
    assert retime_all.summarize(outcomes) == {
        "repaired": 2, "failed": 1, "skipped_or_refused": 1}


def test_format_text_nothing_to_repair(tmp_path):
    text = retime_all.format_text([], tmp_path / "sessions.jsonl", 0)
    assert "nothing to repair" in text


# --- CLI --------------------------------------------------------------------

def test_main_exit_0_when_nothing_sped_up(tmp_path):
    clip = tmp_path / "ok.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("ok.mp4", 60.0)])
    code, text = run_main(
        ["--log", str(log)], canned_probe({"ok.mp4": 59.0}), make_run([]))
    assert code == 0
    assert "nothing to repair" in text


def test_main_exit_0_all_repaired(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    calls = []
    code, text = run_main(
        ["--log", str(log)], canned_probe({"clip.mp4": 6.0}), make_run(calls))
    assert code == 0
    assert len(calls) == 1
    assert "1 sped-up clip(s): 1 repaired" in text


def test_main_exit_1_on_repair_failure(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    code, _ = run_main(
        ["--log", str(log)], canned_probe({"clip.mp4": 6.0}),
        make_run([], fail_on=("clip.mp4",)))
    assert code == 1


def test_main_exit_1_on_skipped_existing_output(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    touch(tmp_path / "clip.retimed.mp4")
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    code, text = run_main(
        ["--log", str(log)], canned_probe({"clip.mp4": 6.0}), make_run([]))
    assert code == 1
    assert "1 skipped/refused" in text


def test_main_dry_run_reports_without_repairing(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    calls = []
    code, text = run_main(
        ["--log", str(log), "--dry-run"],
        canned_probe({"clip.mp4": 6.0}), make_run(calls))
    assert code == 0
    assert calls == []
    assert "setpts" in text  # would-be command shown


def test_main_json_shape(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    code, text = run_main(
        ["--log", str(log), "--json"],
        canned_probe({"clip.mp4": 6.0}), make_run([]))
    assert code == 0
    payload = json.loads(text)
    assert payload["log"] == str(log)
    assert payload["sped_up"] == 1
    assert payload["counts"] == {
        "repaired": 1, "failed": 0, "skipped_or_refused": 0}
    (outcome,) = payload["outcomes"]
    assert outcome["file"] == "clip.mp4"
    assert outcome["code"] == 0


def test_main_tolerance_flows_through_to_selection(tmp_path):
    # 55s video vs 60s logged: OK at the default 10% (tol 6.0),
    # SPED_UP at --tolerance 0 (2s absolute floor only).
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    probe = canned_probe({"clip.mp4": 55.0})
    code, text = run_main(["--log", str(log)], probe, make_run([]))
    assert code == 0 and "nothing to repair" in text
    calls = []
    code, _ = run_main(["--log", str(log), "--tolerance", "0"],
                       probe, make_run(calls))
    assert code == 0 and len(calls) == 1


def test_main_negative_tolerance_is_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        retime_all.main(["--tolerance", "-1"])
    assert exc_info.value.code == 2


def test_main_video_dir_flag(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    clip = clips / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    calls = []
    code, _ = run_main(
        ["--log", str(log), "--video-dir", str(clips)],
        canned_probe({"clip.mp4": 6.0}), make_run(calls))
    assert code == 0
    assert len(calls) == 1
    # repaired clip lands next to the input, not next to the log
    assert str(clips / "clip.retimed.mp4") in " ".join(calls[0])
