#!/usr/bin/env node
/**
 * Phase C: 语义层质检 — semantic_review.js
 *
 * OKedit后audio 重transcription and 预期文本doword-level LCS OK齐，
 * detect残留filler、残留卡顿、语义断裂、content缺失。
 *
 * Usage:
 *   node semantic_review.js \
 *     --new-words <new_subtitles_words.json> \
 *     --original-words <original_subtitles_words.json> \
 *     --delete-segments <delete_segments_edited.json> \
 *     --sentences <sentences.txt> \
 *     --output <qa_semantic_report.json>
 *
 * input:
 *   - new_subtitles_words.json:  edit后audio 重transcription（word-leveltime戳）
 *   - original_subtitles_words.json: sourceaudio transcribe（word-leveltime戳）
 *   - delete_segments_edited.json: 最终deletesegmentcolumn表
 *   - sentences.txt: sourcesentence min 割
 *
 * output:
 *   qa_semantic_report.json
 */

const fs = require('fs');
const path = require('path');

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

// --- constant ---

const FILLER_PATTERNS = /^(嗯|啊|呃|那个|OK|就是|然后|所以说|OKOKOK)$/;
const STUTTER_MIN_LENGTH = 1;  // 最few重复character数

// --- 核心function ---

/**
 * 从sourcetranscribe中calculate"预期keep文本"：原文 - deletesegment
 */
function computeExpectedText(originalWords, deleteSegments) {
  const kept = [];
  for (const word of originalWords) {
    const isDeleted = deleteSegments.some(seg =>
      word.start >= seg.start - 0.01 && word.end <= seg.end + 0.01
    );
    if (!isDeleted) {
      kept.push(word);
    }
  }
  return kept;
}

/**
 * word-level LCS OK齐
 * 返回: { matched, onlyInExpected, onlyInActual }
 */
