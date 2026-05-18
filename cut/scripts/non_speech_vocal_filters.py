"""Post-detection filters for non-speech vocal events.

Each filter takes a list of events (already annotated with zone, overlap_ratio,
prev_word_end, next_word_start by detect_non_speech_vocals_gemini), inspects
each event, and sets event["filter_decision"] in-place. The final filter pass
in apply_all_filters() removes events whose decision is not "ok" or a soft-keep
marker (currently only "boundary_too_tight" is soft-kept).
"""
from __future__ import annotations

import re

# Words / characters indicating the speaker is *describing* or *mimicking* a
# non-speech sound rather than producing one as a nuisance.
DESCRIBING_KEYWORDS = (
    "聽起來像", "聽起來是", "類似", "像是", "那種聲音", "清喉嚨",
    "嗯哼", "咳嗽", "咳的聲音", "鼻音", "鼻子的聲音",
)

# Single Chinese characters that, when REPEATED close together, indicate the
# speaker is mimicking a sound (e.g. "口口口", "嗯嗯嗯" describing it).
MIMICRY_REPEATED_CHARS = set("口咳嗯哼啊呃")


def _words_in_window(words: list[dict], center: float, half: float = 2.0) -> list[dict]:
    """Return Whisper words (non-gap) overlapping [center-half, center+half]."""
    lo, hi = center - half, center + half
    return [w for w in words
            if not w.get("isGap")
            and w.get("end", 0) >= lo
            and w.get("start", 0) <= hi]


def filter_host_describing_sound(events: list[dict], words: list[dict]) -> list[dict]:
    """Filter 1 — drop events where surrounding Whisper text indicates mimicry."""
    for ev in events:
        if ev.get("filter_decision") and ev["filter_decision"] != "ok":
            continue
        center = (ev["start"] + ev["end"]) / 2
        nearby = _words_in_window(words, center, half=2.0)
        text = "".join(w.get("text", "") for w in nearby)

        # Check for descriptive phrases
        if any(kw in text for kw in DESCRIBING_KEYWORDS):
            ev["filter_decision"] = "host_describing_sound"
            continue

        # Check for repeated single-character mimicry (e.g. "口口口")
        for ch in MIMICRY_REPEATED_CHARS:
            # Three or more occurrences of the same character within the window
            if text.count(ch) >= 3:
                ev["filter_decision"] = "host_describing_sound"
                break
    return events


def filter_long_gap_subsume(events: list[dict], gap_threshold: float = 3.0) -> list[dict]:
    """Filter 2 — drop events sitting in a Whisper word-gap larger than `gap_threshold`."""
    for ev in events:
        if ev.get("filter_decision") and ev["filter_decision"] != "ok":
            continue
        prev_end = ev.get("prev_word_end")
        next_start = ev.get("next_word_start")
        if prev_end is None or next_start is None:
            continue  # can't compute gap; keep
        gap = next_start - prev_end
        # Compute the "silence-only" gap, excluding the event width
        event_width = ev.get("end", 0) - ev.get("start", 0)
        silence_gap = gap - event_width
        if silence_gap > gap_threshold:
            ev["filter_decision"] = "subsumed_by_silence_trim"
    return events
