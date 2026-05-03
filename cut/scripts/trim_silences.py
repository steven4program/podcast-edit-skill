#!/usr/bin/env python3
"""
成品silence裁剪 — 将audio中所有超threshold pause裁剪到目标duration。

Usage:
  python3 trim_silences.py input.mp3 [output.mp3] [--threshold 0.8] [--target 0.6] [--noise -30]

argument:
  input.mp3       inputaudio file
  output.mp3      outputfile（default: 在inputfile名后加 _trimmed）
  --threshold T   detectthreshold：超 T s silence会被裁剪（default: 0.8）
  --target T      目标duration：eachsegmentsilence裁剪到 T s（default: 0.6）
  --noise N       silencedetect噪声threshold dB（default: -30）

how it works:
  1. FFmpeg silencedetect 扫描所有超threshold silence segment
  2. eachsegmentsilencekeep target s（前后各 target/2 s），裁掉multi余部 min 
  3. use  atrim + concat 拼接所有keep segment
  4. encode为 MP3

典型场景:
  - cut_audio.py 出成品后，deletecontent前后 短silencemerge成长pause
  - 直接use 成品audio扫一遍比反推 delete_segments 更简single可靠
"""

import json
import subprocess
import sys
import os
import re

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


def detect_silences(audio_file, threshold, noise_db):
    """use  FFmpeg silencedetect 找出所有超 threshold  silence segment"""
    cmd = [
        'ffmpeg', '-i', audio_file,
        '-af', f'silencedetect=noise={noise_db}dB:d={threshold}',
        '-f', 'null', '-'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr

    silences = []
    for line in stderr.split('\n'):
        match = re.search(r'silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)', line)
        if match:
            end = float(match.group(1))
            dur = float(match.group(2))
            start = end - dur
            silences.append({'start': start, 'end': end, 'duration': dur})

    return silences


def get_duration(audio_file):
    """获取audiototal duration"""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
         audio_file],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def build_keep_segments(silences, total_duration, target):
    """根据silence segmentcalculatekeep segment（裁掉eachsegmentsilence中超 target  部 min ）"""
    half = target / 2.0

    trim_segments = []
    for s in silences:
        trim_start = s['start'] + half
        trim_end = s['end'] - half
        if trim_end > trim_start + 0.01:
            trim_segments.append((trim_start, trim_end))

    trim_segments.sort(key=lambda x: x[0])

    keep_segments = []
    cursor = 0
    for ts, te in trim_segments:
        if cursor < ts:
            keep_segments.append((cursor, ts))
        cursor = te
    if cursor < total_duration:
        keep_segments.append((cursor, total_duration))

    return keep_segments


def main():
    # Argument parsing
    positional = []
    threshold = 0.8
    target = 0.6
    noise_db = -30

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == '--threshold' and i + 1 < len(sys.argv):
            threshold = float(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == '--target' and i + 1 < len(sys.argv):
            target = float(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == '--noise' and i + 1 < len(sys.argv):
            noise_db = float(sys.argv[i + 1])
            i += 2
        else:
            positional.append(sys.argv[i])
            i += 1

    if not positional:
        print("Usage: python3 trim_silences.py input.mp3 [output.mp3] [--threshold 0.8] [--target 0.6] [--noise -30]")
        sys.exit(1)

    input_file = positional[0]
    if len(positional) > 1:
        output_file = positional[1]
    else:
        base, ext = os.path.splitext(input_file)
        output_file = f"{base}_trimmed{ext}"

    if not os.path.exists(input_file):
        print(f"not foundinputfile: {input_file}")
        sys.exit(1)

    # 1. detectsilence
    print(f"🔍 扫描 >{threshold}s  silence segment（noise={noise_db}dB）...")
    silences = detect_silences(input_file, threshold, noise_db)
    print(f"   detect到 {len(silences)} 个超 {threshold}s  pause")

    if not silences:
        print("✅ 无needs 裁剪")
        return

    #  min 布statistics
    bins = {'短': 0, '中': 0, '长': 0}
    total_excess = 0
    for s in silences:
        if s['duration'] < 1.0:
            bins['短'] += 1
        elif s['duration'] < 2.0:
            bins['中'] += 1
        else:
            bins['长'] += 1
        total_excess += s['duration'] - target

    print(f"    min 布: 短(<1s)={bins['短']}  中(1-2s)={bins['中']}  长(>2s)={bins['长']}")
    print(f"   总multi余silence: {total_excess:.1f}s")

    # 2. calculatekeep segment
    total_duration = get_duration(input_file)
    keep_segments = build_keep_segments(silences, total_duration, target)

    new_duration = sum(e - s for s, e in keep_segments)
    print(f"   source: {total_duration/60:.1f}min → 裁剪后: {new_duration/60:.1f}min")

    # 3. 构建 FFmpeg filter
    print(f"✂️  裁剪中...")

    filter_parts = []
    for i, (start, end) in enumerate(keep_segments):
        filter_parts.append(
            f'[0:a]atrim=start={start:.4f}:end={end:.4f},asetpts=N/SR/TB[p{i}]'
        )
    concat_inputs = ''.join(f'[p{i}]' for i in range(len(keep_segments)))
    filter_parts.append(f'{concat_inputs}concat=n={len(keep_segments)}:v=0:a=1[out]')

    filter_script = ';\n'.join(filter_parts)
    filter_file = '/tmp/_trim_silences_filter.txt'
    with open(filter_file, 'w') as f:
        f.write(filter_script)

    # 确保不write同一file
    temp_output = output_file
    same_file = os.path.abspath(input_file) == os.path.abspath(output_file)
    if same_file:
        base, ext = os.path.splitext(output_file)
        temp_output = f"{base}_tmp{ext}"

    # 探测源fileencodeargument，matchoutput质量
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=bit_rate,sample_rate,channels',
         '-of', 'default=noprint_wrappers=1', input_file],
        capture_output=True, text=True
    )
    src_bitrate = 128  # default kbps
    src_sample_rate = None
    src_channels = None
    for line in probe.stdout.strip().split('\n'):
        key, _, val = line.partition('=')
        if key == 'bit_rate' and val.strip().isdigit():
            src_bitrate = max(int(val.strip()) // 1000, 128)
        elif key == 'sample_rate' and val.strip().isdigit():
            src_sample_rate = int(val.strip())
        elif key == 'channels' and val.strip().isdigit():
            src_channels = int(val.strip())
    out_bitrate = min(src_bitrate, 192)

    cmd = [
        'ffmpeg', '-y',
        '-i', input_file,
        '-filter_complex_script', filter_file,
        '-map', '[out]',
        '-c:a', 'libmp3lame', '-b:a', f'{out_bitrate}k',
    ]
    if src_sample_rate and src_sample_rate > 16000:
        cmd.extend(['-ar', str(src_sample_rate)])
    if src_channels and src_channels > 1:
        cmd.extend(['-ac', str(src_channels)])
    cmd.append(temp_output)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"❌ FFmpeg Error:\n{result.stderr[-500:]}")
        sys.exit(1)

    if same_file:
        os.replace(temp_output, output_file)

    os.remove(filter_file)

    # 4. 验证
    final_duration = get_duration(output_file)
    saved = total_duration - final_duration
    print(f"✅ Complete: {output_file}")
    print(f"   {total_duration/60:.1f}min → {final_duration/60:.1f}min（saved {saved:.0f}s）")


if __name__ == '__main__':
    main()
