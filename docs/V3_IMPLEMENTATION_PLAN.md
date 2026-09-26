<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Data model v3 — implementation plan

**Companion to** [`DATA_MODEL_V3.md`](DATA_MODEL_V3.md), which is the specification. This file
is the build order: who builds what, in what sequence, what each phase must prove against the
running stack before the next one starts, and what the orchestrator checks.

**Execution model.** Each phase is one external agent session in its own git worktree. The
orchestrator — Claude Code in this repo — reviews the diff, merges, rolls, and proves each
phase live. **Agents do not merge, do not register, do not roll and do not push.**

---

## 0 · House rules — paste this block verbatim into every agent's dispatch

> **Setup.**
> * Work in a git worktree off a **pinned SHA**. The pinning rule: the SHA is the
>   orchestrator's `main`-line HEAD at dispatch time, quoted in your dispatch message. Create it
>   with `git -C /usr/local/deployments/active/legba worktree add .worktrees/<lane> -b <lane>
>   <SHA>`, then prove it with `git -C .../.worktrees/<lane> log -1` before you write anything.
>   If the dispatch message does not name a SHA, stop and ask.
> * **`PYTHONPATH=src`** on every `pytest` / `python` invocation. A worktree's `pytest` will
>   otherwise import the MAIN checkout through the editable-install `.pth`, and you will be
>   testing code you did not write. **Prove your import path once, at the start**, and paste the
>   output: `PYTHONPATH=src python -c "import legba, sys; print(legba.__file__)"` — it must
>   point inside *your worktree*.
> * `seeds/` is gitignored and does not exist in a worktree. Before running tests:
>   `ln -s /usr/local/deployments/active/legba/seeds/world_baseline.yaml <worktree>/seeds/world_baseline.yaml`
>   (create `seeds/` first). Without it, seed/export-import tests fail in a worktree and pass on
>   main, and you will chase a ghost.
>
> **Prohibitions — all hard.**
> * **Never `git stash`.** This is a shared multi-session working directory; a stash can eat
>   another session's work. Commit instead, always.
> * **Never touch the main checkout.** Read it if you must; write only inside your worktree.
> * **Never `git add planning/`.** It is gitignored on purpose. Read it from the main checkout
>   path; never copy it into a commit.
> * **Never push.** Push is operator-initiated only, and only for the request that says so.
> * **No commit trailers of any kind.** No `Co-Authored-By`, no `Generated with`, no session
>   link. A plain subject and body.
> * **No writes to the live database, no descriptor registration, no deploy.** Read-only
>   `SELECT` against the live DB is allowed for verification, from the MAIN checkout directory:
>   `docker compose exec -T postgres psql -U legba -d legba -At -c "SELECT …"`. Nothing else.
>
> **Code rules — all hard.**
> * **No stubs.** A stub, mock, fake, placeholder or echo implementation in `src/**` is
>   forbidden (`tests/test_no_undeclared_stubs.py` enforces it and there is no per-line pragma
>   escape). Anything not built must be a **declared seam** in `docs/SEAMS.md` — a narrative row
>   *and* an allowlist line — and must **fail loud / refuse activation** at its guard rail. The
>   next free seam number is **63**.
> * **Any descriptor you write or edit carries `budget_tokens_per_day: 0` and, wherever an LLM
>   route is declared, `temperature: 1.0`.** No exceptions. The core plane never sends
>   `max_tokens`.
> * **Registry-served modules import leaf modules only.** The registry ships a *slim* image; a
>   route module that pulls `legba.data.analysts` (which drags `pycountry` and more) 500s live
>   even when the import is deferred inside a function. Every new `*_api.py` gets a
>   poisoned-import guard test in the same commit.
> * **Every new route is exercised by a test that traverses the real binding path** — through
>   the router factory and the deps bundle, not by calling the handler function directly. For a
>   new pack tool, the equivalent is a test that goes through `AgencyToolBinding.run_tool` →
>   `Agency.run_pack_tool` → the registered handler, so the `action_pack_invocations` ledger
>   actually records it.
> * **The module-size gate (`tests/test_module_size_gate.py`) is honoured by SPLITTING, never by
>   raising a ceiling.** Raising a number is a visible, reviewable act and the orchestrator will
>   reject it. Extract a cohesive unit into a sibling module instead — the section banners in the
>   file are the author's own seams.
> * Migrations live in `src/legba/data/migrations/` and are globbed-and-sorted by filename
>   (`migrate.py:_discover()`); there is no manifest and no head constant. **Use exactly the
>   number this plan reserves for your phase** — another lane may be holding the next one.
>
> **Testing.**
> * A full-suite run is **not** required per phase. Required: the test files you touched, the
>   test files for the modules you touched, plus `tests/test_module_size_gate.py` and
>   `tests/test_no_undeclared_stubs.py`. The orchestrator runs the whole suite once on the
>   merged tree.
> * **There are no known pre-existing failures.** The list that stood here —
>   `test_consult_cost_and_synthesis`, `test_native_tool_rounds`, `test_voice4_flip_kit`
>   (date-rot), and the daprd / NATS / webhook end-to-end errors — was root-caused and
>   cleared on 2026-09-26 (wave O, lane o6): three were the live `.env` leaking unpinned
>   `LEGBA_CONSULT_*` knobs into the suite, one was a FastAPI 0.140 `include_router` change
>   that broke a test's own route introspection, seven were two unrelated features drifting
>   two byte-identity goldens, and the eight "errors" were honest skips whose reason text the
>   strict classifier read as a broken rig. **A failure you see now is yours.** If you believe
>   otherwise, root-cause it and say so in your report — do not add it back to a list here.
>
> **Deliverable.** Commits on your branch, and a report naming: the branch and final SHA, every
> file added or changed, the migration number(s) you used, the tests you added and their
> results, the deploy markers you stamped, any SEAMS entry you declared, and anything you could
> not verify (say so plainly rather than guessing).

---

## 0.1 · Decisions taken (settled — do not re-open in a phase)

The spec's §8 put eight questions to the operator with a recommendation each. All eight were
accepted as recommended. Agents build against these; a phase that needs one of them reversed
stops and reports rather than deciding.

| # | question (spec §8) | decision | where it binds |
|---|---|---|---|
| Q1 | the AGE probe | **accept the 2026-08-03 negative verdict; do not arm the mirrors; no re-probe** | P4b (no AGE path anywhere) |
| Q2 | the projection target | **build `graph_arcs`** (spec §4.2) | P4b |
| Q3 | the scale branch with Neo4j excluded | **NebulaGraph recorded as the fallback; E1/E4 firing re-opens the question, never auto-buys** | the 2026-11-03 sitting (P4a gauges) |
| Q4 | a fifth `source_class` | **no new class until a non-public source exists**; per-link machinery built regardless | P0/P2 |
| Q5 | the inquiry kind's grants | **first grant = `substrate_read` (incl. `query_events`, `inspect_event`, `belief_as_of`) + `journal_read`; NOT `web_access`** — web later with its own tighten-only governor | P6 (the directed-read grant is the narrow one) |
| Q6 | the hand-check worksheet / typer model | **the typer model is declared frozen at `core120b`**; the worksheet stays optional and still gates any future model change | P5 (no live option changes) |
| Q7 | event↔event causal typing | **deterministic edges only; `caused_by` / `contradicts` declared as SEAMS #56 with a raising guard** | P1 |
| Q8 | the backfill ceiling | **`_BACKFILL_MAX_EVENTS = 5000`; the migration refuses above it** | P0 (0205) |

Companion decisions outside v3, binding in the same way: the grader daily ceiling stays
`LEGBA_GRADER_DAILY_CEILING_USD=2.50` as a ceiling, and the composition correctness gate stays
**OFF** until correctness carries coverage worth gating on.

---

## 1 · Wave map

```
 WAVE 1   P0  events schema + write path + backfill          (L)   ─┐
          P4a trigger gauges + invocation duration           (S)    │  parallel
                                                                    │
 WAVE 2   P1  candidates, clustering, lifecycle, reactivation (L)  ─┤
          P2  provenance: the event ref kind                  (M)   │  parallel
          P3  temporal readers (as_of / belief / edge ledger)  (M)   │  (P3 owns
          P5  typing throughput: the decisive comparison       (S)   │   the port file)
                                                                    │
 WAVE 3   P4b graph projection (graph_arcs) + kill switch      (M)  ─┤  parallel
          P6  consumers: map / timeline / situations / grants  (M)   │
          P7  provenance: origin-class columns + firewall    (S)   │  (after P1)
```

**Dependencies, stated:**

* **P0 gates P1, P2, P4b and P6** — they all read the `events` tables.
* **P3 gates P6.** Both edit `src/legba/runtime/substrate_query_port.py` and the five places a
  tool parameter touches. P3 goes first *and* performs the module extraction the size gate
  forces (§P3), which is what makes P6's additions fit.
