"""Audible blind-comparison lab for SATB arranging policies.

This module intentionally stays small and deterministic.  It connects two
solvable arrangers:

* ``phrase``    -- the full-phrase beam/DP solver in :mod:`solver`;
* ``lookahead`` -- a receding-horizon solver which keeps alternatives only for
  the chord immediately ahead.

It also produces the selected/eliminated path evidence used by the UI.  The
metrics here are plain data so tests and the HTTP layer can consume them
without a browser.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .events import arrangement_to_events
from .rules import RULE_REGISTRY, get_profile
from .solver import (
    ArrangementInput,
    Segment,
    SolveResult,
    _State,
    _add_dicts,
    _beam_prune,
    _build_result,
    _enumerate_voicings,
    _infeasible,
    _precheck,
    _snapshot,
    _transition_cost,
    solve,
)
from .theory import VOICES, VOICE_RANGES, Chord, pitch_to_name

POLICY_PATH = Path(__file__).resolve().parent.parent / ".chorale_lab_policy.json"

# ---------------------------------------------------------------------------
# Built-in short scores
# ---------------------------------------------------------------------------


def breath_phrase_document() -> dict:
    return _stable_document()


def _stable_document() -> dict:
    # The placeholder above is replaced by an explicitly valid score below.
    return {
        "score_id": "breath_phrase",
        "title": "固定女高短句：C 大调呼吸练习",
        "key_root_pc": 0,
        "style": "common_practice_strict",
        "seed": 11,
        "beam_width": 160,
        "planner": "phrase",
        "priority": "phrase",
        "preserve": ["soprano", "breath_after"],
        "breath_marks": [{"after_segment": 2, "beats": 0.35, "label": "呼吸口"}],
        "segments": [
            {"duration": 1, "fixed": {"soprano": 72},
             "chord_candidates": [{"function": "I"}]},
            {"duration": 1, "fixed": {"soprano": 71},
             "chord_candidates": [{"function": "vii°"}]},
            {"duration": 1, "breath_after": 0.35, "fixed": {"soprano": 67},
             "chord_candidates": [{"function": "V7"}]},
            {"duration": 1, "fixed": {"soprano": 69},
             "chord_candidates": [{"function": "vi"}]},
            {"duration": 1, "fixed": {"soprano": 65},
             "chord_candidates": [{"function": "IV"}]},
            {"duration": 1, "fixed": {"soprano": 67},
             "chord_candidates": [{"function": "V"}]},
            {"duration": 2, "fixed": {"soprano": 72},
             "chord_candidates": [{"function": "I"}]},
        ],
    }


def inner_crossing_document() -> dict:
    """All chord tones/consonant, with an A/T crossed candidate at segment 1."""
    doc = _stable_document()
    doc.update({
        "score_id": "inner_crossing",
        "title": "协和但内声交叉：锁音撤销诊断",
        "seed": 23,
        "breath_marks": [{"after_segment": 2, "beats": 0.3, "label": "呼吸口"}],
        "lock_sequence": [
            {"segment": 4, "voice": "alto", "midi": 52, "label": "E3：低于女低 G3 边界的锁音"},
            {"segment": 1, "voice": "alto", "midi": 64, "label": "E4：诱导 A/T 交叉的锁音"},
        ],
        "segments": [
            {"duration": 1, "fixed": {"soprano": 72},
             "chord_candidates": [{"function": "I"}]},
            {"duration": 1, "fixed": {"soprano": 72},
             "chord_candidates": [{"function": "I"}]},
            {"duration": 1, "breath_after": 0.3, "fixed": {"soprano": 67},
             "chord_candidates": [{"function": "V7"}]},
            {"duration": 1, "fixed": {"soprano": 72},
             "chord_candidates": [{"function": "I"}]},
            {"duration": 1, "fixed": {"soprano": 72},
             "chord_candidates": [{"function": "I"}]},
            {"duration": 2, "fixed": {"soprano": 72},
             "chord_candidates": [{"function": "I"}]},
        ],
    })
    return doc


LAB_SCORES = {
    "breath_phrase": _stable_document,
    "inner_crossing": inner_crossing_document,
}


def score_document(score_id: str) -> dict:
    if score_id not in LAB_SCORES:
        raise KeyError(f"unknown lab score {score_id!r}; choose from {sorted(LAB_SCORES)}")
    return copy.deepcopy(LAB_SCORES[score_id]())


def score_fingerprint(doc: dict) -> str:
    payload = {
        "key_root_pc": doc.get("key_root_pc", 0),
        "segments": doc.get("segments", []),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def lock_options_for_document(doc: dict) -> list[dict]:
    inp = input_from_document(doc)
    ranges = inp.voice_ranges()
    options = []
    for k, seg in enumerate(inp.segments):
        for voice in ("alto", "tenor"):
            pitches = set()
            for chord in seg.chord_candidates:
                lists, _ = _voice_lists_for_lab(chord, seg, ranges)
                pitches.update(lists[VOICES.index(voice)])
            options.append({
                "segment": k,
                "voice": voice,
                "pitches": [
                    {"midi": p, "name": pitch_to_name(p, inp.key_root_pc)}
                    for p in sorted(pitches)
                ],
            })
    return options


# ---------------------------------------------------------------------------
# Input construction and policy weighting
# ---------------------------------------------------------------------------


def _build_chord(spec: dict, key_root_pc: int) -> Chord:
    if spec.get("function"):
        return Chord.from_function(str(spec["function"]), key_root_pc)
    return Chord(
        int(spec.get("root_pc", 0)) % 12,
        str(spec.get("quality", "maj")),
        str(spec.get("label", "")),
    )


def input_from_document(doc: dict) -> ArrangementInput:
    key_root_pc = int(doc.get("key_root_pc", 0))
    segments = []
    for raw in doc["segments"]:
        chords = tuple(_build_chord(c, key_root_pc) for c in raw["chord_candidates"])
        fixed = {str(v): int(p) for v, p in (raw.get("fixed") or {}).items()}
        segments.append(Segment(
            chord_candidates=chords,
            fixed=fixed,
            duration=float(raw.get("duration", 1.0)),
            label_hint=str(raw.get("label_hint", "")),
            breath_after=float(raw.get("breath_after", 0.0) or 0.0),
        ))
    ranges = None
    if doc.get("ranges"):
        ranges = {v: (int(b[0]), int(b[1])) for v, b in doc["ranges"].items()}
    return ArrangementInput(
        segments=segments,
        key_root_pc=key_root_pc,
        style=str(doc.get("style", "common_practice_strict")),
        overrides=doc.get("overrides"),
        ranges=ranges,
        beam_width=int(doc.get("beam_width", 160) or 160),
        seed=int(doc.get("seed", 0)),
        candidate_cap=int(doc.get("candidate_cap", 0) or 0),
    )


def apply_priority(inp: ArrangementInput, priority: str) -> ArrangementInput:
    """Return an input whose weights express local-first or phrase-first policy."""
    priority = priority if priority in ("local", "phrase") else "phrase"
    base = inp.profile()
    weights = {key: base.weight(key) for key in RULE_REGISTRY}
    if priority == "local":
        for key in ("parallel_perfect", "max_leap", "upper_spacing", "hidden_parallel"):
            weights[key] *= 2.5
        for key in ("smoothness", "common_tone", "completeness"):
            weights[key] *= 0.65
    else:
        for key in ("smoothness", "common_tone", "completeness", "doubling"):
            weights[key] *= 1.45
        for key in ("parallel_perfect", "max_leap", "upper_spacing"):
            weights[key] *= 0.9
    existing_overrides = inp.overrides or {}
    overrides = {
        "levels": dict(existing_overrides.get("levels") or {}),
        "weights": weights,
        "params": dict(base.params),
    }
    return ArrangementInput(
        segments=inp.segments,
        key_root_pc=inp.key_root_pc,
        style=inp.style,
        overrides=overrides,
        ranges=inp.ranges,
        beam_width=inp.beam_width,
        seed=inp.seed,
        candidate_cap=inp.candidate_cap,
    )


def with_lock(doc: dict, lock: dict | None) -> dict:
    locked = copy.deepcopy(doc)
    if not lock:
        return locked
    segment = int(lock["segment"])
    voice = str(lock["voice"])
    midi = int(lock["midi"])
    locked["segments"][segment].setdefault("fixed", {})[voice] = midi
    locked["active_lock"] = {"segment": segment, "voice": voice, "midi": midi}
    return locked


# ---------------------------------------------------------------------------
# Finite / one-chord lookahead arranger
# ---------------------------------------------------------------------------


def _flatten_voicings(chords, seg, ranges, relaxed, cap, rng):
    options = []
    out_of_range = {}
    for chord in chords:
        voicings, oor = _enumerate_voicings(
            chord, seg, ranges,
            allow_out_of_range=relaxed, cap=cap, rng=rng,
        )
        out_of_range.update(oor)
        options.extend((chord, v) for v in voicings)
    return options, out_of_range


def solve_lookahead(inp: ArrangementInput, lookahead: int = 1) -> SolveResult:
    """Receding-horizon solver.

    ``lookahead=1`` evaluates the current chord plus every feasible voicing of
    the immediately upcoming chord.  No alternatives beyond that chord are
    retained, which is the policy the UI calls "有限前瞻".
    """
    import random

    profile = inp.profile()
    ranges = inp.voice_ranges()
    rng = random.Random(inp.seed + 101)
    segments = [Segment.coerce(s) for s in inp.segments]
    diagnostics = _precheck(inp, ranges)
    hard_blockers = [
        d for d in diagnostics
        if "belongs to none" in d or "unknown voice" in d
    ]
    if hard_blockers:
        return _infeasible(inp, hard_blockers, segments, ranges, profile)

    committed: list[SegmentResult] = []  # noqa: F821 - annotation avoided below
    total_cost = 0.0
    breakdown: dict[str, float] = {}
    violations: list[dict] = []
    prev_chord = None
    prev_voicing = None
    trace = []
    relaxed_used = False

    for k, seg in enumerate(segments):
        current, oor = _flatten_voicings(
            seg.chord_candidates, seg, ranges, False, inp.candidate_cap, rng
        )
        strict_current = []
        for chord, voicing in current:
            if all(ranges[v][0] <= voicing[i] <= ranges[v][1]
                   for i, v in enumerate(VOICES)):
                strict_current.append((chord, voicing))
        used_relaxed = not strict_current
        if used_relaxed:
            relaxed_used = True
            current, oor = _flatten_voicings(
                seg.chord_candidates, seg, ranges, True, inp.candidate_cap, rng
            )
        else:
            current = strict_current

        upcoming: list = []
        if lookahead and k + 1 < len(segments):
            next_seg = segments[k + 1]
            upcoming, _ = _flatten_voicings(
                next_seg.chord_candidates, next_seg, ranges,
                used_relaxed, inp.candidate_cap, rng,
            )

        ranked = []
        for chord, voicing in current:
            scored = _transition_cost(
                profile, inp.key_root_pc, chord, voicing,
                prev_chord, prev_voicing, used_relaxed, ranges, k,
            )
            if scored is None:
                continue
            immediate_costs, immediate_violations = scored
            look_cost = 0.0
            look_count = 0
            if upcoming:
                next_costs = []
                for next_chord, next_voicing in upcoming:
                    next_scored = _transition_cost(
                        profile, inp.key_root_pc, next_chord, next_voicing,
                        chord, voicing, used_relaxed, ranges, k + 1,
                    )
                    if next_scored is not None:
                        next_costs.append(sum(next_scored[0].values()))
                if next_costs:
                    look_cost = min(next_costs)
                    look_count = len(next_costs)
                elif not used_relaxed:
                    continue
            rank_cost = sum(immediate_costs.values()) + look_cost
            ranked.append((rank_cost, chord, voicing, immediate_costs,
                           immediate_violations, look_count))

        if not ranked and not used_relaxed:
            # One explicit relaxed retry keeps this arranger actually solvable
            # for the same boundary conflicts the phrase solver recovers from.
            relaxed_used = True
            current, _ = _flatten_voicings(
                seg.chord_candidates, seg, ranges, True, inp.candidate_cap, rng
            )
            for chord, voicing in current:
                scored = _transition_cost(
                    profile, inp.key_root_pc, chord, voicing,
                    prev_chord, prev_voicing, True, ranges, k,
                )
                if scored is None:
                    continue
                immediate_costs, immediate_violations = scored
                ranked.append((sum(immediate_costs.values()), chord, voicing,
                               immediate_costs, immediate_violations, 0))

        if not ranked:
            empty = ArrangementInput(segments=segments, key_root_pc=inp.key_root_pc,
                                     style=inp.style, overrides=inp.overrides,
                                     ranges=inp.ranges, beam_width=inp.beam_width,
                                     seed=inp.seed)
            from .solver import _infeasible
            return _infeasible(empty, diagnostics + [f"lookahead found no voicing at segment {k}"],
                               segments, ranges, profile)

        ranked.sort(key=lambda row: (row[0], row[2]))
        _, chord, voicing, costs, viols, look_count = ranked[0]
        result_row = _segment_result(k, chord, voicing, costs, viols)
        committed.append(result_row)
        total_cost += sum(costs.values())
        breakdown = _add_dicts(breakdown, costs)
        violations.extend(viols)
        prev_chord, prev_voicing = chord, voicing
        trace.append({
            "segment": k,
            "candidates_enumerated": len(current),
            "distinct_states": len(ranked),
            "beam_kept": 1,
            "tie_band_size": sum(1 for r in ranked if abs(r[0] - ranked[0][0]) < 1e-9),
            "lookahead_candidates": look_count,
        })

    state = _State(
        chord=prev_chord, voicing=prev_voicing, cost=total_cost,
        breakdown=breakdown, violations=violations, per_segment=committed,
        beam_size=1,
    )
    state.beam_trace = trace
    status = "recovered" if relaxed_used or violations else "optimal"
    result = _build_result(status, state, inp, segments, diagnostics, recovery=None)
    if status == "recovered":
        result.recovery = {
            "chosen": "lookahead_relax",
            "failed_segment": next((v.get("segment") for v in violations), 0),
            "strategies": [{
                "name": "lookahead_relax",
                "total_cost": round(total_cost, 4),
                "hard_violations": len(violations),
                "violations": violations,
                "chord_symbols": [s.chord.symbol for s in committed],
            }],
            "comparison_note": "有限前瞻在当前/下一和弦窗口内无解时，显式放宽并记录边界。",
        }
    return result


def _segment_result(index, chord, voicing, costs, violations):
    from .solver import SegmentResult
    return SegmentResult(index, chord, voicing, costs, violations)


# ---------------------------------------------------------------------------
# Musical metrics
# ---------------------------------------------------------------------------


def _pitch_name(midi: int, key_root_pc: int) -> str:
    return pitch_to_name(int(midi), key_root_pc)


def actual_ranges(path: list[tuple[int, int, int, int]], ranges: dict) -> dict:
    report = {}
    for i, voice in enumerate(VOICES):
        pitches = [row[i] for row in path]
        low, high = ranges[voice]
        pmin, pmax = min(pitches), max(pitches)
        position = ((pmin + pmax) / 2 - low) / max(1, high - low)
        if position < 0.34:
            register = "低音区"
        elif position < 0.67:
            register = "中央声区"
        else:
            register = "高音区"
        report[voice] = {
            "min_midi": pmin,
            "max_midi": pmax,
            "min_name": pitch_to_name(pmin),
            "max_name": pitch_to_name(pmax),
            "span_semitones": pmax - pmin,
            "register": register,
            "declared_low": low,
            "declared_high": high,
            "declared_range_names": [pitch_to_name(low), pitch_to_name(high)],
            "out_of_range": [p for p in pitches if not low <= p <= high],
            "in_range": all(low <= p <= high for p in pitches),
        }
    return report


def parallel_events(path: list[tuple[int, int, int, int]]) -> list[dict]:
    events = []
    for k in range(1, len(path)):
        prev, cur = path[k - 1], path[k]
        for i in range(4):
            for j in range(i + 1, 4):
                before = abs(prev[i] - prev[j])
                after = abs(cur[i] - cur[j])
                parallel = (
                    before % 12 in (0, 7) and after % 12 in (0, 7)
                    and prev[i] != prev[j] and cur[i] != cur[j]
                    and (cur[i] - prev[i]) * (cur[j] - prev[j]) > 0
                )
                if parallel:
                    events.append({
                        "segment": k,
                        "voices": [VOICES[i], VOICES[j]],
                        "before_semitones": before,
                        "after_semitones": after,
                        "interval": "pure_octave_or_unison" if after % 12 == 0 else "perfect_fifth",
                        "pitches": {
                            VOICES[i]: [prev[i], cur[i]],
                            VOICES[j]: [prev[j], cur[j]],
                        },
                    })
    return events


def crossing_events(path) -> list[dict]:
    events = []
    for k, row in enumerate(path):
        for i in range(3):
            if row[i] < row[i + 1]:
                events.append({
                    "segment": k,
                    "voices": [VOICES[i], VOICES[i + 1]],
                    "pitches": {VOICES[i]: row[i], VOICES[i + 1]: row[i + 1]},
                    "detail": f"{VOICES[i]} below {VOICES[i + 1]}",
                })
    return events


def path_metrics(path, ranges, profile=None) -> dict:
    parallels = parallel_events(path)
    crossings = crossing_events(path)
    range_violations = sum(
        1 for row in path for i, voice in enumerate(VOICES)
        if not ranges[voice][0] <= row[i] <= ranges[voice][1]
    )
    spacing_violations = leap_violations = 0
    spacing_limit = float(profile.params.get("max_spacing", 12.0)) if profile else 12.0
    leap_limit = float(profile.params.get("max_leap", 12.0)) if profile else 12.0
    for k, row in enumerate(path):
        for i in range(2):
            if row[i] - row[i + 1] > spacing_limit:
                spacing_violations += 1
        if k:
            for i in range(4):
                if abs(row[i] - path[k - 1][i]) > leap_limit:
                    leap_violations += 1
    forbidden = len(parallels) + len(crossings) + spacing_violations + leap_violations
    return {
        "forbidden_count": forbidden,
        "parallel_perfect_count": len(parallels),
        "crossing_count": len(crossings),
        "spacing_violation_count": spacing_violations,
        "leap_violation_count": leap_violations,
        "first_parallel": parallels[0] if parallels else None,
        "parallel_events": parallels,
        "crossing_events": crossings,
        "range_violation_count": range_violations,
        "actual_ranges": actual_ranges(path, ranges),
    }


def count_changed_notes(path_a, path_b) -> dict:
    changes = []
    for k, (a, b) in enumerate(zip(path_a, path_b)):
        for i, voice in enumerate(VOICES):
            if a[i] != b[i]:
                changes.append({"segment": k, "voice": voice, "from": a[i], "to": b[i]})
    by_voice = {v: 0 for v in VOICES}
    for change in changes:
        by_voice[change["voice"]] += 1
    return {"changed_notes": len(changes), "by_voice": by_voice, "changes": changes}


def lock_rewrite_metrics(locked_path, baseline_path, lock_voice: str | None) -> dict:
    diff = count_changed_notes(locked_path, baseline_path)
    if lock_voice not in VOICES:
        return {**diff, "locked_voice": None, "locked_voice_changes": 0,
                "adjacent_voice_rewrites": 0, "far_voice_rewrites": diff["changed_notes"]}
    li = VOICES.index(lock_voice)
    adjacent = far = locked_changes = 0
    by_distance = {"0": 0, "1": 0, "2": 0, "3": 0}
    for change in diff["changes"]:
        vi = VOICES.index(change["voice"])
        distance = abs(vi - li)
        by_distance[str(distance)] += 1
        if distance == 0:
            locked_changes += 1
        elif distance == 1:
            adjacent += 1
        else:
            far += 1
    return {
        **diff,
        "locked_voice": lock_voice,
        "locked_voice_changes": locked_changes,
        "adjacent_voice_rewrites": adjacent,
        "far_voice_rewrites": far,
        "rewrites_by_voice": diff["by_voice"],
        "rewrites_by_distance": by_distance,
    }


def _attach_path_lock_impact(path_report: dict, baseline_path, lock_voice: str | None) -> None:
    if not baseline_path:
        return
    for group in ("paths", "rejected_paths"):
        for path_record in path_report.get(group, []):
            candidate_path = [
                tuple(row["voicing"][voice] for voice in VOICES)
                for row in path_record.get("rows", [])
            ]
            if len(candidate_path) != len(baseline_path):
                continue
            impact = lock_rewrite_metrics(candidate_path, baseline_path, lock_voice)
            path_record.setdefault("metrics", {})["lock_impact"] = impact
            path_record["metrics"]["far_voice_rewrites"] = impact["far_voice_rewrites"]


# ---------------------------------------------------------------------------
# Candidate graph and selected/eliminated paths
# ---------------------------------------------------------------------------


def _segment_states(inp: ArrangementInput, relaxed: bool, raw: bool = False):
    ranges = inp.voice_ranges()
    rng = __import__("random").Random(inp.seed)
    states_by_segment = []
    counts = []
    for k, seg in enumerate(inp.segments):
        states = []
        raw_total = 1
        legal_count = 0
        for chord in seg.chord_candidates:
            lists, oor = _voice_lists_for_lab(chord, seg, ranges)
            raw_total *= max(1, len(lists[0]) * len(lists[1]) * len(lists[2]) * len(lists[3]))
            if raw:
                voicings = _raw_voicings(lists, inp.candidate_cap or 180)
            else:
                voicings, _ = _enumerate_voicings(
                    chord, seg, ranges, relaxed, inp.candidate_cap, rng
                )
            legal_count += len(voicings)
            for voicing in voicings:
                states.append({"segment": k, "chord": chord, "voicing": voicing})
        counts.append({
            "segment": k,
            "raw_combination_count": raw_total,
            "enumerated_legal_count": legal_count,
            "state_count": len(states),
        })
        states_by_segment.append(states)
    return states_by_segment, counts


def _raw_voicings(lists, cap: int):
    out = []
    for row in itertools.product(*lists):
        out.append(tuple(int(x) for x in row))
        if cap and len(out) >= cap:
            break
    return out


def _voice_lists_for_lab(chord, seg, ranges):
    # Reuse the solver's list construction, but preserve fixed out-of-range notes.
    from .solver import _voice_pitch_lists
    return _voice_pitch_lists(chord, seg, ranges, allow_out_of_range=True)


def rank_paths(
    inp: ArrangementInput,
    top_k: int = 8,
    path_limit: int = 12,
    relaxed_override: bool = False,
) -> dict:
    profile = inp.profile()
    ranges = inp.voice_ranges()
    strict_inp = inp
    states_by_seg, candidate_counts = _segment_states(inp, relaxed=relaxed_override)
    relaxed = relaxed_override or any(not states for states in states_by_seg)
    if not relaxed_override and relaxed:
        states_by_seg, candidate_counts = _segment_states(inp, relaxed=True)

    # Each label is a K-best partial path ending in a concrete state.
    layer = []
    for state in states_by_seg[0]:
        scored = _transition_cost(
            profile, inp.key_root_pc, state["chord"], state["voicing"],
            None, None, relaxed, ranges, 0,
        )
        if scored is None:
            continue
        costs, viols = scored
        layer.append({
            "cost": sum(costs.values()),
            "states": [state],
            "breakdown": dict(costs),
            "violations": list(viols),
        })

    for k in range(1, len(inp.segments)):
        next_layer = []
        for state in states_by_seg[k]:
            endings = []
            for prev_path in layer:
                prev_state = prev_path["states"][-1]
                scored = _transition_cost(
                    profile, inp.key_root_pc, state["chord"], state["voicing"],
                    prev_state["chord"], prev_state["voicing"],
                    relaxed, ranges, k,
                )
                if scored is None:
                    continue
                costs, viols = scored
                ending = {
                    "cost": prev_path["cost"] + sum(costs.values()),
                    "states": prev_path["states"] + [state],
                    "breakdown": _add_dicts(prev_path["breakdown"], costs),
                    "violations": prev_path["violations"] + viols,
                }
                endings.append(ending)
            endings.sort(key=lambda p: (p["cost"], p["states"][-1]["voicing"]))
            next_layer.extend(endings[:top_k])
        next_layer.sort(key=lambda p: (p["cost"], p["states"][-1]["voicing"]))
        layer = next_layer[: max(40, top_k * 12)]

    layer.sort(key=lambda p: p["cost"])
    top = layer[:path_limit]
    if not top and not relaxed_override:
        recovered = rank_paths(
            inp, top_k=top_k, path_limit=path_limit, relaxed_override=True
        )
        recovered["strict_paths_empty"] = True
        return recovered
    paths = []
    for rank, candidate in enumerate(top, 1):
        voicing_path = [s["voicing"] for s in candidate["states"]]
        chords = [s["chord"] for s in candidate["states"]]
        metrics = path_metrics(voicing_path, ranges, profile)
        paths.append({
            "id": f"path-{rank}",
            "rank": rank,
            "selected": rank == 1,
            "eliminated": rank != 1,
            "total_cost": round(float(candidate["cost"]), 4),
            "cost_breakdown": {k: round(v, 4) for k, v in candidate["breakdown"].items()},
            "relaxed": relaxed,
            "voicings": {
                voice: [row[i] for row in voicing_path] for i, voice in enumerate(VOICES)
            },
            "rows": [
                {
                    "segment": k,
                    "chord_symbol": chords[k].symbol,
                    "voicing": {v: voicing_path[k][i] for i, v in enumerate(VOICES)},
                    "names": {
                        v: pitch_to_name(voicing_path[k][i], inp.key_root_pc)
                        for i, v in enumerate(VOICES)
                    },
                }
                for k in range(len(voicing_path))
            ],
            "metrics": metrics,
            "rejection_reason": None if rank == 1 else _rejection_reason(metrics, candidate),
        })
    rejected_paths = _rejected_paths(inp)
    return {
        "relaxed": relaxed,
        "paths": paths,
        "rejected_paths": rejected_paths,
        "candidate_counts": candidate_counts,
    }


def _rejected_paths(inp: ArrangementInput, limit: int = 8) -> list[dict]:
    """Raw paths kept only as evidence: crossings are not enumeration-filtered."""
    profile = inp.profile()
    # Evidence ranking uses the loose profile so parallel moves remain visible
    # as weighted alternatives instead of being hidden by a 1000-point recovery
    # penalty. path_metrics still labels them as forbidden under the active view.
    score_profile = get_profile("common_practice_loose")
    ranges = inp.voice_ranges()
    states_by_seg, _ = _segment_states(inp, relaxed=True, raw=True)
    if not states_by_seg or any(not layer for layer in states_by_seg):
        return []

    layer = []
    for state in states_by_seg[0][:220]:
        scored = _transition_cost(
            score_profile, inp.key_root_pc, state["chord"], state["voicing"],
            None, None, True, ranges, 0,
        )
        if scored is None:
            continue
        costs, _viols = scored
        layer.append({
            "cost": sum(costs.values()),
            "states": [state],
            "breakdown": dict(costs),
        })

    for k in range(1, len(inp.segments)):
        next_layer = []
        for state in states_by_seg[k][:220]:
            endings = []
            for prev_path in layer[:90]:
                prev_state = prev_path["states"][-1]
                scored = _transition_cost(
                    score_profile, inp.key_root_pc, state["chord"], state["voicing"],
                    prev_state["chord"], prev_state["voicing"],
                    True, ranges, k,
                )
                if scored is None:
                    continue
                costs, _viols = scored
                endings.append({
                    "cost": prev_path["cost"] + sum(costs.values()),
                    "states": prev_path["states"] + [state],
                    "breakdown": _add_dicts(prev_path["breakdown"], costs),
                })
            endings.sort(key=lambda p: (p["cost"], p["states"][-1]["voicing"]))
            next_layer.extend(endings[:2])
        next_layer.sort(key=lambda p: (p["cost"], p["states"][-1]["voicing"]))
        layer = next_layer[:100]

    evidence = []
    seen = set()
    parallel_example = _parallel_evidence_path(inp, states_by_seg, score_profile)
    if parallel_example:
        state_path = parallel_example.pop("_states")
        seen.add(tuple(state["voicing"] for state in state_path))
        evidence.append(parallel_example)
    categories = (
        ("crossing", lambda m: m["crossing_count"] > 0),
        ("parallel", lambda m: m["parallel_perfect_count"] > 0),
        ("range", lambda m: m["range_violation_count"] > 0),
        ("other_forbidden", lambda m: m["forbidden_count"] > 0),
    )
    for category, predicate in categories:
        best = None
        for candidate in sorted(layer, key=lambda p: p["cost"]):
            voicing_path = [s["voicing"] for s in candidate["states"]]
            metrics = path_metrics(voicing_path, ranges, profile)
            if predicate(metrics):
                key = tuple(voicing_path)
                if key in seen:
                    continue
                best = _rejected_path_dict(
                    f"rejected-{category}", len(evidence) + 1,
                    candidate, inp, metrics, category,
                )
                seen.add(key)
                break
        if best:
            evidence.append(best)

    for candidate in sorted(layer, key=lambda p: p["cost"])[:30]:
        if len(evidence) >= limit:
            break
        voicing_path = [s["voicing"] for s in candidate["states"]]
        metrics = path_metrics(voicing_path, ranges, profile)
        if not (metrics["forbidden_count"] or metrics["range_violation_count"]):
            continue
        key = tuple(voicing_path)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(_rejected_path_dict(
            "rejected-path", len(evidence) + 1, candidate, inp, metrics, "ranked",
        ))
    return evidence[:limit]


def _rejected_path_dict(id_prefix, rank, candidate, inp, metrics, category):
    voicing_path = [s["voicing"] for s in candidate["states"]]
    chords = [s["chord"] for s in candidate["states"]]
    reason = _rejection_reason(metrics, candidate)
    if category == "parallel" and metrics["first_parallel"]:
        fp = metrics["first_parallel"]
        reason = (
            f"第 {fp['segment'] + 1} 段出现 "
            f"{'/'.join(fp['voices'])} 平行完全协和音程，被禁行规则淘汰"
        )
    elif metrics["crossing_count"]:
        first = metrics["crossing_events"][0]
        reason = f"第 {first['segment'] + 1} 段内声交叉，被 no_crossing 淘汰；音响仍可能协和"
    return {
        "id": f"{id_prefix}-{rank}",
        "rank": rank,
        "selected": False,
        "eliminated": True,
        "evidence_category": category,
        "total_cost": round(float(candidate["cost"]), 4),
        "cost_breakdown": {k: round(v, 4) for k, v in candidate["breakdown"].items()},
        "voicings": {v: [row[i] for row in voicing_path] for i, v in enumerate(VOICES)},
        "rows": [
            {
                "segment": k,
                "chord_symbol": chords[k].symbol,
                "voicing": {v: voicing_path[k][i] for i, v in enumerate(VOICES)},
                "names": {
                    v: pitch_to_name(voicing_path[k][i], inp.key_root_pc)
                    for i, v in enumerate(VOICES)
                },
            }
            for k in range(len(voicing_path))
        ],
        "metrics": metrics,
        "rejection_reason": reason,
    }


def _loose_transition_cost(profile, inp, ranges, prev_state, state, k):
    scored = _transition_cost(
        profile, inp.key_root_pc, state["chord"], state["voicing"],
        prev_state["chord"] if prev_state else None,
        prev_state["voicing"] if prev_state else None,
        True, ranges, k,
    )
    return sum(scored[0].values()) if scored is not None else float("inf")


def _parallel_evidence_path(inp, states_by_seg, score_profile):
    """Build one complete rejected path containing a detectable parallel move."""
    ranges = inp.voice_ranges()
    n = len(states_by_seg)
    for k in range(1, n):
        for prev_state in states_by_seg[k - 1][:160]:
            for state in states_by_seg[k][:160]:
                if not parallel_events([prev_state["voicing"], state["voicing"]]):
                    continue
                chosen: list[dict | None] = [None] * n
                chosen[k - 1], chosen[k] = prev_state, state

                for j in range(k - 2, -1, -1):
                    target = chosen[j + 1]
                    chosen[j] = min(
                        states_by_seg[j],
                        key=lambda candidate, t=target, jj=j + 1:
                            _loose_transition_cost(score_profile, inp, ranges,
                                                   candidate, t, jj),
                    )

                for j in range(k + 1, n):
                    source = chosen[j - 1]
                    chosen[j] = min(
                        states_by_seg[j],
                        key=lambda candidate, s=source, jj=j:
                            _loose_transition_cost(score_profile, inp, ranges,
                                                   s, candidate, jj),
                    )

                total = 0.0
                breakdown: dict[str, float] = {}
                for j in range(n):
                    costs = _transition_cost(
                        score_profile, inp.key_root_pc,
                        chosen[j]["chord"], chosen[j]["voicing"],
                        chosen[j - 1]["chord"] if j else None,
                        chosen[j - 1]["voicing"] if j else None,
                        True, ranges, j,
                    )
                    if costs is not None:
                        total += sum(costs[0].values())
                        breakdown = _add_dicts(breakdown, costs[0])
                candidate = {"cost": total, "breakdown": breakdown, "states": chosen}
                metrics = path_metrics(
                    [s["voicing"] for s in chosen], ranges, inp.profile()
                )
                path = _rejected_path_dict(
                    "rejected-parallel", "evidence", candidate,
                    inp, metrics, "parallel",
                )
                path["_states"] = chosen
                return path
    return None


def _rejection_reason(metrics, candidate) -> str:
    if metrics["forbidden_count"]:
        return f"含 {metrics['forbidden_count']} 项禁行/超限进行"
    if metrics["range_violation_count"]:
        return f"含 {metrics['range_violation_count']} 个音域违例"
    if candidate["cost"]:
        return f"总代价较高（{candidate['cost']:.2f}），在全句排序中被淘汰"
    return "同代价 tie-band 中由种子规则淘汰"


def find_matching_path_id(path_report, result: SolveResult) -> str | None:
    if not result.segments:
        return None
    actual = [tuple(s.voicing) for s in result.segments]
    for path in path_report["paths"]:
        candidate = [tuple(row["voicing"][v] for v in VOICES) for row in path["rows"]]
        if candidate == actual:
            return path["id"]
    return None


# ---------------------------------------------------------------------------
# Crossing candidates and point inspection
# ---------------------------------------------------------------------------


def crossing_candidates(inp: ArrangementInput, segment: int, limit: int = 8) -> list[dict]:
    seg = inp.segments[segment]
    ranges = inp.voice_ranges()
    found = []
    for chord in seg.chord_candidates:
        lists, _ = _voice_lists_for_lab(chord, seg, ranges)
        # Product is small for the authored diagnostic score.
        for s in lists[0]:
            for a in lists[1]:
                for t in lists[2]:
                    for b in lists[3]:
                        row = (s, a, t, b)
                        crosses = [i for i in range(3) if row[i] < row[i + 1]]
                        if not crosses:
                            continue
                        metrics = path_metrics([row], ranges, inp.profile())
                        found.append({
                            "segment": segment,
                            "chord_symbol": chord.symbol,
                            "voicing": {v: row[i] for i, v in enumerate(VOICES)},
                            "names": {v: pitch_to_name(row[i], inp.key_root_pc)
                                      for i, v in enumerate(VOICES)},
                            "crossing_pairs": [
                                [VOICES[i], VOICES[i + 1]] for i in crosses
                            ],
                            "crosses_alto_tenor": 1 in crosses,
                            "metrics": metrics,
                            "audibly_consonant": all(
                                p % 12 in set(chord.pitch_classes) for p in row
                            ),
                        })
    found.sort(key=lambda c: (
        0 if c["crosses_alto_tenor"] else 1,
        len(c["crossing_pairs"]),
        c["metrics"]["range_violation_count"],
        c["voicing"]["alto"],
        c["voicing"]["tenor"],
    ))
    return found[:limit]


def _actual_sounding(segment_result, key_root_pc: int, segment: int) -> dict:
    return {
        "chord_symbol": segment_result.chord.symbol,
        "pitches": {v: segment_result.voicing[i] for i, v in enumerate(VOICES)},
        "names": {
            v: pitch_to_name(segment_result.voicing[i], key_root_pc)
            for i, v in enumerate(VOICES)
        },
        "ownership": {v: VOICE_LABEL_ZH[v] for v in VOICES},
        "violations": [
            v for v in segment_result.violations if v.get("segment") == segment
        ],
    }


def inspect_segment(inp: ArrangementInput, result: SolveResult | None, segment: int) -> dict:
    ranges = inp.voice_ranges()
    count_report, counts = _segment_states(inp, relaxed=True)
    seg_counts = next((c for c in counts if c["segment"] == segment), {})
    sounding = None
    if result and segment < len(result.segments):
        sounding = _actual_sounding(result.segments[segment], inp.key_root_pc, segment)
    return {
        "segment": segment,
        "actual_sounding": sounding,
        "candidate_count": seg_counts.get("state_count", 0),
        "legal_count": seg_counts.get("enumerated_legal_count", 0),
        "raw_combination_count": seg_counts.get("raw_combination_count", 0),
        "candidate_counts": counts,
        "crossing_candidates": crossing_candidates(inp, segment),
    }


VOICE_LABEL_ZH = {
    "soprano": "女高", "alto": "女低/中声部",
    "tenor": "男高/次中", "bass": "男低",
}


# ---------------------------------------------------------------------------
# Comparison and lock trace
# ---------------------------------------------------------------------------


def _result_path(result: SolveResult):
    return [tuple(s.voicing) for s in result.segments]


def compare_experiments(body: dict) -> dict:
    doc = _resolve_doc(body)
    priority = body.get("priority") or doc.get("priority") or "phrase"
    requested_planner = body.get("planner") or doc.get("planner") or "phrase"
    lock = body.get("lock") or doc.get("active_lock")
    locked_doc = with_lock(doc, lock)
    locked_doc["priority"] = priority
    inp = apply_priority(input_from_document(locked_doc), priority)

    phrase_result = solve(inp)
    look_result = solve_lookahead(inp, int(body.get("lookahead", 1)))
    results = {"phrase": phrase_result, "lookahead": look_result}

    path_report = rank_paths(inp)
    selected_ids = {
        name: find_matching_path_id(path_report, result)
        for name, result in results.items()
    }

    baseline = None
    baseline_results = {}
    baseline_phrase_path = None
    if lock:
        base_inp = apply_priority(input_from_document(with_lock(doc, None)), priority)
        baseline_results = {
            "phrase": solve(base_inp),
            "lookahead": solve_lookahead(base_inp, 1),
        }
        baseline = baseline_results.get(requested_planner) or baseline_results["phrase"]
        if baseline_results["phrase"].segments:
            baseline_phrase_path = _result_path(baseline_results["phrase"])
        _attach_path_lock_impact(path_report, baseline_phrase_path, lock.get("voice"))

    variants = {}
    paths = {name: _result_path(res) for name, res in results.items()}
    for name, result in results.items():
        ranges = inp.voice_ranges()
        path = paths[name]
        metrics = path_metrics(path, ranges, inp.profile()) if path else {}
        pairwise_other = "lookahead" if name == "phrase" else "phrase"
        pairwise = count_changed_notes(path, paths[pairwise_other]) if path and paths[pairwise_other] else {}
        rewrite = {}
        if lock and name in baseline_results and baseline_results[name].segments:
            rewrite = lock_rewrite_metrics(
                path, _result_path(baseline_results[name]), lock.get("voice")
            )
        if metrics:
            metrics.update({
                "changed_notes": pairwise.get("changed_notes"),
                "far_voice_rewrites": rewrite.get("far_voice_rewrites"),
                "forbidden_progressions": (
                    metrics["parallel_perfect_count"] + metrics["crossing_count"]
                ),
                "range_violations": metrics["range_violation_count"],
                "singing_ranges": metrics["actual_ranges"],
            })
        variants[name] = {
            "planner": name,
            "requested": requested_planner == name,
            "result": result.to_dict(),
            "events": arrangement_to_events(result) if result.segments else None,
            "metrics": {**metrics, "pairwise_difference": pairwise, "lock_impact": rewrite},
            "selected_path_id": selected_ids.get(name),
        }

    trace = unlock_trace(doc, priority) if body.get("include_trace", True) and doc.get("lock_sequence") else None
    fingerprint = score_fingerprint(doc)
    decision = benchmark_policy(doc, repeats=1)["recommendation"] if body.get("benchmark", True) else None
    inspection_segment = int(body.get("inspect_segment", 1 if len(inp.segments) > 1 else 0))
    active_result = results.get(requested_planner, phrase_result)
    inspector = inspect_segment(inp, active_result, inspection_segment)
    inspector["actual_soundings"] = {
        name: _actual_sounding(result.segments[inspection_segment], inp.key_root_pc, inspection_segment)
        for name, result in results.items()
        if inspection_segment < len(result.segments)
    }

    return {
        "score_id": doc.get("score_id", "custom"),
        "title": doc.get("title", "和声盲比实验"),
        "fingerprint": fingerprint,
        "key_root_pc": inp.key_root_pc,
        "priority": priority,
        "active_lock": lock,
        "requested_planner": requested_planner,
        "variants": variants,
        "path_lab": path_report,
        "lock_trace": trace,
        "inspector": inspector,
        "policy_decision": decision,
        "breath_marks": doc.get("breath_marks", []),
        "preserve": doc.get("preserve", []),
    }


def _resolve_doc(body: dict) -> dict:
    if body.get("document"):
        return copy.deepcopy(body["document"])
    score_id = body.get("score_id", "breath_phrase")
    return score_document(score_id)


def unlock_trace(doc: dict, priority: str = "phrase") -> dict:
    """Remove authored locks one at a time and locate the last clean state."""
    sequence = copy.deepcopy(doc.get("lock_sequence", []))
    if not sequence:
        return {"steps": [], "first_violation_step": None, "return_to_step": None}

    active = copy.deepcopy(sequence)
    steps = []

    def evaluate(step_index, locks, removed=None):
        current = copy.deepcopy(doc)
        current.pop("lock_sequence", None)
        for lock in locks:
            current["segments"][lock["segment"]].setdefault("fixed", {})[lock["voice"]] = lock["midi"]
        inp = apply_priority(input_from_document(current), priority)
        t0 = time.perf_counter()
        phrase = solve(inp)
        look = solve_lookahead(inp, 1)
        elapsed = (time.perf_counter() - t0) * 1000
        worst = phrase if _severity(phrase) >= _severity(look) else look
        path = _result_path(worst)
        metrics = path_metrics(path, inp.voice_ranges(), inp.profile()) if path else {
            "forbidden_count": 0, "range_violation_count": 0,
            "crossing_count": 0, "first_parallel": None,
        }
        violation_total = (
            len(phrase.violations) + len(look.violations)
            + metrics.get("forbidden_count", 0)
            + metrics.get("range_violation_count", 0)
        )
        step = {
            "step": step_index,
            "active_locks": copy.deepcopy(locks),
            "removed": removed,
            "phrase_status": phrase.status,
            "lookahead_status": look.status,
            "phrase_violations": len(phrase.violations),
            "lookahead_violations": len(look.violations),
            "metric_violations": violation_total,
            "clean": phrase.status == "optimal" and look.status == "optimal" and violation_total == 0,
            "elapsed_ms": round(elapsed, 2),
            "inspector": inspect_segment(inp, worst, _violation_segment(phrase, look, metrics)),
        }
        steps.append(step)
        return step

    evaluate(0, active)
    for i, lock in enumerate(sequence, 1):
        active = [x for j, x in enumerate(active) if not (
            x["segment"] == lock["segment"] and x["voice"] == lock["voice"]
        )]
        evaluate(i, active, removed=lock)

    first_violation_step = next((s["step"] for s in steps if not s["clean"]), None)
    # In the undo direction, step 0 has all locks.  The clean state immediately
    # before the first forward-added violation is the first clean undo step.
    return_to_step = next((s["step"] for s in steps if s["clean"]), None)
    return {
        "steps": steps,
        "first_violation_step": first_violation_step,
        "return_to_step": return_to_step,
        "instruction": "从全锁状态逐个撤销；return_to_step 是首个可实际发声且无违例的检查点。",
    }


def _severity(result: SolveResult) -> int:
    return {"optimal": 0, "recovered": 1, "infeasible": 2}.get(result.status, 3)


def _violation_segment(phrase, look, metrics) -> int:
    for result in (phrase, look):
        if result.violations:
            return int(result.violations[0].get("segment", 0))
    fp = metrics.get("first_parallel")
    if fp:
        return int(fp["segment"])
    crossings = metrics.get("crossing_events") or []
    if crossings:
        return int(crossings[0]["segment"])
    return 1


# ---------------------------------------------------------------------------
# Empirical policy choice and persistence
# ---------------------------------------------------------------------------


def benchmark_policy(doc: dict | None = None, repeats: int = 3) -> dict:
    docs = [doc] if doc else [score_document(sid) for sid in LAB_SCORES]
    measurements = []
    for one_doc in docs:
        for priority in ("phrase", "local"):
            inp = apply_priority(input_from_document(one_doc), priority)
            for planner, runner in (
                ("phrase", lambda: solve(inp)),
                ("lookahead", lambda: solve_lookahead(inp, 1)),
            ):
                # Warm call exercises imports/caches; then median-ish timing.
                runner()
                times = []
                result = None
                for _ in range(max(1, repeats)):
                    t0 = time.perf_counter()
                    result = runner()
                    times.append((time.perf_counter() - t0) * 1000)
                path = _result_path(result)
                metrics = path_metrics(path, inp.voice_ranges(), inp.profile()) if path else {}
                quality_penalty = (
                    10000 if result.status == "infeasible" else
                    500 if result.status == "recovered" else 0
                )
                quality_penalty += (
                    metrics.get("forbidden_count", 0) * 100
                    + metrics.get("range_violation_count", 0) * 250
                )
                measurements.append({
                    "score_id": one_doc.get("score_id", "custom"),
                    "fingerprint": score_fingerprint(one_doc),
                    "planner": planner,
                    "priority": priority,
                    "status": result.status,
                    "total_cost": None if result.total_cost == float("inf") else round(result.total_cost, 3),
                    "forbidden_count": metrics.get("forbidden_count"),
                    "range_violation_count": metrics.get("range_violation_count"),
                    "elapsed_ms": round(sorted(times)[len(times) // 2], 2),
                    "measured_score": round(quality_penalty + result.total_cost + sorted(times)[len(times) // 2] * 0.01, 3)
                    if result.total_cost != float("inf") else float("inf"),
                })
    preferred_priority = (doc or {}).get("priority", "phrase")
    if preferred_priority not in ("phrase", "local"):
        preferred_priority = "phrase"
    comparable = [
        m for m in measurements
        if m["priority"] == preferred_priority
        and (doc is None or m["score_id"] == doc.get("score_id"))
    ] or measurements
    best = min(comparable, key=lambda m: m["measured_score"])
    planner_by_priority = {}
    for priority in ("phrase", "local"):
        group = [m for m in measurements if m["priority"] == priority]
        if group:
            winner = min(group, key=lambda m: m["measured_score"])
            planner_by_priority[priority] = winner["planner"]
    return {
        "measurements": measurements,
        "planner_by_priority": planner_by_priority,
        "recommendation": {
            "planner": best["planner"],
            "priority": preferred_priority,
            "score_id": best["score_id"],
            "fingerprint": best["fingerprint"],
            "reason": "在同一优先级权重内比较：先排除 infeasible，再惩罚禁行/音域违例，随后比较总代价与中位耗时；不跨优先级比较加权代价。",
        },
    }


def load_policies() -> dict:
    if not POLICY_PATH.exists():
        return {"scores": {}}
    try:
        return json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"scores": {}}


def get_policy(score_id: str | None = None, fingerprint: str | None = None) -> dict | None:
    data = load_policies()
    scores = data.get("scores", {})
    if fingerprint and fingerprint in scores:
        return scores[fingerprint]
    if score_id or fingerprint:
        return None
    return data.get("default")


def save_policy(policy: dict) -> dict:
    data = load_policies()
    data.setdefault("scores", {})
    fingerprint = policy.get("fingerprint")
    if not fingerprint and policy.get("document"):
        fingerprint = score_fingerprint(policy["document"])
    if not fingerprint and policy.get("score_id") in LAB_SCORES:
        fingerprint = score_fingerprint(score_document(policy["score_id"]))
    fingerprint = fingerprint or "global"
    record = {
        "score_id": policy.get("score_id"),
        "fingerprint": fingerprint,
        "planner": policy.get("planner", "phrase"),
        "priority": policy.get("priority", "phrase"),
        "lock": policy.get("lock"),
        "blind_choice": policy.get("blind_choice"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    data["scores"][fingerprint] = record
    data["default"] = record
    POLICY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return record
