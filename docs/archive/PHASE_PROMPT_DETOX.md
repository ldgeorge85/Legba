# Phase: Prompt Detox

**Status**: Complete
**Started**: 2026-03-05

## Problem

The agent died after 5 consecutive heartbeat failures (cycles 409-413). Root cause: the Knowledge Graph Inventory injection from the project-cataloging era was still active, injecting software-project attributes (UsesArchitecture, UsesPersistence, HasSafety, FundedBy) into prompts. The agent was treating UNICEF humanitarian campaigns as software projects and grinding on filling nonsensical fields, producing garbled output and collapsing completions.

The model itself is fine. The infrastructure is fine. The prompt context was poisoned with cataloging-era framing that contradicts the SA mission identity.

## Task Tracking Matrix

### Phase 1: Kill the Project Inventory
| # | Task | Status | Notes |
|---|------|--------|-------|
| 1.1 | Remove project inventory builder from cycle.py (lines 271-381) | DONE | Replaced with entity type counts + relationship summary |
| 1.2 | Replace with entity-type counts + relationship type counts | DONE | No more "fill these gaps" table or completeness columns |
| 1.3 | `graph_inventory` param kept in assembler.py | DONE | Repurposed for new graph summary |
| 1.4 | Remove cycles_since_new_project from cycle.py callers | DONE | Removed from reflection_forward, _mission_review, persist |

### Phase 2: Rewrite Prompts for SA
| # | Task | Status | Notes |
|---|------|--------|-------|
| 2.1 | Rewrite PLAN_PROMPT — remove project references, reframe for SA | DONE | New example uses RSS feeds, events, entity resolution |
| 2.2 | Rewrite MISSION_REVIEW_PROMPT — remove cycles_since_new_project | DONE | Removed metric, updated mission coverage question for SA |
| 2.3 | Rewrite MEMORY_MANAGEMENT_GUIDANCE — SA relationship types first | DONE | SA geopolitical types listed first, general second |
| 2.4 | Update reflection_forward builder in cycle.py — remove cycles_since_new_project | DONE | Removed Project node query and field |
| 2.5 | Update REFLECT_PROMPT example — replace AutoGPT/Project with SA example | DONE | Now uses Reuters/events/entity resolution example |
| 2.6 | Update PLAN_PROMPT example — replace BabyAGI with SA example | DONE | Now uses source portfolio/feed parsing example |

### Phase 3: Clean Canonical Types
| # | Task | Status | Notes |
|---|------|--------|-------|
| 3.1 | Reorder CANONICAL_RELATIONSHIP_TYPES in graph_tools.py — SA types first | DONE | SA geopolitical first, technical last with "backwards compat" note |
| 3.2 | Update RELATIONSHIP_ALIASES — no removal, just reorder comments | SKIPPED | Aliases are fine as-is, normalization still works |

### Phase 4: Documentation Refresh
| # | Task | Status | Notes |
|---|------|--------|-------|
| 4.1 | Rewrite docs/LEGBA.md as single consolidated system spec | DONE | 13 sections, full scope, current |
| 4.2 | Archive stale docs to docs/archive/ | DONE | ARCHITECTURE.md, PHASE12_LLM_TUNING.md, PROMPT_DUMP.md |
| 4.3 | Update MEMORY.md | DONE | |

### Phase 5: Deploy and Verify
| # | Task | Status | Notes |
|---|------|--------|-------|
| 5.1 | Clear agent code volume | DONE | |
| 5.2 | Rebuild agent image | DONE | agent + supervisor rebuilt |
| 5.3 | Restart supervisor | DONE | Resumed from cycle 413 |
| 5.4 | Monitor first 5 cycles for coherent output | DONE | Cycles 414-416 all heartbeat OK. Cycle 416: "Added three high-trust RSS feeds (Al Jazeera, Reuters, UN OCHA)...indexed five news events." Agent doing real SA work. |
