# Non-Speech Vocal Detection (清喉嚨 / 清鼻子等聲響偵測)

**Date**: 2026-05-18
**Status**: Design — pending user review before implementation plan
**Scope**: `cut/` skill only. QA and polish unaffected.

---

## 1. Background & Problem Statement

### The user's pain
主持人習慣性在句前或句尾發出短暫的清喉嚨、清鼻子等非語音聲響（偶爾出現在停頓中）。使用者試過多種方法都無法有效清除：不是過度剪輯（殺到正常呼吸/語氣）就是漏掉很多（撈不到實際存在的事件）。

### Why the existing pipeline can't catch these
Pipeline 既有三條偵測線，**沒有任何一條能看到這些事件**：

| 偵測線 | 看的對象 | 為何漏 |
|---|---|---|
| Filler 規則層 (`2-filler-detection.md`) | Whisper 寫出來的字 | 清喉嚨常常 Whisper 不轉錄、或轉成 "嗯" 但 filler 規則「預設保留單字 filler」 |
| Silence 規則層 + `trim_silences.py` | 能量低於門檻的區段 | 清喉嚨是**有能量**的事件，不會落入 silence |
| QA 訊號層 (`signal_analysis.py`) | 剪輯接縫處的能量跳變 | 偵測「剪壞了」而非「該不該剪」；`breath_truncation` 因誤報過多在 podcast mode 被關閉 |

→ 結構性盲區：這些是**非語音類人聲事件**，既不是文字也不是靜音也不是接縫雜訊。**任何只看文字 + 只看能量的 pipeline 都看不見它**。

---

## 2. Spike Findings (2026-05-17/18)

實測樣本：`source/host-ted-5min.wav`，5 分鐘單軌主持人音檔。

### 2.1 既有頻譜啟發式（前人實驗的孤兒輸出 `nonverbal_events.json`）
- 166 個 raw events，其中 30 個落在 pure_gap / partial_overlap（用戶痛點分布的合理區段）
- 對這 30 個樣本，使用者第一輪審聽 → **8 OK / 22 BAD = 27% precision**
- 比較 OK 和 BAD 的頻譜特徵：spectral_centroid、spectral_flatness、rms_db、zcr **完全重疊**
- **致命結論**：feature space 沒有可分離邊界，**沒有任何門檻調整能拯救這條路**

### 2.2 YAMNet (Google AudioSet 預訓練)
- `Throat clearing` 類別在整段音訊上最高機率 **0.014**（基本上 = 0）
- 所有非語音類別最高機率 < 0.2
- `Speech` 在 625 個 frame 中有 386 個以 1.0 機率主導
- 失敗原因：**weak-label audio tagging + 0.96s 固定 receptive field + 訓練分佈不匹配**。YAMNet 輸出是 multi-label sigmoid（各類獨立），各類分數理論上互不擠壓。觀察到的 throat_clear ≈ 0 現象不是數學壓制，而是 **representation-level domination**：在強語音背景中，0.96s window 內部 feature extractor 被 dominant signal 主導，短暫非語音事件的特徵在最終 embedding 中被稀釋，class head 無從激活。AudioSet 訓練資料是 clip-level 弱標籤，模型學的是「window 的代表性內容」而非「window 內有什麼次要事件」

### 2.3 Whisper non-speech tokens
- 重跑 Whisper-large-v3，`vad_filter=False, suppress_tokens=[]`
- 整段音訊出現的非語音標記（`[Music]`、`[cough]` 等）：**0**
- 13 個已知清喉嚨位置，Whisper 輸出任何相關標記：**0**
- 失敗原因：Whisper 的 Mandarin 訓練資料中**完全沒有**括號標註，模型從沒學過要輸出這些 token

### 2.4 SenseVoice (FunAudioLLM / Alibaba)
- 內建 audio event detection (Cough、Breath、Sneeze、Laughter)
- 對 Mandarin podcast 是現成、本地、免費的方案
- 對 14 個已知位置：**recall 0/14**，全部標成 `<|Speech|>`
- 失敗原因：跟 YAMNet 同種 **representation domination**——segment-level prediction 在強語音背景下，內部表徵被語音主導，event tag head 無法激活；夾雜在語音中的非語音事件被吞掉

### 2.5 其他繁中優化方案調查
| 工具 | 結果 |
|---|---|
| Yating (台灣 AI Labs) | ASR-only，無 event detection 功能 |
| FunASR / Paraformer | 同 SenseVoice 家族架構，預期同 0% recall |
| Qwen3-ASR (Alibaba 新版) | ASR-only |
| MediaTek Breeze-ASR-25 | Whisper-TW 變體，預期同 Whisper 失敗模式 |

