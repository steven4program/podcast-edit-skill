#!/usr/bin/env python3
"""
Edit-sample analyser — extract editing preferences by comparing the source and
final versions of the same audio.

By comparing the source recording with the published cut, automatically infer
the user's editing style:
- filler-word deletion rate
- silence-segment processing threshold
- aggressiveness of content-block trimming
- handling of repetitions / verbal slips

Usage:
    python3 analyze_editing_samples.py \\
        --before /path/to/source.mp3 \\
        --after /path/to/published.mp3 \\
        --before-transcript /path/to/before_transcript.json \\
        --after-transcript /path/to/after_transcript.json \\
        --output /path/to/learned_patterns.json

If no transcripts are provided, the script will prompt to transcribe via Aliyun FunASR first.

Dependencies: pip install librosa numpy
"""

import argparse
import json
import sys
import re
from pathlib import Path
from difflib import SequenceMatcher
from collections import Counter, defaultdict

# --- Filler / tone-word definitions ---
# NOTE: matched against simplified-Chinese FunASR output — keep entries in simplified Chinese.

FILLER_WORDS = {
    '嗯', '啊', '呃', '額', '哦', '噢',
    '就是', '然後', '那個', '這個', '所以',
    '那那那', '那那', '是是是',
    '哈哈', '哈哈哈', '嗯嗯', '嗯嗯嗯',
    '啊啊', '呃呃'
}

STUTTER_PATTERNS = [
    r'(.{1,3})\1{1,}',  # consecutive repetition, e.g. "那那那"
]

# --- Text preprocessing ---

def normalize_text(text):
    """Normalize text for alignment."""
    text = text.strip()
    text = re.sub(r'\s+', '', text)  # remove all whitespace
    text = re.sub(r'[，。！？、；：""''（）【】]', '', text)  # remove punctuation
    return text

def extract_sentences_from_transcript(transcript_data):
    """Extract a sentence list from a transcription JSON."""
    sentences = []

    if isinstance(transcript_data, list):
        # subtitles_words.json format
        current_sentence = []
        current_speaker = None

        for word in transcript_data:
            if word.get('isSpeakerLabel'):
                if current_sentence:
                    text = ''.join(w['text'] for w in current_sentence)
                    sentences.append({
                        'text': text,
                        'speaker': current_speaker,
                        'start': current_sentence[0].get('start', 0),
                        'end': current_sentence[-1].get('end', 0),
                        'words': current_sentence
                    })
                    current_sentence = []
                current_speaker = word.get('speaker', '')
            elif word.get('isGap'):
                # silence segment — inspect duration
                gap_duration = word.get('end', 0) - word.get('start', 0)
                if gap_duration > 0.5 and current_sentence:
                    text = ''.join(w['text'] for w in current_sentence)
                    sentences.append({
                        'text': text,
                        'speaker': current_speaker,
                        'start': current_sentence[0].get('start', 0),
                        'end': current_sentence[-1].get('end', 0),
                        'words': current_sentence,
                        'gap_after': gap_duration
                    })
                    current_sentence = []
            else:
                current_sentence.append(word)

        if current_sentence:
            text = ''.join(w['text'] for w in current_sentence)
            sentences.append({
                'text': text,
                'speaker': current_speaker,
                'start': current_sentence[0].get('start', 0),
                'end': current_sentence[-1].get('end', 0),
                'words': current_sentence
            })

    elif isinstance(transcript_data, dict):
        # Aliyun source format
        for transcript in transcript_data.get('transcripts', []):
            for sent in transcript.get('sentences', []):
                sentences.append({
                    'text': sent.get('text', ''),
                    'speaker': str(sent.get('speaker_id', '')),
                    'start': sent.get('begin_time', 0) / 1000,
                    'end': sent.get('end_time', 0) / 1000,
                    'words': sent.get('words', [])
                })

    return sentences


# --- Text alignment and diff analysis ---

def align_transcripts(before_sentences, after_sentences):
    """Align before/after transcripts and find what was deleted."""
    before_text = [normalize_text(s['text']) for s in before_sentences]
    after_text = [normalize_text(s['text']) for s in after_sentences]

    # Use SequenceMatcher to find the longest common subsequence
    matcher = SequenceMatcher(None, before_text, after_text)

    kept = []      # kept sentence indices (before side)
    deleted = []   # deleted sentence indices (before side)

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'equal':
            for i in range(i1, i2):
                kept.append(i)
        elif op == 'delete':
            for i in range(i1, i2):
                deleted.append(i)
        elif op == 'replace':
            # `replace` may be a partial match — try fine-grained alignment
            for i in range(i1, i2):
                # Check whether before[i] has a fuzzy match in after[j1:j2]
                best_ratio = 0
                for j in range(j1, j2):
                    ratio = SequenceMatcher(None, before_text[i], after_text[j]).ratio()
                    best_ratio = max(best_ratio, ratio)
                if best_ratio > 0.7:
                    kept.append(i)
                else:
                    deleted.append(i)

    return kept, deleted


