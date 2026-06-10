#!/usr/bin/env python3
"""
Stage 5 multitrack final cut: produce per-speaker solo MP3 + a balanced merged MP3.

For episodes recorded with one audio track per speaker (e.g. Riverside/Zencastr
multi-track export). Outputs:

  - <speaker>_solo.mp3   : per-track cut + dynaudnorm + loudnorm to -16 LUFS
  - episode_merged.mp3   : per-speaker loudness alignment, amix (normalize=0),
                           then loudnorm to -16 LUFS.

Two input modes:

1. Legacy --delete-segments mode
   Applies the SAME delete_segments to every track. Simple; cross-talk regions
   risk losing the non-target speaker's audio.

2. --cut-plan mode (preferred for multitrack)
   Reads cut_plan_multitrack.json from classify_segments_multitrack.py:
     - hard_cuts       : removed from every track (length-affecting, current behavior)
     - track_mutes[S]  : silence applied only to speaker S's track BEFORE the
                         keep-segment extraction. Length-preserving — protects
                         the other speakers' audio in cross-talk regions.

Audio quality:
  Each output (every <speaker>_solo.mp3 + episode_merged.mp3) is encoded to MP3
  exactly once — all loudness/trim work happens on intermediate WAVs first, so
  the merged output is never re-encoded twice. Loudness uses two-pass (linear)
  loudnorm to -16 LUFS (transparent gain, no dynamics compression); dynaudnorm
  is opt-in via --dynaudnorm. Conversational pauses are trimmed on by default
  (--trim-silence; same approach as trim_silences.py) at the WAV stage.

Usage:
  python3 cut_audio_multitrack.py \
    --track "Ted=source/ted-kyle-5min/ted.wav" \
    --track "Kyle=source/ted-kyle-5min/kyle.wav" \
    --cut-plan        output/.../2_analysis/cut_plan_multitrack.json \
    --output-dir      output/.../3_output

Notes:
  - Use --offset "Speaker=seconds" to shift a track that starts late.
  - delete_segments.json schema unchanged from cut_audio.py.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Cosine fade applied at the edges of every mute region. Short enough to be
# inaudible, long enough to avoid clicks from a hard zero-crossing.
MUTE_FADE_SEC = 0.008

# ── Seam crossfades (replaces the old back-to-back afade in/out) ──
# Back-to-back fades leave an audible energy dip at every seam; an equal-power
# overlap crossfade doesn't. The overlap length adapts per seam: as long as
# the quiet audio available on both sides allows (up to 60 ms), and only 8 ms
# (click protection) when the seam touches voiced audio — long crossfades over
# speech smear the next word's onset and double the voice.
#
# Multitrack constraint: every track is cut at the same global timestamps and
# mixed afterwards, so the overlap length at seam k MUST be identical across
# tracks or the outputs drift out of sync. Crossfade lengths are therefore
# computed once (min quiet run over all tracks) and shared.
XFADE_MAX_SEC = 0.06
XFADE_MIN_SEC = 0.008
QUIET_PROBE_FRAME_SEC = 0.005
# Absolute cap on the "quiet" threshold (-38 dBFS): protects against a track
# with so little true silence that its noise-floor estimate lands on speech.
QUIET_ABS_CEILING = 10 ** (-38 / 20)


def calc_fade_duration(segment_duration):
    if segment_duration < 0.3:
        return 0.0
    fade = min(segment_duration * 0.08, 0.3)
    return max(fade, 0.03)


def estimate_wav_noise_floor(wav_path, n_windows=120, win_sec=0.05):
    """10th-percentile RMS over evenly spaced 50 ms windows of a WAV file."""
    rms_vals = []
    with sf.SoundFile(str(wav_path)) as snd:
        total = len(snd)
        win = int(win_sec * snd.samplerate)
        if total < win * 4:
            return 1e-4
        step = max((total - win) // n_windows, win)
        for pos in range(0, total - win, step):
            snd.seek(pos)
            data = snd.read(win, dtype="float32", always_2d=True).mean(axis=1)
            rms_vals.append(float(np.sqrt(np.mean(data ** 2))))
    if not rms_vals:
        return 1e-4
    return max(float(np.percentile(rms_vals, 10)), 1e-5)


def _quiet_run_sec(x, sr, threshold, from_end, max_sec=XFADE_MAX_SEC):
    """Consecutive quiet duration (s) at the head/tail of mono signal x."""
    frame = max(1, int(QUIET_PROBE_FRAME_SEC * sr))
    n = min(len(x) // frame, int(max_sec / QUIET_PROBE_FRAME_SEC))
    run = 0
    for k in range(n):
        if from_end:
            seg = x[len(x) - (k + 1) * frame: len(x) - k * frame]
        else:
            seg = x[k * frame:(k + 1) * frame]
        if float(np.sqrt(np.mean(seg ** 2))) <= threshold:
            run += 1
        else:
            break
    return run * QUIET_PROBE_FRAME_SEC


def compute_seam_crossfades(track_raw_wavs, track_keeps):
    """One crossfade length per seam, shared by every track.

    track_raw_wavs: {speaker: Path}  (decoded raw WAV, track-local timeline)
    track_keeps:    {speaker: [(start, end), ...]}

    Returns list of crossfade seconds (len = n_seams), or None when the
    tracks' keep-segment counts differ (offset edge case) — caller falls back
    to the legacy per-segment fade path which never desyncs.
    """
    counts = {spk: len(keeps) for spk, keeps in track_keeps.items()}
    if len(set(counts.values())) != 1:
        print(f"⚠️  keep-segment counts differ across tracks {counts}; "
              f"falling back to legacy per-segment fades (no crossfade).")
        return None
    n_seams = next(iter(counts.values())) - 1
    if n_seams <= 0:
        return []

    floors = {}
    handles = {}
    for spk, wav in track_raw_wavs.items():
        floors[spk] = min(estimate_wav_noise_floor(wav) * (10 ** (6.0 / 20)),  # floor+6dB
                          QUIET_ABS_CEILING)
        handles[spk] = sf.SoundFile(str(wav))

    probe = XFADE_MAX_SEC  # probe window on each side of the seam
    xfades = []
    try:
        for k in range(n_seams):
            quiet = probe
            for spk, snd in handles.items():
                sr = snd.samplerate
                seg_end = track_keeps[spk][k][1]
                nxt_start = track_keeps[spk][k + 1][0]
                # tail of kept segment k
                a = max(0, int((seg_end - probe) * sr))
                snd.seek(a)
                tail = snd.read(int(probe * sr), dtype="float32", always_2d=True).mean(axis=1)
                # head of kept segment k+1
                b = max(0, int(nxt_start * sr))
                snd.seek(min(b, len(snd)))
                head = snd.read(int(probe * sr), dtype="float32", always_2d=True).mean(axis=1)
                q = min(_quiet_run_sec(tail, sr, floors[spk], from_end=True),
                        _quiet_run_sec(head, sr, floors[spk], from_end=False))
                quiet = min(quiet, q)
            # Never longer than half the shorter neighbouring segment.
            seg_caps = []
            for spk in track_keeps:
                s0, e0 = track_keeps[spk][k]
                s1, e1 = track_keeps[spk][k + 1]
                seg_caps.append(min(e0 - s0, e1 - s1) * 0.5)
            xfade = min(max(quiet, XFADE_MIN_SEC), XFADE_MAX_SEC, min(seg_caps))
            xfades.append(max(xfade, 0.0))
    finally:
        for snd in handles.values():
            snd.close()

    at_min = sum(1 for x in xfades if x <= XFADE_MIN_SEC + 1e-4)
    med = sorted(xfades)[len(xfades) // 2] if xfades else 0
    print(f"   seam crossfades: {len(xfades)} seams, median {med*1000:.0f} ms, "
          f"{at_min} voiced seams at {XFADE_MIN_SEC*1000:.0f} ms minimum")
    return xfades


def concat_wavs_crossfade(segment_files, out_wav, xfade_secs):
    """Streaming equal-power crossfade concat (PCM_16 WAV out).

    xfade_secs[k] is the overlap between segment k and k+1; identical lists
    across tracks keep multitrack outputs sample-aligned (modulo ±1-sample
    extraction rounding, which never accumulates because every overlap is
    recomputed from the shared list).
    """
    with sf.SoundFile(str(segment_files[0])) as snd0:
        sr = snd0.samplerate
        ch = snd0.channels

    with sf.SoundFile(str(out_wav), "w", samplerate=sr, channels=ch,
                       subtype="PCM_16") as writer:
        tail = None  # held-back overlap from the previous segment
        for k, seg_path in enumerate(segment_files):
            data, _sr = sf.read(str(seg_path), dtype="float32", always_2d=True)
            if tail is not None and len(tail) > 0:
                # xfade is capped at half the shorter neighbouring segment, so
                # data is always longer than the overlap.
                n = min(len(tail), len(data))
                if n >= 2:
                    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
                    mixed = tail[:n] * np.sqrt(1.0 - ramp) + data[:n] * np.sqrt(ramp)
                    data = np.concatenate([mixed, data[n:]])
                else:
                    data = np.concatenate([tail, data])
            if k < len(segment_files) - 1:
                n_next = int(round(xfade_secs[k] * sr))
                n_next = max(0, min(n_next, len(data) - 1))
                if n_next > 0:
                    writer.write(data[:len(data) - n_next])
                    tail = data[len(data) - n_next:]
                else:
                    writer.write(data)
                    tail = None
            else:
                writer.write(data)


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


def load_cut_plan(path):
    """
    Load a cut_plan_multitrack.json produced by classify_segments_multitrack.py.

    Returns (hard_cuts, track_mutes) where:
      hard_cuts  : sorted list of (start, end)
      track_mutes: { speaker: sorted list of (start, end) }
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    hard = []
    for s in data.get("hard_cuts", []):
        a = float(s["start"])
        b = float(s["end"])
        if b > a:
            hard.append((a, b))
    hard.sort()

    mutes = {}
    for spk, items in (data.get("track_mutes") or {}).items():
        rs = []
        for it in items:
            a = float(it["start"])
            b = float(it["end"])
            if b > a:
                rs.append((a, b))
        rs.sort()
        mutes[spk] = rs

    return hard, mutes


