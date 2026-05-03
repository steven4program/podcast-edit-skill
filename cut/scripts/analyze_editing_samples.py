#!/usr/bin/env python3
"""
edit样本analyze器 — 从edit前后audioOK比中extractedit偏good

通OK比source录音 and 已发布 editversion，自动identifyuse 户 edit风格：
- fillerdelete率
- silence segmentprocessthreshold
- contentblock删减激进度
- 重复/口误process方式

Usage:
    python3 analyze_editing_samples.py \\
        --before /path/to/source录音.mp3 \\
        --after /path/to/发布version.mp3 \\
        --before-transcript /path/to/before_transcript.json \\
        --after-transcript /path/to/after_transcript.json \\
        --output /path/to/learned_patterns.json

e.g.果no提供 transcript，会Tip使use 阿里云 FunASR 先transcribe。

依赖: pip install librosa numpy
"""

import argparse
import json
import sys
import re
from pathlib import Path
from difflib import SequenceMatcher
from collections import Counter, defaultdict

# --- filler and tone words定义 ---

FILLER_WORDS = {
    '嗯', '啊', '呃', '额', '哦', '噢',
    '就是', '然后', '那个', '这个', '所以',
    'OKOKOK', 'OKOK', '是是是',
    '哈哈', '哈哈哈', '嗯嗯', '嗯嗯嗯',
    '啊啊', '呃呃'
}

STUTTER_PATTERNS = [
    r'(.{1,3})\1{1,}',  # 连续重复，e.g."那那那"
]

# --- 文本预process ---

def normalize_text(text):
    """标准化文本use 于OK齐"""
    text = text.strip()
    text = re.sub(r'\s+', '', text)  # 移除所有空格
    text = re.sub(r'[，。！？、；：""''（）【】]', '', text)  # 移除标点
    return text

def extract_sentences_from_transcript(transcript_data):
    """从transcribe JSON extractsentencecolumn表"""
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
                # silence segment，检查duration
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
        # 阿里云sourceformat
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


# --- 文本OK齐 and 差异analyze ---

def align_transcripts(before_sentences, after_sentences):
    """OK齐edit前后 transcribe文本，找出被delete 部 min """
    before_text = [normalize_text(s['text']) for s in before_sentences]
    after_text = [normalize_text(s['text']) for s in after_sentences]

    # 使use  SequenceMatcher 找到最长公total子序column
    matcher = SequenceMatcher(None, before_text, after_text)

    kept = []      # keep sentenceindex（before 侧）
    deleted = []   # 被delete sentenceindex（before 侧）

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'equal':
            for i in range(i1, i2):
                kept.append(i)
        elif op == 'delete':
            for i in range(i1, i2):
                deleted.append(i)
        elif op == 'replace':
            # replace 可能是部 min match，尝试细粒度OK齐
            for i in range(i1, i2):
                # 检查 before[i] whether在 after[j1:j2] 中有近似match
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
    """OKdelete sentence进line min 类"""
    text = sentence['text']
    normalized = normalize_text(text)

    # 检查whether为纯filler
    if normalized in FILLER_WORDS or len(normalized) <= 2 and normalized in {'嗯', '啊', '呃', '哦'}:
        return 'filler_word', text

    # 检查whether为卡顿/重复
    for pattern in STUTTER_PATTERNS:
        if re.search(pattern, normalized):
            return 'stutter', text

    # 检查whether为短（可能是residual sentence）
    if len(normalized) < 5:
        return 'residual', text

    # 检查whether为连续delete（contentblock）
    neighbors_deleted = sum(1 for d in deleted_indices
                            if abs(d - idx) <= 3 and d != idx)
    if neighbors_deleted >= 2:
        return 'content_block', text

    # 检查silence segment后 delete
    if sentence.get('gap_after', 0) > 2.0:
        return 'silence_related', text

    # default min 类
    return 'other', text


# --- statisticsanalyze ---

def analyze_patterns(before_sentences, kept, deleted):
    """从delete模式中extractstatistics规律"""
    total = len(before_sentences)
    deleted_set = set(deleted)

    # statistics各类delete
    deletion_types = defaultdict(list)
    filler_stats = Counter()
    filler_total = Counter()

    for idx in range(total):
        sentence = before_sentences[idx]
        text = normalize_text(sentence['text'])

        # statisticsfillertotal count
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

            # statistics被delete filler
            for fw in FILLER_WORDS:
                count = text.count(fw)
                if count > 0:
                    filler_stats[fw] += count

    # calculatefillerdelete率
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

    # 估计silencethreshold
    silence_durations = []
    for idx in deleted_set:
        gap = before_sentences[idx].get('gap_after', 0)
        if gap > 0.5:
            silence_durations.append(gap)

    silence_threshold = None
    if silence_durations:
        silence_threshold = round(min(silence_durations), 1)

    # calculatedurationstatistics
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
            'silence_durations': sorted(silence_durations)[:10]  # 前 10 个
        },
        'aggressiveness': classify_aggressiveness(len(deleted) / total if total > 0 else 0),
        'recommendations': generate_recommendations(
            filler_deletion_rates, silence_threshold,
            len(deleted) / total if total > 0 else 0,
            deletion_types
        )
    }


