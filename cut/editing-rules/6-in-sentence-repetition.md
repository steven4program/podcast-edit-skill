<!--
input: list of sentences split by silence
output: list of in-sentence-repetition indices
pos: rule, suggest-delete priority

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
-->

# In-sentence repetition

## Definition

Within a single sentence, phrase A appears more than once. Two patterns: word-level and phrase-level.

## Pattern 1: word level (A + middle chars + A)

```
A + middle (1–3 chars) + A
```

### Cases

| Source | Pattern | Delete |
| --- | --- | --- |
| 所以小所以 | 所以 + 小 + 所以 | "所以小" |
| 然后就会然后 | 然后 + 就会 + 然后 | "然后就会" |
| 任务3任务3 | 任务3 + 任务3 | first one |
| 什么关系什么 | 什么 + 关系 + 什么 | "什么关系" |

## Pattern 2: phrase level (focus area! easy to miss)

**Definition**: a phrase ≥4 chars appears twice in the same sentence with a few correction / hesitation words in between.

```
phrase A + hesitation/correction + phrase A (possibly slightly varied)
```

### Detection

```javascript
// Find a repeated substring of ≥4 chars in the sentence text;
// the two occurrences should be separated by at most 2× the phrase length.
function findPhraseRepeats(sentenceText) {
  for (let len = 4; len <= sentenceText.length / 2; len++) {
    for (let i = 0; i <= sentenceText.length - len; i++) {
      const phrase = sentenceText.slice(i, i + len);
      const nextPos = sentenceText.indexOf(phrase, i + len);
      if (nextPos >= 0 && (nextPos - i - len) <= len * 2) {
        return { deleteFrom: i, deleteTo: nextPos };
      }
    }
  }
}
```

### Cases

| Source | Repeated phrase | Delete |
| --- | --- | --- |
| 和大家都很关心的，很，和大家都很关心的，经常发生的 | "和大家都很关心的" | "和大家都很关心的，很，" |
| 放到台面来说，放放到台面上来说 | "放到台面" | "放到台面来说，放" |
| 我觉得这个事情，嗯，我觉得这个事情挺重要 | "我觉得这个事情" | "我觉得这个事情，嗯，" |

### Key distinction

Word-level: A is 1–2 words (所以, 然后); middle 1–3 chars.
Phrase-level: A is ≥4 chars (和大家都很关心的); middle can be longer.

**Phrase-level is harder to detect but more impactful on listening; should be higher priority than word-level.**

## NOT slip-of-the-tongue

| Source | Why |
| --- | --- |
| 任务1任务2任务3 | enumeration |
| to do | English |
| 一个一个地 | emphasis |
| 越来越好，越来越强 | rhetorical progression |
| 开开心心、高高兴兴 | AABB reduplication, normal |
| 慢慢地、轻轻地、好好的 | AAB reduplication, normal |
| 妈妈、宝宝、谢谢、哈哈哈 | AA reduplication / onomatopoeia (see 5-stutter exclusion rules) |

## Delete strategy: keep one copy (extremely important!)

> **Source**: 2026-03-01 feedback. S79 "这条路这条路", S415 "蛮多的蛮多的" — both restored, reason "you deleted one too many".

For `A A` patterns (exact repetition twice in a row), **delete the first A and keep the second**.

| Source | ❌ Wrong: delete both | ✅ Right: delete only one |
| --- | --- | --- |
| 蛮多的**蛮多的**这个信息 | "蛮多的蛮多的" deleted → "这个信息" | "蛮多的" deleted → "蛮多的这个信息" |
| 这条路**这条路**更适合我 | "这条路这条路" deleted → "更适合我" | "这条路" deleted → "这条路更适合我" |
| 一个一个一个新的技术 | every "一个" deleted → "新的技术" | two deleted → "一个新的技术" |

**Implementation notes**:
1. When N consecutive repetitions are detected, `deleteCount = N - 1` (keep the last one).
2. `deleteRange` = from the first A's start to the second-to-last A's end (or to the last A's start).
3. NEVER delete the final copy.
