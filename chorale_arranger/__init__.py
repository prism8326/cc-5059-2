"""Short chorale harmony arranger core package.

Modules:
  theory   -- pitch/chord primitives, voice ranges, interval names
  rules    -- style profiles: hard conditions vs. preference costs
  solver   -- beam-search SATB voicing solver with recovery strategies
  events   -- note-event export/import, MIDI writer, WAV synthesis
"""

from .theory import (
    Note,
    Chord,
    VOICES,
    VOICE_RANGES,
    interval_name,
    parse_pitch,
    pitch_to_name,
    chord_pcs,
)
from .rules import STYLE_PROFILES, get_profile, list_styles, RuleSpec
from .solver import ArrangementInput, Segment, solve, SolveResult
from .events import arrangement_to_events, events_to_arrangement, write_midi

__all__ = [
    "Note",
    "Chord",
    "VOICES",
    "VOICE_RANGES",
    "interval_name",
    "parse_pitch",
    "pitch_to_name",
    "chord_pcs",
    "STYLE_PROFILES",
    "get_profile",
    "list_styles",
    "RuleSpec",
    "ArrangementInput",
    "Segment",
    "solve",
    "SolveResult",
    "arrangement_to_events",
    "events_to_arrangement",
    "write_midi",
]
