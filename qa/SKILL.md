---
name: podcast-edit:qa
description: |
  Podcast-cut quality assurance — three-phase automated review.
  Phase A (data layer): after cut_audio, validate delete_segments — restored sentences mistakenly cut, manual deletes not applied, silence at cut points, large-deletion seams.
  Phase B (signal layer): signal analysis on the cut audio (energy/spectrum/silence), with optional Gemini AI listening — flag clips that need human review.
  Phase C (semantic layer, optional): re-transcribe the cut audio and align via LCS to detect residual fillers, residual stutters, semantic breaks, and missing content.
  Triggers — qa, audit, check edit, review the cut, 質檢, 審查剪輯.
---

<!--
input: delete_segments + cut audio
output: QA report (JSON + readable summary)
pos: run after /podcast-edit-cut

Architecture guardian: when this file is modified, also update:
1. ../README.md skill table
2. ../install/SKILL.md symlink registration
-->

# Podcast-cut Quality Assurance

> Three-phase automated review. Hand the mechanical checks to scripts so the user only listens where human judgment is actually needed.

---

## ⚠️ Ask before starting

**At the very beginning, ask the user for:**

```
Please provide:

1. **output directory**
   - e.g. `output/2026-02-27_meeting_02`
   - Must contain `1_transcript/`, `2_analysis/`, `3_output/`

2. **cut audio path** (Phase B needs it)
   - e.g. `output/.../3_output/podcast_final_v14_trimmed.mp3`

3. **(optional) Gemini API key**
   - If `GEMINI_API_KEY` env var is set, AI listening is enabled.
   - Without it, Phase A + signal-layer analysis already catches most issues.
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      Podcast-cut QA                       │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  Phase A: data layer (audit_cut.js)                  │ │
│  │                                                      │ │
│  │  Inputs: delete_segments + fine_analysis + sentences │ │
│  │                                                      │ │
│  │  Check 1: restored-sentence integrity (excl. fine)   │ │
│  │  Check 2: user-manual deletions actually applied     │ │
│  │  Check 3: silence at cut points                      │ │
│  │  Check 4: large-deletion seam review                 │ │
│  │                                                      │ │
│  │  → auto_fix.js auto-fixes what it can                │ │
│  │  → re-cut → re-verify                                │ │
│  └─────────────────────────────────────────────────────┘  │
│                          ↓                                 │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  Phase B: signal layer                               │ │
│  │                                                      │ │
│  │  Input: cut audio (MP3/WAV)                          │ │
│  │                                                      │ │
│  │  Layer 1: signal analysis (signal_analysis.py / librosa)│
│  │    - spectral jumps (MFCC cosine similarity)         │ │
│  │    - unnatural silence (silence duration)            │ │
│  │    - podcast mode: skip energy_jump (always FP)      │ │
│  │                                                      │ │
│  │  Layer 2: AI listening (ai_listen.py / Gemini, optional)│
│  │    - global sample: six 30 s clips for overall pacing│ │
│  │    - suspicious-clip review: AI re-checks Layer 1 HIGH│ │
│  │                                                      │ │
│  │  Layer 3: combined report (report_generator.py)      │ │
│  └─────────────────────────────────────────────────────┘  │
│                                                           │
│  Output: audit_report.json + qa_report.json + qa_summary.md│
└──────────────────────────────────────────────────────────┘
```

**Design**: Phase A needs no audio — pure data validation. Phase B Layer 1 needs no API key — pure local computation. Layer 2 is icing.

---

## Quick start

```
User: check the cut quality
User: qa this podcast
User: audit my cut
User: check edit
```

---

## Phase A: data layer

Run after cut_audio, before the user listens. You can also run it after a re-cut to confirm all feedback was applied correctly.

### A1: audit

```bash
node <skill_dir>/scripts/audit_cut.js <output_dir>
```

`<output_dir>` is the directory containing `1_transcript/`, `2_analysis/`, `3_output/`.

The script runs four checks:

