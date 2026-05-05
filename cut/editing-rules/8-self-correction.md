<!--
input: subtitles_words.json
output: list of self-correction indices
pos: rule, suggest-delete priority

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
-->

# Self-correction

## Pattern

Said it wrong, immediately corrected — delete the wrong part before the correction.

### 1. Partial repetition

Words on either side overlap but aren't identical:

| Source | Delete |
| --- | --- |
| 你再關你關掉 | "你再關" |
| 怎麼讓它去有個更大的 | "怎麼讓它去" |

### 2. Negation correction

Negation word corrects what was just said:

| Source | Delete |
| --- | --- |
| 它是它不是 | "它是" |
| 可以不可以 | "可以" |

### 3. Word interrupted

Half a word + silence + restated complete word:

| Source | Delete |
| --- | --- |
| 依賴[silence]依賴關係 | "依賴[silence]" |

### 4. Mid-sentence half-restart

Caught the slip mid-way and reorganized. The incomplete first half should go:

| Source | Delete | Notes |
| --- | --- | --- |
| 去裁了只被我困在 | "去裁了只被" | Restarted; keep "我困在" |
| 去裁了之被我困在 | "去裁了之被" | Same; ASR transcription may vary |
| 五年之這五年間 | "五年之" | "五年之" is a half; corrected to "這五年間" |
| 你的對你對世界的 | "你的對" | "你的對" is a half; corrected to "你對世界的" |

### 5. Reference correction

Subject or modifier was changed:

| Source | Delete | Notes |
| --- | --- | --- |
| 你的對你對世界的觀察 | "你的對" | modifier structure changed |

### 6. Particle-end false start (run ending in a tone particle)

A phrase ends in a tone particle (呢/啊/吧/嘛), immediately followed by a more complete restatement:

| Source | Delete | Notes |
| --- | --- | --- |
| 上一集**呢**在上一集的超越百歲裡 | "上一集呢" | "呢" marks an abandoned start; "在上一集的…" is the complete restatement |
| 關於繼續我們繼續講關於這個 | "關於繼續" | "關於繼續" is a stitched false start; "我們繼續講關於…" is the right phrasing |

**Heuristic**: tone particles usually live at sentence end; appearing **mid-sentence** with similar content right after is likely a false start.

### 7. Same-prefix expansion

A phrase with the same prefix appears twice; the second is more complete:

| Source | Delete | Notes |
| --- | --- | --- |
| 是一個底層，是一個底層 boss | "是一個底層，" | Second adds "boss" — more complete |
| 在人身上在人身上電 | "在人身上" | Second adds "電" — more complete |
| 比較慢的一個比較慢速的 | "比較慢的" | "慢速" is more precise than "慢" |
| 中樞神經系统好像中樞神經系统沒辦法去 | "中樞神經系统好像" | Second uses a more definite predicate |

**Heuristic**: in a sliding window (8–12 words), find a common prefix of ≥2 chars appearing twice; if the later one is longer/more complete → delete the earlier.

### 8. Synonymous restatement

Same meaning expressed twice with different wording:

| Source | Delete | Notes |
| --- | --- | --- |
| 睡了一睡睡覺一晚上 | "睡了一睡" | "睡了一睡" is half; "睡覺一晚上" is the complete version |
| 但其實更重要，但其實呃應該是。 | "但其實呃應該是。" | Second was abandoned; keep the first |

**Heuristic**: needs semantic understanding; pure rules can't catch it. The LLM should judge which version is more complete/fluent.

> **User feedback (lucia, 2026-02-24)**: 14 self-correction misses (54 % of total misses). Patterns 6–8 above came from that feedback batch.

### 9. Restart-signal words (rule layer can detect) 🆕

Speaker explicitly says a restart signal, then begins again:

| Source | Delete | Signal |
| --- | --- | --- |
| hello，大家好，等一下hello，大家好 | "hello，大家好，等一下" | 等一下 |
| 我們來討論一下，不對，我們來討論 | "我們來討論一下，不對，" | 不對 |

**Signal list**: 等一下, 重來, 再説一遍, 再來, 重新説, 重新來, 不對, 説錯了, 我重説, 再來一遍.

**Detection (rule layer, `run_fine_analysis.js`)**:
1. Scan the sentence for signal words.
2. Compare the text on either side of the signal (first N words, N ≤ 5).
3. Character overlap ≥ 60 % → false start.
4. Delete first attempt + signal; keep the second attempt.

**Advantage**: deterministic pattern, doesn't depend on LLM, 100 % accurate.

### 10. Noun / measure-word correction (wrong noun → correct noun)

Speaker used the wrong noun / measure-word, immediately corrected:

| Source | Delete | Notes |
| --- | --- | --- |
| 呃台灣大學的老师，交通大學的老师 | "呃台灣大學的老师，" | School name corrected to 交通 |
| 有一段，有幾段工作經驗 | "有一段，" | measure word "一段" corrected to "幾段" |
| 一些啊幾門課 | "一些啊" | "一些" corrected to the more precise "幾門" |

**Heuristic**: in-sentence "X的Y，Z的Y" or "有A，有B" structure with X≠Z / A≠B; the earlier is the wrong version → delete it.

### 11. Mid-restart (paused mid-way and switched phrasing)

| Source | Delete | Notes |
| --- | --- | --- |
| 一年以後或者拿拿到 | "一年以後或者" | "或者" marks the break; what follows is the new phrasing |
| 一個工作的經歷，我或者我第一份工作 | "一個工作的經歷，我或者" | "或者" marks the restatement |

**Heuristic**: "或者" appears mid-sentence and both sides describe the same thing → the earlier is the abandoned version.

