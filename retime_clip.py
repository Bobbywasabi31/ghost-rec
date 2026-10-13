"""Repair a ghost-rec clip that plays back at the wrong speed.

Issue #1 follow-up: when the machine couldn't sustain 60fps, the encoder
stamped every captured frame 1/60s apart, so minutes of screen time played
back in seconds. ``check_playback.py`` *finds* such clips; this tool *fixes*
them by re-timing the video stream back to the wall-clock capture span
(from sessions.jsonl, or an explicit --duration), so the clip plays at real
time again.

The video stream is re-encoded with stretched timestamps (frames are held
as needed at 60fps); any audio track is already in real time (the WASAPI
capture records wall-clock audio), so it is copied untouched -- which also
restores A/V sync. The input file is never modified; a new file is written.

    python retime_clip.py ghost_2026-10-11_12-40-01.mp4
    python retime_clip.py clip.mp4 --duration 300
    python retime_clip.py clip.mp4 --log path\\to\\sessions.jsonl
    python retime_clip.py clip.mp4 --output fixed.mp4
    python retime_clip.py clip.mp4 --dry-run      # print the ffmpeg command
    python retime_clip.py clip.mp4 --force        # overwrite output / allow extreme stretch
    python retime_clip.py clip.mp4 --json         # machine-readable result on stdout

The ffmpeg binary bundled with imageio-ffmpeg (already a ghost-rec
dependency) does the re-encode; CREATE_NO_WINDOW is used on Windows so no
console flashes.

With --json the payload (clip, output, target_s, exit_code, encoded_s,
stretch_factor, message) goes to stdout as pure JSON, mirroring the other
ghost-rec report tools; without it, the human-readable message goes to
stdout on success and stderr on failure.

Exit codes: 0 = repaired (or already real time, nothing to do),
1 = the clip couldn't be probed or ffmpeg failed,
2 = usage error (bad duration, overwrite refused, insane stretch).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from check_playback import _ffmpeg_stderr, _parse_duration
from sessions_summary import load_entries

DEFAULT_LOG = Path.home() / "Videos" / "ghost" / "sessions.jsonl"

# Same tolerance shape as check_playback: a clip this close to the logged
# span counts as real time and is left alone.
REL_TOL = 0.10
ABS_FLOOR = 2.0
# Sanity guard: a stretch/shrink beyond this almost certainly means a bad
# log entry or a wrong --duration; require --force to proceed.
MAX_FACTOR = 120.0

try:
    from imageio_ffmpeg import get_ffmpeg_exe
except ImportError:  # pragma: no cover - optional at probe time
    get_ffmpeg_exe = None


def _ffmpeg_exe() -> str | None:
    if get_ffmpeg_exe is None:
        return None
    try:
        return get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - binary lookup must not kill the tool
        return None


def probe_video_duration(path: Path) -> float | None:
    """Return an MP4's playback duration in seconds, or None if unprobable."""
    stderr = _ffmpeg_stderr(path)
    if stderr is None:
        return None
    return _parse_duration(stderr)


def lookup_duration(clip_name: str, log_path: Path) -> float | None:
    """Return the logged duration_s for *clip_name*, or None.

    The last matching entry wins (a re-recorded name would shadow the old
    one). Garbage values (<=0, NaN, non-numeric) are treated as absent.
    """
    entries, _skipped = load_entries(log_path)
    found = None
    for entry in entries:
        if entry.get("file") == clip_name:
            found = entry.get("duration_s")
    try:
        seconds = float(found)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds <= 0:  # NaN or non-positive
        return None
    return seconds


def resolve_output(input_path: Path, output_arg: Path | None) -> Path:
    """Pick the output path; never defaults onto the input itself."""
    if output_arg is not None:
        return output_arg
    return input_path.with_name(input_path.stem + ".retimed.mp4")


def retime_factor(encoded_s: float, target_s: float) -> float:
    """Timestamp stretch factor that turns encoded_s into target_s."""
    return target_s / encoded_s


def already_realtime(encoded_s: float, target_s: float,
                     rel_tol: float = REL_TOL,
                     abs_floor: float = ABS_FLOOR) -> bool:
    """True when the clip is close enough to real time to leave alone."""
    return abs(encoded_s - target_s) <= max(rel_tol * target_s, abs_floor)


def build_command(exe: str, input_path: Path, output_path: Path,
                  factor: float) -> list[str]:
    """ffmpeg: re-time video with setpts, copy audio untouched."""
    return [
        exe, "-y",
        "-i", str(input_path),
        "-filter:v", f"setpts={factor:.6f}*PTS",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]


def _ffmpeg_run(cmd: list[str], timeout: int = 1800):
    """Run ffmpeg; CREATE_NO_WINDOW on Windows so no console flashes."""
    kwargs: dict = {"capture_output": True, "text": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)  # noqa: S603 - exe is the bundled binary


