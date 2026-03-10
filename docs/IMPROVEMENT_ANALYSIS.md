# Legba Improvement Analysis

*Compiled 2026-03-09 from GPT-OSS 215-cycle run, Claude Sonnet 18-cycle test, code analysis, and prompt review.*

---

## Overview

After 215 cycles on GPT-OSS and 18 cycles on Claude Sonnet, a clear picture emerges of what works, what's broken, and what can be improved. Issues fall into three categories:

1. **Code bugs** — broken pipelines that affect both providers (fix in code)
2. **Prompt/guidance gaps** — behavioral issues addressable through prompt engineering (mostly GPT-OSS)
3. **Architectural improvements** — structural changes for better outcomes

---

## Part 1: Code Bugs (Both Providers)

### 1.1 Entity Profile Assertions Never Persist

**Severity: Critical — blocks all enrichment goals**

The `entity_profile` tool is called hundreds of times (227 on GPT-OSS, 103 on Claude) but every entity has **zero assertions** stored. All enrichment goals ("Enrich Iran to ≥0.8 completeness") are structurally impossible because `completeness_score` depends on assertion count.

**Root cause:** The assertion storage pipeline creates assertions in memory correctly, but they don't survive across cycles. The likely failure path:

1. Cycle N: Create profile for "Russia", add 5 assertions, save to Postgres JSONB → works
2. Cycle N+1: Call `entity_profile("Russia")` → `resolve_entity_name()` loads profile from DB
3. Problem: The loaded profile's `sections` dict comes back empty (deserialization issue with JSONB → Pydantic model), OR the profile is re-created as new (resolve fails silently, creates fresh EntityProfile)
4. New assertions overwrite the empty sections dict, save again → only latest batch persists
5. Net result: always ~0 visible assertions because each cycle overwrites the last

**Key files:**
- `entity_tools.py:148-150` — profile resolution before adding assertions
- `structured.py:683-743` — `save_entity_profile()` serializes with `model_dump_json()`
- `structured.py:745-850` — `resolve_entity_name()` deserializes with `model_validate_json()`
- `entity_profiles.py:44-92` — `Assertion` model and `EntityProfile.sections` dict

**Investigation needed:**
- Add debug logging in `resolve_entity_name()` to check if sections are populated on load
- Check if `model_validate_json()` properly reconstructs the nested `dict[str, list[Assertion]]`
- Check if the JSONB column stores sections correctly (query Postgres directly)
- Check if profile versioning (`entity_profile_versions` table) has the same issue

### 1.2 OpenSearch Gets ~2x More Events Than Postgres

**Severity: Medium — data inconsistency, breaks source utilization metrics**

Both providers show the same ~2:1 ratio (GPT-OSS: 234 OS vs 115 PG; Claude: 67 OS vs 29 PG). The dual-write in `event_tools.py:297-324` saves to Postgres first, then OpenSearch, but:

- Postgres save can fail silently (returns `False` instead of raising)
- OpenSearch save proceeds regardless on a best-effort basis
- Source event count only increments `if pg_ok` — so if PG fails, count stays at 0

**Root cause:** Postgres rejects some events (constraint violations, transaction errors) but OpenSearch accepts them. The tool reports `"partial_failure"` but the agent doesn't retry or investigate.

**Key files:**
- `event_tools.py:297-331` — dual-write logic
- `structured.py` — `save_event()` implementation

**Fix direction:**
- Log the actual Postgres error when `pg_ok` is False
- Either make both writes atomic (both succeed or both fail) or add reconciliation
- At minimum, surface Postgres failures clearly in tool output so the agent (and operator) can see them

### 1.3 Source Event Counts Always Zero

**Severity: Low-Medium — breaks source utilization metrics in ORIENT**

All sources show `events_produced_count = 0` on Claude despite 29 events stored. The increment at `event_tools.py:327-331` only fires `if pg_ok and event.source_id`. Since Postgres saves are failing (see 1.2), the increment never runs.

**Secondary issue:** The JSONB update in `structured.py:573-588` does both a column update AND a `jsonb_set()` on the data field. If the JSONB path is wrong or the data field is null, `jsonb_set()` can silently fail.

