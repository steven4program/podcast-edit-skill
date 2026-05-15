#!/usr/bin/env node
/**
 * Step 6: generate enhanced review UI review_enhanced.html
 *
 * Read transcript and analysis data, inject into HTML template, produce review page with dynamic player.
 *
 * Usage: node generate_review_enhanced.js [options]
 *   --sentences   sentences.txt path       (default: sentences.txt)
 *   --words       subtitles_words.json path (default: ../1_transcript/subtitles_words.json)
 *   --analysis    semantic_deep_analysis.json path (default: semantic_deep_analysis.json)
 *   --fine        fine_analysis.json path   (default: fine_analysis.json, optional)
 *   --audio       audio file path          (default: 1_transcript/audio_seekable.mp3)
 *   --output      output HTML path           (default: ../review_enhanced.html)
 *   --title       Page title                 (default: Podcast review (editable))
 *
 * Key design:
 *   - word index uses actual_words (skipping isGap and isSpeakerLabel)
 *   - each sentence includes a words array (word-level timestamps, for manual edits)
 *   - fine edits precompute ds/de time ranges
 *   - dynamic player skips delete segments in real time
 */

const fs = require('fs');
const path = require('path');

// ===== Argument parsing =====
const args = {};
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace('--', '');
  args[key] = process.argv[i + 1];
}

const sentencesFile = args.sentences || 'sentences.txt';
const wordsFile = args.words || '../1_transcript/subtitles_words.json';
const analysisFile = args.analysis || 'semantic_deep_analysis.json';
const fineFile = args.fine || 'fine_analysis.json';
const audioSrc = args.audio || '1_transcript/audio_seekable.mp3';
// Optional second audio for the Source player (multitrack: raw amix without balance).
// Falls back to the same as audioSrc when not provided (single-track path).
const audioSrcRaw = args['audio-source-raw'] || audioSrc;
const outputFile = args.output || '../review_enhanced.html';
const title = args.title || 'Podcast review (editable)';

// ===== Template path =====
const scriptDir = path.dirname(process.argv[1] || __filename);
const templateFile = path.resolve(scriptDir, '../templates/review_enhanced.html');

// ===== File checks =====
function check(f, name) {
  if (!fs.existsSync(f)) {
    console.error(`❌ 找不到${name}：${f}`);
    process.exit(1);
  }
}
check(sentencesFile, 'sentence file');
check(wordsFile, 'word file');
check(analysisFile, 'semantic analysis');
check(templateFile, 'HTML template');

// ===== Load data =====
console.log('📖 載入資料...');
const sentences = fs.readFileSync(sentencesFile, 'utf8').split('\n').filter(l => l.trim());
const allWords = JSON.parse(fs.readFileSync(wordsFile, 'utf8'));
const analysis = JSON.parse(fs.readFileSync(analysisFile, 'utf8'));

let fineAnalysis = null;
if (fs.existsSync(fineFile)) {
  fineAnalysis = JSON.parse(fs.readFileSync(fineFile, 'utf8'));
  console.log(`   fine 分析：${fineAnalysis.edits.length} 個編輯`);
}

// ===== Build actual_words index (skip gaps and speaker labels) =====
const actualWords = allWords.filter(w => !w.isGap && !w.isSpeakerLabel);
console.log(`   總詞條：${allWords.length}，實際詞：${actualWords.length}，句子：${sentences.length}`);

// ===== Build deletion sets =====
const deletedSet = new Set();
const suggestedDeleteSet = new Set();  // suggested deletions (quality polish)
const blockMap = {};  // sentenceIdx → block info

if (analysis.sentences) {
  analysis.sentences.forEach(s => {
    if (s.action === 'delete') {
      deletedSet.add(s.sentenceIdx);
    } else if (s.action === 'suggest_delete') {
      suggestedDeleteSet.add(s.sentenceIdx);
    }
  });
}

