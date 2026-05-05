#!/usr/bin/env node
/**
 * audit_cut.js — edit-quality audit script
 *
 * Run automatically after a cut to inspect delete_segments and surface:
 *   1. Restore integrity: are restored sentences accidentally covered by any delete segment?
 *   2. Manual-deletion effectiveness: do user-marked deletions all have corresponding segments?
 *   3. Cut-point silence detection: do back-to-back cuts produce audible silent pauses?
 *   4. Large-deletion stitching: do deletions >5s join naturally before/after?
 *
 * Usage:
 *   node audit_cut.js <output_dir>
 *
 * Example:
 *   node audit_cut.js output/2026-02-27_meeting_02
 *
 * Input files (auto-discovered under output_dir):
 *   - 2_analysis/delete_segments_edited.json (or delete_segments.json)
 *   - 2_analysis/fine_analysis.json
 *   - 2_analysis/sentences.txt
 *   - 1_transcript/subtitles_words.json
 *   - ai_feedback (optional, when restore/correction info exists)
 *
 * Outputs:
 *   - 2_analysis/audit_report.json — machine-readable full report
 *   - stdout — human-readable summary
 */

const fs = require('fs');
const path = require('path');

// --- Argument parsing ---
const outputDir = process.argv[2];
if (!outputDir) {
  console.error('Usage: node audit_cut.js <output_dir>');
  process.exit(1);
}

const analysisDir = path.join(outputDir, '2_analysis');
const transcriptDir = path.join(outputDir, '1_transcript');

// --- Data loading ---
function loadJSON(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (e) {
    return null;
  }
}

function loadSegments() {
  let data = loadJSON(path.join(analysisDir, 'delete_segments_edited.json'))
    || loadJSON(path.join(analysisDir, 'delete_segments.json'));
  if (!data) { console.error('找不到 delete_segments 檔案'); process.exit(1); }
  return Array.isArray(data) ? data : (data.segments || data.delete_segments || []);
}

function loadWords() {
  const data = loadJSON(path.join(transcriptDir, 'subtitles_words.json'));
  if (!data) { console.error('找不到 subtitles_words.json'); process.exit(1); }
  return Array.isArray(data) ? data : (data.words || []);
}

function loadSentences() {
  const txt = fs.readFileSync(path.join(analysisDir, 'sentences.txt'), 'utf8').trim();
  return txt.split('\n').map(line => {
    const parts = line.split('|');
    const range = parts[1].split('-');
    return {
      idx: parseInt(parts[0]),
      wordStart: parseInt(range[0]),
      wordEnd: parseInt(range[1]),
      speaker: parts[2],
      text: parts[3]
    };
  });
}

function loadFeedback() {
  // Try multiple candidate feedback file paths
  const candidates = [
    ...fs.readdirSync(analysisDir).filter(f => f.startsWith('ai_feedback')).map(f => path.join(analysisDir, f)),
    // Also check the upload directory
  ];
  for (const fp of candidates) {
    const data = loadJSON(fp);
    if (data && (data.restore_feedback || data.user_corrections)) return data;
  }
  return null;
}

const segments = loadSegments();
const words = loadWords();
const sentences = loadSentences();
const fineAnalysis = loadJSON(path.join(analysisDir, 'fine_analysis.json'));
const edits = fineAnalysis ? (fineAnalysis.edits || []) : [];
const feedback = loadFeedback();
const corrections = loadJSON(path.join(analysisDir, 'segment_corrections.json'));

// --- Helpers ---
function wordTime(wordIdx) {
  const w = words[wordIdx];
  if (!w) return { start: 0, end: 0 };
  return { start: w.start || w.s || 0, end: w.end || w.e || 0 };
}

function sentenceTimeRange(sent) {
  const start = wordTime(sent.wordStart);
  const end = wordTime(sent.wordEnd);
  return { start: start.start, end: end.end };
}

function segmentsOverlapping(start, end) {
  return segments.filter(s => s.start < end && s.end > start);
}

function wordsInRange(startTime, endTime) {
  return words.filter(w => {
    const ws = w.start || w.s || 0;
    const we = w.end || w.e || 0;
    return ws >= startTime && we <= endTime;
  }).map(w => w.text || w.word || w.w || '');
}

