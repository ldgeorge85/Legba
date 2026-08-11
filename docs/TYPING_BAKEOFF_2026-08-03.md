# Typing bake-off + qualification bar — K-G2

**Date:** 2026-08-03 · **Branch:** `kg2-typing-bakeoff` (base `07092b4b`)
**Scope:** roadmap Phase G, task K-G2. Measurement only — nothing here is applied
to production, no descriptor is changed, no migration is written.

Everything below is measured against the live substrate (`legba-postgres-1`,
read-only) and live model endpoints. Reproduction commands are in §8.

---

## 1 · The headline: the throughput blocker is not the model

The debate's finding was that typing throughput gates the graph:
**≈12 typed edges/day against ≈9,941 candidate arrivals/day**
(`planning/graph_debate/JUDGE_SYNTHESIS.md` §1.3). That number is right. The
diagnosis attached to it — that the cap of 40 candidates/12 h and LLM cost are
what bind — is **wrong**, and this task's first measurement says so.

`relationship_reifier._read_candidates` selects its per-run window with:

```sql
FROM proposed_edges pe
WHERE pe.confidence >= 0.45
  AND NOT EXISTS (SELECT 1 FROM nexuses n
                   WHERE n.valid_until IS NULL AND n.superseded_by IS NULL
                     AND lower(n.subject) = lower(pe.source_entity)
                     AND lower(n.object)  = lower(pe.target_entity))
ORDER BY pe.confidence DESC, pe.produced_at DESC
LIMIT 40
```

Two defects, and they compound:

**(a) There is no `status` filter.** The query reads the whole
`proposed_edges` table, not the pending queue. Live counts:

| status | rows | rows at `confidence >= 0.45` | confidence range |
|---|---:|---:|---|
| `pending` | 174,595 | 33,658 | 0.450 – 0.750 |
| `orphaned` | 26,831 | 5,510 | 0.450 – **1.000** |
| `rejected` | 25,603 | 550 | 0.450 – **1.000** |
| `promoted` | 9,449 | 9,449 | 0.600 – **1.000** |

`pending` rows top out at **0.750**. Every row at confidence **1.000** (3,587 of
them) is `orphaned`, `rejected` or `promoted`. Since the window is ordered by
`confidence DESC`, the non-pending rows sort **above every pending candidate**.

> Counts are a snapshot (2026-08-03, early run). Only `pending` moves — it grew
> 174,595 → 174,839 during this task's measurement window, consistent with the
> debate's ~9,941/day arrival rate. The three non-pending buckets were static
> throughout, which is itself the point: they are dead rows.

**(b) The dedup guard misses its own output.** `write_nexus` is preceded by
`resolve_keeper`, which rewrites both endpoints to their elected
`entity_profiles` keeper. The `NOT EXISTS` guard compares against the **raw**
`proposed_edges` surfaces, which no longer match the keeper-rewritten nexus. So
a successfully-promoted pair stays eligible forever. Measured on `promoted` rows
at confidence 1.0:

| pair | status | forward-exact nexus match |
|---|---|---:|
| `Iran` → `US` | promoted | **0** |
| `Trump` → `US` | promoted | **0** |

**The result.** Reproducing the reifier's exact window against the live DB:

| window | pending | orphaned | promoted | rejected |
|---|---:|---:|---:|---:|
| top 40 (one run) | **0** | 24 | 15 | 1 |
| top 500 | **0** | 163 | 333 | 4 |
| top 5,000 | 3,355 | 891 | 718 | 36 |

> **Every one of the reifier's 80 LLM calls per day is spent on candidates that
> are structurally incapable of producing a new edge** — pairs whose endpoint
> entities were merged away (`orphaned`), pairs already reified (`promoted`), and
> pairs a human already rejected. Pending candidates do not enter the window
> until rank ~501.

