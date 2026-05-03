# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A bundle of four **Claude Code skills** that together form an end-to-end Mandarin podcast editing pipeline (transcription → semantic analysis → interactive review → cut → QA → polish). There is **no application binary**; the "product" is the four skill directories that get symlinked into `~/.claude/skills/` and invoked via slash commands.

| Directory | Slash command | Role |
| --- | --- | --- |
| `install/` | `/podcast-edit-install` | Setup: register symlinks, install deps, configure API keys |
| `cut/` | `/podcast-edit-cut` | Orchestrator. Stages 1–5 + 8. Owns transcription, AI cut analysis, the review UI, and the actual audio cutting. |
| `qa/` | `/podcast-edit-qa` | Stage 6. Three-layer QA: data (delete-segment correctness), signal (energy/spectrum/silence), semantic (re-transcribe + LCS align). |
| `polish/` | `/podcast-edit-polish` | Stage 7. Highlight teaser, intro/outro music, chapter timestamps, title, show notes. |

## How a Claude session enters this code

A user types `/podcast-edit-cut <audio>` (or install/qa/polish). Claude Code loads the corresponding `SKILL.md` as the system prompt and follows the bash/node commands inside it step-by-step. Editing any `SKILL.md` directly changes Claude's behavior in future sessions — `SKILL.md` files are runtime instructions, not docs.

`cut/SKILL.md` is the master orchestrator: the 8-stage pipeline and every script invocation are defined there. Read it end-to-end before changing pipeline behavior. The "Pitfalls" section (1–41) documents already-fixed bugs — re-read them before changing `cut_audio.py`, `merge_llm_fine.js`, `refine_boundaries.py`, or anything that touches delete-segment time math.

## Common tasks

### Register / re-register skills (after cloning or moving the repo)

```bash
SKILL_DIR="/Users/kaiwei/side-projects/podcast-edit-skill"   # adjust if moved
mkdir -p ~/.claude/skills
ln -s "$SKILL_DIR/install" ~/.claude/skills/podcast-edit-install
ln -s "$SKILL_DIR/cut"     ~/.claude/skills/podcast-edit-cut
ln -s "$SKILL_DIR/polish"  ~/.claude/skills/podcast-edit-polish
ln -s "$SKILL_DIR/qa"      ~/.claude/skills/podcast-edit-qa
# Restart Claude Code to pick up new skills.
```

### Install runtime deps

```bash
brew install node ffmpeg                    # required
pip install librosa soundfile numpy         # required by qa/scripts/signal_analysis.py and refine_boundaries.py
pip install deepfilternet                   # optional, used by polish noise reduction
npm install                                  # only js-yaml — the only npm dep at repo root
```

### Configure API keys

`.env` at repo root (copy from `.env.example`):
- `DASHSCOPE_API_KEY` — required, Aliyun FunASR transcription
- `GEMINI_API_KEY` — optional, enables Phase B Layer 2 AI listening in QA
- `OSS_*` — optional, only if uploading via Aliyun OSS instead of uguu.se
- `PODCAST_EDIT_USER` — selects which `cut/user-prefs/<userId>/` profile to load

There is no test suite, no linter, and no build step. Validation is end-to-end on a real audio file plus inspection of the `output/` directory.

## Architecture: how the 8 stages glue together

```
 audio.mp3 ──(stage 2.1)──> aliyun_funasr_transcription.json
                              │
                              ├─(2.1)─> subtitles_words.json   ← word-level, source of truth for all timing
                              └─(2.1)─> sentences.txt
                                          │
                                          ├─(2.2 LLM, paragraph-level)─> semantic_deep_analysis.json
                                          │
                                          └─(2.3 hybrid)─> fine_analysis_rules.json   (run_fine_analysis.js)
                                                       └─> fine_analysis_llm.json     (Claude in-context)
                                                       └─> fine_analysis.json         (merge_llm_fine.js → refine_fine_analysis.js → waveform_trim.py)

 → (stage 3) review_agent_catches.json (auto-review fills LLM gaps)
 → (stage 4) review_enhanced.html  ← user edits in browser, exports delete_segments_edited.json
 → (stage 5) cut_audio.py + trim_silences.py → 3_output/*_final_v*.mp3
 → (stage 6) /podcast-edit-qa → audit_report.json + qa_signal_report.json [+ qa_semantic_report.json]
 → (stage 7) /podcast-edit-polish → highlight teaser + music + chapter timestamps
 → (stage 8) review_final.html → episode_history.json
```

Per-episode output lives at `output/<YYYY-MM-DD>_<audio_name>/cut/{1_transcript,2_analysis,3_output}/`. Scripts assume this layout — do not rename directories without updating `cut/SKILL.md` and the consuming scripts.

