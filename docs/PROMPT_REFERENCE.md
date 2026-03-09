# Legba Prompt Reference

This document catalogs every prompt template used by the Legba agent, the phases in
which they appear, and how the `PromptAssembler` class composes them into complete
LLM conversation payloads.

Source files:

| File | Role |
|------|------|
| `src/legba/agent/prompt/templates.py` | All prompt template strings |
| `src/legba/agent/prompt/assembler.py` | `PromptAssembler` class -- builds message lists per phase |
| `src/legba/agent/tools/subagent.py` | Sub-agent system prompt and execution loop |
| `src/legba/agent/llm/client.py` | `LLMClient` with working memory, re-grounding injection, budget-exhausted handling, and sliding window |
| `src/legba/agent/llm/format.py` | `Message` dataclass, `to_chat_messages()`, `format_tool_result()`, `format_tool_definitions()` |
| `src/legba/agent/llm/tool_parser.py` | JSON tool call parser (primary: `{"actions": [...]}` wrapper, fallback: bare `{"tool": ...}`) |
| `src/legba/agent/llm/provider.py` | `VLLMProvider` — `/v1/chat/completions` with single user message (ernie pattern), temp 1.0, no max_tokens |

---

## 1. Prompt Template Inventory

| Template variable | Defined in | Phase(s) | Message role | Purpose |
|---|---|---|---|---|
| `SYSTEM_PROMPT` | `templates.py` | PLAN, REASON | `system` | Core identity (Legba), rules, cycle metadata, reasoning level header |
| `PLAN_PROMPT` | `templates.py` | PLAN | `user` | Asks the model to produce a structured cycle plan (GOAL / APPROACH / TOOLS / SUCCESS). Includes attempt tracking guidance (diminishing returns awareness after 3+ cycles) and a "Valid Goal Outcomes" section (confirming unavailability is a valid finding). (nonce removed) |
| `GOAL_CONTEXT_TEMPLATE` | `templates.py` | PLAN, REASON | `user` | Frames the seed goal as "Primary Mission (Strategic Direction — Not a Task to Complete)" and injects formatted active sub-goals |
| `MEMORY_CONTEXT_TEMPLATE` | `templates.py` | PLAN, REASON | `user` | Injects retrieved episodic memories and known facts |
| `INBOX_TEMPLATE` | `templates.py` | PLAN, REASON | `user` | Delivers human-operator messages with priority/response tags |
| `CYCLE_REQUEST` | `templates.py` | REASON | `user` | Final user message that kicks off the REASON+ACT loop with the cycle plan and working memory (nonce removed) |
| `REGROUND_PROMPT` | `templates.py` | REASON (mid-loop) | `user` | Periodic identity re-grounding injected every 8 tool steps by `LLMClient` |
| `BUDGET_EXHAUSTED_PROMPT` | `templates.py` | REASON (end) | `user` | Injected when step budget is exhausted; forces a wrap-up response |
| `TOOL_CALLING_INSTRUCTIONS` | `templates.py` | REASON, Sub-agent | `developer` | Instructions for JSON tool-call format: `{"actions": [{"tool": "name", "args": {...}}]}`. Single-object wrapper prevents multi-message output errors. |
| `MEMORY_MANAGEMENT_GUIDANCE` | `templates.py` | PLAN, REASON (appended to system) | `system` | Guidance on memory_store, memory_query, memory_promote, memory_supersede, note_to_self |
| `EFFICIENCY_GUIDANCE` | `templates.py` | PLAN, REASON (appended to system) | `system` | Guidance on incremental work, sub-agents, OpenSearch |
| `ANALYTICS_GUIDANCE` | `templates.py` | PLAN, REASON (appended to system) | `system` | Reference table for anomaly_detect, forecast, nlp_extract, graph_analyze, correlate |
| `ORCHESTRATION_GUIDANCE` | `templates.py` | PLAN, REASON (appended to system) | `system` | Guidance on Airflow workflow_define/trigger/status/list/pause |
| `BOOTSTRAP_PROMPT_ADDON` | `templates.py` | PLAN, REASON (early cycles only) | `system` | Extra orientation checklist for cycles 1 through `bootstrap_threshold` (default 5) |
| `REPORTING_REMINDER` | `templates.py` | REASON | embedded in `CYCLE_REQUEST` | Injected into `{reporting_reminder}` slot on reporting cycles (every N cycles) |
| `NO_REPORTING_REMINDER` | `templates.py` | (unused placeholder) | -- | Empty string; the non-reporting case |
| `REFLECT_PROMPT` | `templates.py` | REFLECT | `user` | Asks the model to produce a structured JSON reflection with cycle_summary, facts_learned, entities, etc. |
| `SUBAGENT_SYSTEM_PROMPT` | `subagent.py` | Sub-agent | `system` | Identity and rules for focused sub-agent instances |
| `MISSION_REVIEW_PROMPT` | `templates.py` | MISSION_REVIEW | `user` | Periodic strategic review of goal tree alignment with primary mission. Outputs JSON with goal_assessments, mission_alignment score, underserved_areas, strategic_recommendation. |
| `LIVENESS_PROMPT` | `templates.py` | PERSIST | `user` | Dedicated nonce challenge — LLM transforms a nonce to prove liveness (isolated from reasoning context) |