→ **架構性結論：所有 ASR / 音訊分類器都被訓練成「辨識主要內容」，因此無法處理「主要內容是語音、夾雜次要非語音事件」的場景**。

### 2.6 Gemini-2.5-flash（多模態 LLM）
- 30s chunk × 5s overlap，全程掃描 5 分鐘音訊（含 1 個 503 重跑共 12+1 chunks）
- 50 個 events 分布：throat_clear 9、nose_clear 10、click_smack 19、sharp_breath 12
- 用戶第二輪審聽結果：

| 類別 | OK | BAD | Skip | Precision | 結論 |
|---|---|---|---|---|---|
| **throat_clear** | 6 | 3 | 0 | **67%** | 主痛點，可用 |
| **nose_clear** | 5 | 4 | 1 | **56%** | 主痛點，可用 |
| click_smack | 4 | 10 | 5 | 29% | 雜訊太多，排除 |
| sharp_breath | 1 | 11 | 0 | 8% | 嚴重誤報，排除 |

- 兩個主痛點合計 precision **58%**
- 兩輪合併估算 recall **68%**
- 成本：~$0.015 / 小時音訊

→ **唯一可用方案**。架構上是 LLM 的「描述聽到的全部」訓練目標讓它能識別 dominant class 之外的次要事件。

### 2.7 BAD 樣本根因分析（指導後處理 filter 設計）
| BAD 事件 | 根因 |
|---|---|
| g-7, g-8 (throat_clear) | **主持人在「模仿」清喉嚨聲音當作對話內容**，Whisper 同位置轉錄為 "口口口"。聲學正確、語意錯誤 |
| g-2 (throat_clear) | 位於 18s 長 gap 中央，會被 `trim_silences.py` 整段切掉，獨立列為 edit 是重複 |
| g-15 (nose_clear) | 與下個字距離 0.04s，剪下去**會啃到下一個字 onset** |
| g-34~g-37 (click_smack) | 4 個事件在 3 秒內群聚發生，是連續講話自然口腔摩擦聲、非離散干擾 |
| g-11, g-13, g-16 (nose_clear) | **個人聽感差異**——Gemini 對、用戶不想刪。這 30-40% 是無法靠偵測器解決的主觀層 |

→ 4 個可修的工程性 BAD（佔 BAD 約 50%）、剩下是主觀層。

---

## 3. Architecture Decision

### Approach
**新增 Gemini-based 非語音事件偵測階段**，輸出消耗自 `subtitles_words.json`、生產 `non_speech_vocals.json`，由 `run_fine_analysis.js` 整合進 fine cut。

### Why this approach (vs alternatives)
- Heuristic / YAMNet / SenseVoice / Whisper non-speech tokens — **全部實測 0-27%，全部架構性無解**
- 客製 SED（self-trained sound event detector）— **未來最強解**，但要的不是「fine-tune YAMNet 的 class head」（receptive field 限制無解），而是**用 YAMNet / AST / HeAR / CLAP 當 embedding backbone + 100-400ms 短窗 + 用戶 feedback 標註資料訓 binary detector / reranker**。需累積 ≥100 筆標註才有意義
- `gpt-audio` / `gpt-realtime` (OpenAI) — 同類多模態 audio LLM 競爭者，預期 ±10% 差距內，**可作為未來 ensemble 但目前 Gemini 已整合**。注意 OpenAI 的 `gpt-5` / `gpt-5.5` 主模型 **API 不收 audio input**——audio modality 走獨立的 `gpt-audio` / `gpt-realtime` 端點

### Design philosophy
**不做 auto-delete，做 reviewer assistant**。所有偵測結果以 `needsReview: true` / `enabled: false` 進入 review UI，使用者一鍵確認/拒絕。這符合 pipeline 既有的「filler 規則 → 標 needs_confirmation」哲學。

### Architecture guardian
本 spec 涉及多個跨檔修改。修改實作時，**同步更新**：
- `cut/SKILL.md` Stage 2 描述、Pipeline 圖
- `cut/editing-rules/README.md`（新增 rule pointer）
- `CLAUDE.md` 的 commands 區段、optional env var 區段（GEMINI_API_KEY 變必要）

---

## 4. Pipeline Integration

