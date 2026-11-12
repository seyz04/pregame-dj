#!/usr/bin/env python3
"""Pregame DJ: picks songs from pool.json along an energy curve and mixes between them, with a live
full-screen display of what it's doing. Only controls playback; never edits playlists.

With the sound engine on (the default), Music plays through fx.py: the DJ hears each song's tempo, beat
and key, mixes on the beat (echo-outs, filter blends, drops) and favours songs whose key and tempo fit.

Keys while running:  n = next song now   + / - = more / less energy   d = door mode (anthems now)
                     h = hold the night where it is / let it follow the clock again   r = request a song
                     p = pause/resume   q = quit (music keeps playing)
Sound engine keys:   f = build-up, drops on the next bar   g = loop roll   e = echo throw   l = underwater filter on/off
                     b = extra bass on/off   [ / ] = quieter / louder   x = effects off/on (panic)
"""
import argparse, json, math, os, random, select, shutil, signal, subprocess, sys, termios, time, tty
from collections import deque

import fx
import sync_pool

HERE = os.path.dirname(os.path.abspath(__file__))
REQUESTS = os.path.join(HERE, "requests.txt")

# Song types: H house/groove, M mellow R&B, P pop/hyperpop, B bridge (rhythmic pop-rap), R rap, X rock/metal,
# C door anthems
PHASES = [  # (name, share of the night, bucket weights)
    ("warm-up", 0.20, {"H": 3, "M": 3, "P": 1}),
    ("build",   0.25, {"P": 4, "H": 1, "B": 1}),
    ("peak",    0.45, {"R": 5, "B": 2, "P": 2, "X": 1}),
    ("door",    0.10, {"C": 6, "R": 2, "P": 2}),
]
# Study mode (--mode study, pool study.json): A calm, F focus, J fire jazz, E surge (epic soundtrack / Worlds)
STUDY_PHASES = [
    ("settle",  0.15, {"A": 4, "F": 1}),
    ("lock in", 0.65, {"J": 4, "E": 3, "F": 3, "A": 1}),
    ("surge",   0.20, {"E": 4, "J": 3}),
]
LABEL = {"H": "house", "M": "R&B", "P": "pop", "B": "bridge", "R": "rap", "X": "rock", "C": "anthem",
         "A": "calm", "F": "focus", "J": "fire jazz", "E": "surge"}
COLOR = {"H": "36", "M": "35", "P": "95", "B": "33", "R": "91", "X": "31", "C": "92",
         "A": "36", "F": "94", "J": "93", "E": "91"}  # ANSI colors per song type
PHASE_COLOR = ["36", "95", "91", "92"]


def osa(script, *args):
    try:  # Music sometimes stops answering for a while; never let that freeze the DJ
        r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=4)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Music didn't answer within 4 s")
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def status():
    out = osa('''tell application "Music"
set s to player state as text
if s is "stopped" then return s
return s & "|" & (player position) & "|" & (duration of current track) & "|" & (persistent ID of current track) & "|" & (sound volume)
end tell''')
    if out == "stopped":
        return {"state": "stopped"}
    s, pos, dur, pid, vol = out.split("|")
    return {"state": s, "pos": float(pos), "dur": float(dur), "pid": pid, "vol": int(vol)}


def play(song):
    if song.get("playlist"):
        osa('''on run argv
tell application "Music" to play (first track of user playlist (item 1 of argv) whose persistent ID is (item 2 of argv))
end run''', song["playlist"], song["pid"])
    else:  # a request from the wider library
        osa('''on run argv
tell application "Music" to play (first track of library playlist 1 whose persistent ID is (item 1 of argv))
end run''', song["pid"])


def squash(text):
    return "".join(c for c in text.lower() if c.isalnum())


def search_library(query):
    """Best match for a typed request anywhere in the Music library, or None. Forgiving about spaces and
    punctuation ("crashout" finds "Crash Out")."""
    out = _search(query)
    if not out.strip() and len(squash(query)) >= 3:
        out = _search(squash(query)[:3])
    q, best = squash(query), None
    for l in out.splitlines():
        if l.count("|") != 4:
            continue
        pid, name, artist, genre, plays = l.split("|")
        n, a = squash(name), squash(artist)
        score = 3 if n == q else 2 if n.startswith(q) else 1.5 if q in n else 1 if q in a or q in n + a else 0
        if not score:
            continue
        score += math.log1p(int(plays or 0)) / 10
        if not best or score > best[0]:
            best = (score, {"pid": pid, "playlist": None, "name": name, "artist": artist, "genre": genre,
                            "plays": int(plays or 0), "bucket": sync_pool.bucket(name, genre)})
    return best[1] if best else None


def _search(query):
    return osa('''on run argv
tell application "Music"
set out to ""
set hits to (search library playlist 1 for (item 1 of argv))
repeat with i from 1 to (count of hits)
if i > 25 then exit repeat
set t to item i of hits
set out to out & (persistent ID of t) & "|" & (name of t) & "|" & (artist of t) & "|" & (genre of t) & "|" & (played count of t) & linefeed
end repeat
return out
end tell
end run''', query)


def set_volume(v):
    osa(f'tell application "Music" to set sound volume to {max(0, min(100, int(v)))}')


