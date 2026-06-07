#!/usr/bin/env python3
"""
End-to-end multitrack transcription via WhisperX (Whisper transcribe + wav2vec2
forced alignment). Each track is processed independently and assigned to its
speaker; words from all tracks are merged by time into the repo's canonical
subtitles_words.json format.

Why WhisperX over plain faster-whisper:
  - Whisper's attention-based decoder gives word timestamps with ±100-200 ms
    error. wav2vec2 forced alignment via CTC refines to ±20 ms.
  - This drops 6/8 stutter `skip` verdicts in the safe-cut planner (cuts that
    were rejected because their boundaries landed mid-speech).
  - Lets Tier-2 inner-snap (preserve 30ms breath) actually succeed, because
    the surrounding silence is now correctly INSIDE the cut range.

Output format identical to transcribe_whisper_multitrack.py so downstream
pipeline (generate_sentences, run_fine_analysis, safe_filler_cut, cut_audio)
is unchanged.

Usage:
  python3 transcribe_whisperx_multitrack.py \
      --track "Alice=/path/alice.wav" \
      --track "Bob=/path/bob.wav" \
      --output-dir output/.../1_transcript

Requires:
  pip install whisperx
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--track", action="append", required=True,
                   help='Speaker/audio pair. Repeatable. Format: "Speaker=/path/to/audio.wav".')
    p.add_argument("--offset", action="append", default=[],
                   help='Optional timestamp offset in seconds. Format: "Speaker=0.25".')
    p.add_argument("--output-dir", default=".")
    p.add_argument("--model", default="large-v3",
                   help="WhisperX/Whisper model name. Default: large-v3.")
    p.add_argument("--language", default="zh",
                   help="Language code (default: zh). Required for the align model.")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    p.add_argument("--compute-type", default="int8",
                   help="Whisper compute type (int8/float16/float32). Default: int8 for CPU.")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--gap-threshold", type=float, default=0.5)
    p.add_argument("--align-model", default=None,
                   help="Override wav2vec2 alignment model HuggingFace ID. Default: language-appropriate.")
    return p.parse_args()


def parse_key_value(raw, label):
    if "=" not in raw:
        print(f'Error: invalid {label}: "{raw}". Expected "Name=value".', file=sys.stderr)
        sys.exit(1)
    k, v = raw.split("=", 1)
    k, v = k.strip(), v.strip()
    if not k or not v:
        print(f'Error: invalid {label}: "{raw}". Expected non-empty name and value.', file=sys.stderr)
        sys.exit(1)
    return k, v


def parse_tracks(raw_tracks):
    tracks = [{"speaker": s, "audio": Path(a).expanduser().resolve()}
              for s, a in [parse_key_value(r, "track") for r in raw_tracks]]
    missing = [t for t in tracks if not t["audio"].exists()]
    if missing:
        for t in missing:
            print(f"Error: audio file not found for {t['speaker']}: {t['audio']}", file=sys.stderr)
        sys.exit(1)
    return tracks


def parse_offsets(raw_offsets):
    out = {}
    for raw in raw_offsets:
        s, v = parse_key_value(raw, "offset")
        try:
            out[s] = float(v)
        except ValueError:
            print(f'Error: invalid offset for "{s}": {v}', file=sys.stderr); sys.exit(1)
    return out


def safe_filename(name):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return safe.strip("_") or "speaker"


def shift_words(words, offset):
    if offset == 0:
        return words
    return [{**w, "start": max(0.0, w["start"] + offset),
                  "end":   max(0.0, w["end"]   + offset)} for w in words]


def transcribe_and_align(whisperx_mod, model, audio_path, language, device, batch_size,
                           align_cache):
    """Run Whisper transcribe + wav2vec2 align. Returns list of word dicts."""
    print(f"  loading audio ...")
    audio = whisperx_mod.load_audio(str(audio_path))
    print(f"  Whisper transcribe ...")
    result = model.transcribe(audio, batch_size=batch_size, language=language)
    detected_lang = result.get("language", language)

    # Cache align model per language so we don't reload across tracks
    if detected_lang not in align_cache:
        print(f"  loading wav2vec2 align model for '{detected_lang}' ...")
        try:
            align_model, metadata = whisperx_mod.load_align_model(
                language_code=detected_lang, device=device)
        except Exception as e:
            print(f"  ⚠️  align model failed: {e}; using unaligned timestamps")
            align_cache[detected_lang] = None
            return _extract_words_from_whisper(result, source_offset=0.0)
        align_cache[detected_lang] = (align_model, metadata)

    cached = align_cache[detected_lang]
    if cached is None:
        return _extract_words_from_whisper(result, source_offset=0.0)

    align_model, metadata = cached
    print(f"  forced alignment ({len(result.get('segments', []))} segments) ...")
    aligned = whisperx_mod.align(result["segments"], align_model, metadata, audio,
                                   device, return_char_alignments=False)

    # wav2vec2 alignment outputs the bare characters and DROPS punctuation
    # (。！？，). That breaks Plan B's sentence splitter, which needs
    # punctuation to find natural sentence boundaries. Solution: walk Whisper's
    # ORIGINAL segment text in parallel with the aligned-word stream and append
    # any punctuation that follows an aligned character.
    whisper_text = ''.join(seg.get('text', '') for seg in result.get('segments', []))
    PUNCT = set('。！？，、：；,.!?:;')

    words = []
    whisper_pos = 0   # cursor into whisper_text
    for seg in aligned.get("segments", []):
        for w in seg.get("words", []):
            if "start" not in w or "end" not in w:
                continue
            char = str(w.get("word", "")).strip()
            if not char:
                continue
            # Advance whisper_text cursor past this character (skipping whitespace).
            # If found, attach any IMMEDIATELY following punctuation to this word
            # so downstream sentence splitting works.
            attached_punct = ""
            search_end = min(len(whisper_text), whisper_pos + 30)
            idx = whisper_text.find(char, whisper_pos, search_end)
            if idx >= 0:
                # Position cursor after this char and gobble trailing punctuation
                whisper_pos = idx + len(char)
                while whisper_pos < len(whisper_text) and whisper_text[whisper_pos] in PUNCT:
                    attached_punct += whisper_text[whisper_pos]
                    whisper_pos += 1
            words.append({
                "text": char + attached_punct,
                "start": float(w["start"]),
                "end": float(w["end"]),
                "score": float(w.get("score", 0.0)),
            })
    n_with_punct = sum(1 for w in words if any(c in w['text'] for c in PUNCT))
    print(f"  attached punctuation to {n_with_punct} words from whisper original segments")
    return words


def _extract_words_from_whisper(result, source_offset):
    """Fallback when alignment fails — use whisper's own word timestamps (less precise)."""
    out = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            t = str(w.get("word") or w.get("text", "")).strip()
            if not t or "start" not in w or "end" not in w:
                continue
            out.append({
                "text": t,
                "start": float(w["start"]) + source_offset,
                "end": float(w["end"]) + source_offset,
                "score": float(w.get("probability") or w.get("score", 0.0)),
            })
    return out