**Fix direction:** Fix 1.2 first (Postgres saves), and the counts should start working. Also verify the `jsonb_set()` path is correct.

---

## Part 2: GPT-OSS Behavioral Issues (Prompt Engineering)

These are behaviors where Claude performs well but GPT-OSS doesn't. They're addressable through prompt changes, not code changes.

### 2.1 Event Headlines Stored as Graph Entities

**Problem:** 47 graph nodes are news headlines stored as entity type "Event" (e.g., "Boy, 12, among six dead as tornadoes hit Michigan and Oklahoma"). Claude: zero such nodes.

**Current guidance:** `SYSTEM_PROMPT` warns against unconnected nodes and `SA_GUIDANCE` prescribes using `entity_resolve` for actor extraction. But there's **no explicit prohibition on storing event titles as entities**.

**What's missing in prompts:**
- No rule saying "Do NOT create entities from event headlines. Events are data (stored with `event_store`), not world-model entities. Entities represent persistent actors, places, organizations, and concepts."
- `ENTITY_GUIDANCE` lists "event_series" as a valid entity_type but doesn't clarify it means recurring phenomena (e.g., "Syrian Civil War"), not individual news events

**Fix direction:** Add explicit prohibition in `SYSTEM_PROMPT` or `ENTITY_GUIDANCE`:
```
NEVER create graph entities from event headlines, news titles, or one-time occurrences.
Entities are persistent real-world things: people, countries, organizations, locations, concepts.
Events are stored with event_store. Actors within events are resolved with entity_resolve.
```

### 2.2 Overuse of Generic "RelatedTo" (32% of edges)

**Problem:** GPT-OSS defaults to `RelatedTo` for 124 of 390 edges (32%). Claude: zero RelatedTo edges — always picks a specific type.

**Current guidance:** `PLAN_PROMPT:163` says "Use specific relationship types... avoid defaulting to RelatedTo" — good guidance, but buried in a subsection. The relationship normalization layer (`graph_tools.py:120-152`) maps 70+ aliases to 17 canonical types, with `RelatedTo` as the catch-all fallback for unrecognized input.

**What's missing:**
- No decision tree or priority ordering for relationship types
- The `graph_store` tool description itself lists non-canonical examples ("caused, affects, mitigates") that normalize to `RelatedTo`
- No negative reinforcement — the agent never learns that `RelatedTo` is low-value

**Fix direction:**
1. Move the "avoid RelatedTo" guidance into `SYSTEM_PROMPT` (always visible, not just in PLAN)
2. Add a decision tree: "Before using RelatedTo, consider: Is this LeaderOf, MemberOf, AlliedWith, HostileTo, LocatedIn, OperatesIn, PartOf, AffiliatedWith, FundedBy, SuppliesWeaponsTo, SanctionedBy, TradesWith, BordersWith, or SignatoryTo? Only use RelatedTo if none of these 14 types apply."
3. Clean up the `graph_store` tool parameter description to only show canonical types as examples
4. Consider rejecting or warning on `RelatedTo` at the tool level (return a suggestion for a better type)

### 2.3 Massive Source Duplication (~40%)

**Problem:** 102 sources with ~40% duplicates (NDTV India ×9, Reuters Africa ×7, etc.). Claude: 25 sources, 1 duplicate.

**Current guidance:** `SA_GUIDANCE:507` says "call source_list to check for existing coverage." The tool itself checks the last 500 sources for URL/name overlap. But the agent ignores duplicate detection warnings.

**What's missing:**
- No instruction to treat `duplicate_detected` in tool output as a hard stop
- No systematic audit cycle for source health
- Bootstrap guidance (`BOOTSTRAP_PROMPT_ADDON`) encourages adding sources but doesn't warn about sprawl
- No cap on total sources — the agent just keeps adding

