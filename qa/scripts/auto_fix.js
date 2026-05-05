#!/usr/bin/env node
/**
 * auto_fix.js — automatically fix delete_segments based on audit_report.json
 *
 * What it handles:
 *   1. restored_word_covered → remove segments that wrongly cover restored content
 *      (only for unintended overlaps, not legitimate fine edits)
 *   2. silence_gap → extend adjacent segments to eliminate silent pauses
 *
 * Not handled (requires manual review):
 *   - manual_sentence_not_deleted → needs the precise delete range
 *   - missed_catch_not_covered → same as above
 *   - large_deletion → boundary-stitch review only
 *
 * Usage:
 *   node auto_fix.js <output_dir> [--dry-run]
 */

const fs = require('fs');
const path = require('path');

const outputDir = process.argv[2];
const dryRun = process.argv.includes('--dry-run');

if (!outputDir) {
  console.error('Usage: node auto_fix.js <output_dir> [--dry-run]');
  process.exit(1);
}

const analysisDir = path.join(outputDir, '2_analysis');
const reportPath = path.join(analysisDir, 'audit_report.json');
const segPath = path.join(analysisDir, 'delete_segments_edited.json');
const segFallback = path.join(analysisDir, 'delete_segments.json');

if (!fs.existsSync(reportPath)) {
  console.error('請先執行 audit_cut.js 產生 audit_report.json');
  process.exit(1);
}

const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
const segFile = fs.existsSync(segPath) ? segPath : segFallback;
const segData = JSON.parse(fs.readFileSync(segFile, 'utf8'));
let segs = Array.isArray(segData) ? segData : (segData.segments || segData.delete_segments || []);

const originalCount = segs.length;
let removedCount = 0;
let addedCount = 0;
let modifiedCount = 0;

// --- Fix 1: remove segments that cover restored content ---
const restoredIssues = report.checks.restoredSentences?.issues || [];
if (restoredIssues.length > 0) {
  console.log(`🔧 修正還原覆蓋：${restoredIssues.length} 個問題`);

  // Collect every segment range that needs to be removed
  const toRemove = new Set();
  for (const issue of restoredIssues) {
    const [segStart, segEnd] = issue.coveringSegment;
    const key = Math.round(segStart * 100) + '_' + Math.round(segEnd * 100);
    toRemove.add(key);
  }

  const before = segs.length;
  segs = segs.filter(s => {
    const key = Math.round(s.start * 100) + '_' + Math.round(s.end * 100);
    return !toRemove.has(key);
  });
  removedCount += before - segs.length;
  console.log(`   移除 ${before - segs.length} 個 segment`);
}

// --- Fix 2: eliminate silent pauses at cut points ---
const silenceIssues = report.checks.cutPointSilences?.issues || [];
if (silenceIssues.length > 0) {
  console.log(`🔧 修正切點靜音：${silenceIssues.length} 個停頓`);

  for (const issue of silenceIssues) {
    // Strategy: find the segment immediately before the gap and extend its end to the gap end
    const prevSeg = segs.find(s => Math.abs(s.end - issue.gapStart) < 0.05);
    const nextSeg = segs.find(s => Math.abs(s.start - issue.gapEnd) < 0.05);

    if (prevSeg) {
      // Extend the previous segment's end time
      prevSeg.end = issue.gapEnd;
      modifiedCount++;
      console.log(`   擴展 [${issue.gapStart.toFixed(2)}] → [${issue.gapEnd.toFixed(2)}]（消除 ${issue.duration}s 停頓）`);
    } else if (nextSeg) {
      // Extend the next segment's start time
      nextSeg.start = issue.gapStart;
      modifiedCount++;
      console.log(`   擴展 [${issue.gapStart.toFixed(2)} ←]（消除 ${issue.duration}s 停頓）`);
    } else {
      // Add a new segment to cover this silent gap
      segs.push({ start: issue.gapStart, end: issue.gapEnd, text: '(auto-fix silence gap)' });
      addedCount++;
      console.log(`   新增 [${issue.gapStart.toFixed(2)}-${issue.gapEnd.toFixed(2)}]（消除 ${issue.duration}s 停頓）`);
    }
  }

  segs.sort((a, b) => a.start - b.start);
}

// --- Summary ---
console.log(`\n${'─'.repeat(40)}`);
console.log(`原始 segment 數：${originalCount}`);
console.log(`移除：${removedCount}，新增：${addedCount}，修改：${modifiedCount}`);
console.log(`最終 segment 數：${segs.length}`);

// Issues that could not be auto-fixed
const manualIssues = report.checks.manualDeletions?.issues || [];
if (manualIssues.length > 0) {
  console.log(`\n⚠️  ${manualIssues.length} 個手動刪除問題需要人工處理`);
}

if (dryRun) {
  console.log('\n[dry-run] 未寫入檔案');
} else {
  // Back up the original file
  const backupPath = segFile.replace('.json', '_backup.json');
  if (!fs.existsSync(backupPath)) {
    fs.copyFileSync(segFile, backupPath);
    console.log(`\n💾 備份：${backupPath}`);
  }

  // Write the fixed file
  if (Array.isArray(segData)) {
    fs.writeFileSync(segFile, JSON.stringify(segs, null, 2));
  } else {
    const key = segData.segments ? 'segments' : 'delete_segments';
    segData[key] = segs;
    fs.writeFileSync(segFile, JSON.stringify(segData, null, 2));
  }
  console.log(`✅ 已儲存：${segFile}`);
}
