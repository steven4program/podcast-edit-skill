#!/usr/bin/env node
/**
 * Append a Noise-events review panel to an existing review_enhanced.html.
 * Idempotent: removes any prior injection before re-injecting.
 *
 * Usage:
 *   node inject_noise_panel.js \
 *     --html path/to/review_enhanced.html \
 *     --noise path/to/hogan_events.json [--noise ...] \
 *     [--original-mp3 3_output/episode_merged.mp3] \
 *     [--noise-removed-mp3 3_output_with_noise/episode_merged.mp3]
 */
const fs = require('fs');

const args = {};
const noiseFiles = [];
for (let i = 2; i < process.argv.length; i++) {
  const k = process.argv[i];
  if (k === '--noise') noiseFiles.push(process.argv[++i]);
  else if (k.startsWith('--')) args[k.slice(2)] = process.argv[++i];
}
if (!args.html || noiseFiles.length === 0) {
  console.error('usage: --html <file> --noise <file>... [--original-mp3 ...] [--noise-removed-mp3 ...]');
  process.exit(1);
}

const events = [];
for (const f of noiseFiles) {
  const d = JSON.parse(fs.readFileSync(f, 'utf8'));
  for (const ev of d.events || []) events.push(ev);
}

const MARK_BEGIN = '<!-- NOISE_PANEL_BEGIN -->';
const MARK_END = '<!-- NOISE_PANEL_END -->';

let html = fs.readFileSync(args.html, 'utf8');
const re = new RegExp(`${MARK_BEGIN}[\\s\\S]*?${MARK_END}\\s*`, 'g');
html = html.replace(re, '');

