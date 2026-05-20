#!/usr/bin/env python3
"""
Diagnostic: scan a small time range of an audio file and print YAMNet's top-5
class scores per frame, plus librosa RMS + spectral flatness at fine resolution.
Use this to figure out why a known event was missed.

Usage:
    python3 debug_noise_at_time.py --input audio.mp3 --start 14 --end 17
"""
import argparse
import subprocess
import sys

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")


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
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    args = ap.parse_args()

    import numpy as np
    import librosa
    import tensorflow_hub as hub
    import csv, os

    print(f"🎧 {args.input}  range {args.start}-{args.end}s")
    y = load_audio_16k_mono(args.input)
    sr = 16000

    a = int(args.start * sr)
    b = int(args.end * sr)
    seg = y[max(0, a - sr): min(len(y), b + sr)]  # ±1s padding for YAMNet context

    # YAMNet
    print("\n--- YAMNet ---")
    model_url = os.environ.get("YAMNET_URL", "https://www.kaggle.com/models/google/yamnet/TensorFlow2/yamnet/1")
    model = hub.load(model_url)
    class_map = model.class_map_path().numpy().decode("utf-8")
    classes = []
    with open(class_map, "r", encoding="utf-8") as f:
        rdr = csv.reader(f)
        next(rdr)
        for row in rdr:
            classes.append(row[2])

    scores_tf, _, _ = model(seg)
    scores = scores_tf.numpy()  # (T, 521)
    # Frame i covers [i*0.48 - 0.48, i*0.48 + 0.48] roughly, with hop 0.48
    # seg starts at args.start - 1 in absolute time
    seg_start_abs = max(0, args.start - 1.0)
    print(f"  {scores.shape[0]} frames @ 0.48s hop, win 0.96s")
    print(f"  {'frame':<6} {'abs_t':<8} {'top-5 classes (score)':<60}")
    for i in range(scores.shape[0]):
        t_abs = seg_start_abs + i * 0.48
        if t_abs < args.start - 0.5 or t_abs > args.end + 0.5:
            continue
        top = np.argsort(scores[i])[::-1][:5]
        s = " ".join(f"{classes[k]}={scores[i,k]:.3f}" for k in top)
        # Also flag target-class scores if non-zero
        targets = ["Throat clearing", "Cough", "Sniff", "Sneeze", "Snort",
                   "Gasp", "Breathing", "Burping, eructation", "Hiccup", "Mouth"]
        tgt_scores = {c: float(scores[i, classes.index(c)]) for c in targets if c in classes}
        tgt_nonzero = {k: v for k, v in tgt_scores.items() if v > 0.001}
        tgt_str = "  TARGETS: " + ", ".join(f"{k}={v:.3f}" for k, v in tgt_nonzero.items()) if tgt_nonzero else ""
        print(f"  {i:<6} {t_abs:<8.2f} {s}{tgt_str}")

    # librosa fine
    print("\n--- librosa (10ms hop) ---")
    hop = 160
    win = 400
    rms = librosa.feature.rms(y=y, frame_length=win, hop_length=hop)[0]
    flat = librosa.feature.spectral_flatness(y=y, n_fft=512, hop_length=hop)[0]
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, n_fft=512, hop_length=hop)[0]
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, n_fft=512, hop_length=hop, roll_percent=0.85)[0]
    zcr = librosa.feature.zero_crossing_rate(y=y, frame_length=win, hop_length=hop)[0]
    n = min(len(rms), len(flat), len(cent), len(rolloff), len(zcr))
    rms_med = float(np.median(rms[rms > 1e-4])) if (rms > 1e-4).any() else 1e-3
    # Centroid median computed only over speech-active frames (rms > median)
    speech_mask = rms[:n] > rms_med * 0.5
    cent_speech_med = float(np.median(cent[:n][speech_mask])) if speech_mask.any() else 0
    print(f"  rms_med={rms_med:.5f}  centroid_speech_med={cent_speech_med:.0f}Hz")
    a_f = int(args.start * sr / hop)
    b_f = int(args.end * sr / hop)
    print(f"  {'t':<7} {'rms':<8} {'rms/m':<7} {'flat':<7} {'cent(Hz)':<9} {'cent/m':<7} {'rolloff':<8} {'zcr':<6}")
    for i in range(a_f, min(b_f, n), 5):  # every 50ms
        t = i * hop / sr
        c_ratio = cent[i] / cent_speech_med if cent_speech_med else 0
        print(f"  {t:<7.2f} {rms[i]:<8.4f} {rms[i]/rms_med:<7.2f} {flat[i]:<7.3f} {cent[i]:<9.0f} {c_ratio:<7.2f} {rolloff[i]:<8.0f} {zcr[i]:<6.3f}")


if __name__ == "__main__":
    main()
