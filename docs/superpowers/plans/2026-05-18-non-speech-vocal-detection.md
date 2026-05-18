# Non-Speech Vocal Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Gemini-based non-speech vocal (throat-clear / nose-clear) detection as Stage 2.5 of the cut pipeline, surfacing detected events in `review_enhanced.html` as flag-for-confirmation edits.

**Architecture:** New Python script scans `audio.mp3` in 30s chunks (5s overlap) via Gemini-2.5-flash, cross-references with Whisper words, applies 4 precision filters, writes `non_speech_vocals.json`. `run_fine_analysis.js` integrates these as `non_speech_vocal:*` edits with `needsReview: true`. Review UI gets a dedicated "非語音聲響" section. User decisions feedback into `cut/user-prefs/<userId>/non_speech_vocal_feedback.jsonl`.

**Tech Stack:** Python 3.12+ (faster-whisper venv), `google-genai`, `librosa`, `numpy`, `soundfile`; Node.js; existing `js-yaml` for prefs.

**Source of truth:** `docs/superpowers/specs/2026-05-18-non-speech-vocal-detection-design.md` — refer back for any architectural question.

---

## Pre-flight (do this once at start)

- [ ] **Step 0.1: Confirm spike artifacts are present**

Run:
```bash
ls /Users/kaiwei/side-projects/podcast-edit-skill/output/test_host-ted-5min/cut/2_analysis/spike_gemini_events.json
ls /Users/kaiwei/side-projects/podcast-edit-skill/source/host-ted-5min.wav
```

Expected: both files exist. The spike output and the audio file are needed for validation throughout the plan.

- [ ] **Step 0.2: Confirm GEMINI_API_KEY**

Run:
```bash
grep -c "^GEMINI_API_KEY=" /Users/kaiwei/side-projects/podcast-edit-skill/.env
```

Expected: `1`. If `0`, see spec §11 — set the key in `.env` before proceeding.

- [ ] **Step 0.3: Confirm Python env**

The spike venv at `/tmp/yamnet_spike` has tensorflow + librosa + google-genai + faster-whisper. For production, the **project root** needs these added to `cut/scripts/requirements.txt` so users install via `pip install -r cut/scripts/requirements.txt` per CLAUDE.md.

```bash
python3 -c "import google.genai; import librosa; import soundfile" 2>&1
```

If errors: that's expected for fresh checkouts; Task 1 will add the deps to requirements.

---

## Phase A: Python Detector Core

### Task 1: Add Python dependencies

**Files:**
- Modify: `cut/scripts/requirements.txt`

- [ ] **Step 1.1: Update requirements file**

Current content is `faster-whisper>=1.2.1`. Append the new deps (everything below is on its own line):

```
faster-whisper>=1.2.1
google-genai>=1.5.0
librosa>=0.10.0
soundfile>=0.12.0
numpy>=1.24.0
```

(Note: `librosa` brings in scipy/numpy transitively but pin explicitly so failures are clearer.)

- [ ] **Step 1.2: Verify install on the user's Python**

Run:
```bash
pip install -r /Users/kaiwei/side-projects/podcast-edit-skill/cut/scripts/requirements.txt
```

Expected: clean install. If Python 3.14 incompatibility, document in CLAUDE.md (see Task 16) that NSV detection requires Python ≤ 3.13 — and create a venv path. For Python 3.13 or earlier this should just work.

- [ ] **Step 1.3: Commit**

```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
git add cut/scripts/requirements.txt
git commit -m "feat(cut): add google-genai + librosa for non-speech vocal detection"
```

---

### Task 2: Detector skeleton + Gemini chunked inference

**Files:**
- Create: `cut/scripts/detect_non_speech_vocals_gemini.py`

This task builds the bare-bones detector that calls Gemini and dumps raw responses. Filters and refinement come later. Functions are kept module-level so Task 3+ unit tests can import them.

- [ ] **Step 2.1: Create the file with the prompt + module skeleton**

Write the full content to `cut/scripts/detect_non_speech_vocals_gemini.py`:

```python
#!/usr/bin/env python3
"""
Stage 2.5 — Non-speech vocal event detection (Gemini-2.5-flash).

Scans audio.mp3 in 30s chunks (5s overlap), asks Gemini to flag throat-clear /
nose-clear nuisance sounds with timestamps. Cross-references with Whisper word
timestamps and applies post-detection filters to push precision from ~58% (raw)
toward 75-85% (per spike: docs/superpowers/specs/2026-05-18-non-speech-vocal-detection-design.md).

Usage:
    python3 detect_non_speech_vocals_gemini.py <BASE_DIR>

Where BASE_DIR is the project's cut/ output dir, e.g.:
    output/2026-05-18_episode-name/cut/

Reads:
    {BASE_DIR}/1_transcript/audio.mp3
    {BASE_DIR}/1_transcript/subtitles_words.json

Writes:
    {BASE_DIR}/2_analysis/non_speech_vocals.json
    {BASE_DIR}/2_analysis/non_speech_vocals_raw.json   (debug — Gemini raw responses)

Env:
    GEMINI_API_KEY  (from .env at repo root or environment)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

CHUNK_SEC = 30
HOP_SEC = 25
MODEL = "gemini-2.5-flash"
MAX_RETRIES = 3

CONF_MAP = {
    "very high": 0.95, "high": 0.85, "medium": 0.6,
    "low": 0.4, "very low": 0.2,
}

PROMPT = """You are an audio editor reviewing a clip from a Mandarin Chinese podcast.

The HOST has a recurring problem: brief NON-SPEECH VOCAL noises that listeners
notice and want removed:
- 清喉嚨 / throat clearing — a short rasping, wet, or scratching sound (NOT speech)
- 清鼻子 / nose clearing / snort / sniff — a brief nasal sound

These typically happen:
- Right BEFORE a sentence (preparation sound)
- Right AFTER a sentence ends
- Occasionally in mid-pause

Do NOT flag the following (they are natural and intentional):
- Normal calm breathing between sentences
- Sharp inhalations before speaking (these are natural prep, NOT nuisance)
- Filler words "嗯", "啊", "呃", "對" (handled by a separate system)
- Lip/tongue sounds embedded inside actual speech
- Mouth clicks during continuous speaking
- Laughter, emotive sighs, deliberate emphasis
- Background hum, music, room tone
- The speaker MIMICKING or DESCRIBING throat-clearing sounds as content
  (e.g. if they say "聽起來像口口口的聲音", do NOT flag the imitation)

For each non-speech vocal nuisance, report:
- offset_s: seconds from the START of this clip (0.0 to clip duration)
- duration_s: estimated duration in seconds (typically 0.1-0.6)
- type: one of "throat_clear" or "nose_clear"
- confidence: NUMERIC value 0.0-1.0 (NOT "high"/"medium"/"low")
- description: short note in Chinese

Be conservative — if uncertain, do NOT flag it.

Return JSON only (no markdown, no commentary):
{"events": [
  {"offset_s": 12.3, "duration_s": 0.4, "type": "throat_clear",
   "confidence": 0.9, "description": "句首濕潤的清喉嚨聲"}
]}

If nothing to flag, return: {"events": []}
"""


def load_api_key() -> str:
    """Load GEMINI_API_KEY from env or .env at repo root."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    # Walk upward from this script until we find a .env
    here = Path(__file__).resolve()
    for parent in here.parents:
        env = parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError(
        "GEMINI_API_KEY not found. Set in environment or in .env at repo root."
    )


def get_audio_duration(path: Path) -> float:
    """Return audio duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def extract_clip(src: Path, start: float, duration: float, dest: str) -> None:
    """Extract a single mono 16kHz WAV clip via ffmpeg."""
    cmd = [
        "ffmpeg", "-v", "quiet",
        "-i", str(src),
        "-ss", f"{start}",
        "-t", f"{duration}",
        "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-y", dest,
    ]
    subprocess.run(cmd, check=True)


def parse_json_response(text: str | None) -> dict | None:
    """Extract a JSON object from a Gemini response (mirrors ai_listen.py)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def normalize_confidence(c) -> float:
    """Coerce confidence to a float in [0, 1]; strings like 'high' map to numeric."""
    if isinstance(c, (int, float)):
        return float(max(0.0, min(1.0, c)))
    if isinstance(c, str):
        return CONF_MAP.get(c.lower().strip(), 0.5)
    return 0.5


def call_gemini_with_retry(client, audio_bytes: bytes) -> str | None:
    """Call Gemini with exponential backoff. Returns text on success, None on hard failure."""
    from google.genai import types
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=[
                    PROMPT,
                    types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                ],
            )
            return resp.text
        except Exception as e:
            last_error = e
            msg = str(e)
            if any(code in msg for code in ("503", "429", "RATE_LIMIT", "UNAVAILABLE")):
                wait = 2 ** (attempt + 1)
                print(f"    retryable error ({msg[:80]}); sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            # Non-retryable
            break
    print(f"    Gemini call failed after {MAX_RETRIES} attempts: {last_error}", file=sys.stderr)
    return None


def build_chunks(duration: float, chunk_sec: float = CHUNK_SEC,
                 hop_sec: float = HOP_SEC) -> list[tuple[float, float]]:
    """Return [(start, end), ...] covering the audio with `hop`-spaced windows."""
    chunks = []
    t = 0.0
    while t < duration:
        end = min(t + chunk_sec, duration)
        chunks.append((t, end))
        if end >= duration:
            break
        t += hop_sec
    return chunks


def scan_audio(audio_path: Path, client) -> tuple[list[dict], list[dict], int]:
    """Run Gemini over each chunk. Returns (events, raw_responses, failed_chunks)."""
    duration = get_audio_duration(audio_path)
    chunks = build_chunks(duration)
    print(f"Audio duration: {duration:.1f}s; total chunks: {len(chunks)}", flush=True)

    events: list[dict] = []
    raw_responses: list[dict] = []
    failed_chunks = 0

    with tempfile.TemporaryDirectory() as tmp:
        for i, (start, end) in enumerate(chunks):
            clip = os.path.join(tmp, f"chunk_{i}.wav")
            extract_clip(audio_path, start, end - start, clip)
            with open(clip, "rb") as f:
                audio_bytes = f.read()

            print(f"  [{i+1}/{len(chunks)}] {start:.1f}-{end:.1f}s …", end=" ", flush=True)
            t0 = time.time()
            text = call_gemini_with_retry(client, audio_bytes)
            elapsed = time.time() - t0
            if text is None:
                failed_chunks += 1
                print(f"FAIL ({elapsed:.1f}s)")
                raw_responses.append({"chunk": [start, end], "error": "all retries failed"})
                continue

            parsed = parse_json_response(text)
            n_events = len(parsed.get("events", [])) if parsed else 0
            print(f"({elapsed:.1f}s, {n_events} events)")
            raw_responses.append({"chunk": [start, end], "raw": text, "parsed": parsed})

            if not parsed:
                continue
            for ev in parsed.get("events", []):
                offset = ev.get("offset_s")
                dur = ev.get("duration_s", 0.3)
                if offset is None:
                    continue
                events.append({
                    "start": round(start + offset, 3),
                    "end": round(start + offset + dur, 3),
                    "type": ev.get("type", "other"),
                    "confidence": normalize_confidence(ev.get("confidence", 0.5)),
                    "description": ev.get("description", ""),
                    "chunk_start": start,
                })

    return events, raw_responses, failed_chunks


def main():
    parser = argparse.ArgumentParser(
        description="Detect non-speech vocal events in podcast audio via Gemini."
    )
    parser.add_argument("base_dir", help="Cut output directory (contains 1_transcript/, 2_analysis/)")
    args = parser.parse_args()

    base = Path(args.base_dir).resolve()
    audio_path = base / "1_transcript" / "audio.mp3"
    out_dir = base / "2_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    final_out = out_dir / "non_speech_vocals.json"
    raw_out = out_dir / "non_speech_vocals_raw.json"

    if not audio_path.exists():
        print(f"❌ audio missing: {audio_path}", file=sys.stderr)
        sys.exit(2)

    try:
        api_key = load_api_key()
    except RuntimeError as e:
        print(f"⚠️  {e}; skipping NSV detection (pipeline-graceful failure).", file=sys.stderr)
        # Emit an empty + degraded record so downstream tools see consistent shape
        final_out.write_text(json.dumps({
            "audio": str(audio_path.relative_to(base)),
            "duration": get_audio_duration(audio_path),
            "model": MODEL,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "degraded": True,
            "stats": {"total_chunks": 0, "failed_chunks": 0, "raw_events": 0,
                      "after_dedup": 0, "after_filters": 0, "by_type": {}},
            "events": [],
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    from google import genai
    client = genai.Client(api_key=api_key)

    events, raw_responses, failed_chunks = scan_audio(audio_path, client)
    duration = get_audio_duration(audio_path)
    degraded = failed_chunks >= 2

    # Write debug raw responses
    raw_out.write_text(json.dumps({"chunks": raw_responses}, ensure_ascii=False, indent=2))

    # NOTE: dedup, cross-ref, refinement, and filters added in Tasks 3-9.
    # For now, write the events as-is so we can validate the chunking loop end-to-end.
    final_out.write_text(json.dumps({
        "audio": str(audio_path.relative_to(base)),
        "duration": duration,
        "model": MODEL,
        "chunk_sec": CHUNK_SEC,
        "hop_sec": HOP_SEC,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "degraded": degraded,
        "stats": {
            "total_chunks": len(build_chunks(duration)),
            "failed_chunks": failed_chunks,
            "raw_events": len(events),
            "after_dedup": len(events),
            "after_filters": len(events),
            "by_type": _count_by_type(events),
        },
        "events": events,
    }, ensure_ascii=False, indent=2))

    print(f"\n✅ wrote {final_out}  ({len(events)} events, degraded={degraded})")


def _count_by_type(events: list[dict]) -> dict:
    from collections import Counter
    return dict(Counter(e["type"] for e in events))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2.2: Make executable**

```bash
chmod +x /Users/kaiwei/side-projects/podcast-edit-skill/cut/scripts/detect_non_speech_vocals_gemini.py
```

- [ ] **Step 2.3: Validate against the 5-min sample**

Run:
```bash
python3 /Users/kaiwei/side-projects/podcast-edit-skill/cut/scripts/detect_non_speech_vocals_gemini.py \
  /Users/kaiwei/side-projects/podcast-edit-skill/output/test_host-ted-5min/cut
