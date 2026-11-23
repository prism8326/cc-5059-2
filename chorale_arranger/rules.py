"""Style profiles: hard conditions vs. preference costs.

Design contract
---------------
* Every rule is a named :class:`RuleSpec` with a *category*:
  - ``playability``: conditions required for the result to be singable /
    consistent with the input (ranges, fixed notes, chord membership,
    no voice crossing). These are hard in every shipped profile.
  - ``common_practice``: conventions of the European common-practice
    four-voice chorale. They are **not** laws of music: another profile may
    downgrade them to preference costs or switch them off entirely.
* A rule's *level* is one of ``hard`` / ``preference`` / ``off``.
* ``hard`` violations reject a path outright; ``preference`` violations add
  a weighted cost; ``off`` rules do nothing.
* Ranges can never be silently downgraded by the solver. Relaxation happens
  only through the explicit recovery strategies in :mod:`chorale_arranger.solver`,
  and every relaxed range boundary is reported back as a violation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HARD = "hard"
PREFERENCE = "preference"
OFF = "off"


@dataclass(frozen=True)
class RuleSpec:
    key: str
    category: str          # "playability" | "common_practice"
    description: str
    default_level: str
    default_weight: float
    # Set when a rule only makes sense under a particular historical style.
    convention_note: str = ""


RULE_REGISTRY: dict[str, RuleSpec] = {
    "range": RuleSpec(
        "range", "playability",
        "Each voice stays inside its declared inclusive MIDI range.",
        HARD, 0.0,
    ),
    "chord_tone": RuleSpec(
        "chord_tone", "playability",
        "Every sung pitch is a member of the chosen chord candidate "
        "(this arranger writes chord tones only, no neighbor/passing tones).",
        HARD, 0.0,
    ),
    "fixed_notes": RuleSpec(
        "fixed_notes", "playability",
        "User-fixed pitches must appear in the declared voice and segment.",
        HARD, 0.0,
    ),
    "no_crossing": RuleSpec(
        "no_crossing", "playability",
        "Voices stay ordered S >= A >= T >= B (no voice crossing).",
        HARD, 0.0,
    ),
    "upper_spacing": RuleSpec(
        "upper_spacing", "common_practice",
        "Distance between adjacent upper voices (S-A, A-T) stays within the "
        "declared limit (default one octave).",
        HARD, 8.0,
        "The octave-spacing rule is a common-practice chorale convention; "
        "tighter and wider spacings are normal in other choral styles.",
    ),
    "max_leap": RuleSpec(
        "max_leap", "common_practice",
        "No single-voice move between segments exceeds the declared leap "
        "limit in semitones (default 12).",
        HARD, 6.0,
        "Leap limits reflect common-practice singability conventions; other "
        "styles treat large leaps as costs rather than prohibitions.",
    ),
    "parallel_perfect": RuleSpec(
        "parallel_perfect", "common_practice",
        "No parallel perfect 5ths, octaves or unisons between the same pair "
        "of voices across two segments.",
        HARD, 25.0,
        "The prohibition of parallel 5ths/octaves is a common-practice rule, "
        "not a universal property of music.",
    ),
    "hidden_parallel": RuleSpec(
        "hidden_parallel", "common_practice",
        "No hidden (direct) perfect 5th/octave between outer voices reached "
        "by similar motion.",
        PREFERENCE, 10.0,
        "Hidden-parallel treatment varies widely between theorists and styles.",
    ),
    "leading_tone": RuleSpec(
        "leading_tone", "common_practice",
        "A voice on the leading tone (scale degree 7) resolves upward by a "
        "semitone to the tonic.",
        PREFERENCE, 12.0,
        "Strict only for exposed leading tones in common-practice writing; "
        "inner-voice exceptions and other styles do not require it.",
    ),
    "seventh_resolution": RuleSpec(
        "seventh_resolution", "common_practice",
        "A chord seventh resolves downward by step.",
        PREFERENCE, 12.0,
        "A common-practice tendency-tone convention.",
    ),
    "doubling": RuleSpec(
        "doubling", "common_practice",
        "Triads prefer doubling the root (and omitting the fifth if needed); "
        "diminished triads prefer the third, first-inversion chords the soprano.",
        PREFERENCE, 5.0,
        "Doubling conventions are style-specific heuristics.",
    ),
    "completeness": RuleSpec(
        "completeness", "playability",
        "Prefer voicings that cover every pitch class of the chord.",
        PREFERENCE, 4.0,
    ),
    "smoothness": RuleSpec(
        "smoothness", "playability",
        "Cost proportional to the total semitones moved by all voices "
        "(general voice-leading smoothness, not style-specific).",
        PREFERENCE, 1.0,
    ),
    "common_tone": RuleSpec(
        "common_tone", "common_practice",
        "Prefer keeping shared chord tones in the same voice.",
        PREFERENCE, 3.0,
    ),
}


@dataclass
class StyleProfile:
    name: str
    description: str
    levels: dict[str, str] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    params: dict[str, float] = field(default_factory=dict)

    def level(self, rule_key: str) -> str:
        return self.levels.get(rule_key, RULE_REGISTRY[rule_key].default_level)

    def weight(self, rule_key: str) -> float:
        return self.weights.get(rule_key, RULE_REGISTRY[rule_key].default_weight)

    def is_hard(self, rule_key: str) -> bool:
        return self.level(rule_key) == HARD

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "levels": dict(self.levels),
            "weights": dict(self.weights),
            "params": dict(self.params),
            "rules": [
                {
                    "key": spec.key,
                    "category": spec.category,
                    "description": spec.description,
                    "convention_note": spec.convention_note,
                    "level": self.level(spec.key),
                    "weight": self.weight(spec.key),
                }
                for spec in RULE_REGISTRY.values()
            ],
        }


_CP_PARAMS = {"max_spacing": 12.0, "max_leap": 12.0, "movement_weight": 1.0}

STYLE_PROFILES: dict[str, StyleProfile] = {
    "common_practice_strict": StyleProfile(
        name="common_practice_strict",
        description=(
            "European common-practice four-voice chorale, strict setting: "
            "spacing, leap and parallel-interval conventions are hard "
            "conditions; tendency-tone and doubling conventions are weighted "
            "preferences."
        ),
        levels={
            "upper_spacing": HARD,
            "max_leap": HARD,
            "parallel_perfect": HARD,
            "hidden_parallel": PREFERENCE,
            "leading_tone": PREFERENCE,
            "seventh_resolution": PREFERENCE,
            "doubling": PREFERENCE,
            "completeness": PREFERENCE,
            "smoothness": PREFERENCE,
            "common_tone": PREFERENCE,
        },
        weights={
            "hidden_parallel": 10.0,
            "leading_tone": 14.0,
            "seventh_resolution": 14.0,
            "doubling": 5.0,
            "completeness": 4.0,
            "smoothness": 1.0,
            "common_tone": 3.0,
        },
        params=dict(_CP_PARAMS),
    ),
    "common_practice_loose": StyleProfile(
        name="common_practice_loose",
        description=(
            "Common-practice conventions kept as preference costs only. "
            "Playability rules stay hard; parallel 5ths/octaves, spacing and "
            "large leaps appear in the itemized cost instead of blocking."
        ),
        levels={
            "upper_spacing": PREFERENCE,
            "max_leap": PREFERENCE,
            "parallel_perfect": PREFERENCE,
            "hidden_parallel": PREFERENCE,
            "leading_tone": PREFERENCE,
            "seventh_resolution": PREFERENCE,
            "doubling": PREFERENCE,
            "completeness": PREFERENCE,
            "smoothness": PREFERENCE,
            "common_tone": PREFERENCE,
        },
        weights={
            "upper_spacing": 8.0,
            "max_leap": 6.0,
            "parallel_perfect": 25.0,
            "hidden_parallel": 10.0,
            "leading_tone": 12.0,
            "seventh_resolution": 12.0,
            "doubling": 5.0,
            "completeness": 4.0,
            "smoothness": 1.0,
            "common_tone": 3.0,
        },
        params=dict(_CP_PARAMS),
    ),
    "modern_chorus": StyleProfile(
        name="modern_chorus",
        description=(
            "Modern/eclectic choral voicing: playability stays hard, while "
            "historical spacing, parallel and doubling conventions are "
            "switched off or lightly weighted. Smooth voice leading and "
            "chord completeness remain preferences."
        ),
        levels={
            "upper_spacing": OFF,
            "max_leap": PREFERENCE,
            "parallel_perfect": OFF,
            "hidden_parallel": OFF,
            "leading_tone": OFF,
            "seventh_resolution": OFF,
            "doubling": OFF,
            "completeness": PREFERENCE,
            "smoothness": PREFERENCE,
            "common_tone": PREFERENCE,
        },
        weights={
            "max_leap": 3.0,
            "completeness": 4.0,
            "smoothness": 1.2,
            "common_tone": 2.0,
        },
        params={"max_spacing": 16.0, "max_leap": 19.0, "movement_weight": 1.2},
    ),
}


def get_profile(name: str, overrides: dict | None = None) -> StyleProfile:
    """Return a profile by name with optional level/weight/param overrides."""
    if name not in STYLE_PROFILES:
        raise KeyError(
            f"unknown style {name!r}; available: {', '.join(sorted(STYLE_PROFILES))}"
        )
    base = STYLE_PROFILES[name]
    profile = StyleProfile(
        name=base.name,
        description=base.description,
        levels=dict(base.levels),
        weights=dict(base.weights),
        params=dict(base.params),
    )
    if overrides:
        for key, value in overrides.get("levels", {}).items():
            if key not in RULE_REGISTRY:
                raise KeyError(f"unknown rule in override: {key!r}")
            if value not in (HARD, PREFERENCE, OFF):
                raise ValueError(f"invalid level {value!r} for rule {key!r}")
            # Playability rules may not be weakened.
            if RULE_REGISTRY[key].category == "playability" and value != HARD:
                raise ValueError(
                    f"rule {key!r} is a playability condition and cannot be "
                    "set below hard; use recovery strategies instead"
                )
            profile.levels[key] = value
        profile.weights.update(overrides.get("weights", {}))
        profile.params.update(overrides.get("params", {}))
    return profile


def list_styles() -> list[dict]:
    return [profile.to_dict() for profile in STYLE_PROFILES.values()]