* **P4a gates nothing** and is independent of everything — it touches
  `action_pack_invocations` and one gauge route. It is in wave 1 only because it is small and
  the 2026-11-03 sitting needs it.
* **P5 is fully independent.** It touches the reifier's receipt and a measurement script; no
  other phase reads those.
* **P4b can ship without event arcs** if P0 slips: the projector is per-source and the event
  sources are two of ten. Prefer waiting.
* **P7 waits for P1** (both touch `data/provenance/writes.py`); it is independent of P4b and P6 and
  rides beside either. It is the first piece of the checking-layer arc (Program 7) pulled forward,
  because backfilled history must never be able to enter the substrate before the readers can tell
  it from the present.

**The platform is deployable after every phase.** Every v3 writer ships behind a flag that is
off, every new parameter defaults to absent, and every new table lands empty.

---

## 2 · The phases

### P0 — events: schema, write path, backfill. No consumer.

**Size: L.** **Wave 1.** Reserves migrations **0202, 0203, 0204, 0205**.

**Goal.** The `events` tables exist, `write_analyst_output` can route an `EVENT` payload to
them, the merge fold repoints event actors, and a bounded backfill from the tower has run.
Nothing reads events yet.

**Why it stands alone.** Every table lands empty; `LEGBA_EVENTS` is off, so no writer runs; the
only behavioural change with the flag off is that `OutputKind` has a 14th member, which is why
the registry is rebuilt first (§orchestrator checklist).

