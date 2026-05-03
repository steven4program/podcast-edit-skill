<!--
Architecture guardian: when files in this folder change, update this README.
-->

# Editing rules

Universal editing rules shared by all users. Language patterns, detection algorithms, common thresholds — applicable to every Mandarin podcast.

## Files

| File | Type | Content |
| --- | --- | --- |
| `1-core-principles.md` | principle | delete-the-earlier-keep-the-later |
| `2-filler-detection.md` | preference | 嗯/啊/呃 + delete boundary |
| `3-silence-handling.md` | threshold | ≤0.5 s ignore, 0.5–1 s optional, >1 s suggest delete |
| `4-repeated-sentence.md` | preference | adjacent sentences sharing ≥5 chars at start; delete the shorter |
| `5-stutter.md` | preference | fixed list + any adjacent identical words |
| `6-in-sentence-repetition.md` | preference | word-level (A + middle + A) + phrase-level (≥4 char repeat) |
| `7-consecutive-filler.md` | preference | adjacent fillers + leading filler run (≥3) |
| `8-self-correction.md` | preference | partial repeat, negation correction, word interrupted |
| `9-residual-sentence.md` | preference | sentence cut off mid-way |
| `10-content-analysis-methodology.md` | methodology | 5a paragraph-level content analysis |
| `llm-fine-edit-prompt-template.md` | **LLM guide** | **5b LLM-layer detection checklist and prompt template** |

## AI review priority

1. **Silence > 1 s** → suggest delete (split into 1 s grid)
2. **Residual sentence** → delete (half-sentence + silence)
3. **Repeated sentence** → delete the shorter (start ≥5 chars match)
4. **In-sentence repetition** → word-level (A + middle + A) + phrase-level ≥4-char
5. **Stutter** → delete the earlier (fixed list + any adjacent identical words)
6. **Self-correction** → delete the earlier (partial repeat, negation, word interrupted)
7. **Filler** → mark for human confirmation (嗯/啊/呃)

## Core principle

**Delete the earlier, keep the later**: the second iteration is usually more complete.

## Architecture: rule layer + LLM layer

Step 5b fine-cut uses a hybrid architecture:

| Layer | Responsibility | Output |
| --- | --- | --- |
| **Rule layer** (`run_fine_analysis.js`) | silence detection (needs audio timestamps), basic stutter (consecutive same-word + reduplication/ABB exemption) | `fine_analysis_rules.json` |
| **LLM layer** (Claude in current session) | leading filler, self-correction, residual sentence, pure-filler sentence, in-sentence repetition, consecutive filler | `fine_analysis_llm.json` |
| **Merge** (`merge_llm_fine.js`) | LLM text spans → map back to word-level timestamps → dedupe with rule layer | `fine_analysis.json` |

**Why hybrid**: pure rules can't reliably catch "self-correction" and other types that need semantic understanding (39 % of dexter samples missed by rules were self-corrections). The LLM layer adds the semantic muscle.
