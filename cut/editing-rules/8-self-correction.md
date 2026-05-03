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
| 你再关你关掉 | "你再关" |
| 怎么让它去有个更大的 | "怎么让它去" |

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
| 依赖[silence]依赖关系 | "依赖[silence]" |

### 4. Mid-sentence half-restart

Caught the slip mid-way and reorganized. The incomplete first half should go:

| Source | Delete | Notes |
| --- | --- | --- |
| 去裁了只被我困在 | "去裁了只被" | Restarted; keep "我困在" |
| 去裁了之被我困在 | "去裁了之被" | Same; ASR transcription may vary |
| 五年之这五年间 | "五年之" | "五年之" is a half; corrected to "这五年间" |
| 你的对你对世界的 | "你的对" | "你的对" is a half; corrected to "你对世界的" |

### 5. Reference correction

Subject or modifier was changed:

| Source | Delete | Notes |
| --- | --- | --- |
| 你的对你对世界的观察 | "你的对" | modifier structure changed |

### 6. Particle-end false start (run ending in a tone particle)

A phrase ends in a tone particle (呢/啊/吧/嘛), immediately followed by a more complete restatement:

| Source | Delete | Notes |
| --- | --- | --- |
| 上一期**呢**在上一期的超越百岁里边 | "上一期呢" | "呢" marks an abandoned start; "在上一期的…" is the complete restatement |
| 关于继续我们继续讲关于这个 | "关于继续" | "关于继续" is a stitched false start; "我们继续讲关于…" is the right phrasing |

**Heuristic**: tone particles usually live at sentence end; appearing **mid-sentence** with similar content right after is likely a false start.

### 7. Same-prefix expansion

A phrase with the same prefix appears twice; the second is more complete:

| Source | Delete | Notes |
| --- | --- | --- |
| 是一个底层，是一个底层 boss | "是一个底层，" | Second adds "boss" — more complete |
| 在人身上在人身上电 | "在人身上" | Second adds "电" — more complete |
| 比较慢的一个比较慢速的 | "比较慢的" | "慢速" is more precise than "慢" |
| 中枢神经系统好像中枢神经系统没办法去 | "中枢神经系统好像" | Second uses a more definite predicate |

**Heuristic**: in a sliding window (8–12 words), find a common prefix of ≥2 chars appearing twice; if the later one is longer/more complete → delete the earlier.

### 8. Synonymous restatement

Same meaning expressed twice with different wording:

| Source | Delete | Notes |
| --- | --- | --- |
| 睡了一睡睡觉一晚上 | "睡了一睡" | "睡了一睡" is half; "睡觉一晚上" is the complete version |
| 但其实更重要，但其实呃应该是。 | "但其实呃应该是。" | Second was abandoned; keep the first |

**Heuristic**: needs semantic understanding; pure rules can't catch it. The LLM should judge which version is more complete/fluent.

> **User feedback (lucia, 2026-02-24)**: 14 self-correction misses (54 % of total misses). Patterns 6–8 above came from that feedback batch.

### 9. Restart-signal words (rule layer can detect) 🆕

Speaker explicitly says a restart signal, then begins again:

| Source | Delete | Signal |
| --- | --- | --- |
| hello，大家好，等一下hello，大家好 | "hello，大家好，等一下" | 等一下 |
| 我们来讨论一下，不对，我们来讨论 | "我们来讨论一下，不对，" | 不对 |

**Signal list**: 等一下, 重来, 再说一遍, 再来, 重新说, 重新来, 不对, 说错了, 我重说, 再来一遍.

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
| 呃上海大学的老师，复旦大学的老师 | "呃上海大学的老师，" | School name corrected to 复旦 |
| 有一段，有几段工作经验 | "有一段，" | measure word "一段" corrected to "几段" |
| 一些啊几门课 | "一些啊" | "一些" corrected to the more precise "几门" |

**Heuristic**: in-sentence "X的Y，Z的Y" or "有A，有B" structure with X≠Z / A≠B; the earlier is the wrong version → delete it.

### 11. Mid-restart (paused mid-way and switched phrasing)

| Source | Delete | Notes |
| --- | --- | --- |
| 一年以后或者拿拿到 | "一年以后或者" | "或者" marks the break; what follows is the new phrasing |
| 一个工作的经历，我或者我第一份工作 | "一个工作的经历，我或者" | "或者" marks the restatement |

**Heuristic**: "或者" appears mid-sentence and both sides describe the same thing → the earlier is the abandoned version.

### 12. Inserted stutter ("是也是", "改变我的改变")

