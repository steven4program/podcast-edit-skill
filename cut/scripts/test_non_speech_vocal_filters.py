"""Unit tests for non_speech_vocal_filters."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from non_speech_vocal_filters import (
    filter_host_describing_sound, filter_long_gap_subsume,
)


def make_event(start, end, etype="throat_clear", conf=0.9):
    return {
        "start": start, "end": end, "type": etype,
        "confidence": conf,
        "zone": "pure_gap", "overlap_ratio": 0.0,
        "prev_word_end": max(0, start - 0.5),
        "next_word_start": end + 0.5,
        "filter_decision": "ok",
    }


def make_word(start, end, text):
    return {"start": start, "end": end, "text": text, "isGap": False}


class TestFilterHostDescribing(unittest.TestCase):
    def test_keeps_event_when_context_is_normal(self):
        events = [make_event(10.0, 10.3)]
        words = [make_word(9.0, 9.5, "今天"), make_word(11.0, 11.5, "好的")]
        out = filter_host_describing_sound(events, words)
        self.assertEqual(out[0]["filter_decision"], "ok")
        self.assertEqual(len([e for e in out if e["filter_decision"] == "ok"]), 1)

    def test_drops_when_context_has_kou_repetition(self):
        # Speaker says 口口口 around the event — they're mimicking
        events = [make_event(220.0, 220.5)]
        words = [
            make_word(219.0, 219.3, "口"),
            make_word(219.3, 219.5, "口"),
            make_word(219.6, 219.8, "口"),
            make_word(220.7, 220.9, "的"),
        ]
        out = filter_host_describing_sound(events, words)
        self.assertEqual(out[0]["filter_decision"], "host_describing_sound")

    def test_drops_when_context_has_descriptive_phrase(self):
        events = [make_event(50.0, 50.3)]
        words = [
            make_word(48.0, 48.5, "聽起來"),
            make_word(48.5, 48.8, "像"),
            make_word(49.0, 49.5, "清喉嚨"),
            make_word(51.0, 51.5, "的"),
            make_word(51.5, 52.0, "聲音"),
        ]
        out = filter_host_describing_sound(events, words)
        self.assertEqual(out[0]["filter_decision"], "host_describing_sound")


class TestFilterLongGapSubsume(unittest.TestCase):
    def test_keeps_event_in_normal_gap(self):
        # event in 0.6s gap → keep
        ev = make_event(10.0, 10.3)
        ev["prev_word_end"] = 9.8
        ev["next_word_start"] = 10.5
        out = filter_long_gap_subsume([ev])
        self.assertEqual(out[0]["filter_decision"], "ok")

    def test_drops_event_in_long_gap(self):
        # event in 18s gap → subsumed
        ev = make_event(10.0, 10.3)
        ev["prev_word_end"] = 5.0
        ev["next_word_start"] = 23.0
        out = filter_long_gap_subsume([ev])
        self.assertEqual(out[0]["filter_decision"], "subsumed_by_silence_trim")

    def test_threshold_at_3_seconds(self):
        # event in 2.9s gap → keep, in 3.1s gap → drop
        for gap_size, expected in [(2.9, "ok"), (3.1, "subsumed_by_silence_trim")]:
            ev = make_event(10.0, 10.3)
            ev["prev_word_end"] = 10.0 - gap_size / 2
            ev["next_word_start"] = 10.3 + gap_size / 2
            out = filter_long_gap_subsume([ev])
            self.assertEqual(
                out[0]["filter_decision"], expected,
                f"gap={gap_size}s expected {expected}, got {out[0]['filter_decision']}",
            )

    def test_handles_none_neighbors(self):
        ev = make_event(0.0, 0.3)
        ev["prev_word_end"] = None
        ev["next_word_start"] = 5.0
        out = filter_long_gap_subsume([ev])
        # No prev → can't compute gap; default to keep
        self.assertEqual(out[0]["filter_decision"], "ok")


if __name__ == "__main__":
    unittest.main()
