#!/usr/bin/env node
/**
 * Helper to identify speakers — prints the first 20 sentences so the user
 * can map each speaker_id to a real name.
 *
 * Usage: node identify_speakers.js <aliyun_transcription.json>
 */

const fs = require('fs');

if (process.argv.length < 3) {
    console.error('❌ Error: missing argument');
    console.error('');
    console.error('Usage: node identify_speakers.js <aliyun_transcription.json>');
    console.error('Example: node identify_speakers.js aliyun_funasr_transcription.json');
    process.exit(1);
}

const aliyunFile = process.argv[2];

let data;
try {
    data = JSON.parse(fs.readFileSync(aliyunFile, 'utf8'));
} catch (error) {
    console.error(`❌ Failed to read file: ${aliyunFile}`);
    console.error(error.message);
    process.exit(1);
}

const sentences = data.transcripts[0].sentences;

console.log('='.repeat(80));
console.log('🎤 Speaker Identification Helper');
console.log('='.repeat(80));
console.log('');
console.log(`📊 Total sentences: ${sentences.length}`);

const speakerCounts = {};
sentences.forEach(s => {
    speakerCounts[s.speaker_id] = (speakerCounts[s.speaker_id] || 0) + 1;
});

console.log('\n📈 Speaker distribution:');
Object.keys(speakerCounts).sort((a, b) => a - b).forEach(spk => {
    const count = speakerCounts[spk];
    const pct = (count / sentences.length * 100).toFixed(1);
    console.log(`   Speaker ${spk}: ${count} sentences (${pct}%)`);
});

console.log('\n');
console.log('='.repeat(80));
console.log('🔍 First 20 sentences (use these to identify speakers)');
console.log('='.repeat(80));
console.log('');

sentences.slice(0, 20).forEach((s, i) => {
    const time = (s.begin_time / 1000).toFixed(1);
    const speaker = s.speaker_id;
    const text = s.text.length > 60 ? s.text.substring(0, 60) + '...' : s.text;

    console.log(`${(i + 1).toString().padStart(2)}. [Speaker ${speaker}] ${time}s`);
    console.log(`    ${text}`);
    console.log('');
});

console.log('='.repeat(80));
console.log('📝 How to create speaker_mapping.json');
console.log('='.repeat(80));
console.log('');
console.log('Look for self-introductions in the first 1–2 minutes, e.g.:');
console.log('  "I\'m the host Alice" → Speaker 0 = Alice');
console.log('  "I\'m Bob" → Speaker 1 = Bob');
console.log('');
console.log('Then create speaker_mapping.json:');
console.log('');
console.log('cat > speaker_mapping.json << EOF');
console.log('{');
console.log('  "0": "Alice",');
console.log('  "1": "Bob"');
console.log('}');
console.log('EOF');
console.log('');
console.log('⚠️  Adjust speaker_id and names to match your recording.');
console.log('');
