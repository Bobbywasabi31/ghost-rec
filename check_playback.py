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

Video probing uses the ffmpeg binary bundled with imageio-ffmpeg (already a
ghost-rec dependency); the file itself is never decoded frame-by-frame.

Exit codes: 0 = every checkable session is within tolerance (or nothing was
checkable), 1 = at least one session drifted beyond tolerance, 2 = usage
error. Bad log lines are skipped and counted, never fatal -- same stance as
sessions_summary.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from sessions_summary import load_entries

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


def probe_video_duration(path: Path) -> float | None:
    """Return an MP4's playback duration in seconds, or None if unprobable.

    Runs the bundled ffmpeg with `-i` and parses the `Duration:` line from
    its stderr. Never decodes frames, so even 4K clips probe in milliseconds.
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
    match = _DURATION_RE.search(proc.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    try:
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


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


def check_log(
    log_path: Path,
    video_dir: Path,
    probe=probe_video_duration,
    rel_tol: float = 0.10,
    abs_floor: float = 2.0,
) -> tuple[list[dict], int]:
    """Check every entry in *log_path*; return (results, skipped_lines)."""
    entries, skipped = load_entries(log_path)
    results = [
        check_entry(entry, video_dir, probe, rel_tol, abs_floor)
        for entry in entries
    ]
    return results, skipped


def format_text(results: list[dict], log_path: Path, skipped: int) -> str:
    """Render a human-readable report."""
    lines = [f"playback check: {log_path}"]
    if skipped:
        lines.append(f"  (skipped {skipped} unreadable log line(s))")
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        name = row["file"] or "(no file)"
        video = "?" if row["video_s"] is None else f"{row['video_s']:.1f}s"
        logged = "?" if row["duration_s"] is None else f"{row['duration_s']:.1f}s"
        lines.append(f"  [{row['status']:>10}] {name}  video={video} logged={logged}")
        if row["status"] in ("SPED_UP", "STRETCHED"):
            lines.append(f"               {row['note']}")
    drifted = counts.get("SPED_UP", 0) + counts.get("STRETCHED", 0)
    lines.append(
        f"{len(results)} session(s) checked: {drifted} drifted, "
        f"{counts.get('OK', 0)} ok"
    )
    return "\n".join(lines) + "\n"


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
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON report on stdout")
    args = parser.parse_args(argv)
    if args.tolerance < 0:
        parser.error("--tolerance must be >= 0")
    video_dir = args.video_dir or args.log.parent
    results, skipped = check_log(
        args.log, video_dir, rel_tol=args.tolerance / 100.0
    )
    drifted = [r for r in results if r["status"] in ("SPED_UP", "STRETCHED")]
    if args.json:
        payload = {
            "log": str(args.log),
            "video_dir": str(video_dir),
            "tolerance_pct": args.tolerance,
            "skipped_lines": skipped,
            "sessions": results,
            "drifted": len(drifted),
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(format_text(results, args.log, skipped))
    return 1 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
