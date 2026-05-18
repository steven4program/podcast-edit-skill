#!/usr/bin/env node
/**
 * Fine analysis - RULES LAYER ONLY
 * Handles: silence detection, basic stutter detection (consecutive identical words)
 *
 * Semantic analysis (sentence-start fillers, self-correction, in-sentence repeats,
 * residual sentences, repeated sentences) is handled by the LLM layer.
 *
 * Usage: node run_fine_analysis.js [--analysis-dir DIR]
 *
 * Output: fine_analysis_rules.json (merged with LLM output by merge_llm_fine.js)
 */

const fs = require('fs');
const path = require('path');

// Parse args (same convention as merge_llm_fine.js)
let analysisDir = process.cwd();
const dirArgIdx = process.argv.indexOf('--analysis-dir');
if (dirArgIdx >= 0 && process.argv[dirArgIdx + 1]) {
  analysisDir = path.resolve(process.argv[dirArgIdx + 1]);
}

const wordsPath = path.join(analysisDir, '../1_transcript/subtitles_words.json');
const sentencesPath = path.join(analysisDir, 'sentences.txt');
const analysisPath = path.join(analysisDir, 'semantic_deep_analysis.json');
const outputPath = path.join(analysisDir, 'fine_analysis_rules.json');

const allWords = JSON.parse(fs.readFileSync(wordsPath, 'utf8'));
const sentenceLines = fs.readFileSync(sentencesPath, 'utf8').split('\n').filter(Boolean);
const analysis = JSON.parse(fs.readFileSync(analysisPath, 'utf8'));

// Get deleted sentence indices from 5a
const deletedSentences = new Set(
  analysis.sentences.filter(s => s.action === 'delete').map(s => s.sentenceIdx)
);

const actualWords = allWords.filter(w => !w.isGap && !w.isSpeakerLabel);
const gaps = allWords.filter(w => w.isGap);

// User preferences
const SILENCE_THRESHOLD = 0.8;
const SILENCE_CAP = 0.8;

// === Stage 2.5: Non-speech vocal events ===
const nsvPath = path.join(analysisDir, 'non_speech_vocals.json');
let nsvData = { events: [], degraded: true };
if (fs.existsSync(nsvPath)) {
  try {
    nsvData = JSON.parse(fs.readFileSync(nsvPath, 'utf8'));
  } catch (err) {
    console.warn(`⚠️  Failed to parse ${nsvPath}: ${err.message}`);
  }
}

function nsvTypeLabel(subtype) {
  return { throat_clear: '清喉嚨', nose_clear: '清鼻子' }[subtype] || subtype;
}

function findSentenceIdxForTime(time, sentences) {
  // Find first sentence whose [startTime, endTime] contains the time;
  // fallback to nearest by midpoint.
  for (const s of sentences) {
    if (time >= s.startTime && time <= s.endTime) return s.idx;
  }
  // Fallback: nearest sentence by midpoint
  let bestIdx = sentences.length > 0 ? sentences[0].idx : 0;
  let bestDist = Infinity;
  for (const s of sentences) {
    const mid = (s.startTime + s.endTime) / 2;
    const d = Math.abs(mid - time);
    if (d < bestDist) {
      bestDist = d;
      bestIdx = s.idx;
    }
  }
  return bestIdx;
}

// === Stutter exemption tiers ===

// Tier 1: reduplication-word allow-list — blanket exempt, never flag.
// Matched against simplified-Chinese FunASR output — keep entries simplified.
const REDUPLICATED_WORDS = new Set([
  '媽媽', '爸爸', '寶寶', '哥哥', '姐姐', '弟弟', '奶奶', '爺爺',
  '叔叔', '阿姨', '婆婆', '公公', '舅舅', '姑姑', '伯伯',
  '謝謝', '星星', '多多', '甜甜', '乖乖', '飯飯',
  '試試', '看看', '想想', '説説', '聊聊', '走走', '聽聽', '等等',
  '談談', '講講', '寫寫', '讀讀', '坐坐', '玩玩', '猜猜', '問問',
  '哈哈', '嘻嘻', '呵呵', '嘿嘿', '噗噗',
]);

// Tier 2: high-frequency words/phrases — NO blanket exemption anymore!
// Rules layer catches them ALL, marks needsReview=true for LLM to decide.
// "我我覺" → catch + needsReview (LLM will usually confirm delete)
// "就是就是" → catch + needsReview (LLM judges by context)
const MAYBE_NATURAL_REPEATS = new Set([
  '我', '你', '他', '她', '它', '就', '去', '不', '也', '都', '在', '又', '很', '太', '但', '還',
  '是', '有', '會', '能', '要', '想', '做', '説', '看', '來', '拉',
]);
const MAYBE_NATURAL_PHRASES = new Set([
  '就是', '怎麼', '真是', '真的', '然後', '可能', '其實', '應該', '還是', '這樣',
]);

