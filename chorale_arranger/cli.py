"""Command-line demonstration of the chorale arranger kernel.

Usage::

    python3 -m chorale_arranger.cli [--seed N] [--style NAME] [--out-dir DIR]

Writes the note-event JSON, MIDI and per-voice/ensemble WAV files, and prints
the itemized cost plus an interval-by-interval voice-leading check. Also runs
the contradictory fixed-note scenario to show infeasibility / recovery output.
"""

from __future__ import annotations

import argparse
import json
import os

from .events import (
    SynthConfig,
    arrangement_to_events,
    events_to_arrangement,
    synthesize_voices,
    write_midi,
    write_wav,
)
from .solver import ArrangementInput, Segment, solve
from .theory import VOICES, Chord, interval_name, parse_pitch, pitch_to_name


def build_feasible(seed: int, style: str) -> ArrangementInput:
    plan = [
        ("I", {"soprano": parse_pitch("C5")}),
        ("IV", {"soprano": parse_pitch("A4")}),
        ("V7", {"soprano": parse_pitch("G4")}),
        ("I", {"soprano": parse_pitch("C5"), "bass": parse_pitch("C3")}),
    ]
    segs = [
        Segment((Chord.from_function(fn, 0),), fixed, duration=1.0)
        for fn, fixed in plan
    ]
    return ArrangementInput(segments=segs, key_root_pc=0, style=style, seed=seed)


def build_conflict(seed: int) -> ArrangementInput:
    # Fixed soprano and bass simultaneously straddle the range boundary.
    plan = [
        ({"soprano": parse_pitch("C6"), "bass": parse_pitch("C2")}, "I"),
        ({"soprano": parse_pitch("B5"), "bass": parse_pitch("G2")}, "V7"),
        ({"soprano": parse_pitch("C6"), "bass": parse_pitch("C2")}, "I"),
    ]
    segs = [
        Segment((Chord.from_function(fn, 0),), fixed) for fixed, fn in plan
    ]
    return ArrangementInput(segments=segs, key_root_pc=0, seed=seed)


def print_voice_leading(result) -> None:
    print("\n逐声部旋律线与相邻音程核对：")
    for vi, voice in enumerate(VOICES):
        line = [pitch_to_name(seg.voicing[vi], result.key_root_pc) for seg in result.segments]
        moves = []
        for k in range(1, len(result.segments)):
            delta = result.segments[k].voicing[vi] - result.segments[k - 1].voicing[vi]
            moves.append(
                "同音保持" if delta == 0
                else f"{'上行' if delta > 0 else '下行'}{interval_name(delta)}({delta:+d})"
            )
        print(f"  {voice:8s} | " + " -> ".join(line))
        print(f"  {'':8s} | " + " ; ".join(moves))
    print("\n逐段纵向核对（音程自低到高）：")
    for seg in result.segments:
        v = seg.voicing
        gaps = [v[i] - v[i + 1] for i in range(3)]
        names = [pitch_to_name(p, result.key_root_pc) for p in v]
        print(f"  段{seg.index + 1} {seg.chord.symbol:4s} "
              f"S{names[0]} A{names[1]} T{names[2]} B{names[3]}  "
              f"间距 S-A={gaps[0]} A-T={gaps[1]} T-B={gaps[2]} "
              f"违反={[x['rule'] for x in seg.violations]}")


def run(inp: ArrangementInput, out_dir: str, tag: str) -> dict:
    result = solve(inp)
    print("=" * 78)
    print(f"[{tag}] status={result.status} style={result.style} seed={result.seed} "
          f"total_cost={result.total_cost if result.total_cost != float('inf') else 'inf'}")
    if result.cost_breakdown:
        print("代价分项：")
        for key, value in sorted(result.cost_breakdown.items(), key=lambda kv: -kv[1]):
            print(f"  {key:20s} {value:10.3f}")
    if result.diagnostics:
        print("输入诊断：")
        for d in result.diagnostics:
            print("  -", d)
    if result.recovery:
        print("恢复策略对比：")
        for s in result.recovery["strategies"]:
            print(f"  {s['name']:9s} 硬违反={s['hard_violations']} 代价={s['total_cost']} "
                  f"序列={' -> '.join(s['chord_symbols'])}")
        print("  选定：", result.recovery["chosen"])
    if result.segments:
        print_voice_leading(result)
        events = arrangement_to_events(result)
        os.makedirs(out_dir, exist_ok=True)
        json_path = os.path.join(out_dir, f"{tag}_events.json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(events, fh, ensure_ascii=False, indent=2)
        write_midi(events, os.path.join(out_dir, f"{tag}.mid"))
        audio = synthesize_voices(events, SynthConfig())
        write_wav(os.path.join(out_dir, f"{tag}_ensemble.wav"),
                  audio["ensemble"], audio["sample_rate"], stereo=True)
        for voice in VOICES:
            write_wav(os.path.join(out_dir, f"{tag}_{voice}.wav"),
                      audio["per_voice"][voice], audio["sample_rate"], stereo=False)
        print(f"已写出：{json_path}、{tag}.mid、分轨/合奏 WAV（引擎 {audio['engine']}）")
        return events
    return {}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--style", default="common_practice_strict")
    parser.add_argument("--out-dir", default="chorale_output")
    args = parser.parse_args(argv)

    events = run(build_feasible(args.seed, args.style), args.out_dir, "feasible")
    run(build_conflict(args.seed), args.out_dir, "conflict")

    if events:
        rebuilt = events_to_arrangement(events)
        again = solve(rebuilt)
        original_voicings = [
            tuple(seg["voicing"][v] for v in VOICES)
            for seg in _segments_from_events(events)
        ]
        same = [s.voicing for s in again.segments] == original_voicings
        print("=" * 78)
        print(f"回读重解 status={again.status} cost={again.total_cost:.3f} "
              f"声部序列与导出一致={same}")


def _segments_from_events(events: dict) -> list[dict]:
    grouped: dict[int, dict] = {}
    for e in events["events"]:
        grouped.setdefault(e["segment"], {"voicing": {}})["voicing"][e["voice"]] = e["midi"]
    return [grouped[k] for k in sorted(grouped)]


if __name__ == "__main__":
    main()
