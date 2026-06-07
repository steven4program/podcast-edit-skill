#!/usr/bin/env node
/**
 * Step 4: generate sentences.txt from subtitles_words.json.
 *
 * Plan B (per-speaker stream): split each speaker's stream INDEPENDENTLY into
 * sentences, then merge all sentences by start-time. Speaker switches no
 * longer force a sentence break — so backchannel/bleed words from one track
 * can be interleaved in the merged timeline without fragmenting the other
 * speaker's continuous sentence.
 *
 * Sentence break inside one speaker's stream:
 *   1. punctuation (。！？.!?)
 *   2. internal gap > MAX_GAP_S between consecutive words from THIS speaker
 *      (default 3.0 s — long enough to mean "they stopped talking", short enough
 *      to keep paragraphs separate)
 *
 * Words flagged isBackchannel (by detect_backchannel_multitrack --apply) are
 * skipped entirely — they're bleed from another track.
 *
 * Word-index contract preserved: indices in sentences.txt refer to positions
 * in actualWords = words.filter(w => !w.isGap && !w.isSpeakerLabel). The
 * splitter must NOT shift indices, so we use the original actualWords index
 * even when speakers are split internally.
 *
 * Usage: node generate_sentences.js [subtitles_words.json]
 * Output: sentences.txt   format: sentenceIdx|wordIdxRange|speaker|text
 */
const fs = require('fs');

const wordsFile = process.argv[2] || '../1_transcript/subtitles_words.json';
const MAX_GAP_S = 1.5;  // per-speaker pause that splits a sentence. Lowered from 3.0
                         // to 1.5 because WhisperX strips punctuation and natural
                         // podcast pauses rarely exceed 3s, leaving runs of 60+ seconds
                         // collapsed into one sentence. 1.5s is roughly "they stopped
                         // mid-thought" — short enough to catch sentence boundaries,
                         // long enough to avoid splitting on breath/word-finding pauses.

const MAX_WORD_DURATION_S = 2.0;  // wav2vec2 forced alignment failure mode:
                                   // when Whisper hallucinates a char over silence
                                   // (e.g. while the other speaker is talking), the
                                   // aligner stretches that char to cover the silence,
                                   // producing single chars with duration of seconds.
                                   // These are noise — SKIP the char AND force a
                                   // sentence break at its position (the gap was real,
                                   // it just got hidden behind the stretched char).

let words;
try {
  words = JSON.parse(fs.readFileSync(wordsFile, 'utf8'));
} catch (error) {
  console.error(`❌ Failed to read file: ${wordsFile}`);
  console.error(error.message);
  process.exit(1);
}

// Step 1a — collect real-word candidates (drop gaps/labels/backchannel).
const candidates = words.filter(w => !w.isGap && !w.isSpeakerLabel && !w.isBackchannel);
const skippedBackchannel = words.filter(w => w.isBackchannel).length;

// Step 1b — REPAIR stretched chars + their broken neighbours. wav2vec2 forced-
// alignment stretches a char to cover silence (e.g. 'l' in "Kyle" covering the
// 10-sec gap while the other speaker talks). The broken side-effect: the NEXT
// Latin char of the same speaker ('e') gets timestamped after the silence,
// effectively chopping the English word in half. Fix: detect Latin runs that
// contain any stretched char, then re-pack ALL chars in the run into a tight
// contiguous block starting from the run's first char's start time. The real
// 10-sec gap then moves to AFTER the English word, where MAX_GAP_S splits it.
const STRETCH_REPAIR_DUR_S = 0.10;
const isLatinChar = s => /^[A-Za-z]$/.test((s || '').trim());
let repairedStretched = 0;

// Build per-speaker Latin runs by scanning each speaker's stream
const byspeakerScan = {};
candidates.forEach((w, i) => {
  const s = w.speaker || 'unknown';
  if (!byspeakerScan[s]) byspeakerScan[s] = [];
  byspeakerScan[s].push({ origIdx: i, w });
});
const repairMap = new Map();   // origIdx → {start, end}
for (const stream of Object.values(byspeakerScan)) {
  let i = 0;
  while (i < stream.length) {
    if (!isLatinChar(stream[i].w.text)) { i++; continue; }
    // Find end of Latin run
    let j = i;
    while (j < stream.length && isLatinChar(stream[j].w.text)) j++;
    const run = stream.slice(i, j);
    const hasStretched = run.some(r => (r.w.end - r.w.start) > MAX_WORD_DURATION_S);
    if (hasStretched) {
      let t = run[0].w.start;
      for (const r of run) {
        repairMap.set(r.origIdx, { start: t, end: t + STRETCH_REPAIR_DUR_S });
        t += STRETCH_REPAIR_DUR_S;
      }
      repairedStretched += run.length;
    }
    i = j;
  }
}