```

Expected: prints `[k/12] s.s-e.es … (Xs, N events)` for each chunk, then writes both JSON files. Total ~30-50 events for the 5-min sample (matches spike count of 50). 503 errors retry transparently.

Inspect:
```bash
jq '.stats' /Users/kaiwei/side-projects/podcast-edit-skill/output/test_host-ted-5min/cut/2_analysis/non_speech_vocals.json
```

Expected output similar to:
```json
{
  "total_chunks": 12,
  "failed_chunks": 0,
  "raw_events": 38,
  "after_dedup": 38,
  "after_filters": 38,
  "by_type": {"throat_clear": 9, "nose_clear": 10, "click_smack": 12, "sharp_breath": 11}
}
```

Note: `click_smack`/`sharp_breath` may appear in raw output because the prompt allows them — Task 9's filter pass will remove them. For now we're confirming the loop works.

- [ ] **Step 2.4: Commit**

```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
git add cut/scripts/detect_non_speech_vocals_gemini.py
git commit -m "feat(cut): add Gemini-based non-speech vocal detector skeleton"
```

---

### Task 3: Unit-test parse + confidence normalization

**Files:**
- Create: `cut/scripts/test_detect_non_speech_vocals.py`

The script has pure helpers (`parse_json_response`, `normalize_confidence`, `build_chunks`) that benefit from unit tests. Use stdlib `unittest` to avoid adding a test framework.

- [ ] **Step 3.1: Write failing tests**

Create `cut/scripts/test_detect_non_speech_vocals.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3.2: Run tests to verify they pass**

Run:
```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
python3 -m unittest cut.scripts.test_detect_non_speech_vocals -v
```

Expected output ends with `OK` and all tests pass (the helpers are already implemented in Task 2).

If any test fails, fix the helper in `detect_non_speech_vocals_gemini.py` to match the test (the test is the spec for behavior).

- [ ] **Step 3.3: Commit**

```bash
git add cut/scripts/test_detect_non_speech_vocals.py
git commit -m "test(cut): unit tests for NSV detector helpers"
```

---

### Task 4: Deduplication of overlapping chunk events

**Files:**
- Modify: `cut/scripts/detect_non_speech_vocals_gemini.py` (add `dedupe_events`)
- Modify: `cut/scripts/test_detect_non_speech_vocals.py` (add dedup tests)

Adjacent chunks have 5s overlap → same event may appear twice. Dedup by collapsing events within 0.5s of same type.

- [ ] **Step 4.1: Write the failing test**

Append to `cut/scripts/test_detect_non_speech_vocals.py`:

```python
from detect_non_speech_vocals_gemini import dedupe_events  # added in Task 4


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
```

- [ ] **Step 4.2: Run test to verify it fails**

```bash
python3 -m unittest cut.scripts.test_detect_non_speech_vocals.TestDedupe -v
```

Expected: `ImportError: cannot import name 'dedupe_events'`.

- [ ] **Step 4.3: Add `dedupe_events` to the detector**

Open `cut/scripts/detect_non_speech_vocals_gemini.py`. Add this function above `main()`:

```python
def dedupe_events(events: list[dict], time_window: float = 0.5) -> list[dict]:
    """Merge overlapping events of the same type from adjacent chunks.

    Two events of the same type with start times within `time_window`
    seconds are collapsed; we keep the higher-confidence one.
    """
    sorted_events = sorted(events, key=lambda x: x["start"])
    deduped: list[dict] = []
    for ev in sorted_events:
        if (deduped
                and ev["start"] - deduped[-1]["start"] < time_window
                and ev["type"] == deduped[-1]["type"]):
            if ev["confidence"] > deduped[-1]["confidence"]:
                deduped[-1] = ev
            continue
        deduped.append(ev)
    return deduped
```

Wire it into `main()`: replace the `events` write with deduped events. Replace this block in `main()`:

```python
    events, raw_responses, failed_chunks = scan_audio(audio_path, client)
    duration = get_audio_duration(audio_path)
```

with:

```python
    raw_events, raw_responses, failed_chunks = scan_audio(audio_path, client)
    events = dedupe_events(raw_events)
    duration = get_audio_duration(audio_path)
```

And update the `stats` block to use both counts:

```python
        "stats": {
            "total_chunks": len(build_chunks(duration)),
            "failed_chunks": failed_chunks,
            "raw_events": len(raw_events),
            "after_dedup": len(events),
            "after_filters": len(events),
            "by_type": _count_by_type(events),
        },
```

- [ ] **Step 4.4: Run tests to verify pass**

```bash
python3 -m unittest cut.scripts.test_detect_non_speech_vocals -v
```

Expected: all green.

- [ ] **Step 4.5: Commit**

```bash
git add cut/scripts/detect_non_speech_vocals_gemini.py cut/scripts/test_detect_non_speech_vocals.py
git commit -m "feat(cut): dedupe overlapping NSV events from adjacent chunks"
```

---

### Task 5: Cross-reference with Whisper words (zone classification)

**Files:**
- Modify: `cut/scripts/detect_non_speech_vocals_gemini.py`
- Modify: `cut/scripts/test_detect_non_speech_vocals.py`

Each event gets `zone`, `overlap_ratio`, `prev_word_end`, `next_word_start` — needed for filters and refinement.

- [ ] **Step 5.1: Write the failing test**

Append to `cut/scripts/test_detect_non_speech_vocals.py`:

```python
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
```

- [ ] **Step 5.2: Run test to verify it fails**

```bash
python3 -m unittest cut.scripts.test_detect_non_speech_vocals -v
```

Expected: `ImportError`.

- [ ] **Step 5.3: Implement zone classification in the detector**

Open `cut/scripts/detect_non_speech_vocals_gemini.py`. Add these functions above `main()`:

