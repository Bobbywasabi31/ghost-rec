"""Logic tests for sessions_summary.py.

Run:  pytest -q

Stdlib-only module, so these run on the Ubuntu + Windows CI matrix with no
headless-stub machinery needed.
"""

from datetime import date, timedelta

import csv
import io
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


def csv_rows(summary):
    return list(csv.reader(io.StringIO(sessions_summary.format_csv(summary))))


def test_format_csv_summary_rows():
    s = sessions_summary.summarize([
        json.loads(entry(duration=60.0, size=1000)),
    ])
    rows = csv_rows(s)
    assert rows[0] == ["section", "key", "value"]
    body = rows[1:]
    assert ["summary", "sessions", "1"] in body
    assert ["summary", "total_duration_s", "60.0"] in body
    assert ["summary", "total_bytes", "1000"] in body
    assert ["summary", "audio_sessions", "1"] in body
    assert ["summary", "largest_file", "2026-10-09_140000.mp4"] in body
    assert ["summary", "largest_size_bytes", "1000.0"] in body


def test_format_csv_day_rows_sorted():
    s = sessions_summary.summarize([
        json.loads(entry(start="2026-10-10T09:00:00", file="c.mp4")),
        json.loads(entry(start="2026-10-09T10:00:00", file="a.mp4")),
    ])
    rows = csv_rows(s)
    day_rows = [r for r in rows if r[0] in ("day_sessions", "day_duration_s")]
    assert [r[1] for r in day_rows] == ["2026-10-09", "2026-10-09",
                                       "2026-10-10", "2026-10-10"]
    assert ["day_sessions", "2026-10-09", "1"] in day_rows
    assert ["day_duration_s", "2026-10-10", "60.0"] in day_rows


def test_format_csv_filename_with_comma_roundtrips():
    s = sessions_summary.summarize([
        json.loads(entry(file="odd,name,2026.mp4")),
    ])
    rows = csv_rows(s)
    assert ["summary", "largest_file", "odd,name,2026.mp4"] in rows[1:]


def test_format_csv_empty_summary():
    s = sessions_summary.summarize([])
    rows = csv_rows(s)
    assert rows[0] == ["section", "key", "value"]
    assert ["summary", "sessions", "0"] in rows
    assert not any(r[0] in ("day_sessions", "day_duration_s") for r in rows[1:])
    assert not any(r[1] == "largest_file" for r in rows[1:])


def test_main_csv_output(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry(), "garbage"])
    assert sessions_summary.main(["--log", str(log), "--csv"]) == 0
    captured = capsys.readouterr()
    rows = list(csv.reader(io.StringIO(captured.out)))
    assert rows[0] == ["section", "key", "value"]
    assert ["summary", "sessions", "1"] in rows[1:]
    # notes stay on stderr, stdout stays pure CSV
    assert f"log: {log}" in captured.err
    assert "skipped 1 unreadable line(s)" in captured.err
    assert "skipped" not in captured.out


def test_main_csv_with_range_notes_on_stderr(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        entry(start="2026-10-08T10:00:00", file="a.mp4"),
        entry(start="2026-10-09T10:00:00", file="b.mp4"),
        '{"duration_s": 5.0}',  # undated, excluded while filtering
    ])
    args = ["--log", str(log), "--csv", "--from", "2026-10-09"]
    assert sessions_summary.main(args) == 0
    captured = capsys.readouterr()
    assert "range: 2026-10-09 -> end" in captured.err
    assert "excluded 1 session(s) outside the range" in captured.err
    assert "excluded 1 session(s) with no usable date" in captured.err
    rows = list(csv.reader(io.StringIO(captured.out)))
    assert ["summary", "sessions", "1"] in rows[1:]
    assert "range:" not in captured.out


def test_main_csv_and_json_conflict_exits_2(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry()])
    with pytest.raises(SystemExit) as excinfo:
        sessions_summary.main(["--log", str(log), "--json", "--csv"])
    assert excinfo.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_resolve_days_range_window():
    fake_today = date(2026, 10, 11)
    assert sessions_summary.resolve_days_range("1", fake_today) == (
        fake_today, fake_today,
    )
    assert sessions_summary.resolve_days_range("7", fake_today) == (
        fake_today - timedelta(days=6), fake_today,
    )


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "2.5", ""])
def test_resolve_days_range_rejects_garbage(bad):
    with pytest.raises(ValueError):
        sessions_summary.resolve_days_range(bad, date(2026, 10, 11))


def dated_entry(days_ago, **kwargs):
    start = (date.today() - timedelta(days=days_ago)).isoformat() + "T10:00:00"
    return entry(start=start, **kwargs)


def test_main_days_keeps_last_n_days(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        dated_entry(0, file="today.mp4"),
        dated_entry(6, file="six-ago.mp4"),
        dated_entry(8, file="eight-ago.mp4"),
        '{"duration_s": 5.0}',  # undated, excluded while filtering
    ])
    assert sessions_summary.main(["--log", str(log), "--days", "7"]) == 0
    out = capsys.readouterr().out
    lo = (date.today() - timedelta(days=6)).isoformat()
    hi = date.today().isoformat()
    assert f"Range: {lo} -> {hi}" in out
    assert "Sessions: 2" in out
    assert "(excluded 1 session(s) outside the range)" in out
    assert "(excluded 1 session(s) with no usable date)" in out


def test_main_from_overrides_days_bound(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [
        dated_entry(0, file="today.mp4"),
        dated_entry(1, file="yesterday.mp4"),
        dated_entry(6, file="six-ago.mp4"),
    ])
    # --from sets the lower bound; --days still supplies today as the upper
    args = ["--log", str(log), "--days", "7",
            "--from", (date.today() - timedelta(days=1)).isoformat()]
    assert sessions_summary.main(args) == 0
    out = capsys.readouterr().out
    assert "Sessions: 2" in out
    assert "(excluded 1 session(s) outside the range)" in out


def test_main_days_json_reports_resolved_bounds(tmp_path, capsys):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [dated_entry(0)])
    assert sessions_summary.main(["--log", str(log), "--json", "--days", "3"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"] == 1
    assert payload["date_from"] == (date.today() - timedelta(days=2)).isoformat()
    assert payload["date_to"] == date.today().isoformat()


@pytest.mark.parametrize("args", [
    ["--days", "abc"],
    ["--days", "0"],
    ["--days", "-5"],
])
def test_main_bad_days_exits_2(tmp_path, capsys, args):
    log = tmp_path / "sessions.jsonl"
    write_log(log, [entry()])
    with pytest.raises(SystemExit) as excinfo:
        sessions_summary.main(["--log", str(log)] + args)
    assert excinfo.value.code == 2
    assert "positive integer" in capsys.readouterr().err
