#!/usr/bin/env python3
"""
Stage 2.1 multitrack audio prep: produce a *balanced* mixed mp3 for the review page,
so the user审稿 hears what the final episode_merged will sound like.

Reuses the inter-track balance logic from cut_audio_multitrack.py, but operates on
the UNCUT raw tracks (no delete_segments needed) and outputs:

  - <output-dir>/audio.mp3                : 16 kHz mono, transcription input (balanced mix)
  - <output-dir>/audio_seekable.mp3       : CBR 64k, review-page Final-cut player (balanced)
  - <output-dir>/audio_seekable_raw.mp3   : CBR 64k, review-page Source player
                                            (raw amix, NO per-track loudnorm — so the user
                                            can A/B against the balanced version)

Balance modes are identical to cut_audio_multitrack.py:
  equalize (default) | lift | none

Usage:
  python prep_review_audio_multitrack.py \
    --track "Hogan=source/hogan.mp3" \
    --track "Ted=source/ted.mp3" \
    --output-dir output/.../1_transcript \
    --balance equalize
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


def parse_kv(items, value_type=str):
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"Expected NAME=VALUE, got: {item}")
        k, v = item.split("=", 1)
        out[k.strip()] = value_type(v.strip())
    return out


def measure_lufs(path):
    r = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    summary = r.stderr.split("Summary:")[-1] if "Summary:" in r.stderr else r.stderr
    m = re.search(r"I:\s*(-?\d+\.\d+)\s*LUFS", summary)
    return float(m.group(1)) if m else None


def loudnorm_to_wav(src, dst):
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats", "-i", str(src),
         "-af", "dynaudnorm=f=500:g=15:p=0.7,loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:a", "pcm_s16le", "-y", str(dst)],
        check=True,
    )


def decode_to_wav(src, dst, offset=0.0):
    cmd = ["ffmpeg", "-v", "quiet", "-stats"]
    if offset > 0:
        # delay the track by `offset` seconds (matches cut_audio_multitrack offset semantics)
        cmd += ["-i", str(src),
                "-af", f"adelay={int(offset * 1000)}|{int(offset * 1000)}",
                "-c:a", "pcm_s16le", "-y", str(dst)]
    else:
        cmd += ["-i", str(src), "-c:a", "pcm_s16le", "-y", str(dst)]
    subprocess.run(cmd, check=True)


def mix_tracks(track_wavs_with_gain, out_wav):
    inputs, parts, labels = [], [], []
    for i, (wav, gain_db) in enumerate(track_wavs_with_gain):
        inputs += ["-i", str(wav)]
        if abs(gain_db) > 0.01:
            parts.append(f"[{i}:a]volume={gain_db:.2f}dB,alimiter=limit=0.95[a{i}]")
        else:
            parts.append(f"[{i}:a]anull[a{i}]")
        labels.append(f"[a{i}]")
    n = len(track_wavs_with_gain)
    amix = (
        f"{''.join(labels)}amix=inputs={n}:normalize=0:duration=longest[mix];"
        f"[mix]dynaudnorm=f=500:g=15:p=0.7,loudnorm=I=-16:TP=-1.5:LRA=11[out]"
    )
    fc = ";".join(parts) + ";" + amix
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats", *inputs,
         "-filter_complex", fc, "-map", "[out]",
         "-c:a", "pcm_s16le", "-y", str(out_wav)],
        check=True,
    )


def encode_audio_mp3(src_wav, out_mp3):
    """16 kHz mono mp3, transcription input."""
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats", "-i", str(src_wav),
         "-vn", "-acodec", "libmp3lame", "-ar", "16000", "-ac", "1",
         "-y", str(out_mp3)],
        check=True,
    )


def encode_seekable_mp3(src_mp3, out_mp3):
    """CBR 64k + Xing header → precise browser seek."""
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats", "-i", str(src_mp3),
         "-c:a", "libmp3lame", "-b:a", "64k", "-write_xing", "1",
         "-y", str(out_mp3)],
        check=True,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--track", action="append", required=True,
                   help='Repeatable. "Speaker=/path/to/audio.{mp3,wav,m4a}"')
    p.add_argument("--offset", action="append", default=[],
                   help='Optional per-track delay in seconds, e.g. "Bob=0.25"')
    p.add_argument("--output-dir", required=True,
                   help="Usually <BASE_DIR>/1_transcript")
    p.add_argument("--balance", choices=["equalize", "lift", "none"], default="equalize",
                   help="Inter-track balance strategy (matches cut_audio_multitrack.py)")
    p.add_argument("--keep-intermediates", action="store_true")
    args = p.parse_args()

    tracks = parse_kv(args.track, str)
    offsets = parse_kv(args.offset, float)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_prep_work"
    work.mkdir(exist_ok=True)

    print(f"🎙️  Tracks: {list(tracks.keys())}  | balance={args.balance}")

    # 1) decode each track to wav (apply offset if any)
    raw_wavs = {}
    for spk, src in tracks.items():
        wav = work / f"{spk}_raw.wav"
        decode_to_wav(Path(src), wav, offset=offsets.get(spk, 0.0))
        raw_wavs[spk] = wav

    # 2) per-track conditioning + measurement
    per_track_lufs = {spk: measure_lufs(w) for spk, w in raw_wavs.items()}
    for spk, l in per_track_lufs.items():
        print(f"   {spk}: integrated {l} LUFS")

    if args.balance == "equalize":
        print("🎚️  Loudnorming each track to -16 LUFS...")
        prepped = {}
        for spk, wav in raw_wavs.items():
            ln = work / f"{spk}_ln.wav"
            loudnorm_to_wav(wav, ln)
            prepped[spk] = ln
        track_inputs = [(prepped[s], 0.0) for s in raw_wavs]
    elif args.balance == "lift":
        valid = {s: l for s, l in per_track_lufs.items() if l is not None and l > -70}
        if not valid:
            print("⚠️  No measurable LUFS; lift fallback to mix-as-is.")
            gains = {s: 0.0 for s in raw_wavs}
        else:
            max_l = max(valid.values())
            gains = {}
            for s in raw_wavs:
                tl = per_track_lufs.get(s)
                if tl is None or tl <= -70:
                    gains[s] = 0.0
                else:
                    gains[s] = min(max_l - tl, 12.0)
                print(f"   {s}: {tl} → +{gains[s]:.2f} dB")
        track_inputs = [(raw_wavs[s], gains[s]) for s in raw_wavs]
    else:  # none
        track_inputs = [(raw_wavs[s], 0.0) for s in raw_wavs]

    # 3a) balanced mix → mixed.wav (uses the per-track conditioning from above)
    mixed_wav = work / "mixed.wav"
    print("🎛️  Mixing balanced tracks → mixed.wav")
    mix_tracks(track_inputs, mixed_wav)

    # 3b) RAW mix → mixed_raw.wav (each track at unity, no per-track loudnorm,
    #     no final loudnorm) — this is what the Source player on the review page plays
    #     so the user can hear "before" vs "after".
    raw_mix_wav = work / "mixed_raw.wav"
    print("🎛️  Mixing RAW tracks (no balance) → mixed_raw.wav")
    raw_inputs = [(raw_wavs[s], 0.0) for s in raw_wavs]
    inputs_args, parts, labels = [], [], []
    for i, (wav, _) in enumerate(raw_inputs):
        inputs_args += ["-i", str(wav)]
        parts.append(f"[{i}:a]anull[a{i}]")
        labels.append(f"[a{i}]")
    fc = ";".join(parts) + ";" + (
        f"{''.join(labels)}amix=inputs={len(raw_inputs)}:normalize=0:duration=longest[out]"
    )
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats", *inputs_args,
         "-filter_complex", fc, "-map", "[out]",
         "-c:a", "pcm_s16le", "-y", str(raw_mix_wav)],
        check=True,
    )

    # 4) audio.mp3 (transcription, balanced) + two seekable mp3s for the review page
    audio_mp3 = out_dir / "audio.mp3"
    seekable_mp3 = out_dir / "audio_seekable.mp3"
    seekable_raw_mp3 = out_dir / "audio_seekable_raw.mp3"
    print("📝 Encoding audio.mp3 (16 kHz mono, transcription input)...")
    encode_audio_mp3(mixed_wav, audio_mp3)
    print("🌐 Encoding audio_seekable.mp3 (CBR 64k, balanced — Final-cut player)...")
    encode_seekable_mp3(audio_mp3, seekable_mp3)
    print("🌐 Encoding audio_seekable_raw.mp3 (CBR 64k, raw mix — Source player)...")
    # Encode raw via the same chain (16k mono → cbr) so size/format matches
    raw_audio_mp3 = work / "audio_raw.mp3"
    encode_audio_mp3(raw_mix_wav, raw_audio_mp3)
    encode_seekable_mp3(raw_audio_mp3, seekable_raw_mp3)

    if not args.keep_intermediates:
        for w in raw_wavs.values():
            w.unlink(missing_ok=True)
        for f in work.glob("*"):
            f.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass

    print("=" * 50)
    print("Done.")
    print(f"  {audio_mp3}")
    print(f"  {seekable_mp3}")
    print(f"  {seekable_raw_mp3}")


if __name__ == "__main__":
    main()