// Tier 3: digit / number characters — blanket exempt (e.g. "2022" split into "2","0","2","2")
const NUMBER_CHARS = /^[\d一二三四五六七八九十百千万亿零两几多半]+$/;

// English word detection (avoid false positives on "OPEN"+"EN", "THIS"+"IS")
const ENGLISH_WORD = /^[A-Za-z]+$/;

// Parse sentences
const sentences = sentenceLines.map(line => {
  const parts = line.split('|');
  const [startIdx, endIdx] = parts[1].split('-').map(Number);
  return {
    idx: parseInt(parts[0]),
    wordRange: [startIdx, endIdx],
    speaker: parts[2],
    text: parts[3],
    words: actualWords.slice(startIdx, endIdx + 1),
    startTime: actualWords[startIdx] ? actualWords[startIdx].start : 0,
    endTime: actualWords[endIdx] ? actualWords[endIdx].end : 0,
  };
});

const edits = [];
let editIdx = 0;

function getNextSentenceStart(sentIdx) {
  for (let i = sentIdx + 1; i < sentences.length; i++) {
    return sentences[i].startTime;
  }
  return sentences[sentIdx].endTime;
}

// === RULE 0: Sentence-start filler detection (leading filler) ===
// Catches: 那，/ 嗯，/ 啊，/ 呃，/ 額，/ 哦，/ 噢，/ 那呀，/ 哎，/ 唉，/ 欸，
// These are the #1 edit type (146 in gold) and 100% deterministic.
// All entries match simplified-Chinese FunASR output — keep simplified.
const FILLER_START_SINGLES = ['嗯', '啊', '呃', '額', '哦', '噢', '哎', '唉', '欸', '唔'];
const FILLER_START_MULTI = ['那呀', '那那那', '嗯嗯嗯', '嗯嗯', '那那', '啊那'];
// "那" alone needs context check
const FILLER_START_AMBIGUOUS = ['那'];

for (const sent of sentences) {
  if (deletedSentences.has(sent.idx)) continue;
  const words = sent.words;
  if (words.length < 2) continue; // need filler + real content

  const w0 = words[0].text.replace(/[，。！？、：；]/g, '');
  if (!w0) continue;

  let fillerWordCount = 0;
  let fillerText = '';
  let isAmbiguous = false;

  // Check multi-word fillers first (longer match first)
  const first2 = words.length >= 2 ? (w0 + words[1].text.replace(/[，。！？、：；]/g, '')) : '';
  const first3 = words.length >= 3 ? (first2 + words[2].text.replace(/[，。！？、：；]/g, '')) : '';

  if (FILLER_START_MULTI.some(f => first3 === f) && words.length > 3) {
    fillerWordCount = 3;
    fillerText = words.slice(0, 3).map(w => w.text).join('');
  } else if (FILLER_START_MULTI.some(f => first2 === f) && words.length > 2) {
    fillerWordCount = 2;
    fillerText = words.slice(0, 2).map(w => w.text).join('');
  } else if (FILLER_START_SINGLES.includes(w0)) {
    fillerWordCount = 1;
    fillerText = words[0].text;
  } else if (FILLER_START_AMBIGUOUS.includes(w0)) {
    fillerWordCount = 1;
    fillerText = words[0].text;
    isAmbiguous = true;
  }

  if (fillerWordCount === 0) continue;

  // Check there's real content after the filler
  const remainingWords = words.slice(fillerWordCount);
  const remainingText = remainingWords.map(w => w.text.replace(/[，。！？、：；]/g, '')).join('');
  if (remainingText.length < 2) continue; // too short to be real content

  const deleteWords = words.slice(0, fillerWordCount);
  const globalStart = sent.wordRange[0];
  const globalEnd = sent.wordRange[0] + fillerWordCount - 1;

  edits.push({
    idx: editIdx++,
    sentenceIdx: sent.idx,
    type: 'filler_start',
    rule: '2-filler (leading)',
    wordRange: [globalStart, globalEnd],
    deleteText: fillerText,
    keepText: '',
    deleteStart: parseFloat(deleteWords[0].start.toFixed(2)),
    deleteEnd: parseFloat(deleteWords[deleteWords.length - 1].end.toFixed(2)),
    reason: `句首贅詞 "${w0}"，後接實質內容`,
    needsReview: isAmbiguous,
    reviewHint: isAmbiguous ? `句首 "那" 可能是回應也可能是口頭禪，請根據上下文判斷` : undefined,
    confidence: isAmbiguous ? 0.8 : 0.95
  });
}

