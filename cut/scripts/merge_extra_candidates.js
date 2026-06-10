#!/usr/bin/env node
/**
 * Stage 2.9 — Fold tic / backchannel / Gemini filler candidates into fine_analysis.json.
 *
 * Each detector emits its own confidence + enabled signal; we don't re-evaluate
 * them. We just append them with stable feIdx values so the review UI shows
 * them in the transcript and the safe-cut panel.
 *
 * Inputs (any subset, optional):
 *   2_analysis/fine_analysis.json
 *   2_analysis/speaker_tics.json            (type=speaker_tic)
 *   2_analysis/backchannel_candidates.json  (type=backchannel)
 *   2_analysis/gemini_filler_candidates.json (type=gemini_filler)
 *
 * Output:
 *   2_analysis/fine_analysis.json (in place; original backed to fine_analysis_pre_extra.json)
 *
 * Usage:
 *   node merge_extra_candidates.js --analysis-dir <2_analysis dir>
 */
const fs = require('fs');
const path = require('path');

const dirIdx = process.argv.indexOf('--analysis-dir');
const analysisDir = dirIdx >= 0 && process.argv[dirIdx + 1]
  ? path.resolve(process.argv[dirIdx + 1])
  : process.cwd();

const finePath = path.join(analysisDir, 'fine_analysis.json');
if (!fs.existsSync(finePath)) {
  console.error(`fine_analysis.json not found at ${finePath}`);
  process.exit(1);
}

const fine = JSON.parse(fs.readFileSync(finePath, 'utf8'));
const edits = fine.edits || [];

// Backup original (only on first run; subsequent merges keep the same backup)
const backupPath = path.join(analysisDir, 'fine_analysis_pre_extra.json');
if (!fs.existsSync(backupPath)) {
  fs.writeFileSync(backupPath, JSON.stringify(fine, null, 2));
  console.log(`📦 backed up original → ${path.basename(backupPath)}`);
}

// Strip prior extras so this script is idempotent
const EXTRA_TYPES = new Set(['speaker_tic', 'backchannel', 'gemini_filler']);
const baseEdits = edits.filter(e => !EXTRA_TYPES.has(e.type));
const strippedCount = edits.length - baseEdits.length;
if (strippedCount > 0) console.log(`🧹 stripped ${strippedCount} previous extras (idempotent merge)`);

const newEdits = [...baseEdits];
let nextIdx = newEdits.length;
const stats = { speaker_tic: 0, backchannel: 0, gemini_filler: 0 };

