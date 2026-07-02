# Data Flow — the whole river, end to end

> **Thesis.** Ingest once, enrich once, match many, reason on a slice, ground
> against curated context, **cite every claim, verify every claim**, then
> compose bottom-up (units → per-country → world → banded scorecard) and grade.

This is the conceptual through-line that links the deep docs. The mechanics live
elsewhere and are not repeated here — each section points at the canonical
treatment: `FLOWS.md` (Flow N, the life-of-a-X walkthroughs), `ANALYSIS.md`
(§N, the analysis plane), `ACQUISITION.md` (§N, how data enters), and
`AGENCY_GATING_MODEL.md` (how an analyst is allowed to act). When this doc and a
deep doc disagree, the deep doc wins — this one is the map, not the territory.

This is also the **only** doc that unifies the two fire paths that are otherwise
split across `FLOWS.md` (the Dapr-reminder **cadence** cycle) and `ANALYSIS.md`
(the reactive **trigger** plane). They are two doors into one room; §2 reconciles
them.

What sets this system apart is the **discipline**, not the data: every claim
that reaches a trust surface is cited to a source, checked by a mandatory
faithfulness pass, and auditable hop-by-hop back to the original signal. The
shown exemplar domain is geopolitics over the G20 plus a high-consequence watch
tier — four bounded reasoning units fanning out across **24 country desks** (a
"desk" is a scoped subject a set of analysts work, not a surveilled entity) — but
the domain is configured by declaration (YAML descriptors), not baked into code.

To ground the scale: the substrate is a continuously growing temporal knowledge
graph — on the order of **~4.6k facts** and **~4.9k nexuses** at the time of
writing, with `valid_from` / `valid_until` / decay, accumulating as sources are
polled. Ingestion runs **~50 poll sources** (RSS / API / bulk) on cron. The
three-feed set (BBC / Deutsche Welle / Al Jazeera) is the cold-start
verification minimum, **not** the deployed scope; a fresh instance reaches
current scope by running the full source catalog
(`scripts/bringup_register_source_catalog.py`), not the 3-feed bootstrap.

---

## 1. The river, end to end

Read left to right; every arrow is a substrate boundary, not a function call.
The left half is ingestion-to-output; the right half is the **product spine**
(§4) — bottom-up composition over already-verified claims.

```
SOURCE ─► canonical Signal ─► predicate fan-out ─► the (analyst,target) fire ─► typed Output
        (Tier-1 inline        (coarse NATS subject   (cadence reminder OR         (12 OutputKinds →
         enrich, once)         + SQL WHERE +          reactive coalescer,          facts / nexuses /
                               Starlark residual)     one cooldown CAS)            situations / …)

                         … then the product spine, bottom-up, over VERIFIED claims:

    4 bounded UNITS ─► per-country COMPOSITION ─► world COMPOSITION ─► banded SCORECARD
    (inline_target,     (country_composition,       (world_assessor,      (scorecard_producer,
     cite + verify       INNER JOIN on the           composes country      deterministic rules
     each finding)       verify critique)            reads)                over verified claims)
```

**SOURCE → canonical Signal.** A `SourceCore` actor pulls raw entries on a
cadence and runs the per-source baseline **once** over each one, producing a
single canonical, target-agnostic `Signal` (no `target_id` — an observation, not
an interpretation) via `write_canonical_signal`. The baseline is the **Tier-1
inline** analysis tier: deterministic, no analyst LLM, run synchronously
per-Signal *before* fan-out. Its enrichment chain (`language_detect → geocode →
ner → classify → source_credibility → ingest_dedupe → fact_extractor`) writes
altitude-0 product in place: the enriched signal columns, ingestion `facts`, and
entity rows + `signal_entity_links`. (A webhook front and an S1 NATS
accept-and-enqueue inbound path — `inbound_drain.py` — also exist, but they are
**dormant plumbing** until a live webhook source is wired; the poll path is what
runs today.) See `FLOWS.md` Flow 1 and `ACQUISITION.md` §1–§3.