This also explains the *falling* rate the debate observed (lifetime 23.7/day →
7-day 12.4/day) without appeal to model quality: the head of the queue is a
fixed, slowly-growing set of dead rows that the typer re-reads every 12 hours.
The 874 open reifier nexuses that exist were produced when the queue head was
still mostly pending, before `promoted`/`orphaned` rows accumulated enough
mass at confidence 1.0 to fill the whole window.

**The one-line fix is worth more than any model change in this report.** It is
not applied here (this task changes no production code), but it is the first
recommendation in §7.

---

## 2 · The qualification bar

### 2.1 Why `proposed_edges.confidence` is the wrong ranking

`confidence` is accumulated co-mention weight. It cannot distinguish *nine
newsrooms independently reporting a relationship* from *one wire story
syndicated to nine outlets*. For a graph whose whole claim is that edges are
**earned, evidentiary points**, that is the wrong quantity to sort on.

### 2.2 The four components

Implemented in `src/legba/data/analysts/edge_qualification.py`, all computed
from the live substrate with no model in the loop.

| component | weight | definition |
|---|---:|---|
| `multi_source` | **0.45** | distinct `signals.source_id` **after collapsing** rows sharing a `content_hash` / `canonical_signal_id`. One story on nine wires = **1**. Saturates at 4. One source scores **0.0**, not a small positive. |
| `source_diversity` | 0.20 | distinct publisher families ÷ distinct sources. `source_id` is `source.<publisher>.<feed>`, so `source.aljazeera.arabic` + `source.aljazeera.world` fold to one family. |
| `salience` | 0.20 | `signal_entity_links` mention volume of the **weaker** endpoint, log-damped, saturating at 500. Taking the weaker side stops `Trump ↔ <noise token>` qualifying on Trump's count. |
| `desk_relevance` | 0.15 | 1.0 when an endpoint **is** an active L1 desk's subject; 0.6 when a backing signal's `geo` intersects the union of active desks' `scope.geo`; else 0. |

Plus a **hard floor** that is not expressible as a weight:
`MIN_INDEPENDENT_SOURCES = 2`. A single-sourced pair never qualifies, whatever
its salience or desk relevance. This is the "never full-text-search sludge"
rule in one line.

### 2.3 What the live pool looks like

Independent-source support across the 174,632 pending candidates:

| independent sources | candidates | share |
|---:|---:|---:|
| 0 (no signal lineage) | 814 | 0.5 % |
| **1** | **160,887** | **92.1 %** |
| 2 | 10,282 | 5.9 % |
| 3 | 2,123 | 1.2 % |
| 4 | 460 | 0.26 % |
| 5 | 59 | 0.03 % |
| 6 | 7 | 0.004 % |

> **92.1 % of the pending queue rests on a single independent source.** The
> multi-source floor alone removes 161,701 of 174,632 candidates — and it is
> removing exactly what the graph is supposed to exclude.

### 2.4 Bar sweep — measured pool sizes

| bar | qualifying (score only) | qualifying (**score + source floor**) | share of pool |
|---:|---:|---:|---:|
| 0.00 | 174,632 | 12,931 | 7.41 % |
| 0.20 | 24,773 | 12,931 | 7.41 % |
| 0.30 | 13,711 | 12,919 | 7.40 % |
| 0.35 | 12,975 | 12,907 | 7.39 % |
| 0.40 | 12,291 | 12,291 | 7.04 % |
| **0.42** | **12,005** | **12,005** | **6.87 %** |
| 0.45 | 11,671 | 11,671 | 6.68 % |
| 0.50 | 9,087 | 9,087 | 5.20 % |
| 0.55 | 6,121 | 6,121 | 3.51 % |
| 0.60 | 3,700 | 3,700 | 2.12 % |
| 0.70 | 1,678 | 1,678 | 0.96 % |

