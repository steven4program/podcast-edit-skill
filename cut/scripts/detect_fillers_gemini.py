#!/usr/bin/env python3
"""
Stage 2.8 — Audio-grounded filler detection via Gemini 2.5 Flash.

Pure-text LLM can't disambiguate Chinese filler words (嗯/啊/呃 vs 強調;
就是/然後/那個 vs 真正連接詞) because the distinguishing signal is acoustic
(prosody: stress, pause, duration). This pass sends the audio + the
candidate filler positions to Gemini, which can actually hear stress and
silence around each candidate and decide which are true fillers.

Reads:
    {BASE_DIR}/1_transcript/audio.mp3
    {BASE_DIR}/1_transcript/subtitles_words.json
    {BASE_DIR}/2_analysis/sentences.txt

Writes:
    {BASE_DIR}/2_analysis/gemini_filler_candidates.json
    {BASE_DIR}/2_analysis/gemini_filler_raw.json   (debug)

Usage:
    python3 detect_fillers_gemini.py <BASE_DIR>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHUNK_SEC = 45
HOP_SEC = 40
MODEL = "gemini-2.5-flash"
MAX_RETRIES = 3

PROMPT_TEMPLATE = """You are an expert Mandarin podcast editor. You will hear ~{chunk_dur:.0f} seconds
of a multi-speaker Chinese podcast (host + guest, casual conversation).

You will also be given the timed transcript of this clip:
====
{transcript}
====

Your job: identify which words in the transcript are TRUE fillers / hesitations
that should be removed for a cleaner cut. Use the AUDIO — listen for:
  - 字前/字後是否有明顯的猶豫停頓 (＞150ms) → 強烈 filler 訊號
  - 重音弱、語氣鬆弛 → filler
  - 拉長帶有遲疑語調 (e.g. "嗯⋯⋯") → filler
  - 短促、放在句首沒實質內容 → filler

DO NOT flag:
  - 「對」「好」當作真正附和的回應（語意完整）
  - 「就是」「然後」「那個」當作真正連接詞使用時（語意必要、有重音）
  - 「嗯」「啊」作為情緒、笑聲、思考片段
  - 任何承載資訊的詞

Target filler categories (only flag these word types):
  - hesitation: 嗯/啊/呃/額/哎/欸/哦/噢/哇
  - discourse_filler: 就是/然後/那個/這個/其實/真的/好像/反正/基本上
  - speaker_tic: 我覺得/我跟你說/說真的（若該說話人重複使用且無實質意義）

For each filler, report:
  - offset_s: seconds from the START of this clip (0.0 to clip duration)
  - duration_s: estimated duration in seconds
  - text: the literal word from the transcript
  - category: one of the three categories above
  - confidence: numeric 0.0-1.0
  - reason: one short Chinese sentence explaining WHY (e.g. "句首遲疑、前無重音")

Return JSON only:
{{"fillers": [
  {{"offset_s": 12.3, "duration_s": 0.3, "text": "嗯",
    "category": "hesitation", "confidence": 0.9,
    "reason": "句首遲疑，前後皆有 200ms 停頓，無實質連接作用"}}
]}}

