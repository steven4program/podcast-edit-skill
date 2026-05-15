#!/usr/bin/env python3
"""
Stage 5 multitrack final cut: produce per-speaker solo MP3 + a balanced merged MP3.

For episodes recorded with one audio track per speaker (e.g. Riverside/Zencastr
multi-track export). Applies the SAME delete_segments to every track so the
output timelines stay aligned, then:

  - <speaker>_solo.mp3   : per-track cut + dynaudnorm + loudnorm to -16 LUFS
  - episode_merged.mp3   : measure each track's integrated LUFS, lift the
                           quieter tracks to the loudest track's level,
                           amix (no auto-normalization), then loudnorm to -16 LUFS.

Usage:
  python3 cut_audio_multitrack.py \
    --track "Hogan=source/hogan-5m.mp3" \
    --track "Ted=source/ted35-01-5m.mp3" \
    --delete-segments output/.../2_analysis/delete_segments.json \
    --output-dir       output/.../3_output

Notes:
  - All tracks are expected to share the same timeline (aligned at t=0). Use
    --offset "Speaker=seconds" to shift a track that starts late.
  - delete_segments.json is the canonical merged-rough+fine file produced by
    Stage 2/3. Same schema as cut_audio.py.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


def calc_fade_duration(segment_duration):
    if segment_duration < 0.3:
        return 0.0
    fade = min(segment_duration * 0.08, 0.3)
    return max(fade, 0.03)


def parse_kv_pairs(items, value_type=str):
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"Expected NAME=VALUE, got: {item}")
        k, v = item.split("=", 1)
        out[k.strip()] = value_type(v.strip())
    return out


def load_delete_segments(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "segments" in data:
        data = data["segments"]
    segs = []
    for s in data:
        start = float(s.get("start", s.get("from", 0)))
        end = float(s.get("end", s.get("to", 0)))
        if end > start:
            segs.append((start, end))
    segs.sort()
    return segs


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def build_keep_segments(delete_segs, total_duration, offset=0.0):
    """Invert delete_segments → keep_segments, in the track's local timeline."""
    shifted = [(max(0.0, s - offset), max(0.0, e - offset))
               for s, e in delete_segs if e > offset]
    shifted.sort()
    keeps = []
    cursor = 0.0
    for s, e in shifted:
        s = max(s, 0.0)
        e = min(e, total_duration)
        if s > cursor:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total_duration:
        keeps.append((cursor, total_duration))
    return [(s, e) for s, e in keeps if e - s > 0.01]


def decode_to_wav(input_path, out_wav):
    print(f"   Decoding {input_path} → {out_wav.name}")
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats", "-i", str(input_path),
         "-c:a", "pcm_s16le", "-y", str(out_wav)],
        check=True,
    )


def extract_keep_segments(src_wav, keep_segs, work_dir, prefix):
    """Cut keep_segs out of src_wav with adaptive fades; return list of segment file paths."""
    out_files = []
    for i, (start, end) in enumerate(keep_segs):
        seg_dur = end - start
        seg_path = work_dir / f"{prefix}_seg_{i:05d}.wav"

        is_first = (i == 0)
        is_last = (i == len(keep_segs) - 1)
        fade_in = 0.0 if is_first else calc_fade_duration(seg_dur)
        fade_out = 0.0 if is_last else calc_fade_duration(seg_dur)
        if fade_in + fade_out > seg_dur * 0.6:
            r = (seg_dur * 0.6) / (fade_in + fade_out)
            fade_in *= r
            fade_out *= r

        filters = []
        if fade_in > 0:
            filters.append(f"afade=t=in:d={fade_in:.3f}")
        if fade_out > 0:
            filters.append(f"afade=t=out:st={seg_dur - fade_out:.3f}:d={fade_out:.3f}")

        cmd = ["ffmpeg", "-v", "quiet",
               "-ss", f"{start:.6f}", "-i", str(src_wav),
               "-t", f"{seg_dur:.6f}"]
        if filters:
            cmd += ["-af", ",".join(filters)]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-y", str(seg_path)]
        subprocess.run(cmd, check=True)
        out_files.append(seg_path)
    return out_files