### Insertion point
```
Stage 2: transcribe → sentence split → AI rough cut → AI fine cut

新增子步驟（Stage 2.5）：
   subtitles_words.json
        │
        ├──► 既有：generate_sentences.js / 既有 fine analysis path
        │
        └──► 新增：detect_non_speech_vocals_gemini.py
              輸入：audio.mp3 (16k mono，與 Whisper 同來源；Gemini 接受 16kHz)
              輸出：2_analysis/non_speech_vocals.json

run_fine_analysis.js（修改）
     ↓ 同時讀 fine_analysis_rules.json + non_speech_vocals.json
     ↓ 合併 + 交叉
     ↓ fine_analysis.json （多了 non_speech_vocal:* edit）
     ↓ 既有路徑 → merge_llm_fine.js → refine_fine_analysis.js → review_enhanced.html
```

### Files added
- `cut/scripts/detect_non_speech_vocals_gemini.py` — 偵測主腳本
- `cut/scripts/non_speech_vocal_filters.py` — 後處理 filter (4 個 filter 共用模組)

### Files modified
- `cut/scripts/run_fine_analysis.js` — 載入 `non_speech_vocals.json` + 整合
- `cut/scripts/generate_review_enhanced.js` — 在 review UI 顯示 non_speech_vocal edit
- `cut/scripts/user_manager.js` — 新增 `non_speech_vocal` user-pref section
- `cut/editing-rules/` — 新增 `11-non-speech-vocal.md`
- `cut/SKILL.md`、`README.md`、`CLAUDE.md` — 文檔同步

### Files NOT touched
- `cut/scripts/transcribe_whisper_local.py` — 不影響轉錄
- `cut_audio.py` / `trim_silences.py` — 不影響最終剪輯
- QA / polish skill — 完全不動

---

## 5. The New Script: `detect_non_speech_vocals_gemini.py`

### Inputs
```
positional:
  base_dir          專案輸出根目錄，例 output/YYYY-MM-DD_audio/cut/

reads:
  {base_dir}/1_transcript/audio.mp3          作為 Gemini 輸入
  {base_dir}/1_transcript/subtitles_words.json   作為交叉參考
  cut/user-prefs/{userId}/preferences.yaml   讀使用者設定

writes:
  {base_dir}/2_analysis/non_speech_vocals.json    最終輸出
  {base_dir}/2_analysis/non_speech_vocals_raw.json   Gemini 原始回應（除錯用）

env:
  GEMINI_API_KEY               必要（從 .env 讀取，已有相同邏輯在 ai_listen.py）
  PODCAST_EDIT_USER            選用，影響 prefs 讀取
```

### Chunking strategy
- Chunk size: **30 秒**
- Hop: **25 秒**（5 秒 overlap，避免 chunk 邊界事件漏抓）
- 12 個 chunk / 5 min episode
- 144 個 chunk / 1 hour episode（含 overlap）

理由：30s 是 Gemini 對「描述+精準時間戳」的甜蜜點。更長 → 時間戳精度下降；更短 → token 浪費 + context 不足。

### Prompt (final)
```
You are an audio editor reviewing a clip from a Mandarin Chinese podcast.

The HOST has a recurring problem: brief NON-SPEECH VOCAL noises that listeners 
notice and want removed:
- 清喉嚨 / throat clearing — a short rasping, wet, or scratching sound (NOT speech)
- 清鼻子 / nose clearing / snort / sniff — a brief nasal sound

These typically happen:
- Right BEFORE a sentence (preparation sound)
- Right AFTER a sentence ends
- Occasionally in mid-pause

Do NOT flag the following (they are natural and intentional):
- Normal calm breathing between sentences
- Sharp inhalations before speaking (these are natural prep, NOT nuisance)
- Filler words "嗯", "啊", "呃", "對" (handled by a separate system)
- Lip/tongue sounds embedded inside actual speech
- Mouth clicks during continuous speaking
- Laughter, emotive sighs, deliberate emphasis
- Background hum, music, room tone
- The speaker MIMICKING or DESCRIBING throat-clearing sounds as content
  (e.g. if they say "聽起來像口口口的聲音", do NOT flag the imitation)

For each non-speech vocal nuisance, report:
- offset_s: seconds from the START of this clip (0.0 to clip duration)
- duration_s: estimated duration in seconds (typically 0.1-0.6)
- type: one of "throat_clear" or "nose_clear"
- confidence: NUMERIC value 0.0-1.0 (NOT "high"/"medium"/"low")
- description: short note in Chinese

Be conservative — if uncertain, do NOT flag it.

Return JSON only (no markdown, no commentary):
{"events": [
  {"offset_s": 12.3, "duration_s": 0.4, "type": "throat_clear", 
   "confidence": 0.9, "description": "句首濕潤的清喉嚨聲"}
]}

If nothing to flag, return: {"events": []}
```

