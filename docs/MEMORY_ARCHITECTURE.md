# Memory Architecture

This project uses a layered memory system inspired by Letta/MemGPT memory hierarchy, Graphiti/Zep temporal context graphs, and HiMem/H-MEM hierarchical memory.

The goal is not to store chat history as a flat vector pile. The goal is to build inspectable, numbered, time-aware memory that can support long-term user relationships and persona evolution.

## Principles

- Every memory must be traceable to raw evidence.
- New facts should not erase old facts. They should supersede them with time bounds.
- Core boundaries and relationship facts must outrank casual preferences.
- Summaries are useful, but they must link back to facts and episodes.
- The system should work locally with SQLite now and migrate to PostgreSQL / graph storage later.

## Layers

### L0 Raw Event

Raw messages or external events.

Examples:

- A user message.
- An assistant message.
- A future uploaded image description.
- A future profile edit event.

Table:

```text
memory_events
```

ID format:

```text
EVT-YYYYMMDD-000001
```

### L1 Episode

A compact segment of interaction derived from one or more events.

Examples:

- "The user introduced their preferred name and game preference."
- "The user corrected the AI's tone."

Table:

```text
memory_episodes
```

ID format:

```text
EP-YYYYMMDD-000001
```

### L2 Fact

A stable extracted fact.

Examples:

- "User wants to be called 月宫."
- "User likes 原神."

Table:

```text
memory_facts
```

ID format:

```text
FACT-USER-000001
FACT-PREF-000002
FACT-PLAN-000003
```

### L3 Relation / Boundary

Typed relationship or hard boundary. This is graph-ready.

Examples:

- User -> likes -> 原神
- User -> forbidden_address -> 小明
- Persona -> speaking_style -> short, calm

Table:

```text
memory_relations
```

ID format:

```text
REL-LIKE-000001
REL-BOUNDARY-000002
REL-PERSONA-000003
```

### L4 Persona Feedback

Feedback that should affect future persona versions, but should not automatically mutate the persona without review.

Examples:

- "Speak in shorter sentences."
- "Do not sound like a lecturer."
- "Be more proactive."

Stored primarily as facts and relations with type:

```text
persona_feedback
```

### L5 Summary / Profile

Stable compressed memory blocks used in every chat prompt.

Examples:

- User profile summary.
- Interaction style summary.
- Persona memory summary.
- Conversation summary.

Table:

```text
memory_summaries
```

ID format:

```text
SUM-USER-000001
SUM-PERSONA-000002
SUM-CONV-000003
```

### L6 Temporal Graph

The graph is not a separate table yet. It is represented by `memory_relations` with:

- subject
- predicate
- object
- valid_from
- valid_to
- supersedes_uid
- superseded_by_uid

This keeps the schema compatible with Graphiti/Zep-style temporal knowledge graphs.

## Required Fields

Every durable memory object should have:

```text
uid
layer
type
user_id
persona_id
conversation_id
source_message_id
text
importance
confidence
valid_from
valid_to
supersedes_uid
superseded_by_uid
created_at
updated_at
last_used_at
archived
```

Relations additionally require:

```text
subject
predicate
object
```

## Retrieval Priority

The prompt context should be assembled in this order:

1. Hard boundaries and forbidden address rules.
2. Current persona core prompt.
3. User profile summary and interaction style.
4. Relevant temporal relations.
5. Relevant facts.
6. Recent episodes.
7. Recent raw conversation turns.

## Current vs History Views

Chat prompts should use the current view by default.

Current view:

```text
archived = 0
valid_to IS NULL
```

History view:

```text
archived = 0
including superseded memories
```

Rules:

- Core chat should not receive superseded facts unless explicitly needed for temporal reasoning.
- Debugging and memory review screens should be able to show both current and historical memories.
- If a user asks "what did I used to ask you to call me?", retrieval may use history.
- If a user simply chats, only current facts enter the prompt.

## Program-Controlled Memory Visibility

