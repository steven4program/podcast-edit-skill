<!--
input: full text
output: list of stutter indices
pos: rule, suggest-delete priority

Architecture guardian: when this file is modified, also update:
1. README.md in this folder
-->

# Stutter

## Pattern

The same word repeated 2–3 times in a row:

```javascript
const stutterPatterns = [
  '那个那个',
  '然后然后',
  '这个这个',
  '所以所以'
];
```

## Reduplication exclusion (important — avoids false positives!)

The following Chinese reduplications are **normal expressions, not stutters**, and MUST be skipped:

```javascript
const REDUPLICATED_WORDS = new Set([
  // AA pattern: kinship terms
  '妈妈', '爸爸', '宝宝', '哥哥', '姐姐', '弟弟', '奶奶', '爷爷',
  '叔叔', '阿姨', '婆婆', '公公', '舅舅', '姑姑', '伯伯',
  // AA pattern: everyday vocabulary
  '谢谢', '星星', '多多', '甜甜', '乖乖', '饭饭',
  // Onomatopoeia / colloquial
  '巴拉', // "巴拉巴拉" = "blah blah", colloquial omission
  // Verb reduplication AA
  '试试', '看看', '想想', '说说', '聊聊', '走走', '听听', '等等',
  '谈谈', '讲讲', '写写', '读读', '坐坐', '玩玩', '猜猜', '问问',
  // Onomatopoeia
  '哈哈', '嘻嘻', '呵呵', '嘿嘿', '噗噗',
]);

// Onomatopoeia AAA / AAAA pattern (e.g. 哈哈哈, 哈哈哈哈)
const ONOMATOPOEIA_BASES = ['哈', '嘻', '呵', '嘿', '噗', '啦'];

// AAB / ABB (e.g. 慢慢地, 轻轻地, 好好的)
const AAX_PATTERNS = /^(.)\1(地|的|儿|来|去|看|说)$/;

// ABB (e.g. 粉嘟嘟, 胖乎乎, 绿油油, 白花花)
// Detection: current single-char word repeats twice but the previous word differs → ABB structure.
// "粉" + "嘟" + "嘟" → previous word "粉" ≠ "嘟" → ABB reduplication, not a stutter.
function isABBPattern(words, i) {
  if (words[i].text.length === 1 && i > 0 && words[i-1].text !== words[i].text) {
    return true;
  }
  return false;
}

// AABB (e.g. 开开心心, 高高兴兴, 平平安安)
const AABB_PATTERN = /^(.)(.)(\1)(\2)$/;
```

### Exclusion logic

```javascript
function isReduplicated(currentWord, nextWord, fullContext) {
  const combined = currentWord + nextWord;

  // 1. allow-list
  if (REDUPLICATED_WORDS.has(combined)) return true;

  // 2. onomatopoeia repeated ≥2 times (哈哈哈, 嘻嘻嘻)
  if (ONOMATOPOEIA_BASES.includes(currentWord) &&
      currentWord === nextWord) return true;

  // 3. AAB / ABB: two same chars + suffix
  if (AAX_PATTERNS.test(combined)) return true;

  // 4. AABB
  if (AABB_PATTERN.test(combined)) return true;

  return false;
}
```

**Key principle**: if two adjacent identical words combine into a meaningful Chinese reduplication, it's not a stutter. **Better to under-detect than to wrongly delete a normal reduplication.**

### Articulation slip ≠ stutter (important — from restore feedback)

> **Source**: 2026-03-01 feedback. S83's "思思维" was deleted by AI but the user restored it: "shouldn't have been deleted".

When unclear articulation produces a duplicated leading character (e.g. "思思维" = "思维"), the meaning is still understandable as a complete word. **Don't tag this as a stutter** — deleting one character would break the word.

| Source | AI judgement | Right action | Reason |
| --- | --- | --- | --- |
| 思**思**维 | ❌ delete 思 | ✅ keep | articulation slip, listens as 思维 |
| 自**自**己 | ❌ delete 自 | ✅ keep | articulation slip, listens as 自己 |

**Heuristic**: when "AA + B" is followed such that "AB" is itself a complete word (e.g. 思维, 自己), treat it as articulation slip and keep, not stutter.

## Podcast natural-repetition exemption (podcast mode only)

Podcasts are conversational; short word repetitions are natural rhythm, not the stutters of a formal setting.

### Exemption rules

**Single-syllable high-frequency word repetition is kept by default**:

```javascript
const PODCAST_NATURAL_REPEATS = new Set([
  // Pronouns
  '我', '你', '他', '她', '它',
  // High-freq adverbs / conjunctions
  '就', '去', '不', '也', '都', '在', '又', '很', '太', '但', '还',
  // High-freq verbs
  '是', '有', '会', '能', '要', '想', '做', '说', '看', '来', '拉',
]);
```

**Multi-syllable word repetition**: distinguish "high-freq colloquial phrase" from "real stutter":

```javascript
// Common colloquial phrase repeats — keep
const PODCAST_NATURAL_PHRASES = new Set([
  '就是', '怎么', '真的是', '真的', '然后',
  '可能', '其实', '应该', '已经', '这样',
]);
```

