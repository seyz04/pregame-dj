# Pregame DJ

An auto-DJ for Apple Music on macOS. It plans a night along an energy curve (warm-up, build, peak, last call), picks songs to match, and mixes between them with a live audio engine written in Python.

## How it works
- **`dj.py`** controls Apple Music through AppleScript (`osascript`). It scores songs from a pool by energy and plays them along the curve with fades, plus a full-screen terminal display of what it's doing. It only reads playlists and controls playback; it never edits them.
- **`fx.py`** is the sound engine. Audio is routed Music → BlackHole (virtual audio device) → engine → speakers. It detects tempo, beat position and key with numpy, then applies filters, beat-synced echo, bass boost, loudness matching and a limiter.
- **`sync_pool.py`** / **`study_pool.py`** build the song pools from your library: a party pool, and a calmer study mode with focus and surge lanes.

## Things I learned building it
- Tempo detection on full-range audio mistakes hi-hat patterns for 2:3 tempos. Mixing the full-range and low-end (kick/808) onset envelopes, and scoring the half-beat lag, fixed it.
- Streamed Apple Music tracks are DRM-protected, so live analysis has to capture audio through a loopback device instead of reading files.
- Scripted edits to Apple Music playlists get wiped by iCloud sync, so the DJ treats playlists as read-only.

## Run
Requires macOS, Apple Music, [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole), and Python 3 with `numpy`, `scipy` and `sounddevice`.
```sh
./sync_pool.py "Party"  # read a playlist into a song pool
./dj.py --minutes 150   # play a 2.5-hour set
./dj.py --dry           # simulate a night without playing
```
