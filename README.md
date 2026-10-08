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