// === RULE: Silence detection (>0.8s) ===
for (const gap of gaps) {
  const duration = gap.end - gap.start;
  if (duration <= SILENCE_THRESHOLD) continue;

  let sentIdx = -1;
  for (let i = 0; i < sentences.length; i++) {
    if (deletedSentences.has(i)) continue;
    const s = sentences[i];
    if (gap.start >= s.startTime - 0.5 && gap.start <= getNextSentenceStart(i) + 0.5) {
      sentIdx = i;
    }
  }
  if (sentIdx < 0 || deletedSentences.has(sentIdx)) continue;

  const deleteStart = gap.start + SILENCE_CAP;
  const deleteEnd = gap.end;
  if (deleteEnd - deleteStart < 0.1) continue;

  edits.push({
    idx: editIdx++,
    sentenceIdx: sentIdx,
    type: 'silence',
    rule: '3-silence segment processing',
    duration: parseFloat(duration.toFixed(1)),
    deleteStart: parseFloat(gap.start.toFixed(2)),
    deleteEnd: parseFloat(gap.end.toFixed(2)),
    keepDuration: SILENCE_CAP,
    reason: `靜音 ${duration.toFixed(1)}s，截到 ${SILENCE_CAP}s`
  });
}

// === RULE 1: Exact-match stutter detection (consecutive identical words) ===
// Design: catch ALL repeats, only blanket-exempt reduplication words and numbers.
// High-freq words/phrases: catch + needsReview=true → LLM decides.
for (const sent of sentences) {
  if (deletedSentences.has(sent.idx)) continue;
  const words = sent.words;

  for (let i = 0; i < words.length - 1; i++) {
    const curr = words[i].text;
    const next = words[i + 1].text;

    if (curr === next && curr.length >= 1) {
      // Count total consecutive repeats
      let endRepeat = i + 1;
      while (endRepeat + 1 < words.length && words[endRepeat + 1].text === curr) {
        endRepeat++;
      }
      const repeatCount = endRepeat - i + 1; // total occurrences
      const combined = curr + next;

      // === Blanket exemptions (never flag) ===
      if (REDUPLICATED_WORDS.has(combined)) { i = endRepeat; continue; }
      if (NUMBER_CHARS.test(curr)) { i = endRepeat; continue; }
      // ABB reduplication exemption: single character and previous word differs → "粉嘟嘟" pattern
      if (repeatCount === 2 && curr.length === 1 && i > 0 && words[i - 1].text !== curr) {
        i = endRepeat; continue;
      }

      // === Determine needsReview ===
      let needsReview = false;
      let reviewHint = '';
      if (repeatCount === 2) {
        if (curr.length === 1 && MAYBE_NATURAL_REPEATS.has(curr)) {
          needsReview = true;
          reviewHint = `single-character high-frequency word "${curr}" repeated twice; may be natural speech (e.g. answering "對對"); judge by context`;
        } else if (MAYBE_NATURAL_PHRASES.has(curr)) {
          needsReview = true;
          reviewHint = `高頻短語 "${curr}" 重複兩次，多數情況是卡頓，但例如 "怎麼怎麼做" 可能是修辭`;
        }
      }

      // Create one edit PER repeated word (not one for all).
      // This lets users individually toggle each repeat in the review page.
      // Delete words[i] through words[endRepeat-1], keep words[endRepeat] (last occurrence).
      for (let j = i; j < endRepeat; j++) {
        const globalIdx = sent.wordRange[0] + j;
        const edit = {
          idx: editIdx++,
          sentenceIdx: sent.idx,
          type: 'stutter',
          rule: '5-stutter',
          wordRange: [globalIdx, globalIdx],
          deleteText: words[j].text,
          keepText: curr,
          deleteStart: parseFloat(words[j].start.toFixed(2)),
          deleteEnd: parseFloat(words[j].end.toFixed(2)),
          reason: `"${curr}" 連續重複 ${repeatCount} 次，保留最後一次`
        };
        if (needsReview) {
          edit.needsReview = true;
          edit.reviewHint = reviewHint;
          edit.confidence = 0.7;
        }
        edits.push(edit);
      }

      i = endRepeat;
    }
  }
}

