#!/usr/bin/env python3
"""
Stage 2.7 — Multitrack backchannel / bleed detection.

In multitrack recordings (one mic per speaker) the ASR sometimes hears the
other speaker's "對對對" / "嗯" / "好" through bleed and assigns it to the
wrong person. Or the listener track captures a short backchannel that the
speaker meant as agreement, not content.

This pre-pass cross-checks every word against silero-VAD run on the speaker's
OWN track. If the speaker's track has no voice activity at the word's
timestamp, the word is bleed → flag for deletion.

Reads:
    {BASE_DIR}/1_transcript/whisper_multitrack_manifest.json   (per-track audio paths)
    {BASE_DIR}/1_transcript/subtitles_words.json

Writes:
    {BASE_DIR}/2_analysis/backchannel_candidates.json

Usage:
    python3 detect_backchannel_multitrack.py <BASE_DIR>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('base_dir')
    ap.add_argument('--vad-threshold', type=float, default=0.5,
                    help='silero-vad speech probability threshold (0-1)')
    ap.add_argument('--coverage-floor', type=float, default=0.3,
                    help='Word is bleed if its speaker-track VAD coverage < this')
    ap.add_argument('--short-sentence-cap', type=int, default=2,
                    help='Sentence ≤ this many chars + low-coverage = strong backchannel signal')
    ap.add_argument('--apply', action='store_true',
                    help='Mutate subtitles_words.json — mark strong-signal backchannels with '
                         'isBackchannel=true so the downstream sentence splitter skips them. '
                         'Original backed up to subtitles_words_pre_backchannel.json.')
    return ap.parse_args()


def load_silero():
    """Load silero-vad model (cached after first call)."""
    try:
        from silero_vad import load_silero_vad, get_speech_timestamps, read_audio
    except ImportError:
        print('❌ silero-vad not installed. Run: pip install silero-vad', file=sys.stderr)
        sys.exit(1)
    return load_silero_vad(), get_speech_timestamps, read_audio


def run_vad_on_track(audio_path: Path, threshold: float):
    """Return a list of (start_s, end_s) speech segments."""
    model, get_speech_timestamps, read_audio = load_silero()
    wav = read_audio(str(audio_path), sampling_rate=16000)
    ts = get_speech_timestamps(
        wav, model,
        sampling_rate=16000,
        threshold=threshold,
        min_speech_duration_ms=120,    # filter tiny blips
        min_silence_duration_ms=80,
        return_seconds=True,
    )
    return [(t['start'], t['end']) for t in ts]


def vad_coverage(segments: list[tuple[float, float]], t_start: float, t_end: float) -> float:
    """Fraction of [t_start, t_end] that overlaps any VAD speech segment."""
    if t_end <= t_start:
        return 0.0
    overlap = 0.0
    for s, e in segments:
        if e < t_start:
            continue
        if s > t_end:
            break
        overlap += max(0, min(e, t_end) - max(s, t_start))
    return overlap / (t_end - t_start)


def main():
    args = parse_args()
    base = Path(args.base_dir).resolve()
    manifest_path = base / '1_transcript/whisper_multitrack_manifest.json'
    words_path = base / '1_transcript/subtitles_words.json'

    if not manifest_path.exists():
        print(f'⚠️  Not a multitrack run (no manifest at {manifest_path}); skipping.')
        sys.exit(0)

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    words_all = json.loads(words_path.read_text(encoding='utf-8'))
    actual = [w for w in words_all if not w.get('isGap') and not w.get('isSpeakerLabel')]

    # ── 1. Run VAD on each track once ──
    print(f'🎚️  Running silero-VAD on {len(manifest["tracks"])} tracks ...')
    speaker_segments: dict[str, list[tuple[float, float]]] = {}
    for track in manifest['tracks']:
        spk = track['speaker']
        src = Path(track['source'])
        if not src.exists():
            print(f'   ⚠️  {spk}: track file not found at {src}, skipping', file=sys.stderr)
            continue
        segs = run_vad_on_track(src, args.vad_threshold)
        total_speech = sum(e - s for s, e in segs)
        print(f'   {spk}: {len(segs)} speech segments, {total_speech:.1f}s total')
        speaker_segments[spk] = segs

    # ── 2. Build short-sentence map for the strong-signal rule ──
    # A short isolated word ASR'd onto speaker X while X's own track is quiet
    # is almost always backchannel that should be removed.
    sentence_lengths_chars: dict[int, int] = {}
    sentences_path = base / '2_analysis/sentences.txt'
    if sentences_path.exists():
        for line in sentences_path.read_text(encoding='utf-8').splitlines():
            parts = line.split('|', 3)
            if len(parts) < 4:
                continue
            try:
                sidx = int(parts[0])
                # remove punctuation/space for "real char" count
                text = ''.join(c for c in parts[3] if c.strip() and c not in '，。！？、：；""''（）')
                sentence_lengths_chars[sidx] = len(text)
            except ValueError:
                continue

    # ── 3. Walk every word, compute coverage on its speaker's track ──
    candidates = []
    next_id = 0
    word_global_idx = -1
    sentence_word_running_idx: dict[int, int] = {}

    # Map each word to its sentence via sentences.txt (rebuild)
    word_to_sentence: dict[int, int] = {}
    if sentences_path.exists():
        for line in sentences_path.read_text(encoding='utf-8').splitlines():
            parts = line.split('|', 3)
            if len(parts) < 4:
                continue
            try:
                sidx = int(parts[0])
                ws, we = parts[1].split('-')
                for wi in range(int(ws), int(we) + 1):
                    word_to_sentence[wi] = sidx
            except ValueError:
                continue

    for idx, w in enumerate(actual):
        spk = w.get('speaker', '')
        if spk not in speaker_segments:
            continue
        t_start = w.get('start', 0)
        t_end = w.get('end', 0)
        if t_end <= t_start:
            continue
        cov = vad_coverage(speaker_segments[spk], t_start, t_end)
        if cov >= args.coverage_floor:
            continue

        sentence_idx = word_to_sentence.get(idx)
        sentence_len = sentence_lengths_chars.get(sentence_idx, 999)
        strong_signal = sentence_len <= args.short_sentence_cap

        candidates.append({
            'id': f'bc_{next_id}',
            'wordIdx': idx,
            'sentenceIdx': sentence_idx,
            'speaker': spk,
            'deleteText': w.get('text', ''),
            'deleteStart': round(t_start, 3),
            'deleteEnd': round(t_end, 3),
            'vad_coverage': round(cov, 2),
            'sentence_chars': sentence_len,
            'type': 'backchannel',
            'reason': (f"{spk} 自己的軌在此處無人聲活動（VAD coverage {cov:.0%}）"
                        + (f"；且整句僅 {sentence_len} 字" if strong_signal else '')),
            # Strong signal (短句 + 低 coverage) → default-enabled; weak → user decides.
            'enabled': strong_signal,
            'confidence': 0.85 if strong_signal else 0.5,
        })
        next_id += 1

    out = {
        'summary': {
            'total_candidates': len(candidates),
            'auto_enabled': sum(1 for c in candidates if c['enabled']),
            'tracks_processed': sorted(speaker_segments.keys()),
            'vad_threshold': args.vad_threshold,
            'coverage_floor': args.coverage_floor,
        },
        'candidates': candidates,
    }

    out_path = base / '2_analysis/backchannel_candidates.json'
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'✅ wrote {out_path}')
    print(f'   {len(candidates)} candidates ({out["summary"]["auto_enabled"]} strong signal → auto-enabled)')

    # ── --apply: mutate subtitles_words.json so downstream sentence split skips bleed ──
    if args.apply:
        backup = base / '1_transcript/subtitles_words_pre_backchannel.json'
        if not backup.exists():
            backup.write_text(json.dumps(words_all, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'📦 backed up original → {backup.name}')
        strong_word_indices = {c['wordIdx'] for c in candidates if c['enabled']}
        if not strong_word_indices:
            print('ℹ️  no strong-signal candidates → nothing to apply')
            return
        # Re-index into words_all (skipping isGap/isSpeakerLabel) to find the matching positions
        marked = 0
        actual_pos = 0
        for w in words_all:
            if w.get('isGap') or w.get('isSpeakerLabel'):
                continue
            if actual_pos in strong_word_indices:
                w['isBackchannel'] = True
                marked += 1
            actual_pos += 1
        words_path.write_text(json.dumps(words_all, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'✅ marked {marked} backchannel words in {words_path.name}')


if __name__ == '__main__':
    main()
