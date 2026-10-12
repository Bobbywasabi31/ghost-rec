"""Repair every sped-up clip in a session log in one pass.

Issue #1 follow-up: ``check_playback.py`` *finds* clips whose encoded
duration is much shorter than the wall-clock capture span logged in
sessions.jsonl, and ``retime_clip.py`` *repairs* one clip at a time. When
more than one pre-fix recording exists, repairing them one by one is
tedious -- this tool finds every SPED_UP clip and re-times each back to
its logged duration in a single run:

    python retime_all.py                  # default: ~/Videos/ghost/sessions.jsonl
    python retime_all.py --log path\\to\\sessions.jsonl
    python retime_all.py --video-dir path\\to\\clips   # MP4s live elsewhere
    python retime_all.py --tolerance 5    # drift tolerance, percent
    python retime_all.py --dry-run        # report only, run no ffmpeg
    python retime_all.py --force          # overwrite existing .retimed.mp4s
    python retime_all.py --json           # machine-readable report
    python retime_all.py --csv            # spreadsheet-friendly CSV
    python retime_all.py --from 2026-10-01 --to 2026-10-10  # date range

The date range filters by session start day (inclusive, one-sided ok),
mirroring ``check_playback.py``: sessions starting outside the range are
not repaired, and sessions with no usable start date are excluded (and
counted) whenever a bound is set.

Each clip is repaired exactly as ``retime_clip.py`` would: the video
stream is re-encoded with stretched timestamps (frames held as needed at
60fps); any audio track is already in real time, so it is copied untouched
-- which also restores A/V sync. The input file is never modified; the
repaired clip lands next to it as ``<name>.retimed.mp4``. An output that
already exists is skipped unless ``--force``. STRETCHED clips are left
alone on purpose -- the issue #1 symptom was speed-up, and shrinking a
stretched clip could destroy legitimate content.

Exit codes: 0 = nothing to repair, or every repair succeeded;
1 = at least one repair failed (details in the report); 2 = usage error.
Bad log lines are skipped and counted, never fatal -- same stance as the
sibling tools.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from check_playback import check_log, probe_video_duration
from retime_clip import ABS_FLOOR, REL_TOL, _ffmpeg_run, resolve_output, retime
from sessions_summary import _parse_day

DEFAULT_LOG = Path.home() / "Videos" / "ghost" / "sessions.jsonl"


def find_sped_up(
    log_path: Path,
    video_dir: Path,
    probe=probe_video_duration,
    rel_tol: float = REL_TOL,
    abs_floor: float = ABS_FLOOR,
    date_from=None,
    date_to=None,
) -> tuple[list[dict], int, int, int]:
    """Return (results, skipped, out_of_range, undated_excluded) for clips
    check_playback flags SPED_UP.

    *results* are the raw check_playback result dicts (file, duration_s,
    video_s, status, note) -- only the SPED_UP ones. STRETCHED clips are
    deliberately excluded (see module docstring). date_from/date_to are
    YYYY-MM-DD dates limiting the selection to sessions starting within
    [date_from, date_to], exactly as in check_playback.check_log.
    """
    results, skipped, out_of_range, undated_excluded = check_log(
        log_path, video_dir, probe, rel_tol=rel_tol, abs_floor=abs_floor,
        date_from=date_from, date_to=date_to)
    return ([r for r in results if r["status"] == "SPED_UP"],
            skipped, out_of_range, undated_excluded)


def repair_all(
    results: list[dict],
    video_dir: Path,
    force: bool = False,
    dry_run: bool = False,
    probe=probe_video_duration,
    run=_ffmpeg_run,
    rel_tol: float = REL_TOL,
    abs_floor: float = ABS_FLOOR,
) -> list[dict]:
    """Repair every SPED_UP result; return one outcome dict per clip.

    Outcome: {"file", "output", "code", "message"}. Per-clip failures never
    stop the batch -- a failing clip is recorded and the next one runs.
    rel_tol/abs_floor are the selection tolerance: they flow into
    retime_clip's already-real-time gate so a clip flagged SPED_UP at a
    stricter tolerance is actually repaired, not no-op'd.
    """
    outcomes = []
    for result in results:
        input_path = video_dir / result["file"]
        output_path = resolve_output(input_path, None)
        try:
            code, message = retime(
                input_path, result["duration_s"], output_path,
                force=force, dry_run=dry_run, probe=probe, run=run,
                rel_tol=rel_tol, abs_floor=abs_floor,
            )
        except Exception as exc:  # noqa: BLE001 - batch must continue
            code, message = 1, f"unexpected error: {exc}"
        outcomes.append({
            "file": result["file"],
            "output": str(output_path),
            "code": code,
            "message": message,
        })
    return outcomes


def summarize(outcomes: list[dict]) -> dict:
    """Count per-clip outcomes by exit code."""
    counts = {"repaired": 0, "failed": 0, "skipped_or_refused": 0}
    for outcome in outcomes:
        if outcome["code"] == 0:
            counts["repaired"] += 1
        elif outcome["code"] == 1:
            counts["failed"] += 1
        else:
            counts["skipped_or_refused"] += 1
    return counts


def format_text(outcomes: list[dict], log_path: Path, skipped: int,
                date_from=None, date_to=None,
                out_of_range: int = 0, undated_excluded: int = 0) -> str:
    """Render a human-readable report."""
    lines = [f"batch repair: {log_path}"]
    if date_from is not None or date_to is not None:
        lo = date_from.isoformat() if date_from else "beginning"
        hi = date_to.isoformat() if date_to else "end"
        lines.append(f"  range: {lo} -> {hi}")
    if skipped:
        lines.append(f"  (skipped {skipped} unreadable log line(s))")
    if out_of_range:
        lines.append(
            f"  (excluded {out_of_range} session(s) outside the range)")
    if undated_excluded:
        lines.append(
            f"  (excluded {undated_excluded} session(s) with no usable date)"
        )
    if not outcomes:
        lines.append("nothing to repair -- no SPED_UP clips found")
        return "\n".join(lines) + "\n"
    for outcome in outcomes:
        lines.append(f"  [exit {outcome['code']}] {outcome['file']}")
        lines.append(f"               {outcome['message']}")
    counts = summarize(outcomes)
    lines.append(
        f"{len(outcomes)} sped-up clip(s): {counts['repaired']} repaired, "
        f"{counts['failed']} failed, "
        f"{counts['skipped_or_refused']} skipped/refused"
    )
    return "\n".join(lines) + "\n"


def format_csv(outcomes: list[dict]) -> str:
    """Render the per-clip repair outcomes as a spreadsheet-friendly CSV.

    One header row (file,output,code,message), then one row per clip.
    Plain decimals and strings so spreadsheets and pandas parse cleanly;
    stdlib csv handles quoting, so commas in filenames round-trip.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["file", "output", "code", "message"])
    for outcome in outcomes:
        writer.writerow([
            outcome["file"] or "",
            outcome["output"] or "",
            outcome["code"],
            outcome["message"] or "",
        ])
    return buf.getvalue()