### Inline / hardcoded prompt strings

| Location | Role | Content summary |
|---|---|---|
| `assembler.py` `assemble_reflect_prompt()` | `system` (REFLECT) | `"Reasoning: high\n\nYou are Legba, evaluating your own completed cycle. Respond with a JSON object only.\n\nCycle: {cycle_number}\nMission: {seed_goal[:300]}"` |
| `assembler.py` `assemble_mission_review_prompt()` | `system` (MISSION_REVIEW) | `"Reasoning: high\n\nYou are Legba, conducting a periodic strategic review. Respond with a JSON object only.\n\nCycle: {cycle_number}\nPrimary Mission: {seed_goal[:300]}"` |
| `client.py` line 322-341 | `user` (condensed context) | `"## Previous tool interactions (condensed)"` -- dynamically built summary of older tool steps |

---

## 2. System Prompt Assembly (`_build_system_text`)

The private method `PromptAssembler._build_system_text(cycle_number, context_tokens)`
builds the full system message used in both the PLAN and REASON phases:

```
SYSTEM_PROMPT.format(cycle_number=..., context_tokens=...)
  + [if cycle_number <= bootstrap_threshold] BOOTSTRAP_PROMPT_ADDON.format(cycle_number=...)
  + MEMORY_MANAGEMENT_GUIDANCE
  + EFFICIENCY_GUIDANCE
  + ANALYTICS_GUIDANCE
  + ORCHESTRATION_GUIDANCE
  + SA_GUIDANCE
  + ENTITY_GUIDANCE
```

Dynamic placeholders in `SYSTEM_PROMPT`:
- `{cycle_number}` -- current cycle integer
- `{context_tokens}` -- approximate token count of the assembled prompt (or `"(planning)"` / `"(calculating)"` as placeholder)

The nonce is no longer injected into the system prompt. Liveness verification is now
handled by a dedicated `LIVENESS_PROMPT` in the PERSIST phase (see section 3e below).

The bootstrap addon is appended only for early cycles (default: cycles 1-5).

---

## 3. Phase Message Assembly

### 3a. MISSION_REVIEW Phase -- `assemble_mission_review_prompt()`

Fires every 15 cycles, between the ORIENT and PLAN phases. The model evaluates
whether the current goal tree is aligned with the primary mission and suggests
strategic adjustments.

| Order | Role | Content source | Dynamic injections |
|-------|------|----------------|--------------------|
| 1 | `system` | Inline string: `"Reasoning: high\n\nYou are Legba, conducting a periodic strategic review. Respond with a JSON object only.\n\nCycle: {cycle_number}\nPrimary Mission: {seed_goal[:300]}"` | `cycle_number`, `seed_goal` |
| 2 | `user` | `MISSION_REVIEW_PROMPT` | `{goal_tree}`, `{cycle_number}` |

Notes:
- Uses a minimal but identity-aware system prompt (same pattern as REFLECT). Not the full `_build_system_text`.
- Outputs JSON with `goal_assessments`, `mission_alignment` score, `underserved_areas`, and `strategic_recommendation`.

### 3b. PLAN Phase -- `assemble_plan_prompt()`

Called before the agent takes any actions. The model produces a structured plan.

