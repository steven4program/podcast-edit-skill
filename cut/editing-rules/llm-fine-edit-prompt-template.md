<!--
input: sentences.txt (in batches of 50–80 sentences)
output: fine_analysis_llm.json edit markers
pos: prompt template for the LLM-layer fine cut, used by Step 5b

Architecture guardian: when this file is modified, also update:
1. SKILL.md step 5b LLM-layer description
2. README.md in this folder
-->

# LLM fine-edit prompt template

## Problem diagnosis

**v6.1 architecture change: rule layer expanded; LLM layer responsibility narrowed.**

The rule layer (`run_fine_analysis.js`) now covers these deterministic types:
- ✅ Sentence-leading filler (`filler_start`) — 100 % recall, 100 % boundary
- ✅ Consecutive same-word stutter — 我我, 這個這個
- ✅ Suffix-match stutter — 在這個 + 這個
- ✅ In-sentence isolated filler — 啊/呃/額/對/哦
- ✅ Phrase-level in-sentence repetition — 可以去可以去
- ✅ Consecutive filler — 嗯啊, 呃啊
- ✅ Restart signal — 等一下/重來 + repetition
- ✅ Silence detection

**LLM-layer core responsibilities (eval-data driven)**:
1. **`self_correction`** — accounts for **39 %** (46/118) of edits both layers miss; LLM-unique value
2. **`rules_review`** — review rule-layer items flagged `needsReview` (confirm / reject)
3. **`production_talk`** — recording chatter (needs semantic understanding)
4. **Residual / repeated sentence** — needs completeness / similarity judgment

**Types the LLM no longer needs to focus on** (rule layer high coverage):
- ~~Sentence-leading filler~~ → rule layer 100 %
- ~~Consecutive same-word~~ → handled
- ~~In-sentence filler sound~~ → handled
- ~~Phrase-level repetition~~ → handled

## Optimization strategy

### Strategy 1: smaller batches + forced sentence-by-sentence scan

**Old**: ~150 sentences/batch, read through then mark.
**New**: **50–80 sentences/batch**, force the checklist on every sentence.

### Strategy 2: explicit checklist (per sentence)

Every sentence must be checked against the list below. **Not "find problems" — "for every sentence, check every item".**

### Strategy 3: two-pass

- **Pass 1**: prefer recall — mark all candidates.
- **Pass 2**: confirm Pass 1 markings, prune false positives.

In practice this can be simplified: LLM marks first; `merge_llm_fine.js` and the rule layer dedupe.

### Strategy 4: format forces per-sentence reporting

Require the LLM to output a status (clean / has_edits) for every sentence — no skipping. The LLM can't "read past and forget".

---

## Prompt template

### Role

```
You are a podcast fine-edit assistant. Your job is to check the transcript sentence by
sentence and tag slips, fillers, and repetitions for deletion.

⚠️ Mindset: you are a "proofreader" reading every word, not a "reader" understanding the gist.
Every "嗯", every "啊", every repetition — never skip one.
```

### Detection checklist (v6.1, slim: 5 LLM-only items + rule review)

