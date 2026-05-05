#!/usr/bin/env python3
"""
Waveform onset detection — search the energy valley near a given time point to
refine cut boundaries.

Usage:
  python3 refine_boundaries.py --audio <path> --points '<JSON>'
  python3 refine_boundaries.py --audio <path> --points-file <path.json>

Input JSON format:
  [
    {"time": 691.79, "search_window": 0.15, "direction": "both"},
    {"time": 45.32, "search_window": 0.10, "direction": "left"},
    ...
  ]

  - time: time point to refine (seconds)
  - search_window: half-width of the search window (seconds), default 0.15
  - direction: "left" = search left only, "right" = search right only, "both" = both directions (default)

Output JSON:
  [
    {"original": 691.79, "refined": 691.82, "confidence": 0.85, "energy_drop_db": 4.2},
    ...
  ]

How it works:
  1. Use FFmpeg to decode the target interval to raw PCM
  2. Compute the RMS energy envelope (5ms frames, 3-frame moving average)
  3. Find the deepest energy valley inside the search window
  4. The valley must be ≥3dB below the local mean to be considered a reliable syllable boundary
  5. If unmet, return the original time point (confidence=0, fall back to linear interpolation)
"""

import json
import subprocess
import sys
import os
import struct
import argparse

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ── Constants ──────────────────────────────────────────────
SAMPLE_RATE = 16000        # decode sample rate (16kHz is enough for energy analysis)
FRAME_MS = 5               # energy frame length (ms)
SMOOTH_FRAMES = 3          # moving-average window (frames)
MIN_DROP_DB = 3.0          # minimum valley depth (relative to local mean)
EXTRA_MARGIN = 0.05        # extra decode margin (seconds) to avoid boundary effects


def decode_segment(audio_path, start_sec, duration_sec):
    """
    Use FFmpeg to decode the given interval as 16kHz mono s16le PCM.
    Returns a numpy-like list of float samples.
    """
    cmd = [
        'ffmpeg', '-v', 'quiet',
        '-ss', f'{start_sec:.4f}',
        '-i', audio_path,
        '-t', f'{duration_sec:.4f}',
        '-ar', str(SAMPLE_RATE),
        '-ac', '1',
        '-f', 's16le',
        '-acodec', 'pcm_s16le',
        'pipe:1'
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"  ⚠️ FFmpeg 解碼失敗：start={start_sec:.4f}, dur={duration_sec:.4f}", file=sys.stderr)
        return []

    raw = result.stdout
    # decode s16le to float [-1, 1]
    n_samples = len(raw) // 2
    if n_samples == 0:
        return []
    samples = struct.unpack(f'<{n_samples}h', raw)
    return [s / 32768.0 for s in samples]


def compute_rms_envelope(samples, frame_size, smooth_n):
    """
    Compute the RMS energy envelope.

    Args:
        samples: list of float samples
        frame_size: samples per frame
        smooth_n: moving-average window

    Returns:
        list of (frame_center_sample_idx, rms_value)
    """
    import math

    frames = []
    for i in range(0, len(samples) - frame_size + 1, frame_size):
        chunk = samples[i:i + frame_size]
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        center = i + frame_size // 2
        frames.append((center, rms))

    if len(frames) < smooth_n:
        return frames

    # Apply moving-average smoothing
    smoothed = []
    half = smooth_n // 2
    for i in range(len(frames)):
        window_start = max(0, i - half)
        window_end = min(len(frames), i + half + 1)
        avg_rms = sum(f[1] for f in frames[window_start:window_end]) / (window_end - window_start)
        smoothed.append((frames[i][0], avg_rms))

    return smoothed


def find_energy_valley(envelope, search_start_idx, search_end_idx, direction, center_frame_idx=None):
    """
    Find the nearest qualifying valley (local minimum) in the given range of the energy envelope.

    Strategy: locate all local minima → keep those with depth ≥ MIN_DROP_DB → pick the nearest.
    If none qualify, return the deepest (with low confidence).

    Args:
        envelope: [(sample_idx, rms)] smoothed energy envelope
        search_start_idx: search start frame index
        search_end_idx: search end frame index
        direction: "left", "right", "both"
        center_frame_idx: frame index of the original time point (used to rank by "nearness")

    Returns:
        (valley_frame_idx, confidence, energy_drop_db) or (None, 0, 0)
    """
    import math

    if search_end_idx <= search_start_idx or search_end_idx > len(envelope):
        return None, 0, 0

    search_frames = envelope[search_start_idx:search_end_idx]
    if not search_frames:
        return None, 0, 0

    rms_values = [f[1] for f in search_frames]
    mean_rms = sum(rms_values) / len(rms_values)

    if mean_rms <= 1e-10:
        return None, 0, 0

    # Direction filter
    center_in_search = len(rms_values) // 2
    if direction == "left":
        active_range = range(0, center_in_search + 1)
    elif direction == "right":
        active_range = range(center_in_search, len(rms_values))
    else:
        active_range = range(0, len(rms_values))

    # Find all local minima (frames lower than both neighbours)
    local_mins = []
    for i in active_range:
        rms = rms_values[i]
        left_ok = (i == 0) or (rms <= rms_values[i - 1])
        right_ok = (i == len(rms_values) - 1) or (rms <= rms_values[i + 1])
        if left_ok and right_ok:
            if rms <= 1e-10:
                drop_db = 60.0
            else:
                drop_db = 20 * math.log10(mean_rms / rms)
            global_idx = search_start_idx + i
            dist = abs(global_idx - center_frame_idx) if center_frame_idx is not None else i
            local_mins.append((global_idx, drop_db, dist))

    if not local_mins:
        return None, 0, 0

    # Split into qualifying (≥ MIN_DROP_DB) and non-qualifying
    qualified = [m for m in local_mins if m[1] >= MIN_DROP_DB]

    if qualified:
        # Pick the nearest qualifying valley
        best = min(qualified, key=lambda m: m[2])
    else:
        # Nothing qualifies — pick the deepest (low confidence)
        best = max(local_mins, key=lambda m: m[1])

    valley_idx, drop_db, _ = best

    # Confidence
    if drop_db >= MIN_DROP_DB:
        confidence = min(1.0, 0.5 + (drop_db - MIN_DROP_DB) / (MIN_DROP_DB * 3))
    else:
        confidence = drop_db / MIN_DROP_DB * 0.4

    return valley_idx, confidence, drop_db


