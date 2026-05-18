# Podcast Edit

> A podcast-editing agent built with Claude Code skills. From raw recording to publish-ready cut.

## Why?

Pain points of traditional podcast editors:

1. **No semantic understanding**: pre-show prep, off-topic chit-chat, repeated content — these tools can't tell.
2. **Manual editing is slow**: a 2-hour podcast needs hours of listening to find the issues.
3. **Crude verbal-tic handling**: stutters, self-corrections, consecutive fillers — handled one at a time by hand.

This agent uses Claude's semantic understanding for content analysis, local Whisper for transcription, and an interactive review page for human confirmation. AI-assisted end-to-end.

## Result

- Local transcription + AI analysis + interactive review → final MP3
- Optional OpenAI path remains available when API diarization is preferred
- Paragraph-level content trimming + word-level fine cut (stutters, self-corrections, fillers)
- Gemini-based non-speech vocal review for throat clearing and nose clearing (Stage 2.4)
- In-browser real-time playback with every edit applied instantly

## Install

### 1. Register skills

```bash
# Clone the project
git clone <repo-url> /path/to/podcast-edit-skill

# Register in Claude Code (create symlinks)
SKILL_DIR="/path/to/podcast-edit-skill"   # adjust to your path
mkdir -p ~/.claude/skills
ln -s "$SKILL_DIR/install" ~/.claude/skills/podcast-edit-install
ln -s "$SKILL_DIR/cut"     ~/.claude/skills/podcast-edit-cut
ln -s "$SKILL_DIR/polish"  ~/.claude/skills/podcast-edit-polish
ln -s "$SKILL_DIR/qa"      ~/.claude/skills/podcast-edit-qa
```

Verify: restart Claude Code and type `/` — `podcast-edit-install`, `podcast-edit-cut`, etc. should appear.

### 2. Install dependencies

```bash
brew install node ffmpeg
pip install -r cut/scripts/requirements.txt
pip install librosa soundfile     # for the qa skill
```

### 3. Configure optional API keys

```bash
cd /path/to/podcast-edit-skill
cp .env.example .env
# Edit .env only if you use optional services such as Gemini QA or OpenAI fallback
```

### 4. Use it

In Claude Code:

```
/podcast-edit-cut your-audio-file.mp3
```

Detailed install steps: `/podcast-edit-install`.

## Eight-stage pipeline

```
/podcast-edit-cut
    │
    │  Stage 1: user start
    │  ├─ new user: sample learning / questionnaire
    │  └─ existing user: one-line confirmation
    │
    │  Stage 2: cut analysis
    │  ├─ transcribe (local Whisper by default)
    │  ├─ speaker labels + sentence split
    │  ├─ AI rough-cut (paragraph-level)
    │  ├─ AI fine-cut (word-level: stutter, self-correction, filler)
    │  └─ Gemini non-speech vocal detection (throat_clear / nose_clear)
    │
    │  Stage 3: AI self-review
    │  └─ review agent checks consistency, misdetection, sensitive words
    │
    │  Stage 4: user review
    │  ├─ generate review page → open in browser
    │  │   ┌──────────────────────────────────────┐
    │  │   │  review page (review_enhanced.html)  │
    │  │   │  - fine-cut player (real-time skip)  │
    │  │   │  - sentence delete/restore, fine toggle │
    │  │   │  - manual selection delete + AI feedback export │
    │  │   └──────────────────────────────────────┘
    │  ├─ user reviews + exports delete_segments_edited.json
    │  └─ feedback learning → updates user prefs / editing rules
    │
    │  Stage 5: cut execution
    │  ├─ cut_audio.py (sample-accurate WAV cut)
    │  └─ trim_silences.py (final silence trim)
    │
/podcast-edit-qa
    │  Stage 6: AI QA
    │  ├─ Phase A: data layer (delete-segment correctness)
    │  ├─ Phase B: signal layer (energy/spectrum/silence)
    │  └─ Phase C: semantic layer (re-transcribe LCS align, optional)
    │
/podcast-edit-polish
    │  Stage 7: post-production
    │  ├─ highlight clips → intro teaser
    │  ├─ intro/outro music
    │  └─ chapter timestamps + titles + show notes
    │
    │  Stage 8: final user review
    │  ├─ final page (review_final.html)
    │  │   QA issues + clickable timestamps + confirm/flag
    │  └─ feedback learning → updates editing rules / user prefs
```

