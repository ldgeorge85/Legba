<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# The reference builder

*The job that builds what the correctness grader grades against — on hardware we own, for nothing, under fences that
are code.*

## The gap it closes

[The correctness grader](CORRECTNESS_GRADER.md) answers "is this read true" against a **reference**: an independent
account of what happened to one country over one `window_days` window, built without reading our substrate. It
deliberately does not build one, because a grader that could build its own reference could close the loop on itself.
This job is the other half of that pair: one reference per country per window, span-anchored, date-gated, and honest
about where it is thin. It is the only writer of `unit_references`.

## Why it is a hybrid

The obvious design is to let a good model read the web and write the reference. Measured, that design selects badly
and dates badly: it carries a third of a knowledgeable reader's major developments, and it ships developments that
are simply false — an appointment from a previous year, dated off a site masthead because the page carried no
machine-readable date.

That asymmetry decides the shape. A thin reference **under-covers**; a reference carrying a false development
actively **mis-grades**, marking correct published work as contradicted. So:

* **the model finds and quotes** — free, on the core plane, producing a skeleton it can defend span by span;
* **code decides what is accepted** — every fence runs after the model commits, over archived text the model cannot
  reach, and nothing it asserts about a date, a URL or a tier is taken on its word;
* **thinness is declared, not hidden** — a dimension under `thin_below` surviving developments is written to
  `thin_dimensions`, the column the grader already reads, so the free lane under-claims rather than mis-grades;
* **the substance gap is the operator's money decision**, closed by [a top-up lane](#the-operator-top-up) nothing in
  the platform schedules or can start.

## The six fences

| | fence | what it prevents |
|---|---|---|
| F1 | **fetched-URL manifest** — a development may cite only a page this run actually fetched and archived | fabricated citations, including invented URLs |
| F2 | **domain blocklist** — a host that has refused twice is refused for the rest of the run, dropped from search results, and the refusal costs no tool budget | budget burned on a host that will never answer |
| F3 | **note enforcement** — after every fetch the next turn must be a plain-text note with the span copied out of the text just shown; tool calls that skip it are refused and cost no budget | spans written from memory after the page was evicted |
| F4 | **date gate** — the page's own date, resolved down a six-rung ladder (below), must fall inside the window; no date means rejected. The rung that answered is recorded on the development as `date_source`, and `date_disagreement` records every weaker rung that answered differently | a stale item dated off a masthead, and pre-window items |
| F5 | **tier 1–2 discovery allowlist** — searches are filtered to official, IGO, central-bank, court, wire and record-press hosts | anchoring a reference on an unvetted outlet and tiering it high |
| F6 | **span verification** — every `decisive_span` must be an exact substring of the archived page after whitespace and curly-quote normalisation | quoting a search snippet and calling it a page span |

### F4's date sources, strongest first

Until wave O the gate read three mechanisms, all of them in ISO: JSON-LD, a
`<meta>` table and `<time datetime>`. That is what a newsroom CMS emits.
Official primary sources emit something else, and the top-ups measured it —
`bundestag.de` ships `<meta name="date" content="25.09.2026">` (a key the table
already had, in a format the parser refused), `radio.gov.pk` ships no metadata
at all and only a visible `September 26, 2026`, and `opcw.org` ships a visible
`9 July 2026` with an HTTP `Last-Modified` that tracks its CDN. So a
parliament, a state broadcaster and an IGO were all inadmissible to a reference
while an ordinary newspaper was not.

Every rung is evaluated on every page. The **strongest that answers wins**, and
every weaker rung that answered with a **different** date is recorded in
`date_disagreement` rather than discarded — a page whose metadata contradicts
its own dateline is a fact a reader needs. Nothing is guessed; a page no rung
can date is rejected and counted, exactly as before.

| | `date_source` | read from | notes |
|---|---|---|---|
| 1 | `json-ld` | `datePublished` / `dateCreated` / `uploadDate` | |
| 2 | `meta:<key>` | the publish-time `<meta>` tags | walked in **key** order, not document order, so `article:modified_time` stays last; values are read as ISO **or** as an unambiguous `D.M.YYYY` |
| 3 | `time-datetime` | `<time datetime="…">` | |
| 4 | `url-path` | the date the URL embeds (`_url_canon.url_embedded_date`) | `/YYYY/MM/DD/`, a `YYYY-MM-DD` slug, a `YYYYMMDD` article id |
| 5 | `dateline:<lang>` | **one unambiguous visible dateline** in the article's opening | see the fences below |
| 6 | `http_last_modified` | the HTTP header | **official-class hosts only** (`.gov` / `.mil` / `.int`, a `gov`/`gouv`/`gob`/`go` second level, or a listed IGO) |

