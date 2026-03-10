# Legba Backlog

*Updated: 2026-03-10 (post V2 architecture deploy)*

---

## Deployment

| # | Item | Status | Notes |
|---|------|--------|-------|
| D1 | Push clean repo to GitHub | Ready | `/usr/local/deployments/legba/`, 4 commits, `ldgeorge85/legba` |
| D2 | Rotate Anthropic API key | Pending | Old key in pre-clean git history. Rotate at Anthropic console. |

## Agent Core — Code Fixes

| # | Item | Status | Priority | Notes |
|---|------|--------|----------|-------|
| A1 | Tool loop re-prompt on unparseable response | Done | High | API error retry (2 attempts with backoff), format retry bumped to 2 attempts. |
| A2 | Event dedup without timestamp | Done | Medium | Falls back to checking last 100 events by title similarity when no timestamp provided. |
| A3 | RSS GUID tracking | Done | Medium | `guid` column on events, GUID extracted from feeds, fast-path exact dedup before title similarity. |
| A4 | Goal dedup in goal_create tool | Done | Low | Already implemented — `_word_overlap > 0.6` check against active/paused goals. |
| A5 | `exec` tool investigation | Done | Low | Tool works correctly. Agent passed wrong arg name at cycle 192 (`cmd` instead of `command`). Tool description is clear. |

## Agent Core — Prompt Improvements

| # | Item | Status | Priority | Notes |
|---|------|--------|----------|-------|
| P1 | Entity type vocabulary in prompts | Done | Low | Canonical types added to ENTITY_GUIDANCE. Discourage `other`, `unknown`. |
| P2 | Introspection data audit prompt | Done | Medium | Added data quality audit step (section 5) to introspection: check duplicates, contradictions, use memory_supersede. |
| P3 | Source lifecycle audit prompt | Done | — | Added in cycle 231 deploy. |
| P4 | Fact predicate vocabulary in REFLECT | Done | — | PascalCase vocabulary added to REFLECT prompt. |
| P5 | Self-modification guidance in system prompt | Done | Medium | Dedicated "Self-Improvement" section: full scope, workflow (fs_read→code_test→fs_write), concrete examples, safety model. |
| P6 | Introspection self-review nudge | Done | Medium | Added self-review step (section 6) to introspection: review own code/prompts, implement improvements when concrete issues found. |
| P7 | Research cycle — entity enrichment phase | Done | High | Dedicated research cycle every 5 cycles (non-introspection). Wikipedia/reference lookups to fill entity profiles, verify graph, resolve conflicts. |
| P8 | PLAN prompt — research/enrichment guidance | Done | Medium | Added entity research encouragement, cycle variety guidance, Wikipedia API hint to PLAN prompt. |

## Data Quality — Code

| # | Item | Status | Priority | Notes |
|---|------|--------|----------|-------|
| Q1 | Fact predicate normalization | Done | — | `fact_normalize.py` with 100+ alias mappings. Applied in cycle.py and structured.py. |
| Q2 | Fact triple unique index | Done | — | `idx_facts_triple ON facts (lower(subject), lower(predicate), lower(value))`. Highest confidence wins on conflict. |
| Q3 | Fact value sanitization | Done | — | `normalize_fact_value()` strips date suffixes before storage. |
| Q4 | Source dedup (direct DB query) | Done | — | Name, normalized URL, and domain-level matching. |
| Q5 | Journal archiving to OpenSearch | Done | — | `legba-journal` index, permanent record of entries + consolidations. Backfilled cycles 60-390. |
| Q6 | Stale goal nag throttle | Done | — | Throttled to once per 50 cycles (was every cycle). |

## Architecture / Refactoring

| # | Item | Status | Priority | Notes |
|---|------|--------|----------|-------|
| R1 | cycle.py phase decomposition | Done | Medium | 2005→192 line orchestrator + 10 phase mixins in `phases/`. Deployed cycle 479, running clean. |
| R2 | V2 cycle architecture | Done | High | 5 cycle types (INTROSPECTION/ANALYSIS/RESEARCH/ACQUIRE/NORMAL) with filtered tool sets. 12 phase mixins. Deployed cycle 485. |
| R3 | Data pipeline hardening | Done | High | 3-tier event dedup, source path-prefix dedup, depth-weighted entity completeness, graph fuzzy match 500. |
| R4 | Watchlists + situations | Done | Medium | 7 new tools (watchlist 3 + situation 4), persistent patterns/narratives, full UI CRUD. |
| R5 | Ingestion tracking + journal leads | Done | Medium | Redis `last_ingestion_cycle` tracking, ingestion gap warnings, journal lead extraction + feed-forward. |

## Documentation