The chat model should not decide what it remembers. The program controls memory visibility before the prompt is built.

Important distinction:

```text
Storage does not forget.
Retrieval can be humanized.
```

The backend memory system should preserve complete memory, evidence, history, and supersession chains. "Forgetting" means that some memories are not proactively sent to the chat model in the current prompt.

Memory behavior should feel human, but remain safe and consistent:

- Important simple facts must not disappear.
- Hard boundaries must not disappear.
- Current relationship state must not disappear.
- Low-importance casual details can fade from proactive context.
- Faded memories remain stored and can be recovered if relevant.

### Priority Levels

```text
critical  Always active. Names, forbidden names, safety boundaries.
high      Usually active. Relationship state, strong preferences, persona feedback.
normal    Active when relevant. Interests, plans, recurring topics.
low       Active only when recent or directly relevant. Casual events.
```

### Locked Memories

Locked memories cannot be decayed out of the current context automatically.

Examples:

```text
User wants to be called 小雪.
Do not call the user 小明.
User dislikes lecturing tone.
```

### Visibility Decay

`decay_score` is a retrieval visibility penalty, not deletion and not real forgetting.

The system may reduce active visibility for:

```text
low-importance events
one-off casual details
old plans with no recent mention
weak emotional observations
```

The system must not decay:

```text
identity
boundary
relationship
persona_feedback marked high or critical
```

### Persona-Dependent Forgetting Feel

Different personas may express memory differently, but the program decides visibility.

Unless explicitly specified otherwise, normal personas should have medium-high memory. They should reliably remember:

```text
names
address preferences
hard boundaries
important likes/dislikes
relationship state
recent important events
```

Examples:

```text
Careful persona: more likely to surface small details.
Casual persona: remembers core facts, lets small details fade.
Playful persona: may phrase recall lightly, but cannot forget hard boundaries.
Forgetful/dim persona: lower proactive recall, but backend memory remains complete.
```

This can later be controlled by persona memory parameters:

```text
memory_attentiveness: 0.0 ~ 1.0  default 0.72
detail_retention: 0.0 ~ 1.0      default 0.68
proactive_recall: 0.0 ~ 1.0       default 0.65
```

## Current Summary Implementation

Implemented:

- `priority` on facts and relations.
- `locked` on facts and relations.
- `decay_score` on facts and relations.
- `access_count` on facts and relations.
- `memory_profile_json` on personas.
- Normal personas default to medium-high memory.
- Forgetful/airheaded personas reduce proactive visibility only, not storage completeness.
- Careful personas increase proactive visibility.
- Critical identity and boundary facts are locked.
- Normal preferences are not locked.
- `memory_summaries` can be refreshed from current facts and relations.
- Chat context receives `Stable memory summary` before fragment-level memories.
- `apply_memory_decay` updates `decay_score` for old, unlocked, non-critical memories.
- `decay_score` lowers proactive retrieval visibility; it does not delete memory and does not mean the backend forgot.

Verified behavior:

```text
User: 我叫月宫
User: 叫我小雪
User: 我喜欢原神，不要叫我小明
```

Stable summary:

```text
用户当前希望被称为小雪。
不要称呼用户为：小明。
用户喜欢：原神。
```

The old address "月宫" remains in history, but does not enter the current summary.

Decay behavior:

```text
critical + locked identity/boundary -> decay_score remains 0
normal preference unused for a long time -> decay_score increases
```

## Conflict Handling

When new memory conflicts with old memory:

- Do not delete the old memory.
- Set the old memory's `valid_to`.
- Set `superseded_by_uid`.
- Set the new memory's `supersedes_uid`.
- Keep both linked to their raw event evidence.

Example:

```text
FACT-USER-000001: User wants to be called A. valid_to=2026-05-19
FACT-USER-000002: User wants to be called B. supersedes=FACT-USER-000001
```

## Supersession Policy

Not every new memory should replace an old memory. Replacement is only allowed when the new item belongs to a single-current-state category.

### Replaceable

These categories usually have one current value:

