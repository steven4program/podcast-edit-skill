#!/usr/bin/env python3
"""
librosa boundary refiner for YAMNet noise-event candidates.

For each YAMNet candidate range, compute a fine-resolution RMS energy envelope
within ±0.5s of the range and snap start/end to the actual non-speech "blob"
boundaries. Also annotates each event with energy/spectral features so you can
filter manually in the review panel if you want.

This is a Gemini-free refiner — it does NOT filter out events. Every YAMNet
candidate becomes one event in the output. Pair with a wide YAMNet threshold
(e.g. 0.01) when you want to hear everything.

Usage:
    python3 refine_noise_events_librosa.py \
        --input audio.mp3 \
        --candidates noise_candidates.json \
        --output noise_events.json

Output schema (compatible with the review panel):
{
  "audio_file": "...",
  "model": "yamnet+librosa",
  "events": [
    {
      "start": 130.5, "end": 130.74,
      "class": "Throat clearing",                # from YAMNet
      "yamnet_class": "Throat clearing",
      "yamnet_score": 0.04,
      "verdict": "candidate",
      "is_speech_overlapped": false,             # heuristic
      "confidence": 0.04,
      "explanation": "rms_peak=0.12, spectral_flatness=0.31, dur=240ms",
      "speaker": "Ted",
      "rms_peak": 0.12,
      "spectral_flatness": 0.31
    }
  ],
  "summary": {"total": N, "confirmed": 0, "false_positives": 0,
              "borderline": 0, "candidates": N}
}
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")


def load_audio_16k_mono(path: str):
    import numpy as np
    cmd = [
        "ffmpeg", "-v", "error", "-i", path,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    raw = np.frombuffer(proc.stdout, dtype=np.int16)
    return raw.astype(np.float32) / 32768.0


def refine_event(y, sr, start_s, end_s, pad_s=0.5, hop_ms=10, win_ms=20):
    """
    Snap start/end to the energy 'blob' around the YAMNet range.

    Returns: (refined_start, refined_end, rms_peak, spectral_flatness,
              is_speech_overlapped_heuristic)
    """
    import numpy as np
    import librosa

    win_samp = int(sr * win_ms / 1000)
    hop_samp = int(sr * hop_ms / 1000)

    ctx_start = max(0.0, start_s - pad_s)
    ctx_end = min(len(y) / sr, end_s + pad_s)
    a = int(ctx_start * sr)
    b = int(ctx_end * sr)
    if b - a < win_samp * 2:
        return start_s, end_s, 0.0, 0.0, False

    seg = y[a:b]

    # RMS energy envelope (high-res)
    rms = librosa.feature.rms(y=seg, frame_length=win_samp, hop_length=hop_samp)[0]
    if rms.size == 0:
        return start_s, end_s, 0.0, 0.0, False

    # Find peak inside the YAMNet range — that's the event center
    yam_a_rel = (start_s - ctx_start)
    yam_b_rel = (end_s - ctx_start)
    a_idx = max(0, int(yam_a_rel * sr / hop_samp))
    b_idx = min(len(rms), int(yam_b_rel * sr / hop_samp))
    if b_idx <= a_idx:
        a_idx, b_idx = 0, len(rms)

    peak_idx_rel = a_idx + int(np.argmax(rms[a_idx:b_idx]))
    peak_val = float(rms[peak_idx_rel])

    # Local noise floor = 25th percentile of the context window
    floor = float(np.percentile(rms, 25))
    # Threshold: midway between floor and peak, biased low so we capture tails
    thresh = max(floor * 1.5, floor + (peak_val - floor) * 0.30)
    if peak_val <= floor * 1.1:
        # very weak event; just return YAMNet's range
        return start_s, end_s, peak_val, 0.0, False

    # Walk left from peak until below threshold for >= 3 frames in a row
    left = peak_idx_rel
    below = 0
    while left > 0 and below < 3:
        left -= 1
        if rms[left] < thresh:
            below += 1
        else:
            below = 0
    # Walk right similarly
    right = peak_idx_rel
    below = 0
    while right < len(rms) - 1 and below < 3:
        right += 1
        if rms[right] < thresh:
            below += 1
        else:
            below = 0

    refined_start = ctx_start + (left * hop_samp) / sr
    refined_end = ctx_start + (right * hop_samp) / sr

    # Clamp duration to a reasonable max (don't refine to multi-second blobs)
    max_dur = 2.0
    if refined_end - refined_start > max_dur:
        # collapse back toward YAMNet's range
        refined_start = max(refined_start, start_s - 0.1)
        refined_end = min(refined_end, end_s + 0.1)

    # Spectral flatness around the peak — high flatness ~ noise-like; low ~ tonal
    peak_sample = int((refined_start + (refined_end - refined_start) / 2) * sr)
    half = int(0.05 * sr)
    s0 = max(0, peak_sample - half - a)
    s1 = min(len(seg), peak_sample + half - a)
    flat = 0.0
    if s1 - s0 > 256:
        flat = float(librosa.feature.spectral_flatness(y=seg[s0:s1])[0].mean())

    # Heuristic: if the event range overlaps the rising/falling edge of a
    # *longer* high-energy run (likely speech), call it speech-overlapped.
    # Simple proxy: ratio of "above-thresh" frames in the refined range.
    above = (rms[left:right + 1] > thresh).sum()
    total = max(1, right - left + 1)
    is_overlapped = bool((right - left) >= len(rms) * 0.6)  # event spans most of the context window

    return (
        round(refined_start, 3),
        round(refined_end, 3),
        round(peak_val, 4),
        round(flat, 3),
        is_overlapped,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--candidates", "-c", required=True)
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--pad", type=float, default=0.5,
                    help="context padding around each YAMNet range (s)")
    args = ap.parse_args()

    if not Path(args.input).exists():
        print(f"❌ not found: {args.input}")
        sys.exit(1)

    print(f"🎧 input: {args.input}")
    print(f"📥 candidates: {args.candidates}")

    cand = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    candidates = cand.get("candidates", [])
    print(f"   {len(candidates)} YAMNet candidates")

    print("🎚 decoding to 16kHz mono…")
    y = load_audio_16k_mono(args.input)
    sr = 16000
    print(f"   samples: {len(y)} ({len(y)/sr:.1f}s)")

    events = []
    for i, c in enumerate(candidates):
        rs, re_, peak, flat, overlapped = refine_event(
            y, sr, c["start"], c["end"], pad_s=args.pad
        )
        dur_ms = int((re_ - rs) * 1000)
        ev = {
            "start": rs,
            "end": re_,
            "class": c["class"],
            "yamnet_class": c["class"],
            "yamnet_score": c["score"],
            "verdict": "candidate",
            "is_speech_overlapped": overlapped,
            "confidence": c["score"],
            "explanation": f"rms_peak={peak:.3f}, spectral_flatness={flat:.2f}, dur={dur_ms}ms",
            "rms_peak": peak,
            "spectral_flatness": flat,
        }
        if c.get("speaker"):
            ev["speaker"] = c["speaker"]
        events.append(ev)

        speaker = c.get("speaker", "?")
        print(f"   [{i+1}/{len(candidates)}] {speaker} {c['start']:.2f}-{c['end']:.2f} {c['class']:<22}"
              f" → {rs:.3f}-{re_:.3f} ({dur_ms}ms) peak={peak:.3f} flat={flat:.2f}"
              f"{' [overlap?]' if overlapped else ''}")

    out = {
        "audio_file": os.path.basename(args.input),
        "model": "yamnet+librosa",
        "events": events,
        "summary": {
            "total": len(events),
            "confirmed": 0,
            "false_positives": 0,
            "borderline": 0,
            "candidates": len(events),
        },
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"💾 wrote {args.output}")


if __name__ == "__main__":
    main()
