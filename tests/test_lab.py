import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chorale_arranger import lab
from chorale_arranger.events import arrangement_to_events, events_to_arrangement
from chorale_arranger.lab import (
    compare_experiments,
    input_from_document,
    save_policy,
    get_policy,
    score_document,
    solve_lookahead,
    unlock_trace,
)
from chorale_arranger.solver import solve
from chorale_arranger.theory import VOICES


class TestLabScoresAndSolvers(unittest.TestCase):
    def test_breath_score_preserves_melody_and_audible_breath(self):
        doc = score_document("breath_phrase")
        inp = input_from_document(doc)
        phrase = solve(inp)
        look = solve_lookahead(inp)
        self.assertEqual(phrase.status, "optimal")
        self.assertEqual(look.status, "optimal")

        for result in (phrase, look):
            for k, seg in enumerate(result.segments):
                fixed_s = doc["segments"][k]["fixed"]["soprano"]
                self.assertEqual(seg.voicing[0], fixed_s)
            events = arrangement_to_events(result)["events"]
            breath_events = [e for e in events if e.get("breath_after_beats")]
            self.assertTrue(breath_events)
            for e in breath_events:
                self.assertLess(e["sounding_beats"], e["duration_beats"])

    def test_breath_survives_event_reimport(self):
        doc = score_document("breath_phrase")
        result = solve(input_from_document(doc))
        rebuilt = events_to_arrangement(arrangement_to_events(result))
        self.assertTrue(any(seg.breath_after > 0 for seg in rebuilt.segments))

    def test_compare_connects_two_distinct_audible_arrangements(self):
        payload = compare_experiments({
            "score_id": "breath_phrase",
            "include_trace": False,
            "benchmark": False,
        })
        self.assertEqual(set(payload["variants"]), {"phrase", "lookahead"})
        for variant in payload["variants"].values():
            self.assertEqual(variant["result"]["status"], "optimal")
            self.assertTrue(variant["events"]["events"])
            self.assertIn("actual_ranges", variant["metrics"])
            self.assertIn("first_parallel", variant["metrics"])

        self.assertEqual(
            set(payload["inspector"]["actual_soundings"]),
            {"phrase", "lookahead"},
        )
        diff = payload["variants"]["phrase"]["metrics"]["pairwise_difference"]
        self.assertGreater(diff["changed_notes"], 0)
        self.assertGreater(len(payload["path_lab"]["paths"]), 1)

    def test_lookahead_really_has_one_upcoming_chord_trace(self):
        doc = score_document("breath_phrase")
        result = solve_lookahead(input_from_document(doc))
        self.assertTrue(result.beam_trace)
        self.assertIn("lookahead_candidates", result.beam_trace[0])
        self.assertTrue(any(row["lookahead_candidates"] > 0 for row in result.beam_trace[:-1]))


class TestLockMetricsAndEvidence(unittest.TestCase):
    def test_inner_lock_counts_far_voice_rewrites(self):
        payload = compare_experiments({
            "score_id": "breath_phrase",
            "lock": {"segment": 0, "voice": "alto", "midi": 64},
            "include_trace": False,
            "benchmark": False,
        })
        impact = payload["variants"]["phrase"]["metrics"]["lock_impact"]
        self.assertEqual(impact["locked_voice"], "alto")
        self.assertGreaterEqual(impact["far_voice_rewrites"], 1)
        self.assertIn("rewrites_by_voice", impact)

    def test_strict_path_dead_end_relaxes_path_lab(self):
        payload = compare_experiments({
            "score_id": "breath_phrase",
            "lock": {"segment": 0, "voice": "alto", "midi": 55},
            "include_trace": False,
            "benchmark": False,
        })
        paths = payload["path_lab"]["paths"]
        self.assertTrue(paths)
        self.assertTrue(payload["path_lab"].get("strict_paths_empty"))
        self.assertGreaterEqual(paths[0]["metrics"]["spacing_violation_count"], 1)
        self.assertIn("lock_impact", paths[0]["metrics"])

    def test_rejected_paths_include_crossing_and_parallel_evidence(self):
        payload = compare_experiments({
            "score_id": "inner_crossing",
            "include_trace": False,
            "benchmark": False,
        })
        rejected = payload["path_lab"]["rejected_paths"]
        categories = {p.get("evidence_category") for p in rejected}
        self.assertIn("crossing", categories)
        self.assertIn("parallel", categories)

        crossing = next(p for p in rejected if p["evidence_category"] == "crossing")
        self.assertGreater(crossing["metrics"]["crossing_count"], 0)
        self.assertTrue(crossing["metrics"]["crossing_events"])
        self.assertTrue(all(
            voice in crossing["rows"][0]["voicing"] for voice in VOICES
        ))

        parallel = next(p for p in rejected if p["evidence_category"] == "parallel")
        self.assertGreater(parallel["metrics"]["parallel_perfect_count"], 0)
        self.assertIsNotNone(parallel["metrics"]["first_parallel"])

    def test_unlock_trace_returns_pre_violation_inspector(self):
        doc = score_document("inner_crossing")
        trace = unlock_trace(doc, "phrase")
        self.assertGreaterEqual(len(trace["steps"]), 2)
        self.assertIsNotNone(trace["return_to_step"])
        clean_step = next(s for s in trace["steps"]
                          if s["step"] == trace["return_to_step"])
        self.assertTrue(clean_step["clean"])
        inspector = clean_step["inspector"]
        self.assertGreater(inspector["candidate_count"], 0)
        self.assertIn("soprano", inspector["actual_sounding"]["pitches"])
        self.assertEqual(set(inspector["actual_sounding"]["ownership"]), set(VOICES))
        self.assertTrue(inspector["crossing_candidates"])
        crossing_pairs = {
            tuple(pair)
            for candidate in inspector["crossing_candidates"]
            for pair in candidate["crossing_pairs"]
        }
        self.assertIn(("alto", "tenor"), crossing_pairs)


class TestPolicyPersistence(unittest.TestCase):
    def test_save_and_reopen_policy_for_same_score(self):
        with tempfile.TemporaryDirectory() as td:
            old_path = lab.POLICY_PATH
            lab.POLICY_PATH = Path(td) / "policy.json"
            try:
                doc = score_document("breath_phrase")
                saved = save_policy({
                    "score_id": "breath_phrase",
                    "fingerprint": lab.score_fingerprint(doc),
                    "planner": "lookahead",
                    "priority": "local",
                    "lock": {"segment": 0, "voice": "alto", "midi": 64},
                })
                reopened = get_policy("breath_phrase", saved["fingerprint"])
                self.assertEqual(reopened["planner"], "lookahead")
                self.assertEqual(reopened["priority"], "local")
                self.assertEqual(reopened["lock"]["midi"], 64)
                self.assertTrue(json.loads(lab.POLICY_PATH.read_text()))
            finally:
                lab.POLICY_PATH = old_path


if __name__ == "__main__":
    unittest.main(verbosity=2)
