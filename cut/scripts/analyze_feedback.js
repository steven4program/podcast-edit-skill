#!/usr/bin/env node
/**
 * feedbackanalyze器 — 从审查页  AI feedback JSON 中extract偏good调整建议
 *
 * input: ai_feedback_*.json（by  review_enhanced.html  "导出 AI feedback"按钮generate）
 * output: 偏good调整建议（JSON），附置信度
 *
 * feedback JSON format:
 * {
 *   "missed_catches": [{ sentenceIdx, speaker, selectedText, fullSentence, type, typeLabel, reason }],
 *   "user_corrections": {
 *     "added_deletions": [sentenceIdx...],  // use 户手动添加 delete
 *     "removed_deletions": [sentenceIdx...]  // use 户撤销  AI delete
 *   }
 * }
 *
 * Usage:
 *   node analyze_feedback.js <feedback.json> [analysis.json] [fine_analysis.json]
 *
 * optionalargument:
 *   analysis.json — step 5a   semantic_deep_analysis.json（use 于理解被restoredelete type）
 *   fine_analysis.json — step 5b   fine_analysis.json（use 于理解finetype）
 */

const fs = require('fs');
const path = require('path');

// --- feedbacktype到 editing_rules  映射 ---

const FEEDBACK_TYPE_TO_RULE = {
  // finetype（来自 missed_catches   type charactersegment）
  in_sentence_repeat: 'filler_words',
  repeated_sentence: 'repeated_sentences',
  stutter: 'stutter',
  self_correction: 'self_correction',
  consecutive_filler: 'filler_words',
  single_filler: 'filler_words',
  silence: 'silence',
  residual_sentence: 'residual_sentences',
  // contenttype（来自 semantic_deep_analysis   type charactersegment）
  pre_show: 'content_analysis',
  tech_debug: 'content_analysis',
  chit_chat: 'content_analysis',
  privacy: 'content_analysis',
  repeated_content: 'content_analysis',
  production_talk: 'content_analysis'
};

// --- analyzefunction ---