// === RULE 2: Suffix-match stutter detection (ASR word-boundary issue) ===
// e.g. "在這個" + "這個" → suffix "這個" duplicated
// e.g. "也开始" + "开始" → suffix "开始" duplicated
for (const sent of sentences) {
  if (deletedSentences.has(sent.idx)) continue;
  const words = sent.words;

  for (let i = 0; i < words.length - 1; i++) {
    const w1 = words[i].text.replace(/[，。！？、：；""''（）\s]/g, '');
    const w2 = words[i + 1].text.replace(/[，。！？、：；""''（）\s]/g, '');
    if (!w1 || !w2) continue;
    if (w1 === w2) continue; // already handled by exact match
    if (w1.length <= w2.length) continue; // w1 must be longer

    // Skip English words (avoid "OPEN"+"EN", "THIS"+"IS")
    if (ENGLISH_WORD.test(w1) || ENGLISH_WORD.test(w2)) continue;

    // Check if w1 ends with w2 and w2 is ≥2 chars
    if (w1.endsWith(w2) && w2.length >= 2) {
      const globalIdx = sent.wordRange[0] + i + 1;
      // Check no existing edit overlaps this word
      const alreadyCovered = edits.some(e =>
        e.sentenceIdx === sent.idx &&
        Math.max(words[i + 1].start, e.deleteStart || 0) < Math.min(words[i + 1].end, e.deleteEnd || 0)
      );
      if (alreadyCovered) continue;

      edits.push({
        idx: editIdx++,
        sentenceIdx: sent.idx,
        type: 'stutter',
        rule: '5-stutter (suffix match)',
        wordRange: [globalIdx, globalIdx],
        deleteText: w2,
        keepText: w2,
        deleteStart: parseFloat(words[i + 1].start.toFixed(2)),
        deleteEnd: parseFloat(words[i + 1].end.toFixed(2)),
        reason: `後綴匹配：「${w1}」末尾與「${w2}」重複`,
        needsReview: true,
        reviewHint: `ASR word-boundary issue: first word "${w1}" already contains "${w2}"; second word "${w2}" is the duplicate`,
        confidence: 0.8
      });
    }
  }
}

// === RULE 3: Mid-sentence filler detection (isolated mid-sentence fillers) ===
// Catches: 啊/呃/額/那/哦 appearing mid-sentence as hesitation fillers.
// Based on 2-filler-detect.md rules.
const MID_FILLERS = new Set(['啊', '呃', '額', '那', '哦']);

for (const sent of sentences) {
  if (deletedSentences.has(sent.idx)) continue;
  const words = sent.words;
  if (words.length < 3) continue; // need prev + filler + next

  for (let i = 1; i < words.length - 1; i++) {
    const w = words[i].text.replace(/[，。！？、：；]/g, '');
    if (!MID_FILLERS.has(w)) continue;

    // Skip if word is too long (>0.5s = emphasis/exclamation)
    const duration = words[i].end - words[i].start;
    if (duration > 0.5) continue;

    // Skip if already covered by another edit
    const alreadyCovered = edits.some(e =>
      e.sentenceIdx === sent.idx &&
      Math.abs(e.deleteStart - words[i].start) < 0.1
    );
    if (alreadyCovered) continue;

    const globalIdx = sent.wordRange[0] + i;

    // "那" needs extra care: skip if sentence is very short (likely a response "那，那")
    if (w === '那') {
      const realWords = words.filter(wd => wd.text.replace(/[，。！？、：；]/g, '').length > 0);
      if (realWords.length <= 3) continue; // short sentence — "那" is likely a response
    }

    edits.push({
      idx: editIdx++,
      sentenceIdx: sent.idx,
      type: 'stutter',
      rule: '2-filler (in-sentence)',
      wordRange: [globalIdx, globalIdx],
      deleteText: words[i].text,
      keepText: '',
      deleteStart: parseFloat(words[i].start.toFixed(2)),
      deleteEnd: parseFloat(words[i].end.toFixed(2)),
      reason: `句中贅詞 "${w}"，前後有實質內容，猶豫／換氣`,
      needsReview: w === '那', // "那" is more ambiguous than 啊/呃
      reviewHint: w === '那' ? 'in-sentence catchphrase "那"; please confirm it is not a conversational response' : undefined,
      confidence: w === '那' ? 0.7 : 0.9
    });
  }
}

// === RULE 4: Phrase-level repeat detection (phrase-level in-sentence repetition) ===
// Catches: "可以去可以去", "放到檯面來説，放到檯面上來説"
// Based on 6-in-sentence-repetition.md
//
// Strategy: catch broadly (≥3 chars), use confidence/needsReview to let the LLM decide.
// Only hard-filter pure function-word phrases (e.g. "的一個") that are always structural.
const FUNCTION_WORDS_ONLY = /^[的在是有一個這那些于和為也到上下中不麼什]+$/;

