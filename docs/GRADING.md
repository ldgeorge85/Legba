<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# Grading

For an admin who runs or configures correctness grading: how references are built, how a desk's read is graded
against one, what the outputs are, and which settings control it.

Grading answers "is this read true" for one bounded unit. Three analysts do the work:

- `reference_builder` builds a **reference**: an account of what happened to one subject over one window, taken
  from the open web without reading Legba's own data.
- `reference_deterministic` builds a **keyed reference** from a machine-readable list, such as a vulnerability
  catalog indexed by CVE id.
- `correctness_grader` grades a unit's published read against the current reference and writes two shares, the
  per-claim ledger under them, and a receipt.

The grader reads references and never writes them, so it cannot grade against something it built itself. The wider
measurement layer is described in [ANALYSIS](ANALYSIS.md).

## Turning it on

All three descriptors ship `state: draft`, so they run only where the profile's lifecycle overlay activates them.
Each also has an environment flag that defaults to off. With the flag off, the handler searches nothing, calls no
model and writes nothing.

| analyst | descriptor | flag | schedule (UTC) |
|---|---|---|---|
| reference builder | `descriptors/analyst_reference_builder.yaml` | `LEGBA_REFERENCE_BUILDER_ENABLED` | hourly at :43 |
| keyed reference | `descriptors/analyst_reference_deterministic.yaml` | `LEGBA_REFERENCE_DETERMINISTIC_ENABLED` | daily at 05:53 |
| correctness grader | `descriptors/analyst_correctness_grader.yaml` | `LEGBA_CORRECTNESS_GRADER_ENABLED` | hourly at :20, 01:20 to 09:20 |

The sub-handlers are compiled into the registry image. Rebuild that image before you register or change one of these
descriptors. A descriptor change reaches the runtime only after the registry holds the new content hash.

## References

### What a reference is

A reference is one row in `unit_references`: one subject, one window (`window_days`, default 14), and a list of
developments. Each development carries an `item_id`, a `summary`, a `decisive_span` quoted from the source page, the
`outlet`, the `publish_date`, a source tier and the `date_source` that dated it. The row also records:

- `thin_dimensions`: the units for which fewer than `thin_below` (default 2) developments survived. A claim on a thin
  dimension is more likely to read `silent`, so low coverage there says the reference is short, not that the desk
  over-claimed.
- `span_verified_rate`: verified spans over candidates; null when there were no candidates.

Every writer goes through `_reference_store.py`: the builder, the keyed builder and the hand loader
(`scripts/load_unit_reference.py`). That keeps `thin_dimensions` meaning the same thing whoever built the row.

### Which subjects get one

The builder and the grader read the same roster, defined by the profile (`_reference_roster`):

