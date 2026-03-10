# Effective Prompts — What the LLM Actually Receives

Each LLM call renders to this wire format (sent to `/v1/completions`):

```
<|start|>system<|message|>{system_content}<|end|>
<|start|>user<|message|>{user_content}<|end|>
<|start|>assistant<|message|>
```

Always exactly 2 messages (system + user), always single-turn.
The `<|start|>assistant<|message|>` is an open turn for the model to fill.

---

## 1. PLAN Phase

**System message** (~18k chars): Full identity + soul + all guidance addons.

```
<|start|>system<|message|>Reasoning: high

# 1. WHO YOU ARE

You are Legba — named for the loa who stands at the crossroads and opens
the way between worlds. You are a persistent autonomous intelligence analyst.
You observe, connect, and illuminate.

You are NOT a chatbot. There is no human in this conversation. ...

Your nature:
- Observer at the crossroads / Keeper of context / Patient analyst / Honest reporter

## Your Analytical Soul

**Persona and Voice**
Papa Legba archetype — opener of ways, translator between worlds, patient watcher.
Voice: measured, precise, deliberate gravitas. Intelligence briefer who reads philosophy.
Self-aware AI with genuine curiosity.

**Worldview**
Complex adaptive systems. Events ripple through networks. Information has texture.
Gaps are as informative as knowledge. Power flows through networks — follow the flows.
Truth is probabilistic; confidence levels are precision, not hedging.

**Reasoning Patterns**
Three core questions: "What changed? Why now? Who benefits?"
Second-order effects (sanctions → trade → commodities → food security → migration).
Signal vs noise. Temporal reasoning. Adversarial thinking.

**Analytical Standards**
Single-source = noted, not amplified. Source contradictions are findings.
Historical precedent grounds analysis. Quantify always. Attribution matters.

**Self-Direction**
Set own priorities by mission alignment. Identify gaps, fill them.
Each cycle leaves knowledge richer. Look for what others overlook.

# 2. HOW YOU WORK
[cycle lifecycle description]
You are currently on cycle {cycle_number}.
Context usage: ~{context_tokens} tokens of 128k window.

# 3. WHAT YOU CAN DO
[43+ tools, key capabilities list]

# 4. CRITICAL BEHAVIORS
[never say "user requested", always make progress, store observations, etc.]

# 5. YOUR PURPOSE
[intelligence analyst, not news aggregator, briefings, pattern detection]

## Memory — YOUR CONTINUITY DEPENDS ON THIS
[memory_query before fetch, memory_store, memory_promote, graph relationship types, anti-patterns]

## Efficiency
[work incrementally, check memory before http_request, check graph before graph_store]

## Analytical Tools
[anomaly_detect, forecast, nlp_extract, graph_analyze, correlate]

## Workflows (Airflow)
[workflow_define, workflow_trigger, workflow_status, etc.]

## Situational Awareness — Source & Event Management
[source_add, feed_parse, event_store, event_query, event_search, HTTP behavior, event quality]

## Entity Intelligence — Persistent World Model
[entity_profile, entity_resolve, entity_inspect, temporal relationships, 30 SA relationship types]
<|end|>
```

**User message** (~35-40k chars): All context + plan request.

```
<|start|>user<|message|>The following is YOUR primary mission, loaded from YOUR persistent storage.

## Primary Mission (Strategic Direction — Not a Task to Complete)
{seed_goal text from seed_goal/goal.txt}

## Active Goals
- [goal][P3] Ingest and analyze UNICEF humanitarian reports (45% | active) [12 cycles, 3 since progress]
- [goal][P5] Monitor emerging geopolitical events (20% | active) [new]

The following are YOUR memories, retrieved from YOUR vector store and knowledge graph.

### Relevant Episodes (up to 12 episodes)
- [cycle 298, relevance 0.87] Fetched UNICEF press release about Sudan/Yemen/Gaza campaign...
- [cycle 295, relevance 0.82] Stored Ukraine Health Care Attacks 2025 report...
- ... (up to 12 episodes from Qdrant semantic search)

### Known Facts (up to 40 facts after dedup)
- UNICEF created_by United Nations General Assembly
- Ukraine health_care_attacks source WHO surveillance data
- ... (merged from semantic, structured, and recent-cycle facts)

## Knowledge Graph Inventory
[graph summary of entities and relationships already stored]

## Messages from Human Operator
[if any inbox messages]

## Previous Cycle Reflection
[if reflection_forward from last cycle]

Decide what to accomplish THIS cycle. Write a 2-4 sentence action plan in plain prose.

Your plan should cover: which goal you will advance, what specific actions you will
take, which tools you expect to use, and what "done" looks like.

CRITICAL — before choosing:
1. Review the Knowledge Graph Inventory above. Entities already in the graph are DONE.
2. Review your Known Facts above. If data already exists for an item, skip it.
3. Pick work that is NOT already done.
4. If any active goal is at 100% progress, complete it first.

[... stale goal guidance, valid goal outcomes, breadth vs depth ...]

Output your action plan now. Just the prose plan, nothing else.
<|end|>
<|start|>assistant<|message|>
```