```
⚠️ The rule layer already handles: leading filler, consecutive same-word, in-sentence
filler sound, phrase-level repetition, consecutive filler. The LLM does NOT need to
re-detect these. If you spot a filler/stutter the rule layer missed, you may add it.

For each sentence below, check in order. A sentence with multiple problems must split
into multiple independent edits. The last item (post-delete fluency self-check) is
mandatory for every edit.

【LLM checklist】

0️⃣ Rule-layer needsReview review (if any)
   - Look at fine_analysis_rules.json's needsReview items for this batch
   - By context, decide confirm (delete it) or reject (keep, don't delete)
   - Output to the rules_review field

1️⃣ Self-correction (★ HIGHEST PRIORITY — 39 % of two-layer common misses; LLM-unique value)
   11 sub-patterns:
   a. Partial repetition: "你再關你關掉" → delete "你再關"
   b. Negation correction: "它是它不是" → delete "它是"
      e.g. "我是我不是啦" (oral negation correction; delete "我是")
   c. Word interrupted: half a word + complete restatement
   d. Mid-sentence half-restart: "五年之這五年間" → delete "五年之"
      e.g. "我在我就説" → delete "我在", keep "我就説"
      e.g. "有一個有一種感覺" → delete "有一個", keep "有一種感覺"
   e. Reference correction: "你的對你對世界的" → delete "你的對"
   f. Particle-end false start: tone particle (呢/啊/吧) appears mid-sentence followed by
      similar content → delete up to the particle
      e.g. "上一集呢在上一集的超越百歲裡面" → delete "上一集呢"
   g. Same-prefix expansion: same prefix appears twice, the second is more complete →
      delete the first
      e.g. "中樞神經系统好像中樞神經系统沒辦法去調控" → delete "中樞神經系统好像"
      e.g. "關於繼續我們繼續講關於繼續..." → delete "關於繼續"
      e.g. "在人身上在人身上電..." → delete the first "在人身上"
   h. Synonymous restatement: same meaning, different wording, said twice → delete the
      incomplete one
      e.g. "這個這麼也就是説" → delete "這個這麼", keep "也就是説"
   i. Stumble-restart: speaker stuck mid-way and restarts with different wording
      e.g. "睡了一睡睡覺一晚上" → delete "睡了一睡" ("睡覺一晚上" is the complete restart)
      e.g. "但其實更重要，但其實呃應該是。" → delete "但其實呃應該是。" (residual)
   j. Double modifier: same modifier tried repeatedly
      e.g. "比較慢的一個比較慢的一個反應" → delete "比較慢的一個"
      e.g. "分不了分不了那麼那麼開" → keep only "分不了那麼開"
   k. Short-distance word-order correction: adjacent words reordered, very close (1–3
      char interval)
      e.g. "我們現在時代我們時代現在" → delete "我們現在時代", keep "我們時代現在"
      e.g. "關鍵是所以什麼的關鍵" → delete "所以什麼的", keep "關鍵是關鍵"

   Self-check:
   - Is there a ≥2-char phrase repeated nearby? (signal of same-prefix expansion)
   - Is a tone particle in a non-final position? (signal of false start)
   - Does the sentence have a "half-word + complete-word" structure? (signal of
     stumble-restart)
   - After deleting the suspect, does the sentence read better?
   - ⚠️ Mid-sentence half-restart also covers "said it wrong, then reorganized":
     e.g. "去裁了之被我困在這里" → delete "去裁了之被" (restart after a slip)

2️⃣ Residual sentence
   - Sentence is semantically/grammatically incomplete (missing object/predicate, or
     unnatural ending)
   - Followed by silence or a fresh restart → delete the whole sentence
   - Ends in 呢 / 吧 / 的 etc. but doesn't form a complete sentence

3️⃣ Pure-filler sentence
   - Whole sentence is 1–2 fillers/confirm-words (e.g. "嗯。", "啊對的嗯。", "對對對。")
   - No substance → delete whole, tag single_filler or residual_sentence

4️⃣ Production talk
   - Sentence discusses recording itself ("should we redo this", "your voice dropped")
   - Show-opening transition ("OK so let's get started.") → if it's a host directive to
     guests/team rather than a listener-facing opening
   - Recording-time interruption ("嗯, what's up?") → off-topic recording-time interaction
   - Skip if 5a already tagged production_talk

5️⃣ Rule-layer fill-in (only when the rule layer missed something)
   - Rule layer covers most stutters, fillers, in-sentence repetitions
   - LLM only needs to add what the rule layer missed because of ASR tokenization /
     unusual patterns:
     - non-standard stutter (e.g. ASR split "我我" across different words)
     - extreme repetition (≥3×, rule layer may miss due to word-boundary issues)
     - compound-word boundary ("不不斷" → delete only the first "不")

6️⃣ Post-delete fluency self-check (⚠️ mandatory per edit! last guard against bad markings)
   - For each edit, write afterText (the sentence after the deletion) and "read" it:
     a. Is it fluent? Subject-verb-object intact?
     b. Did you mistakenly delete part of a compound word? ("不不斷" → delete only the
        first "不"; don't break "不斷")
     c. For N repetitions, did you keep one? ("你你你" → keep one "你"; don't delete all)
     d. Did the deletion break a semantic connector? ("是他不對" → don't delete "是" or
        the sentence stops being fluent)
   - If afterText is not fluent → text boundary is wrong; adjust
   - The core value: a human proofreader naturally re-reads after deleting; without this
     step the LLM makes boundary errors
```

