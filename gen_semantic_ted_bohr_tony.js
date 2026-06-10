#!/usr/bin/env node
// Generate semantic_deep_analysis.json (rough-cut content analysis) for the
// 2026-06-08 ted/bohr/tony episode. Hand-authored block decisions by Claude
// after reading the full transcript.
const fs = require('fs');
const path = require('path');

const B = 'output/2026-06-08_ted-bohr-tony/cut';
const sentencesPath = path.join(B, '2_analysis/sentences.txt');
const wordsPath = path.join(B, '1_transcript/subtitles_words.json');
const outPath = path.join(B, '2_analysis/semantic_deep_analysis.json');

const sentLines = fs.readFileSync(sentencesPath, 'utf8').split('\n').filter(Boolean);
const sentences = sentLines.map(line => {
  const [idx, range, speaker, ...rest] = line.split('|');
  const [wStart, wEnd] = range.split('-').map(Number);
  return { idx: Number(idx), wStart, wEnd, speaker, text: rest.join('|') };
});

const allWords = JSON.parse(fs.readFileSync(wordsPath, 'utf8'));
const actualWords = allWords.filter(w => !w.isGap && !w.isSpeakerLabel);

function rangeTime(wStart, wEnd) {
  const a = actualWords[wStart], b = actualWords[wEnd] || actualWords[actualWords.length - 1];
  return { start: a ? a.start : 0, end: b ? b.end : 0 };
}
function fmt(sec) {
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

// --- Rough-cut delete blocks (sentence-index ranges, inclusive) ---
const blockDefs = [
  { type: 'pre_show', range: [0, 15],
    reason: '開錄前針對音量/聽眾 feedback 的閒聊與調整,正式開場在句16「嗨大家好歡迎來到今天的節目」' },
  { type: 'production_talk', range: [44, 44],
    reason: '主持人明說「我這段會剪掉…我也會把它剪成像大家直接回答」— 錄製中的後製討論' },
  { type: 'production_talk', range: [116, 116],
    reason: '協調誰先講 +「我到時候後製會注意」— 錄製中的後製討論' },
  { type: 'chit_chat', range: [211, 229],
    reason: '離題:Arizona/台積電移居、養小孩、生活成本與薪資閒聊;主持人自己喊「你扯太远了」「收回一下」後才回到正題' },
];

// Build a sentenceIdx -> blockId/type map
const delMap = new Map();
const blocks = blockDefs.map((b, i) => {
  const id = i + 1;
  for (let s = b.range[0]; s <= b.range[1]; s++) delMap.set(s, { id, type: b.type });
  const tStart = rangeTime(sentences[b.range[0]].wStart, sentences[b.range[0]].wStart).start;
  const tEnd = rangeTime(sentences[b.range[1]].wEnd, sentences[b.range[1]].wEnd).end;
  return {
    id, range: b.range, type: b.type, reason: b.reason,
    duration: fmt(Math.max(0, tEnd - tStart)),
  };
});

const outSentences = sentences.map(s => {
  const d = delMap.get(s.idx);
  if (d) return { sentenceIdx: s.idx, speaker: s.speaker, action: 'delete', blockId: d.id, type: d.type };
  return { sentenceIdx: s.idx, speaker: s.speaker, action: 'keep' };
});

const deleteCount = outSentences.filter(s => s.action === 'delete').length;
const totalEnd = actualWords.length ? actualWords[actualWords.length - 1].end : 0;
let delDur = 0;
for (const b of blockDefs) {
  const tStart = rangeTime(sentences[b.range[0]].wStart, sentences[b.range[0]].wStart).start;
  const tEnd = rangeTime(sentences[b.range[1]].wEnd, sentences[b.range[1]].wEnd).end;
  delDur += Math.max(0, tEnd - tStart);
}

const out = {
  version: '5.0',
  analysisType: 'two_level',
  totalDuration: `${fmt(totalEnd)} (${Math.round(totalEnd / 60)}min)`,
  targetDuration: `${fmt(totalEnd - delDur)}`,
  blocks,
  sentences: outSentences,
  summary: {
    totalSentences: sentences.length,
    deleteSentences: deleteCount,
    deleteBlocks: blocks.length,
    totalDeleteDuration: fmt(delDur),
    deleteRatio: `${(delDur / totalEnd * 100).toFixed(1)}%`,
  },
};

fs.writeFileSync(outPath, JSON.stringify(out, null, 2), 'utf8');
console.log('✅ wrote', outPath);
console.log('   total sentences:', sentences.length, '| delete:', deleteCount, 'in', blocks.length, 'blocks');
console.log('   delete duration:', fmt(delDur), `(${out.summary.deleteRatio})`, '| target:', out.targetDuration);
blocks.forEach(b => console.log(`   block ${b.id} [${b.type}] sent ${b.range[0]}-${b.range[1]} ~${b.duration}`));