```python
def load_word_spans(words_json_path: Path) -> list[tuple[float, float]]:
    """Load subtitles_words.json and return speech-word (start, end) tuples, sorted."""
    data = json.loads(words_json_path.read_text())
    if isinstance(data, dict) and "words" in data:
        data = data["words"]
    spans = []
    for w in data:
        if w.get("isGap") or w.get("isSpeakerLabel"):
            continue
        s, e = w.get("start"), w.get("end")
        if s is None or e is None:
            continue
        spans.append((s, e))
    spans.sort()
    return spans


def classify_zone(start: float, end: float,
                  spans: list[tuple[float, float]]
                  ) -> tuple[str, float, float | None, float | None]:
    """Classify an event by overlap with the speech-word spans.

    Returns (zone, overlap_ratio, prev_word_end, next_word_start).
    Zones: "pure_gap" (<5% overlap), "partial_overlap" (5-50%), "inside_word" (>=50%).
    """
    overlap = 0.0
    prev_end, next_start = None, None
    for ws, we in spans:
        if we <= start:
            prev_end = we
            continue
        if ws >= end:
            if next_start is None:
                next_start = ws
            break
        overlap += min(we, end) - max(ws, start)
    duration = max(end - start, 1e-9)
    ratio = overlap / duration
    if ratio < 0.05:
        zone = "pure_gap"
    elif ratio < 0.5:
        zone = "partial_overlap"
    else:
        zone = "inside_word"
    return zone, ratio, prev_end, next_start


def annotate_events_with_word_context(events: list[dict],
                                       spans: list[tuple[float, float]]) -> list[dict]:
    """Add zone / overlap_ratio / prev_word_end / next_word_start to each event."""
    out = []
    for ev in events:
        zone, ratio, prev_end, next_start = classify_zone(ev["start"], ev["end"], spans)
        out.append({
            **ev,
            "zone": zone,
            "overlap_ratio": round(ratio, 3),
            "prev_word_end": prev_end,
            "next_word_start": next_start,
        })
    return out
```

Wire into `main()`: replace this part of `main()`:

```python
    raw_events, raw_responses, failed_chunks = scan_audio(audio_path, client)
    events = dedupe_events(raw_events)
    duration = get_audio_duration(audio_path)
```

with:

```python
    words_path = base / "1_transcript" / "subtitles_words.json"
    if not words_path.exists():
        print(f"⚠️  {words_path} missing — events will lack zone info.", file=sys.stderr)
        word_spans = []
    else:
        word_spans = load_word_spans(words_path)

    raw_events, raw_responses, failed_chunks = scan_audio(audio_path, client)
    events = dedupe_events(raw_events)
    events = annotate_events_with_word_context(events, word_spans)
    duration = get_audio_duration(audio_path)
```

- [ ] **Step 5.4: Run tests + integration check**

```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
python3 -m unittest cut.scripts.test_detect_non_speech_vocals -v
```

Expected: all tests pass.

Integration check — re-run on the 5-min sample:

```bash
python3 cut/scripts/detect_non_speech_vocals_gemini.py \
  output/test_host-ted-5min/cut

jq '.events[0]' output/test_host-ted-5min/cut/2_analysis/non_speech_vocals.json
```

Expected: first event has `zone`, `overlap_ratio`, `prev_word_end`, `next_word_start` fields populated.

- [ ] **Step 5.5: Commit**

```bash
git add cut/scripts/detect_non_speech_vocals_gemini.py cut/scripts/test_detect_non_speech_vocals.py
git commit -m "feat(cut): cross-reference NSV events with Whisper word boundaries"
```

---

### Task 6: Boundary refinement using librosa

**Files:**
- Modify: `cut/scripts/detect_non_speech_vocals_gemini.py`
- Modify: `cut/scripts/test_detect_non_speech_vocals.py`

Gemini timestamps drift ±0.5s. Snap event edges to local RMS-envelope onset/offset for clean cuts.

- [ ] **Step 6.1: Write the failing test**

Append to `cut/scripts/test_detect_non_speech_vocals.py`:

```python
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
```

- [ ] **Step 6.2: Run test to verify it fails**

```bash
python3 -m unittest cut.scripts.test_detect_non_speech_vocals.TestRefineEventBoundary -v
```

Expected: `ImportError: cannot import name 'refine_event_boundary'`.

- [ ] **Step 6.3: Implement boundary refinement**

In `cut/scripts/detect_non_speech_vocals_gemini.py`, add at the top of the file (with other imports):

```python
import numpy as np
```

Add this function above `main()`:

```python
def refine_event_boundary(
    audio: "np.ndarray",
    sr: int,
    gem_start: float,
    gem_end: float,
    padding: float = 0.3,
    db_drop: float = 12.0,
) -> tuple[float, float]:
    """Snap event boundary to local RMS-envelope minima.

    Strategy:
        Take audio in [gem_start - padding, gem_end + padding].
        Compute RMS envelope at 10ms hop.
        Find the peak inside [gem_start, gem_end].
        Walk left/right until RMS drops `db_drop` dB below the peak.
        Return absolute (refined_start, refined_end).
    """
    import librosa

    duration = len(audio) / sr
    s = max(0.0, gem_start - padding)
    e = min(duration, gem_end + padding)
    region = audio[int(s * sr): int(e * sr)]
    if len(region) < int(sr * 0.05):
        # Region too short for analysis — return inputs clamped
        return max(0.0, gem_start), min(duration, gem_end)

    hop_s = 0.01
    rms = librosa.feature.rms(
        y=region,
        frame_length=int(sr * 0.03),
        hop_length=int(sr * hop_s),
    )[0]
    rms_db = 20 * np.log10(rms + 1e-9)

    # Find peak idx inside [gem_start - s, gem_end - s] in seconds
    inside_start = max(0, int((gem_start - s) / hop_s))
    inside_end = min(len(rms_db), int((gem_end - s) / hop_s))
    if inside_end <= inside_start:
        return max(0.0, gem_start), min(duration, gem_end)
    peak_idx_rel = int(np.argmax(rms_db[inside_start:inside_end]))
    peak_idx = inside_start + peak_idx_rel

    threshold = rms_db[peak_idx] - db_drop

    left = peak_idx
    while left > 0 and rms_db[left] > threshold:
        left -= 1
    right = peak_idx
    while right < len(rms_db) - 1 and rms_db[right] > threshold:
        right += 1

    refined_start = s + left * hop_s
    refined_end = s + right * hop_s
    return max(0.0, refined_start), min(duration, refined_end)
```

Wire into `main()`. Add after the `events = annotate_events_with_word_context(events, word_spans)` line:

```python
    # Load audio once for boundary refinement
    import soundfile as sf
    audio_data, audio_sr = sf.read(str(audio_path))
    if audio_data.ndim > 1:
        audio_data = audio_data.mean(axis=1)
    audio_data = audio_data.astype("float32")
    if audio_sr != 16000:
        import librosa
        audio_data = librosa.resample(audio_data, orig_sr=audio_sr, target_sr=16000)
        audio_sr = 16000

    for ev in events:
        rs, re_ = refine_event_boundary(audio_data, audio_sr, ev["start"], ev["end"])
        ev["refined_start"] = round(rs, 3)
        ev["refined_end"] = round(re_, 3)
```

- [ ] **Step 6.4: Run tests + integration check**

```bash
python3 -m unittest cut.scripts.test_detect_non_speech_vocals -v
```

Expected: all green.

Integration:

```bash
python3 cut/scripts/detect_non_speech_vocals_gemini.py \
  output/test_host-ted-5min/cut

jq '.events[0:3] | .[] | {start, end, refined_start, refined_end}' \
  output/test_host-ted-5min/cut/2_analysis/non_speech_vocals.json
```

Expected: each event has `refined_start`/`refined_end`, typically within ±0.3s of original.

- [ ] **Step 6.5: Commit**

```bash
git add cut/scripts/detect_non_speech_vocals_gemini.py cut/scripts/test_detect_non_speech_vocals.py
git commit -m "feat(cut): refine NSV event boundaries via librosa RMS envelope"
```

---

## Phase B: Post-Detection Filters

### Task 7: Filter module — context + long-gap subsume

**Files:**
- Create: `cut/scripts/non_speech_vocal_filters.py`
- Create: `cut/scripts/test_non_speech_vocal_filters.py`

Per spec §6: filter 1 (mimicry detection via Whisper context) + filter 2 (long-gap subsume).

- [ ] **Step 7.1: Write failing tests**

Create `cut/scripts/test_non_speech_vocal_filters.py`:

```python
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
```

- [ ] **Step 7.2: Run tests to verify they fail**

```bash
python3 -m unittest cut.scripts.test_non_speech_vocal_filters -v
```

Expected: `ImportError: cannot import name 'filter_host_describing_sound'`.

- [ ] **Step 7.3: Implement filters 1 + 2**

Create `cut/scripts/non_speech_vocal_filters.py`:

```python
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
        if gap > gap_threshold:
            ev["filter_decision"] = "subsumed_by_silence_trim"
    return events
```

- [ ] **Step 7.4: Run tests to verify pass**

```bash
python3 -m unittest cut.scripts.test_non_speech_vocal_filters -v
```

Expected: all green.

- [ ] **Step 7.5: Commit**

```bash
git add cut/scripts/non_speech_vocal_filters.py cut/scripts/test_non_speech_vocal_filters.py
git commit -m "feat(cut): NSV filters 1+2 (host describing, long-gap subsume)"
```

---

### Task 8: Filter module — boundary tight + cluster

**Files:**
- Modify: `cut/scripts/non_speech_vocal_filters.py`
- Modify: `cut/scripts/test_non_speech_vocal_filters.py`

Filter 3 (boundary too tight) keeps the event but soft-marks it; Filter 4 (cluster of ≥3 same-type within 2s) drops all in the cluster.

- [ ] **Step 8.1: Write the failing tests**

Append to `cut/scripts/test_non_speech_vocal_filters.py`:

```python
from non_speech_vocal_filters import (
    filter_boundary_too_tight, filter_speech_artifact_cluster,
)


class TestFilterBoundaryTooTight(unittest.TestCase):
    def test_keeps_event_with_clear_boundary(self):
        ev = make_event(10.0, 10.3)
        ev["next_word_start"] = 11.0  # 0.7s gap to next
        out = filter_boundary_too_tight([ev])
        self.assertEqual(out[0]["filter_decision"], "ok")

    def test_marks_event_too_close_to_next_word(self):
        ev = make_event(10.0, 10.3)
        ev["next_word_start"] = 10.35  # 0.05s gap
        out = filter_boundary_too_tight([ev])
        self.assertEqual(out[0]["filter_decision"], "boundary_too_tight")

    def test_marks_event_too_short(self):
        ev = make_event(10.0, 10.10)  # 0.10s duration
        ev["next_word_start"] = 12.0
        out = filter_boundary_too_tight([ev])
        self.assertEqual(out[0]["filter_decision"], "boundary_too_tight")


class TestFilterCluster(unittest.TestCase):
    def test_keeps_isolated_events(self):
        events = [
            make_event(10.0, 10.3, "click_smack"),
            make_event(20.0, 20.3, "click_smack"),
            make_event(30.0, 30.3, "click_smack"),
        ]
        out = filter_speech_artifact_cluster(events)
        self.assertTrue(all(e["filter_decision"] == "ok" for e in out))

    def test_drops_cluster_of_3_within_2s(self):
        events = [
            make_event(10.0, 10.3, "click_smack"),
            make_event(10.5, 10.7, "click_smack"),
            make_event(11.5, 11.7, "click_smack"),
        ]
        out = filter_speech_artifact_cluster(events)
        for e in out:
            self.assertEqual(e["filter_decision"], "speech_artifact_cluster")

    def test_drops_cluster_of_4(self):
        events = [
            make_event(10.0, 10.2, "click_smack"),
            make_event(10.5, 10.7, "click_smack"),
            make_event(11.0, 11.2, "click_smack"),
            make_event(11.5, 11.7, "click_smack"),
        ]
        out = filter_speech_artifact_cluster(events)
        for e in out:
            self.assertEqual(e["filter_decision"], "speech_artifact_cluster")

    def test_does_not_cluster_different_types(self):
        events = [
            make_event(10.0, 10.3, "throat_clear"),
            make_event(10.5, 10.7, "nose_clear"),
            make_event(11.5, 11.7, "throat_clear"),
        ]
        out = filter_speech_artifact_cluster(events)
        self.assertTrue(all(e["filter_decision"] == "ok" for e in out))

    def test_2_in_window_not_dropped(self):
        events = [
            make_event(10.0, 10.3, "click_smack"),
            make_event(10.5, 10.7, "click_smack"),
        ]
        out = filter_speech_artifact_cluster(events)
        self.assertTrue(all(e["filter_decision"] == "ok" for e in out))
```

