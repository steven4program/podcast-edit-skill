# Podcast Editing Flow

> Distillation of the 8-stage pipeline defined in `cut/SKILL.md`.
> The original SKILL is ~1900 lines; this is the high-level human-readable summary.
> For exact execution details, see `cut/SKILL.md`.

---

## 1. Positioning

`/podcast-edit-cut` is the **orchestrator** of the whole pipeline — end-to-end from raw recording to final MP3. The other three skills are its downstream:

| Skill | Role |
| --- | --- |
| `cut` | Main flow, covers stages 1–5 + 8 |
| `qa` | Stage 6 (post-cut QA) |
| `polish` | Stage 7 (intro/outro music, chapters, titles) |
| `install` | Setup: symlinks + dependencies + API keys |

Design principle: **every feedback writes into skill files** (`cut/editing-rules/` or `cut/user-prefs/`), **not into agent memory**. Survives machine and account changes.

---

## 2. The 8 stages at a glance

```
┌───────────────────────────────────────────────────────────────┐
│ User preferences (persistent)                                  │
│ editing-rules/ (shared) + user-prefs/<userId>/ (personal)      │
│ ← feedback writeback at stages 4, 6, 8                        │
└───────────────────────────────────────────────────────────────┘
        ↓                      ↓                       ↓
┌──────────────┐  ┌────────────────────┐   ┌────────────────────┐
│ Stage 1      │→ │ Stage 2  cut       │→  │ Stage 3 AI self    │
│ user start   │  │ analysis           │   │ review             │
│ new/existing │  │ 2.1 transcribe     │   │ check 5a + 5b      │
│ load prefs   │  │ 2.2 rough cut      │   │ find misses + FPs  │
│              │  │ 2.3 fine cut       │   │                    │
└──────────────┘  └────────────────────┘   └────────────────────┘
                                                    ↓
┌──────────────┐  ┌────────────────────┐   ┌────────────────────┐
│ Stage 6 QA   │← │ Stage 5 cut exec   │←  │ Stage 4 user       │
│ data+signal+ │  │ cut_audio.py       │   │ review             │
│ semantic     │  │ trim_silences.py   │   │ review_enhanced     │
│ /podcast-    │  │                    │   │ .html, interactive  │
│ edit-qa      │  │                    │   │ export delete_      │
│              │  │                    │   │ segments_edited.json│
└──────────────┘  └────────────────────┘   └────────────────────┘
        ↓
┌──────────────┐  ┌────────────────────┐
│ Stage 7      │→ │ Stage 8 final user │
│ polish       │  │ review             │
│ music+chap   │  │ review_final.html  │
│ +title       │  │ pass → write       │
│ /podcast-    │  │ episode_history    │
│ edit-polish  │  │ .json              │
└──────────────┘  └────────────────────┘
```

---

## 3. Stage by stage

### Stage 1: user start

The first thing on entry: ask **who you are**. Don't make the user guess what's needed.

| Path | Flow |
| --- | --- |
| Existing user | Load `user-prefs/<userId>/preferences.yaml` + `editing_rules/` → one-line: "What audio today and how many speakers?" |
| New user (path A, **most accurate**) | Provide before/after sample audio → `analyze_editing_samples.py` extracts preferences → `generate_rule_overrides.js` writes per-user rules |
| New user (path B) | One structured question: audience / purpose / target duration / aggressiveness (conservative 10–20 % / moderate 20–35 % / aggressive 35–50 %) / special needs |

**Hard rule**: speaker count must come from the user. Don't guess. The wrong count tanks FunASR's 98.8 % accuracy.

### Stage 2: cut analysis (the core)

#### 2.1 Infrastructure

```
audio.mp3 (source)
    ├─ audio_original.<ext>     ← used for cutting; preserves original quality
    ├─ audio.mp3 (16k mono)     ← used for transcription; FunASR likes low rate
    └─ audio_seekable.mp3 (CBR) ← used for the review page; VBR drifts seek
```

Then **upload to uguu.se → Aliyun FunASR (~3 min) → identify speakers → produce `subtitles_words.json` (word-level timestamps, source of truth for everything downstream)**.

From `subtitles_words.json` cut `sentences.txt` (sentenceIdx | wordIdxRange | speaker | text), feed to the next two analyses.

#### 2.2 Rough cut (paragraph level) — `semantic_deep_analysis.json`

