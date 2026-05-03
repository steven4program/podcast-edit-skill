#!/usr/bin/env node
/**
 * capture_final_feedback.js — Stage 8: final-review feedback capture → 持久化学习
 *
 * read终审页面导出  final_review_feedback.json，
 * 将use 户标记 Issue min 类后路by 到OK应 持久化存储：
 *   - method论Issue → baseedit规rule/ 相关file
 *   - personal preferencesIssue → user preferences/<userId>/editing_rules/
 *
 * Usage:
 *   node capture_final_feedback.js \
 *     --feedback <final_review_feedback.json> \
 *     [--user <userId>]
 *
 * 依赖: user_manager.js（读写user preferences）
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

// --- Issue min 类 ---

/**
 * 将质检Issue min 类为method论 vs personal preferences
 *
 * method论（write baseedit规rule/）：
 *   - detect算法缺陷（missed detection stutter/filler/repetition）
 *   - 切点质量Issue（energy_jump, spectral 等信号层Issue）
 *   - content缺失（误删有价值content）
 *
 * personal preferences（write user preferences/<userId>/）：
 *   - 残留filler去留（有人觉"嗯"该留，有人觉该删）
 *   - delete激进度调整
 *   - 特定word keep/delete
 */
function classifyIssue(issue) {
  const type = issue.type || '';
  const layer = issue.layer || '';

  // 信号层Issue → method论（切点算法needs改进）
  if (layer === 'signal' || layer === 'signal_ai') {
    return 'methodology';
  }

  // data layerIssue → method论（审计逻辑needs改进）
  if (layer === 'data') {
    return 'methodology';
  }

  // 语义层：content缺失 → method论
  if (type === 'missing_content') {
    return 'methodology';
  }

  // 语义层：残留filler/卡顿 → personal preferences（whether该删取决于use 户）
  if (type === 'residual_filler' || type === 'residual_stutter') {
    return 'preference';
  }

  // default → method论
  return 'methodology';
}

// --- feedback路by  ---

/**
 * 将method论feedbackrecord到学习历史
 * （实际规thenupdateby  Claude 在readfeedback后判断执line）
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

  console.log(`  method论feedback: ${events.length} 条 → learning_history.json`);
}

/**
 * 将personal preferencesfeedbackrecord到user preferences
 */
function routePreferenceFeedback(events, userId) {
  if (events.length === 0) return;

  // record到学习历史
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

  // statistics偏good信号
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
    console.log(`  filler偏good信号: ${fillerKept} 个觉可以留, ${fillerFlagged} 个觉该删`);
  }
  if (stutterKept > 0 || stutterFlagged > 0) {
    console.log(`  卡顿偏good信号: ${stutterKept} 个觉可以留, ${stutterFlagged} 个觉该删`);
  }

  console.log(`  personal preferencesfeedback: ${events.length} 条 → learning_history.json`);
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
  console.log(`use 户: ${userId}`);

  // 检查use 户exists
  if (!userManager.userExists(userId)) {
    console.error(`use 户 "${userId}" 不exists。Please 先运line node user_manager.js create ${userId}`);
    process.exit(1);
  }

  // readfeedback
  if (!fs.existsSync(opts.feedback)) {
    console.error(`feedbackfile does not exist: ${opts.feedback}`);
    process.exit(1);
  }

  const feedback = JSON.parse(fs.readFileSync(opts.feedback, 'utf8'));
  console.log(`feedbackversion: ${feedback.version}`);
  console.log(`判定result: ${feedback.verdict}`);
  console.log(`总Issue数: ${feedback.summary?.total || 0}`);
  console.log(`  confirm无Issue: ${feedback.summary?.confirmed_ok || 0}`);
  console.log(`  标记有Issue: ${feedback.summary?.flagged || 0}`);
  console.log(`  未process:     ${feedback.summary?.pending || 0}`);

  // 只process有明确status Issue（ok  or  flagged）
  const actionableIssues = (feedback.issues || []).filter(
    i => i.status === 'ok' || i.status === 'flagged'
  );

  if (actionableIssues.length === 0) {
    console.log('\nNo actionable feedback entries.');
    return;
  }

  //  min 类
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

  console.log(`\n min 类result:`);
  console.log(`  method论: ${methodologyIssues.length} 条`);
  console.log(`  personal preferences: ${preferenceIssues.length} 条`);

  // 路by 
  routeMethodologyFeedback(methodologyIssues, userId);
  routePreferenceFeedback(preferenceIssues, userId);

  // record到 episode history
  userManager.appendEpisode(userId, {
    source: 'final_review',
    verdict: feedback.verdict,
    audio_source: feedback.audio_source,
    total_issues: feedback.summary?.total || 0,
    flagged: feedback.summary?.flagged || 0,
    confirmed_ok: feedback.summary?.confirmed_ok || 0
  });

  console.log(`\nfeedback已持久化到use 户 "${userId}"  record中。`);

  if (methodologyIssues.some(i => i.status === 'flagged')) {
    console.log('\nTip: methodology-level issues were flagged. Claude should review learning_history.json and consider updating editing-rules/.');
  }
  if (preferenceIssues.some(i => i.status === 'flagged')) {
    console.log('\nTip: per-user-preferences issues were flagged. Claude should review learning_history.json and consider updating editing_rules/.');
  }
}

main();
