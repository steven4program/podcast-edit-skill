<!--
input: list of sentences split by silence
output: list of residual-sentence indices
pos: rule, suggest-delete priority

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
-->

# Residual sentence

## Definition

The speaker stops mid-sentence; followed by silence or a fresh restart.

## Core principle

**Delete the whole sentence**: once a residual is identified, delete from sentence start to end — not just the trailing characters.

## Correct procedure

```
✓ Right: split sentences first → judge completeness → delete the whole sentence
✗ Wrong: scan char-by-char → spot an odd ending → only delete the ending
```

### Steps

1. **Split into sentences first** (silence ≥0.5 s as separator)
2. **Judge whether each sentence is complete** (semantically, grammatically natural)
3. **Mark the whole residual for delete** (from startIdx to endIdx)

## Pattern

```
residual (whole) + [silence] + complete sentence
       ↓
     delete all
```

## Cases

| Residual | What follows | Delete range |
| --- | --- | --- |
| "他呢" | [silence] + "这是我剪出来的..." | "他呢" whole sentence |
| "为什么做这个东西呢一" | [silence] + "做这个东西的原因是" | **whole sentence** (not just "呢一") |
| "分本区别就是剪映它虽然" | [silence 3 s] + "眼影它是没有学习能力的" | whole sentence + silence |
| "我们先打具体怎么做呢" | "首先" | whole sentence |
| "打开我们的AI" | [silence] + "打开我们的AI然后..." | whole sentence (the earlier incomplete version) |

## Heuristics

1. **Sentence incomplete**: missing object, predicate, or unnatural ending.
2. **Followed by silence**: residuals usually have an obvious pause after.
3. **Followed by a restart**: a fresh start of similar content.

## Difference from repeated sentence

- **Repeated sentence**: both sentences are complete, just share an opening → delete the shorter.
- **Residual sentence**: the earlier is clearly incomplete, interrupted → delete the whole incomplete sentence.

## Common residual signals

- Ends in 呢 / 吧 / 的 etc. (function words) but doesn't form a complete sentence
- Ends in a number or measure word with no following noun
- Sentence cuts off abruptly; meaning incomplete
- Cut off mid-thought, followed by a fresh restart

## Pitfalls

```
❌ Only delete "呢一" (the odd ending)
✓ Delete "为什么做这个东西呢一" (the whole residual)
```

**Remember**: a residual's problem isn't the ending — it's that the whole sentence wasn't completed. Delete the whole thing.