if (analysis.blocks) {
  analysis.blocks.forEach(block => {
    for (let i = block.range[0]; i <= block.range[1]; i++) {
      blockMap[i] = block;
    }
  });
}

// ===== Build fine-edit map =====
const fineEditMap = {};  // sentenceIdx → [edit, edit, ...] (multiple edits per sentence supported)
if (fineAnalysis) {
  fineAnalysis.edits.forEach((edit, idx) => {
    edit._idx = idx;
    if (!fineEditMap[edit.sentenceIdx]) {
      fineEditMap[edit.sentenceIdx] = [];
    }
    fineEditMap[edit.sentenceIdx].push(edit);
  });
}

// ===== Building sentencesData =====
console.log('🔨 Building sentencesData...');
const sentencesData = [];

for (let i = 0; i < sentences.length; i++) {
  const parts = sentences[i].split('|');
  if (parts.length < 4) continue;

  const idx = parseInt(parts[0]);
  const [startWordIdx, endWordIdx] = parts[1].split('-').map(Number);
  const speaker = parts[2];
  const text = parts[3];

  // word-level timestamps (indexed into actual_words!)
  const wordsArr = [];
  for (let wi = startWordIdx; wi <= Math.min(endWordIdx, actualWords.length - 1); wi++) {
    const w = actualWords[wi];
    wordsArr.push({
      t: w.text,
      s: Math.round(w.start * 100) / 100,
      e: Math.round(w.end * 100) / 100
    });
  }

  const startTime = wordsArr.length > 0 ? wordsArr[0].s : 0;
  const totalSec = Math.floor(startTime);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const sec = totalSec % 60;
  const timeStr = h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
    : `${m}:${String(sec).padStart(2, '0')}`;

  const entry = {
    idx,
    speaker,
    text,
    startTime,
    endTime: 0,  // filled below
    timeStr,
    words: wordsArr,
    isAiDeleted: deletedSet.has(idx) || suggestedDeleteSet.has(idx),
    isSuggestedDelete: suggestedDeleteSet.has(idx)
  };

  // Deletion type
  if ((deletedSet.has(idx) || suggestedDeleteSet.has(idx)) && blockMap[idx]) {
    entry.deleteType = blockMap[idx].type;
    if (blockMap[idx].confidence === 'suggested') {
      entry.isSuggestedDelete = true;
    }
  }

  // Fine edits (multiple per sentence supported)
  const feList = fineEditMap[idx] || [];

  // Helper: build a single fineEdit entry
  function buildFeEntry(fe) {
    const feEntry = {
      idx: fe._idx,
      type: fe.type,
      deleteText: fe.deleteText || '',
      keepText: fe.keepText || '',
      reason: fe.reason || ''
    };

    if (fe.type === 'single_filler' || fe.type === 'residual_sentence') {
      feEntry.wholeSentence = true;
    }

    // Prefer fine_analysis's own ds/de (precise)
    if (fe.ds !== undefined && fe.de !== undefined) {
      feEntry.ds = Math.round(fe.ds * 100) / 100;
      feEntry.de = Math.round(fe.de * 100) / 100;
    } else if (fe.deleteStart !== undefined && fe.deleteEnd !== undefined) {
      // Silence edits use deleteStart/deleteEnd field names
      feEntry.ds = Math.round(fe.deleteStart * 100) / 100;
      feEntry.de = Math.round(fe.deleteEnd * 100) / 100;
    } else if (fe.deleteText && wordsArr.length > 0) {
      // Fallback: text match
      const wordTexts = wordsArr.map(w => w.t);
      const fullText = wordTexts.join('');
      const pos = fullText.indexOf(fe.deleteText);
      if (pos >= 0) {
        let charCount = 0, delStartWord = null, delEndWord = null;
        for (let wi = 0; wi < wordTexts.length; wi++) {
          const wEnd = charCount + wordTexts[wi].length;
          if (delStartWord === null && wEnd > pos) delStartWord = wi;
          if (wEnd >= pos + fe.deleteText.length) { delEndWord = wi; break; }
          charCount = wEnd;
        }
        if (delStartWord !== null && delEndWord !== null) {
          feEntry.ds = wordsArr[delStartWord].s;
          feEntry.de = wordsArr[delEndWord].e;
        }
      }
    }

    // Silence edits: read precise time from the matching gap element in allWords
    if (fe.type === 'silence' && feEntry.ds === undefined && fe.wordRange) {
      const gap = allWords[fe.wordRange[0]];
      if (gap) {
        feEntry.ds = Math.round(gap.start * 100) / 100;
        feEntry.de = Math.round(gap.end * 100) / 100;
      }
    }

    // Use wordRange to compute the precise charOffset
    if (fe.wordRange && fe.deleteText) {
      const relStart = fe.wordRange[0] - startWordIdx;
      if (relStart >= 0 && relStart < wordsArr.length) {
        let charOff = 0;
        for (let wi = 0; wi < relStart && wi < wordsArr.length; wi++) {
          charOff += wordsArr[wi].t.length;
        }
        feEntry.charOffset = charOff;
      }
    }

    // Pass through dependsOn for silence_merged dependency tracking
    if (fe.dependsOn) {
      feEntry.dependsOn = fe.dependsOn;
    }

    return feEntry;
  }

  if (feList.length > 0) {
    // Split: text edits (stutter/filler/etc.) vs silence edits
    const textEdits = feList.filter(fe => fe.type !== 'silence');
    const silenceEdits = feList.filter(fe => fe.type === 'silence');

    // fineEdit = the primary text edit (used by the front-end); fall back to first silence
    const primaryFe = textEdits.length > 0 ? textEdits[0] : silenceEdits[0];
    entry.fineEdit = buildFeEntry(primaryFe);

    // Extra silence edits (when the primary edit is not silence, append silences to the skip list)
    if (textEdits.length > 0 && silenceEdits.length > 0) {
      entry.extraSilences = silenceEdits.map(buildFeEntry);
    }

    // Extra text edits (when one sentence has multiple stutters)
    if (textEdits.length > 1) {
      entry.extraFineEdits = textEdits.slice(1).map(buildFeEntry);
    }
  }

  sentencesData.push(entry);
}

