#!/usr/bin/env python3
"""Build sentences.txt from subtitles_words.json using isGap as sentence boundary.

Fallback for single-speaker Whisper MVP output that has no punctuation.
"""
import json, sys
from pathlib import Path

words_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])

words = json.loads(words_path.read_text(encoding="utf-8"))

sentences = []
buf_words = []
buf_start = 0
speaker = "Speaker 0"
real_idx = 0

def flush():
    global buf_words, buf_start
    if not buf_words:
        return
    text = "".join(w["text"] for w in buf_words)
    end_idx = buf_start + len(buf_words) - 1
    sentences.append(f"{len(sentences)}|{buf_start}-{end_idx}|{speaker}|{text}")
    buf_start = end_idx + 1
    buf_words = []

for w in words:
    if w.get("isSpeakerLabel"):
        speaker = w.get("speaker", speaker)
        flush()
        continue
    if w.get("isGap"):
        flush()
        continue
    buf_words.append(w)
    real_idx += 1
    # also break on very long buffer (40+ words) to keep chunks tractable
    if len(buf_words) >= 40:
        flush()

flush()

out_path.write_text("\n".join(sentences) + "\n", encoding="utf-8")
print(f"sentences: {len(sentences)}")