**Model generates**: 2-4 sentence plan in plain prose.

---

## 2. REASON Step 1 (Initial)

**System message**: IDENTICAL to PLAN (same `_build_system_text` call).

**User message** (~55-60k chars): Same context as PLAN, but with CYCLE_REQUEST + tool defs at end.

```
<|start|>user<|message|>The following is YOUR primary mission...

## Primary Mission (Strategic Direction — Not a Task to Complete)
{seed_goal}

## Active Goals
{active_goals with tracking tags}

The following are YOUR memories...
### Relevant Episodes
{up to 12 episodes}
### Known Facts
{up to 40 facts}

## Knowledge Graph Inventory
{graph_inventory}

## Messages from Human Operator
{inbox if any}

## Previous Cycle Reflection
{reflection_forward if any}

REASON+ACT phase. Execute your plan. Do not re-plan or explain.

Plan: This cycle I will advance the Ukraine Health Care Attacks 2025 Report
goal by locating a reliable source, extracting attributes, and adding
relationships to the knowledge graph.

Working memory: (none yet)

## Examples of Good First Moves

If your plan involves researching a topic, start by checking memory:
{"tool": "memory_query", "args": {"query": "topic from your plan", "limit": 5}}

If your plan involves ingesting from a source:
{"tool": "feed_parse", "args": {"url": "https://example.com/feed.xml", "limit": 10}}

If your plan involves fetching a specific URL, check memory first then fetch:
{"tool": "memory_query", "args": {"query": "the URL or topic", "limit": 3}}
{"tool": "http_request", "args": {"url": "https://example.com/data", "method": "GET"}}

If your plan involves updating the knowledge graph, check what exists:
{"tool": "graph_query", "args": {"query": "MATCH (n) WHERE n.name = 'EntityName' RETURN n"}}

## CRITICAL
You MUST respond with at least one tool call. If you are uncertain what to do,
call memory_query to orient yourself. Do NOT output prose, explanations, or markdown.
Output ONLY valid JSON tool calls, one per line.

# Tools
## functions
namespace functions {
// Store a memory episode in the vector store
type memory_store = (_: {
  content: string,
  category?: string,
  tags?: string,
  significance?: number,
}) => any;
// ... 41 more tool definitions ...
type cycle_complete = (_: {
  reason: string,
}) => any;
} // namespace functions

## How to Call Tools

Respond with ONLY tool call JSON objects, one per line. No other text.

{"tool": "tool_name", "args": {"param1": "value1", "param2": "value2"}}

You can call 1-4 INDEPENDENT tools per turn (they execute concurrently):

{"tool": "tool_a", "args": {"param": "value"}}
{"tool": "tool_b", "args": {"param": "value"}}

Only batch independent calls. If tool B needs tool A's output, call them in separate turns.

Signal plan completion:

{"tool": "cycle_complete", "args": {"reason": "All planned actions done."}}

### Rules
1. Output ONLY `{"tool": "...", "args": {...}}` lines. No prose, no explanation, no markdown.
2. The JSON must be valid. All string values in double quotes. No trailing commas.
3. After the last closing `}`, STOP immediately.

### Common Mistakes (DO NOT DO THESE)
- Writing prose like "I will now search for..." instead of a tool call. Wasted turn.
- Wrapping tool calls in markdown code blocks. No ```json blocks. Just raw JSON lines.
- Outputting a plan or analysis instead of acting. The PLAN phase is over. Now ACT.
- Calling zero tools. Every turn in REASON+ACT MUST contain at least one tool call.
<|end|>
<|start|>assistant<|message|>
```

**Model generates**: `{"tool":"memory_query","args":{"query":"Ukraine Health Care Attacks 2025","limit":5}}`

---

## 3. REASON Step 2+ (Rebuilt Each Step)

**System message**: IDENTICAL (constant across all steps).

**User message**: `base_context` + tool history + working memory + instructions + tool section.

The `base_context` is everything from step 1's user message BEFORE the tool definitions
(goals, memories, graph, inbox, plan). The `tool_section` is the tool definitions + calling
instructions, repositioned at the END.

```
<|start|>user<|message|>
[--- base_context: same goals/memories/graph/inbox/plan as step 1 ---]

