<!--
input: subtitles_words.json (entries with isGap=true)
output: list of long-silence indices
pos: rule, must-delete priority

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
-->

# Silence handling (podcast version)

## Threshold rules (podcast-specific)

| Silence length | Action | Notes |
| --- | --- | --- |
| ≤ 0.8 s | **ignore** — natural pause | podcasts need breathing room |
| 0.8–2 s | **flag delete** — keep 0.8 s natural pause | delete the excess |
| 2–5 s | **suggest delete** — clear stumble | judge by context |
| > 5 s | **must delete** — serious issue | cutting accident or technical problem |

> **User feedback (zihao, 2026-02-22)**: 0.8 s is a preference value; sentence-leading pauses are especially noticeable.

## Podcast considerations

### Multi-host conversation
- Pauses between turn-taking (1–1.5 s) are normal — **don't delete**
- Thinking time after a question (1–2 s) — **should keep**
- Only abnormally long pauses need handling

### Solo podcast
- Can be tighter than multi-host
- Thinking pauses (1–1.5 s) still moderately kept
- Only consider deleting at 2 s+

## Output format

**Mark whole segments, do not split**

Example: 3.2 s silence → emit 1 entry
```
| 64-66 | 12.86-15.80 | silence 3.2 s | exceeds 2 s threshold | delete |
```

Example: 1.5 s silence → usually keep
```
| 64-66 | 12.86-14.36 | silence 1.5 s | natural pause | keep |
```

The user can change the decision in the review page.

## Special cases

### Very long silence
5 s+ contiguous silence: mark as a single block, pre-select for delete:
```
| 323-371 | 71.38-131.38 | silence 60 s | serious anomaly | delete |
```

### Opening / closing silence
- Opening silence (>0.5 s) — suggest delete (the opening tolerates less; even short pauses should be flagged).
- Closing silence — context-dependent; may be a fade-out.
- **Note**: in the first 5 minutes the silence threshold should be lower than for the body — first impressions matter.

### Sentence-leading pause display
- Silence gaps live in fine_analysis attached to the **previous sentence** (the one containing the last word before the gap).
- But the user's perception of the pause is at the **start of the next sentence**.
- **Fix**: `generate_review_enhanced.js` passes each silence edit to the next sentence as `incomingSilences`; the review page renders `⏸ -Xs` at the sentence head.
- The marker is clickable and links back to the original silence edit's fineEdit idx.

### Post-merge gap cleanup

**Problem**: the rule layer detects silence on the **original timeline**. After deletions (filler, self-correction, whole-sentence delete), surrounding short silences merge into a larger gap.

**Example**:
```
Original: [word A] 0.3s [delete] 0.6s [word B]
After:    [word A] --------0.9s-------- [word B]  ← exceeds 0.8 s but undetected!
```

**Fix**: after merge (rules + LLM), do a second-pass scan:
1. Collect all delete ranges (fine edits + 5a sentence-level deletes).
2. Merge overlapping ranges.
3. Simulate the post-delete timeline; compute the gap between adjacent kept words.
4. Gap > 0.8 s → add a `silence_merged` edit, trim down to 0.8 s.

**Where**: at the end of `merge_llm_fine.js`'s merge step.

> **User feedback (lucia, 2026-02-24)**: deleting "嗯。" merged the surrounding silences into a 2.0 s gap. Post-merge scan caught 134 merged gaps in that episode and trimmed an additional 182 seconds.

### Final-output silence trim (Step 8b)

Post-merge gap cleanup is **prediction-based** — performed at merge time. But user manual edits (restore/delete) in the review page create new merged gaps the prediction can't see.

**Final safety net**: `trim_silences.py` runs `silencedetect` directly on the final MP3 and trims every >0.8 s pause. Doesn't need word-level data or delete_segments — pure audio operation.

**Parameter notes**:
- detection threshold (`--threshold`) = 0.8 s
- retain target (`--target`) = 0.6 s (0.2 s below threshold)
- Reason: silencedetect's boundary and the cut point don't perfectly align; 0.6 s ensures the trimmed silence stays under 0.85 s.

> **User feedback (lucia, 2026-02-24)**: final still had 322 pauses >0.8 s (max 2.76 s); `trim_silences.py` resolved them and saved another 98 s.

### In-sentence vs inter-sentence pause perception

An **inter-sentence** 0.8 s pause is fully natural (breath, digestion). But after deleting a word **inside** a sentence, even a 0.3 s gap reads as "stuck".

**Rule**: when deleting in-sentence content (stutter, self_correction, in-sentence filler), the delete range MUST extend to `[prev_word.end, next_word.start]` so the surrounding words splice seamlessly.

> **User feedback (lucia, 2026-02-24)**: s49 deleted "他也" mid-sentence; a 0.68 s gap sounded like a 1 s pause. Extending to `[279.82, 281.46]` fixed it.

### Context judgment

If the silence sits between two sentences of the same continuous topic, even slightly long (1.5–2 s) is keep-able.
If the silence looks like a cutting accident or technical issue, even <2 s should be deleted.