- **Subjects**: every target whose live head is `active`, carries one of the profile's scored roster tags
  (`rosters.scored_subject_tags`) or `constants.reference_subject_tags` (see [PROFILES](PROFILES.md#constants)), and
  whose units produced within `roster_window_days` (default 30). A target with no head descriptor stays on.
- **Units**: the scored subjects' units, plus every unit scoped to a reference leg (`reference_units`).
- **Expected units per subject**: the head's tags through the roster's fan-out rule (`units_for_tags`). A shipping
  lane is not expected to carry country units. The composition is expected only on a scored subject.

Each subject's code comes from the subject ladder (`legba.data.subject`, see [PROFILES](PROFILES.md)): the
descriptor's `scope.subject_code`, else its single `scope.geo` code, else the target id. Item ids carry that code
(`RD-<CODE>-…-<n>`). Non-country subjects are graded with the same rubric and families as countries.

### How the builder builds one

The model finds pages and quotes them. Code then decides what is accepted, over archived text the model cannot edit.
Each tick:

1. Keep roster targets whose newest reference is older than `cadence_days` (default 7), or that have none.
2. Sort them by last attempt (never-attempted first), then by most overdue. Skip targets inside their retry backoff.
   Take `max_targets_per_run` (default 1).
3. Run the core-plane model with `web_search` and `fetch_page` through the `web_access` pack. After every fetch the
   model must write a note with the span copied from the page. Old page text is evicted after a few turns.
4. At 70% of the tool budget, the wall clock or the token limit, or after two empty searches in a row, remove the
   tools and ask for one JSON object built from the pages read.
5. Run the fences below, count survivors per dimension, stamp thin dimensions, and write one row.

Search patterns come from each unit's `reference_queries` (profile data, with `{subject}` substituted). A unit with
none searches its own `query_terms` after the subject name; a unit with neither gets three generic queries.

| | fence | rejects |
|---|---|---|
| F1 | fetched-URL manifest | a development citing a page this run did not fetch and archive |
| F2 | domain blocklist | a host that refused twice; dropped for the rest of the run at no budget cost |
| F3 | note enforcement | a tool call that skips the note after a fetch; refused at no budget cost |
| F4 | date gate | a page whose own date is outside the window, or that has no date |
| F5 | tier 1–2 allowlist | discovery results outside official, IGO, central-bank, court, wire and record-press hosts |
| F6 | span verification | a `decisive_span` that is not an exact substring of the archived page |

At commit the fences run in the order F1, F4, F6, F5, so a rejection is attributed to the first fence that catches
it. The tier is derived from the URL. `LEGBA_REFERENCE_TIER12_HOSTS` adds hosts to the allowlist. A dimension that
still has fewer than two tier 1–2 developments is opened to tier 3, when the model declares the reference complete
and again at half the tool budget; those items are marked `tier3_admitted`. Hosts the fetcher cannot read
(`UNFETCHABLE_SUFFIXES` in `_reference_fences.py`) keep their tier but are removed from discovery.

F4 checks every rung below on every page. The strongest rung that answers sets `date_source`; each weaker rung that
gave a different date is recorded in `date_disagreement`.

| | `date_source` | read from |
|---|---|---|
| 1 | `json-ld` | `datePublished`, `dateCreated` or `uploadDate` |
| 2 | `meta:<key>` | publish-time `<meta>` tags in key order, ISO or an unambiguous `D.M.YYYY` |
| 3 | `time-datetime` | `<time datetime="…">` |
| 4 | `url-path` | a date embedded in the URL (`/YYYY/MM/DD/`, a `YYYY-MM-DD` slug, a `YYYYMMDD` id) |
| 5 | `dateline:<lang>` | one unambiguous visible dateline in the first 1,200 characters of the article text |
| 6 | `http_last_modified` | the HTTP header, official-class hosts only (`.gov`, `.mil`, `.int`, listed IGOs) |

Rung 5 accepts a closed set of formats with full month names, discards any date that carries a weekday or a clock
time (a masthead), and answers only when exactly one distinct date is found. Rung 6 needs response headers, which
only the admin-run fetch path (`_reference_page.fetch_and_archive`) has.

### Build limits and failures

| limit | descriptor option | environment default |
|---|---|---|
| wall clock per build | `build_max_seconds` | `LEGBA_REFERENCE_BUILD_MAX_SECONDS` (900) |
| tokens per build | `build_max_tokens` | `LEGBA_REFERENCE_BUILD_MAX_TOKENS` (1,200,000) |
| tool calls per build | `tool_call_cap` | none |

The descriptor option wins over the environment default. At 100% of the wall clock the build stops.

A failed build ends in one of three outcomes. All three write nothing and count as a failed attempt.

- `no_commit`: nothing to fence. Check the search plane, the budget and the loop.
- `all_rejected`: every committed development failed a fence. The receipt carries the rejection table.
- `empty_commit_with_material`: the model committed nothing although it had read a usable in-window page.

A failed build saves the model's notes and the loop's page list to
`<LEGBA_ARCHIVE_ROOT>/reference_notes/<target_id>.json`. The next build for that target uses them as leads, and
fetches and checks every URL again. The file is dropped after a day and cleared by a successful build.

### Retries

After `no_commit` or `all_rejected` a target is skipped for `retry_backoff_hours` (default 24).
`LEGBA_REFERENCE_RETRY_BACKOFF_HOURS` overrides the descriptor value, so you can change it without re-registering;
zero turns the backoff off. After three consecutive failures the target is flagged `unbuildable_by_lane` on the
receipt. It stays on the roster and keeps being retried; a [top-up](#top-ups) is the fix. Attempts are recorded on
the builder's own receipts (`data.attempts`).

If `cadence_days` is longer than the grader's grace period, each target reads `reference_stale` for part of every
cycle, and the builder's receipt warns about it. Lower `cadence_days` or raise `LEGBA_GRADER_REFERENCE_GRACE_DAYS`.

### Cost and web access

The builder uses only the core plane, so it has no spend ceiling of its own. Tokens and wall time are recorded in each
reference's `ref_json`. Search goes through the `web_access` pack; the descriptor's `governor_override` gives the
builder its own budget account (`web_access_reference_builder`) and can only tighten the pack's limits.

### Keyed references

`reference_deterministic` fetches a declared list once per tick. For each bound subject it writes one
`unit_references` row of the items dated inside the window, with builder `deterministic:<feed_id>`. Each item's
decisive span is the record itself, serialised canonically. The row's `ref_json.keyed` block tells the grader to
grade it [by key](#keyed-grading).

Options: `feed_id`, `feed_url`, `items_path`, `key_field`, `key_pattern`, `date_field`, `subject_field`,
`summary_fields`, `outlet`, `source_tier`, `list_complete` (default `false`; only a complete list lets an absent key
count as evidence), `bindings` (`target_id=value|value`, matched against `subject_field`) and `dimensions` (default:
the subject's own units). The shipped descriptor has no bindings. Bind a subject only when its reads name the list's
keys; otherwise every claim grades `silent`.

### Top-ups

A scheduled build can miss important developments even when everything it kept is verified. A top-up adds to it by
hand:

```bash
PYTHONPATH=src python3 scripts/reference_topup_packet.py --candidates            # which targets need it most
PYTHONPATH=src python3 scripts/reference_topup_packet.py --target <target_id>    # write the packet
# run a model of your choice over the packet, outside the platform
PYTHONPATH=src python3 scripts/load_unit_reference.py additions.json --merge-into <reference_id>
```

The merge re-fetches every cited URL and re-runs the date gate and span check, dropping additions that fail. It
writes a new row and leaves the original as it was. For an `unbuildable_by_lane` target with no reference, load the
result without `--merge-into`. A hand-loaded reference ages like any other.

## Grading a read

### What the numbers mean

```
correctness_share = contains / (contains + contradicts)
coverage_share    = (contains + contradicts) / n_claims
```

Correctness is over the claims the reference bears on; coverage is over every claim the unit made. Read them
together: a correctness of 1.0 at a coverage of 0.05 is one claim confirmed out of twenty. Both are stored with their
`n` and are null when their denominator is empty. Zero means every decided claim was contradicted; null means nothing
was decided. A true claim the reference does not mention reads `silent`. A claim the families split on is `split`,
counted in the coverage denominator only.

### The rubric

The rubric is `src/legba/data/analysts/rubrics/ANNEX_C_v4.md`, with three labels: `contains` (a development states
the claim's matter), `contradicts` (a development states something incompatible, including a reported change against
"no material change"), and `silent` (nothing bears on it). Its sha256 is checked at import against the pin in
`_correctness_rubric.py` and stored on every row as `unit_correctness.rubric_sha`. Changing the rubric takes three
changes together: the file, the pin, and a new passing calibration row.

### Model families

| family | model | component |
|---|---|---|
| F0 | the core plane's served model (`LEGBA_LLM_MODEL_NAME`) | `llm.primary.openai_compat` |
| F2 | a second model family | the stack component named in `_correctness_grade.FAMILIES` |
| F3 | a third model family | the stack component named in `_correctness_grade.FAMILIES` |

F0 is the same model that writes the reads. A family with no handler wired is skipped and named on the receipt. A
label needs two of three families to agree, else the claim is `split`. An unparseable reply is never turned into a
label, and `max_tokens` is never sent.

`LEGBA_GRADER_DAILY_CEILING_USD` (default 0) bounds what F2 and F3 may spend per day. At zero they are not called:
every claim carries F0's label alone and is written `single_family`. Above zero, the guard refuses any call that could
take the day past the ceiling. An unparseable or negative value reads as zero. F2 and F3 grade only the claims F0 did
not call `silent`, so a `silent` claim always carries one family's label.

### The calibration gate

The grader publishes nothing unless a `grader_calibrations` row has the same `rubric_sha`, covers every model id the
run will use, and has `gate_pass` true. Changing `LEGBA_LLM_MODEL_NAME`, or a family's model id in `FAMILIES`,
therefore stops the grader until a re-gate passes.

```bash
PYTHONPATH=src python3 scripts/load_unit_reference.py --seed-calibration      # seed the passing row
PYTHONPATH=src python3 scripts/correctness_regate.py --target <target_id>     # draw and estimate only
# graded re-gate, in the registry container
PYTHONPATH=/app/src python3 /app/scripts/correctness_regate.py --target <target_id> --grade --cap <usd>
```

`--target` draws today's claims. To check a new model against the old one, use `--packet <frozen_packet.json>`
instead, which grades a frozen claim set through the same path; the loader refuses a packet whose rubric or output
contract differs from this build's. The draw's seed is derived from the rubric digest, the model ids and the date,
and a unique index on `(rubric_sha, packet_sha, model_ids)` refuses a second row for the same draw. A failed gate
still writes its row, with `gate_pass` false.

### What runs, per target

1. Resolve the reference current at the stamp (see [reference currency](#reference-currency)). None gives
   `no_reference` or `reference_stale`, and nothing is graded.
2. Freeze the subject's desk units and, on a scored subject, its composition, as they stood at `as_of`. An expected
   unit with no head is recorded missing.
3. Split the read into claims (`verify._segment_claims`) and apply the [exclusions](#exclusions).
4. Build one packet for all families. The reference is reduced to `item_id`, `summary`, `decisive_span`, `outlet`
   and `publish_date`, then leak-scanned; a leak refuses the packet.
5. Check the calibration gate.
6. Grade one call per claim per family. A span that is not verbatim gets one corrective retry; if that fails too, the
   label stands with `span_unverified`.
7. Write the unit row and its ledger in one transaction.

`(analyst_id, target_id, head_id, reference_id, rubric_sha)` is unique and checked first, so a re-run repeats no
calls.

### Keyed grading

Against a keyed reference the grader calls no model and matches each claim by key (`_correctness_keyed`):

| the claim names | it negates | key on the list | label |
|---|---|---|---|
| no key of this kind | | | `silent` |
| a key | no | yes | `contains` |
| a key | yes | yes | `contradicts` |
| a key | no | no, list complete | `contradicts` |
| a key | yes | no, list complete | `contains` |
| a key | either | no, list not complete | `silent` |

Rows are written under family `D0`, model `deterministic:key_match/1`, flagged single-family and marked
`families.keyed`. The calibration gate still applies ([SEAMS](SEAMS.md) #70).

### Exclusions

Every excluded span is stored with its reason: `machine_line_registered_shape`, `roster_enumeration_*`,
`read_bookkeeping_not_world:<tell>`, `round_notice`, `heading`, `under_40_chars[_after_markers]`,
`not_judgeable_span`, `prior_relative:<pattern>`. The composition's telemetry sections are redacted first.

Prior-relative spans compare the read with the desk's previous read ("no material change versus the prior read"),
which a reference cannot judge. The patterns (`no_change_versus_prior`, `unchanged_from_prior`,
`as_in_the_prior_read`, `already_noted_in_the_prior_read`, `remains_as_previously_assessed`,
`nothing_alters_the_standing_read`) require the no-change wording to be joined to a mention of the earlier read, so
plain absence claims ("no new disruptions") are still graded. A change to the exclusions bumps
`GRADER_PIPELINE_VERSION`, stored on `unit_correctness.families.pipeline_version`; it does not need a re-gate.

### Cadence and rotation

`LEGBA_GRADER_PASS_BUDGET_SECONDS` limits one tick's target loop (zero or less is unbounded). A target in progress
always finishes; the rest are named on the receipt and go first next tick. `max_targets_per_run` limits one tick.
Targets are taken least recently graded first, never-graded first, then by `target_id`, so any cap cycles the roster.

### Reference currency

A reference stays current until a newer one replaces it or it ages out:
`window_start <= as_of <= window_end + LEGBA_GRADER_REFERENCE_GRACE_DAYS` (default 7). Among references that cover
the stamp, the newest `window_end` wins. The environment variable overrides the descriptor option
`reference_grace_days`; zero means the stamp must fall inside the window. Every row stores `reference_age_days`
(`as_of - window_end`). Past the grace the target is `reference_stale`, which is reported apart from `no_reference`.
The rule lives in `src/legba/data/correctness_reference_currency.py`, shared by the grader and the API routes.

## Outputs

### Receipts

Each tick writes one `analyst_outputs` row, with structured fields on `data.data`. The grader's
(`analyst_id = 'correctness_grader'`) reads like this:

```
Correctness grader — graded <n> unit(s) across <n> target(s) for <cost>.
  as_of=… rubric=ANNEX_C_v4.md sha=… pipeline=…
  ceiling=<ceiling>/day spent_before=<spent> targets=[…]
  reference grace=7d (LEGBA_GRADER_REFERENCE_GRACE_DAYS) — current until window_end + grace
  calibration: pooled=… packet=… n_atoms=…
  - [ok] <target_id>: <n> unit(s), <n> claim(s), <cost>, reference_age=<days>d
      <unit>: correctness=<share> (n=…)  coverage=<share> (n=…)
```

Rows under different rubric or pipeline stamps should not be pooled. A `—` correctness beside a non-zero coverage
count means unmeasured. Lines for excluded spans, deferred targets and warnings appear only when they apply.

The builder's receipt (`analyst_id = 'reference_builder'`) is also its attempt ledger. It gives the window, cadence,
roster and due counts; a `CHOSE` line naming the target and why; `next` lines for the rest of the queue with a reason
(`never_attempted`, `oldest_attempt`, or `retry_backoff` with its return time); and per target the outcome, survival
rate, pages read, thin dimensions, tokens and wall time. Warnings include developments from hosts with no recorded
licence class.

### Tables

| table | one row is | written by |
|---|---|---|
| `unit_references` | one reference for one subject and window | builder, keyed builder, hand loader |
| `unit_correctness` | one unit's shares at one stamp, against one reference, under one rubric | grader |
| `unit_correctness_claims` | one claim: each family's label, the outcome, the decisive spans | grader |
| `grader_calibrations` | one calibration run and whether it passed | the two calibration scripts above |

`unit_references` is a different table from `unit_reference_labels`, which holds the attention measure's labels.

### Badge and API

`UnitCorrectnessBadge` shows the number on each desk card and on the cited-assessment card. The text is built on the
server, for example (illustrative):

```
correctness 90.9% (10/11) · coverage 24.4% (n=45) · as of 2026-01-01 · single-family
```

`· reference stale (<n> d)` is appended when the reference is past the grace, and a unit whose reference decided
nothing reads `correctness unmeasured (0 decided)`. Clicking opens the claim ledger, `contradicts` first. With no row,
the card shows a muted `no reference` or `reference stale` chip, or nothing when the reference is current and the
grader has not reached the unit yet.

`GET /api/v1/units/{target_id}/correctness` (`unit_correctness_api.py`) takes `analyst_id`, `grain` (`desk` or
`composition`), `claims`, `claim_limit`, `limit` and `cursor`, and returns the latest row per analyst plus the
target's newest reference and its `current`, `stale` or `none` state.

`GET /api/v1/v3/eval/grader_roster?nights=7&grain=desk` (`grader_roster_api.py`) feeds the Eval Scorecard's roster
section: each desk's newest graded night, trailing means and roster totals, thinnest coverage first. It reports
`correctness_mean_of_desks` (one vote per desk) and `claims_pooled.correctness_pooled` (every claim once). Null shares
count in `desks_unmeasured` and are left out of both. Compositions are reported separately because they are graded
against the same references as their desks. `GET /v3/eval/correctness` is a different measure, based on human labels.

## The composition gate

`LEGBA_COMPOSITION_CORRECTNESS_GATE` (default off) makes the country composition use only desk units whose latest
`unit_correctness` row clears `LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS` (default 0.8) and
`LEGBA_COMPOSITION_GATE_MIN_COVERAGE` (default 0.2) and is not single-family. Set
`LEGBA_COMPOSITION_GATE_ALLOW_SINGLE_FAMILY` to accept single-family rows; at the default zero ceiling every row is
single-family, so without it the gate leaves the composition nothing to use.

A unit that fails is quoted as a short hedged excerpt in the periphery tier and does not count toward the basis.
Each gated run records `finding.data["correctness_gate"]`: the bars, the basis count and a verdict per unit
(`composed`, `quoted` or `no_number`). The gate applies on top of the faithfulness floor. With the flag off the
composition is unchanged.

## Running by hand

```bash
PYTHONPATH=src python3 scripts/correctness_grade_now.py                       # grade every eligible target
PYTHONPATH=src python3 scripts/correctness_grade_now.py --target <target_id> --as-of 2026-01-01T00:00:00+00:00
PYTHONPATH=src python3 scripts/reference_build_now.py                         # build the next due reference
PYTHONPATH=src python3 scripts/reference_build_now.py --target <target_id> --dry-run --save /tmp/out.json
```

Both scripts PUT to the analyst actor's `run` method, the path the schedule uses, and skip the cooldown. Per-run
parameters must sit inside `options`; a parameter at the top level is dropped without an error:

```json
{"trigger_kind": "method",
 "options": {"grader_targets": ["<target_id>"], "as_of": "2026-01-01T00:00:00+00:00"}}
```

The builder passes `--target` as `reference_targets` and `--as-of` as `as_of`, the same key the grader reads. A named
target skips the cadence and the retry backoff. `--dry-run` builds and fences without writing; the reference is on
the receipt. `correctness_grade_now.py` exits non-zero when nothing was graded; `--no-verify` skips its read-back.

## Settings

Defaults are listed in [TUNABLES](TUNABLES.md). The three on/off flags are in [Turning it on](#turning-it-on).

| environment variable | effect |
|---|---|
| `LEGBA_GRADER_DAILY_CEILING_USD` | daily spend limit for F2 and F3; zero means they are not called |
| `LEGBA_GRADER_PASS_BUDGET_SECONDS` | wall clock for one grader tick |
| `LEGBA_GRADER_REFERENCE_GRACE_DAYS` | how long a reference stays current after `window_end`; overrides the descriptor |
| `LEGBA_LLM_MODEL_NAME` | the core plane's model id; part of the calibration identity |
| `LEGBA_REFERENCE_BUILD_MAX_SECONDS`, `_MAX_TOKENS` | default build limits; the descriptor wins |
| `LEGBA_REFERENCE_RETRY_BACKOFF_HOURS` | retry backoff; overrides the descriptor; zero turns it off |
| `LEGBA_REFERENCE_TIER12_HOSTS` | hosts added to the tier 1–2 allowlist |
| `LEGBA_ARCHIVE_ROOT` | where page archives and carried notes go; if unwritable, spans are checked in memory only |

Grader descriptor options (`handler_options['correctness_grader']`): `grader_targets` (empty means every target with
a current reference), `max_targets_per_run`, `max_claims_per_unit`, `max_claims_per_run`, `head_window_days`,
`as_of`, `reference_grace_days`.

Builder descriptor options: `reference_targets`, `max_targets_per_run`, `window_days`, `cadence_days`,
`roster_window_days`, `tool_call_cap`, `page_chars_to_model`, `min_developments`, `thin_below`, `build_max_seconds`,
`build_max_tokens`, `retry_backoff_hours`, and the pack `governor_override`.

## Known limits

- A reference is verified, not complete. A dimension can hold two verified developments and still miss the window's
  main event; nothing detects that.
- Targets whose sources cannot be fetched are flagged `unbuildable_by_lane` and get no number until a top-up loads a
  reference.
- Hosts with no recorded `license_class` are read at teaser depth: the span is checked in memory, the body is not
  archived, and the development carries `span_source: snippet`, so it cannot be re-verified later. See
  [SEAMS](SEAMS.md).
- A host that times out with an empty error is recorded as an ordinary failure, which looks the same as the web
  having nothing. There is no retry on timeout yet.