**This is LLM (Claude) work**, not a script. Claude reads `sentences.txt` end-to-end and tags six big delete types per `editing-rules/10-content-analysis-methodology.md`:

| Type | Tag |
| --- | --- |
| Pre-show prep | `pre_show` |
| Tech debug | `tech_debug` |
| Off-topic chit-chat | `chit_chat` |
| Privacy | `privacy` |
| Repeated content | `repeated_content` |
| Production talk | `production_talk` |

Two-level output: `blocks` (humans see big chunks) + `sentences` (downstream scripts consume per-sentence).

#### 2.3 Fine cut (word/sentence level) — `fine_analysis.json`

**The most complex step in the pipeline**, "rule layer + LLM layer" hybrid:

```
Rule layer (run_fine_analysis.js)
  · 100 % recall on deterministic patterns
  · leading filler, silence, consecutive same-word stutter, suffix-match stutter,
    in-sentence isolated filler, phrase-level repetition, consecutive filler, restart signal
  · some markings flagged needsReview, sent to LLM layer for review
              ↓
LLM layer (Claude reads 50–80 sentences/batch + rule-layer output)
  · LLM-unique value (rules can't do):
    ★ self_correction (39 % of LLM-unique value)
    · residual_sentence
    · repeated_sentence
    · production_talk
  · confirm/reject rule-layer needsReview items
              ↓
Merge (merge_llm_fine.js)
  · LLM text spans → map back to word-level timestamps
  · dedupe with rule layer
              ↓
Boundary refinement (refine_fine_analysis.js → refine_boundaries.py)
  · For edits tagged _refinePoints, run energy-valley detection
  · Snap cut points to inter-syllable quiet spots
              ↓
Waveform calibration (waveform_trim.py)
  · For short segments (filler/stutter < 2 s), full energy-envelope analysis
  · Calibrate start/end to real acoustic boundaries
```

Why hybrid: pure rules can't reliably catch "self-correction" (semantic judgment); LLM-only is too slow and expensive for the deterministic cases. Rules give recall, LLM adds semantics.

### Stage 3: AI self-review

**Goal**: replace manual sentence-by-sentence review. After 5a + 5b but before generating the review UI, run Claude one more time. **Not full re-detection** — "side-by-side check the existing markings, find misses and FPs".

3-round strategy:
1. **Rough-cut review**: in 5a's keep paragraphs, look for missed `production_talk` / chit-chat
2. **Fine-cut review** (core): on 5a's keep sentences, focus on 5b's historically high miss rate types (self_correction 50 %, stutter 29 %, single-char pronoun "我我", "他他", …)
3. **Cross-validation**: in 5b's existing edits, find FPs (emphasis vs slip, numbers / proper terms wrongly flagged)

Output `review_agent_catches.json`, merged into the review UI's `extraFineEdits`. Confidence < 0.7 entries are recorded but not adopted.

### Stage 4: user review

#### 4.1 Generate `review_enhanced.html`

`generate_review_enhanced.js` injects these into the template:
- `subtitles_words.json` — word timestamps
- `sentences.txt` — sentence split
- `semantic_deep_analysis.json` — paragraph-level
- `fine_analysis.json` — word/sentence-level
- `audio_seekable.mp3` — **must be CBR**; VBR drifts seek in the browser

Page features:
- toggle whole sentence (checkbox)
- toggle AI fine cut (click strike-through text)
- manual half-sentence delete (select text → Delete/Backspace, or floating toolbar)
- fix speaker (dropdown)
- click sentence → seek
- Ctrl+Z undo
- auto-save to localStorage

#### 4.2 Dynamic skip player (no pre-cut file)

Plays the source audio and skips marked deletes in real time. Every edit is live.

Key implementation:
- `getSkipRanges()` computes skip ranges dynamically from current state, with adaptive lookahead per range
- `skipIfNeeded()` uses **pause → seek → play** (`audio.muted` leaks the buffered next ms; Web Audio API is blocked by `file://` CORS)
- Inter-sentence gap large (~1 s) → lookahead 300 ms; in-sentence gap small (~0 ms) → 50 ms
- Tight gaps (<200 ms) nudge the range start back 200 ms to defeat JS timer drift

#### 4.3 Export + feedback learning

Two export buttons:
- **"Export cut" (green) → `delete_segments_edited.json`**, fed to `cut_audio.py`
- **"Export AI feedback" (blue) → `ai_feedback_*.json`**, fed to feedback learning

