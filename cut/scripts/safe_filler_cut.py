#!/usr/bin/env python3
"""
Stage 4.5: Safe filler-cut planner.

For each fine_analysis edit, runs an evidence check (text / duration / audio
energy / seam continuity), snaps cut points to the nearest energy valley
(consonant closure) then zero-crossing, and validates them against the local
silence floor. Connected-speech fillers — the common case in Mandarin, where
the filler is fused to its neighbours with no silence gap — are cuttable when
both cut points land in a deep enough valley AND pitch/timbre are continuous
across the resulting seam. Produces safe_cut_plan.json which the cutter and
review UI consume:

  verdict ∈ {"auto", "review", "skip"}
    auto   — safe to delete without human review
    review — text says delete, but evidence is mixed → user must confirm
    skip   — evidence strongly disagrees (e.g. cut point lands on speech)

The original fine_analysis.json is NOT mutated; this is a pure overlay.

Usage:
  python3 safe_filler_cut.py \
      --analysis-dir output/<run>/cut/2_analysis \
      --audio        output/<run>/cut/1_transcript/audio.mp3 \
      [--words       output/<run>/cut/1_transcript/subtitles_words.json] \
      [--out         output/<run>/cut/2_analysis/safe_cut_plan.json]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import librosa

# Edit types this planner evaluates. Silence-only edits are passed through as auto
# (they cut empty space; the existing pipeline already snaps them safely).
FILLER_TYPES = {
    'filler', 'consecutive_filler', 'in_sentence_filler', 'single_filler',
    'stutter', 'self_correction', 'self_correction_rules',
    'residual_sentence', 'llm_edit',
}
SILENCE_TYPES = {'silence', 'silence_merged'}
# New-detector types carry their own decision; the safe-cut planner trusts
# their `enabled` + `confidence` rather than re-running the 4-evidence check.
SELF_DECIDED_TYPES = {'speaker_tic', 'backchannel', 'gemini_filler', 'non_speech_vocal'}

# Conservative words: always force `review` regardless of evidence. These are
# the high-risk verbal tics from editing-rules/2-filler-detection.md that may
# carry rhythm / style, not noise.
CONSERVATIVE_WORDS = {'對', '然後', '就是', '那個', '這個'}

# Per-minute budget — beyond this we throttle to `review` to prevent compounding
# errors. 8/min is roughly one cut every 7.5 s, the empirical comfort ceiling.
PER_MINUTE_BUDGET = 8

# Snap parameters
SNAP_HALF_WINDOW_MS = 20          # ±20 ms search for zero crossing
SILENCE_PROBE_HALF_MS = 30        # ±30 ms RMS window around cut point
SILENCE_FLOOR_HEADROOM_DB = 6.0   # cut-point RMS must be ≤ noise_floor + 6 dB
# Energy-valley search: most connected-speech fillers have no silence around
# them, but Mandarin syllables almost always start with a stop/affricate/
# fricative, so there is a 20-60 ms low-energy consonant closure between
# syllables even in fluent speech. Cutting at that valley is what a human
# editor does. (Same approach as refine_boundaries.py, inlined here.)
VALLEY_HALF_WINDOW_MS = 80        # ±80 ms search window around each cut point
VALLEY_FRAME_MS = 5               # RMS envelope frame size
VALLEY_MIN_DROP_DB = 3.0          # minimum depth (vs local mean) to relocate a cut point
VALLEY_SAFE_DROP_DB = 6.0         # depth that counts as a safe boundary on its own
# Seam continuity: when both sides of the seam are voiced, the join is only
# invisible if pitch and timbre are continuous across it.
CONTINUITY_WIN_MS = 120           # analysis window on each side of the seam
                                  # (pyin frame is 64 ms @16 kHz; 120 ms gives
                                  # ~4 frames — enough for a stable voicing call)
F0_MAX_JUMP_SEMITONES = 2.5       # larger pitch jump across the seam → audible splice
MFCC_MIN_SIMILARITY = 0.45        # cosine similarity of mean MFCC vectors (c1..c12)
INNER_SNAP_MS = 30                # Tier-2 breath preservation: pull cut points inward
                                  # by this much if both new positions are still in silence
                                  # → kept side gets 30 ms of natural pause/ambient sound,
                                  # so the cut transitions through breath instead of
                                  # ripping immediately into the next word.
MID_FILLER_MAX_DUR = 0.5          # in-sentence single-char filler must be < 0.5 s
GAP_NATURAL_MS = 80               # ≥80 ms gap on at least one side counts as natural breath
SR_TARGET = 16000                 # 16 kHz mono is enough for energy/zero-crossing


def db(x: float) -> float:
    return 20.0 * float(np.log10(max(x, 1e-10)))


def find_zero_crossing(y: np.ndarray, sr: int, target_s: float,
                        half_window_ms: int = SNAP_HALF_WINDOW_MS) -> float:
    """Return the time (s) of the zero crossing nearest `target_s` within ±window."""
    center = int(target_s * sr)
    half = int(sr * half_window_ms / 1000)
    lo, hi = max(0, center - half), min(len(y) - 1, center + half)
    if hi <= lo + 1:
        return target_s
    segment = y[lo:hi + 1]
    signs = np.sign(segment)
    # Where the sign changes (or hits zero)
    crossings = np.where(np.diff(signs) != 0)[0]
    if len(crossings) == 0:
        return target_s
    # Pick the crossing closest to center
    local_center = center - lo
    best = crossings[np.argmin(np.abs(crossings - local_center))]
    return (lo + best) / sr


def find_energy_valley(y: np.ndarray, sr: int, target_s: float,
                        half_window_ms: int = VALLEY_HALF_WINDOW_MS) -> tuple[float, float]:
    """Find the nearest qualifying RMS-energy valley around `target_s`.

    Returns (valley_time_s, drop_db). drop_db is the valley's depth below the
    local mean; (target_s, 0.0) when no valley ≥ VALLEY_MIN_DROP_DB exists in
    the window — caller keeps the original point in that case.
    """
    half = half_window_ms / 1000.0
    a = max(0, int((target_s - half) * sr))
    b = min(len(y), int((target_s + half) * sr))
    frame = max(8, int(VALLEY_FRAME_MS / 1000.0 * sr))
    n_frames = (b - a) // frame
    if n_frames < 4:
        return target_s, 0.0
    seg = y[a:a + n_frames * frame]
    rms = np.sqrt(np.mean(seg.reshape(n_frames, frame) ** 2, axis=1))
    if len(rms) >= 3:
        rms = np.convolve(rms, np.ones(3) / 3, mode='same')
    mean_rms = max(float(np.mean(rms)), 1e-10)
    center_f = (target_s * sr - a) / frame
    qualified = []
    for i in range(len(rms)):
        left_ok = (i == 0) or (rms[i] <= rms[i - 1])
        right_ok = (i == len(rms) - 1) or (rms[i] <= rms[i + 1])
        if left_ok and right_ok:
            drop = 20.0 * np.log10(mean_rms / max(float(rms[i]), 1e-10))
            if drop >= VALLEY_MIN_DROP_DB:
                qualified.append((i, drop))
    if not qualified:
        return target_s, 0.0
    i, drop = min(qualified, key=lambda t: abs(t[0] - center_f))
    return float((a + i * frame + frame / 2) / sr), float(drop)


def median_f0(x: np.ndarray, sr: int) -> float | None:
    """Median F0 (Hz) over voiced frames, or None when the window is unvoiced."""
    if len(x) < 512:
        return None
    try:
        f0, voiced, _ = librosa.pyin(x, fmin=65, fmax=400, sr=sr,
                                       frame_length=1024, hop_length=256)
    except Exception:
        return None
    if f0 is None or voiced is None:
        return None
    vals = f0[np.asarray(voiced, dtype=bool)]
    vals = vals[np.isfinite(vals)]
    if len(vals) < 2:
        return None
    return float(np.median(vals))


def seam_continuity(y: np.ndarray, sr: int, cut_s: float, cut_e: float) -> dict:
    """Will [..., cut_s] joined to [cut_e, ...] sound like one utterance?

    A splice is audible mainly when BOTH sides of the seam are voiced and pitch
    or timbre jumps across it. If either side is unvoiced (consonant closure,
    breath, silence) the join is masked and passes automatically.
    """
    win = int(CONTINUITY_WIN_MS / 1000.0 * sr)
    a0, a1 = max(0, int(cut_s * sr) - win), max(0, int(cut_s * sr))
    b0, b1 = min(len(y), int(cut_e * sr)), min(len(y), int(cut_e * sr) + win)
    out = {'pass': True, 'voiced_seam': False,
           'f0_jump_semitones': None, 'mfcc_similarity': None}
    if a1 - a0 < win // 2 or b1 - b0 < win // 2:
        return out
    before, after = y[a0:a1], y[b0:b1]

    f0_before = median_f0(before, sr)
    f0_after = median_f0(after, sr)
    if f0_before is None or f0_after is None:
        return out  # at least one side unvoiced → splice masked
    out['voiced_seam'] = True
    jump = abs(12.0 * np.log2(f0_after / f0_before))
    out['f0_jump_semitones'] = round(float(jump), 2)

    mf_b = librosa.feature.mfcc(y=before, sr=sr, n_mfcc=13, n_fft=512, hop_length=256)
    mf_a = librosa.feature.mfcc(y=after, sr=sr, n_mfcc=13, n_fft=512, hop_length=256)
    vb = np.mean(mf_b[1:], axis=1)   # drop c0 (loudness)
    va = np.mean(mf_a[1:], axis=1)
    denom = (np.linalg.norm(vb) * np.linalg.norm(va))
    sim = float(np.dot(vb, va) / denom) if denom > 1e-10 else 0.0
    out['mfcc_similarity'] = round(sim, 3)

    out['pass'] = bool(jump <= F0_MAX_JUMP_SEMITONES and sim >= MFCC_MIN_SIMILARITY)
    return out


def inner_snap_if_quiet(y: np.ndarray, sr: int, snap_s: float, snap_e: float,
                          ceiling: float) -> tuple[float, float]:
    """Tier-2: pull cut points INWARD by INNER_SNAP_MS to preserve breath.

    "Inward" = the cut becomes NARROWER:
       deleteStart moves LATER  → more of [orig_s, inner_s] kept on the source side
       deleteEnd   moves EARLIER → more of [inner_e, orig_e] kept on the source side

    The exposed-to-listener regions are [orig_s, inner_s] and [inner_e, orig_e].
    For this to be safe (no exposed speech), those regions must currently be
    silence in the source audio. If yes, we keep 30 ms of natural pause around
    each cut, so the seam transitions through ambient sound, not abruptly.

    Falls back to original snap when:
      • exposed regions contain speech (would un-delete real content)
      • the inner cut would be too short to be meaningful
    """
    pad = INNER_SNAP_MS / 1000.0
    inner_s = snap_s + pad
    inner_e = snap_e - pad
    if inner_e <= inner_s + 0.02:   # cut too short after pulling in
        return snap_s, snap_e
    # Check the regions that would be NEWLY KEPT (not the new boundaries)
    pa = max(0, int(snap_s * sr))
    pb = min(len(y), int(inner_s * sr))
    pc = max(0, int(inner_e * sr))
    pd = min(len(y), int(snap_e * sr))
    if pb <= pa or pd <= pc:
        return snap_s, snap_e
    exposed_start_rms = float(np.sqrt(np.mean(y[pa:pb] ** 2)))
    exposed_end_rms = float(np.sqrt(np.mean(y[pc:pd] ** 2)))
    if exposed_start_rms <= ceiling and exposed_end_rms <= ceiling:
        return inner_s, inner_e
    return snap_s, snap_e


def rms_in_window(y: np.ndarray, sr: int, t: float, half_ms: int) -> float:
    a = max(0, int((t - half_ms / 1000) * sr))
    b = min(len(y), int((t + half_ms / 1000) * sr))
    if b <= a:
        return 0.0
    return float(np.sqrt(np.mean(y[a:b] ** 2)))


def compute_noise_floor(y: np.ndarray, sr: int) -> float:
    """10th-percentile RMS over 50 ms hop frames → quiet-room reference."""
    frame_len = int(0.05 * sr)
    if frame_len < 32 or len(y) < frame_len * 4:
        return float(np.sqrt(np.mean(y ** 2)) * 0.25)
    rms = librosa.feature.rms(y=y, frame_length=frame_len, hop_length=frame_len)[0]
    return float(np.percentile(rms, 10))


def find_neighbour_words(words, t_start: float, t_end: float):
    prev_w, next_w = None, None
    for w in words:
        if w.get('isGap') or w.get('isSpeakerLabel'):
            continue
        if w.get('end', 0) <= t_start + 1e-3:
            prev_w = w
        if w.get('start', 0) >= t_end - 1e-3 and next_w is None:
            next_w = w
    return prev_w, next_w


def evaluate_edit(edit, y, sr, noise_floor, words):
    """Return (verdict, evidence_dict, snapped_start, snapped_end)."""
    etype = edit.get('type', '')
    ds = edit.get('deleteStart') or edit.get('ds') or 0.0
    de = edit.get('deleteEnd') or edit.get('de') or 0.0
    if not ds or not de or de <= ds:
        return 'skip', {'reason': 'invalid_range'}, ds, de

    # Silence edits: pass through as auto.
    if etype in SILENCE_TYPES:
        snapped_s = find_zero_crossing(y, sr, ds)
        snapped_e = find_zero_crossing(y, sr, de)
        return 'auto', {'kind': 'silence_passthrough'}, snapped_s, snapped_e

    # Snap cut points: energy valley first (consonant closure / micro-pause in
    # connected speech), then zero crossing at the chosen point.
    valley_s, s_drop = find_energy_valley(y, sr, ds)
    valley_e, e_drop = find_energy_valley(y, sr, de)
    # Guard: valley relocation must not invert or collapse the cut.
    if valley_e <= valley_s + 0.02:
        valley_s, s_drop = ds, 0.0
        valley_e, e_drop = de, 0.0
    snapped_s = find_zero_crossing(y, sr, valley_s)
    snapped_e = find_zero_crossing(y, sr, valley_e)
    valley_info = {
        'start_applied': bool(abs(valley_s - ds) > 1e-4 and s_drop > 0),
        'end_applied': bool(abs(valley_e - de) > 1e-4 and e_drop > 0),
        'start_drop_db': round(s_drop, 1),
        'end_drop_db': round(e_drop, 1),
    }

    # Detector-decided types: trust their own `enabled` + `confidence` for
    # intent, but still validate the AUDIO at the actual cut points — Gemini
    # timestamps in particular can be hundreds of ms off, and an auto cut
    # landing mid-speech is the worst possible artefact.
    if etype in SELF_DECIDED_TYPES:
        enabled = edit.get('enabled', False)
        conf = float(edit.get('confidence', 0.5))
        start_rms = rms_in_window(y, sr, snapped_s, SILENCE_PROBE_HALF_MS)
        end_rms = rms_in_window(y, sr, snapped_e, SILENCE_PROBE_HALF_MS)
        ceiling = noise_floor * (10 ** (SILENCE_FLOOR_HEADROOM_DB / 20))
        start_ok = start_rms <= ceiling or s_drop >= VALLEY_SAFE_DROP_DB
        end_ok = end_rms <= ceiling or e_drop >= VALLEY_SAFE_DROP_DB
        continuity = seam_continuity(y, sr, snapped_s, snapped_e) \
            if (start_rms > ceiling or end_rms > ceiling) \
            else {'pass': True, 'voiced_seam': False,
                  'f0_jump_semitones': None, 'mfcc_similarity': None}
        if enabled and conf >= 0.8 and start_ok and end_ok and continuity['pass']:
            verdict = 'auto'
        else:
            verdict = 'review'
        return verdict, {
            'kind': 'detector_decided',
            'detector': etype,
            'detector_enabled': enabled,
            'detector_confidence': conf,
            'reason': edit.get('reason', ''),
            'valley': valley_info,
            'continuity': continuity,
            'audio_energy': {
                'pass': bool(start_ok and end_ok),
                'start_rms_db': round(db(start_rms), 1),
                'end_rms_db': round(db(end_rms), 1),
                'ceiling_db': round(db(ceiling), 1),
            },
        }, snapped_s, snapped_e

    # === Evidence ===
    evidence = {}

    # 1) Text — present by virtue of this edit existing. Confidence varies by type.
    text_conf = 'high' if etype in {'filler', 'consecutive_filler', 'single_filler',
                                     'stutter'} else 'medium'
    delete_text_clean = (edit.get('deleteText') or '').strip()
    conservative = any(w in delete_text_clean for w in CONSERVATIVE_WORDS)
    evidence['text'] = {
        'pass': True,
        'confidence': text_conf,
        'conservative_word': conservative,
        'deleteText': delete_text_clean,
    }

    # 2) Duration — for in-sentence single-char fillers, duration > 0.5 s is
    # usually emphasis/interjection, not hesitation.
    dur = de - ds
    dur_pass = True
    dur_note = None
    if etype in {'in_sentence_filler', 'single_filler'} and len(delete_text_clean) <= 1:
        if dur > MID_FILLER_MAX_DUR:
            dur_pass = False
            dur_note = f'duration {dur:.2f}s exceeds {MID_FILLER_MAX_DUR}s — likely emphasis'
    evidence['duration'] = {'pass': dur_pass, 'value_s': round(dur, 3), 'note': dur_note}

    # 3) Silence gap on at least one side → natural breath / boundary.
    prev_w, next_w = find_neighbour_words(words, ds, de)
    prev_gap = (ds - prev_w['end']) if prev_w else None
    next_gap = (next_w['start'] - de) if next_w else None
    prev_gap_ms = round((prev_gap or 0) * 1000, 1)
    next_gap_ms = round((next_gap or 0) * 1000, 1)
    natural = max(prev_gap_ms, next_gap_ms) >= GAP_NATURAL_MS
    evidence['silence_gap'] = {
        'pass': natural,
        'prev_gap_ms': prev_gap_ms,
        'next_gap_ms': next_gap_ms,
        'threshold_ms': GAP_NATURAL_MS,
    }

    # 4) Audio energy — each cut point must be near the silence floor OR sit in
    # a deep energy valley (consonant closure between syllables). The valley
    # path is what makes connected-speech fillers cuttable at all.
    start_rms = rms_in_window(y, sr, snapped_s, SILENCE_PROBE_HALF_MS)
    end_rms = rms_in_window(y, sr, snapped_e, SILENCE_PROBE_HALF_MS)
    ceiling = noise_floor * (10 ** (SILENCE_FLOOR_HEADROOM_DB / 20))
    start_quiet = start_rms <= ceiling
    end_quiet = end_rms <= ceiling
    start_ok = start_quiet or s_drop >= VALLEY_SAFE_DROP_DB
    end_ok = end_quiet or e_drop >= VALLEY_SAFE_DROP_DB
    audio_pass = start_ok and end_ok
    # Tier-2: try inner-snap (preserve 30 ms of natural pause on each side)
    inner_s, inner_e = inner_snap_if_quiet(y, sr, snapped_s, snapped_e, ceiling)
    inner_applied = bool((inner_s != snapped_s) or (inner_e != snapped_e))
    evidence['inner_snap'] = {
        'applied': inner_applied,
        'pad_ms': INNER_SNAP_MS if inner_applied else 0,
        'inner_start': round(float(inner_s), 4),
        'inner_end': round(float(inner_e), 4),
    }
    evidence['audio_energy'] = {
        'pass': audio_pass,
        'start_rms_db': round(db(start_rms), 1),
        'end_rms_db': round(db(end_rms), 1),
        'noise_floor_db': round(db(noise_floor), 1),
        'ceiling_db': round(db(ceiling), 1),
    }
    evidence['valley'] = valley_info

    # 5) Seam continuity — only matters when the seam touches voiced audio;
    # quiet seams are masked by definition.
    if start_quiet and end_quiet:
        continuity = {'pass': True, 'voiced_seam': False,
                      'f0_jump_semitones': None, 'mfcc_similarity': None}
    else:
        continuity = seam_continuity(y, sr, snapped_s, snapped_e)
    evidence['continuity'] = continuity

    # === Verdict ===
    # auto  ← duration + audio (floor-or-valley) + seam continuity all pass,
    #          and not a conservative word. A natural silence gap is no longer
    #          required: connected-speech fillers (the majority) have none, and
    #          the valley + continuity checks validate the actual seam instead.
    # review ← any single piece of evidence is mixed, OR conservative word
    # skip  ← audio strongly disagrees (cut point is mid-speech on BOTH ends,
    #          with no usable valley)
    if not start_ok and not end_ok and (start_rms > ceiling * 2 and end_rms > ceiling * 2):
        verdict = 'skip'
    elif conservative:
        verdict = 'review'
    elif dur_pass and audio_pass and continuity['pass']:
        verdict = 'auto'
    else:
        verdict = 'review'

    # snapped_e/snapped_s returned by this fn are the OUTER (safer) ones; the
    # caller will pick inner vs outer based on whether breath preservation
    # succeeded. We stash the chosen pair in evidence.inner_snap.
    return verdict, evidence, snapped_s, snapped_e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--analysis-dir', required=True)
    ap.add_argument('--audio', required=True)
    ap.add_argument('--words', default=None)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    analysis_dir = Path(args.analysis_dir)
    fine_path = analysis_dir / 'fine_analysis.json'
    if not fine_path.exists():
        print(f'❌ fine_analysis.json not found at {fine_path}', file=sys.stderr)
        sys.exit(1)

    words_path = Path(args.words) if args.words else (analysis_dir / '../1_transcript/subtitles_words.json').resolve()
    if not words_path.exists():
        print(f'❌ subtitles_words.json not found at {words_path}', file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out) if args.out else (analysis_dir / 'safe_cut_plan.json')

    fine = json.loads(fine_path.read_text(encoding='utf-8'))
    words = json.loads(words_path.read_text(encoding='utf-8'))
    edits = fine.get('edits', [])

    print(f'🎧 Loading audio ({args.audio}) at {SR_TARGET} Hz mono ...')
    y, sr = librosa.load(args.audio, sr=SR_TARGET, mono=True)
    duration_s = len(y) / sr
    noise_floor = compute_noise_floor(y, sr)
    print(f'   duration {duration_s:.1f}s, noise floor {db(noise_floor):.1f} dB')

    # Silence / silence_merged are handled by the existing pipeline
    # (run_fine_analysis.js silence detection + merge_llm_fine.js post-merge
    # gap cleanup + cut_audio.py adaptive fade). The safe-cut planner stays
    # out of their way — its job is filler/stutter/self-correction only.
    skipped_silence = sum(1 for e in edits if e.get('type') in SILENCE_TYPES)
    edits = [e for e in edits if e.get('type') not in SILENCE_TYPES]
    print(f'🔬 Evaluating {len(edits)} filler-class edits ({skipped_silence} silence edits delegated to existing pipeline) ...')
    candidates = []
    inner_applied_count = 0
    for edit in edits:
        verdict, evidence, snap_s, snap_e = evaluate_edit(edit, y, sr, noise_floor, words)
        # Prefer inner-snapped (breath-preserving) cut points when the planner
        # confirmed both sides are quiet. Falls back to outer snap otherwise.
        inner = evidence.get('inner_snap') or {}
        if inner.get('applied'):
            inner_s, inner_e = inner['inner_start'], inner['inner_end']
            inner_applied_count += 1
        else:
            inner_s, inner_e = snap_s, snap_e
        candidates.append({
            'feIdx': edit.get('idx'),
            'sentenceIdx': edit.get('sentenceIdx'),
            'type': edit.get('type'),
            'rule': edit.get('rule'),
            'deleteText': edit.get('deleteText', ''),
            'original': {
                'start': round(edit.get('deleteStart') or edit.get('ds') or 0.0, 3),
                'end': round(edit.get('deleteEnd') or edit.get('de') or 0.0, 3),
            },
            'snapped': {'start': round(snap_s, 4), 'end': round(snap_e, 4)},
            'snapped_inner': {'start': round(float(inner_s), 4), 'end': round(float(inner_e), 4),
                                'applied': bool(inner.get('applied'))},
            'verdict': verdict,
            'evidence': evidence,
        })

    # Per-minute budget throttle: in any 60-s window, beyond PER_MINUTE_BUDGET
    # `auto` filler/stutter cuts, demote the rest to `review`. Silence cuts are
    # empty space and don't compound editing artefacts, so they don't count.
    candidates.sort(key=lambda c: c['original']['start'])
    auto_times = []
    throttled = 0
    for c in candidates:
        if c['verdict'] != 'auto' or c['type'] in SILENCE_TYPES:
            continue
        t = c['original']['start']
        auto_times = [x for x in auto_times if t - x <= 60.0]
        if len(auto_times) >= PER_MINUTE_BUDGET:
            c['verdict'] = 'review'
            c['evidence']['budget'] = {
                'pass': False,
                'note': f'>{PER_MINUTE_BUDGET} auto cuts in trailing 60s window',
            }
            throttled += 1
        else:
            auto_times.append(t)

    valley_count = sum(
        1 for c in candidates
        if ((c['evidence'].get('valley') or {}).get('start_applied')
            or (c['evidence'].get('valley') or {}).get('end_applied')))
    continuity_blocked = sum(
        1 for c in candidates
        if (c['evidence'].get('continuity') or {}).get('pass') is False)
    summary = {
        'total': len(candidates),
        'auto': sum(1 for c in candidates if c['verdict'] == 'auto'),
        'review': sum(1 for c in candidates if c['verdict'] == 'review'),
        'skip': sum(1 for c in candidates if c['verdict'] == 'skip'),
        'silence_passthrough': skipped_silence,
        'inner_snap_applied': inner_applied_count,
        'inner_snap_pad_ms': INNER_SNAP_MS,
        'valley_snap_applied': valley_count,
        'valley_half_window_ms': VALLEY_HALF_WINDOW_MS,
        'valley_safe_drop_db': VALLEY_SAFE_DROP_DB,
        'continuity_blocked': continuity_blocked,
        'budget_throttled': throttled,
        'noise_floor_db': round(db(noise_floor), 1),
        'per_minute_budget': PER_MINUTE_BUDGET,
        'snap_half_window_ms': SNAP_HALF_WINDOW_MS,
        'silence_probe_half_ms': SILENCE_PROBE_HALF_MS,
        'silence_floor_headroom_db': SILENCE_FLOOR_HEADROOM_DB,
    }

    plan = {'summary': summary, 'candidates': candidates}
    out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'✅ wrote {out_path}')
    print(f'   auto   {summary["auto"]:>4}')
    print(f'   review {summary["review"]:>4} ({throttled} budget-throttled, '
          f'{continuity_blocked} continuity-blocked)')
    print(f'   skip   {summary["skip"]:>4}')
    print(f'   valley snap applied on {valley_count} candidates')


if __name__ == '__main__':
    main()
