"""SATB voicing solver.

The solver never decides each beat independently: a beam-search dynamic
program carries complete partial paths forward, so every transition is scored
with both the previous and next segments in view.

Hard conditions and preference costs are separate:
* hard condition failure rejects a candidate path;
* preference failure adds a named, itemized cost.

Voice ranges can never be silently broken. When the strict pass cannot find a
path (including the case where a fixed soprano and a fixed bass together
straddle a range/spacing boundary), two explicit recovery strategies run and
their full paths are reported for comparison:
  * ``local``   -- relax at the failing segment and continue;
  * ``lookback``-- re-open the previous two chords and re-solve them jointly.
Every relaxed boundary is returned as a recorded violation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .rules import StyleProfile, get_profile, HARD, PREFERENCE, OFF
from .theory import VOICES, VOICE_RANGES, Chord, chord_pcs

RELAX_PENALTY = 1000.0  # cost per relaxed hard condition; soft costs stay below 1.0-ish weight


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    """One timespan (a chord beat/bar).

    ``chord_candidates`` are functional/symbol candidates the solver may pick
    from. ``fixed`` maps voice name -> MIDI pitch for the few notes the user
    pins. Durations are given in beats and used verbatim in the event export.
    """

    chord_candidates: tuple[Chord, ...]
    fixed: dict[str, int] = field(default_factory=dict)
    duration: float = 1.0
    label_hint: str = ""

    @staticmethod
    def coerce(data) -> "Segment":
        if isinstance(data, Segment):
            return data
        fixed = {v: int(p) for v, p in (data.get("fixed") or {}).items()}
        chords = tuple(_coerce_chord(c) for c in data["chord_candidates"])
        return Segment(
            chord_candidates=chords,
            fixed=fixed,
            duration=float(data.get("duration", 1.0)),
            label_hint=str(data.get("label_hint", "")),
        )


def _coerce_chord(data) -> Chord:
    if isinstance(data, Chord):
        return data
    if "root_pc" in data:
        return Chord(
            int(data["root_pc"]) % 12,
            str(data["quality"]),
            str(data.get("label", "")),
        )
    # Functional label against a key is resolved by the caller; the web/CLI
    # layer turns function labels into Chord objects before reaching here.
    raise ValueError(f"cannot coerce chord from {data!r}")


@dataclass
class ArrangementInput:
    segments: list[Segment]
    key_root_pc: int = 0
    style: str = "common_practice_strict"
    overrides: dict | None = None
    ranges: dict[str, tuple[int, int]] | None = None
    beam_width: int = 40
    seed: int = 0
    candidate_cap: int = 0  # 0 == no cap; otherwise per-chord enumeration cap

    def profile(self) -> StyleProfile:
        return get_profile(self.style, self.overrides)

    def voice_ranges(self) -> dict[str, tuple[int, int]]:
        ranges = dict(VOICE_RANGES)
        if self.ranges:
            for voice, bounds in self.ranges.items():
                ranges[voice] = (int(bounds[0]), int(bounds[1]))
        return ranges


# ---------------------------------------------------------------------------
# Result data
# ---------------------------------------------------------------------------

@dataclass
class SegmentResult:
    index: int
    chord: Chord
    voicing: tuple[int, int, int, int]
    cost_breakdown: dict[str, float]
    violations: list[dict]


@dataclass
class SolveResult:
    status: str                     # "optimal" | "recovered" | "infeasible"
    style: str
    seed: int
    beam_width: int
    key_root_pc: int
    segments: list[SegmentResult]
    total_cost: float
    cost_breakdown: dict[str, float]
    violations: list[dict]
    diagnostics: list[str]
    beam_trace: list[dict]
    recovery: dict | None
    input_snapshot: dict

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "style": self.style,
            "seed": self.seed,
            "beam_width": self.beam_width,
            "key_root_pc": self.key_root_pc,
            "total_cost": round(self.total_cost, 4),
            "cost_breakdown": {k: round(v, 4) for k, v in self.cost_breakdown.items()},
            "violations": self.violations,
            "diagnostics": self.diagnostics,
            "beam_trace": self.beam_trace,
            "recovery": self.recovery,
            "segments": [
                {
                    "index": seg.index,
                    "chord": {"root_pc": seg.chord.root_pc,
                              "quality": seg.chord.quality,
                              "label": seg.chord.label,
                              "symbol": seg.chord.symbol,
                              "pitch_classes": list(seg.chord.pitch_classes)},
                    "voicing": dict(zip(VOICES, seg.voicing)),
                    "cost_breakdown": {k: round(v, 4) for k, v in seg.cost_breakdown.items()},
                    "violations": seg.violations,
                }
                for seg in self.segments
            ],
            "input_snapshot": self.input_snapshot,
        }


# ---------------------------------------------------------------------------
# Candidate voicing enumeration
# ---------------------------------------------------------------------------

def _voice_pitch_lists(chord: Chord, seg: Segment, ranges, allow_out_of_range):
    pcs = chord_pcs(chord)
    lists = []
    out_of_range = {}
    for voice in VOICES:
        low, high = ranges[voice]
        if voice in seg.fixed:
            fixed_pitch = seg.fixed[voice]
            if fixed_pitch % 12 not in pcs:
                # Genuinely impossible; recorded by the pre-check as well.
                lists.append([fixed_pitch])
                continue
            if not (low <= fixed_pitch <= high):
                out_of_range[voice] = fixed_pitch
            lists.append([fixed_pitch])
            continue
        pitches = [p for pc in pcs for p in range(low, high + 1) if p % 12 == pc]
        if not pitches:
            # Range contains no chord tone at all; use nearest octave copy so
            # recovery paths can still be built and the breach is recorded.
            nearest = min(
                (p for pc in pcs for p in range(low - 12, high + 13) if p % 12 == pc),
                key=lambda p: min(abs(p - low), abs(p - high)),
            )
            pitches = [nearest]
            out_of_range[voice] = nearest
        lists.append(pitches)
    if out_of_range and not allow_out_of_range:
        return lists, out_of_range
    return lists, out_of_range


def _enumerate_voicings(chord, seg, ranges, allow_out_of_range, cap, rng):
    lists, out_of_range = _voice_pitch_lists(chord, seg, ranges, allow_out_of_range)
    if not allow_out_of_range and out_of_range:
        return [], out_of_range

    results = []

    def backtrack(idx, chosen):
        if cap and len(results) >= cap:
            return
        if idx == 4:
            results.append(tuple(chosen))
            return
        for pitch in lists[idx]:
            if idx > 0 and pitch > chosen[-1]:
                continue  # S >= A >= T >= B
            if idx > 0 and chosen[-1] - pitch > 24:
                continue  # structural guard, never an authored style rule
            chosen.append(pitch)
            backtrack(idx + 1, chosen)
            chosen.pop()

    backtrack(0, [])
    if cap and len(results) > cap:
        rng.shuffle(results)
        results = results[:cap]
        results.sort()
    return results, out_of_range


# ---------------------------------------------------------------------------
# Transition scoring (one chord change, so context spans both segments)
# ---------------------------------------------------------------------------

PERFECT_INTERVALS = {0, 7, 12}


def _is_perfect(a, b):
    return abs(a - b) % 12 in (0, 7) and abs(a - b) <= 24


def _transition_cost(profile, key_root_pc, chord, voicing, prev_chord, prev_voicing,
                     relaxed, ranges, segment_index):
    costs: dict[str, float] = {}
    violations: list[dict] = []

    def note(rule, voice, detail, hard_rule):
        violations.append({
            "segment": segment_index,
            "rule": rule,
            "voice": voice,
            "detail": detail,
            "severity": "hard_relaxed" if (hard_rule and relaxed) else "preference",
        })

    # --- range violations only arise in relaxed mode ----------------------
    for i, voice in enumerate(VOICES):
        low, high = ranges[voice]
        if not (low <= voicing[i] <= high):
            note("range", voice,
                 f"pitch {voicing[i]} outside [{low},{high}]", True)
            costs["range"] = costs.get("range", 0.0) + RELAX_PENALTY

    # --- upper spacing -----------------------------------------------------
    level = profile.level("upper_spacing")
    limit = profile.params.get("max_spacing", 12.0)
    for i, j in ((0, 1), (1, 2)):
        gap = voicing[i] - voicing[j]
        if gap > limit:
            detail = f"{VOICES[i]}-{VOICES[j]} gap {gap}st > limit {limit:g}st"
            if level == HARD:
                if not relaxed:
                    return None
                note("upper_spacing", VOICES[i], detail, True)
                costs["upper_spacing"] = costs.get("upper_spacing", 0.0) + RELAX_PENALTY
            elif level == PREFERENCE:
                costs["upper_spacing"] = costs.get("upper_spacing", 0.0) + (
                    profile.weight("upper_spacing") * (gap - limit)
                )

    # --- melodic leap per voice -------------------------------------------
    level = profile.level("max_leap")
    leap_limit = profile.params.get("max_leap", 12.0)
    if prev_voicing is not None:
        for i, voice in enumerate(VOICES):
            leap = abs(voicing[i] - prev_voicing[i])
            if leap > leap_limit:
                detail = f"leap {leap}st > limit {leap_limit:g}st"
                if level == HARD:
                    if not relaxed:
                        return None
                    note("max_leap", voice, detail, True)
                    costs["max_leap"] = costs.get("max_leap", 0.0) + RELAX_PENALTY
                elif level == PREFERENCE:
                    costs["max_leap"] = costs.get("max_leap", 0.0) + (
                        profile.weight("max_leap") * (leap - leap_limit)
                    )

    # --- smoothness (general, always a preference) ------------------------
    if prev_voicing is not None and profile.level("smoothness") != OFF:
        moved = sum(abs(voicing[i] - prev_voicing[i]) for i in range(4))
        costs["smoothness"] = (
            profile.weight("smoothness")
            * profile.params.get("movement_weight", 1.0)
            * moved
        )

    # --- common tones kept in the same voice ------------------------------
    if prev_voicing is not None and profile.level("common_tone") != OFF:
        prev_pcs = {p % 12 for p in prev_voicing}
        shared = prev_pcs & set(chord_pcs(chord))
        kept = sum(
            1 for i in range(4)
            if voicing[i] % 12 in shared and voicing[i] == prev_voicing[i]
        )
        if shared:
            costs["common_tone"] = profile.weight("common_tone") * (len(shared) - kept)

    # --- parallel / hidden perfect intervals ------------------------------
    if prev_voicing is not None:
        pp_level = profile.level("parallel_perfect")
        pp_hits = 0
        for i in range(4):
            for j in range(i + 1, 4):
                before, after = abs(prev_voicing[i] - prev_voicing[j]), abs(voicing[i] - voicing[j])
                if (before % 12 in (0, 7) and after % 12 in (0, 7)
                        and prev_voicing[i] != prev_voicing[j]
                        and voicing[i] != voicing[j]
                        and (voicing[i] - prev_voicing[i]) * (voicing[j] - prev_voicing[j]) > 0):
                    pp_hits += 1
        if pp_hits:
            detail = f"{pp_hits} parallel perfect interval(s)"
            if pp_level == HARD:
                if not relaxed:
                    return None
                note("parallel_perfect", None, detail, True)
                costs["parallel_perfect"] = costs.get("parallel_perfect", 0.0) + (
                    RELAX_PENALTY * pp_hits
                )
            elif pp_level == PREFERENCE:
                costs["parallel_perfect"] = costs.get("parallel_perfect", 0.0) + (
                    profile.weight("parallel_perfect") * pp_hits
                )

        if profile.level("hidden_parallel") == PREFERENCE:
            s_move, b_move = voicing[0] - prev_voicing[0], voicing[3] - prev_voicing[3]
            outer = abs(voicing[0] - voicing[3])
            if s_move * b_move > 0 and outer % 12 in (0, 7) and outer > 0:
                costs["hidden_parallel"] = costs.get("hidden_parallel", 0.0) + (
                    profile.weight("hidden_parallel")
                )

    # --- leading-tone resolution ------------------------------------------
    if prev_voicing is not None and profile.level("leading_tone") != OFF:
        leading_pc = (key_root_pc - 1) % 12
        tonic_pc = key_root_pc % 12
        breaches = 0
        for i in range(4):
            if prev_voicing[i] % 12 == leading_pc:
                if not (voicing[i] % 12 == tonic_pc
                        and voicing[i] - prev_voicing[i] == 1):
                    breaches += 1
        if breaches:
            costs["leading_tone"] = costs.get("leading_tone", 0.0) + (
                profile.weight("leading_tone") * breaches
            )

    # --- chord-seventh downward resolution --------------------------------
    if prev_voicing is not None and profile.level("seventh_resolution") != OFF:
        offsets = dict(zip(chord_pcs(prev_chord),
                           (off % 12 for off in _quality_offsets(prev_chord))))
        seventh_pcs = {pc for pc, off in offsets.items() if off in (10, 11, 9)}
        breaches = 0
        for i in range(4):
            if prev_voicing[i] % 12 in seventh_pcs:
                if voicing[i] - prev_voicing[i] != -2:
                    breaches += 1
        if breaches:
            costs["seventh_resolution"] = costs.get("seventh_resolution", 0.0) + (
                profile.weight("seventh_resolution") * breaches
            )

    # --- doubling heuristic -----------------------------------------------
    if profile.level("doubling") != OFF:
        costs["doubling"] = costs.get("doubling", 0.0) + _doubling_cost(
            profile, chord, voicing
        )

    # --- chord completeness -----------------------------------------------
    if profile.level("completeness") != OFF:
        missing = len(set(chord_pcs(chord)) - {p % 12 for p in voicing})
        if missing:
            costs["completeness"] = costs.get("completeness", 0.0) + (
                profile.weight("completeness") * missing
            )

    return costs, violations


def _quality_offsets(chord: Chord):
    from .theory import QUALITY_OFFSETS
    return QUALITY_OFFSETS[chord.quality]


def _doubling_cost(profile, chord, voicing):
    weight = profile.weight("doubling")
    pcs = chord_pcs(chord)
    if len(pcs) >= 4:
        # Seventh/extended chords: prefer each note once; doubled members
        # other than the root are mildly discouraged.
        counts = {}
        for p in voicing:
            counts[p % 12] = counts.get(p % 12, 0) + 1
        extra = sum(c - 1 for pc, c in counts.items() if pc != chord.root_pc and c > 1)
        return weight * extra
    counts = {}
    for p in voicing:
        counts[p % 12] = counts.get(p % 12, 0) + 1
    third_pc = pcs[1] if len(pcs) > 1 else None
    root_pc = chord.root_pc
    if chord.quality == "dim":
        preferred = third_pc
    else:
        preferred = root_pc
    score = 0.0
    for pc, count in counts.items():
        if count > 1 and pc != preferred:
            score += 1.0 + (0.5 if pc == third_pc and chord.quality != "dim" else 0.0)
    return weight * score


# ---------------------------------------------------------------------------
# Beam search
# ---------------------------------------------------------------------------

@dataclass
class _State:
    chord: Chord
    voicing: tuple[int, int, int, int]
    cost: float
    breakdown: dict[str, float]
    violations: list[dict]
    per_segment: list[SegmentResult]
    beam_size: int


def _beam_prune(states, beam_width, rng):
    """Prune to ``beam_width`` states.

    Returns ``(kept_states, tie_band_size)``. Equal-cost states at the cutoff
    form the tie band: seeded jitter decides which survive, and the band size
    is exported so the random choice's candidate range is fully documented.
    """
    if len(states) <= beam_width:
        return states, 0
    ordered = sorted(states, key=lambda s: s.cost)
    tie_cost = ordered[beam_width - 1].cost
    tie_band = [s for s in ordered if s.cost == tie_cost]
    if len(tie_band) > 1:
        rng.shuffle(tie_band)
        rank = {id(s): i for i, s in enumerate(tie_band)}
        ordered.sort(key=lambda s: (s.cost, rank.get(id(s), 0)))
    return ordered[:beam_width], len(tie_band)


def _precheck(inp: ArrangementInput, ranges) -> list[str]:
    diagnostics = []
    for k, seg0 in enumerate(inp.segments):
        seg = Segment.coerce(seg0)
        for voice, pitch in seg.fixed.items():
            if voice not in VOICES:
                diagnostics.append(f"segment {k}: unknown voice {voice!r}")
                continue
            low, high = ranges[voice]
            if not (low <= pitch <= high):
                diagnostics.append(
                    f"segment {k}: fixed {voice} note {pitch} is outside "
                    f"declared range [{low}, {high}]"
                )
            matching = [c for c in seg.chord_candidates if pitch % 12 in chord_pcs(c)]
            if not matching:
                diagnostics.append(
                    f"segment {k}: fixed {voice} note {pitch} belongs to none "
                    f"of the chord candidates {[c.symbol for c in seg.chord_candidates]}"
                )
    return diagnostics


def solve(inp: ArrangementInput) -> SolveResult:
    profile = inp.profile()
    ranges = inp.voice_ranges()
    rng = random.Random(inp.seed)
    segments = [Segment.coerce(s) for s in inp.segments]
    diagnostics = _precheck(inp, ranges)

    hard_blockers = [d for d in diagnostics if "belongs to none" in d or "unknown voice" in d]
    if hard_blockers:
        return _infeasible(inp, hard_blockers, segments, ranges, profile)

    strict = _beam_search(segments, profile, ranges, inp, relaxed=False, rng=rng)
    if not isinstance(strict, _Fail) and strict is not None:
        return _build_result("optimal", strict, inp, segments, diagnostics, recovery=None)

    # Strict pass failed: run the two explicit recovery strategies.
    fail_index = strict.failed_at if isinstance(strict, _Fail) else 0
    local = _recovery(segments, profile, ranges, inp, rng, fail_index, lookback=False)
    lookback = _recovery(segments, profile, ranges, inp, rng, fail_index, lookback=True)

    options = [r for r in (local, lookback) if r is not None]
    if not options:
        return _infeasible(inp, diagnostics + ["recovery strategies produced no path"],
                           segments, ranges, profile)
    options.sort(key=lambda r: (len(r.violations), r.cost))
    best = options[0]
    recovery = {
        "failed_segment": fail_index,
        "strategies": [
            {
                "name": r.recovery_strategy,
                "total_cost": round(r.cost, 4),
                "hard_violations": len(r.violations),
                "violations": r.violations,
                "chord_symbols": [seg.chord.symbol for seg in r.per_segment],
            }
            for r in options
        ],
        "chosen": best.recovery_strategy,
        "comparison_note": (
            "local relaxes rules only at/after the failing chord; lookback "
            "re-opens the previous two chords and re-solves them jointly. "
            "Fewer hard violations wins, then lower total cost."
        ),
    }
    return _build_result("recovered", best, inp, segments, diagnostics, recovery)


class _Fail:
    def __init__(self, failed_at):
        self.failed_at = failed_at


def _beam_search(segments, profile, ranges, inp, relaxed, rng,
                 prefix_states=None, start_index=0):
    beam_width = inp.beam_width
    cap = inp.candidate_cap
    beam_trace = []
    states = prefix_states

    for k in range(start_index, len(segments)):
        seg = segments[k]
        layer = {}
        enumerated_total = 0
        for chord in seg.chord_candidates:
            voicings, oor = _enumerate_voicings(
                chord, seg, ranges, allow_out_of_range=relaxed, cap=cap, rng=rng
            )
            if not relaxed and oor:
                return _Fail(k)
            enumerated_total += len(voicings)
            for voicing in voicings:
                if states is None:
                    scored = _transition_cost(
                        profile, inp.key_root_pc, chord, voicing,
                        None, None, relaxed, ranges, k
                    )
                    if scored is None:
                        continue
                    costs, viols = scored
                    state = _State(
                        chord, voicing, sum(costs.values()), dict(costs),
                        list(viols),
                        [SegmentResult(k, chord, voicing, costs, list(viols))],
                        0,
                    )
                else:
                    best_here = None
                    for prev in states:
                        scored = _transition_cost(
                            profile, inp.key_root_pc, chord, voicing,
                            prev.chord, prev.voicing, relaxed, ranges, k
                        )
                        if scored is None:
                            continue
                        costs, viols = scored
                        cand_cost = prev.cost + sum(costs.values())
                        if best_here is None or cand_cost < best_here.cost:
                            best_here = _State(
                                chord, voicing, cand_cost,
                                _add_dicts(prev.breakdown, costs),
                                prev.violations + viols,
                                prev.per_segment + [
                                    SegmentResult(k, chord, voicing, costs, list(viols))
                                ],
                                0,
                            )
                    state = best_here
                if state is None:
                    continue
                key = (chord.root_pc, chord.quality, voicing)
                if key not in layer or state.cost < layer[key].cost:
                    layer[key] = state
        if not layer:
            return _Fail(k)
        pruned, tie_band_size = _beam_prune(list(layer.values()), beam_width, rng)
        beam_trace.append({
            "segment": k,
            "candidates_enumerated": enumerated_total,
            "distinct_states": len(layer),
            "beam_kept": len(pruned),
            "tie_band_size": tie_band_size,
        })
        states = pruned

    if states is None:
        return _Fail(0)
    best = min(states, key=lambda s: s.cost)
    best.beam_size = len(states)
    best.beam_trace = beam_trace
    return best


def _recovery(segments, profile, ranges, inp, rng, fail_index, lookback):
    # Build a strict prefix up to the restart point.
    restart = max(0, fail_index - (2 if lookback else 0))
    prefix_states = None
    trace = []
    if restart > 0:
        head = _beam_search(
            segments[:restart], profile, ranges, inp, relaxed=False,
            rng=random.Random(inp.seed),
        )
        if isinstance(head, _Fail) or head is None:
            return None
        # Reconstruct the full beam at the restart point by re-running the
        # strict pass with a wide beam, then carrying it into relaxed mode.
        wide = ArrangementInput(
            segments=segments, key_root_pc=inp.key_root_pc,
            style=inp.style, overrides=inp.overrides, ranges=inp.ranges,
            beam_width=max(inp.beam_width, 200), seed=inp.seed,
            candidate_cap=inp.candidate_cap,
        )
        head_wide = _beam_search(
            segments[:restart], profile, ranges, wide, relaxed=False,
            rng=random.Random(inp.seed),
        )
        if isinstance(head_wide, _Fail):
            return None
        prefix_states = [head_wide]
        trace = getattr(head_wide, "beam_trace", [])
    tail = _beam_search(
        segments, profile, ranges, inp, relaxed=True,
        rng=random.Random(inp.seed + (97 if lookback else 13)),
        prefix_states=prefix_states, start_index=restart,
    )
    if isinstance(tail, _Fail) or tail is None:
        return None
    tail.recovery_strategy = "lookback" if lookback else "local"
    tail.beam_trace = trace + getattr(tail, "beam_trace", [])
    return tail


def _add_dicts(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def _infeasible(inp, diagnostics, segments, ranges, profile) -> SolveResult:
    return SolveResult(
        status="infeasible",
        style=inp.style,
        seed=inp.seed,
        beam_width=inp.beam_width,
        key_root_pc=inp.key_root_pc,
        segments=[],
        total_cost=float("inf"),
        cost_breakdown={},
        violations=[],
        diagnostics=list(diagnostics),
        beam_trace=[],
        recovery=None,
        input_snapshot=_snapshot(inp, segments),
    )


def _build_result(status, state, inp, segments, diagnostics, recovery) -> SolveResult:
    return SolveResult(
        status=status,
        style=inp.style,
        seed=inp.seed,
        beam_width=inp.beam_width,
        key_root_pc=inp.key_root_pc,
        segments=state.per_segment,
        total_cost=state.cost,
        cost_breakdown=dict(state.breakdown),
        violations=list(state.violations),
        diagnostics=list(diagnostics),
        beam_trace=getattr(state, "beam_trace", []),
        recovery=recovery,
        input_snapshot=_snapshot(inp, segments),
    )


def _snapshot(inp, segments):
    return {
        "key_root_pc": inp.key_root_pc,
        "style": inp.style,
        "overrides": inp.overrides,
        "ranges": inp.ranges,
        "beam_width": inp.beam_width,
        "seed": inp.seed,
        "candidate_cap": inp.candidate_cap,
        "segments": [
            {
                "fixed": dict(seg.fixed),
                "duration": seg.duration,
                "chord_candidates": [
                    {"root_pc": c.root_pc, "quality": c.quality, "label": c.label}
                    for c in seg.chord_candidates
                ],
            }
            for seg in segments
        ],
    }