Feedback routing (**important**):

| Feedback nature | Where it goes |
| --- | --- |
| LLM-missed specific pattern (e.g. new self_correction sub-pattern) | `cut/editing-rules/llm-fine-edit-prompt-template.md` (**manual** integration) |
| Personal aggressiveness preference | `cut/user-prefs/<userId>/editing_rules/` |
| Personal type-retention preference (e.g. "I prefer keeping 嗯") | `cut/user-prefs/<userId>/editing_rules/` |

Only adjustments with confidence ≥ 0.5 are adopted.

### Stage 5: cut execution

**5.1 + 5.2 run consecutively, reported once.** Don't pause to show intermediate output.

```bash
# 5.1 one-shot cut (using audio_original.*, NOT audio.mp3)
python3 cut_audio.py output.mp3 audio_original.<ext> \
  delete_segments_edited.json \
  --speakers-json subtitles_words.json \
  --no-fade            # mandatory; default fade eats short syllables

# 5.2 final silence trim
python3 trim_silences.py output.mp3   # default: >0.8 s trimmed to 0.6 s
```

Key designs of `cut_audio.py`:
- WAV intermediate (sample-accurate; no MP3 frame-boundary slop)
- 3 ms micro-fade (`--no-fade` mode) — defeats clicks; doesn't eat short syllables
- Speaker-volume compensation (`aselect` + `volumedetect`, max +6 dB)
- concat demuxer for fast assembly

**Don't roll your own ffmpeg replacement for `cut_audio.py`** — it has solved a long list of detail issues (pitfalls 10–17).

### Stage 6: AI QA

`preferences.yaml`'s `auto_qa_enabled` flag decides auto-trigger of `/podcast-edit-qa`.

Three layers:
- **Phase A — data layer**: `audit_cut.js` checks `delete_segments` correctness (restored sentences mistakenly cut, manual deletes not applied, silence at cut points, large deletions)
- **Phase B — signal layer**: `signal_analysis.py` (librosa) detects energy spikes, unnatural silence, waveform discontinuity, spectral jumps, breath truncation; optional Gemini AI listening
- **Phase C — semantic layer** (optional): re-transcribe the cut audio → LCS-align with the source → catch residual issues / semantic breaks

QA's *systemic* failure modes (not individual misjudgements) feed back to update `cut/editing-rules/`.

### Stage 7: polish

`preferences.yaml`'s `workflow_automation.auto_post_production` decides auto-trigger of `/podcast-edit-polish`:
- highlight clips → 3–4-clip teaser
- intro music (default 15 s, 2 s in / 3 s out)
- outro music
- chapter timestamps (YouTube / podcast platforms)
- 3–5 title suggestions
- show notes

First time asks the user for music style, timestamp format, title style — saves to `cut/user-prefs/<userId>/post_production.yaml`.

### Stage 8: final user review

`generate_review_final.js` produces `review_final.html`:
- embedded audio player (the cut output)
- AI QA issues sorted by severity, clickable timestamps
- before/after preview around each cut
- "All good" / "Needs re-cut" buttons

| User picks | Then |
| --- | --- |
| Needs re-cut | Loop back to Stage 4 |
| All good | `user_manager.appendEpisode()` writes `episode_history.json`; if there's feedback, `capture_final_feedback.js` distributes to global or personal |

---

## 4. Two key architectural concepts

### 4.1 Two-tier learning

| Tier | Location | Scope | When |
| --- | --- | --- | --- |
| Global methodology | `cut/editing-rules/*.md` | every user | QA reveals a methodology gap (detection logic has a bug) |
| Personal preference | `cut/user-prefs/<userId>/editing_rules/` | one user | user review corrections (restore / add deletes) |

**How to decide**:
- "I don't want this filler deleted" → personal preference
- "This stutter pattern should be detected for everyone" → global methodology

Mixing them either pollutes global rules with personal preference, or buries a general bug as a personal preference.

### 4.2 Three feedback capture points

| Point | Stage | Input | Handler |
| --- | --- | --- | --- |
| FP1 | Stage 4 user review | `ai_feedback_*.json` or editState diff | `analyze_feedback.js` → `apply_feedback_to_rules.js` |
| FP2 | Stage 6 AI QA | QA report + user corrections | systemic failures → update `cut/editing-rules/` |
| FP3 | Stage 8 final review | `final_review_feedback.json` | `capture_final_feedback.js` → routes to global or personal |

