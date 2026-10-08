# ghost-rec optional audio capture — 2026-10-08

**Status: implemented, logic-validated on Linux. NOT tested on Windows —
needs Alex's machine before it can be called working.**

## What it does

Optional system-audio capture for ghost-rec, off by default, toggled with
**Ctrl+Shift+A**. The preference persists in `%USERPROFILE%\Videos\ghost\audio.enabled`
(survives restarts). When on, each session records the default output
device's mix (WASAPI loopback) to a temp WAV alongside the video, then muxes
it into the MP4 when the session ends (video stream copied untouched, audio
re-encoded AAC 128k). The MP4 filename never changes; the mux is atomic via
`os.replace`.

## Design decisions

- **WASAPI loopback via `sounddevice`** (`sounddevice` added to
  requirements.txt), not `pycaw`: one `pip install`, PortAudio is bundled
  in the Windows wheel, no COM plumbing in our code. Loopback is opened as
  an *input* stream on the default *output* device with
  `sd.WasapiSettings(loopback=True)` at the device's native sample rate,
  stereo float32, converted to 16-bit PCM and streamed to disk with the
  stdlib `wave` module (no `soundfile` dependency).
- **Opt-in, off by default.** Loopback captures everything the speakers
  play — calls, meetings, notification sounds. The README says so plainly.
- **Audio never risks the video.** Capture runs on its own thread; any
  audio failure (no `sounddevice`, no loopback device, read error,
  mux failure) logs loudly and the session continues/records video-only.
  Mux failure keeps the video plus a `*_audio.wav` sidecar.
- **No new dependencies for the mux**: uses the `imageio-ffmpeg` binary
  already required for video. The ffmpeg subprocess runs with
  `CREATE_NO_WINDOW` on Windows so no console ever flashes (that would
  break ghost's whole point).
- **Mid-session toggle semantics** (documented in README): enabling
  mid-session starts audio from that moment — the track is shifted with
  `-itsoffset` so it lines up with the video. Disabling mid-session stops
  capture but keeps the partial track for the session-end mux. Re-enabling
  discards the earlier partial segment (logged) and restarts from that
  moment. At most one audio segment per session is ever muxed.
- **Sync is approximate**: the offset is wall-clock (session start vs
  audio-thread start), device/capture latency not compensated. Good
  enough for "what was playing during this recording", not for
  lip-sync-critical use.

## Validation done (Linux, headless — /tmp/test_ghost_audio.py, throwaway)

Stubbed `mss` (synthetic frames), `keyboard`, and `sounddevice` (fake
WASAPI loopback producing silence); real imageio + imageio-ffmpeg for
video encode AND the audio mux. 24/24 checks:

- toggle flips the preference and persists `audio.enabled` (on/off)
- full session with audio on: final MP4 has an AAC audio stream, 90 video
  frames decode, temp WAV cleaned up, mux logged, no sidecar
- mid-session enable→disable: partial audio muxed, MP4 duration equals the
  full video (short audio does NOT truncate the video)
- re-enable mid-session: earlier partial discarded (logged), new segment
  muxed fine
- `sounddevice` missing: session records video-only, failure loudly logged
- mux failure (ffmpeg unavailable): video kept + `*_audio.wav` sidecar kept

One harness bug found during validation (zero frames because the test
didn't start the grab supervisor) — test-side only, not in the shipped code.

## Windows-test gaps (needs Alex's machine)

- [ ] Real WASAPI loopback: does `sd.query_devices(kind="output")` +
      `WasapiSettings(loopback=True)` open on his hardware/driver stack?
      Sample rates other than 48 kHz; mono vs stereo mix formats.
- [ ] `pip install sounddevice` pulls the PortAudio DLL correctly on his
      Python; `--noconsole --onefile` PyInstaller build still works with
      the new dependency.
- [ ] Ctrl+Shift+A under `pythonw` — registers, no conflict with his apps,
      preference survives restarts.
- [ ] Real mux on Windows: bundled ffmpeg binary, `-itsoffset` alignment
      sounds right, `CREATE_NO_WINDOW` truly flashes nothing.
- [ ] CPU/load: 60fps x264 + loopback capture + mux at session end on his
      machine; long-session soak (30+ min) with audio on.
- [ ] Confirm the privacy posture is what he wants: loopback on = every
      sound his PC makes lands in the file.