function maybeLoad(name) {
  const p = path.join(analysisDir, name);
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

const tics = maybeLoad('speaker_tics.json');
if (tics && Array.isArray(tics.candidates)) {
  for (const c of tics.candidates) {
    newEdits.push({
      idx: nextIdx++,
      sentenceIdx: c.sentenceIdx,
      type: 'speaker_tic',
      rule: '2.6-speaker tic',
      wordRange: [c.wordIdx, c.wordIdx],
      deleteText: c.deleteText,
      keepText: '',
      deleteStart: c.deleteStart,
      deleteEnd: c.deleteEnd,
      reason: c.reason,
      enabled: c.enabled,
      confidence: c.confidence,
      speaker: c.speaker,
      matched_tic: c.matched_tic,
    });
    stats.speaker_tic++;
  }
}

const bc = maybeLoad('backchannel_candidates.json');
if (bc && Array.isArray(bc.candidates)) {
  for (const c of bc.candidates) {
    newEdits.push({
      idx: nextIdx++,
      sentenceIdx: c.sentenceIdx,
      type: 'backchannel',
      rule: '2.7-multitrack backchannel',
      wordRange: [c.wordIdx, c.wordIdx],
      deleteText: c.deleteText,
      keepText: '',
      deleteStart: c.deleteStart,
      deleteEnd: c.deleteEnd,
      reason: c.reason,
      enabled: c.enabled,
      confidence: c.confidence,
      speaker: c.speaker,
      vad_coverage: c.vad_coverage,
    });
    stats.backchannel++;
  }
}

const gem = maybeLoad('gemini_filler_candidates.json');
if (gem && Array.isArray(gem.candidates)) {
  // Gemini estimates timestamps by ear (±0.3-1 s error). When the candidate
  // was matched to an ASR word (wordIdx), use that word's precise timestamps
  // instead, extended to the adjacent word boundaries like rule-based fillers
  // (see run_fine_analysis.js boundary extension / editing-rules 2-filler).
  // Unmatched candidates keep Gemini's coarse times but are forced to review.
  const MAX_EXTEND_GAP = 0.20;
  let actualWords = null;
  const wordsPath = path.join(analysisDir, '..', '1_transcript', 'subtitles_words.json');
  if (fs.existsSync(wordsPath)) {
    actualWords = JSON.parse(fs.readFileSync(wordsPath, 'utf8'))
      .filter(w => !w.isGap && !w.isSpeakerLabel);
  } else {
    console.warn(`⚠️  ${wordsPath} not found — gemini_filler timestamps NOT snapped to ASR words`);
  }
  let snapped = 0;
  let unmatched = 0;
  for (const c of gem.candidates) {
    let deleteStart = c.deleteStart;
    let deleteEnd = c.deleteEnd;
    let enabled = c.enabled;
    const hasWord = c.wordIdx !== null && c.wordIdx !== undefined
      && actualWords && actualWords[c.wordIdx];
    if (hasWord) {
      const w = actualWords[c.wordIdx];
      deleteStart = w.start;
      deleteEnd = w.end;
      const prev = actualWords[c.wordIdx - 1];
      if (prev) {
        const gap = deleteStart - prev.end;
        if (gap >= 0 && gap < MAX_EXTEND_GAP) deleteStart = prev.end;
      }
      const next = actualWords[c.wordIdx + 1];
      if (next) {
        const gap = next.start - deleteEnd;
        if (gap >= 0 && gap < MAX_EXTEND_GAP) deleteEnd = next.start;
      }
      snapped++;
    } else if (actualWords) {
      enabled = false; // no ASR anchor → never auto-delete on Gemini's clock alone
      unmatched++;
    }
    newEdits.push({
      idx: nextIdx++,
      sentenceIdx: null,  // Gemini doesn't map to sentenceIdx; UI handles by time
      type: 'gemini_filler',
      rule: `2.8-gemini ${c.category}`,
      wordRange: hasWord ? [c.wordIdx, c.wordIdx] : undefined,
      deleteText: c.deleteText,
      keepText: '',
      deleteStart: parseFloat(deleteStart.toFixed(3)),
      deleteEnd: parseFloat(deleteEnd.toFixed(3)),
      reason: c.reason,
      enabled,
      confidence: c.confidence,
      category: c.category,
    });
    stats.gemini_filler++;
  }
  if (actualWords) {
    console.log(`   gemini_filler timestamps: ${snapped} snapped to ASR words, `
      + `${unmatched} unmatched (forced review)`);
  }
}

// Re-sort by time + re-index
newEdits.sort((a, b) => {
  const aS = a.deleteStart ?? a.ds ?? 0;
  const bS = b.deleteStart ?? b.ds ?? 0;
  return aS - bS;
});
newEdits.forEach((e, i) => { e.idx = i; });

fine.edits = newEdits;
fine.summary = fine.summary || {};
fine.summary.totalEdits = newEdits.length;
fine.summary.extras = stats;

fs.writeFileSync(finePath, JSON.stringify(fine, null, 2));
console.log(`✅ merged: speaker_tic +${stats.speaker_tic}, backchannel +${stats.backchannel}, gemini_filler +${stats.gemini_filler}`);
console.log(`   total edits: ${newEdits.length}`);
