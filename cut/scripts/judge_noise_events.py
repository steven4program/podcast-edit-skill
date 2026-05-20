#!/usr/bin/env python3
"""
Gemini 2.5 Flash fine-judge for YAMNet noise-event candidates.

For each candidate, extract a ±1.5s clip around the YAMNet range and ask Gemini
whether it is a real removable noise event. If real, Gemini also returns refined
start/end offsets so we cut tightly without trimming speech.

Usage:
    python3 judge_noise_events.py --input audio.mp3 \
        --candidates noise_candidates.json --output noise_events.json
        [--model gemini-2.5-flash] [--context 1.5]

Output schema:
{
  "audio_file": "...",
  "model": "gemini-2.5-flash",
  "events": [
    {
      "start": 12.42, "end": 12.97,            # final, refined
      "class": "Cough",                         # final classification
      "yamnet_class": "Cough",
      "yamnet_score": 0.71,
      "verdict": "confirmed" | "false_positive" | "borderline",
      "is_speech_overlapped": false,
      "explanation": "...",
      "speaker": "Hogan"
    }
  ],
  "summary": {"total": N, "confirmed": K, "false_positives": M, "borderline": B}
}
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")


PROMPT = """You are a podcast post-production editor. A coarse audio classifier flagged a possible non-speech noise in a Chinese-language podcast.

Flagged: class="{cls}", coarse_range=[{rs:.2f}s, {re:.2f}s] within this clip (clip starts at 0).
Clip context: this audio clip is {clip_dur:.1f}s long, sampled around the flagged range with ~1.5s padding on each side.

Listen carefully and decide:

1. Is there a REAL removable noise (cough, throat-clearing, sniff, sneeze, snort, gasp, mouth click, lip smack, heavy noticeable breath) in this clip?
   - Ignore: normal speech, natural quiet inhalations between sentences, ambient room tone.
   - "Removable" means: cutting it out would make the edit cleaner without losing meaning.

2. If REAL: give a tight start/end IN CLIP-LOCAL SECONDS (0 = clip start). Be precise — do not pad. Target ±50ms accuracy.

3. If the noise overlaps with the speaker's words (you can hear speech AT THE SAME TIME), set "is_speech_overlapped": true — we will NOT cut it.

4. Pick the best label for the noise: one of [Cough, Throat clearing, Sniff, Sneeze, Snort, Gasp, Mouth click, Lip smack, Heavy breath, Other].

Respond ONLY with this JSON (no markdown, no prose):
{{
  "verdict": "confirmed" | "false_positive" | "borderline",
  "clip_start": 0.00,
  "clip_end": 0.00,
  "class": "Cough",
  "is_speech_overlapped": false,
  "confidence": 0.0,
  "explanation": "one short sentence"
}}

If verdict is "false_positive", set clip_start=clip_end=0.
"""


def load_env_key():
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def extract_clip(input_path, start, end, out_path):
    cmd = [
        "ffmpeg", "-v", "quiet",
        "-i", input_path,
        "-af", f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS",
        "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-y", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


def parse_json(text):
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


def call_gemini(client, model, audio_bytes, prompt, max_retries=3):
    from google.genai import types
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[prompt, types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")],
            )
            return resp.text
        except Exception as e:
            es = str(e)
            if "RATE_LIMIT" in es or "429" in es:
                w = (2 ** attempt) * 2
                print(f"   ⏳ rate-limited, sleeping {w}s")
                time.sleep(w)
            elif attempt < max_retries - 1:
                print(f"   ⚠️ retry ({attempt+1}): {es[:80]}")
                time.sleep(1)
            else:
                print(f"   ❌ giving up: {es[:80]}")
                return None
    return None


def get_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--candidates", "-c", required=True)
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--context", type=float, default=1.5, help="seconds of padding on each side")
    ap.add_argument("--limit", type=int, default=0, help="cap number of candidates (0=all)")
    args = ap.parse_args()

    key = load_env_key()
    if not key:
        print("❌ GEMINI_API_KEY missing (env or .env)")
        sys.exit(1)

    cand_data = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    candidates = cand_data.get("candidates", [])
    if args.limit:
        candidates = candidates[:args.limit]
    print(f"📥 {len(candidates)} candidates to judge")

    duration = get_duration(args.input)

    from google import genai
    client = genai.Client(api_key=key)

    events = []
    confirmed = false_pos = borderline = 0

    with tempfile.TemporaryDirectory() as tmp:
        for idx, c in enumerate(candidates):
            cs = max(0.0, c["start"] - args.context)
            ce = min(duration, c["end"] + args.context)
            clip_dur = ce - cs
            rel_rs = c["start"] - cs
            rel_re = c["end"] - cs

            prompt = PROMPT.format(cls=c["class"], rs=rel_rs, re=rel_re, clip_dur=clip_dur)
            clip_path = os.path.join(tmp, f"c_{idx}.wav")

            print(f"   [{idx+1}/{len(candidates)}] {c['start']:.2f}-{c['end']:.2f} {c['class']} sc={c['score']:.2f} ", end="", flush=True)

            if not extract_clip(args.input, cs, ce, clip_path):
                print("⚠️ extract failed")
                continue

            with open(clip_path, "rb") as f:
                audio = f.read()

            resp = call_gemini(client, args.model, audio, prompt)
            parsed = parse_json(resp)
            if not parsed:
                print("⚠️ parse failed")
                continue

            verdict = parsed.get("verdict", "borderline")
            spoken = bool(parsed.get("is_speech_overlapped", False))
            ev = {
                "yamnet_class": c["class"],
                "yamnet_score": c["score"],
                "verdict": verdict,
                "is_speech_overlapped": spoken,
                "confidence": float(parsed.get("confidence", 0.0)),
                "explanation": parsed.get("explanation", ""),
                "class": parsed.get("class", c["class"]),
            }
            if c.get("speaker"):
                ev["speaker"] = c["speaker"]

            if verdict == "confirmed" and not spoken:
                # Map clip-local seconds back to absolute seconds in source audio
                rs = float(parsed.get("clip_start", rel_rs)) + cs
                re_ = float(parsed.get("clip_end", rel_re)) + cs
                if re_ <= rs:
                    rs, re_ = c["start"], c["end"]
                ev["start"] = round(rs, 3)
                ev["end"] = round(re_, 3)
                confirmed += 1
                emoji = "✅"
            elif verdict == "borderline":
                ev["start"] = c["start"]
                ev["end"] = c["end"]
                borderline += 1
                emoji = "⚠️"
            else:
                ev["start"] = c["start"]
                ev["end"] = c["end"]
                false_pos += 1
                emoji = "🚫"

            print(f"{emoji} {verdict} ({ev['class']}) {ev['explanation'][:60]}")
            events.append(ev)

    out = {
        "audio_file": os.path.basename(args.input),
        "model": args.model,
        "events": events,
        "summary": {
            "total": len(events),
            "confirmed": confirmed,
            "false_positives": false_pos,
            "borderline": borderline,
        },
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n💾 wrote {args.output}")
    print(f"   confirmed={confirmed} false_pos={false_pos} borderline={borderline}")


if __name__ == "__main__":
    main()