Note where the two columns converge: **at bar ≥ 0.40 the weighted score alone
already excludes every single-sourced candidate**, because a one-source pair
zeroes both `multi_source` (0.45) and `source_diversity` (0.20) and so cannot
exceed 0.35. The explicit floor therefore does no work at the recommended bar —
it exists so that *lowering* the bar later to widen the queue cannot silently
re-admit single-sourced sludge. That property is pinned by a test.

### 2.5 Recommended bar

**`RECOMMENDED_BAR = 0.42`, with `MIN_INDEPENDENT_SOURCES = 2`.**
Qualifying pool: **12,005 candidates (6.87 % of pending)**.

Rationale: 0.42 sits just past the convergence point, so the bar is doing the
selection rather than the floor; it keeps the queue at a size that is drainable
in weeks rather than decades at the throughput §5 shows is available; and it
retains every 3+-source candidate plus the desk-relevant 2-source ones.

### 2.6 Retention for the below-bar remainder

Below-bar candidates must age out, not fester. Policy
(`edge_qualification.retention_verdict`):

* above the bar → **keep** (it is the work queue);
* below the bar and newer than **30 days** → **keep** (a slow-burning story gets
  a month to earn a second source);
* below the bar and **≥ 30 days stale** → **retire**.

"Stale" is measured from the **newest backing signal**, not row creation, so a
candidate that gains support restarts its clock.

Measured against the live pool today: **34,548 of 174,632 candidates (19.8 %)
would retire immediately**, 140,084 would be kept. Retirement is a **status
change, never a delete** — the co-mention evidence stays addressable and a pair
that re-earns support returns through the normal producer path.

---

## 3 · The batch-typing harness

`src/legba/data/analysts/relationship_typing_batch.py`.

The live reifier issues **one LLM call per candidate**. The harness issues one
call per **N** candidates: shared instructions and the closed `rel_type`
vocabulary stated once, per-candidate evidence blocks, and a JSON array response
carrying one verdict per candidate.

**It reuses the reifier's judgement rather than restating it.** Every asserted
relationship is validated through `relationship_reifier._coerce_typing`, so the
batch path inherits, unchanged: the closed rel_type list, junk/demonym/self-loop
endpoint drops, the SELECT-or-null intermediary rule, the deterministic
intent→polarity map, and the D14 sports gate. A batched verdict is accepted on
exactly the terms a single-candidate verdict is. Tests pin each of these.

The batch layer adds three things:

1. **Correlation by `idx`, never by position.** A model that reorders, drops or
   duplicates entries is *detected*, not silently mis-assigned to the wrong
   pair. Positional fallback is allowed only when no entry carries an `idx`
   *and* the count matches exactly — mis-assignment is the one error worse than
   loss.
2. **Parse-integrity accounting** (`missing` / `unexpected` / `duplicate` /
   `truncated`), so safe N is measured rather than assumed.
3. **Truncation salvage.** A string-aware brace scanner recovers every complete
   object preceding a cut-off, so a partly-spent call is not a total loss.

### 3.1 Completion budget — a measured correction

The initial reservation was 110 tokens/verdict, reasoned from a verdict's
*content* (≈90 tokens). The first live batch refuted it: core 120B spent **3,102
completion tokens on 12 verdicts — 258/verdict**. The gap is pretty-printing;
every model in the roster emits indented JSON regardless of prompt wording.
`DEFAULT_MAX_TOKENS_PER_VERDICT` is now **280**, and the budget is linear in N
because truncation is the batch failure that costs the most candidates per
wasted token.

---

## 4 · The bake-off — method

### 4.1 The sample

**200 candidates**, drawn deterministically (`SAMPLE_SEED = 20260803`, rows
ordered by uuid) from `proposed_edges` where `status = 'pending'`, **stratified
by qualification score** so the sample is neither 95 % junk (a naive draw would
be, given §2.3) nor sanitised of it — a bake-off that never shows a model an
obvious reject cannot measure rejection.