// Phrase prefix that's a Chinese conjunction/connector — repeated co-occurrence is usually
// "X 然後 A ... 然後 B" (parallel clauses) not phrase repetition. Without this guard, a phrase
// like "然後Code" matches twice in "然後Codex也會用然後CodePalette也會用" but they're parallel
// items, not stutter. (Pitfall 42, 2026-05.)
const CONJUNCTION_PREFIXES = [
  '然後', '然後', '所以', '但是', '可是', '不過', '不過',
  '就是', '因為', '因為', '而且', '或是', '或者', '還有', '還有',
];
function startsWithConjunction(phrase) {
  return CONJUNCTION_PREFIXES.some(p => phrase.startsWith(p));
}

for (const sent of sentences) {
  if (deletedSentences.has(sent.idx)) continue;
  const text = sent.text.replace(/[，。！？、：；""''（）\s]/g, '');
  if (text.length < 8) continue; // need at least 4+4

  let bestMatch = null;

  // Find longest repeating phrase (≥3 chars, search from long to short)
  for (let len = Math.min(Math.floor(text.length / 2), 20); len >= 3; len--) {
    for (let start = 0; start <= text.length - len * 2; start++) {
      const phrase = text.slice(start, start + len);

      // Skip if phrase is all same char (already caught by word-level stutter)
      if (new Set(phrase).size === 1) continue;

      // Hard filter: pure function-word phrases are always structural, not repeats
      if (FUNCTION_WORDS_ONLY.test(phrase)) continue;

      // Hard filter: conjunction-prefixed phrases — these usually mark parallel clauses, not stutter.
      // e.g. "然後Code" matching "然後Codex也會用然後CodePalette也會用" produces a giant false-positive delete.
      if (startsWithConjunction(phrase)) continue;

      const nextPos = text.indexOf(phrase, start + len);
      if (nextPos < 0) continue;

      // Gap between two occurrences should be small (≤ phrase length * 2)
      const gap = nextPos - start - len;
      if (gap > len * 2) continue;

      // Skip if this is a natural pattern (AABB, rhetorical repetition)
      // e.g. "越來越" is not a stutter
      if (len <= 3 && gap === 0) continue; // "越來越來" etc handled by word rules

      if (!bestMatch || len > bestMatch.len) {
        bestMatch = { phrase, start, nextPos, len, gap };
      }
    }
    if (bestMatch) break; // found longest match
  }

  if (!bestMatch) continue;

  // Map character positions back to word positions
  // We need to find the words that correspond to the first occurrence
  const origText = sent.text;
  let charCount = 0;
  const cleanToOrig = []; // maps clean-text index to original-text index
  for (let j = 0; j < origText.length; j++) {
    const c = origText[j];
    if (!/[，。！？、：；""''（）\s]/.test(c)) {
      cleanToOrig[charCount] = j;
      charCount++;
    }
  }

  // Find word indices for the delete range (first occurrence + gap)
  // deleteOrigStart = char position of the FIRST occurrence's first char
  // deleteOrigEnd   = char position of the char immediately BEFORE the second occurrence (inclusive)
  const deleteOrigStart = cleanToOrig[bestMatch.start];
  const deleteOrigEnd = cleanToOrig[bestMatch.nextPos - 1];
  if (deleteOrigStart == null || deleteOrigEnd == null) continue;

  // Find which words fall in the delete range.
  // Old bug: condition `wOrigStart <= deleteOrigStart && deleteWordStart < 0` always picked
  // word 0 (whose start is 0 ≤ anything), making every match span from sentence start.
  // Correct: deleteWordStart is the word that CONTAINS deleteOrigStart (its end > deleteOrigStart);
  //          deleteWordEnd is the last word whose start is ≤ deleteOrigEnd.
  let deleteWordStart = -1, deleteWordEnd = -1;
  let cumLen = 0;
  for (let wi = 0; wi < sent.words.length; wi++) {
    const wText = sent.words[wi].text;
    const wOrigStart = origText.indexOf(wText, cumLen);
    if (wOrigStart < 0) break; // text desync, abort mapping for safety
    const wOrigEnd = wOrigStart + wText.length;
    cumLen = wOrigEnd;

    if (deleteWordStart < 0 && wOrigEnd > deleteOrigStart) deleteWordStart = wi;
    if (wOrigStart <= deleteOrigEnd) deleteWordEnd = wi;
  }

  if (deleteWordStart < 0 || deleteWordEnd < 0 || deleteWordStart >= sent.words.length) continue;
  if (deleteWordEnd < deleteWordStart) continue;
  // Don't delete if it covers the whole sentence
  if (deleteWordStart === 0 && deleteWordEnd >= sent.words.length - 1) continue;

  // Check overlap with existing edits
  const overlapExists = edits.some(e =>
    e.sentenceIdx === sent.idx && e.type !== 'silence' &&
    e.deleteStart < sent.words[deleteWordEnd].end &&
    e.deleteEnd > sent.words[deleteWordStart].start
  );
  if (overlapExists) continue;

  const deleteWords = sent.words.slice(deleteWordStart, deleteWordEnd + 1);
  const deleteTextFull = deleteWords.map(w => w.text).join('');

  // Length sanity guard: a true phrase repetition deletes (len + gap) clean chars,
  // plus a small slack from word-boundary alignment. If the resulting deleteText is
  // dramatically longer, the word-mapping or phrase-search is off — skip rather than
  // emit an obviously-wrong giant delete.
  const expectedCleanLen = bestMatch.len + bestMatch.gap;
  const deleteCleanLen = deleteTextFull.replace(/[，。！？、：；""''（）\s]/g, '').length;
  if (deleteCleanLen > expectedCleanLen + 4) continue;

  // Confidence heuristics:
  // - Short phrase (3-4 chars) or large gap → more likely natural → lower confidence
  // - Long phrase (≥5 chars) with small gap → almost certainly oral stutter → higher confidence
  // - Phrase starting with a structural particle (的/和) → likely structural → needsReview
  const isShort = bestMatch.len <= 4;
  const isLargeGap = bestMatch.gap > bestMatch.len;
  const startsWithParticle = ['的', '和'].includes(bestMatch.phrase[0]);
  const isHighConf = bestMatch.len >= 5 && bestMatch.gap <= 3 && !startsWithParticle;

  edits.push({
    idx: editIdx++,
    sentenceIdx: sent.idx,
    type: 'in_sentence_repeat',
    rule: '6-in-sentence repetition (phrase level)',
    wordRange: [sent.wordRange[0] + deleteWordStart, sent.wordRange[0] + deleteWordEnd],
    deleteText: deleteTextFull,
    keepText: bestMatch.phrase,
    deleteStart: parseFloat(deleteWords[0].start.toFixed(2)),
    deleteEnd: parseFloat(deleteWords[deleteWords.length - 1].end.toFixed(2)),
    reason: `短語 "${bestMatch.phrase}" 重複，刪除第一次與間隔（gap ${bestMatch.gap} 字）`,
    needsReview: !isHighConf,
    reviewHint: isHighConf ? undefined : `短語 "${bestMatch.phrase}" 出現兩次（gap ${bestMatch.gap} 字），可能是口誤重複也可能是並列／強調結構，請根據語境判斷`,
    confidence: isHighConf ? 0.9 : (isShort || startsWithParticle ? 0.5 : 0.7)
  });
}

