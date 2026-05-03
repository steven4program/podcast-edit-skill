#!/usr/bin/env node
/**
 * audit_cut.js — edit质检脚本
 *
 * 在Cut complete后自动审查 delete_segments，发现以下Issue：
 *   1. restore完整性：restore sentencewhether被任何 delete segment 覆盖
 *   2. use 户手动delete生效性：use 户标记 deletewhether都有OK应 segment
 *   3. 切点silencedetect：相邻切点之间whether有可能产生听感pause silence
 *   4. 大segmentdelete衔接：>5s  delete前后文本whether自然衔接
 *
 * Usage:
 *   node audit_cut.js <output_dir>
 *
 * 例:
 *   node audit_cut.js output/2026-02-27_meeting_02
 *
 * inputfile (自动在 output_dir 下find):
 *   - 2_analysis/delete_segments_edited.json ( or  delete_segments.json)
 *   - 2_analysis/fine_analysis.json
 *   - 2_analysis/sentences.txt
 *   - 1_transcript/subtitles_words.json
 *   - ai_feedback (optional，e.g.有 restore/correction info)
 *
 * output:
 *   - 2_analysis/audit_report.json — 机器可读 完整报告
 *   - stdout — 人类可读 摘要
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

// --- 加载data ---
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
  if (!data) { console.error('not found delete_segments file'); process.exit(1); }
  return Array.isArray(data) ? data : (data.segments || data.delete_segments || []);
}

function loadWords() {
  const data = loadJSON(path.join(transcriptDir, 'subtitles_words.json'));
  if (!data) { console.error('not found subtitles_words.json'); process.exit(1); }
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
  // 尝试multi种可能 feedbackfilepath
  const candidates = [
    ...fs.readdirSync(analysisDir).filter(f => f.startsWith('ai_feedback')).map(f => path.join(analysisDir, f)),
    // 也检查uploaddirectory
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

// --- 辅助function ---
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

// --- 检查 1: restore完整性 ---
//
// 核心逻辑：restore = use 户想keep sentence，但sentence内  fine edit（e.g.口吃、fillerdelete）
// 仍然是有意keep 。所以只needs检查：
//   a) 被"非本 fine edit"  segment 覆盖（跨误伤）
//   b) 被"非 fine edit 来源"  segment 覆盖（e.g.旧 HTML 导出  wholeSentence segment）
//
// 不应报告 情况：
//   - segment OK应本  fine_analysis edit（use 户想删口吃但keepsentence整体）
//
function checkRestoredSentences() {
  const issues = [];

  // 获取restorecolumn表
  let restoredIndices = [];
  if (corrections && corrections.all_restored_sentence_indices) {
    restoredIndices = corrections.all_restored_sentence_indices;
  } else if (feedback && feedback.restore_feedback) {
    restoredIndices = [...new Set(feedback.restore_feedback.map(r => r.sentenceIdx))];
  }

  if (restoredIndices.length === 0) return { issues, restoredCount: 0 };

  // 预process：为each个restore收集其 fine_analysis edits  timerange
  // 这些是use 户有意keep delete（口吃、filler等）
  const editRangesBySentence = {};
  for (const sIdx of restoredIndices) {
    editRangesBySentence[sIdx] = edits
      .filter(e => e.sentenceIdx === sIdx && e.deleteStart !== undefined)
      .map(e => ({ start: e.deleteStart, end: e.deleteEnd, type: e.type }));
  }

  // 判断一个 segment whetherOK应本 某个 fine edit
  // 使use 宽松match：segment  大部 min timerange（>70%）落在某个 edit range内
  function isIntentionalEdit(seg, sentIdx) {
    const sentEdits = editRangesBySentence[sentIdx] || [];
    for (const edit of sentEdits) {
      const overlapStart = Math.max(seg.start, edit.start);
      const overlapEnd = Math.min(seg.end, edit.end);
      if (overlapEnd > overlapStart) {
        const overlapDuration = overlapEnd - overlapStart;
        const segDuration = seg.end - seg.start;
        // segment  and  edit 有显著重叠 → 认为是有意  fine edit
        if (overlapDuration / segDuration > 0.5 || overlapDuration > 0.3) {
          return true;
        }
      }
    }
    // 也检查其他sentence  edit whether解释这个 segment（跨 fine edit 也是有意 ）
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

    // 收集覆盖本 word range  所有 segment
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

    // OKeach个覆盖 segment，判断whether是有意  fine edit
    for (const [key, { seg, words: coveredWords }] of coveredSegments) {
      if (isIntentionalEdit(seg, sIdx)) {
        continue; // 有意  fine edit，跳
      }

      // 非预期 覆盖 → 报告Issue
      const wordTexts = coveredWords.map(w => w.text).join('');
      issues.push({
        type: 'restored_word_covered',
        sentenceIdx: sIdx,
        wordTexts,
        wordCount: coveredWords.length,
        coveringSegment: [seg.start, seg.end],
        segmentDuration: parseFloat((seg.end - seg.start).toFixed(2)),
        sentenceText: sent.text.substring(0, 60),
        note: '此 segment 不OK应任何 fine_analysis edit，可能是跨误伤 or 旧 HTML 导出 bug'
      });
    }
  }

  return { issues, restoredCount: restoredIndices.length };
}

// --- 检查 2: use 户手动delete生效性 ---
//
// 检查 user_corrections.added_deletions（use 户confirm 整delete）whether都有 segment 覆盖。
// missed_catches 是 AI 建议 遗漏项，只有when 它带 timestamp 时才检查（通常不带）。
// Note：这里只检查"whether有任何 segment  and sentencerange重叠"，不要求完full覆盖。
// 因为整delete可能 min 拆成multi个 fine edit segments。
//
function checkManualDeletions() {
  const issues = [];

  if (!feedback) return { issues, checkedCount: 0 };

  // 2a: 检查 user_corrections.added_deletions (整delete)
  const addedSentences = [...new Set(feedback.user_corrections?.added_deletions || [])];
  for (const sIdx of addedSentences) {
    const sent = sentences[sIdx];
    if (!sent) continue;
    const range = sentenceTimeRange(sent);

    // 检查整个sentence timerangewhether有 segment 覆盖
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

  // 2b: 检查 missed_catches (AI 建议 遗漏)
  // 只检查带精确time戳 条目；notime戳 跳
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

// --- 检查 3: 切点silencedetect ---
function checkCutPointSilences() {
  const issues = [];
  const SILENCE_THRESHOLD = 0.3; // s，超此值标记为可疑pause

  // 按起始timesort所有 segment
  const sorted = [...segments].sort((a, b) => a.start - b.start);

  for (let i = 0; i < sorted.length - 1; i++) {
    const segEnd = sorted[i].end;
    const nextSegStart = sorted[i + 1].start;
    const gapDuration = nextSegStart - segEnd;

    // 只关注短间距keep segment (gap < 3s  才检查，太长 是正常content)
    if (gapDuration <= 0 || gapDuration > 3.0) continue;

    // 检查这segmentkeepinterval内whether有实际语音content
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
      // 找到前后 实际语音content
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
        suggestion: `可扩展deletesegment [${segEnd.toFixed(2)}-${nextSegStart.toFixed(2)}] 消除pause`
      });
    }
  }

  return { issues };
}

// --- 检查 4: 大segmentdelete衔接 ---
function checkLargeDeletions() {
  const issues = [];
  const LARGE_THRESHOLD = 5.0; // s

  const sorted = [...segments].sort((a, b) => a.start - b.start);

  for (const seg of sorted) {
    const duration = seg.end - seg.start;
    if (duration < LARGE_THRESHOLD) continue;

    // 找delete前后 文本
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

    // 检查speakertoggle
    const beforeSpeaker = beforeWords.length > 0 ? null : null; // 简化：暂不dospeaker检查

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

// --- 主function ---
function main() {
  console.log('🔍 edit质检Start...\n');

  const report = {
    timestamp: new Date().toISOString(),
    outputDir,
    totalSegments: segments.length,
    checks: {}
  };

  // 检查 1
  const restored = checkRestoredSentences();
  report.checks.restoredSentences = restored;
  console.log(`✅ 检查1: restore完整性 — ${restored.restoredCount} 个restore，${restored.issues.length} 个Issue`);
  if (restored.issues.length > 0) {
    restored.issues.forEach(i => {
      console.log(`   ⚠️  s${i.sentenceIdx}   "${i.wordTexts}" (${i.wordCount}word, ${i.segmentDuration}s) 被 segment [${i.coveringSegment[0].toFixed(2)}-${i.coveringSegment[1].toFixed(2)}] 覆盖`);
    });
  }

  // 检查 2
  const manual = checkManualDeletions();
  report.checks.manualDeletions = manual;
  console.log(`\n✅ 检查2: use 户手动delete — ${manual.checkedCount} 项检查，${manual.issues.length} 个Issue`);
  if (manual.issues.length > 0) {
    manual.issues.slice(0, 10).forEach(i => {
      if (i.type === 'manual_sentence_not_deleted') {
        console.log(`   ⚠️  s${i.sentenceIdx} 整delete未生效 [${i.timeRange[0].toFixed(2)}-${i.timeRange[1].toFixed(2)}]`);
      } else {
        console.log(`   ⚠️  s${i.sentenceIdx} "${i.text}" (${i.category}) 未被覆盖`);
      }
    });
    if (manual.issues.length > 10) console.log(`   ... 还有 ${manual.issues.length - 10} 个`);
  }

  // 检查 3
  const silences = checkCutPointSilences();
  report.checks.cutPointSilences = silences;
  console.log(`\n✅ 检查3: 切点silence — ${silences.issues.length} 个可疑pause`);
  if (silences.issues.length > 0) {
    silences.issues.slice(0, 10).forEach(i => {
      console.log(`   ⏸️  [${i.gapStart.toFixed(2)}-${i.gapEnd.toFixed(2)}] ${i.duration}s silence — "${i.beforeText}" → "${i.afterText}"`);
    });
    if (silences.issues.length > 10) console.log(`   ... 还有 ${silences.issues.length - 10} 个`);
  }

  // 检查 4
  const large = checkLargeDeletions();
  report.checks.largeDeletions = large;
  console.log(`\n✅ 检查4: 大segmentdelete — ${large.issues.length} segment (>5s) needs 人工confirm衔接`);
  if (large.issues.length > 0) {
    large.issues.forEach(i => {
      console.log(`   ✂️  [${i.start.toFixed(1)}-${i.end.toFixed(1)}s] ${i.duration}s — "...${i.beforeText}" → "${i.afterText}..."`);
    });
  }

  // 汇总
  const totalIssues = restored.issues.length + manual.issues.length + silences.issues.length;
  console.log(`\n${'─'.repeat(50)}`);
  if (totalIssues === 0) {
    console.log('🎉 质检通！未发现自动可detect Issue。');
    console.log(`   (${large.issues.length} segment大segmentdelete建议人工confirm衔接)`);
  } else {
    console.log(`⚠️  发现 ${totalIssues} 个IssueneedsFix：`);
    if (restored.issues.length) console.log(`   - ${restored.issues.length} 个restore被覆盖`);
    if (manual.issues.length) console.log(`   - ${manual.issues.length} 个手动delete未生效`);
    if (silences.issues.length) console.log(`   - ${silences.issues.length} 个切点silencepause`);
    console.log(`   + ${large.issues.length} segment大segmentdelete建议人工confirm`);
  }

  // save报告
  const reportPath = path.join(analysisDir, 'audit_report.json');
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.log(`\n📄 完整报告: ${reportPath}`);

  // exit码：有Issue返回 1
  process.exit(totalIssues > 0 ? 1 : 0);
}

main();
