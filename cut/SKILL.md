---
name: podcast-edit:cut
description: Podcast audio transcription and AI semantic analysis. Backed by Aliyun FunASR, produces an enhanced review UI and an automatic cut. Triggers — cut podcast, edit podcast, edit audio, 剪播客, 處理播客.
---

<!--
input: audio file (*.mp3, *.wav, *.m4a) + speaker info
output: subtitles_words.json, semantic_deep_analysis.json, review_enhanced.html (dynamic player), final mp3
pos: Aliyun transcription + AI deep understanding + enhanced review + auto cut

Architecture guardian: when this file is modified, also update:
1. ../README.md skill table
2. /CLAUDE.md routing table
-->

# Cut (剪播客) v6

> Aliyun FunASR transcription + Claude semantic analysis + enhanced web review + auto cut + per-user preference learning.

## Quick start

```
User: cut this podcast, three speakers: Alice, Bob, Carol
User: edit this audio /path/to/audio.mp3, two hosts
User: cut this recording, speakers: host, guest
```

**Required inputs**:
1. Audio file path
2. **Speaker count** (2, 3, …) — **must come from the user; do NOT guess**
3. Speaker names

**⚠️ Pre-flight checks**:
- If the user hasn't given the speaker count, **ask**.
- Do not infer or guess the count.
- Wrong speaker count tanks accuracy from 98.8 % to unusable.

**🚫 CRITICAL: never regenerate `review_enhanced.html` over a user-reviewed file!**
- Manual edits live in the page's JS / localStorage state. Regenerating wipes them.
- If you must update template logic, only edit `templates/review_enhanced.html` and let the user refresh or regenerate themselves.
- If a regenerate is truly required, **back up first** (`cp review_enhanced.html review_enhanced.html.bak`), tell the user manual edits will be lost, and wait for explicit confirmation.

## Output directory layout

```
output/
└── YYYY-MM-DD_<audio-name>/
    └── cut/
        ├── 1_transcript/                       # raw transcription data
        │   ├── audio.mp3                       # source (transcription input)
        │   ├── audio_seekable.mp3              # CBR re-encode (review page; precise seek)
        │   ├── audio_url.txt                   # uploaded URL
        │   ├── aliyun_funasr_transcription.json
        │   ├── speaker_mapping.json
        │   └── subtitles_words.json            # word-level (SOURCE OF TRUTH)
        ├── 2_analysis/                         # Claude analysis data
        │   ├── sentences.txt                   # sentence split
        │   ├── semantic_deep_analysis.json     # AI rough cut (paragraph-level)
        │   ├── fine_analysis.json              # AI fine cut (word/sentence-level)
        │   ├── ANALYSIS_COMPLETE.md            # analysis report
        │   ├── selected_default.json           # default delete suggestions
        │   └── delete_segments.json            # time ranges (rough + fine merged)
        ├── 3_output/                           # final audio
        │   ├── <podcast>_final_v1.mp3
        │   ├── <podcast>_final_v2.mp3
        │   └── …
        ├── review_enhanced.html                # review UI
        └── server.log                          # optional
```

## Pipeline (v6, eight stages)

```
┌─────────────────────────────────────────────────────────────────┐
│ User preferences (persistent)                                    │
│ editing-rules/ (shared) + user-prefs/<userId>/ (personal)        │
│ ← feedback writeback at stages 4, 6, 8                          │
└─────────────┬──────────────────────────┬──────────────────┬─────┘
              ↓                          ↓                  ↓
Stage 1: user start
    → new user: sample learning OR onboarding questionnaire (1–2 turns)
    → existing user: "preferences loaded — what audio? how many speakers?"
    ↓
Stage 2: cut analysis
    2.1 infrastructure: dirs → prep audio → upload → transcribe → split sentences
    2.2 rough cut (paragraph level): semantic_deep_analysis.json
    2.3 fine cut (word/sentence level): fine_analysis.json
    ↓
Stage 3: AI self-review
    → automatically reviews rough + fine markings; catches missed edits
    → outputs supplementary catches → merge → re-merge
    ↓
Stage 4: user review
    → generate review_enhanced.html
    → user reviews + edits + exports
    → feedback learning ← ai_feedback → updates user prefs
    ↓
Stage 5: cut execution
    → merge delete suggestions + fine cuts
    → cut_audio.py one-shot final cut
    → trim_silences.py final silence trim
    ↓
Stage 6: AI QA → /podcast-edit-qa
    → Phase A: data layer (delete_segments correctness)
    → Phase B: signal layer (energy/spectrum/silence + optional Gemini AI)
    → Phase C: semantic layer (re-transcribe, residual detection) 🆕v6
    → feedback learning ← QA issues → updates editing-rules/
    ↓
Stage 7: post-production → /podcast-edit-polish
    → highlight teaser + intro/outro music + chapter timestamps + title
    ↓
Stage 8: final user review 🆕v6
    → generate review_final.html (QA results + clickable timestamps)
    → user listens to flagged points → "all good" / "needs re-cut"
    → feedback learning ← final review → updates user prefs
```

---

## Execution

### Stage 1: user start

**⚠️ This is the very first step. When the user says "cut this podcast", this runs first.**

**Core principle: actively guide. Don't wait for the user to guess what's needed. Walk them through like a form.**

#### First question: who are you?

The moment a cut request arrives, **immediately** ask:

> "Are you an existing user or new?"

```bash
cd "$SKILL_DIR/cut"

# List known users
node scripts/user_manager.js list
```

- **Existing user** → ask username → load prefs → jump to "everyday use"
- **New user** → ask username (English/pinyin) → create user → enter onboarding

```bash
# Check whether a user exists
node scripts/user_manager.js check <userId>

# Create a new user (clones from default/)
node scripts/user_manager.js create <userId>
```

---

#### New-user onboarding (1–2 turns)

**Path A — sample learning (most accurate)**
1. Ask: "Do you have a previously-edited audio? (the unedited version + your final cut)"
2. If yes → transcribe both versions → run sample learning:
   ```bash
   python3 scripts/analyze_editing_samples.py --before-transcript … --after-transcript … --output learned_patterns.json
   node scripts/generate_rule_overrides.js learned_patterns.json <userId>
   ```
3. Show the extracted preferences; user confirms → save into `editing_rules/`.
4. Optional: ask for the podcast link (`node scripts/parse_podcast_link.js <url> <userId>`) to flesh out the podcast profile.

**Path B — no prior sample**
A single structured ask in one message:
> "Set your editing preferences:
> 1. Podcast type? (audience, purpose)
> 2. Target duration and aggressiveness? conservative (10–20 % deleted) / moderate (20–35 %) / aggressive (35–50 %)
> 3. Anything special? (e.g. keep all fillers, aggressive stutter removal, …)"

Unanswered items use conservative defaults. Polish-skill prefs are deferred to first polish-skill use.

---

#### Returning user

1. Read the user's `preferences.yaml` + `editing_rules/`.
2. One-line confirm: "Loaded your preferences. What are we cutting today, and how many speakers?"
3. After receiving the audio, `ffprobe` the duration; compare with target. Tell the user immediately if there's a big gap.
4. Confirm if anything special this time (apply temporary overrides if so).

#### Preference management

```bash
# User management
node scripts/user_manager.js list              # list users
node scripts/user_manager.js create <userId>    # create
node scripts/user_manager.js prefs <userId>     # show prefs
node scripts/user_manager.js rules <userId>     # show editing rules

# Direct edit
open user-prefs/<userId>/preferences.yaml

# Or tell Claude:
# "Update my default duration to 90 minutes"
# "I prefer aggressive trimming now"
```

**Preferences directory**: `$SKILL_DIR/cut/user-prefs/<userId>/`

---

### Stage 2: cut analysis

#### 2.1 Infrastructure

##### Create the output directories

```bash
# Variables (adjust to your audio) — every later step depends on these
AUDIO_PATH="/path/to/podcast.mp3"
AUDIO_NAME=$(basename "$AUDIO_PATH" | sed 's/\.[^.]*$//')
DATE=$(date +%Y-%m-%d)
SKILL_DIR="${SKILL_DIR:-$HOME/podcast-edit-skill}"
BASE_DIR="$SKILL_DIR/output/${DATE}_${AUDIO_NAME}/cut"

# Subdirectories
mkdir -p "$BASE_DIR/1_transcript" "$BASE_DIR/2_analysis" "$BASE_DIR/3_output"
```

##### Prepare the audio

```bash
# 1) Keep the original high-quality audio (used for cutting; don't transcode)
AUDIO_EXT="${AUDIO_PATH##*.}"
cp "$AUDIO_PATH" "$BASE_DIR/1_transcript/audio_original.$AUDIO_EXT"

# 2) Downsample to 16 kHz mono MP3 (FunASR likes low sample rates)
ffmpeg -i "file:$AUDIO_PATH" -vn -acodec libmp3lame -ar 16000 -ac 1 -y "$BASE_DIR/1_transcript/audio.mp3"

# 3) ⚠️ MANDATORY: re-encode as CBR MP3 for the review page (precise seek)
# VBR MP3 seeks drift in the browser; clicks in the back half play offsets seconds late.
ffmpeg -i "$BASE_DIR/1_transcript/audio.mp3" -c:a libmp3lame -b:a 64k -write_xing 1 -y "$BASE_DIR/1_transcript/audio_seekable.mp3"

echo "✅ audio prepared:"
echo "   audio_original.$AUDIO_EXT  (cut-time, original quality)"
echo "   audio.mp3                  (transcription, 16 kHz mono)"
echo "   audio_seekable.mp3         (review page, CBR)"
```

> **At cut time (Stage 5) you MUST use `audio_original.*`**, not `audio.mp3`. The latter is 16 kHz downsampled — cutting it produces low-quality output.

> **The review page MUST use `audio_seekable.mp3`**. In Step 6, pass `--audio 1_transcript/audio_seekable.mp3` to the HTML generator.

##### Upload to get a public URL