def get_volume():
    return int(osa('tell application "Music" to return sound volume'))


def fade(start, end, secs, on_step=None, steps=12):
    for i in range(1, steps + 1):
        v = start + (end - start) * i / steps
        set_volume(v)
        if on_step:
            on_step(v)
        time.sleep(secs / steps)


def mix_style(dj, nxt):
    """Pick how to get into the next song, the way a DJ would with one deck:
    fuse  - tempos fit: the old song's last bar keeps looping (bass cut) over the new song, locked to its
            beat, and fades out over 16 beats, so for a moment both songs play together
    blend - tempos and keys agree, so low-pass the old song away and let the new one open up
    slam  - anthems / door mode: build on the old song, echo the last beat, drop the new one on the one
    echo  - anything else: sweep the old song thin, echo it out on the beat, and the echo tail covers
            the tempo or key change while the new one loads"""
    if dj.study:
        return "fade", "study mode → slow crossfade, nothing that pulls focus"
    if dj.door or nxt["bucket"] == "C":
        return "slam", "anthem next → build-up, echo the last beat, drop it on the one"
    cur = dj.history[-1] if dj.history else None
    a, b = (dj.sound(cur), dj.sound(nxt)) if cur else (None, None)
    grid = dj.eng and dj.eng.beat_ref
    if a and b:
        gap = fx.tempo_gap(a["bpm"], b["bpm"])
        kd = fx.key_distance(a["camelot"], b["camelot"]) if a["camelot"] and b["camelot"] else None
        tempo = f"{a['bpm']:.0f}→{b['bpm']:.0f} bpm"
        if grid and gap <= 0.06 and (kd is None or kd <= 2) and random.random() < 0.75:
            return "fuse", f"{tempo}" + (f", {a['camelot']}→{b['camelot']}" if kd is not None else "") + \
                " fit → FUSE: loop the last bar over the new song, locked to its beat"
        if gap <= 0.04 and kd is not None and kd <= 1:
            return "blend", f"{tempo} and {a['camelot']}→{b['camelot']} fit → smooth filter blend"
        if gap > 0.04:
            return "echo", f"{tempo} is a {gap:.0%} tempo change → echo out so the grooves don't rub"
        if kd is not None and kd >= 3:
            return "echo", f"{a['camelot']}→{b['camelot']} keys clash → echo out, no overlap"
        return "echo", f"{tempo}, key unsure → echo out (safe choice)"
    if grid and a and random.random() < 0.6:
        return "fuse", "next song unknown → FUSE, and pull the loop out fast if the tempos don't fit"
    return "echo", "next song not analyzed yet → echo out (safe choice)"


def fx_transition(dj, eng, nxt, why, tick):
    """Mix into nxt on the beat. Blocks for the 4–8 beats of the transition plus the song's load time."""
    style, reason = mix_style(dj, nxt)
    dj.note(f"⇢ transition ({why}) → {nxt['name']}: {style.upper()}", "96")
    dj.note("  " + reason, "33")
    t, B = eng.next_beat(), eng.beats(1)
    try:
        if style == "fuse":
            eng.wait_until(t, tick)
            if not eng.grab_loop(4, end=t):
                style = "echo"
                dj.note("  couldn't cut a clean loop → echo out instead", "33")
            else:  # the loop picks up exactly where the song is, so the switch underneath is invisible
                eng.set("loop_hp", 0); eng.set("loop", 1); eng.ramp("in", 0, 0.02)
        if style == "fade":
            eng.ramp("in", 0, 4)
            eng.wait_until(eng.n_in + 4 * eng.sr, tick)
        elif style == "fuse":
            pass
        elif style == "blend":
            eng.ramp("lp", 0.8, 8 * B / eng.sr, at=t)
            eng.ramp("in", 0, 0.5 * B / eng.sr, at=t + 7.5 * B)
            eng.wait_until(t + 8 * B, tick)
        elif style == "slam":
            eng.ramp("hp", 0.75, 4 * B / eng.sr, at=t)
            eng.automate("echo", [(t + 3 * B, 0), (t + 3 * B + 64, 0.9), (t + 4 * B, 0.9), (t + 4 * B + 64, 0)],
                         start=t + 3 * B)
            eng.automate("in", [(t + 4 * B, 1), (t + 4 * B + 64, 0)], start=t + 4 * B)
            eng.wait_until(t + 4 * B, tick)
        else:
            eng.ramp("hp", 0.6, 8 * B / eng.sr, at=t)
            eng.ramp("echo", 0.8, 3 * B / eng.sr, at=t + 4 * B)
            eng.ramp("in", 0, B / eng.sr, at=t + 7 * B)
            eng.automate("echo", [(t + 4 * B, 0), (t + 7 * B, 0.8), (t + 8 * B, 0.8), (t + 8 * B + 64, 0)],
                         start=t + 4 * B)
            eng.wait_until(t + 8 * B, tick)
        st = eng.save_song()
        if st:
            dj.note(f"  learned: {st['name']} = {st['bpm']} bpm, {st['camelot']} ({st['key']})", "90")
        dj.start(nxt)  # calls eng.new_song once Music has really switched
        B = eng.beats(1)
        if style == "fade":
            eng.ramp("in", 1, 3)
        elif style == "fuse":
            if eng.loop:
                eng.loop["follow"] = True
            eng.ramp("in", 1, 0.05)
            eng.ramp("loop_hp", 0.45, 2 * B / eng.sr)        # bass swap: the new song's low end takes over
            eng.automate("loop", [(eng.n_in + 4 * B, 1), (eng.n_in + 16 * B, 0)])
        elif style == "blend":
            eng.set("lp", 0); eng.set("hp", 0.5)
            eng.ramp("in", 1, 0.05); eng.ramp("hp", 0, 8 * B / eng.sr)
        elif style == "slam":
            eng.set("hp", 0); eng.set("in", 1)
        else:
            eng.set("hp", 0.35)
            eng.ramp("in", 1, 0.05); eng.ramp("hp", 0, 4 * B / eng.sr)
    except Exception:
        for k in ("hp", "lp", "echo", "loop"):
            eng.set(k, 0)
        eng.set("in", 1)
        raise


