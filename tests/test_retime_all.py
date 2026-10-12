"""Logic tests for retime_all.py (issue #1 batch repair tool).

Run:  pytest -q

Everything is hermetic: canned probes stand in for ffmpeg duration
probing, canned runs stand in for ffmpeg itself, so no real video files
are needed. The encode path itself is covered by test_retime_clip.py's
end-to-end tests; these cover the batch glue -- SPED_UP selection,
per-clip outcomes, existing-output skips, --force, --dry-run, batch
continuation after a failure, and the CLI exit codes.
"""

import csv
import io
import json
from contextlib import redirect_stderr, redirect_stdout
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


def entry(file="clip.mp4", duration=60.0, start="2026-10-11T12:00:00"):
    return {
        "start": start,
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
    results, skipped, out_of_range, undated = retime_all.find_sped_up(
        log, tmp_path, probe=probe)
    assert skipped == 0
    assert (out_of_range, undated) == (0, 0)
    assert [r["file"] for r in results] == ["clip.mp4"]
    assert results[0]["status"] == "SPED_UP"


def test_find_sped_up_counts_bad_log_lines(tmp_path):
    log = tmp_path / "sessions.jsonl"
    log.write_text('{"file": "clip.mp4", "duration_s": 60.0}\nnot json\n',
                   encoding="utf-8")
    results, skipped, out_of_range, undated = retime_all.find_sped_up(
        log, tmp_path, probe=canned_probe({}))
    assert skipped == 1
    assert (out_of_range, undated) == (0, 0)
    assert results == []  # clip.mp4 never probed: file missing -> MISSING


def test_find_sped_up_leaves_stretched_clips_alone(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    results, skipped, out_of_range, undated = retime_all.find_sped_up(
        log, tmp_path, probe=canned_probe({"clip.mp4": 120.0}))
    assert (skipped, out_of_range, undated) == (0, 0, 0)
    assert results == []  # STRETCHED is a different symptom; not ours


def test_find_sped_up_missing_log_is_empty(tmp_path):
    results, skipped, out_of_range, undated = retime_all.find_sped_up(
        tmp_path / "nope.jsonl", tmp_path, probe=canned_probe({}))
    assert results == []
    assert (skipped, out_of_range, undated) == (0, 0, 0)


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


# --- date range -------------------------------------------------------------

def _two_day_log(tmp_path):
    """Two SPED_UP clips on consecutive days; both files present."""
    for name in ("old.mp4", "new.mp4"):
        touch(tmp_path / name)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        entry("old.mp4", 60.0, start="2026-10-10T12:00:00"),
        entry("new.mp4", 60.0, start="2026-10-11T12:00:00"),
    ])
    probe = canned_probe({"old.mp4": 6.0, "new.mp4": 6.0})
    return log, probe


def test_find_sped_up_date_range_filters_out_of_range(tmp_path):
    from datetime import date
    log, probe = _two_day_log(tmp_path)
    results, skipped, out_of_range, undated = retime_all.find_sped_up(
        log, tmp_path, probe=probe, date_from=date(2026, 10, 11))
    assert skipped == 0
    assert undated == 0
    assert out_of_range == 1
    assert [r["file"] for r in results] == ["new.mp4"]


def test_find_sped_up_undated_excluded_when_bound_set(tmp_path):
    from datetime import date
    log, probe = _two_day_log(tmp_path)
    # an entry with no usable start date cannot be placed in the range
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('{"file": "dateless.mp4", "duration_s": 60.0}\n')
    touch(tmp_path / "dateless.mp4")
    results, skipped, out_of_range, undated = retime_all.find_sped_up(
        log, tmp_path, probe=probe, date_to=date(2026, 10, 11))
    assert skipped == 0
    assert out_of_range == 0
    assert undated == 1
    assert [r["file"] for r in results] == ["old.mp4", "new.mp4"]


def test_main_from_selects_only_in_range_clips(tmp_path):
    log, probe = _two_day_log(tmp_path)
    calls = []
    code, text = run_main(
        ["--log", str(log), "--from", "2026-10-11"],
        probe, make_run(calls))
    assert code == 0
    assert len(calls) == 1  # only new.mp4 repaired
    assert "new.retimed.mp4" in " ".join(calls[0])
    assert "range: 2026-10-11 -> end" in text
    assert "(excluded 1 session(s) outside the range)" in text


def test_main_to_one_sided(tmp_path):
    log, probe = _two_day_log(tmp_path)
    calls = []
    code, _ = run_main(
        ["--log", str(log), "--to", "2026-10-10"],
        probe, make_run(calls))
    assert code == 0
    assert len(calls) == 1
    assert "old.retimed.mp4" in " ".join(calls[0])


def test_main_garbage_date_is_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        retime_all.main(["--from", "not-a-date"])
    assert exc_info.value.code == 2


