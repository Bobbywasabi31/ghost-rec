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
import json
import sys
from pathlib import Path

from check_playback import check_log, probe_video_duration
from retime_clip import ABS_FLOOR, REL_TOL, _ffmpeg_run, resolve_output, retime

DEFAULT_LOG = Path.home() / "Videos" / "ghost" / "sessions.jsonl"


def find_sped_up(
    log_path: Path,
    video_dir: Path,
    probe=probe_video_duration,
    rel_tol: float = REL_TOL,
    abs_floor: float = ABS_FLOOR,
) -> tuple[list[dict], int]:
    """Return (results, skipped_lines) for clips check_playback flags SPED_UP.

    *results* are the raw check_playback result dicts (file, duration_s,
    video_s, status, note) -- only the SPED_UP ones. STRETCHED clips are
    deliberately excluded (see module docstring).
    """
    results, skipped = check_log(log_path, video_dir, probe,
                                 rel_tol=rel_tol, abs_floor=abs_floor)
    return [r for r in results if r["status"] == "SPED_UP"], skipped


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


def format_text(outcomes: list[dict], log_path: Path, skipped: int) -> str:
    """Render a human-readable report."""
    lines = [f"batch repair: {log_path}"]
    if skipped:
        lines.append(f"  (skipped {skipped} unreadable log line(s))")
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
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON report on stdout")
    args = parser.parse_args(argv)
    if args.tolerance < 0:
        parser.error("--tolerance must be >= 0")
    video_dir = args.video_dir or args.log.parent

    rel_tol = args.tolerance / 100.0
    results, skipped = find_sped_up(
        args.log, video_dir, probe=probe, rel_tol=rel_tol
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
            "sped_up": len(results),
            "dry_run": args.dry_run,
            "outcomes": outcomes,
            "counts": counts,
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(format_text(outcomes, args.log, skipped))
    return 1 if counts["failed"] or counts["skipped_or_refused"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
