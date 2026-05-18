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


def filter_boundary_too_tight(events: list[dict],
                              min_distance: float = 0.1,
                              min_duration: float = 0.15) -> list[dict]:
    """Filter 3 — soft-mark events that are too short or too close to next word.

    Unlike filters 1/2/4, this does NOT remove the event. It marks it so the
    review UI can show a warning ("too short to cut cleanly") while still
    surfacing the candidate for the user to consider.
    """
    for ev in events:
        if ev.get("filter_decision") and ev["filter_decision"] != "ok":
            continue
        duration = ev["end"] - ev["start"]
        next_start = ev.get("next_word_start")
        dist_to_next = (next_start - ev["end"]) if next_start is not None else float("inf")
        if duration < min_duration or dist_to_next < min_distance:
            ev["filter_decision"] = "boundary_too_tight"
    return events


def filter_speech_artifact_cluster(events: list[dict],
                                    window: float = 2.0,
                                    min_count: int = 3) -> list[dict]:
    """Filter 4 — drop all events in a same-type cluster of ≥min_count within `window` s.

    Treats clustered same-type events as natural speech artifacts (e.g. continuous
    mouth-smack noise during speaking), not discrete nuisances.
    """
    by_type: dict[str, list[dict]] = {}
    for ev in events:
        by_type.setdefault(ev["type"], []).append(ev)

    for etype, lst in by_type.items():
        lst.sort(key=lambda e: e["start"])
        # Slide a window
        n = len(lst)
        in_cluster = [False] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and (lst[j + 1]["start"] - lst[i]["start"]) <= window:
                j += 1
            if (j - i + 1) >= min_count:
                for k in range(i, j + 1):
                    in_cluster[k] = True
            i += 1
        for ev, marked in zip(lst, in_cluster):
            if marked and (not ev.get("filter_decision") or ev["filter_decision"] == "ok"):
                ev["filter_decision"] = "speech_artifact_cluster"
    return events


# Soft-keep decisions appear in the final list (with a warning); other non-"ok"
# decisions cause the event to be dropped.
SOFT_KEEP_DECISIONS = {"boundary_too_tight"}


def apply_all_filters(events: list[dict],
                      words: list[dict],
                      detect_types: tuple[str, ...] = ("throat_clear", "nose_clear"),
                      ) -> tuple[list[dict], list[dict]]:
    """Run the full filter pipeline in spec order.

    Returns (kept_events, dropped_events). `kept_events` may include soft-kept
    items (boundary_too_tight). `dropped_events` is for diagnostics / debug.
    """
    # First: drop events of types not in detect_types
    for ev in events:
        if ev["type"] not in detect_types:
            ev["filter_decision"] = "type_disabled"

    filter_host_describing_sound(events, words)
    filter_long_gap_subsume(events)
    filter_speech_artifact_cluster(events)
    filter_boundary_too_tight(events)

    kept, dropped = [], []
    for ev in events:
        decision = ev.get("filter_decision", "ok")
        if decision == "ok" or decision in SOFT_KEEP_DECISIONS:
            kept.append(ev)
        else:
            dropped.append(ev)
    return kept, dropped
