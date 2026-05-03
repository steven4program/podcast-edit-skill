<!--
input: list of sentences split by silence
output: list of repeated-sentence indices
pos: rule, suggest-delete priority

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
-->

# Repeated-sentence detection

## Definition

Adjacent sentences (split by silence) sharing ≥5 characters at the start. Typically a "said it wrong, said it again" pattern.

## Core principle

**Split first, then compare**: split into a sentence list by silence, then compare adjacent sentences.

## Correct procedure

```
✓ Right: split by silence → compare adjacent starts → delete the whole sentence
✗ Wrong: scan character-by-character → find a repeated fragment → delete only the fragment
```

### Steps

1. **Split into sentences** (silence ≥0.5 s as separator)
2. **Compare adjacent sentences** (first 5 chars match → delete the shorter)
3. **Check across one** (when the middle is a residual sentence, also check the surrounding pair)

## Detection logic

```javascript
// Adjacent comparison
if (curr.text.slice(0, 5) === next.text.slice(0, 5)) {
  const shorter = curr.text.length <= next.text.length ? curr : next;
  markAsError(shorter);  // delete the whole sentence, not just the repeated fragment
}

// Across-one comparison (when middle is short/residual)
if (mid.text.length <= 5) {  // middle is residual
  if (curr.text.slice(0, 5) === next.text.slice(0, 5)) {
    markAsError(curr);   // delete previous
    markAsError(mid);    // delete residual
  }
}
```

## Cases

| Sentence A | Sentence B | Delete |
| --- | --- | --- |
| "这是我剪出来的一个案例" | "这是我剪出来的一个案例" | A (exact repeat) |
| "我用cloud code的excuse功能做一个剪辑agent" | "所以我用cloud code的excuse功能做一个剪辑agent" | A |
| "第二个是是q制技能系统第二个" | "第二个是scale技能系统" | A |
| "好我们接下来开始怎么去" | "好我们接下来开始怎么去做一个剪口拨" | A |
| "我们就可以看到这里新的视频" | "我们就可以看到这里新的视频了" | A |

## Across-one repeat (residual sentence in the middle)

When a short residual sits between two repeats, detect them too:

```
A:        "这是我剪出来的一个案例"
residual: "他呢"                      ← residual in the middle
B:        "这是我剪出来的一个案例"

→ delete A + residual
```

| Sentence A | Middle residual | Sentence B | Delete |
| --- | --- | --- | --- |
| "这是我剪出来的一个案例" | "他呢" | "这是我剪出来的一个案例" | A + residual |
| "具体怎么做呢我们首先下载这个" | "提示词" | "具体怎么做呢我们首先复制这个提示词" | A + residual |
| "打开我们的AI" | "打开我们的" | "打开我们的AI然后告诉他去下载" | A + residual |

## Multiple repeats

When the same line is started 3+ times, delete the incomplete ones; keep only the final complete version:

```
"以前呢我会把所有的技能"          → delete
"以前呢我会把所有的功能做"        → delete
"以前呢我会把所有的功能做都"      → delete
"以前呢我会把所有的功能都做成一个大的scale" → keep
```

## Pitfalls

```
❌ char-by-char scan that only finds a local repeated fragment
✓  split sentences first, compare whole-sentence starts, delete the whole sentence

❌ only compare adjacent sentences
✓  also compare across one (the middle may be a residual)
```
