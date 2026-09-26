<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# The correctness grader

*The per-unit answer to "is this read true", as distinct from "is this read grounded in what we collected".*

## The gap it closes

Most of this fleet's model calls are the platform grading itself: faithfulness judges scoring our prose against our
own citations, calibration trackers scoring our own bands, lineage sweeps walking our own lineage. Every one is a
consistency check, and not one can see a tower that is internally immaculate and factually wrong about the world.
Verified is only as good as the verifier: correctness needs a labelled reference per unit, and without one the
verify pass measures groundedness and calls it truth.

So this job grades a bounded unit's published read against an independent reference — an account of what happened to
one country over one window, assembled from the open web and blind to our substrate by the
[reference builder](REFERENCE_BUILDER.md) — and writes two shares, the per-claim ledger under them, and a receipt.
It reads references and can never write one: a grader that could build its own reference could close the loop on
itself. The surrounding measurement layer is [ANALYSIS](ANALYSIS.md) §10.

## What the number means

Two shares, per bounded unit, and neither is readable without the other:

```
correctness_share = contains / (contains + contradicts)
coverage_share    = (contains + contradicts) / n_claims
```

**Correctness** is over the claims the reference bears on: of what could be checked, how much held up. **Coverage**
is over everything the unit said: how much of it could be checked at all. A correctness share of 1.0 at a coverage
of 0.05 is one claim confirmed and nineteen the reference never touched. Both are stored with the `n` they rest on,
and both are null — never `0.0` — when their denominator is empty. Zero means every decided claim was contradicted;
null means nothing was decided. That distinction is the whole measurement, and the schema enforces it.