def classify_deletion(sentence, before_sentences, idx, deleted_indices):
    """Classify a deleted sentence."""
    text = sentence['text']
    normalized = normalize_text(text)

    # Pure filler?
    if normalized in FILLER_WORDS or len(normalized) <= 2 and normalized in {'嗯', '啊', '呃', '哦'}:
        return 'filler_word', text

    # Stutter / repetition?
    for pattern in STUTTER_PATTERNS:
        if re.search(pattern, normalized):
            return 'stutter', text

    # Short (possibly a residual sentence)?
    if len(normalized) < 5:
        return 'residual', text

    # Part of a consecutive block deletion?
    neighbors_deleted = sum(1 for d in deleted_indices
                            if abs(d - idx) <= 3 and d != idx)
    if neighbors_deleted >= 2:
        return 'content_block', text

    # Deletion right after a silence segment?
    if sentence.get('gap_after', 0) > 2.0:
        return 'silence_related', text

    # Default
    return 'other', text


# --- Statistical analysis ---

def analyze_patterns(before_sentences, kept, deleted):
    """Extract statistical patterns from the deletion data."""
    total = len(before_sentences)
    deleted_set = set(deleted)

    # Tally deletions per category
    deletion_types = defaultdict(list)
    filler_stats = Counter()
    filler_total = Counter()

    for idx in range(total):
        sentence = before_sentences[idx]
        text = normalize_text(sentence['text'])

        # Total filler counts
        for fw in FILLER_WORDS:
            count = text.count(fw)
            if count > 0:
                filler_total[fw] += count

        if idx in deleted_set:
            dtype, detail = classify_deletion(sentence, before_sentences, idx, deleted)
            deletion_types[dtype].append({
                'idx': idx,
                'text': sentence['text'][:50],
                'duration': sentence.get('end', 0) - sentence.get('start', 0)
            })

            # Count fillers actually deleted
            for fw in FILLER_WORDS:
                count = text.count(fw)
                if count > 0:
                    filler_stats[fw] += count

    # Compute filler deletion rates
    filler_deletion_rates = {}
    for fw in filler_total:
        total_count = filler_total[fw]
        deleted_count = filler_stats.get(fw, 0)
        if total_count > 0:
            rate = deleted_count / total_count
            filler_deletion_rates[fw] = {
                'total': total_count,
                'deleted': deleted_count,
                'rate': round(rate, 2)
            }

    # Estimate silence threshold
    silence_durations = []
    for idx in deleted_set:
        gap = before_sentences[idx].get('gap_after', 0)
        if gap > 0.5:
            silence_durations.append(gap)

    silence_threshold = None
    if silence_durations:
        silence_threshold = round(min(silence_durations), 1)

    # Duration stats
    before_duration = max(s.get('end', 0) for s in before_sentences) if before_sentences else 0
    deleted_duration = sum(
        before_sentences[i].get('end', 0) - before_sentences[i].get('start', 0)
        for i in deleted_set
    )

    return {
        'version': '1.0',
        'summary': {
            'total_sentences': total,
            'kept_sentences': len(kept),
            'deleted_sentences': len(deleted),
            'deletion_rate': round(len(deleted) / total, 2) if total > 0 else 0,
            'before_duration_seconds': round(before_duration, 1),
            'deleted_duration_seconds': round(deleted_duration, 1),
            'reduction_percent': round(deleted_duration / before_duration * 100, 1) if before_duration > 0 else 0
        },
        'deletion_types': {
            dtype: {
                'count': len(items),
                'total_duration': round(sum(i['duration'] for i in items), 1),
                'examples': [i['text'] for i in items[:3]]
            }
            for dtype, items in deletion_types.items()
        },
        'filler_word_analysis': filler_deletion_rates,
        'silence_analysis': {
            'estimated_threshold': silence_threshold,
            'deleted_silence_count': len(silence_durations),
            'silence_durations': sorted(silence_durations)[:10]  # top 10
        },
        'aggressiveness': classify_aggressiveness(len(deleted) / total if total > 0 else 0),
        'recommendations': generate_recommendations(
            filler_deletion_rates, silence_threshold,
            len(deleted) / total if total > 0 else 0,
            deletion_types
        )
    }