**Signal → predicate fan-out.** The written signal is published immediately to a
coarse NATS subject (`legba.signals.>`). Each target has one aggregated durable
pull consumer bound to the union of its subscription subjects; delivery is then
narrowed **exactly** by a two-stage match — an indexed SQL `WHERE` over `geo` /
`tags` / `entity_classes` / `languages` / `modalities`, then a Starlark
**residual** predicate evaluated under a 5 ms wall-clock budget. Coarse subject
narrows *delivery*; SQL + Starlark decide the *match*. See `FLOWS.md` Flow 1
(steps 5–8) and `ACQUISITION.md` §4.

**Fan-out → the (analyst,target) fire.** This is where the dual path lives; §2
reconciles it. A matched signal marks an `(analyst, target)` pair dirty and the
analyst fires on a gate, not per-signal.

**The fire → typed Output.** The analyst reads its substrate slice, runs its
method, and writes one typed output validated against its kind's pydantic model.
There are **exactly twelve** `OutputKind` values — `finding · situation ·
hypothesis · prediction · alert · meta_finding · critique · fact · nexus ·
prompt_module_candidate · journal · scorecard` (`data/provenance/kinds.py`) —
routed by the universal write path to `situations` / `hypotheses` / the
knowledge-layer `facts` / `nexuses` tables, the dedicated `journal_entries`
table, or the generic `analyst_outputs` table. The `journal` kind sits
**off-chain** (§10); the newest kind, `scorecard`, is the deterministic banded
verdict (§4). There is deliberately no `signal` kind. See `FLOWS.md` Flow 2
(steps 10–13) and `ARCHITECTURE.md` §8.1.

---

## 2. The unified fire path — one room, two doors

`ANALYSIS.md` describes a reactive **trigger** plane; `FLOWS.md` describes a Dapr
**cadence** cycle. They are not two systems — they are two ways the *same*
`(analyst, target)` pair becomes eligible to fire, reconciled at the dispatch seam.

- **Cadence door (`FLOWS.md` Flow 2).** On activation the primary actor registers a
  single Dapr reminder (`run_cadence`). When it ticks, `_cadence_targets()` resolves
  the matched targets and fans them to distinct worker actors. This is the *floor* —
  a quiet target still gets re-evaluated on a schedule, so a slow drip eventually
  fires. (The presence or absence of a `subscription.targets` block is what makes a
  run per-target vs. a single global META run — the four units and the per-country
  composition fan out; `world_assessor` and `scorecard_producer` run globally.)

- **Reactive door (`ANALYSIS.md` §2).** A matched signal (or a new upstream finding —
  event-class `derived`, just another signal on the stream) marks the pair dirty via
  the coalescer (`runtime/triggers/`). It fires on whichever gate trips first —
  **severity** (a critical signal wakes it now), **accumulation** (N pending fire the
  batch together), or **cadence** (the periodic tick, only when something is pending)
  — clamped by a per-pair **cooldown** that is the thrash ceiling.

The two doors converge on one lock. Both the signal path and the cadence tick can
independently *decide* to fire the same pair; the coalescer **CAS-claims** the fire
on the accumulator's last-fired anchor (`claim_fire`), so exactly one worker wins
and the loser backs off (`ANALYSIS.md` §2.3). The worker run then re-checks a
**per-target cooldown** keyed by `target_filter` before doing work, with a slack
band that absorbs drift when `cooldown ≈ cadence` (`FLOWS.md` Flow 2, step 6). Net:
*coalesce + CAS dedup* means a pair fires once per cooldown window no matter how many
doors opened. LLM-bearing analysts are floored to a minimum batch and never fire
per-signal (`ANALYSIS.md` §2.4).

---

## 3. The two tiers — the cost-decoupling spine

The river runs at two altitudes, and keeping them separate is the whole point: cheap
deterministic work happens once per observation; expensive reasoning happens once per
*slice*, not once per signal.

- **Tier 1 — inline, deterministic, per-Signal, pre-fan-out.** The `data/filters/`
  baseline chain (§1). spaCy NER, geo resolution, relation facts, dedup. No
  analyst LLM. Writes altitude-0 product (enriched signal, ingestion facts, entity
  links). This is "enrich once" — every downstream target reads the same enriched
  row. See `ARCHITECTURE.md` §0/§6.1 and `ACQUISITION.md` §3.

