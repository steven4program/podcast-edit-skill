# Step 5: content-analysis guide

> How to identify big chunks of content that should be deleted from a podcast.

## Core principles

1. **Cut big blocks first; tweak details after** — paragraph-level deletion is much faster than sentence-by-sentence work.
2. **Certainty first** — flag the 100 %-certain deletions (tech debug, privacy) before anything that needs judgment (chit-chat, low information density).
3. **Respect the speaker** — when the speaker explicitly says "don't include this", delete unconditionally.
4. **Keep the concise version** — when the same story is told twice, keep the leaner version, delete the long one.

---

## Two-level analysis flow

### Level 1: paragraph scan

**Goal**: read the whole transcript, partition into topic blocks, mark big delete ranges.

**Steps**:
1. Read `sentences.txt` end-to-end.
2. Partition by topic shift (formal open, self-introduction, topic A, topic B, …).
3. For each block, decide whether it falls into one of the six delete types.
4. Record the sentence range of each delete block.

### Level 2: boundary fine-tune

**Goal**: confirm precise cut points for each delete block.

**Steps**:
1. Inspect the 2–3 sentences before and after each delete block.
2. Make sure the cut isn't mid-conversation (don't split a question from its answer).
3. Make sure content before and after the cut joins naturally.
4. **Isolated-residual check**: after deleting a big block, the 1–2 sentences kept just before / after may stand alone and lose context (e.g. "另外一个节目。" hanging out after a long delete). Mark such orphaned sentences for deletion too. How to spot them: after each big-block delete, re-read the 3 sentences kept on either side; if any is now ambiguous in reference or logically broken, fold it into the delete range.

---

## The six delete types

### 1. Pre-show (`pre_show`)

**Definition**: anything before the formal open ("Hello everyone, welcome to…").

**Signals**:
- "can you hear me", "let me open the doc", "who starts"
- discussing recording software, audio settings
- content coordination ("does it sound OK to talk about this?")

**Action**: delete everything; no exception.

**Boundary**: find the formal opening sentence; delete everything before it.

---

### 2. Tech debug (`tech_debug`)

**Definition**: equipment / technical issues that surface during recording.

**Signals**:
- "headphones died", "mic sounds off", "did it record"
- "I see…", "do you see…"
- Riverside / recording-software discussion

**Action**: delete entirely.

**Boundary**:
- Start: from the first mention of the technical issue.
- End: at "OK, let's continue" or whatever line restarts the topic.

---

### 3. Chit-chat (`chit_chat`)

**Definition**: conversation unrelated to the episode's topic.

**Signals**:
- small talk while waiting for a tech fix
- "what city are you in", "what year did you graduate", "how's the housing market there"
- referencing other shows, recommending unrelated resources
- conversation clearly off the main thread

**Action**: delete, with these exceptions:
- If chit-chat naturally transitions to topic-relevant content, keep from the transition point onward.
- A guest's brief background introduction may also appear in the formal introduction — keep the formal version.

**Boundary**:
- Distinguish "lighthearted but related" from "totally off-topic".
- When the conversation naturally returns to the main thread, keep from the return point.

---

### 4. Privacy (`privacy`)

**Definition**: content the speaker explicitly asked to delete, or sensitive personal information.

**Signals**:
- **Explicit ask**: "don't include this", "I'm not sure about this part", "afraid coworkers will see"
- **Sensitive content**: specific company internal processes, coworker names, salary figures, immigration status details, planned departures

**Action**: delete unconditionally. Better to over-delete than to miss.

**Boundary**:
- Explicit ask: delete the requested span AND the request itself ("don't cut this in" must also go).
- Sensitive content: if context discusses "should we cut this", delete the whole discussion plus the content.

---

### 5. Repeated content (`repeated_content`)

**Definition**: the same experience / story told twice.

**Signals**:
- Speaker re-told a leaner version due to privacy concerns
- "let me say it again", "here's the short version"
- Same storyline, same keywords, different level of detail

**Action**:
- Keep the lean version (usually the second telling, more polished)
- Delete the long version (usually the first, raw telling)
- If the first version has unique info absent from the second, flag it for the user

**Boundary**:
- Long-version start: from the first sentence of telling
- Long-version end: just before the privacy discussion (which also gets deleted)
- The lean version stays untouched

---

### 6. Production talk (`production_talk`)

**Definition**: in-recording discussion about cutting strategy / recording coordination.

**Signals**:
- "should we cut this in", "we can cut this later"
- "should we wrap up", "what was the last question again"
- Discussing show structure / time allocation
- "should we keep that part", "we won't cut in any of the personal stuff"
- **Recording coordination** (easy to miss!): "let's redo the intro", "you go first or me", "want to swap order"

**Action**: delete.

**Boundary**:
- From where production talk starts
- To where podcast content formally resumes (e.g. "OK, let's get into…")

**Watch out: isolated coordination sentences**

Coordination doesn't always come in blocks. It may be a single sentence sandwiched between formal content. Easy to miss in Level 1.

How to spot: while scanning sentence-by-sentence, even if both neighbours are formal content, if a sentence is a directive to a fellow host ("let's redo X", "you cover X"), tag it as `production_talk`.

```
Case:
  S37: [formal content] "...burnout is an important topic."
  S38: [coordination]   "OK, let's redo the intro."  ← single sentence, still delete!
  S39: [formal content] "Hello everyone, I'm..."
```

---

## Duration math and gap handling

### Formula