A redundant fragment in the form "X Y X" where the first X is an extraneous false start:

| Source | Delete | Notes |
| --- | --- | --- |
| 这个**是**也是一个 | "是" | "是" is the false start; "也是" is correct |
| 改变**我的改变**我的情况 | "我的改变" | "改变我的改变我的" → delete the middle "我的改变" |
| 可以**去**可以去做的 | "去" | First "去" is the false start |

## Over-deletion guard (learned from restore feedback)

> **Source**: 2026-03-01 feedback. Of 11 restores, 6 were over-deleted self-corrections.

### Iron rule: exact "X X" deletes only one X

When two perfectly identical phrases sit adjacent, **delete only one** (usually the earlier); never delete both.

| Source | ❌ Wrong delete | ✅ Right delete |
| --- | --- | --- |
| 这条路**这条路**更适合我 | "这条路这条路" | "这条路" (one) |
| 蛮多的**蛮多的**这个信息 | "蛮多的蛮多的" | "蛮多的" (one) |
| 能够在**能够在**一个很大规模 | "能够在能够在" | "能够在" (one) |
| 没有被啊**没有被**这个创造过 | "没有被啊没有被" | "没有被啊" (one) |
| 都没有特别的，**都没有特别的** | "都没有特别的，都没有特别的" | "都没有特别的，" (one) |

**Implementation**: when "X X" is detected, `deleteRange` covers only the first X (and any hesitation word in between, e.g. "啊"); keep the complete second X.

### Iron rule: pronunciation slips are not separately deletable

When unclear articulation produces a wrong-sound, if the overall meaning is still understandable, don't delete the wrong-sound on its own.

| Source | ❌ Wrong delete | ✅ Right action |
| --- | --- | --- |
| 思**思**维 | delete 思 | keep as-is ("思思维" reads as 思维) |
| 一**几**段经验 | delete 一 | keep as-is ("一几段" is a natural correction) |
| 经**有**经济价值 | delete 经有 | keep as-is ("经有经济" reads as 经济) |

**Heuristic**: if deleting would break sentence integrity (e.g. only "几段" remains, missing measure-word feel), don't delete.

## Detection logic

```javascript
// Method 1: find common prefix in adjacent words
if (word[i].text.startsWith(prefix) && word[i+n].text.startsWith(prefix)) {
  // and the later is more complete → delete the earlier
}

// Method 2: windowed scan — find repeated start words in an 8–12 word window
// "五年之...这五年间" → "五年" twice, the earlier is incomplete → delete
// "你的对...你对世界的" → "你" twice, the earlier is incomplete → delete
// "是一个底层，...是一个底层 boss" → "是一个底层" twice, the later is more complete → delete the earlier
// Key: the later version is more complete / fluent

// Method 3: particle-end detection — mid-sentence tone particle followed by similar content
// "上一期呢在上一期的..." → "上一期" twice, first ends in "呢" → likely false start
// "关于继续我们继续讲关于..." → "关于/继续" twice → false start
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
| 但最后最后其实我也没当上 | "但最后" | loses "但" connector | "但" carries meaning |
| 怎么能能能能能做到，对吧？ | whole sentence | gone | content lost entirely |
| 所以我觉得啊，我的第二个关键词可能就是就是就是探索。 | whole sentence | gone | key info "second keyword is 探索" lost |

### ✅ Right approach

| Source | Right delete | Keep |
| --- | --- | --- |
| 就是就是努力。 | "就是" | "就是努力。" |
| 但最后最后其实我也没当上 | "最后" (one repeat) | "但最后其实我也没当上" |
| 怎么能能能能能做到，对吧？ | "能能能能" | "怎么能做到，对吧？" |
| 所以我觉得啊...就是就是就是探索。 | "就是就是" | "...就是探索。" |

### Three iron rules

1. **Keep semantic connectors**: 但 / 所以 / 然后 / 因为 etc. carry meaning, never delete even when surrounded by stutters.
2. **Keep the final complete expression**: after N stutters, the last one is the correct version — must keep.
3. **Split complex sentences**: when a sentence has multiple stutter points, split into multiple independent edits, one per stutter.

### Complex-sentence split example

Source: `对，然后但最后最后其实我也没当上产品经理啊，这是一个但这但这个是有原因的了。`

Split into two independent edits:
- Edit 1: delete "最后" (the "最后最后" stutter, only one repeat)
- Edit 2: delete "这是一个但这" (keep "但这个是有原因的了")

Result: `对，然后但最后其实我也没当上产品经理啊，但这个是有原因的了。`
