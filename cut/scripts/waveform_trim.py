#!/usr/bin/env python3
"""
波形引导边界校准 — use  RMS 能量pack络精修 delete_segments   start/end。

背景：
  ASR time戳 and real实声学边界有 100-200ms 偏差，导致剪后audio有 filler 残留。
  人在剪映看波形一刀就能剪准。本工具模拟这个程：
  decodesourceaudio → calculate能量pack络 → 自动找到 filler  real实起止点。

Usage:
  python3 waveform_trim.py <audio_original> <delete_segments.json> [subtitles_words.json]

  optionalargument:
    --output <path>       output校准后  JSON（default: delete_segments_waveform.json）
    --diag-dir <path>     诊断图outputdirectory（default: waveform_diag/）
    --no-diag             不generate诊断图
    --max-duration <sec>  只校准短于此duration segment（default: 2.0s）
    --max-expand <ms>     最大扩展量（default: 200ms）
    --threshold <mult>    能量threshold = 底噪 × mult（default: 2.0）

input: audio_original.* + delete_segments.json（sourceaudiotime戳）
output: delete_segments_waveform.json + waveform_diag/seg_XXX.png

Key design：直接操作sourceaudio（不是剪后audio），避免 MP3 PCM 偏移Issue。
"""

import json
import subprocess
import sys
import os
import struct
import math
import argparse
import glob

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ── constant ──────────────────────────────────────────────
SAMPLE_RATE = 16000       # decode采样率（16kHz 足够do能量analyze）
FRAME_MS = 1              # 1ms frame（比 refine_boundaries   5ms 更精细）
SMOOTH_FRAMES = 5         # 5 frame滑动平均
CONTEXT_SEC = 0.5         # seg 前后decoderange
MAX_EXPAND_MS = 200       # 最大扩展量
THRESHOLD_MULT = 2.0      # 能量threshold = 底噪 × mult
MAX_SEG_DURATION = 2.0    # 只校准短于此duration segment


def decode_segment(audio_path, start_sec, duration_sec):
    """use  FFmpeg decode指定interval为 16kHz mono s16le PCM，返回 int16 column表。"""
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
        return []
    raw = result.stdout
    n_samples = len(raw) // 2
    if n_samples == 0:
        return []
    return list(struct.unpack(f'<{n_samples}h', raw))


