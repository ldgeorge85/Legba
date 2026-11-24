# The forecast ledger

For an admin or contributor who adds forecasts, resolves them, or reads the scorecard.

The forecast ledger holds **exogenous** forecasts: questions the world answers by a date. Each question carries every
issuer's probability, is sealed before the outcome is known, and is scored against the outcome. Every probability on
the ledger names what resolves it and when, and no page shows a forecast number the ledger does not hold.

The acute hazard pilot (`acute_forecasts`) is separate. It scores the platform's own hazard calls and keeps its own
Brier score, apart from the headline.

## Tables

| table | holds |
|---|---|
| `forecast_questions` | the question, its class, resolver kind and spec, window, due date, grace hours, condition text, void rules, subject desks, `sealed_at`, `status` (open, resolved, void) |
| `forecast_numbers` | one row per question, issuer kind and issuer ref: a probability (0.01–0.99, three decimals) or a stated absence with its reason, plus evidence as-of, a short rationale and `sealed_at` |
| `forecast_resolution_proposals` | created by migration 0242 and unused; the resolver writes `forecast_resolution_records` |
| `forecast_resolutions` | the outcome (yes, no, void), who resolved it, the evidence refs, `early`; one active row per question |
| `forecast_resolution_records` | every act on an outcome, append-only (migration 0244): proposal, confirmation, rejection, override, correction |
| `forecast_scores` (view) | every probability against the active yes/no outcome: (p − o)² |
| `scorecard_entries` | weekly rows of a hand-kept scorecard (declared; nothing in the tree writes it) |
| `override_log` | every override: who, what, before and after hashes, and a required reason |

**Issuer kinds** (`ISSUER_KINDS` in `forecast_ledger.py`): `operator` (the person running the instance), `tower` (a
scheduled analyst's number, from a finding carrying `data.forecast = {slug, p}`), `consult`, `frontier_arm` (a
frontier model reading the same evidence packet), a second comparison arm on the deployment's decision route, and
`seat`, `actor_model` and `synthesis`. An issuer with no number is stored as absent, with the reason.

## Resolver kinds

Every question resolves by one of six kinds. The spec names what the resolver checks.

| kind | resolves on | closes itself? | spec keys |
|---|---|---|---|
| `event_reported` | an event class involving named actors or places inside a window, confirmed by one primary official source or two independent reporting sources | no, a person confirms | `class` (+ actor, target area, `window`) |
| `official_document` | a named body publishes a document of a stated type | no, a person confirms | `issuers`, `doc_type` |
| `official_result` | a named authority publishes a result (election, vote, judgment) | no, a person confirms | `authority`, `outcome` |
| `series_threshold` | a named numeric series against a threshold on a date | yes | `source`, `series`, `op`, `value`, `on` |
| `count_threshold` | an official count from a named authority against a threshold | no, a person confirms | `authority`, `count`, `op`, `value` |
| `scheduled_event_status` | a `scheduled_events` row is held, postponed or cancelled | no, a person confirms | `scheduled_event_id`, `expect` |

A spec may add `also: [{kind, spec, negate}]`, sub-conditions that must all hold. `event_reported` needs a window, on
the question or in the spec. Two endogenous kinds, `indicator_state` and `desk_convergence`, score the platform's own
desks; they are stored with `exogenous = false` and never enter the headline Brier score.

**Void rules** are written before the seal, for example: the resolver source publishes nothing by due + grace, or the
premise fails before the window opens. An ambiguity a person decides is recorded in `override_log`. An open question
past due + grace shows as **overdue** until someone closes it.

## Sealing

- A question and each number are hashed at insert (`content_sha256`, canonical JSON). A number's hash includes its
  question's hash, so a number is bound to the wording it was stated on.
- `receipt_anchor` folds every ledger hash into the day's Merkle root and submits the root to two OpenTimestamps
  calendars, so "stated before the outcome" can be checked outside the instance.
- From migration 0262 the anchor also stores the tree it anchored (`receipt_anchor_trees`, `receipt_anchor_leaves`,
  `data/anchor_proof.py`). `GET /v3/ledger/seal-proof?question_id=` returns, for the question and each number, the
  leaf, its sibling path, the root, the day and the calendar rows. The console recomputes the leaf, folds the path in
  the browser and says "included in the anchor of <day>" only when the result matches. Days before 0262 kept only the
  root, so a forecast sealed earlier is provable from the first stored day onward.
- **Sealing** stamps `sealed_at` on a question and all its numbers in one transaction. It is refused while the
  question has no `operator` number, fails its resolver spec, still has an `<OPERATOR …>` placeholder, or holds a
  number stated on an earlier wording.
- After the seal, no number can be added or changed, and a question can change only its `status`. DELETE is refused
  on every ledger table. Database triggers enforce this for every role.
- Before the seal, a question or number may be restated.

## Resolution

No model decides an outcome. A resolver is a rule over the record; a person confirms its proposal or decides what it
cannot.

### The resolvers

