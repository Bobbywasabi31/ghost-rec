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
| Ctrl+Shift+Q   | quit the recorder   |

Change them at the top of `ghost_rec.py` (CONFIG section), along with FPS,
monitor choice, output folder, and quality.

## Files

Videos land in `%USERPROFILE%\Videos\ghost\` as
`ghost_YYYY-MM-DD_HH-MM-SS.mp4`. A small `ghost.log` there records
start/stop times only.

## Notes

- **How you know it's recording:** check the output folder — the MP4 for
  the current session grows while recording. Deliberately nothing else.
- **Privacy:** the script makes no network connections. The single
  localhost UDP bind is only a single-instance guard.
- **Auto-start (optional):** put a shortcut to
  `pythonw.exe "C:\path\to\ghost_rec.py"` in the Startup folder
  (`Win+R` → `shell:startup`).
- **Single .exe (optional):** `pip install pyinstaller`, then
  `pyinstaller --noconsole --onefile ghost_rec.py`.
- **Second copy:** launching it twice just exits silently — the running
  copy keeps working.
