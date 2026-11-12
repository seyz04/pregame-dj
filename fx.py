#!/usr/bin/env python3
"""Live sound engine for the Pregame DJ. Hears Music through BlackHole, works out the tempo, where the
beat lands and the key, and plays it on to the speaker with DJ effects: filter sweeps, beat-synced echo,
automatic bass, even loudness between songs and a limiter.

    Music ─▶ BlackHole 2ch ─▶ this engine ─▶ speaker (JBL, or the Mac's own speakers)

Usage:  ./fx.py listen [--out JBL]   pass-through test with the live readout (Ctrl-C to stop)
        ./fx.py learn [--pool yer]   silently scan songs for tempo/key/loudness (saved to analysis.json)
        ./fx.py restore              put the Mac's sound back if a crash left it on BlackHole
"""
import argparse, json, os, subprocess, sys, threading, time
import numpy as np
import sounddevice as sd
from scipy.signal import lfilter

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYSIS = os.path.join(HERE, "analysis.json")
RESTORE = os.path.join(HERE, ".audio_restore.json")
BLACKHOLE = "BlackHole 2ch"
SWITCH = "/opt/homebrew/bin/SwitchAudioSource"
TARGET_DB = -18.0  # loudness every song is evened out to (RMS of what Music sends at full volume)

# ---------- music theory ----------

PITCHES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
# Krumhansl–Kessler key profiles: how strongly each scale degree is felt in a major / minor key
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
# Camelot wheel number of each major key (by pitch class); its relative minor shares the number
CAMELOT_MAJOR = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}


def camelot(pc, minor):
    return f"{CAMELOT_MAJOR[(pc + 3) % 12] if minor else CAMELOT_MAJOR[pc]}{'A' if minor else 'B'}"


def key_name(pc, minor):
    return PITCHES[pc] + (" minor" if minor else " major")


def key_distance(a, b):
    """Steps apart on the Camelot wheel: 0 same key, 1 neighbour or relative major/minor (mixes cleanly),
    2 is a noticeable shift, 3+ clashes."""
    na, la, nb, lb = int(a[:-1]), a[-1], int(b[:-1]), b[-1]
    d = min((na - nb) % 12, (nb - na) % 12)
    return d if la == lb else d + 1


def tempo_gap(a, b):
    """Fractional tempo difference, treating half/double time as the same groove (70 bpm ≈ 140 bpm)."""
    r = np.log2(max(a, b) / min(a, b))
    return float(2 ** abs(r - round(r)) - 1)


# ---------- analysis ----------

def frames(x, n, hop):
    f = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    return f * np.hanning(n).astype(np.float32)


def onset_env(x, sr, n=2048, hop=512):
    """Spectral flux: how much new sound starts in each ~10 ms frame. Peaks are drum hits.
    Returns the full-range envelope (for tempo), a low-end one (kicks and 808s, for where the beat
    lands) and the share of power below 120 Hz."""
    spec = np.abs(np.fft.rfft(frames(x, n, hop), axis=1))
    mag = np.log1p(100 * spec)
    lo = int(120 * n / sr) + 1
    envs = []
    for band in (mag, mag[:, 1:lo + 2]):
        flux = np.maximum(0, np.diff(band, axis=0)).sum(axis=1)
        flux -= np.convolve(flux, np.ones(16) / 16, mode="same")
        flux = np.maximum(flux, 0)
        envs.append(flux / (flux.std() + 1e-9))  # same scale for both, so they can be mixed
    power = spec ** 2
    bass_ratio = float(power[:, 1:lo].sum() / (power.sum() + 1e-12))
    return envs[0], envs[1], sr / hop, bass_ratio


def tempo(env, fps, lo=68, hi=180):
    """BPM from the autocorrelation of the onset envelope, gently preferring tempos near 120."""
    e = env - env.mean()
    ac = np.correlate(e, e, "full")[len(e) - 1:]
    ac /= ac[0] + 1e-9
    bpms = np.arange(lo, hi, 0.25)
    lags = 60 * fps / bpms
    grid = np.arange(len(ac))
    # a real beat also repeats at two beats and splits into eighth notes; a 2:3 "fake" tempo doesn't split
    score = (np.interp(lags, grid, ac) + 0.5 * np.interp(2 * lags, grid, ac, right=0)
             + 0.25 * np.interp(lags / 2, grid, ac))
    prior = np.exp(-0.5 * (np.log2(bpms / 120) / 0.9) ** 2)
    i = int(np.argmax(score * prior))
    return float(bpms[i]), float(score[i])


