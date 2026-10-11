"""Logic tests for sessions_summary.py.

Run:  pytest -q

Stdlib-only module, so these run on the Ubuntu + Windows CI matrix with no
headless-stub machinery needed.
"""

import json

import pytest

import sessions_summary


def write_log(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def entry(start="2026-10-09T14:00:00", duration=60.0, size=1024, audio=True,
          file="2026-10-09_140000.mp4"):
    return json.dumps({
        "start": start,
        "stop": "2026-10-09T14:01:00",
        "duration_s": duration,
        "file": file,
        "size_bytes": size,
        "audio": audio,
    })


def test_load_entries_good_and_bad_lines(tmp_path):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        entry(),
        '{"not json',
        '[1, 2]',
        entry(file="b.mp4"),
        "",
    ])
    entries, skipped = sessions_summary.load_entries(log)
    assert len(entries) == 2
    assert skipped == 2  # corrupt JSON + non-dict; blank line ignored silently


def test_load_entries_missing_file(tmp_path):
    entries, skipped = sessions_summary.load_entries(tmp_path / "nope.jsonl")
    assert entries == [] and skipped == 0


def test_summarize_totals(tmp_path):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        entry(start="2026-10-09T10:00:00", duration=60.0, size=1000,
              audio=True, file="a.mp4"),
        entry(start="2026-10-09T11:00:00", duration=120.0, size=3000,
              audio=False, file="b.mp4"),
        entry(start="2026-10-10T09:00:00", duration=30.0, size=500,
              audio=True, file="c.mp4"),
    ])
    entries, _ = sessions_summary.load_entries(log)
    s = sessions_summary.summarize(entries)
    assert s["sessions"] == 3
    assert s["total_duration_s"] == 210.0
    assert s["total_bytes"] == 4500
    assert s["audio_sessions"] == 2
    assert s["days"] == {
        "2026-10-09": {"sessions": 2, "duration_s": 180.0},
        "2026-10-10": {"sessions": 1, "duration_s": 30.0},
    }
    assert s["largest"] == {"file": "b.mp4", "size_bytes": 3000.0}


def test_summarize_empty():
    s = sessions_summary.summarize([])
    assert s["sessions"] == 0
    assert s["total_duration_s"] == 0
    assert s["total_bytes"] == 0
    assert s["audio_sessions"] == 0
    assert s["days"] == {}
    assert s["largest"] is None


def test_summarize_bad_start_goes_to_unknown():
    entries = [
        json.loads(entry(start="not-a-date")),
        json.loads(entry(start="2026-10-09T10:00:00", duration=5.0)),
        json.loads('{"duration_s": 5.0}'),  # missing start
    ]
    s = sessions_summary.summarize(entries)
    assert set(s["days"]) == {"unknown", "2026-10-09"}
    assert s["days"]["unknown"]["sessions"] == 2


def test_summarize_junk_numbers_never_crash():
    entries = [
        json.loads(entry(duration="junk", size="junk")),
        json.loads(entry(duration=float("nan"), size=None)),
    ]
    s = sessions_summary.summarize(entries)
    assert s["total_duration_s"] == 0
    assert s["total_bytes"] == 0