| Order | Role | Content source | Dynamic injections |
|-------|------|----------------|--------------------|
| 1 | `system` | `_build_system_text()` | `cycle_number`, context_tokens=`"(planning)"` |
| 2 | `user` | `GOAL_CONTEXT_TEMPLATE` | `{seed_goal}`, `{active_goals}` (formatted by `_format_goals`) |
| 3 | `user` | `MEMORY_CONTEXT_TEMPLATE` | `{memories}`, `{facts}` (formatted by `_format_memories`; omitted if empty) |
| 4 | `user` | Graph inventory text | Entity completeness inventory (omitted if graph empty) |
| 5 | `user` | `INBOX_TEMPLATE` | `{messages}`, `{count}` (omitted if no inbox messages) |
| 6 | `user` | Queue summary text | NATS queue stats (omitted if no data messages) |
| 7 | `user` | Reflection forward text | Previous cycle's `self_assessment` and `next_cycle_suggestion` (omitted if none) |
| 8 | `user` | `PLAN_PROMPT` | (no dynamic injections -- nonce removed) |

Notes:
- No `developer` message (no tool definitions needed for planning).
- No truncation logic applied in this phase.
- Reflection forward data (message 7) provides cycle-to-cycle continuity of intent by injecting the previous cycle's self-assessment and suggested next action.

### 3c. REASON Phase -- `assemble_reason_prompt()`

Called to begin the REASON+ACT tool-calling loop. This is the heaviest prompt.

| Order | Role | Content source | Dynamic injections |
|-------|------|----------------|--------------------|
| 1 | `system` | `_build_system_text()` | `cycle_number`, `context_tokens` (updated after assembly with real count). Budget truncation note appended if context was trimmed. |
| 2 | `developer` | Tool definitions string + `TOOL_CALLING_INSTRUCTIONS` | `{tool_defs}` (passed to constructor as `tool_definitions`) |
| 3 | `user` | `GOAL_CONTEXT_TEMPLATE` | `{seed_goal}`, `{active_goals}` |
| 4 | `user` | `MEMORY_CONTEXT_TEMPLATE` | `{memories}`, `{facts}` (omitted if empty) |
| 5 | `user` | Graph inventory text | Entity completeness inventory (omitted if graph empty) |
| 6 | `user` | `INBOX_TEMPLATE` | `{messages}`, `{count}` (omitted if no inbox) |
| 7 | `user` | Queue summary text | NATS queue stats (omitted if no data) |
| 8 | `user` | Reflection forward text | Previous cycle's `self_assessment` and `next_cycle_suggestion` (omitted if none) |
| 9 | `user` | `CYCLE_REQUEST` | `{cycle_plan}`, `{working_memory_summary}`, `{reporting_reminder}` |

Dynamic content in `CYCLE_REQUEST`:
- `{cycle_plan}` -- the plan text from the PLAN phase (or fallback string)
- `{working_memory_summary}` -- output of `WorkingMemory.summary()`
- `{reporting_reminder}` -- either `REPORTING_REMINDER.format(...)` on reporting cycles or empty string

**Context budget enforcement** (when total tokens exceed `max_context_tokens`):
- Fixed messages (system, developer, inbox, queue, reflection forward, action request) are never truncated.
- Flexible messages (goals, memories) are truncated to fit.
- Memories are truncated first; goals get 40% of the remaining flexible budget.
- The system message is updated after assembly to show the real token count and a truncation note.

### 3d. REFLECT Phase -- `assemble_reflect_prompt()`

Called after the REASON+ACT loop completes. The model produces a JSON reflection.

| Order | Role | Content source | Dynamic injections |
|-------|------|----------------|--------------------|
| 1 | `system` | Inline string with identity and context: `"Reasoning: high\n\nYou are Legba, evaluating your own completed cycle. Respond with a JSON object only.\n\nCycle: {cycle_number}\nMission: {seed_goal[:300]}"` | `cycle_number`, `seed_goal` |
| 2 | `user` | `REFLECT_PROMPT` | `{cycle_plan}`, `{working_memory}`, `{results_summary}` |

Notes:
- Uses a minimal but identity-aware system prompt (not the full `_build_system_text`). Includes cycle number and mission so the model evaluates in context rather than blind.
- `results_summary` is truncated to at most half of `max_context_tokens` (or 50000 tokens).

### 3e. PERSIST Phase (Liveness) -- `assemble_liveness_prompt()`

Called during the PERSIST phase to verify the LLM is still in the loop. This is the
dedicated nonce challenge that replaced the previous approach of embedding the nonce in
SYSTEM_PROMPT, PLAN_PROMPT, and CYCLE_REQUEST.