def build_merged_subtitles(track_results, gap_threshold):
    """Sort all tracks' words by time and emit canonical subtitles_words.json layout."""
    words = []
    for tr in track_results:
        for w in tr["words"]:
            words.append({**w, "isGap": False, "speaker": tr["speaker"]})
    words.sort(key=lambda w: (w["start"], w["end"], w["speaker"]))

    subs = []
    prev_end = None
    prev_speaker = None
    for w in words:
        if prev_end is not None and w["start"] - prev_end >= gap_threshold:
            subs.append({"text": "", "start": prev_end, "end": w["start"], "isGap": True})
        if w["speaker"] != prev_speaker:
            subs.append({"text": f"[{w['speaker']}]", "start": w["start"], "end": w["start"],
                          "isGap": False, "isSpeakerLabel": True, "speaker": w["speaker"]})
            prev_speaker = w["speaker"]
        subs.append({"text": w["text"], "start": w["start"], "end": w["end"],
                      "isGap": False, "speaker": w["speaker"]})
        prev_end = max(prev_end or w["end"], w["end"])
    return subs


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    args = parse_args()
    tracks = parse_tracks(args.track)
    offsets = parse_offsets(args.offset)
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        import whisperx
    except ImportError:
        print("❌ whisperx not installed. Run: pip install whisperx", file=sys.stderr); sys.exit(1)

    print(f"Loading Whisper model: {args.model} ({args.device}, {args.compute_type})")
    model = whisperx.load_model(args.model, args.device, compute_type=args.compute_type,
                                 language=args.language)

    align_cache: dict = {}
    track_results = []
    for tr in tracks:
        offset = offsets.get(tr["speaker"], 0.0)
        print(f"\n=== Track: {tr['speaker']} ({tr['audio'].name}) ===")
        words = transcribe_and_align(
            whisperx, model, tr["audio"], args.language, args.device,
            args.batch_size, align_cache,
        )
        words = shift_words(words, offset)
        track_results.append({
            "speaker": tr["speaker"],
            "source": str(tr["audio"]),
            "offset": offset,
            "model": args.model,
            "language": args.language,
            "words": words,
        })
        per_track_path = out_dir / f"whisper_{safe_filename(tr['speaker'])}.json"
        write_json(per_track_path, {
            "source": str(tr["audio"]),
            "model": args.model,
            "language": args.language,
            "speaker_mode": "track",
            "speaker": tr["speaker"],
            "offset": offset,
            "words": words,
        })
        print(f"  → {per_track_path.name} ({len(words)} words)")

    subs = build_merged_subtitles(track_results, args.gap_threshold)
    subs_path = out_dir / "subtitles_words.json"
    write_json(subs_path, subs)
    manifest_path = out_dir / "whisper_multitrack_manifest.json"
    write_json(manifest_path, {
        "model": args.model,
        "language": args.language,
        "speaker_mode": "multitrack",
        "aligner": "whisperx-wav2vec2",
        "tracks": [{
            "speaker": tr["speaker"],
            "source": tr["source"],
            "offset": tr["offset"],
            "word_count": len(tr["words"]),
        } for tr in track_results],
    })

    real = [w for w in subs if not w.get("isGap") and not w.get("isSpeakerLabel")]
    gaps = [w for w in subs if w.get("isGap")]
    speakers = sorted({w["speaker"] for w in real})
    print(f"\n✅ subtitles_words.json: {subs_path.name}")
    print(f"   manifest:               {manifest_path.name}")
    print(f"   speakers: {', '.join(speakers)}")
    print(f"   words:    {len(real)}   gaps: {len(gaps)}")


if __name__ == "__main__":
    main()