```bash
# Upload to uguu.se (24 h retention)
UPLOAD_RESPONSE=$(curl -s -F "files[]=@$BASE_DIR/1_transcript/audio.mp3" "https://uguu.se/upload?output=text")

echo "✅ uploaded"
echo "   URL: $UPLOAD_RESPONSE"

echo "$UPLOAD_RESPONSE" > "$BASE_DIR/1_transcript/audio_url.txt"
AUDIO_URL="$UPLOAD_RESPONSE"
```

Notes:
- uguu.se files self-delete after 24h.
- For longer retention, use Aliyun OSS or another cloud store.
- The URL must be publicly reachable.

##### Transcribe + speaker mapping → `subtitles_words.json`

This step covers: API transcribe → identify speakers → produce word-level transcript.

```bash
SPEAKER_COUNT=3  # ⚠️ MUST come from the user (2, 3, …)

# 3a. Call Aliyun (~3 min)
# API key auto-loads from .env; no manual export needed.
cd "$BASE_DIR/1_transcript"
bash "$SKILL_DIR/cut/scripts/aliyun_funasr_transcribe.sh" "$AUDIO_URL" "$SPEAKER_COUNT"
# → aliyun_funasr_transcription.json

# 3b. Identify speakers (look at first 20 sentences)
node "$SKILL_DIR/cut/scripts/identify_speakers.js" "$BASE_DIR/1_transcript/aliyun_funasr_transcription.json"
# Sample output:
# 1. [Speaker 0] 0.2s - I'm the host Alice
# 2. [Speaker 1] 29.4s - Hello everyone, I'm Bob

# 3c. Build the mapping
cat > "$BASE_DIR/1_transcript/speaker_mapping.json" << 'EOF'
{
  "0": "Alice",
  "1": "Bob"
}
EOF

# 3d. Generate the word-level transcript (the canonical output)
cd "$BASE_DIR/1_transcript"
node "$SKILL_DIR/cut/scripts/generate_subtitles_from_aliyun.js" \
  aliyun_funasr_transcription.json \
  speaker_mapping.json
# → subtitles_words.json ⭐ THE source of truth

echo "✅ Step 3 done: subtitles_words.json generated"
```

Key points:
- `SPEAKER_COUNT` must be correct (98.8 % accuracy depends on it)
- `identify_speakers.js` is a quick aid for figuring out who's who
- `subtitles_words.json` is the foundation for everything downstream

##### Sentence split (`sentences.txt`)

Build sentence-level text from the word-level transcript for downstream analysis.

```bash
cd "$BASE_DIR/2_analysis"
node "$SKILL_DIR/cut/scripts/generate_sentences.js" "$BASE_DIR/1_transcript/subtitles_words.json"

# Output: sentences.txt
# Format: sentenceIdx|wordIdxRange|speaker|text
```

Sample:
```
0|0-429|Alice|哈喽大家好欢迎来到今天的五点一刻...
1|431-607|Bob|啊对的嗯啊对现在已经26年了...
2|609-612|Bob|啊对的嗯。
```

---

#### 2.2 Rough cut (paragraph level)

> Detailed methodology: `editing-rules/10-content-analysis-methodology.md`

Two-tier analysis: paragraph-level scan to mark big delete blocks, then sentence-level boundary tweak.

**🆕v5 user-level rule loading**:

Load preferences and rule overrides before analyzing:
```javascript
const UserManager = require('./scripts/user_manager');
const prefs = UserManager.loadPreferences(userId);
const rules = UserManager.loadEditingRules(userId);
// rules.user_overrides['content_analysis'] — per-user aggressiveness override (if any)
```
- `prefs.content_analysis.detect_types` flags decide which delete types are enabled
- `prefs.duration.aggressiveness` or `rules.user_overrides.content_analysis.aggressiveness` decides how aggressively
- **Priority**: editing_rules override > preferences intent > base rule defaults