```text
identity.preferred_address
relationship.current_expectation
persona_feedback.speaking_style
persona_feedback.distance_preference
```

Example:

```text
Old: User wants to be called A.
New: User wants to be called B.
```

The old fact remains in history but is no longer current.

### Additive

These categories can accumulate:

```text
preference.likes
preference.dislikes
plan.goals
emotional_pattern
```

Example:

```text
User likes 原神.
User likes 洛圣都.
```

Both can be true.

### Boundary Rules

Boundaries are mostly additive, because multiple forbidden names or tones can coexist.

Replacement only happens when the predicate is the same and the system is confident the new statement updates the same slot.

Example:

```text
Old: Do not call the user 小明.
New: Actually 小明 is fine now.
```

This requires an explicit reversal. Casual mentions should not invalidate boundaries.

## Link Types

`memory_links` supports:

```text
derived_from
supersedes
superseded_by
conflicts_with
supports
```

The first implementation uses `derived_from`, `supersedes`, and `superseded_by`.

## Current Supersession Implementation

Implemented:

- `identity` facts can supersede older `identity` facts.
- `relationship` facts can supersede older `relationship` facts.
- `preferred_address` relations can supersede older `preferred_address` relations.
- `relationship_expectation` relations can supersede older `relationship_expectation` relations.
- `preference` memories are additive.
- `boundary` memories are additive by default.
- Negative address statements such as "不要叫我小明" are treated only as boundaries, not as preferred-address updates.

Example verified:

```text
User: 我叫月宫
User: 叫我小雪
```

Result:

```text
FACT-USER-* 月宫 -> valid_to set, superseded_by = 小雪 fact
FACT-USER-* 小雪 -> current
REL-IDENTITY-* 月宫 -> valid_to set, superseded_by = 小雪 relation
REL-IDENTITY-* 小雪 -> current
```

Example verified:

```text
User: 不要叫我小明
User: 不要叫我老王
```

Result:

```text
Both boundaries remain current.
```

## Current Implementation Step

The current implementation adds the tables and writes parallel layered memory while keeping the legacy `memories` table during migration.

Legacy table:

```text
memories
```

New tables:

```text
memory_events
memory_episodes
memory_facts
memory_relations
memory_summaries
memory_state
memory_links
```

Once the new path is stable, `memories` can become a compatibility view or be retired.

## Memory Reinforcement

To reduce model errors in long conversations, the system should not rely only on free-form memory text.

It reinforces memory into three prompt-friendly forms:

```text
summary     compressed stable prose
key_points  short bullet points
state       explicit state variables
```

State variables are stored in:

```text
memory_state
```

Examples:

```text
preferred_address = "小雪"
forbidden_addresses = ["小明", "老王"]
likes = ["原神"]
dislikes = []
interaction_style = ["不要说教", "少追问"]
relationship_state = "朋友"
```

The chat prompt should prefer state variables over ambiguous prose.

Current implementation:

```text
memory_state.preferred_address
memory_state.forbidden_addresses
memory_state.likes
memory_state.dislikes
memory_state.interaction_style
memory_state.relationship_state
```

The prompt now includes:

```text
Memory state variables:
- preferred_address: "小雪"
- forbidden_addresses: ["小明"]
- likes: ["原神"]
These variables are precise backend state. Follow them over vague memory text.
```

This is designed to reduce long-context mistakes by giving the chat model explicit state instead of asking it to infer from scattered memory fragments.

## Memory Review Console

Memory is managed automatically by the memory modules, not manually by the user.

The review console exists for transparency and correction:

```text
Automatic path:
chat -> extraction -> review/scoring -> layered memory -> summary/state -> chat context

Human/Admin path:
inspect -> lock/unlock -> adjust priority -> archive incorrect memory
```

The review console should show:

```text
state variables
stable summaries
current facts
current relations
historical superseded facts
evidence links
priority / locked / confidence / decay_score
```

Manual edits should be rare. The normal system path remains automatic.