**Fix direction:**
1. Add to `SA_GUIDANCE`: "If source_add returns duplicate_detected, STOP. Do not retry with a different URL. That outlet is already registered."
2. Add a soft cap: "Maintain 20-30 quality sources. Before adding source #31, retire one with zero events in the last 20 cycles."
3. Strengthen the tool-level dedup: fuzzy match on outlet name (not just URL normalization)
4. Add a source audit reminder every 10 cycles (like the reporting reminder every 5)

### 2.4 Duplicate Goals

**Problem:** 18 goals with 3 duplicate pairs. Claude: 4 goals, zero duplicates.

**Current guidance:** None. There's no check-before-create rule for goals.

**Fix direction:**
1. Add to `PLAN_PROMPT` or `MISSION_REVIEW_PROMPT`: "Before calling goal_create, review all active and deferred goals. If an existing goal covers ≥70% of the same scope, update that goal instead of creating a new one."
2. Consider tool-level validation in `goal_create` — fuzzy match against existing goal descriptions
3. Add consolidation guidance for introspection: "If you find duplicate active goals, complete one and update the other to cover the combined scope."

### 2.5 Analytics Tools Never Used

**Problem:** GPT-OSS never called `anomaly_detect`, `forecast`, `correlate`, or `graph_analyze` in 215 cycles. Claude used all four within 18 cycles.

**Current guidance:** `ANALYTICS_GUIDANCE` is a passive 10-line table listing the tools. It says what they do but never says *when* to use them. Analytics tools are excluded from introspection cycles (which use "internal query tools only").

**What's missing:**
- No trigger conditions ("When you have >50 events, run anomaly_detect")
- No goal alignment ("If your goal involves pattern detection, use these tools")
- No integration with introspection (the deepest analysis phase excludes analytics)
- No reflection check ("Did you use analytics this cycle? Should you have?")
- `PLAN_PROMPT` never mentions analytics as an approach option

**Fix direction:**
1. Expand `ANALYTICS_GUIDANCE` with trigger conditions:
   ```
   - anomaly_detect: Use when you have >30 events and want to find unusual patterns
   - graph_analyze: Use when you have >20 graph relationships to find central actors or clusters
   - correlate: Use when you have 10+ entities with numeric attributes
   - forecast: Use when you have 20+ time-series data points
   ```
2. Add analytics to `PLAN_PROMPT` approach options: "Consider whether statistical analysis (anomaly_detect, correlate, graph_analyze) would surface insights faster than manual review."
3. Add analytics tools to introspection's allowed tool set (they're read-only analysis, not external actions)
4. Add to `REFLECT_PROMPT`: "Did you use any analytical tools? If you processed data without statistical analysis, note whether analytics would have added value."

### 2.6 Token Count Comparison Is Misleading (970 vs 303)

**Observed:** GPT-OSS averages 970 tokens/completion vs Claude's 303. Initially this looked like a 3.2x verbosity problem.

**Why it's misleading:** GPT-OSS uses Harmony format with `<analysis>` reasoning channels. The `completion_tokens` count in the API response includes ALL generated tokens — including reasoning tokens that are stripped by `strip_harmony_response()` before the output reaches the tool parser. The actual JSON output (what the agent "says") may be comparable to Claude's 303. We can't measure real output verbosity from the API metrics alone.

**What this means:**
- `reasoning: high` MUST stay — it's the quality dial, not a verbosity lever. Lowering it degrades analytical quality.
- The 7 min vs 2.4 min cycle time gap is primarily from fewer tool calls per cycle (20.4 vs 43.7) and LLM inference speed — not from verbosity.
- The token cost is irrelevant since GPT-OSS is self-hosted.

**Actual fix direction:**
1. Track stripped-vs-output token counts separately to get real verbosity data (log `len(raw)` before and after `strip_harmony_response()`)
2. If actual JSON output IS verbose, add brevity guidance for `note_to_self` (one sentence max)
3. Do NOT change reasoning levels — this was explicitly rejected as harmful to quality

### 2.7 Tool Loop Early Exit — The Real Efficiency Problem

**Problem:** GPT-OSS hits max 20 steps only 3.5% of cycles. Claude hits max 69%. GPT-OSS averages 20.4 tool calls/cycle vs Claude's 43.7. This is the primary performance gap — not verbosity.

