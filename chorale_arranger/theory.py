"""Music-theory primitives: pitches (MIDI numbers), chords, voice ranges, intervals.

Nothing in this module encodes a *style rule*. It only provides data and
mechanical helpers. Style rules live in :mod:`chorale_arranger.rules` so that
a historical convention can never be presented as universally mandatory.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Pitch spelling
# ---------------------------------------------------------------------------

_SHARP_SPELL = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT_SPELL = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
_NATURAL_PC = {0: "C", 2: "D", 4: "E", 5: "F", 7: "G", 9: "A", 11: "B"}

# Position on the circle of fifths for each major key root (C=0, G=1, F=-1 ...).
_KEY_FIFTHS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6,
               5: -1, 10: -2, 3: -3, 8: -4, 1: -5}


def _spelling_table(key_fifths: int) -> list[str]:
    if key_fifths < 0:
        return list(_FLAT_SPELL)
    if key_fifths > 0:
        return list(_SHARP_SPELL)
    # C major / A minor: spell chromatic notes as sharps, diatonic notes natural.
    table = []
    for pc in range(12):
        table.append(_NATURAL_PC.get(pc, _SHARP_SPELL[pc]))
    return table


def pitch_to_name(midi: int, key_root_pc: int = 0) -> str:
    """Return a spelled name such as ``C4`` / ``Bb3`` consistent with the key."""
    key_fifths = _KEY_FIFTHS.get(key_root_pc % 12, 0)
    table = _spelling_table(key_fifths)
    pc = midi % 12
    octave = midi // 12 - 1
    return f"{table[pc]}{octave}"


def parse_pitch(token: str) -> int:
    """Parse names like ``C4``, ``Bb3``, ``f#5`` (case-insensitive) to MIDI."""
    token = token.strip()
    if not token:
        raise ValueError("empty pitch token")
    letter = token[0].upper()
    idx = 1
    accidental = 0
    while idx < len(token) and token[idx] in "#b":
        accidental += 1 if token[idx] == "#" else -1
        idx += 1
    octave = int(token[idx:])
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter]
    return (octave + 1) * 12 + base + accidental


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------

# Semitone count -> interval name (within one octave). Compound intervals are
# derived mechanically by adding the octave count.
_INTERVAL_NAMES = {
    0: "unison", 1: "minor 2nd", 2: "major 2nd", 3: "minor 3rd",
    4: "major 3rd", 5: "perfect 4th", 6: "tritone", 7: "perfect 5th",
    8: "minor 6th", 9: "major 6th", 10: "minor 7th", 11: "major 7th",
    12: "octave",
}


def interval_name(semitones: int) -> str:
    """Human-readable interval name for an absolute semitone distance."""
    n = abs(int(semitones))
    octaves, rem = divmod(n, 12)
    if octaves == 0:
        return _INTERVAL_NAMES[rem]
    if rem == 0:
        return f"{octaves + 1} octaves" if octaves > 1 else "octave"
    return f"{octaves} octave + {_INTERVAL_NAMES[rem]}"


# ---------------------------------------------------------------------------
# Voices and ranges (inclusive MIDI bounds; standard SATB concert-pitch ranges)
# ---------------------------------------------------------------------------

VOICES = ("soprano", "alto", "tenor", "bass")

# (low, high) inclusive. These are playability ranges for the default
# four-voice chorus, not style rules -- callers may widen them.
VOICE_RANGES = {
    "soprano": (parse_pitch("C4"), parse_pitch("A5")),
    "alto": (parse_pitch("G3"), parse_pitch("D5")),
    "tenor": (parse_pitch("C3"), parse_pitch("A4")),
    "bass": (parse_pitch("E2"), parse_pitch("E4")),
}


# ---------------------------------------------------------------------------
# Chords
# ---------------------------------------------------------------------------

# Chord quality -> pitch-class offsets from the root.
QUALITY_OFFSETS = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "maj7": (0, 4, 7, 11),
    "min7": (0, 3, 7, 10),
    "7": (0, 4, 7, 10),
    "dim7": (0, 3, 6, 9),
    "m7b5": (0, 3, 6, 10),
    "maj9": (0, 4, 7, 11, 14),
    "min9": (0, 3, 7, 10, 14),
    "9": (0, 4, 7, 10, 14),
    "6": (0, 4, 7, 9),
    "min6": (0, 3, 7, 9),
    "add9": (0, 4, 7, 14),
    "power": (0, 7),
}

# Functional label -> relative scale degree of the chord root in major key.
_SCALE_DEGREE = {"I": 0, "II": 2, "III": 4, "IV": 5, "V": 7, "VI": 9, "VII": 11}


@dataclass(frozen=True)
class Chord:
    """A chord candidate for one segment.

    ``label`` is the functional/symbol label (e.g. ``I``, ``V7``, ``ii``).
    The actual sounding pitches are fully determined by ``root_pc`` + ``quality``,
    so labels never silently add pitch classes.
    """

    root_pc: int
    quality: str
    label: str = ""

    def __post_init__(self) -> None:
        if self.quality not in QUALITY_OFFSETS:
            raise ValueError(f"unknown chord quality: {self.quality!r}")

    @property
    def pitch_classes(self) -> tuple[int, ...]:
        return tuple((self.root_pc + off) % 12 for off in QUALITY_OFFSETS[self.quality])

    @property
    def symbol(self) -> str:
        if self.label:
            return self.label
        return f"{_SHARP_SPELL[self.root_pc % 12]}{self.quality}"

    @staticmethod
    def from_function(label: str, key_root_pc: int) -> "Chord":
        """Build a chord from a Roman-style functional label in a major key.

        Upper case = major, lower case = minor, ``°``/``dim`` = diminished,
        a trailing ``7`` selects the corresponding seventh quality.
        Examples in C major: ``I`` -> C maj, ``V7`` -> G7, ``vi`` -> A min,
        ``vii°7`` -> B dim7.
        """
        text = label.strip()
        seventh = text.endswith("7")
        if seventh:
            text = text[:-1]
        diminished = text.endswith(("°", "o", "dim"))
        if diminished:
            text = text[:-1] if text[-1] in "°o" else text[:-3]
        degree = text.upper()
        if degree not in _SCALE_DEGREE:
            raise ValueError(f"unknown function label: {label!r}")
        root_pc = (key_root_pc + _SCALE_DEGREE[degree]) % 12
        is_minor = text[:1].islower() and not diminished
        if seventh:
            quality = "dim7" if diminished else ("min7" if is_minor else "7")
        else:
            quality = "dim" if diminished else ("min" if is_minor else "maj")
        return Chord(root_pc, quality, label)


def chord_pcs(chord: Chord) -> tuple[int, ...]:
    return chord.pitch_classes


@dataclass(frozen=True)
class Note:
    """A fixed note: voice name + MIDI pitch (or ``None`` to leave free)."""

    voice: str
    midi: int | None
