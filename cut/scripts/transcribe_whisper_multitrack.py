#!/usr/bin/env python3
"""
Transcribe aligned multi-track audio with faster-whisper.

Each input track is treated as one speaker. The script transcribes tracks
independently, merges all words by timestamp, then writes the repo's canonical
subtitles_words.json format.

Usage:
  python3 transcribe_whisper_multitrack.py \
    --track "Alice=/path/alice.wav" \
    --track "Bob=/path/bob.wav" \
    --output-dir output/.../1_transcript
"""

import argparse
import json
import re
import sys
from pathlib import Path

from transcribe_whisper_local import clean_word_text, import_faster_whisper, segment_to_dict


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run local Whisper on aligned multi-track audio and generate subtitles_words.json."
    )
    parser.add_argument(
        "--track",
        action="append",
        required=True,
        help='Speaker/audio pair. Repeatable. Format: "Speaker=/path/to/audio.wav".',
    )
    parser.add_argument(
        "--offset",
        action="append",
        default=[],
        help='Optional timestamp offset in seconds. Repeatable. Format: "Speaker=0.25".',
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for per-track Whisper JSON and merged subtitles_words.json.",
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


def parse_key_value(raw, label):
    if "=" not in raw:
        print(f'Error: invalid {label}: "{raw}". Expected "Name=value".', file=sys.stderr)
        sys.exit(1)
    key, value = raw.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key or not value:
        print(f'Error: invalid {label}: "{raw}". Expected non-empty name and value.', file=sys.stderr)
        sys.exit(1)
    return key, value


def parse_tracks(raw_tracks):
    tracks = [
        {
            "speaker": speaker,
            "audio": Path(audio).expanduser().resolve(),
        }
        for speaker, audio in [parse_key_value(raw, "track") for raw in raw_tracks]
    ]

    missing = [track for track in tracks if not track["audio"].exists()]
    if missing:
        [
            print(f"Error: audio file not found for {track['speaker']}: {track['audio']}", file=sys.stderr)
            for track in missing
        ]
        sys.exit(1)

    duplicate_speakers = sorted(
        {
            track["speaker"]
            for track in tracks
            if [item["speaker"] for item in tracks].count(track["speaker"]) > 1
        }
    )
    if duplicate_speakers:
        print(f"Error: duplicate speaker labels: {', '.join(duplicate_speakers)}", file=sys.stderr)
        sys.exit(1)

    return tracks


def parse_offsets(raw_offsets):
    offsets = {}
    for raw in raw_offsets:
        speaker, value = parse_key_value(raw, "offset")
        try:
            offsets[speaker] = float(value)
        except ValueError:
            print(f'Error: invalid offset seconds for "{speaker}": {value}', file=sys.stderr)
            sys.exit(1)
    return offsets


def safe_filename(name):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return safe.strip("_") or "speaker"


def transcribe_track(model, track, args, offset):
    print(f"Transcribing track: {track['speaker']} -> {track['audio']}")
    segments_iter, info = model.transcribe(
        str(track["audio"]),
        beam_size=args.beam_size,
        language=args.language,
        word_timestamps=True,
        vad_filter=True,
    )
    segments = [segment_to_dict(segment) for segment in segments_iter]
    shifted_segments = apply_offset(segments, offset)
    words = track_words(shifted_segments, track["speaker"])

    return {
        "source": str(track["audio"]),
        "model": args.model,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "speaker_mode": "track",
        "speaker": track["speaker"],
        "offset": offset,
        "segments": shifted_segments,
        "words": words,
    }


def apply_offset(segments, offset):
    if offset == 0:
        return segments

    def shift_time(value):
        return max(0, value + offset)

    return [
        {
            **segment,
            "start": shift_time(segment["start"]),
            "end": shift_time(segment["end"]),
            "words": [
                {
                    **word,
                    "start": shift_time(word["start"]),
                    "end": shift_time(word["end"]),
                }
                for word in segment.get("words", [])
            ],
        }
        for segment in segments
    ]


def track_words(segments, speaker):
    return [
        {
            "text": clean_word_text(word["text"]),
            "start": word["start"],
            "end": word["end"],
            "isGap": False,
            "speaker": speaker,
        }
        for segment in segments
        for word in (segment.get("words") or [])
        if clean_word_text(word["text"])
    ]


def build_merged_subtitles(track_results, gap_threshold):
    words = sorted(
        [word for result in track_results for word in result["words"]],
        key=lambda word: (word["start"], word["end"], word["speaker"]),
    )
    subtitles = []
    previous_end = None
    previous_speaker = None

    for word in words:
        if previous_end is not None and word["start"] - previous_end >= gap_threshold:
            subtitles.append(
                {
                    "text": "",
                    "start": previous_end,
                    "end": word["start"],
                    "isGap": True,
                }
            )

        if word["speaker"] != previous_speaker:
            subtitles.append(
                {
                    "text": f"[{word['speaker']}]",
                    "start": word["start"],
                    "end": word["start"],
                    "isGap": False,
                    "isSpeakerLabel": True,
                    "speaker": word["speaker"],
                }
            )
            previous_speaker = word["speaker"]

        subtitles.append(word)
        previous_end = max(previous_end or word["end"], word["end"])

    return subtitles


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    args = parse_args()
    tracks = parse_tracks(args.track)
    offsets = parse_offsets(args.offset)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    unknown_offsets = sorted(set(offsets) - {track["speaker"] for track in tracks})
    if unknown_offsets:
        print(f"Error: offsets specified for unknown speakers: {', '.join(unknown_offsets)}", file=sys.stderr)
        sys.exit(1)

    WhisperModel = import_faster_whisper()
    print(f"Loading model: {args.model}")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)

    track_results = [
        transcribe_track(model, track, args, offsets.get(track["speaker"], 0.0))
        for track in tracks
    ]

    for result in track_results:
        path = output_dir / f"whisper_{safe_filename(result['speaker'])}.json"
        write_json(path, {key: value for key, value in result.items() if key != "words"})
        print(f"  Track JSON: {path}")

    subtitles_words = build_merged_subtitles(track_results, args.gap_threshold)
    subtitles_path = output_dir / "subtitles_words.json"
    manifest_path = output_dir / "whisper_multitrack_manifest.json"
    write_json(subtitles_path, subtitles_words)
    write_json(
        manifest_path,
        {
            "model": args.model,
            "language": args.language,
            "speaker_mode": "multitrack",
            "tracks": [
                {
                    "speaker": result["speaker"],
                    "source": result["source"],
                    "offset": result["offset"],
                    "word_count": len(result["words"]),
                }
                for result in track_results
            ],
        },
    )

    real_words = [word for word in subtitles_words if not word.get("isGap") and not word.get("isSpeakerLabel")]
    gaps = [word for word in subtitles_words if word.get("isGap")]
    speakers = sorted({word["speaker"] for word in real_words})

    print("Done")
    print(f"  subtitles_words.json: {subtitles_path}")
    print(f"  Manifest: {manifest_path}")
    print(f"  Speakers: {', '.join(speakers)}")
    print(f"  Words: {len(real_words)}")
    print(f"  Gaps: {len(gaps)}")


if __name__ == "__main__":
    main()
