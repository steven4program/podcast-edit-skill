#!/usr/bin/env node
/**
 * Noise pipeline orchestrator — wraps the proven detect_noise_events.py
 * (YAMNet dual-pass + F0 voicing filter + word-gap edge clamp) and optionally
 * injects the resulting events into review_enhanced.html via inject_noise_panel.js.
 *
 * This is the "11 events" recipe — single detector, two passes, conservative.
 *
 *   Pass A (label): YAMNet target labels (Cough/Throat clearing/Plop/...) above
 *                   --min-conf, vetoed by Speech/Laughter scores.
 *   Pass B (burst): YAMNet Speech >= --burst-speech-conf but NO speaker has a
 *                   transcribed word at that time → likely untranscribed clear/cough.
 *   F0 filter:      drop burst events whose voiced (pyin) frame fraction exceeds
 *                   --max-voiced-frac (real speech / mm-hmm / laugh have pitch).
 *   Edge clamp:     burst events get clamped inside the surrounding inter-word gap
 *                   minus --burst-edge-buffer to protect adjacent speech.
 *
 * Defaults match the final tuned CLI: --no-energy-tighten, --max-voiced-frac 0.50,
 * --burst-edge-buffer 0.02. Override via --flag VALUE if you need to.
 *
 * Usage:
 *   node run_noise_pipeline.js \
 *     --track Hogan=source/hogan-5m.mp3 \
 *     --track Ted=source/ted35-01-5m.mp3 \
 *     --words output/<run>/cut/1_transcript/subtitles_words.json \
 *     --out-dir output/<run>/cut/noise \
 *     [--review-html output/<run>/cut/review_enhanced.html]
 *
 * Extra knobs forwarded verbatim to detect_noise_events.py:
 *   --min-conf 0.40       (Pass A label score floor)
 *   --speech-veto 0.30
 *   --burst-speech-conf 0.50
 *   --burst-laughter-veto 0.20
 *   --burst-min-dur 0.30  --burst-max-dur 2.0
 *   --max-voiced-frac 0.50         [default here = wide]
 *   --burst-edge-buffer 0.02       [default here = wide]
 *   --energy-threshold 0.35
 *   --no-energy-tighten            [enabled here by default = wide]
 *   --no-pitch-filter
 *   --merge-gap 0.30  --pad 0.05
 *   --keep-in-word
 */
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const SCRIPT_DIR = __dirname;
const PY = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

const tracks = [];
const args = {};
const flagPassthrough = [];           // forwarded to detect_noise_events.py verbatim

// Boolean flags we pass through if present
const BOOL_FLAGS = new Set([
  'no-burst-pass', 'no-energy-tighten', 'no-pitch-filter', 'keep-in-word',
  'no-lowfreq-pass',
]);
// Numeric flags forwarded verbatim
const NUM_FLAGS = new Set([
  'min-conf', 'speech-veto', 'burst-speech-conf', 'burst-laughter-veto',
  'burst-min-dur', 'burst-max-dur', 'max-voiced-frac', 'burst-edge-buffer',
  'energy-threshold', 'merge-gap', 'pad',
  // Pass C (lowfreq) — all forwarded verbatim
  'lowfreq-min-score', 'lowfreq-max-voiced-frac',
  'lowfreq-rolloff-max-hz', 'lowfreq-zcr-max', 'lowfreq-rms-min-mult',
  'lowfreq-centroid-max-hz', 'lowfreq-min-dur-ms', 'lowfreq-max-dur-ms',
  'lowfreq-merge-gap-ms', 'lowfreq-min-frames',
]);
const userFlags = new Set();

for (let i = 2; i < process.argv.length; i++) {
  const k = process.argv[i];
  if (k === '--track') {
    const v = process.argv[++i];
    const eq = v.indexOf('=');
    if (eq < 0) { console.error(`bad --track: ${v} (expected Name=path)`); process.exit(2); }
    tracks.push({ speaker: v.slice(0, eq).trim(), audio: v.slice(eq + 1).trim() });
  } else if (k.startsWith('--')) {
    const name = k.slice(2);
    if (BOOL_FLAGS.has(name)) {
      flagPassthrough.push(k);
      userFlags.add(name);
    } else if (NUM_FLAGS.has(name)) {
      flagPassthrough.push(k, process.argv[++i]);
      userFlags.add(name);
    } else {
      args[name] = process.argv[++i];
    }
  }
}