**Root cause (`client.py:314-317`):**
```python
if not tool_calls:
    # No tool call — this is the model's final response
    history_messages.append(Message(role="assistant", content=raw))
    return raw, history_messages
```

When `parse_tool_calls()` returns empty, the loop exits immediately — treating it as "the model is done." But GPT-OSS may produce responses where:
- Tool call JSON is malformed (missing quotes, trailing comma)
- Reasoning text leaks around the JSON (Harmony stripping is imperfect)
- The model produces prose with tool names mentioned but not in the expected format
- The model generates a valid `note_to_self` reflection without a tool action

All of these trigger the same `return` — silent early exit. No retry, no re-prompt, no logging.

**What's missing in code:**
- No distinction between "model intentionally decided it's done" vs "format compliance failure"
- No re-prompt on parse failure (ask the model to reformat)
- No logging of the raw response that failed to parse — invisible debugging
- `has_tool_call()` exists in tool_parser.py but is never used here to detect likely-intended-but-malformed calls

**Secondary cause — batching gap:**
GPT-OSS averages 1.36 tool calls/step vs Claude's 2.18. Even when staying in the loop, GPT-OSS does one thing at a time. Batching guidance was already added (Phase C, item 10), but may need to be stronger.

**Fix direction:**
1. **Re-prompt on likely format failure** (`client.py`): If `parse_tool_calls()` returns empty but `has_tool_call()` detects tool-like content in `raw`, add a re-prompt step instead of exiting. Send a message like "Your previous response contained tool references but wasn't in valid JSON format. Respond with the actions JSON only."
2. **Log unparsed exits**: When the loop exits via the `not tool_calls` path, log the raw response (truncated) at WARNING level so we can see what GPT-OSS is actually producing.
3. **Add `cycle_complete` as the only clean exit**: Don't treat "no parseable tool calls" as completion. Only exit on explicit `cycle_complete` or budget exhaustion. If `parse_tool_calls()` returns empty AND `has_tool_call()` returns False (truly no tool intent), re-prompt once with `REGROUND_PROMPT` before giving up.
4. **Consider step budget increase**: Raise `AGENT_MAX_REASONING_STEPS` from 20 to 25. GPT-OSS underuses the budget due to early exits; increasing it gives more room once early exits are fixed.
5. **Batching**: Already addressed in prompts (Phase C). Monitor whether GPT-OSS starts batching more after the prompt changes.

---

## Part 3: Cross-Cutting Improvements

### 3.1 Entity Type Quality

Both providers mistype major entities (GPT-OSS: 27% mistyped; Claude: 19%). Countries stored as "Unknown", persons as "Other".

**Direction:**
- Add entity type validation in `graph_store` — if an entity name matches a known country (pycountry lookup), force type to "Country"
- Add to `ENTITY_GUIDANCE`: "Common entity types and examples: Person (leaders, officials), Country (sovereign states), Organization (companies, agencies, armed groups), InternationalOrg (UN, EU, NATO, BRICS), Location (cities, regions), PoliticalParty (CDU, ANC, BJP)"
- Consider a periodic graph cleanup tool or introspection step that audits entity types

### 3.2 Report Data Grounding

GPT-OSS: 100% claims verified against stored data. Claude: 62.5% (mixes in world briefing content). Different failure modes — GPT-OSS stays too narrow, Claude goes too broad.

**Direction:**
- For Claude: Strengthen `ANALYSIS_REPORT_PROMPT` to require event ID citations: "Every claim must reference a stored event by title or ID. If you cannot cite a stored event, explicitly note the claim as 'from world briefing' or 'from prior knowledge'."
- For GPT-OSS: Encourage broader synthesis but maintain citation discipline
- Consider injecting a "data available for this report" summary into the report prompt showing actual event counts by region, so the LLM knows what it has to work with

### 3.3 Memory Auto-Promotion Calibration

Claude promotes 89% of episodes to long-term (significance ≥0.6 threshold). GPT-OSS promotes 10%. Claude's REFLECT phase assigns consistently high significance scores, diluting the signal.

