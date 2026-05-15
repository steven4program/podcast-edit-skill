# Podcast Edit Skill — 使用方式

> 從原始錄音到可發布成品的 AI 剪輯流程。四個 skill、八個階段、雙層學習。

---

## 0. 開始前確認

```bash
# 在 Claude Code 裡輸入 / 應該看得到這四個指令
/podcast-edit-install
/podcast-edit-cut
/podcast-edit-qa
/podcast-edit-polish
```

看不到 → 先跑 `/podcast-edit-install` 或檢查 `~/.claude/skills/` 的 symlink。

依賴:Node.js、FFmpeg、Python 3、`faster-whisper`(本地轉錄)、`librosa` + `soundfile`(QA)。

---

## 1. 把音檔放進來

把要剪的音檔放到 `source/`(或任何路徑都可以,絕對/相對都行)。

```
source/
├── hogan-5m.mp3
└── ted35-01-5m.mp3
```

支援格式:`*.mp3` / `*.wav` / `*.m4a`。

---

## 2. 三種典型呼叫

### A. 單檔、單講者(MVP 最快路徑)

```
/podcast-edit-cut 剪 source/hogan-5m.mp3,講者叫 Hogan
```

走本地 Whisper,所有字會掛在同一個 speaker label 上。最省事,適合獨白型 podcast。

### B. 同一集、多軌錄音(每位講者一條音軌)

```
/podcast-edit-cut 雙軌:source/hogan-5m.mp3=Hogan, source/ted35-01-5m.mp3=Ted
```

或更口語的等效寫法:

```
/podcast-edit-cut 這集多軌錄音:
  source/hogan-5m.mp3 = Hogan
  source/ted35-01-5m.mp3 = Ted
```

只要符合「**2 個以上音檔 + 明確的 `檔案=講者` 對應**」、或出現「雙軌 / 多軌 / multitrack / one track per speaker」這類字眼,我就會自動走 multitrack 流程:

- Stage 2.1 用 `prep_review_audio_multitrack.py` 同時產**兩份**審稿用 mp3:
  - `audio_seekable.mp3` — 軌間平衡後的混音(用你選的 `--balance` 模式),給審稿頁的 **Final-cut(live)** 播放器
  - `audio_seekable_raw.mp3` — 純 amix、**沒做**任何平衡的原始混音,給審稿頁的 **Source** 播放器
  - 切換 Source ↔ Final-cut 時 `currentTime` 會保留 → 同個位置直接 A/B 對比「處理前 vs 處理後」
- Stage 2 用 `transcribe_whisper_multitrack.py`(真正分得出講者)
- Stage 5 用 `cut_audio_multitrack.py`(`--balance` 必須跟 prep 時一致),**同時產出**:
  - `<Speaker>_solo.mp3`(每位講者一個,各自 loudnorm 到 -16 LUFS)
  - `episode_merged.mp3`(依 `--balance` 模式做軌間平衡 → amix → 最終 loudnorm 到 -16 LUFS)

Riverside / Zencastr / Squadcast 匯出的分軌就走這條。如果某軌晚開錄,加 `offset` 參數,例如 `Ted=0.25` 代表 Ted 那軌晚 0.25 秒進場。

### C. 多集獨立節目(各自剪)

```
/podcast-edit-cut 剪 source/hogan-5m.mp3,講者 Hogan
# 跑完一輪後
/podcast-edit-cut 接著剪 source/ted35-01-5m.mp3,講者 Ted
```

---

## 3. 第一次跑會被問什麼

Stage 1 一定先問:

1. **新用戶 / 舊用戶?**
2. **username**(英文或拼音,例如 `samuel`)
3. 新用戶會再問:
   - 有沒有「原始 + 已剪好」的配對檔可以學你的口味?(有 → 走 Path A 樣本學習,最準)
   - 沒有 → 一次問完三題:
     - podcast 類型(對象、目的)
     - 目標長度 + 激進度(保守 10–20% / 中等 20–35% / 激進 35–50%)
     - 特殊偏好(例如「保留所有語助詞」「結巴一律砍」)

舊用戶只會被一句確認:「載入完偏好,今天剪什麼?幾位講者?」

---

## 4. 自動跑完的階段(你只要等)

```
Stage 2  轉錄 → 切句 → 段落級粗剪 → 字級精剪
Stage 3  AI 自審補刀
```

中間會在 `output/YYYY-MM-DD_<audio>/cut/` 產出:
- `1_transcript/subtitles_words.json` — 字級時間戳(後續真理之源)
- `2_analysis/semantic_deep_analysis.json` — 粗剪建議
- `2_analysis/fine_analysis.json` — 精剪建議
- `2_analysis/delete_segments.json` — 合併後的刪除區間

---

## 5. Stage 4:你要親自審稿(最關鍵)

跑完 Stage 3 會自動在瀏覽器開:

```
output/YYYY-MM-DD_<audio>/cut/review_enhanced.html
```

