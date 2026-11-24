# The forecast ledger

The forecast ledger holds **exogenous** forecasts: questions the world answers on a date, each
with every issuer's probability, sealed before the outcome is known and scored against it. A
probability on the ledger names what resolves it and when; a page never shows a number the
ledger does not hold.

The acute hazard pilot (`acute_forecasts`) is separate: it scores the platform's own hazard calls
and keeps its own Brier, segregated from the headline.

## What a row is

| Table | Holds |
|---|---|
| `forecast_questions` | the question, its class, resolver kind and spec, window, due date, grace hours, the condition text its author wrote, the pre-registered void rules, the subject desks, `sealed_at`, `status` (open · resolved · void) |
| `forecast_numbers` | one row per (question, issuer kind, issuer ref): the probability (clamped 0.01–0.99, three decimals) **or** a stated absence with its reason; the evidence as-of; a short rationale and a reference to the full record; `sealed_at` |
| `forecast_resolution_proposals` | the 0242 proposal table; unused — the resolver job writes `forecast_resolution_records` |
| `forecast_resolutions` | the outcome (yes · no · void), who resolved it, the evidence refs, `early`; one active row per question |
| `forecast_resolution_records` | every act on an outcome, append-only (migration 0244): a resolver's **proposal**, a **confirmation**, a person's **override**, a **correction** — with the evidence, clauses, judgements, searched scope, void-rule reading, resolver version and as-of |
| `forecast_scores` (view) | every number with a probability against the active yes/no resolution: (p − o)² |
| `scorecard_entries` | weekly rows of a hand-kept scorecard (declared; no writer in the tree) |
| `override_log` | every override: who, what, the before/after hashes, a reason (required) |

**Issuers.** `operator` (the number of the person who runs the instance; the issuer kind keeps that name), `tower` (a scheduled analyst's number: a
finding carrying `data.forecast = {slug, p}`), `consult`, `frontier_arm` (a frontier model reading
the same evidence packet), `clef_arm` (the Clef-flash decision route, $0), and
`seat`, `actor_model` and `synthesis`. An issuer with no number is recorded as **absent, with the
reason** — never imputed, never shown as a blank.

## The resolver vocabulary

Every question resolves by one of six kinds. The spec names what the resolver checks.

| Kind | Resolves on | Resolves itself? | Spec keys |
|---|---|---|---|
| `event_reported` | an event class involving named actors or places inside a window, confirmed by one primary official source or two independent reporting sources | proposed, a person accepts | `class` (+ actor, target area, `window`) |
| `official_document` | a named body publishes a document of a stated type | proposed, a person accepts | `issuers`, `doc_type` |
| `official_result` | a named authority publishes a result (election, vote, judgment) | proposed, a person accepts | `authority`, `outcome` |
| `series_threshold` | a named numeric series against a threshold on a date | **yes** | `source`, `series`, `op`, `value`, `on` |
| `count_threshold` | an official count from a named authority against a threshold | proposed, a person accepts | `authority`, `count`, `op`, `value` |
| `scheduled_event_status` | a `scheduled_events` row is held, postponed or cancelled | proposed, a person accepts | `scheduled_event_id`, `expect` |

A spec may add `also: [{kind, spec, negate}]` — sub-conditions that must all hold (a halt
announced by both parties **and** no attributed strike in the last seven days). `event_reported`
needs a window, on the question or in the spec.

Two endogenous kinds, `indicator_state` and `desk_convergence`, score the platform's own desks.
They are stored with `exogenous = false` and never enter the headline Brier.

**Void rules** are written before the seal: the resolver source publishes nothing by due + grace;
the premise fails before the window opens; an ambiguity a person decides, recorded as an
`override_log` row. An open question past due + grace shows as **overdue**; it is never silently
expired.

## The seal

- A question and each number are hashed at insert (`content_sha256`, canonical JSON). A number's
  hash includes its question's hash, so a number is bound to the exact words it was stated on.
- `receipt_anchor` folds every ledger hash into the day's Merkle root after the receipt-chain
  heads, and the root goes to two OpenTimestamps calendars: "stated before the outcome" is
  checkable outside the instance. Ledger leaves have been in the live anchor since the anchor of
  2026-11-10 (contract `2026-11/st0`); the first seven sealed questions and their 28 numbers, as
  sealed (sealing does not change a hash), are in every anchor from 2026-11-11 (sealed 00:00,
  anchored 00:10), the eighth question and its number from 2026-11-14 — checked on 2026-11-14
  by recomputing the live roots of 11-11 (127 leaves: 92 chain heads + 7 questions + 28 numbers) and
  11-14 (131 leaves: 93 heads + 8 questions + 29 numbers + the audit-chain head) from the rows and
  matching them byte for byte.