**Rung 5 reads visible prose, which is a reversal, and it is fenced five ways.**
R1 shipped a false development because a model read a masthead
("Wednesday, Sep 16, 2026") off an undated page whose article was from November
2024. So: the dateline is read from the **extracted text** (`head`, `nav`,
`footer`, `aside` and `script` are already gone); only the first 1,200
characters are scanned; only a closed format set is read — ISO, `D Month YYYY`,
`Month D, YYYY` and an unambiguous dotted date, with **full month names only**
in a closed set of languages chosen by the page's own `lang` hint; **a date
carrying a weekday or a clock is a masthead and is discarded**; and what
survives must be exactly **one distinct date**, so an index page, an archive
listing or a two-dateline page yields nothing.

**Rung 6 is last, and fenced, because a `Last-Modified` is a file mtime.** On a
news CDN it tracks the cache: the `opcw.org` fixture in
`tests/data_pkg/test_reference_dates.py` has one landing 79 days after the
page's own dateline. On an official publisher's document server it is
frequently the only date there is, which is why it is admitted at all — as the
weakest rung, named in the row, never silently.

**Where each rung is fed.** `_reference_page.fetch_and_archive` — the
operator-run door for the top-up merge and the proving harness — holds the
response and offers all six. The scheduled loop goes through the `web_access`
pack, whose `web_fetch` result carries `url` / `status_code` / `content_type` /
`body` and **no response headers**, so rung 6 does not answer there and the
loop is dated by the five rungs above it.

Fences are applied in the order F1, F4, F6, F5, so a rejected item is attributed to the fence that actually caught
it: an invented URL is a fabrication, not a bad quote. An unverified development is dropped, not flagged and kept.
The tier that F5 decides is **derived from the URL**, never read off a field the model wrote.

**The escape hatch on F5.** A dimension the record genuinely does not carry at record-press level would otherwise
come back empty, and an empty dimension teaches the grader nothing. So a dimension still under two tier 1–2
developments is opened to tier 3 — on a premature "reference complete", and again once half the tool budget is
spent, because without that second trigger a run that spends its whole budget gathering never reaches the hatch, and
that is precisely the run that needed it. Every item admitted this way is marked `tier3_admitted` with the dimension
it was admitted for, so a reader can see which parts of the reference rest on it.

**Reference-grade hosts this lane cannot read leave discovery and are refused at the fetch call.** Six are measured
unreadable — `reuters.com`, `apnews.com`, `timesofisrael.com`, `haaretz.com`, `ft.com` and `bloomberg.com` — through
robots exclusion, a browser challenge or a paywall, and browser impersonation changes nothing on any of them. They
keep their **tier**, because they are wires and papers of record; what they lose is the right to consume budget,
since a result the lane cannot read is not a lead. This is reachability data, not a quality claim, which is why a
host leaves the list by becoming readable rather than by being re-argued.

## What runs, in order

1. **Roster** — targets whose dimension desks have produced within `roster_window_days`, each with the dimensions
   *that* target actually carries. Per-target, because `thin` must mean the same thing for every country.
2. **Cadence** — drop targets whose newest reference is inside `cadence_days`. A target is due when its newest
   reference is older than the cadence, or when it has none.
3. **Order** — read the attempt ledger off this lane's own receipts, sort the due set by last attempt then by
   most-overdue, mark anything that failed inside `retry_backoff_hours` ineligible, and take `max_targets_per_run`
   from the eligible head. Nothing is ever removed from the queue.
4. **Loop** — the core plane under the stage-one instruction re-anchored to this country and window, with two tools
   (`web_search`, `fetch_page`) through the `web_access` pack. Page text older than a few turns is evicted; the
   model's note is the only record of it that survives, and the model is told so. If the last attempt on this target
   left carried notes, they are appended to the opening turn as leads.
5. **Commit** — at 70% of the tool budget, the wall clock or the token ceiling, or immediately on two dead searches
   running, the tools come off the table and the model is handed the manifest of pages it actually read and asked
   for one JSON object.