class DJ:
    def __init__(self, pool, minutes, eng=None, logfile=None):
        self.pool, self.minutes, self.eng, self.logfile = pool, minutes, eng, logfile
        self.study = PHASES is STUDY_PHASES
        self.played, self.history = set(), []
        self.energy, self.door, self.t0 = 0, False, time.time()
        self.hold = None  # phase index the host locked the night at, or None to follow the clock
        self.rise = None  # (target phase, step per song): climb the hold gradually, e.g. build → peak
        self.log = deque(maxlen=300)
        self.analysis = eng.cache if eng else fx.load_analysis()

    def sound(self, song):
        """What's known about a song's tempo and key: live from the engine for the one playing now,
        otherwise from earlier listens (analysis.json)."""
        eng = self.eng
        if eng and eng.song and eng.song["pid"] == song["pid"] and eng.bpm:
            return {"bpm": eng.bpm, "camelot": eng.camelot if eng.key_conf >= 0.05 else None}
        a = self.analysis.get(song["pid"])
        if not a:
            return None
        return {"bpm": a["bpm"], "camelot": a["camelot"] if a.get("key_conf", 0) >= 0.05 else None}

    def fit(self, cand):
        """Mixing fit with the song playing now, from tempo (half/double time counts as the same groove)
        and the Camelot wheel. 1.0 is neutral; returns (factor, reason)."""
        if not self.history:
            return 1.0, ""
        a, b = self.sound(self.history[-1]), self.sound(cand)
        if not a or not b:
            return 1.0, ""
        f, why = 1.0, []
        gap = fx.tempo_gap(a["bpm"], b["bpm"])
        if gap <= 0.04:
            f, _ = f * 1.35, why.append(f"tempo {a['bpm']:.0f}→{b['bpm']:.0f} ✓")
        elif gap <= 0.08:
            f, _ = f * 1.1, why.append(f"tempo {a['bpm']:.0f}→{b['bpm']:.0f} ~")
        elif gap > 0.15:
            f, _ = f * 0.8, why.append(f"tempo {a['bpm']:.0f}→{b['bpm']:.0f} jump")
        if a["camelot"] and b["camelot"]:
            kd = fx.key_distance(a["camelot"], b["camelot"])
            mark = "✓" if kd <= 1 else ("~" if kd == 2 else "clash")
            f *= 1.3 if kd <= 1 else (1.0 if kd == 2 else 0.8)
            why.append(f"key {a['camelot']}→{b['camelot']} {mark}")
        return f, ", ".join(why)

    def note(self, msg, color="90"):
        self.log.append((time.strftime("%H:%M:%S"), msg, color))
        if self.logfile:
            self.logfile.write(f"{time.strftime('%H:%M:%S')}  {msg}\n")
            self.logfile.flush()

    def progress(self):
        return min(1.0, (time.time() - self.t0) / 60 / self.minutes)

    def phase(self):
        if self.door:
            return len(PHASES) - 1
        if self.hold is not None:
            return max(0, min(len(PHASES) - 1, int(self.hold) + self.energy))
        frac, acc = self.progress(), 0
        for i, (_, share, _) in enumerate(PHASES):
            acc += share
            if frac < acc:
                break
        return max(0, min(len(PHASES) - 1, i + self.energy))

    def pick(self):
        name, _, base_weights = PHASES[self.phase()]
        weights = dict(base_weights)
        frac = (self.hold % 1) if self.hold is not None and not self.door else 0
        if frac and self.phase() + 1 < len(PHASES):  # partway between two parts of the night: blend their lanes
            name, nxt = f"{name}→{PHASES[self.phase() + 1][0]} {frac:.0%}", PHASES[self.phase() + 1][2]
            weights = {k: (1 - frac) * base_weights.get(k, 0) + frac * nxt.get(k, 0)
                       for k in set(base_weights) | set(nxt)}
        last = self.history[-1]["bucket"] if self.history else None
        self.note(f"dj.pick()  phase={name}  weights=" + " ".join(f"{LABEL[k]}:{v}" for k, v in weights.items()), "94")
        # tempo bridges: half-time rap -> 128 pop/house goes through a bridge song, and mellow never jumps straight to rap
        if last == "R" and ("P" in weights or "H" in weights):
            weights = {"B": 5, **{k: v for k, v in weights.items() if k == "R"}}
            self.note("  rule: last song was half-time rap → bridge before jumping back to ~128 bpm", "33")
        if last == "M" and "R" in weights:
            weights.pop("R")
            self.note("  rule: coming off slow R&B → no hard rap yet", "33")
        recent = {s["artist"] for s in self.history[-4:]}
        cands = [s for s in self.pool if s["pid"] not in self.played and s["bucket"] in weights
                 and s["artist"] not in recent]
        if recent:
            self.note("  skip artists played recently: " + ", ".join(sorted(recent)), "90")
        if not cands:  # loosen up rather than run dry
            self.note("  no match left in this lane → opening up the whole pool", "33")
            cands = [s for s in self.pool if s["pid"] not in self.played]
            if not cands:
                self.note("  every song played → starting the pool over", "33")
                self.played.clear()
                cands = self.pool
        # roll for a lane (weights squared, so the main lane still dominates), then take the best song in it
        lanes = {s["bucket"] for s in cands if weights.get(s["bucket"], 0) > 0}
        if len(lanes) > 1:
            lane_list = sorted(lanes)
            w2 = [weights[b] ** 2 for b in lane_list]
            lane = random.choices(lane_list, w2)[0]
            self.note(f"  lane roll: {LABEL[lane]} ({w2[lane_list.index(lane)] / sum(w2):.0%} chance)", "90")
            cands = [s for s in cands if s["bucket"] == lane]
        scored = []
        for s in cands:
            fit, why = self.fit(s)
            scored.append((weights.get(s["bucket"], 0.3) * (1 + math.log1p(s["plays"]) / 2) * fit
                           * random.uniform(0.6, 1.4), s, why))
        sc, best, why = max(scored, key=lambda x: x[0])
        self.note(f'  {len(cands)} candidates → "{best["name"]}" ({LABEL[best["bucket"]]}, '
                  f'{best["plays"]} plays, score {sc:.2f})', COLOR[best["bucket"]])
        if why:
            self.note("  mix fit: " + why, "35")
        return best

    def start(self, song):
        """Play song and confirm Music really switched (streamed songs can take a few seconds to load).
        If it won't load, mark it played and try fresh picks so the music never stalls."""
        for _ in range(4):
            self.played.add(song["pid"])
            for _attempt in range(2):
                try:
                    play(song)
                except RuntimeError:
                    continue
                for _ in range(10):
                    time.sleep(0.4)
                    try:
                        if status().get("pid") == song["pid"]:
                            if self.eng:
                                self.eng.new_song(song)
                            self.history.append(song)
                            self.note(f'▶ now playing: {song["name"]} — {song["artist"]}', "97;1")
                            return song
                    except RuntimeError:
                        pass
            self.note(f'  "{song["name"]}" wouldn\'t load → picking another', "33")
            song = self.pick()
        return None

    def describe(self, song):
        return f'{song["name"]} — {song["artist"]}  [{LABEL[song["bucket"]]}]'


