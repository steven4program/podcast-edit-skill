#!/usr/bin/env python3
"""
金segment + 背景音樂混合腳本

將高亮segment疊加在連續背景音樂上，人聲出現時音樂自動降低。
解決Issue：金no背景音樂聽起來突兀。

Usage:
  python3 mix_highlights_with_music.py \
    --theme theme_song.mp3 \
    --clips clip1.mp3 clip2.mp3 clip3.mp3 \
    --output intro_complete.wav \
    [--intro-dur 10]       片頭純音樂duration（default10s）
    [--gap-dur 5]          segment間渡duration（default5s）
    [--outro-dur 9]        尾聲渡到正文duration（default9s）
    [--music-vol 0.16]     人聲時背景音樂音量（default0.16=约8%聽感）
    [--voice-gain 2.0]     人聲gain倍數（default2.0）
    [--fade-transition 1.5] 音樂升降漸變duration（default1.5s）

output:
  intro_complete.wav - 完整片頭（連續音樂 + 人聲疊加）

how it works:
  1. calculatetotal duration and 各time點
  2. 創建連續背景音樂軌（volume expression 動態调音量）
  3. 逐個疊加人聲（amerge+pan，不use  amix！）
  4. 混合音樂軌 + 人聲軌
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile


def get_duration(filepath):
    """獲得audioduration"""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
         filepath],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def check_volume(filepath):
    """檢查audio音量，返回 max_volume (dB)"""
    result = subprocess.run(
        ['ffmpeg', '-i', filepath, '-af', 'volumedetect', '-f', 'null', '-'],
        capture_output=True, text=True
    )
    for line in result.stderr.split('\n'):
        if 'max_volume' in line:
            val = line.split('max_volume:')[1].strip().split(' ')[0]
            return float(val)
    return -999


def build_volume_expression(timeline, music_vol, fade_dur, gap_vol=1.0):
    """
    建立 volume=eval=frame  動態音量表達式。

    timeline: [(start, end, vol), ...] 各時segment 目標音量
    music_vol: 人聲時 背景音量 (0-1)
    fade_dur: 音量漸變duration (s)
    gap_vol: 渡segment（無人聲時） 音樂音量 (0-1)，default1.0
    """
    # sorttime線
    timeline.sort(key=lambda x: x[0])

    # 建立 if-else 嵌套表達式
    parts = []
    for i, (start, end, vol) in enumerate(timeline):
        if i == 0 and start > 0:
            # 片頭區域（start 之前）
            parts.append(f"if(lt(t,{start:.3f}),{gap_vol},")

        if vol < 1.0:
            # 人聲區域：漸入到低音量
            fade_in_end = start + fade_dur
            fade_out_start = end
            fade_out_end = end + fade_dur

            # 漸入低音量（從 gap_vol 漸變到 music_vol）
            parts.append(
                f"if(lt(t,{fade_in_end:.3f}),"
                f"{gap_vol}-(t-{start:.3f})/{fade_dur:.3f}*({gap_vol}-{music_vol}),"
            )
            # 保持低音量
            parts.append(
                f"if(lt(t,{fade_out_start:.3f}),{music_vol},"
            )
            # 漸出restore（從 music_vol 漸變回 gap_vol）
            parts.append(
                f"if(lt(t,{fade_out_end:.3f}),"
                f"{music_vol}+(t-{fade_out_start:.3f})/{fade_dur:.3f}*({gap_vol}-{music_vol}),"
            )
        else:
            parts.append(f"if(lt(t,{end:.3f}),{gap_vol},")

    # 最後一segment
    parts.append(f"{gap_vol}")
    # close所有括號
    parts.append(")" * (len(parts) - 1))

    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description='金segment + 背景音樂混合')
    parser.add_argument('--theme', required=True, help='主題曲filepath')
    parser.add_argument('--clips', nargs='+', required=True, help='高亮segmentfilepathcolumn表')
    parser.add_argument('--output', default='intro_complete.wav', help='outputfilepath')
    parser.add_argument('--intro-dur', type=float, default=10, help='片頭純音樂duration(s)')
    parser.add_argument('--gap-dur', type=float, default=5, help='segment間渡duration(s)')
    parser.add_argument('--outro-dur', type=float, default=9, help='尾聲渡到正文duration(s)')
    parser.add_argument('--music-vol', type=float, default=0.08, help='人聲時背景音樂音量(0-1)')
    parser.add_argument('--gap-vol', type=float, default=1.0, help='渡segment（無人聲）音樂音量(0-1)')
    parser.add_argument('--voice-gain', type=float, default=2.0, help='人聲gain倍數')
    parser.add_argument('--fade-transition', type=float, default=1.5, help='音樂升降漸變duration(s)')
    parser.add_argument('--theme-start', type=float, default=0, help='主題曲截取起點(s)')

    args = parser.parse_args()

    # 檢查file
    if not os.path.exists(args.theme):
        print(f"❌ not found主題曲: {args.theme}")
        sys.exit(1)
    for clip in args.clips:
        if not os.path.exists(clip):
            print(f"❌ not foundsegment: {clip}")
            sys.exit(1)

    # 獲得各segment duration
    clip_durations = []
    for clip in args.clips:
        dur = get_duration(clip)
        clip_durations.append(dur)
        print(f"   segment: {os.path.basename(clip)} ({dur:.1f}s)")

    # calculatetime線
    # 结構: [片頭音樂] [segment1+低音樂] [渡] [segment2+低音樂] [渡] ... [尾聲漸出]
    timeline = []  # (start_of_voice, end_of_voice, target_vol)
    cursor = args.intro_dur

    clip_positions = []  # each個segment在time軸上 位置 (ms)
    for i, dur in enumerate(clip_durations):
        clip_start = cursor
        clip_end = cursor + dur
        clip_positions.append(clip_start)

        # 人聲區域：音樂降低（從漸變Start到漸變End）
        timeline.append((clip_start - args.fade_transition, clip_end, args.music_vol))

        cursor = clip_end + args.gap_dur

    total_dur = cursor - args.gap_dur + args.outro_dur
    theme_dur = get_duration(args.theme)

    print(f"\n📊 time線:")
    print(f"   片頭音樂: 0 ~ {args.intro_dur:.1f}s")
    for i, (pos, dur) in enumerate(zip(clip_positions, clip_durations)):
        print(f"   segment{i+1}: {pos:.1f} ~ {pos+dur:.1f}s ({dur:.1f}s)")
        if i < len(clip_durations) - 1:
            gap_start = pos + dur
            print(f"   渡: {gap_start:.1f} ~ {gap_start + args.gap_dur:.1f}s")
    print(f"   尾聲: {cursor - args.gap_dur:.1f} ~ {total_dur:.1f}s")
    print(f"   total duration: {total_dur:.1f}s")

    if args.theme_start + total_dur > theme_dur:
        print(f"   ⚠️ 主題曲 ({theme_dur:.0f}s) 可能不夠長，將自動循環")

    work_dir = tempfile.mkdtemp(prefix='podcastcut_mix_')
    print(f"\n🔧 工作directory: {work_dir}")

    try:
        # ===== Step 1: 創建連續背景音樂軌 =====
        print("\n🎵 Step 1: 創建連續背景音樂軌...")

        vol_expr = build_volume_expression(timeline, args.music_vol, args.fade_transition, args.gap_vol)

        music_bed = os.path.join(work_dir, 'music_bed.wav')
        theme_end = args.theme_start + total_dur
        fade_out_start = total_dur - 3

        af_filter = (
            f"atrim=start={args.theme_start:.3f}:end={theme_end:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d=2,"
            f"afade=t=out:st={fade_out_start:.3f}:d=3,"
            f"volume=eval=frame:volume='{vol_expr}'"
        )

        cmd = [
            'ffmpeg', '-v', 'warning',
            '-i', args.theme,
            '-af', af_filter,
            '-c:a', 'pcm_s16le', '-ar', '44100', '-ac', '2',
            '-y', music_bed
        ]
        subprocess.run(cmd, check=True)

        # 檢查音量
        vol = check_volume(music_bed)
        print(f"   音樂軌: {total_dur:.1f}s, max_volume={vol:.1f}dB")
        if vol < -40:
            print(f"   ⚠️ 音樂軌音量太低 ({vol:.1f}dB)，可能是silence！檢查 --theme-start argument")

        # ===== Step 2: 創建人聲軌 =====
        print("\n🎤 Step 2: 創建人聲軌（amerge+pan 逐步疊加）...")

        # 2a. 創建silence基底
        silence = os.path.join(work_dir, 'silence.wav')
        cmd = [
            'ffmpeg', '-v', 'warning',
            '-f', 'lavfi', '-i', f'anullsrc=r=44100:cl=stereo',
            '-t', str(total_dur),
            '-c:a', 'pcm_s16le',
            '-y', silence
        ]
        subprocess.run(cmd, check=True)

        # 2b. 逐個疊加人聲
        current_base = silence
        for i, (clip, pos) in enumerate(zip(args.clips, clip_positions)):
            delay_ms = int(pos * 1000)
            step_out = os.path.join(work_dir, f'voice_step{i+1}.wav')

            cmd = [
                'ffmpeg', '-v', 'warning',
                '-i', current_base,
                '-i', clip,
                '-filter_complex',
                # alimiter after voice gain: voice-gain 2.0× can push peaks past 0 dBFS and clip.
                f"[1:a]volume={args.voice_gain},alimiter=limit=0.95,adelay={delay_ms}|{delay_ms},apad=whole_dur={total_dur:.3f}[v];"
                f"[0:a][v]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3[out]",
                '-map', '[out]',
                '-c:a', 'pcm_s16le',
                '-y', step_out
            ]
            subprocess.run(cmd, check=True)
            print(f"   疊加segment{i+1}: delay={delay_ms}ms, gain={args.voice_gain}x")
            current_base = step_out

        voice_track = current_base

        # ===== Step 3: 混合音樂 + 人聲 =====
        print("\n🔗 Step 3: 混合音樂軌 + 人聲軌...")

        cmd = [
            'ffmpeg', '-v', 'warning',
            '-i', music_bed,
            '-i', voice_track,
            '-filter_complex',
            # Final limiter on the merged music+voice — additive amerge+pan can exceed 0 dBFS
            # when both tracks peak together; -0.45 dBFS ceiling guarantees no clip in output.
            '[0:a][1:a]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3,alimiter=limit=0.95[out]',
            '-map', '[out]',
            '-c:a', 'pcm_s16le',
            '-y', args.output
        ]
        subprocess.run(cmd, check=True)

        # 最後檢查
        final_dur = get_duration(args.output)
        final_vol = check_volume(args.output)
        print(f"\n✅ Complete: {args.output}")
        print(f"   duration: {final_dur:.1f}s, max_volume={final_vol:.1f}dB")

        # outputtime線 JSON（供後續time戳偏移calculate）
        timeline_info = {
            'total_duration': round(total_dur, 3),
            'intro_music_end': args.intro_dur,
            'clips': [
                {
                    'file': os.path.basename(clip),
                    'start': round(pos, 3),
                    'end': round(pos + dur, 3),
                    'duration': round(dur, 3)
                }
                for clip, pos, dur in zip(args.clips, clip_positions, clip_durations)
            ],
            'outro_start': round(total_dur - args.outro_dur, 3)
        }
        timeline_path = args.output.replace('.wav', '_timeline.json').replace('.mp3', '_timeline.json')
        with open(timeline_path, 'w') as f:
            json.dump(timeline_info, f, indent=2, ensure_ascii=False)
        print(f"   time線: {timeline_path}")

    finally:
        # 清理臨時file
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
        print(f"\n🧹 已清理臨時file")


if __name__ == '__main__':
    main()