def last_beat(env, fps, bpm):
    """Frame of the most recent beat: the beat grid offset whose ticks line up with the most drum hits
    (recent hits count more)."""
    p = 60 * fps / bpm
    k = np.arange(int((len(env) - 1) / p))
    if len(k) < 4:
        return None
    phis = np.arange(0, p, 0.5)
    idx = phis[:, None] + k[None, :] * p
    vals = np.interp(idx.ravel(), np.arange(len(env)), env).reshape(idx.shape)
    phi = phis[int(np.argmax((vals * np.linspace(0.5, 1, len(k))).sum(axis=1)))]
    return phi + int((len(env) - 1 - phi) / p) * p


def chroma(x, sr, n=8192, hop=4096):
    """Energy in each of the 12 pitch classes, from 60 Hz to 2 kHz."""
    spec = np.abs(np.fft.rfft(frames(x, n, hop), axis=1)).sum(axis=0)
    f = np.fft.rfftfreq(n, 1 / sr)
    m = (f > 60) & (f < 2000)
    pc = np.round(69 + 12 * np.log2(f[m] / 440)).astype(int) % 12
    weight = np.where(f[m] < 250, 2.0, 1.0)  # bass notes usually carry the chord's root
    return np.bincount(pc, weights=spec[m] * weight, minlength=12)


def detect_key(c):
    """Krumhansl–Schmuckler: correlate the pitch-class profile with all 24 rotated key profiles."""
    scores = sorted(((np.corrcoef(c, np.roll(prof, pc))[0, 1], pc, minor)
                     for minor, prof in ((0, MAJOR), (1, MINOR)) for pc in range(12)), reverse=True)
    s, pc, minor = scores[0]
    return pc, bool(minor), float(s - scores[1][0])


def load_analysis():
    try:
        return json.load(open(ANALYSIS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------- filters (RBJ audio-EQ cookbook biquads) ----------

def biquad(kind, f, sr, q=0.707, db=0.0):
    w = 2 * np.pi * min(f, sr * 0.45) / sr
    cw, sw = np.cos(w), np.sin(w)
    if kind == "shelf":
        A = 10 ** (db / 40)
        al = sw / 2 * np.sqrt(2)
        sa = 2 * np.sqrt(A) * al
        b = [A * ((A + 1) - (A - 1) * cw + sa), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sa)]
        a = [(A + 1) + (A - 1) * cw + sa, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sa]
    else:
        al = sw / (2 * q)
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2] if kind == "hp" else [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + al, -2 * cw, 1 - al]
    return np.array(b) / a[0], np.array(a) / a[0]


# ---------- audio routing ----------

def _switch(*args):
    r = subprocess.run([SWITCH, *args], capture_output=True, text=True)
    return r.stdout.strip()


def eqmac_running():
    return subprocess.run(["pgrep", "-x", "eqMac"], capture_output=True).returncode == 0


def route_to_blackhole():
    """Send the Mac's sound into BlackHole so the engine hears it. eqMac must be quit first or it pulls its
    device out from under us. Everything needed to undo this is saved to .audio_restore.json."""
    if os.path.exists(RESTORE):
        restore_audio()
    current = _switch("-c", "-t", "output")
    state = {"output": current, "real": current.replace(" (eqMac)", ""), "eqmac": eqmac_running()}
    json.dump(state, open(RESTORE, "w"))
    if state["eqmac"]:
        subprocess.run(["osascript", "-e", 'quit app "eqMac"'], capture_output=True)
        for _ in range(20):
            if "(eqMac)" not in _switch("-a", "-t", "output"):
                break
            time.sleep(0.25)
    _switch("-t", "output", "-s", BLACKHOLE)
    return state


