"""Note-event export / re-import, MIDI writer and WAV synthesis.

The JSON event format is the round-trip artifact: files written here can be
read back with :func:`events_to_arrangement` and re-synthesized. MIDI uses a
dependency-free writer (format 0, 480 PPQ). WAV synthesis uses numpy when
available and a small pure-stdlib fallback otherwise.
"""

from __future__ import annotations

import contextlib
import math
import struct
import wave
from dataclasses import dataclass

from .solver import SolveResult, Segment, ArrangementInput
from .theory import VOICES, VOICE_RANGES, Chord, pitch_to_name

EVENT_FORMAT_VERSION = 1

# Simple choral-ish stereo placement per voice.
VOICE_PAN = {"soprano": -0.25, "alto": -0.08, "tenor": 0.08, "bass": 0.25}
VOICE_MIDI_CHANNEL = {"soprano": 0, "alto": 1, "tenor": 2, "bass": 3}


# ---------------------------------------------------------------------------
# Event JSON (round-trip format)
# ---------------------------------------------------------------------------

def arrangement_to_events(result: SolveResult) -> dict:
    """Convert a solve result to re-readable note events (durations in beats)."""
    events = []
    cursor = 0.0
    for seg in result.segments:
        seg_meta = result.input_snapshot["segments"][seg.index]
        durations = seg_meta["duration"]
        breath = float(seg_meta.get("breath_after", 0.0) or 0.0)
        sounding = max(0.125, float(durations) - breath)
        for i, voice in enumerate(VOICES):
            midi = seg.voicing[i]
            events.append({
                "event_id": f"{seg.index}:{voice}",
                "segment": seg.index,
                "voice": voice,
                "midi": midi,
                "name": pitch_to_name(midi, result.key_root_pc),
                "start_beat": round(cursor, 6),
                "duration_beats": float(durations),
                "sounding_beats": round(sounding, 6),
                "breath_after_beats": breath,
                "start_time": None,  # filled when a tempo is used for audio
                "duration_time": None,
                "velocity": 78,
                "chord_label": seg.chord.label or seg.chord.symbol,
                "fixed": voice in result.input_snapshot["segments"][seg.index].get("fixed", {}),
            })
        cursor += float(durations)
    return {
        "format": "chorale_arranger_events",
        "version": EVENT_FORMAT_VERSION,
        "key_root_pc": result.key_root_pc,
        "style": result.style,
        "seed": result.seed,
        "beam_width": result.beam_width,
        "total_beats": cursor,
        "voices": list(VOICES),
        "voice_ranges": {v: list(VOICE_RANGES[v]) for v in VOICES},
        "segments": result.input_snapshot["segments"],
        "events": events,
        "status": result.status,
        "total_cost": result.total_cost if math.isfinite(result.total_cost) else None,
        "cost_breakdown": result.cost_breakdown,
        "violations": result.violations,
    }


def events_to_arrangement(payload: dict) -> ArrangementInput:
    """Rebuild an :class:`ArrangementInput` from previously exported events."""
    if payload.get("format") != "chorale_arranger_events":
        raise ValueError("not a chorale_arranger_events payload")
    events = payload["events"]
    n_segments = max(e["segment"] for e in events) + 1 if events else 0
    fixed = [dict() for _ in range(n_segments)]
    durations = [1.0] * n_segments
    breaths = [0.0] * n_segments
    for e in events:
        if e.get("fixed"):
            fixed[e["segment"]][e["voice"]] = int(e["midi"])
        durations[e["segment"]] = float(e["duration_beats"])
        breaths[e["segment"]] = max(
            breaths[e["segment"]], float(e.get("breath_after_beats", 0.0) or 0.0)
        )
    segments = []
    for k in range(n_segments):
        # Round-trip preserves fixed notes and durations; chord candidates
        # are reconstructed from the sounding pitch classes (as "power"-free
        # exact pitch-class sets via explicit root+quality when available).
        segs = payload.get("segments")
        if segs is not None:
            raw = segs[k]
            chords = tuple(
                Chord(int(c["root_pc"]), c["quality"], c.get("label", ""))
                for c in raw["chord_candidates"]
            )
            breath_after = float(raw.get("breath_after", breaths[k]) or 0.0)
        else:
            pcs = sorted({e["midi"] % 12 for e in events if e["segment"] == k})
            chords = (Chord(pcs[0], "power", label="reimported"),)
            breath_after = breaths[k]
        segments.append(Segment(
            chord_candidates=chords,
            fixed=fixed[k],
            duration=durations[k],
            breath_after=breath_after,
        ))
    return ArrangementInput(
        segments=segments,
        key_root_pc=int(payload.get("key_root_pc", 0)),
        style=str(payload.get("style", "common_practice_strict")),
        seed=int(payload.get("seed", 0)),
        beam_width=int(payload.get("beam_width", 40)),
    )


