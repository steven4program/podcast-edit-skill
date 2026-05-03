#!/usr/bin/env node
/**
 * 精修 fine_analysis.json 中标记 _refinePoints  edittime戳。
 *
 * 在 merge_llm_fine.js output fine_analysis.json 后、generate审查页之前执line。
 * 扫描所有edit  _refinePoints，调use  refine_boundaries.py do波形 onset detection，
 * use 精修resultupdate deleteStart/deleteEnd。
 *
 * Usage:
 *   node refine_fine_analysis.js --analysis-dir <dir> --audio <path>
 *
 * argument:
 *   --analysis-dir   include fine_analysis.json  directory
 *   --audio          sourceaudio filepath
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
  console.error(`❌ fine_analysis.json 不exists: ${fineAnalysisPath}`);
  process.exit(1);
}

if (!audioPath) {
  // default尝试找sourceaudio
  const defaultAudio = path.join(analysisDir, '..', '1_transcript', 'audio_seekable.mp3');
  if (fs.existsSync(defaultAudio)) {
    audioPath = defaultAudio;
  } else {
    const defaultAudio2 = path.join(analysisDir, '..', '1_transcript', 'audio.mp3');
    if (fs.existsSync(defaultAudio2)) {
      audioPath = defaultAudio2;
    } else {
      console.error('❌ 未指定 --audio，且default audio path does not exist');
      process.exit(1);
    }
  }
}

console.log(`🔍 Refine fine_analysis: onset detection 精修`);
console.log(`   fine_analysis: ${fineAnalysisPath}`);
console.log(`   audio: ${audioPath}`);

// 加载 fine_analysis
const data = JSON.parse(fs.readFileSync(fineAnalysisPath, 'utf8'));
const edits = data.edits || [];

// 收集所有 _refinePoints
const allPoints = [];
const pointToEdit = []; // 追踪each个 point 属于哪个 edit  and  point index

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
  console.log('   no refinement needed 切割点，跳');
  process.exit(0);
}

console.log(`   收集到 ${allPoints.length} 个待精修点`);

// 写临时 JSON file
const tempPointsPath = path.join(analysisDir, '_refine_points_temp.json');
fs.writeFileSync(tempPointsPath, JSON.stringify(allPoints));

// 调use  refine_boundaries.py
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
  console.error(`❌ refine_boundaries.py 执lineFailed:`, err.message);
  // 清理临时file
  try { fs.unlinkSync(tempPointsPath); } catch (e) {}
  process.exit(1);
}

// 清理临时file
try { fs.unlinkSync(tempPointsPath); } catch (e) {}

// 应use 精修result
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

  // 根据 type updateOK应charactersegment（方向约束：只允许边界向delete区域内部移动）
  if (type === 'partial_start' || type === 'filler_start') {
    const oldVal = edit.deleteStart ?? edit.ds ?? 0;
    // deleteStart 只能往右移（refined >= original）
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
    // deleteEnd 只能往左移（refined <= original）
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

console.log(`\n📊 精修result: ${applied} applied, ${skipped} skipped (low confidence or no change)`);

// 备份原file
const backupPath = fineAnalysisPath.replace('.json', '_pre_refine.json');
fs.copyFileSync(fineAnalysisPath, backupPath);
console.log(`   备份: ${backupPath}`);

// 清理 _refinePoints charactersegment（已应use ，不needskeep在output中）
for (const edit of edits) {
  delete edit._refinePoints;
}

// 在 summary 中record精修info
if (!data.summary) data.summary = {};
data.summary.onsetDetection = {
  totalPoints: allPoints.length,
  applied,
  skipped
};

// 写回
fs.writeFileSync(fineAnalysisPath, JSON.stringify(data, null, 2));
console.log(`✅ 已update fine_analysis.json（${applied} 个切割点精修Complete）`);
