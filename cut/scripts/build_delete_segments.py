#!/usr/bin/env python3
"""Build delete_segments.json from semantic + fine analyses (merged, deduped)."""
import json, sys
from pathlib import Path

base = Path(sys.argv[1])  # path to /cut directory
audio_duration = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0

words = json.loads((base / "1_transcript/subtitles_words.json").read_text(encoding="utf-8"))
actual = [w for w in words if not w.get("isGap") and not w.get("isSpeakerLabel")]

sentences = []
for line in (base / "2_analysis/sentences.txt").read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    parts = line.split("|", 3)
    if len(parts) < 4:
        continue
    idx = int(parts[0])
    s, e = parts[1].split("-")
    sentences.append((idx, int(s), int(e)))

semantic = json.loads((base / "2_analysis/semantic_deep_analysis.json").read_text(encoding="utf-8"))
deleted = {s["sentenceIdx"] for s in semantic["sentences"] if s["action"] == "delete"}

ranges = []
for idx, ws, we in sentences:
    if idx in deleted and ws < len(actual) and we < len(actual):
        ranges.append((actual[ws]["start"], actual[we]["end"], f"sentence_delete S{idx}"))

fine = json.loads((base / "2_analysis/fine_analysis.json").read_text(encoding="utf-8"))
for e in fine["edits"]:
    if e.get("deleteStart") is not None and e.get("deleteEnd") is not None:
        ranges.append((e["deleteStart"], e["deleteEnd"], f"fine_{e.get('type','?')}"))

ranges.sort()
merged = []
for s, e, label in ranges:
    if merged and s <= merged[-1][1] + 0.05:
        merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2] + " + " + label)
    else:
        merged.append([s, e, label])

merged = [(s, e, l) for s, e, l in merged if (e - s) >= 0.05]

segments = [{"start": round(s, 3), "end": round(e, 3)} for s, e, _ in merged]
(base / "2_analysis/delete_segments.json").write_text(
    json.dumps({"segments": segments}, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

total_del = sum(e - s for s, e, _ in merged)
print(f"merged: {len(segments)} segments, total delete {total_del:.1f}s", end="")
if audio_duration:
    print(f" of {audio_duration:.0f}s ({total_del/audio_duration*100:.1f}%); remaining {audio_duration-total_del:.1f}s")
else:
    print()