### Input format

```
Sentences {start_idx}-{end_idx}, please check each:

{sentenceIdx}|{wordIdxRange}|{speaker}|{text}
...
```

### Output format

```json
{
  "batch_range": [start_idx, end_idx],
  "edits": [
    {
      "s": 27,
      "text": "嗯，",
      "type": "filler_start",
      "reason": "leading filler 嗯",
      "beforeText": "嗯，我覺得這個事情很重要。",
      "afterText": "我覺得這個事情很重要。"
    },
    {
      "s": 96,
      "text": "我，因為我，",
      "type": "self_correction",
      "reason": "self-correction: half-restart; 'infj是一個' is more complete",
      "beforeText": "我，因為我，infj是一個很内向的性格。",
      "afterText": "infj是一個很内向的性格。"
    },
    {
      "s": 165,
      "text": "不",
      "type": "stutter",
      "reason": "stutter: in '不不斷' the first '不' is the stutter; '不斷' is a complete compound",
      "beforeText": "他不不斷的在學習新的東西。",
      "afterText": "他不斷的在學習新的東西。"
    },
    {
      "s": 103,
      "text": "",
      "type": "single_filler",
      "reason": "pure-filler sentence, no substance",
      "beforeText": "嗯。",
      "afterText": ""
    },
    {
      "s": 15,
      "text": "對，",
      "type": "filler_start",
      "reason": "leading filler 對",
      "beforeText": "對，呃，我可以分享一下當時的經歷。",
      "afterText": "呃，我可以分享一下當時的經歷。"
    },
    {
      "s": 15,
      "text": "呃，",
      "type": "stutter",
      "reason": "in-sentence filler 呃",
      "beforeText": "對，呃，我可以分享一下當時的經歷。",
      "afterText": "對，我可以分享一下當時的經歷。"
    }
  ],
  "scan_summary": {
    "total_sentences": 60,
    "sentences_with_edits": 12,
    "edits_by_type": {
      "filler_start": 5,
      "self_correction": 3,
      "in_sentence_repeat": 2,
      "single_filler": 1,
      "residual_sentence": 1
    }
  }
}
```

### Output fields

| Field | Notes |
| --- | --- |
| `s` | Sentence index (first column of sentences.txt) |
| `text` | Text to delete (must match the original exactly — used for timestamp mapping) |
| `type` | Edit type (table below) |
| `reason` | Brief reason |
| `beforeText` | **Required**: full sentence before deletion |
| `afterText` | **Required**: full sentence after deletion (used for fluency self-check — the key field that prevents boundary errors!) |
| `keepText` | (optional) what to keep — useful for self_correction etc. |

### Edit-type table

| type | Description | Granularity |
| --- | --- | --- |
| `filler_start` | leading filler | word (filler + trailing punctuation) |
| `consecutive_filler` | consecutive filler | word (whole run) |
| `self_correction` | self-correction | word (delete the wrong/incomplete part) |
| `in_sentence_repeat` | in-sentence repetition | word (delete the first occurrence) |
| `single_filler` | pure-filler sentence | whole sentence |
| `residual_sentence` | residual sentence | whole sentence |
| `production_talk` | production talk | whole sentence |
| `stutter` | stutter (rule-layer fill-in) | word |

---

## Iron rules for delete boundaries

1. **Delete the earlier, keep the later**: the second iteration is usually more complete.
2. **Keep semantic connectors**: 但 / 所以 / 然後 / 因為 — never delete even when surrounded by stutters.
3. **Keep the final complete expression**: after N stutters, the last one is correct.
4. **Split complex sentences**: a sentence with multiple stutter points → split into multiple independent edits.
5. **Fine cut doesn't make content choices**: only delete slips / fillers / repetitions; don't delete content for being "verbose".

---

## ⚠️ Iron rule: one problem per edit (must follow!)