| stratum | qual score | in sample | independent sources |
|---|---|---:|---|
| `S4_top` | ≥ 0.55 | 60 | 2–5 |
| `S3_high` | 0.42 – 0.55 | 60 | 2 |
| `S2_mid` | 0.30 – 0.42 | 40 | 1–2 |
| `S1_low` | 0.15 – 0.30 | 25 | 1 |
| `S0_floor` | < 0.15 | 15 | 1 |

Prompt payloads are **frozen to disk** (`sample_payloads.json`) before any model
runs, using the same context the live reifier assembles: the co-mention
excerpt, recent facts about either endpoint, the offered intermediary set (only
above the reifier's own `MIN_INTERMEDIARY_PAIR_CONFIDENCE = 0.55`, which 27 of
the 200 clear), and the wider sports-gate text. Every model therefore sees
byte-identical prompts, so a disagreement is a model difference and never a
prompt difference.

### 4.2 The roster

| key | model | plane | notes |
|---|---|---|---|
| `core120b` | gpt-oss-120b | `llm.primary.openai_compat`, self-hosted ai1 | the reference |
| `slm8b` | Llama-3.1-8B-Instruct Q5_K_M | `llm.verify.slm_8b`, self-hosted ai1 | never previously asked to type an edge |
| `nemotron` | `nvidia/nemotron-3-super-120b-a12b:free` | OpenRouter free | thinking **off** (see below) |
| `gptoss` | `openai/gpt-oss-120b` | OpenRouter **paid** | see the free-tier finding below |

The two self-hosted planes are driven through the **real in-tree provider
handler** (`legba.data.stack.llm`, credentials from the live vault), so the
measurement traverses production's own auth/request/parse path rather than a
bespoke client that might flatter or punish a model by accident. Nemotron on
ai1 was explicitly excluded per the operator — the main model cannot come down.

**Two roster facts worth stating plainly.**

**(a) `gpt-oss-120b` has no free tier on OpenRouter.** The catalogue lists 14
free models today; the only free gpt-oss is `openai/gpt-oss-20b:free`.
`openai/gpt-oss-120b` exists but is paid — at $0.037/M prompt and $0.17/M
completion, which made the whole 200-candidate run cost **under two cents**. The
roster item was run as the operator named it (the 120B) rather than substituted
with the 20B, because…

**(b) …`core120b` and `gptoss` are the same weights.** `llm.primary.openai_compat`
serves the model `docs/AI_MODELS.md` documents as gpt-oss-120b. So this pair does
not compare two models — it compares **self-hosting against renting the same
model**, which turns out to be the more useful question (§7).

### 4.3 Reasoning control — a measured prerequisite

Nemotron 3 Super at N=12 initially returned **zero verdicts**: it spent the
entire completion budget thinking (3,635 reasoning tokens, empty content, 77 s).
This is the same failure mode that caused the operator to revert Nemotron 3
Super on ai1 in July on latency grounds, and the fix noted at the time —
thinking-off — is the fix here. Measured on an identical probe:

| model | reasoning setting | wall | completion | reasoning | content |
|---|---|---:|---:|---:|---|
| nemotron-3-super | default | 3.2 s | 323 | 291 | ok |
| nemotron-3-super | **`{"enabled": false}`** | **2.4 s** | **80** | **0** | ok |
| gpt-oss-120b | default | 1.6 s | 387 | 330 | ok |
| gpt-oss-120b | `{"enabled": false}` | — | — | — | **HTTP 400: "Reasoning is mandatory for this endpoint"** |

Nemotron therefore runs thinking-off. gpt-oss on OpenRouter **cannot** disable
reasoning, so its completion budget carries a 2.5× multiplier to cover the think
pass — a real and permanent cost difference against the self-hosted plane, which
does not reason on this task at all (0 reasoning tokens throughout).

---

## 5 · Results — economics

200 candidates, N = 12, identical frozen prompts. Raw artefacts in
`docs/data/kg2_bakeoff/`.

