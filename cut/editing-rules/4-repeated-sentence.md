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
| "這是我剪出來的一個案例" | "這是我剪出來的一個案例" | A (exact repeat) |
| "我用claude code的excuse功能做一個剪輯agent" | "所以我用claude code的excuse功能做一個剪輯agent" | A |
| "第二個是是q制技能系统第二個" | "第二個是scale技能系统" | A |
| "好我們接下來開始怎麼去" | "好我們接下來開始怎麼去做一個剪口拨" | A |
| "我們就可以看到這裡新的影片" | "我們就可以看到這裡新的影片了" | A |

## Across-one repeat (residual sentence in the middle)

When a short residual sits between two repeats, detect them too:

```
A:        "這是我剪出來的一個案例"
residual: "他呢"                      ← residual in the middle
B:        "這是我剪出來的一個案例"

→ delete A + residual
```

| Sentence A | Middle residual | Sentence B | Delete |
| --- | --- | --- | --- |
| "這是我剪出來的一個案例" | "他呢" | "這是我剪出來的一個案例" | A + residual |
| "實際上怎麼做呢我們先下載這個" | "提示詞" | "實際上怎麼做呢我們先複製這個提示詞" | A + residual |
| "打開我們的AI" | "打開我們的" | "打開我們的AI然後告訴他去下載" | A + residual |

## Multiple repeats

When the same line is started 3+ times, delete the incomplete ones; keep only the final complete version:

```
"以前呢我會把所有的技能"          → delete
"以前呢我會把所有的功能做"        → delete
"以前呢我會把所有的功能做都"      → delete
"以前呢我會把所有的功能都做成一個大的scale" → keep
```

## Pitfalls

```
❌ char-by-char scan that only finds a local repeated fragment
✓  split sentences first, compare whole-sentence starts, delete the whole sentence

❌ only compare adjacent sentences
✓  also compare across one (the middle may be a residual)
```