- [ ] **Step 8.2: Run tests to verify they fail**

```bash
python3 -m unittest cut.scripts.test_non_speech_vocal_filters -v
```

Expected: `ImportError`.

- [ ] **Step 8.3: Implement filters 3 + 4**

Append to `cut/scripts/non_speech_vocal_filters.py`:

```python
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
```

- [ ] **Step 8.4: Run tests to verify pass**

```bash
python3 -m unittest cut.scripts.test_non_speech_vocal_filters -v
```

Expected: all green.

- [ ] **Step 8.5: Commit**

```bash
git add cut/scripts/non_speech_vocal_filters.py cut/scripts/test_non_speech_vocal_filters.py
git commit -m "feat(cut): NSV filters 3+4 (boundary too tight, speech-artifact cluster)"
```

---

### Task 9: Filter orchestrator + integration into detector

**Files:**
- Modify: `cut/scripts/non_speech_vocal_filters.py`
- Modify: `cut/scripts/detect_non_speech_vocals_gemini.py`
- Modify: `cut/scripts/test_non_speech_vocal_filters.py`

Add `apply_all_filters()` that runs filters in spec order and emits final filtered list. Plug into the detector. Also drop click_smack / sharp_breath here (not in `detect_types`).

- [ ] **Step 9.1: Write the failing test**

Append to `cut/scripts/test_non_speech_vocal_filters.py`:

```python
from non_speech_vocal_filters import apply_all_filters


class TestApplyAllFilters(unittest.TestCase):
    def test_keeps_only_enabled_types(self):
        events = [
            make_event(10.0, 10.3, "throat_clear"),
            make_event(15.0, 15.3, "nose_clear"),
            make_event(20.0, 20.3, "click_smack"),
            make_event(25.0, 25.3, "sharp_breath"),
        ]
        out, dropped = apply_all_filters(events, words=[], detect_types=("throat_clear", "nose_clear"))
        self.assertEqual({e["type"] for e in out}, {"throat_clear", "nose_clear"})
        # The 2 dropped events still appear in dropped with reason "type_disabled"
        type_disabled = [d for d in dropped if d["filter_decision"] == "type_disabled"]
        self.assertEqual(len(type_disabled), 2)

    def test_runs_all_4_filters_in_order(self):
        # Same-type cluster of 3 with one in a long gap and one too short
        events = [
            make_event(10.0, 10.3, "throat_clear"),
            make_event(10.5, 10.7, "throat_clear"),
            make_event(11.0, 11.2, "throat_clear"),
        ]
        out, dropped = apply_all_filters(events, words=[],
                                          detect_types=("throat_clear", "nose_clear"))
        # All 3 cluster — all dropped
        self.assertEqual(out, [])
        self.assertEqual(len(dropped), 3)
        for d in dropped:
            self.assertEqual(d["filter_decision"], "speech_artifact_cluster")

    def test_keeps_boundary_too_tight_as_soft_keep(self):
        ev = make_event(10.0, 10.10, "throat_clear")  # 0.10s duration < 0.15
        ev["next_word_start"] = 12.0
        out, dropped = apply_all_filters([ev], words=[],
                                          detect_types=("throat_clear", "nose_clear"))
        # boundary_too_tight is a soft-keep — event stays in out
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["filter_decision"], "boundary_too_tight")
        self.assertEqual(dropped, [])
```

- [ ] **Step 9.2: Run test to verify it fails**

```bash
python3 -m unittest cut.scripts.test_non_speech_vocal_filters.TestApplyAllFilters -v
```

Expected: `ImportError`.

- [ ] **Step 9.3: Implement the orchestrator**

Append to `cut/scripts/non_speech_vocal_filters.py`:

```python
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
```

- [ ] **Step 9.4: Wire orchestrator into the detector**

In `cut/scripts/detect_non_speech_vocals_gemini.py`, near the top after other imports:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent))
from non_speech_vocal_filters import apply_all_filters
```

(If `sys.path.insert` already exists, skip the first line.)

In `main()`, after the boundary refinement loop, add the filter pass. Replace this block:

```python
    for ev in events:
        rs, re_ = refine_event_boundary(audio_data, audio_sr, ev["start"], ev["end"])
        ev["refined_start"] = round(rs, 3)
        ev["refined_end"] = round(re_, 3)
```

with:

```python
    for ev in events:
        rs, re_ = refine_event_boundary(audio_data, audio_sr, ev["start"], ev["end"])
        ev["refined_start"] = round(rs, 3)
        ev["refined_end"] = round(re_, 3)
        ev["filter_decision"] = "ok"  # baseline; filters may override

    # Load Whisper words (with isGap=True entries kept) for filter 1 context check
    words_full = []
    if words_path.exists():
        wd = json.loads(words_path.read_text())
        if isinstance(wd, dict) and "words" in wd:
            wd = wd["words"]
        words_full = wd

    after_dedup_count = len(events)
    kept, dropped = apply_all_filters(events, words_full,
                                       detect_types=("throat_clear", "nose_clear"))
    events = kept
```

Update the `stats` block to reflect the new counts:

```python
        "stats": {
            "total_chunks": len(build_chunks(duration)),
            "failed_chunks": failed_chunks,
            "raw_events": len(raw_events),
            "after_dedup": after_dedup_count,
            "after_filters": len(events),
            "dropped_count": len(dropped),
            "by_type": _count_by_type(events),
        },
```

Add `nsv_id` to each event before writing (used by JS for cross-reference):

```python
    for i, ev in enumerate(events):
        ev["id"] = f"nsv-{i}"
```

- [ ] **Step 9.5: Run all tests + end-to-end check**

```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
python3 -m unittest cut.scripts.test_detect_non_speech_vocals cut.scripts.test_non_speech_vocal_filters -v
```

Expected: all green.

End-to-end:

```bash
python3 cut/scripts/detect_non_speech_vocals_gemini.py output/test_host-ted-5min/cut

jq '.stats' output/test_host-ted-5min/cut/2_analysis/non_speech_vocals.json
```

Expected stats roughly:
- `raw_events`: ~40-50
- `after_dedup`: same as raw_events for this sample
- `after_filters`: ~15-25 (mostly throat_clear + nose_clear; click_smack/sharp_breath/clustered events dropped)
- `by_type`: only `throat_clear` and `nose_clear`

Inspect one event:

```bash
jq '.events[0]' output/test_host-ted-5min/cut/2_analysis/non_speech_vocals.json
```

Should have all the expected fields per spec §5 output schema: `id`, `start`, `end`, `type`, `confidence`, `description`, `zone`, `overlap_ratio`, `prev_word_end`, `next_word_start`, `refined_start`, `refined_end`, `filter_decision`.

- [ ] **Step 9.6: Commit**

```bash
git add cut/scripts/non_speech_vocal_filters.py cut/scripts/detect_non_speech_vocals_gemini.py cut/scripts/test_non_speech_vocal_filters.py
git commit -m "feat(cut): wire NSV filter orchestrator into detector pipeline"
```

---

## Phase C: JS Integration into Fine Analysis

### Task 10: Load + emit NSV edits in `run_fine_analysis.js`

**Files:**
- Modify: `cut/scripts/run_fine_analysis.js`

NSV events flow in as fine edits with `type: 'non_speech_vocal'`. They participate in the existing edit pipeline (sorted by start, indexed, fed to merge/refine/review).

- [ ] **Step 10.1: Read existing run_fine_analysis edit shape**

Read `cut/scripts/run_fine_analysis.js` lines 550-720 to confirm the edit object shape and how `edits` is built. (Already shown in plan recon; see e.g. how `consecutive_filler` edits are emitted.)

The fields used downstream are: `idx`, `sentenceIdx`, `type`, `rule`, `wordRange`, `deleteText`, `keepText`, `deleteStart`, `deleteEnd`, `reason`, `needsReview`, `confidence`.

For NSV we add: `subtype`, `source`, `nsv_id`, `description`, `filter_decision`. Downstream tools should ignore unknown fields.

- [ ] **Step 10.2: Add NSV loading + edit emission**

In `cut/scripts/run_fine_analysis.js`, near the top of the file (after the existing path setup and before `// === Stutter exemption tiers ===`), add:

```javascript
// === Stage 2.5: Non-speech vocal events ===
const nsvPath = path.join(analysisDir, 'non_speech_vocals.json');
let nsvData = { events: [], degraded: true };
if (fs.existsSync(nsvPath)) {
  try {
    nsvData = JSON.parse(fs.readFileSync(nsvPath, 'utf8'));
  } catch (err) {
    console.warn(`⚠️  Failed to parse ${nsvPath}: ${err.message}`);
  }
}

function nsvTypeLabel(subtype) {
  return { throat_clear: '清喉嚨', nose_clear: '清鼻子' }[subtype] || subtype;
}

function findSentenceIdxForTime(time, sentences) {
  // Find first sentence whose [startTime, endTime] contains the time;
  // fallback to nearest by midpoint.
  for (const s of sentences) {
    if (time >= s.startTime && time <= s.endTime) return s.idx;
  }
  // Fallback: nearest sentence by midpoint
  let bestIdx = sentences.length > 0 ? sentences[0].idx : 0;
  let bestDist = Infinity;
  for (const s of sentences) {
    const mid = (s.startTime + s.endTime) / 2;
    const d = Math.abs(mid - time);
    if (d < bestDist) {
      bestDist = d;
      bestIdx = s.idx;
    }
  }
  return bestIdx;
}
```