頁面功能:
- ▶️ 即時播放(每個刪除馬上在播放器上生效,聽起來就是成品的感覺)
- 句子層級「刪除 / 復原」
- 精剪 toggle(看要不要連字級瑕疵都砍)
- 手動框選任意範圍刪除
- 寫 AI 回饋(「這段不該刪因為 XXX」)→ 影響下次

確認後按頁面上的匯出,會產:
- `delete_segments_edited.json`
- `ai_feedback.json`

兩個檔都會回寫到 `cut/user-prefs/<你>/`,**下次更懂你**。

> ⚠️ **絕對警告**:一旦你審過,我**不會**主動重新產 `review_enhanced.html`,因為會清掉你的所有手動編輯。如果真的非重產不可,我會先備份並等你明確同意。

---

## 6. Stage 5:執行剪輯(自動)

依輸入形式自動分路:

### 6.1 單軌(情境 A、C)

```
cut_audio.py     sample-accurate WAV → dynaudnorm → loudnorm -16 LUFS → MP3
trim_silences.py 過長靜音收尾
```

產出:
```
output/YYYY-MM-DD_<audio>/cut/3_output/<podcast>_final_v1.mp3
```

### 6.2 多軌(情境 B)

```
cut_audio_multitrack.py  同一份 delete_segments 套到每軌
                         → 每軌 dynaudnorm + loudnorm -16 LUFS → <speaker>_solo.mp3
                         → 合併版依 --balance 模式混音 → 最終 loudnorm -16 LUFS → episode_merged.mp3
```

**`--balance` 三種模式**:

| 模式 | 行為 | 適合什麼情境 |
|---|---|---|
| `equalize`(**預設**) | 每軌**各自**先 loudnorm 到 -16 LUFS 再混音 | 兩位講者響度要**真的一致**。代價:大聲那位的動態被壓一點 |
| `lift` | 量測各軌 LUFS,小聲軌增益到最大那軌(+12 dB 上限)再混 | 想保留**大聲那位**的自然動態,只把小聲那位拉上來 |
| `none` | 直接混,只在最後做整體 loudnorm | 兩軌錄音水位本來就接近,不想再動 |

不指定的話用 `equalize`。想換成 `lift`,告訴我「這次用 lift 模式」就行。

產出(以 source 裡兩個檔為例):
```
output/YYYY-MM-DD_<episode>/cut/3_output/
├── Hogan_solo.mp3       # Hogan 自己的軌,剪好、響度規範化
├── Ted_solo.mp3         # Ted 自己的軌,剪好、響度規範化
└── episode_merged.mp3   # 兩軌音量對齊後混音的成品
```

> ⚠️ **軌間平衡 vs 軌內平衡**:`dynaudnorm` 處理同一講者的忽大忽小(麥距、語氣);軌間 LUFS 對齊處理「主持人比來賓大聲」這種情境。兩者都做了。

### 6.3 共通的後處理

不論 6.1 還是 6.2,最後都會跑:
```
trim_silences.py   掃描成品,把剪輯後合併出的過長靜音收掉
```

到這裡 `/podcast-edit-cut` 結束。下面是選用。

---

## 7. `/podcast-edit-qa` — 品保(建議跑)

```
/podcast-edit-qa output/2026-05-13_hogan-5m
```

三階段檢查:
- **Phase A 資料層** — `delete_segments` 範圍是否合理
- **Phase B 訊號層** — 能量/頻譜/靜音 + 可選 Gemini AI 聽
- **Phase C 語義層** — 把成品再轉錄一次,LCS 對齊原稿,抓殘留沒砍乾淨的

QA 發現的算法盲點會回寫 `cut/editing-rules/`(**全使用者共用變聰明**)。

---

## 8. `/podcast-edit-polish` — 後製(可選)

```
/podcast-edit-polish output/2026-05-13_hogan-5m
```

做:
- 抽精華做片頭 teaser(`mix_highlights_with_music.py`)
- 進/出場音樂混音
- 章節時間戳 + 標題 + show notes

---

## 9. Stage 8:最終審稿

`/podcast-edit-cut` 會收尾產:

```
output/.../review_final.html
```

把 QA 標記的點列成清單,每個都可點時間戳跳轉。你聽過後標 "all good" / "needs re-cut",再回寫一次偏好學習。

---

## 10. 常用一句話指令對照表

| 你說 | 我做 |
|---|---|
| 「剪 source/xxx.mp3」 | 跑完整 8 階段(單軌) |
| 「雙軌:a.mp3=Alice, b.mp3=Bob」 | 走 multitrack,產 solo + merged |
| 「multitrack: a.mp3=Alice, b.mp3=Bob,offset Bob=0.25」 | 多軌且 Bob 晚 0.25 秒進場 |
| 「只轉錄就好」 | 停在 Stage 2.1 給逐字稿 |
| 「先別審稿,直接出片」 | 用 AI 預設刪除清單直接到 Stage 5(首次不建議) |
| 「用我之前剪好的 xxx 當樣本學風格」 | 走 Path A 樣本學習 |
| 「只跑 QA」 | `/podcast-edit-qa <output 目錄>` |
| 「幫這集做片頭精華 + show notes」 | `/podcast-edit-polish <output 目錄>` |
| 「把目標長度改成 30 分鐘」 | 改 `cut/user-prefs/<你>/preferences.yaml` |
| 「列出所有使用者」 | `node cut/scripts/user_manager.js list` |
| 「給我看我的偏好」 | `node cut/scripts/user_manager.js prefs <你>` |