def restore_audio():
    try:
        state = json.load(open(RESTORE))
    except (OSError, ValueError):
        return False
    outs = _switch("-a", "-t", "output").splitlines()
    target = state["real"] if state["real"] in outs and state["real"] != BLACKHOLE else "MacBook Air Speakers"
    _switch("-t", "output", "-s", target)
    if state["eqmac"] and not eqmac_running():
        subprocess.run(["open", "-g", "-a", "eqMac"])
        for _ in range(40):  # eqMac recreates its device a few seconds after launch; go back through it
            if state["output"] in _switch("-a", "-t", "output").splitlines():
                _switch("-t", "output", "-s", state["output"])
                break
            time.sleep(0.25)
    os.remove(RESTORE)
    return True


def find_devices(out_hint=None):
    """(BlackHole input index, speaker output index, speaker name). Prefers a JBL if one is connected."""
    sd._terminate(); sd._initialize()  # refresh: the device list changes when eqMac quits or the JBL connects
    devs = sd.query_devices()
    bh = next(i for i, d in enumerate(devs) if d["name"] == BLACKHOLE and d["max_input_channels"] >= 2)
    outs = [(i, d["name"]) for i, d in enumerate(devs) if d["max_output_channels"] >= 2
            and d["name"] != BLACKHOLE and "eqMac" not in d["name"]]
    try:
        real = json.load(open(RESTORE))["real"]
    except (OSError, ValueError, KeyError):
        real = None
    for want in (out_hint, "JBL", real, "MacBook Air Speakers"):
        for i, name in outs:
            if want and want.lower() in name.lower():
                return bh, i, name
    raise RuntimeError("no speaker found; outputs are: " + ", ".join(n for _, n in outs))


# ---------- the engine ----------