- **Inclusion is provable from migration 0262 on.** Until then the anchor kept only its root, so one
  seal's presence in it could be recomputed only by someone holding every leaf of that day. From the
  first anchor after 0262 the handler stores the tree it anchored, in order
  (`receipt_anchor_trees` / `receipt_anchor_leaves`, `data/anchor_proof.py`; only on the tick whose
  root IS the day's anchored root; a stored day is never rewritten and past days are never
  back-filled). `GET /v3/ledger/seal-proof?question_id=` serves, for the question and each of its
  numbers, the leaf, its sibling path, the root, the day and that day's calendar rows (submitted /
  pending; the timestamp proof itself is never verified on the server). The console recomputes the
  leaf from the Ledger route's hash, folds the path in the browser and compares the result with the
  root the anchors route lists; it says "included in the anchor of <day>" only when that holds, and
  that the anchors before that day kept their roots only. A forecast sealed before 0262 is therefore
  proven from the first stored day, and timestamped by root alone before it.
- **Sealing** stamps `sealed_at` on a question and every number on it, in one transaction. It is
  refused while the question has no `operator`-issuer number, fails its resolver spec, still carries an
  `<OPERATOR …>` placeholder, or holds a number stated on an earlier wording.
- After the seal: no new number; no change to a sealed number; no change to a sealed question but
  its `status`. DELETE is refused on every ledger table, sealed or not. The database triggers
  enforce this for every role.
- Before the seal a question or number may be restated — the condition text and
  numbers can arrive after the questions do.

## Resolution

No model decides an outcome. A resolver is a RULE over the record; a person confirms it or decides
what it cannot.