| Order | Role | Content source | Dynamic injections |
|-------|------|----------------|--------------------|
| 1 | `system` | Minimal system string: `"Reasoning: high\n\nYou are a liveness verifier."` | (none) |
| 2 | `user` | `LIVENESS_PROMPT` | `{nonce}` -- the random challenge string the model must transform to prove liveness |

Notes:
- Completely isolated from the reasoning context -- lightweight, single-purpose LLM call.
- The supervisor issues the nonce, the agent calls `assemble_liveness_prompt()`, sends it to the LLM, and returns the transformed nonce in the heartbeat response.
- This separation ensures the nonce challenge does not consume reasoning context budget or interfere with the agent's planning/action loop.

---

## 4. Mid-Loop Injections (LLMClient)

During the REASON+ACT tool-calling loop in `LLMClient.reason_with_tools()`, additional
messages are injected dynamically:

### 4a. Re-grounding (every `REGROUND_INTERVAL` steps, default 8)

After every 8th tool step, a `user` message is appended containing:

```
REGROUND_PROMPT.format(working_memory_summary=working_memory.summary())
```

This prevents identity drift during long tool chains.

### 4b. Budget exhaustion (when `max_steps` is reached)

If the step budget is exhausted without the model producing a final response,
a `user` message is appended:

```
Step budget exhausted. Before finishing:
1. Call note_to_self with what you accomplished and what remains unfinished.
2. Call goal_update to record your progress percentage on the goal you advanced.
Then provide a brief final summary.
```

This is a static string (no format placeholders). The model is then called one
final time to force a wrap-up. The explicit instructions to call `note_to_self`
and `goal_update` ensure the agent saves its state before the cycle ends.

### 4c. Sliding window condensation

The `_build_conversation()` method manages context growth:

- The last `SLIDING_WINDOW_SIZE` (default 5) tool steps are kept in full.
- Older steps are condensed into a single `user` message with the header
  `"## Previous tool interactions (condensed)"` followed by one-line summaries:
  `- Step N: tool_name(args_preview) -> result_preview`
- Tool result previews are capped at `CONDENSED_RESULT_MAX_CHARS` (500 chars).

---

## 5. Sub-Agent Prompt Assembly

Sub-agents are spawned via `run_subagent()` in `subagent.py`. They get their own
fresh 128k context window.

| Order | Role | Content source | Dynamic injections |
|-------|------|----------------|--------------------|
| 1 | `system` | `SUBAGENT_SYSTEM_PROMPT` | (none -- static) |
| 2 | `developer` | Filtered tool definitions + `TOOL_CALLING_INSTRUCTIONS` | Only definitions for `allowed_tools` |
| 3 | `user` | Task + context string | `task` (what to do), `context` (from head agent) |

The sub-agent then enters its own REASON+ACT loop via `LLMClient.reason_with_tools()`
with the same re-grounding and budget-exhaustion mechanics as the head agent.

---

## 6. Working Memory

`WorkingMemory` (defined in `client.py`) is an in-cycle scratchpad:

- `add_tool_result(step, tool_name, args_summary, result_summary)` -- recorded after each tool call
- `add_note(note)` -- recorded from `note_to_self` tool calls
- `summary()` -- compact format used in `CYCLE_REQUEST` and `REGROUND_PROMPT`
- `full_text()` -- verbose format used in `REFLECT_PROMPT`

Working memory does NOT persist across cycles. Cross-cycle persistence is handled
by episodic memory (`memory_store`).

---

## 7. Key Constants

| Constant | File | Default | Purpose |
|---|---|---|---|
| `SLIDING_WINDOW_SIZE` | `client.py` | 8 | Number of recent tool steps kept in full |
| `REGROUND_INTERVAL` | `client.py` | 8 | Steps between re-grounding injections |
| `CONDENSED_RESULT_MAX_CHARS` | `client.py` | 2000 | Max chars per condensed tool result |
| `bootstrap_threshold` | `assembler.py` constructor | 5 | Cycles for which `BOOTSTRAP_PROMPT_ADDON` is included |
| `max_context_tokens` | `assembler.py` constructor | 120000 | Context budget (of 128k window) |
| `report_interval` | `assembler.py` constructor | 5 | Cycles between reporting reminders |
| `mission_review_interval` | `assembler.py` constructor | 15 | Cycles between MISSION_REVIEW phase invocations |