function wordLevelLCS(expected, actual) {
  const expTexts = expected.map(w => w.text || w.word);
  const actTexts = actual.map(w => w.text || w.word);

  const m = expTexts.length;
  const n = actTexts.length;

  // LCS DP
  const dp = Array(m + 1).fill(null).map(() => Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (expTexts[i - 1] === actTexts[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }

  // 回溯
  const matched = [];
  const onlyInExpected = [];
  let i = m, j = n;
  while (i > 0 && j > 0) {
    if (expTexts[i - 1] === actTexts[j - 1]) {
      matched.unshift({ expected: expected[i - 1], actual: actual[j - 1] });
      i--; j--;
    } else if (dp[i - 1][j] > dp[i][j - 1]) {
      onlyInExpected.unshift(expected[i - 1]);
      i--;
    } else {
      j--;
    }
  }
  while (i > 0) {
    onlyInExpected.unshift(expected[i - 1]);
    i--;
  }

  const onlyInActual = [];
  // 简化：actual 中未match word
  const matchedActualIndices = new Set(matched.map((_, idx) => idx));
  // TODO: 更精确  actual-only extract

  return { matched, onlyInExpected, onlyInActual };
}

/**
 * C1: 残留fillerdetect
 */
function checkResidualFillers(newWords) {
  const issues = [];
  for (const word of newWords) {
    const text = (word.text || word.word || '').trim();
    if (FILLER_PATTERNS.test(text)) {
      issues.push({
        time: word.start,
        text,
        context: `...${text}...`
      });
    }
  }
  return issues;
}

/**
 * C2: 残留卡顿detect（相邻重复word）
 */
function checkResidualStutters(newWords) {
  const issues = [];
  for (let i = 1; i < newWords.length; i++) {
    const prev = (newWords[i - 1].text || newWords[i - 1].word || '').trim();
    const curr = (newWords[i].text || newWords[i].word || '').trim();
    if (prev.length >= STUTTER_MIN_LENGTH && prev === curr) {
      // 间隔小于 0.5s 才算卡顿
      if (newWords[i].start - newWords[i - 1].end < 0.5) {
        issues.push({
          time: newWords[i - 1].start,
          text: `${prev}${curr}`,
          context: `重复: "${prev}" × 2`
        });
      }
    }
  }
  return issues;
}

/**
 * C4: content缺失detect（LCS 中预期exists但实际缺失 连续segment）
 */
function checkMissingContent(onlyInExpected) {
  const issues = [];
  if (onlyInExpected.length === 0) return issues;

  // merge连续缺失word为segment
  let currentGroup = [onlyInExpected[0]];
  for (let i = 1; i < onlyInExpected.length; i++) {
    const prev = currentGroup[currentGroup.length - 1];
    const curr = onlyInExpected[i];
    if (curr.start - prev.end < 1.0) {
      currentGroup.push(curr);
    } else {
      if (currentGroup.length >= 3) {
        const text = currentGroup.map(w => w.text || w.word).join('');
        issues.push({
          expected: text,
          time_range: [currentGroup[0].start, currentGroup[currentGroup.length - 1].end]
        });
      }
      currentGroup = [curr];
    }
  }
  if (currentGroup.length >= 3) {
    const text = currentGroup.map(w => w.text || w.word).join('');
    issues.push({
      expected: text,
      time_range: [currentGroup[0].start, currentGroup[currentGroup.length - 1].end]
    });
  }

  return issues;
}

// --- 主逻辑 ---

function main() {
  const opts = parseArgs();

  if (!opts.new_words || !opts.original_words || !opts.delete_segments || !opts.output) {
    console.error('Usage: node semantic_review.js --new-words <file> --original-words <file> --delete-segments <file> --sentences <file> --output <file>');
    process.exit(1);
  }

  console.log('Phase C: 语义层质检');
  console.log('='.repeat(50));

  // readinput
  const newWords = JSON.parse(fs.readFileSync(opts.new_words, 'utf8'));
  const originalWords = JSON.parse(fs.readFileSync(opts.original_words, 'utf8'));
  const deleteSegments = JSON.parse(fs.readFileSync(opts.delete_segments, 'utf8'));

  // extractwordarray（兼容不同format）
  const newWordList = Array.isArray(newWords) ? newWords :
    (newWords.words || newWords.subtitles?.flatMap(s => s.words) || []);
  const originalWordList = Array.isArray(originalWords) ? originalWords :
    (originalWords.words || originalWords.subtitles?.flatMap(s => s.words) || []);
  const segmentList = Array.isArray(deleteSegments) ? deleteSegments :
    (deleteSegments.segments || []);

  // calculate预期keep文本
  console.log(`sourceword数: ${originalWordList.length}`);
  console.log(`deletesegment数: ${segmentList.length}`);
  const expectedKept = computeExpectedText(originalWordList, segmentList);
  console.log(`预期keepword数: ${expectedKept.length}`);
  console.log(`重transcribeword数: ${newWordList.length}`);

  // C1: 残留filler
  const residualFillers = checkResidualFillers(newWordList);
  console.log(`\nC1 残留filler: ${residualFillers.length} 个`);

  // C2: 残留卡顿
  const residualStutters = checkResidualStutters(newWordList);
  console.log(`C2 残留卡顿: ${residualStutters.length} 个`);

  // C3: 语义断裂 — needs Claude 评估，这里只标记切点位置
  // （by 调use 方  Claude 实例read report 后评估）
  console.log(`C3 语义断裂: needs  Claude 评估切点上下文`);

  // LCS OK齐
  const { matched, onlyInExpected } = wordLevelLCS(expectedKept, newWordList);
  console.log(`\nLCS matchword数: ${matched.length}`);
  console.log(`预期中缺失word数: ${onlyInExpected.length}`);

  // C4: content缺失
  const missingContent = checkMissingContent(onlyInExpected);
  console.log(`C4 content缺失segment: ${missingContent.length} 个`);

  // generate报告
  const report = {
    phase: 'C',
    timestamp: new Date().toISOString(),
    stats: {
      original_words: originalWordList.length,
      expected_kept: expectedKept.length,
      actual_words: newWordList.length,
      lcs_matched: matched.length,
      lcs_missing: onlyInExpected.length
    },
    checks: {
      residual_fillers: residualFillers,
      residual_stutters: residualStutters,
      semantic_breaks: [], // by  Claude 填充
      missing_content: missingContent
    },
    summary: {
      total_issues: residualFillers.length + residualStutters.length + missingContent.length,
      by_severity: {
        HIGH: missingContent.filter(m => m.expected.length > 10).length,
        MEDIUM: residualFillers.length + residualStutters.length,
        LOW: missingContent.filter(m => m.expected.length <= 10).length
      },
      note: 'C3 语义断裂needs Claude read切点上下文后评估，不在此脚本中自动detect'
    }
  };

  fs.writeFileSync(opts.output, JSON.stringify(report, null, 2), 'utf8');
  console.log(`\n报告已write: ${opts.output}`);
  console.log(`总Issue数: ${report.summary.total_issues} (HIGH: ${report.summary.by_severity.HIGH}, MEDIUM: ${report.summary.by_severity.MEDIUM})`);
}

main();