# ---------- live display ----------

def clock(secs):
    secs = max(0, int(secs))
    return f"{secs // 60}:{secs % 60:02d}"


def line(*segs, width):
    """Join (text, color) segments, cut to the terminal width without breaking color codes."""
    out, left = "", width
    for text, color in segs:
        text = text[:left]
        left -= len(text)
        out += f"\x1b[{color}m{text}\x1b[0m" if color else text
        if left <= 0:
            break
    return out + "\x1b[K"


def bar(frac, width, fill="█", empty="░"):
    n = int(round(max(0.0, min(1.0, frac)) * width))
    return fill * n + empty * (width - n)


class Screen:
    def __init__(self, dj, cut):
        self.dj, self.cut = dj, cut
        self.st, self.vol, self.nxt, self.size, self.typing = {}, 0, None, None, None

    def __enter__(self):
        sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def draw(self):
        cols, rows = shutil.get_terminal_size((100, 32))
        if cols < 40 or rows < 20:  # unknown or tiny window: draw at a sane size
            cols, rows = max(cols, 100), max(rows, 32)
        if (cols, rows) != self.size:
            sys.stdout.write("\x1b[2J")
            self.size = (cols, rows)
        w, dj, st = cols - 2, self.dj, self.st
        out = []
        add = lambda *segs: out.append(line(*segs, width=cols))

        elapsed = time.time() - dj.t0
        live = ("● LIVE", "91;1") if st.get("state") == "playing" else ("❚❚ PAUSED", "33;1")
        add(("  STUDY DJ  " if dj.study else "  PREGAME DJ  ", "30;47;1"), ("  ", None), live,
            (f"   {time.strftime('%H:%M:%S')}   night {clock(elapsed)} / {clock(dj.minutes * 60)}", "90"))
        add(("─" * w, "90"))

        song = dj.history[-1] if dj.history else None
        add(("  NOW PLAYING", "90;1"))
        if song:
            col = COLOR[song["bucket"]]
            add(("    " + song["name"], "97;1"))
            add(("    " + song["artist"], None), ("   [" + LABEL[song["bucket"]] + "]", col))
            if "dur" in st:
                bw = max(10, min(50, w - 34))
                to_fade = st["dur"] - st["pos"] - self.cut
                add(("    ", None), (bar(st["pos"] / max(1, st["dur"]), bw), col),
                    (f"  {clock(st['pos'])} / {clock(st['dur'])}", None),
                    (f"   fade in {clock(to_fade)}" if to_fade > 0 else "   fading…", "90"))
            else:
                add(("", None))
        else:
            add(("    starting…", "90")); add(("", None)); add(("", None))
        add(("", None))

        # energy curve: each phase as a colored stretch, with a marker for where the night is
        bw = max(20, min(64, w - 8))
        segs, marker = [], int(dj.progress() * (bw - 1))
        for i, (name, share, _) in enumerate(PHASES):
            n = max(1, round(share * bw))
            segs.append((("▆" if i == dj.phase() else "▂") * n, PHASE_COLOR[i]))
        cur = PHASES[dj.phase()][0]
        tag = "DOOR MODE" if dj.door else f"energy {dj.energy:+d}" + (
            (f" · RISING {dj.hold % 1:.0%}→{PHASES[dj.phase() + 1][0]}" if dj.hold % 1 else " · HOLD (h to release)")
            if dj.hold is not None else "")
        add(("  ENERGY CURVE  ", "90;1"), (f"now: {cur}", PHASE_COLOR[dj.phase()] + ";1"), (f"   ({tag})", "90"))
        add(("    ", None), *segs)
        add(("    " + " " * marker + "▲ you are here", "97"))
        labels = "".join(name.ljust(max(1, round(share * bw))) for name, share, _ in PHASES)
        add(("    " + labels, "90"))
        add(("", None))

        vw = max(10, min(40, w - 20))
        eng = dj.eng
        if eng:
            fl = eng.beat_flash()
            beats = " ".join(("●" if i == fl else "○") for i in range(4)) if fl is not None else "listening…"
            key = f"{eng.camelot} {eng.key}" + ("" if eng.key_conf >= 0.05 else "?") if eng.camelot else "key …"
            add(("  SOUND   ", "90;1"), (f"{eng.bpm:5.1f} bpm  " if eng.bpm else "  ... bpm  ", "97;1"),
                (beats, "91;1"), (f"   {key}", "35;1"), (f"   bass +{eng.bass_db:.1f} dB", "36"))
            lvl = (eng.level_db + 50) / 50
            hp, lp, echo = eng.value("hp"), eng.value("lp"), eng.value("echo")
            filt = []
            if hp > 0.02:
                filt.append(f"high-pass {20 * 60 ** hp:.0f} Hz")
            if lp > 0.02:
                filt.append(f"low-pass {20000 / 60 ** lp:.0f} Hz")
            fxs = "BYPASS (x to turn effects back on)" if eng.bypass else (
                "  ".join(filt + ([f"echo {echo:.0%}"] if echo > 0.02 else [])
                          + ([f"LOOP {eng.value('loop'):.0%}"] if eng.loop and eng.value("loop") > 0.02 else [])
                          + (["+4 dB bass"] if eng.p["bass_extra"] else [])) or "clean")
            add(("  LEVEL   ", "90;1"), (bar(lvl, vw, "▮", "▯"), "32"),
                (f"  limiter {eng.lim_db:4.1f} dB" if eng.lim_db < -0.5 else "", "33"))
            add(("  FX      ", "90;1"), (fxs, "93" if fxs != "clean" else "90"))
            add(("  VOLUME  ", "90;1"), (bar(eng.value("master") / 1.5, vw, "▮", "▯"), "32"),
                (f"  {eng.value('master') * 100:.0f}%   → {eng.out_name}", None),
                (f"   ⚠ {eng.err}" if eng.err else "", "91"))
        else:
            add(("  VOLUME  ", "90;1"), (bar(self.vol / 100, vw, "▮", "▯"), "32"), (f"  {int(self.vol)}", None))
        if self.nxt:
            add(("  UP NEXT ", "90;1"), (" " + self.nxt["name"] + " — " + self.nxt["artist"], None),
                ("  [" + LABEL[self.nxt["bucket"]] + "]", COLOR[self.nxt["bucket"]]))
        add(("  SET     ", "90;1"), (f" {len(dj.played)} played · {len(dj.pool) - len(dj.played)} left in the pool", None))
        add(("─" * w, "90"))
        add(("  DJ BRAIN  ", "90;1"), ("live decision log", "90"))

        footer = 2
        room = max(0, rows - len(out) - footer)
        for t, msg, color in list(dj.log)[-room:]:
            add((f"  {t}  ", "90"), (msg, color))
        while len(out) < rows - footer:
            add(("", None))
        add(("─" * w, "90"))
        if self.typing is not None:
            out.append(line(("  REQUEST ▸ ", "30;43;1"), (" " + self.typing + "▌", "97;1"),
                            ("   Enter = add   Esc = cancel", "90"), width=cols))
            sys.stdout.write("\x1b[H" + "\r\n".join(out) + "\x1b[J")
            sys.stdout.flush()
            return
        keys = [("r", "request"), ("n", "next"), ("h", "hold"), ("+/-", "energy"), ("d", "door"), ("p", "pause"), ("q", "quit")]
        if dj.eng:
            keys += [("f", "build"), ("g", "roll"), ("e", "echo"), ("l", "underwater"), ("b", "bass"), ("[ ]", "vol"), ("x", "fx off")]
        out.append(line(*[seg for k, label in keys for seg in ((f"  {k}", "97;1"), (f" {label} ", "90"))], width=cols))
        sys.stdout.write("\x1b[H" + "\r\n".join(out) + "\x1b[J")
        sys.stdout.flush()