def test_main_text_output(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry()])
    assert sessions_summary.main(["--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "Sessions: 1" in out
    assert "2026-10-09: 1 session(s)" in out
    assert "Sessions with audio: 1" in out


def test_main_json_output(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry(), "garbage"])
    assert sessions_summary.main(["--log", str(log), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"] == 1
    assert payload["skipped_lines"] == 1
    assert payload["log"] == str(log)


def test_main_missing_log_is_not_an_error(tmp_path, capsys):
    assert sessions_summary.main(["--log", str(tmp_path / "absent.jsonl")]) == 0
    assert "Sessions: 0" in capsys.readouterr().out


def test_filter_entries_by_date_inclusive_bounds():
    entries = [
        json.loads(entry(start="2026-10-08T10:00:00")),  # before range
        json.loads(entry(start="2026-10-09T10:00:00")),  # on --from
        json.loads(entry(start="2026-10-10T09:00:00")),  # on --to
        json.loads(entry(start="2026-10-11T09:00:00")),  # after range
        json.loads('{"duration_s": 5.0}'),  # no date at all
    ]
    kept, out_of_range, undated = sessions_summary.filter_entries_by_date(
        entries,
        sessions_summary._parse_day("2026-10-09"),
        sessions_summary._parse_day("2026-10-10"),
    )
    assert [e["start"] for e in kept] == ["2026-10-09T10:00:00", "2026-10-10T09:00:00"]
    assert out_of_range == 2
    assert undated == 1


def test_filter_entries_by_date_no_bounds_keeps_everything():
    entries = [json.loads(entry()), json.loads('{"duration_s": 5.0}')]
    kept, out_of_range, undated = sessions_summary.filter_entries_by_date(entries)
    assert len(kept) == 2
    assert (out_of_range, undated) == (0, 0)


def test_filter_entries_by_date_one_sided_bounds():
    entries = [
        json.loads(entry(start="2026-10-08T10:00:00")),
        json.loads(entry(start="2026-10-10T09:00:00")),
    ]
    kept, _, _ = sessions_summary.filter_entries_by_date(
        entries, date_to=sessions_summary._parse_day("2026-10-08")
    )
    assert [e["start"] for e in kept] == ["2026-10-08T10:00:00"]
    kept, _, _ = sessions_summary.filter_entries_by_date(
        entries, date_from=sessions_summary._parse_day("2026-10-10")
    )
    assert [e["start"] for e in kept] == ["2026-10-10T09:00:00"]


def test_parse_day_rejects_garbage():
    with pytest.raises(ValueError):
        sessions_summary._parse_day("october 9")


def test_main_from_to_text(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        entry(start="2026-10-08T10:00:00", file="a.mp4"),
        entry(start="2026-10-09T10:00:00", file="b.mp4"),
        entry(start="2026-10-10T09:00:00", file="c.mp4"),
        '{"duration_s": 5.0}',  # undated, excluded while filtering
    ])
    args = ["--log", str(log), "--from", "2026-10-09", "--to", "2026-10-10"]
    assert sessions_summary.main(args) == 0
    out = capsys.readouterr().out
    assert "Range: 2026-10-09 -> 2026-10-10" in out
    assert "Sessions: 2" in out
    assert "2026-10-09: 1 session(s)" in out
    assert "(excluded 1 session(s) outside the range)" in out
    assert "(excluded 1 session(s) with no usable date)" in out


def test_main_from_to_json(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        entry(start="2026-10-08T10:00:00", file="a.mp4"),
        entry(start="2026-10-09T10:00:00", file="b.mp4"),
    ])
    args = ["--log", str(log), "--json", "--from", "2026-10-09"]
    assert sessions_summary.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"] == 1
    assert payload["date_from"] == "2026-10-09"
    assert payload["date_to"] is None
    assert payload["out_of_range"] == 1
    assert payload["undated_excluded"] == 0


def test_main_no_range_json_has_null_bounds(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry()])
    assert sessions_summary.main(["--log", str(log), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["date_from"] is None and payload["date_to"] is None
    assert payload["out_of_range"] == 0 and payload["undated_excluded"] == 0


@pytest.mark.parametrize("args", [
    ["--from", "not-a-date"],
    ["--to", "2026-13-40"],
    ["--from", "2026-10-10", "--to", "2026-10-09"],  # from > to
])
def test_main_bad_range_exits_2(tmp_path, capsys, args):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry()])
    with pytest.raises(SystemExit) as excinfo:
        sessions_summary.main(["--log", str(log)] + args)
    assert excinfo.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err or "after" in capsys.readouterr().err


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (59, "59s"), (61, "1m 1s"), (3600, "1h 0m 0s"),
    (3661, "1h 1m 1s"),
])
def test_fmt_seconds(seconds, expected):
    assert sessions_summary._fmt_seconds(seconds) == expected


@pytest.mark.parametrize("num,expected", [
    (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"),
    (5 * 1024 * 1024, "5.0 MB"),
])
def test_fmt_bytes(num, expected):
    assert sessions_summary._fmt_bytes(num) == expected
