#!/usr/bin/env python3
"""
Stage 1.5 — Refine word timestamps via wav2vec2 forced alignment (WhisperX).

⚠️  STATUS: WIP. The current implementation produces large drift (~36 s) on
the test multitrack run because whisperx.align() expects whisper-native
segments with text matched to audio windows, not pre-computed word lists.
The script backs up the original first; if alignment looks wrong, restore via
    cp subtitles_words_pre_align.json subtitles_words.json
Future work: drive whisperx end-to-end (Whisper transcribe → align) per track
rather than retro-fitting our subtitles_words format.

Whisper's attention-based decoder gives word timestamps with ±100-200ms
error. That's why filler/stutter cuts often land mid-speech (the audio-energy
check in safe_filler_cut.py rejects them). WhisperX wraps wav2vec2 CTC
forced alignment to refine each word's start/end to ±20ms.

This script runs ON TOP of the existing whisper_*.json + subtitles_words.json
output — it doesn't replace the transcribe step. The original is backed up.

Reads:
    {BASE_DIR}/1_transcript/audio.mp3   (or per-track audio for multitrack)
    {BASE_DIR}/1_transcript/whisper_transcription.json   (single-track)
    {BASE_DIR}/1_transcript/whisper_<speaker>.json       (multitrack)
    {BASE_DIR}/1_transcript/subtitles_words.json

Writes:
    {BASE_DIR}/1_transcript/subtitles_words_pre_align.json   (backup)
    {BASE_DIR}/1_transcript/subtitles_words.json             (refined in place)
    {BASE_DIR}/1_transcript/alignment_report.json            (precision stats)

Usage:
    python3 refine_word_alignment.py <BASE_DIR>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_LANG = 'zh'


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('base_dir')
    ap.add_argument('--language', default=DEFAULT_LANG)
    ap.add_argument('--device', default='cpu', choices=['cpu', 'cuda'])
    return ap.parse_args()


def import_whisperx():
    try:
        import whisperx
        return whisperx
    except ImportError:
        print('❌ whisperx not installed. Run: pip install whisperx', file=sys.stderr)
        sys.exit(1)


def words_to_whisperx_segments(words: list[dict]) -> list[dict]:
    """Convert subtitles_words → whisperx-friendly segments grouped by gap."""
    actual = [w for w in words if not w.get('isGap') and not w.get('isSpeakerLabel')]
    if not actual:
        return []
    # One segment per gap-bounded run
    segs = []
    cur = {'start': actual[0]['start'], 'end': actual[0]['end'], 'text': '', 'words': []}
    prev_end = actual[0]['start']
    for w in actual:
        if w['start'] - prev_end > 1.0 and cur['words']:
            segs.append(cur)
            cur = {'start': w['start'], 'end': w['end'], 'text': '', 'words': []}
        cur['words'].append({
            'word': w['text'],
            'start': w['start'],
            'end': w['end'],
        })
        cur['end'] = w['end']
        cur['text'] += w['text']
        prev_end = w['end']
    if cur['words']:
        segs.append(cur)
    return segs


def align_track(audio_path: Path, words_for_track: list[dict],
                 language: str, device: str, whisperx_mod) -> tuple[dict[int, tuple[float, float]], dict]:
    """Align this track's words against its audio. Returns:
        - dict mapping word's index-in-input → (new_start, new_end)
        - stats dict (drift_ms_mean, drift_ms_p95, aligned_count, total_count)
    """
    if not audio_path.exists():
        print(f'   ⚠️  audio not found: {audio_path}', file=sys.stderr)
        return {}, {'error': 'audio_missing'}

    indexed = [(i, w) for i, w in enumerate(words_for_track)
               if not w.get('isGap') and not w.get('isSpeakerLabel')]
    if not indexed:
        return {}, {'aligned_count': 0, 'total_count': 0}

    segments = words_to_whisperx_segments(words_for_track)
    if not segments:
        return {}, {'aligned_count': 0, 'total_count': 0}

    print(f'   loading wav2vec2 align model for {language} ...')
    align_model, metadata = whisperx_mod.load_align_model(language_code=language, device=device)
    print(f'   loading audio ...')
    audio = whisperx_mod.load_audio(str(audio_path))
    print(f'   aligning {len(indexed)} words across {len(segments)} segments ...')
    aligned = whisperx_mod.align(segments, align_model, metadata, audio, device,
                                   return_char_alignments=False)

    # whisperx returns {"segments": [{"words": [{"word", "start", "end", "score"}]}], ...}
    refined_words = []
    for seg in aligned.get('segments', []):
        for w in seg.get('words', []):
            if 'start' in w and 'end' in w:
                refined_words.append(w)

    # Match refined words back to original indexed positions by sequence order
    # (Chinese is character-level; whisperx splits 'text' by chars, so order is preserved).
    drift_ms = []
    out: dict[int, tuple[float, float]] = {}
    n_match = min(len(refined_words), len(indexed))
    for k in range(n_match):
        orig_i, orig_w = indexed[k]
        ref = refined_words[k]
        ds = abs(ref['start'] - orig_w.get('start', 0)) * 1000
        de = abs(ref['end'] - orig_w.get('end', 0)) * 1000
        drift_ms.append(max(ds, de))
        # Only update if the shift is sane (< 1s) — otherwise wav2vec2 mis-aligned
        if max(ds, de) < 1000:
            out[orig_i] = (ref['start'], ref['end'])

    import numpy as np
    stats = {
        'total_words': len(indexed),
        'wx_aligned': len(refined_words),
        'applied': len(out),
        'drift_ms_mean': round(float(np.mean(drift_ms)), 1) if drift_ms else 0,
        'drift_ms_p50': round(float(np.percentile(drift_ms, 50)), 1) if drift_ms else 0,
        'drift_ms_p95': round(float(np.percentile(drift_ms, 95)), 1) if drift_ms else 0,
    }
    return out, stats


def main():
    args = parse_args()
    base = Path(args.base_dir).resolve()
    transcript_dir = base / '1_transcript'
    words_path = transcript_dir / 'subtitles_words.json'
    if not words_path.exists():
        print(f'❌ {words_path} not found', file=sys.stderr); sys.exit(1)

    words = json.loads(words_path.read_text(encoding='utf-8'))

    # Backup original
    backup = transcript_dir / 'subtitles_words_pre_align.json'
    if not backup.exists():
        backup.write_text(json.dumps(words, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'📦 backed up original → {backup.name}')

    whisperx_mod = import_whisperx()

    manifest_path = transcript_dir / 'whisper_multitrack_manifest.json'
    track_audio_for: dict[str, Path] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for tr in manifest.get('tracks', []):
            track_audio_for[tr['speaker']] = Path(tr['source'])
        print(f'📡 multitrack: aligning each speaker against their own track')
    else:
        # Single-track fallback
        for cand in ('audio.mp3', 'audio.wav', 'audio_seekable.mp3'):
            p = transcript_dir / cand
            if p.exists():
                track_audio_for['_single'] = p
                break
        if not track_audio_for:
            print('❌ no audio file found in 1_transcript/', file=sys.stderr); sys.exit(1)

    overall_stats: dict[str, dict] = {}

    if '_single' in track_audio_for:
        refined, stats = align_track(
            track_audio_for['_single'], words, args.language, args.device, whisperx_mod
        )
        overall_stats['_single'] = stats
        for i, (s, e) in refined.items():
            words[i]['start'] = round(float(s), 4)
            words[i]['end'] = round(float(e), 4)
    else:
        # Per-track: split words by speaker, align each independently
        for spk, audio_path in track_audio_for.items():
            print(f'🎙️  track: {spk}')
            # Slice indices belonging to this speaker
            spk_words = [w for w in words if w.get('speaker') == spk]
            # Need to keep mapping back to original indices
            orig_indices = [i for i, w in enumerate(words) if w.get('speaker') == spk]
            refined, stats = align_track(
                audio_path, spk_words, args.language, args.device, whisperx_mod
            )
            overall_stats[spk] = stats
            for local_i, (s, e) in refined.items():
                orig_i = orig_indices[local_i]
                words[orig_i]['start'] = round(float(s), 4)
                words[orig_i]['end'] = round(float(e), 4)

    words_path.write_text(json.dumps(words, ensure_ascii=False, indent=2), encoding='utf-8')

    report = {
        'language': args.language,
        'device': args.device,
        'per_track': overall_stats,
    }
    (transcript_dir / 'alignment_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'✅ refined timestamps written to {words_path.name}')
    for tr, st in overall_stats.items():
        if 'error' in st:
            print(f'   {tr}: ERROR {st["error"]}')
            continue
        print(f'   {tr}: {st["applied"]}/{st["total_words"]} words refined; '
              f'drift mean {st["drift_ms_mean"]}ms p95 {st["drift_ms_p95"]}ms')


if __name__ == '__main__':
    main()