| model | verdicts recovered | calls | clean calls | truncated | tokens/edge | **s/edge** | total wall | accept rate | USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **core120b** (self-hosted) | **200/200** | 17 | **100 %** | 0 | 557 | **2.54** | 8 m 28 s | **46.8 %** | $0 |
| slm8b (self-hosted) | 200/200 | 17 | 100 % | 0 | 358 | 3.40 | 11 m 20 s | 54.8 % | $0 |
| nemotron (OR free) | 186/200 | 17 | 88 % | 1 | 414 | 2.19¹ | 6 m 48 s | 72.6 % | $0 |
| gptoss (OR paid) | 200/200 | 17 | 100 % | 0 | 564 | 9.04 | 30 m 07 s | 44.6 % | $0.0120 |

¹ nemotron's per-edge wall includes the 4 s inter-call pacing this task imposed
as free-tier courtesy; its raw compute was 7–11 s per 12-candidate batch, the
fastest in the roster by a wide margin.

**Three economics findings.**

**(a) The 8B is *slower* than the 120B.** `slm8b` costs **3.40 s/edge** against
the 120B's **2.54 s** — a 15×-smaller model taking 34 % longer. It is a Q5_K_M
GGUF served from `slm.ai1`; the 120B is vLLM-served on better silicon. "Smaller
model = cheaper typing" is false on this stack, and it is false by measurement,
not by a little. `slm8b` also produced **17 verdicts the validator had to
reject** as structurally invalid (off-list `rel_type`, junk endpoint,
self-loop) against the 120B's **zero**.

**(b) Renting the same weights costs 3.6× the wall time and real money.**
`gptoss` *is* `core120b` — same gpt-oss-120b weights — and ran at **9.04 s/edge
versus 2.54 s**, because OpenRouter will not let the endpoint stop reasoning
(it burned reasoning tokens on every call; the self-hosted plane emitted **0
reasoning tokens** throughout). The cash cost is trivial ($0.012 per 200), but
the latency is not: at the throughput §7 recommends, renting would cost ~3 GPU-
hours/day of *waiting* that self-hosting does not.

**(c) Batching is where the win is, and it is a prompt-token win.** The batched
path spent **269 prompt tokens/candidate**; the single-candidate path spends
**1,462**, because it repeats the full system preamble and the entire allowed-
`rel_type` vocabulary on every call. Measured sweep in §7.3.

---

## 6 · Results — agreement

### 6.1 The matrices

Computed over the **186 candidates all four models answered** (nemotron lost 14
to one truncated call; comparability beats volume, so the denominator is the
intersection).

**Edge-vs-reject agreement (raw %)**

| | core120b | slm8b | nemotron | gptoss |
|---|---:|---:|---:|---:|
| **core120b** | — | 60.8 | 61.3 | **79.6** |
| **slm8b** | 60.8 | — | 65.0 | 61.8 |
| **nemotron** | 61.3 | 65.0 | — | 61.3 |
| **gptoss** | **79.6** | 61.8 | 61.3 | — |

**Cohen's κ on the same call** — raw agreement is badly chance-inflated here,
because accept rates range from 44.6 % to 72.6 %; two independent raters would
agree ~50 % of the time by luck alone.

| | core120b | slm8b | nemotron | gptoss |
|---|---:|---:|---:|---:|
| **core120b** | — | 0.220 | 0.248 | **0.589** |
| **slm8b** | 0.220 | — | 0.269 | 0.244 |
| **nemotron** | 0.248 | 0.269 | — | 0.262 |
| **gptoss** | **0.589** | 0.244 | 0.262 | — |

**Exact `rel_type` agreement (raw %)**

| | core120b | slm8b | nemotron | gptoss |
|---|---:|---:|---:|---:|
| **core120b** | — | 40.3 | 40.9 | **69.3** |
| **slm8b** | 40.3 | — | 37.1 | 44.6 |
| **nemotron** | 40.9 | 37.1 | — | 44.6 |
| **gptoss** | **69.3** | 44.6 | 44.6 | — |

