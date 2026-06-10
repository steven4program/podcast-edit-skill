#!/usr/bin/env bash
# 3-track pipeline runner for the 2026-06-08 ted/bohr/tony episode.
# Adapted from run_full_pipeline.sh (validated 2-track) → 3 tracks.
# Expensive stages (prep, WhisperX, Gemini) are resumable: if their output
# already exists they are skipped, so a late failure doesn't redo hours.
set -e
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

REPO="$(pwd)"
B="output/2026-06-08_ted-bohr-tony/cut"

TED="$REPO/source/2026-06-08--ted35.wav"
BOHR="$REPO/source/2026-06-08--bohr.wav"
TONY="$REPO/source/2026-06-08--tony.wav"

t0=$SECONDS
log() { echo; echo "==== [$(( (SECONDS-t0)/60 ))m$(( (SECONDS-t0)%60 ))s] $* ===="; }

# ---------------------------------------------------------------------------
log "1/13 prep balanced review audio (audio.mp3 + seekable + raw)"
if [ -f "$B/1_transcript/audio_seekable_raw.mp3" ]; then
  echo "  ✓ already done, skipping"
else
  python cut/scripts/prep_review_audio_multitrack.py \
    --track "Ted=$TED" --track "Bohr=$BOHR" --track "Tony=$TONY" \
    --output-dir "$B/1_transcript" --balance equalize
fi

# ---------------------------------------------------------------------------
log "2/13 WhisperX transcribe + align  (LONGEST — expect 60-90 min on CPU)"
if [ -f "$B/1_transcript/subtitles_words.json" ]; then
  echo "  ✓ subtitles_words.json exists, skipping"
else
  python cut/scripts/transcribe_whisperx_multitrack.py \
    --track "Ted=$TED" --track "Bohr=$BOHR" --track "Tony=$TONY" \
    --output-dir "$B/1_transcript" --language zh --device cpu --compute-type int8
fi

# ---------------------------------------------------------------------------
log "3/13 backchannel / bleed detect --apply"
python cut/scripts/detect_backchannel_multitrack.py "$B" --apply

# ---------------------------------------------------------------------------
log "4/13 generate sentences (Plan B per-speaker)"
( cd "$B/2_analysis" && node "$REPO/cut/scripts/generate_sentences.js" )

# ---------------------------------------------------------------------------
log "5/13 speaker-tic detector"
python cut/scripts/detect_speaker_tics.py "$B"

# ---------------------------------------------------------------------------
log "6/13 Gemini filler detect  (LONG — expect 80-100 min on 73-min audio)"
if [ -f "$B/2_analysis/non_speech_vocals.json" ] || [ -f "$B/2_analysis/gemini_fillers.json" ]; then
  echo "  ✓ gemini output exists, skipping"
else
  python cut/scripts/detect_fillers_gemini.py "$B"
fi

# ---------------------------------------------------------------------------
log "7/13 rule layer (run_fine_analysis)"
( cd "$B/2_analysis" && node "$REPO/cut/scripts/run_fine_analysis.js" --analysis-dir . )

# ---------------------------------------------------------------------------
log "8/13 merge_llm_fine"
( cd "$B/2_analysis" && node "$REPO/cut/scripts/merge_llm_fine.js" --analysis-dir . )

# ---------------------------------------------------------------------------
log "9/13 merge extra candidates"
node cut/scripts/merge_extra_candidates.js --analysis-dir "$B/2_analysis"

# ---------------------------------------------------------------------------
log "10/13 reattach sentence idx"
python cut/scripts/reattach_sentence_idx.py "$B"

# ---------------------------------------------------------------------------
log "11/13 safe-cut planner"
python cut/scripts/safe_filler_cut.py --analysis-dir "$B/2_analysis" \
  --audio "$B/1_transcript/audio.mp3"

# ---------------------------------------------------------------------------
log "12/13 build delete segments"
DUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$B/1_transcript/audio.mp3")
python cut/scripts/build_delete_segments.py "$B" "$DUR"

# ---------------------------------------------------------------------------
log "13/13 preview auto-cut (merged mp3) + review UI"
( cd "$B/3_output" && \
  python "$REPO/cut/scripts/cut_audio.py" podcast_v2_tier12.mp3 \
    ../1_transcript/audio.mp3 ../2_analysis/delete_segments.json \
    --speakers-json ../1_transcript/subtitles_words.json )

( cd "$B/2_analysis" && \
  node "$REPO/cut/scripts/generate_review_enhanced.js" \
    --audio 1_transcript/audio_seekable.mp3 \
    --audio-source-raw 1_transcript/audio_seekable_raw.mp3 )

log "DONE"
echo "Total wall time: $(( (SECONDS-t0)/60 )) min"
echo "Review UI: $B/review_enhanced.html"