def classify_aggressiveness(deletion_rate):
    """根据delete率判断激进度"""
    if deletion_rate < 0.15:
        return 'conservative'
    elif deletion_rate < 0.30:
        return 'moderate'
    else:
        return 'aggressive'


def generate_recommendations(filler_rates, silence_threshold, deletion_rate, deletion_types):
    """generate editing_rules 建议"""
    recs = []

    # filler建议
    high_delete = [fw for fw, data in filler_rates.items() if data['rate'] > 0.6]
    low_delete = [fw for fw, data in filler_rates.items() if data['rate'] < 0.3]

    if high_delete:
        recs.append({
            'rule': 'filler_words',
            'action': 'set_high_deletion',
            'words': high_delete,
            'confidence': 0.85,
            'reason': f'这些filler在样本中被delete超 60%: {", ".join(high_delete)}'
        })

    if low_delete:
        recs.append({
            'rule': 'filler_words',
            'action': 'preserve',
            'words': low_delete,
            'confidence': 0.80,
            'reason': f'这些filler在样本中大multi被keep: {", ".join(low_delete)}'
        })

    # silencethreshold建议
    if silence_threshold:
        recs.append({
            'rule': 'silence',
            'action': 'set_threshold',
            'value': silence_threshold,
            'confidence': 0.75,
            'reason': f'样本中delete 最短silence segment为 {silence_threshold}s'
        })

    # 激进度建议
    recs.append({
        'rule': 'aggressiveness',
        'action': 'set',
        'value': classify_aggressiveness(deletion_rate),
        'confidence': 0.90,
        'reason': f'样本总体delete率 {deletion_rate:.0%}'
    })

    return recs


# --- 主逻辑 ---

def main():
    parser = argparse.ArgumentParser(
        description='从edit前后audioOK比中extractedit偏good',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  # 使use 已有transcribe
  python3 analyze_editing_samples.py \\
      --before-transcript before_words.json \\
      --after-transcript after_words.json \\
      --output learned_patterns.json

  # 完整flow（needs 先use 阿里云transcribe两个audio）
  python3 analyze_editing_samples.py \\
      --before source录音.mp3 \\
      --after 发布version.mp3 \\
      --output learned_patterns.json
        """
    )
    parser.add_argument('--before', help='sourceaudio filepath')
    parser.add_argument('--after', help='edit后audio filepath')
    parser.add_argument('--before-transcript', help='sourceaudio transcribe JSON')
    parser.add_argument('--after-transcript', help='edit后audio transcribe JSON')
    parser.add_argument('--output', required=True, help='output learned_patterns.json path')

    args = parser.parse_args()

    # 检查input
    if not args.before_transcript or not args.after_transcript:
        if args.before and args.after:
            print("⚠️  未提供transcribefile。Please 先使use 阿里云 FunASR transcribe两个audio：", file=sys.stderr)
            print(f"   1. transcribesourceaudio: {args.before}", file=sys.stderr)
            print(f"   2. transcribeeditversion: {args.after}", file=sys.stderr)
            print("   3. 将两个 subtitles_words.json  min 别传入 --before-transcript  and  --after-transcript", file=sys.stderr)
            sys.exit(1)
        else:
            parser.print_help()
            sys.exit(1)

    # 加载transcribe
    print("📖 加载transcribefile...", file=sys.stderr)
    with open(args.before_transcript, 'r', encoding='utf-8') as f:
        before_data = json.load(f)
    with open(args.after_transcript, 'r', encoding='utf-8') as f:
        after_data = json.load(f)

    # extractsentence
    before_sentences = extract_sentences_from_transcript(before_data)
    after_sentences = extract_sentences_from_transcript(after_data)

    print(f"   source: {len(before_sentences)} ", file=sys.stderr)
    print(f"   editversion: {len(after_sentences)} ", file=sys.stderr)

    # OK齐 and analyze
    print("🔍 OK齐transcribe文本...", file=sys.stderr)
    kept, deleted = align_transcripts(before_sentences, after_sentences)
    print(f"   keep: {len(kept)} , delete: {len(deleted)} ", file=sys.stderr)

    # analyze模式
    print("📊 analyzeedit模式...", file=sys.stderr)
    patterns = analyze_patterns(before_sentences, kept, deleted)

    # saveresult
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(patterns, f, ensure_ascii=False, indent=2)

    print(f"\n✅ analyzeComplete，resultsaved: {output_path}", file=sys.stderr)
    print(f"   总体删减率: {patterns['summary']['reduction_percent']}%", file=sys.stderr)
    print(f"   激进度: {patterns['aggressiveness']}", file=sys.stderr)
    print(f"   建议数量: {len(patterns['recommendations'])}", file=sys.stderr)

    # output到 stdout（供管道使use ）
    print(json.dumps(patterns, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