// --- Check 1: restore integrity ---
//
// Core logic: a "restore" means the user wants to keep the sentence, but in-sentence
// fine edits (deleting stutters, fillers, etc.) inside that sentence are still
// intentional. So we only need to flag segments that:
//   a) Are NOT this fine edit's segment (cross-sentence damage).
//   b) Are NOT from a fine_analysis source at all (e.g. legacy whole-sentence
//      segments from an old HTML export).
//
// Cases that should NOT be reported:
//   - segment matches this sentence's fine_analysis edit (the user wants to delete
//     stutters/fillers but keep the sentence overall).
//
function checkRestoredSentences() {
  const issues = [];

  // Get the restored list
  let restoredIndices = [];
  if (corrections && corrections.all_restored_sentence_indices) {
    restoredIndices = corrections.all_restored_sentence_indices;
  } else if (feedback && feedback.restore_feedback) {
    restoredIndices = [...new Set(feedback.restore_feedback.map(r => r.sentenceIdx))];
  }

  if (restoredIndices.length === 0) return { issues, restoredCount: 0 };

  // Pre-process: collect each restored sentence's fine_analysis edit time ranges.
  // These represent intentional deletions (stutters, fillers, etc.).
  const editRangesBySentence = {};
  for (const sIdx of restoredIndices) {
    editRangesBySentence[sIdx] = edits
      .filter(e => e.sentenceIdx === sIdx && e.deleteStart !== undefined)
      .map(e => ({ start: e.deleteStart, end: e.deleteEnd, type: e.type }));
  }

  // Decide whether a segment corresponds to some fine edit.
  // Loose match: the bulk of the segment's time range (>70%) falls within an edit range.
  function isIntentionalEdit(seg, sentIdx) {
    const sentEdits = editRangesBySentence[sentIdx] || [];
    for (const edit of sentEdits) {
      const overlapStart = Math.max(seg.start, edit.start);
      const overlapEnd = Math.min(seg.end, edit.end);
      if (overlapEnd > overlapStart) {
        const overlapDuration = overlapEnd - overlapStart;
        const segDuration = seg.end - seg.start;
        // Significant overlap between segment and edit → treat as an intentional fine edit
        if (overlapDuration / segDuration > 0.5 || overlapDuration > 0.3) {
          return true;
        }
      }
    }
    // Also check edits from other sentences (cross-sentence fine edits are intentional too)
    for (const edit of edits) {
      if (edit.deleteStart === undefined) continue;
      const overlapStart = Math.max(seg.start, edit.deleteStart);
      const overlapEnd = Math.min(seg.end, edit.deleteEnd);
      if (overlapEnd > overlapStart) {
        const overlapDuration = overlapEnd - overlapStart;
        const segDuration = seg.end - seg.start;
        if (overlapDuration / segDuration > 0.5 || overlapDuration > 0.3) {
          return true;
        }
      }
    }
    return false;
  }

  for (const sIdx of restoredIndices) {
    const sent = sentences[sIdx];
    if (!sent) continue;

    // Collect every segment that overlaps the sentence's word range
    const coveredSegments = new Map(); // seg key → { seg, words: [] }
    for (let wi = sent.wordStart; wi <= sent.wordEnd; wi++) {
      const wt = wordTime(wi);
      const overlaps = segmentsOverlapping(wt.start, wt.end);
      for (const seg of overlaps) {
        const key = `${Math.round(seg.start * 100)}_${Math.round(seg.end * 100)}`;
        if (!coveredSegments.has(key)) {
          coveredSegments.set(key, { seg, words: [] });
        }
        const wordText = words[wi].text || words[wi].word || words[wi].w || '';
        coveredSegments.get(key).words.push({ idx: wi, text: wordText, time: wt });
      }
    }

    // Decide whether each covering segment is an intentional fine edit
    for (const [key, { seg, words: coveredWords }] of coveredSegments) {
      if (isIntentionalEdit(seg, sIdx)) {
        continue; // intentional fine edit — skip
      }

      // Unintended coverage → report as an issue
      const wordTexts = coveredWords.map(w => w.text).join('');
      issues.push({
        type: 'restored_word_covered',
        sentenceIdx: sIdx,
        wordTexts,
        wordCount: coveredWords.length,
        coveringSegment: [seg.start, seg.end],
        segmentDuration: parseFloat((seg.end - seg.start).toFixed(2)),
        sentenceText: sent.text.substring(0, 60),
        note: '此 segment 不對應任何 fine_analysis edit，可能是跨句誤傷或舊 HTML 匯出 bug'
      });
    }
  }

  return { issues, restoredCount: restoredIndices.length };
}