def concat_wavs(segment_files, out_wav):
    list_path = out_wav.parent / f"_concat_{out_wav.stem}.txt"
    with open(list_path, "w") as f:
        for s in segment_files:
            f.write(f"file '{s.as_posix()}'\n")
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats",
         "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", "-y", str(out_wav)],
        check=True,
    )
    list_path.unlink(missing_ok=True)


def measure_integrated_lufs(wav_path):
    """Run ebur128 to get integrated loudness (LUFS). Returns float or None."""
    result = subprocess.run(
        ["ffmpeg", "-i", str(wav_path), "-af", "ebur128=peak=true",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # ebur128 logs to stderr. Find the "I:" line in the Summary block.
    summary = result.stderr.split("Summary:")[-1] if "Summary:" in result.stderr else result.stderr
    match = re.search(r"I:\s*(-?\d+\.\d+)\s*LUFS", summary)
    if match:
        return float(match.group(1))
    return None


def encode_mp3_with_loudnorm(wav_in, mp3_out, bitrate_kbps=192, extra_gain_db=0.0):
    """dynaudnorm + optional gain + loudnorm to -16 LUFS → MP3."""
    filters = []
    if abs(extra_gain_db) > 0.01:
        filters.append(f"volume={extra_gain_db:.2f}dB")
        filters.append("alimiter=limit=0.95")
    filters.append("dynaudnorm=f=500:g=15:p=0.7")
    filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats",
         "-i", str(wav_in),
         "-af", ",".join(filters),
         "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
         "-y", str(mp3_out)],
        check=True,
    )


def loudnorm_to_wav(wav_in, wav_out):
    """dynaudnorm + loudnorm to -16 LUFS → WAV (so we can feed it to amix)."""
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats",
         "-i", str(wav_in),
         "-af", "dynaudnorm=f=500:g=15:p=0.7,loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:a", "pcm_s16le",
         "-y", str(wav_out)],
        check=True,
    )


