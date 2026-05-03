#!/usr/bin/env node
/**
 * auto_fix.js — 根据 audit_report.json 自动Fix delete_segments
 *
 * process：
 *   1. restored_word_covered → 移除覆盖restore  segment（仅非 fine edit  异常覆盖）
 *   2. silence_gap → 扩展相邻 segment 以消除silencepause
 *
 * 不自动process（needs 人工）：
 *   - manual_sentence_not_deleted → needs知道精确deleterange
 *   - missed_catch_not_covered → 同上
 *   - large_deletion → 仅衔接审查
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
  console.error('Please 先运line audit_cut.js generate audit_report.json');
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

// --- Fix 1: 移除覆盖restore  segment ---
const restoredIssues = report.checks.restoredSentences?.issues || [];
if (restoredIssues.length > 0) {
  console.log(`🔧 Fixrestore覆盖: ${restoredIssues.length} 个Issue`);

  // 收集所有needs移除  segment range
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
  console.log(`   移除 ${before - segs.length} 个 segment`);
}

// --- Fix 2: 消除切点silencepause ---
const silenceIssues = report.checks.cutPointSilences?.issues || [];
if (silenceIssues.length > 0) {
  console.log(`🔧 Fix切点silence: ${silenceIssues.length} 个pause`);

  for (const issue of silenceIssues) {
    // 策略：找到 gap 前  segment，扩展其 end 到 gap End
    const prevSeg = segs.find(s => Math.abs(s.end - issue.gapStart) < 0.05);
    const nextSeg = segs.find(s => Math.abs(s.start - issue.gapEnd) < 0.05);

    if (prevSeg) {
      // 扩展前一个 segment  Endtime
      prevSeg.end = issue.gapEnd;
      modifiedCount++;
      console.log(`   扩展 [${issue.gapStart.toFixed(2)}] → [${issue.gapEnd.toFixed(2)}] (消除 ${issue.duration}s pause)`);
    } else if (nextSeg) {
      // 扩展后一个 segment  Starttime
      nextSeg.start = issue.gapStart;
      modifiedCount++;
      console.log(`   扩展 [${issue.gapStart.toFixed(2)} ←] (消除 ${issue.duration}s pause)`);
    } else {
      // added一个 segment 覆盖这segmentsilence
      segs.push({ start: issue.gapStart, end: issue.gapEnd, text: '(auto-fix silence gap)' });
      addedCount++;
      console.log(`   added [${issue.gapStart.toFixed(2)}-${issue.gapEnd.toFixed(2)}] (消除 ${issue.duration}s pause)`);
    }
  }

  segs.sort((a, b) => a.start - b.start);
}

// --- 汇总 ---
console.log(`\n${'─'.repeat(40)}`);
console.log(`source segment 数: ${originalCount}`);
console.log(`移除: ${removedCount}, added: ${addedCount}, modify: ${modifiedCount}`);
console.log(`最终 segment 数: ${segs.length}`);

// 未能自动Fix Issue
const manualIssues = report.checks.manualDeletions?.issues || [];
if (manualIssues.length > 0) {
  console.log(`\n⚠️  ${manualIssues.length} 个手动deleteIssueneeds人工process`);
}

if (dryRun) {
  console.log('\n[dry-run] 未writefile');
} else {
  // 备份原file
  const backupPath = segFile.replace('.json', '_backup.json');
  if (!fs.existsSync(backupPath)) {
    fs.copyFileSync(segFile, backupPath);
    console.log(`\n💾 备份: ${backupPath}`);
  }

  // writeFix后 file
  if (Array.isArray(segData)) {
    fs.writeFileSync(segFile, JSON.stringify(segs, null, 2));
  } else {
    const key = segData.segments ? 'segments' : 'delete_segments';
    segData[key] = segs;
    fs.writeFileSync(segFile, JSON.stringify(segData, null, 2));
  }
  console.log(`✅ saved: ${segFile}`);
}