**關鍵設計**：
- 明確排除 `click_smack` 和 `sharp_breath` 類別（spike 結果證實低精度）
- 明確排除主持人「模仿/描述」清喉嚨的情境（spike 中 g-7, g-8 的失敗案例）
- 明確要求 numeric confidence（spike 中 chunk 9 retry 出現 "high"/"medium" 字串問題）
- 描述要求**中文**輸出，方便 review UI 直接顯示

### Confidence normalization
即使 prompt 要求 numeric，仍要 defensive 處理：
```python
CONF_MAP = {"high": 0.85, "medium": 0.6, "low": 0.4, 
            "very high": 0.95, "very low": 0.2}
if isinstance(c, str):
    c = CONF_MAP.get(c.lower().strip(), 0.5)
```

### Failure handling (per chunk)
- **503 / quota exceeded**: exponential backoff retry (2s, 4s, 8s)，最多 3 次
- **連續 2 個 chunk 失敗**: 整個 stage 標記 `degraded: true`，仍寫出已成功的部分，pipeline 繼續
- **JSON parse 失敗**: 同 ai_listen 的 `parse_json_response` 策略（直接 parse → markdown block → bare object）
- **空白音訊 chunk**（音檔尾端短於 1s）: 跳過不發

### Deduplication (overlap merging)
相鄰 chunk 因 5s overlap 會重複偵測同事件：
```python
events.sort(key=lambda x: x["start"])
deduped = []
for ev in events:
    if (deduped and 
        ev["start"] - deduped[-1]["start"] < 0.5 and 
        ev["type"] == deduped[-1]["type"]):
        # Keep higher-confidence version
        if ev["confidence"] > deduped[-1]["confidence"]:
            deduped[-1] = ev
        continue
    deduped.append(ev)
```

### Output schema (`non_speech_vocals.json`)
```json
{
  "audio": "1_transcript/audio.mp3",
  "duration": 300.0,
  "model": "gemini-2.5-flash",
  "chunk_sec": 30,
  "hop_sec": 25,
  "generated_at": "2026-05-18T10:30:00Z",
  "degraded": false,
  "stats": {
    "total_chunks": 12,
    "failed_chunks": 0,
    "raw_events": 38,
    "after_dedup": 38,
    "after_filters": 19,
    "by_type": {"throat_clear": 9, "nose_clear": 10}
  },
  "events": [
    {
      "id": "nsv-0",
      "start": 26.45,
      "end": 26.85,
      "duration": 0.40,
      "type": "throat_clear",
      "confidence": 0.95,
      "description": "句首濕潤的清喉嚨聲",
      "zone": "pure_gap",
      "overlap_ratio": 0.0,
      "prev_word_end": 25.80,
      "next_word_start": 27.10,
      "refined_start": 26.48,
      "refined_end": 26.83,
      "filters_applied": [],
      "filter_decision": "ok"
    }
  ]
}
```

---

## 6. Post-Detection Filters

四個工程性 filter，目標把 throat+nose precision 從 58% 推到 75-85%。位於 `non_speech_vocal_filters.py`，pipeline 序執行。

### Filter 1: Whisper context check（解 g-7/g-8 — 主持人模仿）
- 對每個事件，在 Whisper transcript 上取事件時間 ±2s 內的所有字
- 若字串中包含模仿/描述標記：`口`（單字重複出現視為模仿）、`咳`、`清喉`、`嗯哼`、`那種聲音`、`聽起來像`、`類似`、`像是` —— **標記事件為 `filter_decision: "host_describing_sound"`，從 events 中移除**

### Filter 2: Long-gap subsume（解 g-2）
- 對每個事件，看其所在 word gap 大小（`next_word_start - prev_word_end`）
- 若 gap > 3s → **標記 `filter_decision: "subsumed_by_silence_trim"`，從 events 中移除**
- 理由：這整個 gap 會被既有 `silence_handling` rule 處理，獨立列為 edit 是重複

