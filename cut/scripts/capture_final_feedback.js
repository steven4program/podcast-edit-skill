#!/usr/bin/env node
/**
 * capture_final_feedback.js — Stage 8: capture final-review feedback into persistent learning storage.
 *
 * Reads final_review_feedback.json exported from the final-review page,
 * classifies user-flagged issues, and routes them to the right persistence:
 *   - methodology issues → editing-rules/* shared files
 *   - personal-preference issues → user-prefs/<userId>/editing_rules/
 *
 * Usage:
 *   node capture_final_feedback.js \
 *     --feedback <final_review_feedback.json> \
 *     [--user <userId>]
 *
 * Dependencies: user_manager.js (reads/writes user preferences)
 */

const fs = require('fs');
const path = require('path');
const userManager = require('./user_manager');

// --- Argument parsing ---

function parseArgs() {
  const args = process.argv.slice(2);
  const opts = {};
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i].replace(/^--/, '').replace(/-/g, '_');
    opts[key] = args[i + 1];
  }
  return opts;
}

// --- Issue classification ---

/**
 * Classify a QA issue as methodology vs personal preference.
 *
 * Methodology (writes to editing-rules/):
 *   - detection-algorithm gaps (missed stutter/filler/repetition)
 *   - cut-point quality (energy_jump, spectral, etc.)
 *   - missing content (valuable content wrongly deleted)
 *
 * Personal preference (writes to user-prefs/<userId>/):
 *   - residual filler retention (some people keep "嗯", others remove it)
 *   - deletion aggressiveness
 *   - keep/delete preferences for specific words
 */
function classifyIssue(issue) {
  const type = issue.type || '';
  const layer = issue.layer || '';

  // Signal-layer issue → methodology (cut-point algorithm needs work)
  if (layer === 'signal' || layer === 'signal_ai') {
    return 'methodology';
  }

  // Data-layer issue → methodology (audit logic needs work)
  if (layer === 'data') {
    return 'methodology';
  }

  // Semantic layer: missing content → methodology
  if (type === 'missing_content') {
    return 'methodology';
  }

  // Semantic layer: residual filler/stutter → preference (user-dependent)
  if (type === 'residual_filler' || type === 'residual_stutter') {
    return 'preference';
  }

  // Default → methodology
  return 'methodology';
}

// --- Feedback routing ---

/**
 * Append methodology feedback to learning history.
 * (The actual rule updates are decided by Claude after reading the feedback.)
 */
function routeMethodologyFeedback(events, userId) {
  if (events.length === 0) return;

  for (const event of events) {
    userManager.appendLearningEvent(userId, {
      source: 'final_review',
      category: 'methodology',
      type: event.type,
      layer: event.layer,
      severity: event.severity,
      detail: event.detail,
      user_note: event.note,
      user_status: event.status,
      timestamp_in_audio: event.time
    });
  }

  console.log(`  方法論回饋：${events.length} 條 → learning_history.json`);
}

/**
 * Append preference feedback to the user's preferences.
 */
function routePreferenceFeedback(events, userId) {
  if (events.length === 0) return;

  // Append to learning history
  for (const event of events) {
    userManager.appendLearningEvent(userId, {
      source: 'final_review',
      category: 'preference',
      type: event.type,
      detail: event.detail,
      user_note: event.note,
      user_status: event.status,
      timestamp_in_audio: event.time
    });
  }

  // Tally preference signals
  const fillerKept = events.filter(e =>
    e.type === 'residual_filler' && e.status === 'ok'
  ).length;
  const fillerFlagged = events.filter(e =>
    e.type === 'residual_filler' && e.status === 'flagged'
  ).length;
  const stutterKept = events.filter(e =>
    e.type === 'residual_stutter' && e.status === 'ok'
  ).length;
  const stutterFlagged = events.filter(e =>
    e.type === 'residual_stutter' && e.status === 'flagged'
  ).length;

  if (fillerKept > 0 || fillerFlagged > 0) {
    console.log(`  贅詞偏好訊號：${fillerKept} 個覺得可保留，${fillerFlagged} 個覺得該刪`);
  }
  if (stutterKept > 0 || stutterFlagged > 0) {
    console.log(`  卡頓偏好訊號：${stutterKept} 個覺得可保留，${stutterFlagged} 個覺得該刪`);
  }

  console.log(`  個人偏好回饋：${events.length} 條 → learning_history.json`);
}

// --- 主逻辑 ---

function main() {
  const opts = parseArgs();

  if (!opts.feedback) {
    console.error('Usage: node capture_final_feedback.js --feedback <final_review_feedback.json> [--user <userId>]');
    process.exit(1);
  }

  const userId = opts.user || userManager.getCurrentUser();

  console.log('Stage 8: final-review feedback capture');
  console.log('='.repeat(50));
  console.log(`使用者：${userId}`);

  // Verify the user exists
  if (!userManager.userExists(userId)) {
    console.error(`使用者 "${userId}" 不存在。請先執行 node user_manager.js create ${userId}`);
    process.exit(1);
  }

  // Read feedback
  if (!fs.existsSync(opts.feedback)) {
    console.error(`回饋檔案不存在：${opts.feedback}`);
    process.exit(1);
  }

  const feedback = JSON.parse(fs.readFileSync(opts.feedback, 'utf8'));
  console.log(`回饋版本：${feedback.version}`);
  console.log(`判定結果：${feedback.verdict}`);
  console.log(`總問題數：${feedback.summary?.total || 0}`);
  console.log(`  確認無問題：${feedback.summary?.confirmed_ok || 0}`);
  console.log(`  標記有問題：${feedback.summary?.flagged || 0}`);
  console.log(`  未處理：    ${feedback.summary?.pending || 0}`);

  // Only handle issues with a definite status (ok or flagged)
  const actionableIssues = (feedback.issues || []).filter(
    i => i.status === 'ok' || i.status === 'flagged'
  );

  if (actionableIssues.length === 0) {
    console.log('\nNo actionable feedback entries.');
    return;
  }

  // Classify
  const methodologyIssues = [];
  const preferenceIssues = [];

  for (const issue of actionableIssues) {
    const category = classifyIssue(issue);
    if (category === 'methodology') {
      methodologyIssues.push(issue);
    } else {
      preferenceIssues.push(issue);
    }
  }

  console.log(`\n分類結果：`);
  console.log(`  方法論：${methodologyIssues.length} 條`);
  console.log(`  個人偏好：${preferenceIssues.length} 條`);

  // Route
  routeMethodologyFeedback(methodologyIssues, userId);
  routePreferenceFeedback(preferenceIssues, userId);

  // Append to episode history
  userManager.appendEpisode(userId, {
    source: 'final_review',
    verdict: feedback.verdict,
    audio_source: feedback.audio_source,
    total_issues: feedback.summary?.total || 0,
    flagged: feedback.summary?.flagged || 0,
    confirmed_ok: feedback.summary?.confirmed_ok || 0
  });

  console.log(`\n回饋已持久化到使用者 "${userId}" 的記錄中。`);

  if (methodologyIssues.some(i => i.status === 'flagged')) {
    console.log('\nTip: methodology-level issues were flagged. Claude should review learning_history.json and consider updating editing-rules/.');
  }
  if (preferenceIssues.some(i => i.status === 'flagged')) {
    console.log('\nTip: per-user-preferences issues were flagged. Claude should review learning_history.json and consider updating editing_rules/.');
  }
}

main();