6. **Fences** — F1, F4, F6, F5 over what it committed.
7. **Thin** — per-dimension counts over the survivors; anything under `thin_below` is stamped.
8. **Write** — one row into `unit_references` through the shared writer.

## The scheduler, and why the tick is hourly

One reference is minutes of core-plane time and over a million prompt tokens, so a roster rebuilt every
`cadence_days` is affordable only if at most one build runs at a time. The tick therefore fires hourly and the
handler takes **one** target. That is the entire scheduler: the roster staggers itself, a country joins by appearing
in it, a skipped tick costs a country an hour rather than its slot, and there is no stagger table to drift out of
sync. Most ticks build nothing and cost two queries.

**The order is by last attempt, not by last success.** Overdue-ness measured from `unit_references.built_at` cannot
move for a target that *cannot* be built, because a failed build writes no row — so a country the lane cannot fetch
stays permanently the most overdue thing on the roster and every hourly tick picks it again. Eligible targets
therefore come first, ordered by last attempt ascending with never-attempted first, then most-overdue, then target
id; backed-off targets follow, soonest-to-return first, staying in the receipt's due queue carrying `reason:
retry_backoff` and the instant they come back, because the row has to say why a plainly overdue target was passed
over. Most-overdue is demoted to the tie-break, not deleted.

**The attempts are recorded on this lane's own receipts**, and nowhere else — no new table, no migration. Each
tick's receipt carries a compact `data.attempts` object: one entry per target the run put on the plane, three
scalars each (`at`, `status`, `ok`), capped. The next tick folds the last few hundred such rows into each target's
last attempt, outcome and consecutive-failure count; unreadable, it degrades to most-overdue ordering rather than
failing the run. `no_model` and `no_web` are **not attempts** — they are refusals before the build, nothing searched
and nothing spent, and recording them would rotate the queue on a misconfiguration that hits every target equally.

**The retry backoff and the three-strike flag.** After a build that ends `no_commit` or `all_rejected` the target is
ineligible for `retry_backoff_hours`, so a chronically failing country costs the lane one build a day rather than
one an hour and the roster still cycles far faster than the cadence needs. `LEGBA_REFERENCE_RETRY_BACKOFF_HOURS`
**wins over the descriptor knob** — the inverse of the two build walls, deliberately: those are volumes an operator
sizes once, where this is the lever reached for while the lane is stuck, when a descriptor PUT plus a re-register
plus a fresh actor record is exactly the ceremony that cannot be afforded. The receipt records which source it read,
and zero disables the fence, which is the only way to measure what it is doing.

After three consecutive failures a target is flagged `unbuildable_by_lane` on the receipt. It is never dropped and
never stops being retried — "this lane cannot fetch that country" is a statement about today's fetchers and licence
classes, both of which change, and a target quietly removed from a roster is how a country disappears from a
correctness programme without anyone deciding that. The flag routes it to the
[operator top-up](#the-operator-top-up), the one thing that can close it.

## Where the cadence meets the grader's currency window

The grader treats a reference as current until `window_end` plus its grace; this lane rebuilds every `cadence_days`.
Each target is therefore covered for the grace and reads `reference_stale` for the rest of the cycle, and at equal
grace and cadence it is covered throughout. Nothing else would say so — the builder would report healthy builds and
the grader stale references, each correct, neither naming the other — so the builder's receipt names it, with both
remedies and the note that neither is its to take: lower `cadence_days` toward the grace, at more core-plane time
and still no money; or raise the grader's grace, free, but grading claims against an account of the world that much
older.

## The commit wall

A build is a bounded thing, in code, and none of the bounds are the model's to choose. Without them a loop's only
exits toward a commit are the tool budget running out or the model volunteering that it is finished, and a model
that does neither runs to the round limit while every round re-sends the whole conversation.

| wall | knob and environment variable |
|---|---|
| wall clock, whole build | `build_max_seconds` · `LEGBA_REFERENCE_BUILD_MAX_SECONDS` |
| tokens, whole build | `build_max_tokens` · `LEGBA_REFERENCE_BUILD_MAX_TOKENS` |
| tool calls | `tool_call_cap` |

At 70% of whichever comes first the loop stops offering tools, sends `tool_choice: none`, and asks for the reference
in one final turn: commit now from the pages you have, omitting anything you cannot anchor to a fetched page. Past
100% of the wall clock the loop stops whether or not anything was committed. Two further triggers fire early: **two
consecutive searches that return nothing** record `search_degraded` and go straight to the commit turn, because a
third would not answer either; and a **search-to-fetch ratio guard** fires on the shape of the whole build once
enough calls are spent. The raw result count is what the dead-search test reads, never the filtered list: a query
that found thirty items and had all thirty dropped for being off the tier allowlist is a strict build, not a dead
search plane, and the budget-pressure tier-3 opening handles that case. A turn that parses as the reference object
enters the commit turn on its own, so a model that writes the answer without the closing phrase is not charged for
more rounds.

**Sizing, not resuming.** A resumable build would need a per-target state row, a way to rehydrate the conversation,
and several ticks to finish one reference, while still holding the actor's turn each time and — because each
resumption re-sends the accumulated context — costing *more* tokens. Sizing is one number and no new state. The
wall-clock cap sits above what a good build needs and below both the cadence cooldown and the tick period, so a
build can never still be running when its next reminder fires, and a wiring test asserts each of those relations.

## The manifest, and the carried notes

After every successful fetch the **loop** — not the model — records the page: url, outlet, title, the page's own
machine-readable date, whether that date is inside the window, the archive digest, and the first
`MANIFEST_EXCERPT_CHARS` of the archived text. No model cooperation is involved and none is possible. The commit
turn is rendered from that record: in-window pages first, each with the text the model may quote; out-of-window and
undated pages named afterwards with the verdict printed, so an omission never reads as a lost page. A span copied
from an excerpt is a prefix of the archived text, so it verifies by construction.

A failed build keeps the model's own note lines **and the loop's own page lines** under
`<LEGBA_ARCHIVE_ROOT>/reference_notes/<target_id>.json`, and the next build for that target starts from them — the
page lines are why a failed build carries something even when the model wrote no notes. They are **leads, not
evidence**: every URL, date and span is re-fetched and re-checked by the same fences, the model is told so in terms,
and a URL appearing only in a model note is **not** admitted for fetching, because that is a claim about a page and
admitting it would let a hallucinated URL survive by being written down once. The carry is bounded three ways — the
newest whole note lines that fit the budget, dropped after a day, one file per target, replaced not appended — and a
build that commits clears it, so the next window is never seeded from the last one's leads. This is deliberately not
a resume: no conversation, no tool state, no partial reference.

A URL the model asks for is **repaired** to the result it was shown whenever the loose key (host without `www.`,
plus path) matches, and a fetch of a URL **no search offered** is refused for free, naming back the URLs actually on
offer for that host. Queries are rewritten before the pack sees them, with the reason returned, because
pre-filtering an already filtered search narrows it onto hosts the allowlist has discarded.

## The three failure outcomes, and why they are different diagnoses

* **`no_commit`** — the build produced nothing to fence, whether the reply was unparseable or a parseable object
  with no developments. Look at the search plane, the budget and the loop. It carries no rejection table, and the
  receipt line names what stopped the build.
* **`all_rejected`** — the model *found* things and every one failed a fence. Look at the fences, or at a window
  with nothing citable in it. Reachable only with at least one committed development, and it carries the rejection
  table.
* **`empty_commit_with_material`** — an empty commit while the loop's own manifest held at least one in-window page
  with usable text, rendered into the commit turn, quotable and dated. The plane answered, the pages were read, the
  fences were never reached: that is a model failure and the receipt says so, rather than sending its reader to the
  search plane.

All three are failed attempts: nothing written, notes kept, the target yields the hour. All three are counted on the
receipt, because a lane that builds nothing twice running is a different fact from one that builds nothing once.
Every row also carries what the loop **read** — pages fetched, pages in window, searches, fetches, queries rewritten
— which is what actually decides yield.

## Cost, and the egress it is allowed

The core plane, always and only. There is **no paid route in this analyst's path** to gate, which is why — unlike
the grader — it carries no spend-ceiling environment variable: the ceiling is the architecture. Tokens in, tokens
out and wall time are recorded on every reference's own `ref_json` header, so the cost of a reference travels with
the reference.

Search is the one place money could leak, and it is fenced by the grant rather than by this module. The analyst's
own `action_packs` entry carries a `governor_override` re-targeting the ledger account to
`web_access_reference_builder`, so this lane draws on its own hourly bucket instead of sharing one with the standing
auditor and the desk reference — a grant override is tightening-only, taking the more restrictive of every numeric
cap, and re-targeting the account is not a loosening. Search runs the pack's ladder: the free local rung first, the
paid rung only as a declared fallback under the pack's daily cost brake. A second registered pack would have worked
and was rejected: it duplicates the tool specs, the guarded fetch route and the prompt rules, and adds a lifecycle
to drift.

## The operator top-up

What the free lane misses is substance, not verification, and closing that costs money, so the decision stays the
operator's and nothing here takes it.

```bash
PYTHONPATH=src python3 scripts/reference_topup_packet.py --candidates   # 0. which countries most need it
PYTHONPATH=src python3 scripts/reference_topup_packet.py --target country_watch_il   # 1. write the packet
# 2. the operator runs their own lane over the packet, on whatever model they choose
# 3. merge the result back through the same fences
PYTHONPATH=src python3 scripts/load_unit_reference.py additions.json --merge-into <reference_id>
```

The packet lists the established developments as already established, with the per-dimension counts and the thin
set, so a paid lane spends its budget on the gap rather than re-deriving the skeleton. The merge is not a rubber
stamp: it re-fetches every cited URL through the platform's guarded transport and re-runs the date gate and the span
check against what those pages serve now, dropping and counting any addition that fails. A paid lane gets no easier
ride than the free one — the only way the merged reference stays one instrument rather than two — and it mints a
**new row** rather than mutating the skeleton, because a reference a published share was computed against must stay
readable as it was.

`--candidates` is step zero because a target flagged `unbuildable_by_lane` has a different shape of problem: there
is often no skeleton to difference against at all. For those the top-up lane runs from scratch and the result is
loaded with no `--merge-into`, and the script says so rather than telling the operator to build one first — what the
lane has already failed to do three times.

## The receipt, and how to read it

Every tick writes one receipt as an `analyst_outputs` row under `analyst_id = 'reference_builder'`, and that row is
also the attempt ledger the next tick reads.

```
Reference builder — built 1 reference(s), 2 verified development(s).
  t0=… window=14d cadence=7d roster=32 due=1 pipeline=2026-09-16/1
  plane=core builder=core-plane-lane paid_api=$0.00
  CHOSE country_watch_tw: oldest_attempt, last attempt … (all_rejected), 1 consecutive failure(s), never built
  - [built] country_watch_tw: 2 verified of 2 committed, rate=100.0%
      pages: 4 read, 3 in window; 22 searches / 5 fetches; 11 queries rewritten; 2 ratio nudge(s)
      THIN: economic_coercion, energy_security, internal_stability, leadership_transition
      27 tool calls, 343,790 in / 14,905 out, 285.4s