| # | Item | Status | Priority | Notes |
|---|------|--------|----------|-------|
| DOC1 | Failure modes / safety guarantees | Pending | Low | What happens when each service is unavailable, agent crashes, heartbeat missing. Section in OPERATIONS.md. |
| DOC2 | Ops: safe actions during active cycles | Pending | Low | Which operations are safe while agent runs vs. require supervisor stop. Add to OPERATIONS.md. |
| DOC3 | Documentation overlap cleanup | Pending | Low | README → quickstart only, DESIGN → rationale, OPERATIONS → runbooks, CODE_MAP → navigation. Trim duplication, cross-link. |

## Testing / CI

| # | Item | Status | Priority | Notes |
|---|------|--------|----------|-------|
| T1 | CI pipeline for GitHub repo | Pending | Medium | GitHub Actions: lint, unit tests, type check. Set up when D1 lands. |
| T2 | Test coverage tracking | Pending | Low | Coverage reporting, identify gaps in critical paths. |

## UI — Completed

| # | Item | Status |
|---|------|--------|
| U1 | Graph explorer — increased repulsion, degree-scaled nodes, search, cose-bilkent layout | Done |
| U2 | Facts view — `/facts` with search, confidence filter, pagination | Done |
| U3 | Memory view — `/memory` with collection toggle, semantic search | Done |
| U4 | Sources CRUD — inline status toggle, add form, delete | Done |
| U5 | Goals CRUD — status buttons (pause/complete/abandon), add form | Done |

## UI — Pending

| # | Item | Priority | Notes |
|---|------|----------|-------|
| U10 | Entity CRUD — merge duplicate entities | Medium | Reassign facts, events, graph nodes. Deferred — needs careful UX. |
| U15 | Watchlist page | Done | `/watchlist` — list, create, delete watches. Trigger history view. |
| U16 | Situations page | Done | `/situations`, `/situations/{id}` — list, create, update status, delete. Event timeline detail. |

## UI — Recently Completed

| # | Item | Status |
|---|------|--------|
| U6 | Facts CRUD — delete with confirmation | Done |
| U7 | Facts CRUD — inline edit (subject, predicate, value, confidence) | Done |
| U8 | Memory CRUD — delete episode (Qdrant delete-by-ID) | Done |
| U9 | Entity CRUD — add/remove assertions (POST/DELETE) | Done |
| U11 | Event CRUD — delete + cascade (entity links, OpenSearch) | Done |
| U12 | Event CRUD — edit metadata (category, tags, confidence) | Done |
| U13 | Graph edges — add/remove from explorer (30 relationship types, datalist) | Done |
| U14 | Source edit — full edit form (name, url, type, reliability, bias, tags, desc) | Done |

## Data Curation — Manual

| # | Item | Status | Notes |
|---|------|--------|-------|
| C1 | Source dedup cleanup | Done | 149 → 81 sources. 68 duplicates removed, 9 events reassigned. |
| C2 | Fact cleanup — predicate normalization | Done | 8 variant forms normalized. |
| C3 | Fact cleanup — dedup | Done | 1,927 → 1,157 facts. 770 duplicates/junk removed. |
| C4 | Fact cleanup — incorrect assertions | Done | Israel HostileTo US, Iran HostileTo Hezbollah removed. |
| C5 | Entity cleanup — junk entities | Done | Event titles, vague entries removed. |
| C6 | Entity cleanup — merge near-duplicates | Done | Netanyahu→Benjamin Netanyahu, Gaza Strip→Gaza, Russian Federation→Russia, Gulf variants deleted. |
| C7 | Event dedup | Done | 334 → 215 events. 119 title-duplicates removed. |
| C8 | Goal dedup | Done | 22 → 16 goals. 6 duplicate completed goals removed. |
| C9 | Paused source review | Done | 44 paused → 43 deleted (0 events, never worked), 1 reactivated (Al Jazeera Africa, 14 events). 81 → 38 sources total, all active. |

## Feedback Reviewed — Declined

Items considered from external reviews and deliberately not pursued:

- **Prometheus/Grafana monitoring** — heavy infrastructure add, audit logs + UI dashboard sufficient at this scale
- **Secrets manager (Vault/etc.)** — .env is fine for single deployment
- **Contributor quickstart doc** — single-developer project, README covers it
- **"Lite" mode (SQLite)** — no use case currently
- **Dependency grouping docs** — pyproject.toml is clean enough
- **Configuration doctor command** — over-engineering
- **Add docstrings everywhere** — don't touch code you didn't change
- **Journal tone toggle (narrative vs clinical)** — narrative voice IS the feature
- **Self-modification as a "strength"** — infrastructure exists but was never successfully used; prompting is the fix (P5, P6)