def classify_aggressiveness(deletion_rate):
    """Classify aggressiveness from the deletion rate."""
    if deletion_rate < 0.15:
        return 'conservative'
    elif deletion_rate < 0.30:
        return 'moderate'
    else:
        return 'aggressive'


def generate_recommendations(filler_rates, silence_threshold, deletion_rate, deletion_types):
    """Generate editing_rules recommendations."""
    recs = []

    # Filler recommendations
    high_delete = [fw for fw, data in filler_rates.items() if data['rate'] > 0.6]
    low_delete = [fw for fw, data in filler_rates.items() if data['rate'] < 0.3]

    if high_delete:
        recs.append({
            'rule': 'filler_words',
            'action': 'set_high_deletion',
            'words': high_delete,
            'confidence': 0.85,
            'reason': f'這些贅詞在樣本中被刪除超過 60%：{", ".join(high_delete)}'
        })

    if low_delete:
        recs.append({
            'rule': 'filler_words',
            'action': 'preserve',
            'words': low_delete,
            'confidence': 0.80,
            'reason': f'這些贅詞在樣本中大多保留：{", ".join(low_delete)}'
        })

    # Silence threshold recommendation
    if silence_threshold:
        recs.append({
            'rule': 'silence',
            'action': 'set_threshold',
            'value': silence_threshold,
            'confidence': 0.75,
            'reason': f'樣本中被刪除的最短靜音片段為 {silence_threshold}s'
        })

    # Aggressiveness recommendation
    recs.append({
        'rule': 'aggressiveness',
        'action': 'set',
        'value': classify_aggressiveness(deletion_rate),
        'confidence': 0.90,
        'reason': f'樣本整體刪除率 {deletion_rate:.0%}'
    })

    return recs


# --- 主逻辑 ---

def main():
    parser = argparse.ArgumentParser(
        description='Extract editing preferences by comparing before/after audio',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  # Use pre-existing transcripts
  python3 analyze_editing_samples.py \\
      --before-transcript before_words.json \\
      --after-transcript after_words.json \\
      --output learned_patterns.json

  # Full flow (transcribe both audios via Aliyun first)
  python3 analyze_editing_samples.py \\
      --before source.mp3 \\
      --after published.mp3 \\
      --output learned_patterns.json
        """
    )
    parser.add_argument('--before', help='source audio file path')
    parser.add_argument('--after', help='edited audio file path')
    parser.add_argument('--before-transcript', help='source-audio transcription JSON')
    parser.add_argument('--after-transcript', help='edited-audio transcription JSON')
    parser.add_argument('--output', required=True, help='output learned_patterns.json path')

    args = parser.parse_args()

    # Check inputs
    if not args.before_transcript or not args.after_transcript:
        if args.before and args.after:
            print("⚠️  未提供轉錄檔案。請先使用阿里雲 FunASR 轉錄兩個音訊：", file=sys.stderr)
            print(f"   1. 轉錄原始音訊：{args.before}", file=sys.stderr)
            print(f"   2. 轉錄剪輯版本：{args.after}", file=sys.stderr)
            print("   3. 將兩個 subtitles_words.json 分別傳入 --before-transcript 與 --after-transcript", file=sys.stderr)
            sys.exit(1)
        else:
            parser.print_help()
            sys.exit(1)

    # Load transcripts
    print("📖 載入轉錄檔案...", file=sys.stderr)
    with open(args.before_transcript, 'r', encoding='utf-8') as f:
        before_data = json.load(f)
    with open(args.after_transcript, 'r', encoding='utf-8') as f:
        after_data = json.load(f)

    # Extract sentences
    before_sentences = extract_sentences_from_transcript(before_data)
    after_sentences = extract_sentences_from_transcript(after_data)

    print(f"   原始：{len(before_sentences)} 句", file=sys.stderr)
    print(f"   剪輯版本：{len(after_sentences)} 句", file=sys.stderr)

    # Align and analyse
    print("🔍 對齊轉錄文字...", file=sys.stderr)
    kept, deleted = align_transcripts(before_sentences, after_sentences)
    print(f"   保留：{len(kept)} 句，刪除：{len(deleted)} 句", file=sys.stderr)

    # Analyse patterns
    print("📊 分析剪輯模式...", file=sys.stderr)
    patterns = analyze_patterns(before_sentences, kept, deleted)

    # Save results
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(patterns, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 分析完成，結果已儲存：{output_path}", file=sys.stderr)
    print(f"   整體刪減率：{patterns['summary']['reduction_percent']}%", file=sys.stderr)
    print(f"   激進度：{patterns['aggressiveness']}", file=sys.stderr)
    print(f"   建議數量：{len(patterns['recommendations'])}", file=sys.stderr)

    # Output to stdout (for pipelines)
    print(json.dumps(patterns, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
