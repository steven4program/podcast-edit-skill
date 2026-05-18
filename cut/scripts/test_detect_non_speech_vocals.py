"""Unit tests for detect_non_speech_vocals_gemini helpers.

Run: python3 -m unittest cut/scripts/test_detect_non_speech_vocals.py
"""
import sys
import unittest
from pathlib import Path

# Make sibling script importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect_non_speech_vocals_gemini import (
    parse_json_response, normalize_confidence, build_chunks,
    dedupe_events,  # added in Task 4
)


class TestParseJson(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(
            parse_json_response('{"events": []}'),
            {"events": []},
        )

    def test_markdown_fenced(self):
        self.assertEqual(
            parse_json_response('```json\n{"events": [{"offset_s": 1.0}]}\n```'),
            {"events": [{"offset_s": 1.0}]},
        )

    def test_bare_object_in_text(self):
        text = 'Here is the result: {"events": [{"type": "throat_clear"}]}\nthanks!'
        self.assertEqual(
            parse_json_response(text),
            {"events": [{"type": "throat_clear"}]},
        )

    def test_empty_returns_none(self):
        self.assertIsNone(parse_json_response(""))
        self.assertIsNone(parse_json_response(None))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_json_response("this is not json at all"))


class TestNormalizeConfidence(unittest.TestCase):
    def test_numeric_passthrough(self):
        self.assertEqual(normalize_confidence(0.85), 0.85)
        self.assertEqual(normalize_confidence(1.0), 1.0)
        self.assertEqual(normalize_confidence(0), 0.0)

    def test_numeric_clamped(self):
        self.assertEqual(normalize_confidence(1.5), 1.0)
        self.assertEqual(normalize_confidence(-0.2), 0.0)

    def test_string_mapping(self):
        self.assertEqual(normalize_confidence("high"), 0.85)
        self.assertEqual(normalize_confidence("HIGH"), 0.85)
        self.assertEqual(normalize_confidence("medium"), 0.6)
        self.assertEqual(normalize_confidence("very high"), 0.95)

    def test_unknown_string_defaults_05(self):
        self.assertEqual(normalize_confidence("bizarre"), 0.5)

    def test_non_value_defaults_05(self):
        self.assertEqual(normalize_confidence(None), 0.5)
        self.assertEqual(normalize_confidence([]), 0.5)


class TestBuildChunks(unittest.TestCase):
    def test_short_audio(self):
        chunks = build_chunks(duration=20, chunk_sec=30, hop_sec=25)
        self.assertEqual(chunks, [(0.0, 20)])

    def test_exact_chunk_size(self):
        chunks = build_chunks(duration=30, chunk_sec=30, hop_sec=25)
        self.assertEqual(chunks, [(0.0, 30)])

    def test_5min_audio(self):
        chunks = build_chunks(duration=300, chunk_sec=30, hop_sec=25)
        # First chunk: 0-30, hop 25 → 25-55, 50-80, ... last starts at 275
        self.assertEqual(chunks[0], (0.0, 30))
        self.assertEqual(chunks[1], (25.0, 55))
        # Last chunk ends at duration
        self.assertEqual(chunks[-1][1], 300)

    def test_overlap_is_5_sec(self):
        chunks = build_chunks(duration=300)
        for a, b in zip(chunks, chunks[1:]):
            # Overlap = previous.end - next.start = 30 - 25 = 5
            overlap = a[1] - b[0]
            self.assertAlmostEqual(overlap, 5, places=1)


class TestDedupe(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(dedupe_events([]), [])

    def test_no_overlap_preserved(self):
        events = [
            {"start": 1.0, "end": 1.3, "type": "throat_clear", "confidence": 0.8},
            {"start": 10.0, "end": 10.3, "type": "throat_clear", "confidence": 0.7},
        ]
        self.assertEqual(len(dedupe_events(events)), 2)

    def test_overlap_within_threshold_keeps_higher_conf(self):
        events = [
            {"start": 5.0, "end": 5.3, "type": "throat_clear", "confidence": 0.6},
            {"start": 5.2, "end": 5.5, "type": "throat_clear", "confidence": 0.9},
        ]
        result = dedupe_events(events)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["confidence"], 0.9)

    def test_different_types_not_merged(self):
        events = [
            {"start": 5.0, "end": 5.3, "type": "throat_clear", "confidence": 0.8},
            {"start": 5.2, "end": 5.5, "type": "nose_clear", "confidence": 0.8},
        ]
        self.assertEqual(len(dedupe_events(events)), 2)

    def test_unsorted_input_sorted(self):
        events = [
            {"start": 10.0, "end": 10.3, "type": "throat_clear", "confidence": 0.8},
            {"start": 1.0, "end": 1.3, "type": "throat_clear", "confidence": 0.8},
        ]
        result = dedupe_events(events)
        self.assertEqual([e["start"] for e in result], [1.0, 10.0])


if __name__ == "__main__":
    unittest.main()