**Check 1 — restored-sentence integrity**
Walk every sentence the user restored. For each word check it isn't covered by a delete segment. Key improvement: exclude *intentional* fine edits (stutter/filler deletes) — only report unexplained coverage. This catches:
- legacy whole-sentence segments left over from old HTML exports
- accidental cross-sentence segment coverage (when restore didn't clean up the original AI segment)

**Check 2 — manual deletions**
Read `user_corrections.added_deletions` (sentences the user explicitly deleted) and verify the corresponding time range really has a delete segment. `missed_catches` (AI suggestions) are only checked when the timestamp is precise.

**Check 3 — silence at cut points**
Scan every kept-region between adjacent segments. If the kept region has no speech and exceeds 0.3 s, flag it as a potential pause. These pauses aren't audible in the source but become obvious after editing the surroundings.

**Check 4 — large deletions**
List every delete > 5 s with surrounding text. Cannot be auto-judged but lets the user jump straight to the spots that warrant attention.

### A2: auto-fix

```bash
# Dry-run first to see what would change
node <skill_dir>/scripts/auto_fix.js <output_dir> --dry-run

# Apply (originals are backed up)
node <skill_dir>/scripts/auto_fix.js <output_dir>
```

Auto-fix covers:
- **restored-sentence coverage** → remove the offending segment (excluding intentional fine edits)
- **silence at cut points** → extend the adjacent segment to absorb the silence

Not auto-fixed (reported only):
- **manual deletion not applied** → needs human confirmation of the exact range
- **large-deletion seams** → needs the human ear

### A3: re-cut and re-verify

After auto-fix, re-run `cut_audio.py` and `trim_silences.py`, then run `audit_cut.js` once more to confirm all issues are gone.

### A4: produce a listening guide

Once Phase A passes, generate a concise listening guide for the user. Include:
- timestamps of large deletions with before/after text
- timestamps of any previously-reported bug for the user to confirm the fix
- timestamps of new manual deletions
- timestamps of restored sentences (confirm they survived intact)

Sort by time, with approximate output-audio time (offset by total deleted duration), so the user can scrub through.

---

## Phase B: signal layer

Run after Phase A passes and the audio is re-cut. Analyzes the output audio's signal quality directly.

### B1: Layer 1 — signal analysis

```bash
python3 <skill_dir>/scripts/signal_analysis.py \
  --input <audio-path> \
  --output <output_dir>/2_analysis/qa_signal_report.json
```

Auto-detects cut points and runs five checks (energy ratio, unnatural silence, waveform discontinuity, spectral jump, breath truncation).

**Podcast-mode optimization** (applied automatically in `report_generator`):
- `energy_jump` is always FP in podcasts (natural prosody / speaker swap) — skipped
- `zcr_discontinuity` / `breath_truncation` over-trigger in podcasts — skipped
- Keep only `spectral_jump` and `unnatural_silence`

### B2: Layer 2 — AI listening (optional)

```bash
# Needs GEMINI_API_KEY (env var or .env)
python3 <skill_dir>/scripts/ai_listen.py \
  --input <audio-path> \
  --signal-report <output_dir>/2_analysis/qa_signal_report.json \
  --output <output_dir>/2_analysis/qa_ai_report.json
```

Two sampling strategies:
- **global sample**: six evenly-spaced 30 s clips for overall pacing and consistency
- **suspicious-clip recheck**: AI re-confirms Layer 1's HIGH issues (cuts FP rate)

### B3: Layer 3 — combined report

```bash
python3 <skill_dir>/scripts/report_generator.py \
  --signal <output_dir>/2_analysis/qa_signal_report.json \
  --ai <output_dir>/2_analysis/qa_ai_report.json \
  --output <output_dir>/2_analysis/qa_report.json \
  --summary <output_dir>/2_analysis/qa_summary.md
```

Merges Layer 1 and Layer 2, produces a combined score and a human-readable summary.

---

## Phase C: semantic layer

Run after Phase A/B. Re-transcribes the cut audio, aligns it against the expected text, and detects residual issues and semantic breaks.

**Default: optional** (extra cost — another ~3 min Aliyun API call). Auto-enabled when Phase A/B finds issues.
User toggle: `workflow_automation.semantic_review_enabled` in preferences.

### C1: re-transcribe + align

```bash
# 1. Re-transcribe the cut audio
bash <skill_dir>/../cut/scripts/aliyun_funasr_transcribe.sh <cut_audio_url> <speaker_count>

# 2. Generate word-level transcript
node <skill_dir>/../cut/scripts/generate_subtitles_from_aliyun.js \
  <new_transcription.json> <speaker_mapping.json>

# 3. Semantic review
node <skill_dir>/scripts/semantic_review.js \
  --new-words <new_subtitles_words.json> \
  --original-words <original_subtitles_words.json> \
  --delete-segments <delete_segments_edited.json> \
  --sentences <sentences.txt> \
  --output <output_dir>/2_analysis/qa_semantic_report.json
```

### C2: four checks

| Check | What | How |
| --- | --- | --- |
| **C1 residual fillers** | leftover 嗯/啊/那個/對/就是 after the cut | pattern match the re-transcript |
| **C2 residual stutters** | leftover 我我 / 他他 patterns | repetition pattern detection |
| **C3 semantic break** | discontinuity around a cut point | Claude evaluates a 10-sentence window |
| **C4 missing content** | content that should have been kept is gone | word-level LCS alignment gaps |

### C3: output

`2_analysis/qa_semantic_report.json`:

```json
{
  "phase": "C",
  "checks": {
    "residual_fillers": [{ "time": 12.5, "text": "嗯", "context": "..." }],
    "residual_stutters": [],
    "semantic_breaks": [{ "cut_point_time": 345.2, "before": "...", "after": "...", "severity": "HIGH" }],
    "missing_content": [{ "expected": "this point matters", "time_range": [120, 125] }]
  },
  "summary": { "total_issues": 3, "by_severity": { "HIGH": 1, "MEDIUM": 2 } }
}
```

---

## Full flow

```
0. Ask user: output directory + audio path
    ↓
1. Phase A: data-layer QA
   a. audit_cut.js → check delete_segments
   b. auto_fix.js → fix what's auto-fixable
   c. re-cut audio (if anything was fixed)
   d. re-run audit_cut.js to confirm clean
    ↓
2. Phase B: signal-layer QA
   a. signal_analysis.py → detect cut-point signal issues
   b. ai_listen.py → AI listening (optional)
   c. report_generator.py → combined report
    ↓
3. Phase C: semantic-layer QA (optional, auto-on if A/B finds issues)
   a. re-transcribe the cut audio
   b. semantic_review.js → four checks
   c. Claude evaluates semantic breaks (C3)
    ↓
4. Show summary to the user
   - Phase A unresolved (manual deletes not applied, …)
   - Phase B clips needing review (spectral jumps, …)
   - Phase C semantic issues (residual fillers, breaks, …)
   - Large-deletion seam review points
    ↓
Done
```

---

## Inputs / outputs

**Phase A inputs (read automatically from `output_dir`):**
- `2_analysis/delete_segments_edited.json` (or `delete_segments.json`)
- `2_analysis/fine_analysis.json`
- `2_analysis/sentences.txt`
- `2_analysis/segment_corrections.json` (if present)
- `2_analysis/ai_feedback_*.json` (if present)
- `1_transcript/subtitles_words.json`

**Phase B inputs:**
- the cut audio file (MP3 / WAV / M4A)
- (optional) `GEMINI_API_KEY`

**Outputs:**
- `2_analysis/audit_report.json` — Phase A data-layer report
- `2_analysis/qa_signal_report.json` — Layer 1 signal-analysis report
- `2_analysis/qa_ai_report.json` — Layer 2 AI report (optional)
- `2_analysis/qa_report.json` — combined report (JSON)
- `2_analysis/qa_summary.md` — combined report (Markdown)
- `2_analysis/qa_semantic_report.json` — Phase C semantic report (optional)

---

## Progress checklist

Create on entry:

```
- [ ] Ask user: output directory + audio path
- [ ] Phase A: data-layer QA (audit_cut.js)
- [ ] Phase A: auto-fix (auto_fix.js)
- [ ] Phase A: re-cut and re-verify (if needed)
- [ ] Phase B: Layer 1 signal analysis
- [ ] Phase B: Layer 2 AI listening (optional)
- [ ] Phase B: Layer 3 combined report
- [ ] Phase C: semantic-layer QA (if needed)
- [ ] Show summary to user
```

---

## Common issue patterns

### Phase A common issues

| Pattern | Root cause | Auto-fix? |
| --- | --- | --- |
| restored sentence still cut (not a fine edit) | legacy HTML export or cross-sentence segment | ✅ remove the offending segment |
| restored-sentence fine edit removed | over-aggressive batch fix (now avoided via per-edit match) | — no longer occurs |
| unnatural pause after deletion | natural source silence exposed by the cut | ✅ extend segment to absorb |
| manual whole-sentence deletion not applied | pipeline didn't generate a segment for that sentence | ❌ needs human confirmation of range |
| large-deletion seam unnatural | crossed topics / context break | ❌ needs the human ear |

### Phase B common issues

| Pattern | Root cause | Resolution |
| --- | --- | --- |
| `energy_jump` is all FP | natural prosody / speaker swap | podcast mode skips automatically |
| `spectral_jump` | background-noise change at the cut | needs human listening |
| `unnatural_silence` | overly short or long silence at cut | adjust the cut range |

---

## Pitfalls / lessons learned

### Pitfall 1: in podcasts, energy_jump is always FP

Signal analysis on a 56-min podcast flagged 1725 issues (score 1.0/10), almost all `energy_jump`. Natural speaker swaps and prosody produce 10× – 105× ratios. AI re-check confirmed the top 10 most extreme jumps (105×, 78×, 72×…) are **all FP**.

Solution: in podcast mode, skip `energy_jump` entirely. Keep only `spectral_jump` and `unnatural_silence`. Filtered count: 800 → 1; clips needing review: 394 → 9.

### Pitfall 2: Gemini model name needs to track latest

`gemini-2.0-flash` returns 404. Use `gemini-2.5-flash`. Models update frequently; on 404 use `client.models.list()` to inspect what's available.

### Pitfall 3: Check 1 false positive — intentional fine edit

Check 1 reports a restored sentence covered by a segment, but the segment is actually an intentional stutter/filler delete the user kept.

Solution: `audit_cut.js` matches segments to fine_analysis edits and treats overlap (>50 % or >0.3 s) as intentional, not reported.

### Pitfall 4: API keys auto-load from .env

`ai_listen.py` reads `GEMINI_API_KEY` from `.env` at the project root automatically — no manual export needed.

---

## Dependencies

```txt
# Phase A (Node.js)
node >= 16

# Phase B (Python)
librosa>=0.10.0
numpy>=1.24.0
soundfile>=0.12.0
google-genai>=1.0.0    # optional, for Layer 2
```

```bash
pip install librosa numpy soundfile
pip install google-genai          # optional, enables AI listening
```

---

## Real-world numbers (2026-02-22)

| Metric | Value |
| --- | --- |
| Audio | podcast_final.mp3 (56:32) |
| Layer 1 raw issues | 1725 |
| After podcast-mode filter | 1 (spectral_jump) |
| Layer 2 AI score | 7.3/10 |
| AI re-check FP rate | 100 % (10/10 false positives) |
| Combined score | 8.3/10 |
| Clips needing review | 9 (~45 s) |
| Gemini model | gemini-2.5-flash |
| API calls | 16 (6 global + 10 suspicious recheck) |

---

## Relationship to other skills

```
/podcast-edit-cut    → Stages 1–5 + 8 (transcribe → analyze → review → cut → final review)
/podcast-edit-qa     → Stage 6 (this skill: Phase A + B + C)
/podcast-edit-polish → Stage 7 (post-production)
```

This skill corresponds to **Stage 6** of the pipeline — between "cut execution" (Stage 5) and "post-production" (Stage 7).