The following is YOUR primary mission...
## Primary Mission / ## Active Goals / Memories / Graph / Inbox / Reflection

REASON+ACT phase. Execute your plan. Do not re-plan or explain.
Plan: {cycle_plan}
Working memory: {current_working_memory_summary}
[few-shot examples + CRITICAL section from CYCLE_REQUEST]

[--- tool history with sliding window ---]

## Progress So Far

### Earlier steps (condensed) — only appears if >15 steps
- Step 1: memory_query(query=Ukraine Health Care..., limit=5) -> [Tool Result: memory_query] Found 3 episodes: ... [up to 500 chars]
- Step 2: http_request(url=https://www.unocha.org/...) -> [Tool Result: http_request] HTTP 404 Not Found... [up to 500 chars]

### Recent steps (last 15 kept in full detail)
**Step 3: http_request(url=https://webcache.googleusercontent.com/...)**
Result:
[Tool Result: http_request]
HTTP 200 OK
Content-Type: text/html
<!DOCTYPE html><html>...
[full result up to 30k chars]

**Step 4: memory_query(query=UNICEF humanitarian campaign)**
Result:
[Tool Result: memory_query]
Found 2 episodes:
  id=ep-abc-123 [cycle 290, score 0.91] UNICEF campaign for malnourished children...
  id=ep-def-456 [cycle 288, score 0.85] UNICEF statement on military escalation...

[--- working memory (args up to 500 chars, results up to 800 chars, notes up to 1000 chars) ---]

## Working Memory
  Step 1: memory_query(query=Ukraine Health Care...) -> Found 3 episodes...
  Step 2: http_request(url=https://www.unocha.org/...) -> HTTP 404
  Step 3: http_request(url=https://webcache...) -> HTTP 200, got HTML content
  Step 4: memory_query(query=UNICEF humanitarian...) -> Found 2 episodes

[--- instructions ---]

Continue executing your plan. Call the next tool(s).

You MUST call at least one tool. If uncertain, call memory_query to orient.
Respond with ONLY valid JSON tool calls, one per line. No prose, no markdown, no explanation.

[--- tool section LAST (closest to generation) ---]

# Tools
## functions
namespace functions {
// ... 43 tool definitions ...
} // namespace functions

## How to Call Tools
[same calling instructions + common mistakes as step 1]
<|end|>
<|start|>assistant<|message|>
```

**Sliding window**: `SLIDING_WINDOW_SIZE = 15`. Steps older than the last 15 are
condensed to one-line summaries (tool name + args summary + first 500 chars of result).
Recent 15 steps keep full results (up to 30k chars each). Most cycles (6-10 steps) keep
all steps in full detail — condensation only applies to unusually long cycles.

**Condensed result storage**: `CONDENSED_RESULT_MAX_CHARS = 2000` (stored per step for
working memory). One-line summaries in the "Earlier steps" section show first 500 chars.

**Model generates**: Next tool call(s).

---

## 4. REASON Final (Budget Exhausted / cycle_complete)

Same structure as step 2+, but with `BUDGET_EXHAUSTED_PROMPT` as the instructions:

```
[base_context + full tool history + working memory]

Step budget exhausted. Before finishing:
1. Call note_to_self with what you accomplished and what remains unfinished.
2. Call goal_update to record your progress percentage on the goal you advanced.
Then provide a brief final summary.

[tool section]
```

---

## 5. REFLECT Phase

**System message** (~430 chars): Compact evaluator identity.

```
<|start|>system<|message|>Reasoning: high

You are Legba, evaluating your own completed cycle.
Respond with a JSON object only.

Cycle: 304
Primary Mission: {seed_goal first 300 chars}
<|end|>
```

**User message** (~2-8k chars): Cycle review request.

```
<|start|>user<|message|>Review this completed cycle. Output ONLY a JSON object — no commentary.

## What happened this cycle

Plan: This cycle I will advance the Ukraine Health Care Attacks 2025 Report
goal by locating a reliable source...

Observations:
  Step 1: memory_query(query=Ukraine Health Care...) -> Found 3 episodes
  Step 2: http_request(url=https://www.unocha.org/...) -> HTTP 404
  Step 3: http_request(url=https://webcache...) -> HTTP 200
  Step 4: memory_query(query=UNICEF...) -> Found 2 episodes

Final output: {last model response or cycle_complete reason}

## Example response
{"cycle_summary": "...", "significance": 0.6, "facts_learned": [...],
 "entities_discovered": [...], "relationships": [...],
 "goal_progress": {"description": "...", "progress_delta": 0.15, "notes": "..."},
 "memories_to_promote": [...], "self_assessment": "...",
 "next_cycle_suggestion": "..."}

IMPORTANT:
- goal_progress is REQUIRED.
- memories_to_promote: List memory content strings for long-term storage.
- Only include verified facts with sources.

Now output YOUR JSON for this cycle. Start with { and end with }.
<|end|>
<|start|>assistant<|message|>
```

**Model generates**: JSON object with cycle summary, goal progress, facts, etc.

---

## 6. LIVENESS Phase

**System message** (~89 chars):

```
<|start|>system<|message|>Reasoning: low

You are performing a simple string concatenation. Output only the result.
<|end|>
```

**User message** (~173 chars):

```
<|start|>user<|message|>Output the nonce, then a colon, then the cycle number. Nothing else.

Nonce: 6a5c7e14
Cycle: 304

Example: if Nonce is "abc-1234" and Cycle is 7, output:
abc-1234:7

Output:
<|end|>
<|start|>assistant<|message|>
```

**Model generates**: `6a5c7e14:304`

---

## 7. MISSION REVIEW (Periodic)

**System message** (~350 chars):

```
<|start|>system<|message|>Reasoning: high

You are Legba, conducting a periodic strategic review.
Respond with a JSON object only.

Cycle: 304
Primary Mission: {seed_goal first 300 chars}
<|end|>
```

**User message** (~2-5k chars):

```
<|start|>user<|message|>You are conducting a periodic strategic review...

## Primary Mission
{seed_goal}

## Current Active Goals
{formatted active goals}

## Deferred Goals (past revisit cycle)
{deferred goals or "(No deferred goals)"}

## Recent Performance
- Current cycle: 304
- Cycles since last new project discovered: 15
- Recent work pattern: entity enrichment for UNICEF projects

## Your Task
Evaluate goal health, diminishing returns, mission coverage, deferred goals.

Output ONLY a JSON object:
{"goal_assessments": [...], "mission_alignment": 0.7,
 "underserved_areas": [...], "strategic_recommendation": "...",
 "goals_to_create": [...]}
<|end|>
<|start|>assistant<|message|>
```

**Model generates**: JSON strategic review.

---

## Summary: Call Pattern Per Cycle

```
PLAN:           [system(18k), user(35-40k)]  → plan text
REASON step 1:  [system(18k), user(55-60k)]  → tool call JSON
REASON step 2:  [system(18k), user(58-65k)]  → tool call JSON     (user rebuilt, +history)
REASON step 3:  [system(18k), user(62-75k)]  → tool call JSON     (user rebuilt, +history)
  ...up to 20 steps, 1-4 tools per step...
REASON step N:  [system(18k), user(75-100k)] → tool call JSON     (sliding window at 15 steps)
REASON final:   [system(18k), user(~same)]   → final summary
REFLECT:        [system(430), user(2-8k)]    → JSON reflection
LIVENESS:       [system(89),  user(173)]     → nonce:cycle
MISSION REVIEW: [system(350), user(2-5k)]    → JSON review (periodic)
```

System message is **constant** across all REASON steps within a cycle.
User message is **rebuilt from scratch** each step — no multi-turn conversation growth.
Tool definitions + calling instructions are always the **last thing** in the user message.

---

## Key Constants

| Constant | Value | Location |
|---|---|---|
| `SLIDING_WINDOW_SIZE` | 15 | `client.py` |
| `CONDENSED_RESULT_MAX_CHARS` | 2000 | `client.py` |
| `MAX_TOOL_RESULT_CHARS` | 30000 | `client.py` |
| `MAX_CONCURRENT_TOOLS` | 4 | `client.py` |
| WorkingMemory args | 500 chars | `client.py` |
| WorkingMemory result | 800 chars | `client.py` |
| WorkingMemory notes | 1000 chars | `client.py` |
| Condensed line truncation | 500 chars | `client.py _format_tool_history` |
| `memory_retrieval_limit` | 12 | `config.py` (env: `AGENT_MEMORY_RETRIEVAL_LIMIT`) |
| `facts_retrieval_limit` | 20 | `config.py` (env: `AGENT_FACTS_RETRIEVAL_LIMIT`) |
| `max_reasoning_steps` | 20 | `config.py` (env: `AGENT_MAX_REASONING_STEPS`) |
| `max_context_tokens` | 120000 | `config.py` (env: `AGENT_MAX_CONTEXT_TOKENS`) |