**Direction:**
- Model-specific significance calibration may be needed
- Or: add to `REFLECT_PROMPT`: "Significance scale: 0.0-0.3 (routine data collection), 0.4-0.5 (new entity or relationship discovered), 0.6-0.7 (significant pattern or development), 0.8-1.0 (major geopolitical shift or breakthrough insight). Most cycles should score 0.3-0.5."

### 3.4 Source Lifecycle Management

Neither provider disables broken sources. The `source_update(status=disabled)` capability exists but is never used.

**Direction:**
- Auto-pause is already implemented at 5 consecutive failures — verify it's working
- Add to `SA_GUIDANCE`: "Every 10 cycles, review source health. Sources with reliability_score < 0.3 or zero events in 20 cycles should be disabled with source_update(status=disabled)."
- Surface source health more prominently in ORIENT context

### 3.5 Event-Entity Link Coverage

Only 49% of entities and 61% of events have cross-links. The entity resolution pipeline exists but isn't applied comprehensively.

**Direction:**
- Add to `SA_GUIDANCE`: "After every event_store call, immediately call entity_resolve for EACH actor and location mentioned. An event without entity links is analytically invisible."
- Consider making entity_resolve automatic in event_store (code-level: extract actors from event, auto-resolve)

---

## Part 4: Priority Matrix

### Must Fix (Code Bugs)

| # | Issue | Impact | Effort |
|---|-------|--------|--------|
| 1 | Entity assertion storage | Blocks all enrichment | Medium — debug deserialization |
| 2 | Postgres event save failures | Data loss, metric corruption | Medium — add logging, fix constraints |
| 3 | Source event_count | Broken utilization metrics | Low — follows from #2 |

### High Impact Prompt Changes

| # | Issue | Expected Impact | Effort |
|---|-------|-----------------|--------|
| 4 | Prohibit event headlines as entities | Eliminate 47-node graph pollution | Low — add 3 lines to prompt |
| 5 | Reduce RelatedTo usage | More analytically useful graph | Low — add decision tree to prompt |
| 6 | Analytics tool triggers | Unlock 8 unused tools (16% of toolset) | Low — expand ANALYTICS_GUIDANCE |
| 7 | Tool loop re-prompt on parse failure | Prevent silent early exits, +50% tool utilization | Medium — code change in client.py |

### Medium Impact Prompt Changes

| # | Issue | Expected Impact | Effort |
|---|-------|-----------------|--------|
| 8 | Source dedup strengthening | Eliminate 40% duplication | Low — prompt + tool-level fuzzy match |
| 9 | Goal dedup check | Eliminate duplicate goals | Low — add check-before-create rule |
| 10 | Batching guidance | More tool calls per cycle | Low — clearer examples in prompt |
| 11 | Entity type guidance | Reduce mistyped entities | Low — add type examples to prompt |
| 12 | Report data grounding (Claude) | Better citation discipline | Low — require event ID citations |

### Lower Priority

| # | Issue | Expected Impact | Effort |
|---|-------|-----------------|--------|
| 13 | Source lifecycle management | Cleaner source registry | Low — add audit reminder |
| 14 | Event-entity link coverage | Better cross-referencing | Low — prompt reinforcement |
| 15 | Memory promotion calibration | Better long-term memory signal | Low — add scale guidance to REFLECT |
| 16 | Cycle timeout increase | Fewer graceful shutdowns | Trivial — change env var |

---

## Part 5: Implementation Approach

### Phase A: Code Fixes (items 1-3) — DONE

| # | Fix | File(s) | What changed |
|---|-----|---------|--------------|
| 1 | Entity assertion crashes | `entity_tools.py` | `_str_arg()` helper for type-safe arg extraction. Handles list/null/bool from LLM. Assertions accept pre-parsed lists. |
| 2 | Postgres event FK failures | `structured.py` | Catches `ForeignKeyViolationError`, retries with `source_id=NULL`. Error logging added. |
| 3 | Source event count stuck at 0 | `event_tools.py` | Removed `pg_ok` gate on increment. Error logging added. |