def key_pressed():
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.read(1)
    return None


def dry_run(pool, minutes):
    dj = DJ(pool, minutes)
    t = 0
    while t < minutes * 60:
        dj.t0 = time.time() - t
        s = dj.pick()
        dj.played.add(s["pid"]); dj.history.append(s)
        print(f'{int(t // 60):4} min  {PHASES[dj.phase()][0]:8} {dj.describe(s)}')
        t += 210 - 14  # ~3:30 songs, minus the outro cut


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=150, help="length of the pregame (default 150)")
    ap.add_argument("--cut", type=float, default=14, help="seconds before a song ends to start the transition")
    ap.add_argument("--dry", action="store_true", help="print a simulated night, play nothing")
    ap.add_argument("--pool", default="pool", help="song pool file in this folder, without .json (e.g. yer)")
    ap.add_argument("--no-fx", action="store_true", help="plain volume fades, no sound engine")
    ap.add_argument("--out", help="speaker to play through (default: a JBL if connected)")
    ap.add_argument("--mode", choices=["party", "study"], default="party",
                    help="study: calm/focus/surge lanes, gentle crossfades, quieter start (default pool study.json)")
    ap.add_argument("--hold", help="stay in this part of the night (h releases), e.g. build, peak, lock in")
    ap.add_argument("--level", type=float, help="exact spot to hold at: 0 warm-up, 1 build, 2 peak, 1.5 halfway")
    ap.add_argument("--resume", action="store_true", help="unpause the song Music is paused on and carry on")
    ap.add_argument("--rise", help="climb the hold to here, a quarter step per song")
    ap.add_argument("--request", help="mix straight into this song (searched in the whole library)")
    ap.add_argument("--log", help="also write the DJ's decisions to this file")
    ap.add_argument("--volume", type=float, default=50, help="starting volume in %% with the engine (default 50)")
    args = ap.parse_args()
    global PHASES
    if args.mode == "study":
        PHASES = STUDY_PHASES
        args.pool = "study" if args.pool == "pool" else args.pool
        args.volume = 20 if args.volume == 50 else args.volume  # in-ear: start quiet

    pool = json.load(open(os.path.join(HERE, args.pool + ".json"), encoding="utf-8"))
    if args.dry:
        return dry_run(pool, args.minutes)

    base = get_volume()
    eng = None
    # closing the Terminal window or Ctrl-C still runs the clean-up below, so the Mac's sound comes back
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))
    old_tty = termios.tcgetattr(sys.stdin)
    try:
        if not args.no_fx:
            print("Starting the sound engine…")
            fx.route_to_blackhole()
            eng = fx.Engine(args.out)
            eng.set("master", 0)
            eng.start()
            set_volume(100)  # Music sends full level into the engine; loudness is set by [ ] and the speaker
            eng.ramp("master", args.volume / 100, 4)
        dj = DJ(pool, args.minutes, eng, open(args.log, "a", encoding="utf-8") if args.log else None)
        if args.log and os.path.exists(args.log):  # remember what already played in the last 6 hours
            cutoff, now = time.strftime("%H:%M:%S", time.localtime(time.time() - 6 * 3600)), time.strftime("%H:%M:%S")
            recent = (lambda t: cutoff <= t <= now) if cutoff <= now else (lambda t: t >= cutoff or t <= now)  # past midnight
            names = {l.split("▶ now playing: ", 1)[1].rsplit(" — ", 1)[0].strip()
                     for l in open(args.log, encoding="utf-8") if "▶ now playing: " in l and recent(l[:8])}
            dj.played |= {s["pid"] for s in pool if s["name"] in names}
            if names:
                dj.note(f"remembering {len(names)} songs already played tonight", "90")
        if args.hold:
            dj.hold = [p[0] for p in PHASES].index(args.hold)
        if args.level is not None:  # resume a climb partway, e.g. 1.5 = halfway from build to peak
            dj.hold = args.level
        if args.rise and dj.hold is not None:
            dj.rise = ([p[0] for p in PHASES].index(args.rise), 0.25)
        tty.setcbreak(sys.stdin.fileno())
        with Screen(dj, args.cut) as screen:
            screen.vol = base
            dj.note(f"booting: {len(pool)} songs loaded, {args.minutes:g}-minute night planned", "92")
            if eng:
                known = sum(s["pid"] in dj.analysis for s in pool)
                dj.note(f"sound engine: BlackHole → {eng.out_name} at {eng.sr} Hz; "
                        f"{known}/{len(pool)} songs already analyzed", "92")
            now = status()
            playing = None
            if args.resume and now.get("state") == "paused":  # carry on with the song that was paused
                osa('tell application "Music" to play')
                now = status()
            if now.get("state") == "playing":
                playing = next((s for s in pool if s["pid"] == now.get("pid")), None)
                if not playing:  # a request from the wider library: pick it up too rather than cutting it off
                    try:
                        name, artist, genre = osa('''tell application "Music"
return (name of current track) & "|" & (artist of current track) & "|" & (genre of current track)
end tell''').split("|")[:3]
                        playing = {"pid": now["pid"], "playlist": None, "name": name, "artist": artist,
                                   "genre": genre, "plays": 0, "bucket": sync_pool.bucket(name, genre)}
                    except (RuntimeError, ValueError):
                        playing = None
            if playing:  # pick up where Music already is instead of cutting it off
                dj.played.add(playing["pid"]); dj.history.append(playing)
                if eng:
                    eng.new_song(playing)
                dj.note(f'▶ picking up mid-song: {playing["name"]} — {playing["artist"]}', "97;1")
            else:
                dj.start(dj.pick())
            screen.nxt = dj.pick()

            def on_fade(v):
                screen.vol = v
                screen.draw()

            queue, requested = [], False  # requests after the one up next; whether up next is a request

            def up_next():
                nonlocal requested
                requested = bool(queue)
                if queue:
                    return queue.pop(0)
                if dj.rise and dj.hold is not None and dj.hold < dj.rise[0]:
                    dj.hold = min(dj.rise[0], dj.hold + dj.rise[1])
                    dj.note(f"rising: {dj.hold - int(dj.hold):.0%} of the way to {PHASES[int(dj.hold) + 1][0]}"
                            if dj.hold % 1 else f"rising: reached {PHASES[int(dj.hold)][0]}", "95;1")
                return dj.pick()

            def request(query):
                """Put a song in line: yer first (exact-ish name), then the whole library.
                A leading ! makes it play next, ahead of everything already in line."""
                nonlocal requested
                jump = query.startswith("!")
                query = query.lstrip("!").strip()
                q = squash(query)
                req = next((s for s in pool if squash(s["name"]).startswith(q)), None)
                if not req:
                    try:
                        req = search_library(query)
                    except RuntimeError:
                        req = None
                if not req:
                    dj.note(f'request: nothing in your library matches "{query}"', "91")
                    return None
                queue[:] = [s for s in queue if s["pid"] != req["pid"]]  # no doubles if it was already in line
                if jump and requested:
                    if screen.nxt["pid"] != req["pid"]:
                        queue.insert(0, screen.nxt)
                    screen.nxt = req
                    dj.note(f'request: "{query}" → {req["name"]} — {req["artist"]} jumps the line, up next', "92;1")
                elif not requested:
                    screen.nxt, requested = req, True
                    dj.note(f'request: "{query}" → {req["name"]} — {req["artist"]} is up next (n = now)', "92;1")
                else:
                    queue.append(req)
                    dj.note(f'request: "{query}" → {req["name"]} — {req["artist"]}, #{len(queue) + 1} in line', "92;1")
                return req

            def transition(why):
                nonlocal base
                nxt = screen.nxt
                if eng:
                    fx_transition(dj, eng, nxt, why, screen.draw)
                else:
                    dj.note(f"⇢ transition ({why}): fade out → {nxt['name']}", "96")
                    fade(base, base * 0.3, 5, on_fade)
                    dj.start(nxt)
                    fade(base * 0.3, base, 2.5, on_fade)
                screen.nxt = up_next()

            if args.request and request(args.request):
                transition("request")
            last_poll, host_paused, stalled_since, silent_since, last_nudge = 0, False, None, None, 0
            while True:
                k = key_pressed()
                if screen.typing is not None:  # request box: letters go into the box, not to the DJ
                    if k in ("\r", "\n"):
                        query, screen.typing = screen.typing.strip(), None
                        if query:
                            request(query)
                    elif k == "\x1b":
                        screen.typing = None
                    elif k in ("\x7f", "\b"):
                        screen.typing = screen.typing[:-1]
                    elif k and k.isprintable():
                        screen.typing += k
                    k = None
                elif k == "r":
                    screen.typing, k = "", None
                if k == "q":
                    break
                if k and k in "+-":
                    dj.energy += 1 if k == "+" else -1
                    dj.note(f"host: energy {dj.energy:+d} → re-planning up next", "97")
                    if not requested:
                        screen.nxt = dj.pick()
                if k == "h":
                    dj.hold = None if dj.hold is not None else dj.phase() - dj.energy
                    dj.note("host: " + (f"holding the night at {PHASES[dj.phase()][0]}" if dj.hold is not None
                                        else "hold released, following the clock again"), "97")
                    if not requested:
                        screen.nxt = dj.pick()
                if k == "d":
                    dj.door = not dj.door
                    dj.note("host: door mode " + ("ON → anthems next" if dj.door else "off"), "97")
                    if not requested:
                        screen.nxt = dj.pick()
                if k == "p":
                    host_paused = not host_paused
                    try:
                        osa('tell application "Music" to playpause')
                    except RuntimeError:
                        pass
                if eng and k:
                    fx_key(dj, eng, k)
                if k or time.time() - last_poll > 0.4:
                    last_poll = time.time()
                    if os.path.exists(REQUESTS):  # one song per line, added from outside (e.g. by Claude)
                        try:
                            lines = open(REQUESTS, encoding="utf-8").read().splitlines()
                            os.remove(REQUESTS)
                        except OSError:
                            lines = []
                        for l in lines:
                            if l.strip().lower().startswith("rise "):  # e.g. "rise peak": climb a quarter per song
                                target = [p[0] for p in PHASES].index(l.split()[1])
                                dj.hold = dj.phase() if dj.hold is None else dj.hold
                                dj.rise = (target, 0.25)
                                dj.note(f"the night will climb to {PHASES[target][0]} a quarter step per song", "95;1")
                            elif l.strip().lower() == "clear":  # drop every request; the DJ picks again
                                queue.clear()
                                requested = False
                                screen.nxt = dj.pick()
                                dj.note("requests cleared → the DJ has the wheel", "92;1")
                            elif l.strip():
                                request(l.strip())
                    try:
                        screen.st = status()
                    except RuntimeError:
                        screen.st = {}
                    state = screen.st.get("state")
                    mine = dj.history and screen.st.get("pid") == dj.history[-1]["pid"]
                    if state == "playing":
                        host_paused = False
                    # Music went off on its own (played something else, or stopped) and the host didn't pause it
                    if (state == "playing" and not mine) or (state in ("paused", "stopped") and not host_paused):
                        stalled_since = stalled_since or time.time()
                        if time.time() - stalled_since > 3:
                            dj.note("Music went off-script (" + (f'playing "{screen.st.get("pid")}"' if state == "playing"
                                    else state) + ") → taking back over", "91")
                            stalled_since = None
                            transition("recovering")
                            continue
                    else:
                        stalled_since = None
                    # Music says it's playing but nothing reaches the engine: Music is stuck on a vanished output
                    # device (e.g. eqMac's, which goes away when the DJ starts). A pause/play makes it follow BlackHole.
                    if eng and state == "playing" and eng.level_db < -60:
                        silent_since = silent_since or time.time()
                        if time.time() - silent_since > 4 and time.time() - last_nudge > 20:
                            dj.note("Music is playing but no sound is reaching the engine → nudging Music's output", "91")
                            try:
                                osa('tell application "Music" to pause'); time.sleep(0.5)
                                osa('tell application "Music" to play')
                            except RuntimeError:
                                pass
                            last_nudge, silent_since = time.time(), None
                    else:
                        silent_since = None
                    if state == "playing" and mine:
                        if not eng:
                            base = screen.st["vol"]  # follow manual volume changes
                            screen.vol = base
                        remaining = screen.st["dur"] - screen.st["pos"]
                        if k == "n" or remaining <= args.cut:
                            transition("host skipped" if k == "n" else f"{int(remaining)}s left")
                screen.draw()
                time.sleep(0.08 if eng else 0.4)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
        try:
            set_volume(base)  # before the sound goes back to the speaker directly, so it isn't at 100
        except RuntimeError:
            pass
        if eng:
            eng.save_song()
            eng.stop()
        if not args.no_fx:
            fx.restore_audio()
        print("DJ off. Music keeps playing" + (f", sound is back on {fx._switch('-c', '-t', 'output')}." if eng else "."))


