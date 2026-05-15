#!/usr/bin/env python3
"""
Transcribe local audio with faster-whisper and emit this repo's canonical
subtitles_words.json format.

Usage:
  python3 transcribe_whisper_local.py audio.mp3 --output-dir output/.../1_transcript
"""

import argparse
import json
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run local Whisper transcription and generate subtitles_words.json."
    )
    parser.add_argument("audio", help="Path to a local audio file.")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for whisper_transcription.json and subtitles_words.json.",
    )
    parser.add_argument(
        "--model",
        default="large-v3",
        help="faster-whisper model name or local model path. Default: large-v3.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Language code hint, e.g. zh, en, ja. Omit for auto-detect.",
    )
    parser.add_argument(
        "--speaker",
        default="Speaker 0",
        help="Speaker label to assign to all words in MVP mode.",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="Inference device. Default: auto.",
    )
    parser.add_argument(
        "--compute-type",
        default="auto",
        help="faster-whisper compute type, e.g. auto, int8, float16.",
    )
    parser.add_argument(
        "--gap-threshold",
        type=float,
        default=0.5,
        help="Insert an isGap marker when silence is at least this many seconds.",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="Beam size for transcription. Default: 5.",
    )
    return parser.parse_args()


def import_faster_whisper():
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "Error: faster-whisper is not installed. Run: pip install faster-whisper",
            file=sys.stderr,
        )
        sys.exit(1)
    return WhisperModel


def clean_word_text(text):
    return text.strip()


def segment_to_dict(segment):
    words = [
        {
            "text": word.word,
            "start": word.start,
            "end": word.end,
            "probability": word.probability,
        }
        for word in (segment.words or [])
    ]
    return {
        "id": segment.id,
        "start": segment.start,
        "end": segment.end,
        "text": segment.text,
        "words": words,
    }


def build_subtitles_words(segments, speaker, gap_threshold):
    subtitles = []
    previous_end = None
    speaker_label_added = False

    for segment in segments:
        segment_words = [
            word for word in (segment.get("words") or []) if clean_word_text(word["text"])
        ]
        if not segment_words:
            continue

        segment_start = segment_words[0]["start"]
        if previous_end is not None and segment_start - previous_end >= gap_threshold:
            subtitles.append(
                {
                    "text": "",
                    "start": previous_end,
                    "end": segment_start,
                    "isGap": True,
                }
            )

        if not speaker_label_added:
            subtitles.append(
                {
                    "text": f"[{speaker}]",
                    "start": segment_start,
                    "end": segment_start,
                    "isGap": False,
                    "isSpeakerLabel": True,
                    "speaker": speaker,
                }
            )
            speaker_label_added = True

        subtitles.extend(
            [
                {
                    "text": clean_word_text(word["text"]),
                    "start": word["start"],
                    "end": word["end"],
                    "isGap": False,
                    "speaker": speaker,
                }
                for word in segment_words
            ]
        )
        previous_end = segment_words[-1]["end"]

    return subtitles


def main():
    args = parse_args()
    audio_path = Path(args.audio).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not audio_path.exists():
        print(f"Error: audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    WhisperModel = import_faster_whisper()
    print(f"Loading model: {args.model}")
    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
    )

    print(f"Transcribing: {audio_path}")
    segments_iter, info = model.transcribe(
        str(audio_path),
        beam_size=args.beam_size,
        language=args.language,
        word_timestamps=True,
        vad_filter=True,
    )
    segments = [segment_to_dict(segment) for segment in segments_iter]

    whisper_json = {
        "source": str(audio_path),
        "model": args.model,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "speaker_mode": "single",
        "speaker": args.speaker,
        "segments": segments,
    }
    subtitles_words = build_subtitles_words(
        segments=segments,
        speaker=args.speaker,
        gap_threshold=args.gap_threshold,
    )

    whisper_path = output_dir / "whisper_transcription.json"
    subtitles_path = output_dir / "subtitles_words.json"
    whisper_path.write_text(json.dumps(whisper_json, ensure_ascii=False, indent=2), encoding="utf-8")
    subtitles_path.write_text(json.dumps(subtitles_words, ensure_ascii=False, indent=2), encoding="utf-8")

    real_words = [w for w in subtitles_words if not w.get("isGap") and not w.get("isSpeakerLabel")]
    gaps = [w for w in subtitles_words if w.get("isGap")]
    print("Done")
    print(f"  Whisper JSON: {whisper_path}")
    print(f"  subtitles_words.json: {subtitles_path}")
    print(f"  Language: {info.language} ({info.language_probability:.2f})")
    print(f"  Words: {len(real_words)}")
    print(f"  Gaps: {len(gaps)}")
    print(f"  Speaker: {args.speaker}")


if __name__ == "__main__":
    main()