def main(
    argv: list[str] | None = None,
    probe=probe_video_duration,
    run=_ffmpeg_run,
) -> int:
    parser = argparse.ArgumentParser(
        description="Repair every sped-up ghost-rec clip in the session log "
        "(issue #1 follow-up batch tool)."
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
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing .retimed.mp4 outputs; also "
                        "allow extreme stretch factors")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be repaired without running "
                        "ffmpeg")
    parser.add_argument("--from", dest="date_from", default=None,
                        help="only repair sessions starting on/after "
                        "YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", default=None,
                        help="only repair sessions starting on/before "
                        "YYYY-MM-DD")
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
    date_from = date_to = None
    if args.date_from or args.date_to:
        try:
            date_from = _parse_day(args.date_from) if args.date_from else None
            date_to = _parse_day(args.date_to) if args.date_to else None
        except ValueError as exc:
            parser.error(str(exc))
        if date_from and date_to and date_from > date_to:
            parser.error(f"--from {date_from} is after --to {date_to}")
    video_dir = args.video_dir or args.log.parent

    rel_tol = args.tolerance / 100.0
    results, skipped, out_of_range, undated_excluded = find_sped_up(
        args.log, video_dir, probe=probe, rel_tol=rel_tol,
        date_from=date_from, date_to=date_to,
    )
    outcomes = repair_all(
        results, video_dir, force=args.force, dry_run=args.dry_run,
        probe=probe, run=run, rel_tol=rel_tol,
    )
    counts = summarize(outcomes)
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
            "sped_up": len(results),
            "dry_run": args.dry_run,
            "outcomes": outcomes,
            "counts": counts,
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    elif args.csv:
        sys.stdout.write(format_csv(outcomes))
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
                f"excluded {undated_excluded} session(s) with no usable date")
        sys.stderr.write("\n".join(notes) + "\n")
    else:
        sys.stdout.write(format_text(outcomes, args.log, skipped,
                                     date_from, date_to,
                                     out_of_range, undated_excluded))
    return 1 if counts["failed"] or counts["skipped_or_refused"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
