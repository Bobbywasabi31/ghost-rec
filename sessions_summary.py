"""Summarize ghost-rec's session log.

Reads the sessions.jsonl written by ghost_rec.py (one JSON object per line
per finished session) and prints a small summary: how many sessions were
recorded, total recorded time and disk usage, how many had audio, a per-day
breakdown, and the largest file.

Stdlib only -- runs anywhere, including a bare Windows box:

    python sessions_summary.py                # default: ~/Videos/ghost/sessions.jsonl
    python sessions_summary.py --log out/sessions.jsonl
    python sessions_summary.py --json         # machine-readable summary
    python sessions_summary.py --from 2026-10-01 --to 2026-10-10  # date range

Bad lines (corrupt JSON, missing fields) are skipped and counted, never
fatal: the log is a personal review aid, not a database.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_LOG = Path.home() / "Videos" / "ghost" / "sessions.jsonl"


def _as_number(value, default=0):
    """Coerce a value to a number for aggregation; junk becomes *default*."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # NaN -> default


def load_entries(log_path: Path) -> tuple[list[dict], int]:
    """Parse *log_path* into entry dicts; return (entries, skipped_bad_lines)."""
    entries: list[dict] = []
    skipped = 0
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return entries, skipped
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(entry, dict):
            skipped += 1
            continue
        entries.append(entry)
    return entries, skipped


def _entry_date(entry: dict):
    """Return the entry's start day as a date, or None if missing/invalid."""
    start = entry.get("start")
    if isinstance(start, str) and len(start) >= 10:
        try:
            return datetime.fromisoformat(start).date()
        except ValueError:
            return None
    return None


def _day_key(entry: dict) -> str:
    day = _entry_date(entry)
    return day.isoformat() if day else "unknown"


def _parse_day(value: str):
    """Parse a YYYY-MM-DD CLI date; raise ValueError with a friendly message."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"bad date {value!r}: expected YYYY-MM-DD") from None


def filter_entries_by_date(entries: list[dict], date_from=None, date_to=None):
    """Keep entries whose start day falls within [date_from, date_to].

    Bounds are dates or None (open). Entries with no usable start date are
    excluded whenever a bound is set -- they cannot be placed in the range.
    Returns (kept, out_of_range, undated_excluded).
    """
    if date_from is None and date_to is None:
        return list(entries), 0, 0
    kept: list[dict] = []
    out_of_range = 0
    undated = 0
    for entry in entries:
        day = _entry_date(entry)
        if day is None:
            undated += 1
            continue
        if (date_from is not None and day < date_from) or (
            date_to is not None and day > date_to
        ):
            out_of_range += 1
            continue
        kept.append(entry)
    return kept, out_of_range, undated


def _fmt_seconds(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _fmt_bytes(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def summarize(entries: list[dict]) -> dict:
    """Reduce parsed log entries to a summary dict."""
    sessions = len(entries)
    total_duration = sum(_as_number(e.get("duration_s")) for e in entries)
    total_bytes = sum(_as_number(e.get("size_bytes")) for e in entries)
    audio_sessions = sum(1 for e in entries if e.get("audio") is True)

    per_day: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "duration_s": 0.0})
    for entry in entries:
        day = per_day[_day_key(entry)]
        day["sessions"] += 1
        day["duration_s"] += _as_number(entry.get("duration_s"))

    largest: dict | None = None
    for entry in entries:
        size = _as_number(entry.get("size_bytes"))
        if largest is None or size > largest["size_bytes"]:
            largest = {"file": entry.get("file"), "size_bytes": size}

    return {
        "sessions": sessions,
        "total_duration_s": round(total_duration, 1),
        "total_bytes": int(total_bytes),
        "audio_sessions": audio_sessions,
        "days": {
            day: {"sessions": d["sessions"], "duration_s": round(d["duration_s"], 1)}
            for day, d in sorted(per_day.items())
        },
        "largest": largest,
    }


def format_text(summary: dict, log_path: Path, skipped: int,
                date_from=None, date_to=None,
                out_of_range: int = 0, undated_excluded: int = 0) -> str:
    """Render the summary as a human-readable one-page report."""
    lines = [f"ghost-rec sessions: {log_path}"]
    if date_from is not None or date_to is not None:
        lo = date_from.isoformat() if date_from else "beginning"
        hi = date_to.isoformat() if date_to else "end"
        lines.append(f"Range: {lo} -> {hi}")
    lines.append(f"Sessions: {summary['sessions']}")
    lines.append(
        f"Recorded: {_fmt_seconds(summary['total_duration_s'])} "
        f"in {summary['sessions']} session(s), "
        f"{_fmt_bytes(summary['total_bytes'])} total"
    )
    lines.append(f"Sessions with audio: {summary['audio_sessions']}")
    largest = summary["largest"]
    if largest and largest["file"]:
        lines.append(
            f"Largest file: {largest['file']} "
            f"({_fmt_bytes(largest['size_bytes'])})"
        )
    if summary["days"]:
        lines.append("Per day:")
        for day, d in summary["days"].items():
            lines.append(
                f"  {day}: {d['sessions']} session(s), "
                f"{_fmt_seconds(d['duration_s'])}"
            )
    if skipped:
        lines.append(f"(skipped {skipped} unreadable line(s))")
    if out_of_range:
        lines.append(f"(excluded {out_of_range} session(s) outside the range)")
    if undated_excluded:
        lines.append(
            f"(excluded {undated_excluded} session(s) with no usable date)"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize ghost-rec's sessions.jsonl session log."
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help="path to sessions.jsonl (default: ~/Videos/ghost/sessions.jsonl)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the summary as JSON instead of text",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        metavar="YYYY-MM-DD",
        help="only include sessions starting on or after this day",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        metavar="YYYY-MM-DD",
        help="only include sessions starting on or before this day",
    )
    args = parser.parse_args(argv)

    date_from = date_to = None
    if args.date_from or args.date_to:
        try:
            date_from = _parse_day(args.date_from) if args.date_from else None
            date_to = _parse_day(args.date_to) if args.date_to else None
        except ValueError as exc:
            parser.error(str(exc))
        if date_from and date_to and date_from > date_to:
            parser.error(f"--from {args.date_from} is after --to {args.date_to}")

    entries, skipped = load_entries(args.log)
    entries, out_of_range, undated_excluded = filter_entries_by_date(
        entries, date_from, date_to
    )
    summary = summarize(entries)
    if args.json:
        payload = {
            "log": str(args.log),
            "skipped_lines": skipped,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "out_of_range": out_of_range,
            "undated_excluded": undated_excluded,
            **summary,
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(
            format_text(
                summary, args.log, skipped,
                date_from, date_to, out_of_range, undated_excluded,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
