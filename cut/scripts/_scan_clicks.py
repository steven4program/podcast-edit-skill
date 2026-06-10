#!/usr/bin/env python3
"""Diagnostic: count click-like transients (sudden broadband spikes) in audio.

A splice click = one-sample-scale discontinuity → huge energy in the
first-difference signal, isolated, much louder than the surrounding
high-frequency floor. Natural speech (plosives, mouth noise) raises HF energy
too but over tens of ms, not isolated 1 ms bursts.
"""
import sys
import numpy as np
import librosa

RATIO = 14.0          # 1ms HF burst must exceed this x the local 100ms median
ABS_MIN = 0.01        # and be non-trivial in absolute terms

def scan(path):
    y, sr = librosa.load(path, sr=16000, mono=True)
    d = np.abs(np.diff(y))
    win = int(0.001 * sr)               # 1 ms frames
    n = len(d) // win
    e = d[:n * win].reshape(n, win).max(axis=1)   # peak |diff| per 1ms frame
    # local context: median over +-50 frames (100 ms)
    k = 50
    med = np.empty(n)
    for i in range(n):
        a, b = max(0, i - k), min(n, i + k + 1)
        med[i] = np.median(e[a:b])
    med = np.maximum(med, 1e-5)
    hits = np.where((e > RATIO * med) & (e > ABS_MIN))[0]
    # collapse consecutive frames into events
    events = []
    for i in hits:
        t = i * 0.001
        if events and t - events[-1] < 0.05:
            continue
        events.append(t)
    dur_min = len(y) / sr / 60
    print(f"{path}")
    print(f"  duration {dur_min:.1f} min, click-like events: {len(events)} "
          f"({len(events)/dur_min:.2f}/min)")
    for t in events[:12]:
        print(f"    at {int(t//60)}:{t%60:05.2f}")
    return len(events) / dur_min

if __name__ == '__main__':
    for p in sys.argv[1:]:
        scan(p)
