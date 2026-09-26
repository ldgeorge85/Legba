# Decision records — analysis, flows, data model, acquisition

Every dated statement, decision record, ticket id and "was → now" narrative pulled out of
`docs/ANALYSIS.md`, `docs/FLOWS.md`, `docs/DATA_MODEL.md` and `docs/ACQUISITION.md` when those four
were rewritten in the present tense. Oldest first. Each entry is **date · area — what changed, and why**.

Ticket ids are preserved exactly as the source docs carried them; `TICKETS.md` is where they are
defined. Entries whose source gave no date are filed at the end, under the wave that produced them.

---

## 2026-06

**2026-06-17 · data quality** — A live-data audit found the analytical machinery built and the running
data short of it: ingestion fact confidence hardcoded `1.0`, no NER junk gate on the fact write path,
an unreconciled predicate vocabulary, `signals.source_credibility` 100% NULL, an empty `graph_metrics`
sink, ungoverned `proposed_edges`, entity fragmentation and mistyping, and ACH outcome resolution
never firing. The audit produced the Phase B/C/D pass below.

**2026-06-17 · graph** — 27 smoke-test fixture rows were loaded into the Apache AGE graph
`legba_graph`. They were the only rows it ever held; migration `0150` deleted them.

**2026-06-18 · data quality (Phases B / C / D)** — Closed most of the 06-17 audit. B: ingestion fact
confidence derived from the extractor signal with a 0.75 fallback; a `_is_junk_triple` gate plus
identical-triple dedupe on the facts write path; `vocabulary.normalize_predicate` converged both
write paths on one canonical form. C: `entity_resolution` gained the deterministic
`canonicalize_entity` pass, provenanced merges and `entity_profile_versions`. D:
`signals.source_credibility` populated at the canonical write path from the scored
`source_credibility` table; `structural_balance` / `graph_mining` / `nexus_decay` began writing a
`graph_metrics` row per run; `proposed_edge_governance` began promoting well-corroborated pending
edges into neutral `CoOccursWith` nexuses; ACH outcome resolution wired at terminal status
(`resolved_by='status_transition'`, migration `0038`) so `calibration_tracking` computed a real Brier
instead of `n = 0`.

**2026-06-18 · calibration** — The Brier the Phase-D wiring produced is a *self-consistency* Brier,
not calibration against exogenous reality: the same evidence that drove a hypothesis to
`confirmed`/`refuted` resolves its outcome. Findings are flagged `self_consistency_only` and
`calibration_tracking` segregates the sample so a self-consistency Brier can never be read as
calibration.

