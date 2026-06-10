#!/usr/bin/env bash
# Full pipeline runner for kyle-ted35-full episode. Executes WhisperX + all
# detectors + safe planner + render, then opens the review UI.
set -e
export PYTHONIOENCODING=utf-8
B=output/2026-05-12_kyle-ted35-full/cut

t0=$SECONDS
log() { echo; echo "==== [$((SECONDS-t0))s] $* ===="; }

log "1/12 WhisperX transcribe + align (largest step — expect 25-35 min)"
python cut/scripts/transcribe_whisperx_multitrack.py \
  --track "kyle=$B/1_transcript/kyle_original.mp3" \
  --track "Ted=$B/1_transcript/Ted_original.mp3" \
  --output-dir "$B/1_transcript" --language zh --device cpu --compute-type int8

log "2/12 backchannel detect --apply"
python cut/scripts/detect_backchannel_multitrack.py "$B" --apply

log "3/12 generate sentences (Plan B per-speaker)"
( cd "$B/2_analysis" && node ../../../../cut/scripts/generate_sentences.js )

log "4/12 tic detector"
python cut/scripts/detect_speaker_tics.py "$B"

log "5/12 Gemini filler (LONG — expect 60-80 min on 1h audio)"
python cut/scripts/detect_fillers_gemini.py "$B"

log "6/12 rule layer (run_fine_analysis)"
( cd "$B/2_analysis" && node ../../../../cut/scripts/run_fine_analysis.js --analysis-dir . )

log "7/12 merge_llm_fine (LLM layer empty for this run)"
( cd "$B/2_analysis" && node ../../../../cut/scripts/merge_llm_fine.js --analysis-dir . )

log "8/12 merge extra candidates"
node cut/scripts/merge_extra_candidates.js --analysis-dir "$B/2_analysis"

log "9/12 reattach sentence idx"
python cut/scripts/reattach_sentence_idx.py "$B"

log "10/12 safe-cut planner"
python cut/scripts/safe_filler_cut.py --analysis-dir "$B/2_analysis" --audio "$B/1_transcript/audio.mp3"

log "11/12 build delete segments"
DUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$B/1_transcript/audio.mp3")
python cut/scripts/build_delete_segments.py "$B" "$DUR"

log "12/12 render mp3 (crossfade) + baseline"
( cd "$B/3_output" && \
  python ../../../../cut/scripts/cut_audio.py podcast_v2_tier12.mp3 \
    ../1_transcript/audio.mp3 ../2_analysis/delete_segments.json \
    --speakers-json ../1_transcript/subtitles_words.json )
( cd "$B/3_output" && \
  python ../../../../cut/scripts/cut_audio.py podcast_v1_butt_cut.mp3 \
    ../1_transcript/audio.mp3 ../2_analysis/delete_segments.json \
    --speakers-json ../1_transcript/subtitles_words.json --no-crossfade )

log "generate review UI"
( cd "$B/2_analysis" && \
  node ../../../../cut/scripts/generate_review_enhanced.js \
    --audio 1_transcript/audio_seekable.mp3 \
    --audio-source-raw 1_transcript/audio_seekable_raw.mp3 )

log "DONE — opening UI"
start "" "$(cygpath -w "$B/review_enhanced.html")"
echo
echo "Total wall time: $((SECONDS-t0)) seconds"
