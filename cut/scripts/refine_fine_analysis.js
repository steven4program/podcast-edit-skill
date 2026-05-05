#!/usr/bin/env node
/**
 * Refine the edit timestamps marked as _refinePoints in fine_analysis.json.
 *
 * Run after merge_llm_fine.js produces fine_analysis.json and before generating
 * the review page. Scans every edit's _refinePoints, calls refine_boundaries.py
 * to do waveform onset detection, and updates deleteStart/deleteEnd with the
 * refined results.
 *
 * Usage:
 *   node refine_fine_analysis.js --analysis-dir <dir> --audio <path>
 *
 * Args:
 *   --analysis-dir   directory containing fine_analysis.json
 *   --audio          source audio file path
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

// Parse args
let analysisDir = process.cwd();
let audioPath = null;

for (let i = 2; i < process.argv.length; i++) {
  if (process.argv[i] === '--analysis-dir' && process.argv[i + 1]) {
    analysisDir = path.resolve(process.argv[++i]);
  } else if (process.argv[i] === '--audio' && process.argv[i + 1]) {
    audioPath = path.resolve(process.argv[++i]);
  }
}

const fineAnalysisPath = path.join(analysisDir, 'fine_analysis.json');
const scriptDir = __dirname;
const refinePyPath = path.join(scriptDir, 'refine_boundaries.py');

if (!fs.existsSync(fineAnalysisPath)) {
  console.error(`❌ fine_analysis.json 不存在：${fineAnalysisPath}`);
  process.exit(1);
}

if (!audioPath) {
  // Default: try to locate the source audio
  const defaultAudio = path.join(analysisDir, '..', '1_transcript', 'audio_seekable.mp3');
  if (fs.existsSync(defaultAudio)) {
    audioPath = defaultAudio;
  } else {
    const defaultAudio2 = path.join(analysisDir, '..', '1_transcript', 'audio.mp3');
    if (fs.existsSync(defaultAudio2)) {
      audioPath = defaultAudio2;
    } else {
      console.error('❌ 未指定 --audio，且預設音訊路徑不存在');
      process.exit(1);
    }
  }
}

console.log(`🔍 Refine fine_analysis：onset detection 精修`);
console.log(`   fine_analysis：${fineAnalysisPath}`);
console.log(`   音訊：${audioPath}`);

// Load fine_analysis
const data = JSON.parse(fs.readFileSync(fineAnalysisPath, 'utf8'));
const edits = data.edits || [];

// Collect all _refinePoints
const allPoints = [];
const pointToEdit = []; // tracks which edit and point index each entry came from

for (let ei = 0; ei < edits.length; ei++) {
  const edit = edits[ei];
  if (!edit._refinePoints || edit._refinePoints.length === 0) continue;

  for (let pi = 0; pi < edit._refinePoints.length; pi++) {
    const pt = edit._refinePoints[pi];
    allPoints.push(pt);
    pointToEdit.push({ editIdx: ei, pointIdx: pi, type: pt.type });
  }
}

if (allPoints.length === 0) {
  console.log('   無需精修的切割點，略過');
  process.exit(0);
}

console.log(`   收集到 ${allPoints.length} 個待精修點`);

// Write a temporary JSON file
const tempPointsPath = path.join(analysisDir, '_refine_points_temp.json');
fs.writeFileSync(tempPointsPath, JSON.stringify(allPoints));

// Call refine_boundaries.py
let results;
try {
  const cmd = `python3 "${refinePyPath}" --audio "${audioPath}" --points-file "${tempPointsPath}"`;
  const stdout = execSync(cmd, {
    encoding: 'utf8',
    maxBuffer: 10 * 1024 * 1024,
    timeout: 120000
  });
  results = JSON.parse(stdout);
} catch (err) {
  console.error(`❌ refine_boundaries.py 執行失敗：`, err.message);
  // Clean up the temp file
  try { fs.unlinkSync(tempPointsPath); } catch (e) {}
  process.exit(1);
}

// Clean up the temp file
try { fs.unlinkSync(tempPointsPath); } catch (e) {}

// Apply the refined results
let applied = 0;
let skipped = 0;

for (let i = 0; i < results.length; i++) {
  const result = results[i];
  const { editIdx, type } = pointToEdit[i];
  const edit = edits[editIdx];

  if (result.confidence < 0.5) {
    skipped++;
    continue;
  }

  const delta = result.refined - result.original;
  if (Math.abs(delta) < 0.001) {
    skipped++;
    continue;
  }

  // Update the matching field by type (direction constraint: boundaries only move into the deletion range)
  if (type === 'partial_start' || type === 'filler_start') {
    const oldVal = edit.deleteStart ?? edit.ds ?? 0;
    // deleteStart can only move right (refined >= original)
    if (result.refined < oldVal - 0.001) {
      skipped++;
      continue;
    }
    edit._originalDeleteStart = oldVal;
    edit.deleteStart = result.refined;
    if (edit.ds != null) edit.ds = result.refined;
    applied++;
    console.log(`   ✅ Edit #${edit.idx} ${edit.type}: start ${oldVal.toFixed(4)} → ${result.refined.toFixed(4)} (Δ${(delta * 1000).toFixed(1)}ms)`);
  } else if (type === 'partial_end' || type === 'filler_end') {
    const oldVal = edit.deleteEnd ?? edit.de ?? 0;
    // deleteEnd can only move left (refined <= original)
    if (result.refined > oldVal + 0.001) {
      skipped++;
      continue;
    }
    edit._originalDeleteEnd = oldVal;
    edit.deleteEnd = result.refined;
    if (edit.de != null) edit.de = result.refined;
    applied++;
    console.log(`   ✅ Edit #${edit.idx} ${edit.type}: end ${oldVal.toFixed(4)} → ${result.refined.toFixed(4)} (Δ${(delta * 1000).toFixed(1)}ms)`);
  }
}

console.log(`\n📊 精修結果：${applied} applied, ${skipped} skipped (low confidence or no change)`);

// Back up the original file
const backupPath = fineAnalysisPath.replace('.json', '_pre_refine.json');
fs.copyFileSync(fineAnalysisPath, backupPath);
console.log(`   備份：${backupPath}`);

// Remove _refinePoints fields (already applied; not needed in output)
for (const edit of edits) {
  delete edit._refinePoints;
}

// Record refinement info in summary
if (!data.summary) data.summary = {};
data.summary.onsetDetection = {
  totalPoints: allPoints.length,
  applied,
  skipped
};

// Write back
fs.writeFileSync(fineAnalysisPath, JSON.stringify(data, null, 2));
console.log(`✅ 已更新 fine_analysis.json（${applied} 個切割點精修完成）`);