```

The header line says what the tick did and what it cost, always nothing here. The `CHOSE` line says which target was
taken and why, and `next` lines name the rest of the due queue with each one's reason — `never_attempted`,
`oldest_attempt`, or `retry_backoff` with the instant it returns and its strike count — so rotation is
distinguishable from starvation at a glance. The per-target block gives the outcome, the survival rate, what the
loop read, the thin dimensions and the build's own cost. Warnings carry what an operator can act on, such as
developments whose host has no reviewed licence class and whose page bodies were therefore not archived.

The same facts are structured on the row's `data.data`: `t0`, `roster_size`, `n_due`, `n_eligible`, `due_queue`,
`attempts`, `per_target`, `window_days`, `cadence_days`, `retry_backoff_hours`/`_source`, `unbuildable_by_lane`,
`n_references_written`, `n_no_commit`, `n_all_rejected`, `n_empty_commit_with_material`, `n_search_degraded`,
`n_forced_commit`, `n_url_repairs`, `n_manifest_pages`/`_in_window`, `cost`, `dry_run`, `enabled`,
`pipeline_version`, `warnings`, `caveat`.

## The table it writes

**One, and no new one.** `unit_references` was already the right shape and this lane is its only writer: one row is
one independent reference — a country, a window, and the developments a blind builder found. `thin_dimensions` and
`span_verified_rate` are used for exactly what their column comments say, and `span_verified_rate` is a real number
(verified over candidates) except on an empty denominator, where null is the honest value because zero would read as
"every span failed".

The writer lives in `_reference_store.py` and is shared with `scripts/load_unit_reference.py`. Two writers of one
row is how `thin_dimensions` comes to mean one thing when a person loads a file and another when the job writes one
— at which point the grader's thin contract quietly depends on *who* built the reference.

## Running it by hand

```bash
PYTHONPATH=src python3 scripts/reference_build_now.py   # the next target the cadence would take
PYTHONPATH=src python3 scripts/reference_build_now.py --target country_watch_il   # one named target
# build and fence it but do not write it; the reference travels on the receipt
PYTHONPATH=src python3 scripts/reference_build_now.py --target country_watch_il --dry-run --save /tmp/out.json
```

A build takes minutes of core-plane time, so the script's default timeout is half an hour. The forced run takes the
**same** path as the cadence — a PUT to the analyst actor's own `run` method — not a second entry point. `--target`
travels as `reference_targets` and `--as-of` as `as_of`, the **same key the correctness grader reads**, so an
operator pinning a fortnight passes one value to both jobs. A named target bypasses both the cadence and the retry
backoff, because naming one is a person saying build this now, and a lane answering "not for another nineteen hours"
would obey its own scheduler over the operator; its strike history still travels on the receipt.

Both knobs ride inside the body's `options` object, the one channel the actor merges into a sub-handler's option
mapping. A knob at the **top level** of the body is discarded silently, with no error and no log line. A wiring test
drives the script's own `build_body` and pins the envelope, so a drift is a red test rather than a run that quietly
used the defaults.

## The knobs

Values live in [TUNABLES](TUNABLES.md) §1 and §2. What each governs:

| knob | what it does |
|---|---|
| `LEGBA_REFERENCE_BUILDER_ENABLED` | off, the handler searches nothing, fetches nothing, calls no model and writes nothing |
| `LEGBA_REFERENCE_BUILD_MAX_SECONDS` / `_MAX_TOKENS` | the wall-clock and token walls on one build; the descriptor knobs win over these |
| `LEGBA_REFERENCE_RETRY_BACKOFF_HOURS` | how long a failed target stays ineligible; this wins over the descriptor knob, and zero disables the fence |
| `LEGBA_REFERENCE_TIER12_HOSTS` | hosts **added** to the tier 1–2 allowlist, never replacing it |
| `LEGBA_ARCHIVE_ROOT` | where page archives and a failed build's carried notes land; unwritable degrades to memory-only with a warning, and spans are still verified — they just cannot be re-verified after the process exits |

Descriptor options, through a registry PUT of `analyst_reference_builder.yaml`: `reference_targets`,
`max_targets_per_run`, `window_days`, `cadence_days`, `roster_window_days`, `tool_call_cap`, `page_chars_to_model`,
`min_developments` (the target the instruction states, and the threshold under which the loop pushes back on a
premature finish), `thin_below`, `build_max_seconds`, `build_max_tokens`, `retry_backoff_hours`, and the pack
`governor_override`. They live in the descriptor body, so the runtime sees a change only once the registry holds the
new content hash, which rolls the actor id; the sub-handler is compiled into the registry image, so that image is
rebuilt before the descriptor is registered.

## Seams

**Some countries are not buildable by this lane at all.** Where a country's record is largely unfetchable here,
builds commit nothing however well the loop behaves. The lane names those targets `unbuildable_by_lane` and keeps
retrying them at the backoff cadence; closing one is the operator top-up, and `--candidates` is the list. Until then
those targets have **no reference**, so the grader writes no number for them — the honest state, not a silent zero.
**Salience is the matching residual**: the fences make a reference *true*, not *complete*. A dimension can carry two
verified developments and still miss the fortnight's decisive event, nothing in-tree detects that, and it is
declared per dimension rather than hidden.

**Licence classes are still the operator's.** With no `license_class` recorded for a host, the archiver puts it at
teaser depth: the page is read and the span checked against it in memory, but the body is **not archived** and the
development carries `span_source: snippet`. Those developments are real and correctly fenced; what they are not is
re-verifiable later, and the receipt says so per build with the count. See [SEAMS](SEAMS.md).

**Some reachable hosts hang intermittently, and it reads as absence.** A read timeout with an empty message is
recorded by the blocklist as an ordinary host failure, and nothing downstream distinguishes it from the web having
nothing — even where the same URLs serve normally through the same guarded client minutes later. That belongs to the
fetcher rather than to this lane, and it is the largest measured residual on yield: a retry-once-with-backoff on a
timeout, and a counter separating *timed out* from *served a stub*, are what would close it.

**A thin window is still a thin window.** Every fence and repair above is about not wasting the budget; none
conjures an in-window page that does not exist. `no_commit` with zero pages in window is the honest reading of a
fortnight this lane could not date, where `empty_commit_with_material` says the fault was the model's.