Find the section near the end where `edits.push(...)` is no longer called and `edits.sort(...)` is. **Before** the line `edits.sort((a, b) => a.deleteStart - b.deleteStart);` add:

```javascript
// === Emit NSV events as fine edits ===
for (const ev of (nsvData.events || [])) {
  // Use refined boundaries if present, else raw
  const dStart = (ev.refined_start ?? ev.start);
  const dEnd = (ev.refined_end ?? ev.end);
  const sentenceIdx = findSentenceIdxForTime(dStart, sentences);

  edits.push({
    idx: editIdx++,
    sentenceIdx,
    type: 'non_speech_vocal',
    subtype: ev.type,                                  // 'throat_clear' | 'nose_clear'
    rule: '11-non-speech-vocal',
    wordRange: null,                                    // NSV is between/near words
    deleteText: '',                                     // no transcript text consumed
    keepText: '',
    deleteStart: parseFloat(dStart.toFixed(2)),
    deleteEnd: parseFloat(dEnd.toFixed(2)),
    reason: `${nsvTypeLabel(ev.type)}（Gemini 偵測）${ev.description ? '：' + ev.description : ''}`,
    needsReview: true,
    enabled: false,                                     // default: don't delete; user confirms
    confidence: ev.confidence,
    source: 'gemini',
    nsv_id: ev.id,
    description: ev.description,
    filter_decision: ev.filter_decision,
  });
}
```

Now also tally NSV in the summary. Find the existing `byType` accumulation:

```javascript
const byType = {};
let needsReviewCount = 0;
for (const e of edits) {
  byType[e.type] = (byType[e.type] || 0) + 1;
  if (e.needsReview) needsReviewCount++;
}
```

No change needed — the existing accumulator already counts `non_speech_vocal` since we use `type: 'non_speech_vocal'`.

- [ ] **Step 10.3: Run the script against the 5-min sample**

```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
node cut/scripts/run_fine_analysis.js \
  --analysis-dir output/test_host-ted-5min/cut/2_analysis
```

Expected: stdout shows `By type: ...non_speech_vocal: N...` with N > 0 (matches NSV events count).

Inspect:
```bash
jq '.edits | map(select(.type == "non_speech_vocal")) | .[0:3]' \
  output/test_host-ted-5min/cut/2_analysis/fine_analysis_rules.json
```

Expected: each entry has subtype, reason in Chinese, `needsReview: true`, `enabled: false`, deleteStart/End populated.

- [ ] **Step 10.4: Commit**

```bash
git add cut/scripts/run_fine_analysis.js
git commit -m "feat(cut): emit Gemini NSV events as fine edits in rules layer"
```

---

### Task 11: Cross-reference Whisper filler words with NSV events

**Files:**
- Modify: `cut/scripts/run_fine_analysis.js`

If a Whisper word matches the filler list AND overlaps an NSV event, override the filler edit (which by default keeps single fillers) to force-delete and link to the NSV.

- [ ] **Step 11.1: Find existing filler-detection logic**

Read the relevant section in `cut/scripts/run_fine_analysis.js`. The leading-filler rule emits an edit with `type: 'single_filler'`. Per spec §7, we need to detect overlap between NSV events and any `single_filler` edit, and if so:
- Mark the filler edit `enabled: true` (override the keep)
- Add `reason: 'filler_overridden_by_nsv:<subtype>'`
- Cross-link via `nsv_id`

- [ ] **Step 11.2: Add the cross-reference pass**

In `cut/scripts/run_fine_analysis.js`, **after** the NSV emission block from Task 10 and **before** `edits.sort(...)`, add:

```javascript
// === Cross-ref: upgrade filler edits that overlap an NSV event ===
const FILLER_WORDS = new Set(['嗯', '啊', '呃', '對', '哎', '欸', '哦', '噢', '額', '唉']);

const nsvIntervals = (nsvData.events || []).map(ev => ({
  start: (ev.refined_start ?? ev.start),
  end: (ev.refined_end ?? ev.end),
  subtype: ev.type,
  id: ev.id,
}));

for (const e of edits) {
  if (e.type !== 'single_filler' && e.type !== 'consecutive_filler') continue;
  if (!FILLER_WORDS.has((e.deleteText || '').trim())) continue;
  // Find any NSV that overlaps this edit's delete range
  const match = nsvIntervals.find(n =>
    n.start < e.deleteEnd && n.end > e.deleteStart
  );
  if (!match) continue;
  e.enabled = true;
  e.needsReview = false;          // override is decisive
  e.nsvOverride = { nsv_id: match.id, subtype: match.subtype };
  e.reason = `${e.reason}（Gemini 偵測到此處實為${nsvTypeLabel(match.subtype)}，自動勾選刪除）`;
}
```

- [ ] **Step 11.3: Validate the override against a known case**

For the host-ted-5min sample, position 23.34s the user marked OK throat_clear and Whisper transcribed "嗯" right after at 23.96s. The NSV event near 23.34 should NOT overlap the "今天" word (24.32s start) but may overlap a filler if one was emitted.

Run:
```bash
node cut/scripts/run_fine_analysis.js \
  --analysis-dir output/test_host-ted-5min/cut/2_analysis

jq '.edits | map(select(.nsvOverride != null)) | .[0:5]' \
  output/test_host-ted-5min/cut/2_analysis/fine_analysis_rules.json
```

Expected: zero or more entries. **The key is the script must not crash even when there are no overlaps.** If the user audio has filler-NSV overlaps, we get an entry; otherwise empty array — both acceptable.

- [ ] **Step 11.4: Commit**

```bash
git add cut/scripts/run_fine_analysis.js
git commit -m "feat(cut): cross-ref NSV events with filler edits to override keep"
```

---

## Phase D: Review UI

### Task 12: Add "非語音聲響" section to review HTML

**Files:**
- Modify: `cut/scripts/generate_review_enhanced.js`
- Modify: `cut/templates/review_enhanced.html`

Render NSV edits in a dedicated collapsible section in the review HTML.

- [ ] **Step 12.1: Pass NSV edits separately to template**

In `cut/scripts/generate_review_enhanced.js`, find where the fine_analysis edits are loaded and template data assembled. Locate the data-passing object (something like `const templateData = { ... }` or similar near the end of the file).

Add a section that filters the fine edits for `type === 'non_speech_vocal'` and exposes them as `nsvEdits`:

Insert near where other template data is built (before the template string-replace / render step). Search the file for a pattern like `fineEdits` being passed to the template:

```javascript
// === Build NSV section payload ===
const nsvEdits = fineAnalysis.edits
  .filter(e => e.type === 'non_speech_vocal')
  .map(e => ({
    feIdx: e.idx,
    nsv_id: e.nsv_id,
    subtype: e.subtype,
    subtypeLabel: ({ throat_clear: '清喉嚨', nose_clear: '清鼻子' }[e.subtype] || e.subtype),
    start: e.deleteStart,
    end: e.deleteEnd,
    description: e.description || '',
    confidence: e.confidence,
    filter_decision: e.filter_decision,
    enabled: e.enabled,
  }));
```

Then add `nsvEdits` to whatever data object is passed to the template. If the template uses string replacement (e.g. `__DATA__` placeholder), add a new placeholder `__NSV_EDITS__` and replace with `JSON.stringify(nsvEdits)`.

- [ ] **Step 12.2: Add the section to the HTML template**

In `cut/templates/review_enhanced.html`, the template needs a new section. Find a good insertion point (typically after the silence section, before the export/footer).

Add this HTML block (place near a visually appropriate spot — search for a comment like `<!-- silence section -->` or look for where other edit-type sections render):

```html
<!-- Non-speech vocal section -->
<section class="nsv-section">
  <h2>非語音聲響（Gemini 偵測）</h2>
  <p class="nsv-help">主持人句前/句尾的清喉嚨、清鼻子聲。逐筆確認是否刪除。</p>
  <div id="nsv-list"></div>
</section>
```

And add this script block to render it (place inside the existing main `<script>` after the audio player is initialised):

```javascript
const nsvEdits = __NSV_EDITS__;  // injected by generate_review_enhanced.js

function renderNsvList() {
  const root = document.getElementById('nsv-list');
  if (!root) return;
  root.replaceChildren();
  if (!nsvEdits || nsvEdits.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'nsv-empty';
    empty.textContent = '此集沒有偵測到清喉嚨/清鼻子事件。';
    root.appendChild(empty);
    return;
  }
  for (const nsv of nsvEdits) {
    const card = document.createElement('div');
    card.className = 'nsv-card';
    card.dataset.feIdx = nsv.feIdx;

    const head = document.createElement('div');
    head.className = 'nsv-head';
    const t = document.createElement('span');
    t.className = 'nsv-time';
    t.textContent = nsv.start.toFixed(2) + '-' + nsv.end.toFixed(2) + 's';
    head.appendChild(t);
    const lbl = document.createElement('span');
    lbl.className = 'nsv-type type-' + nsv.subtype;
    lbl.textContent = nsv.subtypeLabel;
    head.appendChild(lbl);
    const conf = document.createElement('span');
    conf.className = 'nsv-conf';
    conf.textContent = 'conf ' + nsv.confidence.toFixed(2);
    head.appendChild(conf);
    if (nsv.filter_decision === 'boundary_too_tight') {
      const w = document.createElement('span');
      w.className = 'nsv-warning';
      w.textContent = '⚠ 邊界過緊，剪輯易產生 artifact';
      head.appendChild(w);
    }
    card.appendChild(head);

    if (nsv.description) {
      const d = document.createElement('p');
      d.className = 'nsv-desc';
      d.textContent = nsv.description;
      card.appendChild(d);
    }

    const actions = document.createElement('div');
    actions.className = 'nsv-actions';
    const play = document.createElement('button');
    play.textContent = '▶ 試聽';
    play.addEventListener('click', () => playClip(nsv.start, nsv.end));
    actions.appendChild(play);
    const confirm = document.createElement('button');
    confirm.className = 'btn-confirm';
    confirm.textContent = '✅ 確認刪除';
    confirm.addEventListener('click', () => setNsvDecision(nsv.feIdx, true));
    actions.appendChild(confirm);
    const keep = document.createElement('button');
    keep.className = 'btn-keep';
    keep.textContent = '❌ 保留';
    keep.addEventListener('click', () => setNsvDecision(nsv.feIdx, false));
    actions.appendChild(keep);
    card.appendChild(actions);

    root.appendChild(card);
  }
}

function setNsvDecision(feIdx, enable) {
  // Use the existing fineEditsDisabled set: enabled=true means it's NOT disabled
  if (enable) {
    fineEditsDisabled.delete(feIdx);
  } else {
    fineEditsDisabled.add(feIdx);
  }
  // Visual feedback
  const card = document.querySelector('.nsv-card[data-fe-idx="' + feIdx + '"]');
  if (card) {
    card.classList.toggle('nsv-confirmed', enable);
    card.classList.toggle('nsv-kept', !enable);
  }
  saveStateToLocalStorage && saveStateToLocalStorage();
}

function playClip(start, end) {
  const audio = document.getElementById('player');
  if (!audio) return;
  audio.currentTime = Math.max(0, start - 0.6);
  audio.play();
  const stopAt = end + 0.6;
  const onTime = () => {
    if (audio.currentTime >= stopAt) {
      audio.pause();
      audio.removeEventListener('timeupdate', onTime);
    }
  };
  audio.addEventListener('timeupdate', onTime);
}

// Initialise on load
renderNsvList();
```