### Filter 3: Boundary too tight（解 g-15）
- 對每個事件，計算與下個 Whisper word 的距離 `dist_to_next = next_word_start - event.end`
- 若 `dist_to_next < 0.1s` 或 `event.duration < 0.15s` → **標記 `filter_decision: "boundary_too_tight"`**
- **保留事件但 enabled=false 強制 needsReview=true**（不是直接 remove，因為使用者可能想知道有這事件、只是難剪）

### Filter 4: Cluster detection（解 click_smack 連發場景）
- 同類型事件在 < 2s 內連續 ≥ 3 個 → 視為自然口腔摩擦聲，**標記 `filter_decision: "speech_artifact_cluster"`，從 events 中移除整組**

### Filter execution order
1. Whisper context check（先濾掉「主持人在講話內容」）
2. Long-gap subsume（再濾掉「會被 silence-trim 處理」）
3. Cluster detection（再濾掉「連發是自然」）
4. Boundary too tight（最後標記「剪起來會壞」但保留供 review）

每個 filter 把決策寫進事件的 `filter_decision` 欄位（保留可追溯性），同時更新 top-level `stats.after_filters` 計數。

---

## 7. Integration with `run_fine_analysis.js`

### Loading
```javascript
const nsvPath = path.join(baseDir, '2_analysis/non_speech_vocals.json');
const nsv = fs.existsSync(nsvPath) 
  ? JSON.parse(fs.readFileSync(nsvPath, 'utf8'))
  : { events: [], degraded: true };
```

### Edit emission
對每個 nsv 事件，emit 一筆 fine edit：
```javascript
{
  type: 'non_speech_vocal',
  subtype: ev.type,                        // 'throat_clear' / 'nose_clear'
  start: ev.refined_start ?? ev.start,
  end: ev.refined_end ?? ev.end,
  reason: `non_speech_vocal:${ev.type}`,
  description: ev.description,
  confidence: ev.confidence,
  enabled: false,                          // 預設不啟用（needs review）
  needsReview: true,
  source: 'gemini',
  nsv_id: ev.id,                           // 追溯用
  filter_decision: ev.filter_decision,     // 'ok' / 'boundary_too_tight' / ...
}
```

### Cross-reference with Whisper filler words（解使用者「Whisper 把清喉嚨寫成嗯」場景）
找出與 nsv 事件位置重疊的 Whisper words，若 word 是 filler (嗯/啊/呃/對)：
- **既有 filler 規則「預設保留單字 filler」被覆寫**
- 該 filler word 直接升級為 `enabled: true`、reason 寫 `filler_overridden_by_nsv:throat_clear`
- 在 review UI 顯示「Gemini 偵測到該位置有清喉嚨，已自動勾選刪除」

具體實作：
```javascript
function crossRefFillers(nsvEvents, words) {
  const FILLERS = new Set(['嗯','啊','呃','對','哎','欸','哦','噢']);
  for (const ev of nsvEvents) {
    const overlapping = words.filter(w => 
      !w.isGap && w.start < ev.end && w.end > ev.start
      && FILLERS.has(w.text.trim())
    );
    for (const w of overlapping) {
      w._nsvOverride = { type: ev.type, confidence: ev.confidence };
    }
  }
}
```

### Boundary refinement (Python side, in detect script)
Gemini timestamp ±0.5s 不精準。對每個事件用 librosa：
```python
def refine_event_boundary(audio, sr, gem_start, gem_end, padding=0.3):
    """Snap event boundary to local RMS envelope minima."""
    s = max(0, gem_start - padding)
    e = min(len(audio)/sr, gem_end + padding)
    region = audio[int(s*sr):int(e*sr)]
    
    # RMS envelope @ 10ms hop
    hop = int(sr * 0.01)
    rms = librosa.feature.rms(y=region, frame_length=int(sr*0.03), hop_length=hop)[0]
    rms_db = 20 * np.log10(rms + 1e-9)
    
    # Find peak inside [gem_start, gem_end] region
    peak_idx = np.argmax(rms_db[int((gem_start-s)/0.01):int((gem_end-s)/0.01)]) \
               + int((gem_start-s)/0.01)
    
    # Walk left from peak until RMS drops 12dB below peak → refined_start
    # Walk right similarly → refined_end
    threshold = rms_db[peak_idx] - 12
    left = peak_idx
    while left > 0 and rms_db[left] > threshold:
        left -= 1
    right = peak_idx
    while right < len(rms_db) - 1 and rms_db[right] > threshold:
        right += 1
    
    return s + left * 0.01, s + right * 0.01
```
保留 0.03s safety margin（onset leak 既有經驗值，見 `cut/SKILL.md` line 1762）。

