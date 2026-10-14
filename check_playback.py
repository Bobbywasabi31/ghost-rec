"""Check that ghost-rec clips play back at real-time speed.

Issue #1 follow-up: when the machine couldn't sustain 60fps, the encoder
stamped every captured frame 1/60s apart, so minutes of screen time played
back in seconds (a ~30x speed-up on Alex's 4K clip). ghost_rec.py now pads
with duplicate frames to track wall-clock time, and sessions.jsonl records
the wall-clock capture span (duration_s) per session -- this tool
cross-checks each MP4's actual playback duration against that logged span
and flags drift in either direction:

    python check_playback.py                  # default: ~/Videos/ghost/sessions.jsonl
    python check_playback.py --log path\\to\\sessions.jsonl
    python check_playback.py --video-dir path\\to\\clips   # MP4s live elsewhere
    python check_playback.py --tolerance 5    # relative drift tolerance, percent
    python check_playback.py --json           # machine-readable report
    python check_playback.py --csv            # spreadsheet-friendly CSV
    python check_playback.py --from 2026-10-01 --to 2026-10-10  # date range
    python check_playback.py --days 7     # last 7 days including today

Video probing uses the ffmpeg binary bundled with imageio-ffmpeg (already a
ghost-rec dependency); the file itself is never decoded frame-by-frame.

The date range filters by session start day (inclusive, one-sided ok),
mirroring sessions_summary.py: sessions outside the range are not checked,
and sessions with no usable start date are excluded (and counted) whenever
a bound is set. `--days N` (N >= 1) is shorthand for the last N days
including today and just fills whichever bound an explicit --from/--to
doesn't set, so `--from 2026-10-01 --days 30` checks that day through
today; garbage --days values exit 2.

Exit codes: 0 = every checkable session is within tolerance (or nothing was
checkable), 1 = at least one session drifted beyond tolerance, 2 = usage
error. Bad log lines are skipped and counted, never fatal -- same stance as
sessions_summary.py.

The tool also flags MP4s in the video dir that no log entry names
(status ORPHANED -- a crashed session, a pre-logging recording, or a file
copied in by hand; `*.retimed.mp4` repair outputs are repair artifacts and
are excluded). Orphans are warnings only: they never flip the exit code to
1, since there is no logged span to compare them against. Under a date
range, orphans are filtered by file modification day instead of session
start day.

Notes (log path, skipped lines, active filters) go to stderr under --csv so
stdout stays pure CSV; --json and --csv cannot be combined.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sessions_summary import (
    _parse_day, filter_entries_by_date, load_entries, resolve_days_range,
)

DEFAULT_LOG = Path.home() / "Videos" / "ghost" / "sessions.jsonl"

_DURATION_RE = re.compile(r"Duration:\s+(\d+):(\d+):([\d.]+)")

try:
    from imageio_ffmpeg import get_ffmpeg_exe
except ImportError:  # pragma: no cover - optional at probe time
    get_ffmpeg_exe = None


def _coerce_seconds(value) -> float | None:
    """Coerce a logged duration to a finite positive float, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number if number > 0 else None