// Fill endTime (next sentence's startTime; for the last sentence, use the last word's end)
for (let i = 0; i < sentencesData.length; i++) {
  if (i + 1 < sentencesData.length) {
    sentencesData[i].endTime = sentencesData[i + 1].startTime;
  } else {
    const w = sentencesData[i].words;
    sentencesData[i].endTime = w.length > 0 ? w[w.length - 1].e : sentencesData[i].startTime + 1;
  }
}

// ===== Leading-pause marker (also surface trailing silences on the next sentence) =====
// During user review, the perceived pause is at the start of the next sentence,
// not the end of the previous one. So each silence — beyond being marked on
// prevSentence — is also forwarded as an incomingSilence on nextSentence.
const sentIdxToPos = {};
sentencesData.forEach((s, pos) => { sentIdxToPos[s.idx] = pos; });
if (fineAnalysis) {
  fineAnalysis.edits.forEach(edit => {
    if (edit.type !== 'silence') return;
    const curPos = sentIdxToPos[edit.sentenceIdx];
    if (curPos === undefined) return;
    // Find the next non-deleted sentence
    for (let np = curPos + 1; np < sentencesData.length; np++) {
      const nextS = sentencesData[np];
      if (!nextS.isAiDeleted) {
        if (!nextS.incomingSilences) nextS.incomingSilences = [];
        nextS.incomingSilences.push({
          idx: edit._idx,
          duration: edit.duration || parseFloat(((edit.deleteEnd || 0) - (edit.deleteStart || 0)).toFixed(1)),
          ds: Math.round((edit.deleteStart || 0) * 100) / 100,
          de: Math.round((edit.deleteEnd || 0) * 100) / 100,
          fromSentenceIdx: edit.sentenceIdx
        });
        break;
      }
    }
  });
}
const incomingCount = sentencesData.filter(s => s.incomingSilences).length;
if (incomingCount > 0) {
  console.log(`   首停頓標記：${incomingCount} 個句子`);
}