const actualWords = candidates.map((w, i) => {
  if (repairMap.has(i)) {
    const r = repairMap.get(i);
    return { ...w, start: r.start, end: r.end, _stretchedRepaired: true };
  }
  // Catch any non-Latin stretched chars we missed (rare but possible)
  const dur = (w.end || 0) - (w.start || 0);
  if (dur > MAX_WORD_DURATION_S) {
    return { ...w, end: (w.start || 0) + STRETCH_REPAIR_DUR_S, _stretchedRepaired: true };
  }
  return w;
});
const skippedStretched = 0;
const stretchedAtIdx = new Set();

// Step 2 — group actualWords by speaker, preserving each word's
// actualWords-index. Per-speaker streams are time-ordered (they came from a
// time-merged source).
const byspeaker = new Map();   // speaker → [{actualIdx, w}, ...]
actualWords.forEach((w, i) => {
  const s = w.speaker || 'unknown';
  if (!byspeaker.has(s)) byspeaker.set(s, []);
  byspeaker.get(s).push({ actualIdx: i, w });
});

// Step 3 — split each speaker's stream into sentences (punct OR per-speaker gap)
const PUNCT = /[。！？.!?]/;
let allSentences = [];   // [{startIdx, endIdx, speaker, text, startTime}]
for (const [speaker, stream] of byspeaker) {
  let cur = null;
  let prevEnd = null;
  for (const { actualIdx, w } of stream) {
    const stretchedBreak = stretchedAtIdx.has(actualIdx);
    if (cur && (
        (prevEnd !== null && (w.start - prevEnd) > MAX_GAP_S) ||
        stretchedBreak
    )) {
      // Per-speaker gap or stretched-alignment artifact → close current sentence
      allSentences.push(cur);
      cur = null;
    }
    if (cur === null) {
      cur = {
        startIdx: actualIdx,
        endIdx: actualIdx,
        speaker,
        text: w.text,
        startTime: w.start,
        wordIdxs: [actualIdx],
      };
    } else {
      cur.endIdx = actualIdx;
      cur.text += w.text;
      cur.wordIdxs.push(actualIdx);
    }
    prevEnd = w.end;
    // Punctuation closes the sentence
    if (PUNCT.test(w.text)) {
      allSentences.push(cur);
      cur = null;
    }
  }
  if (cur) allSentences.push(cur);
}

// Step 4 — merge by startTime so the file reads in chronological order
allSentences.sort((a, b) => a.startTime - b.startTime);

// Step 5 — emit
// NOTE: wordIdxRange is non-contiguous in Plan B (because other speaker's words
// sit between this speaker's words in actualWords numbering). Downstream
// consumers should treat it as [min, max]; per-sentence word lookup must use
// the per-word speaker tag, not naive slicing. The existing splitting/merging
// code in this repo already iterates words by index and checks speaker, so the
// [min, max] contract still works.
const lines = allSentences.map((s, i) => {
  return `${i}|${s.startIdx}-${s.endIdx}|${s.speaker}|${s.text}`;
});

fs.writeFileSync('sentences.txt', lines.join('\n'));

console.log(`✅ Generated sentences.txt`);
console.log(`   Total sentences: ${lines.length}`);
console.log(`   Skipped (isBackchannel): ${skippedBackchannel}`);
console.log(`   Repaired (stretched alignment >${MAX_WORD_DURATION_S}s → re-packed at ${STRETCH_REPAIR_DUR_S}s/char): ${repairedStretched}`);
console.log(`   Per-speaker streams: ${byspeaker.size}`);

const speakerCounts = {};
allSentences.forEach(s => { speakerCounts[s.speaker] = (speakerCounts[s.speaker] || 0) + 1; });
console.log('\n📊 Speaker distribution:');
Object.keys(speakerCounts).sort().forEach(sp => {
  const c = speakerCounts[sp];
  const pct = (c / allSentences.length * 100).toFixed(1);
  console.log(`   ${sp}: ${c} (${pct}%)`);
});

console.log('\n📝 First 5 preview:');
allSentences.slice(0, 5).forEach((s, i) => {
  const preview = s.text.substring(0, 60);
  console.log(`${String(i).padStart(3)}. [${s.speaker}] ${preview}${s.text.length > 60 ? '...' : ''}`);
});
