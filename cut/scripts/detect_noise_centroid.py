#!/usr/bin/env python3
"""
Spectral-centroid based detector for throat-clearing / cough / low-frequency
non-speech bursts that sit ADJACENT TO or INSIDE speech (where YAMNet fails).

Heuristic: in a speech-active frame, if the spectral centroid drops well below
the local speech baseline (rolling median across ±1s), this is unusual enough
to flag. Throat clears have lots of low-frequency rumble (centroid 400-800Hz)
while normal speech sits at 1000-1600Hz.

Combines with:
  - rolloff_85 ratio (low rolloff = energy concentrated in bass)
  - rms (must be loud enough to be a real event, not silence)
  - zcr (low ZCR + low centroid + active energy = voiced low-frequency burst)

Usage:
    python3 detect_noise_centroid.py --input audio.mp3 \
        --output noise_candidates_centroid.json --speaker Ted \
        [--centroid-ratio 0.65] [--min-dur-ms 60] [--max-dur-ms 600]
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
    cmd = ["ffmpeg", "-v", "error", "-i", path,
           "-f", "s16le", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", "-"]
    p = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.int16).astype("float32") / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--speaker", default=None)
    ap.add_argument("--centroid-ratio", type=float, default=0.65,
                    help="frame flagged when centroid < local_speech_median * this (default 0.65)")
    ap.add_argument("--rolloff-ratio", type=float, default=0.65,
                    help="optional: flag also when rolloff_85 < local_speech_median * this (default 0.65)")
    ap.add_argument("--min-rms-mult", type=float, default=2.0,
                    help="frame must have rms > global_speech_median * this (default 2.0)")
    ap.add_argument("--min-dur-ms", type=int, default=60)
    ap.add_argument("--max-dur-ms", type=int, default=600)
    ap.add_argument("--merge-gap-ms", type=int, default=120)
    ap.add_argument("--local-window-s", type=float, default=2.0,
                    help="rolling-median window for local speech baseline (default 2.0s)")
    args = ap.parse_args()

    import numpy as np
    import librosa
    from scipy.ndimage import median_filter

    print(f"🎧 input: {args.input}")
    y = load_audio_16k_mono(args.input)
    sr = 16000

    hop = 160  # 10ms
    win = 400  # 25ms
    n_fft = 512

    rms = librosa.feature.rms(y=y, frame_length=win, hop_length=hop)[0]
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, n_fft=n_fft, hop_length=hop)[0]
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, n_fft=n_fft, hop_length=hop, roll_percent=0.85)[0]
    zcr = librosa.feature.zero_crossing_rate(y=y, frame_length=win, hop_length=hop)[0]
    n = min(len(rms), len(cent), len(rolloff), len(zcr))
    rms = rms[:n]; cent = cent[:n]; rolloff = rolloff[:n]; zcr = zcr[:n]

    # Global speech baseline: median of speech-active frames
    rms_med = float(np.median(rms[rms > 1e-4])) if (rms > 1e-4).any() else 1e-3
    active = rms > rms_med * args.min_rms_mult
    if not active.any():
        print("   no active frames — bailing")
        Path(args.output).write_text(json.dumps({"candidates": []}, ensure_ascii=False))
        return

    cent_speech_med_global = float(np.median(cent[active]))
    rolloff_speech_med_global = float(np.median(rolloff[active]))
    print(f"   rms_med={rms_med:.5f}, active frames={int(active.sum())}/{n}")
    print(f"   global speech centroid med={cent_speech_med_global:.0f}Hz, "
          f"rolloff_85 med={rolloff_speech_med_global:.0f}Hz")

    # Local rolling median across only ACTIVE frames (so silences don't pull the
    # baseline down). Use scipy.ndimage.median_filter on the centroid signal
    # where inactive frames carry the global median (so the filter passes
    # through them without distorting local stats).
    win_frames = int(args.local_window_s * sr / hop)  # e.g. 200 frames for 2s
    cent_for_filter = np.where(active, cent, cent_speech_med_global)
    rolloff_for_filter = np.where(active, rolloff, rolloff_speech_med_global)
    cent_local = median_filter(cent_for_filter, size=win_frames, mode="nearest")
    rolloff_local = median_filter(rolloff_for_filter, size=win_frames, mode="nearest")

    # Flag: ACTIVE + low centroid OR low rolloff (low-frequency dominance)
    cent_low = cent < cent_local * args.centroid_ratio
    rolloff_low = rolloff < rolloff_local * args.rolloff_ratio
    flag = active & (cent_low | rolloff_low)

    # Light cleanup: erode tiny 1-frame flickers, dilate small gaps
    # Replace with simple smoothing — keep flag if 2+ of 5 surrounding frames are flagged
    smoothed = np.zeros_like(flag, dtype=bool)
    cumsum = np.cumsum(flag.astype(int))
    for i in range(n):
        lo = max(0, i - 2)
        hi = min(n - 1, i + 2)
        if cumsum[hi] - (cumsum[lo - 1] if lo > 0 else 0) >= 2:
            smoothed[i] = True
    flag = smoothed

    # Run-length encode
    events_raw = []
    i = 0
    while i < n:
        if not flag[i]:
            i += 1
            continue
        j = i
        while j < n and flag[j]:
            j += 1
        events_raw.append((i, j))
        i = j

    # Merge bursts close together
    gap_frames = int(args.merge_gap_ms / 1000 * sr / hop)
    merged = []
    for a, b in events_raw:
        if merged and a - merged[-1][1] <= gap_frames:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    # Duration filter
    min_f = int(args.min_dur_ms / 1000 * sr / hop)
    max_f = int(args.max_dur_ms / 1000 * sr / hop)
    candidates = []
    for a, b in merged:
        dur_f = b - a
        if dur_f < min_f or dur_f > max_f:
            continue
        start_s = a * hop / sr
        end_s = b * hop / sr
        peak_rms = float(rms[a:b].max())
        mean_cent = float(cent[a:b].mean())
        mean_rolloff = float(rolloff[a:b].mean())
        mean_zcr = float(zcr[a:b].mean())
        # Local baselines at the event center for context
        mid = (a + b) // 2
        local_cent = float(cent_local[mid])
        local_rolloff = float(rolloff_local[mid])
        c_ratio = mean_cent / local_cent if local_cent else 0
        r_ratio = mean_rolloff / local_rolloff if local_rolloff else 0
        # Composite score for sorting
        score = round(min(1.0, max(0.0, (1 - c_ratio) * 0.7 + (1 - r_ratio) * 0.3)), 3)
        c = {
            "start": round(start_s, 3),
            "end": round(end_s, 3),
            "class": "Low-centroid burst",
            "score": score,
            "centroid_hz": round(mean_cent, 0),
            "centroid_ratio": round(c_ratio, 2),
            "rolloff_hz": round(mean_rolloff, 0),
            "zcr": round(mean_zcr, 3),
            "rms_peak": round(peak_rms, 4),
        }
        if args.speaker:
            c["speaker"] = args.speaker
        candidates.append(c)

    print(f"   raw events: {len(events_raw)} → merged: {len(merged)} → kept "
          f"(dur in [{args.min_dur_ms},{args.max_dur_ms}]ms): {len(candidates)}")
    for c in candidates[:30]:
        print(f"   {c['start']:>6.2f}-{c['end']:>6.2f}s  cent={c['centroid_hz']:>4.0f}Hz "
              f"({c['centroid_ratio']:.2f}x)  rolloff={c['rolloff_hz']:>4.0f}Hz ({r_ratio:.2f}x ignored)  "
              f"rms_peak={c['rms_peak']:.3f}  zcr={c['zcr']:.3f}")
    if len(candidates) > 30:
        print(f"   ... ({len(candidates) - 30} more)")

    out = {
        "audio_file": os.path.basename(args.input),
        "duration": round(len(y) / sr, 2),
        "model": "centroid-drop",
        "params": vars(args),
        "candidates": candidates,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"💾 wrote {args.output}")


if __name__ == "__main__":
    main()