// === RULE 5: Consecutive filler detection ===
// Catches: "嗯啊", "呃啊", "嗯嗯嗯", "這個這個這個" (≥3 consecutive filler words)
const CONSEC_FILLERS = new Set(['嗯', '啊', '呃', '額', '哦', '噢', '唔', '這個', '就是', '然後']);

for (const sent of sentences) {
  if (deletedSentences.has(sent.idx)) continue;
  const words = sent.words;
  if (words.length < 2) continue;

  for (let i = 0; i < words.length - 1; i++) {
    const w1 = words[i].text.replace(/[，。！？、：；]/g, '');
    const w2 = words[i + 1].text.replace(/[，。！？、：；]/g, '');

    // Both must be filler words and different (same = handled by stutter rule)
    if (!CONSEC_FILLERS.has(w1) || !CONSEC_FILLERS.has(w2)) continue;
    if (w1 === w2) continue; // handled by exact-match stutter

    // Extend the run
    let runEnd = i + 1;
    while (runEnd + 1 < words.length) {
      const wn = words[runEnd + 1].text.replace(/[，。！？、：；]/g, '');
      if (CONSEC_FILLERS.has(wn)) { runEnd++; } else break;
    }

    // Check no overlap with existing edits
    const overlapExists = edits.some(e =>
      e.sentenceIdx === sent.idx && e.type !== 'silence' &&
      e.deleteStart < words[runEnd].end && e.deleteEnd > words[i].start
    );
    if (overlapExists) { i = runEnd; continue; }

    const deleteWords = words.slice(i, runEnd + 1);
    const deleteText = deleteWords.map(w => w.text).join('');

    edits.push({
      idx: editIdx++,
      sentenceIdx: sent.idx,
      type: 'consecutive_filler',
      rule: '7-consecutive filler',
      wordRange: [sent.wordRange[0] + i, sent.wordRange[0] + runEnd],
      deleteText,
      keepText: '',
      deleteStart: parseFloat(deleteWords[0].start.toFixed(2)),
      deleteEnd: parseFloat(deleteWords[deleteWords.length - 1].end.toFixed(2)),
      reason: `連續贅詞 "${deleteText}"`,
      needsReview: false,
      confidence: 0.95
    });

    i = runEnd; // skip past this run
  }
}