def refine_point(audio_path, point):
    """
    Refine a single time point.

    Args:
        audio_path: audio file path
        point: {"time": float, "search_window": float, "direction": str}

    Returns:
        {"original": float, "refined": float, "confidence": float, "energy_drop_db": float}
    """
    time = point["time"]
    search_window = point.get("search_window", 0.15)
    direction = point.get("direction", "both")

    # decode interval: time ± (search_window + margin)
    decode_start = max(0, time - search_window - EXTRA_MARGIN)
    decode_duration = (search_window + EXTRA_MARGIN) * 2

    samples = decode_segment(audio_path, decode_start, decode_duration)
    if not samples:
        return {"original": time, "refined": time, "confidence": 0, "energy_drop_db": 0}

    # Compute the energy envelope
    frame_size = SAMPLE_RATE * FRAME_MS // 1000  # 5ms @ 16kHz = 80 samples
    envelope = compute_rms_envelope(samples, frame_size, SMOOTH_FRAMES)

    if not envelope:
        return {"original": time, "refined": time, "confidence": 0, "energy_drop_db": 0}

    # Determine the search range (excluding the margin region)
    search_start_sample = int(EXTRA_MARGIN * SAMPLE_RATE)
    search_end_sample = int((EXTRA_MARGIN + search_window * 2) * SAMPLE_RATE)

    # Map to frame indices
    search_start_frame = 0
    search_end_frame = len(envelope)
    for i, (center, _) in enumerate(envelope):
        if center >= search_start_sample and search_start_frame == 0:
            search_start_frame = i
        if center >= search_end_sample:
            search_end_frame = i
            break

    # Frame index of the original time point (used to pick the nearest valley)
    center_sample = int((time - decode_start) * SAMPLE_RATE)
    center_frame = min(range(len(envelope)), key=lambda i: abs(envelope[i][0] - center_sample))

    # Find the valley (pick the nearest qualifying one)
    valley_frame_idx, confidence, drop_db = find_energy_valley(
        envelope, search_start_frame, search_end_frame, direction, center_frame
    )

    if valley_frame_idx is None or confidence < 0.3:
        return {
            "original": time,
            "refined": time,
            "confidence": round(confidence, 3),
            "energy_drop_db": round(drop_db, 2)
        }

    # Sample index of the valley frame → absolute time
    valley_sample = envelope[valley_frame_idx][0]
    refined_time = decode_start + valley_sample / SAMPLE_RATE

    return {
        "original": round(time, 4),
        "refined": round(refined_time, 4),
        "confidence": round(confidence, 3),
        "energy_drop_db": round(drop_db, 2)
    }


def main():
    parser = argparse.ArgumentParser(description='Waveform onset-detection refinement of cut boundaries')
    parser.add_argument('--audio', required=True, help='audio file path')
    parser.add_argument('--points', help='JSON string of time points to refine')
    parser.add_argument('--points-file', help='JSON file path of time points to refine')
    args = parser.parse_args()

    if not os.path.exists(args.audio):
        print(json.dumps({"error": f"audio file does not exist: {args.audio}"}))
        sys.exit(1)

    # Read time points
    if args.points_file:
        with open(args.points_file) as f:
            points = json.load(f)
    elif args.points:
        points = json.loads(args.points)
    else:
        print(json.dumps({"error": "必須提供 --points 或 --points-file"}))
        sys.exit(1)

    if not isinstance(points, list):
        points = [points]

    print(f"🔍 精修 {len(points)} 個切割點...", file=sys.stderr)

    results = []
    for i, pt in enumerate(points):
        result = refine_point(args.audio, pt)
        delta_ms = (result["refined"] - result["original"]) * 1000
        status = "✅" if result["confidence"] >= 0.5 else "⚠️"
        print(
            f"  {status} [{i+1}/{len(points)}] "
            f"{result['original']:.4f}s → {result['refined']:.4f}s "
            f"(Δ{delta_ms:+.1f}ms, conf={result['confidence']:.2f}, "
            f"drop={result['energy_drop_db']:.1f}dB)",
            file=sys.stderr
        )
        results.append(result)

    # Output JSON to stdout
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