def fx_key(dj, eng, k):
    """Live effect keys. Everything lands on the beat grid so it stays musical."""
    if k == "f":  # build-up: high-pass rises over 8 beats with an echo on the last one, then the drop
        t = eng.next_beat()
        eng.automate("hp", [(t + eng.beats(8), 0.7), (t + eng.beats(8) + 64, 0)], start=t)
        eng.automate("echo", [(t + eng.beats(7), 0), (t + eng.beats(7) + 64, 0.7), (t + eng.beats(8), 0.7),
                              (t + eng.beats(8) + 64, 0)], start=t + eng.beats(7))
        dj.note("host: build-up → drops in 8 beats", "93")
    elif k == "g":  # loop roll: half a beat repeated for 4 beats, then back to the song on the one
        t = eng.next_beat()
        if eng.grab_loop(0.5):
            eng.automate("loop", [(t, 0), (t + 1, 1), (t + eng.beats(4), 1), (t + eng.beats(4) + 64, 0)], start=t)
            eng.automate("in", [(t, 1), (t + 64, 0), (t + eng.beats(4), 0), (t + eng.beats(4) + 64, 1)], start=t)
            eng.set("loop_hp", 0)
            dj.note("host: loop roll", "93")
        else:
            dj.note("host: loop roll needs the beat first, try again in a few seconds", "33")
    elif k == "e":  # echo throw: one beat into the echo, which repeats on its own
        t = eng.next_beat()
        eng.automate("echo", [(t + 64, 0.9), (t + eng.beats(1), 0.9), (t + eng.beats(1) + 64, 0)], start=t)
        dj.note("host: echo throw", "93")
    elif k == "l":
        on = eng.value("lp") < 0.3
        eng.ramp("lp", 0.65 if on else 0, 2 * eng.beat(), at=eng.next_beat())
        dj.note("host: underwater " + ("on" if on else "off"), "93")
    elif k == "b":
        eng.ramp("bass_extra", 0 if eng.p["bass_extra"] else 4, 0.5)
        dj.note("host: extra bass " + ("off" if eng.value("bass_extra") else "on"), "93")
    elif k in "[]":
        eng.ramp("master", min(1.5, max(0.02, eng.value("master") * (1.19 if k == "]" else 0.84))), 0.2)
    elif k == "x":
        eng.bypass = not eng.bypass
        dj.note("host: effects " + ("OFF (straight through)" if eng.bypass else "back on"), "93")


if __name__ == "__main__":
    main()
