# History

The history tier holds what the rest of the docs no longer carry: when something changed,
what it replaced, and the measurement that decided it. The design and reference pages state
the platform's shape in the present tense; this directory keeps the record behind that shape.

Two kinds of thing live here.

- **[../CHANGELOG.md](../../CHANGELOG.md)** — one line per dated change, newest first, grouped by
  month. It is the index to everything below.
- **Measurement reports and recorded cases** — a measurement too long to compress into a line,
  kept as written and dated. Each is listed below with what it holds.

---

## The measurement reports

These were written as reports and moved here unchanged (the bake-off and its rerun now share one file). Each answers one question with
numbers, and each is cited by the changelog rather than summarised into it.

### [AGE_PROBE_REPORT.md](AGE_PROBE_REPORT.md) — *is the graph ceiling ours or the engine's?* (2026-08-03)

The question was whether the limits the graph debate attributed to Apache AGE were
really AGE's, or just an untuned Postgres. A throwaway container pinned to the same image
digest the live substrate runs was loaded twice at each of two scales — 100k and 1M edges —
once as an AGE graph and once as a relational twin over ordinary btrees, with a synthetic
world graph shaped like the real one (power-law degree, the project's entity-class
proportions, the four edge-family tiers, signed polarity, 8 % of edges closed). Six
configurations were measured: default, tuned, tuned plus a vertex property index, JIT off,
eight parallel workers, and a larger work_mem. **The verdict was negative, and specific about
why.** Tuning and indexing bought large, real wins on the plans that can use an index — an
anchor lookup from 250 ms to 0.15 ms — and bought nothing at all on open-ended ego expansion,
which is the graph-viewer verb: ego1, ego2 and ego3 did not move across a 200× spread in
effort. The unstable-signed-triad pattern query, the capability the debate treated as AGE's
unique contribution, timed out past 60 s in every arm at both scales while the relational twin
answered it in about 10 ms. And per-hop predicates over a variable-length path — which every
temporal reader in this platform needs — are not expressible in AGE 1.7 at all, which is a
correctness ceiling rather than a performance one. The report also re-measured graph-tool
demand against the live substrate (27 lifetime invocations, 1.47 % of all tool use) and
concluded that the obstacle is upstream of any engine: 810 open derived typed edges growing at
~12/day. Its recommendation is to keep the freeze, build on the relational edge table, and
treat typing throughput as the measurement that matters next. The platform later went further:
the AGE graph, which never held a production row, was dropped by migration
`20261115T1829_drop_empty_age_graph`, and graph-shaped reads use the relational edge store and the
rebuilt `graph_arcs` projection ([DATA_MODEL.md](../DATA_MODEL.md)).

### [TYPING_BAKEOFF.md](TYPING_BAKEOFF.md), part one — *what actually gates typed-edge throughput?* (2026-08-03)

The companion measurement, run read-only against the live substrate and live model endpoints.
Its headline is that the throughput blocker is not the model. The reifier's candidate window
carried no status filter and a dedup guard blind to keeper rewriting, so it read the whole
proposed-edge table ordered by confidence — and every row at the top was orphaned, rejected or
already promoted. Reproducing the exact window found **zero** pending candidates in the top
500; all 80 of the day's LLM calls were spent on rows that were structurally incapable of
producing a new edge. The report then measures a qualification bar (four components, an
eleven-setting sweep) and runs a four-model bake-off on a deterministic stratified sample. Its
governing finding is a ceiling, not a ranking: the same model hosted two ways agrees with
itself on edge-versus-reject only 79.6 % of the time, so roughly 40 % of apparent model
disagreement is irreducible noise on an underdetermined task, models cannot be ranked by
agreement with a reference that disagrees with itself, and any model swap re-rolls about a
fifth of the graph's edges. No difficulty signal — qualification score, evidence length,
source count — predicts disagreement, so the recommendation is a **single typer** at batch
size twelve, a 0.42 bar with two independent sources, and human labels before any model swap.
The projected effect of the queue fix alone is 12 typed edges a day to about 205 in steady
state, at no extra cost.

### [TYPING_BAKEOFF.md](TYPING_BAKEOFF.md), part two — *the pool, or the path?* (2026-09-22)

Production's realised accept rate settled at 7–11.5 % over fourteen runs, against the August
bake-off's 46.8 % on the same model. The spec named one experiment that separates the two
explanations — re-run the harness on 200 candidates from today's pool, same procedure, same
seed discipline — with a two-pole reading: about 47 % means the pool is fine and the
production path is the defect; about 10 % means the pool thinned and the bar admits a thinner
population. **The rerun was not executed, and the report says so rather than estimating.**
Neither prerequisite was available where the report was written: the live pool's container did
not exist there, and the model endpoint resolves through a stack registry and vault that are not
reachable from there. No number is claimed and the comparison stays open; the report carries the exact
read-only runbook for whoever runs it against a deployment. What it does land is the
outcome-independent half — two readings the platform was already producing and discarding: a
drain flag that is true only when the scoring scan hit its limit *and* every examined row
cleared the bar, and a split of the reifier's `written` count into newly-minted edges and
re-observations of existing ones, so a parked endpoint shows up as the residue.

---

## Recorded cases

A recorded case keeps real sessions as test fixtures and reads them side by side.

- **[CONSULT_DIVERGENCE_2026-09-28.md](CONSULT_DIVERGENCE_2026-09-28.md)** — *one question, five
  runs, two models.* The same consult question run on Opus, on the core model before the
  argument-validation fixes (which wrote a fabricated tool result into a call's arguments) and
  three times after. The sessions live in `tests/data_pkg/fixtures/consult_divergence/`, and
  `test_consult_divergence_admission.py` replays every recorded call through `validate_args`:
  the first model-divergence case and the admission bar for a tool-using role.
