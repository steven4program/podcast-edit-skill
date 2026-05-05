#!/usr/bin/env python3
"""
Layer 1: signal-layer analysis — podcast edit-quality detection.

Auto-detects cut points and analyses 5 metrics:
1. Energy jumps (RMS energy ratio)
2. Unnatural silences (silence duration)
3. Waveform discontinuity (ZCR jump)
4. Spectral jumps (MFCC cosine similarity)
5. Breath-sound truncation (energy envelope pattern)

Requires no API key — pure local computation.

Usage:
    python3 signal_analysis.py --input podcast.mp3 --output qa_report.json
"""

import argparse
import json
import sys
from pathlib import Path

import librosa
import numpy as np


def detect_cut_points(y, sr, hop_length=512):
    """Auto-detect cut points in audio (based on energy and spectral changes)."""
    # Short-time RMS energy
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    times = librosa.times_like(rms, sr=sr, hop_length=hop_length)

    # Absolute value of energy difference
    rms_diff = np.abs(np.diff(rms))

    # Adaptive threshold: mean + 2 standard deviations
    threshold = np.mean(rms_diff) + 2.0 * np.std(rms_diff)

    # Find spike points
    peaks = np.where(rms_diff > threshold)[0]

    # Merge adjacent points (within 200ms count as the same cut)
    min_gap_frames = int(0.2 * sr / hop_length)
    merged = []
    for p in peaks:
        if not merged or p - merged[-1] > min_gap_frames:
            merged.append(p)

    cut_times = [float(times[p]) for p in merged if p < len(times)]
    return cut_times


def check_energy_jump(y, sr, cut_time, window_ms=100):
    """Check 1: energy jump before/after the cut point."""
    window_samples = int(window_ms / 1000 * sr)
    cut_sample = int(cut_time * sr)

    start_before = max(0, cut_sample - window_samples)
    end_after = min(len(y), cut_sample + window_samples)

    before = y[start_before:cut_sample]
    after = y[cut_sample:end_after]

    if len(before) == 0 or len(after) == 0:
        return None

    rms_before = np.sqrt(np.mean(before ** 2)) + 1e-10
    rms_after = np.sqrt(np.mean(after ** 2)) + 1e-10
    ratio = max(rms_before, rms_after) / min(rms_before, rms_after)

    if ratio > 3.0:
        return {
            "timestamp": round(cut_time, 2),
            "type": "energy_jump",
            "severity": "high" if ratio > 5.0 else "medium",
            "detail": f"Energy ratio {ratio:.1f}x at cut point",
            "suggestion": "Add 50ms crossfade",
            "listen_range": [round(max(0, cut_time - 2), 1), round(cut_time + 3, 1)],
            "metric": round(ratio, 2),
        }
    return None


def check_silence(y, sr, cut_time, top_db=40):
    """Check 2: unnatural silence (too short or too long)."""
    # Detect silence within a 2-second window around the cut point
    window = int(2 * sr)
    cut_sample = int(cut_time * sr)
    start = max(0, cut_sample - window)
    end = min(len(y), cut_sample + window)
    segment = y[start:end]

    # Detect non-silent intervals
    intervals = librosa.effects.split(segment, top_db=top_db)
    if len(intervals) < 2:
        return None

    # Compute silence segment durations
    for i in range(len(intervals) - 1):
        silence_start = intervals[i][1]
        silence_end = intervals[i + 1][0]
        silence_dur_ms = (silence_end - silence_start) / sr * 1000

        # Silence segment containing the cut point
        silence_start_time = start / sr + silence_start / sr
        silence_end_time = start / sr + silence_end / sr

        if silence_start_time <= cut_time <= silence_end_time:
            if silence_dur_ms < 100:
                return {
                    "timestamp": round(cut_time, 2),
                    "type": "unnatural_silence",
                    "severity": "medium",
                    "detail": f"Silence duration {silence_dur_ms:.0f}ms between sentences (expected 300-500ms)",
                    "suggestion": "Extend silence to 300ms",
                    "listen_range": [round(max(0, cut_time - 2), 1), round(cut_time + 3, 1)],
                    "metric": round(silence_dur_ms, 0),
                }
            elif silence_dur_ms > 2000:
                return {
                    "timestamp": round(cut_time, 2),
                    "type": "unnatural_silence",
                    "severity": "medium",
                    "detail": f"Silence duration {silence_dur_ms:.0f}ms is unusually long (>2s)",
                    "suggestion": "Trim silence to 500-800ms",
                    "listen_range": [round(max(0, cut_time - 2), 1), round(cut_time + 3, 1)],
                    "metric": round(silence_dur_ms, 0),
                }
    return None


