#!/usr/bin/env node
/**
 * Step 4: generate sentences.txt from subtitles_words.json
 *
 * Usage: node generate_sentences.js [subtitles_words.json]
 * Output: sentences.txt
 *
 * Format: sentenceIdx|wordIdxRange|speaker|text
 */

const fs = require('fs');

const wordsFile = process.argv[2] || '../1_transcript/subtitles_words.json';
let words;

try {
  words = JSON.parse(fs.readFileSync(wordsFile, 'utf8'));
} catch (error) {
  console.error(`❌ Failed to read file: ${wordsFile}`);
  console.error(error.message);
  process.exit(1);
}

let sentences = [];
let currentSentence = { words: [], speaker: null, startIdx: 0 };
let wordIdx = 0;

words.forEach((w) => {
  if (w.isSpeakerLabel) {
    if (currentSentence.words.length > 0) {
      const text = currentSentence.words.map(w => w.text).join('');
      sentences.push(`${sentences.length}|${currentSentence.startIdx}-${wordIdx-1}|${currentSentence.speaker}|${text}`);
      currentSentence = { words: [], speaker: null, startIdx: wordIdx };
    }
    currentSentence.speaker = w.speaker;
  } else if (!w.isGap) {
    currentSentence.words.push(w);
    wordIdx++;
    // Sentence boundary: 。！？
    if (/[。！？.!?]/.test(w.text)) {
      const text = currentSentence.words.map(w => w.text).join('');
      sentences.push(`${sentences.length}|${currentSentence.startIdx}-${wordIdx-1}|${currentSentence.speaker}|${text}`);
      currentSentence = { words: [], speaker: currentSentence.speaker, startIdx: wordIdx };
    }
  }
});

if (currentSentence.words.length > 0) {
  const text = currentSentence.words.map(w => w.text).join('');
  sentences.push(`${sentences.length}|${currentSentence.startIdx}-${wordIdx-1}|${currentSentence.speaker}|${text}`);
}

fs.writeFileSync('sentences.txt', sentences.join('\n'));

console.log(`✅ Generated sentences.txt`);
console.log(`   Total sentences: ${sentences.length}`);

const speakerCounts = {};
sentences.forEach(line => {
  const speaker = line.split('|')[2];
  speakerCounts[speaker] = (speakerCounts[speaker] || 0) + 1;
});

console.log('\n📊 Speaker distribution:');
Object.keys(speakerCounts).sort().forEach(speaker => {
  const count = speakerCounts[speaker];
  const pct = (count / sentences.length * 100).toFixed(1);
  console.log(`   ${speaker}: ${count} (${pct}%)`);
});

console.log('\n📝 First 5 preview:');
sentences.slice(0, 5).forEach(line => {
  const parts = line.split('|');
  const text = parts[3].substring(0, 60);
  console.log(`${parts[0].padStart(3)}. [${parts[2]}] ${text}${parts[3].length > 60 ? '...' : ''}`);
});