// === RULE: Restart marker detection (A + restart signal + A) ===
// Pattern: speaker says something, then "等一下"/"重來" etc., then repeats.
// Delete the first occurrence + restart marker.
const RESTART_MARKERS = new Set([
  '等一下', '重來', '再説一遍', '再來', '重新説', '重新來',
  '等等', '不對', '説錯', '我重説', '再來一遍',
]);

for (const sent of sentences) {
  if (deletedSentences.has(sent.idx)) continue;
  const words = sent.words;
  if (words.length < 4) continue; // need at least: A marker A

  for (let m = 1; m < words.length - 1; m++) {
    // Check single-word and two-word markers
    let markerLen = 0;
    const w1 = words[m].text.replace(/[，。！？、]/g, '');
    const w2 = m + 1 < words.length ? (w1 + words[m + 1].text.replace(/[，。！？、]/g, '')) : '';

    if (RESTART_MARKERS.has(w2) && m + 1 < words.length - 1) {
      markerLen = 2;
    } else if (RESTART_MARKERS.has(w1)) {
      markerLen = 1;
    }
    if (markerLen === 0) continue;

    // Found a restart marker at position m (length markerLen)
    // Compare text before marker vs text after marker
    const beforeStart = 0;
    const beforeEnd = m; // exclusive
    const afterStart = m + markerLen;

    if (afterStart >= words.length) continue;

    // Get text snippets (first N words before and after marker)
    const compareLen = Math.min(beforeEnd - beforeStart, words.length - afterStart, 5);
    if (compareLen < 1) continue;

    const beforeText = words.slice(beforeEnd - compareLen, beforeEnd)
      .map(w => w.text.replace(/[，。！？、]/g, '')).join('');
    const afterText = words.slice(afterStart, afterStart + compareLen)
      .map(w => w.text.replace(/[，。！？、]/g, '')).join('');

    // Check similarity: at least 60% character overlap
    const overlap = [...beforeText].filter(c => afterText.includes(c)).length;
    const similarity = overlap / Math.max(beforeText.length, 1);

    if (similarity >= 0.6) {
      const deleteWords = words.slice(beforeStart, afterStart);
      const deleteText = deleteWords.map(w => w.text).join('');
      const keepWords = words.slice(afterStart);
      const keepText = keepWords.map(w => w.text).join('');

      // Check for duplicate with existing stutter edits
      const dupExists = edits.some(e =>
        e.sentenceIdx === sent.idx &&
        Math.abs(e.deleteStart - deleteWords[0].start) < 0.1
      );
      if (dupExists) continue;

      edits.push({
        idx: editIdx++,
        sentenceIdx: sent.idx,
        type: 'self_correction',
        rule: '8-self-correction(restart-marker)',
        wordRange: [sent.wordRange[0] + beforeStart, sent.wordRange[0] + afterStart - 1],
        deleteText,
        keepText,
        deleteStart: parseFloat(deleteWords[0].start.toFixed(2)),
        deleteEnd: parseFloat(deleteWords[deleteWords.length - 1].end.toFixed(2)),
        reason: `重啟訊號 "${words.slice(m, m + markerLen).map(w => w.text).join('')}" 前後文字相似（${(similarity * 100).toFixed(0)}%），刪除第一遍與訊號詞`
      });
      break; // one restart per sentence
    }
  }
}

// === Boundary extension for non-silence edits ===
// ASR timestamps have onset leaking (actual sound starts before reported .start)
// and there's often a gap between filler.end and next_word.start.
// Extend to [prev_word.end, next_word.start] for clean cuts without plosives.
// See MEMORY.md: "filler delete range must extend to adjacent word boundaries"
const MAX_EXTEND_GAP = 0.20; // only extend if gap < 200ms (avoid eating real pauses)

