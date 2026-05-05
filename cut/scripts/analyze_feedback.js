#!/usr/bin/env node
/**
 * Feedback analyser — extract preference-adjustment suggestions from the review page's AI feedback JSON.
 *
 * Input: ai_feedback_*.json (produced by the "Export AI feedback" button in review_enhanced.html)
 * Output: preference-adjustment suggestions (JSON) with a confidence score.
 *
 * Feedback JSON shape:
 * {
 *   "missed_catches": [{ sentenceIdx, speaker, selectedText, fullSentence, type, typeLabel, reason }],
 *   "user_corrections": {
 *     "added_deletions": [sentenceIdx...],  // user-added deletions
 *     "removed_deletions": [sentenceIdx...]  // user-restored AI deletions
 *   }
 * }
 *
 * Usage:
 *   node analyze_feedback.js <feedback.json> [analysis.json] [fine_analysis.json]
 *
 * Optional args:
 *   analysis.json — step 5a's semantic_deep_analysis.json (helps understand restored deletion types)
 *   fine_analysis.json — step 5b's fine_analysis.json (helps understand fine edit types)
 */

const fs = require('fs');
const path = require('path');

// --- Feedback-type → editing_rules mapping ---

const FEEDBACK_TYPE_TO_RULE = {
  // Fine types (from missed_catches' type field)
  in_sentence_repeat: 'filler_words',
  repeated_sentence: 'repeated_sentences',
  stutter: 'stutter',
  self_correction: 'self_correction',
  consecutive_filler: 'filler_words',
  single_filler: 'filler_words',
  silence: 'silence',
  residual_sentence: 'residual_sentences',
  // Content types (from semantic_deep_analysis' type field)
  pre_show: 'content_analysis',
  tech_debug: 'content_analysis',
  chit_chat: 'content_analysis',
  privacy: 'content_analysis',
  repeated_content: 'content_analysis',
  production_talk: 'content_analysis'
};

// --- Analyser ---

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

  // --- Analyse missed_catches (AI misses) ---
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
      reason: `AI 遺漏 ${items.length} 個 "${type}" 類型內容`,
      examples: items.slice(0, 3).map(i => ({
        text: i.selectedText?.slice(0, 50) || '',
        sentence: i.fullSentence?.slice(0, 80) || ''
      }))
    });
  }

  // --- Analyse removed_deletions (user-restored AI deletions) ---
  const removedIndices = feedback.user_corrections?.removed_deletions || [];
  if (removedIndices.length > 0 && analysis) {
    // Look up the original deletion type for each restored sentence
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
        reason: `使用者還原 ${items.length} 個 "${type}" 類型的 AI 刪除`,
        examples: items.slice(0, 3).map(i => ({ idx: i.idx, reason: i.reason }))
      });
    }

    // Check whether any fine-edit types were restored (from fineAnalysis)
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
          reason: `使用者還原 ${items.length} 個 fine "${type}" 類型刪除`,
          examples: items.slice(0, 3).map(i => ({
            text: i.deleteText?.slice(0, 30) || '',
            rule: i.rule
          }))
        });
      }
    }
  }

  // --- Analyse added_deletions (user-added deletions) ---
  const addedCount = feedback.user_corrections?.added_deletions?.length || 0;
  if (addedCount > 3) {
    results.adjustments.push({
      direction: 'increase_aggressiveness',
      target_rule: 'content_analysis',
      feedback_type: 'user_added',
      count: addedCount,
      confidence: Math.min(0.4 + addedCount * 0.05, 0.80),
      reason: `使用者手動新增 ${addedCount} 個刪除，可能需提高整體激進度`
    });
  }

  // --- Filter out low-confidence suggestions ---
  results.adjustments = results.adjustments.filter(a => a.confidence >= 0.5);

  // --- Sort by confidence ---
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

Analyse the AI feedback exported from the review page and generate editing_rules adjustment suggestions.

Args:
  feedback.json       file produced by the review page's "Export AI feedback"
  analysis.json       (optional) step 5a's semantic_deep_analysis.json
  fine_analysis.json  (optional) step 5b's fine_analysis.json

Output: JSON-formatted adjustment suggestions.

Example:
  node analyze_feedback.js ai_feedback_2026-02-21.json \\
      semantic_deep_analysis.json fine_analysis.json`);
    process.exit(1);
  }

  if (!fs.existsSync(feedbackPath)) {
    console.error(`❌ 檔案不存在：${feedbackPath}`);
    process.exit(1);
  }

  const results = analyzeFeedback(feedbackPath, analysisPath, fineAnalysisPath);

  // Print a human-readable summary to stderr
  console.error(`\n📊 回饋分析結果：`);
  console.error(`   AI 遺漏：${results.summary.missed_catches}`);
  console.error(`   使用者新增刪除：${results.summary.added_deletions}`);
  console.error(`   使用者還原刪除：${results.summary.removed_deletions}`);
  console.error(`   調整建議：${results.adjustments.length} 條\n`);

  for (const adj of results.adjustments) {
    const arrow = adj.direction.includes('increase') ? '↑' : '↓';
    console.error(`   ${arrow} [${adj.target_rule}] ${adj.reason}（置信度：${adj.confidence.toFixed(2)}）`);
  }

  // Print full JSON to stdout
  console.log(JSON.stringify(results, null, 2));
}

module.exports = { analyzeFeedback };