def _ffmpeg_stderr(path: Path) -> str | None:
    """Run the bundled ffmpeg with `-i` and return its stderr, or None.

    Never decodes frames, so even 4K clips probe in milliseconds. Returns
    None when ffmpeg is unavailable or the file can't be opened.
    """
    if get_ffmpeg_exe is None:
        return None
    try:
        exe = get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - binary lookup must not kill the tool
        return None
    try:
        proc = subprocess.run(
            [exe, "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stderr


def _parse_duration(stderr: str) -> float | None:
    """Parse ffmpeg's `Duration: HH:MM:SS.cc` line; None when absent."""
    match = _DURATION_RE.search(stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    try:
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def probe_video_duration(path: Path) -> float | None:
    """Return an MP4's playback duration in seconds, or None if unprobable."""
    stderr = _ffmpeg_stderr(path)
    if stderr is None:
        return None
    return _parse_duration(stderr)


def check_entry(
    entry: dict,
    video_dir: Path,
    probe=probe_video_duration,
    rel_tol: float = 0.10,
    abs_floor: float = 2.0,
) -> dict:
    """Check one log entry against its MP4; return a result dict.

    Statuses: OK (within tolerance), SPED_UP (video much shorter than the
    logged capture span -- the issue #1 symptom), STRETCHED (video much
    longer), MISSING (file not found), UNREADABLE (couldn't probe),
    NO_FILE (session logged no file -- zero-frame sessions never write one),
    NO_DURATION (logged duration missing/unusable, nothing to compare).
    """
    name = entry.get("file")
    expected = _coerce_seconds(entry.get("duration_s"))
    result = {
        "file": name,
        "duration_s": expected,
        "video_s": None,
        "status": None,
        "note": "",
    }
    if not name:
        result["status"] = "NO_FILE"
        result["note"] = "session logged no file (zero frames captured)"
        return result
    path = video_dir / name
    if not path.is_file():
        result["status"] = "MISSING"
        result["note"] = f"{path} not found"
        return result
    video_s = probe(path)
    result["video_s"] = video_s
    if video_s is None:
        result["status"] = "UNREADABLE"
        result["note"] = "ffmpeg could not read the file"
        return result
    if expected is None:
        result["status"] = "NO_DURATION"
        result["note"] = "logged duration_s missing or unusable"
        return result
    tol = max(rel_tol * expected, abs_floor)
    if video_s < expected - tol:
        result["status"] = "SPED_UP"
        pct = 100.0 * video_s / expected
        result["note"] = (
            f"video is {video_s:.1f}s for {expected:.1f}s of capture "
            f"({pct:.0f}% of real time) -- sped-up playback"
        )
    elif video_s > expected + tol:
        result["status"] = "STRETCHED"
        result["note"] = (
            f"video is {video_s:.1f}s for {expected:.1f}s of capture "
            f"-- longer than real time"
        )
    else:
        result["status"] = "OK"
        result["note"] = f"{video_s:.1f}s vs {expected:.1f}s logged"
    return result


def _check_entries(
    entries: list[dict],
    skipped: int,
    video_dir: Path,
    probe=probe_video_duration,
    rel_tol: float = 0.10,
    abs_floor: float = 2.0,
    date_from=None,
    date_to=None,
) -> tuple[list[dict], int, int, int]:
    """Check already-loaded entries; return (results, skipped, out_of_range,
    undated_excluded)."""
    entries, out_of_range, undated_excluded = filter_entries_by_date(
        entries, date_from, date_to
    )
    results = [
        check_entry(entry, video_dir, probe, rel_tol, abs_floor)
        for entry in entries
    ]
    return results, skipped, out_of_range, undated_excluded


def check_log(
    log_path: Path,
    video_dir: Path,
    probe=probe_video_duration,
    rel_tol: float = 0.10,
    abs_floor: float = 2.0,
    date_from=None,
    date_to=None,
) -> tuple[list[dict], int, int, int]:
    """Check the log's entries; return (results, skipped, out_of_range,
    undated_excluded).

    When date_from/date_to are set, only entries whose start day falls
    within [date_from, date_to] are checked; entries outside the range
    count as out_of_range, and undated entries count as undated_excluded.
    """
    entries, skipped = load_entries(log_path)
    return _check_entries(entries, skipped, video_dir, probe,
                          rel_tol, abs_floor, date_from, date_to)


def find_orphans(
    video_dir: Path,
    known_names,
    probe=probe_video_duration,
    date_from=None,
    date_to=None,
) -> tuple[list[dict], int, int]:
    """Find MP4s on disk that no log entry names.

    Returns (orphans, out_of_range, undated): each orphan is
    {"file": name, "video_s": probed playback seconds or None}. Files are
    matched against known_names exactly and case-insensitively (the recorder
    runs on case-insensitive Windows). `*.retimed.mp4` repair outputs are
    repair artifacts derived from logged clips, not orphaned recordings,
    so they are excluded. When date_from/date_to are set, orphans are
    filtered by file modification day (an orphan has no session start);
    files whose mtime can't be read are excluded and counted as undated.
    """
    known = {n for n in known_names if n}
    known_lower = {n.lower() for n in known}
    orphans: list[dict] = []
    out_of_range = 0
    undated = 0
    try:
        files = sorted(video_dir.iterdir())
    except OSError:
        return orphans, out_of_range, undated
    for path in files:
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        name = path.name
        if name.lower().endswith(".retimed.mp4"):
            continue
        if name in known or name.lower() in known_lower:
            continue
        if date_from is not None or date_to is not None:
            try:
                day = datetime.fromtimestamp(path.stat().st_mtime).date()
            except OSError:
                undated += 1
                continue
            if (date_from and day < date_from) or (
                    date_to and day > date_to):
                out_of_range += 1
                continue
        orphans.append({"file": name, "video_s": probe(path)})
    return orphans, out_of_range, undated


def format_text(results: list[dict], log_path: Path, skipped: int,
                date_from=None, date_to=None,
                out_of_range: int = 0, undated_excluded: int = 0,
                orphans: list[dict] | None = None,
                orphan_out_of_range: int = 0,
                orphan_undated: int = 0) -> str:
    """Render a human-readable report."""
    orphans = orphans or []
    lines = [f"playback check: {log_path}"]
    if date_from is not None or date_to is not None:
        lo = date_from.isoformat() if date_from else "beginning"
        hi = date_to.isoformat() if date_to else "end"
        lines.append(f"  range: {lo} -> {hi}")
    if skipped:
        lines.append(f"  (skipped {skipped} unreadable log line(s))")
    if out_of_range:
        lines.append(f"  (excluded {out_of_range} session(s) outside the range)")
    if undated_excluded:
        lines.append(
            f"  (excluded {undated_excluded} session(s) with no usable date)"
        )
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        name = row["file"] or "(no file)"
        video = "?" if row["video_s"] is None else f"{row['video_s']:.1f}s"
        logged = "?" if row["duration_s"] is None else f"{row['duration_s']:.1f}s"
        lines.append(f"  [{row['status']:>10}] {name}  video={video} logged={logged}")
        if row["status"] in ("SPED_UP", "STRETCHED"):
            lines.append(f"               {row['note']}")
    for row in orphans:
        video = "?" if row["video_s"] is None else f"{row['video_s']:.1f}s"
        lines.append(f"  [  ORPHANED] {row['file']}  video={video} logged=?")
        lines.append("               on disk with no log entry "
                     "(crash, pre-logging build, or copied in by hand)")
    if orphan_out_of_range:
        lines.append(
            f"  (excluded {orphan_out_of_range} unlogged file(s) "
            "outside the range)")
    if orphan_undated:
        lines.append(
            f"  (excluded {orphan_undated} unlogged file(s) "
            "with unreadable mtime)")
    drifted = counts.get("SPED_UP", 0) + counts.get("STRETCHED", 0)
    summary = (f"{len(results)} session(s) checked: {drifted} drifted, "
               f"{counts.get('OK', 0)} ok")
    if orphans:
        summary += f", {len(orphans)} orphaned file(s) on disk"
    lines.append(summary)
    return "\n".join(lines) + "\n"


def format_csv(results: list[dict], orphans: list[dict] | None = None) -> str:
    """Render the per-session results as a spreadsheet-friendly CSV.

    One header row (file,status,video_s,duration_s,drift_ratio), then one
    row per checked session, then one ORPHANED row per unlogged MP4 found on
    disk (duration_s and drift_ratio empty -- there is no logged span to
    compare against). Durations are one-decimal seconds; drift_ratio
    is video_s / duration_s (3 decimals, empty when either side is
    unusable -- MISSING/UNREADABLE/NO_FILE/NO_DURATION). Plain decimals so
    spreadsheets and pandas parse cleanly; stdlib csv handles quoting, so
    commas in filenames round-trip.
    """
    orphans = orphans or []
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["file", "status", "video_s", "duration_s", "drift_ratio"])
    for row in results:
        video = row["video_s"]
        expected = row["duration_s"]
        if video is None or expected is None:
            ratio = ""
        else:
            ratio = f"{video / expected:.3f}"
        writer.writerow([
            row["file"] or "",
            row["status"],
            f"{video:.1f}" if video is not None else "",
            f"{expected:.1f}" if expected is not None else "",
            ratio,
        ])
    for row in orphans:
        video = row["video_s"]
        writer.writerow([
            row["file"],
            "ORPHANED",
            f"{video:.1f}" if video is not None else "",
            "",
            "",
        ])
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify ghost-rec clips play back at real-time speed "
        "(issue #1 follow-up)."
    )
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG,
                        help="sessions.jsonl path (default: %(default)s)")
    parser.add_argument("--video-dir", type=Path, default=None,
                        help="directory holding the MP4s "
                        "(default: the log file's directory)")
    parser.add_argument("--tolerance", type=float, default=10.0,
                        help="relative drift tolerance in percent "
                        "(default: %(default)s); a 2s absolute floor "
                        "always applies")
    parser.add_argument("--from", dest="date_from", default=None,
                        help="only check sessions starting on/after "
                        "YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", default=None,
                        help="only check sessions starting on/before "
                        "YYYY-MM-DD")
    parser.add_argument("--days", default=None, metavar="N",
                        help="shorthand for the last N days including today; "
                        "fills any bound that --from/--to doesn't set")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON report on stdout")
    parser.add_argument("--csv", action="store_true",
                        help="print the results as CSV (notes go to stderr "
                        "so stdout stays pure)")
    args = parser.parse_args(argv)
    if args.json and args.csv:
        parser.error("--json and --csv cannot be combined")
    if args.tolerance < 0:
        parser.error("--tolerance must be >= 0")
    # --days supplies defaults; explicit --from/--to override whichever
    # bound they name, so e.g. --from 2026-10-01 --days 30 checks that day
    # through today.
    days_from = days_to = None
    if args.days is not None:
        try:
            days_from, days_to = resolve_days_range(args.days)
        except ValueError as exc:
            parser.error(str(exc))
    date_from = date_to = None
    if args.date_from or args.date_to or days_from is not None:
        try:
            date_from = (_parse_day(args.date_from) if args.date_from
                         else days_from)
            date_to = (_parse_day(args.date_to) if args.date_to
                       else days_to)
        except ValueError as exc:
            parser.error(str(exc))
        if date_from and date_to and date_from > date_to:
            parser.error(f"--from {date_from} is after --to {date_to}")
    video_dir = args.video_dir or args.log.parent
    entries, skipped = load_entries(args.log)
    results, _, out_of_range, undated_excluded = _check_entries(
        entries, skipped, video_dir, rel_tol=args.tolerance / 100.0,
        date_from=date_from, date_to=date_to,
    )
    # A clip with a log entry out of the checked range still has a log
    # entry, so known names come from the unfiltered entries.
    known_names = {e.get("file") for e in entries if e.get("file")}
    orphans, orphan_out_of_range, orphan_undated = find_orphans(
        video_dir, known_names, date_from=date_from, date_to=date_to)
    drifted = [r for r in results if r["status"] in ("SPED_UP", "STRETCHED")]
    if args.json:
        payload = {
            "log": str(args.log),
            "video_dir": str(video_dir),
            "tolerance_pct": args.tolerance,
            "skipped_lines": skipped,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "out_of_range": out_of_range,
            "undated_excluded": undated_excluded,
            "sessions": results,
            "drifted": len(drifted),
            "orphans": orphans,
            "orphaned": len(orphans),
            "orphan_out_of_range": orphan_out_of_range,
            "orphan_undated": orphan_undated,
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    elif args.csv:
        sys.stdout.write(format_csv(results, orphans))
        notes = [f"log: {args.log}"]
        if date_from is not None or date_to is not None:
            lo = date_from.isoformat() if date_from else "beginning"
            hi = date_to.isoformat() if date_to else "end"
            notes.append(f"range: {lo} -> {hi}")
        if skipped:
            notes.append(f"skipped {skipped} unreadable line(s)")
        if out_of_range:
            notes.append(
                f"excluded {out_of_range} session(s) outside the range")
        if undated_excluded:
            notes.append(
                f"excluded {undated_excluded} session(s) with no usable date)")
        if orphan_out_of_range:
            notes.append(
                f"excluded {orphan_out_of_range} unlogged file(s) "
                "outside the range")
        if orphan_undated:
            notes.append(
                f"excluded {orphan_undated} unlogged file(s) "
                "with unreadable mtime")
        if orphans:
            notes.append(f"{len(orphans)} orphaned file(s) on disk")
        sys.stderr.write("\n".join(notes) + "\n")
    else:
        sys.stdout.write(format_text(results, args.log, skipped,
                                     date_from, date_to,
                                     out_of_range, undated_excluded,
                                     orphans, orphan_out_of_range,
                                     orphan_undated))
    return 1 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