**Read first, in this order.**
`docs/DATA_MODEL_V3.md` §2 and §7 · `src/legba/data/provenance/kinds.py` (the `OutputKind`
enum at `:66`, `OutputKindSpec` at `:489`, `KIND_REGISTRY` at `:523`, `register_kind` at `:648`)
· `src/legba/data/provenance/models.py` (`SituationPayload` at `:509` — the closest shape) ·
`src/legba/data/provenance/writes.py` (`write_analyst_output` at `:297`, `_insert_situation` at
`:841` including its `ON CONFLICT` at `:876` and the NULL-`analyst_id` guard at `:858`) ·
`src/legba/data/migrations/0184_situation_events.sql` (the append-only ledger contract you are
copying) · `src/legba/data/migrations/0143_entity_edges.sql` (`resolve_entity_name()`,
`fold_entity_edges()`, and the header's tone for a backfill expectation) ·
`src/legba/data/situations/trajectory.py` (the FSM module shape) ·
`src/legba/data/migrations/0144_backfill_entity_edges_from_nexuses.sql` (the park-don't-drop
backfill idiom).

**Tasks.**

1. **0202_events.sql** — `events`, `signal_event_links`, `event_entity_links` + the indexes in
   spec §2.1. `analyst_id` is `NOT NULL` (NULLs are distinct in a unique index; make the
   `writes.py:858` guard structural). No FK on `signal_event_links.signal_id` — deliberate,
   and say so in the header.
2. **0203_event_edges.sql** — `event_edges`, `situation_event_links`. Add the CHECK that
   `correlated_with` / `contradicts` rows are stored canonically (`src_event_id < dst_event_id`).
3. **0204_event_lifecycle_events.sql** — the ledger, the two CHECK vocabularies, the
   evidence-requires-transition CHECK, and the `BEFORE DELETE` / `BEFORE UPDATE` trigger pair.
   Copy 0184's function shape; do not invent a new one.
4. **`src/legba/data/events/lifecycle.py`** (new leaf) — the vocabulary constants, the
   `next_state(...)` pure function, the `LifecycleEvent` frozen dataclass validated at
   construction. Model it line-for-line on `src/legba/data/situations/trajectory.py`: raise
   `EventTransitionError` rather than coerce; the DB CHECK mirrors the Python frozensets.
   **No I/O in this module.**
5. **`src/legba/data/events/signature.py`** (new leaf) — the `evt:<topic>|<entities>#evt:<anchor>`
   builder, using `_entity_canon.identity_fold`, `is_junk_entity`, and
   `_frame_anchor.anchor_token`. **Ship the Postgres twin as a SQL constant in the same module**
   and a test asserting Python and SQL agree row-for-row, exactly as
   `test_anchor_token_python_and_sql_agree` does for `ANCHOR_TOKEN_SQL`. The backfill in 0205
   is SQL and the handler is Python; the twin is the only thing keeping them honest.
6. **`OutputKind.EVENT` + `EventPayload` + a `KIND_REGISTRY` entry** routing to `events`, and
   `_insert_event` in `writes.py` carrying the `ON CONFLICT (event_signature, analyst_id)`.
   Do **not** add a fourth bypass of `write_analyst_output`.
7. **`fold_event_entity_links(uuid)`** — a SQL function called from
   `entity_researcher.merge_pair` (`:568`) **inside the transaction that sets `merged_into`**,
   immediately beside the `fold_entity_edges()` call at `:638` — and, like it, **after** the
   `merged_into` UPDATE, so the fold's `resolve_entity()` read sees the redirect. Repoint to `resolve_entity()`'s terminal survivor, collapse
   duplicates, return the fold count into the receipt. Without this, events strand on
   tombstones exactly as 10,646 `proposed_edges` rows did.
8. **0205_backfill_events_from_tower.sql** — two paths, spec §7.2. **Dry-run first**: run the
   counting half, put the measured numbers in the migration header, then commit the writing
   half. Path 1 (`situation_events`, 1,436 evidence-bearing rows as of 2026-09-21) is bounded
   by the data. Path 2 (findings) is bounded by: 30 days · `effective_confidence >= 0.50` ·
   ≥2 resolvable signals · collapsed by signature · **`_BACKFILL_MAX_EVENTS = 5000`, above
   which the migration RAISES with its counts rather than writing**. Park unresolvable
   candidates; never drop them.
9. **Flag** `LEGBA_EVENTS`, default off, read in one place, `LEGBA_AGE_DERIVED_FROM`'s
   `_age_derived_from_enabled()` (`runtime/dapr_actors.py:794`) as the pattern.

**Tests to add.**
`tests/data_pkg/test_event_lifecycle.py` (every transition, every refusal, idempotence) ·
`tests/data_pkg/test_event_signature.py` (incl. the Python/SQL twin) ·
`tests/data_pkg/test_event_writes.py` (the upsert key; the NULL-`analyst_id` refusal; a
`derived_from` union) · `tests/data_pkg/test_event_ledger_immutability.py` (an `UPDATE` and a
`DELETE` each raise) · `tests/test_module_size_gate.py`, `tests/test_no_undeclared_stubs.py`.

**Deploy markers** (for `--verify-grep`):
`data/events/lifecycle.py:EVENTS_LIFECYCLE_VERSION = "2026-09/p0"` ·
`data/provenance/kinds.py:EVENT = "event"` ·
`data/provenance/writes.py:_insert_event`

**Live acceptance proof** (orchestrator runs, after the roll):

```bash
# 1. the tables exist and the backfill landed the number the header claims
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT source_method, count(*) FROM events GROUP BY 1 ORDER BY 1;"
#    expect: tower = the dry-run number from 0205's header, ±0. clustering = 0.

# 2. every backfilled event opened its ledger
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT (SELECT count(*) FROM events) = (SELECT count(*) FROM event_lifecycle_events
      WHERE transition='opened') AS opened_matches;"
#    expect: t

# 3. the ledger is genuinely append-only (this MUST error)
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "UPDATE event_lifecycle_events SET why='tamper' WHERE true;"
#    expect: ERROR — situation_events-style mutation trigger

# 4. nothing is writing events live (the flag is off)
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FROM events WHERE created_at > now() - interval '10 minutes'
     AND source_method='clustering';"
#    expect: 0

# 5. every kind still activates — the new-OutputKind check
docker compose logs legba-runtime-dapr --since 10m | grep -c reconcile.failed
#    expect: 0
```

**Flags / descriptors to register.** `LEGBA_EVENTS=0` in `.env` (explicitly, not by absence).
No descriptor yet.

**Rollback.** `LEGBA_EVENTS=0`; `DROP TABLE` in reverse dependency order; the backfill is
`DELETE FROM events WHERE source_method='tower'` and cascades take the links.
`OutputKind.EVENT` stays (harmless — a declared kind nothing writes).

---

### P1 — candidates, clustering, lifecycle, dedup, reactivation

**Size: L.** **Wave 2.** No new migration.

**Goal.** `event_clustering` runs on a cadence and produces events from both sources, maintains
lifecycle for every open event, writes a ledger row on every transition, and reactivates a
resolved event when a new signal links.

**Why it stands alone.** The handler is `state: draft` and `LEGBA_EVENTS` stays off until the
orchestrator flips it; with the flag off the descriptor is inert.

**Read first.**
`DATA_MODEL_V3.md` §2.2-§2.4 ·
`src/legba/data/analysts/deterministic_handlers/situation_clustering.py` — the whole shape you
are copying: signature grouping (`:740`), the two status clocks (`_situation_status`, `:594`),
the direct upsert with `RETURNING (xmax = 0) AS inserted` (`:846`), `_DEFAULT_LOOKBACK_DAYS`
(`:106`) · `src/legba/data/analysts/situation_tracker.py` + `descriptors/analyst_situation_tracker.yaml`
(the META-analyst descriptor shape, the watermark idiom on `alert_trigger_watermarks`, and the
"first run seeds silently" rule) · `src/legba/data/filters/dedupe.py` (the 4-tier dedupe:
`normalized_title` at `:771`, `Tier3Config` at `:190`, `Tier4Config` at `:198`) ·
`src/legba/data/analysts/deterministic_handlers/cross_source_dedup.py` (`:175-217` — **read the
degenerate-embedding rationale before you use cosine at all**) ·
`cross_source_coalesce.py` (`_coalesce_pairs` at `:194`, the union-find, `_titles_close`) ·
`src/legba/data/_entity_canon.py` (`identity_fold` `:2114`, `is_junk_entity` `:1214`,
`differs_by_direction` `:1719`) · the retired v1 cognitive-architecture design defect **B1**.

**Tasks.**

1. **`src/legba/data/analysts/deterministic_handlers/event_clustering.py`** — the ONE event
   writer. Per tick: (a) cluster the new signal slice; (b) match each cluster against **open
   events** (not against a signal lookback — spec §2.2, the re-attachment rule); (c) upsert;
   (d) recompute lifecycle for every open event and write ledger rows on transition.
   Split into siblings before the module reaches 1,500 lines — the gate's entry threshold.
2. **The matcher** — March's four features at March's weights and threshold, plus the two new
   terms as **additive gates measured separately**. Cosine goes through Qdrant `legba_signals`
   **with `cross_source_dedup`'s structural gate** (`embedding_ref ~ <uuid regex>`); without it
   60.8 % of high-cosine pairs are degenerate.
3. **The mega-bucket guard** — `_MAX_CLUSTER_MEMBERS = 30`; hitting it sets `oversized=true`,
   **refuses promotion**, and increments a receipt counter. Do not truncate. This is the
   explicit fix for B1.
4. **The promotion bar** — ≥2 distinct `source_id`, or one `source_class='official'` (read from
   `source_descriptors.body->'scope'->>'source_class'`, the extraction
   `signal_salience.py:614` already does).
5. **Tower candidates** — verify-passed findings citing ≥2 resolvable signals, and
   evidence-bearing `situation_events` rows. Same signature, so a collision upserts.
6. **The deterministic reconciler** (`event_reconciler.py`) — `correlated_with` between two
   producers' rows on one signature; `evolves_from` between temporally adjacent events sharing
   ≥K actors. **`caused_by` and `contradicts` are NOT produced**: declare **SEAMS #56** and make
   `write_event_edge` raise for those two types with the seam reference. A CHECK that admits a
   type nothing writes is not a guard rail.
7. **Descriptors** — `descriptors/analyst_event_clustering.yaml` and
   `analyst_event_reconciler.yaml`, both `state: draft`, `budget_tokens_per_day: 0`,
   `temperature: 1.0` on any LLM route, a cadence clear of `situation_clustering`'s
   `5-59/20` and `situation_tracker`'s `41 * * * *`.
8. **Receipt funnel** — `examined / clustered / oversized / promoted / declined_single_source /
   reattached / transitions_by_kind`, on the trace payload.

**Tests to add.**
`tests/data_pkg/test_event_clustering.py` (a two-source cluster promotes; a single-source
cluster does not; an oversized cluster is refused **and counted**; a September signal
re-attaches to a March event and writes a `reactivated` row) ·
`tests/data_pkg/test_event_matcher.py` (identity-fold overlap; the compass gate refuses
*North*/*South* stems; the degenerate-embedding gate) · the gate tests.

**Deploy markers.**
`data/analysts/deterministic_handlers/_event_matcher.py:EVENT_CLUSTERING_VERSION: str = "2026-09/p1"` (the
constant lives in the matcher sibling and is re-exported by `event_clustering.py`) ·
`data/analysts/deterministic_handlers/event_reconciler.py:SEAMS #56`

**Live acceptance proof.**

```bash
# force one run of the clusterer (flag ON, descriptor promoted to active)
#   then:
docker compose exec -T postgres psql -U legba -d legba -At -F'|' -c \
  "SELECT source_method, lifecycle_state, count(*) FROM events GROUP BY 1,2 ORDER BY 1,2;"
#    expect: a nonzero clustering/emerging population that did not exist before the run

docker compose exec -T postgres psql -U legba -d legba -At -F'|' -c \
  "SELECT transition, count(*) FROM event_lifecycle_events
    WHERE created_at > now() - interval '1 hour' GROUP BY 1;"
#    expect: 'opened' > 0; any 'advanced'/'resolved' rows consistent with the run's funnel

docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FROM events WHERE oversized;"
#    expect: a number, reported. NONZERO IS NOT A FAILURE — it is the tuning signal.
#    Nonzero AND large (>10% of clusters) means the threshold is wrong: report, do not ship on.

docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FROM events e WHERE NOT EXISTS
     (SELECT 1 FROM signal_event_links l WHERE l.event_id = e.id);"
#    expect: 0 — the orphan-event class March shipped

docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT output_payload->'data'->'funnel' FROM analyst_traces
    WHERE analyst_id='event_clustering' ORDER BY run_ended_at DESC LIMIT 1;"
#    expect: the eight counters, non-null
```

**Rollback.** `LEGBA_EVENTS=0`; retire both descriptors (`POST /descriptors/analyst/{id}/retire`
— removing the file from the bringup script is **not** a retirement for a reactive analyst).

---

### P2 — provenance: the event ref kind

**Size: M.** **Wave 2.** No new migration.

**Goal.** A finding can cite `event:<uuid>`; the citation builder expands it to per-signal
entries carrying real `source_text`; the judge grounds on those; the event's own summary never
reaches a grader.

**Why it stands alone.** `LEGBA_EVENT_CITATIONS` off ⇒ no analyst emits an event citation and
nothing changes. `verify.py` is not modified at all.

**Read first.**
`DATA_MODEL_V3.md` §2.5 · `src/legba/data/analysts/inline_target.py` `_citation_entry` (`:1262`)
and the precedence chain at `:1313-1323` — **this is the faithfulness trust boundary; read why
`distilled_body` is excluded before you touch it** — and `_SOURCE_TEXT_CHARS` (`:378`) ·
`src/legba/data/provenance/judge_evidence.py` (`_marker_to_evidence` `:126`, the `source_text`
read at `:171`, the render at `:200-227`) · `src/legba/data/provenance/kinds.py`
`GROUNDING_REF_KINDS` (`:451`) and `is_grounding_citation` (`:467`) ·
`src/legba/data/analysts/composition_citations.py` (the `[[ref:N]]` shape) ·
`src/legba/data/registry/lineage_api.py` (`_SUBSTRATE_TABLES`).

**Tasks.**

1. **`src/legba/data/provenance/event_citations.py`** (new leaf, stdlib + asyncpg only) —
   `expand_event_citation(conn, event_id, *, start_ordinal, max_signals)` returning ordinary
   citation dicts: `signal_id`, `source_text` (same precedence, same 3,200-char cap),
   `ref_kind="event"`, `event_id`, and `event_expansion_truncated` when the event has more
   members than the cap. Ordered by `relevance DESC, linked_at DESC`.
2. **Wire it at citation-build time** in `inline_target` and `composition_citations`. **Do not
   modify `verify.py`.** If you find yourself editing `verify.py`, the design has gone wrong —
   stop and report.
3. **`event` is NOT added to `GROUNDING_REF_KINDS`.** Add a test that asserts it is absent and
   a comment in `kinds.py` saying why (an expanded entry carries a `signal_id`, so
   `is_grounding_citation` correctly returns False at `:477`; adding it would let an event be
   grounded on its own summary).
4. **`derived_from` carries both** the event id and the expanded signal ids.
5. **Lineage** — add `events` to `lineage_api._SUBSTRATE_TABLES`. `journal_entries` stays out.
6. **Flag** `LEGBA_EVENT_CITATIONS`, default off. **`LEGBA_EVENT_EXPANSION_MAX_SIGNALS`,
   default 4.**

**Tests to add.**
`tests/data_pkg/test_event_citations.py`: an event citation expands to N signal entries each
with non-empty `source_text` · the expansion respects the cap and stamps the truncation flag ·
**the negative test that matters: an `events.summary` string never appears anywhere in a
rendered evidence envelope** · a finding citing an event scores `supported` on the
deterministic floor when its signals resolve, and `unresolved_citation` when they do not ·
`event` is not in `GROUNDING_REF_KINDS`.

**Deploy markers.**
`data/provenance/event_citations.py:EVENT_CITATION_VERSION = "2026-09/p2"` ·
`data/registry/lineage_api.py:"events"`

**Live acceptance proof.**

```bash
# with the flag on and one analyst granted event citations, after its next run:
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FROM analyst_outputs o
    WHERE o.kind='finding' AND o.data->'data'->'citations' @> '[{\"ref_kind\":\"event\"}]';"
#    expect: > 0

# the judge graded it, and graded it on SIGNALS
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT c.title, c.data->'verification'->'counters'
     FROM analyst_outputs c WHERE c.kind='critique'
      AND c.data->>'analyzed_output_id' = '<the finding id>';"
#    expect: a Faithfulness verify row with checkable_claims > 0 and supported > 0

# the negative proof: the event summary is not in the envelope
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FROM analyst_outputs o, events e
    WHERE o.kind='finding' AND e.summary <> ''
      AND o.data::text LIKE '%' || e.summary || '%';"
#    expect: 0

# lineage resolves an event node
curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/lineage/<the finding id>" | grep -c '"kind":"event"'
#    expect: >= 1
```

**Rollback.** `LEGBA_EVENT_CITATIONS=0`. Already-written citations keep resolving, because every
expanded entry is an ordinary signal entry.

---

### P3 — the temporal readers

**Size: M.** **Wave 2.** Reserves migration **0206**.

**Goal.** `as_of` on the validity-time tools, `believed_as_of` on findings, `since`/`until` on
`get_timeline`, a `belief_as_of` reader, and the append-only edge transition ledger.

**Why it stands alone.** Every parameter defaults to `None`, and `None` runs today's SQL. The
ledger is flag-gated off.

**Read first.**
`DATA_MODEL_V3.md` §3 (all of it — the two-clock rule and the "an as-of read must NOT apply the
open-row predicate" rule are the two things this phase gets wrong if it skims) ·
`src/legba/runtime/substrate_query_port.py` (`query_facts` `:547`, `query_nexuses` `:1235`,
`_PATH_WALK_SQL` `:318`, `_BROKER_WALK_SQL` `:363`, `get_timeline` `:1828`, `list_situations`
`:1563`, `list_findings` `:1456`) · `src/legba/data/provenance/entity_edge_writes.py`
(`close_prior_entity_edges` `:209`, the close at `:231`, `_INSERT_SQL` `:248`, the `DO UPDATE`
at `:271`, the link-back at `:394`) · `src/legba/data/analysts/agency/substrate_read.py`
(`_call_port` `:102`) · `src/legba/data/stack/llm/tool_rounds.py` (`TOOL_SCHEMAS` `:243`,
`_fallback_spec` `:454`, `is_write_tool` `:444`) ·
`src/legba/data/registry/substrate_reads_api.py` (the `/situations` route at `:946` — direct
SQL, and the curl proof below goes through it) · `tests/test_module_size_gate.py:266`.

**Tasks.**

1. **Extract first, then add.** `substrate_query_port.py` is **3,761 lines against a ceiling of
   3,910** — 149 lines of headroom, and this phase will exceed it. Create
   `src/legba/runtime/substrate_temporal.py` (leaf): the canonical as-of predicate builder, the
   ISO-8601 parse-or-refuse, the `unbounded_start` counter. Extract, re-run the gate, *then*
   add parameters. **Do not raise a ceiling.**
2. `as_of` on `query_facts`, `query_nexuses`, `query_paths`, `find_proxy_chains`,
   `query_brokers`, `list_situations`. The walks apply it **per hop, on both the seed and the
   recursive arm**.
3. `since` / `until` on `get_timeline`; `believed_as_of` on `list_findings`.
4. **All five surfaces, every time** — port, Protocol stub in `consult_on_demand.py`,
   `_call_port` coercion, `TOOL_SCHEMAS`, and the three prose catalogues
   (`gather_surface.py:75`, `consult_on_demand.py:583`, `journal_assessor.py:2282`).
   **While you are there, close the two incidental defects** (both pre-existing): add JSON
   schemas for `query_paths`, `find_proxy_chains`, `query_brokers`, `list_targets`,
   `list_sources` so `is_write_tool` stops classifying three read-only graph walks as writes;
   and add the missing `families` parameter to the three Protocol stubs (`:421/:431/:441`).
5. **`belief_as_of`** — the tool and the `/api/v1/v3/belief` route, spec §3.4. Two folds
   (`as_of` default, `latest`), the fold stamped on the response, `verdict_pending_at_as_of`
   counted, **no pooled single number**.
6. **`as_of` on `GET /api/v1/v3/situations`** (`substrate_reads_api.py:946`) — this is what
   makes the acceptance proof a curl instead of a forced analyst run.
7. **0206_entity_edge_events.sql** + the write inside `entity_edge_writes`, **in the same
   transaction as the upsert**, gated by `LEGBA_EDGE_TRANSITION_LEDGER` (default off).
   Transitions only, not observations — spec §3.5. Split `written` into `inserted` / `folded`
   with `RETURNING (xmax = 0) AS inserted` while you are in the file.
8. State in `0206`'s header that the ~8,872 already-folded re-observations are **not
   recoverable** and that `facts` / `nexuses` / `situations` do **not** get this property.

**Tests to add.**
`tests/runtime/test_substrate_temporal.py` (the predicate; NULL `valid_from`; a malformed
`as_of` refuses loud rather than defaulting to `now()`) · `tests/runtime/test_as_of_readers.py`
(**the one that matters: an as-of read returns a row that is superseded today** — proving the
open-row predicate was dropped) · `tests/data_pkg/test_belief_as_of.py` (both folds; the
pending-verdict count) · `tests/data_pkg/test_entity_edge_ledger.py` · a real-binding-path test
for the new route · `tests/test_module_size_gate.py` (must pass **without** a ceiling edit).

**Deploy markers.**
`runtime/substrate_temporal.py:TEMPORAL_READER_VERSION = "2026-09/p3"` ·
`data/registry/belief_api.py:build_belief_router` ·
`data/provenance/entity_edge_writes.py:entity_edge_events`

**Live acceptance proof.**

```bash
# pick a date before a known situation close
docker compose exec -T postgres psql -U legba -d legba -At -F'|' -c \
  "SELECT id, status, valid_from, valid_until FROM situations
    WHERE valid_until IS NOT NULL ORDER BY valid_until DESC LIMIT 3;"

# the as-of read must return that situation AS IT WAS — i.e. a row that is closed today
curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/situations?as_of=<a date inside that row's validity>&limit=50" \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; \
      print(len(d), any(x['id']=='<that id>' for x in d))"
#    expect: a count, and True. Without as_of the same id must be ABSENT.

# belief-as-of: the fold is stamped and pending verdicts are counted, not hidden
curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/belief?as_of=2026-08-15T00:00:00Z&target_id=country_g20_us" \
  | python3 -m json.tool | head -30
#    expect: fold_verdicts="as_of"; a verdict_pending_at_as_of count >= 0; NO pooled score

# byte-identity with the parameter absent
curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/situations?limit=50" | sha256sum
#    expect: identical to the same call on the pre-roll deployment

# the edge ledger is off and writing nothing
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FROM entity_edge_events;"
#    expect: 0 until LEGBA_EDGE_TRANSITION_LEDGER=1; then > 0 after the next reifier tick
```

**Rollback.** Omit the parameters (they are optional); `LEGBA_EDGE_TRANSITION_LEDGER=0` +
`DROP TABLE entity_edge_events`.

---

### P4a — the trigger gauges

**Size: S.** **Wave 1** (independent of everything). Reserves migration **0208**.
*(0208 lands before 0207 in wall-clock time; see spec §7.1 "On the out-of-order pair" —
the two files are independent and the runner keys on filename, so this is safe.)*

**Goal.** The five pre-registered triggers are *readable* on 2026-11-03. Two of them are not,
today.

**Why it stands alone.** One additive nullable column and one read-only route.

**Read first.**
`DATA_MODEL_V3.md` §4.4 · `planning/graph_debate/JUDGE_SYNTHESIS.md` §4.2 (the trigger table
and the thresholds — read it in the main checkout, never copy it into a commit) ·
`src/legba/data/analysts/agency/agency.py` (`run_pack_tool` `:92` — where the ledger row is
written) · `src/legba/data/registry/production_gauge_api.py` (the gauge-route template) ·
`docs/history/AGE_PROBE_REPORT.md` §5.2 (the ~8 µs/edge rebuild figure E4 is extrapolated from).

**Tasks.**

1. **0208_invocation_duration.sql** — `ALTER TABLE action_pack_invocations ADD COLUMN
   duration_ms integer;` Nullable, no default, no backfill. **This is what makes E2 readable
   at all** — the table has `cost_usd`, `units` and `outcome`, and no duration column, so the
   judge's E2 ("p95 latency over ≥100 invocations") cannot be computed today.
2. Stamp `duration_ms` at the settle point in `run_pack_tool`.
3. **`src/legba/data/registry/graph_triggers_api.py`** — `GET /api/v1/v3/graph-triggers`
   returning all five with the **threshold, the reading, and the distance**, and an explicit
   `"unreadable"` state with a reason where a gauge cannot be computed. A gauge that cannot be
   read must say so, never return 0.
   * **E1** = `count(*) FROM entity_edges WHERE edge_family='relation' AND valid_until IS NULL
     AND superseded_by IS NULL` (16,860 on 2026-09-21; threshold 250,000).
   * **E2** = p95 `duration_ms` over the graph tools, 30-day window, with the `n` shown;
     `"unreadable: insufficient invocations"` below 100.
   * **E3** = invocations/day for `query_paths` / `find_proxy_chains` / `query_brokers` over 14
     days (lifetime total is **6**); the shape-share half stays `"unreadable: no shape
     classifier"` and says so.
   * **E4** = the timed rebuild, read from the projector's receipt once P4b ships;
     `"unreadable: projector not deployed"` until then.
   * **E5** = a product decision, reported as such, not as a number.
4. A poisoned-import guard test for the new route module.

**Tests to add.**
`tests/registry/test_graph_triggers_api.py` (real binding path; the unreadable states;
thresholds match `JUDGE_SYNTHESIS` §4.2) · `tests/registry/test_graph_triggers_imports.py`.

**Deploy markers.**
`data/registry/graph_triggers_api.py:GRAPH_TRIGGER_GAUGE_VERSION = "2026-09/p4a"` ·
`data/analysts/agency/agency.py:duration_ms`

**Live acceptance proof.**

```bash
curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/graph-triggers" | python3 -m json.tool
#    expect: E1 reading ≈ 16,860 / threshold 250,000;
#            E2 "unreadable: insufficient invocations (n=<small>)";
#            E3 reading in single digits;
#            E4 "unreadable: projector not deployed";
#            E5 a stated product position, not a number.

# duration is actually being recorded
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FILTER (WHERE duration_ms IS NOT NULL), count(*)
     FROM action_pack_invocations WHERE occurred_at > now() - interval '1 hour';"
#    expect: the two numbers equal, once any pack tool has run post-roll
```

**Rollback.** Remove the route from `server.py`; the column is harmless.

---

### P5 — typing throughput: the decisive comparison

**Size: S.** **Wave 2.** No migration, no descriptor change.

**Goal.** Settle why the live accept rate is 7–11.5 % against the bake-off's measured 46.8 %,
and stop throwing away two readings the platform already produces.

**Why it stands alone.** It changes a receipt and adds a measurement script. No live options
move.

**Read first.**
`DATA_MODEL_V3.md` §5 (the 14-run funnel table is the starting point — do not re-derive it) ·
`docs/history/TYPING_BAKEOFF_2026-08-03.md` (all of §1, §7) · `docs/data/kg2_bakeoff/README.md` ·
`src/legba/data/analysts/reifier_selection.py` (`EXAMINE_MULTIPLIER` `:93`, `MAX_EXAMINE` `:98`,
`examine = min(...)` `:327`, and the counter docstring at `:199` that has been saying "the
scan's LIMIT was binding" on every run for a fortnight) ·
`src/legba/data/analysts/relationship_reifier.py` (`:1441` the receipt line, `:1456` the data
block) · `src/legba/data/provenance/entity_edge_writes.py` (`:248` the upsert).

**Tasks.**

1. **The one experiment.** Re-run `scripts/kg2_pool_measure.py` → `kg2_sample_prep.py` →
   `kg2_typing_bakeoff.py` → `kg2_bakeoff_score.py` on **today's** pool (320,161 pending, up
   from 174,632 in August), single model `core120b`, N=12. Report the harness accept rate
   beside production's 7–11.5 %. **That single number decides the lever** (spec §5.3):
   harness ≈47 % ⇒ the production path is defective; harness ≈10 % ⇒ the pool thinned and the
   bar is the lever.
   *Run the harness read-only against the live DB from the MAIN checkout. It writes nothing.*
2. **Surface the binding-scan signal.** When `qualified == examined`, emit a named
   `scan_limit_binding: true` on the receipt instead of leaving it to be inferred from two
   integers that happen to be equal.
3. **Count the fold.** Split `written` into `inserted` / `folded` using
   `RETURNING (xmax = 0) AS inserted`. (Coordinate with P3 task 7 — same file, same idiom; if
   P3 lands first, this is already done.)
4. **Write up, do not tune.** Deliver the comparison and the recommended single knob. **Do not
   change `qualification_bar`, `max_candidates`, `batch_size`, `EXAMINE_MULTIPLIER` or the
   model in this phase.** One knob per run is the bake-off's own standard and the operator
   owns the knob.

**Tests to add.** `tests/data_pkg/test_reifier_receipt_counters.py` (the binding flag fires
when equal and not otherwise; the insert/fold split).

**Deploy markers.**
`data/analysts/reifier_selection.py:scan_limit_binding` ·
`data/provenance/entity_edge_writes.py:folded`

**Live acceptance proof.**

```bash
# after the next 12-hourly reifier tick post-roll
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT output_payload->'data'->'selection'->>'scan_limit_binding',
          output_payload->'data'->>'inserted', output_payload->'data'->>'folded'
     FROM analyst_traces WHERE analyst_id='relationship_reifier'
    ORDER BY run_ended_at DESC LIMIT 1;"
#    expect: true | <int> | <int>, and inserted+folded = written
```

Plus the written comparison, filed beside `docs/history/TYPING_BAKEOFF_2026-08-03.md`.

**Rollback.** Revert the module; the counters are additive.

---

### P4b — the cross-layer graph projection

**Size: M.** **Wave 3.** Reserves migration **0207**.

**Goal.** `graph_arcs` — one whole-rebuild projection of every cross-layer arc, plane-tagged,
temporally queryable, behind a kill switch, with a staleness scalar that fails loud.

**Why it stands alone.** `LEGBA_GRAPH_PROJECTION` off ⇒ the table is not built, the projector
does not run, and every reader answers `projection_disabled`.

**Read first.**
`DATA_MODEL_V3.md` §4 (all of it; §4.5 explains why this is not AGE) ·
`planning/graph_debate/JUDGE_SYNTHESIS.md` §3.2 (the 0-for-6 incremental-mirror argument — this
phase exists in the shape it does because of that paragraph) · `docs/history/AGE_PROBE_REPORT.md` §5.2
· `src/legba/data/analysts/deterministic_handlers/graph_mining.py` and `structural_balance.py`
(`_MAX_NODES` 5,000 and 1,500 — the scoped snapshots that keep their job) ·
`src/legba/data/registry/graph_walk_api.py` (the route + date-parameter shape to copy) ·
`src/legba/data/registry/production_gauge_api.py` (the S-1 loop registration).

**Tasks.**

1. **0207_graph_arcs.sql** — `graph_arcs` + the singleton `graph_arcs_meta`, spec §4.2.
   No foreign keys, and the header says why.
2. **`graph_projector.py`** — one `INSERT … SELECT` per source in spec §4.1, into
   `graph_arcs_new`, then one `ALTER TABLE … RENAME` swap inside a transaction. **No
   incremental path exists and none is added.** Record `build_seconds` — that reading is E4.
3. **`plane` is NOT NULL and `world` is the default filter on every world-facing reader.** 74 %
   of the ~4.0 M arcs are output lineage; a world-graph query that defaults to "all arcs" would
   report who cited whom as the state of the world. This is Decision 3's lesson in a second
   domain, and it is the single most important line in the phase.
4. **Exclusions are explicit and commented**: `proposed_edges` (candidate queue — the judge's
   Decision 4), `journal_entries` (off-chain by construction; a projection including it would
   let a graph walk surface a journal node).
5. **`GET /api/v1/v3/graph/arcs`** — plane-filtered bounded ego / walk with `as_of`, refusing
   `projection_disabled` / `projection_stale` / `projection_empty` as **distinct** states.
   Never `found=False` against an empty structure — that is the exact defect the judge filed as
   P0 item 2 for `/graph/path`.
6. **Repoint the scoped snapshots**: `graph_mining` and `structural_balance` build their
   networkx subgraphs from `graph_arcs WHERE plane='world'` instead of from `nexuses`,
   **keeping their node caps**. This is what finally stops `balance_ratio` measuring UN
   co-membership, because `family` is now on every arc.
7. **Register the S-1 closed loop**: expected-vs-actual arc counts per source table, build
   seconds, staleness age.
8. **`descriptors/analyst_graph_projector.yaml`**, `state: draft`, cadence hourly,
   `budget_tokens_per_day: 0`.

**Tests to add.**
`tests/data_pkg/test_graph_projector.py` — **the load-bearing one: build twice from the same
substrate snapshot and assert the two tables are row-identical** · plane assignment per source ·
`proposed_edges` and `journal_entries` produce zero arcs · a temporal walk excludes an arc
closed before the `as_of` date · the three refusal states · a real-binding-path route test ·
a poisoned-import guard test.

**Deploy markers.**
`data/analysts/deterministic_handlers/graph_projector.py:GRAPH_PROJECTION_VERSION = "2026-09/p4b"` ·
`data/registry/graph_arcs_api.py:projection_stale`

**Live acceptance proof.**

```bash
# flag off first: the reader refuses, distinctly
curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/graph/arcs?from_kind=entity&from_id=<uuid>" | head -3
#    expect: projection_disabled — NOT an empty result

# then flag on, force one projector run:
docker compose exec -T postgres psql -U legba -d legba -At -F'|' -c \
  "SELECT arc_count, build_seconds, projected_at FROM graph_arcs_meta;"
#    expect: arc_count ≈ 4.0M (2026-09-21 baseline: 2,947,956 derived_from + 848,436 mentions
#            + 45,613 entity edges + 48,076 consumption + 42,008 supersessions
#            + 22,969 bearing + 15,471 aliases + 14,657 situation members + 1,374 merges
#            + 44 echoes, plus whatever events exist). build_seconds is the E4 reading.

docker compose exec -T postgres psql -U legba -d legba -At -F'|' -c \
  "SELECT plane, count(*) FROM graph_arcs GROUP BY 1 ORDER BY 2 DESC;"
#    expect: lineage ≫ evidence > world. If world dominates, the plane map is wrong.

docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FROM graph_arcs WHERE src_table IN ('proposed_edges','journal_entries');"
#    expect: 0

curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/graph/arcs?from_kind=entity&from_id=<a hub uuid>&plane=world&limit=25" \
  | python3 -m json.tool | head -20
#    expect: real arcs, and a projected_at stamp on the envelope

curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/graph-triggers" | grep -A2 '"E4"'
#    expect: E4 now readable, carrying build_seconds
```

**Rollback.** `LEGBA_GRAPH_PROJECTION=0`; `DROP TABLE graph_arcs, graph_arcs_meta`; revert the
two snapshot readers to `nexuses`. This is the whole exit cost.

---

### P6 — consumers: map, timeline, situations, and the directed-read grants

**Size: M.** **Wave 3**, after P3 has merged (same port file).

**Goal.** Events are visible and reachable: on the map with their own geo, on the timeline as
spans, under the situations that track them, and through three new read tools.

**Why it stands alone.** UI-only surfaces plus additive read tools; nothing writes.

**Read first.**
`DATA_MODEL_V3.md` §6.1-§6.3 · `legba-ui-v3/src/v4/world/mapData.ts` (`targetCountry` `:148`,
`centroidOf` `:139`, and the three loaders — the 2026-09-21 watch-desk fix is the context) ·
`legba-ui-v3/src/lib/timelinePoints.ts`, `timelineWindows.ts`,
`src/panels/merged/Timeline.tsx` · `src/panels/target/Situations.tsx` ·
`src/legba/data/registry/substrate_reads_api.py` (the page contract: default 50, max 500,
cursor) · `src/legba/data/analysts/agency/substrate_read.py` (`register_substrate_read_tools`
`:374`) · `descriptors/action_pack_substrate_read.yaml`.

**Extraction first.** `substrate_query_port.py` is 3,753 lines
under a 3,910-line ceiling; P3 lifted 164 lines out and added about as many back. P6's three
tools will not fit in 157 lines. Extract a cohesive sibling (the situation and timeline readers are
the natural cut) BEFORE adding anything, exactly as P3 did with `substrate_temporal.py`; the
module-size ceiling is not edited.

**Tasks.**

1. **Three tools** — `query_events`, `inspect_event`, `belief_as_of` (the last only if P3 left
   it as a route without a tool). **All five surfaces each** (§P3 task 4), plus the pack
   descriptor's `tools:` list.
2. **`events_api.py`** — `GET /v3/events`, `/v3/events/{id}`, `/v3/events/{id}/lifecycle`,
   house page contract, poisoned-import guard test.
3. **Map** — an events layer in `mapData.ts` + `MapPanel.tsx`. Events carry `geo` and
   `geo_lat/lon`, so **no centroid fallback is needed and none should be added**: that is the
   point. Region / lane / flow desks carry `scope.geo = []` and will never place by target; an
   event about them places by its own geo.
4. **Timeline** — events as **spans** (`time_start` → `time_end`), lifecycle state as the badge.
5. **Situations** — the tracked-events list per situation, from `situation_event_links`.
6. **Panel registration** if a new panel is added (`ui_panel_registrations`; the matrix is
   regenerated in the same commit).
7. **The directed-read ("inquiry") kind's grants — the narrow first grant.**
   `substrate_read` (now including the three event tools and the temporal parameters) +
   `journal_read`. **Not `web_access`**, and the reason is concrete: that pack's governor is
   currently lifted to 1,000,000/h and 1,000,000/min, so a directed reader on it is an
   unmetered egress surface. Adding web later is a descriptor PUT with a tighten-only
   `governor_override` and no roll. **See spec §8 Q5 — this is an operator decision, and the
   brief and the ledger disagree about it. Implement the narrow grant and flag it in your
   report; do not decide it.**

**Tests to add.**
Real-binding-path tests for the three tools (through `AgencyToolBinding.run_tool` →
`Agency.run_pack_tool`, so `action_pack_invocations` records them) · route tests ·
`mapData` unit tests beside the existing ones · `Timeline.test.tsx` span rendering ·
`tsc` clean.

**Deploy markers.**
`data/registry/events_api.py:build_events_router` ·
`data/analysts/agency/substrate_read.py:query_events` ·
and the built UI bundle carrying `eventsLayer` (the `--ui` roll).

**Live acceptance proof.**

```bash
curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/events?limit=3" | python3 -m json.tool | head -40
#    expect: rows with geo, lifecycle_state, signal_count, distinct_source_count

curl -s -H "Authorization: Bearer $LEGBA_TOKEN" \
  "http://localhost/api/v1/v3/events/<id>/lifecycle" | python3 -m json.tool | head -20
#    expect: the ledger, oldest→newest, with an 'opened' row

# the tool traverses the real binding path and is ledgered
docker compose exec -T postgres psql -U legba -d legba -At -F'|' -c \
  "SELECT tool_name, count(*) FROM action_pack_invocations
    WHERE tool_name IN ('query_events','inspect_event') GROUP BY 1;"
#    expect: nonzero after the first consult/journal run that calls them

# the map places an event with NO country target
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "SELECT count(*) FROM events WHERE geo_lat IS NOT NULL AND (target_id IS NULL
     OR target_id NOT LIKE 'country_%');"
#    expect: > 0 — these are the ones that could never place before
```

**Rollback.** Remove the routes from `server.py`; revert the tool registrations and the
descriptor `tools:` entries; re-roll the UI.

---

### P7 — provenance: the origin-class columns and the reader firewall

**Size: S.** **Wave 3**, after P1 (shared file: `writes.py`). Reserves migration **0209**.

**Goal.** Every substrate row says where it came from, in one closed vocabulary, and the live
gate refuses the classes that are history. Nothing new is ingested; no existing row changes
class except the ones that are demonstrably seed or web-retrieved; every reader answers
byte-identically for today's rows.

**Why now.** The open-row gate is `superseded_by IS NULL AND valid_until IS NULL` (migration
0032, kept by P3 for the non-`as_of` path). A backfilled 2016 fact with an open `valid_until`
therefore reads as *current today*, and it would also feed freshness, source health, calibration,
salience and the alert plane. The firewall has to be the origin class, not the validity window,
and it has to exist before a single historical row does. Program 7's collections (the loader,
the manifests, the observations table) come later; this phase is only the columns, the stamp on
the two write paths that already produce non-live rows, the one gate the v3 readers share, and a
constraint that makes the meantime safe.

**Read first.**
`src/legba/data/retrieval_origin.py` (the existing per-signal origin vocabulary: `curated_source`,
`web_search:*`, `web_evidence` — P7 does not replace it; it derives the class from it) ·
`src/legba/data/provenance/writes.py` (the `INSERT INTO facts` and the four `SET superseded_by`
sites) · `src/legba/data/seed/_driver.py` and `manual_schema.py` (`ProvenanceTier` — seeds are the
one non-live class that exists today) · `src/legba/runtime/substrate_query_port.py` lines 400–460
(the open-row clauses P3 left in place) and `src/legba/runtime/substrate_temporal.py` · `docs/DATA_MODEL_V3.md`
§"Bitemporality, honestly" · `planning/NEXT_ARC_CAPTURE_2026-09-23.md` §B5 (readable from the main checkout).

**The vocabulary** (closed; a CHECK constraint, not an enum type):
`live` · `web_retrieval` · `seed` · `backfill_native` · `backfill_reconstructed` · `archive`.
`LIVE_CLASSES = {live, web_retrieval, seed}` is what every reader sees today. The three history
classes are refused at the table until Program 7's reader sweep lands (task 4).

**Tasks.**

1. **`0209_origin_class.sql`** — `CREATE EXTENSION IF NOT EXISTS btree_gist` (trusted; the
   temporal keys of Program 7's `entity_aliases` need it and the line belongs with the columns).
   On `signals`, `facts`, `events`: `origin_class text NOT NULL DEFAULT 'live'` and
   `collection_id text NULL` (no FK — the collections table does not exist yet; the header says
   so). On `facts`: `superseded_at timestamptz NULL`. Two constraints per table, named exactly:
   `<table>_origin_class_vocab CHECK (origin_class IN (<the six>))` and
   `<table>_origin_class_readers_not_swept CHECK (origin_class IN ('live','web_retrieval','seed'))`.
   The second is the SEAM: it is dropped by the migration that lands the reader sweep, and its
   name is what a premature backfill sees in its error. Backfill of existing rows, **measured and
   stamped in the header** (2026-09-23 baseline: facts 154,960 of which `seed_batch_id IS NOT NULL`
   1,014 and `source_type='seed'` 1,012; signals 287,927 of which `retrieval_origin LIKE
   'web_search:%'` 127; events 985; `superseded_by IS NOT NULL` 8,654):
   facts with `seed_batch_id IS NOT NULL OR source_type='seed'` → `seed`; signals with
   `retrieval_origin LIKE 'web_search:%' OR retrieval_origin = 'web_evidence'` → `web_retrieval`;
   facts whose `evidence_set` cites only such signals → `web_retrieval` (measure; if the join is
   not expressible in one statement, say so and leave them `live`); everything else stays the
   default. `superseded_at` for the 8,654 superseded facts = the successor's `created_at`, and the
   header says it is an approximation. The column defaults are metadata-only in Postgres 18; the
   UPDATEs touch ~1,200 rows.
2. **`src/legba/data/provenance/origin.py`** (a leaf: no imports from the runtime) —
   `ORIGIN_CLASSES`, `LIVE_CLASSES`, `origin_class_for(retrieval_origin: str | None) -> str`,
   and `live_gate_sql(alias: str) -> str` returning the one predicate
   `"<a>.superseded_by IS NULL AND <a>.valid_until IS NULL AND <a>.origin_class IN ('live','web_retrieval','seed')"`
   (the class list rendered from `LIVE_CLASSES`, sorted, so the SQL is stable). Stamp
   `ORIGIN_CLASS_VERSION = "2026-09/p7"` here.
   Also `as_of_gate_sql(alias, param_index)` — the one rendering of the validity predicate
   `COALESCE(<a>.valid_from, '-infinity') <= $n AND (<a>.valid_until IS NULL OR <a>.valid_until > $n)`
   that P3 wrote out six more times (19 sites in `src/` on 2026-09-23). The two v3 reader files
   adopt it in this phase; the inventory test pins the rest exactly as it pins the live gate.
3. **Write paths.** The facts INSERT takes `origin_class` (default `live`) and `collection_id`
   (default NULL); the seed driver passes `seed`; the research-evidence signal writer passes
   `origin_class_for(retrieval_origin)`; the four `SET superseded_by` sites also
   `SET superseded_at = now()`. The event writer stamps `live`. No other writer changes.
4. **The gate, in the readers that v3 owns.** `substrate_query_port.py` and `substrate_temporal.py`
   replace their inline open-row clauses with `live_gate_sql()`. The `as_of` (validity) path gains
   `include_origin: list[str] | None = None` → `None` means `LIVE_CLASSES` (so a future backfill
   row is invisible to an as-of read unless the reader asks); `believed_as_of` needs no change —
   decision time is `created_at`, and a row recorded in 2026 was never believed in 2016, which is
   the whole point of the pair. **The other ~58 files carrying `superseded_by IS NULL` are NOT
   swept in this phase.** Instead: `tests/data_pkg/test_origin_gate_inventory.py` walks `src/`
   for the literal, asserts the exact set of files against a pinned allowlist in the test (the
   list shrinks as Program 7 sweeps them; a new unlisted site fails the test), and the
   `readers_not_swept` constraint keeps the un-swept readers correct by making the rows they
   cannot filter impossible to insert. Declare **SEAMS #51** with that pair as the guard rail.
5. **`docs/DATA_MODEL_V3.md` §"Bitemporality, honestly"** gains one paragraph: the six classes,
   which readers see which, and the contrast with Graphiti's invalidate-on-contradiction (ours
   routes a contradicting source to contention; supersession is for single-value predicates).
   `docs/DATA_MODEL.md`'s facts and signals rows list the new columns.

**Tests to add.**
`tests/data_pkg/test_origin_class.py` — a plain fact insert reads back `live` · a seed batch
through the driver reads back `seed` · a signal written with `retrieval_origin='web_search:x'`
reads back `web_retrieval` and `origin_class_for` agrees · an insert with `backfill_native` fails
naming `facts_origin_class_readers_not_swept` · a supersede stamps `superseded_at` · inside a
transaction that drops the `readers_not_swept` constraint (rolled back at the end — DDL is
transactional), a row forced to `archive` is absent from the port's open read and present with
`include_origin=['archive']` on the `as_of` read · `live_gate_sql('f')` is the exact expected
string · the inventory test above · the poisoned-import guard for `origin.py`.

**Deploy markers.**
`data/provenance/origin.py:ORIGIN_CLASS_VERSION = "2026-09/p7"` ·
`data/migrations/0209_origin_class.sql:readers_not_swept`

**Live acceptance proof.**

```bash
docker compose exec -T postgres psql -U legba -d legba -At -F'|' -c \
  "SELECT 'facts', origin_class, count(*) FROM facts GROUP BY 2
   UNION ALL SELECT 'signals', origin_class, count(*) FROM signals GROUP BY 2
   UNION ALL SELECT 'events', origin_class, count(*) FROM events GROUP BY 2;"
#    expect: the 0209 header's numbers, exactly
docker compose exec -T postgres psql -U legba -d legba -c \
  "INSERT INTO facts (subject,predicate,value,origin_class) VALUES ('x','y','z','archive');"
#    expect: ERROR … violates check constraint "facts_origin_class_readers_not_swept"
# byte-identity: the same substrate read for one target before and after the roll
curl -s -H "Authorization: Bearer $LEGBA_TOKEN" "http://localhost/api/v1/v3/facts?target_id=country_il&limit=200" | sha256sum
```

**Rollback.** The migration is additive (columns with defaults, two constraints, one trusted
extension); revert the code and the columns are inert. Dropping them is a separate migration.

---

## 3 · Orchestrator checklist

### 3.0 The block that applies to every phase

**Review the diff for — in this order, because the first four are the ones that get past a
reading that starts with the code:**

1. **Trailers.** `git log --format='%B' <base>..<branch> | grep -Ei 'co-authored|generated with|claude'` → must be empty. Reject the branch, do not fix it for them.
2. **`planning/` in the diff.** `git diff --stat <base>..<branch> -- planning/` → must be empty.
3. **The module-size gate.** `git diff <base>..<branch> -- tests/test_module_size_gate.py` → a
   changed ceiling *number* is a rejection unless it went **down** in the same commit as a real
   extraction. Raising is not negotiable.
4. **Stubs.** Any new `NotImplementedError`, `TODO`, `fake`, `mock`, `placeholder` or
   `echo`-named symbol in `src/**` must have a matching `docs/SEAMS.md` row **and** allowlist
   line in the same diff.
5. **Descriptors.** Every added/edited descriptor: `budget_tokens_per_day: 0`; `temperature:
   1.0` on every declared LLM route; no `max_tokens` on a core-plane route it did not already
   have; `state: draft` for a new kind.
6. **Slim-image discipline.** A new `src/legba/data/registry/*_api.py` importing
   `legba.data.analysts` anywhere — including inside a function — is a rejection. Confirm the
   poisoned-import guard test exists and runs.
7. **Real binding path.** A new route or tool tested by calling the handler directly, rather
   than through the router factory / `Agency.run_pack_tool`, is a rejection. Audit with
   `action_pack_invocations` counts for tools.
8. **Flag defaults.** Every new flag defaults **off** and is read in exactly one place.
9. **Migration number** matches the one this plan reserved, and nothing else in the tree claims
   it: `ls src/legba/data/migrations/ | grep '^0 2'`.

**Merge order.** Wave order (§1). Within a wave, merge smallest first so a conflict surfaces
against the least work: **P4a → P5 → P2 → P3 → P1** in wave 2; **P4b → P6** in wave 3. P3 is
merged **before** P6 unconditionally — they share `substrate_query_port.py` and the five
tool-declaration surfaces.

**Suite on the merged tree, once per wave** (not per phase):

```bash
PYTHONPATH=src python -m pytest -q
# Zero expected failures since 2026-09-26 (wave O / o6 cleared the baseline —
# see §0). Every failure is the wave's.
```

**Roll shape** — registry first is not optional; the script enforces it:

```bash
scripts/rolling_deploy.sh \
  --verify-grep '<phase marker 1>' \
  --verify-grep '<phase marker 2>' \
  [--verify-file <path>] \
  [--ui]            # P6 only
```

Exit 5 is marker verification failing inside one or more containers — that means the image is
stale, not that the marker is wrong. Exit 2 is the image-age guard; re-run without
`--skip-build`.

**Post-roll, always:** re-read the live descriptor heads you PUT; confirm
`docker compose logs legba-runtime-dapr --since 10m | grep -c reconcile.failed` is 0; then run
the phase's acceptance proof, and only then write the ledger line.

**Ledger line shape** (append-only, into the live ledger, naming what it spent):

```
- 2026-MM-DD HH:MMZ UTC — V3 <PHASE> MERGED + ROLLED (<sha>): <one clause on what landed>.
  Markers verified in all 3 containers. PROOF: <the reading, with the number>. Flags: <name>=<0|1>.
  Descriptors: <ids + state>. Cost: <$0 | the figure>. Next: <the next phase or the wait>.
```

### 3.1 Per-phase specifics

| phase | extra diff review | roll | post-roll registrations | the proof, in one line | ledger must record |
|---|---|---|---|---|---|
| **P0** | 0202-0205 headers carry the **measured** dry-run numbers; `analyst_id NOT NULL`; no FK on `signal_event_links.signal_id`; `fold_event_entity_links` is called **inside** `merge_pair`'s transaction, after the `merged_into` UPDATE; the ledger trigger is 0184's function shape, not a new one | `--verify-grep 'data/events/lifecycle.py:EVENTS_LIFECYCLE_VERSION: str = "2026-09/p0"' --verify-grep 'data/provenance/writes.py:_insert_event'` · **registry image REBUILT first — a new `OutputKind` is a shared-schema change** | none (no descriptor yet); set `LEGBA_EVENTS=0` explicitly in `.env` | `SELECT source_method, count(*) FROM events GROUP BY 1` equals the 0205 header, and the ledger `UPDATE` errors | the backfill counts, both paths, and that clustering = 0 |
| **P4a** | the gauge route returns `"unreadable"` with a reason where it cannot compute — **never 0**; thresholds match `JUDGE_SYNTHESIS` §4.2 exactly | `--verify-grep 'data/registry/graph_triggers_api.py:GRAPH_TRIGGER_GAUGE_VERSION = "2026-09/p4a"'` | none | `curl /v3/graph-triggers` shows E1 ≈16,860/250,000 and E2/E4 explicitly unreadable | the five readings as of the roll — this is the 11-03 baseline |
| **P1** | the `oversized` path **refuses**, never truncates; the matcher uses the degenerate-embedding structural gate; SEAMS #56 declared with a raising guard rail | `--verify-grep 'data/analysts/deterministic_handlers/_event_matcher.py:EVENT_CLUSTERING_VERSION: str = "2026-09/p1"' --verify-grep 'data/analysts/deterministic_handlers/event_reconciler.py:SEAMS #56'` | POST both descriptors to `/descriptors/analyst` (a new id is a POST; PUT only updates a head), then `/transition` to configured and active, `draft → configured → active` via the **new-kind activation checklist**; then `LEGBA_EVENTS=1`; force one run | `SELECT count(*) FROM events WHERE NOT EXISTS (…signal_event_links…)` = 0, and the funnel is non-null | the funnel, the `oversized` count, and the first `reactivated` row if one fired |
| **P2** | **`verify.py` is not in the diff** — if it is, the design was not followed; `event` absent from `GROUNDING_REF_KINDS`; the summary-leak negative test exists | `--verify-grep 'data/provenance/event_citations.py:EVENT_CITATION_VERSION = "2026-09/p2"'` | grant event citations to ONE analyst first; `LEGBA_EVENT_CITATIONS=1` | the summary-leak query returns 0 **and** a Faithfulness verify row exists for the event-citing finding | the judge's score on the first event-cited finding |
| **P3** | the **extraction landed before the additions** and the gate passes with no ceiling edit; the as-of SQL **drops** `superseded_by IS NULL`; all five declaration surfaces moved together | `--verify-grep 'runtime/substrate_temporal.py:TEMPORAL_READER_VERSION = "2026-09/p3"' --verify-grep 'data/registry/belief_api.py:build_belief_router'` | `LEGBA_EDGE_TRANSITION_LEDGER=0` at roll; flip to 1 only after the as-of proof passes | `/v3/situations?as_of=<past>` returns an id that is **absent** without `as_of` | both readings (with and without `as_of`) and the byte-identity sha |
| **P5** | **no live option changed** — `qualification_bar`, `max_candidates`, `batch_size`, `EXAMINE_MULTIPLIER`, model all untouched | `--verify-grep 'data/analysts/reifier_selection.py:scan_limit_binding'` | none | the next reifier tick's receipt carries `scan_limit_binding` and `inserted + folded = written` | the harness-vs-production accept rates, side by side, and the single knob it indicts |
| **P4b** | **no incremental path anywhere** in the projector; the rebuild-twice-identical test exists and passes; `proposed_edges` and `journal_entries` are excluded with comments | `--verify-grep 'data/analysts/deterministic_handlers/graph_projector.py:GRAPH_PROJECTION_VERSION = "2026-09/p4b"'` | PUT the projector descriptor `draft → active`; `LEGBA_GRAPH_PROJECTION=1`; force one build | `graph_arcs_meta.arc_count` ≈ 4.0 M, plane split is lineage ≫ evidence > world, and the excluded-source count is 0 | `arc_count`, `build_seconds` (**this is the E4 reading**), the plane split |
| **P6** | events place on the map **without** a centroid fallback; the directed-read grant is the **narrow** one (no `web_access`) — decided 2026-09-21, §0.1 Q5; `tsc` clean | `--verify-grep 'data/registry/events_api.py:build_events_router' --verify-grep 'data/analysts/agency/substrate_read.py:query_events' --ui` | add the three tools to `action_pack_substrate_read.yaml` and re-run `scripts/bringup_register_action_packs.py`; register any new UI panel and regenerate the matrix | `/v3/events?limit=3` returns rows, and `action_pack_invocations` records `query_events` after the first run that calls it | the tool-invocation counts (**this is E3's gauge starting to move**) and the count of events placing with no country target |
| **P7** | both constraints exist with the exact names; the header carries the **measured** class counts; only the two v3 reader files touch the gate; the inventory test's allowlist equals the tree; `believed_as_of` untouched | `--verify-grep 'data/provenance/origin.py:ORIGIN_CLASS_VERSION = "2026-09/p7"' --verify-grep 'data/migrations/0209_origin_class.sql:readers_not_swept'` | none | the class counts match the header, the `archive` insert fails naming the constraint, and one target's substrate read hashes identically before and after | the class counts, the inventory count of un-swept gate sites (the number Program 7 drives to 0) |

### 3.2 Two things to watch across the whole programme

**The E4 gauge moves because of us.** The cross-layer projection is ~4.0 M arcs against E4's
≈7.8 M-edge threshold. P4b's `build_seconds` is the first real reading anyone has taken, and it
is the number the 2026-11-03 sitting will care most about. Record it at every roll after P4b,
not once.

**Nothing in v3 spends.** Every writer is on the `$0` core plane or is pure SQL. The one place
cost could enter is a future LLM-typed event↔event causal edge (SEAMS #56), which is
deliberately not built. If any phase's diff introduces a paid route, that is a rejection and an
operator question, not a merge.