**2026-06-26 · facts (task #101, "Holes-A")** — Fact supersession became source-tier-aware: a
machine-extracted `ingestion`/`agent` fact no longer closes an open human-curated `seed`/`curated`
fact (`seed == curated > ingestion == agent`); same-tier recency still wins. N agreeing sources on a
`(subject, predicate, value)` combine confidence by a bounded noisy-OR (cap 0.99) instead of MAX.
Every `facts` row gained a `source_credibility real` prior (migration `0054`).

**2026-06-29 · facts (task #101, "Holes-B")** — The contested-claim arbiter was built: disputed values
kept alive at the fact layer plus a credibility-weighted *surfaced* winner (migration `0055`).
Detect-only and flag-gated — it never mutates a fact, and the write-path coexistence that keeps two
disputed values open ships OFF by default behind `LEGBA_FACT_CONTENTION`. Machine-extracted ingestion
confidence floored at `_INGESTION_DEFAULT_CONFIDENCE` (~0.5) rather than the old hardcoded 1.0.

---

## 2026-07

**2026-07-03 · grounding** — The `vector:world_context` RAG pilot was rolled back on
`leadership_transition` after it thickened the low-faithfulness tail.

**2026-07-06 · entity resolution** — The dedup pre-lookup became alias/article-aware and
class-guarded: an article/case/alias variant is rewritten onto an existing keeper's canonical surface
before the dedup key, a fallback-elected keeper is never class-mutated, a junk gate drops
numeric/quantity/possessive surfaces, and conservative class validation corrects only high-confidence
mistypes. Migration `0076` folded the leftover fragments (`entity_profiles` 12,257 → 12,144). *Why:*
after the Phase-4 entity merge, new resolutions were re-fragmenting ("the Strait of Hormuz" forking
from "Strait of Hormuz").

**2026-07-06 · NER coverage (M11 / M12)** — `text` was added to the `ner_multilingual` text fields so
Telegram message bodies (carried in `payload.text`) stop yielding zero entities (M12); non-Latin
bodies (Arabic / Cyrillic / Hebrew / CJK) are translated to English through the hosted NLLB
`/translate` endpoint before extraction (M11). Both forward-only — re-enriching the ~9k older
telegram / non-Latin signals is a separate operator job.

**2026-07-06 · verify calibration (M13 / M14 / M15)** — Three demote-only guards so the deterministic
floor stops mis-scoring honest findings: a stale-leader guard on current-officeholder claims plus a
current-officeholder anchor in the grounding preamble (M13); a null-result rubric plus citation-parser
fixes — range markers `[1-92]` expand to their integer members (capped), explicit `[no citation]`
lines are floor-exempt, and a corpus-scoped absence finding grades as a faithful survey (M14); and a
target-consistency guard flagging a per-country unit finding whose named subject-country contradicts
its desk (M15). Migration `0080` closed the historical cross-target mislabels.

**2026-07-06 · graph mining** — The hostile-edge shortlist gained a vetting pass: canonical
class-checked endpoints only (dropping NER fragments like `Parl` / `Fed` / `West` / `Leader`), a
genuine hostility `rel_type` **and** negative polarity both required, a subject-attribution guard, and
a per-edge quality score.

**2026-07-06 · thematic proposal** — Absence / negation-framed compositions excluded from candidacy,
and each slug derived from the stable `situation_signature` so proposals dedup rather than piling up
variants.

**2026-07-06 · cross-analyst correlation** — Three fixes made `cross_analyst_correlator` a real
reader: its `READ_SLICE` repointed off the retired `country_assessor` / `country_predictor` onto the
live composition and unit layer; it entered the mandatory faithfulness verify pass; and it began
superseding prior same-relationship heads via a stable `situation_signature`, with a `blind_spot` head
decaying only when its scope is revisited (M17). Migration `0079` swept the stale heads the old read
slice had left.

**2026-07-06 · fact and nexus write-path gates (migrations `0076`–`0080`)** — A predicate-argument /
relation-direction gate rejects inversions ("NATO member of Turkiye") and quantity/person objects for
`member`/`part` predicates; a demonym and relative-temporal subject reject; adjective-nationality value
normalization scoped to geographic/relational predicates only. A pre-existing over-aggressive "sports
roster" gate was fixed — it had been silently dropping real IGO-membership facts. On the nexus side: a
junk / vague-endpoint gate at both producers, a same-referent self-edge gate, and demonym / plural dyad
canonicalization. Migrations `0077` (facts) and `0078` (nexuses) closed the historical strays,
reversibly.

**2026-07-06 · source credibility** — Migration `0080` seeded state-affiliated and social hosts below
the 0.5 ingestion nominal (`presstv.ir` 0.25, `irna.ir` 0.30, `ukrinform.net` 0.45, `t.me` 0.30) —
un-scored state-affiliated hosts had been out-crediting their own seeded peers.

**2026-07-06 · grounding RAG pilot** — Recalibrated and re-activated on `internal_stability` only.
The embedder (bge-m3) was never the problem; the fixes were in retrieval usage — a focused
`"<country> <theme>"` query replacing a diluted unit-name-plus-entity blob, doc contextualization
(chunks embedded with a `"<Country> — <section>"` lead), the 293-point corpus re-embedded in place by
`scripts/reembed_world_context.py`, and the relevance floor lowered 0.65 → 0.55 (on-target ~0.6,
off-target ~0.42).

**2026-07-06 · journal model plane** — The journal's narrate handler moved off the Anthropic Opus plane
onto the core plane (`llm.primary.openai_compat`), so the journal costs no Anthropic spend; the billed
Anthropic plane is reserved for the on-demand consult / deep-consult kinds.

**2026-07-21 · voices** — Operator GO for the chronicle tier and the four faculty lens reads
(`VOICES_PLAN_2026-07-21`, `CHRONICLE_BUILD_2026-07-21`). Weekly, staggered off the 00:00/12:00 cadence
burst implicated in the actor-plane wedge (B0-13).

**2026-07-28 · alerting and readouts (migrations `0091`–`0105`)** — Nine deterministic analysts landed
as `state: draft`: `alert_trigger_scan`, `geo_convergence_scan`, `band_calibration_tracker`,
`fact_decay_scan`, `source_track_record`, `narrative_mapper`, `desk_baseline`, `evidence_archiver`,
`collection_gap`. All LLM-free; the readout half shares one pattern — derived, fully recomputable,
wholesale-refreshed, never a mutation of the primary rows.

**2026-07-28 · judge route and provenance** — The judge LLM stopped being implicitly "whatever
`method.llm.verify` says" and resolved through an explicit opt-in ladder: the opt-in gate first (an
analyst with neither a `judge` nor a `verify` key gets no judge route at all), then
`LEGBA_JUDGE_STACK_REF`, `method.llm.judge`, `method.llm.verify`, `method.llm.primary`. Every critique
stamps `judge_llm_ref` and `judge_route`; failures classify hard vs soft (`_FAIL_CLASS_BY_REASON`,
AST-drift-guarded); the per-claim `claim_verdicts` ledger began recording supported claims, not only
failures. None of it changes the score.

**2026-07-28 · verify (W31)** — `unscoped_absence_claim` added after the gold-set loop surfaced that
5 of 8 sampled downgrades in one week's cohort were faithful to their inputs and wrong about the
world: a thin-collection desk stating an absence as a world fact. Two changes — a deterministic soft
backstop (`unscoped_absence_spans`, pure lexical, folding exactly one to the denominator) and a
collection-scoped absence rule added to every inline unit prompt, test-pinned.

**2026-07-28 · contested claims (the arbiter tail, migrations `0097`, `0099`)** — Surfacing gained a
soak window (`LEGBA_CONTENTION_SURFACE_SOAK_HOURS`, default 48h), a weighted tie-break past soak, an
earned-track-record term behind `LEGBA_CONTENTION_EARNED_WEIGHT` (default 0.0 = OFF) computed with a
72h lag and a self-exclusion guard, a cached LLM near-tie adjudicator (`fact_contention_tiebreak`),
surfacing provenance (`surfaced_by` / `surfaced_at` / `surface_rationale` / `surface_history`), and
re-open on new evidence.

**2026-07-28 · retention** — `analyst_traces_retention` became a thin shim over one shared sweep engine
driven by the `retention_policies` table (migration `0109`); both seeded policies ship `ttl_days = 0`,
sweep disabled.

**2026-07-28 · acquisition** — `collection_gap` began draining a second backlog (`hypotheses` rows with
`status='source_request'`) and writing both into `collection_requirements` (migration `0113`).

**2026-07-29 · geo convergence** — `geo_convergence_scan` folded into `alert_trigger_scan` as a trigger
class; the standalone entry point became a no-op deprecation stub and the descriptor was later retired.

**2026-07-29 · claim_watch precision (round 1)** — 122 stratified pairs labeled out of plane. Pairwise
precision split hard by plane mix: `vector`+`entity`+`geo` 1.000 (17/17, effective n≈9),
`vector`+`entity` 0.538 (14/26), `entity`+`geo` 0.120 (3/25), `entity` only 0.000 (0/54),
meta-questions 0.035 (2/57), substantive theses 0.492 (32/65), pooled 0.279 (34/122) against a
recommended ≥0.85 bar. The apparent version gap (`3.0.0` 0.456 vs `3.1.0` 0.056) was sample
composition, not version quality. `3.2.0` responded with three levers: meta-question exclusion at match
time, global hub-entity damping, and an omnibus damper (≤8 questions per signal, same-`canonical_url`
collapse). Post-exclusion pooled precision projected ≈0.49.

**2026-07-31 · acquisition** — Telegram polls began rotating through the channel list with a
write-ahead resume pointer; handlers advertise their own poll bounds; chat text became a first-class
corpus field. *Why:* the generic poll budget had been truncating the walk before its tail — the newest
channels had produced one signal ever; post-fix, dozens in hours at a 0% poll-cap rate.

**2026-07-31 · judge pipeline `2026-07-31/1`** — The first judge pipeline version stamp. The train (V-F
claim-splitter hygiene, V-C metadata lookup, V-D earned hard-fail severity, V-B slice-scoped absence,
A3's counter) was expected to shift mean faithfulness upward as a *measurement correction* — the readout
established both judges over-fail, so the prior mean understated true faithfulness. Splitting on the
stamp is what makes that statement checkable rather than asserted.

---

## 2026-08

**2026-08 · forecasting** — The first exogenously graded acute-forecast cohort landed
(`resolved_by='forecast_acute_exogenous'`).

**2026-08-02 · judge pipeline `2026-08-02/1` (the F-A precision train)** — All three pre-declared gates
failed at `2026-07-31/1` (70% agreement vs 85%, 60% failure precision vs 75%, one pass-side miss vs
zero). W1 makes the contradicted branch earn its hard fail; W2 makes a hard fail auditable and actually
refuting; W3 splits the citationless shapes; W4 lands four small checkers. Hard-fail count was expected
to fall sharply and mean faithfulness to fall slightly — only the split key makes that legible as a
rule change rather than a quality movement.

**2026-08-03 · judge pipeline `2026-08-03/1` (the V-G train)** — The acceptance re-run failed all three
gates again and agreement regressed 70% → 63%. F-A's filters worked (contradicted 27 → 15) and in
clearing them exposed what they had hidden: the judge was refuting findings with findings — 14 of 24
hard fails rested on a quote from an analyst output, 13 of them the desk's own superseded prior read.
V-G1 requires a hard fail's quote to resolve to source reporting or to evidence the claim itself cites;
anything else demotes to the soft class `judge_prior_read_conflict`.

**2026-08-03 · correctness axis** — The correctness axis moved onto the weekly gold-set verdicts
(`correctness_labels`, via the shared `correctness_axis` module; first labeled cohort n=8). The
deterministic `unit_reference_labels` reference leg stayed tiny (n≈1), reported insufficient-sample,
never pooled with the operator leg. Migration `0170` recorded the promotion (comment-only).

**2026-08-03 · typing** — `TYPING_BAKEOFF_2026-08-03.md` recorded the relationship-typing model
comparison.

**2026-08-09 · continuity** — `situation_tracker` first activation seeded every open situation through
the audited FSM route; real transitions landed on the next tick.

**2026-08-10 · judge pipeline `2026-08-10/1`** — The stamp the docs carried as current before the
2026-09 trains.

**2026-08-15 · composition** — `meta_findings_synthesizer.DEFAULT_VERIFY_FLOOR` was raised to `0.50`,
so `LEGBA_COMPOSITION_TIERED_EVIDENCE` no longer moves the basis bar (it had moved it from the
historical `0.0` default); flipping the flag now only adds the labeled periphery section.

**2026-08-15 · judge** — `JUDGE_PIPELINE_VERSION` and its per-train lineage moved out of `verify.py`
into `provenance/judge_pipeline_version.py` — the module-size-gate seam; `verify` imports it one way
and re-exports it.

**2026-08-21 · scorecard (FRAME-3)** — The `severity:<level>` tag was redefined as the dimension's
**standing state** — where it stands on the desk today — not the severity of what moved in the unit's
72-hour slice. *Why:* before the split, a standing war tagged `low` on a quiet week banded `low`.
Movement now rides a separate `severity_delta:<rose|fell|steady|new>` tag, is carried beside the band,
and no rule reads it. Each verdict stamps `banding_semantics` so cards written under the two contracts
stay comparable; a head with no delta reads `null`, never `steady`.

**2026-08-27 · scorecard (H3, the damper retirement)** — The one-rung damper between the confidence
floor and the `CONF_CONFIDENT` knee was retired. Under severity-as-state that subtraction demoted a
standing level rather than a slice delta, and CORRECTNESS-R2 measured it net-negative across six lanes —
22 of 49 banded dimensions damped, 12 losing a real rung (CD `internal_stability` shipping `low` while
carrying `severity:moderate` + `severity_delta:rose`; IR `escalation` shipping `watch` in month six of
a shooting war). `effective_confidence` now decides admission only; the weak-confidence case is named
(`reason: qualified-low-confidence`) rather than subtracted, each row records `damped_would_have_been`,
and `damping_semantics` is stamped beside `banding_semantics`. An R2 replay of ten graded countries
moved the mean rung distance to the graders' blind reference bands from 1.449 → 1.245 over 49 scored
slots, exact matches 6 → 8.

**2026-08-27 · scorecard (H3, basis alignment)** — CORRECTNESS-R2 found the card and the prose
admitting rows under the same floors in opposite order: the composition puts the floor in the `WHERE`
and the head-fold in the `DISTINCT ON`; the card folded heads first and applied floors after. 21 of 21
product-`insufficient-evidence` slots in the round sat beside a composition that had consumed a
verified head for that desk, 20 clearing the composition's own 0.50 bar — BF `energy_security` at
effective 0.90 published `insufficient-evidence` beside prose asserting all seven desks produced
verified reads. The card now resolves the composition head's `derived_from` and bands the newest of
those rows that passes the same unchanged guards, or abstains naming the consumed ids and the refusing
rule (`basis_alignment.state = consumed-unbandable`). The replay recovered 20 of the 21. Complementary
to B0-5's read-time `disagreements` (`scorecard_reconcile`), which still reports the scheduling race —
30 of 31 targets compose after their card freezes.

**2026-08-27 · verify (H1)** — `register_self_corroboration` added: a world claim whose only support is
the open-situation register, stated as currency or corroboration. Soft — nothing is fabricated; the
register is the product's own bookkeeping.

**2026-08-29 · external audit** — `EXTERNAL_AUDIT_PIPELINE_VERSION = "2026-08-29/1"`, the narrow-leg
stamp for the daily six-claim sweep.

---

## 2026-09

**2026-09-03 · alerting (#82)** — `coverage_floor` added as a trigger class: a foreign polity a desk's
own salience-scored 14-day slice keeps naming that not one of its open situation frames names. The
first class whose subject is the gap between two of the engine's own artifacts. Five clauses bar it,
"naming" runs the shared `_entity_canon` alias and demonym maps in both directions, it fires `low`,
once per newly-uncovered polity per desk, interval-gated at 6h.

**2026-09-04 · the demotion (D-1)** — `DEMOTION_D1_SPEC_2026-09-04` split every composition tier into
two rows under two analyst ids on one kind: the **record** (a deterministic assembly that quotes one
lead span per input head under a closed connective vocabulary, makes no LLM call, and stamps
`usage = {"prompt_tokens": 0, "completion_tokens": 0}`) and the **voice** (interpretive prose beside
it, fenced to that one record). *Why:* an LLM writing the record could restate its inputs wrongly; a
record that only quotes cannot.

**2026-09-04 · composition (D-2)** — The region / world / thematic slice-assembly branches, their roster
and membership resolvers, and the per-mode coverage vocabulary were extracted from
`meta_findings_synthesizer` into `composition_slice` at the module-size-gate seam; the synthesizer
imports one way and re-exports every moved name.

**2026-09-05 · research** — `RESEARCH_PROGRAM_SPEC_2026-09-05` defined the `research` pack and the
outbound-research write path (`web_evidence`); `EXTERNAL_GRADING_WIDTH_DESIGN_2026-09-05` defined
external grading at width, and `external_grades` (migration `0190`) became the platform's first
database home for external truth.

**2026-09-05 · the voice evidence map** — The first live Assessment graded 0.00 on seven claims, six of
them faithful, because its evidence map carried the quoted spans but not the counters printed on the
record's own page. `assessment_evidence_text` now joins the block's quoted span and the record's own
arithmetic under a visible `EVIDENCE_ARITHMETIC_RULE` separator.

**2026-09-05 · the voice (unsupported markers)** — `UNSUPPORTED_INSTRUMENT_PROSE` added: the voice
narrating the record's own machinery. `desk_reference` stamped `REFERENCE_PIPELINE_VERSION =
"2026-09-05/2"` — bumped to `/2` to avoid a same-day collision with a neighbouring instrument's stamp.

**2026-09-06 · the voice (unsupported markers)** — `UNSUPPORTED_APERTURE_UNROSTERED` added: a proper
name in the "what this reading misses" section that appears nowhere in the record's three surfaces.

**2026-09-16 · acquisition** — Challenge detection landed. The wall detector's patterns had been gated
behind a 500-char cap measured against no-JS fallback pages; a modern interstitial is far bigger (AP's
Cloudflare page 5,507 B, Times of Israel's 12,604 B), so a challenge sailed past the gate and landed as
a thin or failed extraction that downstream read as *the web had nothing*. `_challenge_detect.py`
classifies at three tiers and the verdict travels as `blocked_by_challenge`; the archiver records
`evidence_archive.status = 'blocked_challenge'`, `web_evidence` sets `depth_reason = fetch_blocked`. On
the measured sample, 8 of 24 urls (33%) are now reported as blocked which previously read as absence.

**2026-09-16 · acquisition** — `LEGBA_FETCH_IMPERSONATE` shipped as a measured **negative** result and
stays default `off`. Probed over 24 urls / 13 hosts (59 live GETs, robots obeyed), flag off then on:
zero of the eight challenge-class urls became `ok` (the review's bar was ≥ 1/3) and two regressed
(`arabnews.com` served 200 plain and a Cloudflare 403 with the Chrome fingerprint — the
UA/TLS-consistency refusal the design anticipated). One finding points somewhere: four `aa.com.tr` urls
that httpx fails with `RemoteProtocolError` fetch cleanly under curl_cffi — an HTTP-stack
incompatibility, a per-host question for an operator. Reuters is closed by its own robots.txt and no
fetcher option may be pointed at it.

**2026-09-16 · correctness (Program 2, track G2)** — The composition correctness gate was built: with
its flag on, a desk unit whose latest `unit_correctness` row is missing, weak, or single-family is
**quoted** by `country_composition` instead of composed over. *Why:* the spine's rule is that authority
climbs only as far as the verification underneath it reaches, and until this the platform could not
obey it for correctness — only faithfulness was measured. `BUILDER_PIPELINE_VERSION = "2026-09-16/1"`
stamped the reference builder in the same program.

**2026-09-16 · consult** — The operator reversed the consult spend guards and compaction: per-run input
token and cost caps effectively off, full rounds kept.

**2026-09-20 · judge pipeline `2026-09-20/1`** — `GRADER_PIPELINE_VERSION` carries
the same string on a different instrument; the two populations must never pool.
(Superseded by `2026-09-24/1`, below.)

**2026-09-20 · correctness grader** — Family F3 was repointed from `mistralai/mistral-large-2512`
(removed upstream) to `mistralai/mistral-medium-3.1`; the component id is unchanged. Because the
calibration interlock keys on the model ids a run will use, that repoint makes the interlock refuse
until a `grader_calibrations` row covers the new model.

**2026-09-20 · external audit** — The operator flipped the paid SERP rung on, so
`serp_provider_order` reads `[searxng, serper]` and the escalation is live. The auditor's drain caps
were raised (tick 40 → 200, day 600 → 5,000, SERP 900 → 10,000, queue 4,000 → 20,000) and the
per-tick clamp began resolving from the live `web_access` governor rather than a copied constant.

**2026-09-20 · contested claims** — `fact_contention_pass` added pass planning. Measured live, one
arbiter pass held the actor turn a median of 651 s (min 443 s, max 994 s) against a 180 s invoke
timeout: reconcile `ENSURE_ACTIVE` blew its deadline 124 times in 24 h, the heal breaker opened 144
times, and all 24 cadence fires landed on a closed actor. The fix rests on one observation — almost
nothing changes between passes (~15 groups an hour against 22,208 recomputed) — so a group whose input
fingerprint matches what is stored is skipped whole.

**2026-09-20 · cadence (STEP E)** — The world tiers were restaggered so the world record assembles
after the cycle's country voices (`world_assessor` `20 0,12`, `world_assessment` `35 0,12`), and the
world voice began reading each carried country's own assessment in full as a context span
(`assessment_prompt.v4`).

**2026-09-20 · tunables** — `TUNABLES.md` was written from a full read-only sweep of the tree plus live
state; it is the one place budget, cap, governor and threshold values live.

**2026-09-21 · region and world tiers (D-5, the cascade)** — `region_composition` retired its
generative path: no LLM, no prompt, no faithfulness judge. It emits `region_rollup.v1` — an arithmetic
statement over its member country assemblies, carrying member block objects byte-identically (origin
still pointing at the desk head that wrote the sentence) rather than re-quoting. The world tier stops
reading region heads and reads the country assemblies directly, so a cross-border claim is made once,
at the tier that can cite both sides. *Why the world had to move:* a rollup carries no faithfulness
verdict, so it can never pass a `verify_floor` INNER JOIN, and the world would otherwise degrade every
region to country-fallback forever. Reported: 80.8% untraceable prose replaced by 100% byte-identical
carry, ~70 reads and 212 judge calls a week returned; flag-off byte-identity held with zero region test
files edited.

**2026-09-21 · the voice (D-6)** — `world_assessment` landed as its own row, fenced to the record it
reads (`derived_from == [spine_id]` plus an AST guard asserting the prompt builder's only data argument
is the payload), with unsupported markers carrying UTF-16-exact offsets and fidelity graded by the
existing machinery.

**2026-09-21 · external audit at width** — `EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH = "2026-09-21/4"`
(heads `"2026-09-21/3"`). The absence gate was narrowed to apply only to an *unverified empty* search:
enforcing it as "refuse unless `supports_absence_claim`" had refused 104 of 104 absence claims in 24
hours of live rows.

**2026-09-21 / 22 · correctness grader (H2)** — The grader moved to hourly ticks under
`LEGBA_GRADER_PASS_BUDGET_SECONDS=600`, roughly four targets each; eight ticks measured 30 of 32.

**2026-09-22 · integration** — D-5 and D-6's new files were repointed onto `text_fold`; the fold union
predates their branches.

**2026-09-24 · judge pipeline `2026-09-24/1` (H3)** — The reply contract now asks every verdict entry
to name its claim (`claim_index`), so a short or reordered judge reply aligns by id rather than
failing the whole call to the floor. The alignment arm existed since `2026-09-20/1`; no prompt asked
for the ids. Both prompted profile versions move: `citsupp.v5 → v6`, `absence.v4 → v5`. Rubric,
evidence envelope, verdict vocabulary, model and route unchanged.

**2026-11-03 · pre-registered** — The E2 latency gauge sitting, readable from
`action_pack_invocations.duration_ms` (migration `0208`).

---

## Undated narrative, filed by the wave that produced it

**Migration numbering.** `0095`, `0100`, `0110` and `0111` are unused: `0095`/`0100` were skipped in
the release wave; `0110`/`0111` were reserved for the C3 source-quality ledger, which landed at `0115`
after `0112`–`0114` took the intervening slots and needed only one of the two. `0206`/`0207` are
reserved but unbuilt, and `0208` landed before them — the runner keys on filename and the files are
independent. The runner has no manifest and no head constant: `migrate.py:_discover()` globs `*.sql`
and sorts, so numeric gaps are harmless and "head" means the lexicographically-last file present.

**`0108`'s in-file comments all say "0106".** It was renumbered `0106 → 0108` when a parallel branch
claimed the `0106`/`0107` slots and the body text was not re-swept. The runner keys on the filename.

**Migration ranges as the source docs described them.** `0081`–`0085` are the signal-content-depth
corpus/embedding markers (summarized / indexed / reindex / embedding / reenriched). `0086`–`0090` are
the entity-identity / salience / journal-data wave. `0117`–`0121` are data-hygiene soft-closes and
retirements (prefix-paired relational facts, telegram widget chrome, the never-written `entity_alias`
table, poisoned journal rows, the GDELT doc-api source). `0143`–`0145` are the `entity_edges` graph
substrate, backfilled again from facts at `0180`. `0186`–`0201` are the audit/grader wave.
`0202`–`0205` are the v3 event surface.

**Retirements and freezes.** `country_assessor` (the monolithic per-country one-pager) is retired and
removed from bringup because the units plus composition supersede it (SEAMS #35); its ~1.2k historical
`finding` rows remain in the database, unread. The forecast-as-claim predictors `country_predictor` /
`india_energy_predictor` are retired and stopped (SEAMS #31); their ~539 historical `prediction` rows
remain. The monolithic `country_optimizer` is cadence-frozen — descriptor registered, cadence null
(SEAMS #30) — so the reminder-flood regression class cannot recur. `world_assessor` was **not** retired:
it was repointed off `inline_target` onto `meta_findings_synthesizer` (SEAMS #34), which retired the
verdict-from-nowhere framing.

**The agency plane's zero-callers finding is closed.** The old major review found
`Agency.run_pack_tool` had no callers. The consult kind now routes every ReAct tool call through its
`substrate_read` binding, and the actor run path fires `escalate_finding` on a landed finding that
crosses the pack's post-verify gate. Two rewires (S3-T4 / S8-T2) sharpened that gate versus the old
"severity tag **or** raw confidence" test: severity became a first-class read column
(`analyst_outputs.severity`, partial-indexed for `high`/`critical`), and the score gates on the
verify-demoted `effective_confidence`, closing the raw-confidence escape hatch. The A-3 wiring and
JetStream binding for `channels.>` / `governor.events.>` landed in commits `10491f9` / `6938c0b`.

**`discover_sources` was removed** per decision F-1; deep-crawl discovery is a direction item.

**The `demote_and_continue` cheap-model fallback** is a declared seam (SEAMS F-2): it logs a
pause-until-reset rather than swapping in a real fallback model.

**The Dapr instance id must not contain `::`** — activity result parsing splits on `::` and would hang
forever; the optimizer id uses `optimizer.` instead, and `deep_consult` mints
`deep_consult.{scope}.{run8}`.

**The optimizer passes its training set by reference** (`TrainingSetRef`, SEAMS #23) so the serialized
workflow input stays under the 4 MB gRPC cap — the reminder-flood incident class (a >4 MB payload
orphaning per-activity reminders) cannot regress.

**The cooldown trap.** A `cooldown_seconds` stamped at run-completion that equals the cadence interval
lands past the next fire and silently halves the cadence (the 6h→12h / 12h→24h trap). Each cadence pairs
with a cooldown held below the interval, and the per-target run adds a 5%-of-cooldown slack band
(capped 600 s) that absorbs drift (commit `cefd8ca`).

**The critic fan-out fix.** `_critic_ungraded_targets()` resolving the newest-N ungraded findings and
fanning one bounded worker grade per finding row is what un-stuck the critic→optimizer eval loop.

**Live optimizer example.** A `unit_optimizer` candidate over `leadership_transition` scored parent
0.34 → candidate 0.29 (delta −0.05) and was refused — a negative delta cannot promote.

**claim_watch matcher lineage.** `claim_watch/1.0.0` was a boolean entity plane (a flat weight — desk
co-membership); `2.0.0` graded but unweighted; `3.0.0` specificity-weighted; `3.1.0` the measured vector
floor (0.60 → 0.45, from 14,280 live pairs over 4 desks: related thesis-vs-body pairs cluster p50 0.36 /
p90 0.43, random pairs p90 0.385 / p99 ≈0.444, so the old floor admitted ~0.2% of related pairs and the
plane was inert); `3.2.0` the three-lever response; `3.3.0` the bearing-pipeline seam; `4.0.0` the
precision train (blocking confirm leg, desk identity in the bearing prompts, deictic-thesis refusal,
contention liveness/subject anchors); `4.1.0` the current form (a consequence-specificity clause in both
bearing prompts, article-id URL dedupe, the question self-flag surface). Round 3 measured the `3.3.0`
gated stream at 0.267; round 4 measured the live `4.0.0` stream at **0.908**, over the ≥0.85 bar
(SEAMS #49) — so building the closer became a held operator decision rather than an unmet measurement.

**AGE.** The Apache AGE graph `legba_graph` exists inside Postgres (11 vertex / 21 edge labels
registered by migrations `0001` and `0037`) but has never held a production row. `/api/v1/graph/path`
returns an explicit `graph_unpopulated` error rather than a confident "no path".
`docs/history/AGE_PROBE_REPORT.md` carries the measurements the decision should be made from.

**GLiREL, not REBEL.** The hosted NLP stack runs GLiREL (`jackboyla/glirel-large-v0`) for the
`/extract` relation extraction the `fact_extractor` reuses, emitting real per-relation confidence scores
(live facts span 0.75 / 0.80 / 0.92 / 0.95 with a small tail at exactly 1.0). In-repo `fact_extractor`
comments still saying "REBEL" are stale; correcting them — and reconciling the conf-1.0-sentinel
handling against GLiREL's real scores — is a tracked code follow-up.

**The RAG pilot state file** lives at an ephemeral path; moving it to a volume is a tracked follow-up.

**The gold-set provenance claim is discipline, not an invariant.** "Labels come from outside the
production plane" is operational discipline: the code records `labeled_by`, with no CHECK constraint, no
allow-list, and no route-side rejection. Read the stamp; do not trust the architecture for this one.

**The `change`-apply path** of the journal proposal queue is import-verified but not exercised against a
live registry; `correction` and `self_revision` are tested end to end. A journal critic and optimizer
over the journal's own voice (Wave 5) is designed, not built, and gated on first building a critic
actuator.

**The intra-source duplicate audit.** A live audit found ~41% of stored rows were intra-source
exact-hash duplicates — feeds re-serving the same entry poll after poll, one earthquake bulletin stored
194 times. That is what `LEGBA_INTRASOURCE_DEDUP` closes at the write path; the historical duplicate pool
is an operator cleanup previewed by `scripts/report_intrasource_dupes.py`.

**The corpus orphan audit.** 41.5% of the OpenSearch corpus had silently orphaned before
`corpus_tombstones` (migration `0175`) gave the delete path an audit trail.

**Stale prose worth not repeating.** `analyst_world_assessment.yaml`'s comment block says the cadence
stagger "is deliberately not taken here", but the shipped schedules show it was taken.
`analyst_correctness_grader.yaml`'s comment says "hence 1-8, not \*" while the value is `1-9`. The
`correctness_grader` and `reference_builder` modules carry in-source defaults their descriptors
override (the descriptor is the live volume). `analyst_standing_auditor.yaml`'s prose still describes
the paid SERP rung as inert after the operator enabled it.
