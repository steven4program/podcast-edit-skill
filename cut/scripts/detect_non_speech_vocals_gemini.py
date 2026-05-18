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

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from non_speech_vocal_filters import apply_all_filters

CHUNK_SEC = 30
HOP_SEC = 25
MODEL = "gemini-2.5-flash"
MAX_RETRIES = 3

CONF_MAP = {
    "very high": 0.95, "high": 0.85, "medium": 0.6,
    "low": 0.4, "very low": 0.2,
}

NSV_PREF_DEFAULTS = {
    "enabled": True,
    "detect_types": ["throat_clear", "nose_clear"],
    "confidence_threshold": 0.4,
    "auto_delete_threshold": 0.99,
    "default_action": "review",
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
    """Load GEMINI_API_KEY from env or .env at repo root.

    Set NSV_SKIP_DOTENV=1 to disable the .env-walk fallback (used by the
    validation script to test the missing-key path without moving .env).
    """
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    if os.environ.get("NSV_SKIP_DOTENV") == "1":
        raise RuntimeError(
            "GEMINI_API_KEY not set and NSV_SKIP_DOTENV=1 (forcing degraded mode)."
        )
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


def load_user_prefs(user_id: str) -> dict:
    """Load non_speech_vocal preferences for a user, merged with defaults.

    Looks up `cut/user-prefs/<userId>/preferences.yaml` relative to this script.
    Missing file or missing section → defaults. Any key the user did set wins.

    PyYAML is required (listed in cut/scripts/requirements.txt). We intentionally
    let ImportError propagate rather than silently falling back to defaults —
    silent fallback was a bug that masked unset prefs as "fine, using defaults".
    """
    prefs_path = Path(__file__).resolve().parent.parent / "user-prefs" / user_id / "preferences.yaml"
    if not prefs_path.exists():
        return dict(NSV_PREF_DEFAULTS)
    import yaml  # ImportError here means deps not installed; surface it loudly.
    try:
        data = yaml.safe_load(prefs_path.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"⚠️  YAML parse error in {prefs_path}: {e}; using defaults", file=sys.stderr)
        return dict(NSV_PREF_DEFAULTS)
    section = data.get("non_speech_vocal") or {}
    merged = dict(NSV_PREF_DEFAULTS)
    merged.update({k: v for k, v in section.items() if v is not None})
    return merged


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


def write_skipped_output(final_out: Path, audio_path: Path, base: Path, reason: str) -> None:
    """Write an empty degraded NSV file so downstream tools see consistent shape."""
    final_out.write_text(json.dumps({
        "audio": str(audio_path.relative_to(base)),
        "duration": get_audio_duration(audio_path),
        "model": MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "degraded": True,
        "skipped_reason": reason,
        "stats": {"total_chunks": 0, "failed_chunks": 0, "raw_events": 0,
                  "after_dedup": 0, "after_filters": 0, "by_type": {}},
        "events": [],
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Detect non-speech vocal events in podcast audio via Gemini."
    )
    parser.add_argument("base_dir", help="Cut output directory (contains 1_transcript/, 2_analysis/)")
    parser.add_argument("--user", default=os.environ.get("PODCAST_EDIT_USER", "default"),
                        help="User id (drives cut/user-prefs/<userId>/preferences.yaml). "
                             "Defaults to $PODCAST_EDIT_USER or 'default'.")
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

    prefs = load_user_prefs(args.user)
    print(f"User '{args.user}' non_speech_vocal prefs: {prefs}", flush=True)

    if not prefs.get("enabled", True) or prefs.get("default_action") == "ignore":
        reason = ("user disabled non_speech_vocal" if not prefs.get("enabled", True)
                  else "user set default_action=ignore")
        print(f"⚠️  {reason}; skipping NSV detection.", file=sys.stderr)
        write_skipped_output(final_out, audio_path, base, reason)
        sys.exit(0)

    try:
        api_key = load_api_key()
    except RuntimeError as e:
        print(f"⚠️  {e}; skipping NSV detection (pipeline-graceful failure).", file=sys.stderr)
        write_skipped_output(final_out, audio_path, base, f"missing_api_key: {e}")
        sys.exit(0)

    from google import genai
    client = genai.Client(api_key=api_key)

    words_path = base / "1_transcript" / "subtitles_words.json"
    if not words_path.exists():
        print(f"⚠️  {words_path} missing — events will lack zone info.", file=sys.stderr)
        word_spans = []
    else:
        word_spans = load_word_spans(words_path)

    raw_events, raw_responses, failed_chunks = scan_audio(audio_path, client)
    events = dedupe_events(raw_events)
    events = annotate_events_with_word_context(events, word_spans)

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
        ev["filter_decision"] = "ok"  # baseline; filters may override

    # Load Whisper words (with isGap=True entries kept) for filter 1 context check
    words_full = []
    if words_path.exists():
        wd = json.loads(words_path.read_text())
        if isinstance(wd, dict) and "words" in wd:
            wd = wd["words"]
        words_full = wd

    after_dedup_count = len(events)
    detect_types = tuple(prefs.get("detect_types") or NSV_PREF_DEFAULTS["detect_types"])
    kept, dropped = apply_all_filters(events, words_full, detect_types=detect_types)
    events = kept

    # Confidence-threshold filter (user-tunable; default 0.4 = passes most events)
    conf_threshold = float(prefs.get("confidence_threshold", 0.4))
    if conf_threshold > 0:
        before = len(events)
        events = [e for e in events if e.get("confidence", 0) >= conf_threshold]
        if before != len(events):
            print(f"  confidence_threshold={conf_threshold} dropped {before - len(events)} events", flush=True)

    # auto_delete_threshold → high-confidence events get auto_enable=true so
    # run_fine_analysis.js can emit them with enabled=true (skip user review).
    # default_action=delete overrides: every surviving event gets auto_enable.
    auto_threshold = float(prefs.get("auto_delete_threshold", 0.99))
    default_action = prefs.get("default_action", "review")
    for ev in events:
        ev["auto_enable"] = (
            default_action == "delete"
            or ev.get("confidence", 0) >= auto_threshold
        )

    duration = get_audio_duration(audio_path)
    degraded = failed_chunks >= 2

    # Write debug raw responses
    raw_out.write_text(json.dumps({"chunks": raw_responses}, ensure_ascii=False, indent=2))

    for i, ev in enumerate(events):
        ev["id"] = f"nsv-{i}"

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
            "raw_events": len(raw_events),
            "after_dedup": after_dedup_count,
            "after_filters": len(events),
            "dropped_count": len(dropped),
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