class Engine:
    PARAMS = {"gain": 1.0, "in": 1.0, "hp": 0.0, "lp": 0.0, "echo": 0.0, "master": 0.5, "bass_extra": 0.0,
              "loop": 0.0, "loop_hp": 0.0}

    def __init__(self, out_hint=None, listen_only=False, block=1024):
        self.in_dev, self.out_dev, self.out_name = find_devices(out_hint)
        self.listen_only, self.block = listen_only, block
        self.p, self.ramps = dict(self.PARAMS), {}
        self.bypass, self.running, self.xruns, self.err = False, False, 0, None
        self.cache = load_analysis()
        # live readout
        self.bpm = self.beat_ref = self.key = self.camelot = None
        self.key_conf = self.bpm_conf = 0.0
        self.level_db, self.bass_db, self.bass_ratio, self.lim_db = -90.0, 0.0, None, 0.0
        self.song = None

    # -- lifecycle --
    def start(self):
        for sr in (48000, 44100):
            try:
                self._open(sr)
                break
            except sd.PortAudioError as e:
                self.err = str(e)
        else:
            raise RuntimeError("couldn't open audio: " + self.err)
        self.err = None
        self.running = True
        threading.Thread(target=self._analyze_loop, daemon=True).start()

    def _open(self, sr):
        self.sr = sr
        self.ring = np.zeros(sr * 32, np.float32)
        self.n_in = self.song_t0 = 0
        self.dl = np.zeros((sr * 3, 2), np.float32)
        self.dl_w, self.lim_g = 0, 1.0
        self.sring = np.zeros((sr * 20, 2), np.float32)  # stereo history, for cutting loops
        self.loop = None
        self.zi = {k: np.zeros((2, 2)) for k in ("shelf", "hp", "lp", "ehp")}
        self.zi["damp"] = np.zeros((1, 2))
        self.zi["loop_hp"] = np.zeros((2, 2))
        self.ehp = biquad("hp", 250, sr)
        self.new_song(None)
        if self.listen_only:
            self.stream = sd.InputStream(device=self.in_dev, samplerate=sr, channels=2, blocksize=self.block,
                                         dtype="float32", callback=lambda i, f, t, s: self._capture(i, f, s))
        else:
            self.stream = sd.Stream(device=(self.in_dev, self.out_dev), samplerate=sr, channels=2,
                                    blocksize=self.block, dtype="float32", latency="high", callback=self._cb)
        self.stream.start()

    def stop(self):
        self.running = False
        try:
            self.stream.stop(); self.stream.close()
        except Exception:
            pass

    # -- control (called from the DJ's thread) --
    def value(self, k, t=None):
        """A parameter's value at sample time t, following its automation (straight lines between points)."""
        r = self.ramps.get(k)
        if r is None:
            return self.p[k]
        t = self.n_in if t is None else t
        ts, vs = r
        if t >= ts[-1]:
            self.p[k] = vs[-1]
            if self.ramps.get(k) is r:
                self.ramps.pop(k, None)
            return vs[-1]
        return float(np.interp(t, ts, vs))

    def automate(self, k, points, start=None):
        """Replace k's automation with [(sample time, value), ...]. It holds its current value until
        `start` (default now), then moves in straight lines through the points."""
        t0 = self.n_in if start is None else int(start)
        ts = [t0] + [max(t0, int(t)) for t, _ in points]
        vs = [self.value(k, t0)] + [float(v) for _, v in points]
        self.ramps[k] = (ts, vs)

    def ramp(self, k, target, secs, at=None):
        t0 = self.n_in if at is None else int(at)
        self.automate(k, [(t0 + max(1, int(secs * self.sr)), target)], start=t0)

    def set(self, k, v):
        self.ramps.pop(k, None)
        self.p[k] = float(v)

    def beats(self, n):
        """n beats in samples."""
        return n * self.beat() * self.sr

    def beat(self):
        """Seconds per beat (falls back to 120 bpm until the tempo is known)."""
        return 60 / (self.bpm or 120)

    def next_beat(self, after=0.0):
        """Sample time of the first beat at least `after` seconds from now."""
        now = self.n_in + after * self.sr
        if not self.beat_ref:
            return now
        per = self.beat() * self.sr
        return self.beat_ref + np.ceil((now - self.beat_ref) / per) * per

    def wait_until(self, t, tick=None):
        deadline = time.time() + max(0, (t - self.n_in) / self.sr) + 2  # never hang if audio stalls
        while self.n_in < t and self.running and time.time() < deadline:
            if tick:
                tick()
            time.sleep(0.03)

    # -- per-song state --
    def new_song(self, song):
        """Called when Music switches songs: forget the old song's measurements, load what's known."""
        self.song, self.song_t0 = song, self.n_in
        self.chroma_acc, self.rms_hist, self.bpm_hist, self.bass_hist = np.zeros(12), [], [], []
        self.chroma_upto, self.key_conf, self.bpm_conf = self.n_in, 0.0, 0.0
        self.bpm = self.beat_ref = self.key = self.camelot = None
        known = self.cache.get(song["pid"]) if song else None
        if known:
            self.bpm, self.camelot, self.key = known["bpm"], known["camelot"], known["key"]
            if "gain_db" in known:
                self.ramp("gain", 10 ** (known["gain_db"] / 20), 0.5)

    def song_stats(self):
        """Summary of what was heard of the current song, or None if not enough to trust."""
        heard = (self.n_in - self.song_t0) / self.sr
        if not self.song or heard < 12 or not self.bpm_hist or not self.rms_hist:
            return None
        pc, minor, conf = detect_key(self.chroma_acc)
        rms_db = float(np.median(self.rms_hist))
        return {"name": self.song["name"], "bpm": round(float(np.median(self.bpm_hist)), 1),
                "key": key_name(pc, minor), "camelot": camelot(pc, minor), "key_conf": round(conf, 3),
                "rms_db": round(rms_db, 1), "gain_db": round(float(np.clip(TARGET_DB - rms_db, -6, 6)), 1),
                "bass_ratio": round(float(np.median(self.bass_hist)), 3), "heard_s": int(heard)}

    def save_song(self):
        st = self.song_stats()
        if st:
            old = self.cache.get(self.song["pid"])
            if not old or old.get("heard_s", 0) <= st["heard_s"]:
                self.cache[self.song["pid"]] = st
                tmp = ANALYSIS + ".tmp"
                json.dump(self.cache, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                os.replace(tmp, ANALYSIS)
        return st

    # -- audio thread --
    def _capture(self, x, frames_, status):
        if status:
            self.xruns += 1
        n0, L = self.n_in, len(self.ring)
        mono = x.mean(axis=1)
        i = n0 % L
        if i + frames_ <= L:
            self.ring[i:i + frames_] = mono
            if not self.listen_only:
                j = n0 % len(self.sring)
                idx = (j + np.arange(frames_)) % len(self.sring)
                self.sring[idx] = x
        else:
            k = L - i
            self.ring[i:], self.ring[:frames_ - k] = mono[:k], mono[k:]
            if not self.listen_only:
                idx = (n0 + np.arange(frames_)) % len(self.sring)
                self.sring[idx] = x
        self.n_in = n0 + frames_
        return n0

    def _cb(self, indata, outdata, frames_, t, status):
        n0 = self._capture(indata, frames_, status)
        v = {k: self.value(k, n0 + frames_ // 2) for k in self.p}
        if self.bypass:
            outdata[:] = indata * v["master"]
            return
        sr = self.sr
        y = indata * (v["gain"] * v["in"])
        b, a = biquad("shelf", 90, sr, db=self.bass_db + v["bass_extra"])
        y, self.zi["shelf"] = lfilter(b, a, y, axis=0, zi=self.zi["shelf"])
        b, a = biquad("hp", 20 * 60 ** v["hp"], sr, q=0.9)          # 0 → 20 Hz (open) … 1 → 1.2 kHz
        y, self.zi["hp"] = lfilter(b, a, y, axis=0, zi=self.zi["hp"])
        b, a = biquad("lp", 20000 / 60 ** v["lp"], sr, q=0.9)       # 0 → 20 kHz (open) … 1 → 330 Hz
        y, self.zi["lp"] = lfilter(b, a, y, axis=0, zi=self.zi["lp"])
        # beat-synced echo: a dotted eighth note (3/4 of a beat), bass filtered out, darkening as it repeats
        L = len(self.dl)
        d = int(np.clip(0.75 * self.beat() * sr, frames_ + 1, L - 1))
        idx = (self.dl_w + np.arange(frames_)) % L
        wet = self.dl[(idx - d) % L]
        send, self.zi["ehp"] = lfilter(*self.ehp, y * v["echo"], axis=0, zi=self.zi["ehp"])
        fb, self.zi["damp"] = lfilter([0.65], [1, -0.35], wet * 0.55, axis=0, zi=self.zi["damp"])
        self.dl[idx] = np.tanh(send + fb)
        self.dl_w = (self.dl_w + frames_) % L
        y = y + wet
        if self.loop is not None and v["loop"] > 0.001:
            ls, self.zi["loop_hp"] = lfilter(*biquad("hp", 20 * 60 ** v["loop_hp"], sr), self._loop_block(n0, frames_),
                                             axis=0, zi=self.zi["loop_hp"])
            y = y + ls * v["loop"]
        y = y * v["master"]
        # limiter: instant attack, slow release, then a hard ceiling as a last resort
        peak = float(np.abs(y).max())
        target = min(1.0, 0.89 / peak) if peak > 0 else 1.0
        g1 = target if target < self.lim_g else min(1.0, self.lim_g + (1 - self.lim_g) * 0.03)
        y *= np.linspace(self.lim_g, g1, frames_)[:, None]
        self.lim_g, self.lim_db = g1, 20 * np.log10(g1)
        outdata[:] = np.clip(y, -1, 1)

    # -- analysis thread --
    def window(self, secs):
        end = self.n_in
        n = int(min(secs * self.sr, end - self.song_t0, len(self.ring) - self.sr))
        if n <= 0:
            return end, np.zeros(0, np.float32)
        i = (end - n) % len(self.ring)
        x = np.concatenate([self.ring[i:], self.ring[:max(0, i + n - len(self.ring))]])[:n]
        return end - n, x

    def _analyze_loop(self):
        while self.running:
            time.sleep(0.5)
            try:
                self._analyze()
            except Exception as e:  # keep the music going whatever happens here
                self.err = f"analysis: {e}"

    def _analyze(self):
        sr = self.sr
        _, last = self.window(0.5)
        if len(last):
            rms = float(np.sqrt(np.mean(last ** 2)))
            self.level_db = 20 * np.log10(rms + 1e-9)
            if self.level_db > -45:
                self.rms_hist.append(self.level_db)
                if not (self.song and self.song["pid"] in self.cache) and len(self.rms_hist) >= 16:
                    self.ramp("gain", 10 ** (np.clip(TARGET_DB - np.median(self.rms_hist), -6, 6) / 20), 2)
        lp = self.loop
        if lp is not None:
            if self.value("loop") <= 0.001 and "loop" not in self.ramps:
                self.loop = None
            elif lp["follow"] and self.bpm and not lp.get("dropped") and tempo_gap(self.bpm, lp["bpm"]) > 0.06:
                lp["dropped"] = True
                self.ramp("loop", 0, 1.0)  # tempos don't fit: pull the loop out fast instead of trainwrecking
        start, x = self.window(12)
        if len(x) < sr * 5 or self.level_db < -45:
            return
        env, env_lo, fps, bass_ratio = onset_env(x, sr)
        bpm, conf = tempo(env + env_lo, fps)  # kicks/808s count fully, so hi-hat patterns can't fake a 2:3 tempo
        if conf > 0.1:
            self.bpm_hist.append(bpm)
            if len(self.bpm_hist) >= 3 or not self.bpm:
                self.bpm, self.bpm_conf = float(np.median(self.bpm_hist[-12:])), conf
        if self.bpm:
            f = last_beat(env_lo + 0.25 * env, fps, self.bpm)
            if f is not None:
                self.beat_ref = start + int((f + 1) * (sr / fps) + 1024)  # flux frame → sample at window centre
        # bass: thin-sounding songs get up to +5 dB at 90 Hz, songs already heavy on 808s get none
        self.bass_hist.append(bass_ratio)
        self.bass_ratio = float(np.median(self.bass_hist[-20:]))
        target = float(np.clip((0.40 - self.bass_ratio) * 20, 0, 5))
        self.bass_db += (target - self.bass_db) * 0.2
        # key: add each new 4 s of audio to the song's pitch-class profile
        if self.n_in - self.chroma_upto >= 4 * sr:
            _, chunk = self.window((self.n_in - self.chroma_upto) / sr)
            self.chroma_upto = self.n_in
            if len(chunk) > 8192 * 2:
                self.chroma_acc += chroma(chunk, sr)
                pc, minor, kc = detect_key(self.chroma_acc)
                self.key, self.camelot, self.key_conf = key_name(pc, minor), camelot(pc, minor), kc

    # -- loops: cut from what just played, replayed on the beat (fusing songs, loop rolls) --
    def grab_loop(self, beats, end=None):
        """Cut the last `beats` beats ending on a beat (default: the most recent one) out of the stereo
        history. Returns False if the beat grid isn't known yet."""
        if not self.beat_ref or not self.bpm:
            return False
        per = self.beat() * self.sr
        end = int(end if end is not None else self.next_beat() - per)
        n = int(beats * per)
        if end > self.n_in or end - n < self.n_in - len(self.sring) + self.block:
            return False
        idx = (end - n + np.arange(n)) % len(self.sring)
        buf = self.sring[idx].copy()
        f = min(144, n // 8)  # 3 ms fades so the loop point doesn't click
        buf[:f] *= np.linspace(0, 1, f)[:, None]; buf[-f:] *= np.linspace(1, 0, f)[:, None]
        self.loop = {"buf": buf, "beats": beats, "per_src": per, "ref": end, "bpm": self.bpm, "follow": False}
        return True

    def _loop_block(self, n0, frames_):
        lp = self.loop
        ref, per = lp["ref"], lp["per_src"]
        if lp["follow"] and self.beat_ref and self.bpm:  # lock onto the new song's beat grid
            k = round(np.log2(self.bpm / lp["bpm"]))      # half/double time: one old beat = 2^k new beats
            ref, per = self.beat_ref, self.beat() * self.sr * 2 ** k
        phase = ((n0 + np.arange(frames_) - ref) / per) % lp["beats"]
        pos = phase * lp["per_src"]
        i = pos.astype(int)
        frac = (pos - i)[:, None]
        buf = lp["buf"]
        return buf[i % len(buf)] * (1 - frac) + buf[(i + 1) % len(buf)] * frac

    def beat_flash(self):
        """0..3: which beat of the bar we're on (bar position is a guess), or None."""
        if not self.beat_ref:
            return None
        per = self.beat() * self.sr
        return int(np.floor((self.n_in - self.beat_ref) / per)) % 4

    def readout(self):
        bpm = f"{self.bpm:5.1f} bpm" if self.bpm else "  ... bpm"
        key = f"{self.camelot} ({self.key})" if self.camelot else "key …"
        return f"{bpm}   {key}   bass +{self.bass_db:.1f} dB   level {self.level_db:5.1f} dB"


# ---------- command line ----------

def cmd_listen(args):
    route_to_blackhole()
    eng = Engine(args.out)
    try:
        eng.start()
        eng.ramp("master", args.master, 3)
        print(f"BlackHole → {eng.out_name} at {eng.sr} Hz. Play something in Music. Ctrl-C to stop.")
        while True:
            fl = eng.beat_flash()
            dots = "".join("●" if i == fl else "○" for i in range(4)) if fl is not None else "    "
            print(f"\r{dots}  {eng.readout()}  xruns {eng.xruns}  {eng.err or ''}\x1b[K", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()
        restore_audio()
        print("\nSound is back on", json.dumps(_switch("-c", "-t", "output")))


def cmd_learn(args):
    import dj
    pool = json.load(open(os.path.join(HERE, args.pool + ".json"), encoding="utf-8"))
    cache = load_analysis()
    todo = [s for s in pool if s["pid"] not in cache]
    known = len(pool) - len(todo)
    todo = todo[: args.limit or None]
    print(f"{known} of {len(pool)} songs already known; scanning {len(todo)} "
          f"(~{len(todo) * (args.secs + 6) / 60:.0f} min, silent: Music plays into BlackHole only)")
    if not todo:
        return
    vol0, state0 = dj.get_volume(), dj.status().get("state")
    route_to_blackhole()
    eng = Engine(listen_only=True)
    try:
        eng.start()
        dj.set_volume(100)  # loudness is measured at full Music volume, the same level the DJ plays at
        for n, s in enumerate(todo, 1):
            try:
                dj.play(s)
                for _ in range(20):
                    time.sleep(0.4)
                    st = dj.status()
                    if st.get("pid") == s["pid"]:
                        break
                else:
                    print(f"[{n}/{len(todo)}] {s['name']}: wouldn't load, skipped")
                    continue
                dj.osa(f'tell application "Music" to set player position to {max(30, st["dur"] * 0.35):.0f}')
                time.sleep(1)
                eng.new_song(s)
                time.sleep(args.secs)
                r = eng.save_song()
                print(f"[{n}/{len(todo)}] {s['name']} — {s['artist']}: " +
                      (f"{r['bpm']} bpm, {r['camelot']} {r['key']}, loudness {r['rms_db']} dB" if r else "too quiet to read"))
            except RuntimeError as e:
                print(f"[{n}/{len(todo)}] {s['name']}: {e}")
    except KeyboardInterrupt:
        print("\nstopped; everything scanned so far is saved")
    finally:
        try:
            dj.osa('tell application "Music" to pause')
            dj.set_volume(vol0)
        except RuntimeError:
            pass
        eng.stop()
        restore_audio()
        print("Sound is back on", _switch("-c", "-t", "output"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("listen"); p.add_argument("--out"); p.add_argument("--master", type=float, default=0.5)
    p = sub.add_parser("learn"); p.add_argument("--pool", default="yer")
    p.add_argument("--secs", type=float, default=14); p.add_argument("--limit", type=int)
    sub.add_parser("restore")
    args = ap.parse_args()
    if args.cmd == "listen":
        cmd_listen(args)
    elif args.cmd == "learn":
        cmd_learn(args)
    else:
        print("restored" if restore_audio() else "nothing to restore", "→", _switch("-c", "-t", "output"))


if __name__ == "__main__":
    main()
