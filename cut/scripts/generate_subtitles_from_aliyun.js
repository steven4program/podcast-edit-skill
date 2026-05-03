#!/usr/bin/env node
/**
 * Generate subtitles_words.json from an Aliyun FunASR transcription.
 *
 * Usage: node generate_subtitles_from_aliyun.js <aliyun_transcription.json> <speaker_mapping.json>
 *
 * speaker_mapping.json format: {"0": "Alice", "1": "Bob"}
 */

const fs = require('fs');

if (process.argv.length < 3) {
    console.error('❌ Error: missing argument');
    console.error('');
    console.error('Usage: node generate_subtitles_from_aliyun.js <aliyun_transcription.json> [speaker_mapping.json]');
    console.error('Example: node generate_subtitles_from_aliyun.js aliyun_funasr_transcription.json speaker_mapping.json');
    console.error('');
    console.error('Without speaker_mapping.json, falls back to: Speaker 0, Speaker 1, ...');
    process.exit(1);
}

const aliyunFile = process.argv[2];
const mappingFile = process.argv[3];

let aliyunData;
try {
    aliyunData = JSON.parse(fs.readFileSync(aliyunFile, 'utf8'));
} catch (error) {
    console.error(`❌ Failed to read file: ${aliyunFile}`);
    console.error(error.message);
    process.exit(1);
}

let speakerMapping = {};
if (mappingFile) {
    try {
        speakerMapping = JSON.parse(fs.readFileSync(mappingFile, 'utf8'));
        console.log('✅ Loaded speaker mapping:', speakerMapping);
    } catch (error) {
        console.warn(`⚠️  Could not read mapping file: ${mappingFile}, using defaults`);
    }
}

const sentences = aliyunData.transcripts[0].sentences;

console.log(`📝 Processing ${sentences.length} sentences...`);

const words = [];

sentences.forEach((sentence, idx) => {
    const speakerId = sentence.speaker_id;
    const speakerName = speakerMapping[speakerId] || `Speaker ${speakerId}`;

    // Speaker label as a synthetic "word"
    if (idx === 0 || sentences[idx - 1].speaker_id !== speakerId) {
        words.push({
            text: `[${speakerName}]`,
            start: sentence.begin_time / 1000,
            end: sentence.begin_time / 1000,
            isGap: false,
            isSpeakerLabel: true,
            speaker: speakerName
        });
    }

    sentence.words.forEach(word => {
        const text = word.text + (word.punctuation || '');
        words.push({
            text: text,
            start: word.begin_time / 1000,
            end: word.end_time / 1000,
            isGap: false,
            speaker: speakerName
        });
    });

    // Inter-sentence gap
    if (idx < sentences.length - 1) {
        const currentEnd = sentence.end_time / 1000;
        const nextStart = sentences[idx + 1].begin_time / 1000;
        const gap = nextStart - currentEnd;

        if (gap >= 0.5) {
            words.push({
                text: '',
                start: currentEnd,
                end: nextStart,
                isGap: true
            });
        }
    }
});

const outputFile = 'subtitles_words.json';
fs.writeFileSync(outputFile, JSON.stringify(words, null, 2));

console.log('✅ Generated:', outputFile);
console.log(`   Total words: ${words.filter(w => !w.isGap && !w.isSpeakerLabel).length}`);
console.log(`   Speaker labels: ${words.filter(w => w.isSpeakerLabel).length}`);
console.log(`   Gaps: ${words.filter(w => w.isGap).length}`);

const speakerCounts = {};
words.forEach(w => {
    if (w.speaker && !w.isSpeakerLabel && !w.isGap) {
        speakerCounts[w.speaker] = (speakerCounts[w.speaker] || 0) + 1;
    }
});

console.log('\n📊 Speaker word count:');
Object.keys(speakerCounts).sort().forEach(speaker => {
    const count = speakerCounts[speaker];
    const total = words.filter(w => !w.isGap && !w.isSpeakerLabel).length;
    const pct = (count / total * 100).toFixed(1);
    console.log(`   ${speaker}: ${count} words (${pct}%)`);
});