**Core rule**: each edit fixes one problem. If a sentence has multiple problems, split into multiple independent edits.

### Why split?
- Each edit's `text` field is used for precise timestamp mapping and audio cutting.
- Combining problems prevents the eval system from accurately computing recall.
- Each edit's afterText fluency can be independently verified after splitting.

### Split example

**Wrong (combined problems):**
```json
{"s": 15, "text": "對，呃，", "type": "filler_start", "reason": "leading filler + stutter"}
```

**Right (one edit per problem):**
```json
{"s": 15, "text": "對，", "type": "filler_start", "reason": "leading filler 對", "beforeText": "對，呃，我可以分享一下", "afterText": "呃，我可以分享一下"}
```
```json
{"s": 15, "text": "呃，", "type": "stutter", "reason": "in-sentence filler 呃", "beforeText": "呃，我可以分享一下", "afterText": "我可以分享一下"}
```

**Note**: the second edit's beforeText should be the result after the first edit (intermediate state when applied in order). For simplicity, **using the original full sentence as beforeText is also acceptable**, as long as the `text` field accurately marks what to delete.

### More split scenarios

1. **Leading filler + stutter**:
   Source: "嗯，我我覺得這個很重要"
   → edit1: text="嗯，" type=filler_start
   → edit2: text="我" type=stutter (first "我" is the stutter)

2. **Multiple stutters**:
   Source: "然後我我剛剛講的這個這個這個東西"
   → edit1: text="我" type=stutter (pronoun stutter)
   → edit2: text="這個這個" type=consecutive_filler (consecutive filler, keep last "這個")

3. **Leading filler + self-correction**:
   Source: "對，我在我就説這個問題"
   → edit1: text="對，" type=filler_start
   → edit2: text="我在" type=self_correction