// --- Check 2: manual-deletion effectiveness ---
//
// Verify that user_corrections.added_deletions (user-confirmed full-sentence
// deletions) all have at least one covering segment. missed_catches are AI-suggested
// misses; only check entries that carry a timestamp (most do not).
// Note: we only check whether *any* segment overlaps with the sentence range, not
// whether the entire sentence is covered — because a full-sentence deletion may be
// split across multiple fine edit segments.
//
function checkManualDeletions() {
  const issues = [];

  if (!feedback) return { issues, checkedCount: 0 };

  // 2a: check user_corrections.added_deletions (whole-sentence deletions)
  const addedSentences = [...new Set(feedback.user_corrections?.added_deletions || [])];
  for (const sIdx of addedSentences) {
    const sent = sentences[sIdx];
    if (!sent) continue;
    const range = sentenceTimeRange(sent);

    // Check whether any segment overlaps the sentence's full time range
    const overlap = segmentsOverlapping(range.start, range.end);
    if (overlap.length === 0) {
      issues.push({
        type: 'manual_sentence_not_deleted',
        sentenceIdx: sIdx,
        timeRange: [range.start, range.end],
        sentenceText: sent.text.substring(0, 60)
      });
    }
  }

  // 2b: check missed_catches (AI-suggested misses)
  // Only check entries with precise timestamps; skip those without.
  const missedCatches = feedback.missed_catches || [];
  let missedWithTs = 0;
  for (const mc of missedCatches) {
    if (!mc.timestamp || mc.timestamp.start === undefined) continue;
    missedWithTs++;
    const overlap = segmentsOverlapping(mc.timestamp.start, mc.timestamp.end);
    if (overlap.length === 0) {
      issues.push({
        type: 'missed_catch_not_covered',
        sentenceIdx: mc.sentenceIdx,
        timeRange: [mc.timestamp.start, mc.timestamp.end],
        text: (mc.selectedText || '').substring(0, 40),
        category: mc.typeLabel || mc.type
      });
    }
  }

  return { issues, checkedCount: addedSentences.length + missedWithTs };
}

// --- Check 3: cut-point silence detection ---
function checkCutPointSilences() {
  const issues = [];
  const SILENCE_THRESHOLD = 0.3; // seconds — anything beyond this is flagged as a suspicious pause

  // Sort all segments by start time
  const sorted = [...segments].sort((a, b) => a.start - b.start);

  for (let i = 0; i < sorted.length - 1; i++) {
    const segEnd = sorted[i].end;
    const nextSegStart = sorted[i + 1].start;
    const gapDuration = nextSegStart - segEnd;

    // Only consider short kept gaps (<3s); longer gaps are normal content.
    if (gapDuration <= 0 || gapDuration > 3.0) continue;

    // Check whether the kept interval contains any actual speech content
    const gapWords = words.filter(w => {
      const ws = w.start || w.s || 0;
      const we = w.end || w.e || 0;
      return ws >= segEnd && we <= nextSegStart;
    });

    const hasContent = gapWords.some(w => {
      const text = (w.text || w.word || w.w || '').replace(/[，。！？、：；""''（）\s]/g, '');
      return text.length > 0;
    });

    if (!hasContent && gapDuration > SILENCE_THRESHOLD) {
      // Look for actual speech content immediately before/after the gap
      const beforeWords = words.filter(w => {
        const we = w.end || w.e || 0;
        return we <= segEnd && we > segEnd - 2;
      });
      const afterWords = words.filter(w => {
        const ws = w.start || w.s || 0;
        return ws >= nextSegStart && ws < nextSegStart + 2;
      });
      const beforeText = beforeWords.slice(-3).map(w => w.text || w.word || w.w || '').join('');
      const afterText = afterWords.slice(0, 3).map(w => w.text || w.word || w.w || '').join('');

      issues.push({
        type: 'silence_gap',
        gapStart: segEnd,
        gapEnd: nextSegStart,
        duration: parseFloat(gapDuration.toFixed(3)),
        beforeText,
        afterText,
        suggestion: `可擴展刪除片段 [${segEnd.toFixed(2)}-${nextSegStart.toFixed(2)}] 消除停頓`
      });
    }
  }

  return { issues };
}

// --- Check 4: large-deletion stitching ---
function checkLargeDeletions() {
  const issues = [];
  const LARGE_THRESHOLD = 5.0; // seconds

  const sorted = [...segments].sort((a, b) => a.start - b.start);

  for (const seg of sorted) {
    const duration = seg.end - seg.start;
    if (duration < LARGE_THRESHOLD) continue;

    // Find the text immediately before and after the deletion
    const beforeWords = words.filter(w => {
      const we = w.end || w.e || 0;
      return we <= seg.start && we > seg.start - 5;
    });
    const afterWords = words.filter(w => {
      const ws = w.start || w.s || 0;
      return ws >= seg.end && ws < seg.end + 5;
    });

    const beforeText = beforeWords.slice(-8).map(w => w.text || w.word || w.w || '').join('');
    const afterText = afterWords.slice(0, 8).map(w => w.text || w.word || w.w || '').join('');

    // Speaker switch check (skipped for now — placeholder)
    const beforeSpeaker = beforeWords.length > 0 ? null : null;

    issues.push({
      type: 'large_deletion',
      start: seg.start,
      end: seg.end,
      duration: parseFloat(duration.toFixed(1)),
      beforeText,
      afterText,
      needsReview: true
    });
  }

  return { issues };
}