def apply_mutes_to_wav(wav_path, mute_ranges, fade_sec=MUTE_FADE_SEC):
    """
    Zero-out mute_ranges in `wav_path` IN PLACE, with cosine fades at both edges.

    Reads the whole WAV with soundfile (mono or stereo). Short fades (~8ms)
    avoid the click that a hard cut to zero produces. Length and sample
    positions are preserved exactly — this is the per-track "silence"
    treatment, not a cut.
    """
    if not mute_ranges:
        return 0

    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    n_frames = audio.shape[0]
    fade_n = max(int(round(fade_sec * sr)), 1)

    applied = 0
    for start_s, end_s in mute_ranges:
        a = max(int(round(start_s * sr)), 0)
        b = min(int(round(end_s * sr)), n_frames)
        if b <= a:
            continue

        # If the region is shorter than one fade pair, shrink the fade.
        region_len = b - a
        local_fade = min(fade_n, region_len // 2) if region_len >= 2 else 0

        if audio.ndim == 1:
            seg = audio[a:b]
        else:
            seg = audio[a:b, :]

        if local_fade > 0:
            # Cosine ramp 1→0 at the start of the mute region, 0→1 at the end.
            ramp_out = 0.5 * (1 + np.cos(np.linspace(0, np.pi, local_fade, dtype=np.float32)))
            ramp_in = ramp_out[::-1]
            if seg.ndim == 1:
                seg[:local_fade] *= ramp_out
                if region_len > 2 * local_fade:
                    seg[local_fade:region_len - local_fade] = 0.0
                seg[region_len - local_fade:] *= ramp_in
            else:
                seg[:local_fade, :] *= ramp_out[:, None]
                if region_len > 2 * local_fade:
                    seg[local_fade:region_len - local_fade, :] = 0.0
                seg[region_len - local_fade:, :] *= ramp_in[:, None]
        else:
            if seg.ndim == 1:
                seg[:] = 0.0
            else:
                seg[:, :] = 0.0
        applied += 1

    sf.write(str(wav_path), audio, sr, subtype="PCM_16")
    return applied


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


def extract_keep_segments(src_wav, keep_segs, work_dir, prefix, with_fades=False):
    """Cut keep_segs out of src_wav; return list of segment file paths.

    with_fades=False (default): plain sample-range copies — seams are handled
    by the equal-power crossfade in concat_wavs_crossfade.
    with_fades=True: legacy adaptive afade in/out per segment, used only when
    crossfades can't be shared across tracks (keep-segment count mismatch).
    """
    out_files = []
    for i, (start, end) in enumerate(keep_segs):
        seg_dur = end - start
        seg_path = work_dir / f"{prefix}_seg_{i:05d}.wav"

        filters = []
        if with_fades:
            is_first = (i == 0)
            is_last = (i == len(keep_segs) - 1)
            fade_in = 0.0 if is_first else calc_fade_duration(seg_dur)
            fade_out = 0.0 if is_last else calc_fade_duration(seg_dur)
            if fade_in + fade_out > seg_dur * 0.6:
                r = (seg_dur * 0.6) / (fade_in + fade_out)
                fade_in *= r
                fade_out *= r
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
            # NOT -c copy: stream copy cuts at packet boundaries (tens of ms
            # off), which would desync tracks under the shared-crossfade
            # design. PCM re-encode is lossless and sample-accurate.
            cmd += ["-c:a", "pcm_s16le"]
        cmd += ["-y", str(seg_path)]
        subprocess.run(cmd, check=True)
        out_files.append(seg_path)
    return out_files


def concat_wavs(segment_files, out_wav):
    list_path = out_wav.parent / f"_concat_{out_wav.stem}.txt"
    with open(list_path, "w") as f:
        for s in segment_files:
            # Absolute paths: the concat demuxer resolves relative `file` entries
            # against the list file's own directory, which doubles a relative path.
            f.write(f"file '{s.resolve().as_posix()}'\n")
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


# ---------------------------------------------------------------------------
# Silence trimming (WAV stage, lossless re-cut)
#
# Ported from trim_silences.py so the multitrack pipeline trims real
# conversational pauses (room tone ~ -30 dB) instead of only near-dead silence.
# Critically, it operates on the intermediate WAV BEFORE the single MP3 encode,
# so the merged output is no longer re-encoded twice (which muffled the audio).
# ---------------------------------------------------------------------------

def detect_silences(audio_path, threshold_sec, noise_db):
    """ffmpeg silencedetect → list of {start, end, duration} for pauses > threshold."""
    result = subprocess.run(
        ["ffmpeg", "-i", str(audio_path),
         "-af", f"silencedetect=noise={noise_db}dB:d={threshold_sec}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    silences = []
    for line in result.stderr.split("\n"):
        m = re.search(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", line)
        if m:
            end = float(m.group(1))
            dur = float(m.group(2))
            silences.append({"start": end - dur, "end": end, "duration": dur})
    return silences


def build_trim_keep_segments(silences, total_duration, target_sec):
    """Keep `target_sec` of each over-threshold silence (target/2 per side), drop the rest."""
    half = target_sec / 2.0
    trims = []
    for s in silences:
        ts = s["start"] + half
        te = s["end"] - half
        if te > ts + 0.01:
            trims.append((ts, te))
    trims.sort()
    keeps = []
    cursor = 0.0
    for ts, te in trims:
        if cursor < ts:
            keeps.append((cursor, ts))
        cursor = te
    if cursor < total_duration:
        keeps.append((cursor, total_duration))
    return keeps


def compute_word_gap_cuts(words_path, total_duration, threshold_sec=0.6, target_sec=0.4):
    """
    Find dead-air cuts from word-level timing across ALL speakers.

    A stretch where NO speaker has a word for longer than `threshold_sec` is
    genuine dead air — safe to remove from every track (keeps them aligned).
    Each such gap is trimmed down to `target_sec` (target/2 retained on each
    side as breathing room, which also buffers against Whisper's word-timing
    slop so speech tails aren't clipped).

    Returns a list of (start, end) cuts in the ORIGINAL timeline — caller folds
    these into the hard-cut delete list so the normal keep-segment extraction
    removes them identically from each track.
    """
    words = json.loads(Path(words_path).read_text(encoding="utf-8"))
    spoken = sorted(
        (float(w["start"]), float(w["end"]))
        for w in words
        if not w.get("isSpeakerLabel") and not w.get("isGap")
        and float(w.get("end", 0)) > float(w.get("start", 0))
    )

    half = target_sec / 2.0
    cuts = []
    prev_end = 0.0
    for start, end in spoken:
        if start - prev_end > threshold_sec:
            a = prev_end + half
            b = start - half
            if b > a:
                cuts.append((a, b))
        prev_end = max(prev_end, end)
    # Trailing dead air after the last word.
    if total_duration - prev_end > threshold_sec:
        a = prev_end + half
        if total_duration > a:
            cuts.append((a, total_duration))
    return cuts


def compute_dead_air_keeps(merged_wav, threshold_sec=0.8, target_sec=0.6, noise_db=-30.0):
    """
    Detect dead air on the MERGED track and return the keep-segments to retain.

    Silence on the merged mix means *every* speaker is quiet — genuine dead air.
    The returned keeps are applied identically to every track (each solo + the
    merged) so all outputs stay sample-aligned and a solo is never collapsed into
    a monologue (which independent per-track detection would do, since one
    speaker's track looks "silent" whenever the other is talking).

    Returns (keeps, total_duration, n_pauses). keeps is None when nothing to trim.
    """
    silences = detect_silences(merged_wav, threshold_sec, noise_db)
    total = probe_duration(merged_wav)
    if not silences:
        return None, total, 0
    keeps = build_trim_keep_segments(silences, total, target_sec)
    if not keeps:
        return None, total, len(silences)
    return keeps, total, len(silences)


def apply_keeps_to_wav(wav_path, keeps):
    """Re-cut a WAV to `keeps` (lossless PCM, atrim+concat) IN PLACE. Returns new duration."""
    parts = [
        f"[0:a]atrim=start={s:.4f}:end={e:.4f},asetpts=N/SR/TB[p{i}]"
        for i, (s, e) in enumerate(keeps)
    ]
    parts.append("".join(f"[p{i}]" for i in range(len(keeps)))
                 + f"concat=n={len(keeps)}:v=0:a=1[out]")
    script_path = wav_path.parent / f"_trim_{wav_path.stem}.txt"
    script_path.write_text(";\n".join(parts), encoding="utf-8")

    tmp_path = wav_path.with_name(wav_path.stem + ".trim.wav")
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats",
         "-i", str(wav_path),
         "-filter_complex_script", str(script_path),
         "-map", "[out]",
         "-c:a", "pcm_s16le", "-ar", "48000",
         "-y", str(tmp_path)],
        check=True,
    )
    script_path.unlink(missing_ok=True)
    after = probe_duration(tmp_path)
    tmp_path.replace(wav_path)
    return after


# ---------------------------------------------------------------------------
# Loudness (two-pass loudnorm — transparent gain, no dynamics compression)
# ---------------------------------------------------------------------------

def measure_loudnorm(wav_in, I=-16.0, TP=-1.5, LRA=11.0):
    """Pass 1: measure integrated loudness/TP/LRA/threshold. Returns dict or None."""
    result = subprocess.run(
        ["ffmpeg", "-i", str(wav_in),
         "-af", f"loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for block in reversed(re.findall(r"\{[^{}]*\}", result.stderr, re.S)):
        if '"input_i"' in block:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                return None
    return None


def loudnorm_wav(wav_in, wav_out, dynaudnorm=False, I=-16.0, TP=-1.5, LRA=11.0):
    """
    Normalize to -16 LUFS → WAV using two-pass (linear) loudnorm.

    Two-pass linear normalization is a transparent gain + true-peak limit — it
    does NOT compress dynamics, so it avoids the "over-pressed / pumping" sound
    of single-pass dynamic loudnorm. dynaudnorm (intra-track leveling) is opt-in
    via `dynaudnorm=True` for tracks with bad mic-distance swings.
    """
    stats = measure_loudnorm(wav_in, I, TP, LRA)
    measured_ok = False
    if stats:
        try:
            measured_ok = float(stats["input_i"]) > -70.0
        except (KeyError, ValueError):
            measured_ok = False

    chain = ["dynaudnorm=f=500:g=15:p=0.7"] if dynaudnorm else []
    if measured_ok:
        chain.append(
            f"loudnorm=I={I}:TP={TP}:LRA={LRA}"
            f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}:linear=true"
        )
    else:
        # Fallback: silent/degenerate track — single-pass loudnorm.
        chain.append(f"loudnorm=I={I}:TP={TP}:LRA={LRA}")

    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats",
         "-i", str(wav_in),
         "-af", ",".join(chain),
         "-c:a", "pcm_s16le", "-ar", "48000",
         "-y", str(wav_out)],
        check=True,
    )


def encode_wav_to_mp3(wav_in, mp3_out, bitrate_kbps=192):
    """Single, final MP3 encode (no further filtering — keeps it lossless until here)."""
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats",
         "-i", str(wav_in),
         "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
         "-y", str(mp3_out)],
        check=True,
    )