for (const e of edits) {
  if (e.type === 'silence') continue; // silence boundaries are already precise
  if (!e.wordRange) continue;

  const sent = sentences.find(s => s.idx === e.sentenceIdx);
  if (!sent || !sent.words) continue;

  const localStart = e.wordRange[0] - sent.wordRange[0];
  const localEnd = e.wordRange[1] - sent.wordRange[0];

  // Extend start: snap to prev word's end (if close enough)
  if (localStart > 0) {
    const prevWord = sent.words[localStart - 1];
    const gap = e.deleteStart - prevWord.end;
    if (gap >= 0 && gap < MAX_EXTEND_GAP) {
      e.deleteStart = parseFloat(prevWord.end.toFixed(2));
    }
  }

  // Extend end: snap to next word's start (if close enough)
  if (localEnd < sent.words.length - 1) {
    const nextWord = sent.words[localEnd + 1];
    const gap = nextWord.start - e.deleteEnd;
    if (gap >= 0 && gap < MAX_EXTEND_GAP) {
      e.deleteEnd = parseFloat(nextWord.start.toFixed(2));
    }
  }
}

// === Emit NSV events as fine edits ===
for (const ev of (nsvData.events || [])) {
  // Use refined boundaries if present, else raw
  const dStart = (ev.refined_start ?? ev.start);
  const dEnd = (ev.refined_end ?? ev.end);
  const sentenceIdx = findSentenceIdxForTime(dStart, sentences);

  edits.push({
    idx: editIdx++,
    sentenceIdx,
    type: 'non_speech_vocal',
    subtype: ev.type,                                  // 'throat_clear' | 'nose_clear'
    rule: '11-non-speech-vocal',
    wordRange: null,                                    // NSV is between/near words
    deleteText: '',                                     // no transcript text consumed
    keepText: '',
    deleteStart: parseFloat(dStart.toFixed(2)),
    deleteEnd: parseFloat(dEnd.toFixed(2)),
    reason: `${nsvTypeLabel(ev.type)}（Gemini 偵測）${ev.description ? '：' + ev.description : ''}`,
    needsReview: true,
    enabled: false,                                     // default: don't delete; user confirms
    confidence: ev.confidence,
    source: 'gemini',
    nsv_id: ev.id,
    description: ev.description,
    filter_decision: ev.filter_decision,
  });
}

// === Cross-ref: upgrade filler edits that overlap an NSV event ===
const FILLER_WORDS = new Set(['嗯', '啊', '呃', '對', '哎', '欸', '哦', '噢', '額', '唉']);

const nsvIntervals = (nsvData.events || []).map(ev => ({
  start: (ev.refined_start ?? ev.start),
  end: (ev.refined_end ?? ev.end),
  subtype: ev.type,
  id: ev.id,
}));

for (const e of edits) {
  if (e.type !== 'single_filler' && e.type !== 'consecutive_filler') continue;
  if (!FILLER_WORDS.has((e.deleteText || '').trim())) continue;
  // Find any NSV that overlaps this edit's delete range
  const match = nsvIntervals.find(n =>
    n.start < e.deleteEnd && n.end > e.deleteStart
  );
  if (!match) continue;
  e.enabled = true;
  e.needsReview = false;          // override is decisive
  e.nsvOverride = { nsv_id: match.id, subtype: match.subtype };
  e.reason = `${e.reason}（Gemini 偵測到此處實為${nsvTypeLabel(match.subtype)}，自動勾選刪除）`;
}

// Sort edits by time
edits.sort((a, b) => a.deleteStart - b.deleteStart);
edits.forEach((e, i) => e.idx = i);

// Summary
const byType = {};
let needsReviewCount = 0;
for (const e of edits) {
  byType[e.type] = (byType[e.type] || 0) + 1;
  if (e.needsReview) needsReviewCount++;
}

const totalTimeSaved = edits.reduce((sum, e) => {
  if (e.type === 'silence') {
    return sum + (e.duration - e.keepDuration);
  }
  return sum + (e.deleteEnd - e.deleteStart);
}, 0);

const result = {
  edits,
  summary: {
    totalEdits: edits.length,
    needsReview: needsReviewCount,
    byType,
    estimatedTimeSaved: `${Math.floor(totalTimeSaved / 60)}:${String(Math.floor(totalTimeSaved % 60)).padStart(2, '0')}`
  }
};

fs.writeFileSync(outputPath, JSON.stringify(result, null, 2));
console.log(`✅ Rules layer complete: ${outputPath}`);
console.log(`   Total edits: ${edits.length} (${needsReviewCount} needsReview → LLM decides)`);
console.log(`   By type:`, JSON.stringify(byType));
console.log(`   Estimated time saved: ${result.summary.estimatedTimeSaved}`);

// Show needsReview items for visibility
if (needsReviewCount > 0) {
  console.log(`\n   🔍 needsReview items (LLM will decide):`);
  edits.filter(e => e.needsReview).forEach(e => {
    console.log(`      S${e.sentenceIdx}: "${e.deleteText}" — ${e.reviewHint}`);
  });
}
