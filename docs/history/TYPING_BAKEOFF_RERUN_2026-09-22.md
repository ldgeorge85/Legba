# Typing bake-off rerun — V3/P5 (the decisive comparison + two receipt fixes)

**Date:** 2026-09-22 · **Branch:** `portfolio-ingestion-v2` (base `53f5fc3a`,
the P4a commit — P5's pinned base per the roadmap's dependency order)
**Scope:** DATA MODEL V3 §5.3. Two production receipt readings + the bake-off
re-run on today's pool. **No option was tuned** — `qualification_bar`,
`max_candidates`, `batch_size`, `EXAMINE_MULTIPLIER` and the model are all
exactly where the 2026-08-03 bake-off put them.

---

## 1 · The question this phase exists to answer

Production's realised accept rate is **7–11.5 %** over the 14 runs to
2026-09-21 (`analyst_traces.output_payload`, `relationship_reifier`). The
August bake-off measured **46.8 %** on the same model. The spec's §5.3 names
exactly one experiment that separates the two possible causes:

> Re-run the bake-off harness on 200 candidates drawn from today's pool, same
> procedure, same seed discipline, and compare its accept rate to
> production's 7–11 %.

| harness on today's pool | verdict | lever |
|---|---|---|
| ~47 % accepted | the pool is fine — **the production path is the defect** (batched prompt, N=12 rendering, or a model/route drift vs `llm.primary.openai_compat`) | a code fix worth ~3× the graph's growth rate |
| ~10 % accepted | **the pool has changed** — 320,161 pending vs 174,632 then; the bar admits a thinner population | re-sweep the bar against `docs/data/kg2_bakeoff/pool_summary.json`'s 11-setting curve, on today's pool |

## 2 · What landed in this phase

Two readings the platform was already producing and throwing away:

### 2.1 `scan_limit_binding` — the named DRAIN flag

`src/legba/data/analysts/reifier_selection.py` — `SelectionCounters` gains
`scan_limit_binding`, serialized into the run receipt at
`data->'selection'->'scan_limit_binding'`.

It is **true** exactly when the scoring scan returned its LIMIT's worth of
rows (`examined == examine`, i.e. `min(MAX_EXAMINE, max_candidates ×
EXAMINE_MULTIPLIER)` was reached) **and** every examined row cleared the bar —
the DRAIN state, where qualifying supply provably extends below the cut. On
all 14 production runs to 2026-09-21 this held at `1800/1800`, unread.

Deliberately stricter than the spec's informal "`qualified == examined`": an
exhausted pool (`examined < examine`) is the STEADY state even when the two
integers happen to be equal, and an empty scan must not report "binding".

### 2.2 `inserted` / `folded` — the upsert split

`src/legba/data/provenance/entity_edge_writes.py` — the dual-write upsert now
`RETURNING id, (xmax = 0) AS inserted` (the `situation_clustering.py` idiom),
and the reifier receipt splits `written` into `inserted` (a new edge minted)
+ `folded` (re-observation of an existing open edge — `observed_count + 1`).
A parked endpoint shows up as the `written − (inserted + folded)` residue.

The per-run channel is a **task-local `ContextVar` sink**
(`bind_edge_write_sink`, the `run_accounting` pattern) — not the process-wide
`COUNTERS` — because every nexus producer shares the runtime process, and a
run receipt must count exactly its own writes.

**Marker greps:**
- `data/analysts/reifier_selection.py:scan_limit_binding`
- `data/provenance/entity_edge_writes.py:folded`

**Size gate:** the sink wrap pushed `relationship_reifier.py` to 1,505 lines —
over the 1,500 entry threshold — so the summary builder extracted to
`data/analysts/_reifier_receipt.py` (`build_reifier_summary`), per the gate's
own extract-don't-raise instruction. The module is back under at 1,426.

## 3 · The rerun — NOT EXECUTED from this lane

**Both hard prerequisites are absent on this box:**

1. **The live pool.** `kg2_pool_measure.py` reads
   `docker exec legba-postgres-1 psql` — that container does not exist here
   (only the test scratch `legba-scratch-pg`, whose `proposed_edges` holds
   **0 pending rows** — verified). There is no 320,161-row pool to sample.
2. **The model.** `kg2_typing_bakeoff.py` resolves
   `llm.primary.openai_compat` (the self-hosted gpt-oss-120b endpoint) through
   the stack registry + credential vault; neither is reachable from this
   worktree.

This is the outcome the plan anticipated ("if the live DB or the model
endpoint is unreachable from the lane, the report must say so"). **No number
is claimed.** The decisive comparison remains open; nothing in §1's table
has been decided.

## 4 · The orchestrator's runbook (read-only)

From the **main checkout** (this worktree is not the deployment), with the
live stack up:

```bash
# 1. Measure today's pool + draw the stratified 200 (SAMPLE_SEED unchanged —
#    an unchanged pool reproduces docs/data/kg2_bakeoff/sample_candidates.csv
#    byte-identically; a changed pool yields a fresh honest sample)
PYTHONPATH=src python scripts/kg2_pool_measure.py --out /tmp/kg2_rerun --sample-size 200
PYTHONPATH=src python scripts/kg2_sample_prep.py --dir /tmp/kg2_rerun

# 2. Type the sample — ONLY core120b (llm.primary.openai_compat) at N=12.
#    This is the one model+shape production runs; no ladder, no sweep.
scripts/kg2_run_in_container.sh /tmp/kg2_rerun --models core120b --batch-size 12

# 3. Score it and compare the accept rate against production's 7–11.5 %.
PYTHONPATH=src python scripts/kg2_bakeoff_score.py --dir /tmp/kg2_rerun --models core120b
```

The harness is read-only: it selects, types, and scores — it writes nothing
to the substrate (same guarantee as the August run; `kg2_typing_bakeoff.py`
calls the model endpoint, never `write_nexus`).

## 5 · Reading the result when it lands

- **Accept ≈ 47 %** → diff the harness's batch render against
  `relationship_typing_batch.py`'s live path (prompt text, N=12 layout,
  verdict coercion) and the resolved endpoint for `llm.primary.openai_compat`
  (route drift is a real hypothesis — the harness and the handler resolve the
  component through different config layers).
- **Accept ≈ 10 %** → the pool/bar is the lever. `pool_summary.json` gives
  the 11-setting bar sweep's shape; re-run `kg2_pool_measure.py` (step 1
  alone) for today's curve and re-pick the bar — that is a *descriptor* knob
  (`qualification_bar`), an orchestrator decision, not this lane's.
- **Anything between** → report it plainly; the matrix is two poles of an
  explanation, not a forced dichotomy — a 25 % reading would mean both the
  pool thinned AND the path diverged.

The receipt additions (§2) are the outcome-independent half: whichever way
the experiment lands, the DRAIN flag and the insert/fold split are the
readings the next report needs and were already being produced.