def retime(input_path: Path, target_s: float, output_path: Path,
           force: bool = False, dry_run: bool = False,
           probe=probe_video_duration,
           run=_ffmpeg_run,
           rel_tol: float = REL_TOL,
           abs_floor: float = ABS_FLOOR,
           details: dict | None = None) -> tuple[int, str]:
    """Repair one clip; return (exit_code, human-readable message).

    rel_tol/abs_floor shape the already-real-time no-op gate; batch callers
    (retime_all) pass the selection tolerance so a flagged clip is actually
    repaired instead of no-op'd.

    When ``details`` is a dict it is filled with ``encoded_s`` (the probed
    playback length, or None), ``stretch_factor`` (the setpts multiplier
    applied, or None when nothing was stretched), and ``output`` (the
    resolved output path) -- used by main() for the --json payload.
    """
    if details is not None:
        details.update(encoded_s=None, stretch_factor=None,
                       output=str(output_path))
    if output_path.exists() and not force:
        return 2, (
            f"{output_path} already exists -- pass --force to overwrite "
            "it or --output to pick another name"
        )
    if output_path.resolve() == input_path.resolve():
        return 2, "refusing to write the repaired clip over the input file"
    encoded_s = probe(input_path)
    if encoded_s is None:
        return 1, f"could not probe a video duration from {input_path}"
    if details is not None:
        details["encoded_s"] = encoded_s
    if already_realtime(encoded_s, target_s, rel_tol, abs_floor):
        return 0, (
            f"{input_path.name} already plays at real time "
            f"({encoded_s:.1f}s vs {target_s:.1f}s target) -- nothing to do"
        )
    factor = retime_factor(encoded_s, target_s)
    if (factor > MAX_FACTOR or factor < 1.0 / MAX_FACTOR) and not force:
        return 2, (
            f"stretch factor {factor:.1f}x looks insane "
            f"({encoded_s:.1f}s -> {target_s:.1f}s); pass --force if the "
            "target duration is really right"
        )
    exe = _ffmpeg_exe()
    if exe is None:
        return 1, "ffmpeg binary unavailable (imageio-ffmpeg not installed?)"
    cmd = build_command(exe, input_path, output_path, factor)
    if dry_run:
        if details is not None:
            details["stretch_factor"] = factor
        return 0, " ".join(cmd)
    try:
        proc = run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"ffmpeg failed to start: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        return 1, "ffmpeg failed:\n" + "\n".join(tail)
    out_s = probe(output_path)
    out_note = f"{out_s:.1f}s" if out_s is not None else "unprobable"
    if details is not None:
        details["stretch_factor"] = factor
    return 0, (
        f"{input_path.name}: {encoded_s:.1f}s -> {out_note} "
        f"({factor:.1f}x stretch), wrote {output_path}"
    )


def main(argv: list[str] | None = None,
         probe=probe_video_duration,
         run=_ffmpeg_run) -> int:
    parser = argparse.ArgumentParser(
        description="Repair a sped-up ghost-rec clip back to real-time "
        "playback (issue #1 follow-up)."
    )
    parser.add_argument("clip", type=Path, help="the MP4 to repair")
    parser.add_argument("--duration", type=float, default=None,
                        help="target wall-clock duration in seconds "
                        "(default: look it up in the session log)")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG,
                        help="sessions.jsonl path (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=None,
                        help="where to write the repaired clip "
                        "(default: <name>.retimed.mp4 next to the input)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing output file; also allow "
                        "extreme stretch factors")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the ffmpeg command without running it")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON result on stdout "
                        "(stdout stays pure JSON)")
    args = parser.parse_args(argv)

    if not args.clip.is_file():
        parser.error(f"clip not found: {args.clip}")
    if args.duration is not None and not args.duration > 0:
        parser.error("--duration must be a positive number of seconds")

    target_s = args.duration
    if target_s is None:
        target_s = lookup_duration(args.clip.name, args.log)
        if target_s is None:
            parser.error(
                f"no usable duration_s for {args.clip.name} in {args.log} "
                "-- pass --duration explicitly"
            )

    output_path = resolve_output(args.clip, args.output)
    details: dict = {}
    code, message = retime(args.clip, target_s, output_path,
                           force=args.force, dry_run=args.dry_run,
                           probe=probe, run=run, details=details)
    if args.json:
        payload = {
            "clip": str(args.clip),
            "output": details["output"],
            "target_s": target_s,
            "dry_run": args.dry_run,
            "exit_code": code,
            "encoded_s": details["encoded_s"],
            "stretch_factor": details["stretch_factor"],
            "message": message,
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    elif code == 0:
        print(message)
    else:
        print(f"retime_clip: {message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