- **Tier 2 — slice / cadence, reasoning, per-(analyst,target).** The
  `data/analysts/` kinds read an accumulated substrate **slice** on a fire and
  *reason* over it — never per-signal. This is where LLM cost lands, and the
  coalescer (§2) is precisely the device that decouples that cost from signal
  arrival rate. The core analyst plane is a self-hosted gpt-oss-120B
  (`llm.primary.openai_compat`, $0); Claude Opus 4.8 is reserved for the billed
  `consult` / `deep_consult` surfaces only. See `ANALYSIS.md` §3 and `FLOWS.md`
  Flow 2.

The coalescer is the membrane between the tiers: Tier 1 produces a stream of cheap
enriched signals; Tier 2 consumes *batches* of them on a clamped schedule.

---

## 4. The product spine — units → composition → scorecard

The product is not any single verdict-from-nowhere pass — it is a **bottom-up
composition** in which nothing rises to a higher altitude until it has been cited
and verified at the lower one. Four layers, each reading only the verified output
of the one below.

1. **Four bounded reasoning UNITS** (`kind: inline_target` LLM analysts):
   `leadership_transition`, `energy_security`, `escalation`,
   `narrative_coordination`. Each is scoped across **24 country desks** by a
   `has_tag("g20") or has_tag("watch")` fan-out — the 19 G20 countries plus a
   high-consequence **watch tier** (Israel, Iran, Ukraine, Taiwan, North Korea;
   descriptor ids `country_watch_il/ir/ua/tw/kp`) — and answers **one narrow
   question**. Adding a desk is register-a-target: no code change. Per run it
   ASSEMBLEs a cited signal slice (72h window) plus a grounding preamble of
   accumulated substrate context (§6), then cited-SYNTHESIZEs a strict-JSON
   `FindingPayload` whose prose carries `[N]` citation markers mapped to signal
   ids, runs the **mandatory faithfulness verify** (§5), and folds
   `effective_confidence`. Skill is a **per-unit** number, not a platform boast
   (§7). The units stagger across the clock (e.g. 01:00 / 13:00 UTC) so the four
   spread the shared per-day token budget. They supersede the retired monolithic
   `country_assessor` one-pager (§ retirements below).

2. **Per-country COMPOSITION** (`country_composition`, kind
   `meta_findings_synthesizer`, same `g20`/`watch` fan-out as the units): one
   second-order finding per desk that reads THAT desk's four verified units and
   writes a hedged, cited synthesis (`[[ref:N]]` markers to the sub-claim
   findings). Its READ_SLICE **INNER JOINs on the faithfulness critique above the
   floor**, so an unverified sub-claim never enters the composition. A desk whose
   units produced no verify-passed sub-claim yields an empty slice → an honest
   `confidence=0.0` "no source findings to synthesize" finding, never an invented
   read.