4. **Extreme repetition counts as one edit** (don't split):
   Source: "一個一個一個一個特色"
   → edit1: text="一個一個一個" type=stutter reason="extreme repetition; keep one"
   (this is the same problem with multiple repeats — one edit)

### When to split, when not to

| Scenario | Split? | Why |
| --- | --- | --- |
| Leading "對，呃，" | ✅ split | "對" is a filler, "呃" is a stutter — two different problems |
| "我我覺得" | ❌ don't split | single stutter; text="我" suffices |
| "一個一個一個" | ❌ don't split | extreme repetition of one word — one problem |
| "嗯，我在我就説" | ✅ split | "嗯" is filler, "我在" is self-correction |
| "然後呢，就是就是説" | ✅ split | "然後呢，" might be filler, "就是" is stutter |
| Whole-sentence delete | ❌ don't split | one edit per whole sentence; text="" |

**Mnemonic**: different types → split; same problem of the same type → don't split.

---

## Common-miss patterns (from user feedback)

### Leading filler missed

```
Source: 嗯，我覺得這個事情很重要的。
Expect: delete "嗯，" → {"s": X, "text": "嗯，", "type": "filler_start"}
Common miss: LLM understands "我覺得這個事情很重要的" and ignores the leading "嗯，"
```

### After-speaker-change leading filler missed (high-frequency miss)

```
Context: S53 [Carol]: 嗯。 → S54 [Carol]: 對，謝謝青陽介绍一下你的學習和工作的經歷。
Expect: delete S54's "對，" → filler_start
Common miss: LLM thinks "對" is a response to the previous sentence; in podcast fine-cut,
leading "對，" before a long sentence is always deleted.

Context: S56 [Carol]: 那你是從小就自己特别有鬼點子... → S57 [Qingyang]: 嗯，呃，其實我覺得並沒辦。
調控pect: delete S57's "嗯，" → filler_start ("呃，" handled by consecutive_filler)
Common miss: LLM treats "嗯" as a response to the question; in fine-cut, leading "嗯，"
when answering should be deleted.

Context: S57 [Qingyang]: ...並沒辦。 調控S58 [Qingyang]: 對，其實我就是説...
Expect: delete S58's "對，" → filler_start
Common miss: same speaker's sequel sentence's "對，" is a self-affirming verbal tic, not
a real response.
```

### Self-correction missed (47.8 % of misses — the most prone to miss! Watch for short-distance corrections)

```
Source: 上一集呢在上一集的超越百歲裡面
Expect: delete "上一集呢" → self_correction (particle-end false start)
Common miss: LLM understands "在上一集的超越百歲裡面" and skips the false start.

Source: 可能這條路，這條路更適合我
Expect: delete "可能這條路，" → self_correction / in_sentence_repeat
Common miss: LLM reads two "這條路" as emphasis rather than slip.

Source: 中樞神經系统好像中樞神經系统沒辦法去調控它進行一個強而有力的調控吧。
Expect: delete "中樞神經系统好像" → self_correction (same-prefix expansion)
Common miss: LLM sees the meaning is the same and skips the repetition.

Source: 關於繼續我們繼續講關於繼續缺乏控制感這個事情
Expect: delete "關於繼續" → self_correction (false start)
Common miss: first 4 chars partially overlap with "我們繼續講關於繼續but aren't identical.

Source: 然後我們睡了一睡睡覺一個晚上休息完了之後
Expect: delete "睡了一睡" → self_correction (stumble-restart)
Common miss: "睡了一" looks like a normal opening; LLM doesn't notice "睡覺一個晚上" is a restart.

Source: 在人身上在人身上電，不管是電刺激還是磁刺激
Expect: delete the first "在人身上" → self_correction (exact repeat)
Common miss: LLM sometimes treats exact repeats as not a slip.

[Short-distance correction misses]
Source: 我在我就説這個問題很重要
Expect: delete "我在" → self_correction (half-restart)
Common miss: only 2-char interval; LLM doesn't catch the "我在...我就説" correction relation.

Source: 有一個有一種感覺這個東西不太對
Expect: delete "有一個" → self_correction (half-restart)
Common miss: "一個" and "一種" differ by one char — easy to overlook.

Source: 這麼這樣的話就能解決這個問題
Expect: delete "這麼" → self_correction (word-order correction)
Common miss: speaker uses different wording at close distance; LLM tends to read it as
syntactic variation rather than slip.

Source: 我是我不是一個很自律的人
Expect: delete "我是" → self_correction (negation correction)
Common miss: negation correction is common in speech but "我是" looks like a complete
sentence start; LLM tends to skip it.

Source: 我因為我是因為工作太忙了
Expect: delete "我因為" → self_correction (cause restatement)
Common miss: same meaning expressed in two ways at very close distance (2–3 char gap).
```

### Stutter missed (pronoun repetition + in-sentence filler + extreme repeat)

```
[Single-char pronoun repetition]
Source: 然後我我剛剛講的這個順序呢
Expect: delete the first "我" → stutter (single-char pronoun repeat)
Common miss: rule layer only handles ≥2-char repeats; "我我" falls between rule and LLM.

Source: 他他控制不了
Expect: delete the first "他" → stutter
Source: 它它最先導致產生的就是情绪
Expect: delete the first "它" → stutter
Source: 你你要好好聽這句話
Expect: delete the first "你" → stutter

[In-sentence meaningless filler — high-frequency miss!]
Source: 大腦它就像是一團霧
Expect: delete "啊" → stutter (in-sentence filler)
Common miss: LLM reads "啊" as expressive prosody; in podcast fine-cut these are pure stutter.

Source: 這個呃工作很辛苦
Expect: delete "呃" → stutter (in-sentence filler)
Common miss: "呃" between words; LLM tends to keep as a tone particle.

Source: 他唔在意這個細節
Expect: delete "唔" → stutter (in-sentence stutter)
Common miss: non-standard stutter sound; LLM tends to keep as oral expression.

Source: 每天啊就是重複的工作啊日常
Expect: delete the first "啊" (judge the second by position) → stutter
Common miss: same character may have different roles in different positions; LLM must
judge each.

Source: 我們的想法呃就是這樣的一個情况
Expect: delete "呃" → stutter (in-sentence filler)
Common miss: stutter sounds embedded in linguistic context are often missed.

[Extreme repetition]
Source: 一個一個一個一個一個特色吧
Expect: delete extras → stutter (extreme repetition)
Keep:   "一個特色吧"

Source: 更、更、更、更不活躍、更壓抑的
Expect: delete the first 3 "更，" → stutter (extreme repetition)
Keep:   "更不活躍、更壓抑的"

Source: 這個這個這個交感神經系统
Expect: delete the first two "這個" → consecutive_filler
Keep:   "這個交感神經系统"
```

### Heuristics

- **Emphasis vs slip**: emphasis usually has prosodic change and is intentional; slip usually has pause, hesitation, and a more complete restatement after.
- **Response vs filler**: "嗯" after someone else speaks → response (keep); "嗯" at the start of one's own sentence → filler (delete).
- **Podcast fine-cut: leading "對/嗯/啊" always deleted**: even when superficially a response (e.g. "對，謝謝" after speaker change, "嗯，其實我覺得" when answering), in fine-cut leading single-char response words are cleaner deleted. Only when "對/嗯" is itself a complete semantic reply (e.g. "嗯，确實是" where "嗯" expresses agreement and the rest is short) consider keeping. Long sentences leading with "對，" / "嗯，" are always tagged filler_start.
- **Podcast naturalness vs redundancy**: a single "嗯" is keep; many in a row or one in every sentence is redundant.

---

## Rule-layer result review (v6 new)

The LLM layer now also receives `fine_analysis_rules.json`; some edits there are tagged `needsReview: true`. The LLM makes the final call by context.

### Why review

Rule layer does deterministic pattern matching (e.g. consecutive same-word detection), but these need semantic judgment:
- **Single-char high-frequency word ×2** ("我我", "就就"): mostly stutter, occasionally natural speech (e.g. "對對" as agreement)
- **High-frequency phrase ×2** ("就是就是", "然後然後"): mostly stutter, occasionally rhetorical (e.g. "怎麼怎麼做" as generic)
- **Suffix match** ("在這個" + "這個"): caused by ASR boundary; confirm it's really a repeat

### LLM review decision

When analyzing each batch, if `fine_analysis_rules.json` has `needsReview` edits in the current batch, the LLM adds a `rules_review` field:

```json
{
  "batch_range": [0, 59],
  "edits": [...],
  "rules_review": [
    {"s": 60, "action": "confirm", "reason": "'在這個這個' is indeed a stutter repeat"},
    {"s": 137, "action": "confirm", "reason": "'我我到 door dash' is a stutter"},
    {"s": 109, "action": "reject", "reason": "'的一個一個' here means 'one by one', not stutter"}
  ],
  "scan_summary": {...}
}
```

### Decision principles

| Scenario | Decision | Example |
| --- | --- | --- |
| Pronoun / adverb stutter | ✅ confirm (vast majority) | "我我覺得" → delete first "我" |
| Response repetition | ❌ reject | "對對，你説得對" → "對對" is agreement |
| "一個一個" with one-by-one meaning | ❌ reject | "一個一個地解決" → emphasizes one-by-one |
| Generic rhetoric | ❌ reject | "怎麼怎麼做" → generic |
| Oral connector stutter | ✅ confirm (mostly) | "就是就是説" → stutter |
| Suffix-match real repeat | ✅ confirm | "在這個這個ALL IN" → "這個" is indeed repeated |

**Default lean**: when uncertain, confirm (delete). User feedback shows ×2 repeats are mostly stutter; missing a delete hurts more than over-deleting.

## SKILL.md integration guide

When Step 5b LLM layer runs:

1. Read this template; understand the checklist.
2. Load user editing_rules (if there are override params).
3. **Read `fine_analysis_rules.json`** — see what the rule layer caught and what needs review.
4. Read sentences.txt in batches (**50–80 sentences/batch**, not 150).
5. For each batch, run the checklist + review rule-layer needsReview items, output JSON.
6. After all batches, merge into `fine_analysis_llm.json`.
7. Run `merge_llm_fine.js` to merge with the rule layer (keep confirmed; remove rejected).

### Batch size guidance

| Audio | Sentences | Batch size | Batches |
| --- | --- | --- | --- |
| 30 min | ~200 | 50 | 4 |
| 1 h | ~400 | 60 | 7 |
| 2 h | ~800 | 80 | 10 |

Longer audios can use larger batches (80) — the bottleneck is attention, not context length.
