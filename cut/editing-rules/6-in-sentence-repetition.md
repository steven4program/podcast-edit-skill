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
| 然後就會然後 | 然後 + 就會 + 然後 | "然後就會" |
| 任務3任務3 | 任務3 + 任務3 | first one |
| 什麼關係什麼 | 什麼 + 關係 + 什麼 | "什麼關係" |

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
| 和大家都很關心的，很，和大家都很關心的，經常發生的 | "和大家都很關心的" | "和大家都很關心的，很，" |
| 放到檯面來説，放放到檯面上來説 | "放到檯面" | "放到檯面來説，放" |
| 我覺得這個事情，嗯，我覺得這個事情很重要 | "我覺得這個事情" | "我覺得這個事情，嗯，" |

### Key distinction

Word-level: A is 1–2 words (所以, 然後); middle 1–3 chars.
Phrase-level: A is ≥4 chars (和大家都很關心的); middle can be longer.

**Phrase-level is harder to detect but more impactful on listening; should be higher priority than word-level.**

## NOT slip-of-the-tongue

| Source | Why |
| --- | --- |
| 任務1任務2任務3 | enumeration |
| to do | English |
| 一個一個地 | emphasis |
| 越來越好，越來越强 | rhetorical progression |
| 開開心心、高高興興 | AABB reduplication, normal |
| 慢慢地、輕輕地、好好的 | AAB reduplication, normal |
| 媽媽、寶寶、謝謝、哈哈哈 | AA reduplication / onomatopoeia (see 5-stutter exclusion rules) |
| 然後Codex也會用然後Copilot| parallel clauses joined by 然後/所以/但是…etc. (Pitfall 42) |
| 我也是我目前還沒有 | conjunction-prefixed structural repetition |

### Conjunction-prefix hard filter

Phrases starting with a Chinese conjunction (`然後` `所以` `但是` `可是` `不過` `就是` `因為` `而且` `或是` `或者` `還有`) are almost always **parallel clause markers**, not stuttered phrase repeats. Hard-filter them before running the phrase search.

Without this filter, sentences like `"然後Codex也會用然後Copilot也會用"` get matched on `"然後Code"` and the rule emits a giant delete that wipes most of the kept content. (Pitfall 42, 2026-05.)

## Implementation safeguards (rules layer)

The rules-layer detector lives in `cut/scripts/run_fine_analysis.js`. Three safeguards protect against the false positives that previously emitted multi-second deletes from a single short repeat:

1. **Word-mapping bug** — the original code used `wOrigStart <= deleteOrigStart && deleteWordStart < 0`, which always picked word 0 (whose start is 0). Correct: `deleteWordStart` is the first word whose **end** is greater than `deleteOrigStart` (the word that contains the start char). (Pitfall 42.)
2. **Conjunction-prefix filter** — see above.
3. **Length sanity guard** — after computing `deleteText` from the chosen word range, drop the edit if `deleteText` (clean of punctuation) is more than `phraseLen + gap + 4` chars longer than expected. This is the last line of defence: if either the phrase search or the word-mapping is off, the safeguard prevents emitting a clearly-wrong giant delete.

## Delete strategy: keep one copy (extremely important!)

> **Source**: 2026-03-01 feedback. S79 "這條路這條路", S415 "蠻多的蠻多的" — both restored, reason "you deleted one too many".

For `A A` patterns (exact repetition twice in a row), **delete the first A and keep the second**.

| Source | ❌ Wrong: delete both | ✅ Right: delete only one |
| --- | --- | --- |
| 蠻多的**蠻多的**這個訊息 | "蠻多的蠻多的" deleted → "這個訊息" | "蠻多的" deleted → "蠻多的這個訊息" |
| 這條路**這條路**更適合我 | "這條路這條路" deleted → "更適合我" | "這條路" deleted → "這條路更適合我" |
| 一個一個一個新的技術 | every "一個" deleted → "新的技術" | two deleted → "一個新的技術" |

**Implementation notes**:
1. When N consecutive repetitions are detected, `deleteCount = N - 1` (keep the last one).
2. `deleteRange` = from the first A's start to the second-to-last A's end (or to the last A's start).
3. NEVER delete the final copy.