3. **World COMPOSITION** (`world_assessor`, repointed to
   `meta_findings_synthesizer`): one global run per tick that composes over the
   per-country reads into a cited, hedged world view, surfacing cross-country
   disagreement rather than averaging it. It drills country → units → source.
   This is **not** the old raw-signal executive one-pager — that
   verdict-from-nowhere framing was retired and the analyst graduated into the
   composition (`SEAMS.md` #34).

4. **Banded SCORECARD** (`scorecard_producer`, deterministic META, the 12th
   `OutputKind` `scorecard`): each tick it side-writes **one banded row per
   active desk tagged `g20`/`watch`** from a few high-precision RULES over already-verified
   claims — the `severity:<level>` tag × the folded `effective_confidence`,
   **demote-never-promote**, over a **14-day** verified-claim window. Every band
   NAMES the verified-claim id it rests on; a dimension with no qualifying
   verified claim reads **`insufficient-evidence`** with an explicit machine
   reason (never a fabricated band), and a per-claim faithfulness below the floor
   demotes to **`low-faithfulness`**. Pure SQL, no LLM, $0. It stays deterministic
   precisely so the honest top cannot hallucinate.

**Honest read of the live scorecard:** it is a MIX, by design. Some countries
band; others read all-`insufficient-evidence` — e.g. the US, because its unit
faithfulness is genuinely low, so the demote-never-promote rules refuse to
manufacture a band. That is the intended behavior, not a gap.

---

## 5. The mandatory faithfulness verify — the gate every claim passes

Legba MEASURES **groundedness**, not truth: the verify pass asks *does each claim
follow from its cited evidence?* — not *is the claim true in the world?* Say so
plainly; it is the entire product thesis.

Every cited finding is scored for faithfulness in `[0,1]` by two layers
(`data/provenance/verify.py`):

- A **deterministic citation-presence floor** (always on): every fact-asserting
  clause in the prose is checked against the resolved `data['citations']` bridge.
  A claim that asserts a fact with no `[N]` marker, or whose marker resolves to no
  real signal id, is an UNSUPPORTED span; the score is the fraction of checkable
  claims that are supported. A planted fabrication with no real citation is flagged
  unsupported.
- An **optional LLM judge** — currently the same core model
  (`llm.primary.openai_compat`, gpt-oss-120B) that produced the finding, **not**
  cross-family — flag-gated (`LEGBA_VERIFY_LLM_JUDGE`, code default off), that
  refines per-claim verdicts when enabled. Same-model judging is a deliberate,
  temporary choice (the earlier cross-family 8B judge
  `legba-slm`/`llm.verify.slm_8b` proved too weak; known limitation — it shares
  the producer's blind spots; a dedicated reasoning judge is planned). When the
  flag is off or the judge is unreachable, the result **degrades to the floor and
  is labelled `judge-unavailable`** — it never fabricates a number.

The verdict persists as a `critique`, so the existing actuation gate consumes it:
`effective_confidence = min(confidence, faithfulness_score)` is folded at read
time and gates a visible low-confidence tier — it **never hard-deletes**. This
fold is what the composition INNER JOIN (§4) and the scorecard bands (§4) key on,
so an unfaithful claim is quietly down-weighted out of everything above it while
staying auditable. See `ANALYSIS.md` §6.2 and `SEAMS.md` (the verify pass).

---

## 6. Grounding — the stale-cutoff corrector

The core analyst LLM has a training cutoff that predates the present, so left alone
it backfills *current* world facts (who holds office, which alliances hold, the state
of an ongoing conflict) from a stale prior — observed live, an assessor called the
sitting US president a "former" president. The signal slice rarely restates such
background, so the model has nothing in-context to correct it.

Grounding fixes this by **reusing the substrate as its own knowledge store** rather
than adding one. The temporal facts (`valid_from` / `valid_until` / `superseded_by`)
and polarity-signed nexuses (a `+`/`−` edge sign, not a cryptographic signature),
seeded from the curated `world_baseline` adapter and the live
`wikidata_leaders` adapter, hold the temporally-honest "who holds office now". A
**GROUND phase** runs between PLAN and REASON for `inline_target` analysts that
declare `grounding.enabled: true` (all four units do), prepending a dated
"AUTHORITATIVE CURRENT CONTEXT" preamble — e.g. "US head of government: Trump since
2025-01-20; US in active conflict with Iran since 2026-02-28; NATO member since
1949" — which also SUPERSEDES stale model priors. The preamble is restricted to
`source_type IN ('seed','curated')`; machine-extracted ingestion facts are floored at
a conservative `_INGESTION_DEFAULT_CONFIDENCE` (~0.5, below the 0.95 curated seed), so
the gate still prefers seed/curated ground truth over a hallucinated live fact. Off
(byte-for-byte unchanged) for any analyst that does not opt in. See `FLOWS.md` Flow
10 and `ANALYSIS.md` §7.9.

---

## 7. The scoreboards and the measured experiments — reported honestly

Producing a verified output is not the end of the river; the analysis leg grades
itself and, where it can, tries to improve. The ambitious legs return **only as
measured, honest experiments** — a no-skill or insufficient-sample result is
*published*, not hidden.

- **Skill scoreboard (per unit).** Each bounded unit is evaluated on
  **faithfulness** (§5) and **correctness-vs-reference** (`unit_correctness_scorer`,
  a deterministic scorer against operator gold labels — **honest-null** when a unit
  has 0 labels; the gold set is tiny today, `n=1`, reported insufficient-sample).
  The exogenous calibration **Brier** (`calibration_tracking`) and the acute-forecast
  **BSS** are folded here too. All of it surfaces on the calibration scoreboard route
  (`GET /api/v1/v3/eval/calibration`), each metric reported honestly. Per-analyst run
  timing (count, avg / max wall-clock seconds, last run, non-success) surfaces
  separately on `GET /api/v1/v3/eval/analyst_runtime`, read straight from
  `analyst_traces`.

- **GEPA self-optimizer (scoped, measured).** GEPA returns scoped to **one** measured
  unit (`leadership_transition`) as the `unit_optimizer` descriptor. Every candidate
  carries a REAL before/after **paired faithfulness delta** measured on the same
  faithfulness judge (currently the core `llm.primary.openai_compat` model, not
  cross-family) that gates the live findings (live: parent 0.34 → candidate 0.29, **delta
  −0.05**). It stays `promotion_gate=human_gated` and can **never** auto-promote on a
  degenerate, absent, or non-positive delta (`optimizer.should_auto_promote` runs the
  measurement gates first; `tests/test_p4t8_honesty_optimizer_promotion.py`). The old
  monolithic `country_optimizer` stays **cadence-frozen** (descriptor still
  `state=active`, `fallback_schedule: null`, `SEAMS.md` #30) — no reminder-flood
  regression.

- **Forecasting (a scoreboard, never a claim).** Forecasting returns **only** as a
  precise-question `acute_forecasts` Brier/BSS scoreboard (question + window +
  probability + auto-resolve), driven weekly by `forecast_scoreboard` and surfaced
  solely on the calibration route — never as a free-text claim or finding. A
  geography-dominated / degenerate probability vector **ABSTAINS** (zero rows). It
  currently reports **no proven skill**, and that null is published: the project earns
  the word "forecast" only when the BSS is positive on a non-degenerate, at-sample
  pilot (`SEAMS.md` #31/#31b; `tests/data_pkg/test_p4t8_honesty_forecast_skill.py`).

**Retirements / freezes (sequenced, documented in `SEAMS.md`).** The monolithic
`country_assessor` one-pager is **retired and stopped** (removed from bringup
`ANALYST_FILES`; nothing in the product spine reads it — it was feeding untrusted
findings; the units + composition supersede it). It is a *stopped* producer, not a
clean slate: its **~1.2k historical findings remain in the DB, unread**. The
forecast-as-claim predictors (`country_predictor`, `india_energy_predictor`) are
likewise **retired/frozen and stopped** — a numeric forecast is a claim that must be
scored before it is shown, so forecasting returns only as the `acute_forecasts`
Brier/BSS scoreboard (above), never a free-text claim; their **~539 historical
prediction rows remain**. The `journal_assessor` is **not** frozen — it runs on
cadence as an introspective instrument, off the fact/finding/nexus chain (§10).
None of these producers is deleted or de-registered, and their historical rows
persist.

---

## 8. Agency — the governed actuation surface

When an analyst needs to *act* on the world (enqueue a media job, emit to a channel,
discover a source, fetch a URL) rather than only write substrate, it does so through
the **action-pack agency** plane — never directly. Every action passes a three-way
gate (resolve ∩ allow ∩ applicability), a per-pack governor against a global budget
envelope, and lands a reversible, stamped provenance row. Web egress is guarded, not
trusted; writes are operator-gated by default. This is the only place untrusted text
can drive an effect, and it is treated as such. See `AGENCY_GATING_MODEL.md` (all
sections) and `ANALYSIS.md` §5.

---

## 9. Provenance — the through-line

Every stage above stamps lineage, and that is what makes the river auditable end to
end. A signal carries immutable acquisition provenance (source, fetch time,
pipeline). Every analyst output stamps `derived_from` with the substrate UUIDs it
read, so the lineage walker can backtrack: a world composition → its country reads →
their units → each unit's cited signals → each signal's acquisition record.
`GET /api/v1/lineage/finding/{id}` walks that chain hop by hop to the real source URL
with **zero dangling links** (a lineage-integrity sweep prunes dangling
`derived_from`). Even the scorecard bands (§4) resolve: each band's basis
finding-id is a `derived_from` member a lineage walk resolves.

Orthogonally, every analyst run extends a per-analyst **SHA-256 hash-chained receipt
chain** in `analyst_traces` (`receipt_hash`, `prev_receipt_hash`), following the
analyst identity across descriptor versions. Each lineage node carries a
`receipt_hash` and a `chain_consistent` boolean; the UI badge reads
**"chain-consistent (single-node)"** — an honest scope statement, *not* an external
cryptographic-signing claim (Ed25519 signing exists only on the descriptor audit-log
/ audit checkpoints, not on analyst outputs). Together, `derived_from` (what it read)
and the receipt chain (how it ran) let the system answer "why do we believe this?" at
every altitude. See `ANALYSIS.md` §6.1 and §7.7.

---

## 10. The journal — the reflective layer *over* the river

Every stage above is part of the chain. The `journal_assessor` is not: it is Legba's
first-person reflective voice, the one analyst pointed at the **whole organism** —
its own self, state, and flow — rather than at one slice of it. It is the 11th
`OutputKind` (`journal`) and it is deliberately **off-chain**:

- It lands in the dedicated `journal_entries` table (migration 0048), **not**
  `analyst_outputs` — and never the knowledge-layer `facts` / `nexuses` tables. It
  must never write a fact / finding / nexus (a grant-layer backstop — it holds only
  the read-only `journal_read` pack and the propose-only `journal_propose` pack — and
  a gating test enforce this).
- Its `derived_from` is **always empty**, and the table is deliberately absent from
  the lineage catalog (`lineage_api._SUBSTRATE_TABLES`). A `derived_from` walk from a
  fact / situation / nexus can therefore never surface a journal node (§9). Citations
  live only in the row's `claims` / `cited_substrate_refs` (an up-only reference, not
  a lineage edge), rendered as provenance chips that deep-link to the cited record.
  So the journal is **not** a stage in the lineage — it is the reflective layer above
  and across it.

It is a single META analyst kind (`target_filter=None`, like `world_assessor`), run
under the in-actor `llm_planner` GATHER envelope (PLAN → GATHER → NARRATE); the heavy
GATHER loop runs on the core gpt-oss plane, and only the bounded final **voice**
synthesis runs on the Anthropic plane (Opus 4.8). Two descriptors share the kind: an
**entry** tier (`journal_assessor`) and a daily **consolidation** tier
(`journal_consolidator`).

It **runs on cadence** — the entry tier every 12h and the consolidation tier daily
— as an always-on introspective instrument, **not** a producer in the
cited-synthesis spine. Its running cannot pollute product output: it writes only
`journal_entries` and holds only the read-only `journal_read` and propose-only
`journal_propose` packs, so it can never emit a fact / finding / nexus (§9). Its only
un-gated effect is its own continuity; everything outward — a correction, a
self-revision, even a change to its own instructions — would be **proposed** into the
human-gated `journal_proposals` queue rather than written to a live table, and
actually routing its reflections back through that queue is a **future item, not yet
wired**. It can write its own next breath, but it cannot rewrite its own rules
without the operator. See `ANALYSIS.md` (the journal kind) and `ARCHITECTURE.md` §8.1.

---

## See also

- `FLOWS.md` — the executable life-of-a-X walkthroughs (Flow 1 signal, Flow 2
  cadence, Flow 6 facts, Flow 7 nexus, Flow 8 ACH, Flow 10 grounded assessment).
- `ANALYSIS.md` — the analysis plane in depth (triggers §2, kinds §3, agency §5,
  eval + verify §6, methodology §7).
- `ACQUISITION.md` — how data enters and reaches analysis (§1 SourceActor, §3
  baseline, §4 fan-out).
- `SEAMS.md` — the sequenced retirements / freezes (the stopped `country_assessor`,
  the retired predictors, the cadence-frozen optimizer) and the verify pass.
- `AGENCY_GATING_MODEL.md` — how an analyst is allowed to act.
- `ARCHITECTURE.md` §8.1 — the real `OutputKind` enum members (all 12).
