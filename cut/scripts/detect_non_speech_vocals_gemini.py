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