def mix_tracks(track_wavs_with_gain, merged_out_mp3, bitrate_kbps=192,
               post_dynaudnorm=True):
    """
    Mix multiple WAVs with per-track volume gain, then loudnorm to -16 LUFS.

    track_wavs_with_gain: list of (wav_path, gain_db).
    Uses amix normalize=0 so we control the levels ourselves (avoids amix's
    default /N attenuation).

    post_dynaudnorm: when True (default, used by lift/none), runs dynaudnorm AFTER
      mixing for extra dynamics control. When False (used by equalize), skips the
      second dynaudnorm because each input track was already dynaudnorm'd during
      loudnorm_to_wav() — running it twice makes merged sound flatter than solo
      and breaks "solo MP3 ≈ that speaker as heard in merged" consistency.
    """
    inputs = []
    filter_parts = []
    labels = []
    for idx, (wav, gain_db) in enumerate(track_wavs_with_gain):
        inputs += ["-i", str(wav)]
        # Apply gain (can be 0) and feed into amix
        if abs(gain_db) > 0.01:
            filter_parts.append(
                f"[{idx}:a]volume={gain_db:.2f}dB,alimiter=limit=0.95[a{idx}]"
            )
        else:
            filter_parts.append(f"[{idx}:a]anull[a{idx}]")
        labels.append(f"[a{idx}]")

    n = len(track_wavs_with_gain)
    post_chain = (
        "dynaudnorm=f=500:g=15:p=0.7,loudnorm=I=-16:TP=-1.5:LRA=11"
        if post_dynaudnorm else
        "loudnorm=I=-16:TP=-1.5:LRA=11"
    )
    amix = (
        f"{''.join(labels)}amix=inputs={n}:normalize=0:duration=longest[mix];"
        f"[mix]{post_chain}[out]"
    )
    filter_complex = ";".join(filter_parts) + ";" + amix

    cmd = ["ffmpeg", "-v", "quiet", "-stats", *inputs,
           "-filter_complex", filter_complex,
           "-map", "[out]",
           "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
           "-y", str(merged_out_mp3)]
    subprocess.run(cmd, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--track", action="append", required=True,
                   help='Repeatable. Format: "Speaker=/path/to/audio.{mp3,wav,m4a}"')
    p.add_argument("--offset", action="append", default=[],
                   help='Optional timestamp offset (sec) per speaker, e.g. "Bob=0.25"')
    p.add_argument("--delete-segments", required=True,
                   help="Path to delete_segments.json (rough+fine merged).")
    p.add_argument("--output-dir", required=True,
                   help="Directory for solo + merged outputs.")
    p.add_argument("--bitrate", type=int, default=192,
                   help="MP3 bitrate kbps (default 192).")
    p.add_argument("--balance", choices=["equalize", "lift", "none"], default="equalize",
                   help=(
                       "Inter-track volume strategy for the merged mix.\n"
                       "  equalize (default): each track is loudnormed to -16 LUFS individually before mixing.\n"
                       "                      Strongest consistency — both speakers feel the same level.\n"
                       "                      Trade-off: the louder speaker's dynamics get compressed.\n"
                       "  lift             : measure each track's LUFS, lift quieter tracks up to the\n"
                       "                      loudest track's level (+12 dB cap), then mix.\n"
                       "                      Preserves the loudest speaker's natural dynamics.\n"
                       "  none             : just mix the raw cut WAVs + final loudnorm. Big level\n"
                       "                      differences stay big."
                   ))
    p.add_argument("--keep-intermediates", action="store_true",
                   help="Don't delete the per-track cut WAVs after encoding.")
    args = p.parse_args()

    tracks = parse_kv_pairs(args.track, str)
    offsets = parse_kv_pairs(args.offset, float)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = output_dir / "_multitrack_work"
    work_dir.mkdir(exist_ok=True)

    delete_segs = load_delete_segments(args.delete_segments)
    print(f"📄 Loaded {len(delete_segs)} delete segments from {args.delete_segments}")
    print(f"🎙️  Tracks: {list(tracks.keys())}")
    print("")

    per_track_cut_wavs = {}  # speaker -> Path (cut, no loudnorm yet)
    per_track_lufs = {}      # speaker -> float

    # --- Phase 1: per-track cut → cut WAV (no loudnorm) ---
    for speaker, audio_path in tracks.items():
        print(f"=== {speaker} ===")
        audio_path = Path(audio_path)
        duration = probe_duration(audio_path)
        offset = offsets.get(speaker, 0.0)
        keep_segs = build_keep_segments(delete_segs, duration, offset=offset)
        print(f"   Duration {duration:.2f}s, offset {offset:.2f}s → {len(keep_segs)} keep segments")

        raw_wav = work_dir / f"{speaker}_raw.wav"
        cut_wav = work_dir / f"{speaker}_cut.wav"
        decode_to_wav(audio_path, raw_wav)

        seg_files = extract_keep_segments(raw_wav, keep_segs, work_dir, speaker)
        concat_wavs(seg_files, cut_wav)
        for sf in seg_files:
            sf.unlink(missing_ok=True)
        raw_wav.unlink(missing_ok=True)

        lufs = measure_integrated_lufs(cut_wav)
        per_track_lufs[speaker] = lufs
        per_track_cut_wavs[speaker] = cut_wav
        print(f"   Integrated loudness: {lufs} LUFS")
        print("")

    # --- Phase 2: per-track loudnorm (WAV) → solo MP3 ---
    print(f"🎚️  Balance mode: {args.balance}")
    per_track_loudnormed_wavs = {}
    if args.balance == "equalize":
        print("   Loudnorming each track to -16 LUFS (WAV) before mix...")
        for speaker, cut_wav in per_track_cut_wavs.items():
            ln_wav = work_dir / f"{speaker}_loudnormed.wav"
            loudnorm_to_wav(cut_wav, ln_wav)
            per_track_loudnormed_wavs[speaker] = ln_wav
            print(f"   ✅ {speaker} → {ln_wav.name}")
    print("")

    print("🎚️  Encoding per-speaker solo MP3s (loudnorm -16 LUFS each)...")
    for speaker, cut_wav in per_track_cut_wavs.items():
        solo_path = output_dir / f"{speaker}_solo.mp3"
        if args.balance == "equalize":
            # Re-encode the already-loudnormed WAV (avoids double-loudnorm).
            subprocess.run(
                ["ffmpeg", "-v", "quiet", "-stats",
                 "-i", str(per_track_loudnormed_wavs[speaker]),
                 "-c:a", "libmp3lame", "-b:a", f"{args.bitrate}k",
                 "-y", str(solo_path)],
                check=True,
            )
        else:
            encode_mp3_with_loudnorm(cut_wav, solo_path, bitrate_kbps=args.bitrate)
        print(f"   ✅ {solo_path}")
    print("")

    # --- Phase 3: merged ---
    merged_path = output_dir / "episode_merged.mp3"

    if args.balance == "equalize":
        print("🎛️  Mixing pre-equalized tracks (every speaker at -16 LUFS)...")
        # All tracks already at -16 LUFS → mix with normalize=0, then mild final
        # loudnorm to clean up the post-sum level (2 tracks at -16 LUFS sum to ~-13 LUFS).
        track_inputs = [(per_track_loudnormed_wavs[s], 0.0) for s in per_track_cut_wavs]
    elif args.balance == "lift":
        print("🎛️  Computing inter-track gain (lift quieter tracks to loudest)...")
        valid_lufs = {s: l for s, l in per_track_lufs.items() if l is not None and l > -70}
        if not valid_lufs:
            print("⚠️  Could not measure LUFS on any track; mixing without inter-track gain.")
            gains = {s: 0.0 for s in per_track_cut_wavs}
        else:
            max_lufs = max(valid_lufs.values())
            gains = {}
            for speaker in per_track_cut_wavs:
                track_lufs = per_track_lufs.get(speaker)
                if track_lufs is None or track_lufs <= -70:
                    gains[speaker] = 0.0
                    print(f"   {speaker}: LUFS unavailable, gain 0 dB")
                else:
                    delta = max_lufs - track_lufs
                    gain = min(delta, 12.0)
                    gains[speaker] = gain
                    print(f"   {speaker}: {track_lufs:.1f} LUFS → +{gain:.2f} dB (target {max_lufs:.1f})")
        track_inputs = [(per_track_cut_wavs[s], gains[s]) for s in per_track_cut_wavs]
    else:  # none
        print("🎛️  No inter-track balance; mixing raw cut WAVs.")
        track_inputs = [(per_track_cut_wavs[s], 0.0) for s in per_track_cut_wavs]

    # In equalize mode, each track was already dynaudnorm'd during loudnorm_to_wav.
    # Skipping the post-mix dynaudnorm keeps "solo MP3 ≈ that speaker in merged".
    skip_post_dyn = (args.balance == "equalize")
    chain_desc = "loudnorm only" if skip_post_dyn else "dynaudnorm + loudnorm"
    print(f"🎚️  Mixing → {merged_path.name} (amix normalize=0 + final {chain_desc} -16 LUFS)...")
    mix_tracks(track_inputs, merged_path, bitrate_kbps=args.bitrate,
               post_dynaudnorm=not skip_post_dyn)
    print(f"   ✅ {merged_path}")
    print("")

    # --- Cleanup ---
    if not args.keep_intermediates:
        for w in per_track_cut_wavs.values():
            w.unlink(missing_ok=True)
        for w in per_track_loudnormed_wavs.values():
            w.unlink(missing_ok=True)
        try:
            work_dir.rmdir()
        except OSError:
            pass
    else:
        print(f"📁 Intermediates kept in {work_dir}")

    # --- Summary ---
    print("=" * 50)
    print("Done.")
    for speaker in per_track_cut_wavs:
        print(f"  {output_dir / f'{speaker}_solo.mp3'}")
    print(f"  {merged_path}")


if __name__ == "__main__":
    main()