def mix_to_wav(track_wavs_with_gain, mixed_out_wav):
    """
    Mix multiple WAVs with per-track volume gain → raw summed WAV (no loudnorm).

    track_wavs_with_gain: list of (wav_path, gain_db). amix normalize=0 so we
    control levels ourselves (avoids amix's default /N attenuation). The final
    loudnorm runs separately on the result via loudnorm_wav().
    """
    inputs = []
    filter_parts = []
    labels = []
    for idx, (wav, gain_db) in enumerate(track_wavs_with_gain):
        inputs += ["-i", str(wav)]
        if abs(gain_db) > 0.01:
            filter_parts.append(
                f"[{idx}:a]volume={gain_db:.2f}dB,alimiter=limit=0.95[a{idx}]"
            )
        else:
            filter_parts.append(f"[{idx}:a]anull[a{idx}]")
        labels.append(f"[a{idx}]")

    n = len(track_wavs_with_gain)
    amix = f"{''.join(labels)}amix=inputs={n}:normalize=0:duration=longest[out]"
    filter_complex = ";".join(filter_parts) + ";" + amix

    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-stats", *inputs,
         "-filter_complex", filter_complex,
         "-map", "[out]",
         "-c:a", "pcm_s16le", "-ar", "48000",
         "-y", str(mixed_out_wav)],
        check=True,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--track", action="append", required=True,
                   help='Repeatable. Format: "Speaker=/path/to/audio.{mp3,wav,m4a}"')
    p.add_argument("--offset", action="append", default=[],
                   help='Optional timestamp offset (sec) per speaker, e.g. "Bob=0.25"')
    p.add_argument("--delete-segments", default=None,
                   help="Path to delete_segments.json (rough+fine merged). Required unless --cut-plan is provided.")
    p.add_argument("--cut-plan", default=None,
                   help=(
                       "Path to cut_plan_multitrack.json from classify_segments_multitrack.py.\n"
                       "When given, supersedes --delete-segments: hard_cuts apply globally,\n"
                       "and track_mutes silence each speaker's own track only (length-preserving).\n"
                       "This is how cross-talk safe NSV / overlap removal works."
                   ))
    p.add_argument("--trim-silence", action=argparse.BooleanOptionalAction, default=True,
                   help=(
                       "Trim conversational dead air from every output (each solo + merged),\n"
                       "keeping them sample-aligned. ON by default; use --no-trim-silence to skip.\n"
                       "Two detectors:\n"
                       "  --word-gaps  (preferred): dead air = stretch where NO speaker has a word.\n"
                       "               Uses word timing, immune to the other mic's room-tone/bleed.\n"
                       "  audio (fallback when --word-gaps is omitted): silencedetect on the merged\n"
                       "               track (same approach as trim_silences.py)."
                   ))
    p.add_argument("--word-gaps", default=None,
                   help=(
                       "Path to subtitles_words.json. When given, dead air is detected from\n"
                       "word timing across all speakers (preferred) and folded into the hard\n"
                       "cuts; the audio merged-trim is skipped."
                   ))
    p.add_argument("--trim-threshold", type=float, default=0.6,
                   help="Gaps longer than this many seconds get trimmed (default 0.6).")
    p.add_argument("--trim-target", type=float, default=0.4,
                   help="Each trimmed gap is reduced to this many seconds (default 0.4).")
    p.add_argument("--trim-noise", type=float, default=-30.0,
                   help="silencedetect noise floor in dB (audio fallback detector only; default -30).")
    p.add_argument("--dynaudnorm", action="store_true",
                   help=(
                       "Opt back into dynaudnorm (intra-track dynamics leveling) on top of the\n"
                       "two-pass loudnorm. OFF by default — two-pass loudnorm is transparent and\n"
                       "dynaudnorm is what made isolated tracks sound over-compressed. Enable only\n"
                       "for a track with bad mic-distance swings."
                   ))
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

    if not args.cut_plan and not args.delete_segments:
        p.error("either --cut-plan or --delete-segments must be provided")

    track_mutes = {}
    if args.cut_plan:
        hard_cut_pairs, track_mutes = load_cut_plan(args.cut_plan)
        delete_segs = hard_cut_pairs  # hard_cuts drive the global keep-segment inversion
        print(f"📄 Loaded cut plan from {args.cut_plan}")
        print(f"   hard_cuts: {len(delete_segs)}")
        for spk, mutes in track_mutes.items():
            print(f"   mutes[{spk}]: {len(mutes)}")
        unknown = set(track_mutes) - set(tracks)
        if unknown:
            print(f"⚠️  Cut plan has mutes for unknown speakers {sorted(unknown)}; they will be ignored.")
    else:
        delete_segs = load_delete_segments(args.delete_segments)
        print(f"📄 Loaded {len(delete_segs)} delete segments from {args.delete_segments}")
    print(f"🎙️  Tracks: {list(tracks.keys())}")

    def _union_len(segs):
        merged = []
        for a, b in sorted(segs):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return sum(b - a for a, b in merged)

    # Dead-air trimming. Preferred path: word-gap detection folded into the hard
    # cuts (removed from every track by the normal keep-segment extraction →
    # outputs stay aligned). Falls back to the audio merged-trim in Phase 4 when
    # --word-gaps isn't supplied.
    use_audio_trim = args.trim_silence and not args.word_gaps
    if args.trim_silence and args.word_gaps:
        total_ref = max(probe_duration(Path(p)) for p in tracks.values())
        wg_cuts = compute_word_gap_cuts(
            args.word_gaps, total_ref, args.trim_threshold, args.trim_target)
        before = _union_len(delete_segs)
        delete_segs = sorted(delete_segs + wg_cuts)
        after = _union_len(delete_segs)
        print(f"🧹 Word-gap dead-air trim: {len(wg_cuts)} gaps "
              f"(> {args.trim_threshold}s → keep {args.trim_target}s), "
              f"net +{after - before:.0f}s removed across all tracks.")
    print("")

    per_track_cut_wavs = {}  # speaker -> Path (cut, no loudnorm yet)
    per_track_lufs = {}      # speaker -> float

    # --- Phase 1a: decode every track + apply mutes + compute keep segments ---
    # All tracks are decoded up front so seam crossfade lengths can be computed
    # ONCE from all tracks' audio (they must be identical across tracks to keep
    # the outputs sample-aligned for the mix).
    track_raw_wavs = {}
    track_keeps = {}
    for speaker, audio_path in tracks.items():
        print(f"=== {speaker} ===")
        audio_path = Path(audio_path)
        duration = probe_duration(audio_path)
        offset = offsets.get(speaker, 0.0)
        keep_segs = build_keep_segments(delete_segs, duration, offset=offset)
        print(f"   Duration {duration:.2f}s, offset {offset:.2f}s → {len(keep_segs)} keep segments")

        raw_wav = work_dir / f"{speaker}_raw.wav"
        decode_to_wav(audio_path, raw_wav)

        # Apply per-track mutes from the cut plan, BEFORE keep-segment extraction.
        # Mutes are length-preserving silence treatments (cross-talk safe);
        # the keep-segments below then perform any length-affecting hard cuts.
        spk_mutes = track_mutes.get(speaker, [])
        if spk_mutes:
            shifted_mutes = [
                (max(s - offset, 0.0), max(e - offset, 0.0))
                for s, e in spk_mutes if e > offset
            ]
            shifted_mutes = [(s, e) for s, e in shifted_mutes if e > s]
            n_applied = apply_mutes_to_wav(raw_wav, shifted_mutes)
            print(f"   Muted {n_applied} regions on {speaker}'s track (fade {MUTE_FADE_SEC*1000:.0f} ms)")

        track_raw_wavs[speaker] = raw_wav
        track_keeps[speaker] = keep_segs
    print("")

    # --- Phase 1b: shared seam crossfade plan (adaptive, sample-aligned) ---
    print("🔗 Computing per-seam crossfade lengths (shared across tracks)...")
    xfades = compute_seam_crossfades(track_raw_wavs, track_keeps)
    print("")

    # --- Phase 1c: per-track cut → cut WAV (no loudnorm) ---
    for speaker in tracks:
        raw_wav = track_raw_wavs[speaker]
        keep_segs = track_keeps[speaker]
        cut_wav = work_dir / f"{speaker}_cut.wav"

        if xfades is not None:
            seg_files = extract_keep_segments(raw_wav, keep_segs, work_dir, speaker)
            concat_wavs_crossfade(seg_files, cut_wav, xfades)
        else:
            # Fallback: legacy per-segment fades + abutting concat (no overlap,
            # can't desync even with mismatched keep counts).
            seg_files = extract_keep_segments(raw_wav, keep_segs, work_dir, speaker,
                                                with_fades=True)
            concat_wavs(seg_files, cut_wav)
        for seg in seg_files:
            seg.unlink(missing_ok=True)
        raw_wav.unlink(missing_ok=True)

        lufs = measure_integrated_lufs(cut_wav)
        per_track_lufs[speaker] = lufs
        per_track_cut_wavs[speaker] = cut_wav
        print(f"   {speaker}: cut → {cut_wav.name}, integrated loudness {lufs} LUFS")
    print("")

    # --- Phase 2: per-track two-pass loudnorm (WAV, transparent) ---
    # Every track gets loudnormed to -16 LUFS. The solo MP3 is built from this,
    # and equalize-mode merge reuses these same WAVs.
    print(f"🎚️  Balance mode: {args.balance}  |  dynaudnorm: {'on' if args.dynaudnorm else 'off'}")
    dyn = "dynaudnorm + " if args.dynaudnorm else ""
    print(f"   Loudnorming each track to -16 LUFS ({dyn}two-pass, WAV)...")
    per_track_loudnormed_wavs = {}
    for speaker, cut_wav in per_track_cut_wavs.items():
        ln_wav = work_dir / f"{speaker}_loudnormed.wav"
        loudnorm_wav(cut_wav, ln_wav, dynaudnorm=args.dynaudnorm)
        per_track_loudnormed_wavs[speaker] = ln_wav
        print(f"   ✅ {speaker} → {ln_wav.name}")
    print("")

    # --- Phase 3: build the merged mix (WAV) ---
    # Built first so dead-air can be detected on the merged track (silence there
    # = every speaker quiet) and the same trim applied to every output.
    merged_path = output_dir / "episode_merged.mp3"

    if args.balance == "equalize":
        print("🎛️  Mixing pre-equalized tracks (every speaker at -16 LUFS)...")
        # All tracks already at -16 LUFS → mix with normalize=0, then final
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

    mixed_raw = work_dir / "merged_raw.wav"
    merged_norm = work_dir / "merged_norm.wav"
    print(f"🎚️  Mixing → merged WAV (amix normalize=0 + two-pass loudnorm -16 LUFS)...")
    mix_to_wav(track_inputs, mixed_raw)
    loudnorm_wav(mixed_raw, merged_norm, dynaudnorm=args.dynaudnorm)
    mixed_raw.unlink(missing_ok=True)
    print("")

    # --- Phase 4: audio-fallback dead-air trim (only when --word-gaps absent) ---
    # Word-gap trimming already happened in Phase 1 (folded into hard cuts). This
    # audio detector is the fallback path; one trim plan from the merged keeps
    # every output sample-aligned and never collapses a solo into a monologue.
    keeps = None
    if use_audio_trim:
        print(f"🧹 Detecting dead air on merged (pauses > {args.trim_threshold}s @ {args.trim_noise:.0f} dB)...")
        keeps, total, n_pauses = compute_dead_air_keeps(
            merged_norm, args.trim_threshold, args.trim_target, args.trim_noise)
        if keeps:
            kept = sum(e - s for s, e in keeps)
            print(f"   {n_pauses} dead-air pauses → trim {total:.1f}s → {kept:.1f}s "
                  f"(−{total - kept:.0f}s), applied to every output.")
        else:
            print(f"   No dead air > {args.trim_threshold}s on the merged track; nothing to trim.")
        print("")

    # Solo MP3s: copy loudnormed WAV, apply shared keeps, single MP3 encode.
    print("🎚️  Encoding per-speaker solo MP3s (single MP3 encode each)...")
    for speaker in per_track_cut_wavs:
        solo_path = output_dir / f"{speaker}_solo.mp3"
        solo_wav = work_dir / f"{speaker}_solo.wav"
        shutil.copyfile(per_track_loudnormed_wavs[speaker], solo_wav)
        if keeps:
            apply_keeps_to_wav(solo_wav, keeps)
        encode_wav_to_mp3(solo_wav, solo_path, bitrate_kbps=args.bitrate)
        solo_wav.unlink(missing_ok=True)
        print(f"   ✅ {solo_path}")
    print("")

    # Merged MP3: apply the same keeps, single MP3 encode.
    print(f"🎚️  Encoding {merged_path.name}...")
    if keeps:
        apply_keeps_to_wav(merged_norm, keeps)
    encode_wav_to_mp3(merged_norm, merged_path, bitrate_kbps=args.bitrate)
    merged_norm.unlink(missing_ok=True)
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