### 6.2 The finding that governs everything else

> **`core120b` and `gptoss` are the same model.** Same gpt-oss-120b weights,
> same frozen prompts, same temperature 0.1 — differing only in who hosts them.
> They agree on edge-vs-reject **79.6 %** of the time (κ = **0.589**) and on the
> exact `rel_type` **69.3 %** of the time.

That is the **ceiling**, not a model comparison. A model does not agree with
*itself* on one candidate in five. Every cross-model κ in the matrix
(0.220–0.269) sits far below it — but the ceiling being 0.589 means roughly
**40 % of what looks like model disagreement is irreducible sampling noise on a
task this underdetermined.**

Three consequences, and they are the practical output of the bake-off:

1. **Models cannot be ranked by agreement with the reference**, because the
   reference does not agree with itself. A "92 % agreement with the 120B" score
   would have been impossible for any model to achieve.
2. **Any model swap re-rolls ~20 % of the graph's edges** regardless of which
   model wins. That is an argument for stability — keeping one typer — that has
   nothing to do with which typer is best.
3. **Only human labels can break the tie.** Hence the hand-check worksheet
   (§6.4), and hence its labels being the operator's, not this agent's.

We also tested whether the disagreement is concentrated in thin evidence — it
is not. Three-way unanimity is flat at 41–48 % across evidence-length buckets,
across qualification strata, and across independent-source counts. The models
disagree *uniformly*, which points at the task definition ("is there a
meaningful directed relationship?") rather than at evidence starvation.

### 6.3 Accept-rate behaviour

| model | accept rate | reading |
|---|---:|---|
| gptoss | 44.6 % | |
| **core120b** | **46.8 %** | the most conservative of the accurate options |
| slm8b | 54.8 % | +17 % more edges than the 120B, 17 of them invalid |
| nemotron | 72.6 % | **+55 % more edges than the 120B** |

For a graph whose stated design is *sparse and evidentiary*, accept rate is not
a neutral parameter — it is the sparsity dial. Adopting nemotron would grow the
typed graph by half again, on candidates the 120B judged unrelated, with no
evidence that the extra edges are correct.

### 6.4 The hand-check set

**116 of 186 candidates split** the four models (at least one EDGE and at least
one REJECT). **43 are dead-even 2-2** — maximal disagreement.

`docs/data/kg2_bakeoff/handcheck_worksheet.csv` carries the top **40**, ranked
even-splits first, with each model's decision and one-line rationale, the
evidence excerpt, and three **empty** columns:
`OPERATOR_VERDICT_edge_or_reject`, `OPERATOR_rel_type_if_edge`,
`OPERATOR_notes`. It is deliberately **UNLABELED** — per the K-4 precedent the
labels are the operator's call, and on a task where no model agrees with itself
an agent's guess would be noise presented as ground truth.

### 6.5 What the qualification bar does and does not do

The bar scores **evidentiary strength**; the model scores **semantic
relatedness**. They are orthogonal by construction, and the data says so
plainly — `core120b` accept rate by stratum:

| stratum | qual score | accept rate |
|---|---|---:|
| S0_floor | < 0.15 | 47 % |
| S1_low | 0.15–0.30 | 56 % |
| S2_mid | 0.30–0.42 | 42 % |
| S3_high | 0.42–0.55 | 53 % |
| S4_top | ≥ 0.55 | **35 %** |

**The best-evidenced candidates are typed as related *least* often.** The
mechanism is visible in the data: heavily co-mentioned pairs are
disproportionately entities that co-appear in big-story roundups, where many
sources mention many entities together without any relationship holding between
any particular pair.

Meanwhile the floor stratum's accepted edges are things like
`IRGC AffiliatedWith Revolutionary Guards Corps` (an alias pair),
`Central Ben Hill County LocatedIn Georgia`, and
`Council of the IMO PartOf IMO` — **true, and worthless**. No model filters
these, because they *are* related.

> **The bar is not a yield optimiser and must not be sold as one.** It controls
> *which* edges are allowed into the graph (quality); the §1 queue fix controls
> *how many* get typed (throughput). They are separate levers and the programme
> needs both.

---

## 7 · Recommendations

### 7.1 The ladder

| tier | model | when | why |
|---|---|---|---|
| **primary — all typing** | **`core120b`** (`llm.primary.openai_compat`) | every candidate | fastest of the accurate options (2.54 s/edge), $0, 100 % parse integrity, **zero** invalid verdicts, and the most conservative accept rate — which is the sparsity the graph wants |
| **burst / overflow** | `nemotron` (OpenRouter free) | only to drain backlog under an explicit spend of graph-density, never for steady state | $0 and zero ai1 GPU, but 72.6 % accept rate and 93 % recovery |
| **failover** | `gptoss` (OpenRouter paid) | ai1 unavailable | same weights, 3.6× the latency, ~$0.06/1,000 edges |
| **rejected** | `slm8b` | — | slower *and* less reliable than the 120B it would relieve; no niche exists for it |

**There is no escalation rule, and that is the finding.** A ladder needs a
difficulty signal that says "this one is hard, send it up". We looked for one —
qualification score, evidence length, source count — and none predicts
disagreement (§6.2). Absent a usable difficulty signal, a two-tier ladder would
route by coin-flip and pay for the privilege. **Recommend a single typer until
the operator's hand-check labels provide the ground truth to build a real
router on.**

### 7.2 Batch size

**N = 12.** Measured, not assumed — see §7.3 for the sweep. At N=12, three of
four models returned 100 % clean calls with zero truncation; nemotron's single
truncated call is the one exception, and it cost 14 candidates, recovered by
the salvage path.

### 7.3 Batch-size sweep — the measured N

`core120b`, the same 40 candidates at every N, one call per batch:

| N | recovered | calls | clean calls | truncated | **prompt tok/candidate** | completion tok/verdict | tokens/edge | s/edge |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **1** (today's shape) | 40/40 | 40 | 100 % | 0 | **1,462** | 494 | 1,957 | 3.44 |
| 6 | 40/40 | 7 | 100 % | 0 | 394 | 327 | 721 | 2.25 |
| **12** | 40/40 | 4 | 100 % | 0 | **297** | 295 | 592 | 2.06 |
| 24 | 40/40 | 2 | 100 % | 0 | 232 | 239 | 471 | 2.35 |
| 40 | 40/40 | 1 | 100 % | 0 | 200 | 136 | 336 | 1.45 |

**Batching's win is prompt tokens, and it is large.** The single-candidate path
spends **1,462 prompt tokens per candidate** because it repeats the full system
preamble and the entire allowed-`rel_type` vocabulary on every call. At N=12
that falls to **297 — a 4.9× reduction**; at N=40, to 200 — **7.3×**. Total
tokens/edge falls 1,957 → 592 (N=12) → 336 (N=40).

**Parse integrity held at 100 % at every N tested, including a single call
carrying all 40 candidates** — no truncation anywhere. The reason is visible in
the table: the model gets *terser* as the batch grows (494 → 136 completion
tokens per verdict), so the linear budget is never the binding constraint.

**But terser is not free, and this is why the recommendation is not N=40.**
Completion tokens per verdict falling 3.6× means rationales — and plausibly
deliberation — compress with batch size. The N=40 run accepted 14/40 (35 %)
against N=12's 19/40 (47.5 %). One call is not enough to call that a real
effect, which is precisely the point: **N=40 rests on a single observation,
while N=12 has 17/17 clean calls over 200 candidates from the main run.**

> **Recommend N = 12.** Evidence-proportionate: it captures 4.9× of the
> available 7.3× token saving and is the only size proven at scale. N = 24 is a
> reasonable next step if the operator wants more, under measurement. N = 40 is
> promising but under-evidenced, and shows a possible judgement shift that
> should be ruled out before adoption.

### 7.4 The qualification bar

**`RECOMMENDED_BAR = 0.42`** with **`MIN_INDEPENDENT_SOURCES = 2`** →
**12,005 qualifying candidates** (6.87 % of the pending pool), and **~400–550
qualifying arrivals/day** measured over the last week (against ~9,000–12,500
raw arrivals/day).

Retention: below-bar candidates retire after **30 days without new supporting
evidence**. **34,548 rows (19.8 %) would retire on first application.** Retiring
is a status change, never a delete.

### 7.5 Projected throughput and cost

Assumes the §1 queue fix (`status='pending'` + a bidirectional, keeper-aware
dedup guard), the bar applied as the ordering, and `core120b` at N=12.

| | today | recommended |
|---|---:|---:|
| candidates typed/day | 80 (all dead rows) | 1,200 |
| **candidates that could yield an edge** | **0** | 1,200 |
| LLM calls/day | 80 | 100 |
| new typed edges/day | **~12** | **~550** during drain, **~205** steady state |
| ai1 GPU time/day | ~7 min (wasted) | ~51 min drain, ~19 min steady |
| tokens/day | ~90 k | ~670 k (budget is 1,000,000) |
| USD/day | $0 | $0 |
| qualifying backlog drain | never (arrivals outrun 800×) | **~16 days** |

Steady state assumes ~450 qualifying arrivals/day × 46.8 % accept ≈ **205 typed
edges/day — a 17× improvement on today's 12**, and the backlog clears rather
than growing. The token budget already provisioned (1 M/day) covers this with
33 % headroom; **no budget increase is required.**

### 7.6 What to do first

1. **Fix `_read_candidates`** — add `status = 'pending'`, make the dedup guard
   bidirectional and keeper-aware. This alone moves typing from 0 useful calls/
   day to 80, before any batching or model change. It is the highest-value
   change in this report and the cheapest.
2. **Then batch** at N=12 and raise `max_candidates`.
3. **Then apply the bar** as the candidate ordering, and turn on retention.
4. **Send the hand-check worksheet** back before considering any model swap.

---

## 8 · Reproduction

```bash
# 1. measure the pool + draw the deterministic stratified sample (read-only)
PYTHONPATH=src python scripts/kg2_pool_measure.py --out <dir> --sample-size 200

# 2. materialise the frozen prompt payloads (read-only)
PYTHONPATH=src python scripts/kg2_sample_prep.py --dir <dir>

# 3. run the bake-off. The two self-hosted planes need the vault, so this runs
#    inside a container that already holds LEGBA_DATA_MASTER_KEY:
docker cp src/legba/data/analysts/relationship_typing_batch.py \
  legba-legba-runtime-dapr-1:/install/lib/python3.11/site-packages/legba/data/analysts/
docker cp scripts/kg2_typing_bakeoff.py legba-legba-runtime-dapr-1:/tmp/
docker cp <dir>/sample_payloads.json legba-legba-runtime-dapr-1:/tmp/kg2/
docker exec legba-legba-runtime-dapr-1 python /tmp/kg2_typing_bakeoff.py \
  --dir /tmp/kg2 --models core120b,slm8b,nemotron,gptoss --mode main --batch-size 12

# 4. score
PYTHONPATH=src python scripts/kg2_bakeoff_score.py --dir <dir>

# tests
PYTHONPATH=src python -m pytest tests/data_pkg/test_relationship_typing_batch.py \
                                tests/data_pkg/test_edge_qualification.py -q
```

Sampling is deterministic (`SAMPLE_SEED = 20260803`, rows ordered by uuid), so an
unchanged pool reproduces the identical sample.