Add minimal CSS in the template's `<style>` block:

```css
.nsv-section { margin: 24px 0; padding: 16px; background: #1a1f28; border-radius: 8px; }
.nsv-section h2 { font-size: 16px; margin: 0 0 8px; }
.nsv-help { color: #9aa3ad; font-size: 13px; margin: 0 0 12px; }
.nsv-empty { color: #9aa3ad; font-style: italic; }
.nsv-card { border: 1px solid #2f3742; padding: 10px 12px; margin: 8px 0; border-radius: 6px; }
.nsv-card.nsv-confirmed { background: rgba(111, 207, 151, 0.1); border-color: #6fcf97; }
.nsv-card.nsv-kept { background: rgba(235, 87, 87, 0.08); border-color: #eb5757; }
.nsv-head { display: flex; gap: 12px; align-items: center; font-size: 13px; }
.nsv-time { font-family: ui-monospace, monospace; }
.nsv-type.type-throat_clear { color: #eb5757; font-weight: 600; }
.nsv-type.type-nose_clear { color: #f2c94c; font-weight: 600; }
.nsv-warning { color: #f2c94c; font-size: 12px; }
.nsv-desc { color: #cdd5dc; font-size: 13px; margin: 6px 0; }
.nsv-actions button { margin-right: 6px; padding: 4px 10px; }
.btn-confirm { border-color: #6fcf97; }
.btn-keep { border-color: #eb5757; }
```

- [ ] **Step 12.3: Generate the review HTML**

```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
node cut/scripts/generate_review_enhanced.js \
  --analysis-dir output/test_host-ted-5min/cut/2_analysis \
  --audio output/test_host-ted-5min/cut/1_transcript/audio_seekable.mp3
```

(If the CLI args differ from what your version of `generate_review_enhanced.js` accepts, adapt — but the typical invocation comes from the SKILL.md commands.)

Open the generated review HTML and verify the new section appears with the NSV events:

```bash
open output/test_host-ted-5min/cut/review_enhanced.html
```

Manually verify:
1. "非語音聲響" section visible
2. Each event has time, label (清喉嚨/清鼻子), confidence, description
3. ▶ 試聽 plays from 0.6s before to 0.6s after
4. ✅ / ❌ buttons toggle the card style and (silently) flip `fineEditsDisabled`

- [ ] **Step 12.4: Commit**

```bash
git add cut/scripts/generate_review_enhanced.js cut/templates/review_enhanced.html
git commit -m "feat(cut): add 非語音聲響 section to review HTML"
```

---

### Task 13: Feedback writeback to user-prefs

**Files:**
- Modify: `cut/templates/review_enhanced.html`
- Modify: `cut/scripts/generate_review_enhanced.js` (export script that captures user actions)

The existing template has an export flow that writes user decisions to `user_corrections.json` and propagates to user-prefs on Stage 4 export. NSV needs to participate.

- [ ] **Step 13.1: Find existing export logic**

In `cut/templates/review_enhanced.html`, search for the export button handler — typically a function that builds `user_corrections` and offers download. Note its structure.

- [ ] **Step 13.2: Add NSV-specific records to export**

In `cut/templates/review_enhanced.html`, locate the export handler (search for `user_corrections` or `download` / `Blob` patterns). In the function that builds the corrections payload, ADD an `nsv_decisions` array:

```javascript
// Inside the existing export-builder function, after building the rest of the payload:
const nsvDecisions = nsvEdits.map(nsv => ({
  nsv_id: nsv.nsv_id,
  subtype: nsv.subtype,
  start: nsv.start,
  end: nsv.end,
  confidence: nsv.confidence,
  description: nsv.description,
  filter_decision: nsv.filter_decision,
  user_decision: fineEditsDisabled.has(nsv.feIdx) ? 'reject' : 'confirm',
  timestamp: new Date().toISOString(),
}));
payload.nsv_decisions = nsvDecisions;  // attach to the object that becomes user_corrections.json
```

(Adapt variable names to match the actual export function — the key is `payload` is whatever object is serialized.)

- [ ] **Step 13.3: Add a Node helper to fan decisions out to user-prefs feedback file**

Create `cut/scripts/capture_nsv_feedback.js`:

```javascript
#!/usr/bin/env node
/**
 * Capture NSV decisions from user_corrections.json into the user's feedback log.
 *
 * Usage: node capture_nsv_feedback.js <analysisDir> [--user <userId>]
 *
 * Reads:   {analysisDir}/../3_output/user_corrections.json
 * Writes:  cut/user-prefs/{userId}/non_speech_vocal_feedback.jsonl  (append)
 */
const fs = require('fs');
const path = require('path');

const SCRIPT_DIR = __dirname;
const SKILL_DIR = path.resolve(SCRIPT_DIR, '..');

let analysisDir = process.argv[2];
let userId = process.env.PODCAST_EDIT_USER || 'default';
const uIdx = process.argv.indexOf('--user');
if (uIdx > 0 && process.argv[uIdx + 1]) userId = process.argv[uIdx + 1];
if (!analysisDir) {
  console.error('Usage: node capture_nsv_feedback.js <analysisDir> [--user <userId>]');
  process.exit(1);
}

const correctionsPath = path.resolve(analysisDir, '..', '3_output', 'user_corrections.json');
if (!fs.existsSync(correctionsPath)) {
  console.error('No user_corrections.json — nothing to capture.');
  process.exit(0);
}

const corrections = JSON.parse(fs.readFileSync(correctionsPath, 'utf8'));
const nsv = corrections.nsv_decisions || [];

if (nsv.length === 0) {
  console.log('No NSV decisions in user_corrections.json — nothing to log.');
  process.exit(0);
}

const userDir = path.join(SKILL_DIR, 'user-prefs', userId);
if (!fs.existsSync(userDir)) {
  console.error(`User dir ${userDir} does not exist. Create user first via user_manager.js.`);
  process.exit(2);
}

const logPath = path.join(userDir, 'non_speech_vocal_feedback.jsonl');
const out = fs.createWriteStream(logPath, { flags: 'a' });
for (const dec of nsv) {
  out.write(JSON.stringify(dec) + '\n');
}
out.end();

console.log(`✅ appended ${nsv.length} NSV decisions to ${logPath}`);
```

Make it executable:

```bash
chmod +x /Users/kaiwei/side-projects/podcast-edit-skill/cut/scripts/capture_nsv_feedback.js
```

- [ ] **Step 13.4: Validate**

Generate review HTML (as in Task 12), open it, click ✅ on 2-3 NSV cards, click ❌ on 1-2, click the export button, save the downloaded `user_corrections.json` into `output/test_host-ted-5min/cut/3_output/`.

Then run:

```bash
node cut/scripts/capture_nsv_feedback.js \
  output/test_host-ted-5min/cut/2_analysis --user default

tail -5 cut/user-prefs/default/non_speech_vocal_feedback.jsonl
```

Expected: the file has 1 line per NSV decision the user clicked, each a JSON object with `nsv_id`, `subtype`, `user_decision`, `timestamp`, etc.

- [ ] **Step 13.5: Commit**

```bash
git add cut/scripts/capture_nsv_feedback.js cut/templates/review_enhanced.html cut/scripts/generate_review_enhanced.js
git commit -m "feat(cut): capture NSV review decisions into user-prefs feedback log"
```

---

## Phase E: User Preferences

### Task 14: Add NSV section to preferences + CLI

**Files:**
- Modify: `cut/scripts/user_manager.js`
- Modify: any existing user `preferences.yaml` files (default + existing users) — Step 14.3

- [ ] **Step 14.1: Add NSV defaults to the user-creation template**

In `cut/scripts/user_manager.js`, find the `createUser(userId)` function (around line 124). Read the function to find where the default `preferences.yaml` content is built. Add the NSV section to those defaults.

Look for a default preferences object literal or YAML string in `createUser`. Add this section (matching the existing indentation / style):

```yaml
non_speech_vocal:
  enabled: true
  detect_types:
    - throat_clear
    - nose_clear
  confidence_threshold: 0.4
  auto_delete_threshold: 0.99
  default_action: review
```

Match whatever pattern the existing function uses (whether it builds a JS object then yaml.dump, or interpolates a heredoc). Either way, the goal is `preferences.yaml` for a new user has this section.

- [ ] **Step 14.2: Add `prefs` CLI support for nested keys**

Read the existing `prefs` CLI command in `user_manager.js`. If it already supports nested-key dot syntax (`non_speech_vocal.enabled`), no change. If it only supports flat keys, extend it:

Search the file for `case 'prefs':` or `command === 'prefs'`. The existing pattern likely uses something like `prefs[key] = value`. Replace it with a helper that walks dots:

```javascript
function setNestedKey(obj, dotPath, value) {
  const parts = dotPath.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] === undefined || typeof cur[parts[i]] !== 'object') {
      cur[parts[i]] = {};
    }
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

function getNestedKey(obj, dotPath) {
  return dotPath.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
```

Use these in `prefs set <userId> <key> <value>` and `prefs get <userId> <key>` cases.

- [ ] **Step 14.3: Migrate existing user-prefs files**

For each existing user (look in `cut/user-prefs/*`), append the new section if not already present. List existing:

```bash
ls cut/user-prefs
```

For each user (e.g. `default`, `steven`), open the file and append the YAML section from Step 14.1 manually if missing. (Programmatic migration would require js-yaml round-trip and risks losing comments — manual append is safer.)

