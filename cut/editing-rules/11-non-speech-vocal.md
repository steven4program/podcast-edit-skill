<!--
input: 1_transcript/audio.mp3 + 1_transcript/subtitles_words.json
output: 2_analysis/non_speech_vocals.json → fine_analysis edits with type=non_speech_vocal
pos: rule, human-confirmation priority (always needs review unless user prefs override)

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
2. cut/SKILL.md Stage 2.5 section
3. CLAUDE.md commands + env var section
-->

# Non-Speech Vocal Detection (清喉嚨 / 清鼻子)

## Detection target

主持人習慣性在句前或句尾發出短暫的非語音聲響：
- **清喉嚨** (throat clearing) — 短暫沙啞、濕潤的摩擦聲
- **清鼻子** (nose clearing / sniff / snort) — 短暫鼻音

非偵測對象（依 spike 結果排除）：
- 自然呼吸、句前準備吸氣
- Filler "嗯/啊/呃/對"（既有 filler 規則處理）
- 講話過程中的脣齒摩擦
- 笑聲、嘆息（情緒性，刻意保留）
- 主持人**模仿**或**描述**清喉嚨的聲音當作對話內容

## Detection mechanism

外部呼叫 `cut/scripts/detect_non_speech_vocals_gemini.py`，使用 Gemini-2.5-flash 多模態 LLM 全程掃描音檔 30s chunks。為什麼選 Gemini，見：
`docs/superpowers/specs/2026-05-18-non-speech-vocal-detection-design.md` §2 Spike Findings.

決定不採用的方案：YAMNet (Recall 0%)、Whisper non-speech tokens (中文無此標註)、SenseVoice / FunASR (segment-level dominant-class bias)、Yating ASR-only。

## Post-detection filters

四個工程性 filter（`non_speech_vocal_filters.py`），按序執行：

1. **Host describing sound**：若事件 ±2s Whisper 文字內出現「口口口」、「聽起來像」、「清喉嚨」、「咳嗽」等模仿/描述語，移除事件
2. **Long-gap subsume**：若事件位於 > 3s 的 word gap 中，移除（會由既有 silence trim 處理）
3. **Speech artifact cluster**：同類型事件在 2s 內連發 ≥ 3 個視為自然口腔摩擦聲，整組移除
4. **Boundary too tight**：事件長度 < 0.15s 或與下個字距離 < 0.1s 時，標記但保留（soft-keep）—— UI 顯示警告

## Output format

每個事件最終以 fine edit 形式進入 `fine_analysis_rules.json`：

```json
{
  "idx": 12,
  "type": "non_speech_vocal",
  "subtype": "throat_clear",
  "rule": "11-non-speech-vocal",
  "deleteStart": 26.45,
  "deleteEnd": 26.85,
  "reason": "清喉嚨（Gemini 偵測）：句首濕潤的清喉嚨聲",
  "needsReview": true,
  "enabled": false,
  "confidence": 0.95,
  "source": "gemini",
  "nsv_id": "nsv-0",
  "filter_decision": "ok"
}
```

## Cross-reference with filler rules

若 NSV 事件位置剛好有 Whisper 標的「嗯/啊/呃/對」filler word：
- 既有 filler 規則「預設保留單字 filler」被覆寫
- Filler edit 升級為 `enabled: true`、`needsReview: false`
- `reason` 後綴加上「Gemini 偵測到此處實為 X，自動勾選刪除」

## User preference

`cut/user-prefs/<userId>/preferences.yaml`:

```yaml
non_speech_vocal:
  enabled: true                # 主開關（false → 完全跳過偵測）
  detect_types:                # 啟用類別
    - throat_clear
    - nose_clear
  confidence_threshold: 0.4    # 低於此值不顯示
  auto_delete_threshold: 0.99  # ≥ 此值才 auto-enable（預設等同不 auto）
  default_action: review       # review | delete | ignore
```

## Feedback loop

每次 review 後使用者按 ✅/❌ 的決策寫入 `cut/user-prefs/<userId>/non_speech_vocal_feedback.jsonl`。累積 ≥ 100 筆後可考慮訓練 user-specific classifier 作為後置 filter（v2，另文 spec）。
