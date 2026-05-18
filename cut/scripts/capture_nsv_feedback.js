#!/usr/bin/env node
/**
 * Capture NSV decisions from an exported ai_feedback file into the user's feedback log.
 *
 * Usage:
 *   node capture_nsv_feedback.js <analysisDir> [--user <userId>] [--feedback-file <path>]
 *
 * If --feedback-file is omitted, picks the newest ai_feedback_*.json in
 * {analysisDir}/../3_output/.
 *
 * Reads:   the chosen ai_feedback_*.json (looks for `nsv_decisions` array)
 * Writes:  cut/user-prefs/{userId}/non_speech_vocal_feedback.jsonl  (append)
 */
const fs = require('fs');
const path = require('path');

const SCRIPT_DIR = __dirname;
const SKILL_DIR = path.resolve(SCRIPT_DIR, '..');

let analysisDir = process.argv[2];
let userId = process.env.PODCAST_EDIT_USER || 'default';
let feedbackFile = null;

const uIdx = process.argv.indexOf('--user');
if (uIdx > 0 && process.argv[uIdx + 1]) userId = process.argv[uIdx + 1];

const fIdx = process.argv.indexOf('--feedback-file');
if (fIdx > 0 && process.argv[fIdx + 1]) feedbackFile = path.resolve(process.argv[fIdx + 1]);

if (!analysisDir) {
  console.error('Usage: node capture_nsv_feedback.js <analysisDir> [--user <userId>] [--feedback-file <path>]');
  process.exit(1);
}

if (!feedbackFile) {
  const outputDir = path.resolve(analysisDir, '..', '3_output');
  if (!fs.existsSync(outputDir)) {
    console.error(`3_output dir missing: ${outputDir}`);
    process.exit(2);
  }
  const candidates = fs.readdirSync(outputDir)
    .filter(name => /^ai_feedback_.*\.json$/.test(name))
    .map(name => ({ name, path: path.join(outputDir, name), mtime: fs.statSync(path.join(outputDir, name)).mtimeMs }))
    .sort((a, b) => b.mtime - a.mtime);
  if (candidates.length === 0) {
    console.error(`No ai_feedback_*.json files in ${outputDir}`);
    process.exit(2);
  }
  feedbackFile = candidates[0].path;
  console.log(`Using newest feedback file: ${feedbackFile}`);
}

if (!fs.existsSync(feedbackFile)) {
  console.error(`Feedback file does not exist: ${feedbackFile}`);
  process.exit(2);
}

const feedback = JSON.parse(fs.readFileSync(feedbackFile, 'utf8'));
const nsv = feedback.nsv_decisions || [];

if (nsv.length === 0) {
  console.log('No NSV decisions in feedback file — nothing to log.');
  process.exit(0);
}

const userDir = path.join(SKILL_DIR, 'user-prefs', userId);
if (!fs.existsSync(userDir)) {
  console.error(`User dir ${userDir} does not exist. Create user first via user_manager.js.`);
  process.exit(2);
}

// Strict: only accept entries that were exported by a new-version review HTML
// (which always writes explicit:true). Pre-fix exports lack the field and may
// contain untouched defaults disguised as decisions — reject them so the
// feedback log never gets polluted with non-decisions.
const explicit = nsv.filter(d => d.explicit === true);
const dropped = nsv.length - explicit.length;
if (dropped > 0) {
  console.warn(
    `  ⚠️  skipped ${dropped} entries missing explicit:true (probably from a` +
    ` pre-fix review HTML — re-export from the latest review_enhanced.html` +
    ` so untouched NSV defaults are not recorded as user decisions).`
  );
}
if (explicit.length === 0) {
  console.log('No explicit NSV decisions in feedback file — nothing to log.');
  process.exit(0);
}

const logPath = path.join(userDir, 'non_speech_vocal_feedback.jsonl');
const out = fs.createWriteStream(logPath, { flags: 'a' });
for (const dec of explicit) {
  out.write(JSON.stringify(dec) + '\n');
}
out.end();

console.log(`✅ appended ${explicit.length} NSV decisions to ${logPath}`);