**Flow**:
1. Read `sentences.txt` end-to-end; partition by topic.
2. Per `prefs.detect_types`, pick which delete types are enabled (see table below).
3. Compute block durations; compare with target duration.
4. If we still need to trim, mark low-density paragraphs as `delete` per the user's aggressiveness.
5. **Quality-optimization scan (always run, even if there's no duration gap)**: per `10-content-analysis-methodology.md`'s "quality optimization analysis", flag verbose, over-expanded, low-density, weakly-related sections as `suggest_delete`.
6. Tweak the boundary cuts of every delete block.
7. Emit `semantic_deep_analysis.json`.

**Six delete types**:

| Type | Tag | Description |
| --- | --- | --- |
| Pre-show | `pre_show` | Anything before the formal open |
| Tech debug | `tech_debug` | Equipment trouble, recording interruptions, audio checks |
| Chit-chat | `chit_chat` | Off-topic small talk (waiting-room banter, etc.) |
| Privacy | `privacy` | Speaker explicitly asked to delete; sensitive personal info |
| Repeated | `repeated_content` | Same story told twice (keep the concise version) |
| Production talk | `production_talk` | Discussing what to keep / cut while recording |

**Output file**: `semantic_deep_analysis.json`

**Format** (two-level):
```json
{
  "version": "5.0",
  "analysisType": "two_level",
  "totalDuration": "2:08:06 (128min)",
  "targetDuration": "90min",
  "blocks": [
    {
      "id": 1,
      "range": [0, 19],
      "type": "pre_show",
      "reason": "Pre-show prep: noise check, who-talks-first, opening doc",
      "duration": "1:06"
    }
  ],
  "sentences": [
    { "sentenceIdx": 0, "speaker": "Carol", "action": "delete", "blockId": 1, "type": "pre_show" },
    { "sentenceIdx": 20, "speaker": "Alice", "action": "keep" }
  ],
  "summary": {
    "totalSentences": 1404,
    "deleteSentences": 227,
    "deleteBlocks": 13,
    "totalDeleteDuration": "14:23",
    "deleteRatio": "16.2%"
  }
}
```

**Key design**:
- `blocks` — paragraph-level for human review (what big chunks were cut)
- `sentences` — sentence-level for downstream consumers
- Delete sentences have `action: "delete"`; kept sentences have `action: "keep"`

---

#### 2.3 Fine cut (word / sentence level)

> Base rules: `editing-rules/1-9.md` (shared)
> User overrides: `user-prefs/<userId>/editing_rules/` (per-user)

Stage 5 deletes big blocks (content level); stage 5b deletes verbal tics and stutters (word/sentence level). Both are merged before the review UI.

**🆕v5 rule merging**:

```
Final rules = base rules (editing-rules/) + user overrides (editing_rules/)
```
- Base rules (`editing-rules/1-9.md`): detection methodology and default thresholds shared by everyone
- User overrides (`editing_rules/*.yaml`): per-user parameters from sample learning or feedback loop
  - `filler_words.yaml` — per-filler delete rate (e.g. 嗯 85 %, 啊 55 %)
  - `silence.yaml` — custom silence threshold (e.g. 2.5 s)
  - `stutter.yaml` — extra stutter patterns
- When a user override exists, it wins.

**Subject of analysis**: sentences marked `keep` in step 5 (already-deleted ones aren't re-analyzed).

**Detection priority** (rules: `editing-rules/README.md`):

| Priority | Type | Rule file | Layer | Notes |
| --- | --- | --- | --- | --- |
| 0 | Leading filler | 2-filler-detection | rules ✅ | "嗯，"/"对，"/"啊，" 100 % recall |
| 1 | Long silence | 3-silence-handling | rules ✅ | >0.8 s cap, >2 s suggest delete |
| 2 | Stutter | 5-stutter | rules ✅ | consecutive same word + suffix-match + in-sentence filler |
| 3 | In-sentence repeat | 6-in-sentence-repetition | rules ✅ | phrase-level "可以去可以去" |
| 4 | Consecutive filler | 7-consecutive-filler | rules ✅ | "嗯啊", "呃啊" |
| 5 | Restart signal | 8-self-correction | rules ✅ | "等一下"/"重来" + repetition |
| 6 | Self-correction | 8-self-correction | **LLM ★** | said-then-corrected, 11 sub-patterns (highest LLM-only value) |
| 7 | Residual sentence | 9-residual-sentence | LLM | half-sentence interrupted, delete the whole thing |
| 8 | Repeated sentence | 4-repeated-sentence | LLM | adjacent sentences share ≥5 chars at start; delete the shorter |
| 9 | Production talk | — | LLM | meta-conversation about recording itself |

**Core principles** (`editing-rules/1-core-principles.md`):
- **Delete the earlier, keep the later**: the second iteration is usually more complete.
- **Podcast-specific**: keep thinking pauses, keep response timing, keep some fillers.

**Flow (v6.1, rules-first)**:

```
Step 5b = rule layer (high recall) → LLM layer (semantic + rule review) → merge

Rule layer (run_fine_analysis.js → fine_analysis_rules.json):
  - leading filler (filler_start) — 100 % recall ✅
  - silence detection — needs audio timestamps
  - consecutive same-word stutter — "我我", "这个这个"
  - suffix-match stutter — "在这个"+"这个"
  - in-sentence isolated filler — 啊/呃/额/对/哦
  - phrase-level in-sentence repeat — "可以去可以去"
  - consecutive filler — "嗯啊", "呃啊"
  - restart signal — "等一下"/"重来" + repetition
  ⚠️ Some edits flagged needsReview=true → LLM layer reviews

LLM layer (Claude → fine_analysis_llm.json):
  ★ self-correction — LLM-unique value, ~39 % of unique misses
  - residual sentence / pure-filler sentence / repeated sentence (semantic)
  - production_talk
  - rule-layer needsReview review (confirm/reject)
  - rule-layer fill-in (only when ASR tokenization caused a miss)

Merge (merge_llm_fine.js → fine_analysis.json):
  LLM text spans → map back to word-level timestamps → dedupe with rule layer
  rules_review rejected items removed from final
```

1. **Rule layer**: run `run_fine_analysis.js` → `fine_analysis_rules.json`.
2. **LLM layer**: Claude reads `sentences.txt` in batches (**50–80 sentences per batch**) + `fine_analysis_rules.json` → `fine_analysis_llm.json`.
   - **⚠️ Read `editing-rules/llm-fine-edit-prompt-template.md` first** and follow its checklist sentence-by-sentence.
   - Mindset: a "proofreader" reading every word, not a "reader" understanding the gist.
   - **Top priority: self-correction** (`self_correction`, 11 sub-patterns, ~39 % of LLM-only value).
   - Rule layer already handles fillers / stutters / phrase repeats; LLM doesn't redo those.
3. **Merge**: run `merge_llm_fine.js` → `fine_analysis.json` (final merged + deduped).
4. **Boundary refinement**: run `refine_fine_analysis.js` → waveform onset detection on tight boundaries.
   ```bash
   node "$SKILL_DIR/cut/scripts/refine_fine_analysis.js" \
     --analysis-dir "$BASE_DIR/2_analysis" \
     --audio "$BASE_DIR/1_transcript/audio_seekable.mp3"
   ```
   - Auto-scans `fine_analysis.json` for edits with `_refinePoints`.
   - Calls `refine_boundaries.py` to find energy valleys (inter-syllable gaps) near the cut points.
   - Refined timestamps overwrite `fine_analysis.json`; original backed up to `fine_analysis_pre_refine.json`.

**LLM-layer execution notes**:
- Batch size: 50–80 sentences (not 150) — prevents attention dilution.
- Per-sentence checklist: self-correction ★, residual sentence, pure-filler, production_talk, rule fill-in, post-delete fluency self-check.
- Confirm/reject decisions on rule-layer needsReview items.
- Output `scan_summary` with per-type counts for self-audit.

**LLM-layer output format**:
```json
{
  "batch_range": [0, 59],
  "edits": [
    {"s": 27, "text": "嗯，", "type": "filler_start", "reason": "leading filler 嗯", "beforeText": "嗯，我觉得挺重要的。", "afterText": "我觉得挺重要的。"},
    {"s": 96, "text": "我，因为我，", "type": "self_correction", "reason": "self-correction: half-restart", "beforeText": "我，因为我，infj是内向的。", "afterText": "infj是内向的。"}
  ],
  "scan_summary": {"total_sentences": 60, "sentences_with_edits": 8}
}
```

**Output file**: `fine_analysis.json`

```json
{
  "edits": [
    {
      "sentenceIdx": 2,
      "type": "stutter",
      "rule": "5-stutter",
      "wordRange": [25, 26],
      "deleteText": "那那那次",
      "keepText": "那次",
      "reason": "consecutive repetition; keep the last"
    },
    {
      "sentenceIdx": 85,
      "type": "silence",
      "rule": "3-silence-handling",
      "wordRange": [915, 915],
      "duration": 3.2,
      "reason": "silence 3.2 s exceeds 2 s threshold"
    }
  ],
  "summary": {
    "totalEdits": 47,
    "byType": {
      "silence": 12,
      "residual_sentence": 3,
      "repeated_sentence": 5,
      "in_sentence_repeat": 8,
      "stutter": 7,
      "self_correction": 4,
      "consecutive_filler": 6,
      "single_filler": 2
    },
    "estimatedTimeSaved": "2:30"
  }
}
```

**Relationship to step 5**:
- Step 5 produces `semantic_deep_analysis.json` (paragraph level, big block deletes).
- Step 5b produces `fine_analysis.json` (word/sentence level, verbal tic deletes).
- Both are independently produced and merged in the step 6 review UI; the user manually edits and exports `delete_segments_edited.json`.

---

### Stage 3: AI self-review

**Goal**: automatically audit the rough (5a) and fine (5b) markings to catch missed edits. Replaces manual sentence-by-sentence review.

**When**: after step 5b completes, before the review UI is generated. That way the review page contains all markings up front and the user reviews once.

**Why we need it**:
- 5b LLM-layer self-correction miss rate is ~50 % (meeting_02 data: 14/28 missed).
- Single-character pronoun stutters (我我 / 他他) fall between the rule layer and LLM layer.
- 5a rough cut may miss `production_talk` (recording chatter / opening transitions).
- Manual review of 500+ sentences is slow.

**Design**:
- Independent of the 5b LLM layer (different prompt, different lens).
- Builds on the existing 5a + 5b markings; focuses on finding misses.
- Outputs `review_agent_catches.json`, merged into the review UI's `extraFineEdits`.

**Inputs / outputs**:

```bash
# Inputs:
# - sentences.txt (full text)
# - semantic_deep_analysis.json (5a markings)
# - fine_analysis.json (5b merged: rule + LLM)
# - subtitles_words.json (word timestamps)

# Outputs:
# - review_agent_catches.json (supplementary catches)
```

**Review strategy (3 rounds)**:

**Round 1: rough-cut review (5a quality)**
- For each kept paragraph, check for missed `production_talk` / chit-chat.
- Check delete-block boundaries: any cuts mid-sentence? Any swallowed value?
- Batch by 5a's `blocks` structure.

**Round 2: fine-cut review (5b quality)** — the core round
- For all kept sentences, re-scan against the same 9-item checklist as 5b LLM.
- **Different focus**: not full re-detection — **find what 5b missed**.
- Pay special attention to types with the highest miss rate historically:
  1. self_correction (50 % miss rate): same-prefix expansion, stumble-restart, particle-end false start
  2. stutter (29 %): single-char pronoun repeats (我我/他他/它它), extreme repeats (≥3×)
  3. consecutive_filler: continuous "这个这个这个"
  4. production_talk: opening transitions, recording-time interactions
- Batch: 50–80 sentences (matches 5b).

**Round 3: cross-validation**
- Check whether 5b's existing edits are mismarked (false positive).
- Special focus: emphasis vs slip-of-tongue, numbers/proper terms mistakenly flagged.

**Output format**:
```json
{
  "version": "review_agent_v1",
  "reviewed_at": "2026-02-27T...",
  "catches": [
    {
      "sentenceIdx": 30,
      "type": "self_correction",
      "deleteText": "我",
      "reason": "single-char pronoun repeat 我我; 5b missed",
      "source": "review_agent_round2",
      "confidence": 0.9
    }
  ],
  "false_positives": [
    {
      "sentenceIdx": 85,
      "existingEditIdx": 3,
      "reason": "tagged stutter, but actually emphasis repetition",
      "confidence": 0.7
    }
  ],
  "summary": {
    "sentences_reviewed": 400,
    "new_catches": 12,
    "false_positives_found": 2,
    "by_type": { "self_correction": 6, "stutter": 4, "production_talk": 2 }
  }
}
```

**Integration with the review UI**:
- `review_agent_catches.json` catches → merged into `fine_analysis.json` as `extraFineEdits`.
- `false_positives` → marked "to be confirmed" in the UI (not auto-cancelled).
- Re-running the HTML generator includes them automatically.

**⚠️ Execution notes**:
- The review agent uses an independent prompt — not the 5b `llm-fine-edit-prompt-template.md`.
- Prompt focus: "find the holes" and "side-by-side check", not "comprehensive detection".
- For 5b's existing edits, default-trust them unless there's a clear false-positive signal.
- Catches with `confidence < 0.7` are recorded but NOT pushed into `extraFineEdits`.

**🔧 v5.1 update: tighter `residual_sentence` judgment**
- **A sentence break is not a residual sentence**: cross-sentence speech ("…broadly expanded my own original." + "…work, because…") is normal speech-flow chunking, should not be deleted. Many user-recovered FPs are this — 5c misjudged the break as a residual.
- **Only flag a sentence as residual when it's actually incomplete and has no follow-up completion** (e.g. restart-then-cut-off with no conclusion).
- **`residual_sentence` confidence threshold raised to 0.9**: cuts FP rate.

---

### Stage 4: user review

#### 4.1 Generate the enhanced review UI

Generate `review_enhanced.html` — visual review + real-time playback + interactive editing.

**Inputs**:
- `subtitles_words.json` — word timestamps (the canonical data source)
- `sentences.txt` — sentence split
- `semantic_deep_analysis.json` — 5a paragraph-level
- `fine_analysis.json` — 5b fine cut
- `1_transcript/audio.mp3` (or `audio_seekable.mp3`) — source audio

**⚠️ HTML generation rules**:

**1. Word-index mapping (must be correct)**:
```
sentences.txt word index → actual_words (skips isGap and isSpeakerLabel)
actual_words = words.filter(w => !w.isGap && !w.isSpeakerLabel)
❌ Wrong: words[wordIdx] (the full array, including gap/label)
✅ Right: actual_words[wordIdx]
```
> This bug once offset every sentence's startTime by ~25 s.

**2. `sentencesData` shape (every sentence must include)**:
```json
{
  "idx": 0,
  "speaker": "Alice",
  "text": "...",
  "startTime": 69.4,
  "endTime": 74.8,
  "timeStr": "1:09",
  "words": [{"t": "everyone", "s": 69.5, "e": 69.7}, ...],
  "isAiDeleted": true,
  "deleteType": "pre_show",
  "fineEdit": {
    "idx": 0, "type": "stutter", "deleteText": "那那",
    "keepText": "那", "reason": "...",
    "ds": 69.5, "de": 69.6
  }
}
```
- `endTime` = next sentence's startTime (last sentence uses last word's end)
- `words` = every word's timestamp (used to map manual-edit text to time)
- `fineEdit.ds` / `fineEdit.de` = precomputed start/end of the fine delete

**3. Fine-cut player — dynamic skip (no pre-cut file)**:
```
Approach: play the original audio; in real time skip every marked delete range
Sources:  currentDeletedSet (sentence) + fineEdits (word) + manualEdits (manual)
Benefit:  every edit is instant — no audio regen needed
```
- `getSkipRanges()` — dynamically computes skip ranges from the current edit state.
  - Each range carries an adaptive lookahead: `[start, end, lookahead]`.
  - Inter-sentence gap large (~1 s) → lookahead 300 ms; in-sentence gap small (~0 ms) → 50 ms.
  - When the gap is tight (<200 ms), nudge the range start back 200 ms to defeat JS timer drift.
  - First range: if start < 5 s, extend to 0 (kill pre-roll noise).
- `skipIfNeeded()` — pause → seek → play (NOT muted; cuts output cleanly).
  - ❌ `audio.muted` (decoder buffer leaks the next ms)
  - ❌ Web Audio `GainNode` (file:// CORS blocks it)
  - `seekTarget = e` (precise landing; no offset, so we don't truncate the next kept word)
  - Use `nextKept.startTime` only when it falls within 0.5 s of `e` (else we'd skip an entire kept sentence).
- `originalToVirtual()` / `virtualToOriginal()` — original ↔ virtual time conversion.
- The progress bar is virtual time (deleted duration subtracted automatically).

**4. Interactive editing**:

| Feature | Action | Notes |
| --- | --- | --- |
| Toggle whole sentence | click the checkbox | flip delete state |
| Toggle AI fine cut | click the strike-through text or orange tag | flip word-level fine cut |
| Manual half-sentence delete | select text → "mark deleted" | floating toolbar; or press Delete/Backspace |
| Fix speaker | click speaker name | dropdown of known speakers + custom input |
| Click to seek | click any sentence row | both players seek |
| Undo | Ctrl+Z | every action undoable |
| Export cut file | green "Export cut" | `delete_segments_edited.json`, ready for `cut_audio.py` |
| Export AI feedback | blue "Export AI feedback" | in the stats panel; for AI accuracy review |

**5. Audio file**:
- **Must be `audio_seekable.mp3`** (Step 1 generated CBR 64k + Xing header).
- VBR MP3 drifts seek progressively; clicks in the back half land seconds late.
- The HTML `<audio>` uses `preload="auto"` for fast seeking.

**6. Content-deletion overview (summary table)**:
- Page top auto-generates a foldable overview of every deletion.
- Each row: topic, type tag, time (clickable to seek), duration, reason.
- Whole-block check/uncheck; footer shows source duration / paragraph deletes / total deletes / projected remaining.

**Generate command**:
```bash
cd "$BASE_DIR/2_analysis"

# Generate the review HTML (audio_seekable.mp3 was made in Step 1)
node "$SKILL_DIR/cut/scripts/generate_review_enhanced.js" \
  --sentences sentences.txt \
  --words "$BASE_DIR/1_transcript/subtitles_words.json" \
  --analysis semantic_deep_analysis.json \
  --fine fine_analysis.json \
  --audio "1_transcript/audio_seekable.mp3" \
  --output "$BASE_DIR/review_enhanced.html" \
  --title "Podcast review (editable)"

# Open it
open "$BASE_DIR/review_enhanced.html"
```

> Template: `templates/review_enhanced.html`. The script injects data into it.
> Without `fine_analysis.json` (e.g. step 5b skipped), the script silently skips fine data.

---

#### 4.2 Review, edit, export

Open the page; review AI suggestions; manually edit; export the cut file.

```bash
open "$BASE_DIR/review_enhanced.html"
```

**Workflow**:
1. Browse all sentences and AI suggestions (delete tags, fine-cut tags).
2. Listen to the result via the fine-cut player.
3. Manually adjust:
   - Check/uncheck: mark or restore deletions.
   - Click a fine tag: enable/disable a word-level cut.
   - Select text → "mark deleted": manual half-sentence delete.
4. Edits **auto-save to localStorage**, surviving page refreshes.
5. Click green "Export cut" → downloads `delete_segments_edited.json`.
6. Copy that file into `2_analysis/`.

**Auto-save**:
- After every edit, save 500 ms later (page-title-keyed, distinguishes podcasts).
- Refresh restores: delete markings, fine toggles, manual edits, AI-miss feedback.
- 30-day cleanup of old entries.
- Top-right shows "✓ saved" briefly.

**Export files**:
- "Export cut" (green) → `delete_segments_edited.json` — every manual change applied; feed to `cut_audio.py`.
- "Export modifications" → `review_modifications_*.json` — backup of edit state (optional).

**Range-precision compensation on export**:
- Range start: at tight gaps, pull back 50 ms (compensate ASR onset lag).
- Range end: snap to next kept sentence's start (matches the player's `seekTarget`).
- First range: extend to 0 if it starts within the first 5 s.

---

#### 4.3 Feedback learning

**User review corrections → analyze → tiered update (universal prompt + personal prefs)**

**Input source** (one of):
- **A**: review page's "Export AI feedback" button (blue) → `ai_feedback_*.json`
- **B**: convert from `delete_segments.json`'s `editState` (see Pitfall 30)

**Flow**:
```bash
cd "$BASE_DIR/3_output"

# 0. If there's no ai_feedback file, convert from editState (see Pitfall 30):
#    missedCatches → missed_catches, manualEdits → added_deletions, userRemoved → removed_deletions

# 1. Analyze feedback
node "$SKILL_DIR/cut/scripts/analyze_feedback.js" \
  ai_feedback_*.json \
  "$BASE_DIR/2_analysis/semantic_deep_analysis.json" \
  "$BASE_DIR/2_analysis/fine_analysis_llm.json" \
  > feedback_analysis_result.json

# 2. Apply personal-pref adjustments to editing_rules (e.g. aggressiveness)
node "$SKILL_DIR/cut/scripts/apply_feedback_to_rules.js" \
  feedback_analysis_result.json \
  <userId>

# 3. ⚠️ Universal-detection improvements MUST be hand-merged into the prompt template (see Pitfall 31)
# - Extract specific missed patterns from missedCatches
# - Add the new pattern + example into editing-rules/llm-fine-edit-prompt-template.md
# - Clean editing_rules of items that don't belong to personal preferences (e.g. missed_count)
```

**Tiered update principle (Pitfall 31)**:

| Feedback nature | Update target | Example |
| --- | --- | --- |
| LLM-missed specific pattern | `editing-rules/llm-fine-edit-prompt-template.md` | new self_correction sub-pattern, stutter examples |
| Personal aggressiveness pref | `user-prefs/<userId>/editing_rules/` | `content_analysis.aggressiveness: aggressive` |
| Personal type-retention pref | `user-prefs/<userId>/editing_rules/` | reduce filler delete aggressiveness |

**Learning rules**:
- confidence ≥ 0.5 → suggestion accepted
- personal-pref entries auto-write into `editing_rules`
- universal-detection improvements need Claude to hand-analyze the pattern and update the prompt template
- recorded in `learning_history.json`

---

#### 4.4 Evaluation metrics

**Auto-compute AI analysis quality metrics; track AI-quality trend.**

**When**: triggered automatically after the user clicks "Export AI feedback" in step 7.

**Logic**:

Based on user corrections in `ai_feedback_*.json`:

```json
{
  "eval_date": "2026-02-28",
  "episode": "episode_name",
  "metrics": {
    "overall": {
      "tp": 24,           // TP = AI suggestions − user-restored FPs (29 − 5)
      "fp": 5,            // user-restored edits
      "fn": 8,            // user-added catches (from missedCatches)
      "precision": 0.828, // TP / (TP + FP) = 24 / 29
      "recall": 0.75      // TP / (TP + FN) = 24 / 32
    },
    "by_type": {
      "self_correction": { "tp": 8, "fp": 1, "fn": 2, "precision": 0.89, "recall": 0.8 },
      "stutter":         { "tp": 7, "fp": 1, "fn": 3, "precision": 0.875, "recall": 0.7 },
      "production_talk": { "tp": 5, "fp": 1, "fn": 1, "precision": 0.833, "recall": 0.833 },
      "filler_word":     { "tp": 4, "fp": 2, "fn": 2, "precision": 0.667, "recall": 0.667 }
    }
  },
  "analysis": {
    "high_confidence_types": ["self_correction", "production_talk"],
    "needs_improvement": ["filler_word"],
    "false_positive_pattern": "sentence-break mistaken for residual (s48/80/88/128/187)"
  }
}
```

**Scripts** (auto-triggered):

```bash
node "$SKILL_DIR/cut/scripts/calculate_eval_metrics.js" \
  ai_feedback_*.json \
  "$BASE_DIR/2_analysis/fine_analysis.json" \
  > eval_metrics_current.json

# Append to history
node "$SKILL_DIR/cut/scripts/append_eval_history.js" \
  eval_metrics_current.json \
  "$BASE_DIR/eval_history.json"
```

**Outputs**:
- `3_output/eval_metrics_current.json`: detailed per-episode metrics
- `eval_history.json` (project root): trend record

```json
// eval_history.json
{
  "episodes": [
    { "episode": "ep1", "date": "2026-02-15", "precision": 0.82,  "recall": 0.75 },
    { "episode": "ep2", "date": "2026-02-28", "precision": 0.828, "recall": 0.75 }
  ],
  "by_type_trends": {
    "self_correction": [0.80, 0.89],
    "stutter": [0.70, 0.875],
    "production_talk": [0.90, 0.833]
  },
  "improvement_notes": [
    "2026-02-28: raised residual_sentence threshold to 0.9 to reduce sentence-break FPs"
  ]
}
```

**Use**:
- 📊 Track AI-quality trend across versions
- 🎯 Identify types that need improvement (e.g. filler_word recall too low)
- 📝 Provide data for prompt tuning (which types FP / FN often)
- 📈 After many episodes, plot Precision@recall curves

**⚠️ Notes**:
- Evaluation assumes user corrections are correct.
- The first episode's metrics become the baseline.
- If feedback isn't exported, that episode is excluded from history.

---

### Stage 5: cut execution

> **Interaction rule**: Stage 5 is 5.1 + 5.2 — execute consecutively, report once. Don't show the user intermediate output of 5.1; go straight into 5.2. After both finish, tell the user the final path and duration stats.

#### 5.1 One-shot final cut

Use FFmpeg. Decode to WAV first to ensure sample-accurate cuts.

```bash
cd "$BASE_DIR/2_analysis"

# Find the original high-quality audio (saved at Stage 1)
ORIGINAL_AUDIO=$(ls "$BASE_DIR/1_transcript/audio_original."* 2>/dev/null | head -1)
if [ -z "$ORIGINAL_AUDIO" ]; then
  echo "⚠️ audio_original.* not found; falling back to audio.mp3 (quality will degrade)"
  ORIGINAL_AUDIO="$BASE_DIR/1_transcript/audio.mp3"
fi

python3 "$SKILL_DIR/cut/scripts/cut_audio.py" \
  "$BASE_DIR/3_output/${AUDIO_NAME}_final_v1.mp3" \
  "$ORIGINAL_AUDIO" \
  delete_segments_edited.json \
  --speakers-json "$BASE_DIR/1_transcript/subtitles_words.json" \
  --no-fade
```

> Pass `--speakers-json` always. The script auto-detects volume difference and skips compensation when < 0.5 dB; no side effect.
> **`--no-fade` is mandatory**: the default adaptive fade (max 0.3 s) eats short syllables. `--no-fade` uses a 3 ms micro-fade instead — defeats clicks without affecting speech.

**Output**:
- `3_output/<podcast>_final_v1.mp3`
- Need adjustments? Go back to step 7, modify, re-export, re-run.

**Cut characteristics**:
- ✅ WAV intermediate (sample-accurate; no MP3 frame-boundary slop)
- ✅ 3 ms micro-fade (`--no-fade`): defeats PCM-discontinuity clicks without affecting speech
  - ⚠️ Don't use the default adaptive fade: max 0.3 s eats short syllables (e.g. a 0.36 s "很久" almost entirely faded)
- ✅ Speaker volume alignment (`--speakers-json`): per-speaker mean loudness, auto-compensate (max +6 dB)
- ✅ Consecutive-deletion grouping (no fragmentation)
- ✅ Re-encode for precise seek
- ✅ Duration savings printed

**⚠️ MUST use `cut_audio.py`**: don't hand-write FFmpeg commands or roll your own cutting logic. See Pitfall 17.

---

#### 5.2 Final silence trim

After the cut, short silences from removed content can merge into long pauses. **Always sweep the final.**

**Why not at delete-segments stage?**
- User manual edits (restore/delete) create new merged gaps.
- `merge_llm_fine.js`'s post-merge gap cleanup is prediction-based, not exact.
- **Just scan the cut audio directly with FFmpeg silencedetect.**

```bash
python3 "$SKILL_DIR/cut/scripts/trim_silences.py" \
  "$BASE_DIR/3_output/${AUDIO_NAME}_final_v1.mp3"
# Defaults: detect >0.8 s silence, trim to 0.6 s
# Output: *_trimmed.mp3

# Custom params:
python3 "$SKILL_DIR/cut/scripts/trim_silences.py" \
  input.mp3 output.mp3 \
  --threshold 0.8 \   # detection threshold
  --target 0.6 \      # target retain duration per silence
  --noise -30          # silencedetect noise threshold dB
```

**Key design**:
- target is 0.2 s below threshold (retain 0.3+0.3=0.6 s) because silencedetect's boundary and the trim point don't perfectly align (Pitfall 24).
- Stand-alone script — independent of delete_segments — runs on any MP3.
- Iterable: tweak params and re-run if the user isn't happy.


### Stage 6: AI QA → /podcast-edit-qa

**Condition**: `preferences.yaml` `workflow_automation.auto_qa_enabled: true`.

After cut completes, auto-trigger the QA skill to check audio-quality issues at cut points.

```bash
# Auto-triggers /podcast-edit-qa
# Inputs: final audio + delete_segments_edited.json
# Outputs: QA report (energy spikes, silence anomalies, spectral jumps)
```

**Flow**:
1. Read `preferences.yaml` and check `auto_qa_enabled`.
2. If on, call `/podcast-edit-qa`.
3. If issues are flagged, present them to the user.
4. User decides whether to go back to step 7 and tweak.

---

### Stage 7: post-production → /podcast-edit-polish

**Condition**: `preferences.yaml` `workflow_automation.auto_post_production` controls auto-trigger.

**First time using polish**:
1. Ask polish preferences (intro music, timestamp format, title style, …).
2. Save to `user-prefs/<userId>/post_production.yaml`.
3. Execute `/podcast-edit-polish`.

**Subsequent uses**:
1. Read `post_production.yaml`.
2. Confirm any adjustments for this episode.
3. Execute `/podcast-edit-polish`.

```bash
# Read polish preferences
node -e "
  const um = require('$SKILL_DIR/cut/scripts/user_manager');
  const pp = um.loadPostProduction('$PODCAST_EDIT_USER');
  console.log(JSON.stringify(pp, null, 2));
"

# Trigger polish skill
# → highlight teaser, intro music, chapter timestamps, titles, show notes
```

---

### Stage 8: final user review 🆕v6

**Generate the final-review page + final confirmation + record episode_history.**

**Flow**:
1. Generate `review_final.html`:
   ```bash
   node "$SKILL_DIR/cut/scripts/generate_review_final.js" \
     --audio "$BASE_DIR/3_output/<podcast>_final_trimmed.mp3" \
     --audit-report "$BASE_DIR/2_analysis/audit_report.json" \
     --signal-report "$BASE_DIR/2_analysis/qa_signal_report.json" \
     --semantic-report "$BASE_DIR/2_analysis/qa_semantic_report.json" \
     --words "$BASE_DIR/1_transcript/subtitles_words.json" \
     --output "$BASE_DIR/review_final.html"
   ```
   The page has:
   - embedded audio player (the cut output)
   - AI QA issues (data / signal / semantic), sorted by severity
   - clickable timestamps → audio auto-seeks
   - context preview around each cut
   - "All good" / "Needs re-cut" buttons
   - stats: source duration, final duration, deletion ratio
2. After interacting, the user exports `final_review_feedback.json`.
3. If "Needs re-cut" markings exist → loop back to Stage 4.
4. If everything passes → record into `episode_history.json`:
   ```bash
   node -e "
     const um = require('$SKILL_DIR/cut/scripts/user_manager');
     um.appendEpisode('$PODCAST_EDIT_USER', {
       audio_file: 'original-filename',
       original_duration_min: 128,
       final_duration_min: 92,
       delete_ratio: '28%',
       content_blocks_deleted: 13,
       fine_edits: 47,
       qa_issues: 0,
       post_production: true
     });
   "
   ```
5. Feedback learning: if there's final feedback, run `capture_final_feedback.js` to update preferences.

---

## Feedback learning (built in, replaces the archived self-evolution skill)

> Lets the agent improve from feedback continuously, with no separate trigger.

### Two-tier learning

| Type | Target | When | Example |
| --- | --- | --- | --- |
| **Global methodology** | `editing-rules/*.md` | a detection-logic gap is found | new stutter pattern, improved silence algorithm |
| **Personal preferences** | `user-prefs/<userId>/editing_rules/` | user review corrections | "I prefer to keep 嗯", lower filler delete rate |

**Key distinction**: user review corrections (restore / add deletes) → personal preferences. Methodology gaps (detection logic has a bug) → global rules.

### Three feedback capture points

| Point | Stage | Input | Handler |
| --- | --- | --- | --- |
| FP1 | Stage 4 (user review) | `ai_feedback_*.json` or editState diff | `analyze_feedback.js` → `apply_feedback_to_rules.js` → updates `editing_rules/` |
| FP2 | Stage 6 (AI QA) | QA report + user corrections | systemic QA failure modes → updates `editing-rules/` |
| FP3 | Stage 8 (final review) | `final_review_feedback.json` | `capture_final_feedback.js` → routes to global or personal |

### Principles

- Feedback **writes into skill files** (`editing-rules/` or `user-prefs/`); **NOT into agent memory**, so it survives across machines and accounts.
- Integrate into the right section of the doc (don't only append at the end).
- Feedback log records the event only, doesn't re-state rules.
- Analyze the issue from context directly — don't ask "what was the problem?".

---

## Configuration

### Aliyun API key

```bash
# Method 1: env var
export DASHSCOPE_API_KEY="sk-your-api-key"

# Method 2: .env
cd "$SKILL_DIR"
cat >> .env << 'EOF'
DASHSCOPE_API_KEY=sk-your-api-key
EOF
```

**Get a key**:
1. Visit https://dashscope.console.aliyun.com/
2. Activate "Model Service Lingji"
3. Create an API key

**Pricing**:
- Charged by audio length
- ~¥X / hour (check Aliyun for the latest rate)

### Speaker count

**How to determine**:
1. Listen to the first 2–3 minutes
2. Or check the show notes
3. Compute: hosts + guests = total

**Examples**:
- Solo: 1
- Two-host: 2
- Interview (2 hosts + 1 guest): 3
- Round-table: actual count

**Important**: wrong count → bad speaker identification.

---

## Data formats

### `aliyun_funasr_transcription.json`

```json
{
  "transcripts": [{
    "sentences": [
      {
        "sentence_id": 1,
        "speaker_id": 0,
        "text": "嗯，哈喽，大家好，我是主播麦雅。",
        "begin_time": 69400,
        "end_time": 74800,
        "words": [
          { "text": "嗯", "begin_time": 69400, "end_time": 69600, "punctuation": "，" }
        ]
      }
    ]
  }]
}
```

### `speaker_mapping.json`

```json
{
  "0": "Alice",
  "1": "Bob",
  "2": "Carol"
}
```

### `subtitles_words.json`

```json
[
  {"text": "[Alice]", "start": 69.4, "end": 69.4, "isGap": false, "isSpeakerLabel": true, "speaker": "Alice"},
  {"text": "大家",     "start": 69.5, "end": 69.7, "isGap": false, "speaker": "Alice"},
  {"text": "",         "start": 70.5, "end": 71.2, "isGap": true}
]
```

---

## Podcast editing tips

Differences from short-form / video voice-over:

1. **Silence threshold**:
   - Video: 0.3–0.5 s
   - Podcast: 1–2 s (preserve natural rhythm)

2. **Filler handling**:
   - Video: aggressive removal
   - Podcast: moderate retention (preserve conversational feel)

3. **Repetition**:
   - Video: strict removal
   - Podcast: only obvious repeats; keep light repetition

4. **Conversation style**:
   - Multi-host: keep response time and natural pauses
   - Solo: tighter is OK, but don't over-trim

5. **Proper terms**:
   - Make sure the dictionary includes domain terms
   - Especially names and company names

---

## FAQ

### Q1: Aliyun API vs local FunASR — which?

**Recommend Aliyun API**:
- ✅ 7× faster (3 min vs 20 min)
- ✅ Speaker recognition (98.8 %)
- ✅ No local install needed
- ✅ Good for occasional use or when you need speed

**Choose local FunASR**:
- ✅ Free
- ✅ Data privacy (stays local)
- ✅ Good for heavy / frequent use
- ✅ Slightly higher accuracy (99 %+)

### Q2: speaker recognition isn't accurate

**Check**:
1. Is `SPEAKER_COUNT` correct?
2. Is the audio clean?
3. Are speaker voices distinguishable?

**Still bad**: use local FunASR (slightly higher accuracy), or hand-correct (usually <2 % difference; small workload).

### Q3: uguu.se URL expires after 24 h

**Solutions**:
1. Aliyun OSS (recommended, long-term)
2. Qiniu, Tencent COS, etc.
3. Your own server

**Aliyun OSS example**:
```bash
ossutil cp audio.mp3 oss://your-bucket/podcast.mp3
ossutil sign oss://your-bucket/podcast.mp3 --timeout 604800   # 7-day signed URL
```

### Q4: batch process multiple podcasts

```bash
for audio in /path/to/podcasts/*.mp3; do
  echo "Processing: $audio"
  # Invoke /podcast-edit-cut on each
done
```

### Q5: cost estimate

- Aliyun FunASR API: charged by audio length, ~¥X / hour. A 2-hour podcast costs ~¥X.
- uguu.se: free, <100 MB, 24-hour auto-delete.

---

## Pitfalls (lessons learned — read before changing time math!)

> Past landmines, not to be repeated.

### Pitfall 1: subtitles_words.json double-index

`subtitles_words.json` has three kinds of entries: real words, silence gaps (`isGap: true`), and speaker labels (`isSpeakerLabel: true`).

`sentences.txt`'s word indices refer to **the real-words array** (filtered).

```python
# ✅ Correct: filtered array
actual_words = [w for w in words if not w.get('isGap') and not w.get('isSpeakerLabel')]
time = actual_words[word_idx]['start']

# ❌ Wrong: full array (offset by ~25 s)
time = words[word_idx]['start']
```

### Pitfall 2: consecutive-sentence grouping (`convert_to_segments.js`)

**Problem**: emit one delete segment per sentence + naive merge → big delete blocks fragment into many tiny clips.

**Right way**: group consecutive deleted sentence indices first (e.g. 0,1,2,…,19 as one group), emit `[groupStart.startTime, groupEnd.endTime]` per group.

### Pitfall 3: MP3 concat lacks seek index

`ffmpeg -c copy` concat MP3 lacks Xing/LAME header → browser seeks are imprecise. Always re-encode after concat:
```bash
ffmpeg -i concat.mp3 -c:a libmp3lame -b:a 64k output.mp3
```

### Pitfall 4: extend the first segment to 0

If the first delete segment starts within the first 5 s, both `getSkipRanges()` (dynamic player) and `merge_fine_edits.js` (static cut) auto-extend to 0 to kill opening noise.

### Pitfall 5: fine-cut seekTarget must not jump to the next sentence

Fine cuts are intra-sentence partial deletes. `sentencesData.find(ns => ns.startTime >= e)` would return the **next** sentence (because the current sentence's startTime < e — the delete is mid-sentence), so kept content after the delete in the current sentence gets skipped entirely.

**Real case**: sentence 22's fine delete `[85.06, 87.34]`; the kept tail "和大家都很关心的经常发生的BURN OUT相关" at `87.5–91.18` was skipped because seekTarget jumped to sentence 23's startTime 92.76 — 5.4 s of kept content lost.

**Right way**: only use `nextKept.startTime` when `nextKept.startTime <= e + 0.5`; otherwise `seekTarget = e`.

### Pitfall 6: dynamic player skip precision

HTML5 `<audio>.currentTime` seek isn't frame-accurate (each MP3 frame ~26 ms), and JS timers add 50–100 ms latency. We need three layers of defense:

**6a. Adaptive lookahead**: per skip range, look up the previous kept word's end:
```javascript
const gap = rangeStart - closestPrevWordEnd;
// inter-sentence gap (~1s) → 300ms; in-sentence (~0s) → 50ms
range[2] = Math.min(0.30, Math.max(0.05, gap));
```

**6b. Tight-gap range nudge**:
```javascript
if (gap < 0.02) {
  // zero gap (word boundary): nudge 100 ms back to defeat onset leak
  merged[i][0] = Math.max(0, merged[i][0] - 0.10);
} else if (gap < 0.10) {
  merged[i][0] = Math.max(0, merged[i][0] - 0.05);
}
```

Real case: sentence 143 "方面的" ends 992.59, "困扰" (delete) starts 992.59 (gap=0). Nudged back to `[992.49, …]` to prevent the "困" onset leaking.

**6c. mute → seek → fast-restore**:
```javascript
audio.volume = 0;
audio.currentTime = seekTarget;
const resume = () => {
  audio.volume = savedVol * 0.3;
  setTimeout(() => { audio.volume = savedVol; }, 20);
  scheduleNextSkip();
};
audio.addEventListener('seeked', resume, { once: true });
setTimeout(resume, 80);  // aggressive fallback (was 200 ms)
```

Failed alternatives:
- ❌ `audio.muted = true` — buffered audio leaks during the gap
- ❌ Web Audio `GainNode` — file:// CORS blocks it
- ❌ `seekTarget = e + 0.05` — eats 50 ms of next kept word's onset
- ❌ pause → seek → play — pause sometimes prevents `seeked`, audio hangs
- ❌ 200 ms fallback — sentence-level skips feel laggy
- ✅ Conditional strategy: pause for tight gaps (defeat leak), seek for wide gaps (no click)

**6d. Precise seekTarget**:
- `seekTarget = e` (precise end of delete range, **no offset**)
- Use `nextKept.startTime` only when `nextKept.startTime <= e + 0.5` (see Pitfall 5)

### Pitfall 8: export must NOT use player skip ranges

`getSkipRanges()` ranges include 200 ms nudges and adaptive lookaheads (compensating for JS timer latency). The export to `cut_audio.py`'s `delete_segments_edited.json` must use **clean merged ranges** without nudges/lookaheads — ffmpeg cuts at PCM-sample precision.

### Pitfall 9: the browser cannot generate MP3 directly

- `fetch('file://...')` → CORS reject
- `createMediaElementSource` → file:// CORS, completely silent
- Web Audio decode + lamejs → 2-hour podcast needs ~1 GB RAM

**Conclusion**: HTML exports JSON; the user runs `python3 cut_audio.py` to produce the final audio.

### Pitfall 10: ffmpeg `-ss` placement controls the filter time origin

When `-af` is combined with `-ss`, `-ss`'s position matters:

```bash
# ❌ Wrong: -ss after -i (output option). Filter sees the global timeline;
# afade fires at global 6.93 s — by the time we extract from 10 s, volume is already 0.
ffmpeg -v quiet -i source.wav -ss 10 -to 17 \
  -af "afade=t=in:d=0.3,afade=t=out:st=6.93:d=0.3" -y output.wav

# ✅ Right: -ss before -i (input option); use -t (duration) instead of -to (absolute).
# Filter time starts at 0; afade times line up with the segment.
ffmpeg -v quiet -ss 10 -i source.wav -t 7 \
  -af "afade=t=in:d=0.3,afade=t=out:st=6.7:d=0.3" -y output.wav
```

**Real bug**: `cut_audio.py` placed `-ss`/`-to` after `-i`; every segment except the first (start=0) had its fade-out fire before extraction even reached the segment — output was 55 minutes of silence. The first segment (0–9.12 s) was the only correct one.

### Pitfall 11: cut_audio.py must use a WAV intermediate

MP3 `-c copy` cuts are only frame-accurate (~26 ms) → first kept word's onset is eaten or the previous deleted word's tail leaks (e.g. "对", "放").

**Fix (v2)**: decode to WAV → cut from WAV (sample-accurate) → concat → encode back to MP3. ~647 MB temp WAV for a 2-hour podcast; cleaned automatically.

### Pitfall 12: export range end must match seekTarget

The HTML player lands on `nextKept.startTime` after skipping. The export must do the same alignment (snap range end to next kept sentence's start), or ffmpeg's cut points and the audible HTML preview diverge.

### Pitfall 13: review-page manual edits must use charOffset for matching

**Problem 1**: user selects "你" mid-sentence; `indexOf("你")` matches the first "你" (which is already a fine-edit), tagging the wrong position.

**Problem 2**: user selects a whole sentence; `sel.toString()` includes UI tags' text (e.g. `">stutter"`), text matching fails.

**Right way**:
1. `markSelectionDeletedAndPrompt()` clones the range, removes all UI tag elements (`.fine-tag`, `.manual-tag`), then takes `textContent`.
2. Compute `charOffset` (char position of the selection inside the plain text) and store it in the manual-edit object.
3. `rebuildRowWithManualEdits()` matches via `charOffset` first; `indexOf` is fallback.

```javascript
// Compute charOffset
const preRange = document.createRange();
preRange.setStart(textEl, 0);
preRange.setEnd(range.startContainer, range.startOffset);
const preFrag = preRange.cloneContents();
preFrag.querySelectorAll('.fine-tag, .manual-tag, ...').forEach(el => el.remove());
const charOffset = (preFrag.textContent || '').length;
```

### Pitfall 14: don't use regex on innerHTML for review-page text

**Problem**: a missed-catch patch used regex on `innerHTML` to match text; it hit HTML attribute values (e.g. text inside `title="stutter"`), producing garbled output (`">stutter`).

**Right way**: all text operations go through DOM API (`querySelectorAll`, `insertBefore`, `createElement`). No regex on `innerHTML`.

### Pitfall 15: decoration tags must not block text selection

**Problem**: `.manual-tag` (the "manual" badge) blocked mouse events on the text below; single-character "你" couldn't be selected.

**Fix**: `pointer-events: none` on every decoration tag:
```css
.manual-tag, .fine-tag, .missed-catch-tag { pointer-events: none; }
```

### Pitfall 16: browser-preview judder ≠ final-output problem

**Symptom**: in cut-mode review playback, 0 ms-gap connected words ("这个球"→"所以", "但其实困住我们的") sound clipped at delete boundaries.

**Cause**: the browser's `<audio>` seek precision is ~26 ms (MP3 frame boundary) plus decoder settling time. Connected words with 0 ms gap can't be cleanly cut.

**Conclusion**: physical browser limit, **does not affect the final**. `cut_audio.py` decodes to WAV and operates at PCM-sample precision (~0.02 ms @ 44100 Hz) plus adaptive cross-fade — even tight connections cut cleanly. Verified by listening.

**Things NOT needed**:
- ❌ Recommend the user not delete tight connections — the FFmpeg final is fine.
- ❌ AI voice cloning to regenerate — over-engineering. FFmpeg cross-fade is enough.

### Pitfall 17: Stage 8 must use `cut_audio.py`, not hand-written FFmpeg

**Problem**: a hand-written `generate_cut.js` (filter_complex with 188 atrim) made FFmpeg molasses-slow (every segment decoded the whole file from scratch).

**Right way**: call `cut_audio.py --no-fade` directly. It already solves every known issue:
- WAV intermediate (sample-accurate)
- `-ss` before `-i` (Pitfall 10)
- `--no-fade`: 3 ms micro-fade defeats clicks; doesn't eat short syllables
- Speaker volume compensation
- concat demuxer (fast)

**Don't reinvent the wheel.** If you're tempted, read `cut_audio.py` first to confirm before writing alternatives.

### Pitfall 22: leading-pause marker on the wrong sentence

**Symptom**: a silence gap belongs to the previous sentence in `fine_analysis` (containing the last word before the gap), but the user listening for the pause reads the next sentence's start → "didn't catch it".

**Data**: of 12 user-annotated leading pauses, 9 were detected but shown on the previous sentence; 3 had gap < 0.8 s (user perception variance).

**Fix**: `generate_review_enhanced.js` adds an `incomingSilences` field, passing silence edits to the next non-deleted sentence too. The HTML template renders `⏸ -X s` at the sentence head (yellow dashed border, clickable to toggleFineEdit). **Fixed.**

### Pitfall 23: `merge_fine_edits.js` silence edits don't reach delete_segments (triple bug)

**Symptom**: `fine_analysis.json` detected 113 silence edits, but `merge_fine_edits.js` lost almost all of them when converting to delete_segments → final didn't trim pauses.

**Triple bug**:
1. **Intra-sentence search**: silence gaps live at sentence boundaries (last word of A → first of B), but the script searched between in-sentence words → no match.
2. **`actualWords` filtered out `isGap`**: the array has only natural word gaps (~0.1 s), not real pauses.
3. **Wrong threshold**: hard-coded `> 1.0 s` instead of `> 0.8 s`.

**Root cause**: `fine_analysis.json` already has accurate `deleteStart`/`deleteEnd`; the merge script ignored them and tried to recompute.

**Fix**: use `edit.deleteStart` / `edit.deleteEnd` directly; keep 0.8 s natural pause and delete the excess. **Fixed.**

### Pitfall 24: silence-trim retain duration must be below the threshold

**Problem**: silencedetect threshold 0.8 s, retain 0.4+0.4=0.8 s. Result: 300 silences of 0.80–0.85 s still flagged.

**Cause**: silencedetect's "silence boundary" and the trim point are different positions. silencedetect looks at noise-dB-low contiguous regions; trim cuts on timestamps. Edge low-energy audio (breath tail) gets included by silencedetect.

**Right way**: retain = threshold − 0.2 s buffer. For threshold 0.8 s, retain 0.3+0.3=0.6 s. `trim_silences.py` defaults to `--target 0.6` which already includes this buffer.

### Pitfall 25: in-sentence gap perception is much lower than inter-sentence

**Problem**: deleted "他也" (stutter repeat); the delete range only covers the words `[279.94, 280.78]`. Surrounding gap (279.82 → 281.46 = 1.64 s) sounded like an unnatural pause.

**Cause**:
1. 0.8 s inter-sentence pause is natural (breath/digestion); 0.3 s+ in-sentence gap reads as "hung".
2. ASR timestamps have gaps: deleted word's start lags real onset, end leads next word's start.

**Right way**: in-sentence deletes extend to `[prev_word.end, next_word.start]` — no leftover gap. Applies to stutter, self_correction, in-sentence filler, every in-sentence delete type.

### Pitfall 26: filler delete range must cover onset leak

**Problem**: sentence 9's tail "嗯" was tagged delete `[21.13, 21.73]`, but the final still has residual sound.

**Cause**: ASR reports `filler.start` = 21.13, but the real onset is later. Previous word "岁" ended at 20.53; the 0.6 s gap between contains the start of "嗯".

**Right way**: filler delete range = `[prev_word.end, next_word.start]`, not `[filler.start, filler.end]`. See `editing-rules/2-filler-detection.md` "delete boundary".

### Pitfall 27: `--no-fade` is not optional

**Problem**: every run of `cut_audio.py` someone forgets `--no-fade` → default 0.3 s adaptive fade eats short syllables (e.g. after "呢啊" delete, the next word's volume drops sharply).

**Lesson**: `--no-fade` **must be passed** (already emphasized in Step 8), but easy to forget. Self-check when executing Step 8.

### Pitfall 28: ASR onset pullback must cover all gap sizes

**Problem**: review_enhanced.html's `exportDeleteSegments()` only did onset pullback for `gap < 300 ms` (back 50 ms); `gap ≥ 300 ms` got no pullback. Result: S13's "对" (gap 470 ms), S14's "嗯" (gap 280 ms) had residual onset after delete.

**Cause**: ASR word-start timestamps lag real onset by 30–90 ms regardless of gap size.

**Fix**: add `else if (s > 0.05)` — pull back for every nonzero gap, clamped to `min(50ms, gap/2)`:
```javascript
} else if (s > 0.05) {
  const pullback = Math.min(0.05, gapBefore / 2);
  merged[i][0] = Math.max(0, s - pullback);
}
```

### Pitfall 29: single-char text-match ambiguity (`merge_llm_fine.js`)

**Problem**: S100 LLM edit `text: "一"` matched via `fullClean.indexOf("一")` to W1758 in "介绍一下" (first occurrence) instead of the intended W1761 in "的一" → "介绍一下" wrongly deleted.

**Cause**: `indexOf` matches the first occurrence; single- or two-char edits frequently land on the wrong position.

**Right way (LLM prompt layer)**: LLM's `text` field must include enough context to disambiguate:
- ❌ `"text": "一"` — ambiguous
- ✅ `"text": "的一"` — precise

**Already updated `llm-fine-edit-prompt-template.md`.** Could add a match validator in `merge_llm_fine.js` (check that surrounding words match the LLM's flagged sentence).

### Pitfall 30: feedback-loop input format conversion needed

**Problem**: `analyze_feedback.js` expects `ai_feedback_*.json` (from the review page's "Export AI feedback"), but in practice users sometimes export `delete_segments.json` whose `editState` has a different format.

**Conversion**:
- `editState.missedCatches` → `feedback.missed_catches`
- `editState.manualEdits[].sentenceIdx` → `feedback.user_corrections.added_deletions`
- `editState.userRemoved` → `feedback.user_corrections.removed_deletions`

### Pitfall 31: feedback-loop output should be tiered

**Problem**: `apply_feedback_to_rules.js` wrote every adjustment to `user-prefs/editing_rules/` (per-user). But most missed detections (self_correction patterns, stutter patterns) are general LLM gaps, not personal preferences.

**Right way**:
- **Universal detection improvements** (LLM-missed specific patterns) → update `editing-rules/llm-fine-edit-prompt-template.md` (every user benefits).
- **Personal preferences** (aggressiveness, type-retention preferences) → write to `user-prefs/editing_rules/`.

### Pitfall 32: silence_merged segment covers user-restored sentences

**Symptom**: user un-checks deleted sentences (restore), but the fine player still skips that time range.

**Root**: `merge_llm_fine.js`'s gap-cleanup makes `silence_merged` segments attached to neighbours (e.g. attached to S94), whose time range covers the entire delete block (e.g. 269.3–297.12 s). When the user restores S97/S98, those sentences are off but the silence_merged (on S94) is still on → `collectActiveRanges()` still includes the range.

**Fix**: in `getSkipRanges()` and `exportDeleteSegments()`, after merging skip ranges, "punch holes" — for every kept sentence, subtract its time range from the skip ranges. Same fix in `templates/review_enhanced.html`. **Fixed.**

### Pitfall 33: punch-holes wipes fine edits (Pitfall 32 regression)

**Symptom**: every fine marking (stutter/filler/self_correction) shows strike-through but doesn't actually skip.

**Root**: Pitfall 32's punch-holes fix was too aggressive — for every kept sentence, every overlapping skip range was removed or trimmed. But fine edits (word-level deletes) produce skip ranges entirely inside kept sentences (they ARE intra-sentence sub-ranges), and got dropped as "wrong coverage".

**Fix**: in punch loop, if a range is fully inside a kept sentence (`r[0] >= ksStart && r[1] <= ksEnd`), it's a fine edit — keep it:
```javascript
if (r[0] >= ksStart && r[1] <= ksEnd) {
  newRanges.push(r);
  continue;
}
```
Fixed in both `getSkipRanges()` and `exportDeleteSegments()`. **Fixed.**

**Follow-up 1**: `silence_merged` can attach across sentences (S304's silence_merged covers S307). Fix: when emitting silence_merged, `merge_llm_fine.js` records a `dependsOn` array (the edit feIdx that caused the gap). `collectActiveRanges()` checks `dependsOn` and suppresses silence_merged when any dependency is disabled.

**Follow-up 2 (Pitfall 34)**: punch-holes removed entirely. When silence_merged is adjacent to a manual/fine edit (gap ≤ 50 ms), sort-and-merge collapses them into one large range. punch-holes sees that range crossing kept-sentence boundaries, splits it, drops the inside — manual and fine skips were collateral. `dependsOn` already controls silence_merged at the source (`collectActiveRanges`); punch-holes is redundant and harmful. **Removed all punch-holes from `getSkipRanges()` and `exportDeleteSegments()`.**

**Lesson**: (1) Derived data (silence_merged) should record dependencies at production time, not be heuristically inferred at consumption. (2) In multi-stage post-processing (collect → merge → punch), the merge step destroys range edge semantics; downstream punch can't reliably tell "should-keep intra-sentence edit" from "should-not-keep cross-sentence overflow". (3) Right way: control which ranges are valid at source (`collectActiveRanges`) via precise dependencies; don't do coarse geometric trimming downstream.

### Pitfall 34: ASR merged-word delete out-of-bounds (S188 bug)

**Symptom**: user wanted to delete "就是要" (keep "就是一口答应" after); the final eats "就是" too.

**Root**: FunASR merged "就是要就是" into a single ASR word (time range 690.69–692.53 s). `mapTextToTimestamps()` mapping the delete text "就是要" took the whole word's `end=692.53` — should be at the 3/5 mark (~691.79 s).

**Fix**:
1. `merge_llm_fine.js`'s `mapTextToTimestamps()` adds partial-word time interpolation (character-ratio) as the initial estimate.
2. Tag `_refinePoints` metadata; `refine_fine_analysis.js` calls `refine_boundaries.py` for waveform onset detection.
3. Onset detection uses RMS-energy envelope to find inter-syllable valleys — more accurate than linear interpolation (S188 verified: 691.79 → 691.88).

**Fixed.**

### Pitfall 35: waveform onset detection — pick the closest valley, not the deepest

**Symptom**: `refine_boundaries.py` initial version used "deepest valley" — but the wider the search window, the more it would jump to a far-away inter-sentence silence (46 dB drop), drifting from the target.

**Fix**: switched to "closest qualifying valley" — find every local min ≥3 dB drop, pick the one closest to the original time. 0.10 s / 0.15 s / 0.20 s windows now converge stably.

**Fixed.**

### Pitfall 36: manual edits also need boundary extension

**Symptom**: user manually deletes text in the review page; final has "deleted-but-not-quite" residual.

**Root**: in `collectActiveRanges()`, manual-edit time ranges weren't extended to neighbouring word boundaries the way auto-fine edits are. ASR onset leak then leaves a residual head.

**Fix**: start uses 200 ms threshold (same as auto-fine); end uses 2.0 s threshold (manual filler-deletes often have a long dead zone after them). **Fixed.**

### Pitfall 37: review_enhanced.html injected state overwrites user edits

**Symptom**: user edits in the review page; refresh; everything's lost.

**Root**: `generate_review_enhanced.js`'s injected `_injectedState` unconditionally overwrites localStorage on every page load — even if localStorage has newer user edits.

**Fix**: injection compares timestamps; only injects when localStorage has no newer state. **Fixed.**

### Pitfall 38: onset detection direction must search inward toward the delete region

**Symptom**: after onset detection refinement, some kept characters get eaten (e.g. "再比如" loses 61 % of "再").

**Root**: when collecting boundary points for delete segments, used `direction="both"` (bidirectional). The algorithm finds a fake valley in the kept word's onset / weak syllable and pushes the boundary outward. Humans cutting waveforms only look INTO the delete region for quiet gaps.

**Rules**:
- delete START → `direction: "right"` (search rightward, into the delete)
- delete END → `direction: "left"` (search leftward, into the delete)
- safety clamp: refined START ≥ original; refined END ≤ original

Fixed in `merge_llm_fine.js` (4 directions) and `refine_fine_analysis.js` (clamp). **Fixed.**

### Pitfall 39: filler delete START must cover full leading energy

**Symptom**: deleting "嗯/呃" leaves a click (truncated onset fragment).

**Root**: ASR word-start timestamps lag the acoustic onset by 100–200 ms. Filler vocalization includes glottal prep + breath; energy starts rising before ASR's tagged time. With deleteStart at the ASR word start, 100–200 ms of pre-onset energy remains — sounds like a click.

**Case**: sentence 277 "嗯，" ASR-tagged `[1306.73, 1307.13]`; energy starts rising at 1306.48. deleteStart=1306.73 leaves 250 ms of "嗯" attack.

**Rule**: filler/stutter delete START extends to `prevWord.end + 50 ms` to cover gap pre-onset energy.

Fixed in `merge_llm_fine.js` (filler pre-onset extension). **Fixed.**

### Pitfall 40: large-block delete onset can push past the start kept word

**Symptom**: onset direction=right pushes the start of a long delete into a kept word, truncating it (e.g. "然后" leaves only "然").

**Root**: a long delete (e.g. 200 s) may have a short weak kept word right at its start (e.g. "然后", 280 ms). Onset search direction=right finds a valley inside that kept word, pushing past it.

**Case**: sentence 223 Seg 34 `[921.59, 1122.19]`; onset pushed start to 921.73, crossing "然后" (921.64–921.92), leaking "然".

**Rule**: when onset-detecting on user-exported segments, watch for short kept words before delete starts. If a kept word is < 300 ms and the refined onset crosses it, clamp or skip.

### Pitfall 41: low-quality MP3 source + PCM decode offset

**Symptom**: cut audio sounds "muffled". Trying to PCM-sample-fix the cut, the mute position drifts from the audible position; widening the range repeatedly never converges.

**Root**:
1. **Source quality**: `audio.mp3` is the 16 kHz/24 kbps ASR-downsampled version (8 kHz frequency cap). Must use `audio_original.*` (typically 44.1–48 kHz). `cut/SKILL.md` already says this, but old projects may lack `audio_original.*`.
2. **PCM offset**: MP3 LAME inserts ~1152 samples (~26 ms) of encoder delay at the file head. `ffmpeg -f s16le` raw PCM output: sample 0 ≠ playback time 0. Sample-level fixes on a cut MP3 systematically drift by 20–30 ms.

**Rules**:
- **Don't do PCM sample-level fixes on a cut MP3.** If precise tweaks are needed, calibrate `delete_segments` BEFORE `cut_audio.py` (source timestamps have no offset).
- Use `waveform_trim.py` to calibrate before cutting — not after.
- All cuts MUST use `audio_original.*`, not `audio.mp3`.

`waveform_trim.py` (waveform calibration) and `cut_audio.py` (source selection) are involved.

---

## Waveform-guided boundary calibration (`waveform_trim.py`)

An additional refinement step between `refine_boundaries.py` (single-point valley detection) and `cut_audio.py`. For short segments in `delete_segments` (filler/stutter < 2 s), runs full energy-envelope analysis to align start/end with real acoustic boundaries.

### Difference from `refine_boundaries.py`

| | refine_boundaries.py | waveform_trim.py |
| --- | --- | --- |
| Goal | find a valley near a single time point | calibrate a delete segment's start/end |
| Scope | local: 150 ms search window | global: 500 ms before/after the segment |
| Detection | valley (energy minimum) | threshold crossing (drops to noise floor × 2) |
| Output | refined timestamp | calibrated delete_segments JSON |
| Diagnostics | none | PNG waveform per calibrated segment |

### Usage

```bash
python3 waveform_trim.py <audio_original> <delete_segments.json> [--output out.json] [--diag-dir dir/]
```

### Integration (full pipeline)

```
merge_llm_fine.js (tag _refinePoints)
     ↓
refine_fine_analysis.js (collect + call refine_boundaries.py)
     ↓
user review → export delete_segments.json
     ↓
[NEW] waveform_trim.py (waveform-calibrate delete boundaries)
     ↓
cut_audio.py (using audio_original.*)
```

---

## Waveform onset detection refinement (`refine_boundaries.py`)

A refinement step between `merge_llm_fine.js` and `cut_audio.py`. Uses audio energy analysis to find real syllable boundaries — replaces inaccurate linear-character interpolation.

### Principle

Mandarin syllables average ~200 ms; inter-syllable energy valleys are 10–30 ms (breath transitions). Even when ASR merges multiple characters into one word, the valleys remain in the waveform.

### Algorithm

1. FFmpeg-decode the target region to 16 kHz mono PCM.
2. 5 ms-frame RMS + 3-frame moving average → smoothed energy envelope.
3. Find every local minimum (lower than both neighbours).
4. Filter to valleys ≥ 3 dB below local mean.
5. Pick the qualifying valley closest to the original time (not the deepest).
6. confidence < 0.5 → fall back to original time.

### Integration

```
merge_llm_fine.js (tag _refinePoints)
     ↓
refine_fine_analysis.js (collect + call Python)
     ↓
refine_boundaries.py (waveform analysis → refined timestamp)
     ↓
fine_analysis.json (updated)
     ↓
generate_review_enhanced.js / cut_audio.py
```

### Trigger conditions

- **Partial-word interpolation**: `mapTextToTimestamps()` sees a delete text covering only part of an ASR word
- **Filler / stutter tight join**: edit's `deleteStart`/`deleteEnd` is < 50 ms from the neighbouring word

### Key parameters

| Parameter | Value | Notes |
| --- | --- | --- |
| SAMPLE_RATE | 16000 | decode rate (sufficient for energy analysis) |
| FRAME_MS | 5 | energy-frame length |
| SMOOTH_FRAMES | 3 | moving-average window (15 ms) |
| MIN_DROP_DB | 3.0 | minimum valley depth |
| search_window | 0.10–0.15 s | partial: 0.15 s, filler: 0.10 s |

---

**Recommended workflow**: Aliyun API transcribe + Claude analyze + enhanced review + one-shot cut ✨
