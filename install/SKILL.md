---
name: podcast-edit:install
description: Environment setup. Register skills, install dependencies, configure API keys, and verify the install. Triggers — install, setup, initialize, prepare podcast editing environment, 安裝, 初始化.
---

<!--
input: none
output: a working environment
pos: prerequisite skill, run before first use

Architecture guardian: when this file is modified, also update:
1. ../README.md skill table
2. /CLAUDE.md routing table
-->

# Install

> Environment prep — run this once before first use.

## Quick start

```
User: install the environment
User: initialize
User: prepare the podcast editing environment
```

## Step 0: register skills

Claude Code discovers skills under `~/.claude/skills/`. Create a symlink for each skill folder.

```bash
# Set the project path to wherever you cloned this repo
SKILL_DIR="/path/to/podcast-edit-skill"

# Create symlinks
mkdir -p ~/.claude/skills
ln -s "$SKILL_DIR/install" ~/.claude/skills/podcast-edit-install
ln -s "$SKILL_DIR/cut"     ~/.claude/skills/podcast-edit-cut
ln -s "$SKILL_DIR/polish"  ~/.claude/skills/podcast-edit-polish
ln -s "$SKILL_DIR/qa"      ~/.claude/skills/podcast-edit-qa
```

Verify: type `/` in Claude Code — `podcast-edit-install`, `podcast-edit-cut`, etc. should appear.

> If `~/.claude/skills/` does not exist, create it first: `mkdir -p ~/.claude/skills/`.

## Step 1: install dependencies

| Dependency | Purpose | Install |
| --- | --- | --- |
| Node.js | Run JavaScript scripts | `brew install node` |
| FFmpeg | Audio processing, CBR re-encode | `brew install ffmpeg` |
| Python 3 | `cut_audio.py` and other scripts | bundled with macOS, or `brew install python3` |
| DeepFilterNet | Audio noise reduction (polish skill, optional) | `pip install deepfilternet` |
| librosa | Audio signal analysis (qa skill) | `pip install librosa soundfile` |
| curl | API calls | system default |

```bash
# macOS
brew install node ffmpeg

# Python deps
pip install librosa soundfile     # qa skill — required
pip install deepfilternet         # polish skill — optional

# Node deps (just js-yaml)
cd "$SKILL_DIR"
npm install

# Verify
node -v
ffmpeg -version
python3 --version
```

## Step 2: configure API keys

### Aliyun DashScope (speech recognition — required)

Console: https://dashscope.console.aliyun.com/

1. Sign up for an Aliyun account.
2. Activate the "Model Service Lingji" service.
3. Create an API key.

```bash
cd "$SKILL_DIR"
cp .env.example .env
# Edit .env and fill in your API key
```

`.env`:

```
DASHSCOPE_API_KEY=sk-your-api-key-here
```

### Gemini (optional — QA Phase B Layer 2)

Adds AI listening to QA. Without it, signal-layer analysis still works.

```
GEMINI_API_KEY=your-gemini-api-key
```

Get a key at https://aistudio.google.com/apikey.

## Step 3: verify

```bash
node -v                               # Node.js
ffmpeg -version                       # FFmpeg
python3 --version                     # Python 3
cat "$SKILL_DIR/.env" | grep DASHSCOPE  # API key
ls -la ~/.claude/skills/ | grep podcast-edit  # registered skills
```

If all four show output, you are ready:

```
/podcast-edit-cut your-audio-file.mp3
```

## FAQ

### Q1: where do I get the Aliyun API key?

Aliyun console → https://dashscope.console.aliyun.com/ → create API key.

### Q2: ffmpeg not found

```bash
which ffmpeg
# If empty: brew install ffmpeg
```

### Q3: how long can a podcast be?

- Verified: 2.5 hours (147 min) processed normally.
- Aliyun FunASR transcription completes in ~3 min regardless of audio length.
- No hard limit; no need to chunk long audio.

### Q4: does it identify speakers in multi-host conversations?

Yes. Aliyun FunASR provides speaker diarization with ~98.8 % accuracy in our tests. You must specify the correct number of speakers when transcribing.

### Q5: skills don't show up after registration

1. Check the symlink: `ls -la ~/.claude/skills/ | grep podcast-edit`.
2. Check the target exists: `ls "$SKILL_DIR/install/SKILL.md"`.
3. Restart Claude Code so it rescans `~/.claude/skills/`.

### Q6: how do I add intro/outro music and chapter timestamps?

Use the polish skill (`/podcast-edit-polish`): highlight teasers, intro/outro music, chapter timestamps, title suggestions, show notes.