**The resolvers** (`data/forecast_resolvers.py`) cover the kinds the sealed questions use —
`event_reported`, `official_document`, `official_result`, `series_threshold`, and `also`
sub-conditions (negated or not). `count_threshold` and `scheduled_event_status` refuse loud
(SEAMS #76). A resolver reads canonical signals sentence by sentence (title, summary, archived text)
for a report of the act in the realis mood, by the named actor, inside the window: an agreement to
act, the future or conditional, a denial, and a sentence recalling an earlier act (a month or year
outside the window, "from time to time", "the most recent ...") are not reports. Confirmation is the
sealed one — a primary official source of an actor (source class `official`, its geo) or two
independent publishers (two state outlets of one state count once); "acknowledged by" questions need
the actor's own channel. `series_threshold` reads LIVE `observations` of the series named by the
sealed `source` + `series` (id `<source>.<...>.<series>`; EIA RBRTE is `eia.pet_spot.rbrte`, from
`source.eia.petroleum_spot_daily`), each day AS RECORDED at the as-of (the newest vintage with
`record_time` at or before it — a later revision never leaks into an earlier reading), with the
observation id and its fetch receipt (`fetch_version:<id>`) as evidence. The value on the date
itself; a day in the seven before it stands in only once a LATER day is held (the publisher's
release passed the date without a value — a holiday); with nothing past the date held, the reading
is `undetermined` and at due + grace a judgement, never a void read off a weekly publisher's lag
(st1.2). The lexicon is in the module and versioned (`RESOLVER_VERSION`).

**A proposal** says: `yes` · `no` · `void` · `undetermined` (grace still running, a source below the
bar, a series not held) · `needs_judgement`; each clause of the criterion (`met` · `unmet` ·
`below_bar` · `judgement`); the evidence rows and spans with their publisher, source class, fetch
and publication times against the window; the judgements it waits on; the void rule and whether it
applies or is arguable; and what was searched (bounds, patterns, rows matched, signals in scope, the
official sources the corpus holds per actor), so an absence is a scoped absence. A clause a rule
cannot decide — "is this the exchange agreed at the named talks", "is the location beyond the named
island", "is the strike attributed", "does the condition hold" — and every sentence of the void rules
that says a person decides become judgements. A silence void rule reads `void` only when
every channel it names was silent; a quiet no-show (nothing reported because nothing happened) is
arguable and goes to a person.

**The job** (`forecast_resolver`, every 6 h; `scripts/resolve_forecasts.py run`) proposes on sealed
open questions at or past their due date (absence decides nothing before due + grace), writes a
proposal only when the reading is new (idempotent on its fingerprint; a changed reading supersedes
the last), settles `series_threshold` by rule, and raises one alert per new proposal through the
outward alert path (channel `forecast_resolution`). `rehearse` runs every sealed question as if due
now on a read-only session.

**A person** closes every other kind — on the console (Gate → Resolutions) or on the host,
through the same store functions:

- the console's queue (`GET /v3/ledger/resolution-queue`) shows, per waiting proposal, the evidence
  the resolver cites, the clauses, the rule in plain words and the judgements it waits on, then the
  decision: **Confirm** (`POST /v3/ledger/resolutions/{record}/confirm`, the body repeats the
  proposal's outcome; a void needs its reason), **Reject** with a reason
  (`…/reject`: a `rejection` record, migration 0262 — nothing closes, the question stays open for the
  resolver, the rejected reading can no longer be confirmed), or, where the rule cannot decide, a
  **judgement** with a reason (`…/judgement`: an override with the proposal as its basis). Each passes
  an in-page "are you sure" step and the page says what happened. A wrong decision is corrected by
  an admin or the owner (`…/{act}/correct`): a new record that supersedes it with a reason, both kept.
  **Who decides**: admins and the owner any question; a contributor any question except one where
  they entered a number (a `forecast_numbers` row whose `issuer_ref` or `detail.entered_by` is their
  actor), and the queue hides those from them (saying how many and why); readers and services never.
  The record keeps the actor and the role (`actor_role`: owner / admin / contributor); readers see
  the role and the date, never the person. Every act lands one signed, chained audit-log row
  (`namespace forecast_ledger`) in the same transaction; a repeat returns the first act;
- `resolve_forecasts.py confirm --record <proposal> --actor operator:<id>` — a yes/no/void proposal;
- `resolve_forecasts.py reject --record <proposal> --actor operator:<id> --reason "…"` — the
  console's Reject;
- `resolve_forecasts.py override --slug <q> --outcome yes|no|void --actor operator:<id> --reason "…"
  [--judgement <key>] [--basis <proposal>] [--evidence <ref>]` — a `needs_judgement` or
  `undetermined` proposal, or a disagreement: an `override_log` row (who, why), then the resolution;
- `resolve_forecasts.py correct …` — a later change: a new resolution (the old retires) and a
  record that supersedes the act it corrects. Nothing is ever edited.

The store's rules hold underneath: a resolution is refused before the due date unless it is marked
`early` **and** names its evidence; a yes/no is refused on a question that was never sealed; a second
resolution is a correction with a reason and an `override_log` row. The question's `status` is the
one column the seal lets change. Every record's hash folds into the receipt anchor beside the 0242
leaves.

## Adding a forecast

1. Write the question into `seeds/forecasts.yaml` (gitignored; the schema doc with two fictional
   rows is `seeds/forecasts.example.yaml`): slug, question, resolver kind and spec, window, due
   date, grace, void rules, the subject desks, and `operator_probability` with `operator_as_of`
   (or `null` while owed).
2. `scripts/load_forecasts.py --dry-run` — every row valid; the seal preflight lists what is still
   owed.
3. `scripts/load_forecasts.py` — loads the questions and the `operator`-issuer numbers; a re-run writes
   nothing.
4. `scripts/forecast_arms.py` — states the comparison arms' numbers at one as-of from one evidence
   packet (the question, its resolver, each subject desk's served brief). The Clef arm costs
   nothing; the frontier arm is paid and refuses without `--execute --ceiling-usd N` and the
   verified prices file; a call is admitted only while the spend so far plus its worst case fits
   the ceiling. The tower arm records the scheduled analysts' number or its absence.
   `--dry-run` prints the packets and the cost bounds and calls nothing.
5. `scripts/load_forecasts.py --seal` — seals every question in the file, or none.

Run the arms after the question's wording is final: a question still carrying a placeholder is
skipped, and the seal refuses a number stated on an earlier wording.

## Where it shows

`GET /api/v1/v3/ledger/forecasts` lists every question, open first by due date, with every
issuer's number, the sealed state and the resolution. In the workstation it is the **Forecasts**
tab of the Ledgers panel.

`GET /api/v1/v3/ledger/scorecard` is the **Scorecard** tab beside it: every question with its status,
due date, each arm's number and the spread between arms, the outcome and its evidence once resolved,
what is due next, and void and single-arm questions apart. **No Brier figure appears anywhere** — not
per number, per arm, baseline or skill — until `SCORES_MIN_RESOLVED` (5) questions have resolved: the
payload carries `scores_withheld: true` and the threshold, and the page says "n resolved; scores
appear at 5". From 5: Brier per number, the arm table (mean Brier with n over questions that carry two
or more arms), the climatological 0.5 baseline (and a declared `base_rate`) and skill. Reliability
bins wait for their own minimum (`calibration_honesty`) and say "n too small" until then. It reads only the sealed ledger — never the legacy calibration figures, which stay
labelled untrusted. The consult reads the same ledger through `list_due_resolvers`.

## Code

`src/legba/data/forecast_ledger.py` (the store and the vocabulary),
`src/legba/data/migrations/0242_forecast_ledger.sql`, `0244_forecast_resolution_records.sql`,
`src/legba/data/forecast_resolvers.py`, `forecast_evidence.py`, `forecast_resolution.py`,
`forecast_scoring.py`, `data/registry/forecast_scorecard_api.py`,
`data/analysts/deterministic_handlers/forecast_resolver.py`, `scripts/resolve_forecasts.py`,
`src/legba/data/registry/forecast_ledger_api.py`, `scripts/load_forecasts.py`,
`scripts/forecast_arms.py`, `legba-ui-v3/src/panels/system/Ledger.tsx` with
`legba-ui-v3/src/lib/ledgerModel.ts`, `panels/system/Scorecard.tsx` with `lib/scorecardModel.ts`.
