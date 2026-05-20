#!/usr/bin/env node
// Convert a noise_candidates.json into an events.json the panel can render —
// no boundary refinement, no judging. Preserves original timestamps.
const fs = require('fs');
const [,, inp, out] = process.argv;
if (!inp || !out) { console.error('usage: candidates_to_events.js <in> <out>'); process.exit(1); }
const d = JSON.parse(fs.readFileSync(inp, 'utf8'));
const events = (d.candidates || []).map(c => ({
  start: c.start,
  end: c.end,
  class: c.class,
  yamnet_class: c.class,
  yamnet_score: c.score,
  verdict: 'candidate',
  is_speech_overlapped: false,
  confidence: c.score,
  explanation: [
    c.centroid_hz != null ? `centroid=${c.centroid_hz}Hz (${c.centroid_ratio}x)` : null,
    c.rolloff_hz != null ? `rolloff=${c.rolloff_hz}Hz` : null,
    c.rms_peak != null ? `rms_peak=${c.rms_peak}` : null,
    c.zcr != null ? `zcr=${c.zcr}` : null,
  ].filter(Boolean).join(', '),
  ...(c.speaker ? { speaker: c.speaker } : {}),
  ...c,
}));
fs.writeFileSync(out, JSON.stringify({
  audio_file: d.audio_file, model: d.model, events,
  summary: { total: events.length, candidates: events.length, confirmed: 0, false_positives: 0, borderline: 0 }
}, null, 2));
console.log(`${events.length} candidates → ${out}`);