def test_main_from_after_to_is_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        retime_all.main(["--from", "2026-10-11", "--to", "2026-10-10"])
    assert exc_info.value.code == 2


def test_main_json_carries_date_bounds(tmp_path):
    log, probe = _two_day_log(tmp_path)
    code, text = run_main(
        ["--log", str(log), "--from", "2026-10-10", "--to", "2026-10-11",
         "--json"],
        probe, make_run([]))
    assert code == 0
    payload = json.loads(text)
    assert payload["date_from"] == "2026-10-10"
    assert payload["date_to"] == "2026-10-11"
    assert payload["out_of_range"] == 0
    assert payload["undated_excluded"] == 0
    assert payload["sped_up"] == 2


def test_format_text_shows_range_and_exclusions(tmp_path):
    from datetime import date
    text = retime_all.format_text(
        [], tmp_path / "sessions.jsonl", 0,
        date(2026, 10, 1), date(2026, 10, 10),
        out_of_range=2, undated_excluded=1)
    assert "range: 2026-10-01 -> 2026-10-10" in text
    assert "(excluded 2 session(s) outside the range)" in text
    assert "(excluded 1 session(s) with no usable date)" in text


# --- --csv export ------------------------------------------------------------

def run_main_streams(argv, probe, run):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = retime_all.main(argv, probe=probe, run=run)
    return code, out.getvalue(), err.getvalue()


def test_format_csv_header_and_rows():
    csv_text = retime_all.format_csv([
        {"file": "a.mp4", "output": "a.retimed.mp4",
         "code": 0, "message": "repaired"},
        {"file": "b.mp4", "output": "b.retimed.mp4",
         "code": 1, "message": "ffmpeg boom"},
    ])
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0] == ["file", "output", "code", "message"]
    assert rows[1] == ["a.mp4", "a.retimed.mp4", "0", "repaired"]
    assert rows[2] == ["b.mp4", "b.retimed.mp4", "1", "ffmpeg boom"]


def test_format_csv_comma_in_filename_roundtrips():
    csv_text = retime_all.format_csv([
        {"file": "clip, one.mp4", "output": "clip, one.retimed.mp4",
         "code": 0, "message": "ok, repaired"},
    ])
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[1] == ["clip, one.mp4", "clip, one.retimed.mp4",
                       "0", "ok, repaired"]


def test_format_csv_empty_outcomes_is_header_only():
    rows = list(csv.reader(io.StringIO(retime_all.format_csv([]))))
    assert rows == [["file", "output", "code", "message"]]


def test_main_csv_stdout_is_pure_csv_and_notes_on_stderr(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    code, text, err = run_main_streams(
        ["--log", str(log), "--csv"],
        canned_probe({"clip.mp4": 6.0}), make_run([]))
    assert code == 0
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["file", "output", "code", "message"]
    assert rows[1][0] == "clip.mp4"
    assert rows[1][2] == "0"
    assert rows[1][1].endswith("clip.retimed.mp4")
    assert f"log: {log}" in err


def test_main_csv_empty_outcomes_header_only(tmp_path):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])  # probed at real time
    code, text, err = run_main_streams(
        ["--log", str(log), "--csv"],
        canned_probe({"clip.mp4": 60.0}), make_run([]))
    assert code == 0
    assert list(csv.reader(io.StringIO(text))) == [
        ["file", "output", "code", "message"]]
    assert f"log: {log}" in err


def test_main_csv_failed_repair_row_and_exit_1(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry("clip.mp4", 60.0)])
    code, text, _ = run_main_streams(
        ["--log", str(log), "--csv"],
        canned_probe({"clip.mp4": 6.0}), make_run([], fail_on=["clip"]))
    assert code == 1
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1][2] == "1"
    assert "mock ffmpeg boom" in rows[1][3]


def test_main_csv_skipped_line_note_on_stderr(tmp_path):
    clip = tmp_path / "clip.mp4"
    touch(clip)
    log = tmp_path / "sessions.jsonl"
    log.write_text("not json\n" + json.dumps(entry("clip.mp4", 60.0)) + "\n",
                   encoding="utf-8")
    _, text, err = run_main_streams(
        ["--log", str(log), "--csv"],
        canned_probe({"clip.mp4": 6.0}), make_run([]))
    assert "skipped 1 unreadable line(s)" in err
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 2  # header + the one repaired clip


def test_main_csv_range_note_on_stderr(tmp_path):
    log, probe = _two_day_log(tmp_path)
    _, text, err = run_main_streams(
        ["--log", str(log), "--from", "2026-10-10", "--to", "2026-10-11",
         "--csv"],
        probe, make_run([]))
    assert "range: 2026-10-10 -> 2026-10-11" in err
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 3  # header + both clips


def test_main_json_and_csv_conflict_is_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        retime_all.main(["--json", "--csv"])
    assert exc_info.value.code == 2
