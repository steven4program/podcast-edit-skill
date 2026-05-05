#!/usr/bin/env node
/**
 * Phase C: semantic-layer QA — semantic_review.js
 *
 * Re-transcribe the edited audio and align it word-by-word against the expected
 * text via LCS, then surface residual fillers, residual stutters, semantic
 * breaks, and missing content.
 *
 * Usage:
 *   node semantic_review.js \
 *     --new-words <new_subtitles_words.json> \
 *     --original-words <original_subtitles_words.json> \
 *     --delete-segments <delete_segments_edited.json> \
 *     --sentences <sentences.txt> \
 *     --output <qa_semantic_report.json>
 *
 * Inputs:
 *   - new_subtitles_words.json: re-transcription of the edited audio (word-level timestamps)
 *   - original_subtitles_words.json: transcription of the source audio (word-level timestamps)
 *   - delete_segments_edited.json: final list of delete segments
 *   - sentences.txt: sentence segmentation of the source
 *
 * Output:
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

// --- Constants ---

// Patterns are matched against FunASR output (simplified Chinese) — keep them simplified.
const FILLER_PATTERNS = /^(嗯|啊|呃|那個|OK|就是|然後|所以説|OKOKOK)$/;
const STUTTER_MIN_LENGTH = 1;  // minimum repeated character count

// --- Core functions ---

/**
 * Compute "expected kept text" from the source transcription: original minus delete segments.
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
 * Word-level LCS alignment.
 * Returns: { matched, onlyInExpected, onlyInActual }
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

  // Backtrack
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
  // Simplification: unmatched words in actual are not extracted precisely yet.
  const matchedActualIndices = new Set(matched.map((_, idx) => idx));
  // TODO: more precise extraction of actual-only words

  return { matched, onlyInExpected, onlyInActual };
}

/**
 * C1: residual filler detection
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
 * C2: residual stutter detection (adjacent repeated words)
 */
function checkResidualStutters(newWords) {
  const issues = [];
  for (let i = 1; i < newWords.length; i++) {
    const prev = (newWords[i - 1].text || newWords[i - 1].word || '').trim();
    const curr = (newWords[i].text || newWords[i].word || '').trim();
    if (prev.length >= STUTTER_MIN_LENGTH && prev === curr) {
      // Only counts as a stutter when the gap is under 0.5s
      if (newWords[i].start - newWords[i - 1].end < 0.5) {
        issues.push({
          time: newWords[i - 1].start,
          text: `${prev}${curr}`,
          context: `重複："${prev}" × 2`
        });
      }
    }
  }
  return issues;
}

/**
 * C4: missing content detection (continuous runs of words present in expected
 * but absent from the actual transcription via LCS).
 */
function checkMissingContent(onlyInExpected) {
  const issues = [];
  if (onlyInExpected.length === 0) return issues;

  // Group consecutive missing words into segments
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

// --- Main ---

function main() {
  const opts = parseArgs();

  if (!opts.new_words || !opts.original_words || !opts.delete_segments || !opts.output) {
    console.error('Usage: node semantic_review.js --new-words <file> --original-words <file> --delete-segments <file> --sentences <file> --output <file>');
    process.exit(1);
  }

  console.log('Phase C: 語義層品質檢測');
  console.log('='.repeat(50));

  // Load inputs
  const newWords = JSON.parse(fs.readFileSync(opts.new_words, 'utf8'));
  const originalWords = JSON.parse(fs.readFileSync(opts.original_words, 'utf8'));
  const deleteSegments = JSON.parse(fs.readFileSync(opts.delete_segments, 'utf8'));

  // Extract word arrays (compatible with multiple input formats)
  const newWordList = Array.isArray(newWords) ? newWords :
    (newWords.words || newWords.subtitles?.flatMap(s => s.words) || []);
  const originalWordList = Array.isArray(originalWords) ? originalWords :
    (originalWords.words || originalWords.subtitles?.flatMap(s => s.words) || []);
  const segmentList = Array.isArray(deleteSegments) ? deleteSegments :
    (deleteSegments.segments || []);

  // Compute expected kept text
  console.log(`原始字數：${originalWordList.length}`);
  console.log(`刪除片段數：${segmentList.length}`);
  const expectedKept = computeExpectedText(originalWordList, segmentList);
  console.log(`預期保留字數：${expectedKept.length}`);
  console.log(`重新轉錄字數：${newWordList.length}`);

  // C1: residual fillers
  const residualFillers = checkResidualFillers(newWordList);
  console.log(`\nC1 殘留贅詞：${residualFillers.length} 個`);

  // C2: residual stutters
  const residualStutters = checkResidualStutters(newWordList);
  console.log(`C2 殘留卡頓：${residualStutters.length} 個`);

  // C3: semantic breaks — needs Claude evaluation; this script only flags cut points.
  // (Caller's Claude instance should evaluate after reading the report.)
  console.log(`C3 語義斷裂：需要 Claude 評估切點上下文`);

  // LCS alignment
  const { matched, onlyInExpected } = wordLevelLCS(expectedKept, newWordList);
  console.log(`\nLCS 比對字數：${matched.length}`);
  console.log(`預期中缺失字數：${onlyInExpected.length}`);

  // C4: missing content
  const missingContent = checkMissingContent(onlyInExpected);
  console.log(`C4 內容缺失片段：${missingContent.length} 個`);

  // Build report
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
      semantic_breaks: [], // filled in by Claude
      missing_content: missingContent
    },
    summary: {
      total_issues: residualFillers.length + residualStutters.length + missingContent.length,
      by_severity: {
        HIGH: missingContent.filter(m => m.expected.length > 10).length,
        MEDIUM: residualFillers.length + residualStutters.length,
        LOW: missingContent.filter(m => m.expected.length <= 10).length
      },
      note: 'C3 語義斷裂需 Claude 讀取切點上下文後評估，不在此腳本中自動偵測'
    }
  };

  fs.writeFileSync(opts.output, JSON.stringify(report, null, 2), 'utf8');
  console.log(`\n報告已寫入：${opts.output}`);
  console.log(`總問題數：${report.summary.total_issues} (HIGH: ${report.summary.by_severity.HIGH}, MEDIUM: ${report.summary.by_severity.MEDIUM})`);
}

main();