---

## 8. Review UI Changes (`generate_review_enhanced.js`)

### New category section
在 review HTML 既有的「精修建議」區段之下，新增「**非語音聲響**」區塊：

```
非語音聲響 (Non-Speech Vocal)        [展開/收合]
  ┌─────────────────────────────────────────────────────────┐
  │ ☐ 26.45-26.85  清喉嚨  conf 0.95                        │
  │    句首濕潤的清喉嚨聲                                    │
  │    [▶ 試聽]  [✅ 確認刪除]  [❌ 保留]                   │
  ├─────────────────────────────────────────────────────────┤
  │ ☐ 78.40-78.70  清鼻子  conf 0.85                        │
  │    句首前的鼻音                                          │
  │    [▶ 試聽]  [✅ 確認刪除]  [❌ 保留]                   │
  └─────────────────────────────────────────────────────────┘
```

每個事件顯示：
- 時間範圍（refined boundary）
- 類型中文化（throat_clear → 清喉嚨、nose_clear → 清鼻子）
- 信心度
- Gemini 中文描述
- 試聽按鈕（同既有 silence/filler review 體驗）
- 確認/保留按鈕

### Feedback writeback
按 ✅ 或 ❌ 時，事件記錄寫入：
```
{base_dir}/3_output/user_corrections.json （既有檔案）
  + 新增 segment：
    {
      "type": "non_speech_vocal_review",
      "nsv_id": "nsv-0",
      "user_decision": "confirm" | "reject",
      "timestamp": "2026-05-18T11:00:00Z"
    }
```

匯出（Stage 4 結束）時，同時複製到：
```
cut/user-prefs/{userId}/non_speech_vocal_feedback.jsonl  （追加模式）
  每筆一行 JSON，包含 user_decision、type、confidence、duration、zone、description
```

→ 累積 100+ 筆後可在未來訓練 user-specific classifier。

### Special case: Whisper filler word 被 nsv 覆寫
既有的「filler word」review 區塊新增一個小標籤：
```
☑ 145.20  嗯  → ⚡ Gemini 偵測到此處實為清喉嚨，已勾選刪除
```
使用者可以仍取消勾選。

---

## 9. User Preferences Schema

新增 section to `cut/user-prefs/{userId}/preferences.yaml`：
```yaml
non_speech_vocal:
  enabled: true                # 主開關
  detect_types:                # 啟用的偵測類別
    - throat_clear
    - nose_clear
    # click_smack, sharp_breath 預設關閉（spike 證實低精度）
  confidence_threshold: 0.4    # 低於此值不顯示
  auto_delete_threshold: 0.99  # 高於此值才 auto-enable（預設等於不 auto）
  default_action: review       # review | delete | ignore
```

`user_manager.js prefs <userId> set non_speech_vocal.enabled false` 等 CLI 操作沿用既有 user manager 介面。

### Feedback aggregation
新增 `cut/scripts/analyze_nsv_feedback.js`（類似既有 `analyze_feedback.js`）：
```bash
node cut/scripts/analyze_nsv_feedback.js <userId>
```
- 統計 confirm / reject 比例
- 按類型、信心度、時長、zone 切片
- 當累積 ≥ 100 筆時提示「可考慮訓練 user-specific filter」

→ 此 CLI 是後續 v2 自動分類器的入口，**本 spec 不含分類器訓練本身**（另文）。

---

## 10. Stage Documentation Updates

### `cut/SKILL.md`
Stage 2 描述更新：
> 2. Stage 2 — Transcribe → 句子切分 → AI rough cut → AI fine cut → **non-speech vocal detection (Gemini)**

新增 Stage 2.5 章節說明本 spec 的內容、JSON shape、必要 env var。

### `cut/editing-rules/11-non-speech-vocal.md`（新檔）
與既有 rule docs 一致的 frontmatter，描述：
- 偵測對象（throat_clear / nose_clear）
- 排除項目（click_smack / sharp_breath / 主持人模仿）
- Filter 邏輯
- 與既有 filler 規則的交叉
- User preference 控制點

### `CLAUDE.md`
- Commands 區段：加入 `python3 cut/scripts/detect_non_speech_vocals_gemini.py <BASE_DIR>`
- Optional env var 區段：`GEMINI_API_KEY` 從 optional 升級為 **non-speech vocal detection 必要**
- Critical gotchas 加一條：「Gemini 偵測失敗不阻斷 pipeline——`degraded: true` 表示部分 chunk 失敗，事件清單仍可用」

