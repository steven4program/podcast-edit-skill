#!/usr/bin/env python3
"""
Quick patch: re-attach sentenceIdx on every fine_analysis edit by mapping its
deleteStart time → which (current) sentence covers that timestamp.

Necessary when the upstream sentence layout changes (e.g. Plan B per-speaker
splitter rewrote sentences.txt with new numbering) while edits inherited
sentenceIdx from the old layout. After this script, every edit in
fine_analysis.json points to a valid sentence in the current sentences.txt
(or null if its time range falls into no sentence).

Reads:
    {BASE_DIR}/2_analysis/sentences.txt
    {BASE_DIR}/2_analysis/fine_analysis.json
    {BASE_DIR}/1_transcript/subtitles_words.json

Writes:
    {BASE_DIR}/2_analysis/fine_analysis.json (in place; backup → fine_analysis_pre_reattach.json)

Usage:
    python3 reattach_sentence_idx.py <BASE_DIR>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('base_dir')
    return ap.parse_args()


def build_sentence_intervals(base: Path) -> list[tuple[int, float, float]]:
    """Return [(sentenceIdx, t_start, t_end), ...] from sentences.txt + subtitles_words."""
    sents_path = base / '2_analysis/sentences.txt'
    words_path = base / '1_transcript/subtitles_words.json'
    if not sents_path.exists() or not words_path.exists():
        print(f'❌ sentences.txt or subtitles_words.json missing', file=sys.stderr)
        sys.exit(1)
    words = json.loads(words_path.read_text(encoding='utf-8'))
    actual = [w for w in words
              if not w.get('isGap') and not w.get('isSpeakerLabel')
              and not w.get('isBackchannel')]

    intervals = []
    for line in sents_path.read_text(encoding='utf-8').splitlines():
        parts = line.split('|', 3)
        if len(parts) < 4:
            continue
        try:
            sidx = int(parts[0])
            ws, we = parts[1].split('-')
            ws, we = int(ws), int(we)
        except ValueError:
            continue
        if ws >= len(actual) or we >= len(actual):
            continue
        # The per-speaker splitter makes wordIdx ranges potentially non-contiguous
        # in time (other speakers interleave). Use the min start of any word in the
        # sentence and the max end — this defines the sentence's covered time span.
        # Words actually belonging to this sentence are identified by speaker.
        speaker = parts[2]
        sent_words = [actual[i] for i in range(ws, we + 1)
                       if actual[i].get('speaker') == speaker]
        if not sent_words:
            continue
        t_start = min(w.get('start', 0) for w in sent_words)
        t_end = max(w.get('end', 0) for w in sent_words)
        intervals.append((sidx, t_start, t_end))
    return intervals


def find_sentence_for_time(intervals, t: float) -> int | None:
    """Return the sentenceIdx whose [t_start, t_end] covers t. Ties broken by tightest fit."""
    candidates = [(s, ts, te) for s, ts, te in intervals if ts <= t <= te]
    if candidates:
        # Pick the tightest interval (smallest span containing t)
        candidates.sort(key=lambda c: c[2] - c[1])
        return candidates[0][0]
    # Fallback: closest sentence by start time
    best = None
    best_d = 9e9
    for s, ts, te in intervals:
        mid = (ts + te) / 2
        d = abs(mid - t)
        if d < best_d and d < 5.0:   # only attach if within 5s
            best = s
            best_d = d
    return best


def main():
    args = parse_args()
    base = Path(args.base_dir).resolve()
    fine_path = base / '2_analysis/fine_analysis.json'
    if not fine_path.exists():
        print(f'❌ fine_analysis.json not found at {fine_path}', file=sys.stderr); sys.exit(1)

    fine = json.loads(fine_path.read_text(encoding='utf-8'))
    edits = fine.get('edits', [])

    intervals = build_sentence_intervals(base)
    print(f'📚 {len(intervals)} sentence intervals built from sentences.txt')

    backup = base / '2_analysis/fine_analysis_pre_reattach.json'
    if not backup.exists():
        backup.write_text(json.dumps(fine, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'📦 backed up → {backup.name}')

    n_changed, n_unchanged, n_orphan = 0, 0, 0
    by_type_changed = {}
    for e in edits:
        t = e.get('deleteStart')
        if t is None:
            t = e.get('ds')
        if t is None:
            n_orphan += 1
            continue
        new_sidx = find_sentence_for_time(intervals, float(t))
        old_sidx = e.get('sentenceIdx')
        if new_sidx is None:
            e['sentenceIdx'] = None
            n_orphan += 1
            continue
        if new_sidx != old_sidx:
            e['sentenceIdx'] = new_sidx
            n_changed += 1
            t_type = e.get('type', '?')
            by_type_changed[t_type] = by_type_changed.get(t_type, 0) + 1
        else:
            n_unchanged += 1

    fine_path.write_text(json.dumps(fine, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'✅ re-attached: {n_changed} edits updated, {n_unchanged} unchanged, {n_orphan} no match')
    if by_type_changed:
        print('   by type:')
        for t, n in sorted(by_type_changed.items()):
            print(f'     {t}: {n}')


if __name__ == '__main__':
    main()
