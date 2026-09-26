# History

The history tier holds what the rest of the docs no longer carry: when something changed,
what it replaced, and the measurement that decided it. The design and reference pages state
the platform's shape in the present tense; this directory keeps the record behind that shape.

Two kinds of thing live here.

- **[../CHANGELOG.md](../../CHANGELOG.md)** — one line per dated change, newest first, grouped by
  month. It is the index to everything below, and the only place in the docs tree where dates
  and ticket ids appear.
- **Reports and extracts** — a measurement too long to compress into a line, or the narrative
  a rewritten page shed. Each is listed below with what it holds.

---

## The measurement reports

These three were written as reports and move here unchanged. Each answers one question with
numbers, and each is cited by the changelog rather than summarised into it.

### AGE_PROBE_REPORT.md — *is the graph ceiling ours or the engine's?* (2026-08-03)

The operator's question was whether the limits the graph debate attributed to Apache AGE were
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
treat typing throughput as the measurement that matters next.

### TYPING_BAKEOFF_2026-08-03.md — *what actually gates typed-edge throughput?* (2026-08-03)

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

### TYPING_BAKEOFF_RERUN_2026-09-22.md — *the pool, or the path?* (2026-09-22)

Production's realised accept rate settled at 7–11.5 % over fourteen runs, against the August
bake-off's 46.8 % on the same model. The spec named one experiment that separates the two
explanations — re-run the harness on 200 candidates from today's pool, same procedure, same
seed discipline — with a two-pole reading: about 47 % means the pool is fine and the
production path is the defect; about 10 % means the pool thinned and the bar admits a thinner
population. **The rerun was not executed, and the report says so rather than estimating.**
Neither prerequisite was available from the lane: the live pool's container does not exist
there, and the model endpoint resolves through a stack registry and vault that are not
reachable. No number is claimed and the comparison stays open; the report carries the exact
read-only runbook for whoever runs it on the main checkout. What it does land is the
outcome-independent half — two readings the platform was already producing and discarding: a
drain flag that is true only when the scoring scan hit its limit *and* every examined row
cleared the bar, and a split of the reifier's `written` count into newly-minted edges and
re-observations of existing ones, so a parked endpoint shows up as the residue.

---

## Extracts from the rewritten pages

Each of these holds the dated narrative a page shed when it was rewritten in the present
tense. Nothing here is a rule the platform still follows — the rules moved beside the
procedure or mechanism they guard. These files answer *when, and what it replaced*.

- **[RUNBOOK_extracts.md](RUNBOOK_extracts.md)** — the incident and era write-ups pulled out of
  the runbook when it became procedure-only: each keeps its original section number, heading,
  date and text, ordered by section rather than by date.
- **[DESIGN_decisions.md](DESIGN_decisions.md)** — everything dated, ticketed or narrated as
  "was / now" from ARCHITECTURE, DESIGN and DIRECTION, chronological and grouped by month, each
  entry naming its source page.
- **[ANALYSIS_decisions.md](ANALYSIS_decisions.md)** — the same for ANALYSIS, FLOWS, DATA_MODEL
  and ACQUISITION: dated decision records, oldest first, with undated entries filed under the
  wave that produced them.

If a further extract lands here later, add it to this list with one line saying which page it
came from and what question it answers.
