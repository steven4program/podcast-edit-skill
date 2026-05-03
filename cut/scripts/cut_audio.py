#!/usr/bin/env python3
"""
Step 8: one-shot final cut audio generator.

Usage: python3 cut_audio.py [output_name.mp3] [audio_file] [delete_segments.json]
       python3 cut_audio.py [output_name.mp3] [audio_file] [delete_segments.json] --speakers-json subtitles_words.json

Defaults:
  - output_name:     podcast_final_v1.mp3
  - audio_file:      ../1_transcript/audio.mp3
  - delete_segments: delete_segments.json

v4: optional speaker-loudness alignment — measures per-speaker average loudness and compensates
    differences (max +6 dB).
v3: adaptive fade-in/out at each cut point removes the choppy feel.
v2: decode to WAV first to ensure sample-accurate cuts (MP3 -c copy is only frame-accurate, ~26 ms).
"""

import json
import subprocess
import sys
import os
import re
from collections import defaultdict

# Force line buffering so progress is visible when piped.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


def calc_fade_duration(segment_duration):
    """
    Adaptive fade duration tied to segment length.

    Rules:
    - Very short segment (< 0.3s): no fade (would distort).
    - Short    (0.3 – 2s): fade = length × 8 %, min 0.03s.
    - Medium   (2 – 8s):    fade = 0.15 – 0.25s.
    - Long     (> 8s):      fade = 0.3s (cap).
    """
    if segment_duration < 0.3:
        return 0.0
    fade = min(segment_duration * 0.08, 0.3)
    return max(fade, 0.03)


MAX_GAIN_DB = 6.0  # cap on gain to avoid amplifying noise


def load_speaker_segments(speakers_json_path):
    """
    Extract per-speaker time segments from subtitles_words.json.

    Format:
    [
      {"text": "[Alice]", "start": 69.4, "end": 69.4, "isSpeakerLabel": true, "speaker": "Alice"},
      {"text": "hello",   "start": 69.5, "end": 69.7, "speaker": "Alice"},
      {"text": "",        "start": 70.5, "end": 71.2, "isGap": true},
      ...
    ]

    Returns: {speaker_name: [(start, end), ...]}
    """
    with open(speakers_json_path) as f:
        words = json.load(f)

    speaker_segments = defaultdict(list)
    current_speaker = None
    seg_start = None
    seg_end = None

    for w in words:
        if w.get('isGap') or w.get('isSpeakerLabel'):
            continue

        speaker = w.get('speaker')
        if not speaker:
            continue

        start = w.get('start', 0)
        end = w.get('end', 0)

        if speaker == current_speaker and seg_end is not None and start - seg_end < 1.0:
            # Same speaker, gap < 1s, extend segment
            seg_end = end
        else:
            # New speaker or gap too large — flush previous, start new
            if current_speaker and seg_start is not None:
                speaker_segments[current_speaker].append((seg_start, seg_end))
            current_speaker = speaker
            seg_start = start
            seg_end = end

    if current_speaker and seg_start is not None:
        speaker_segments[current_speaker].append((seg_start, seg_end))

    return dict(speaker_segments)


