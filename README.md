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

`tests/` runs the session/mux/toggle logic headless on Linux CI: stub
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
