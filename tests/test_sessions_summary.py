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
