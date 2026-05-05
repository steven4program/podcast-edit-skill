<!--
input: subtitles_words.json
output: list of consecutive-filler indices
pos: rule, suggest-delete priority

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
-->

# Consecutive filler

## Pattern 1: two filler words adjacent

```
嗯啊, 啊呃, 哦嗯, 呃啊
```

## Pattern 2: leading filler/confirm run (≥3 in a row)

**Definition**: a run of fillers + confirm-words (對, 是, 好) at the sentence start, carrying no substantive info.

```
嗯嗯，呃，對，就是…
嗯，對對對，然後…
啊，呃，好，就是説…
```

### Detection

```javascript
const fillerWords  = ['嗯', '啊', '哎', '欸', '呃', '額', '唉', '哦', '噢', '呀'];
const confirmWords = ['對', '是', '好', '行', '嗯嗯'];

// Pattern 1: two adjacent fillers
if (fillerWords.includes(curr) && fillerWords.includes(next)) {
  markAsError(curr, next);
}

// Pattern 2: leading filler+confirm run (≥3)
const allFiller = [...fillerWords, ...confirmWords];
function checkSentenceStartFillerRun(sentenceWords) {
  let runLen = 0;
  for (const w of sentenceWords) {
    if (allFiller.includes(w.text)) {
      runLen++;
    } else break;
  }
  if (runLen >= 3) {
    markAsError(sentenceWords.slice(0, runLen));
  }
}
```

### Cases

| Source | Filler run | Delete |
| --- | --- | --- |
| 嗯嗯，呃，對，就是我 burn out 的一次 | 嗯嗯+呃+對 (3) | "嗯嗯，呃，對，" |
| 嗯，對對對，然後我去了 | 嗯+對+對+對 (4) | "嗯，對對對，" |
| 啊，呃，好，就是説我們 | 啊+呃+好 (3) | "啊，呃，好，" |

## Delete strategy

Delete every member of the run.
