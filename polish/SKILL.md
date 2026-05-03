---
name: podcast-edit:polish
description: Final podcast polish — highlight teaser, intro/outro music, chapter timestamps, title suggestions, show notes. Triggers — final touch, polish, intro music, generate timestamps, 後期, 最終潤色.
---

# Podcast Final Polish

> Highlight clips → teaser → intro/outro music → chapter timestamps → titles + show notes.

---

## ⚠️ Ask before starting (music files and durations)

**At the very beginning, ask the user for:**

```
Please provide:

1. **Intro music file**
   - e.g. `/path/to/intro-music.mp3`

2. **Outro music file**
   - "same as intro" if they share a file
   - Tell me if you don't want outro music

3. **Cut podcast audio**
   - e.g. `/path/to/podcast_v2.mp3`

4. **(optional) Transcript or transcription JSON**
   - Used for highlight selection and chapter timestamps
```

**Do not assume any specific music!** Every podcast has a different intro/outro style.

### No music yet? Recommended free-music sources

| Site | Notes | License |
| --- | --- | --- |
| [Free Music Archive](https://freemusicarchive.org/) | Large indie library, browse by genre/mood | Creative Commons (check per-track) |
| [Pixabay Music](https://pixabay.com/music/) | Fully free, no attribution required | Pixabay License, commercial OK |
| [Incompetech](https://incompetech.com/music/) | Kevin MacLeod's classic library | Free with attribution; paid no-attribution |
| [Unminus](https://www.unminus.com/) | Clean UI, easy preview | Free, commercial OK |
| [YouTube Audio Library](https://studio.youtube.com/channel/audio) | Google's library | Some free, some attribution required |

> If the file isn't MP3, convert: `ffmpeg -i input.wav -c:a libmp3lame -b:a 192k output.mp3`.

### After receiving music: confirm the duration plan

Show the default plan and wait for confirmation:

```
Got it! Music plan:

| Position | Music file        | Duration | Fade in | Fade out |
| -------- | ----------------- | -------- | ------- | -------- |
| Intro    | intro-music.mp3   | 15 s     | 2 s     | 3 s      |
| Outro    | outro-music.mp3   | 15 s     | 5 s     | 3 s      |

Confirm, or tell me the durations you want (e.g. "intro 20s, outro 10s").
```

### ⚠️ Cross-machine portability: copy music into the project

After getting paths, **the very first step** is to copy them into the project's polish working directory:

```bash
# Intro
cp "$USER_INTRO_MUSIC" "$WORK_DIR/theme_intro.mp3"
THEME_INTRO="$WORK_DIR/theme_intro.mp3"

# Outro (may differ from intro)
cp "$USER_OUTRO_MUSIC" "$WORK_DIR/theme_outro.mp3"
THEME_OUTRO="$WORK_DIR/theme_outro.mp3"
```

**All subsequent commands use `$WORK_DIR/theme_intro.mp3` / `$WORK_DIR/theme_outro.mp3` — never the user's original absolute path.** This makes the project directory self-contained.

---

## Intro/outro rules (defaults)

| Position | Default duration | Effect | Configurable |
| --- | --- | --- | --- |
| Intro | 15 s | Music fades in → fades out as voice enters | duration, music file |
| Outro | 15 s | Voice ends → music fades in → fades out | duration, music file |

**Intro and outro can use different music files** or the same one. Duration is configurable at start.

---

## Quick start

```
User: do the final touch on this podcast
User: add an intro teaser
User: generate chapter timestamps
User: final touch
```

## Inputs

- A cut podcast audio (typically `/podcast-edit-cut`'s output)
- (optional) Transcript or transcription JSON
- (optional) Intro music file
- (optional) Outro music file (may differ from intro)

## Outputs

1. **Highlight teaser** — 3–4 highlight clips concatenated into the intro
2. **Teaser with background music** — the teaser overlaid with bed music
3. **Chapter timestamps** — for YouTube / podcast platforms
4. **Title suggestions** — 3–5 options
5. **Show notes** — ready-to-publish description

---

## Flow

```
0.   Ask user: intro music, outro music, cut audio, transcript
0.5  Show default duration plan; wait for confirm or override
 1.  Analyze content; recommend highlight clips
     【user picks 3–4】
 2.  Extract clips; concatenate into teaser
 3.  Add intro music (default 15 s, fade in/out)
 4.  Add outro music (default 15 s, fade in/out)
 5.  Analyze topic structure; generate chapter timestamps
 6.  Generate title suggestions
 7.  Generate show notes
Done
```

---

## I. Highlight clip recommendations

### What makes a good highlight

| Trait | Description |
| --- | --- |
| Money quote | Strong, concise, memorable |
| Clash | Spirited disagreement |
| Emotional peak | Laughter, surprise, awe |
| Hook | Makes the listener want to keep going |
| Core insight | Distillation of the episode's most valuable point |

### Recommendation format

```markdown
## Highlight clip suggestions

I've found these standout moments. Pick 3–4 for the intro teaser:

| # | Time | Speaker | Summary | Why |
| - | ---- | ------- | ------- | --- |
| 1 | 15:32–15:58 | Alice | "The meaning of work isn't…" | Money quote, core insight |
| 2 | 32:45–33:12 | Bob   | "I knew right then it was off…" | Strong narrative, suspense |
| 3 | 48:20–48:45 | Carol | "These are completely different things…" | Clash |
| 4 | 1:05:30–1:06:00 | Alice | [laughs] "That metaphor is *killer*" | Emotional peak |
| 5 | 1:18:22–1:18:50 | Bob   | "If I had to choose again…" | Thought-provoking |

Reply with numbers, e.g. `1, 3, 4` or `pick 1 3 4`.
```

### Identification heuristics

1. **Scan the transcript** for strong phrasings, metaphors, rhetorical questions.
2. **Detect emotion words**: laughter, "wow", "really?", "exactly".
3. **Watch for pivots**: "but", "actually", "to be honest", "I think".
4. **Spot money-quote shapes**: short sentences, parallels, contrasts, analogies.
5. **Recommend 5–8** so the user can pick 3–4.

---

## II. Highlight teaser

### Teaser specs

| Spec | Value |
| --- | --- |
| Clip count | 3–4 |
| Per-clip length | 10–30 s |
| Total length | 30–90 s |
| Order | Most arresting first |

### FFmpeg concatenation

```bash
# 1. Extract clips
ffmpeg -i podcast.mp4 -ss 15:32 -to 15:58 -c copy clip1.mp4
ffmpeg -i podcast.mp4 -ss 32:45 -to 33:12 -c copy clip2.mp4
ffmpeg -i podcast.mp4 -ss 48:20 -to 48:45 -c copy clip3.mp4

# 2. Build the concat list
echo "file 'clip1.mp4'" > clips.txt
echo "file 'clip2.mp4'" >> clips.txt
echo "file 'clip3.mp4'" >> clips.txt

# 3. Concatenate
ffmpeg -f concat -safe 0 -i clips.txt -c copy preview.mp4
```

---

## III. Intro / outro music

### Structure

```
┌─────────────────────────────────────────────────────────────────┐
│  Intro music   │  Highlight teaser  │   Main content   │  Outro │
│  (configurable)│   (30–90 s)        │                  │  music │
│  fade in→out   │                    │                  │        │
└─────────────────────────────────────────────────────────────────┘
```

**Intro and outro can use different files** — for example a punchy intro jingle and an emotional outro.

### Duration config

| Position | Default | Range | Default fade-in | Default fade-out |
| --- | --- | --- | --- | --- |
| Intro | 15 s | 5–30 s | 2 s | 3 s |
| Outro | 15 s | 5–30 s | 5 s | 3 s |

Fades adapt to the chosen duration:
- duration ≤ 10 s → fade-in 2 s, fade-out 2 s
- 10 – 20 s → fade-in 5 s, fade-out 3 s (default)
- > 20 s → fade-in 5 s, fade-out 4 s

### Intro processing

```bash
# Variables (confirmed at start)
THEME_INTRO="$WORK_DIR/theme_intro.mp3"
INTRO_DUR=15        # user-overridable
INTRO_FADE_IN=2
INTRO_FADE_OUT=3
INTRO_FADE_OUT_ST=$((INTRO_DUR - INTRO_FADE_OUT))

# Trim, fade, and re-encode the intro music
ffmpeg -i "$THEME_INTRO" \
  -af "atrim=start=0:end=$INTRO_DUR,asetpts=PTS-STARTPTS,afade=t=in:d=$INTRO_FADE_IN,afade=t=out:st=$INTRO_FADE_OUT_ST:d=$INTRO_FADE_OUT" \
  -c:a libmp3lame -b:a 128k intro_music.mp3

# Concatenate intro music + teaser + main content
ffmpeg -i intro_music.mp3 -i preview_and_main.mp3 \
  -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1[outa]" \
  -map "[outa]" output_with_intro.mp3
```

### Outro processing

```bash
THEME_OUTRO="$WORK_DIR/theme_outro.mp3"
OUTRO_DUR=15
OUTRO_FADE_IN=5
OUTRO_FADE_OUT=3
OUTRO_FADE_OUT_ST=$((OUTRO_DUR - OUTRO_FADE_OUT))

ffmpeg -i "$THEME_OUTRO" \
  -af "atrim=start=0:end=$OUTRO_DUR,asetpts=PTS-STARTPTS,afade=t=in:d=$OUTRO_FADE_IN,afade=t=out:st=$OUTRO_FADE_OUT_ST:d=$OUTRO_FADE_OUT" \
  -c:a libmp3lame -b:a 128k outro_music.mp3

ffmpeg -i main_content.mp3 -i outro_music.mp3 \
  -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1[outa]" \
  -map "[outa]" output_with_outro.mp3
```

### One-shot concat

```bash
# Intro music + teaser + main content + outro music
ffmpeg -i intro_music.mp3 -i preview.mp3 -i main.mp3 -i outro_music.mp3 \
  -filter_complex "[0:a][1:a][2:a][3:a]concat=n=4:v=0:a=1[outa]" \
  -map "[outa]" -c:a libmp3lame -q:a 2 \
  podcast_final.mp3
```

### Volume tweaks

```bash
# Lower volume to 80%
ffmpeg -i intro_music.mp3 -af "volume=0.8" intro_music_adjusted.mp3

# Raise to 120%
ffmpeg -i intro_music.mp3 -af "volume=1.2" intro_music_adjusted.mp3
```

---

## IV. Chapter timestamps

### Format

```
00:00 Intro teaser
01:30 Show open

02:05 Topic 1 title
07:58 Topic 2 title
26:16 Topic 3 title

1:00:08 Topic 4 title
1:23:00 Topic 5 title
```

### Format rules

| Rule | Description |
| --- | --- |
| Time format | `MM:SS` or `H:MM:SS` (when over 1h) |
| First chapter | `00:00 Intro teaser` (if there is one) |
| Spacing | Optional blank line between topic groups |
| Title length | Concise, ≤ 20 chars |

### Chapter identification

1. **Topic-shift markers** in the transcript.
2. **Signal phrases**: "next let's talk about…", "speaking of which…", "another topic is…", "lastly…".
3. **Granularity**:
   - ≤ 30 min → 3–5 chapters
   - 1 h → 5–8 chapters
   - 2 h → 8–12 chapters

---

## V. Title suggestions

### Title types

| Type | Example | When |
| --- | --- | --- |
| Question | "What does work mean?" | Provoke thought |
| Statement | "Work shouldn't define you" | Take a stand |
| Money-quote | "We're all looking for our own answer" | Emotional resonance |
| Topic | "On big tech, identity, and choice" | Information-dense |
| Hook | "The moment I stopped working for status" | Click-bait |

### Output format

```markdown
## Title suggestions

1. **What does work mean? Three "salarymen" reflect** (question + info)
2. **Why are we talking about meaning at work?** (question)
3. **Done with work defining me — now what?** (hook)
4. **Big tech, identity, choice — three honest takes** (topic)
5. **Two kinds of people, one answer: find your own work ethic** (money-quote)

Recommendation: #1 (information-dense and arresting).
```

---

## VI. Show notes

### Structure

```markdown
【Topic】
One sentence summary

【Hosts/guests】
- Name: one-line bio

【Timestamps】
(paste chapter timestamps)

【Highlights】
- "Money quote 1"
- "Money quote 2"

【About us】
Standing podcast description (user-supplied template)
```

---

## Progress checklist

```
- [ ] Ask user: intro music, outro music, cut audio, transcript
- [ ] Show default duration plan; wait for confirm/override
- [ ] Analyze content; recommend 5–8 highlights
- [ ] Wait for user to pick 3–4
- [ ] Extract and concatenate teaser
- [ ] Add intro music (confirmed duration, fade in/out)
- [ ] Add outro music (confirmed duration, fade in/out)
- [ ] Generate chapter timestamps
- [ ] Generate title suggestions
- [ ] Generate show notes
```

---

## Output files

```
theme_intro.mp3                # Intro music (local copy)
theme_outro.mp3                # Outro music (local copy)
podcast_preview.mp3            # Highlight teaser
podcast_intro.mp3              # Processed intro music (with fades)
podcast_outro.mp3              # Processed outro music (with fades)
podcast_final.mp3              # Final mix (intro + teaser + main + outro)
podcast_timestamps.txt         # Chapter timestamps
podcast_title_suggestions.txt  # Title options
podcast_show_notes.txt         # Show notes
```

---

## Relationship to other skills

```
/podcast-edit-cut    → Stages 1–5 + 8 (cut)
/podcast-edit-qa     → Stage 6 (qa)
/podcast-edit-polish → Stage 7 (this skill)
```

---

## VII. FFmpeg lessons learned ⭐

### 7.1 `-ss` is unreliable on MP3 — use `atrim`

```bash
# ❌ Wrong: -ss may seek to a silent frame
ffmpeg -i song.mp3 -ss 20 -t 3 output.mp3   # might be -91 dB silence

# ✅ Right: use the atrim filter
ffmpeg -i song.mp3 \
  -af "atrim=start=5:end=8,asetpts=PTS-STARTPTS" \
  output.mp3
```

### 7.2 Always check the output volume

```bash
ffmpeg -i output.mp3 -af "volumedetect" -f null - 2>&1 | grep max_volume
# Healthy: between -20 dB and 0 dB
```

### 7.3 Audio mixing — use `amerge+pan`, NOT `amix`

**⚠️ Critical: `amix` normalizes (divides by input count), severely attenuating the voice.**

```bash
# ❌ Wrong: amix halves voice volume
ffmpeg -i voice.mp3 -i bg_music.mp3 \
  -filter_complex "[0:a][1:a]amix=inputs=2:duration=first[out]" \
  -map "[out]" output.mp3

# ✅ Right: amerge + pan adds without attenuation
ffmpeg -i voice.wav -i bg_music.wav \
  -filter_complex "[0:a][1:a]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3[out]" \
  -map "[out]" -c:a pcm_s16le output.wav
```

When stacking multiple voice tracks, amerge+pan one at a time on top of a silent base — never chain amix.

### 7.4 `-ss` placement controls the filter time origin

**⚠️ When `-af` and `-ss` are both used, `-ss` MUST come before `-i`.**

```bash
# ❌ Wrong: -ss after -i means filters operate on the global timeline.
# afade fires at global time; by the time we reach the segment, the volume is 0.
ffmpeg -i source.wav -ss 10 -to 17 -af "afade=t=out:st=6.93:d=0.3" output.wav

# ✅ Right: -ss before -i; use -t (duration) instead of -to.
ffmpeg -ss 10 -i source.wav -t 7 -af "afade=t=out:st=6.7:d=0.3" output.wav
```

This bug once made `cut_audio.py` produce 55 minutes of silence except the first segment.

### 7.5 Continuous bed music — control volume with `volume=eval=frame`

**Don't stitch separate music clips between voice clips (creates jump cuts). Use one continuous music track and modulate the volume in time.**

```bash
# Single bed track that ducks during voice and recovers in transitions.
ffmpeg -i "$THEME_INTRO" \
  -af "atrim=start=0:end=89,asetpts=PTS-STARTPTS,\
afade=t=in:st=0:d=2,afade=t=out:st=86:d=3,\
volume=eval=frame:volume='if(lt(t,8),1.0,\
if(lt(t,10),1.0-(t-8)/2*(1.0-0.08),\
if(lt(t,17.36),0.08,\
if(lt(t,18.86),0.08+(t-17.36)/1.5*(1.0-0.08),\
if(lt(t,21.86),1.0,\
if(lt(t,23.36),1.0-(t-21.86)/1.5*(1.0-0.08),\
... ))))))'" \
  -c:a pcm_s16le music_bed.wav

# Then amerge+pan voice tracks on top (see 7.3)
```

Reference values:
- Voice gain: 2.0× (clearly above the bed)
- Bed during voice: 0.08 (audible but not intrusive)
- Bed in transitions: 1.0 (full volume)
- Volume ramps: 1.5 s in/out around voice
- Final ramp into main content: 9 s (1.5 s in, 2 s out)

### 7.6 Transition into main content

```bash
# 9-second transition: 1.5 s in, 2 s out
ffmpeg -i song.mp3 \
  -af "atrim=start=60:end=69,asetpts=PTS-STARTPTS,volume=0.5,afade=t=in:st=0:d=1.5,afade=t=out:st=7:d=2" \
  -c:a libmp3lame -b:a 128k music_to_content.mp3
```

---

## VIII. Full teaser structure (recommended)

### Core principle

**One continuous bed track for the entire intro region; voice is layered on top. Never stitch music clips — that creates jump cuts.**

### Layout

```
┌────────────── continuous bed music (one track, modulated) ──────────────┐
│ 🔊full   │ 🔉ducked │ 🔊full │ 🔉ducked │ 🔊full │ 🔉ducked │ 🔊fade  │
│ intro    │ clip 1   │ trans  │ clip 2   │ trans  │ clip 3   │ → main  │
│ (10 s)   │ voice 2× │ (5 s)  │ voice 2× │ (5 s)  │ voice 2× │ (9 s)   │
├──────────┼──────────┼────────┼──────────┼────────┼──────────┼─────────┤
│          │ +voice   │        │ +voice   │        │ +voice   │         │
└──────────┴──────────┴────────┴──────────┴────────┴──────────┴─────────┘
```

### One-shot mix (recommended)

```bash
# After picking highlight clips, mix everything in one step.
# Note: we use the intro music file as bed music for the teaser.
python3 "$SKILL_DIR/polish/scripts/mix_highlights_with_music.py" \
  --theme "$WORK_DIR/theme_intro.mp3" \
  --clips clip1.mp3 clip2.mp3 clip3.mp3 \
  --output "$WORK_DIR/intro_complete.wav"

# Tunable (sensible defaults):
#   --intro-dur 10        intro music-only duration
#   --gap-dur 5           between-clip transition length
#   --outro-dur 9         transition into main content
#   --music-vol 0.08      bed volume during voice (0–1)
#   --voice-gain 2.0      voice gain
#   --fade-transition 1.5 volume ramp on enter/leave voice
#   --theme-start 0       offset into the theme song
#
# The script:
# 1. Computes the timeline (intro → clips → transitions → outro)
# 2. Builds the continuous bed track (volume expression)
# 3. Layers voice (amerge+pan, no attenuation)
# 4. Mixes and emits a timeline JSON
```

### Volume reference

| Param | Value | Notes |
| --- | --- | --- |
| Voice gain | 2.0× | Voice clearly forward |
| Bed during voice | 8 % (0.08) | Atmospheric, doesn't mask voice |
| Bed in transitions | 100 % (1.0) | Full |
| Volume ramp | 1.5 s | Smooth in/out around voice |
| Between-clip transition | 5 s | Comfortable pacing |
| Into main content | 9 s | 1.5 s in + 2 s out |

### Final concat

**⚠️ The main content must end with a 3 s fade-out so main → outro feels natural.**

```bash
# 1. Add 3 s fade-out at the tail of main content
MAIN_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$MAIN_AUDIO")
FADE_START=$(echo "$MAIN_DUR - 3" | bc)
ffmpeg -y -i "$MAIN_AUDIO" \
  -af "afade=t=out:st=${FADE_START}:d=3" \
  -c:a pcm_s16le -ar 44100 -ac 2 "$WORK_DIR/main_content.wav"

# 2. Concat: intro (with bed+voice) + main (with tail fade) + outro
cat > "$WORK_DIR/concat_final.txt" << EOF
file 'intro_complete.wav'
file 'main_content.wav'
file 'outro_music.wav'
EOF

ffmpeg -f concat -safe 0 -i "$WORK_DIR/concat_final.txt" \
  -c:a libmp3lame -b:a 128k "$WORK_DIR/podcast_final.mp3"
```

---

## IX. Timestamp offset

When you prepend a teaser, every timestamp in the main content shifts by the teaser length.

### Calculation

```
offset = intro music + clip1 + transition + clip2 + transition + … + ramp
       = 15 + 10 + 4 + 20 + 8 = 57 s
```

### Example

| Original | After offset |
| --- | --- |
| 00:45 Show open | 00:57 Show open (+12 s) |
| 01:30 Topic 1 | 01:42 Topic 1 |
| 10:00 Topic 2 | 10:12 Topic 2 |

---

## X. Output organization

### Publishing folder

```bash
mkdir -p "$WORK_DIR/publish"

cp "$WORK_DIR/podcast_final.mp3"           "$WORK_DIR/publish/<podcast-name>_final.mp3"
cp "$WORK_DIR/podcast_timestamps.txt"      "$WORK_DIR/publish/"
cp "$WORK_DIR/podcast_title_suggestions.txt" "$WORK_DIR/publish/"
cp "$WORK_DIR/podcast_show_notes.txt"      "$WORK_DIR/publish/"
```

### Output structure

```
publish/
├── <podcast-name>_final.mp3       # Final audio
├── podcast_timestamps.txt         # Chapter timestamps (offset applied)
├── podcast_title_suggestions.txt  # Title options
└── podcast_show_notes.txt         # Show notes
```