---

## 11. 雙層學習在哪裡

| 層 | 路徑 | 何時更新 |
|---|---|---|
| 共用編輯規則 | `cut/editing-rules/` | Stage 6 QA 發現算法盲點 |
| 個人偏好 | `cut/user-prefs/<userId>/` | Stage 4 審稿 + Stage 8 最終審稿 |

跨機器跨帳號可攜(因為都是檔案,跟著 repo 走)。

---

## 12. 產物結構(每次跑都會產一份)

### 單軌情境
```
output/YYYY-MM-DD_<audio>/cut/
├── 1_transcript/                  # 轉錄資料
│   ├── audio.mp3
│   ├── audio_seekable.mp3         # 審稿頁專用 CBR 重編碼
│   ├── whisper_transcription.json
│   └── subtitles_words.json       # 字級時間戳(真理之源)
├── 2_analysis/                    # AI 分析結果
│   ├── sentences.txt
│   ├── semantic_deep_analysis.json
│   ├── fine_analysis.json
│   └── delete_segments.json
├── 3_output/
│   └── <podcast>_final_v1.mp3     # ← 唯一成品
├── review_enhanced.html
└── review_final.html
```

### 多軌情境(差別在 1_transcript 與 3_output)
```
output/YYYY-MM-DD_<episode>/cut/
├── 1_transcript/
│   ├── whisper_Hogan.json              # 每位講者一份
│   ├── whisper_Ted.json
│   ├── whisper_multitrack_manifest.json
│   └── subtitles_words.json            # 合併時間軸,真理之源
├── 2_analysis/                         # 跟單軌一樣
│   └── ...
├── 3_output/
│   ├── Hogan_solo.mp3                  # ← 講者 1 單獨成品
│   ├── Ted_solo.mp3                    # ← 講者 2 單獨成品
│   └── episode_merged.mp3              # ← 軌間音量已對齊的混音成品
├── review_enhanced.html
└── review_final.html
```

---

## 13. 第一次最推薦的流程

### 單軌
1. 把音檔放進 `source/`
2. 對 Claude 說:「剪 source/hogan-5m.mp3,新用戶,我叫 samuel」
3. 回答 Stage 1 的三個偏好問題(用預設也行)
4. 等到瀏覽器開出 `review_enhanced.html` → 認真審一次(這是 AI 學你口味的關鍵時刻)
5. 匯出
6. 對 Claude 說:「跑 QA」
7. 對 Claude 說:「做 polish」
8. 完成

### 多軌(每人一條音軌)
1. 把每位講者的音軌放進 `source/`(檔名最好能對應講者,例如 `hogan-ep01.mp3`)
2. 對 Claude 說:
   ```
   /podcast-edit-cut 雙軌:source/hogan-ep01.mp3=Hogan, source/ted-ep01.mp3=Ted
   新用戶,我叫 samuel
   ```
3. 回答 Stage 1 偏好
4. 等審稿頁開出 → 審稿 → 匯出
5. Stage 5 自動產 `Hogan_solo.mp3` + `Ted_solo.mp3` + `episode_merged.mp3`
6. 跑 QA、跑 polish(polish 預設用 `episode_merged.mp3`,要剪精華也是從合併版抽)
7. 完成

之後第二次起,Stage 1 只會一句確認,AI 越用越懂你的剪輯品味。

---

## 14. 聲音處理在哪幾步發生(常被問)

| 處理 | 在哪 | 做什麼 |
|---|---|---|
| 軌內音量平衡 | Stage 5 ffmpeg `dynaudnorm` | 同講者的麥距/語氣大小聲拉平 |
| 響度規範化 | Stage 5 ffmpeg `loudnorm` | 整體推到 -16 LUFS,Apple/Spotify podcast 標準 |
| 防爆音 | Stage 5 ffmpeg `loudnorm TP=-1.5` | True Peak ≤ -1.5 dBFS 安全網 |
| 切點淡入淡出 | Stage 5 `cut_audio.py` 自適應 fade | 預設 3 ms 微淡(`--no-fade`),避免切點 click 又不吃字 |
| 軌間音量對齊 | **只在多軌** `episode_merged.mp3` 階段 | 預設 `equalize`:每軌各自 loudnorm 到 -16 LUFS 再混(響度真的一致);可切 `lift`(保留大聲方動態)或 `none`(不平衡) |
| 過長靜音收尾 | Stage 5 `trim_silences.py` | 剪輯後合併出的過長停頓掃掉 |

> 想跳過任何一項?直接告訴我「這次別做 dynaudnorm」之類的,我可以針對性關掉。