### Two-tier learning system (read before touching feedback code)

| Tier | Location | Scope | Updated by |
| --- | --- | --- | --- |
| **Shared rules** | `cut/editing-rules/{1-9}-*.md`, `llm-fine-edit-prompt-template.md`, `10-content-analysis-methodology.md` | Detection methodology, default thresholds, LLM prompt content. Affects every user. | Manual edits when QA reveals a methodology gap; pitfall 31. |
| **Per-user prefs** | `cut/user-prefs/<userId>/preferences.yaml` and `editing_rules/*.yaml` | Aggressiveness, per-filler-word rates, retain/delete preferences. Affects one user. | `analyze_feedback.js` → `apply_feedback_to_rules.js` driven by review-page exports. |

`scripts/user_manager.js` is the only sanctioned reader/writer for both tiers — go through it instead of fs-reading the YAML directly.

### Stage 2.3: rules + LLM hybrid for fine-grained edits

Step 5b is intentionally split:
- **Rule layer** (`run_fine_analysis.js`) handles deterministic patterns at high recall: leading filler, silence detection, consecutive-same-word stutters, suffix-match stutters, in-sentence phrase repetition, consecutive fillers, restart signals.
- **LLM layer** (Claude reading 50–80 sentences/batch) is the sole source of `self_correction` (~39% of unique misses), `residual_sentence`, `repeated_sentence`, `production_talk`, and confirms/rejects the rule layer's `needsReview` items. The exact prompt is in `cut/editing-rules/llm-fine-edit-prompt-template.md` — keep prompt and detection lists in sync.
- `merge_llm_fine.js` maps LLM text spans back to word-level timestamps and dedupes against the rule layer. `refine_fine_analysis.js` + `refine_boundaries.py` (energy-valley onset detection) snap boundaries to acoustic syllable gaps. `waveform_trim.py` does a final RMS-envelope pass.

Anything that produces `delete_segments` MUST eventually flow through `cut_audio.py` (with `--no-fade`) on `audio_original.*`, never on the 16 kHz `audio.mp3` (pitfalls 17, 27, 41).

## Hardcoded paths to know about

The bash blocks inside `cut/SKILL.md`, `qa/SKILL.md`, and `polish/SKILL.md` use a `SKILL_DIR` variable. The scripts default to `${SKILL_DIR:-$HOME/podcast-edit-skill}` so the repo is portable across machines. When running the skills locally, either:

1. `export SKILL_DIR=/path/to/your/checkout` before invoking the skill, or
2. let Claude resolve it from the symlinked `~/.claude/skills/podcast-edit-*` location.

If you change `SKILL_DIR` references, search-and-replace across all three `SKILL.md` files; do not patch only one.

## Architecture guardian rules (already documented in the files)

Each `SKILL.md` opens with an "Architecture guardian" comment that lists files that must be updated together. Honour them:

- Modifying any of `install/SKILL.md`, `cut/SKILL.md`, `qa/SKILL.md`, `polish/SKILL.md` → also update `README.md`'s skill table.
- Adding/removing/renaming files in `cut/editing-rules/` → update `cut/editing-rules/README.md`'s file inventory.
- Detection methodology changes (e.g. new self_correction sub-pattern) belong in `cut/editing-rules/`, **not** in user prefs (pitfall 31).

## Critical, non-obvious invariants

- `subtitles_words.json` filters out `isGap` and `isSpeakerLabel` to produce `actual_words`; sentence-level `wordIdx` indexes into `actual_words`, never into the raw `words` array. Mixing them up offsets every sentence's `startTime` (pitfall in `generate_review_enhanced.js`).
- The review HTML must be served the **CBR** `audio_seekable.mp3`, not the VBR `audio.mp3`. VBR drift makes seek-by-click drift several seconds late in the back half (Stage 2.1 prep step).
- Deleting in-sentence content extends the cut range to `[prev_word.end, next_word.start]`, not `[word.start, word.end]` — ASR onset lag bleeds the deleted token into the kept audio otherwise (pitfalls 25/26/39).
- Never call `cut_audio.py` without `--no-fade` (pitfall 27). The default 0.3 s adaptive fade eats short syllables.
- Speaker count for FunASR must be supplied by the user; auto-guessing tanks accuracy from 98.8 % to unusable (top of `cut/SKILL.md`).
- `review_enhanced.html` persists user edits in `localStorage`. Regenerating the HTML on top of a user-reviewed file silently destroys their work — back it up and confirm before regenerating (top of `cut/SKILL.md`).