def check_zcr_jump(y, sr, cut_time, window_ms=50):
    """Check 3: waveform discontinuity (zero-crossing rate jump)."""
    window_samples = int(window_ms / 1000 * sr)
    cut_sample = int(cut_time * sr)

    # Take 5 windows on each side of the cut
    n_windows = 5
    zcr_before = []
    zcr_after = []

    for i in range(n_windows):
        s = cut_sample - (i + 1) * window_samples
        e = s + window_samples
        if s >= 0:
            seg = y[s:e]
            zcr_before.append(np.mean(librosa.feature.zero_crossing_rate(seg)))

        s = cut_sample + i * window_samples
        e = s + window_samples
        if e <= len(y):
            seg = y[s:e]
            zcr_after.append(np.mean(librosa.feature.zero_crossing_rate(seg)))

    if not zcr_before or not zcr_after:
        return None

    mean_before = np.mean(zcr_before)
    mean_after = np.mean(zcr_after)
    std_all = np.std(zcr_before + zcr_after) + 1e-10
    z_score = abs(mean_after - mean_before) / std_all

    if z_score > 2.0:
        return {
            "timestamp": round(cut_time, 2),
            "type": "zcr_discontinuity",
            "severity": "medium",
            "detail": f"Zero-crossing rate jump: z-score {z_score:.1f}",
            "suggestion": "Apply short crossfade at cut point",
            "listen_range": [round(max(0, cut_time - 2), 1), round(cut_time + 3, 1)],
            "metric": round(z_score, 2),
        }
    return None


def check_spectral_jump(y, sr, cut_time, window_ms=200):
    """Check 4: spectral jump (MFCC cosine similarity)."""
    window_samples = int(window_ms / 1000 * sr)
    cut_sample = int(cut_time * sr)

    start_before = max(0, cut_sample - window_samples)
    end_after = min(len(y), cut_sample + window_samples)

    before = y[start_before:cut_sample]
    after = y[cut_sample:end_after]

    if len(before) < sr * 0.05 or len(after) < sr * 0.05:
        return None

    # Compute MFCC
    mfcc_before = np.mean(librosa.feature.mfcc(y=before, sr=sr, n_mfcc=13), axis=1)
    mfcc_after = np.mean(librosa.feature.mfcc(y=after, sr=sr, n_mfcc=13), axis=1)

    # Cosine similarity
    cos_sim = np.dot(mfcc_before, mfcc_after) / (
        np.linalg.norm(mfcc_before) * np.linalg.norm(mfcc_after) + 1e-10
    )

    if cos_sim < 0.7:
        return {
            "timestamp": round(cut_time, 2),
            "type": "spectral_jump",
            "severity": "high" if cos_sim < 0.5 else "medium",
            "detail": f"MFCC cosine similarity {cos_sim:.2f} (threshold: 0.7)",
            "suggestion": "Check for background noise change at cut point",
            "listen_range": [round(max(0, cut_time - 2), 1), round(cut_time + 3, 1)],
            "metric": round(cos_sim, 3),
        }
    return None


def check_breath_truncation(y, sr, cut_time, window_ms=150):
    """Check 5: breath-sound truncation."""
    window_samples = int(window_ms / 1000 * sr)
    cut_sample = int(cut_time * sr)

    # Breath features: low energy plus a particular spectral shape
    start = max(0, cut_sample - window_samples)
    end = min(len(y), cut_sample + window_samples)
    segment = y[start:end]

    if len(segment) < sr * 0.05:
        return None

    # Breath sounds typically sit in the 200-2000Hz range with low energy
    rms = np.sqrt(np.mean(segment ** 2))
    overall_rms = np.sqrt(np.mean(y ** 2))

    # Breath-sound energy is typically 5-20% of normal speech
    energy_ratio = rms / (overall_rms + 1e-10)

    if 0.05 < energy_ratio < 0.25:
        # Check whether the cut sits in the "middle" of a breath (asymmetric envelope)
        mid = len(segment) // 2
        first_half_rms = np.sqrt(np.mean(segment[:mid] ** 2))
        second_half_rms = np.sqrt(np.mean(segment[mid:] ** 2))
        asymmetry = abs(first_half_rms - second_half_rms) / (max(first_half_rms, second_half_rms) + 1e-10)

        if asymmetry > 0.5:
            return {
                "timestamp": round(cut_time, 2),
                "type": "breath_truncation",
                "severity": "low",
                "detail": f"Possible breath sound truncated at cut point (asymmetry: {asymmetry:.2f})",
                "suggestion": "Extend cut boundary to include full breath",
                "listen_range": [round(max(0, cut_time - 1.5), 1), round(cut_time + 1.5, 1)],
                "metric": round(asymmetry, 3),
            }
    return None