---

## 11. Cost & Operations

### API cost
- Gemini-2.5-flash audio input: **$0.075 / 1M tokens**
- 1 hour episode ≈ 144 chunks × 30s × 32 tokens/s ≈ 138k tokens ≈ **$0.010 / 集**
- 連 retry 預估 **$0.015 / 集**
- 一年 50 集 ≈ **$0.75**

### Rate limits
Gemini 2.5 Flash free tier：15 RPM / 1500 RPD（截至 2026-05）
- 5 min episode: 12 chunks → 不會撞 RPM
- 1 hour episode: 144 chunks → 順序執行 ≈ 10 分鐘，不會撞 RPM
- 連續處理 10 集會撞 RPD → 需要升級 tier 或延後執行

### Failure modes & mitigation
| 失敗類型 | 偵測 | 處理 |
|---|---|---|
| 503 service unavailable | response error 503 | exp backoff retry × 3 |
| 429 rate limit | response error 429 | exp backoff retry × 3 |
| JSON parse 失敗 | parse_json_response 三層後仍 None | 該 chunk 記空、繼續下一個 |
| 連續 ≥2 chunk 失敗 | 連續計數 | 整個 stage `degraded: true`、發出 warning，pipeline 繼續 |
| GEMINI_API_KEY 缺 | env load 失敗 | 該 stage 跳過、發出 warning「啟用 NSV 偵測需設置 GEMINI_API_KEY」、pipeline 繼續 |

→ **NSV 偵測永遠是 graceful 補強，不該擋住既有 pipeline**。

---

## 12. Validation Plan

### Acceptance criteria
1. **End-to-end**: 對 `source/host-ted-5min.wav` 跑完整 pipeline (transcribe → NSV detect → fine analysis → review HTML)，無 error，review_enhanced.html 出現「非語音聲響」區塊且至少含 10 個事件
2. **Precision improvement over spike**: 用戶第三輪 review，throat+nose 整體 precision **明顯高於 spike 的 58%**（目標 ≥ 70%，套上 4 個 filter 後預期 75-85%）
3. **Recall preservation**: 4 個 filter 處理過後，spike 已標 OK 的 11 個 Gemini 事件中應有 ≥ 9 個 (82%) 仍在最終輸出（filter 不該大量誤殺真實事件）
4. **Failure resilience**: 模擬 GEMINI_API_KEY 缺失、Gemini 全部 503 兩種情境，pipeline 仍能跑完並輸出有效 review HTML（只是 NSV 區塊為空）
5. **Feedback path**: 在 review HTML 按 ✅/❌ 後，匯出檢查 `non_speech_vocal_feedback.jsonl` 已追加對應 entry

> 註：絕對 recall (vs 「所有真實事件」)無法在 5-min spike sample 上嚴格度量，因為「真實事件總數」要靠人工窮舉整段音檔才能定。本 spec 用「filter 不該誤殺 spike 確認的 OK 事件」作為替代指標；絕對 recall 待 v2 / 真實長集驗證再評估。

### Validation script
`cut/scripts/validate_nsv_pipeline.sh`（新檔）：
1. 跑完整 pipeline 對 5-min sample
2. 比對輸出 JSON 結構符合 schema（用 jq 檢查）
3. 印出精度數字
4. 模擬 API key 缺失情境

---

## 13. Out of Scope (Not in this spec)

明確排除，避免 scope creep：

| 項目 | 為何不在此 spec | 何時處理 |
|---|---|---|
| `click_smack` / `sharp_breath` 偵測 | Spike 證實精度 8-29%、不足以加入 | 未來累積標註後再評估 |
| User-specific SED reranker 訓練（用 AST / CLAP / HeAR embedding + 短窗 + 累積 feedback labels） | 需 ≥100 筆 review feedback；技術路徑見 §3 | 累積後另文 spec |
| Multi-track 支援 | 既有 multi-track pipeline 另路 | 與 multi-track 整合另文 |
| `gpt-audio` / `gpt-realtime` ensemble | Gemini 為主、可考慮備援但本期不做 | Phase 3+ |
| 即時 / streaming 偵測 | Podcast 後製場景不需要 | 永不（除非用例變化） |
| 雲端 batch API | 容量沒到、不需要 | 用戶量擴大後再評估 |

---

## 14. Open Questions

實作前需要使用者確認：

