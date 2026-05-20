#!/usr/bin/env node
/**
 * Merge confirmed noise events from one or more noise_events.json files into an
 * existing delete_segments.json. Confirmed events (with verdict="confirmed" and
 * is_speech_overlapped=false) are added as new delete ranges; overlapping
 * ranges are merged.
 *
 * Usage:
 *   node merge_noise_into_deletes.js \
 *     --deletes path/to/delete_segments_edited.json \
 *     --noise path/to/hogan_events.json \
 *     [--noise path/to/ted_events.json] \
 *     [--pad 0.05] \
 *     --output path/to/delete_segments_with_noise.json
 */

const fs = require('fs');
const path = require('path');

const args = {};
const noiseFiles = [];
for (let i = 2; i < process.argv.length; i++) {
  const k = process.argv[i];
  if (k === '--noise') { noiseFiles.push(process.argv[++i]); }
  else if (k.startsWith('--')) { args[k.slice(2)] = process.argv[++i]; }
}

if (!args.deletes || !args.output || noiseFiles.length === 0) {
  console.error("usage: --deletes <file> --noise <file> [--noise <file>...] --output <file> [--pad 0.05]");
  process.exit(1);
}

const pad = Number(args.pad ?? 0.05);

const baseRaw = JSON.parse(fs.readFileSync(args.deletes, 'utf8'));
const baseSegs = (baseRaw.segments || baseRaw).map(s => ({ start: +s.start, end: +s.end }));

const noiseSegs = [];
for (const nf of noiseFiles) {
  const data = JSON.parse(fs.readFileSync(nf, 'utf8'));
  for (const ev of data.events || []) {
    if (ev.verdict !== 'confirmed') continue;
    if (ev.is_speech_overlapped) continue;
    const s = Math.max(0, ev.start - pad);
    const e = ev.end + pad;
    if (e > s) {
      noiseSegs.push({
        start: +s.toFixed(3),
        end: +e.toFixed(3),
        source: 'noise',
        class: ev.class,
        speaker: ev.speaker || null,
        explanation: ev.explanation || '',
      });
    }
  }
}

console.log(`base deletes: ${baseSegs.length}, noise adds: ${noiseSegs.length}`);

// Merge overlapping ranges (keep noise metadata when noise dominates)
const all = [...baseSegs, ...noiseSegs].sort((a, b) => a.start - b.start);
const merged = [];
for (const s of all) {
  if (merged.length && s.start <= merged[merged.length - 1].end) {
    const top = merged[merged.length - 1];
    top.end = Math.max(top.end, s.end);
    // Preserve noise tagging when this range originated from noise
    if (s.source === 'noise' && !top.source) {
      top.source = 'noise';
      top.class = s.class;
      top.speaker = s.speaker;
      top.explanation = s.explanation;
    }
  } else {
    merged.push({ ...s });
  }
}

const out = { segments: merged };
fs.mkdirSync(path.dirname(args.output), { recursive: true });
fs.writeFileSync(args.output, JSON.stringify(out, null, 2));
console.log(`merged → ${args.output}  (${merged.length} segments, ${merged.filter(m => m.source === 'noise').length} noise-tagged)`);
