#!/usr/bin/env node
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || '/Users/kaiwei/side-projects/podcast-edit-skill/output/2026-05-05_ted-hogan-podcast/cut';
const sentencesPath = path.join(BASE, '2_analysis/sentences.txt');
const wordsPath = path.join(BASE, '1_transcript/subtitles_words.json');
const outPath = path.join(BASE, '2_analysis/semantic_deep_analysis.json');

const allWords = JSON.parse(fs.readFileSync(wordsPath, 'utf8'));
const actualWords = allWords.filter(w => !w.isGap && !w.isSpeakerLabel);

const lines = fs.readFileSync(sentencesPath, 'utf8').split('\n').filter(Boolean);
const sentences = lines.map(line => {
  const [idx, wordRange, speaker, text] = line.split('|');
  const [s, e] = wordRange.split('-').map(n => parseInt(n, 10));
  return { sentenceIdx: parseInt(idx, 10), wordStart: s, wordEnd: e, speaker, text };
});

const formatTime = sec => {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
};
const formatDur = sec => {
  if (sec >= 60) return `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
  return `${sec.toFixed(1)}s`;
};

const blocks = [
  {
    id: 1,
    range: [0, 2],
    type: 'pre_show',
    reason: '開錄前的身份/公司資訊確認，明顯非節目內容',
  },
  {
    id: 2,
    range: [56, 68],
    type: 'production_talk',
    reason: 'Ted 發現 Hogan 的電腦音效（cococo）被錄進來，要求重講並暫停播放電腦的東西；屬於錄音技術干擾與導播指示',
  },
];

const totalDuration = actualWords[actualWords.length - 1].end;

blocks.forEach(b => {
  const startWord = actualWords[sentences[b.range[0]].wordStart];
  const endWord = actualWords[sentences[b.range[1]].wordEnd];
  b.startTime = startWord.start;
  b.endTime = endWord.end;
  b.duration = formatDur(b.endTime - b.startTime);
  b.startTimeStr = formatTime(b.startTime);
  b.endTimeStr = formatTime(b.endTime);
});

const deleteSentenceMap = new Map();
blocks.forEach(b => {
  for (let i = b.range[0]; i <= b.range[1]; i++) {
    deleteSentenceMap.set(i, { blockId: b.id, type: b.type });
  }
});

const sentencesOut = sentences.map(s => {
  const d = deleteSentenceMap.get(s.sentenceIdx);
  if (d) return { sentenceIdx: s.sentenceIdx, speaker: s.speaker, action: 'delete', blockId: d.blockId, type: d.type };
  return { sentenceIdx: s.sentenceIdx, speaker: s.speaker, action: 'keep' };
});

const totalDeleteDur = blocks.reduce((sum, b) => sum + (b.endTime - b.startTime), 0);
const deleteRatio = ((totalDeleteDur / totalDuration) * 100).toFixed(1);

const out = {
  version: '5.0',
  analysisType: 'two_level',
  totalDuration: `${formatTime(totalDuration)} (${Math.round(totalDuration / 60)}min)`,
  targetDuration: 'quality-driven (no fixed target)',
  aggressiveness: 'conservative',
  blocks,
  sentences: sentencesOut,
  summary: {
    totalSentences: sentences.length,
    deleteSentences: sentencesOut.filter(s => s.action === 'delete').length,
    deleteBlocks: blocks.length,
    totalDeleteDuration: formatDur(totalDeleteDur),
    deleteRatio: `${deleteRatio}%`,
  },
};

fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
console.log(`✅ Wrote ${outPath}`);
console.log(`   Total: ${out.totalDuration}`);
console.log(`   Blocks: ${blocks.length}, sentences deleted: ${out.summary.deleteSentences}/${sentences.length}`);
console.log(`   Delete duration: ${out.summary.totalDeleteDuration} (${out.summary.deleteRatio})`);
blocks.forEach(b => {
  console.log(`   - Block ${b.id} [${b.type}] sentences ${b.range[0]}-${b.range[1]} (${b.startTimeStr}-${b.endTimeStr}, ${b.duration})`);
});