| Type | Example | Action | Basis |
| --- | --- | --- | --- |
| Single-syl natural repeat | 就就, 我我, 去去, 不不 | ✅ keep | in PODCAST_NATURAL_REPEATS |
| High-freq phrase repeat | 就是就是, 怎么怎么, 真的是真的是 | ✅ keep | in PODCAST_NATURAL_PHRASES |
| Real stutter | 来自来自, 塑造塑造, 困扰困扰 | ❌ delete | not on any allow-list |
| Reduplication | 妈妈, 看看, 哈哈 | ✅ keep | the reduplication rule above |

**Source**: user xiaoqunzi's sample learning (18 stutters AI flagged):
- 8 single-syl repeats: user kept all → PODCAST_NATURAL_REPEATS exemption was correct ✅
- 4 high-freq phrase repeats (就是就是, 怎么怎么, 真的是真的是, 拉拉): user kept all → PODCAST_NATURAL_PHRASES exemption was correct ✅
- 6 real stutters (最最, 困扰困扰, 如何如何, 来自来自, 我就我就, 塑造塑造): user deleted all → correctly flagged ✅

**User-variation note**:
- xiaoqunzi (lenient): keep every 2× natural repeat
- lucia (strict): even 2× repeats like "我我", "对对" are flagged as misses; "这个" ×5 must be deleted
- **Conclusion**: NATURAL_REPEATS is the default exemption, but ≥3× repeats MUST be flagged regardless of allow-list. Whether 2× is deleted depends on user aggressiveness: moderate/aggressive → delete, conservative → keep.

## Number / measure-word exemption (important — very high FP rate!)

Number + measure-word combinations (e.g. "100份", "一万多块钱", "985") are often split into multiple ASR words, looking like repetition but actually a single number expression. **Must exempt.**

### Exemption rule

```javascript
// Number-related: Arabic digits, Chinese numerals, measure words
const NUMBER_CHARS = /^[\d一二三四五六七八九十百千万亿零两几多半]+$/;
const MEASURE_WORDS = new Set(['个', '份', '块', '元', '万', '亿', '年', '月', '天', '次', '遍', '种', '条', '只']);

// If consecutive repeats are numbers or number components → not a stutter
function isNumberContext(words, i) {
  const w = words[i].text;
  const next = words[i+1] ? words[i+1].text : '';
  const prev = words[i-1] ? words[i-1].text : '';

  // "100" + "份" → number+measure, not stutter
  if (NUMBER_CHARS.test(w) || NUMBER_CHARS.test(next) || NUMBER_CHARS.test(prev)) return true;
  // "一万" + "多" + "块钱" → number expression
  if (MEASURE_WORDS.has(w) || MEASURE_WORDS.has(next)) return true;
  return false;
}
```

**Real cases**:

| Source | ASR split | AI judgement | Right action |
| --- | --- | --- | --- |
| 你可能投100份简历 | "100" "份" | ❌ stutter | ✅ number+measure, keep |
| 一万多块钱 | "一万" "多" "块钱" | ❌ stutter | ✅ number expression, keep |
| 100万 | "100" "万" | ❌ stutter | ✅ number, keep |
| 985 | "985" | ❌ stutter | ✅ proper number, keep |

## Extended pattern: any word repeated in a row

Beyond the fixed list, **any word that appears twice in a row** should be detected as a stutter, **but reduplication and number contexts MUST be excluded first**:

```javascript
// Dynamic: two adjacent identical words (excluding reduplication + numbers)
if (words[i].text === words[i+1].text && words[i].text.length >= 1) {
  if (!isReduplicated(words[i].text, words[i+1].text) && !isNumberContext(words, i)) {
    // stutter — delete the earlier, keep the later
  }
}
```

### Common-miss cases

| Source | Stutter | Delete |
| --- | --- | --- |
| 第一第一份工作 | 第一 | first 第一 |
| 我我最近 | 我 | first 我 |
| 但但是 | 但 | first 但 |
| 放放到台面 | 放 | first 放 |

**Key**: don't rely on the fixed list alone — every adjacent identical pair is a candidate stutter.

### ≥3× repeats must be cleaned to one

> **Source**: 2026-03-01 feedback. S112 "都都都都都被 lay off" — user said "missed one 都".

When the same word appears ≥3 times in a row (even if it's in PODCAST_NATURAL_REPEATS), **trim down to 1**. The natural-repeat exemption applies to 2× only.

| Source | Delete | Keep |
| --- | --- | --- |
| 都都都都都被 lay off | first 4 都 | "都被 lay off" |
| 一个一个一个新的技术 | first 2 一个 | "一个新的技术" |
| 更更更align | first 2 更 | "更align" |

## Delete strategy

Delete the earlier ones, keep the last.

```
Source: "那个那个我想说"
Delete: "那个"
Keep:   "那个我想说"

Source: "第一第一份工作"
Delete: "第一"
Keep:   "第一份工作"
```