def analyze(audio_path, output_path=None):
    """Main analysis function."""
    print(f"Loading audio: {audio_path}")
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    duration = librosa.get_duration(y=y, sr=sr)
    print(f"Duration: {duration:.1f}s ({duration/60:.1f} min), Sample rate: {sr}Hz")

    # Auto-detect cut points
    print("Detecting cut points...")
    cut_points = detect_cut_points(y, sr)
    print(f"Detected {len(cut_points)} potential cut points")

    # Run all 5 checks against each cut point
    issues = []
    checks = [
        ("energy_jump", check_energy_jump),
        ("silence", check_silence),
        ("zcr", check_zcr_jump),
        ("spectral", check_spectral_jump),
        ("breath", check_breath_truncation),
    ]

    for i, cut_time in enumerate(cut_points):
        if (i + 1) % 10 == 0:
            print(f"  Analyzing cut point {i+1}/{len(cut_points)}...")
        for name, check_fn in checks:
            result = check_fn(y, sr, cut_time)
            if result:
                issues.append(result)

    # Dedupe (when one timestamp has multiple issues, keep the most severe)
    seen_times = {}
    severity_order = {"high": 3, "medium": 2, "low": 1}
    for issue in issues:
        t = issue["timestamp"]
        if t not in seen_times or severity_order.get(issue["severity"], 0) > severity_order.get(seen_times[t]["severity"], 0):
            seen_times[t] = issue
    issues = sorted(seen_times.values(), key=lambda x: x["timestamp"])

    # Compute the score
    high_count = sum(1 for i in issues if i["severity"] == "high")
    medium_count = sum(1 for i in issues if i["severity"] == "medium")
    low_count = sum(1 for i in issues if i["severity"] == "low")

    # Score formula: start from 10 and deduct
    deduction = high_count * 0.8 + medium_count * 0.3 + low_count * 0.1
    score = max(1.0, round(10.0 - deduction, 1))

    report = {
        "audio_file": str(Path(audio_path).name),
        "duration_seconds": round(duration, 1),
        "detected_cut_points": len(cut_points),
        "issues": issues,
        "signal_score": score,
        "summary": {
            "total_issues": len(issues),
            "high": high_count,
            "medium": medium_count,
            "low": low_count,
            "pass_rate": round((len(cut_points) - len(issues)) / max(len(cut_points), 1) * 100, 1),
        },
    }

    # Output
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nReport saved: {output_path}")

    # Print summary
    print(f"\n{'='*50}")
    print(f"Signal Analysis Report")
    print(f"{'='*50}")
    print(f"Audio: {report['audio_file']}")
    print(f"Duration: {duration/60:.1f} min")
    print(f"Cut points detected: {len(cut_points)}")
    print(f"Issues found: {len(issues)} (HIGH: {high_count}, MEDIUM: {medium_count}, LOW: {low_count})")
    print(f"Signal Score: {score} / 10")
    print(f"Pass Rate: {report['summary']['pass_rate']}%")

    if issues:
        print(f"\nIssues requiring attention:")
        for i, issue in enumerate(issues, 1):
            ts = issue["timestamp"]
            mins, secs = divmod(ts, 60)
            print(f"  {i}. [{int(mins):02d}:{secs:05.2f}] {issue['type']} ({issue['severity']}) — {issue['detail']}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Podcast edit signal analysis (Layer 1)")
    parser.add_argument("--input", "-i", required=True, help="Input audio file path")
    parser.add_argument("--output", "-o", help="Output JSON report path")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"Error: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    analyze(args.input, args.output)


if __name__ == "__main__":
    main()