Verify via:

```bash
cat cut/user-prefs/steven/preferences.yaml
```

Expected: file ends with the new `non_speech_vocal:` block.

- [ ] **Step 14.4: Wire prefs into the detector**

(Defer to Task 17 — the integration script will read these prefs and pass `detect_types` etc. as args. For now, ensure the prefs file is loadable.)

Sanity check:

```bash
node cut/scripts/user_manager.js prefs default get non_speech_vocal.enabled
```

Expected output: `true`.

- [ ] **Step 14.5: Commit**

```bash
git add cut/scripts/user_manager.js cut/user-prefs/
git commit -m "feat(cut): add non_speech_vocal section to user preferences"
```

---

## Phase F: Documentation, Validation, Integration

### Task 15: Editing-rule documentation

**Files:**
- Create: `cut/editing-rules/11-non-speech-vocal.md`
- Modify: `cut/editing-rules/README.md`

- [ ] **Step 15.1: Create the rule doc**

Create `cut/editing-rules/11-non-speech-vocal.md`:

```markdown
<!--
input: 1_transcript/audio.mp3 + 1_transcript/subtitles_words.json
output: 2_analysis/non_speech_vocals.json → fine_analysis edits with type=non_speech_vocal
pos: rule, human-confirmation priority (always needs review unless user prefs override)

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
2. cut/SKILL.md Stage 2.5 section
3. CLAUDE.md commands + env var section
-->

# Non-Speech Vocal Detection (清喉嚨 / 清鼻子)

## Detection target

主持人習慣性在句前或句尾發出短暫的非語音聲響：
- **清喉嚨** (throat clearing) — 短暫沙啞、濕潤的摩擦聲
- **清鼻子** (nose clearing / sniff / snort) — 短暫鼻音

非偵測對象（依 spike 結果排除）：
- 自然呼吸、句前準備吸氣
- Filler "嗯/啊/呃/對"（既有 filler 規則處理）
- 講話過程中的脣齒摩擦
- 笑聲、嘆息（情緒性，刻意保留）
- 主持人**模仿**或**描述**清喉嚨的聲音當作對話內容

## Detection mechanism

外部呼叫 `cut/scripts/detect_non_speech_vocals_gemini.py`，使用 Gemini-2.5-flash 多模態 LLM 全程掃描音檔 30s chunks。為什麼選 Gemini，見：
`docs/superpowers/specs/2026-05-18-non-speech-vocal-detection-design.md` §2 Spike Findings.

決定不採用的方案：YAMNet (Recall 0%)、Whisper non-speech tokens (中文無此標註)、SenseVoice / FunASR (segment-level dominant-class bias)、Yating ASR-only。

## Post-detection filters

四個工程性 filter（`non_speech_vocal_filters.py`），按序執行：

1. **Host describing sound**：若事件 ±2s Whisper 文字內出現「口口口」、「聽起來像」、「清喉嚨」、「咳嗽」等模仿/描述語，移除事件
2. **Long-gap subsume**：若事件位於 > 3s 的 word gap 中，移除（會由既有 silence trim 處理）
3. **Speech artifact cluster**：同類型事件在 2s 內連發 ≥ 3 個視為自然口腔摩擦聲，整組移除
4. **Boundary too tight**：事件長度 < 0.15s 或與下個字距離 < 0.1s 時，標記但保留（soft-keep）—— UI 顯示警告

## Output format

每個事件最終以 fine edit 形式進入 `fine_analysis_rules.json`：
```json
{
  "idx": 12,
  "type": "non_speech_vocal",
  "subtype": "throat_clear",
  "rule": "11-non-speech-vocal",
  "deleteStart": 26.45,
  "deleteEnd": 26.85,
  "reason": "清喉嚨（Gemini 偵測）：句首濕潤的清喉嚨聲",
  "needsReview": true,
  "enabled": false,
  "confidence": 0.95,
  "source": "gemini",
  "nsv_id": "nsv-0",
  "filter_decision": "ok"
}
```

## Cross-reference with filler rules

若 NSV 事件位置剛好有 Whisper 標的「嗯/啊/呃/對」filler word：
- 既有 filler 規則「預設保留單字 filler」被覆寫
- Filler edit 升級為 `enabled: true`、`needsReview: false`
- `reason` 後綴加上「Gemini 偵測到此處實為 X，自動勾選刪除」

## User preference

`cut/user-prefs/<userId>/preferences.yaml`:
```yaml
non_speech_vocal:
  enabled: true                # 主開關（false → 完全跳過偵測）
  detect_types:                # 啟用類別
    - throat_clear
    - nose_clear
  confidence_threshold: 0.4    # 低於此值不顯示
  auto_delete_threshold: 0.99  # ≥ 此值才 auto-enable（預設等同不 auto）
  default_action: review       # review | delete | ignore
```

## Feedback loop

每次 review 後使用者按 ✅/❌ 的決策寫入 `cut/user-prefs/<userId>/non_speech_vocal_feedback.jsonl`。累積 ≥ 100 筆後可考慮訓練 user-specific classifier 作為後置 filter（v2，另文 spec）。
```

- [ ] **Step 15.2: Update editing-rules README**

Open `cut/editing-rules/README.md`. Append to the rule list:

```markdown
11. **Non-speech vocal** → Gemini 偵測 + 4 filter + 強制 needsReview（清喉嚨/清鼻子）
```

- [ ] **Step 15.3: Commit**

```bash
git add cut/editing-rules/11-non-speech-vocal.md cut/editing-rules/README.md
git commit -m "docs(cut): add editing-rule 11 (non-speech vocal detection)"
```

---

### Task 16: Update top-level docs

**Files:**
- Modify: `cut/SKILL.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 16.1: Update `cut/SKILL.md`**

Find the Stage 2 description and the pipeline overview. After "Stage 2 — Transcribe → 句子切分 → AI rough cut → AI fine cut" insert a Stage 2.5 step. Find a good insertion point and add:

```markdown
### Stage 2.5 — Non-speech vocal detection (Gemini)

**When**: After transcription (`subtitles_words.json` exists), before `run_fine_analysis.js`.

**Command**:
```bash
python3 cut/scripts/detect_non_speech_vocals_gemini.py <BASE_DIR>
```

Detects 清喉嚨 / 清鼻子 events that the filler / silence rules cannot see.
Writes `2_analysis/non_speech_vocals.json`.

**Requires**: `GEMINI_API_KEY` set in `.env` at repo root. If missing, the
stage emits an empty `non_speech_vocals.json` with `degraded: true` and the
rest of the pipeline continues.

See `cut/editing-rules/11-non-speech-vocal.md` for detection logic and filter
behaviour. See `docs/superpowers/specs/2026-05-18-non-speech-vocal-detection-design.md`
for why this is a separate Stage (not part of fine-cut rules).
```

- [ ] **Step 16.2: Update `CLAUDE.md`**

Find the "Commands you'll actually run" section. Add the NSV detection command after the transcription block:

```bash
# Stage 2.5 — Non-speech vocal detection (requires GEMINI_API_KEY)
python3 cut/scripts/detect_non_speech_vocals_gemini.py <BASE_DIR>
```

Find the "Optional environment variables (`.env`)" section. Update the `GEMINI_API_KEY` line:

Before:
```
- `GEMINI_API_KEY` — enables QA Phase B Layer 2 (AI listening via `gemini-2.5-flash`). Without it, signal-layer analysis still runs.
```

After:
```
- `GEMINI_API_KEY` — required for non-speech vocal detection (Stage 2.5, `cut/`) and QA Phase B Layer 2 (`qa/`). Without it, both stages skip gracefully and the rest of the pipeline continues.
```

Find the "Critical gotchas" section. Add a new bullet:

```markdown
- **Non-speech vocal detection is opt-out via API key absence.** If `GEMINI_API_KEY` is unset, Stage 2.5 emits an empty `non_speech_vocals.json` with `degraded: true` and the pipeline continues. Same for ≥2 consecutive Gemini failures. Never blocks the pipeline.
```

- [ ] **Step 16.3: Update `README.md`**

The README has a skill table — add a line mentioning the new Stage 2.5 detection, but keep it brief. Find the line that mentions cut-skill stages and add:

```
- Stage 2.5: Non-speech vocal detection (清喉嚨/清鼻子, requires GEMINI_API_KEY) — `cut/scripts/detect_non_speech_vocals_gemini.py`
```

(Adapt to the README's existing list style.)

- [ ] **Step 16.4: Commit**

```bash
git add cut/SKILL.md CLAUDE.md README.md
git commit -m "docs: document Stage 2.5 non-speech vocal detection"
```

---

### Task 17: Validation script + end-to-end run

**Files:**
- Create: `cut/scripts/validate_nsv_pipeline.sh`

End-to-end validation script that exercises detection, fine-analysis integration, review HTML, and failure cases.

- [ ] **Step 17.1: Create validation script**

Create `cut/scripts/validate_nsv_pipeline.sh`:

```bash
#!/usr/bin/env bash
# Validate the NSV detection pipeline end-to-end against host-ted-5min.wav.
#
# Usage:
#   bash cut/scripts/validate_nsv_pipeline.sh
#
# Exits non-zero on any failure. Stdout summarises each check.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BASE_DIR="$REPO/output/test_host-ted-5min/cut"
RESULT_OK=0
RESULT_FAIL=0

ok()   { echo "✅ $1"; RESULT_OK=$((RESULT_OK + 1)); }
fail() { echo "❌ $1"; RESULT_FAIL=$((RESULT_FAIL + 1)); }

# --- 1. Detector script exists and runs ---
if [[ ! -f "$REPO/cut/scripts/detect_non_speech_vocals_gemini.py" ]]; then
  fail "detect_non_speech_vocals_gemini.py missing"
  exit 1
fi
ok "detector script present"

# --- 2. Run detector ---
echo ""
echo "→ Running detector on host-ted-5min sample..."
python3 "$REPO/cut/scripts/detect_non_speech_vocals_gemini.py" "$BASE_DIR"
if [[ $? -ne 0 ]]; then
  fail "detector exited non-zero"
  exit 1
fi
ok "detector ran successfully"

# --- 3. Output schema check ---
NSV_JSON="$BASE_DIR/2_analysis/non_speech_vocals.json"
if [[ ! -f "$NSV_JSON" ]]; then
  fail "$NSV_JSON not produced"
  exit 1
fi