It is **not correctness against the world**: a true claim the reference never mentions reads `silent`, which is why
coverage travels with every share and why the builder records thin reference dimensions. It is **not a verdict on a
split**: where no two families agree the claim is published `split`, counted in the coverage denominator, in neither
numerator, never tie-broken. And it is **not a gate by itself** — this job measures and writes rows; what reads
those rows is the default-off [composition gate](#the-composition-gate) below.

## The rubric, and why its digest matters

The rubric is `src/legba/data/analysts/rubrics/ANNEX_C_v4.md`: three labels, one question, no tiers and no
precedence ladder. **contains** — a development states the core claim's matter. **contradicts** — a development
states something incompatible with it, including an absence claim ("no material change") the reference reports as
having changed. **silent** — no development bears on the matter at all.

It ships as a data file and its sha256 is checked at import against the pin in `_correctness_rubric.py`, because
that digest is the identity of every number written under it (`unit_correctness.rubric_sha`). Changing the rubric is
a three-part change — the file, the pin, and a fresh passing calibration row for the new digest — and any one alone
fails loud. The label vocabulary and the span policy (which labels require a verbatim quote and which require an
empty one) are read out of the rubric text, never hardcoded: a constant would keep passing the day the rubric moved.

## The three families

| | model | component | cost |
|---|---|---|---|
| F0 | the core plane's served model (`LEGBA_LLM_MODEL_NAME`) | `llm.primary.openai_compat` | $0 |
| F2 | `meta-llama/llama-3.3-70b-instruct` | `llm.audit.openrouter_llama33_70b.openai_compat` | paid |
| F3 | `mistralai/mistral-medium-3.1` | `llm.judge.openrouter_mistral_large.openai_compat` | paid |

All three resolve through stack components, so the key, the endpoint, the timeout and the price table are the
operator's configuration rather than this module's constants. A component id is stable across a repoint; the model
id behind it is not, and the model id is what the calibration lookup matches on — so repointing a component at a new
model fires the interlock below on purpose, rather than silently making a three-family number a two-family one.

**F0 is the producer family**: the same model writes the reads it grades here. That is disclosed rather than fenced:
fencing it would delete the only free family, and with it any correctness number on a deployment that spends
nothing. It is measured not to be the outlier. F2 and F3 pass the external-grader fence: never Anthropic (consult
only), never the writer's family, never the live judge's, never each other's. A family that fails the fence or fails
to resolve is absent, and the receipt says so. Adjudication is two of three or better, else `split`; an unparseable
majority is never promoted to a label. `max_tokens` is never sent to any family — a house rule for the core plane,
and the calibration sent none to the paid lanes either, so sending one would change the instrument.

## The ceiling, and the triage

`LEGBA_GRADER_DAILY_CEILING_USD` bounds what the paid families may spend in a day ([TUNABLES](TUNABLES.md) §2
carries the value). At zero, F2 and F3 are never called — not resolved, not attempted, not estimated: the whole
instrument runs on the free core plane, every claim carries one family's label, and every such claim is written
`single_family` and counted in `unit_correctness.n_single_family`. That is a weaker number, and the flag is what
makes it visibly weaker. Above zero, the guard refuses the call that *would* breach — spent plus the largest cost
seen so far, over the ceiling — so the run stops before the breach rather than reporting it afterwards. An
unparseable or negative value reads as zero: a typo must fail toward not spending. The ceiling is an environment
variable and not a descriptor option, deliberately: a descriptor PUT is an API call any holder of the registry token
can make, and raising the money this job may spend should be a deploy step with a person at the other end.

**The triage.** F2 and F3 grade only the claims F0 did not call `silent`. A silent claim sits in the coverage
denominator and in neither numerator, so a second opinion cannot move the correctness share and would only spend
money confirming an absence. The consequence, stated rather than buried: a `silent` claim carries one family's label
even when the ceiling is open.

## The calibration interlock

The job refuses to publish any number unless a `grader_calibrations` row names the same `rubric_sha`, covers every
model id the run will use, and says `gate_pass`. Coverage is a superset test: running a subset of the calibrated
families (F0 alone at a zero ceiling) is inside what was measured, and running a model the gate never saw is outside
it. Repointing a component, or the core plane, therefore stops the grader until a re-gate lands — the re-gate
trigger, enforced rather than remembered.

```bash
PYTHONPATH=src python3 scripts/load_unit_reference.py --seed-calibration   # seed the passing row
PYTHONPATH=src python3 scripts/correctness_regate.py --target country_watch_il   # free: draw + estimate
# the graded re-gate (paid for F2/F3), in the registry container
PYTHONPATH=/app/src python3 /app/scripts/correctness_regate.py \
    --target country_watch_il --grade --cap 0.25
```

A live draw asks whether the families agree today, on today's claims — the right question when the rubric moves, the
wrong one when a family moves. Nothing is then wrong with the claims: the question is whether the new model reads
the rubric the way the gated one did, and that is asked by handing it the same atoms the passing gate was measured
on. `--packet <frozen_packet.json>` does that, grading a frozen atom set exactly as a live draw is graded — all
three families, the same `--grade`/`--cap` discipline, the same bars, the same row, the same verdict. The loader
refuses a packet whose embedded rubric or output contract is not byte-identical to this build's, since the row
carries this build's `rubric_sha` and gating on other bytes would pool two rubrics under one identity, and it
refuses duplicate ids, empty assertions, items with no reference and anything the leak scan catches, because a
frozen packet's ids are in a namespace the live builder does not mint. A live draw stays the default: `--packet` and
`--target` are mutually exclusive, and the draw knobs are refused with `--packet`, not silently ignored.

The draw's seed is derived from the rubric digest, the model ids and the date rather than chosen: a seed somebody
picks is a seed somebody can pick again until the gate passes. A unique index on `(rubric_sha, packet_sha,
model_ids)` refuses a second row for a draw already scored, because a second roll of the same dice is not
independent confirmation. A fired gate still writes its row with `gate_pass` false, and the refusal downstream keeps
the grader off until somebody decides what to do.

## What runs, in order

Per `(target, as_of)`:

1. **Resolve the reference** — the `unit_references` row current at the stamp (see
   [reference currency](#reference-currency)); none means `no_reference` or `reference_stale`, nothing graded.
2. **Freeze the heads** — the eight dimension desks and the composition, under `created_at <= as_of AND
   (superseded_at IS NULL OR superseded_at > as_of)`; a registered unit with no head is recorded missing.
3. **Segment** — the shipped segmenter (`verify._segment_claims` and `._is_judgeable_claim`), then the
   [exclusions](#the-exclusions).
4. **Build one packet** — byte-identical across families, the reference reduced to a five-field whitelist
   (`item_id`, `summary`, `decisive_span`, `outlet`, `publish_date`) plus the band table, then leak-scanned; a leak
   refuses the whole packet, because an external family must never see our telemetry about the read it grades.
5. **Check the gate** — the calibration interlock above.
6. **Grade** — one call per claim per family, the label set read out of the rubric, one corrective retry. A span not
   verbatim from a development triggers that retry naming the failure; failing twice, the label stands with
   `span_unverified`, because a span quibble must never cost a label the grader actually gave.
7. **Adjudicate and write** — the unit row and its ledger in one transaction: a share can never exist without the
   ledger that justifies it.

`(analyst_id, target_id, head_id, reference_id, rubric_sha)` is unique — the same head against the same reference
under one rubric is the same measurement — and the handler reads that table first, so a re-run does not burn paid
calls the index would then discard.

## The exclusions

Every excluded span carries a named reason and its text, because an exclusion nobody can audit is one nobody should
trust: `machine_line_registered_shape`, `roster_enumeration_*`, `read_bookkeeping_not_world:<tell>`, `round_notice`,
`heading`, `under_40_chars[_after_markers]`, `not_judgeable_span`, `prior_relative:<pattern>`. The composition's
assembly render goes through the redactor first: its per-block telemetry and bookkeeping sections are the platform's
own numbers about itself, and no world reference can bear on them.

**Prior-relative claims** are the subtle class. A desk read is written against its own previous read, so the reads
say things like "no material change versus the prior read". The reference cannot bear on that: it is a window of
world developments with no prior read and no opinion about whether this read differs from the last, so a grader
handed such a span beside developments reads "developments happened" and returns `contradicts` — the right answer
for an absence claim about the world, meaningless here. The segmenter excludes them, recording the pattern that
caught each span.

The rule is anchored on the prior-read reference, never on the negation. Absence claims are claims under the rubric,
so "no new disruptions" stays gradeable; every pattern therefore requires the continuity assertion to be
grammatically joined to a reference to the unit's own earlier read.

| pattern | catches |
|---|---|
| `no_change_versus_prior` | "no material change since/versus/compared with the prior read" |
| `unchanged_from_prior` | "the pressure level is unchanged from the prior read" |
| `as_in_the_prior_read` | "the assessment remains moderate as in the prior read" |
| `already_noted_in_the_prior_read` | "… were already noted in the previous assessment" |
| `remains_as_previously_assessed` | "the posture remains as previously assessed" |
| `nothing_alters_the_standing_read` | "no new development alters the outlook" |

A span that merely mentions a prior read while asserting something about the world keeps its claim and is graded —
"the pressure highlighted in the prior read persists, with continued fuel-price spikes" is a world claim. The joined
form is shipped because the looser form — a prior-read mention anywhere plus a no-change phrase anywhere — swallows
genuine world claims. Which spans reach the rubric decides every unit's denominator, so a change to the exclusion
classes bumps the pipeline stamp (`GRADER_PIPELINE_VERSION`, an opaque identifier, currently `2026-09-20/1`, on
`unit_correctness.families.pipeline_version`), keeping the two populations separable in a query rather than only in
a commit message. Handing the rubric different spans is not a change to the rubric and needs no re-gate; a test pins
its digest against the value every `unit_correctness` row already carries.

## Cadence, rotation and the turn budget

The sweep runs as hourly ticks with a cooldown just under the tick interval, starting early enough in the day to
read settled heads rather than race them ([TUNABLES](TUNABLES.md) §2 has the schedule). A tick holds one actor turn,
and `LEGBA_GRADER_PASS_BUDGET_SECONDS` bounds the wall clock its target loop may hold: the check fires between
targets — a target in flight always finishes, because the write is the atomic thing — the deferred tail is named on
the receipt, and the rotation below puts it at the head of the next tick's queue. Several short turns cover the
roster where one long turn outlives the reconciler's deadline and returns no receipt at all.

`max_targets_per_run` bounds what one tick costs. It must never decide which countries are ever measured, so the
population arrives **least recently graded first**:

```sql
  LEFT JOIN (SELECT target_id, MAX(created_at) AS last_graded_at
               FROM unit_correctness GROUP BY target_id) AS g ON g.target_id = r.target_id
 ORDER BY g.last_graded_at ASC NULLS FIRST, r.target_id
```

The join is a left join so a target that has never been graded still appears; an inner join would make "never
graded" mean "never eligible", the exact starvation this ordering prevents. `NULLS FIRST` puts those targets at the
head, because the database's default for an ascending sort is nulls last and leaving it implicit would starve them
again, more quietly. `target_id` breaks ties, so the order is total and a sweep is reproducible. The recency is the
newest `created_at` over all of a target's rows, not per analyst and not filtered by rubric: the question the
rotation asks is when this instrument last spent a tick on this country. Any cap therefore cycles the roster, and
when a cap bites the receipt says so, so an operator can tell rotation from starvation at a glance.

## Reference currency

A reference is built for a window that ends at its own T0, and every read it grades lands after that instant, so
strict containment (`window_end >= as_of`) would make the job ungradable by construction. A reference therefore
stays current past its `window_end` until a newer reference for that target supersedes it or it ages out —
`window_start <= as_of <= window_end + LEGBA_GRADER_REFERENCE_GRACE_DAYS` — and among references that both cover the
stamp the newest `window_end` wins, `built_at` only breaking the tie.

* **Grace** is both a descriptor option (`reference_grace_days`) and an environment variable, and the environment
  wins — the ceiling's discipline, for the same reason: how long a published number may rest on an ageing reference
  is a measurement-integrity decision, not a per-analyst knob. Zero is legal and restores strict containment.
* **`reference_age_days`** (`as_of - window_end`, non-negative, fractional) is written on every row and printed on
  the receipt: "88% correct" and "88% correct against a reference six days closed" are different statements, and
  only the second is true.
* Past the grace the target is `reference_stale`, distinct from `no_reference`: one says the builder has not reached
  this target, the other says it did and then stopped. Different operator actions, so different statuses — and no
  model call is made in either case. The population sweep uses the same grace predicate as the lookup, because a
  population looser than the lookup would grade nothing and say nothing about why.

## The receipt, and how to read it

Every tick writes one receipt as an `analyst_outputs` row under `analyst_id = 'correctness_grader'`. The numbers
live in the tables; the receipt is the record of what the tick did and why.

```
Correctness grader — graded 37 unit(s) across 4 target(s) for $0.0237.
  as_of=… rubric=ANNEX_C_v4.md sha=1b51d7f5187c7f93… pipeline=2026-09-20/1
  ceiling=$2.50/day spent_before=$0.0422 targets=[…]
  reference grace=7d (LEGBA_GRADER_REFERENCE_GRACE_DAYS) — current until window_end + grace
  calibration: pooled=0.8444 packet=0a011222a043bf67… n_atoms=30
  - [ok] country_g20_au: 9 unit(s), 53 claim(s), $0.0088, reference_age=4.53d
      economic_coercion: correctness=100.0% (n=1)  coverage=25.0% (n=4)
```

Read it in that order. The headline says how much was measured and what it cost; the stamp line says which rubric
and pipeline the numbers belong to, and rows under different stamps do not pool; the ceiling line says how much had
already been spent today, which decides whether the paid families ran; the calibration line is the passing row this
tick published under. Then one block per target with its status and reference age, and one line per unit — a `—`
correctness beside a non-zero coverage denominator is the unmeasured case, not a zero. Three lines appear below only
when true: `prior-relative spans excluded: N`, a `deferred (N)` list naming the tail the turn budget or ceiling
stopped, and any warnings.

The same facts are structured on the row's `data.data` for querying — `as_of`, `rubric_sha256`, `pipeline_version`,
`ceiling_usd`, `spent_before_run_usd`, `run_cost_usd`, `reference_grace_days`, `targets`, `deferred_targets`,
`n_units_written`, `calibration`, `refusal`, `n_prior_relative_excluded`, `warnings`, and a `per_target` array
carrying each block above.

## The tables it owns

| table | one row is |
|---|---|
| `unit_correctness` | one unit's correctness at one stamp, against one reference, under one rubric |
| `unit_correctness_claims` | the per-claim ledger under it — every family's label, the adjudicated outcome, the decisive spans |
| `grader_calibrations` | one calibration run and whether it passed |

It reads `unit_references` and can never write to it; that table belongs to the
[reference builder](REFERENCE_BUILDER.md). `unit_correctness.reference_age_days` is nullable, and null is not zero:
a row written before the column existed measured against a reference whose window contained the stamp, but the
distance was never recorded, and backfilling a zero would assert a freshness nobody measured.

`unit_references` is not `unit_reference_labels`: that table holds the attention instrument's reference, at most
five items for one unit and target for one day, where this one holds eight dimensions of developments over a
fortnight per country. A table whose rows mean two things is a table someone eventually averages.

## Running it by hand

```bash
# the whole eligible population at the current stamp
PYTHONPATH=src python3 scripts/correctness_grade_now.py
# one target pinned to a past stamp
PYTHONPATH=src python3 scripts/correctness_grade_now.py \
    --target country_watch_il --as-of 2026-09-16T19:30:00+00:00
```

That PUTs to the sidecar's `…/v1.0/actors/AnalystActor/<actor_id>/method/run`, the same door the cadence uses; a
forced run skips the cooldown and is otherwise the identical path. **The body shape is load-bearing**: the actor
reads its contract keys (`trigger_kind`, `target_filter`) off the top level and merges per-run parameters from the
nested `options` object only.

```json
{"trigger_kind": "method",
 "options": {"grader_targets": ["country_watch_il"], "as_of": "2026-09-16T19:30:00+00:00"}}
```

A parameter sent at the top level is dropped silently — no error, no log line — and the run stamps the current
instant. `build_body()` is the one place that shape is written, and a runtime test drives the real actor with the
body it returns, asserting on the `as_of` column of the row that lands. The script exits non-zero when the sweep
graded nothing: HTTP 200 is the sidecar saying the actor ran, not the instrument saying it measured anything. It
reads the receipt row back and prints the headline, the per-target statuses and the reference age; `--no-verify`
skips the read-back and says so.

## The knobs

Values live in [TUNABLES](TUNABLES.md) §2. What each governs:

| knob | what it does |
|---|---|
| `LEGBA_CORRECTNESS_GRADER_ENABLED` | off, the handler writes nothing at all — no row, no model call, no spend |
| `LEGBA_GRADER_DAILY_CEILING_USD` | the daily paid ceiling; at zero F2 and F3 are never called |
| `LEGBA_GRADER_PASS_BUDGET_SECONDS` | the wall clock one tick's target loop may hold; zero or less is unbounded |
| `LEGBA_GRADER_REFERENCE_GRACE_DAYS` | how long past its `window_end` a reference stays current; overrides the descriptor option |
| `LEGBA_LLM_MODEL_NAME` | the core plane's served model id, and part of the calibration identity |

Descriptor options, through a registry PUT of `handler_options['correctness_grader']`: `grader_targets` (empty means
every target with a live reference), `max_targets_per_run`, `max_claims_per_unit`, `max_claims_per_run`,
`head_window_days`, `as_of`, and `reference_grace_days` (the environment wins over this one). The sub-handler is
compiled into the registry image, so that image is rebuilt before this descriptor is registered.

## The read surface

The number appears beside the read it grades, not on a page of its own: a correctness share read apart from the
prose it measures is a statistic, beside it a caveat. `UnitCorrectnessBadge` mounts on each desk card in the country
units panel and on the cited-assessment card, taking `(analystId, targetId)` and nothing else. The badge string is
composed server-side, on the contract `/eval/scores` also keeps, so one place decides how a null share prints:

```
correctness 90.9% (10/11) · coverage 24.4% (n=45) · as of 2026-09-16 · single-family
```

`single-family` appears only when true, and `· reference stale (9.0 d)` appends when the number rests on a reference
aged past the grace. A unit whose reference bore on nothing reads `correctness unmeasured (0 decided)`.

| situation | what the reader sees |
|---|---|
| a `unit_correctness` row exists | the badge line; click to open the ledger |
| no row, and the country's newest reference is absent or past the grace | a muted `no reference` or `reference stale` chip |
| no row, and the reference is current | nothing at all — the grader has not reached this unit yet |

The last row is the important one: an empty space is the truthful render for an unmeasured unit, where a dash, a
`0%` or an optimistic `100%` would each be a claim the platform has not earned. Clicking the badge fetches the
per-claim ledger — every family's label unpooled, the adjudicated outcome, each decisive span — `contradicts` first,
because a refuted claim is why anyone opens it.

"Stale" has one definition, in the stdlib-only leaf `src/legba/data/correctness_reference_currency.py`, which the
grader re-exports and the route imports directly; reaching the rule through the grader's import graph 500s the route
instead, since the registry image carries no runtime analyst dependencies. So a reference inside the grace reads
`current` on the badge, and each row's age is the grader's stored `reference_age_days`: a badge expiring a reference
the grader still uses, or re-deriving an age from a window since edited, would restate a decision nobody made.

The API is `GET /api/v1/units/{target_id}/correctness` (`src/legba/data/registry/unit_correctness_api.py`, in the
substrate-reads router), taking `analyst_id`, `grain` (`desk` or `composition` — never pooled), `claims`,
`claim_limit`, and `limit`/`cursor` keyed on `analyst_id`. It returns the latest row per `analyst_id` plus a
page-level `reference` block carrying the target's newest reference, aged at now, and its `current`/`stale`/`none`
state; `none` stays page-level, since a row always has a reference. It is select-only, and a test asserts row counts
are unchanged across a request.

The FLEET is `GET /api/v1/v3/eval/grader_roster?nights=7&grain=desk` (`src/legba/data/registry/grader_roster_api.py`,
its own leaf mounted beside the v3 telemetry router), rendered as the Eval Scorecard's "grader correctness, roster"
section. Per-unit badges answer "was this desk right"; nothing answers "is the instrument reaching anything", and on a
typical night most of the graded desks carry a null correctness share — the reference decided nothing they said.
That is the number this route exists to put on the page. The route returns each desk's newest graded night inside the window
(`DISTINCT ON (target_id, analyst_id)`), its trailing means over that window, and the roster totals, ordered thinnest
coverage first because the desks nobody looked at are the finding. Two roster figures ship, each labelled: the
unweighted `correctness_mean_of_desks` (one desk, one vote) and `claims_pooled.correctness_pooled` (every claim under
the rendered rows, counted once). They rest on different denominators and routinely disagree by a point or two at the same stamp, on
coverage under a tenth either way, so neither is ever served as "the" number. A null share is counted in
`desks_unmeasured` and excluded from both means; coalescing it to 0.0 would publish a roster correctness near an
eighth where the figure over the desks that have a number is around a half. The badge on each row is `correctness_badge`
verbatim, so the roster and the per-unit surface cannot print a null share differently. `grain` defaults to `desk` and
is echoed on the response: `country_composition` / `country_assessment` are graded against the same references as the
desks they compose over, so pooling the grains would count those claims twice. It is select-only and needs no index —
a 232-desk roster over 1,521 rows plans as a sequential scan, measured live at 16.3 ms / 652 buffers at `nights=7` and
22.0 ms at `nights=30`. It is NOT the operator gold-set axis `GET /v3/eval/correctness` serves: that one is a human's
read over `correctness_labels`, this one is the machine grader's against an independent reference, and the two are
never pooled.

## The composition gate

Authority climbs only as far as the verification underneath it reaches. Behind `LEGBA_COMPOSITION_CORRECTNESS_GATE`,
default off, the country composition composes only over desk units whose latest `unit_correctness` row exists, is
not single-family (unless `LEGBA_COMPOSITION_GATE_ALLOW_SINGLE_FAMILY` is set), and clears both
`LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS` and `LEGBA_COMPOSITION_GATE_MIN_COVERAGE`.

A unit that fails is **quoted, not dropped**: marked into the periphery tier the composition already has, out of the
load-bearing basis, not consuming the input cap, not driving salience, rendered as a delimited, hedged, capped
excerpt the composition may refer to but never rests on — a desk that silently vanished would make a composition
over three units look identical to one over eight. Both bars bind together, because a correctness share alone is
trivially gamed by silence. The strict default on single-family means a deployment at a zero ceiling composes over
nothing until the operator funds a second family or says one family is enough: it fails toward not asserting.

Every gated run stamps `finding.data["correctness_gate"]` with the bars in force, the basis count, and a verdict per
unit — `composed`, `quoted` or `no_number`, each with its reason and its numbers. The last two have the same effect
and are different facts, so they are recorded apart: a unit nobody graded is a gap in the instrument, a unit graded
at 40% is a finding about the read. When the gate empties the basis the composition names the gate, a correctness
withholding rather than an absence of reads, and the quarantine header names which reason applies to each row,
because a read withheld on correctness may have scored well on faithfulness. The gate and the faithfulness floor are
and-ed, never or-ed: a read the verify pass already withheld must not walk back into the basis because an
independent reference happened to agree with it. The thresholds are environment, not descriptor options — a bar
deciding what the platform will assert is a policy the operator owns — and an unparseable value falls back to the
shipped default, never to "no bar". With the flag off the composition is byte-identical to a tree without the gate,
down to the query set and the prompt bytes, and two tests enforce that.

## Seams

**The reference is not built here.** This job grades against whatever `unit_references` rows exist and records
`no_reference`, loudly, when there are none. Loading or topping up a reference by hand is
`scripts/load_unit_reference.py`; the scheduled builder is [its own analyst](REFERENCE_BUILDER.md) in its own
module. See [SEAMS](SEAMS.md).

**Read `thin_dimensions` with every number.** The builder stamps a dimension thin when it found under two verified
developments, which makes every claim on that dimension likelier to read `silent`. A low coverage share on a thin
dimension is the reference under-claiming, not the desk over-claiming, and the two must not be confused.

**A hand-loaded reference ages.** Past the grace its target reads `reference_stale` and grading stops for it — the
intended signal that the builder has stopped, not a failure of this job. Relatedly, `span_verified_rate` on a
reference row is the builder's number and is never re-derived at load time: only the builder had the archived page
text to check a decisive span against, and absent it the column is null, which is not zero.