if (!tracks.length || !args['out-dir']) {
  console.error('usage: --track Name=path [--track ...] [--words FILE] --out-dir DIR [--review-html FILE] [forwarded flags]');
  process.exit(1);
}

// Apply the tuned wide defaults if user did not override them
function addDefault(flag, val) {
  if (!userFlags.has(flag)) {
    if (val === true) flagPassthrough.push('--' + flag);
    else flagPassthrough.push('--' + flag, String(val));
  }
}
addDefault('no-energy-tighten', true);
addDefault('max-voiced-frac', '0.50');
addDefault('burst-edge-buffer', '0.02');

const outDir = path.resolve(args['out-dir']);
fs.mkdirSync(outDir, { recursive: true });

if (args.words && !fs.existsSync(args.words)) {
  console.error(`✖ --words file not found: ${args.words}`);
  process.exit(2);
}
if (!args.words) {
  console.warn('⚠  no --words given. Pass B will not be able to detect untranscribed bursts (no word-gap to anchor on); expect false positives.');
}

// 1) Run detect_noise_events.py once with all tracks
const noiseJson = path.join(outDir, 'noise_events.json');
const detectArgs = [
  path.join(SCRIPT_DIR, 'detect_noise_events.py'),
  '--output', noiseJson,
  ...flagPassthrough,
];
if (args.words) { detectArgs.push('--words', args.words); }
for (const t of tracks) {
  if (!fs.existsSync(t.audio)) {
    console.error(`✖ audio not found: ${t.audio}`);
    process.exit(2);
  }
  detectArgs.push('--track', `${t.speaker}=${t.audio}`);
}

console.log(`▶ ${PY} ${detectArgs.join(' ')}\n`);
const r = spawnSync(PY, detectArgs, { stdio: 'inherit' });
if (r.status !== 0) { console.error(`✖ detect_noise_events.py failed (exit ${r.status})`); process.exit(r.status || 1); }

// 2) Adapt array output → {events: [...]} for the panel
const arr = JSON.parse(fs.readFileSync(noiseJson, 'utf8'));
const events = arr.map(e => ({
  start: e.start,
  end: e.end,
  class: e.label,
  yamnet_class: e.label,
  yamnet_score: e.confidence,
  verdict: 'candidate',
  is_speech_overlapped: !!e.in_word,
  confidence: e.confidence,
  speaker: e.track,
  explanation: `${e.source} pass · conf=${e.confidence}` +
               (e.speech_conf != null ? ` · speech=${e.speech_conf}` : '') +
               (e.voiced_frac != null ? ` · voiced=${e.voiced_frac}` : ''),
  source: e.source,
}));
const panelJson = path.join(outDir, 'events.json');
fs.writeFileSync(panelJson, JSON.stringify({
  audio_file: tracks.map(t => path.basename(t.audio)).join(','),
  model: 'yamnet-dual-pass',
  events,
  summary: { total: events.length, candidates: events.length, confirmed: 0, false_positives: 0, borderline: 0 },
}, null, 2));
console.log(`\n📦 panel events → ${panelJson}  (${events.length} events)`);

// 3) Inject into review_enhanced.html if asked (front-end unchanged)
if (args['review-html']) {
  if (!fs.existsSync(args['review-html'])) {
    console.error(`✖ review html not found: ${args['review-html']}`);
    process.exit(2);
  }
  const injArgs = ['--html', args['review-html'], '--noise', panelJson];
  if (args['original-mp3']) injArgs.push('--original-mp3', args['original-mp3']);
  if (args['noise-removed-mp3']) injArgs.push('--noise-removed-mp3', args['noise-removed-mp3']);
  const ri = spawnSync('node', [path.join(SCRIPT_DIR, 'inject_noise_panel.js'), ...injArgs], { stdio: 'inherit' });
  if (ri.status !== 0) { console.error('✖ inject_noise_panel.js failed'); process.exit(ri.status || 1); }
}

console.log(`\n✅ done.`);
