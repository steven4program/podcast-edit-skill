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

# Safe-cut overlay: when safe_cut_plan.json exists, the planner has opinions
# about a SUBSET of fine edits (filler / stutter / self_correction etc).
# Silence handling stays with the existing pipeline — those edits are not in
# the plan and pass through unconditionally. For plan-mentioned edits, only
# 'auto' verdict gets applied; 'review' / 'skip' are pre-disabled.
safe_plan_path = base / "2_analysis/safe_cut_plan.json"
plan_verdict = {}    # fe_idx → 'auto' | 'review' | 'skip'
snapped_by_idx = {}
if safe_plan_path.exists():
    plan = json.loads(safe_plan_path.read_text(encoding="utf-8"))
    for c in plan.get("candidates", []):
        idx = c.get("feIdx")
        if idx is None:
            continue
        plan_verdict[idx] = c.get("verdict")
        # Prefer the inner-snapped (Tier-2 breath-preserving) cut points when
        # available; fall back to the outer snap if not.
        inner = c.get("snapped_inner")
        if inner and inner.get("applied"):
            snapped_by_idx[idx] = (inner["start"], inner["end"])
        elif c.get("snapped"):
            snapped_by_idx[idx] = (c["snapped"]["start"], c["snapped"]["end"])
    s = plan.get("summary", {})
    print(f"safe-cut overlay: {s.get('auto',0)}/{s.get('total',0)} filler-class auto-applied; {s.get('silence_passthrough',0)} silence edits via existing pipeline")

for e in fine["edits"]:
    if e.get("deleteStart") is None or e.get("deleteEnd") is None:
        continue
    fe_idx = e.get("idx")
    # If the planner has an opinion, honor it; otherwise pass through.
    if fe_idx in plan_verdict and plan_verdict[fe_idx] != "auto":
        continue
    if fe_idx in snapped_by_idx:
        ds, de = snapped_by_idx[fe_idx]
    else:
        ds, de = e["deleteStart"], e["deleteEnd"]
    ranges.append((ds, de, f"fine_{e.get('type','?')}"))

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
