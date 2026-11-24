"""Dependency-free HTTP server driving the chorale arranger UI.

Run with::

    python3 -m chorale_arranger.server [--port 8000]

The browser UI is a set of static files; all computation happens in this
process through the kernel package, so the controls are driven by the same
solver the CLI uses and any failure is reported straight back to the UI.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .events import (
    SynthConfig,
    arrangement_to_events,
    events_to_arrangement,
    synthesize_voices,
    write_midi,
    write_wav,
)
from .rules import list_styles
from .solver import ArrangementInput, Segment, solve
from . import lab
from .theory import Chord, VOICES, VOICE_RANGES, pitch_to_name

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------

def _build_chord(spec, key_root_pc):
    if "function" in spec and spec["function"]:
        return Chord.from_function(spec["function"], key_root_pc)
    return Chord(
        int(spec["root_pc"]) % 12,
        str(spec["quality"]),
        str(spec.get("label", "")),
    )


def _parse_input(body) -> ArrangementInput:
    key_root_pc = int(body.get("key_root_pc", 0))
    segments = []
    for raw in body["segments"]:
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
    if body.get("ranges"):
        ranges = {}
        for voice, bounds in body["ranges"].items():
            ranges[voice] = (int(bounds[0]), int(bounds[1]))
    return ArrangementInput(
        segments=segments,
        key_root_pc=key_root_pc,
        style=str(body.get("style", "common_practice_strict")),
        overrides=body.get("overrides") or None,
        ranges=ranges,
        beam_width=int(body.get("beam_width", 40)),
        seed=int(body.get("seed", 0)),
        candidate_cap=int(body.get("candidate_cap", 0) or 0),
    )


def _annotate(result):
    payload = result.to_dict()
    for seg in payload["segments"]:
        seg["note_names"] = {
            voice: pitch_to_name(midi, payload["key_root_pc"])
            for voice, midi in seg["voicing"].items()
        }
    return payload


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _wav_base64(pcm, sr, stereo=True):
    buf = io.BytesIO()
    write_wav(buf, pcm, sr, stereo=stereo)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _midi_base64(events, bpm):
    buf = io.BytesIO()
    write_midi(events, buf, bpm=bpm)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ChoraleArranger/1.0"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        from urllib.parse import parse_qs
        query = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if path == "/api/styles":
            return self._send_json({"styles": list_styles(),
                                    "voices": list(VOICES),
                                    "voice_ranges": {v: list(b) for v, b in VOICE_RANGES.items()}})
        if path == "/api/experiment/scores":
            scores = []
            for score_id in lab.LAB_SCORES:
                doc = lab.score_document(score_id)
                scores.append({
                    "score_id": score_id,
                    "title": doc.get("title", score_id),
                    "fingerprint": lab.score_fingerprint(doc),
                    "priority": doc.get("priority", "phrase"),
                    "planner": doc.get("planner", "phrase"),
                    "breath_marks": doc.get("breath_marks", []),
                    "lock_sequence": doc.get("lock_sequence", []),
                    "lock_options": lab.lock_options_for_document(doc),
                    "document": doc,
                })
            return self._send_json({"scores": scores})
        if path == "/api/experiment/policy":
            score_id = (query.get("score_id") or [None])[0]
            fingerprint = (query.get("fingerprint") or [None])[0]
            return self._send_json({"policy": lab.get_policy(score_id, fingerprint)})
        if path.startswith("/web/"):
            return self._serve_static(path[len("/web/"):])
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/solve":
                return self._handle_solve(body)
            if path in ("/api/experiment/compare", "/api/blind-compare"):
                return self._handle_experiment_compare(body)
            if path in ("/api/experiment/benchmark", "/api/blind-benchmark"):
                return self._send_json(lab.benchmark_policy(
                    lab._resolve_doc(body) if body.get("score_id") or body.get("document") else None,
                    repeats=int(body.get("repeats", 3)),
                ))
            if path == "/api/experiment/policy":
                return self._send_json({"policy": lab.save_policy(body.get("policy") or body)})
            if path == "/api/synth":
                return self._handle_synth(body)
            if path == "/api/export":
                return self._handle_export(body)
            if path == "/api/reimport":
                return self._handle_reimport(body)
            return self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # kernel failures feed back into the UI
            tb = traceback.format_exc(limit=4)
            return self._send_json({"error": str(exc), "traceback": tb}, 400)

    # -- routes -------------------------------------------------------------

    def _handle_solve(self, body):
        inp = _parse_input(body)
        if body.get("priority"):
            inp = lab.apply_priority(inp, str(body["priority"]))
        planner = body.get("planner", "phrase")
        if planner == "lookahead":
            from .lab import solve_lookahead
            result = solve_lookahead(inp, int(body.get("lookahead", 1)))
        else:
            result = solve(inp)
        payload = _annotate(result)
        payload["events"] = arrangement_to_events(result)
        return self._send_json(payload)

    def _handle_experiment_compare(self, body):
        payload = lab.compare_experiments(body)
        key_root_pc = 0
        for variant in payload.get("variants", {}).values():
            result = variant.get("result") or {}
            key_root_pc = result.get("key_root_pc", 0)
            for seg in result.get("segments", []):
                seg["note_names"] = {
                    voice: pitch_to_name(midi, key_root_pc)
                    for voice, midi in seg.get("voicing", {}).items()
                }
        return self._send_json(payload)

    def _handle_synth(self, body):
        events = body["events"]
        cfg = SynthConfig(
            bpm=float(body.get("bpm", 90)),
            sample_rate=int(body.get("sample_rate", 22050)),
        )
        audio = synthesize_voices(events, cfg)
        sr = audio["sample_rate"]
        out = {
            "sample_rate": sr,
            "engine": audio["engine"],
            "ensemble_wav": _wav_base64(audio["ensemble"], sr, stereo=True),
            "per_voice_wav": {
                voice: _wav_base64(audio["per_voice"][voice], sr, stereo=False)
                for voice in VOICES
            },
        }
        return self._send_json(out)

    def _handle_export(self, body):
        kind = body.get("kind", "events")
        events = body["events"]
        bpm = float(body.get("bpm", 90))
        if kind == "events":
            data = json.dumps(events, ensure_ascii=False, indent=2).encode("utf-8")
            mime = "application/json"
            filename = "chorale_events.json"
        elif kind == "midi":
            buf = io.BytesIO()
            write_midi(events, buf, bpm=bpm)
            data = buf.getvalue()
            mime = "audio/midi"
            filename = "chorale.mid"
        else:
            return self._send_json({"error": f"unknown export kind {kind!r}"}, 400)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_reimport(self, body):
        events = body.get("events") or body
        rebuilt = events_to_arrangement(events)
        result = solve(rebuilt)
        annotated = _annotate(result)
        annotated["events"] = arrangement_to_events(result)
        return self._send_json({
            "rebuilt_input": {
                "key_root_pc": rebuilt.key_root_pc,
                "style": rebuilt.style,
                "seed": rebuilt.seed,
                "segments": [
                    {
                        "duration": seg.duration,
                        "breath_after": seg.breath_after,
                        "fixed": seg.fixed,
                        "chord_candidates": [
                            {"root_pc": c.root_pc, "quality": c.quality, "label": c.label}
                            for c in seg.chord_candidates
                        ],
                    }
                    for seg in rebuilt.segments
                ],
            },
            "result": annotated,
        })

    def _serve_static(self, name, content_type=None):
        safe = os.path.normpath(os.path.join(WEB_DIR, name))
        if not safe.startswith(WEB_DIR):
            return self._send_json({"error": "forbidden"}, 403)
        if not os.path.isfile(safe):
            return self._send_json({"error": "not found"}, 404)
        ctype = content_type or _guess_type(safe)
        with open(safe, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _guess_type(path):
    if path.endswith(".html"):
        return "text/html; charset=utf-8"
    if path.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chorale arranger web UI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Chorale arranger UI: http://{args.host}:{args.port}/  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
