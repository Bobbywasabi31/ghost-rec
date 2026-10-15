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
    python retime_clip.py clip.mp4 --verify       # re-probe the repaired output
                                                  # and confirm it plays at real time

The ffmpeg binary bundled with imageio-ffmpeg (already a ghost-rec
dependency) does the re-encode; CREATE_NO_WINDOW is used on Windows so no
console flashes.

With --json the payload (clip, output, target_s, exit_code, encoded_s,
stretch_factor, message, verification) goes to stdout as pure JSON, mirroring
the other ghost-rec report tools; without it, the human-readable message goes
to stdout on success and stderr on failure.

--verify closes the single-clip loop the way retime_all.py --verify does for
batches: the repaired output is re-probed and checked against the target
duration with the same tolerance. A clip that needed no repair is verified
against the input itself (it just passed the real-time gate); --dry-run and
failed repairs report "skipped".

Exit codes: 0 = repaired (or already real time, nothing to do),
1 = the clip couldn't be probed, ffmpeg failed, or --verify found the
output still drifted/unprobable,
2 = usage error (bad duration, overwrite refused, insane stretch).
"""

from __future__ import annotations

import argparse
import csv
import io
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


def verify_output(check_path: Path, target_s: float,
                  probe=probe_video_duration,
                  rel_tol: float = REL_TOL,
                  abs_floor: float = ABS_FLOOR) -> tuple[str, float | None, str]:
    """Re-probe a repaired clip; confirm it now plays at real time.

    Single-clip mirror of retime_all.verify_repairs' per-clip semantics:
    "verified" when the file probes within tolerance of target_s, "drifted"
    when it probes outside it (the repair didn't land), "unprobable" when it
    can't be probed, "skipped" when the file doesn't exist. Returns
    (status, video_s, note). Callers pick which file to check -- the repaired
    output after a real repair, the input itself after an already-real-time
    no-op (nothing was written).
    """
    if not check_path.exists():
        return "skipped", None, "no output file was written"
    video_s = probe(check_path)
    if video_s is None:
        return "unprobable", None, f"could not probe {check_path.name}"
    if already_realtime(video_s, target_s, rel_tol, abs_floor):
        return ("verified", video_s,
                f"{check_path.name} plays at {video_s:.1f}s vs "
                f"{target_s:.1f}s target -- real time")
    return ("drifted", video_s,
            f"{check_path.name} plays at {video_s:.1f}s vs "
            f"{target_s:.1f}s target -- still drifted")


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
    applied, or None when nothing was stretched), ``output`` (the
    resolved output path), and ``repaired`` (True only when the repair
    actually ran -- the already-real-time no-op writes nothing) -- used by
    main() for the --json payload and --verify.
    """
    if details is not None:
        details.update(encoded_s=None, stretch_factor=None,
                       output=str(output_path), repaired=False)
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
        details["repaired"] = True
    return 0, (
        f"{input_path.name}: {encoded_s:.1f}s -> {out_note} "
        f"({factor:.1f}x stretch), wrote {output_path}"
    )


def format_csv(payload: dict) -> str:
    """Render the single-clip result as a spreadsheet-friendly CSV.

    One header row (clip,output,exit_code,target_s,encoded_s,
    stretch_factor,dry_run,verification,message), then one data row.
    Durations are one-decimal seconds, stretch_factor 3 decimals (empty when
    None, e.g. already-real-time no-ops and probe failures); verification is
    the --verify status word (verified/drifted/unprobable/skipped, empty when
    --verify wasn't used); plain decimals so spreadsheets and pandas parse
    cleanly; stdlib csv handles quoting, so commas in messages round-trip.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["clip", "output", "exit_code", "target_s", "encoded_s",
                     "stretch_factor", "dry_run", "verification", "message"])
    encoded = payload["encoded_s"]
    factor = payload["stretch_factor"]
    verification = payload.get("verification") or {}
    writer.writerow([
        payload["clip"],
        payload["output"] or "",
        payload["exit_code"],
        f"{payload['target_s']:.1f}",
        f"{encoded:.1f}" if encoded is not None else "",
        f"{factor:.3f}" if factor is not None else "",
        "true" if payload["dry_run"] else "false",
        verification.get("status", ""),
        payload["message"],
    ])
    return buf.getvalue()


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
    parser.add_argument("--verify", action="store_true",
                        help="re-probe the repaired output and confirm it "
                        "plays at real time (a no-op repair verifies the "
                        "input; --dry-run and failed repairs report skipped)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON result on stdout "
                        "(stdout stays pure JSON)")
    parser.add_argument("--csv", action="store_true",
                        help="machine-readable CSV result on stdout "
                        "(stdout stays pure CSV)")
    args = parser.parse_args(argv)

    if args.json and args.csv:
        parser.error("--json and --csv cannot be combined")

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

    verification = None
    if args.verify:
        if args.dry_run:
            verification = {"status": "skipped", "video_s": None,
                            "note": "--dry-run wrote no files"}
        elif code != 0:
            verification = {"status": "skipped", "video_s": None,
                            "note": "repair did not succeed"}
        else:
            # A no-op repair writes nothing, so verify the input itself
            # (it just passed the real-time gate); a real repair is
            # verified against the output file.
            check_path = (output_path if details.get("repaired")
                          else args.clip)
            status, video_s, note = verify_output(check_path, target_s,
                                                  probe=probe)
            verification = {"status": status, "video_s": video_s,
                            "note": note}
        if verification["status"] in ("drifted", "unprobable"):
            code = 1

    if args.json or args.csv:
        payload = {
            "clip": str(args.clip),
            "output": details["output"],
            "target_s": target_s,
            "dry_run": args.dry_run,
            "exit_code": code,
            "encoded_s": details["encoded_s"],
            "stretch_factor": details["stretch_factor"],
            "verification": verification,
            "message": message,
        }
        if args.json:
            json.dump(payload, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(format_csv(payload))
    else:
        stream = sys.stdout if code == 0 else sys.stderr
        if code == 0:
            print(message, file=stream)
        else:
            print(f"retime_clip: {message}", file=stream)
        if verification is not None:
            print(f"verify: {verification['note']}", file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
