#!/usr/bin/env node
/**
 * Combine multiple noise_candidates.json files into one, deduplicating
 * overlapping ranges (same speaker, time-overlap > 50%). Keeps the entry with
 * the higher score and tags the merged class with both labels.
 *
 * Usage:
 *   node combine_noise_candidates.js \
 *     --in candidates_a.json --in candidates_b.json ... \
 *     --output combined.json
 */
const fs = require('fs');
const path = require('path');

const ins = [];
const out_args = {};
for (let i = 2; i < process.argv.length; i++) {
  const k = process.argv[i];
  if (k === '--in') ins.push(process.argv[++i]);
  else if (k.startsWith('--')) out_args[k.slice(2)] = process.argv[++i];
}
if (!out_args.output || ins.length === 0) {
  console.error('usage: --in <file> [--in ...] --output <file>');
  process.exit(1);
}

const all = [];
let duration = 0;
let audioFile = '';
for (const f of ins) {
  const d = JSON.parse(fs.readFileSync(f, 'utf8'));
  duration = Math.max(duration, d.duration || 0);
  audioFile = audioFile || d.audio_file || '';
  for (const c of d.candidates || []) all.push({ ...c, _source: d.model || path.basename(f) });
}

all.sort((a, b) => a.start - b.start);

const merged = [];
for (const c of all) {
  let absorbed = false;
  for (const m of merged) {
    if (m.speaker !== c.speaker) continue;
    const overlap = Math.max(0, Math.min(m.end, c.end) - Math.max(m.start, c.start));
    const shorter = Math.min(m.end - m.start, c.end - c.start);
    if (shorter > 0 && overlap / shorter > 0.5) {
      // Same event — merge
      m.start = Math.min(m.start, c.start);
      m.end = Math.max(m.end, c.end);
      if (c.score > m.score) {
        m.score = c.score;
        m.class = c.class + ' / ' + m.class;
      } else {
        m.class = m.class + ' / ' + c.class;
      }
      m._source = m._source + '+' + c._source;
      absorbed = true;
      break;
    }
  }
  if (!absorbed) merged.push({ ...c });
}

merged.sort((a, b) => a.start - b.start);

const out = {
  audio_file: audioFile,
  duration,
  model: 'combined',
  candidates: merged,
};
fs.mkdirSync(path.dirname(out_args.output), { recursive: true });
fs.writeFileSync(out_args.output, JSON.stringify(out, null, 2));
console.log(`combined ${all.length} candidates from ${ins.length} files → ${merged.length} unique → ${out_args.output}`);