### Phase B: High-Impact Prompt Changes (items 4-7) — DONE

Woven into existing voice — subtle guidance, not blunt rules:

| # | Fix | Approach |
|---|-----|----------|
| 4 | Event headlines as entities | "What Is an Entity?" section in ENTITY_GUIDANCE. Removed "event" from graph_store entity_type options. |
| 5 | RelatedTo overuse | Reframed analytically in SYSTEM_PROMPT §5. Canonical types only in graph_store tool description. |
| 6 | Analytics tool triggers | Expanded ANALYTICS_GUIDANCE with "when to reach for it". Added to SYSTEM_PROMPT, MISSION_REVIEW, PLAN_PROMPT. |
| 7 | Tool loop re-prompt on parse failure | **NOT YET DONE** — code change in `client.py` to re-prompt instead of silently exiting when `parse_tool_calls()` fails. See §2.7. |

### Phase C: Medium-Impact Prompt Changes (items 8-12) — DONE (except 12)

| # | Fix | Approach |
|---|-----|----------|
| 8 | Source dedup | "If source_add returns duplicate_detected, move on." in SA_GUIDANCE |
| 9 | Goal dedup | "Before creating a new goal, look at your active goals" in PLAN_PROMPT |
| 10 | Batching | Concrete examples in EFFICIENCY_GUIDANCE. Plural "tools" in REGROUND_PROMPT. |
| 11 | Entity types | Expanded type list with descriptions in ENTITY_GUIDANCE |
| 12 | Report grounding (Claude) | Not yet done — Claude-specific, lower priority |

### Phase D: Operational Tuning (items 13-16) — DONE (except 16)

| # | Fix | Approach |
|---|-----|----------|
| 13 | Source lifecycle | "Audit source health periodically — disable sources with zero events or reliability < 0.3" in SA_GUIDANCE |
| 14 | Event-entity links | "An event without entity links is analytically invisible" in ENTITY_GUIDANCE |
| 15 | Memory promotion calibration | Tightened significance scale in REFLECT_PROMPT — "most cycles should land 0.3-0.5" |
| 16 | Cycle timeout | Not changed — evaluate after tool loop re-prompt fix (item 7) is deployed |

### Deployed

All changes deployed to GPT-OSS instance at cycle 231 (2026-03-09T05:06Z). Continuing on existing data — no reset.

---

## Appendix: Claude vs GPT-OSS Quick Reference

| Dimension | GPT-OSS (215 cycles) | Claude (18 cycles) | Winner |
|-----------|---------------------|-------------------|--------|
| Error rate | 4.5% | 0% | Claude |
| Cycle time (median) | 7.0 min | 2.4 min | Claude |
| Tool calls/cycle | 20.4 | 43.7 | Claude |
| Tools discovered | 22/50 (44%) | 29/50 (58%) | Claude |
| Analytics tools used | 0 | 4 of 4 | Claude |
| Tokens/completion* | 970 | 303 | Misleading* |
| Steps hitting max 20 | 3.5% | 69% | Claude |
| RelatedTo edges | 32% | 0% | Claude |
| Source duplicates | ~40% | 4% | Claude |
| Goal duplicates | 3 pairs | 0 | Claude |
| Event headline entities | 47 | 0 | Claude |
| Report claim verification | 100% | 62.5% | GPT-OSS |
| Cost per cycle | ~$0 (self-hosted) | ~$1.90 | GPT-OSS |
| Cost per day (continuous) | ~$0 | ~$921 | GPT-OSS |

*\*GPT-OSS's 970 tokens/completion includes Harmony reasoning tokens that are generated then stripped — the actual JSON output may be comparable to Claude's 303. Not a fair comparison.*

*GPT-OSS wins on cost and report grounding. Claude wins on everything else. The primary efficiency gap is tool loop utilization (early exits due to format parse failures), not verbosity. The goal is to close the behavioral gap through prompt engineering and targeted code fixes while keeping GPT-OSS's cost advantage.*

---

*Analysis compiled 2026-03-09. Based on run reports, code analysis, and prompt review.*
