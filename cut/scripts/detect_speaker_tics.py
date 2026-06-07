#!/usr/bin/env python3
"""
Stage 2.6 — Per-speaker verbal-tic detection.

The sentence-by-sentence LLM scan can't see cross-sentence frequency, so
口頭禪 like "其實" / "我覺得" / "就是" slip through. This pre-pass aggregates
per-(speaker, token) frequency over the WHOLE episode and emits each
occurrence beyond a per-minute baseline as a low-confidence candidate.

Inputs:
    {BASE_DIR}/1_transcript/subtitles_words.json
    {BASE_DIR}/2_analysis/sentences.txt   (optional, for sentenceIdx mapping)

Output:
    {BASE_DIR}/2_analysis/speaker_tics.json

Usage:
    python3 detect_speaker_tics.py <BASE_DIR>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


# Common Chinese verbal-tic candidates. Words here are SCANNED for frequency
# but only flagged when a speaker uses them noticeably more than the baseline.
# Single-char "對/好/嗯" excluded — those are conversation responses handled
# by other rules. Focus on the multi-char hesitation/filler phrases.
TIC_CANDIDATES = {
    '其實', '就是', '然後', '那個', '這個', '我覺得',
    '真的', '好像', '反正', '基本上', '就是說', '怎麼說',
    '對啊', '其实', '就是说', '那个', '这个', '我觉得',
    '其實上', '然後就', '就是這樣', '基本上就', '說真的',
    '所以說', '我跟你說', '你知道', '我想說',
    # Simplified-Chinese equivalents (FunASR/Aliyun often outputs simplified)
    '我跟你说', '所以说', '你知道吗', '我想说', '说真的',
}

# Words that frequently appear as part of a normal sentence — high counts are
# expected and not a tic per se. Exclude from flagging.
EXCLUDE_FROM_FLAGGING = {'就', '對', '好', '嗯', '是', '的', '我', '你', '他'}


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('base_dir', help='cut/ output directory')
    ap.add_argument('--per-minute-threshold', type=float, default=2.0,
                    help='Flag candidate if a speaker uses it more than N times per minute')
    ap.add_argument('--min-occurrences', type=int, default=3,
                    help='Minimum total occurrences to consider')
    return ap.parse_args()


def load_words(base: Path) -> list[dict]:
    p = base / '1_transcript/subtitles_words.json'
    return json.loads(p.read_text(encoding='utf-8'))


def load_sentence_index(base: Path) -> list[tuple[int, int, int, str]]:
    """Parse sentences.txt → [(sIdx, wStart, wEnd, speaker), ...]."""
    p = base / '2_analysis/sentences.txt'
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding='utf-8').splitlines():
        parts = line.split('|', 3)
        if len(parts) < 4:
            continue
        try:
            sidx = int(parts[0])
            ws, we = parts[1].split('-')
            out.append((sidx, int(ws), int(we), parts[2]))
        except ValueError:
            continue
    return out


def find_sentence_for_word(word_idx: int, sentence_index) -> int | None:
    for sidx, ws, we, _spk in sentence_index:
        if ws <= word_idx <= we:
            return sidx
    return None


def main():
    args = parse_args()
    base = Path(args.base_dir).resolve()
    if not base.exists():
        print(f'❌ base dir not found: {base}', file=sys.stderr)
        sys.exit(1)

    words_all = load_words(base)
    sentence_index = load_sentence_index(base)
    actual = [w for w in words_all if not w.get('isGap') and not w.get('isSpeakerLabel')]

    if not actual:
        print('❌ no actual words found', file=sys.stderr)
        sys.exit(1)

    # ── 1. Compute per-speaker minutes (sum of word durations grouped by speaker) ──
    speaker_dur: dict[str, float] = defaultdict(float)
    for w in actual:
        s = w.get('speaker', '?')
        speaker_dur[s] += max(0.0, w.get('end', 0) - w.get('start', 0))
    speaker_minutes = {s: max(d / 60.0, 0.1) for s, d in speaker_dur.items()}

    # ── 2. Count per-(speaker, word) occurrences ──
    speaker_word_count: dict[tuple[str, str], int] = defaultdict(int)
    speaker_word_indices: dict[tuple[str, str], list[int]] = defaultdict(list)
    actual_idx = 0
    for w in actual:
        text = (w.get('text') or '').strip()
        if not text or text in EXCLUDE_FROM_FLAGGING:
            actual_idx += 1
            continue
        spk = w.get('speaker', '?')
        # Score against the tic candidate set: exact match OR a candidate appears as substring
        for cand in TIC_CANDIDATES:
            if text == cand or (len(text) >= 2 and cand in text):
                speaker_word_count[(spk, cand)] += 1
                speaker_word_indices[(spk, cand)].append(actual_idx)
                break  # one candidate match per word
        actual_idx += 1

    # ── 3. Decide which (speaker, word) pairs are tics ──
    flagged_pairs: list[dict] = []
    for (spk, word), count in speaker_word_count.items():
        if count < args.min_occurrences:
            continue
        per_min = count / speaker_minutes[spk]
        if per_min < args.per_minute_threshold:
            continue
        flagged_pairs.append({
            'speaker': spk,
            'word': word,
            'count': count,
            'speaker_minutes': round(speaker_minutes[spk], 2),
            'per_minute': round(per_min, 2),
            'word_indices': speaker_word_indices[(spk, word)],
        })

    flagged_pairs.sort(key=lambda x: -x['per_minute'])

    # ── 4. Emit per-occurrence candidates ──
    candidates = []
    next_id = 0
    for pair in flagged_pairs:
        spk = pair['speaker']
        word = pair['word']
        for word_idx in pair['word_indices']:
            w = actual[word_idx]
            sentence_idx = find_sentence_for_word(word_idx, sentence_index)
            candidates.append({
                'id': f'tic_{next_id}',
                'sentenceIdx': sentence_idx,
                'wordIdx': word_idx,
                'type': 'speaker_tic',
                'speaker': spk,
                'deleteText': w.get('text', ''),
                'matched_tic': word,
                'deleteStart': round(w.get('start', 0), 3),
                'deleteEnd': round(w.get('end', 0), 3),
                'reason': f"{spk} 在 {pair['speaker_minutes']:.1f} 分鐘內說「{word}」{pair['count']} 次（每分鐘 {pair['per_minute']}）",
                # Default to disabled — user must opt-in via review UI
                'enabled': False,
                'confidence': 0.6,
            })
            next_id += 1

    out = {
        'flagged_tics': flagged_pairs,
        'candidates': candidates,
        'summary': {
            'total_candidates': len(candidates),
            'total_tic_pairs': len(flagged_pairs),
            'speakers_seen': sorted(speaker_minutes.keys()),
            'speaker_minutes': {s: round(m, 2) for s, m in speaker_minutes.items()},
            'per_minute_threshold': args.per_minute_threshold,
            'min_occurrences': args.min_occurrences,
        },
    }

    out_path = base / '2_analysis/speaker_tics.json'
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'✅ wrote {out_path}')
    print(f'   flagged {len(flagged_pairs)} (speaker, tic) pairs → {len(candidates)} occurrence candidates')
    for p in flagged_pairs[:10]:
        print(f"   {p['speaker']:10s} 「{p['word']}」 {p['count']}x ({p['per_minute']}/min over {p['speaker_minutes']}min)")


if __name__ == '__main__':
    main()