```
need_to_trim   = total_duration - target_duration
certain_delete = sum of the six type durations
gap            = need_to_trim - certain_delete
```

### If certain delete is enough

Go straight to quality-optimization analysis (next section). No additional duration-driven trimming.

### If a gap remains

Expand the delete range progressively (mark `"action": "delete"`):

1. **Repetitive points** — same point made by different speakers from different angles; keep the best one.
2. **Over-elaborate transitions** — "let's move to the next topic", "嗯嗯嗯对对对" — informationless filler.
3. **Low-density paragraphs** — long buildup but only one or two sentences carry the core info.
4. **Listener-weak details** — overly personal details (unless that's the show's selling point).

---

## Quality-optimization analysis (always run)

**Whether or not there's a duration gap**, scan the whole transcript for the items below. The difference from gap-handling above:
- Gap-handling is **forced delete** (`"action": "delete"`), driven by duration target.
- Quality optimization is **suggest delete** (`"action": "suggest_delete"`), driven by content quality; the user may restore in the review page.

### 1. Repetitive points

Same point expressed by different speakers from different angles. Keep the best telling; flag the rest.

**How**: find adjacent paragraphs (3–5 sentences) with high semantic similarity; compare information delta. If the later paragraph just rephrases the earlier core point, flag the later one.

**Case**:
```
S77: "But in that moment, I felt nothing at all."
S79: "No happiness, not even a hint of relief."
→ S79 is a semantic restatement of S77; suggest deleting S79.
```

### 2. Over-elaborate detail

Narrative with execution-level detail that doesn't help the listener understand the topic.

**How**: when a stretch's core info can be captured in 1–2 sentences but actually uses 5+ sentences for procedural detail, flag the procedural detail.

**Case**:
```
Core info: "I spent days picking the restaurant" (one sentence is enough)
Over-elaborate (suggest delete):
  S33: "First three days I went through every popular list on Dianping and looked at the ambiance one."
  S34: "And service."
  S35: "And taste rankings, and praise rankings — I went through them all."
  S37: "Including the minimum spend for our six people; that took about three days."
```

### 3. Low-density transition

Long buildup with only one or two sentences carrying the actual content; or insubstantial responses.

**How**:
- Short responses that neither introduce a new point nor pivot the conversation — flag.
  - e.g. "Yeah." "Yeah this is the case." "Right."
- Unfinished transitional sentences — flag.
  - e.g. "Yeah, I think this is just." "But our first time out, I personally might."

### 4. Listener-weak personal detail

Overly personal details, unless it's part of the show's appeal.

**How**: ask "does the listener gain anything from this for the topic?" If no, flag.

### Output format

`suggest_delete` blocks are tagged `"confidence": "suggested"` to distinguish from certain deletes:

```json
{
  "id": 2,
  "range": [29, 37],
  "type": "low_density",
  "confidence": "suggested",
  "reason": "Restaurant-picking detail (39s): core info 'I spent days picking the restaurant' — middle steps don't help the listener",
  "duration": "0:39"
}
```

Sentence-level marker uses `"action": "suggest_delete"`:

```json
{ "sentenceIdx": 33, "speaker": "Bella", "action": "suggest_delete", "blockId": 2, "type": "low_density" }
```

---

## Output format

### `semantic_deep_analysis.json`

```json
{
  "version": "5.0",
  "analysisType": "two_level",
  "totalDuration": "2:08:06 (128min)",
  "targetDuration": "90min",
  "blocks": [
    {
      "id": 1,
      "range": [0, 19],
      "type": "pre_show",
      "reason": "Pre-show prep: noise check, who-talks-first, open the doc",
      "duration": "1:06"
    }
  ],
  "sentences": [
    {
      "sentenceIdx": 0,
      "speaker": "Carol",
      "action": "delete",
      "blockId": 1,
      "type": "pre_show",
      "reason": "Pre-show prep: noise check, who-talks-first, open the doc"
    },
    {
      "sentenceIdx": 20,
      "speaker": "Alice",
      "action": "keep"
    }
  ],
  "summary": {
    "totalSentences": 1404,
    "deleteSentences": 227,
    "keepSentences": 1177,
    "deleteBlocks": 13,
    "totalDeleteDuration": "14:23",
    "deleteRatio": "16.2%"
  }
}
```

**Design notes**:
- `blocks` is for humans — quick overview of what big chunks were cut.
- `sentences` is for scripts — `generate_default_selection.js` consumes it directly.
- A kept sentence only needs `sentenceIdx` + `speaker` + `action: "keep"` — no redundant fields.
- A deleted sentence carries `blockId`, `type`, `reason` for traceability.

---

## Real case

### Case: burnout-themed podcast (2h → target 90min)

**Total**: 128 min
**Target**: 90 min
**Need to trim**: 38 min

**Certain delete (14 min)**:

| Type | Blocks | Duration | Typical content |
| --- | --- | --- | --- |
| pre_show | 1 | 1:06 | noise check, open doc, role split |
| tech_debug | 3 | 1:53 | headphone disconnect, muffled mic |
| chit_chat | 2 | 3:22 | Duke vs RTP, recommending therapists |
| privacy | 3 | 1:54 | "don't cut this in", immigration details |
| repeated_content | 2 | 4:48 | burnout background long version, therapist-story long version |
| production_talk | 2 | 1:20 | "should we cut this", wrap-up discussion |
| **Total** | **13** | **14:23** | |

**Gap**: ~24 min, need to fill via information-density analysis.