REQUIRED_TOP_KEYS=(audio duration model generated_at degraded stats events)
for k in "${REQUIRED_TOP_KEYS[@]}"; do
  if ! jq -e "has(\"$k\")" "$NSV_JSON" > /dev/null; then
    fail "top-level key '$k' missing"
  fi
done

REQUIRED_STAT_KEYS=(total_chunks failed_chunks raw_events after_dedup after_filters by_type)
for k in "${REQUIRED_STAT_KEYS[@]}"; do
  if ! jq -e ".stats | has(\"$k\")" "$NSV_JSON" > /dev/null; then
    fail "stats.$k missing"
  fi
done
ok "schema check passed"

# --- 4. Each event has required fields ---
N_EVENTS=$(jq '.events | length' "$NSV_JSON")
echo "→ events emitted: $N_EVENTS"
if [[ "$N_EVENTS" -ge 10 ]]; then
  ok "≥10 events emitted (acceptance criterion #1)"
else
  fail "only $N_EVENTS events emitted (need ≥10)"
fi

REQUIRED_EVENT_KEYS=(id start end type confidence zone refined_start refined_end filter_decision)
MISSING=$(jq -r ".events[] | [.id, .start, .type, .refined_start, .filter_decision] | join(\" \")" "$NSV_JSON" | head -1)
echo "  sample event: $MISSING"

# --- 5. Only throat_clear / nose_clear remain after filters ---
ALLOWED=$(jq -r '.events | map(.type) | unique | sort | tostring' "$NSV_JSON")
EXPECTED='["nose_clear","throat_clear"]'
if [[ "$ALLOWED" == "$EXPECTED" ]] || [[ "$ALLOWED" == '["nose_clear"]' ]] || [[ "$ALLOWED" == '["throat_clear"]' ]] || [[ "$ALLOWED" == '[]' ]]; then
  ok "filter removed click_smack/sharp_breath"
else
  fail "unexpected event types in output: $ALLOWED"
fi

# --- 6. Fine-analysis integration ---
echo ""
echo "→ Running run_fine_analysis.js to integrate NSV events..."
node "$REPO/cut/scripts/run_fine_analysis.js" --analysis-dir "$BASE_DIR/2_analysis"
if [[ $? -ne 0 ]]; then
  fail "run_fine_analysis.js failed"
  exit 1
fi
ok "fine analysis integrated NSV events"

NSV_EDITS=$(jq '.edits | map(select(.type == "non_speech_vocal")) | length' "$BASE_DIR/2_analysis/fine_analysis_rules.json")
if [[ "$NSV_EDITS" -ge 1 ]]; then
  ok "$NSV_EDITS NSV edits emitted in fine_analysis_rules.json"
else
  fail "no non_speech_vocal edits in fine_analysis_rules.json"
fi

# --- 7. Graceful failure when GEMINI_API_KEY missing ---
echo ""
echo "→ Testing graceful failure on missing GEMINI_API_KEY..."
TMP_BASE=$(mktemp -d)
mkdir -p "$TMP_BASE/1_transcript" "$TMP_BASE/2_analysis"
cp "$BASE_DIR/1_transcript/audio.mp3" "$TMP_BASE/1_transcript/audio.mp3"
cp "$BASE_DIR/1_transcript/subtitles_words.json" "$TMP_BASE/1_transcript/subtitles_words.json"
(
  unset GEMINI_API_KEY
  # Temporarily move .env so it's not found
  if [[ -f "$REPO/.env" ]]; then mv "$REPO/.env" "$REPO/.env.tmp"; fi
  python3 "$REPO/cut/scripts/detect_non_speech_vocals_gemini.py" "$TMP_BASE"
  RC=$?
  if [[ -f "$REPO/.env.tmp" ]]; then mv "$REPO/.env.tmp" "$REPO/.env"; fi
  exit $RC
)
if [[ $? -eq 0 ]] && jq -e '.degraded == true' "$TMP_BASE/2_analysis/non_speech_vocals.json" > /dev/null; then
  ok "graceful failure: degraded=true on missing API key"
else
  fail "expected graceful exit + degraded=true on missing API key"
fi
rm -rf "$TMP_BASE"

# --- Summary ---
echo ""
echo "============================================================"
echo "RESULTS: $RESULT_OK pass, $RESULT_FAIL fail"
echo "============================================================"
if [[ $RESULT_FAIL -gt 0 ]]; then
  exit 1
fi
```

Make executable:

```bash
chmod +x /Users/kaiwei/side-projects/podcast-edit-skill/cut/scripts/validate_nsv_pipeline.sh
```

- [ ] **Step 17.2: Run validation**

```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
bash cut/scripts/validate_nsv_pipeline.sh
```

Expected output: ends with `RESULTS: 8 pass, 0 fail` (or close — exact numbers may differ by a count or two).

If anything fails, investigate. Common issues:
- `jq` not installed → `brew install jq`
- API quota / rate limit → wait + retry
- Stage 2.5 output schema drift → fix `detect_non_speech_vocals_gemini.py`

- [ ] **Step 17.3: User acceptance test (manual)**

Open the review HTML:

```bash
open output/test_host-ted-5min/cut/review_enhanced.html
```

Manually verify against spec §12 acceptance criteria:
- [ ] "非語音聲響" section visible
- [ ] ≥10 events listed
- [ ] Each event has 試聽 / ✅ / ❌ buttons
- [ ] 試聽 plays from 0.6s before to 0.6s after
- [ ] ✅ / ❌ toggle the card style
- [ ] Filter `boundary_too_tight` events show the ⚠ warning
- [ ] Click some ✅ and ❌, export, then run `node cut/scripts/capture_nsv_feedback.js ...` and verify `non_speech_vocal_feedback.jsonl` populated

Compare precision against spike data:
- Spike raw Gemini: 11/19 = 58% on throat+nose
- After filters target: ≥70% precision

To measure, the user should ✅/❌ the events in the rendered review HTML, then count `confirm` vs `reject` decisions in `non_speech_vocal_feedback.jsonl`. **Don't gate the commit on this — it's a metric for the user, not for CI**.

- [ ] **Step 17.4: Commit**

```bash
git add cut/scripts/validate_nsv_pipeline.sh
git commit -m "feat(cut): validate_nsv_pipeline.sh end-to-end + degraded-mode check"
```

---

## Phase G: Final Self-Review

### Task 18: Sweep and check

- [ ] **Step 18.1: Cleanup spike artifacts (optional)**

The spike produced files in `output/test_host-ted-5min/cut/`:

```bash
ls output/test_host-ted-5min/cut/spike_*.html \
   output/test_host-ted-5min/cut/2_analysis/spike_*.json \
   output/test_host-ted-5min/cut/2_analysis/nonverbal_events*.json \
   output/test_host-ted-5min/cut/1_transcript/whisper_no_vad.json 2>/dev/null
```

These are referenced in the spec §17 as artifacts. The user may want to keep them for reference. **Don't delete unless explicitly asked** — they're useful for future tuning.

- [ ] **Step 18.2: Run full test suite**

```bash
cd /Users/kaiwei/side-projects/podcast-edit-skill
python3 -m unittest \
  cut.scripts.test_detect_non_speech_vocals \
  cut.scripts.test_non_speech_vocal_filters \
  -v
```

Expected: all green.

- [ ] **Step 18.3: Smoke test the whole `cut` pipeline ↓**

(Assumes a recent episode has been transcribed already.) Run the relevant Stage 2.5 + 3-5 commands per `cut/SKILL.md` and confirm the review HTML opens correctly with the NSV section, and `fine_analysis_rules.json` contains the new edit type.

- [ ] **Step 18.4: Verify spec coverage**

Open `docs/superpowers/specs/2026-05-18-non-speech-vocal-detection-design.md` and check section by section that every implementation requirement has a matching task:

- §4 Pipeline Integration → Tasks 2, 10
- §5 The New Script → Tasks 2-6
- §6 Filters → Tasks 7-9
- §7 run_fine_analysis integration → Tasks 10, 11
- §8 Review UI → Tasks 12, 13
- §9 User Preferences → Task 14
- §10 Docs Updates → Tasks 15, 16
- §11 Cost / Failure handling → Task 2 (failure logic), Task 17 (degraded mode test)
- §12 Validation Plan → Task 17

If any spec section has no matching task, **add a task to this plan and document**.

- [ ] **Step 18.5: Final commit (only if any cleanup happened)**

If Step 18.1 deleted anything or Step 18.4 added tasks:

```bash
git status
git add -A
git commit -m "chore(cut): cleanup after NSV pipeline implementation"
```

---

## Done Criteria

Implementation is complete when:
1. All 18 tasks above are checked off
2. `bash cut/scripts/validate_nsv_pipeline.sh` exits 0
3. `python3 -m unittest cut.scripts.test_detect_non_speech_vocals cut.scripts.test_non_speech_vocal_filters` exits 0
4. The user has manually run a real episode through the pipeline and confirmed the review HTML shows the new section with sensible content
5. `non_speech_vocal_feedback.jsonl` for at least one user contains real decisions

After all done, transition to Phase 2 of the spec (which is **out of scope for this plan**): accumulate feedback data, evaluate user-specific classifier training, consider GPT-4o-audio ensemble. Open a new spec then.

---

## Spec Coverage Summary

| Spec § | What | Tasks |
|---|---|---|
| §1 Background | (context, no implementation) | — |
| §2 Spike Findings | (context, no implementation) | — |
| §3 Architecture Decision | (context, no implementation) | — |
| §4 Pipeline Integration | New script slot + JSON files | 2, 10 |
| §5 The New Script | Detector with chunking/dedup/refinement | 2-6 |
| §6 Post-Detection Filters | 4 filters | 7-9 |
| §7 run_fine_analysis Integration | Load + emit + filler override | 10, 11 |
| §8 Review UI Changes | "非語音聲響" section + feedback writeback | 12, 13 |
| §9 User Preferences Schema | Defaults + CLI + migration | 14 |
| §10 Stage Documentation Updates | SKILL.md / CLAUDE.md / rule 11 / README | 15, 16 |
| §11 Cost & Operations | Failure handling baked into Task 2; validation in 17 | 2, 17 |
| §12 Validation Plan | validate_nsv_pipeline.sh | 17 |
| §13 Out of Scope | (explicit non-goals; no tasks) | — |
| §14 Open Questions | (resolved via spec's suggestions in §14) | — |
| §15 Risk Register | (mitigations in Task 2's failure handling + Task 17's tests) | 2, 17 |
| §16 Implementation Phases | (this plan = Phase 1; Phase 2 out of scope) | — |
| §17 Spike Artifacts | (preserved by default; cleanup optional in Task 18) | 18 |