---

## 5. Key data flow

```
audio.mp3
    ↓ (FunASR)
aliyun_funasr_transcription.json  +  speaker_mapping.json
    ↓ (generate_subtitles_from_aliyun.js)
subtitles_words.json   ← ★ source of truth for all timestamps
    ↓ (generate_sentences.js)
sentences.txt
    ↓ ┌─────────────────────────────────┐
      │ Claude (rough cut) → semantic_deep_analysis.json
      │ run_fine_analysis.js (rule layer) → fine_analysis_rules.json
      │ Claude (LLM layer) → fine_analysis_llm.json
      │ merge_llm_fine.js → fine_analysis.json
      │ refine_fine_analysis.js → boundary refinement
      │ waveform_trim.py → waveform calibration
      │ Claude (self-review) → review_agent_catches.json
      └─────────────────────────────────┘
                    ↓ (generate_review_enhanced.js)
review_enhanced.html   ← user edits in the browser
                    ↓ (user clicks the green button)
delete_segments_edited.json
                    ↓ (cut_audio.py)
3_output/<name>_final_v1.mp3
                    ↓ (trim_silences.py)
3_output/<name>_final_v1_trimmed.mp3
                    ↓ (/podcast-edit-qa)
audit_report.json + qa_signal_report.json [+ qa_semantic_report.json]
                    ↓ (/podcast-edit-polish)
intro teaser + chapter timestamps + titles + show notes
                    ↓ (generate_review_final.js)
review_final.html → episode_history.json
```

---

## 6. Red lines (curated from the 41 pitfalls)

1. **Always cut from `audio_original.*`, never `audio.mp3`**
   `audio.mp3` is 16 kHz / 24 kbps ASR-downsampled (8 kHz frequency cap). Cutting it tanks the final's audio quality (pitfall 41).

2. **`cut_audio.py` MUST take `--no-fade`**
   Default 0.3 s adaptive fade eats short syllables (pitfall 27).

3. **In-sentence delete range must extend to `[prev_word.end, next_word.start]`**
   ASR has onset leak; cutting tight to the word leaves residual sound (pitfalls 25 / 26 / 39).

4. **Never regenerate the review HTML over a user-reviewed file**
   localStorage holds the user's manual edits; regenerating wipes them. To regenerate, `cp review_enhanced.html review_enhanced.html.bak` first and confirm with the user.

5. **Word indices index into `actual_words`, not the raw `words` array**
   `subtitles_words.json` includes gap and speaker-label entries; `sentences.txt` indices are post-filter. Mixing them up offsets every sentence's startTime by ~25 s (pitfall 1).

6. **Review HTML's audio MUST be CBR**
   `audio_seekable.mp3` is built in step 2.1 with `ffmpeg -c:a libmp3lame -b:a 64k -write_xing 1`. VBR drifts in the back half by seconds (pitfall 3).

7. **Detection methodology gaps don't go in personal preferences**
   General problems (LLM-missed patterns) belong in `cut/editing-rules/llm-fine-edit-prompt-template.md`. Only "this user wants X" goes in `editing_rules/` (pitfall 31).

The full 41-item pitfall list lives in `cut/SKILL.md` "Pitfalls" section.

---

## 7. User / system flow recap

```
User says: "cut this podcast, 3 speakers: Alice, Bob, Carol"
          ↓
Stage 1 → load or create user-prefs/<userId>/
Stage 2 → Aliyun transcribe + Claude rough cut + rule+LLM fine cut + boundary refinement
Stage 3 → Claude self-review finds misses
Stage 4 → user reviews review_enhanced.html in the browser ← only mandatory human gate
Stage 5 → cut_audio.py produces the final
Stage 6 → 3-layer QA
Stage 7 → polish (music, chapters, title)
Stage 8 → user finalizes via review_final.html
          ↓
Output: 3_output/<name>_final_v1_trimmed.mp3 + chapter timestamps + title suggestions + show notes
```

**Design philosophy**: hand mechanical work to scripts (rule-layer detection, silence trim, volume alignment, waveform calibration), hand semantic judgment to the LLM (rough cut, self_correction, self-review), hand the unautomatable final calls to humans (Stages 4 and 8 — two browser review gates). **Feedback always writes to files, never to memory** — survives machine, account, and user changes.