## Skill list

| Skill | Slash command | Function |
| --- | --- | --- |
| install | `/podcast-edit-install` | register skills, install deps, configure API key |
| cut | `/podcast-edit-cut` | the 8-stage orchestrator: transcribe + analyze + review + cut + final review |
| qa | `/podcast-edit-qa` | three-phase QA: data + signal + semantic |
| polish | `/podcast-edit-polish` | highlight teaser, intro music, chapter timestamps, titles, show notes |

## Directory layout

```
podcast-edit-skill/
├── README.md
├── CLAUDE.md
├── .env.example
├── install/                       # install skill
│   └── SKILL.md
├── cut/                           # core skill (stages 1-5, 8)
│   ├── SKILL.md                   # full pipeline doc (8 stages)
│   ├── scripts/
│   │   ├── aliyun_funasr_transcribe.sh  # TODO: Replace with OpenAI Whisper
│   │   ├── transcribe_whisper_local.py
│   │   ├── transcribe_whisper_multitrack.py
│   │   ├── identify_speakers.js
│   │   ├── generate_subtitles_from_aliyun.js # TODO: Replace with OpenAI Whisper
│   │   ├── generate_sentences.js
│   │   ├── generate_review_enhanced.js
│   │   ├── generate_review_final.js
│   │   ├── capture_final_feedback.js
│   │   ├── detect_non_speech_vocals_gemini.py
│   │   ├── non_speech_vocal_filters.py
│   │   ├── capture_nsv_feedback.js
│   │   ├── validate_nsv_pipeline.sh
│   │   ├── cut_audio.py
│   │   ├── trim_silences.py
│   │   ├── merge_llm_fine.js
│   │   └── user_manager.js
│   ├── templates/
│   │   └── review_enhanced.html
│   ├── editing-rules/             # shared rules (every user)
│   │   ├── 1-core-principles.md
│   │   ├── 2-filler-detection.md
│   │   ├── ...
│   │   └── 10-content-analysis-methodology.md
│   └── user-prefs/                # personal prefs (per-user)
│       ├── default/
│       └── <userId>/
├── polish/                        # final-polish skill (stage 7)
│   ├── SKILL.md
│   └── scripts/
│       └── mix_highlights_with_music.py
├── qa/                            # qa skill (stage 6)
│   ├── SKILL.md
│   └── scripts/
│       ├── signal_analysis.py
│       ├── semantic_review.js
│       ├── audit_cut.js
│       └── report_generator.py
└── output/                        # output directory (auto-created)
    └── YYYY-MM-DD_<audio>/
        └── cut/
            ├── 1_transcript/
            ├── 2_analysis/
            ├── 3_output/
            ├── review_enhanced.html
            └── review_final.html
```

Non-speech vocal detection (Stage 2.4) requires `GEMINI_API_KEY` in `.env`. Tunable per-user via `cut/user-prefs/<userId>/preferences.yaml` → `non_speech_vocal:` (enabled, detect_types, confidence_threshold, auto_delete_threshold). See `cut/editing-rules/11-non-speech-vocal.md` for detection logic and `docs/superpowers/specs/2026-05-18-non-speech-vocal-detection-design.md` for why Gemini (spike found YAMNet/Whisper/SenseVoice all hit 0% recall on Mandarin throat-clears).

## Two-tier learning

| Tier | Location | Content | When |
| --- | --- | --- | --- |
| Editing rules | `cut/editing-rules/` | detection algorithms, common thresholds, methodology | algorithmic gaps found by QA |
| User prefs | `cut/user-prefs/<userId>/` | aggressiveness, per-token keep/delete | user review feedback |

Feedback is captured at three points — Stage 4 (user review), Stage 6 (AI QA), Stage 8 (final review) — and persisted to skill files, so it works across machines and accounts.

## Dependencies

| Dep | Purpose | Install |
| --- | --- | --- |
| Node.js | run scripts | `brew install node` |
| FFmpeg | audio processing | `brew install ffmpeg` |
| Python 3 | audio cutting | macOS built-in |
| faster-whisper | local speech recognition | `pip install -r cut/scripts/requirements.txt` |

## License

MIT
