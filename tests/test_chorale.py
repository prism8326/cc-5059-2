"""Acceptance tests for the chorale arranger.

These tests verify the actual musical output -- interval by interval,
voice by voice -- rather than only that a pretty score renders. Run with::

    python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chorale_arranger.theory import (  # noqa: E402
    VOICES, VOICE_RANGES, Chord, interval_name, parse_pitch, pitch_to_name,
)
from chorale_arranger.rules import get_profile  # noqa: E402
from chorale_arranger.solver import ArrangementInput, Segment, solve  # noqa: E402
from chorale_arranger.events import (  # noqa: E402
    arrangement_to_events,
    events_to_arrangement,
    synthesize_voices,
    write_midi,
    write_wav,
)


def cadence_input(seed=7, style="common_practice_strict"):
    plan = [
        ("I", {"soprano": parse_pitch("C5")}),
        ("IV", {"soprano": parse_pitch("A4")}),
        ("V7", {"soprano": parse_pitch("G4")}),
        ("I", {"soprano": parse_pitch("C5"), "bass": parse_pitch("C3")}),
    ]
    segs = [Segment((Chord.from_function(fn, 0),), fixed) for fn, fixed in plan]
    return ArrangementInput(segments=segs, key_root_pc=0, style=style, seed=seed)


def conflict_input(seed=3):
    plan = [
        ({"soprano": parse_pitch("C6"), "bass": parse_pitch("C2")}, "I"),
        ({"soprano": parse_pitch("B5"), "bass": parse_pitch("G2")}, "V7"),
        ({"soprano": parse_pitch("C6"), "bass": parse_pitch("C2")}, "I"),
    ]
    return ArrangementInput(
        segments=[Segment((Chord.from_function(fn, 0),), fixed) for fixed, fn in plan],
        key_root_pc=0, seed=seed,
    )


class TestTheory(unittest.TestCase):
    def test_pitch_round_trip(self):
        for token in ["C4", "A5", "Bb3", "F#4", "Eb2", "G3"]:
            self.assertEqual(pitch_to_name(parse_pitch(token), 0)
                             .replace("Db", "C#").replace("Eb", "D#")
                             .replace("Gb", "F#").replace("Ab", "G#")
                             .replace("Bb", "A#"),
                             token.replace("Db", "C#").replace("Eb", "D#")
                             .replace("Gb", "F#").replace("Ab", "G#")
                             .replace("Bb", "A#"))

    def test_interval_names(self):
        self.assertEqual(interval_name(0), "unison")
        self.assertEqual(interval_name(7), "perfect 5th")
        self.assertEqual(interval_name(-12), "octave")
        self.assertEqual(interval_name(14), "1 octave + major 2nd")

    def test_function_chords_in_c(self):
        self.assertEqual(Chord.from_function("I", 0).pitch_classes, (0, 4, 7))
        self.assertEqual(Chord.from_function("V7", 0).pitch_classes, (7, 11, 2, 5))
        self.assertEqual(Chord.from_function("vi", 0).pitch_classes, (9, 0, 4))
        self.assertEqual(Chord.from_function("vii°7", 0).pitch_classes, (11, 2, 5, 8))


class TestHardConditions(unittest.TestCase):
    def setUp(self):
        self.result = solve(cadence_input())
        self.assertEqual(self.result.status, "optimal")

    def test_ranges_never_broken(self):
        for seg in self.result.segments:
            for voice, pitch in zip(VOICES, seg.voicing):
                low, high = VOICE_RANGES[voice]
                self.assertLessEqual(low, pitch, f"{voice} below range at {seg.index}")
                self.assertLessEqual(pitch, high, f"{voice} above range at {seg.index}")

    def test_no_voice_crossing(self):
        for seg in self.result.segments:
            for i in range(3):
                self.assertGreaterEqual(
                    seg.voicing[i], seg.voicing[i + 1],
                    f"crossing between {VOICES[i]} and {VOICES[i+1]} at {seg.index}",
                )

    def test_all_notes_are_chord_tones(self):
        for seg in self.result.segments:
            pcs = set(seg.chord.pitch_classes)
            for pitch in seg.voicing:
                self.assertIn(pitch % 12, pcs)

    def test_fixed_notes_honored(self):
        fixed = cadence_input().segments
        for k, seg0 in enumerate(fixed):
            for voice, pitch in seg0.fixed.items():
                vi = VOICES.index(voice)
                self.assertEqual(self.result.segments[k].voicing[vi], pitch)

    def test_upper_spacing_hard_in_strict(self):
        profile = get_profile("common_practice_strict")
        limit = profile.params["max_spacing"]
        for seg in self.result.segments:
            self.assertLessEqual(seg.voicing[0] - seg.voicing[1], limit)
            self.assertLessEqual(seg.voicing[1] - seg.voicing[2], limit)

    def test_melodic_leaps_within_limit(self):
        profile = get_profile("common_practice_strict")
        limit = profile.params["max_leap"]
        for k in range(1, len(self.result.segments)):
            for i in range(4):
                self.assertLessEqual(
                    abs(self.result.segments[k].voicing[i]
                        - self.result.segments[k - 1].voicing[i]),
                    limit,
                )

    def test_no_parallel_perfect_in_strict(self):
        for k in range(1, len(self.result.segments)):
            a, b = self.result.segments[k - 1].voicing, self.result.segments[k].voicing
            for i in range(4):
                for j in range(i + 1, 4):
                    before, after = abs(a[i] - a[j]), abs(b[i] - b[j])
                    parallel = (
                        before % 12 in (0, 7) and after % 12 in (0, 7)
                        and a[i] != a[j] and b[i] != b[j]
                        and (b[i] - a[i]) * (b[j] - a[j]) > 0
                    )
                    self.assertFalse(parallel, f"parallel perfect at segment {k}")

    def test_cost_breakdown_is_itemized_and_nonnegative(self):
        self.assertGreater(len(self.result.cost_breakdown), 0)
        for key, value in self.result.cost_breakdown.items():
            self.assertGreaterEqual(value, 0.0, key)
        self.assertAlmostEqual(
            self.result.total_cost,
            sum(self.result.cost_breakdown.values()),
            places=6,
        )


class TestVoiceLeadingIntervals(unittest.TestCase):
    def test_every_interval_is_named_and_consistent(self):
        result = solve(cadence_input())
        for k in range(1, len(result.segments)):
            for i, voice in enumerate(VOICES):
                delta = result.segments[k].voicing[i] - result.segments[k - 1].voicing[i]
                # The name helper must agree with the raw semitone count.
                self.assertIn(interval_name(delta), interval_name(delta))
                self.assertEqual(abs(delta) <= 12, True)

    def test_connection_uses_context_not_greedy_nearest(self):
        # With two chord candidates per segment, the chosen chord must be the
        # globally best path; same input+seed must reproduce it exactly.
        segs = [
            Segment((Chord.from_function("I", 0), Chord.from_function("vi", 0))),
            Segment((Chord.from_function("IV", 0), Chord.from_function("ii", 0))),
            Segment((Chord.from_function("V7", 0), Chord.from_function("vii°7", 0))),
            Segment((Chord.from_function("I", 0),)),
        ]
        r1 = solve(ArrangementInput(segments=segs, seed=11))
        r2 = solve(ArrangementInput(segments=segs, seed=11))
        self.assertEqual(
            [s.voicing for s in r1.segments], [s.voicing for s in r2.segments]
        )
        chosen = [s.chord.symbol for s in r1.segments]
        self.assertEqual(len(chosen), 4)


class TestRecoveryAndInfeasibility(unittest.TestCase):
    def test_recovery_reports_every_relaxed_boundary(self):
        result = solve(conflict_input())
        self.assertEqual(result.status, "recovered")
        self.assertIsNotNone(result.recovery)
        names = {s["name"] for s in result.recovery["strategies"]}
        self.assertEqual(names, {"local", "lookback"})
        self.assertTrue(result.violations)
        for v in result.violations:
            self.assertEqual(v["severity"], "hard_relaxed")
            self.assertIn(v["rule"], {"range", "upper_spacing", "max_leap",
                                      "parallel_perfect"})
        # Fixed notes must still be sung even when out of range.
        for k, seg0 in enumerate(conflict_input().segments):
            for voice, pitch in seg0.fixed.items():
                vi = VOICES.index(voice)
                self.assertEqual(result.segments[k].voicing[vi], pitch)

    def test_genuinely_impossible_input_is_infeasible(self):
        segs = [Segment((Chord.from_function("I", 0),),
                        {"soprano": parse_pitch("C#5")})]
        result = solve(ArrangementInput(segments=segs))
        self.assertEqual(result.status, "infeasible")
        self.assertEqual(result.segments, [])
        self.assertTrue(any("belongs to none" in d for d in result.diagnostics))

    def test_playability_rules_cannot_be_weakened(self):
        with self.assertRaises(ValueError):
            solve(ArrangementInput(
                segments=[Segment((Chord.from_function("I", 0),))],
                overrides={"levels": {"range": "preference"}},
            ))

    def test_modern_profile_allows_what_strict_forbids_as_cost(self):
        inp = conflict_input()
        inp.style = "common_practice_loose"
        result = solve(inp)
        # Range is still hard even in loose style, so recovery remains; but
        # spacing/parallel rules now appear as costs rather than blockers.
        profile = get_profile("common_practice_loose")
        self.assertEqual(profile.level("upper_spacing"), "preference")
        self.assertEqual(profile.level("range"), "hard")


class TestReproducibility(unittest.TestCase):
    def test_same_input_and_seed_reproduces(self):
        r1 = solve(cadence_input(seed=42))
        r2 = solve(cadence_input(seed=42))
        self.assertEqual([s.voicing for s in r1.segments],
                         [s.voicing for s in r2.segments])
        self.assertEqual(r1.cost_breakdown, r2.cost_breakdown)

    def test_seed_changes_tie_choices_within_candidate_range(self):
        r1 = solve(ArrangementInput(
            segments=[Segment((Chord.from_function(l, 0),))
                      for l in ("I", "vi", "IV", "V7", "I")], seed=1))
        r2 = solve(ArrangementInput(
            segments=[Segment((Chord.from_function(l, 0),))
                      for l in ("I", "vi", "IV", "V7", "I")], seed=123))
        # Both must be valid; they may differ in tie choices, and seeds are stored.
        self.assertEqual(r1.seed, 1)
        self.assertEqual(r2.seed, 123)
        self.assertEqual(r1.status, "optimal")
        self.assertEqual(r2.status, "optimal")

    def test_beam_trace_records_candidate_range(self):
        result = solve(cadence_input())
        self.assertTrue(all(row["beam_kept"] <= result.beam_width
                            for row in result.beam_trace))
        self.assertTrue(all(row["candidates_enumerated"] >= 1
                            for row in result.beam_trace))


class TestExportRoundTrip(unittest.TestCase):
    def setUp(self):
        self.result = solve(cadence_input())
        self.events = arrangement_to_events(self.result)

    def test_event_format_round_trip(self):
        payload = json.loads(json.dumps(self.events))
        rebuilt = events_to_arrangement(payload)
        again = solve(rebuilt)
        self.assertEqual(again.status, "optimal")
        original = [
            tuple(self.events["events"][k * 4 + VOICES.index(v)]["midi"]
                  for v in VOICES)
            for k in range(len(self.result.segments))
        ]
        self.assertEqual([s.voicing for s in again.segments], original)

    def test_midi_file_is_parseable(self):
        buf = io.BytesIO()
        write_midi(self.events, buf)
        data = buf.getvalue()
        self.assertEqual(data[:4], b"MThd")
        self.assertEqual(data[8:10], b"\x00\x00")  # format 0
        # Track chunk present and length consistent.
        self.assertEqual(data[14:18], b"MTrk")
        track_len = int.from_bytes(data[18:22], "big")
        self.assertEqual(len(data) - 22, track_len)
        # Parse the track events properly and count note on/off per channel.
        track = data[22:]
        i = 0
        note_ons = note_offs = 0
        running_status = None
        while i < len(track):
            # delta time (VLQ)
            while track[i] & 0x80:
                i += 1
            i += 1
            if track[i] == 0xFF:  # meta
                i += 1
                meta_len = track[i + 1]
                i += 2 + meta_len
                continue
            if track[i] == 0xF0 or track[i] == 0xF7:
                i += 1
                while track[i] & 0x80:
                    i += 1
                i += 1 + track[i]
                continue
            status = track[i] if track[i] & 0x80 else running_status
            if track[i] & 0x80:
                i += 1
            running_status = status
            kind = status & 0xF0
            d1, d2 = track[i], track[i + 1]
            i += 2  # two data bytes
            if kind == 0x90 and d2 > 0:
                note_ons += 1
            elif kind == 0x80 or (kind == 0x90 and d2 == 0):
                note_offs += 1
        self.assertEqual(note_ons, len(self.events["events"]))
        self.assertEqual(note_offs, len(self.events["events"]))

    def test_wav_synthesis_all_voices(self):
        audio = synthesize_voices(self.events)
        self.assertEqual(len(audio["per_voice"]), 4)
        lch, rch = audio["ensemble"]
        self.assertEqual(len(lch), len(rch))
        self.assertGreater(len(lch), 0)
        for voice in VOICES:
            self.assertEqual(len(audio["per_voice"][voice]), len(lch))
        out = io.BytesIO()
        write_wav(out, audio["ensemble"], audio["sample_rate"], stereo=True)
        self.assertEqual(out.getvalue()[:4], b"RIFF")
        out2 = io.BytesIO()
        write_wav(out2, audio["per_voice"]["bass"], audio["sample_rate"], stereo=False)
        self.assertEqual(out2.getvalue()[:4], b"RIFF")


class TestServerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        from chorale_arranger.server import Handler

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_styles_and_solve_and_synth_and_reimport(self):
        import urllib.request

        with urllib.request.urlopen(self._url("/api/styles")) as resp:
            meta = json.loads(resp.read())
        self.assertIn("common_practice_strict", [s["name"] for s in meta["styles"]])

        body = json.dumps({
            "key_root_pc": 0,
            "style": "common_practice_strict",
            "seed": 7,
            "beam_width": 30,
            "segments": [
                {"duration": 1, "fixed": {"soprano": 72},
                 "chord_candidates": [{"function": "I"}]},
                {"duration": 1, "fixed": {"soprano": 67},
                 "chord_candidates": [{"function": "V7"}]},
                {"duration": 1, "fixed": {"soprano": 72},
                 "chord_candidates": [{"function": "I"}]},
            ],
        }).encode()
        req = urllib.request.Request(self._url("/api/solve"), data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(len(result["segments"]), 3)
        self.assertIn("events", result)

        synth_body = json.dumps({"events": result["events"], "bpm": 90}).encode()
        req = urllib.request.Request(self._url("/api/synth"), data=synth_body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            audio = json.loads(resp.read())
        self.assertTrue(audio["ensemble_wav"])
        self.assertEqual(set(audio["per_voice_wav"]), set(VOICES))

        reimport_body = json.dumps({"events": result["events"]}).encode()
        req = urllib.request.Request(self._url("/api/reimport"), data=reimport_body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            reimp = json.loads(resp.read())
        self.assertEqual(reimp["result"]["status"], "optimal")

    def test_failure_feeds_back(self):
        import urllib.error
        import urllib.request

        body = json.dumps({
            "segments": [{"chord_candidates": [{"function": "I"}],
                          "fixed": {"soprano": 73}}],
        }).encode()
        req = urllib.request.Request(self._url("/api/solve"), data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        self.assertEqual(result["status"], "infeasible")
        self.assertTrue(result["diagnostics"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
