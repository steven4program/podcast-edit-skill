#!/usr/bin/env bash
# Re-run transcribe + downstream after sentence-split fix. Reuses existing
# gemini_filler_candidates.json (audio didn't change, results still valid).
set -e
export PYTHONIOENCODING=utf-8
B=output/2026-05-12_kyle-ted35-full/cut
t0=$SECONDS
log() { echo; echo "==== [$((SECONDS-t0))s] $* ===="; }

log "1/9 WhisperX re-transcribe with punctuation restoration"
python cut/scripts/transcribe_whisperx_multitrack.py \
  --track "kyle=$B/1_transcript/kyle_original.mp3" \
  --track "Ted=$B/1_transcript/Ted_original.mp3" \
  --output-dir "$B/1_transcript" --language zh --device cpu --compute-type int8

log "2/9 backchannel --apply"
python cut/scripts/detect_backchannel_multitrack.py "$B" --apply

log "3/9 generate sentences (MAX_GAP_S=1.5 + punctuation)"
( cd "$B/2_analysis" && node ../../../../cut/scripts/generate_sentences.js )

log "4/9 tic detector"
python cut/scripts/detect_speaker_tics.py "$B"

log "5/9 rule layer"
( cd "$B/2_analysis" && node ../../../../cut/scripts/run_fine_analysis.js --analysis-dir . )

log "6/9 merge_llm_fine"
( cd "$B/2_analysis" && node ../../../../cut/scripts/merge_llm_fine.js --analysis-dir . )

log "7/9 merge extras (reuses existing Gemini results)"
node cut/scripts/merge_extra_candidates.js --analysis-dir "$B/2_analysis"

log "8/9 reattach sentence idx + safe planner"
python cut/scripts/reattach_sentence_idx.py "$B"
python cut/scripts/safe_filler_cut.py --analysis-dir "$B/2_analysis" --audio "$B/1_transcript/audio.mp3"

log "9/9 build delete + render + UI"
DUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$B/1_transcript/audio.mp3")
python cut/scripts/build_delete_segments.py "$B" "$DUR"
( cd "$B/3_output" && \
  python ../../../../cut/scripts/cut_audio.py podcast_v2_tier12.mp3 \
    ../1_transcript/audio.mp3 ../2_analysis/delete_segments.json \
    --speakers-json ../1_transcript/subtitles_words.json )
( cd "$B/2_analysis" && \
  node ../../../../cut/scripts/generate_review_enhanced.js \
    --audio 1_transcript/audio_seekable.mp3 \
    --audio-source-raw 1_transcript/audio_seekable_raw.mp3 )

log "DONE — opening UI"
start "" "$(cygpath -w "$B/review_enhanced.html")"
echo
echo "Total wall time: $((SECONDS-t0)) seconds"