def detect_speaker_loudness(wav_file, speaker_segments):
    """
    Use ffmpeg's volumedetect to measure each speaker's mean_volume in dB.

    For each speaker, sample up to 30 segments (avoids long runtime on 2h podcasts),
    then run volumedetect once per speaker via aselect.

    Returns: {speaker_name: mean_volume_dB}
    """
    speaker_loudness = {}

    for speaker, segments in speaker_segments.items():
        # Sample up to 30 evenly distributed segments
        if len(segments) > 30:
            step = len(segments) / 30
            sampled = [segments[int(i * step)] for i in range(30)]
        else:
            sampled = segments

        # Filter out segments shorter than 0.3s — too short to measure reliably
        sampled = [(s, e) for s, e in sampled if e - s >= 0.3]

        if not sampled:
            continue

        # Combine all segments into a single ffmpeg filter expression
        select_parts = []
        for s, e in sampled:
            select_parts.append(f'between(t,{s:.3f},{e:.3f})')

        select_expr = '+'.join(select_parts)
        af = f"aselect='{select_expr}',aresample=async=1,volumedetect"

        cmd = [
            'ffmpeg', '-v', 'info',
            '-i', wav_file,
            '-af', af,
            '-f', 'null', '-'
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        stderr = result.stderr

        match = re.search(r'mean_volume:\s*([-\d.]+)\s*dB', stderr)
        if match:
            speaker_loudness[speaker] = float(match.group(1))

    return speaker_loudness


def calc_volume_compensation(speaker_loudness):
    """
    Use the loudest speaker as the 0 dB baseline; compute gain (in dB) needed by others.
    Caps gain at MAX_GAIN_DB.

    Returns: {speaker_name: gain_dB}
    """
    if not speaker_loudness:
        return {}

    max_vol = max(speaker_loudness.values())
    compensation = {}

    for speaker, vol in speaker_loudness.items():
        gain = max_vol - vol  # positive = needs boost
        gain = min(gain, MAX_GAIN_DB)
        # Differences below 0.5 dB are inaudible — skip
        compensation[speaker] = round(gain, 2) if gain >= 0.5 else 0.0

    return compensation


def get_segment_speaker(seg_start, seg_end, speaker_segments):
    """
    Decide which speaker a kept segment mostly belongs to by total overlap duration.
    """
    best_speaker = None
    best_overlap = 0

    for speaker, segments in speaker_segments.items():
        overlap = 0
        for s, e in segments:
            ov_start = max(seg_start, s)
            ov_end = min(seg_end, e)
            if ov_end > ov_start:
                overlap += ov_end - ov_start

        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = speaker

    return best_speaker


def main():
    # Argument parsing: positional + --speakers-json / --no-fade options
    positional_args = []
    speakers_json = None
    no_fade = False

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == '--speakers-json':
            if i + 1 < len(sys.argv):
                speakers_json = sys.argv[i + 1]
                i += 2
            else:
                print("--speakers-json requires a file path")
                sys.exit(1)
        elif sys.argv[i] == '--no-fade':
            no_fade = True
            i += 1
        else:
            positional_args.append(sys.argv[i])
            i += 1

    output_name = positional_args[0] if len(positional_args) > 0 else 'podcast_final_v1.mp3'
    audio_file = positional_args[1] if len(positional_args) > 1 else '../1_transcript/audio.mp3'
    delete_file = positional_args[2] if len(positional_args) > 2 else 'delete_segments.json'

    if not os.path.exists(audio_file):
        print(f"Audio file not found: {audio_file}")
        sys.exit(1)

    if not os.path.exists(delete_file):
        print(f"Delete-segments file not found: {delete_file}")
        sys.exit(1)

    if speakers_json and not os.path.exists(speakers_json):
        print(f"Speakers JSON not found: {speakers_json}")
        sys.exit(1)

    # Load delete segments (supports both new {segments: [...], editState: {...}} and legacy [...] format)
    with open(delete_file) as f:
        raw = json.load(f)
    delete_segs = raw['segments'] if isinstance(raw, dict) and 'segments' in raw else raw

    # Build keep segments
    keep_segs = []
    last_end = 0

    for seg in delete_segs:
        if seg['start'] > last_end:
            keep_segs.append((last_end, seg['start']))
        last_end = seg['end']

    # Total audio duration
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
         audio_file],
        capture_output=True, text=True
    )
    total_duration = float(result.stdout.strip())

    if last_end < total_duration:
        keep_segs.append((last_end, total_duration))

    print(f"📊 Cut stats:")
    print(f"   Keep segments:   {len(keep_segs)}")
    print(f"   Delete segments: {len(delete_segs)}")
    print(f"   Source duration: {int(total_duration // 60)}m {int(total_duration % 60)}s")
    print("")

    # Decode to WAV (sample-accurate cuts; MP3 -c copy is only frame-accurate, ~26ms)
    temp_wav = '_source_temp.wav'
    print("🔊 Decoding to WAV (for sample-accurate cuts)...")
    cmd = [
        'ffmpeg', '-v', 'quiet', '-stats',
        '-i', audio_file,
        '-c:a', 'pcm_s16le',
        '-y', temp_wav
    ]
    subprocess.run(cmd, check=True)
    wav_size_mb = os.path.getsize(temp_wav) / (1024 * 1024)
    print(f"   temp WAV: {wav_size_mb:.0f} MB")
    print("")

    # Speaker-loudness alignment (optional)
    speaker_compensation = {}
    speaker_segments_data = {}
    if speakers_json:
        print("🎙️ Analyzing speaker loudness...")
        speaker_segments_data = load_speaker_segments(speakers_json)
        print(f"   Detected {len(speaker_segments_data)} speakers: {', '.join(speaker_segments_data.keys())}")

        speaker_loudness = detect_speaker_loudness(temp_wav, speaker_segments_data)
        for spk, vol in speaker_loudness.items():
            print(f"   {spk}: mean volume {vol:.1f} dB")

        speaker_compensation = calc_volume_compensation(speaker_loudness)
        any_compensation = any(g > 0 for g in speaker_compensation.values())

        if any_compensation:
            print("   Compensation plan:")
            for spk, gain in speaker_compensation.items():
                if gain > 0:
                    print(f"     {spk}: +{gain:.1f} dB")
                else:
                    print(f"     {spk}: baseline (no boost)")
        else:
            print("   Speaker volume difference < 0.5 dB; no compensation needed")
        print("")

    # Extract keep segments from the WAV
    has_vol = speaker_compensation and any(g > 0 for g in speaker_compensation.values())
    if no_fade:
        print(f"🎬 Extracting keep segments (no fades{', plus speaker volume alignment' if has_vol else ''})...")
    elif has_vol:
        print("🎬 Extracting keep segments (adaptive fades + speaker volume alignment)...")
    else:
        print("🎬 Extracting keep segments (adaptive fades)...")
    segment_files = []
    fade_count = 0

    for i, (start, end) in enumerate(keep_segs):
        seg_dur = end - start
        output = f'segment_{i:04d}.wav'

        is_first = (i == 0)
        is_last = (i == len(keep_segs) - 1)

        if no_fade:
            # Tiny 3 ms fade prevents PCM-discontinuity clicks but does not eat audio
            fade_in_dur = 0.0 if is_first else 0.003
            fade_out_dur = 0.0 if is_last else 0.003
        else:
            fade_in_dur = 0.0 if is_first else calc_fade_duration(seg_dur)
            fade_out_dur = 0.0 if is_last else calc_fade_duration(seg_dur)

        # Safety: fade-in + fade-out cannot exceed 60% of segment length
        if fade_in_dur + fade_out_dur > seg_dur * 0.6:
            ratio = (seg_dur * 0.6) / (fade_in_dur + fade_out_dur)
            fade_in_dur *= ratio
            fade_out_dur *= ratio

        # Decide per-segment volume compensation
        vol_gain = 0.0
        if speaker_compensation and speaker_segments_data:
            seg_speaker = get_segment_speaker(start, end, speaker_segments_data)
            if seg_speaker:
                vol_gain = speaker_compensation.get(seg_speaker, 0.0)

        needs_fade = fade_in_dur > 0 or fade_out_dur > 0
        needs_filter = needs_fade or vol_gain > 0

        if needs_filter:
            filters = []
            if vol_gain > 0:
                filters.append(f'volume={vol_gain:.2f}dB')
            if fade_in_dur > 0:
                filters.append(f'afade=t=in:d={fade_in_dur:.3f}')
            if fade_out_dur > 0:
                fade_out_start = seg_dur - fade_out_dur
                filters.append(f'afade=t=out:st={fade_out_start:.3f}:d={fade_out_dur:.3f}')

            cmd = [
                'ffmpeg', '-v', 'quiet',
                '-ss', str(start),
                '-i', temp_wav,
                '-t', str(seg_dur),
                '-af', ','.join(filters),
                '-y', output
            ]
            if needs_fade:
                fade_count += 1
        else:
            # Direct copy — no fade, no volume compensation needed
            cmd = [
                'ffmpeg', '-v', 'quiet',
                '-i', temp_wav,
                '-ss', str(start),
                '-to', str(end),
                '-c', 'copy',
                '-y', output
            ]

        subprocess.run(cmd, check=True)
        segment_files.append(output)

        if (i + 1) % 50 == 0:
            print(f"   Extracted {i+1}/{len(keep_segs)} segments")

    print(f"✅ Extracted all {len(keep_segs)} segments; {fade_count} cut points received fades")
    print("")

    # Concatenate WAV segments
    print("🔗 Concatenating segments...")
    concat_file = 'concat_list.txt'
    with open(concat_file, 'w') as f:
        for seg_file in segment_files:
            f.write(f"file '{seg_file}'\n")

    temp_concat = '_concat_temp.wav'
    cmd = [
        'ffmpeg', '-v', 'quiet', '-stats',
        '-f', 'concat',
        '-safe', '0',
        '-i', concat_file,
        '-c', 'copy',
        '-y', temp_concat
    ]
    subprocess.run(cmd, check=True)

    # Probe source encoding to match output quality
    probe_result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=bit_rate,sample_rate,channels',
         '-of', 'default=noprint_wrappers=1', audio_file],
        capture_output=True, text=True
    )
    src_bitrate = 128000  # default
    src_sample_rate = None
    src_channels = None
    for line in probe_result.stdout.strip().split('\n'):
        if line.startswith('bit_rate=') and line.split('=')[1].strip().isdigit():
            src_bitrate = int(line.split('=')[1].strip())
        elif line.startswith('sample_rate=') and line.split('=')[1].strip().isdigit():
            src_sample_rate = int(line.split('=')[1].strip())
        elif line.startswith('channels=') and line.split('=')[1].strip().isdigit():
            src_channels = int(line.split('=')[1].strip())

    # MP3 bitrate: at least 128k, cap at 192k
    out_bitrate = max(src_bitrate // 1000, 128)
    out_bitrate = min(out_bitrate, 192)
    print(f"🔧 Encoding to MP3 (source: {src_bitrate//1000} kbps {src_sample_rate} Hz {src_channels} ch → output: {out_bitrate} kbps)...")

    cmd = [
        'ffmpeg', '-v', 'quiet', '-stats',
        '-i', temp_concat,
        '-c:a', 'libmp3lame', '-b:a', f'{out_bitrate}k',
    ]
    if src_sample_rate and src_sample_rate > 16000:
        cmd.extend(['-ar', str(src_sample_rate)])
    if src_channels and src_channels > 1:
        cmd.extend(['-ac', str(src_channels)])
    cmd.extend(['-y', output_name])
    subprocess.run(cmd, check=True)

    # Clean up temp files
    os.remove(temp_wav)
    os.remove(temp_concat)
    for seg_file in segment_files:
        os.remove(seg_file)
    os.remove(concat_file)

    print("")
    print(f"✅ Cut complete: {output_name}")
    print("")

    # Summary
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
         output_name],
        capture_output=True, text=True
    )
    final_duration = float(result.stdout.strip())

    original_min = int(total_duration // 60)
    final_min = int(final_duration // 60)
    saved_min = original_min - final_min

    print("📈 Cut summary:")
    print(f"   Source: {original_min} min")
    print(f"   Final:  {final_min} min")
    print(f"   Saved:  {saved_min} min ({saved_min/original_min*100:.1f}%)")


if __name__ == '__main__':
    main()
