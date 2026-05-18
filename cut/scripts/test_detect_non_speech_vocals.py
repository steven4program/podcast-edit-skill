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


from detect_non_speech_vocals_gemini import (
    load_word_spans, classify_zone, annotate_events_with_word_context,
)


class TestZoneClassification(unittest.TestCase):
    def setUp(self):
        # 3 words: [0.0-1.0] "Hello", [2.0-3.0] "World", [5.0-6.0] "End"
        self.spans = [(0.0, 1.0), (2.0, 3.0), (5.0, 6.0)]

    def test_pure_gap(self):
        # Event 1.2-1.5 is between word1.end=1.0 and word2.start=2.0
        zone, ratio, prev_end, next_start = classify_zone(1.2, 1.5, self.spans)
        self.assertEqual(zone, "pure_gap")
        self.assertAlmostEqual(ratio, 0.0)
        self.assertEqual(prev_end, 1.0)
        self.assertEqual(next_start, 2.0)

    def test_inside_word(self):
        # Event 0.3-0.7 fully inside word1 [0.0-1.0]
        zone, ratio, _, _ = classify_zone(0.3, 0.7, self.spans)
        self.assertEqual(zone, "inside_word")
        self.assertAlmostEqual(ratio, 1.0)

    def test_partial_overlap(self):
        # Event 0.8-1.4 covers tail of word1 (0.8-1.0) plus gap (1.0-1.4)
        # Overlap = 0.2 / total 0.6 = 0.33 → partial_overlap
        zone, ratio, _, _ = classify_zone(0.8, 1.4, self.spans)
        self.assertEqual(zone, "partial_overlap")
        self.assertAlmostEqual(ratio, 0.2 / 0.6, places=3)

    def test_before_first_word(self):
        zone, ratio, prev_end, next_start = classify_zone(0.0, 0.0, self.spans[1:])
        # No prev_end available
        self.assertIsNone(prev_end)

    def test_after_last_word(self):
        zone, ratio, prev_end, next_start = classify_zone(7.0, 7.5, self.spans)
        self.assertEqual(zone, "pure_gap")
        self.assertIsNone(next_start)


class TestAnnotate(unittest.TestCase):
    def test_annotates_events(self):
        spans = [(0.0, 1.0), (2.0, 3.0)]
        events = [{"start": 1.2, "end": 1.5, "type": "throat_clear", "confidence": 0.8}]
        annotated = annotate_events_with_word_context(events, spans)
        self.assertEqual(annotated[0]["zone"], "pure_gap")
        self.assertIn("overlap_ratio", annotated[0])
        self.assertIn("prev_word_end", annotated[0])
        self.assertIn("next_word_start", annotated[0])


class TestLoadWordSpans(unittest.TestCase):
    def test_loads_speech_words_only(self):
        import tempfile, json as _json
        data = {"words": [
            {"start": 0.0, "end": 1.0, "text": "嗨", "isGap": False},
            {"start": 1.0, "end": 1.5, "text": "", "isGap": True},
            {"start": 1.5, "end": 2.5, "text": "你好", "isGap": False},
        ]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            _json.dump(data, f)
            path = Path(f.name)
        try:
            spans = load_word_spans(path)
            self.assertEqual(spans, [(0.0, 1.0), (1.5, 2.5)])
        finally:
            path.unlink()


from detect_non_speech_vocals_gemini import refine_event_boundary


class TestRefineEventBoundary(unittest.TestCase):
    def setUp(self):
        import numpy as np
        # Construct a synthetic signal: 1s silence, 0.3s burst, 1s silence
        self.sr = 16000
        self.audio = np.concatenate([
            np.zeros(int(1.0 * self.sr)),
            np.random.normal(0, 0.3, int(0.3 * self.sr)),
            np.zeros(int(1.0 * self.sr)),
        ]).astype("float32")
        # The burst is at 1.0-1.3 in the audio. We give Gemini a sloppy estimate 0.85-1.45.
        self.gem_start = 0.85
        self.gem_end = 1.45

    def test_refines_to_within_50ms_of_truth(self):
        refined_start, refined_end = refine_event_boundary(
            self.audio, self.sr, self.gem_start, self.gem_end,
        )
        # Truth: 1.0-1.3; allow ±0.05s
        self.assertAlmostEqual(refined_start, 1.0, delta=0.05)
        self.assertAlmostEqual(refined_end, 1.3, delta=0.05)

    def test_handles_event_near_boundary(self):
        # Event at start of file (gem_start would be negative without padding)
        refined_start, refined_end = refine_event_boundary(
            self.audio, self.sr, gem_start=0.0, gem_end=0.2,
        )
        # Should not crash, return values in [0, duration]
        self.assertGreaterEqual(refined_start, 0)
        self.assertLessEqual(refined_end, len(self.audio) / self.sr)


if __name__ == "__main__":
    unittest.main()