# ---------------------------------------------------------------------------
# MIDI writer (format 0, dependency-free)
# ---------------------------------------------------------------------------

def _vlq(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def write_midi(payload: dict, path: str, ppq: int = 480, bpm: float = 90.0) -> None:
    """Write note events as a format-0 MIDI file (one multi-channel track)."""
    ticks_per_beat = ppq
    meta_tempo = int(60_000_000 / bpm)
    track = bytearray()
    # Tempo meta event.
    track += _vlq(0) + bytes([0xFF, 0x51, 0x03]) + meta_tempo.to_bytes(3, "big")

    entries = []  # (tick, order, raw_message)
    for e in payload["events"]:
        start = int(round(e["start_beat"] * ticks_per_beat))
        sounding_beats = float(e.get("sounding_beats", e["duration_beats"]))
        end = int(round((e["start_beat"] + sounding_beats) * ticks_per_beat))
        ch = VOICE_MIDI_CHANNEL[e["voice"]]
        vel = int(e.get("velocity", 78))
        pitch = int(e["midi"])
        entries.append((start, 0, bytes([0x90 | ch, pitch, vel])))
        entries.append((end, 1, bytes([0x80 | ch, pitch, 0])))
    entries.sort(key=lambda x: (x[0], x[1]))

    last_tick = 0
    for tick, _order, msg in entries:
        track += _vlq(max(0, tick - last_tick)) + msg
        last_tick = tick
    track += _vlq(0) + bytes([0xFF, 0x2F, 0x00])

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq)
    chunk = b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    if hasattr(path, "write"):
        path.write(header + chunk)
    else:
        with open(path, "wb") as fh:
            fh.write(header + chunk)


# ---------------------------------------------------------------------------
# WAV synthesis
# ---------------------------------------------------------------------------

@dataclass
class SynthConfig:
    bpm: float = 90.0
    sample_rate: int = 22050
    vibrato_hz: float = 5.2
    vibrato_cents: float = 9.0
    level: float = 0.22
    attack: float = 0.035
    release: float = 0.12


def _adsr(n, sr, cfg: SynthConfig):
    env = [1.0] * n
    a = max(1, int(cfg.attack * sr))
    r = max(1, int(cfg.release * sr))
    for i in range(min(a, n)):
        env[i] = i / a
    for i in range(min(r, n)):
        env[n - 1 - i] = min(env[n - 1 - i], i / r)
    return env


def synthesize_voices(payload: dict, cfg: SynthConfig | None = None) -> dict:
    """Return ``{"per_voice": {voice: float PCM}, "ensemble": (L, R)}``.

    Uses numpy when installed; otherwise a compact pure-Python renderer.
    """
    cfg = cfg or SynthConfig()
    try:
        import numpy as np  # type: ignore
        return _synthesize_numpy(payload, cfg, np)
    except ImportError:
        return _synthesize_pure(payload, cfg)