// ===== Stats =====
const totalSentences = sentencesData.length;
const deletedCount = sentencesData.filter(s => s.isAiDeleted && !s.isSuggestedDelete).length;
const suggestedCount = sentencesData.filter(s => s.isSuggestedDelete).length;
const fineEditCount = sentencesData.filter(s => s.fineEdit).length;
console.log(`   句子：${totalSentences}，確定刪除：${deletedCount}，建議刪除：${suggestedCount}，fine：${fineEditCount}`);

// ===== Build blocksData =====
const blocksDataArr = [];
if (analysis.blocks) {
  analysis.blocks.forEach(block => {
    const entry = {
      id: block.id,
      range: block.range,
      type: block.type,
      reason: block.reason || '',
      confidence: block.confidence || 'confirmed'
    };
    // Compute duration
    const startSent = sentencesData.find(s => s.idx === block.range[0]);
    const endSent = sentencesData.find(s => s.idx === block.range[1]);
    if (startSent && endSent) {
      const dur = Math.round(endSent.endTime - startSent.startTime);
      const dm = Math.floor(dur / 60);
      const ds = dur % 60;
      entry.duration = `${dm}:${String(ds).padStart(2, '0')}`;
    } else {
      entry.duration = '0:00';
    }
    blocksDataArr.push(entry);
  });
}

// ===== Build speaker styles + class mapping =====
const speakerColors = ['var(--blue)', 'var(--green)', 'var(--purple)', 'var(--orange, #d97706)', 'var(--red, #dc2626)'];
const uniqueSpeakers = [...new Set(sentencesData.map(s => s.speaker))];
const speakerStyles = uniqueSpeakers.map((sp, i) => {
  return `.s-speaker.sp-${i} { color: ${speakerColors[i % speakerColors.length]}; }`;
}).join('\n');
const speakerClassParts = uniqueSpeakers.map((sp, i) => {
  return `s.speaker === ${JSON.stringify(sp)} ? 'sp-${i}'`;
});
speakerClassParts.push(`'sp-0'`);
const speakerClassExpr = speakerClassParts.join(' : ');

// ===== Inject template =====
console.log('📝 產生 HTML...');
let template = fs.readFileSync(templateFile, 'utf8');

const dataJson = JSON.stringify(sentencesData);
template = template.replace('__SENTENCES_DATA__', dataJson);
template = template.replace('__BLOCKS_DATA__', JSON.stringify(blocksDataArr));
template = template.replace('__AI_DELETED_COUNT__', String(deletedCount + suggestedCount));
template = template.replace('__AI_SUGGESTED_COUNT__', String(suggestedCount));
template = template.replace('__TOTAL_SENTENCES__', String(totalSentences));
template = template.replace('__SPEAKER_STYLES__', speakerStyles);
template = template.replaceAll('__SPEAKER_CLASS_FUNC__', speakerClassExpr);
template = template.replace(/__AUDIO_SRC__/g, audioSrc);
template = template.replace(/__AUDIO_SRC_RAW__/g, audioSrcRaw);
template = template.replace(/__TITLE__/g, title);
template = template.replace('__GEN_TIMESTAMP__', String(Date.now()));

fs.writeFileSync(outputFile, template);
const sizeKB = Math.round(fs.statSync(outputFile).size / 1024);

console.log(`✅ Generated: ${outputFile} (${sizeKB}KB)`);
console.log(`   句子：${totalSentences}，AI 確定刪除：${deletedCount}，AI 建議刪除：${suggestedCount}，fine：${fineEditCount}`);
console.log(`   word-level 時間戳：${sentencesData.reduce((sum, s) => sum + s.words.length, 0)} 個`);
