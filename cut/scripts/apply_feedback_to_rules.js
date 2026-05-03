#!/usr/bin/env node
/**
 * 将feedbackanalyzeresult应use 到use 户  editing_rules
 *
 * read analyze_feedback.js  output，updateuse 户  editing_rules/ YAML file。
 * 同时record到 learning_history.json。
 *
 * Usage:
 *   node apply_feedback_to_rules.js <analysis_result.json> [userId]
 *
 *  or 通管道:
 *   node analyze_feedback.js feedback.json | node apply_feedback_to_rules.js - [userId]
 */

const fs = require('fs');
const path = require('path');
const UserManager = require('./user_manager');

// --- 激进度调整映射 ---

const AGGRESSIVENESS_LEVELS = ['conservative', 'moderate', 'aggressive'];

function adjustAggressiveness(current, direction) {
  const idx = AGGRESSIVENESS_LEVELS.indexOf(current);
  if (idx === -1) return current;

  if (direction === 'increase' && idx < AGGRESSIVENESS_LEVELS.length - 1) {
    return AGGRESSIVENESS_LEVELS[idx + 1];
  }
  if (direction === 'decrease' && idx > 0) {
    return AGGRESSIVENESS_LEVELS[idx - 1];
  }
  return current;
}

// --- 应use 调整到 editing_rules ---

function applyAdjustments(userId, analysisResult) {
  const adjustments = analysisResult.adjustments || [];
  const applied = [];

  for (const adj of adjustments) {
    // 只应use 高置信度 建议
    if (adj.confidence < 0.6) continue;

    const ruleName = adj.target_rule;

    // 加载现有 use 户覆盖（e.g.果有）
    const rules = UserManager.loadEditingRules(userId);
    let existingRule = rules.user_overrides[ruleName] || {};

    switch (ruleName) {
      case 'filler_words': {
        if (adj.direction === 'decrease_aggressiveness') {
          existingRule.overall_aggressiveness = adjustAggressiveness(
            existingRule.overall_aggressiveness || 'moderate', 'decrease'
          );
          existingRule._last_adjustment = {
            date: new Date().toISOString().slice(0, 10),
            reason: adj.reason,
            direction: adj.direction
          };
        } else if (adj.direction === 'increase_detection') {
          // AI 遗漏filler，增加detect
          if (!existingRule.additional_patterns) existingRule.additional_patterns = [];
          for (const ex of (adj.examples || [])) {
            if (ex.text && !existingRule.additional_patterns.includes(ex.text)) {
              existingRule.additional_patterns.push(ex.text);
            }
          }
        }
        break;
      }

      case 'silence': {
        if (adj.direction === 'decrease_aggressiveness') {
          const current = existingRule.threshold_seconds || 3.0;
          existingRule.threshold_seconds = Math.min(current + 0.5, 6.0);
          existingRule._last_adjustment = {
            date: new Date().toISOString().slice(0, 10),
            reason: adj.reason,
            direction: adj.direction
          };
        } else if (adj.direction === 'increase_detection') {
          const current = existingRule.threshold_seconds || 3.0;
          existingRule.threshold_seconds = Math.max(current - 0.5, 1.5);
        }
        break;
      }

      case 'content_analysis': {
        if (adj.direction === 'decrease_aggressiveness') {
          existingRule.aggressiveness = adjustAggressiveness(
            existingRule.aggressiveness || 'moderate', 'decrease'
          );
          // record哪些type被度delete
          if (!existingRule.over_deleted_types) existingRule.over_deleted_types = [];
          if (adj.feedback_type && !existingRule.over_deleted_types.includes(adj.feedback_type)) {
            existingRule.over_deleted_types.push(adj.feedback_type);
          }
        } else if (adj.direction === 'increase_aggressiveness') {
          existingRule.aggressiveness = adjustAggressiveness(
            existingRule.aggressiveness || 'moderate', 'increase'
          );
        }
        existingRule._last_adjustment = {
          date: new Date().toISOString().slice(0, 10),
          reason: adj.reason,
          direction: adj.direction
        };
        break;
      }

      case 'stutter':
      case 'self_correction':
      case 'repeated_sentences':
      case 'residual_sentences': {
        if (adj.direction === 'increase_detection') {
          existingRule.sensitivity = (existingRule.sensitivity || 'moderate');
          existingRule.missed_count = (existingRule.missed_count || 0) + adj.count;
        } else if (adj.direction === 'decrease_aggressiveness') {
          existingRule.sensitivity = adjustAggressiveness(
            existingRule.sensitivity || 'moderate', 'decrease'
          );
        }
        existingRule._last_adjustment = {
          date: new Date().toISOString().slice(0, 10),
          reason: adj.reason,
          direction: adj.direction
        };
        break;
      }
    }

    // 标记来源
    existingRule._source = existingRule._source || 'feedback_learning';

    // saved touse 户  editing_rules
    UserManager.saveEditingRule(userId, ruleName, existingRule);

    applied.push({
      rule: ruleName,
      direction: adj.direction,
      confidence: adj.confidence,
      reason: adj.reason
    });
  }

  // record学习事件
  if (applied.length > 0) {
    UserManager.appendLearningEvent(userId, {
      type: 'feedback_learning',
      source_file: analysisResult.source_file || '',
      adjustments_applied: applied,
      summary: analysisResult.summary
    });
  }

  return applied;
}

// --- CLI ---

if (require.main === module) {
  let inputPath = process.argv[2];
  const userId = process.argv[3] || UserManager.getCurrentUser();

  if (!inputPath) {
    console.log(`Usage: node apply_feedback_to_rules.js <analysis_result.json> [userId]

将feedbackanalyzeresult应use 到use 户  editing_rules。

argument:
  analysis_result.json   analyze_feedback.js  output（ or use  - 表示 stdin）
  userId                 use 户 ID（default从环境variableread）

Example:
  # 两步执line
  node analyze_feedback.js feedback.json > analysis.json
  node apply_feedback_to_rules.js analysis.json lixiang

  # 管道执line
  node analyze_feedback.js feedback.json 2>/dev/null | node apply_feedback_to_rules.js - lixiang`);
    process.exit(1);
  }

  // Supported stdin
  let rawInput;
  if (inputPath === '-') {
    rawInput = fs.readFileSync(0, 'utf8');  // read from stdin
  } else {
    if (!fs.existsSync(inputPath)) {
      console.error(`❌ file does not exist: ${inputPath}`);
      process.exit(1);
    }
    rawInput = fs.readFileSync(inputPath, 'utf8');
  }

  if (!UserManager.userExists(userId)) {
    console.error(`❌ use 户 "${userId}" 不exists`);
    process.exit(1);
  }

  const analysisResult = JSON.parse(rawInput);
  const applied = applyAdjustments(userId, analysisResult);

  if (applied.length === 0) {
    console.error('ℹ️  noneeds应use  调整（所有建议置信度不足 or 无变更）');
    process.exit(0);
  }

  console.error(`\n✅ 已应use  ${applied.length} 条调整到use 户 "${userId}"   editing_rules:`);
  for (const a of applied) {
    const arrow = a.direction.includes('increase') ? '↑' : '↓';
    console.error(`   ${arrow} [${a.rule}] ${a.reason}`);
  }

  const configPath = UserManager.getUserConfigPath(userId);
  console.error(`\n📂 update file: ${configPath}/editing_rules/`);
  console.error(`📝 学习record: ${configPath}/learning_history.json`);

  // output应use result到 stdout
  console.log(JSON.stringify({ applied, userId }, null, 2));
}

module.exports = { applyAdjustments };
