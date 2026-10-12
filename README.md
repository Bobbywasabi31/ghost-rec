# ghost-rec — private screen recorder (Windows)

Records your screen with **zero on-screen indication**: no window, no tray
icon, no notifications, no preview. Hotkey controlled. Files never leave
the machine.

## Setup (once)

1. Install Python 3.10+ from python.org (tick **"Add python.exe to PATH"**).
2. Open a terminal in this folder and run:
   ```
   pip install -r requirements.txt
   ```

## Run

```
pythonw ghost_rec.py
```

`pythonw` (not `python`) = no console window, ever. The only trace is
`pythonw.exe` in Task Manager and the video files it writes.

## Hotkeys

| Keys           | Action              |
|----------------|---------------------|
| Ctrl+Shift+R   | start / stop recording |
| Ctrl+Shift+A   | toggle audio capture (off by default) |
| Ctrl+Shift+Q   | quit the recorder   |

Change them at the top of `ghost_rec.py` (CONFIG section), along with FPS,
monitor choice, output folder, and quality.

## Files

Videos land in `%USERPROFILE%\Videos\ghost\` as
`ghost_YYYY-MM-DD_HH-MM-SS.mp4`. A small `ghost.log` there records
start/stop times only.

## Session log

Every finished session appends one JSON object to
`%USERPROFILE%\Videos\ghost\sessions.jsonl` (one per line), e.g.

```json
{"start": "2026-10-09T21:14:02", "stop": "2026-10-09T21:16:47", "duration_s": 165.2, "file": "ghost_2026-10-09_21-14-02.mp4", "size_bytes": 48210311, "audio": false}
```

Fields: local start/stop timestamps (ISO-8601, no timezone suffix),
duration in seconds, the MP4 file name, its final size in bytes
(`0` when the session produced no file), and whether an audio track
was captured for the session (`true` only when audio actually made it
into the file or a sidecar — a failed/empty capture logs `false`).

The log is local-only, append-only, and never leaves the machine —
it exists so you can review what got recorded and when. It keeps the
newest 5000 sessions; older lines are dropped automatically (atomic
rewrite, no half-file states).

`sessions_summary.py` (stdlib only) prints a one-page summary of the log:

```
python sessions_summary.py                # default log location above
python sessions_summary.py --log path\to\sessions.jsonl
python sessions_summary.py --json         # machine-readable JSON
python sessions_summary.py --csv          # spreadsheet-friendly CSV
python sessions_summary.py --from 2026-10-01 --to 2026-10-10   # date range
python sessions_summary.py --days 7       # last 7 days including today
```

`--from`/`--to` take `YYYY-MM-DD` (inclusive); garbage dates or `--from`
after `--to` exit with a usage error. `--days N` is shorthand for the last
N days including today (N ≥ 1; garbage exits 2) and just fills whichever
bound an explicit `--from`/`--to` doesn't set, so `--from 2026-10-01
--days 30` reads that day through today. Undated entries are excluded whenever
a range is set, and the report (or `--json`) notes what was excluded.

Under `--csv`, stdout is pure CSV (header `section,key,value`, with
`summary`, `largest`, and per-day `day_sessions`/`day_duration_s` rows), so
it pipes straight into a spreadsheet or pandas; log path, skipped lines,
and filter notes go to stderr instead. `--json` and `--csv` cannot be
combined.

`check_playback.py` (issue #1 follow-up) verifies each clip actually plays
back at real-time speed: it probes every MP4's encoded duration with the
bundled ffmpeg (no frame decoding) and compares it against the wall-clock
`duration_s` in the log. Sessions whose video is much shorter than the
logged capture span — the sped-up-playback symptom — are flagged
`SPED_UP`; much longer is `STRETCHED`; missing or unreadable files are
reported, never fatal:

```
python check_playback.py                  # default log location above
python check_playback.py --log path\to\sessions.jsonl
python check_playback.py --video-dir path\to\clips   # MP4s live elsewhere
python check_playback.py --tolerance 5    # relative drift tolerance, percent
python check_playback.py --json           # machine-readable report
python check_playback.py --csv            # spreadsheet-friendly CSV
python check_playback.py --from 2026-10-01 --to 2026-10-10  # date range
```

`--from`/`--to` (inclusive YYYY-MM-DD, one-sided ok) limit the check to
sessions starting in the range — same filter as `sessions_summary.py`.
Sessions outside the range aren't probed, and sessions with no usable
start date are excluded (and counted) while filtering.

MP4s are assumed to sit next to the log file unless `--video-dir` says
otherwise. Drift tolerance defaults to 10% with a 2-second absolute floor
so short sessions don't false-positive. `--csv` prints one row per session
(file, status, video_s, duration_s, drift_ratio) for spreadsheets or pandas;
log path, skipped lines, and filter notes go to stderr instead, and `--json`
and `--csv` cannot be combined. Exit code is 1 when any session
drifted, 0 when everything is within tolerance (or nothing was checkable),
2 on a usage error.

`retime_clip.py` (issue #1 follow-up) *repairs* a clip that
`check_playback.py` flagged: it re-times the video stream back to the
wall-clock capture span so the clip plays at real-time speed again. The
target duration comes from `sessions.jsonl` (matched by file name) unless
`--duration` is given explicitly. The video is re-encoded with stretched
timestamps (frames held as needed at 60fps); any audio track is already
in real time, so it is copied untouched — which also restores A/V sync.
The input file is never modified; the repaired clip lands next to it as
`<name>.retimed.mp4` unless `--output` says otherwise.

```
python retime_clip.py ghost_2026-10-11_12-40-01.mp4
python retime_clip.py clip.mp4 --duration 300
python retime_clip.py clip.mp4 --output fixed.mp4
python retime_clip.py clip.mp4 --dry-run      # print the ffmpeg command
python retime_clip.py clip.mp4 --force        # overwrite output / allow extreme stretch
```

Safety rails: refuses to write over the input or an existing output
without `--force`; does nothing when the clip already plays at real time
(same 10%/2s tolerance as `check_playback`); and treats a stretch factor
beyond 120x as a probable bad log entry, requiring `--force`. Exit code
is 1 when the clip can't be probed or ffmpeg fails, 2 on a usage error.

`retime_all.py` (issue #1 follow-up) repairs *every* clip that
`check_playback.py` flags `SPED_UP` in one pass — for when more than one
pre-fix recording needs repairing. It reuses `retime_clip`'s logic per
clip (same `<name>.retimed.mp4` outputs, same `--force`/`--dry-run`
semantics), skips outputs that already exist unless `--force`, and keeps
going when one clip fails. STRETCHED clips are left alone on purpose:
shrinking them could destroy legitimate content. The `--tolerance` used
for flagging also shapes the repair gate, so a clip flagged at a
stricter tolerance is actually repaired, not no-op'd:

```
python retime_all.py                  # default log location above
python retime_all.py --log path\to\sessions.jsonl
python retime_all.py --video-dir path\to\clips
python retime_all.py --dry-run        # report only, run no ffmpeg
python retime_all.py --force          # overwrite existing .retimed.mp4s
python retime_all.py --json           # machine-readable report
python retime_all.py --csv            # per-clip results as CSV (notes go to stderr so stdout stays pure)
python retime_all.py --from 2026-10-01 --to 2026-10-10  # only repair clips recorded in that date range (session start day, inclusive)
```

Exit code is 0 when there was nothing to repair or every repair
succeeded, 1 when at least one repair failed or was refused (details in
the report), 2 on a usage error.

## Audio (optional, off by default)

`Ctrl+Shift+A` toggles system-audio capture. It records **whatever plays
through your speakers** (WASAPI loopback of the default output device —
calls, videos, notification sounds), via the `sounddevice` package.
There is no microphone capture. The choice persists in
`audio.enabled` in the output folder.

When audio is on, each session's audio is muxed into the MP4 when the
session ends (video stream copied untouched, audio as AAC). Toggling
mid-session works: enabling starts audio from that moment (the track is
shifted to line up with the video); disabling stops it but keeps the
partial track; re-enabling restarts from that moment and discards the
earlier partial segment. If audio capture fails for any reason, the
session just records video — your video is never at risk.

## Tests

`tests/` runs the session/mux/toggle logic headless on Linux and Windows CI
(the matrix covers both; it targets Windows): stub
modules stand in for `mss`, `keyboard`, and `sounddevice` (which reports
no WASAPI, like any non-Windows host), and the real encoder
(imageio-ffmpeg) validates the MP4s. Hotkeys, real WASAPI capture, and
`pythonw` behaviour still need a Windows machine (see `docs/`).

```
pip install -r requirements.txt pytest
pytest -q
```

## Notes

- **How you know it's recording:** check the output folder — the MP4 for
  the current session grows while recording. Deliberately nothing else.
- **Privacy:** the script makes no network connections. The single
  localhost UDP bind is only a single-instance guard. Note: audio
  capture is loopback — it records everything your speakers play, so
  keep it off (the default) when you wouldn't want that on disk.
- **Auto-start (optional):** put a shortcut to
  `pythonw.exe "C:\path\to\ghost_rec.py"` in the Startup folder
  (`Win+R` → `shell:startup`).
- **Single .exe (optional):** `pip install pyinstaller`, then
  `pyinstaller --noconsole --onefile ghost_rec.py`.
- **Second copy:** launching it twice just exits silently — the running
  copy keeps working.