def compute_rms_envelope(samples, frame_ms=FRAME_MS, smooth_n=SMOOTH_FRAMES):
    """
    calculate RMS 能量pack络。返回 [(center_sample_idx, rms_value), ...]。
    frame_ms: frame长（毫s），smooth_n: 滑动平均窗口。
    """
    frame_size = SAMPLE_RATE * frame_ms // 1000
    frames = []
    for i in range(0, len(samples) - frame_size + 1, frame_size):
        chunk = samples[i:i + frame_size]
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        frames.append((i + frame_size // 2, rms))

    if len(frames) < smooth_n:
        return frames

    # 滑动平均
    half = smooth_n // 2
    smoothed = []
    for i in range(len(frames)):
        ws = max(0, i - half)
        we = min(len(frames), i + half + 1)
        avg = sum(f[1] for f in frames[ws:we]) / (we - ws)
        smoothed.append((frames[i][0], avg))
    return smoothed


def estimate_noise_floor(envelope, seg_start_sample, seg_end_sample, margin_samples):
    """
    估算底噪：取 seg 外围 margin range内  RMS 中位数。
     min 别取 seg 前 and  seg 后 安静区域。
    """
    before_vals = [rms for (idx, rms) in envelope
                   if idx < seg_start_sample and idx >= seg_start_sample - margin_samples]
    after_vals = [rms for (idx, rms) in envelope
                  if idx > seg_end_sample and idx <= seg_end_sample + margin_samples]

    quiet_vals = before_vals + after_vals
    if not quiet_vals:
        # fallback: 取整segment最低 10%
        all_rms = sorted([rms for (_, rms) in envelope])
        n10 = max(1, len(all_rms) // 10)
        quiet_vals = all_rms[:n10]

    quiet_vals.sort()
    return quiet_vals[len(quiet_vals) // 2]  # 中位数


def calibrate_segment(audio_path, seg, seg_idx, prev_end, next_start,
                      max_expand_ms, threshold_mult):
    """
    校准single个 delete segment  边界。

    返回: {
        "original": {"start": float, "end": float},
        "calibrated": {"start": float, "end": float},
        "delta_start_ms": float,
        "delta_end_ms": float,
        "noise_floor": float,
        "threshold": float,
        "confidence": str,  # "high" / "medium" / "low"
        "envelope_data": [...],  # for diagnostic chart
        "decode_start": float,
    }
    """
    seg_start = seg['start']
    seg_end = seg['end']
    seg_dur = seg_end - seg_start

    # decode：seg 前后各 CONTEXT_SEC
    decode_start = max(0, seg_start - CONTEXT_SEC)
    decode_end = seg_end + CONTEXT_SEC
    decode_dur = decode_end - decode_start

    samples = decode_segment(audio_path, decode_start, decode_dur)
    if not samples:
        return None

    # calculate能量pack络
    envelope = compute_rms_envelope(samples)
    if not envelope:
        return None

    # seg 边界在 PCM 中 位置
    seg_start_sample = int((seg_start - decode_start) * SAMPLE_RATE)
    seg_end_sample = int((seg_end - decode_start) * SAMPLE_RATE)

    # 底噪估算
    margin_samples = int(0.1 * SAMPLE_RATE)  # 100ms
    noise_floor = estimate_noise_floor(envelope, seg_start_sample, seg_end_sample, margin_samples)
    threshold = noise_floor * threshold_mult

    max_expand_samples = int(max_expand_ms / 1000 * SAMPLE_RATE)

    # ── 校准 start（向前扩展，找能量降到threshold以下 点）──
    new_start_sample = seg_start_sample
    # 从 seg_start 向前search
    search_start = max(0, seg_start_sample - max_expand_samples)
    candidates = [(idx, rms) for (idx, rms) in envelope
                  if search_start <= idx < seg_start_sample]
    # 从 seg_start 向前走，找最后一个低于threshold frame
    for idx, rms in reversed(candidates):
        if rms <= threshold:
            new_start_sample = idx
            break

    # 安full约束：不能侵入前一个 keep segment
    prev_end_sample = int((prev_end - decode_start) * SAMPLE_RATE) if prev_end is not None else 0
    new_start_sample = max(new_start_sample, prev_end_sample)

    # ── 校准 end（向后扩展）──
    new_end_sample = seg_end_sample
    search_end = min(len(samples), seg_end_sample + max_expand_samples)
    candidates = [(idx, rms) for (idx, rms) in envelope
                  if seg_end_sample < idx <= search_end]
    # 从 seg_end 向后走，找最后一个低于threshold frame
    for idx, rms in candidates:
        if rms <= threshold:
            new_end_sample = idx
            break

    # 安full约束：不能侵入后一个 keep segment
    if next_start is not None:
        next_start_sample = int((next_start - decode_start) * SAMPLE_RATE)
        new_end_sample = min(new_end_sample, next_start_sample)

    # 转回绝OKtime
    new_start = decode_start + new_start_sample / SAMPLE_RATE
    new_end = decode_start + new_end_sample / SAMPLE_RATE

    delta_start_ms = (new_start - seg_start) * 1000
    delta_end_ms = (new_end - seg_end) * 1000

    # 信心度判断
    if abs(delta_start_ms) < 5 and abs(delta_end_ms) < 5:
        confidence = "unchanged"
    elif noise_floor < 50:
        confidence = "high"  # 安静环境，边界detect准
    elif noise_floor < 200:
        confidence = "medium"
    else:
        confidence = "low"  # 嘈杂环境

    return {
        "original": {"start": round(seg_start, 4), "end": round(seg_end, 4)},
        "calibrated": {"start": round(new_start, 4), "end": round(new_end, 4)},
        "delta_start_ms": round(delta_start_ms, 1),
        "delta_end_ms": round(delta_end_ms, 1),
        "noise_floor": round(noise_floor, 1),
        "threshold": round(threshold, 1),
        "confidence": confidence,
        "envelope_data": envelope,
        "decode_start": decode_start,
        "samples": samples,
    }


def generate_diagnostic(result, seg_idx, diag_dir):
    """generatesingle个 segment  波形诊断 PNG。"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  ⚠️ matplotlib 未安装，跳诊断图", file=sys.stderr)
        return

    envelope = result['envelope_data']
    decode_start = result['decode_start']
    orig = result['original']
    cal = result['calibrated']
    samples = result['samples']

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 6), sharex=True)

    # 波形
    t_wave = [decode_start + i / SAMPLE_RATE for i in range(len(samples))]
    ax1.plot(t_wave, samples, color='steelblue', linewidth=0.2)
    ax1.axvspan(orig['start'], orig['end'], color='red', alpha=0.1, label='Original delete')
    ax1.axvspan(cal['start'], cal['end'], color='green', alpha=0.1, label='Calibrated delete')
    ax1.axvline(orig['start'], color='red', linestyle='--', linewidth=0.8)
    ax1.axvline(orig['end'], color='red', linestyle='--', linewidth=0.8)
    ax1.axvline(cal['start'], color='green', linestyle='-', linewidth=1.2)
    ax1.axvline(cal['end'], color='green', linestyle='-', linewidth=1.2)
    ax1.set_ylabel('Amplitude')
    ax1.set_title(f'Seg {seg_idx}: {orig["start"]:.3f}-{orig["end"]:.3f}s '
                  f'(delta: {result["delta_start_ms"]:+.0f}ms / {result["delta_end_ms"]:+.0f}ms, '
                  f'{result["confidence"]})')
    ax1.legend(fontsize=8, loc='upper right')

    # RMS 能量
    t_rms = [decode_start + idx / SAMPLE_RATE for (idx, _) in envelope]
    rms_vals = [rms for (_, rms) in envelope]
    ax2.plot(t_rms, rms_vals, color='blue', linewidth=0.6)
    ax2.axhline(result['noise_floor'], color='gray', linestyle=':', linewidth=0.8,
                label=f'Noise floor {result["noise_floor"]:.0f}')
    ax2.axhline(result['threshold'], color='red', linestyle=':', linewidth=0.8,
                label=f'Threshold {result["threshold"]:.0f}')
    ax2.axvspan(cal['start'], cal['end'], color='green', alpha=0.1)
    ax2.axvline(cal['start'], color='green', linestyle='-', linewidth=1.2)
    ax2.axvline(cal['end'], color='green', linestyle='-', linewidth=1.2)
    ax2.set_xlabel('Time (seconds)')
    ax2.set_ylabel('RMS Energy')
    ax2.legend(fontsize=8, loc='upper right')

    plt.tight_layout()
    os.makedirs(diag_dir, exist_ok=True)
    path = os.path.join(diag_dir, f'seg_{seg_idx:03d}.png')
    plt.savefig(path, dpi=120)
    plt.close()
    return path


def find_audio_original(base_dir):
    """自动find audio_original.* file（SKILL.md 规定 命名规范）。"""
    pattern = os.path.join(base_dir, '1_transcript', 'audio_original.*')
    matches = glob.glob(pattern)
    audio_exts = {'.mp3', '.m4a', '.wav', '.aac', '.flac', '.ogg'}
    matches = [m for m in matches if os.path.splitext(m)[1].lower() in audio_exts]
    if matches:
        return matches[0]
    # fallback: audio.mp3（低质量，会打印Warning）
    fallback = os.path.join(base_dir, '1_transcript', 'audio.mp3')
    if os.path.exists(fallback):
        print(f"⚠️ not found audio_original.*，回退到 audio.mp3（音质会降低）", file=sys.stderr)
        return fallback
    return None


def main():
    parser = argparse.ArgumentParser(
        description='波形引导边界校准 — 精修 delete_segments   start/end')
    parser.add_argument('audio', nargs='?', help='sourceaudio path（推荐 audio_original.*）')
    parser.add_argument('delete_segments', nargs='?', default='delete_segments.json',
                        help='delete_segments JSON file')
    parser.add_argument('words_json', nargs='?', default=None,
                        help='subtitles_words.json（optional，use 于word边界约束）')
    parser.add_argument('--output', default='delete_segments_waveform.json',
                        help='outputfilepath')
    parser.add_argument('--diag-dir', default='waveform_diag',
                        help='诊断图directory')
    parser.add_argument('--no-diag', action='store_true',
                        help='不generate诊断图')
    parser.add_argument('--max-duration', type=float, default=MAX_SEG_DURATION,
                        help=f'只校准短于此duration segment（default {MAX_SEG_DURATION}s）')
    parser.add_argument('--max-expand', type=float, default=MAX_EXPAND_MS,
                        help=f'最大扩展量 ms（default {MAX_EXPAND_MS}ms）')
    parser.add_argument('--threshold', type=float, default=THRESHOLD_MULT,
                        help=f'threshold倍数（default {THRESHOLD_MULT}）')
    args = parser.parse_args()

    # 自动findaudio
    audio_path = args.audio
    if audio_path is None:
        audio_path = find_audio_original('.')
        if audio_path is None:
            print("❌ not foundaudio file。Please 指定path or 确保 1_transcript/audio_original.* exists。")
            sys.exit(1)
    if not os.path.exists(audio_path):
        print(f"❌ audio file does not exist: {audio_path}")
        sys.exit(1)

    # read delete_segments
    if not os.path.exists(args.delete_segments):
        print(f"❌ not found: {args.delete_segments}")
        sys.exit(1)
    with open(args.delete_segments) as f:
        raw = json.load(f)
    segments = raw['segments'] if isinstance(raw, dict) and 'segments' in raw else raw
    edit_state = raw.get('editState', {}) if isinstance(raw, dict) else {}

    print(f"🔍 波形校准 delete_segments ({len(segments)} segment，audio: {audio_path})")
    print(f"   threshold: 底噪×{args.threshold}, 最大扩展: {args.max_expand}ms, "
          f"校准segment < {args.max_duration}s")

    # 校准each个 segment
    calibrated_segments = []
    stats = {"calibrated": 0, "skipped_long": 0, "skipped_error": 0, "unchanged": 0}

    for i, seg in enumerate(segments):
        seg_dur = seg['end'] - seg['start']

        # 跳长segment
        if seg_dur > args.max_duration:
            calibrated_segments.append(seg)
            stats["skipped_long"] += 1
            continue

        # 前后segment边界（安full约束）
        prev_end = segments[i - 1]['end'] if i > 0 else None
        next_start = segments[i + 1]['start'] if i < len(segments) - 1 else None

        result = calibrate_segment(
            audio_path, seg, i, prev_end, next_start,
            args.max_expand, args.threshold
        )

        if result is None:
            calibrated_segments.append(seg)
            stats["skipped_error"] += 1
            continue

        cal = result['calibrated']
        calibrated_segments.append({"start": cal['start'], "end": cal['end']})

        if result['confidence'] == 'unchanged':
            stats['unchanged'] += 1
            marker = "="
        else:
            stats['calibrated'] += 1
            marker = "✅" if result['confidence'] in ('high', 'medium') else "⚠️"

        # 只打印有变化 
        if result['delta_start_ms'] != 0 or result['delta_end_ms'] != 0:
            print(f"  {marker} Seg{i:3d}: {seg['start']:.3f}-{seg['end']:.3f}s "
                  f"→ {cal['start']:.3f}-{cal['end']:.3f}s "
                  f"(Δstart={result['delta_start_ms']:+.0f}ms, "
                  f"Δend={result['delta_end_ms']:+.0f}ms, "
                  f"noise={result['noise_floor']:.0f}, {result['confidence']})")

        # generate诊断图
        if not args.no_diag and result['confidence'] != 'unchanged':
            diag_path = generate_diagnostic(result, i, args.diag_dir)
            if diag_path:
                print(f"       📊 {diag_path}")

    # output校准后  JSON
    output_data = {"segments": calibrated_segments}
    if edit_state:
        output_data["editState"] = edit_state
    with open(args.output, 'w') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"\n📊 statistics:")
    print(f"   校准: {stats['calibrated']}  未变: {stats['unchanged']}  "
          f"跳(长segment): {stats['skipped_long']}  跳(Error): {stats['skipped_error']}")
    print(f"✅ output: {args.output}")


if __name__ == '__main__':
    main()
