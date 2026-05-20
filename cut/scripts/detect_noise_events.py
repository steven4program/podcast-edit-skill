#!/usr/bin/env python3
"""
Detect non-semantic noise events (cough / throat-clearing / sniff / thud / mic bumps...)
in podcast audio while preserving natural communication signals (laughter, "嗯嗯",
breathing, thinking pauses).

Two detection passes:

  Pass A — YAMNet label match
    Flag frames where any class in NOISE_LABELS scores >= --min-conf, with a
    speech/laughter veto to protect real speech and "嗯嗯".

  Pass B — Untranscribed voiced burst (default ON; --no-burst-pass to disable)
    YAMNet often classifies short throat-clearing/cough as plain "Speech" with no
    Cough/Throat clearing signal at all. We catch these by looking for frames
    where YAMNet says Speech >= --burst-speech-conf, but NO speaker has a
    transcribed word at that time. Backchannels like "mm-hmm" survive because
    the other speaker is usually mid-sentence (their word overlaps the burst).
    A high YAMNet Laughter score also vetoes the burst so laughter survives.

Multitrack-aware: each speaker's track is analysed independently, so a cough on
one mic doesn't taint other speakers. Events also cross-reference ALL speakers'
words (not just the track's own speaker) for the burst-pass veto.

Usage (multi-track):
  python detect_noise_events.py \
    --track "Alice=path/alice.wav" \
    --track "Bob=path/bob.wav" \
    --words   subtitles_words.json \
    --output  noise_events.json

Usage (single track):
  python detect_noise_events.py \
    --audio   audio.mp3 --speaker Alice \
    --words   subtitles_words.json \
    --output  noise_events.json

Output schema (noise_events.json):
[
  {
    "track":      "Alice",
    "start":      12.480,
    "end":        12.960,
    "label":      "Cough",
    "confidence": 0.83,
    "speech_conf":0.04,
    "source":     "label" | "burst",
    "in_word":    false
  },
  ...
]
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


# AudioSet class names (exact strings from YAMNet's class_map.csv).
# Expanded based on real-world hits: mic handling/object noise tends to land on
# Plop / Scratch / Whack / Sound effect / Boing / Music (chunky transient) more
# often than on Cough/Sneeze/Sniff.
NOISE_LABELS = {
    # Body sounds we want gone
    "Cough", "Throat clearing", "Sneeze", "Sniff",
    "Gasp", "Hiccup", "Burping, eructation",
    # Mic / object handling
    "Tap", "Thump, thud", "Knock", "Slap, smack", "Smash, crash",
    "Crackle", "Crumpling, crinkling", "Rustle",
    "Microphone", "Static", "Hum", "Buzz", "Mains hum",
    "White noise", "Pink noise", "Hiss",
    "Clicking", "Click",
    # Newly added: chunky transients YAMNet reaches for when speech models fail
    "Plop", "Whack, thwack", "Scratch",
    "Scratching (performance technique)",
    "Sound effect", "Boing",
}

# These must NEVER be flagged.
PROTECTED_LABELS = {
    "Speech", "Male speech, man speaking", "Female speech, woman speaking",
    "Conversation", "Narration, monologue",
    "Laughter", "Giggle", "Snicker", "Belly laugh",
    "Chuckle, chortle", "Baby laughter",
    "Breathing", "Inhalation", "Exhalation", "Wheeze", "Snore",
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--track", action="append", default=[],
                   help='Multitrack input. Repeatable. "Speaker=/path/audio.wav"')
    p.add_argument("--audio", default=None, help="Single-track audio file.")
    p.add_argument("--speaker", default=None, help="Speaker name for --audio.")
    p.add_argument("--words", default=None, help="subtitles_words.json (all speakers).")
    p.add_argument("--output", default="noise_events.json", help="Output path.")
    # Pass A
    p.add_argument("--min-conf", type=float, default=0.40,
                   help="Pass A: min YAMNet score for noise classes (default 0.40).")
    p.add_argument("--speech-veto", type=float, default=0.30,
                   help="Pass A: drop if Speech/Laughter >= this (default 0.30).")
    # Pass C — librosa low-frequency burst (catches throat-clears INSIDE speech)
    p.add_argument("--no-lowfreq-pass", action="store_true",
                   help="Disable Pass C (librosa low-freq burst detector).")
    p.add_argument("--lowfreq-min-score", type=float, default=0.40,
                   help="Pass C: composite score gate (default 0.40). Lower = more recall, more FP. "
                        "Score combines rolloff/centroid/ZCR (lower = more bassy + less voiced).")
    p.add_argument("--lowfreq-max-voiced-frac", type=float, default=0.30,
                   help="Pass C: drop candidate when pyin voiced-frame fraction >= this (default 0.30). "
                        "Tighter than Pass B because lowfreq FPs are usually tone-3 syllables with F0.")
    p.add_argument("--lowfreq-rolloff-max-hz", type=float, default=1500.0,
                   help="Pass C: rolloff_85 must be below this (default 1500 Hz).")
    p.add_argument("--lowfreq-zcr-max", type=float, default=0.08,
                   help="Pass C: zero-crossing rate must be below this (default 0.08).")
    p.add_argument("--lowfreq-rms-min-mult", type=float, default=3.0,
                   help="Pass C: RMS must exceed median*this (default 3.0).")
    p.add_argument("--lowfreq-centroid-max-hz", type=float, default=900.0,
                   help="Pass C: spectral centroid must be below this (default 900 Hz).")
    p.add_argument("--lowfreq-min-dur-ms", type=int, default=60)
    p.add_argument("--lowfreq-max-dur-ms", type=int, default=500)
    p.add_argument("--lowfreq-merge-gap-ms", type=int, default=80)
    p.add_argument("--lowfreq-min-frames", type=int, default=3)
    # Pass B
    p.add_argument("--no-burst-pass", action="store_true",
                   help="Disable Pass B (untranscribed voiced burst).")
    p.add_argument("--burst-speech-conf", type=float, default=0.50,
                   help="Pass B: min Speech score to consider (default 0.50).")
    p.add_argument("--burst-laughter-veto", type=float, default=0.20,
                   help="Pass B: drop if Laughter >= this (default 0.20).")
    p.add_argument("--burst-min-dur", type=float, default=0.30,
                   help="Pass B: minimum burst duration in s (default 0.30).")
    p.add_argument("--burst-max-dur", type=float, default=2.0,
                   help="Pass B: maximum burst duration; longer = real speech (default 2.0).")
    p.add_argument("--max-voiced-frac", type=float, default=0.30,
                   help="Drop burst events whose voiced (F0-detected) frame fraction "
                        ">= this. Voiced = real speech / mm-hmm / laugh. Default 0.30.")
    p.add_argument("--burst-edge-buffer", type=float, default=0.10,
                   help="Clamp burst events inside the surrounding inter-word gap "
                        "by this many seconds, to protect adjacent speech. Default 0.10.")
    p.add_argument("--energy-threshold", type=float, default=0.35,
                   help="Tighten burst events to RMS >= peak * this (0..1). "
                        "Lower = wider mute. Default 0.35.")
    p.add_argument("--no-energy-tighten", action="store_true",
                   help="Disable RMS-based event tightening.")
    p.add_argument("--no-pitch-filter", action="store_true",
                   help="Disable F0/pitch filter on burst events.")
    # Common
    p.add_argument("--merge-gap", type=float, default=0.30,
                   help="Merge frames separated by <= this seconds (default 0.30).")
    p.add_argument("--pad", type=float, default=0.05,
                   help="Pad each event by this many seconds on both sides (default 0.05).")
    p.add_argument("--keep-in-word", action="store_true",
                   help="Don't filter events that overlap a transcribed word "
                        "(default: filtered).")
    return p.parse_args()


def load_yamnet():
    try:
        import tensorflow as tf  # noqa
        import tensorflow_hub as hub
    except ImportError as e:
        sys.exit("Missing deps. pip install tensorflow tensorflow-hub librosa soundfile numpy\n"
                 f"(import error: {e})")
    print("Loading YAMNet from TF Hub (cached after first run)...")
    model_url = os.environ.get(
        "YAMNET_URL",
        "https://www.kaggle.com/models/google/yamnet/TensorFlow2/yamnet/1",
    )
    model = hub.load(model_url)
    cls_path = model.class_map_path().numpy().decode("utf-8")
    classes = []
    with open(cls_path) as f:
        for row in csv.DictReader(f):
            classes.append(row["display_name"])
    return model, classes


def load_audio_16k_mono(path):
    import librosa
    y, _ = librosa.load(str(path), sr=16000, mono=True)
    return y


def load_all_words(words_path):
    """Return dict: speaker -> [(start, end), ...] for all speakers."""
    if not words_path or not os.path.exists(words_path):
        return {}
    with open(words_path, encoding="utf-8") as f:
        words = json.load(f)
    out = {}
    for w in words:
        if w.get("isGap") or w.get("isSpeakerLabel"):
            continue
        sp = w.get("speaker")
        s, e = w.get("start"), w.get("end")
        if not sp or s is None or e is None or e <= s:
            continue
        out.setdefault(sp, []).append((s, e))
    for sp in out:
        out[sp].sort()
    return out


def tighten_to_energy_core(y, sr, t_start, t_end, threshold_ratio=0.35, min_dur=0.08):
    """Shrink event boundaries to the contiguous high-energy region around the
    RMS peak inside [t_start, t_end]. YAMNet's 0.96s window almost always over-
    estimates the actual burst duration; this finds the real start/end.

    threshold_ratio = 0.35 means: keep frames whose RMS is >= 35% of the peak.
    Returns (new_start, new_end) — never wider than the input.
    """
    import numpy as np
    import librosa

    i0 = max(0, int(t_start * sr))
    i1 = min(len(y), int(t_end * sr))
    seg = y[i0:i1]
    if len(seg) < int(0.1 * sr):
        return t_start, t_end
    hop = 256
    rms = librosa.feature.rms(y=seg, frame_length=1024, hop_length=hop)[0]
    if len(rms) == 0 or rms.max() < 1e-5:
        return t_start, t_end
    peak = int(rms.argmax())
    threshold = rms[peak] * threshold_ratio
    left = peak
    while left > 0 and rms[left - 1] >= threshold:
        left -= 1
    right = peak
    while right < len(rms) - 1 and rms[right + 1] >= threshold:
        right += 1
    hop_t = hop / sr
    new_s = t_start + left * hop_t
    new_e = t_start + (right + 1) * hop_t
    if new_e - new_s < min_dur:
        # Symmetric expansion around peak to reach min_dur
        center = t_start + (peak + 0.5) * hop_t
        new_s = max(t_start, center - min_dur / 2)
        new_e = min(t_end, center + min_dur / 2)
    return new_s, new_e


def voiced_fraction(y, sr, t_start, t_end):
    """Fraction of frames in [t_start, t_end] where pyin detects an F0.

    pyin reports voiced_flag per frame. Speech / mm-hmm / laughter have F0 in
    most frames; cough / throat-clearing / mic noise / sniff don't.

    Returns (fraction in [0, 1], n_frames). Returns (0.0, 0) on too-short or
    pyin failure (so the event is treated as noise -> kept).
    """
    import numpy as np
    import librosa

    i0 = max(0, int(t_start * sr))
    i1 = min(len(y), int(t_end * sr))
    seg = y[i0:i1]
    if len(seg) < int(0.10 * sr):  # need >=100ms for reliable pitch
        return 0.0, 0
    try:
        # 70-500 Hz covers adult speech & most "嗯/mm-hmm" without picking up rumble.
        f0, voiced_flag, _ = librosa.pyin(
            seg, fmin=70.0, fmax=500.0, sr=sr,
            frame_length=1024, hop_length=256,
        )
    except Exception:
        return 0.0, 0
    if voiced_flag is None or len(voiced_flag) == 0:
        return 0.0, 0
    frac = float(np.nanmean(voiced_flag.astype(float)))
    return frac, int(len(voiced_flag))


def surrounding_gap(t_center, all_intervals, total_duration):
    """Return (gap_start, gap_end): the no-word gap (across ALL speakers) that
    contains t_center. all_intervals must be sorted by start.
    """
    prev_end = 0.0
    next_start = total_duration
    for s, e in all_intervals:
        if e <= t_center:
            if e > prev_end:
                prev_end = e
        elif s >= t_center:
            next_start = s
            break
    return prev_end, next_start


def overlaps_any(t_start, t_end, intervals):
    for s, e in intervals:
        if t_start < e and t_end > s:
            return True
        if s >= t_end:
            break
    return False


def detect_track(audio_path, speaker, model, classes, all_words, args):
    import numpy as np

    print(f"\n[{speaker}] loading {audio_path}")
    y = load_audio_16k_mono(audio_path)
    duration = len(y) / 16000.0
    print(f"  {duration:.1f}s; running YAMNet...")
    scores, _, _ = model(y)
    scores = scores.numpy()
    n_frames = scores.shape[0]
    hop, win = 0.48, 0.96

    name_to_idx = {n: i for i, n in enumerate(classes)}
    noise_idx = {name_to_idx[n] for n in NOISE_LABELS if n in name_to_idx}
    speech_like = [name_to_idx[n] for n in (
        "Speech", "Conversation", "Narration, monologue",
        "Male speech, man speaking", "Female speech, woman speaking") if n in name_to_idx]
    laugh_like = [name_to_idx[n] for n in (
        "Laughter", "Giggle", "Chuckle, chortle", "Snicker", "Belly laugh") if n in name_to_idx]
    speech_idx = name_to_idx.get("Speech", -1)

    own_words = all_words.get(speaker, [])
    other_words = [iv for sp, ivs in all_words.items() if sp != speaker for iv in ivs]
    other_words.sort()
    all_intervals = sorted([iv for ivs in all_words.values() for iv in ivs])

    # ───── Pass A: noise label hits ─────
    flagged_a = []
    for f in range(n_frames):
        row = scores[f]
        speech_score = max(row[i] for i in speech_like) if speech_like else 0.0
        laugh_score = max(row[i] for i in laugh_like) if laugh_like else 0.0
        veto = max(speech_score, laugh_score)
        best_i, best_s = -1, 0.0
        for i in noise_idx:
            if row[i] > best_s:
                best_s, best_i = row[i], i
        if best_i < 0 or best_s < args.min_conf:
            continue
        if veto >= args.speech_veto:
            continue
        flagged_a.append({
            "t_start": f * hop, "t_end": f * hop + win,
            "label": classes[best_i], "conf": float(best_s),
            "speech_conf": float(speech_score), "source": "label",
        })

    # ───── Pass B: untranscribed voiced burst ─────
    flagged_b = []
    if not args.no_burst_pass and speech_idx >= 0:
        for f in range(n_frames):
            row = scores[f]
            sp = float(row[speech_idx])
            if sp < args.burst_speech_conf:
                continue
            laugh_score = max(row[i] for i in laugh_like) if laugh_like else 0.0
            if laugh_score >= args.burst_laughter_veto:
                continue
            t0, t1 = f * hop, f * hop + win
            # Veto if any speaker has a transcribed word here
            if overlaps_any(t0, t1, own_words):
                continue
            if overlaps_any(t0, t1, other_words):
                continue
            flagged_b.append({
                "t_start": t0, "t_end": t1,
                "label": "Speech (untranscribed burst)", "conf": sp,
                "speech_conf": sp, "source": "burst",
            })

    # ───── Pass C: librosa low-frequency burst (in-speech non-vocal events) ─────
    # This pass exists to catch throat-clears / coughs / low-rumble that sit
    # INSIDE continuous speech, where YAMNet's 0.96 s window is dominated by
    # the surrounding words and Pass B's word-overlap veto fires for every frame.
    # It looks at the raw spectral signature (bassy + low ZCR + loud) instead of
    # asking YAMNet "is this speech?".
    flagged_c = []
    if not args.no_lowfreq_pass:
        import librosa
        hop = 160  # 10 ms
        win = 400  # 25 ms
        n_fft = 512
        sr_lf = 16000
        rms_lf = librosa.feature.rms(y=y, frame_length=win, hop_length=hop)[0]
        cent_lf = librosa.feature.spectral_centroid(y=y, sr=sr_lf, n_fft=n_fft, hop_length=hop)[0]
        rolloff_lf = librosa.feature.spectral_rolloff(y=y, sr=sr_lf, n_fft=n_fft, hop_length=hop, roll_percent=0.85)[0]
        zcr_lf = librosa.feature.zero_crossing_rate(y=y, frame_length=win, hop_length=hop)[0]
        n_lf = min(len(rms_lf), len(cent_lf), len(rolloff_lf), len(zcr_lf))
        rms_lf = rms_lf[:n_lf]; cent_lf = cent_lf[:n_lf]
        rolloff_lf = rolloff_lf[:n_lf]; zcr_lf = zcr_lf[:n_lf]
        rms_med_lf = float(np.median(rms_lf[rms_lf > 1e-4])) if (rms_lf > 1e-4).any() else 1e-3
        rms_min_lf = rms_med_lf * args.lowfreq_rms_min_mult
        flag_lf = (
            (rms_lf > rms_min_lf)
            & (rolloff_lf < args.lowfreq_rolloff_max_hz)
            & (zcr_lf < args.lowfreq_zcr_max)
            & (cent_lf < args.lowfreq_centroid_max_hz)
        )
        # Run-length encode → bursts
        bursts = []
        i = 0
        while i < n_lf:
            if not flag_lf[i]:
                i += 1
                continue
            j = i
            while j < n_lf and flag_lf[j]:
                j += 1
            if j - i >= args.lowfreq_min_frames:
                bursts.append((i, j))
            i = j
        # Merge close bursts (same cough often has two syllabic peaks)
        gap_frames = int(args.lowfreq_merge_gap_ms / 1000 * sr_lf / hop)
        merged_lf = []
        for a, b in bursts:
            if merged_lf and a - merged_lf[-1][1] <= gap_frames:
                merged_lf[-1] = (merged_lf[-1][0], b)
            else:
                merged_lf.append((a, b))
        # Duration filter + score + voicing veto
        min_f = int(args.lowfreq_min_dur_ms / 1000 * sr_lf / hop)
        max_f = int(args.lowfreq_max_dur_ms / 1000 * sr_lf / hop)
        lowfreq_voiced_dropped = 0
        lowfreq_score_dropped = 0
        for a, b in merged_lf:
            dur_f = b - a
            if dur_f < min_f or dur_f > max_f:
                continue
            start_s = a * hop / sr_lf
            end_s = b * hop / sr_lf
            mean_cent = float(cent_lf[a:b].mean())
            mean_rolloff = float(rolloff_lf[a:b].mean())
            mean_zcr = float(zcr_lf[a:b].mean())
            peak_rms = float(rms_lf[a:b].max())
            c_score = max(0, 1 - mean_cent / args.lowfreq_centroid_max_hz)
            r_score = max(0, 1 - mean_rolloff / args.lowfreq_rolloff_max_hz)
            z_score = max(0, 1 - mean_zcr / args.lowfreq_zcr_max)
            score = min(1.0, 0.4 * r_score + 0.3 * c_score + 0.3 * z_score)
            if score < args.lowfreq_min_score:
                lowfreq_score_dropped += 1
                continue
            # F0 voicing veto — drop tone-3 syllables (have F0)
            v_frac, n_pf = voiced_fraction(y, 16000, start_s, end_s)
            if n_pf > 0 and v_frac >= args.lowfreq_max_voiced_frac:
                lowfreq_voiced_dropped += 1
                continue
            flagged_c.append({
                "t_start": start_s, "t_end": end_s,
                "label": f"Low-freq burst (cent={mean_cent:.0f}Hz)",
                "conf": float(score),
                "speech_conf": 0.0,
                "source": "lowfreq",
                "_voiced_frac": v_frac,
                "_rolloff": mean_rolloff,
                "_zcr": mean_zcr,
                "_rms_peak": peak_rms,
            })
        print(f"  Pass C bursts: {len(bursts)} → merged: {len(merged_lf)}; "
              f"score-dropped: {lowfreq_score_dropped}, voiced-dropped: {lowfreq_voiced_dropped}; "
              f"kept: {len(flagged_c)}")

    # Merge consecutive frames; only merge same-source events.
    def merge(frames):
        out = []
        for fr in sorted(frames, key=lambda x: x["t_start"]):
            if out and fr["source"] == out[-1]["source"] \
               and (fr["t_start"] - out[-1]["t_end"]) <= args.merge_gap:
                m = out[-1]
                m["t_end"] = fr["t_end"]
                if fr["conf"] > m["conf"]:
                    m["conf"] = fr["conf"]
                    m["label"] = fr["label"]
                m["speech_conf"] = max(m["speech_conf"], fr["speech_conf"])
            else:
                out.append(dict(fr))
        return out

    merged = merge(flagged_a + flagged_b + flagged_c)

    events = []
    pitch_dropped = 0
    clamped = 0
    for m in merged:
        # Burst-pass duration window check
        dur = m["t_end"] - m["t_start"]
        if m["source"] == "burst" and not (args.burst_min_dur <= dur <= args.burst_max_dur):
            continue
        s = max(0.0, m["t_start"] - args.pad)
        e = min(duration, m["t_end"] + args.pad)

        # Burst events: clamp boundaries to the surrounding inter-word gap so we
        # don't bleed into adjacent speech (across all speakers).
        if m["source"] == "burst" and args.burst_edge_buffer > 0:
            center = (m["t_start"] + m["t_end"]) / 2
            gap_s, gap_e = surrounding_gap(center, all_intervals, duration)
            new_s = max(s, gap_s + args.burst_edge_buffer)
            new_e = min(e, gap_e - args.burst_edge_buffer)
            if new_e - new_s < 0.15:  # too short after clamp -> drop
                continue
            if new_s != s or new_e != e:
                clamped += 1
            s, e = new_s, new_e

        # Tighten burst events to the actual RMS peak core inside the window.
        if m["source"] == "burst" and not args.no_energy_tighten:
            s, e = tighten_to_energy_core(y, 16000, s, e,
                                          threshold_ratio=args.energy_threshold)

        in_word = overlaps_any(s, e, own_words)
        # Pass C events are EXPECTED to overlap words (that's their whole purpose —
        # catching noise mid-speech). Only filter label/burst sources by in_word.
        if in_word and not args.keep_in_word and m["source"] != "lowfreq":
            continue

        # F0 / pitch filter — voiced events are real speech, mm-hmm, or laughter.
        # Apply to burst events only by default (label-pass already had speech veto).
        v_frac = -1.0
        if m["source"] == "burst" and not args.no_pitch_filter:
            v_frac, n_pf = voiced_fraction(y, 16000, s, e)
            if n_pf > 0 and v_frac >= args.max_voiced_frac:
                pitch_dropped += 1
                continue

        events.append({
            "track": speaker,
            "start": round(s, 3), "end": round(e, 3),
            "label": m["label"],
            "confidence": round(m["conf"], 3),
            "speech_conf": round(m["speech_conf"], 3),
            "source": m["source"],
            "in_word": in_word,
            "voiced_frac": round(v_frac, 3) if v_frac >= 0 else None,
        })

    print(f"  Pass A flagged: {len(flagged_a)} frames")
    print(f"  Pass B flagged: {len(flagged_b)} frames")
    print(f"  Pass C kept:    {len(flagged_c)} events (in-speech low-freq bursts)")
    print(f"  Merged events: {len(merged)}; clamped: {clamped}; pitch-dropped: {pitch_dropped}; "
          f"kept after filters: {len(events)}")
    if events:
        from collections import Counter
        for lbl, n in Counter((e["source"], e["label"]) for e in events).most_common():
            print(f"    [{lbl[0]}] {lbl[1]}: {n}")
    return events


def main():
    args = parse_args()

    inputs = []
    for spec in args.track:
        if "=" not in spec:
            sys.exit(f"Bad --track: {spec}")
        sp, path = spec.split("=", 1)
        inputs.append((sp.strip(), Path(path.strip())))
    if args.audio:
        inputs.append((args.speaker or "unknown", Path(args.audio)))
    if not inputs:
        sys.exit("Provide --track or --audio.")
    for sp, p in inputs:
        if not p.exists():
            sys.exit(f"File not found: {p}")

    model, classes = load_yamnet()
    matched = [n for n in NOISE_LABELS if n in classes]
    print(f"  YAMNet: {len(classes)} classes; {len(matched)} noise labels matched.")

    all_words = load_all_words(args.words) if args.words else {}
    if all_words:
        for sp, ivs in all_words.items():
            print(f"  [{sp}] {len(ivs)} word intervals")

    all_events = []
    for sp, path in inputs:
        all_events.extend(detect_track(path, sp, model, classes, all_words, args))

    all_events.sort(key=lambda e: (e["start"], e["track"]))
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_events, f, ensure_ascii=False, indent=2)

    print("")
    print(f"Wrote {len(all_events)} events to {args.output}")
    if all_events:
        total = sum(e["end"] - e["start"] for e in all_events)
        print(f"  Total flagged duration: {total:.2f}s\n")
        print("  All events:")
        for e in all_events:
            vf = e.get("voiced_frac")
            vf_str = f" voiced={vf:.2f}" if vf is not None else ""
            print(f"    [{e['track']:>10}] {e['start']:7.2f}-{e['end']:7.2f}  "
                  f"{e['source']:5}  {e['label']:<32} conf={e['confidence']:.2f}{vf_str}")


if __name__ == "__main__":
    main()