function analyzeFeedback(feedbackPath, analysisPath, fineAnalysisPath) {
  const feedback = JSON.parse(fs.readFileSync(feedbackPath, 'utf8'));
  let analysis = null;
  let fineAnalysis = null;

  if (analysisPath && fs.existsSync(analysisPath)) {
    analysis = JSON.parse(fs.readFileSync(analysisPath, 'utf8'));
  }
  if (fineAnalysisPath && fs.existsSync(fineAnalysisPath)) {
    fineAnalysis = JSON.parse(fs.readFileSync(fineAnalysisPath, 'utf8'));
  }

  const results = {
    version: 'feedback_analysis_v1',
    analyzed_at: new Date().toISOString(),
    source_file: feedbackPath,
    adjustments: [],
    summary: {
      missed_catches: (feedback.missed_catches || []).length,
      added_deletions: (feedback.user_corrections?.added_deletions || []).length,
      removed_deletions: (feedback.user_corrections?.removed_deletions || []).length
    }
  };

  // --- analyze missed_catches（AI 遗漏） ---
  const missedByType = {};
  for (const mc of (feedback.missed_catches || [])) {
    const type = mc.type || 'unknown';
    if (!missedByType[type]) missedByType[type] = [];
    missedByType[type].push(mc);
  }

  for (const [type, items] of Object.entries(missedByType)) {
    const targetRule = FEEDBACK_TYPE_TO_RULE[type] || 'unknown';
    results.adjustments.push({
      direction: 'increase_detection',
      target_rule: targetRule,
      feedback_type: type,
      count: items.length,
      confidence: Math.min(0.5 + items.length * 0.1, 0.95),
      reason: `AI 遗漏 ${items.length} 个 "${type}" type content`,
      examples: items.slice(0, 3).map(i => ({
        text: i.selectedText?.slice(0, 50) || '',
        sentence: i.fullSentence?.slice(0, 80) || ''
      }))
    });
  }

  // --- analyze removed_deletions（use 户restore AI delete ） ---
  const removedIndices = feedback.user_corrections?.removed_deletions || [];
  if (removedIndices.length > 0 && analysis) {
    // find被restoresentence sourcedeletetype
    const removedByType = {};
    const sentenceMap = {};

    if (analysis.sentences) {
      for (const s of analysis.sentences) {
        if (s.action === 'delete') {
          sentenceMap[s.sentenceIdx] = s;
        }
      }
    }

    for (const idx of removedIndices) {
      const original = sentenceMap[idx];
      if (original) {
        const type = original.type || 'content_block';
        if (!removedByType[type]) removedByType[type] = [];
        removedByType[type].push({ idx, reason: original.reason });
      }
    }

    for (const [type, items] of Object.entries(removedByType)) {
      const targetRule = FEEDBACK_TYPE_TO_RULE[type] || 'content_analysis';
      results.adjustments.push({
        direction: 'decrease_aggressiveness',
        target_rule: targetRule,
        feedback_type: type,
        count: items.length,
        confidence: Math.min(0.5 + items.length * 0.1, 0.90),
        reason: `use 户restore ${items.length} 个 "${type}" type  AI delete`,
        examples: items.slice(0, 3).map(i => ({ idx: i.idx, reason: i.reason }))
      });
    }

    // 检查whether有finetype被restore（从 fineAnalysis）
    if (fineAnalysis && fineAnalysis.edits) {
      const fineEditMap = {};
      for (const edit of fineAnalysis.edits) {
        fineEditMap[edit.sentenceIdx] = edit;
      }

      const removedFineByType = {};
      for (const idx of removedIndices) {
        const fineEdit = fineEditMap[idx];
        if (fineEdit) {
          const type = fineEdit.type || 'unknown';
          if (!removedFineByType[type]) removedFineByType[type] = [];
          removedFineByType[type].push(fineEdit);
        }
      }

      for (const [type, items] of Object.entries(removedFineByType)) {
        const targetRule = FEEDBACK_TYPE_TO_RULE[type] || 'filler_words';
        results.adjustments.push({
          direction: 'decrease_aggressiveness',
          target_rule: targetRule,
          feedback_type: type,
          count: items.length,
          confidence: Math.min(0.5 + items.length * 0.15, 0.90),
          reason: `use 户restore ${items.length} 个fine "${type}" type delete`,
          examples: items.slice(0, 3).map(i => ({
            text: i.deleteText?.slice(0, 30) || '',
            rule: i.rule
          }))
        });
      }
    }
  }

  // --- analyze added_deletions（use 户added delete） ---
  const addedCount = feedback.user_corrections?.added_deletions?.length || 0;
  if (addedCount > 3) {
    results.adjustments.push({
      direction: 'increase_aggressiveness',
      target_rule: 'content_analysis',
      feedback_type: 'user_added',
      count: addedCount,
      confidence: Math.min(0.4 + addedCount * 0.05, 0.80),
      reason: `use 户手动added ${addedCount} 个delete，可能needs提高整体激进度`
    });
  }

  // --- 滤低置信度建议 ---
  results.adjustments = results.adjustments.filter(a => a.confidence >= 0.5);

  // --- 按置信度sort ---
  results.adjustments.sort((a, b) => b.confidence - a.confidence);

  return results;
}

// --- CLI ---

if (require.main === module) {
  const feedbackPath = process.argv[2];
  const analysisPath = process.argv[3];
  const fineAnalysisPath = process.argv[4];

  if (!feedbackPath) {
    console.log(`Usage: node analyze_feedback.js <feedback.json> [analysis.json] [fine_analysis.json]

analyze审查页导出  AI feedback，generate editing_rules 调整建议。

argument:
  feedback.json       审查页"导出 AI feedback"generate file
  analysis.json       (optional) step 5a   semantic_deep_analysis.json
  fine_analysis.json  (optional) step 5b   fine_analysis.json

output: JSON format 调整建议

Example:
  node analyze_feedback.js ai_feedback_2026-02-21.json \\
      semantic_deep_analysis.json fine_analysis.json`);
    process.exit(1);
  }

  if (!fs.existsSync(feedbackPath)) {
    console.error(`❌ file does not exist: ${feedbackPath}`);
    process.exit(1);
  }

  const results = analyzeFeedback(feedbackPath, analysisPath, fineAnalysisPath);

  // output人类可读摘要到 stderr
  console.error(`\n📊 feedbackanalyzeresult:`);
  console.error(`   AI 遗漏: ${results.summary.missed_catches}`);
  console.error(`   use 户addeddelete: ${results.summary.added_deletions}`);
  console.error(`   use 户restoredelete: ${results.summary.removed_deletions}`);
  console.error(`   调整建议: ${results.adjustments.length} 条\n`);

  for (const adj of results.adjustments) {
    const arrow = adj.direction.includes('increase') ? '↑' : '↓';
    console.error(`   ${arrow} [${adj.target_rule}] ${adj.reason} (置信度: ${adj.confidence.toFixed(2)})`);
  }

  // output完整 JSON 到 stdout
  console.log(JSON.stringify(results, null, 2));
}

module.exports = { analyzeFeedback };
