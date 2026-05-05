#!/usr/bin/env node
/**
 * Apply feedback-analysis results to a user's editing_rules.
 *
 * Reads analyze_feedback.js's output and updates the user's editing_rules/ YAML files.
 * Also appends to learning_history.json.
 *
 * Usage:
 *   node apply_feedback_to_rules.js <analysis_result.json> [userId]
 *
 * Or via pipeline:
 *   node analyze_feedback.js feedback.json | node apply_feedback_to_rules.js - [userId]
 */

const fs = require('fs');
const path = require('path');
const UserManager = require('./user_manager');

// --- Aggressiveness adjustment mapping ---

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

// --- Apply adjustments to editing_rules ---

function applyAdjustments(userId, analysisResult) {
  const adjustments = analysisResult.adjustments || [];
  const applied = [];

  for (const adj of adjustments) {
    // Only apply high-confidence suggestions
    if (adj.confidence < 0.6) continue;

    const ruleName = adj.target_rule;

    // Load any existing user overrides
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
          // AI missed fillers — boost detection
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
          // Record which types were over-deleted
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

    // Tag the source
    existingRule._source = existingRule._source || 'feedback_learning';

    // Save to the user's editing_rules
    UserManager.saveEditingRule(userId, ruleName, existingRule);

    applied.push({
      rule: ruleName,
      direction: adj.direction,
      confidence: adj.confidence,
      reason: adj.reason
    });
  }

  // Append a learning event
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

Apply feedback-analysis results to a user's editing_rules.

Args:
  analysis_result.json   output of analyze_feedback.js (or "-" for stdin)
  userId                 user ID (defaults to the value from the environment)

Example:
  # Two-step execution
  node analyze_feedback.js feedback.json > analysis.json
  node apply_feedback_to_rules.js analysis.json lixiang

  # Piped execution
  node analyze_feedback.js feedback.json 2>/dev/null | node apply_feedback_to_rules.js - lixiang`);
    process.exit(1);
  }

  // Support stdin
  let rawInput;
  if (inputPath === '-') {
    rawInput = fs.readFileSync(0, 'utf8');  // read from stdin
  } else {
    if (!fs.existsSync(inputPath)) {
      console.error(`❌ 檔案不存在：${inputPath}`);
      process.exit(1);
    }
    rawInput = fs.readFileSync(inputPath, 'utf8');
  }

  if (!UserManager.userExists(userId)) {
    console.error(`❌ 使用者 "${userId}" 不存在`);
    process.exit(1);
  }

  const analysisResult = JSON.parse(rawInput);
  const applied = applyAdjustments(userId, analysisResult);

  if (applied.length === 0) {
    console.error('ℹ️  無需套用的調整（所有建議置信度不足或無變更）');
    process.exit(0);
  }

  console.error(`\n✅ 已套用 ${applied.length} 條調整到使用者 "${userId}" 的 editing_rules：`);
  for (const a of applied) {
    const arrow = a.direction.includes('increase') ? '↑' : '↓';
    console.error(`   ${arrow} [${a.rule}] ${a.reason}`);
  }

  const configPath = UserManager.getUserConfigPath(userId);
  console.error(`\n📂 更新檔案：${configPath}/editing_rules/`);
  console.error(`📝 學習記錄：${configPath}/learning_history.json`);

  // Output the apply result to stdout
  console.log(JSON.stringify({ applied, userId }, null, 2));
}

module.exports = { applyAdjustments };