const block = `${MARK_BEGIN}
<style>
  #noise-panel { position: fixed; right: 14px; bottom: 14px; width: 400px; max-height: 80vh;
    background: #fff; border: 1px solid #ddd; border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.12);
    z-index: 9999; font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 13px;
    display: flex; flex-direction: column; }
  #noise-panel header { padding: 10px 14px; border-bottom: 1px solid #eee; display: flex;
    align-items: center; justify-content: space-between; cursor: pointer; }
  #noise-panel header h3 { margin: 0; font-size: 14px; }
  #noise-panel .body { overflow-y: auto; padding: 8px 10px; }
  #noise-panel.collapsed .body, #noise-panel.collapsed .compare { display: none; }
  #noise-panel .compare { padding: 8px 10px; border-top: 1px solid #eee; display: flex; gap: 6px; flex-wrap: wrap; }
  #noise-panel .compare audio { width: 100%; }
  .ne-item { padding: 8px; border-bottom: 1px solid #f3f3f3; }
  .ne-item:last-child { border-bottom: none; }
  .ne-row { display: flex; align-items: center; gap: 6px; }
  .ne-badge { padding: 1px 6px; border-radius: 4px; font-size: 11px; color: #fff; }
  .ne-v-confirmed { background: #dc2626; }
  .ne-v-false_positive { background: #6b7280; }
  .ne-v-borderline { background: #f59e0b; }
  .ne-v-candidate { background: #2563eb; }
  .ne-class { font-weight: 600; }
  .ne-time { color: #6b7280; font-variant-numeric: tabular-nums; }
  .ne-speaker { font-size: 11px; color: #2563eb; }
  .ne-expl { color: #4b5563; margin-top: 4px; font-size: 12px; }
  .ne-btn { padding: 2px 8px; border: 1px solid #ddd; background: #f9fafb; border-radius: 4px;
    font-size: 11px; cursor: pointer; }
  .ne-btn:hover { background: #eef2ff; }
  #noise-panel .summary { padding: 6px 10px; background: #fafafa; font-size: 12px; color: #4b5563;
    border-bottom: 1px solid #eee; }
  .ne-score { font-weight:600; padding:1px 5px; border-radius:3px; background:#f3f4f6; color:#111827; font-size:11px; }
  .ne-score.high { background:#fee2e2; color:#991b1b; }
  .ne-score.med { background:#fef3c7; color:#92400e; }
  #noise-panel .filter-row { padding: 6px 10px; display: flex; gap: 4px; border-bottom: 1px solid #eee; flex-wrap:wrap; }
  #noise-panel .sort-row { padding: 4px 10px; font-size: 11px; color:#6b7280; border-bottom: 1px solid #eee; }
  #noise-panel .filter-row button { padding: 2px 8px; border: 1px solid #ddd; background: #fff;
    border-radius: 4px; font-size: 11px; cursor: pointer; }
  #noise-panel .filter-row button.active { background: #2563eb; color: #fff; border-color: #2563eb; }
  .ne-check { margin-right: 6px; transform: scale(1.15); cursor: pointer; }
  .ne-item.selected { background: #fef3c7; }
  #noise-panel .actions { padding: 8px 10px; border-top: 1px solid #eee; display: flex; gap: 6px; flex-wrap: wrap; }
  #noise-panel .actions button { padding: 4px 10px; border: 1px solid #2563eb; background: #fff; color: #2563eb; border-radius: 4px; font-size: 12px; cursor: pointer; }
  #noise-panel .actions button.primary { background: #2563eb; color: #fff; }
  #noise-panel .actions button:hover { background: #1d4ed8; color: #fff; }
  #noise-panel .sel-summary { padding: 6px 10px; background: #fffbeb; border-bottom: 1px solid #fde68a; font-size: 12px; color: #92400e; }
</style>

<div id="noise-panel">
  <header onclick="(function(){document.getElementById('noise-panel').classList.toggle('collapsed');})()">
    <h3>🔊 噪音事件 (YAMNet + Gemini)</h3>
    <span id="ne-toggle">▾</span>
  </header>
  <div class="summary" id="ne-summary"></div>
  <div class="sel-summary" id="ne-sel-summary"></div>
  <div class="filter-row">
    <button data-filter="all" class="active" onclick="neSetFilter('all')">全部</button>
    <button data-filter="selected" onclick="neSetFilter('selected')">已選</button>
    <button data-filter="candidate" onclick="neSetFilter('candidate')">候選</button>
    <button data-filter="confirmed" onclick="neSetFilter('confirmed')">已確認</button>
    <button data-filter="false_positive" onclick="neSetFilter('false_positive')">誤判</button>
    <button data-filter="borderline" onclick="neSetFilter('borderline')">邊界</button>
    <button onclick="neSetSort('score')">分數排</button>
    <button onclick="neSetSort('time')">時間排</button>
  </div>
  <div class="sort-row" id="ne-sort-label">依分數高→低排序</div>
  <div class="body" id="ne-list"></div>
  <div class="actions">
    <button onclick="neSelectAllVisible()">全選顯示</button>
    <button onclick="neSelectByScore()">選 score ≥ 0.5</button>
    <button onclick="neClearAll()">全清</button>
    <button class="primary" onclick="neDownload()">⬇ 下載刪除清單</button>
    <button onclick="neCopyJSON()">📋 複製 JSON</button>
  </div>
  <div class="compare">
    <div style="font-size:12px; color:#4b5563; width:100%; margin-bottom:4px;">A/B 試聽（成片）：</div>
    ${args['original-mp3'] ? `<div style="width:100%"><div style="font-size:11px;color:#6b7280">原版（無噪音層）</div><audio controls preload="none" src="${args['original-mp3']}"></audio></div>` : ''}
    ${args['noise-removed-mp3'] ? `<div style="width:100%"><div style="font-size:11px;color:#6b7280">噪音層移除版</div><audio controls preload="none" src="${args['noise-removed-mp3']}"></audio></div>` : ''}
  </div>
</div>

<script>
(function(){
  const events = ${JSON.stringify(events)};
  // Each event gets a stable id from start+end+speaker (resilient to re-injection)
  for (const e of events) e._id = (e.speaker||'')+':'+e.start.toFixed(3)+'-'+e.end.toFixed(3);
  const STORAGE_KEY = 'ne_selected_v1';
  const selected = new Set(JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'));
  let filter = 'all';

  function fmt(t){ const m = Math.floor(t/60); const s = (t%60).toFixed(2); return m+":"+(s.padStart(5,'0')); }
  function saveSelection(){ localStorage.setItem(STORAGE_KEY, JSON.stringify([...selected])); }
  function toggleSel(id){ if (selected.has(id)) selected.delete(id); else selected.add(id); saveSelection(); renderSelSummary(); render(); }
  window.neToggle = toggleSel;

  function playRange(start, end){
    // Use the existing source/balanced player if present
    const audio = document.querySelector('audio');
    if (!audio) return;
    audio.currentTime = Math.max(0, start - 0.3);
    audio.play();
    const stopAt = end + 0.5;
    const stopper = () => {
      if (audio.currentTime >= stopAt) { audio.pause(); audio.removeEventListener('timeupdate', stopper); }
    };
    audio.addEventListener('timeupdate', stopper);
  }
  window.nePlay = playRange;

  let sortMode = 'score'; // 'score' or 'time'
  let lastFilteredVisible = [];
  function render(){
    const list = document.getElementById('ne-list');
    let filtered = events.filter(e => {
      if (filter === 'all') return true;
      if (filter === 'selected') return selected.has(e._id);
      return e.verdict === filter;
    });
    if (sortMode === 'score') filtered = filtered.slice().sort((a,b) => (b.score||b.confidence||0) - (a.score||a.confidence||0));
    else filtered = filtered.slice().sort((a,b) => a.start - b.start);
    lastFilteredVisible = filtered;
    list.innerHTML = filtered.map((e, i) => {
      const dur = (e.end - e.start).toFixed(2);
      const speaker = e.speaker ? '<span class="ne-speaker">'+e.speaker+'</span>' : '';
      const sc = +(e.score ?? e.confidence ?? 0);
      const scCls = sc >= 0.5 ? 'high' : (sc >= 0.3 ? 'med' : '');
      const isSel = selected.has(e._id);
      const idEsc = e._id.replace(/'/g, "\\\\'");
      return '<div class="ne-item'+(isSel?' selected':'')+'">' +
        '<div class="ne-row">' +
          '<input type="checkbox" class="ne-check"'+(isSel?' checked':'')+' onclick="neToggle(\\''+idEsc+'\\')">' +
          '<span class="ne-score '+scCls+'">'+sc.toFixed(2)+'</span>' +
          '<span class="ne-badge ne-v-'+e.verdict+'">'+e.verdict+'</span>' +
          '<span class="ne-class">'+(e.class || e.yamnet_class)+'</span>' +
          speaker +
          '<span class="ne-time">'+fmt(e.start)+' · '+dur+'s</span>' +
          '<button class="ne-btn" onclick="nePlay('+e.start+','+e.end+')">▶</button>' +
        '</div>' +
        (e.explanation ? '<div class="ne-expl">'+e.explanation.replace(/</g,'&lt;')+'</div>' : '') +
      '</div>';
    }).join('');
    if (!filtered.length) list.innerHTML = '<div style="padding:14px;color:#9ca3af;text-align:center">沒有符合的事件</div>';
    document.getElementById('ne-sort-label').textContent = sortMode === 'score' ? '依分數高→低排序' : '依時間排序';
  }
  function renderSelSummary(){
    let total = 0;
    const selList = events.filter(e => selected.has(e._id));
    for (const e of selList) total += (e.end - e.start);
    document.getElementById('ne-sel-summary').innerHTML =
      '已選 <b>'+selList.length+'</b> / '+events.length+' · 總長 <b>'+total.toFixed(2)+'s</b>' +
      (selList.length ? '  (LocalStorage 持久化)' : '');
  }

  window.neSelectAllVisible = function(){
    for (const e of lastFilteredVisible) selected.add(e._id);
    saveSelection(); renderSelSummary(); render();
  };
  window.neSelectByScore = function(){
    const threshold = parseFloat(prompt('Score 門檻？(預設 0.5)', '0.5'));
    if (isNaN(threshold)) return;
    for (const e of events) {
      const sc = +(e.score ?? e.confidence ?? 0);
      if (sc >= threshold) selected.add(e._id);
    }
    saveSelection(); renderSelSummary(); render();
  };
  window.neClearAll = function(){
    if (!selected.size || confirm('確定清掉 '+selected.size+' 筆選擇？')) {
      selected.clear(); saveSelection(); renderSelSummary(); render();
    }
  };
  function buildDeleteJSON(){
    const selList = events.filter(e => selected.has(e._id)).slice().sort((a,b) => a.start - b.start);
    // Mark as verdict=confirmed so merge_noise_into_deletes.js will pick them up.
    const out = selList.map(e => ({
      start: e.start, end: e.end,
      class: e.class || e.yamnet_class,
      speaker: e.speaker || null,
      verdict: 'confirmed',
      is_speech_overlapped: false,
      confidence: e.confidence ?? e.score ?? 0,
      explanation: e.explanation || '',
      source: e.source || 'manual',
    }));
    return { audio_file: '', model: 'manual-selection', events: out,
             summary: { total: out.length, confirmed: out.length, false_positives: 0, borderline: 0, candidates: 0 } };
  }
  window.neDownload = function(){
    const data = buildDeleteJSON();
    if (!data.events.length) { alert('沒有勾選任何事件'); return; }
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const ts = new Date().toISOString().replace(/[:.]/g,'-').slice(0,19);
    a.href = url; a.download = 'noise_deletes_'+ts+'.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 500);
  };
  window.neCopyJSON = function(){
    const data = buildDeleteJSON();
    if (!data.events.length) { alert('沒有勾選任何事件'); return; }
    navigator.clipboard.writeText(JSON.stringify(data, null, 2)).then(() => {
      alert('已複製 '+data.events.length+' 筆事件到剪貼簿');
    }, () => alert('複製失敗，用下載按鈕'));
  };
  window.neSetSort = function(m){ sortMode = m; render(); };

  function renderSummary(){
    const cand = events.filter(e=>e.verdict==='candidate').length;
    const c = events.filter(e=>e.verdict==='confirmed').length;
    const fp = events.filter(e=>e.verdict==='false_positive').length;
    const b = events.filter(e=>e.verdict==='borderline').length;
    document.getElementById('ne-summary').innerHTML =
      '總計 <b>'+events.length+'</b>' +
      (cand ? ' · 候選 <b style="color:#2563eb">'+cand+'</b>' : '') +
      (c ? ' · 確認 <b style="color:#dc2626">'+c+'</b>' : '') +
      (fp ? ' · 誤判 <b>'+fp+'</b>' : '') +
      (b ? ' · 邊界 <b>'+b+'</b>' : '');
  }

  window.neSetFilter = function(f){
    filter = f;
    document.querySelectorAll('#noise-panel .filter-row button').forEach(b => {
      b.classList.toggle('active', b.dataset.filter === f);
    });
    render();
  };

  renderSummary();
  renderSelSummary();
  render();
})();
</script>
${MARK_END}
`;

html = html.replace('</body>', block + '\n</body>');
fs.writeFileSync(args.html, html);
console.log(`✅ injected ${events.length} noise events into ${args.html}`);
