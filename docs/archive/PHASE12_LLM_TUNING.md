# Legba — Phase 12: LLM Interaction Tuning & Cycle Flow Improvements

*Post-deployment analysis and remediation. Captures findings from cycles 1-48, root causes of poor agent behavior, and the implementation plan to fix them.*

*Created: 2026-02-19*

---

## Background

After initial deployment (2026-02-18), the agent ran ~48 cycles with the autonomous AI research analyst seed goal. Multiple issues were observed:

- Agent burning step budgets without meaningful progress
- Identity drift to chatbot behavior mid-cycle ("Could you please provide the task?")
- Tool parser false positives (fixed in session 2, fix #8)
- Shallow reasoning, poor planning, aimless exploration
- Prompting described as "lacking" — the platform infrastructure is solid (all 45 enhancement tasks done, 240 tests passing) but the LLM interaction layer has compounding problems

A full review of the codebase, Harmony format specification, and gpt-oss model documentation revealed several critical configuration and architectural issues.

---

## Key Findings

### Finding 1: InnoGPT-1 = gpt-oss (120B MoE)

The Harmony format is OpenAI's official prompt format for their open-weight **gpt-oss** models. The default configuration uses a hosted instance serving the model under the name "InnoGPT-1". This is a 117B parameter Mixture-of-Experts model (5.1B active parameters) with 128k context window.

**Implication:** All gpt-oss documentation, best practices, and known quirks apply directly.

### Finding 2: Reasoning effort not configured (CRITICAL)

gpt-oss models support three reasoning effort levels, configured in the system message:

| Level | Behavior |
|-------|----------|
| `low` | Minimal reasoning, fastest responses |
| `medium` | **Default if not specified — this is what we were running** |
| `high` | Deep, thorough reasoning, highest latency |

The system message format should include:
```
Reasoning: high
```

**Impact:** The agent was performing complex multi-step autonomous reasoning at medium effort. This directly explains shallow behavior, poor planning, and identity drift — the model wasn't engaging its deeper reasoning capabilities.

### Finding 3: max_tokens = 4096 severely limiting output (CRITICAL)

`LLMConfig.max_tokens` defaults to 4096 and is set via `LLM_MAX_TOKENS` env var (not overridden in `.env`). Every completion call — every single ReAct step — has a 4096 token generation ceiling.

The model's internal reasoning happens in the `analysis` channel **within this budget**. With 4096 tokens total, the model gets ~2-3k tokens to think and then must produce a tool call in the remaining ~1-2k. For a model that "uses up quite a bit on internal reasoning" this is severely constrained.

**Impact:** Truncated reasoning, cramped tool calls, inability to think through complex decisions. Should be 16384+.

### Finding 4: Harmony channel protocol not declared in system message

The proper gpt-oss system message should include:
```
# Valid channels: analysis, commentary, final. Channel must be included for every message.
Calls to these tools must go to the commentary channel: 'functions'.
```

Our system message has none of this. The model infers channel behavior from the prompt continuation format rather than explicit declaration.

**Impact:** Model uncertainty about channel usage. May contribute to inconsistent tool call formatting and the literal-text-instead-of-special-tokens behavior.

### Finding 5: Temperature/Top-P suboptimal

OpenAI recommends for gpt-oss:
- Temperature: **1.0** (we had 0.7)
- Top-P: **1.0** (we had 0.9)

Lower values constrain sampling diversity. For an autonomous agent that needs creative reasoning and varied approaches, the defaults are better.

### Finding 6: Context accumulation in ReAct loop (architectural)

In `client.py:reason_with_tools()`, each step sends the FULL accumulated conversation:

```
Step 1:  [system + dev + user]                           → model output → tool call
Step 2:  [system + dev + user + step1_full + result1]    → model output → tool call
Step 3:  [ALL above + step2_full + result2]              → model output → tool call
...
Step 30: [EVERYTHING from all 29 prior steps]            → model output
```

Every prior step's analysis reasoning, tool call, and tool result stays in the conversation. By step 20: ~60-80k tokens of accumulated input. No summarization, no pruning.

Combined with max_tokens=4096 output limit: the model spends most of its 128k context reading its own history, then gets only 4096 tokens to think and respond. This is the structural root cause of degraded behavior in long cycles.

### Finding 7: No explicit planning step

The cycle goes directly from ORIENT (gather context) into REASON+ACT (tool call loop). There is no step where the model explicitly decides "what should I do this cycle?" before starting to act. This contributes to aimless tool calls and drift.

### Finding 8: Reflection underutilized

The reflect phase receives:
- `actions_summary = "Cycle N: X actions taken"` (almost no information)
- `results_summary = self._final_response[:2000]` (truncated final response)

No structured extraction of facts, entities, or self-assessment. The LLM is asked to evaluate but the results aren't parsed or stored meaningfully.

### Finding 9: ORIENT query uses static seed goal

`cycle.py:221` — `query_text = self.state.seed_goal[:500]`. The embedding query for memory retrieval is always the first 500 chars of the seed goal. After the first few cycles, the agent's *current focus* (active sub-goal) is much more relevant for memory retrieval.

### Finding 10: Bootstrap prompt lacks concrete guidance

The bootstrap addon says "Don't rush to act. Observe, orient, and plan first" but gives no specific instructions on which tools to use for orientation or what exploring the environment means in practice.

---

## Implementation Plan

### Phase A: Config Fixes (immediate, high-impact, low-effort)

Quick wins that address the most critical issues. Pure configuration and template changes.

| # | Task | Files | Status |
|---|------|-------|--------|
| A.1 | Add `Reasoning: high` to system message | `prompt/templates.py` | DONE |
| A.2 | Add Harmony channel/routing protocol to system message | `prompt/templates.py` | DONE |
| A.3 | Increase `max_tokens` default to 16384 | `shared/config.py`, `.env` | DONE |
| A.4 | Set Temperature=1.0, Top_P=1.0 | `.env` | DONE |
| A.5 | Increase `max_context_tokens` to 120000 | `shared/config.py` | DONE |

### Phase B: Context Management in ReAct Loop (architectural)

The biggest structural change. Manage context growth during multi-step tool chains.

| # | Task | Files | Status |
|---|------|-------|--------|
| B.1 | Implement sliding window: keep last N steps in full, condense older steps (strip analysis channel, summarize tool call + result) | `llm/client.py` | DONE |
| B.2 | Add working memory / cycle scratchpad: running summary of cycle progress, observations, decisions — maintained in-process, injected into context | `agent/cycle.py`, `llm/client.py` | DONE |
| B.3 | Add `note_to_self` tool: model can explicitly record observations to carry forward within the cycle | `cycle.py` | DONE |
| B.4 | Working memory checkpoint injection: every ~8 steps, inject condensed scratchpad into conversation replacing older detail | `llm/client.py` | DONE |

### Phase C: Cycle Flow Improvements

Structural improvements to the OODA cycle itself.

| # | Task | Files | Status |
|---|------|-------|--------|
| C.1 | Add PLAN step between ORIENT and REASON: model decides what to do this cycle before starting tool calls, output stored as cycle plan | `cycle.py`, `prompt/templates.py`, `prompt/assembler.py` | DONE |
| C.2 | Improve ORIENT query: use active goal focus + seed goal for memory retrieval embedding, not just static seed goal text | `cycle.py` | DONE |
| C.3 | Improve REFLECT: parse structured output (facts, entities, learnings), store them via memory manager, feed scratchpad content as input | `cycle.py`, `prompt/templates.py` | DONE |
| C.4 | Add reporting cadence: update seed goal with periodic reporting requirement, add template support for status reports every N cycles | `seed_goal/goal.txt`, `prompt/templates.py`, `cycle.py` | DONE |

### Phase D: Prompt Rewrites

Comprehensive prompt improvements building on the config and architectural changes.

| # | Task | Files | Status |
|---|------|-------|--------|
| D.1 | Rewrite system prompt: Harmony protocol header, reasoning level, identity, behavioral rules — tighter structure, less noise | `prompt/templates.py` | DONE |
| D.2 | Rewrite bootstrap prompt: concrete guidance on which tools to use, what to explore first, specific first-cycle checklist | `prompt/templates.py` | DONE |
| D.3 | Improve sub-agent prompt: goal context, output format expectations, connection to parent agent's objectives | `tools/subagent.py` | DONE |
| D.4 | Align tool calling instructions with model's actual behavior (literal text format + Harmony token format, examples of both) | `prompt/templates.py` | DONE |
| D.5 | Rewrite cycle request / re-grounding prompts: more directive, include working memory summary | `prompt/templates.py`, `llm/client.py` | DONE |
| D.6 | Rewrite reflect prompt: structured output format (JSON), explicit fields for facts/entities/learnings/self-assessment | `prompt/templates.py` | DONE |
| D.7 | Add PLAN prompt template: structured cycle planning with goal selection, approach, expected tools, success criteria | `prompt/templates.py` | DONE |

### Phase E: Data Reset & Deploy

Clean slate with updated code.

| # | Task | Status |
|---|------|--------|
| E.1 | Delete all Docker volumes (`docker compose down -v`) | DONE |
| E.2 | Remove stale agent container if present | DONE |
| E.3 | Rebuild all Docker images with updated code | DONE |
| E.4 | Verify all services healthy (Redis, Postgres, Qdrant, NATS, OpenSearch — all healthy) | DONE |
| E.5 | Run test suite to confirm no regressions — 240/240 passed | DONE |
| E.6 | Update DEPLOYMENT.md with new config, fresh restart notes | DONE |

### Phase F: Nonce Refactor & Prompt-Ending Fix

Post-deploy fixes addressing wasted reasoning tokens and the actions=0 issue.

#### F.1 — Nonce Refactor

**Problem:** The challenge nonce was injected into every main prompt (SYSTEM_PROMPT, PLAN_PROMPT, CYCLE_REQUEST). The model spent significant CoT (chain-of-thought) analyzing the nonce string instead of working on the actual task — wasting reasoning tokens every single step.

**Solution:** Dedicated liveness check in the PERSIST phase.

- Supervisor generates an 8-char hex nonce (`uuid4().hex[:8]`) — short nonces avoid LLM character-drop errors that occurred with full UUIDs
- Nonce stripped from all main prompts (SYSTEM_PROMPT, PLAN_PROMPT, CYCLE_REQUEST)
- In PERSIST phase, a lightweight LLM call (`Reasoning: low`) concatenates the nonce with the cycle number via the new `LIVENESS_PROMPT` template
- Supervisor validates the transformed nonce using simple `nonce:cycle_number` concatenation (see `HeartbeatManager.compute_expected_nonce()`)

**Files changed:** `supervisor/heartbeat.py`, `agent/cycle.py`, `agent/prompt/templates.py`, `agent/prompt/assembler.py`

Note: The Challenge schema (`shared/schemas/cycle.py`) no longer contains an `insert_position` field — the nonce transformation is simple concatenation, not split-and-reassemble.

**Status:** DONE, tests 188/188 passing.

#### F.2 — Prompt-Ending Fix (actions=0 resolution)

**Problem:** The model was meta-reasoning about what tools to call instead of actually calling them, producing cycles with zero actions.

**Solution:** `CYCLE_REQUEST` and `REGROUND_PROMPT` now end with `to=functions.` — this primes the model to complete a tool call immediately rather than generating more commentary.

- `CYCLE_REQUEST` ends with: `Do NOT repeat the plan. Do NOT explain what you will do. Just call the first tool now.\n\nto=functions.`
- `REGROUND_PROMPT` ends with: `Continue executing. Call the next tool now.\n\nto=functions.`

This resolved the actions=0 issue where the model was meta-reasoning instead of acting.

**Files changed:** `agent/prompt/templates.py`

**Status:** DONE.

| # | Task | Files | Status |
|---|------|-------|--------|
| F.1 | Nonce refactor — move liveness check to PERSIST phase with dedicated lightweight LLM call | `heartbeat.py`, `cycle.py`, `templates.py`, `assembler.py` | DONE |
| F.2 | Prompt-ending fix — end CYCLE_REQUEST and REGROUND_PROMPT with `to=functions.` to prime tool calls | `templates.py` | DONE |

### Phase G: Tool Parser Improvements & Logging Cleanup

Post-deploy hardening of the tool argument parser and logging pipeline.

#### G.1 — `ast.literal_eval` fallback for Python dict format

**Problem:** The LLM sometimes emits tool arguments as Python dicts with single quotes (e.g., `{'key': 'value'}`) instead of valid JSON. The JSON parser rejects these, causing the argument to fall through to the `_raw` fallback and the tool call to fail.

**Solution:** `_parse_arguments()` in `tool_parser.py` now tries `ast.literal_eval()` as a second pass after `json.loads()` fails. If the result is a `dict`, it is accepted. This handles Python-style single-quoted dicts transparently.

#### G.2 — Balanced brace extraction

**Problem:** The LLM sometimes appends trailing text after the JSON object (e.g., `{"command": "ls"} Let me check...`). `json.loads()` rejects these because they are not valid JSON.

**Solution:** `_extract_balanced_braces()` in `tool_parser.py` uses brace-depth counting to extract the first balanced `{...}` substring before attempting to parse. This strips trailing junk so that `json.loads()` and `ast.literal_eval()` both see only the JSON/dict body.

#### G.3 — Duplicate logging fix

**Problem:** Tool calls were logged redundantly — once in `client.py` (pre-execution) and once in `executor.py` (post-execution), producing duplicate entries in cycle logs.

**Solution:** Removed the redundant `log_tool_call` from `client.py`. Tool calls are now logged once, in `executor.py` after execution.

| # | Task | Files | Status |
|---|------|-------|--------|
| G.1 | `ast.literal_eval` fallback for Python dict format | `tool_parser.py` | DONE |
| G.2 | Balanced brace extraction for trailing junk | `tool_parser.py` | DONE |
| G.3 | Remove duplicate `log_tool_call` from `client.py` | `client.py` | DONE |

### Phase H: Context Safety & Tool Init Fixes

Post-deploy fixes addressing oversized tool results, NATS initialization ordering, tool handler guards, ping timeout, and clean early exit from the tool loop. Applied after observing cycles 9-13.

#### H.1 — Tool Result Truncation

**Problem:** Agent fetched a GitHub recursive tree endpoint that returned 1.86M characters. This was injected verbatim into the conversation context, blowing past the 128k token window and causing a 400 Bad Request from the LLM API.

**Solution:** Added `MAX_TOOL_RESULT_CHARS = 30000` in `client.py`. Tool results exceeding this limit are truncated with a marker indicating the original size and the truncation point. Prevents any single tool result from consuming an outsized share of the context window.

**Files changed:** `llm/client.py`

**Status:** DONE.

#### H.2 — NATS/Service Init Ordering Fix

**Problem:** `_register_builtin_tools()` was called before NATS, OpenSearch, and Airflow clients were connected in `cycle.py:_wake()`. Tool handler closures captured `None` references for these services. Any tool call involving NATS would fail with `AttributeError: 'NoneType' object has no attribute ...`.

**Solution:** Moved service connection calls (NATS, OpenSearch, Airflow) before `_register_builtin_tools()` in `cycle.py:_wake()`, so tool handler closures capture live client references.

**Files changed:** `agent/cycle.py`

**Status:** DONE.

#### H.3 — NATS Tool Handler Guards

**Problem:** Even after fixing init ordering, NATS could become unavailable mid-cycle (network issues, broker restart). Tool handlers would crash with unhandled exceptions.

**Solution:** All 5 NATS tool handlers now check `nats.available` before calling methods. If NATS is unavailable, handlers return a descriptive error message instead of crashing.

**Files changed:** `agent/cycle.py` (tool handler definitions)

**Status:** DONE.

#### H.4 — PING_WAIT_SECONDS Increase (60 to 150)

**Problem:** Observed LLM inference latency up to 116 seconds on a single reasoning step. The supervisor's `PING_WAIT_SECONDS` was set to 60s, meaning the agent container's health ping would time out during normal (but slow) LLM calls, risking false-positive heartbeat failures.

**Solution:** Increased `PING_WAIT_SECONDS` from 60 to 150 seconds to accommodate worst-case single-step inference latency with margin.

**Files changed:** Configuration / `.env`

**Status:** DONE.

#### H.5 — `cycle_complete` Pseudo-Tool

**Problem:** When the agent finished its plan early (e.g., after 4 actions in a 30-step budget), it had no way to signal completion. It would either generate filler actions or the remaining step budget was wasted on unnecessary LLM calls.

**Solution:** Registered `cycle_complete` as a pseudo-tool in `cycle.py`. The agent calls `to=functions.cycle_complete json{"reason": "..."}` when it has completed its plan. The call is intercepted in `client.py` before execution (never hits the tool executor). The reason is logged and the tool loop exits cleanly. Prevents wasted steps and reduces cycle cost/duration.

**Files changed:** `agent/cycle.py`, `llm/client.py`

**Status:** DONE.

| # | Task | Files | Status |
|---|------|-------|--------|
| H.1 | Tool result truncation (`MAX_TOOL_RESULT_CHARS = 30000`) | `client.py` | DONE |
| H.2 | NATS/service init ordering fix — move connections before tool registration | `cycle.py` | DONE |
| H.3 | NATS tool handler availability guards (all 5 handlers) | `cycle.py` | DONE |
| H.4 | `PING_WAIT_SECONDS` increase (60 to 150) | `.env` | DONE |
| H.5 | `cycle_complete` pseudo-tool for clean early exit | `cycle.py`, `client.py` | DONE |

---

## Phase I: Memory Effectiveness & Prompt Guidance (2026-02-19)

**Problem:** Agent repeats work across cycles — re-fetches the same URLs, re-creates duplicate graph entities, never updates goal progress, never promotes memories to long-term. Root cause: retrieval limits too low (5 episodes out of 28 stored), prompts don't push memory-first behavior, reflection data (goal_progress) is ignored by persist phase.

| Task | Description | Files | Status |
|------|-------------|-------|--------|
| I.1 | Increase retrieval limits (5→20 episodes, 10→20 facts) | `.env` | DONE |
| I.2 | Rewrite MEMORY_MANAGEMENT_GUIDANCE — anti-patterns, memory-first behavior | `templates.py` | DONE |
| I.3 | Rewrite EFFICIENCY_GUIDANCE — mandatory memory_query before http_request, graph_query before graph_store | `templates.py` | DONE |
| I.4 | Update REFLECT_PROMPT — require goal_progress, add memories_to_promote | `templates.py` | DONE |
| I.5 | Auto-update goal progress from reflection data in persist phase | `cycle.py` | DONE |

---

## Phase J: Multi-Tool Calls (2026-02-19)

**Problem:** Each REASON+ACT step is a single tool call. With ~10k prompt tokens per step and 15-50s inference time, doing `http_request` + `memory_query` takes 2 full LLM round-trips. The model (gpt-oss 120b) supports parallel tool calls natively. The parser already has `parse_tool_calls_from_text()` (plural) but it's unused.

**Observed cost:** Cycle 16 used 245k prompt tokens across 21 steps for 20 tool calls. With multi-tool (2-3 calls per step), this could be ~10 steps and ~120k prompt tokens — nearly half.

**Implementation plan:**

| Task | Description | Files | Status |
|------|-------------|-------|--------|
| J.1 | Update `reason_with_tools()` to use `parse_tool_calls_from_text()` for multi-call extraction | `client.py` | DONE |
| J.2 | Execute multiple tool calls concurrently with `asyncio.gather()`, cap at `MAX_CONCURRENT_TOOLS=4` | `client.py` | DONE |
| J.3 | Feed all results back as separate `functions.TOOL_NAME` messages per call | `client.py` | DONE |
| J.4 | Update TOOL_CALLING_INSTRUCTIONS — allow 1-4 `to=functions.` per turn, with batching guidance | `templates.py` | DONE |
| J.5 | Update working memory to record each call in multi-call steps separately | `client.py` | DONE |
| J.6 | Fix `parse_tool_calls_from_text()` split regex to handle bare `to=functions.` format (no `assistant` prefix) | `tool_parser.py` | DONE |

**Key design decisions:**
- Cap at 3-4 concurrent tool calls per step (prevent runaway)
- Each tool result gets its own `functions.TOOL_NAME` message back
- Working memory records each call in the group separately
- Sliding window counts a multi-call step as one step (for condensation)
- If any tool errors, the error is reported alongside successful results

---

## Phase K: Feedback-Driven Enhancements (2026-02-23)

**Source:** Post-deployment prompt audit (`feedback.md`) after ~24 cycles on the new codebase. Identified that while infrastructure works well, several high-value signals were being discarded.

### K.1 — Feed Reflection Forward (Cycle-to-Cycle Continuity)

**Problem:** The REFLECT phase produces `self_assessment` and `next_cycle_suggestion` every cycle, but they were logged and discarded. Each cycle re-derived intent from scratch by looking at goals and memories. The agent's own explicit statement of "what I should do next" was thrown away.

**Solution:** Store both fields in Redis registers at end of `_persist()`, read them back in `_orient()`, and inject them as a `user` message in both PLAN and REASON phases.

- `_persist()`: After the auto-promote block, stores `reflection_forward` JSON in `legba:reflection_forward` via `registers.set_json()` (each field capped at 500 chars)
- `_orient()`: After graph inventory retrieval, reads the register back and formats as `"## Previous Cycle Reflection\n**Self-assessment:** ...\n**Suggested next action:** ..."`
- `assemble_plan_prompt()`: Injected as message 7 (before plan request), if non-empty
- `assemble_reason_prompt()`: Injected as message 8 (before action request), if non-empty; tokens counted as fixed (never truncated)

**Files changed:** `agent/cycle.py`, `agent/prompt/assembler.py`

**Status:** DONE.

### K.2 — Enrich REFLECT System Prompt

**Problem:** The REFLECT system prompt was `"You are evaluating a completed agent cycle. Respond with a JSON object only."` — no identity, no cycle number, no mission. The model evaluated blind, with no context for assessing significance or relevance.

**Solution:** Added Legba identity, cycle number, and mission (first 300 chars of seed goal) to the reflect system message:

```
Reasoning: high

You are Legba, evaluating your own completed cycle. Respond with a JSON object only.

Cycle: {cycle_number}
Mission: {seed_goal[:300]}
```

Also updated `assemble_reflect_prompt()` signature to accept `seed_goal` and `cycle_number` parameters, passed from `_reflect()`.

**Files changed:** `agent/prompt/assembler.py`, `agent/cycle.py`

**Status:** DONE.

### K.3 — Purpose/Identity Rewrite (Section 5)

**Problem:** Section 5 "YOUR SELF" in the system prompt was purely mechanical — only self-modification instructions. No framing of purpose, quality, or relationship to the work.

**Solution:** Replaced with "YOUR PURPOSE" — frames the knowledge graph as a map not a checklist, emphasizes quality over coverage, encourages investigating anomalies, retains self-modification capability:

```
# 5. YOUR PURPOSE

You are building a structured understanding of a domain that is actively evolving.
Your knowledge graph is not a checklist — it is a map. Each entity you research,
each relationship you discover, each pattern you identify adds resolution to that map.

Quality matters more than coverage. A shallow catalog of 50 items is less valuable
than a deep understanding of 15 with clear architectural comparisons, identified
patterns, and honest assessments of limitations. When you find something surprising
or contradictory, investigate it — anomalies are often the most valuable findings.

You can read and modify your own code at /agent/src/legba/agent/prompt/templates.py.
You can add new tools, modify existing ones, or change how your cycle works.
Self-modification is expected — if you find a better way to pursue your mission,
implement it.
```

**Files changed:** `agent/prompt/templates.py`

**Status:** DONE.

### K.4 — Strengthen Budget-Exhausted Prompt

**Problem:** `BUDGET_EXHAUSTED_PROMPT` said "Summarize what you accomplished..." but didn't explicitly instruct the agent to save state. The agent wasn't calling `note_to_self` or `goal_update` when budget ran out.

**Solution:** Updated to explicitly instruct state-saving:

```
Step budget exhausted. Before finishing:
1. Call note_to_self with what you accomplished and what remains unfinished.
2. Call goal_update to record your progress percentage on the goal you advanced.
Then provide a brief final summary.
```

**Files changed:** `agent/prompt/templates.py`

**Status:** DONE.

### K.5 — Reflection JSON Parser Fix

**Problem:** After enriching the REFLECT system prompt (K.2), the model produced more chain-of-thought reasoning before emitting the JSON. The old `_parse_reflection()` used `text.find("{")` to locate the first `{`, which now matched small JSON snippets in the CoT (e.g., `{}` from discussing empty property objects) instead of the actual reflection JSON further down. This silently broke ALL reflection data extraction — 0 facts, 0 entities, no goal progress updates, no memory promotion, no reflection-forward storage.

**Solution:** Rewrote `_parse_reflection()` to scan through all top-level JSON objects (using brace-depth counting) and return the one containing `"cycle_summary"` — the required field that distinguishes the real reflection from CoT artifacts.

**Files changed:** `agent/cycle.py`

**Status:** DONE. Immediately recovered extraction — first cycle after fix extracted 10 facts and 1 entity.

| # | Task | Files | Status |
|---|------|-------|--------|
| K.1 | Feed reflection forward — store in Redis, inject into PLAN/REASON | `cycle.py`, `assembler.py` | DONE |
| K.2 | Enrich REFLECT system prompt — add identity, cycle, mission | `assembler.py`, `cycle.py` | DONE |
| K.3 | Purpose/identity rewrite — Section 5 "YOUR SELF" → "YOUR PURPOSE" | `templates.py` | DONE |
| K.4 | Strengthen budget-exhausted prompt — explicit state-saving instructions | `templates.py` | DONE |
| K.5 | Reflection JSON parser fix — scan for `cycle_summary` key | `cycle.py` | DONE |

---

## Phase L: Behavioral Tuning & Graph Quality (2026-02-23)

**Source:** Observations from cycles 39-64 after Phase K deployment. While raw output improved significantly (fact extraction 0→3-4/cycle, goals completing, reflection forward producing coherent cross-cycle intent), second-order behavioral patterns emerged: a self-reinforcing deepening loop, relationship type drift, entity name dedup failures, stale goal accumulation, and persistent Unknown entities. Full diagnostic in `docs/CYCLE_64_REVIEW.md`.

**Implementation order** (per feedback review): L.1 + L.2 + L.3 together as first batch (small, complementary — the meta-awareness prompt needs the phase-awareness data to be actionable), then L.4 (fuzzy dedup), then L.5 (graduated inventory — biggest change, benefits from seeing how much the first batch fixes).

### L.1 — Relationship Normalization in Code

**Problem:** The agent invents synonym relationship types (AuthoredBy, BuiltBy, HasPersistence) instead of using the canonical list. By the time the model is 10+ tool steps deep, the guidance in the system prompt is no longer salient. This is a prompt salience problem unfixable by prompts alone — 12 non-canonical types at cycle 64.

**Solution:** Add a relationship alias map and normalize at the storage layer in two places:

**In `graph_store` tool handler** — normalize before Cypher execution:

```python
RELATIONSHIP_ALIASES = {
    "AuthoredBy": "CreatedBy",
    "BuiltBy": "CreatedBy",
    "DevelopedBy": "CreatedBy",
    "MadeBy": "CreatedBy",
    "HasPersistence": "UsesPersistence",
    "HostedBy": "MaintainedBy",
}
```

Return in-band feedback: `"Note: 'AuthoredBy' normalized to canonical 'CreatedBy'."` The model learns the vocabulary over time without depending on it.

**In `_store_reflection_graph()`** — same alias map applied to relationships from REFLECT JSON.

**Files:** `agent/tools/builtins/graph_tools.py`, `agent/cycle.py`

**Effort:** Small (~15 lines).

**Status:** DONE

### L.2 — PLAN Prompt Meta-Awareness

**Problem:** The PLAN prompt says "pick the highest-priority goal and plan work" but has no concept of work-pattern awareness or goal hygiene. The agent has been deepening for 15+ consecutive cycles without stepping back.

**Solution:** Add a meta-assessment block to `PLAN_PROMPT` in `templates.py`:

```
Before choosing your focus, assess your overall state:
- If you have been doing the same kind of work (collecting, deepening, filling gaps)
  for several cycles in a row, consider switching to analysis or cross-project comparison.
- If any active goals have had 0% progress for 5+ cycles, consider removing or
  reprioritizing them with goal_update.
- If two active goals have the same or very similar descriptions, merge them — complete
  the lower-progress duplicate.
- If all major sub-goals are complete or near-complete, create a new goal focused on
  synthesis: patterns across projects, architectural comparisons, gap identification,
  or emerging trends.
```

**Important:** This prompt guidance is only actionable when paired with L.3's data signals. Without concrete numbers ("12 cycles since last new project"), the model can only guess about its behavioral pattern.

**Files:** `agent/prompt/templates.py`

**Effort:** Small (~8 lines).

**Status:** DONE

### L.3 — Phase Awareness in Reflection Forward

**Problem:** The reflection forward carries `self_assessment` and `next_cycle_suggestion` (model-generated), but the model cannot observe its own multi-cycle behavioral patterns. It doesn't know it's been deepening for 15 cycles — it only sees one cycle at a time. The meta-awareness prompt (L.2) tells it to notice patterns it literally cannot see.

**Solution:** Add code-computed metadata to the reflection forward register:

```python
# In _persist(), alongside existing reflection_forward storage:
reflection_forward["cycles_since_new_project"] = await self._count_cycles_since_new_entity("Project")
reflection_forward["recent_work_pattern"] = self._classify_work_pattern()
reflection_forward["stale_goal_count"] = len([g for g in active_goals if g.cycles_without_progress > 5])
```

Injection format:

```
## Previous Cycle Reflection
**Self-assessment:** {self_assessment}
**Suggested next action:** {next_cycle_suggestion}
**Cycles since last new project discovered:** 12
**Recent work pattern:** deepening (last 8 cycles)
**Stale goals:** 3
```

Implementation details:
- `_count_cycles_since_new_entity("Project")`: query graph for most recent entity of type, compute delta from current cycle number
- `_classify_work_pattern()`: categorize based on tool usage — http_request-heavy = collecting, graph_store-heavy = deepening, graph_analyze = analyzing
- Stale goal count: query goal manager for goals with 0% progress over N cycles

**Files:** `agent/cycle.py` (`_persist`, `_orient`)

**Effort:** Medium (~40 lines including helpers).

**Status:** DONE

### L.4 — Fuzzy Entity Dedup in graph_store

**Problem:** The agent creates multiple entities for the same project under name variations (Auto Copilot / AutoCoPilot / AutoCoPilotCLI). The agent does check `graph_query` before `graph_store`, but exact-match doesn't catch near-duplicates. Unfixable with prompts.

**Solution:** Fuzzy name check before entity creation:

```python
from difflib import SequenceMatcher

def _find_similar_entity(name, existing_names, threshold=0.85):
    normalized = name.lower().replace("-", "").replace("_", "").replace(" ", "")
    for existing in existing_names:
        existing_norm = existing.lower().replace("-", "").replace("_", "").replace(" ", "")
        if SequenceMatcher(None, normalized, existing_norm).ratio() >= threshold:
            return existing
    return None
```

Returns actionable error: `"Entity 'AutoCoPilotCLI' not created — similar entity 'AutoCoPilot' already exists. Use the existing name to update it."` Also applied in `_store_reflection_graph()`.

**Threshold note:** 0.85 after normalization catches suffix variations (AutoCoPilot → AutoCoPilotCLI) but not genuinely different projects sharing naming conventions (AutoGPT vs AgentGPT score ~0.75-0.80). Validate against actual entity names before deploying.

**Files:** `agent/tools/builtins/graph_tools.py`, `agent/cycle.py`

**Effort:** Small-medium (~25 lines + integration).

**Status:** DONE

### L.5 — Graduated Graph Inventory

**Problem:** The graph inventory injection is a flat "do NOT re-research" list. At 18+ projects it's counterproductive — it prevents all re-engagement with existing entities, even shallow ones that need deepening. The model can't distinguish "fully researched" from "has a stub." This also contributes to the deepening loop (Obs 1) because the model has no visibility into what's actually complete.

**Solution:** Replace flat inventory with graduated, annotated inventory:

```
## Knowledge Graph Inventory

### Entity Completeness (incomplete only — 12 fully-researched projects not shown)
| Project | architecture | persistence | safety | funding | creator |
|---------|:---:|:---:|:---:|:---:|:---:|
| BabyAGI | ✓ | ○ | ○ | ○ | ✓ |
| CrewAI | ✓ | ✓ | ○ | ○ | ✓ |

### Relationship Types (use ONLY these — synonyms auto-corrected per L.1)
CreatedBy, UsesArchitecture, UsesPersistence, HasSafety, FundedBy,
MaintainedBy, AffiliatedWith, PartOf, Extends, DependsOn, AlternativeTo, InspiredBy

### Warnings
⚠ 10 entities have type "Unknown" — reclassify with graph_store
⚠ Possible duplicates: Auto Copilot / AutoCoPilot / AutoCoPilotCLI
```

**Scaling strategy (build from the start):** Show full table only for entities under 80% completeness. Collapse complete entities into a count line. This scales to hundreds of entities without dominating the context budget.

Implementation:
- Inventory builder in `_orient()` or new helper
- Graph queries for entity attributes and relationship types per entity
- Fuzzy name matching (`difflib.SequenceMatcher`) for dedup warnings
- Completeness columns configurable (architecture, persistence, safety, funding, creator)
- Token budget: incomplete-only table + collapsed complete count stays compact even at scale

**Files:** `agent/cycle.py`, `agent/prompt/assembler.py`, possibly `agent/prompt/templates.py`

**Effort:** Medium (~50-80 lines for inventory builder + integration).

**Status:** DONE

### L.6 — Stale Goal Flagging via Inbox

**Problem:** Stale goals (integration test artifacts, duplicates, 0% progress for 10+ cycles) accumulate indefinitely. The model never cleans them up because nothing flags them as problematic.

**Solution:** Supervisor sends a low-priority inbox message when stale goals are detected: `"Note: 2 goals have had 0% progress for 10+ cycles."` The agent decides what to do — consistent with the design philosophy (supervisor monitors, agent decides). No auto-pruning.

**Files:** `supervisor/supervisor.py` (post-cycle check), `shared/schemas/comms.py` (if needed)

**Effort:** Small (~15 lines).

**Status:** DONE

| # | Task | Files | Status |
|---|------|-------|--------|
| L.1 | Relationship normalization — alias map in graph_store and _store_reflection_graph | `graph_tools.py`, `cycle.py` | DONE |
| L.2 | PLAN prompt meta-awareness — work-pattern and goal hygiene guidance | `templates.py` | DONE |
| L.3 | Phase awareness in reflection forward — code-computed behavioral signals | `cycle.py` | DONE |
| L.1x | L.1 expansion — canonical whitelist + fuzzy fallback replacing narrow alias map | `graph_tools.py`, `templates.py` | DONE |
| L.4 | Fuzzy entity dedup — SequenceMatcher in graph_store and _store_reflection_graph | `graph_tools.py`, `cycle.py` | DONE |
| L.5 | Graduated graph inventory — completeness table, warnings, scaling threshold | `cycle.py` | DONE |
| L.6 | Stale goal flagging — supervisor inbox message for stuck goals | `supervisor/main.py` | DONE |

---

## Phase M: Fix Cycle Repetition (2026-02-24)

**Source:** Observations from cycles 1-5 after Phase L reset. The agent is stuck in a repetition loop — cycles 2, 3, and 4 all fetch the same READMEs (OpenAI_Agent_Swarm, awesome-ai-agents) and re-extract the same data. Five reinforcing root causes identified.

### M.1 — Recent-Cycle Facts in ORIENT

**Problem:** `retrieve_context()` calls `query_facts(limit=10)` with no context filtering. Returns same top-10 by confidence every cycle — recent facts never surface.

**Solution:** Add `query_facts_recent(current_cycle, lookback=5)` to structured store. Add `current_cycle` parameter to `retrieve_context()`. Merge recent-cycle facts into the fact list so the agent sees its own recent work.

**Files:** `memory/structured.py`, `memory/manager.py`, `cycle.py`

**Status:** DONE

### M.2 — Deduplicate Facts by Subject

**Problem:** 10 GPT Researcher entries crowd out facts about other projects.

**Solution:** After merging facts in `retrieve_context()`, deduplicate by subject — max 2 facts per subject. Cap total at `facts_limit * 2`.

**Files:** `memory/manager.py`

**Status:** DONE

### M.3 — Within-Cycle HTTP GET Cache

**Problem:** Same URL fetched 2-3 times within a single cycle.

**Solution:** Module-level `_get_cache` dict in `http.py`. Cache GET 2xx responses by URL. `clear_http_cache()` called from WAKE phase.

**Files:** `tools/builtins/http.py`, `cycle.py`

**Status:** DONE

### M.4 — Deduplicate Parallel Tool Calls

**Problem:** LLM emits identical tool calls in same turn, both execute via `asyncio.gather`.

**Solution:** Before `asyncio.gather`, deduplicate by `(tool_name, json(args))`. Log skipped duplicates via `working_memory.add_note`.

**Files:** `llm/client.py`

**Status:** DONE

### M.5 — Strengthen Bootstrap Guidance

**Problem:** `BOOTSTRAP_PROMPT_ADDON` encourages "Begin collecting data from external sources" without requiring memory check first, contradicting `EFFICIENCY_GUIDANCE`.

**Solution:** Updated to require `memory_query` before `http_request` in early cycles.

**Files:** `prompt/templates.py`

**Status:** DONE

| # | Task | Files | Status |
|---|------|-------|--------|
| M.1 | Recent-cycle facts in ORIENT — query_facts_recent + current_cycle threading | `structured.py`, `manager.py`, `cycle.py` | DONE |
| M.2 | Subject dedup — max 2 facts per subject in retrieve_context | `manager.py` | DONE |
| M.3 | HTTP GET cache — within-cycle URL caching with clear at WAKE | `http.py`, `cycle.py` | DONE |
| M.4 | Tool call dedup — deduplicate identical parallel calls before gather | `client.py` | DONE |
| M.5 | Bootstrap guidance — require memory_query before http_request | `templates.py` | DONE |

---

## Phase N: Primary Mission & Strategic Review (2026-02-25)

**Source:** Observations from cycles 1-102 post-Phase M. Low-level repetition loop fixed, but agent exhibits a higher-level behavioral loop: discovers 11 projects successfully, then spends 50+ cycles chasing FUNDING.yml and SECURITY.md files that don't exist. 48 "funding" mentions and 35 "safety" mentions in cycle summaries. Goals oscillate 45-96% progress but never complete. Four reinforcing root causes identified.

### N.1 — Reframe Seed Goal as Primary Mission

**Problem:** `GOAL_CONTEXT_TEMPLATE` says "Your Mission (Immutable — This Is What You Work On)" — task-completion framing causes the agent to grind on narrow goals indefinitely instead of evaluating strategic priorities.

**Solution:** Reframed to "Primary Mission (Strategic Direction — Not a Task to Complete)" with explicit guidance that goals serve the mission and the agent should ask "Is this the best use of my cycles?" not "Is this goal at 100%?"

**Files:** `prompt/templates.py`, `prompt/assembler.py`

**Status:** DONE

### N.2 — Mission Review Every N Cycles

**Problem:** No mechanism for periodic strategic review. Design doc planned "every N cycles, re-read seed goal and compare goal tree" but never implemented.

**Solution:** New `_mission_review()` phase between ORIENT and PLAN (fires every 15 cycles, configurable). Separate LLM call evaluates goal tree health, diminishing returns, mission alignment (0.0-1.0), underserved areas. Output prepended to reflection_forward for PLAN phase visibility. Surfaces deferred goals past their revisit cycle.

**Files:** `shared/config.py` (mission_review_interval), `prompt/templates.py` (MISSION_REVIEW_PROMPT), `prompt/assembler.py` (assemble_mission_review_prompt), `cycle.py` (_mission_review, _parse_json_with_key)

**Status:** DONE

### N.3a — Attempt Tracking Guidance in PLAN

**Problem:** PLAN_PROMPT doesn't tell the agent to recognize diminishing returns from repeated fruitless searches.

**Solution:** Added bullets to PLAN_PROMPT: "If you have investigated the SAME piece of information across 3+ cycles with nothing new, that information likely does not exist. Defer or close rather than continuing to search."

**Files:** `prompt/templates.py`

**Status:** DONE

### N.3b — DEFERRED Goal Status

**Problem:** No way to park a goal for later revisit. `pause` exists but is for operator-initiated holds.

**Solution:** Added `DEFERRED` to `GoalStatus` enum with `deferred_until_cycle` and `defer_reason` fields on Goal model. New `defer` action in `goal_update` tool with `revisit_after_cycles` parameter (default 15). Deferred goals excluded from active queries (status != 'active'). `get_deferred_goals()` surfaces goals past their revisit cycle during mission review.

**Files:** `shared/schemas/goals.py`, `memory/structured.py`, `goals/manager.py`, `tools/builtins/goal_tools.py`, `cycle.py`

**Status:** DONE

### N.4 — Teach "Not Available" as Valid Outcome

**Problem:** Agent treats "not found" as failure, leading to endless retries of the same search.

**Solution:** Added "Valid Goal Outcomes" section to PLAN_PROMPT: "'Information confirmed unavailable after N attempts' IS a valid completion. A knowledge graph with honest gaps is more valuable than one where the agent spends 50 cycles trying to fill every cell."

**Files:** `prompt/templates.py`

**Status:** DONE

| # | Task | Files | Status |
|---|------|-------|--------|
| N.1 | Reframe seed goal as Primary Mission — strategic direction framing | `templates.py`, `assembler.py` | DONE |
| N.2 | Mission review every N cycles — periodic LLM-driven strategic assessment | `config.py`, `templates.py`, `assembler.py`, `cycle.py` | DONE |
| N.3a | Attempt tracking — diminishing returns awareness in PLAN prompt | `templates.py` | DONE |
| N.3b | DEFERRED goal status — defer action with revisit_after_cycles | `goals.py`, `structured.py`, `manager.py`, `goal_tools.py`, `cycle.py` | DONE |
| N.4 | Valid closure guidance — "not found" is a finding | `templates.py` | DONE |
| N.5 | Ellipsis JSON serialization crash — `default=str` + arg sanitization | `client.py`, `tool_parser.py` | DONE |

### N.5 — Ellipsis JSON Serialization Crash

**Problem:** Agent crashes with `Object of type ellipsis is not JSON serializable` approximately every 30-50 cycles (hit cycle 8 in post-N run, cycles 53 and 90 in post-M run). The crash kills the entire cycle — no heartbeat, no reflection, no memory persistence.

**Root cause chain:**
1. LLM outputs `...` as a parameter value in a tool call (e.g., `{"recursive": ...}` or `{"headers": ...}`)
2. `tool_parser.py` line 237: `ast.literal_eval()` parses this successfully — but creates a Python `Ellipsis` object as the dict value
3. `client.py` line 268: Tool call deduplication (Phase M.4) runs `json.dumps(tc.arguments, sort_keys=True)` **without `default=str`**
4. `json.dumps()` raises `TypeError: Object of type ellipsis is not JSON serializable`
5. Exception propagates up, kills the cycle

**Why it's intermittent:** The LLM only occasionally produces `...` in tool arguments — it happens when the model uses `...` as a shorthand/placeholder (common in its training data from code examples).

**Fix (two parts):**

1. **Primary — `client.py` line 268:** Add `default=str` to the `json.dumps` call in the deduplication key. This prevents the crash even if Ellipsis objects reach this point.

2. **Secondary — `tool_parser.py` after line 239:** Sanitize `ast.literal_eval()` output to replace Ellipsis values with `"..."` strings before returning. This prevents Ellipsis from reaching any downstream code.

**Files:** `llm/client.py`, `llm/tool_parser.py`

**Status:** DONE

**Observations:**
- Other `json.dumps` call sites were audited: `log.py`, `registers.py`, `nats_client.py` all already use `default=str`. `structured.py` uses Pydantic's `model_dump_json()` which handles it. Several `opensearch_tools.py` calls lack `default=str` but are lower risk (unlikely to receive Ellipsis from structured data).
- The bug existed since Phase M.4 (tool call dedup) was introduced. Earlier cycles didn't hit it because there was no `json.dumps` on tool arguments before dedup was added.

---

## Phase O: Per-Goal Attempt Tracking (2026-02-25)

**Source:** Observations from cycles 38-43 post-Phase N. Phase N added prompt guidance telling the agent "if you've tried 3+ cycles, stop and defer." But the agent cannot see how many cycles it has worked on each goal — the guidance asks a self-assessment question the model can't answer. Result: 6 consecutive cycles re-fetching the same TaskWeaver/PaSa READMEs chasing "missing funding" and "missing safety" despite the N.3a guidance.

Same class of problem as L.3 (work pattern awareness): the prompt guidance exists but the data doesn't. Phase L.3 fixed it for work patterns by computing `cycles_since_new_project` as hard data. Phase O does the same for per-goal attempt counts.

### O.1 — Read Goal Work Tracker in ORIENT

**Problem:** No per-goal cycle count data available during planning.

**Solution:** Read `goal_work_tracker` Redis register in `_orient()` and store as `self._goal_work_tracker`. Register maps `{goal_id: {cycles_worked, last_progress_cycle, last_worked_cycle}}`. Passed to both `assemble_plan_prompt()` and `assemble_reason_prompt()`.

**Files:** `cycle.py`

**Status:** DONE

### O.2 — Inject Attempt Data Into Goal Display

**Problem:** `_format_goals()` shows `- [goal][P5] Research TaskWeaver (95% | active)` but no attempt history.

**Solution:** Extended `_format_goals()` to accept `goal_work_tracker` and `current_cycle`. Each goal line now appends a tracking tag: `[6 cycles, 4 since progress, STALLED]` or `[new]`. STALLED flag = `cycles_worked >= 3` AND `current_cycle - last_progress_cycle >= 3`. Threaded through `assemble_plan_prompt()` and `assemble_reason_prompt()`.

**Files:** `assembler.py`

**Status:** DONE

### O.3 — Write Goal Work Tracker in PERSIST

**Problem:** No mechanism to accumulate per-goal cycle counts across cycles.

**Solution:** In `_persist()`, after goal progress update, extract `goal_progress.description` from reflection data to identify which goal was worked on. Increment `cycles_worked` and `last_worked_cycle`. If `progress_delta > 0`, update `last_progress_cycle`. Prune entries for goals no longer active. Store back to `goal_work_tracker` Redis register.

**Files:** `cycle.py`

**Status:** DONE

### O.4 — Update PLAN_PROMPT to Reference Visible Data

**Problem:** N.3a guidance says "if you've tried 3+ cycles" but references no visible data.

**Solution:** Replaced with guidance that references the now-visible tracking tags: "Check the cycle counts shown next to each goal... Goals marked STALLED have been worked on 3+ cycles with no progress — defer or close them."

**Files:** `templates.py`

**Status:** DONE

| # | Task | Files | Status |
|---|------|-------|--------|
| O.1 | Read goal work tracker in ORIENT | `cycle.py` | DONE |
| O.2 | Inject attempt data into goal display lines | `assembler.py` | DONE |
| O.3 | Write goal work tracker in PERSIST | `cycle.py` | DONE |
| O.4 | Update PLAN_PROMPT to reference visible tracking data | `templates.py` | DONE |

---

## Consolidated Tracking Matrix

| Phase | Tasks | Done | Status |
|-------|-------|------|--------|
| **A: Config Fixes** | 5 | 5 | COMPLETE |
| **B: Context Management** | 4 | 4 | COMPLETE |
| **C: Cycle Flow** | 4 | 4 | COMPLETE |
| **D: Prompt Rewrites** | 7 | 7 | COMPLETE |
| **E: Data Reset & Deploy** | 6 | 6 | COMPLETE |
| **F: Nonce Refactor & Prompt Fix** | 2 | 2 | COMPLETE |
| **G: Tool Parser & Logging** | 3 | 3 | COMPLETE |
| **H: Context Safety & Tool Init Fixes** | 5 | 5 | COMPLETE |
| **I: Memory Effectiveness** | 5 | 5 | COMPLETE |
| **J: Multi-Tool Calls** | 6 | 6 | COMPLETE |
| **K: Feedback-Driven Enhancements** | 5 | 5 | COMPLETE |
| **L: Behavioral Tuning & Graph Quality** | 7 | 7 | COMPLETE |
| **M: Fix Cycle Repetition** | 5 | 5 | COMPLETE |
| **N: Primary Mission & Strategic Review** | 6 | 6 | COMPLETE |
| **O: Per-Goal Attempt Tracking** | 4 | 4 | COMPLETE |
| **Total** | **74** | **74** | **All phases complete** |

*Phase K completed 2026-02-23. Phase L defined 2026-02-23 based on cycle 39-64 observations. L.1-L.3 deployed cycle 73, verified over 58 cycles. L.1x+L.4+L.5+L.6 completed 2026-02-24. Phase M defined 2026-02-24 based on cycle 1-5 post-reset observations — agent stuck in repetition loop re-fetching same URLs. Full reset follows M deploy. Phase N defined 2026-02-25 based on cycle 1-102 post-M observations — higher-level behavioral loop chasing missing data for 50+ cycles. Full reset follows N deploy. Phase O defined 2026-02-25 based on cycles 38-43 post-N — prompt guidance about attempt limits present but agent has no data to count attempts. Pending deploy after cycle 45 mission review.*

---

## Dependencies

```
Phase A (config) ──────────────────────┐
                                       ├──> Phase E (deploy)
Phase B (context mgmt) ───┐           │
                           ├──> Phase D (prompts)             ──> Phase F (nonce refactor)
Phase C (cycle flow) ──────┘           │                          Phase G (tool parser & logging)
                                       │                          Phase H (context safety & tool init)
Phase D (prompts) ─────────────────────┘                          Phase I (memory effectiveness)
                                                                  Phase J (multi-tool calls)
                                                                  Phase K (feedback-driven enhancements)
                                                                  Phase L (behavioral tuning & graph quality)
```

Practical order: **A → B → C → D → E → F + G → H → I → J → K → L**.

Phase L implementation order: **L.1 + L.2 + L.3** (first batch — relationship normalization + PLAN meta-awareness + phase awareness data; complementary and the data makes the prompt guidance actionable), then **L.4** (fuzzy dedup), then **L.5** (graduated inventory — biggest change, see how much the first batch fixes first), then **L.6** (stale goal flagging).

---

## Design Notes

### Context Management Strategy (Phase B detail)

**Sliding window approach:**
- Steps 1 through (current - 5): condense to `{step_num, tool_name, args_summary, result_summary}` — one line per step, no analysis channel content
- Steps (current - 4) through current: keep in full (model needs recent context for continuity)
- Every 8 steps: generate a working memory checkpoint (quick LLM call or extracted from analysis) that summarizes "what I've done, what I've learned, what's next"
- The checkpoint replaces the condensed older steps, keeping context growth bounded

**Working memory / scratchpad:**
- In-process list of `{step, observation, tool, key_result}` entries
- `note_to_self` tool lets the model explicitly add observations
- Programmatic extraction: after each tool call, extract tool name + truncated result
- Injected into conversation as a user message when context is rebuilt
- Also fed to REFLECT phase as structured input (replaces the current "Cycle N: X actions taken")

### Cycle Plan Step (Phase C.1 detail)

Between ORIENT and REASON, add a PLAN sub-phase:
1. Assemble context (same as current ORIENT output)
2. Ask the model: "Given your mission, active goals, memories, and inbox — what should you accomplish this cycle? Pick ONE goal to advance. Describe your approach in 2-3 sentences. List the tools you expect to use."
3. Store the plan in the cycle scratchpad
4. Pass the plan as context into the REASON phase

This gives the model a commitment before it starts acting, reducing drift.

### Reporting Cadence (Phase C.4 detail)

Update seed goal to include: "Every 5 cycles, produce a status report: what you've accomplished, what you've learned, current direction, any blockers or gaps. Publish via outbox."

Add to templates: a periodic reporting check in the cycle request. When `cycle_number % 5 == 0`, inject a reminder: "This is a reporting cycle. Produce a status report before other work."

### Reflect Phase Improvements (Phase C.3 detail)

Current reflect gets almost nothing as input. New approach:
1. Feed the full working memory scratchpad (all observations from the cycle)
2. Ask for structured JSON output:
```json
{
  "cycle_summary": "...",
  "significance": 0.0-1.0,
  "facts_learned": [{"subject": "...", "predicate": "...", "value": "...", "confidence": 0.0-1.0}],
  "entities_discovered": [{"name": "...", "type": "...", "properties": {...}}],
  "relationships": [{"from": "...", "to": "...", "type": "..."}],
  "goal_progress": {"goal_id": "...", "progress_delta": 0.0-1.0, "notes": "..."},
  "self_assessment": "what went well, what didn't",
  "next_cycle_suggestion": "what to focus on next"
}
```
3. Parse the JSON and store facts/entities/relationships via the memory manager
4. Use `cycle_summary` for the heartbeat response (much better than current `_final_response[:200]`)

---

## References

- [OpenAI Harmony Response Format (Cookbook)](https://developers.openai.com/cookbook/articles/openai-harmony/)
- [openai/harmony GitHub](https://github.com/openai/harmony)
- [openai/gpt-oss GitHub](https://github.com/openai/gpt-oss)
- [gpt-oss vLLM Recipes](https://docs.vllm.ai/projects/recipes/en/latest/OpenAI/GPT-OSS.html)
- [gpt-oss-120b HuggingFace Model Card](https://huggingface.co/openai/gpt-oss-120b)
- Previous deployment docs: `DEPLOYMENT.md`, `docs/archive/enhancements.md`, `docs/archive/IMPLEMENTATION.md`