def _midi_freq(midi: float) -> float:
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def _synthesize_numpy(payload, cfg, np):
    sr = cfg.sample_rate
    beat_s = 60.0 / cfg.bpm
    total_beats = float(payload["total_beats"])
    total_s = total_beats * beat_s + cfg.release + 0.1
    n = int(total_s * sr)
    per_voice = {}
    for voice in VOICES:
        per_voice[voice] = np.zeros(n, dtype=np.float64)
    left = np.zeros(n, dtype=np.float64)
    right = np.zeros(n, dtype=np.float64)

    t_all = np.arange(n) / sr
    for e in payload["events"]:
        voice = e["voice"]
        start = int(e["start_beat"] * beat_s * sr)
        dur_s = float(e.get("sounding_beats", e["duration_beats"])) * beat_s
        m = int(dur_s * sr)
        f0 = _midi_freq(e["midi"])
        t = np.arange(m) / sr
        vib = 2.0 ** (cfg.vibrato_cents / 1200.0 *
                      np.sin(2 * math.pi * cfg.vibrato_hz * t))
        phase = 2 * math.pi * f0 * t
        # Formant-ish additive voice: strong fundamental, decaying partials.
        wave_sig = (
            np.sin(phase * vib)
            + 0.32 * np.sin(2 * phase * vib)
            + 0.14 * np.sin(3 * phase * vib)
            + 0.06 * np.sin(4 * phase * vib)
        )
        env = np.ones(m)
        a = max(1, int(cfg.attack * sr))
        r = max(1, int(cfg.release * sr))
        env[:a] = np.linspace(0, 1, a)
        env[-r:] = np.minimum(env[-r:], np.linspace(1, 0, r))
        sig = wave_sig * env * cfg.level
        idx = start + np.arange(m)
        idx = idx[idx < n]
        per_voice[voice][idx] += sig[: len(idx)]
        pan = VOICE_PAN[voice]
        left[idx] += sig[: len(idx)] * math.sqrt((1 - pan) / 2) * 1.4
        right[idx] += sig[: len(idx)] * math.sqrt((1 + pan) / 2) * 1.4

    peak = max(np.max(np.abs(left)), np.max(np.abs(right)), 1e-9)
    if peak > 0.95:
        left *= 0.95 / peak
        right *= 0.95 / peak
    for voice in VOICES:
        p = np.max(np.abs(per_voice[voice]))
        if p > 0.95:
            per_voice[voice] *= 0.95 / p
    return {
        "per_voice": {v: per_voice[v].tolist() for v in VOICES},
        "ensemble": (left.tolist(), right.tolist()),
        "sample_rate": sr,
        "engine": "numpy",
    }


def _synthesize_pure(payload, cfg: SynthConfig):
    sr = min(cfg.sample_rate, 11025)
    beat_s = 60.0 / cfg.bpm
    total_s = float(payload["total_beats"]) * beat_s + cfg.release + 0.1
    n = int(total_s * sr)
    per_voice = {v: [0.0] * n for v in VOICES}
    left = [0.0] * n
    right = [0.0] * n
    for e in payload["events"]:
        voice = e["voice"]
        start = int(e["start_beat"] * beat_s * sr)
        m = max(1, int(float(e.get("sounding_beats", e["duration_beats"])) * beat_s * sr))
        f0 = _midi_freq(e["midi"])
        env = _adsr(m, sr, cfg)
        pan = VOICE_PAN[voice]
        gl, gr = math.sqrt((1 - pan) / 2) * 1.4, math.sqrt((1 + pan) / 2) * 1.4
        for j in range(m):
            idx = start + j
            if idx >= n:
                break
            tt = j / sr
            vib = 2.0 ** (cfg.vibrato_cents / 1200.0 *
                          math.sin(2 * math.pi * cfg.vibrato_hz * tt))
            ph = 2 * math.pi * f0 * tt * vib
            s = (math.sin(ph) + 0.32 * math.sin(2 * ph)
                 + 0.14 * math.sin(3 * ph)) * env[j] * cfg.level
            per_voice[voice][idx] += s
            left[idx] += s * gl
            right[idx] += s * gr
    peak = max(max(abs(x) for x in left), max(abs(x) for x in right), 1e-9)
    scale = 0.95 / peak if peak > 0.95 else 1.0
    if scale != 1.0:
        left = [x * scale for x in left]
        right = [x * scale for x in right]
        for v in VOICES:
            per_voice[v] = [x * scale for x in per_voice[v]]
    return {
        "per_voice": per_voice,
        "ensemble": (left, right),
        "sample_rate": sr,
        "engine": "stdlib",
    }


@contextlib.contextmanager
def _open_wav(path):
    if hasattr(path, "write"):
        yield wave.open(path, "wb")
    else:
        with wave.open(path, "wb") as wf:
            yield wf


def write_wav(path: str, pcm, sr: int, stereo: bool = True) -> None:
    """Write 16-bit PCM WAV from float samples in [-1, 1]."""
    if stereo:
        frames = bytearray()
        lch, rch = pcm
        for l, r in zip(lch, rch):
            frames += struct.pack("<hh",
                                  int(max(-1.0, min(1.0, l)) * 32767),
                                  int(max(-1.0, min(1.0, r)) * 32767))
        channels = 2
        data = bytes(frames)
    else:
        data = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767)) for x in pcm
        )
        channels = 1
    with _open_wav(path) as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(data)