### 12. Inserted stutter ("是也是", "改變我的改變")

A redundant fragment in the form "X Y X" where the first X is an extraneous false start:

| Source | Delete | Notes |
| --- | --- | --- |
| 這個**是**也是一個 | "是" | "是" is the false start; "也是" is correct |
| 改變**我的改變**我的情况 | "我的改變" | "改變我的改變我的" → delete the middle "我的改變" |
| 可以**去**可以去做的 | "去" | First "去" is the false start |

## Over-deletion guard (learned from restore feedback)

> **Source**: 2026-03-01 feedback. Of 11 restores, 6 were over-deleted self-corrections.

### Iron rule: exact "X X" deletes only one X

When two perfectly identical phrases sit adjacent, **delete only one** (usually the earlier); never delete both.

| Source | ❌ Wrong delete | ✅ Right delete |
| --- | --- | --- |
| 這條路**這條路**更適合我 | "這條路這條路" | "這條路" (one) |
| 蠻多的**蠻多的**這個訊息 | "蠻多的蠻多的" | "蠻多的" (one) |
| 能夠在**能夠在**一個很大規模 | "能夠在能夠在" | "能夠在" (one) |
| 沒有被啊**沒有被**這個創造過 | "沒有被啊沒有被" | "沒有被啊" (one) |
| 都沒有特别的，**都沒有特别的** | "都沒有特别的，都沒有特别的" | "都沒有特别的，" (one) |

**Implementation**: when "X X" is detected, `deleteRange` covers only the first X (and any hesitation word in between, e.g. "啊"); keep the complete second X.

### Iron rule: pronunciation slips are not separately deletable

When unclear articulation produces a wrong-sound, if the overall meaning is still understandable, don't delete the wrong-sound on its own.

| Source | ❌ Wrong delete | ✅ Right action |
| --- | --- | --- |
| 思**思**维 | delete 思 | keep as-is ("思思维" reads as 思维) |
| 一**幾**段經驗 | delete 一 | keep as-is ("一幾段" is a natural correction) |
| 經**有**經濟價值 | delete 經有 | keep as-is ("經有經濟" reads as 經濟) |

**Heuristic**: if deleting would break sentence integrity (e.g. only "幾段" remains, missing measure-word feel), don't delete.

## Detection logic

```javascript
// Method 1: find common prefix in adjacent words
if (word[i].text.startsWith(prefix) && word[i+n].text.startsWith(prefix)) {
  // and the later is more complete → delete the earlier
}

// Method 2: windowed scan — find repeated start words in an 8–12 word window
// "五年之...這五年間" → "五年" twice, the earlier is incomplete → delete
// "你的對...你對世界的" → "你" twice, the earlier is incomplete → delete
// "是一個底層，...是一個底層 boss" → "是一個底層" twice, the later is more complete → delete the earlier
// Key: the later version is more complete / fluent

// Method 3: particle-end detection — mid-sentence tone particle followed by similar content
// "上一集呢在上一集的..." → "上一集" twice, first ends in "呢" → likely false start
// "關於繼續我們繼續講關於..." → "關於/繼續" twice → false start
```

**Difficulty**: this kind of restatement doesn't always have a common prefix — it's "said a few words, realized it was wrong, paused, restarted with new phrasing". Needs LLM semantic understanding to identify which part is the abandoned half.

**LLM-layer self-check (every sentence)**:
1. Is there a phrase of ≥2 chars repeated nearby? → likely pattern 1 / 4 / 7
2. Does a tone particle (呢/啊/吧) appear in a non-final position? → likely pattern 6
3. Does the sentence read more fluently after deleting the suspect? → if yes, very likely should delete
4. Does the same meaning appear twice in different wording? → likely pattern 8

## Delete-boundary principle (extremely important!)

**Delete only the stuttered/wrong prefix; keep the final correct expression.** This is where self-correction is most often over-cut.

### ❌ Common mistake: over-deletion

| Source | Wrong delete | Result | Problem |
| --- | --- | --- | --- |
| 就是就是努力。 | "就是就是努力。" | whole sentence gone | "努力" is the key meaning |
| 但最後最後其實我也沒錄取 | "但最後" | loses "但" connector | "但" carries meaning |
| 怎麼能能能能能做到，對吧？ | whole sentence | gone | content lost entirely |
| 所以我覺得啊，我的第二個關鍵詞可能就是就是就是探索。 | whole sentence | gone | key info "second keyword is 探索" lost |

### ✅ Right approach

| Source | Right delete | Keep |
| --- | --- | --- |
| 就是就是努力。 | "就是" | "就是努力。" |
| 但最後最後其實我也沒錄取 | "最後" (one repeat) | "但最後其實我也沒錄取" |
| 怎麼能能能能能做到，對吧？ | "能能能能" | "怎麼能做到，對吧？" |
| 所以我覺得啊...就是就是就是探索。 | "就是就是" | "...就是探索。" |

### Three iron rules

1. **Keep semantic connectors**: 但 / 所以 / 然後 / 因為 etc. carry meaning, never delete even when surrounded by stutters.
2. **Keep the final complete expression**: after N stutters, the last one is the correct version — must keep.
3. **Split complex sentences**: when a sentence has multiple stutter points, split into multiple independent edits, one per stutter.

### Complex-sentence split example

Source: `對，然後但最後最後其實我也沒錄取產品經理啊，這是一個但這但這個是有原因的了。`

Split into two independent edits:
- Edit 1: delete "最後" (the "最後最後" stutter, only one repeat)
- Edit 2: delete "這是一個但這" (keep "但這個是有原因的了")

Result: `對，然後但最後其實我也沒錄取產品經理啊，但這個是有原因的了。`