`data/forecast_resolvers.py` implements `event_reported`, `official_document`, `official_result`, `series_threshold`
and `also` sub-conditions. `count_threshold` and `scheduled_event_status` raise an error (SEAMS #76). The lexicon is
in the module and versioned (`RESOLVER_VERSION`).

- Text kinds read canonical signals sentence by sentence (title, summary, archived text) for a report of the act, by
  the named actor, inside the window. An agreement to act, a future or conditional, a denial, and a sentence recalling
  an earlier act do not count. Confirmation needs a primary official source of the actor or two independent
  publishers (two state outlets of one state count once).
- `series_threshold` reads `observations` for the sealed `source` and `series`, each day as recorded at the as-of, so
  a later revision never changes an earlier reading. It uses the value on the date. A day in the seven before it
  stands in only once a later day is held (a holiday gap). With nothing past the date held, the reading is
  `undetermined`, and at due + grace it goes to a person.

A **proposal** gives the outcome (`yes`, `no`, `void`, `undetermined` or `needs_judgement`); each clause of the
criterion (`met`, `unmet`, `below_bar`, `judgement`); the evidence with publisher, source class and times; the
judgements it waits on; the void rule and whether it applies; and what was searched, so an absence is a scoped
absence. A silence void rule reads `void` only when every channel it names was silent; otherwise a person decides.

### The job

`forecast_resolver` runs every 6 hours (`scripts/resolve_forecasts.py run` by hand). Its descriptor ships
`state: draft`, so it runs only where the profile's lifecycle overlay activates it. It proposes on sealed open
questions at or past their due date, writes a proposal only when the reading changed, closes `series_threshold` by
rule, and raises one alert per new proposal on channel `forecast_resolution`. `resolve_forecasts.py rehearse` runs
every sealed question as if due now, read-only.

### Closing a question

On the console, open Gate → Resolutions (`GET /v3/ledger/resolution-queue`). Each waiting proposal shows its evidence,
clauses, rule and pending judgements. You can:

- **Confirm** (`POST /v3/ledger/resolutions/{record}/confirm`). The body repeats the outcome; a void needs a reason.
- **Reject** with a reason (`…/reject`). Nothing closes; the question stays open and that reading can no longer be
  confirmed.
- Record a **judgement** with a reason (`…/judgement`), where the rule cannot decide. This is an override with the
  proposal as its basis.

An admin or the owner corrects a wrong decision with `…/{act}/correct`, which writes a new record that supersedes it;
both are kept. Admins and the owner may decide any question. A contributor may decide any question except one where
they entered a number, and the queue hides those from them. Readers and services cannot decide. The record keeps the
actor and `actor_role`; readers see the role and date, not the person. Every act writes a signed audit-log row
(`namespace forecast_ledger`) in the same transaction, and a repeated act returns the first.

The same acts from the host:

```bash
python3 scripts/resolve_forecasts.py confirm  --record <proposal> --actor operator:<id>
python3 scripts/resolve_forecasts.py reject   --record <proposal> --actor operator:<id> --reason "…"
python3 scripts/resolve_forecasts.py override --slug <q> --outcome yes|no|void --actor operator:<id> --reason "…" \
    [--judgement <key>] [--basis <proposal>] [--evidence <ref>]
python3 scripts/resolve_forecasts.py correct …
```

The store also enforces: no resolution before the due date unless marked `early` with evidence; no yes/no on an
unsealed question; a second resolution only as a correction with a reason and an `override_log` row. Every record's
hash joins the receipt anchor.

## Adding a forecast

1. Write the question into `seeds/forecasts.yaml` (gitignored; `seeds/forecasts.example.yaml` documents the schema
   with two fictional rows): slug, question, resolver kind and spec, window, due date, grace, void rules, subject
   desks, and `operator_probability` with `operator_as_of` (or `null` while owed).
2. Run `scripts/load_forecasts.py --dry-run`. Every row should validate; the seal preflight lists what is still owed.
3. Run `scripts/load_forecasts.py` to load the questions and `operator` numbers. A re-run writes nothing.
4. Run `scripts/forecast_arms.py` to record the comparison arms' numbers at one as-of from one evidence packet (the
   question, its resolver, each subject desk's served brief). The frontier arm refuses without
   `--execute --ceiling-usd N` and a verified prices file, and admits a call only while spend so far plus its worst
   case fits the ceiling. The tower arm records the scheduled analysts' number or its absence. `--dry-run` prints the
   packets and cost bounds and calls nothing.
5. Run `scripts/load_forecasts.py --seal` to seal every question in the file, or none.

Run the arms after the wording is final: a question still carrying a placeholder is skipped, and the seal refuses a
number stated on an earlier wording.

## Where it shows

`GET /api/v1/v3/ledger/forecasts` lists every question, open first by due date, with each issuer's number, the sealed
state and the resolution. In the workstation it is the **Forecasts** tab of the Ledgers panel.

`GET /api/v1/v3/ledger/scorecard` backs the **Scorecard** tab: each question's status, due date, each arm's number and
the spread between arms, and the outcome and evidence once resolved. No Brier figure appears until
`SCORES_MIN_RESOLVED` (5) questions have resolved; until then the payload carries `scores_withheld: true` and the page
says "n resolved; scores appear at 5". From then on it shows Brier per number, mean Brier per arm over questions with
two or more arms, the 0.5 baseline (or a declared `base_rate`) and skill. Reliability bins wait for their own minimum
sample. The scorecard reads only the sealed ledger. The consult reads the same ledger through `list_due_resolvers`.

## Code

`src/legba/data/forecast_ledger.py` (store and vocabulary), `src/legba/data/migrations/0242_forecast_ledger.sql`,
`0244_forecast_resolution_records.sql`, `src/legba/data/forecast_resolvers.py`, `forecast_evidence.py`,
`forecast_resolution.py`, `forecast_scoring.py`, `data/registry/forecast_ledger_api.py`,
`data/registry/forecast_scorecard_api.py`, `data/analysts/deterministic_handlers/forecast_resolver.py`,
`scripts/resolve_forecasts.py`, `scripts/load_forecasts.py`, `scripts/forecast_arms.py`,
`legba-ui-v3/src/panels/system/Ledger.tsx` with `legba-ui-v3/src/lib/ledgerModel.ts`, `panels/system/Scorecard.tsx`
with `lib/scorecardModel.ts`.