If nothing to flag, return: {{"fillers": []}}
"""


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('base_dir')
    ap.add_argument('--audio', default=None, help='override audio file (default: 1_transcript/audio.mp3)')
    return ap.parse_args()


def load_api_key() -> str:
    key = os.environ.get('GEMINI_API_KEY')
    if key:
        return key
    here = Path(__file__).resolve()
    for parent in here.parents:
        env = parent / '.env'
        if env.exists():
            for line in env.read_text().splitlines():
                line = line.strip()
                if line.startswith('GEMINI_API_KEY='):
                    return line.split('=', 1)[1].strip().strip('"\'')
    raise RuntimeError('GEMINI_API_KEY not found.')


def get_audio_duration(path: Path) -> float:
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def extract_clip(src: Path, start: float, duration: float, dest: str) -> None:
    subprocess.run([
        'ffmpeg', '-v', 'quiet',
        '-i', str(src), '-ss', str(start), '-t', str(duration),
        '-c:a', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        '-y', dest,
    ], check=True)


def parse_json_response(text: str | None) -> dict | None:
    if not text:
        return None
    for pat in (text,
                re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL)
                  and re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL).group(1),
                re.search(r'\{.*\}', text, re.DOTALL)
                  and re.search(r'\{.*\}', text, re.DOTALL).group(0)):
        if not pat:
            continue
        try:
            return json.loads(pat)
        except json.JSONDecodeError:
            continue
    return None


def call_gemini(client, audio_bytes: bytes, prompt: str) -> str | None:
    from google.genai import types
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=[prompt,
                          types.Part.from_bytes(data=audio_bytes, mime_type='audio/wav')],
            )
            return resp.text
        except Exception as e:
            last_err = e
            msg = str(e)
            if any(c in msg for c in ('503', '429', 'RATE_LIMIT', 'UNAVAILABLE')):
                wait = 2 ** (attempt + 1)
                print(f'    retry in {wait}s (err: {msg[:80]})', file=sys.stderr)
                time.sleep(wait)
                continue
            break
    print(f'    Gemini failed after {MAX_RETRIES} attempts: {last_err}', file=sys.stderr)
    return None


def build_chunks(duration: float):
    out = []
    t = 0.0
    while t < duration:
        e = min(t + CHUNK_SEC, duration)
        out.append((t, e))
        if e >= duration:
            break
        t += HOP_SEC
    return out


def transcript_for_window(actual_words, t_start: float, t_end: float) -> str:
    """Build a 'MM:SS.s | speaker | text' transcript for the window."""
    lines = []
    for w in actual_words:
        ws = w.get('start', 0)
        we = w.get('end', 0)
        if we < t_start or ws > t_end:
            continue
        offset = ws - t_start
        lines.append(f"[{offset:6.2f}s {w.get('speaker','?')}] {w.get('text','')}")
    return '\n'.join(lines) if lines else '(no transcript in this window)'


def main():
    args = parse_args()
    base = Path(args.base_dir).resolve()
    audio_path = Path(args.audio) if args.audio else (base / '1_transcript/audio.mp3')
    words_path = base / '1_transcript/subtitles_words.json'

    if not audio_path.exists():
        print(f'❌ audio not found: {audio_path}', file=sys.stderr); sys.exit(1)
    if not words_path.exists():
        print(f'❌ words not found: {words_path}', file=sys.stderr); sys.exit(1)

    try:
        api_key = load_api_key()
    except RuntimeError as e:
        print(f'❌ {e}', file=sys.stderr); sys.exit(2)

    from google import genai
    client = genai.Client(api_key=api_key)

    actual = [w for w in json.loads(words_path.read_text(encoding='utf-8'))
              if not w.get('isGap') and not w.get('isSpeakerLabel')]
    duration = get_audio_duration(audio_path)
    chunks = build_chunks(duration)
    print(f'🎧 audio {duration:.1f}s → {len(chunks)} chunks')

    all_fillers = []
    raw = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (s, e) in enumerate(chunks):
            transcript = transcript_for_window(actual, s, e)
            prompt = PROMPT_TEMPLATE.format(chunk_dur=e - s, transcript=transcript)
            clip = os.path.join(tmp, f'chunk_{i}.wav')
            extract_clip(audio_path, s, e - s, clip)
            with open(clip, 'rb') as f:
                audio_bytes = f.read()

            print(f'  [{i+1}/{len(chunks)}] {s:.1f}-{e:.1f}s ...', end=' ', flush=True)
            t0 = time.time()
            text = call_gemini(client, audio_bytes, prompt)
            elapsed = time.time() - t0
            if not text:
                print(f'FAIL ({elapsed:.1f}s)')
                raw.append({'chunk': [s, e], 'error': 'gemini_failed'})
                continue
            parsed = parse_json_response(text)
            n = len(parsed.get('fillers', [])) if parsed else 0
            print(f'({elapsed:.1f}s, {n} fillers)')
            raw.append({'chunk': [s, e], 'raw': text, 'parsed': parsed})
            if not parsed:
                continue
            for f in parsed.get('fillers', []):
                off = f.get('offset_s')
                dur = f.get('duration_s', 0.3)
                if off is None:
                    continue
                all_fillers.append({
                    'start': round(s + off, 3),
                    'end': round(s + off + dur, 3),
                    'text': f.get('text', ''),
                    'category': f.get('category', 'unknown'),
                    'confidence': float(f.get('confidence', 0.5)),
                    'reason': f.get('reason', ''),
                    'chunk_start': s,
                })

    # ── De-dupe overlaps (same text/category within 0.4s) ──
    all_fillers.sort(key=lambda x: x['start'])
    deduped = []
    for f in all_fillers:
        if (deduped and
                f['start'] - deduped[-1]['start'] < 0.4 and
                f['text'] == deduped[-1]['text'] and
                f['category'] == deduped[-1]['category']):
            if f['confidence'] > deduped[-1]['confidence']:
                deduped[-1] = f
            continue
        deduped.append(f)

    # ── Map each filler back to a word index ──
    candidates = []
    for i, f in enumerate(deduped):
        # Find the closest word within ±0.5s whose text matches
        best = None
        best_d = 9e9
        for wi, w in enumerate(actual):
            mid = (w.get('start', 0) + w.get('end', 0)) / 2
            d = abs(mid - (f['start'] + f['end']) / 2)
            if d < best_d and d < 0.5 and (
                f['text'] == w.get('text') or
                f['text'] in w.get('text', '') or
                w.get('text', '') in f['text']
            ):
                best = wi
                best_d = d
        candidates.append({
            'id': f'gemfill_{i}',
            'wordIdx': best,
            'type': 'gemini_filler',
            'category': f['category'],
            'deleteText': f['text'],
            'deleteStart': f['start'],
            'deleteEnd': f['end'],
            'confidence': f['confidence'],
            'reason': f['reason'],
            # Default behavior: high-confidence → auto-enabled, others → review
            'enabled': f['confidence'] >= 0.8,
        })

    out = {
        'summary': {
            'total_candidates': len(candidates),
            'auto_enabled': sum(1 for c in candidates if c['enabled']),
            'by_category': {},
            'model': MODEL,
        },
        'candidates': candidates,
    }
    for c in candidates:
        out['summary']['by_category'][c['category']] = out['summary']['by_category'].get(c['category'], 0) + 1

    out_path = base / '2_analysis/gemini_filler_candidates.json'
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    (base / '2_analysis/gemini_filler_raw.json').write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'✅ wrote {out_path}')
    print(f'   {len(candidates)} candidates ({out["summary"]["auto_enabled"]} auto-enabled), by category: {out["summary"]["by_category"]}')


if __name__ == '__main__':
    main()
