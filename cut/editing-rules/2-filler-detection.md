<!--
input: subtitles_words.json
output: list of filler-word indices
pos: rule, human-confirmation priority (podcast version: more lenient)

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
-->

# Filler-word detection (podcast version)

## Filler word list

```javascript
const fillerWords = ['嗯', '啊', '哎', '欸', '呃', '額', '唉', '哦', '噢', '呀', '欸', '那個', '就是', '然後'];
```

## Podcast handling principle

**Important**: fillers are part of natural conversation in podcasts — don't over-delete.

### Keep
1. **Single filler**: usually keep, adds naturalness.
2. **Thinking pause**: "嗯…" indicating thought — keep.
3. **Emotional expression**: "啊!", "哦~" — keep.
4. **Conversational response**: "嗯", "對" after the other person speaks (signals listening) — keep.

### Delete (suggested)
1. **Sentence-leading 嗯/呃/啊**: when the first 1–2 words are filler (嗯, 呃, 啊, 嗯，, 呃，, …) followed by substantive content, delete just the filler (word-level fine cut, not the whole sentence). **This is the highest-priority fine-cut rule and must be detected.**
   - Match pattern: sentence starts with `嗯[，、]?|呃[，、]?|啊[，、]?`
   - Delete range: filler + the trailing comma / 顿号 (if any)
2. **3+ in a row**: 嗯嗯嗯, 啊啊啊
3. **Obvious stutter**: 呃呃…那個那個…
4. **Meaningless rapid repetition**: 就是就是就是

## Delete boundary (extremely important!)

```
❌ Wrong: delete using filler timestamps (filler.start - filler.end)
   → leftover onset leak (faint sound) at the start, unnatural silence after

✅ Right: from the previous word's end to the next word's start
   → [prevWord.end, nextWord.start]
```

### Why must we extend the range?

Two known ASR-timestamp issues:

1. **Onset leak**: filler vocalization actually starts before ASR's reported `start`. Deleting `[filler.start, filler.end]` leaves a faint head.
2. **Tail gap**: silence between filler.end and the next word.start, after deleting the filler, becomes an unnatural pause.

### Real cases

| Filler | ASR timestamp | Extended range | Notes |
| --- | --- | --- | --- |
| S11 嗯 | [58.67, 59.03] | [58.67, 59.39] | extended right to next word.start |
| S24 嗯 | [147.31, 147.91] | [147.07, 148.61] | extended left to "因為".end, right to "我".start |
| S26 嗯 | [156.77, 156.89] | [156.17, 156.97] | extended on both sides |
| S28 呃 | [168.49, 169.41] | [167.77, 169.41] | extended left to "很多。".end |

## In-sentence single-character filler (fine-cut focus!)

> **Source**: 2026-03-01 feedback — of 46 stutter misses, ~35 were in-sentence single-char fillers (啊/呃/對).

The current rule only catches **sentence-leading** fillers. In real podcasts a lot of misses happen **inside the sentence**. These should be flagged for deletion:

### In-sentence 呃/啊 right after a function word

Pattern: `…的呃/啊 + content` or `…這個呃/啊 + content`

Filler shows up after structural words like 的, 這個, 一個— speaker is searching for the next word. Deletion makes the sentence flow.

| Source | Delete | Notes |
| --- | --- | --- |
| 我自己的**啊**這個經歷 | 啊 | hesitation after 的 |
| 一個**呃**專業 | 呃 | hesitation after 一個 |
| 這個**呃**比較STATE OF THE ART | 呃 | hesitation after 這個 |
| 覺得**呃**這不是我特别有興趣的 | 呃 | hesitation after a verb |
| 是我**呃**第一個沒有想到的 | 呃 | hesitation after a copula |
| 大概**呃**大幾十個人 | 呃 | hesitation between quantifiers |

### In-sentence 啊 between commas

Pattern: `…X，啊，Y…` or `…X，啊Y…`

In-sentence 啊 sandwiched by commas / pauses, carries no semantics — breath or hesitation.

| Source | Delete | Notes |
| --- | --- | --- |
| 再怎麼樣，**啊**，其實這個如果碰到 | 啊 | hesitation between commas |
| 2017年左右**啊**，如果你想要 | 啊 | hesitation after a time phrase |
| 一些更加叫第一性原理…啊…THINKING | 啊 | hesitation between Chinese/English switch |

### In-sentence verbal tic 對

Pattern: `…content，對，content…`

"對" inserted as a verbal tic mid-sentence (NOT a conversational response). There's substantive content on both sides and "對" carries no semantics.

| Source | Delete | Notes |
| --- | --- | --- |
| 短時間内，**對**，比較快的去 | 對 | mid-sentence tic |
| 這個可能是我啊，**對**，跟我最後决定 | 對 | mid-sentence tic |
| 先先解釋一下，**對**，然後我自己 | 對 | mid-sentence tic |

### Detection rule

```javascript
// In-sentence filler (complements the leading-filler check)
// Conditions:
// 1. word is 啊/呃/額/對 and not at the sentence start
// 2. both neighbours are substantive (non-filler)
// 3. word duration < 0.5 s (excludes interjection / emphasis)
// 4. not a conversational response (when "對" is its own sentence → keep)
function isMidSentenceFiller(word, prevWord, nextWord, isFirstWord) {
  const MID_FILLERS = new Set(['啊', '呃', '額', '對']);
  if (!MID_FILLERS.has(word.text)) return false;
  if (isFirstWord) return false; // leading filler handled by the existing rule
  if (word.end - word.start > 0.5) return false; // long = interjection/emphasis
  if (!prevWord || !nextWord) return false; // sentence-final keep
  return true;
}
```

### Difference from keep rules

| Scenario | Action | Reason |
| --- | --- | --- |
| 嗯，對 as a whole sentence | keep | conversational response |
| 對對對 expressing agreement | keep | emotion |
| In-sentence …X，對，Y… | delete | verbal tic, no semantics |
| 啊! interjection | keep | emotion (duration > 0.5 s) |
| In-sentence …的啊這個… | delete | hesitation filler |

## User preference (podcast default)

- **Default keep single filler**: preserve conversational naturalness
- **Only delete repeats**: only obvious excess
- **Respect speaking style**: some hosts' "嗯" is a stylistic choice
- **Conservative for dialogue**: in multi-host shows, keep response words

## AI annotation hints

When marking, add a reason, e.g.:

```
| 45 | 10.2-10.4 | 嗯，我覺得... | leading 嗯, fine-cut delete | delete (word-level) |
| 52 | 12.1-12.3 | 嗯 | single, conversational response | keep |
| 67-69 | 15.6-16.1 | 嗯嗯嗯 | 3 in a row | delete |
```
