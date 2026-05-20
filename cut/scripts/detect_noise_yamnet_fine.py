#!/usr/bin/env python3
"""
YAMNet noise detector with fine effective hop (0.096s) via offset stacking.

Standard YAMNet has a 0.48s internal hop. Short events (cough/throat-clear
~100-400ms) that fall between frames get diluted — the 0.96s window covers
~80% speech + ~20% event, and the target-class score stays near zero.

We run YAMNet N=5 times with start offsets [0, 0.096, 0.192, 0.288, 0.384]s.
Stacking interleaves the frames into an effective 0.096s hop. Short events
that align better with one of the offset passes get a much higher score
because the window is more centered on them.

Usage:
    python3 detect_noise_yamnet_fine.py --input audio.mp3 \
        --output noise_yamnet_fine.json --speaker Ted \
        --threshold 0.03 --min-gap 0.2
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")

TARGET_CLASSES = {
    "Cough", "Throat clearing", "Sneeze", "Sniff", "Snort",
    "Gasp", "Wheeze", "Hiccup", "Burping, eructation",
    "Breathing", "Pant",
}


def load_audio_16k_mono(path):
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-i", path,
           "-f", "s16le", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", "-"]
    p = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.int16).astype("float32") / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--speaker", default=None)
    ap.add_argument("--threshold", type=float, default=0.03)
    ap.add_argument("--min-gap", type=float, default=0.2)
    ap.add_argument("--n-passes", type=int, default=5,
                    help="number of offset passes (default 5 → effective 0.096s hop)")
    ap.add_argument("--also-flag-speech-drop", action="store_true",
                    help="ALSO flag frames where Speech<0.7 and any target>0.003")
    args = ap.parse_args()

    import numpy as np
    import tensorflow_hub as hub

    print(f"🎧 input: {args.input}")
    y = load_audio_16k_mono(args.input)
    sr = 16000
    print(f"   {len(y)} samples ({len(y)/sr:.1f}s)")

    print("📦 loading YAMNet…")
    model_url = os.environ.get("YAMNET_URL", "https://www.kaggle.com/models/google/yamnet/TensorFlow2/yamnet/1")
    model = hub.load(model_url)
    class_map = model.class_map_path().numpy().decode("utf-8")
    classes = []
    with open(class_map, "r", encoding="utf-8") as f:
        rdr = csv.reader(f); next(rdr)
        for row in rdr:
            classes.append(row[2])

    speech_idx = classes.index("Speech")
    target_idx = {classes.index(c): c for c in TARGET_CLASSES if c in classes}
    print(f"   target classes: {len(target_idx)}")

    yam_hop_s = 0.48
    yam_win_s = 0.96
    n_passes = args.n_passes
    offset_step_s = yam_hop_s / n_passes  # 0.096 for n=5
    offset_step_samples = int(offset_step_s * sr)

    print(f"🧠 running YAMNet × {n_passes} offset passes (effective hop {offset_step_s*1000:.0f}ms)…")
    # Each pass i: feed audio starting at offset i*offset_step_samples
    # YAMNet returns frames at times: pass_offset + k*yam_hop_s, k=0..K-1
    # We collect (absolute_time, scores) tuples then sort by time.
    all_scores = []  # list of (t, scores_vec)
    for i in range(n_passes):
        off = i * offset_step_samples
        sub = y[off:]
        if len(sub) < int(yam_win_s * sr):
            continue
        scores_tf, _, _ = model(sub)
        scores = scores_tf.numpy()
        # Frame k absolute time = off/sr + k*yam_hop_s + yam_win_s/2 (center)
        # Use frame START for compatibility with the standard detector
        base_t = off / sr
        for k in range(scores.shape[0]):
            t = base_t + k * yam_hop_s
            all_scores.append((round(t, 4), scores[k]))
        print(f"   pass {i+1}/{n_passes}: offset {off/sr:.3f}s → {scores.shape[0]} frames")

    all_scores.sort(key=lambda x: x[0])
    n_total = len(all_scores)
    print(f"   total frames: {n_total}")

    # Frame-level hits
    hits = []
    for t, sc in all_scores:
        speech = float(sc[speech_idx])
        for cidx, cname in target_idx.items():
            v = float(sc[cidx])
            if v >= args.threshold:
                hits.append((t, cname, v))
                break  # one hit per frame is enough
        else:
            if args.also_flag_speech_drop and speech < 0.7:
                # pick the highest-scoring target class even if below threshold
                best_cidx = max(target_idx.keys(), key=lambda i: sc[i])
                best_v = float(sc[best_cidx])
                if best_v > 0.003:
                    hits.append((t, target_idx[best_cidx] + " (speech-drop)", best_v))

    print(f"   raw frame hits: {len(hits)}")

    # Merge consecutive hits within min_gap into events
    hits.sort()
    events = []
    for t, name, sc in hits:
        if events and t - events[-1]["end"] <= args.min_gap:
            ev = events[-1]
            ev["end"] = t + yam_win_s
            if sc > ev["score"]:
                ev["score"] = sc
                ev["class"] = name
        else:
            events.append({"start": t, "end": t + yam_win_s, "class": name, "score": sc})

    # Don't let merged events sprawl past the last hit + 0.2s
    for ev in events:
        ev["end"] = min(ev["end"], ev["start"] + yam_win_s + 0.5)
        ev["start"] = round(ev["start"], 3)
        ev["end"] = round(ev["end"], 3)
        ev["score"] = round(ev["score"], 3)

    print(f"   merged events: {len(events)}")
    for e in events[:40]:
        print(f"   {e['start']:>6.2f}-{e['end']:>6.2f}  {e['class']:<28} score={e['score']:.3f}")

    out_candidates = []
    for e in events:
        c = dict(e)
        if args.speaker:
            c["speaker"] = args.speaker
        out_candidates.append(c)

    out = {
        "audio_file": os.path.basename(args.input),
        "duration": round(len(y) / sr, 2),
        "model": f"yamnet-fine-hop-{offset_step_s*1000:.0f}ms",
        "params": {"threshold": args.threshold, "min_gap": args.min_gap,
                   "n_passes": n_passes},
        "candidates": out_candidates,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"💾 wrote {args.output}")


if __name__ == "__main__":
    main()