// --- Main ---
function main() {
  console.log('🔍 剪輯品質檢測開始...\n');

  const report = {
    timestamp: new Date().toISOString(),
    outputDir,
    totalSegments: segments.length,
    checks: {}
  };

  // Check 1
  const restored = checkRestoredSentences();
  report.checks.restoredSentences = restored;
  console.log(`✅ 檢查 1：還原完整性 — ${restored.restoredCount} 個還原，${restored.issues.length} 個問題`);
  if (restored.issues.length > 0) {
    restored.issues.forEach(i => {
      console.log(`   ⚠️  s${i.sentenceIdx} 中 "${i.wordTexts}" (${i.wordCount} 字, ${i.segmentDuration}s) 被 segment [${i.coveringSegment[0].toFixed(2)}-${i.coveringSegment[1].toFixed(2)}] 覆蓋`);
    });
  }

  // Check 2
  const manual = checkManualDeletions();
  report.checks.manualDeletions = manual;
  console.log(`\n✅ 檢查 2：使用者手動刪除 — ${manual.checkedCount} 項檢查，${manual.issues.length} 個問題`);
  if (manual.issues.length > 0) {
    manual.issues.slice(0, 10).forEach(i => {
      if (i.type === 'manual_sentence_not_deleted') {
        console.log(`   ⚠️  s${i.sentenceIdx} 整句刪除未生效 [${i.timeRange[0].toFixed(2)}-${i.timeRange[1].toFixed(2)}]`);
      } else {
        console.log(`   ⚠️  s${i.sentenceIdx} "${i.text}" (${i.category}) 未被覆蓋`);
      }
    });
    if (manual.issues.length > 10) console.log(`   ... 還有 ${manual.issues.length - 10} 個`);
  }

  // Check 3
  const silences = checkCutPointSilences();
  report.checks.cutPointSilences = silences;
  console.log(`\n✅ 檢查 3：切點靜音 — ${silences.issues.length} 個可疑停頓`);
  if (silences.issues.length > 0) {
    silences.issues.slice(0, 10).forEach(i => {
      console.log(`   ⏸️  [${i.gapStart.toFixed(2)}-${i.gapEnd.toFixed(2)}] ${i.duration}s 靜音 — "${i.beforeText}" → "${i.afterText}"`);
    });
    if (silences.issues.length > 10) console.log(`   ... 還有 ${silences.issues.length - 10} 個`);
  }

  // Check 4
  const large = checkLargeDeletions();
  report.checks.largeDeletions = large;
  console.log(`\n✅ 檢查 4：大段刪除 — ${large.issues.length} 段 (>5s) 需人工確認銜接`);
  if (large.issues.length > 0) {
    large.issues.forEach(i => {
      console.log(`   ✂️  [${i.start.toFixed(1)}-${i.end.toFixed(1)}s] ${i.duration}s — "...${i.beforeText}" → "${i.afterText}..."`);
    });
  }

  // Summary
  const totalIssues = restored.issues.length + manual.issues.length + silences.issues.length;
  console.log(`\n${'─'.repeat(50)}`);
  if (totalIssues === 0) {
    console.log('🎉 品質檢測通過！未發現可自動偵測的問題。');
    console.log(`   (${large.issues.length} 段大段刪除建議人工確認銜接)`);
  } else {
    console.log(`⚠️  發現 ${totalIssues} 個問題需修正：`);
    if (restored.issues.length) console.log(`   - ${restored.issues.length} 個還原被覆蓋`);
    if (manual.issues.length) console.log(`   - ${manual.issues.length} 個手動刪除未生效`);
    if (silences.issues.length) console.log(`   - ${silences.issues.length} 個切點靜音停頓`);
    console.log(`   + ${large.issues.length} 段大段刪除建議人工確認`);
  }

  // Save report
  const reportPath = path.join(analysisDir, 'audit_report.json');
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.log(`\n📄 完整報告：${reportPath}`);

  // Exit code: 1 when there are issues
  process.exit(totalIssues > 0 ? 1 : 0);
}

main();