1. **GEMINI_API_KEY 設定方式**：spec 預設使用 `.env`（同 `ai_listen.py`）。是否需要支援 `~/.config/...` 全域設定？
   - **建議**：先用 `.env` 即可，跟既有 `ai_listen.py` 一致
2. **偵測在 pipeline 是預設開啟還是 opt-in？**
   - **建議**：若有 `GEMINI_API_KEY` 自動啟用，否則跳過 + warning
3. **多語混講場景**：若主持人講「I'm going to clear my throat」**英文**，prompt 也要排除嗎？
   - **建議**：先不擴充，spike audio 是純中文。未來若有混語 podcast 再加
4. **review HTML 預設展開或收合？**
   - **建議**：預設**展開**，因為事件少（< 50 個/集），預期使用者會逐筆審

---

## 15. Risk Register

| 風險 | 機率 | 影響 | 緩解 |
|---|---|---|---|
| Gemini API 改版破壞 | 中 | 高 | 鎖版 SDK，失敗時 graceful skip |
| Gemini 配額耗盡 | 中 | 中 | retry + 提前估算用量、user-pref 開關 |
| Prompt drift（Gemini 模型升級後行為改變） | 高 | 中 | 把 prompt 寫進版本化檔案 (`cut/editing-rules/11-non-speech-vocal.md`)，未來重跑 spike 時對比 |
| Filter 過度激進殺到真實事件 | 中 | 中 | 所有 filter 把決策寫進 `filter_decision`，可逐筆 audit；events 沒移除 `boundary_too_tight` 仍會顯示在 UI |
| 使用者習慣自然形成的「拒絕率」過高 | 低 | 低 | review UI 設計允許「全部拒絕」一鍵，且 feedback 累積觸發 v2 classifier |
| 5-min spike 結果不能 generalise 到 60-min 真實集 | 中 | 中 | acceptance criteria 包含跑完整集驗證 |

---

## 16. Implementation Phases

實作建議切分為兩個 implementation plan：

### Phase 1（本 spec MVP）
- [ ] `detect_non_speech_vocals_gemini.py`（含 prompt、chunking、dedup、boundary refinement、confidence normalization、failure handling）
- [ ] `non_speech_vocal_filters.py`（4 個 filter）
- [ ] `run_fine_analysis.js` 整合（讀 JSON、emit edit、cross-ref filler）
- [ ] `generate_review_enhanced.js` UI 區塊
- [ ] user-prefs schema + `user_manager.js` 支援
- [ ] `validate_nsv_pipeline.sh`
- [ ] 文檔更新（SKILL.md、CLAUDE.md、editing-rules/11、README.md）
- [ ] 對 host-ted-5min.wav 跑完整 pipeline + 使用者第三輪審聽
- [ ] 完整集（≥30 min）的真實樣本驗證

### Phase 2（另文 spec）
- Feedback aggregation tooling（從 `non_speech_vocal_feedback.jsonl` 抽 features）
- 用 **AST / CLAP / HeAR / YAMNet** 之一當 frozen embedding backbone，對候選事件取 100-400ms 短窗（或多尺度窗）embedding
- 用累積的 confirm / reject feedback 訓練 binary reranker（gradient boosting on embeddings + duration + zone + 句首/句尾位置等 feature）
- 部署為 Gemini 輸出後的 precision filter
- `gpt-audio` / `gpt-realtime` ensemble 評估（與 Gemini 比對交集當高信心）
- Multi-track 支援

---

## 17. Spike Artifacts (for future reference)

實作時可以對照的 spike 產物（放在 `output/test_host-ted-5min/cut/`）：
- `2_analysis/spike_yamnet_events.json` — YAMNet @ 0.15 (1 event)
- `2_analysis/spike_yamnet_events_raw.json` — YAMNet @ 0.05 (6 events)
- `2_analysis/spike_gemini_events.json` — Gemini 50 events with raw responses
- `1_transcript/whisper_no_vad.json` — Whisper with `vad_filter=False`
- `spike_audit.rendered.html` — 第一輪審聽結果（heuristic 30 events）
- `spike_audit_v2.html` — 第二輪審聽結果（Gemini 50 events）
- `nonverbal_events.json` — 前人遺留的頻譜啟發式輸出（166 events）

實作完成後這些 artifacts 可清掉（在 `cut/scripts/cleanup_spike_artifacts.sh` 中列清單），但 spec 完成前先保留。

---

## End of Spec

審完此 spec 後請給予 feedback。確認無誤後將進入 implementation plan（用 writing-plans skill）。
