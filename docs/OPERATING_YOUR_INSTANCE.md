<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Operating your instance — the practice layer

[SETUP.md](SETUP.md) gets you booted. [RUNBOOK.md](RUNBOOK.md) is the mechanics:
which command, which container, which log line. This document is the layer above
both — the **practice** that makes an instance good rather than merely running.

The distinction matters because of an honest structural fact about this project:
**the code ships complete, and the code is not the whole system.** What you clone
is machinery plus a set of defaults that were measured somewhere else. A useful
instance is the machinery *plus* your seed data, *plus* your source mix measured
over weeks, *plus* thresholds re-derived on that mix, *plus* a data-quality habit
that keeps finding the things the receipts do not volunteer. None of that ships,
and most of it cannot: some of it is yours to curate, some of it is licensed such
that nobody may redistribute it, and some of it is simply time.

So this page teaches method, not results. Every number quoted here is an example
of *how a number was arrived at*, never a value you should adopt without
repeating the measurement.

**Contents:**
[0 What you inherit](#0-what-you-inherit-and-what-you-have-to-build) ·
[1 Seeding your world](#1-seeding-your-world) ·
[2 Curating your corpus](#2-curating-your-corpus) ·
[3 Measurement practice](#3-measurement-practice--inherited-constants-are-defaults-not-truths) ·
[4 The data-quality practice](#4-the-data-quality-practice) ·
[5 Gate governance](#5-gate-governance) ·
[6 Expectations timeline](#6-expectations-timeline)

---

## 0. What you inherit, and what you have to build

Three categories, and it is worth being blunt about which is which.

**Ships and works immediately.** The substrate and control plane, the descriptor
registry and lifecycle FSM, the acquisition handlers, the cite→verify→audit
spine, the receipt chain, the migrations, the deploy path, the UI. Nothing
important is stubbed; where something is deliberately not built it is a declared
seam that fails loudly ([SEAMS.md](SEAMS.md)).

**Fills on its own, given time and a working model plane.** Signals, entity
graph, vectors, corpus index, findings and their supersession history, decay
states, contention rows, narratives. You do not have to do anything for these
except keep the pipeline alive and wait. Some have hard floors — a desk baseline
needs 28 days of history *by construction*, band calibration resolves at 14- and
28-day horizons — and no amount of effort compresses them.

**Only accumulates if you practise it.** Seed data. Corpus curation. Threshold
re-measurement. Gold labels. Source triage. Entity-merge hygiene. Activation
judgment. This document is about that third category, because it is the only one
where reading the runbook is not enough.

A note on the inversion, since it is easy to read the above as discouraging: a
fresh instance has real advantages over a long-running one. No legacy data
defects from code that has since been fixed. Free choice of models, unconstrained
by prompts tuned against one family. A domain corpus of your own choosing —
the machinery is domain-agnostic (desks are descriptors; a desk need not be a
country), and a **narrower** domain converges faster, has a smaller
source-health surface to keep alive, and has a tighter distribution that is
*easier* to measure than a global news mix.

---

## 1. Seeding your world

### 1.1 The seed layer is not the source layer

A **source** handler writes raw `signals`. A **seed** adapter writes to the
knowledge layer — `facts`, `nexuses`, `entity_profiles` — with real `valid_from`
temporality and idempotent upserts. They are different planes with different
failure modes, and the seed plane is the one that is empty on a fresh clone.

That emptiness has a specific functional cost, and it is worth understanding
before you decide seeding is optional. Structured (Tier-1) knowledge grounding
injects current officeholder / alliance / conflict facts into analyst prompts.
It exists to stop a model asserting a head of state it learned during training
and never updated. With no seeded facts there is nothing to inject, the units
run un-grounded — degrade-not-drop, by design — and **the failure mode the
grounding layer exists to prevent comes back, silently**. Nothing errors. You
just get confident staleness.

### 1.2 The `.example.yaml` contracts

`seeds/` ships the machinery and the format examples, never data:

- `seeds/world_baseline.example.yaml` — the leaders / alliances / conflicts
  contract, with inline field comments. This is the file you copy and fill.
- `seeds/source_ratings.example.yaml` — the source-assurance rubric contract.
  **The example sources in it are fabricated placeholders**, deliberately, so
  nobody mistakes them for a recommendation.
- `seeds/README.md` — the adapter table and the import commands.

The adapters that read curated YAML **degrade gracefully**: a missing file logs
a warning and returns zero rows rather than crashing, so `deploy/deploy.sh
--seed` on a fresh checkout is a clean no-op. That is convenient and it is also
a trap — a silent no-op looks identical to a successful seed if you do not check
the row counts afterwards. Always dry-run first, and always verify:

```bash
python scripts/seed.py --list                            # what adapters exist
python scripts/seed.py --source world_baseline --dry-run # parse + map, no writes
python scripts/seed.py --source world_baseline           # for real
```

Then confirm the write actually happened (`facts` / `nexuses` with
`source_type='seed'`, and a `seed_batches` ledger row).

Two shapes matter when you author a baseline. Leader rows produce **both** a
subject-is-the-leader fact and a country-subject office fact; the second is the
supersession-correct shape the grounding injection reads, so a new officeholder
closes the prior row cleanly and distinct offices (head of state vs head of
government) coexist. Conflict rows should use the `sides:` grouping rather than
a flat belligerent list — sides generate hostile edges *across* coalitions and
allied edges *within* them, which is the graph shape downstream analysis expects.

One adapter needs no curated file at all: `wikidata_leaders` pulls current
officeholders from live Wikidata over the network. It is the cheapest way to
bootstrap a real leader baseline on day one, and it comes with the caveat in the
next section.

### 1.3 Diagnose before you seed — always

**Live upstream data can be vandalized, and a seed import writes straight into
your knowledge layer.** A public SPARQL endpoint will happily serve you a head
of state who held office for four minutes because somebody edited the page. If
that lands, it supersedes your correct fact, and every grounded prompt from then
on carries it as current truth.

The shipped example of the discipline is `scripts/diagnose_stale_leaders.py`. It
is **read-only by construction** — every database statement is a `SELECT`, the
"fresh" side runs the adapter's `fetch` + `map` in memory and never touches the
write path. It prints exactly the set of open officeholder facts that a re-seed
would supersede, i.e. the delta, and nothing else.

The practice generalizes to any adapter that pulls from a live upstream:

1. Run the read-only delta preview.
2. **Read it with your own eyes.** Not the count — the rows. A delta of 40 rows
   after an election is normal; a delta of 3 rows where one is a person you have
   never heard of taking office yesterday is the case the diagnostic exists for.
3. Only then apply. If a row is genuinely ambiguous, leave it for a human call
   rather than letting the import decide.

This has caught real vandalism on the reference deployment. It costs thirty
seconds and it is the difference between a knowledge base you can ground on and
one you cannot.

If you write your own adapter against a live upstream, write its diagnostic at
the same time. A seed adapter without a read-only preview is a loaded gun.

### 1.4 Source-catalog strategy: start small, measure, then widen

Descriptors ship deliberately conservative: a small active working set, and a
much larger set registered `draft`. The headers say so verbatim — bulk
registration creates no live actor; activation is the operator's, after
verifying the route live.

**The `draft → configured → active` FSM is a deliberate gate, not friction.**
The reason is that "this feed returns HTTP 200" and "this feed is worth
ingesting" are unrelated propositions, and only the second one matters. Between
them sit: routes that serve a frozen snapshot behind a live-looking
`lastBuildDate`; feeds that return 200 with zero items indefinitely; aggregator
routes whose upstream quietly stopped; feeds whose content is real but is a
recurring digest that will corroborate itself into false confidence.

A workable widening cadence:

1. **Activate a small set** — enough to prove the pipeline end to end. Prefer
   sources whose subject matter your desks actually scope to; an active source
   nothing reads is pure cost.
2. **Let it run for days, not hours.** Poll health is a *trend*. You cannot
   detect "this feed froze" without a baseline of what it looked like unfrozen.
3. **Read the poll-outcome history before widening** (§4.1). Retire the liars
   rather than leaving them active-and-silent — a retired source is honest, an
   active source producing nothing is a lie your own dashboard tells you.
4. **Widen in batches you can evaluate**, and repeat. Ten sources you have
   triaged beat sixty you have not.

Two mechanical notes. Geocoding refuses to construct a public-Nominatim backend
unless `LEGBA_GEOCODER_CONTACT_EMAIL` is a real address, per the OSM terms of
use — it fails loudly at activation rather than degrading, which is correct and
surprising the first time. And sources that need credentials stay `draft` until
the vault is loaded; that is the FSM doing its job.

---

## 2. Curating your corpus

The vector corpus is the second grounding tier: free-text background retrieved
by similarity and injected as **prior**, never as evidence. It is optional, it
is genuinely useful, and it is the part of the system where a careless operator
does the most damage.

### 2.1 Operational-only discipline

**Corpus bytes never enter the repository and never enter an image.** The loader
code ships; the data does not. Keep your corpus in a working directory that git
ignores, load it into the instance, and let the images carry code only.

This is not fastidiousness. Most documents worth having in an analytic corpus
are *publicly readable* and *not redistributable* — a distinction that is easy
to elide and expensive to get wrong. If your corpus lives in your repo and your
repo is public, you have redistributed it, whatever you intended.

### 2.2 The acquisition-list pattern

Before you download anything, write the list. One row per candidate document,
with a **license verdict decided in advance** — not after the file is already on
disk and already chunked.

A verdict legend that has held up in practice, with the two columns that
actually matter:

| Verdict | Meaning | May you ingest? | May you redistribute? |
|---|---|---|---|
| **public-domain** | Government work with no copyright (e.g. US federal works under 17 U.S.C. §105) | yes | yes |
| **open-license** | Explicit open licence — Crown copyright under OGL, Creative Commons, etc. | yes | yes, with attribution + licence notice |
| **treaty / UN-official** | Treaty text and UN-authored official text, freely reproducible | yes | yes, attributing the issuing body |
| **releasable-not-open** | Publicly released by the owner but never placed under an open licence; the owner retains rights | yes — **operational only** | **no**; summarize-only in any shared artifact |
| **copyrighted-summarize-only** | Commercially published / third-party copyright | **no** — do not ingest full text | no; quotes and summaries under fair use only |
| **leaked / license-none** | No lawful licence exists | operator decision, default **out** | **never** |

Three things this legend buys you:

- **It decides the redistribution question once**, at acquisition time, rather
  than at the worst possible moment later. `releasable-not-open` is the class
  that catches people out: publicly reachable is not public domain, and a
  document marked "releasable" by its owner is still the owner's.
- **It makes "do not ingest" a real outcome.** Some documents belong on the list
  only so that a future you knows they were considered and rejected, and why.
- **It survives you.** A per-document verdict with the source URL and the date
  you checked is a record; a memory of having thought about it is not.

Record, per row: the document, its canonical URL, a working retrieval URL if the
primary is bot-blocked, the verdict, and any caveat (edition matters — an older
edition of the same publication can carry a distribution restriction the current
one does not).

### 2.3 Per-chunk provenance is mandatory

Every chunk you load carries, at minimum: `license`, `source_url`,
`effective_date`, `corpus`, `doc_id`. No exceptions, including for documents you
are certain are public domain.

Two reasons. First, the license posture is a property of the *chunk* at
retrieval time — the moment a retrieved passage is rendered into a prompt, the
question "may this text leave this host in an artifact?" has to be answerable
from the chunk itself. Second, the effective date is what lets you reason about
staleness later; doctrine and factbook material ages, and an undated chunk ages
invisibly.

### 2.4 The non-citable-prior rule, and why

**Curated facts cite. Vector text informs.** Retrieved corpus text is rendered
into the prompt as a fenced background block with no citation ids, and the
verify pass will not accept it as support for anything.

The honest reason is a failure mode that was observed, not a theoretical one:
**an uncited prior leaking into cited analysis.** A model reads the fenced
background, finds a proposition it likes, and asserts it in the finding as
though it came from the evidence — with no `[N]` marker, because there is no
citation to give. The finding then reads as confident and grounded and its
support is a paragraph of background the verify pass never saw as a claim. On
the reference deployment, turning retrieval on measurably **thickened the
low-faithfulness tail** even with the non-citable header in place.

What follows from that, in practice:

- Keep the two tiers architecturally separate. Structured facts are citable
  substrate; corpus text is a prior. Do not let a convenience wrapper blur them.
- **Roll it out per-unit, with a control.** Enable retrieval for one analysis
  unit at a time, keep at least one comparable unit deliberately *without* it,
  and compare. `scripts/rag_watch.py` is the shipped example: it reports, per
  unit, before-vs-after the flip — n, trailing-mean faithfulness, low-faith
  rate, tokens per run, latency — against a **pre-registered** rollback rule.
- **Arm an automatic guard, not a good intention.** The shipped guard
  re-evaluates every run and suppresses injection on a faithfulness drop, a
  low-faith-ratio rise, or a token-cost rise past a bound. Persist its state
  somewhere that survives a container recreate.
- Watch the *tail*, not the mean. A retrieval change that lifts the average and
  fattens the bottom decile has made your instance worse.

### 2.5 Your corpus needs its own retrieval floor

The relevance floor that ships was calibrated against one corpus, on one
embedder, by probing on-target and off-target queries and reading where the
score distributions separated. On a different corpus with a different embedder
it is a guess.

Re-derive it the same way: take a dozen queries you know should hit, a dozen you
know should not, record the best-chunk score for each, and put the floor in the
gap. If there is no gap, the problem is your chunking or your embedder, and no
floor will fix it. A floor set too high filters everything — which looks exactly
like "retrieval isn't helping" and is actually "retrieval never fired."

---

## 3. Measurement practice — inherited constants are defaults, not truths

### 3.1 The honest statement

There are roughly forty numeric constants in this tree that are **conclusions
from measuring one instance's data**: vector similarity floors, entity-ubiquity
damping knees, fusion weights, junk-pattern length bounds, geocoder token
stop-sets, slice row caps, cadence and poll bounds, alert severity weights.

Their provenance ships — the comments are specific about what was measured and
when, and you should read them. **The measurement apparatus does not ship**, and
neither does the corpus it was run against. They transfer as reasonable priors.
Their validity on your source mix is unproven, by definition, until you prove it.

Some are re-tunable from configuration (an env var, or a descriptor
`method.options` key). Some are module constants and require a patch. Before
assuming a knob does not exist, grep the constant name — not every env var a
handler reads is listed in `.env.example`, and the descriptor options surface is
larger than it looks.

Where a threshold is most likely to be wrong on your instance:

| Class | Why it moves | Symptom when it is wrong |
|---|---|---|
| Cosine floors | Different embedder, different text lengths, different corpus | Semantic plane is inert (too high) or matches everything (too low) |
| Entity-ubiquity damping | Different entity distribution — a domain corpus has different hubs than a global news mix | Your busiest entity dominates every match, or nothing is ever damped |
| Junk gates / stop-sets | Different source formats produce different junk | Junk profiles accumulate; or real entities get rejected |
| Geocoder stop-sets | Token collisions depend on your feeds' title conventions | Signals land in the wrong country, confidently |
| Slice caps and time windows | Depend on your per-desk volume | Evidence is silently dropped at the cap, or slices are mostly empty |
| Cadence and poll bounds | Depend on your tick geometry and feed sizes | Runs get eaten; polls truncate before their tail |

### 3.2 Recipe A — measure the distribution before you trust a floor

`scripts/measure_claim_watch_cosines.py` is the worked example, and its shape is
the point:

- It reads the **exact plane the runtime reads** — same vector collection, same
  stored vectors, same embedder resolved through the same registry and vault
  path, same similarity helper, same entity-linkage SQL imported directly from
  the handler rather than re-implemented. A measurement against a
  reimplementation measures the reimplementation.
- It is **read-only by construction**, and says so in its docstring, statement
  by statement.
- It reports a **distribution**, not a verdict: the score profile of pairs that
  should match against the profile of pairs that should not.

Then set the floor from the separation. On the reference instance this
measurement is *why* the vector floor moved: the prior floor was admitting only
a sliver of genuinely related pairs, so the semantic plane was effectively
switched off while appearing to be on. The published floor came out of roughly
fourteen thousand live pairs. **Your distribution is a different distribution.**

The generalizable rule: *before* you trust any threshold, plot what it is
thresholding. A floor with no measured distribution behind it is a preference.

### 3.3 Recipe B — the gold-label loop

This is the highest-value practice in the document and the one nothing automates.

1. **Sample stratified, never uniform.** A uniform sample of a skewed population
   is almost entirely the boring class. `scripts/sample_k4_match_worksheet.py`
   and `scripts/measure_entity_merge_quality.py` both ship as two-step CLIs
   (`sample` → worksheet → human labels → `score`) precisely because the
   interesting strata are rare. Draw enough of each class to say something about
   each class.
2. **The sampler never labels.** Every labeler column the worksheet writes is
   blank. A tool that pre-fills its own answer produces agreement, not truth.
3. **Label out of plane.** Use a *different model family* from your analytical
   plane — or a human — with live web verification where the claim is checkable.
   A same-model judge grading same-model prose shares its blind spots, and a
   gold set is the one place you cannot afford that.
4. **Stamp `labeled_by` on every label.** Note honestly what this is: recorded
   provenance, *not* a code-enforced invariant. The stamp tells a later reader
   which plane produced the verdict; nothing validates it. That makes
   out-of-plane labeling an **operational discipline**, and disciplines decay
   unless someone checks.
5. **Run a calibration pass first.** Label a small overlap set twice and check
   agreement on the decision-critical class before you trust the bulk run.
6. **Pre-declare the acceptance gate, in writing, before you measure.** "We will
   build the automatic closer if pairwise precision ≥ 0.85." Deciding the bar
   after seeing the number is not measurement.
7. **Honor a failed gate by not building the thing.** This is the whole
   discipline compressed into one line. On the reference instance a stratified
   out-of-plane labeling round put pooled pairwise precision at **0.279** against
   a pre-declared ≥0.85 bar; a second round after three measured tuning levers,
   at volume, came out **lower**. The dependent feature — an automatic
   question-closer — **remained unbuilt**, the edges stayed trace-only, and the
   published debt figure carries `match_verified: false` on the wire. Two
   further measured lever-trains later, round 4 cleared the bar (0.908 on the
   live gated stream) — and the closer is *still* unbuilt, because meeting the
   gate earns the *decision*, not the feature: arming a write-back loop is an
   operator call. The measurements were worth more than the feature would have
   been.

Also worth internalizing: **faithfulness cannot see a faithful-but-wrong claim.**
Verification checks a claim against its cited evidence, not against the world.
Only a correctness label does the latter, and correctness labels do not
accumulate on their own at any rate whatsoever. If you want a correctness figure,
you have to sit down every week and make one. A small weekly stratified cohort
is enough to be useful — one such cohort on the reference instance produced the
single most valuable finding in the loop's history (a specific *class* of
failure, appearing in a majority of that week's downgrades, which became a
deterministic backstop plus a prompt rule across every inline unit). Report the
sample size next to the number, always, and say "insufficient sample" when it is.

### 3.4 Recipe C — preflight before activation

Before flipping a desk active, measure what it will actually see.
`scripts/preflight_supply_chain_lanes.py` is the shipped pattern: it mirrors the
substrate slice reader clause for clause, read-only, and reports per candidate
desk how many rows the SQL pushdown admits inside the time window — against the
pre-filter cap that runs *before* any free-text predicate.

The design rule it enforces is worth stating plainly: **the SQL discriminator is
the selector; the free-text predicate is only a refiner.** A desk whose window
plus pushdown overshoots the cap does not error. It silently drops evidence, and
the finding it writes looks exactly as confident as one that saw everything.

The reference deployment sized several desks' time windows from this preflight —
at the standard window one lane would have blown past the cap and quietly lost
about half its evidence. That is a five-minute script preventing a class of
error that is invisible from the output.

### 3.5 Recipe D — guarded flips with pre-registered rollback

Any change that could plausibly degrade quality gets: a captured **before**
baseline, a **pre-registered** rollback rule written down before the flip, a
watch that re-evaluates every run rather than at boot, and a **one-line
rollback** you have actually rehearsed.

Two anti-patterns, both learned the expensive way:

- **Comparing the new thing to the old thing's scores proves nothing.** When the
  reference deployment repointed the faithfulness judge to a different model
  family, the score deltas across the swap were explicitly *not* treated as
  evidence about judge quality — the comparison that counts is against the gold
  labels, not against the previous judge's opinions.
- **A flip you cannot revert in one line is not a trial**, it is a migration.

### 3.6 Soak time is not optional, and it cannot be compressed

- **Desk baselines** carry a hard 28-day window. Below it they report
  `insufficient_history` and refuse to produce a band. On a real instance many
  desks stay short well past 28 calendar days, because the metric needs *active*
  days and quiet desks have few.
- **Band calibration** resolves per claim at 14- and 28-day horizons. Your first
  resolution is two weeks out; your second is a month out. Until then you have
  no calibration signal at all — not a weak one, none.
- **Source-health trend** needs weeks of poll history before "this looks wrong"
  means anything.
- **Track records** (which sources win contested facts) need contested facts to
  *resolve*, which needs source diversity plus months.

The temptation at week one is to conclude that the derived layers are broken and
start tuning them. They are not broken; they are honest. **Impatience here
produces confident nonsense** — a band computed from four days of history is a
number with a shape and no meaning. Let the floors do their job, and note that
the components that refuse to emit below a data floor (the scorecard refusing to
fabricate a band, the specificity damping reporting itself inert, the forecast
abstaining on degenerate windows) are working *correctly* when they say nothing.

---

## 4. The data-quality practice

The single most useful habit: **assume the middle of the pipeline is lying to
you, and go check.** The top of a system can measure entirely healthy — verified
findings, clean receipts, reminders firing — while the middle quietly does
something else. On the reference deployment a systematic sweep of a
healthy-looking pipeline turned up fourteen distinct defects in one night, and
the published account of them ([CHANGELOG.md](../CHANGELOG.md), 2026-07-31) is
deliberately unflattering because that is the useful version.

Run the sweep on a cadence — monthly is reasonable; after any significant
release is mandatory. It is read-only and it is cheap.

### 4.1 The seven-point sweep

**Rule zero: every claim carries evidence.** A row id, a count with the query
that produced it, or a `file:line`. "Sources look fine" is not a finding. Do the
whole pass read-only — `SELECT` and logs — and only then decide what to fix.

**1. Sources and poll health.**
Per active source: when did it last actually *produce* something, how do its
recent poll outcomes read, is it truncating?

```sql
-- staleness truth: first-insert time, NOT last-success time
SELECT source_id, max(fetched_at) AS newest_signal, count(*) AS n
  FROM signals GROUP BY source_id ORDER BY newest_signal;
```

The methodology point is load-bearing: **a "success" outcome is not evidence of
freshness.** A productive-poll outcome fires when a poll writes a signal *or
collapses an intra-source duplicate*, so a feed re-serving the same items
forever logs healthy successes indefinitely. Use the newest signal a source
actually wrote. Cross-check `source_poll_outcomes` for the `capped` flag (the
poll hit its budget and stopped early — everything after the cut is invisible)
and for `newest_entry_ts`, which discriminates *the feed is quiet* from *our
cursor is broken*.

**2. Payload completeness, by handler kind.**
Take a real row from each source kind and read its payload. Ask: is the text
where the corpus indexer looks for it? Structured feeds are the usual offender —
prose nested where no text path looks (tens of thousands of alert rows on the
reference instance carried full bulletins that nothing ever read), and chat-style
sources whose message text was absent from the corpus field ladder, making the
overwhelming majority of that source's content unsearchable while every count
looked fine.

**3. Enrichment spot-checks on real rows.**
Not fixtures — live rows. Sample a handful per kind and check language, geo,
entities. Geo is where confident errors hide: a body-text country mention
outranking the actual subject, an ISO token colliding with a common word (`AL`
for Albania in a US state abbreviation, `PA` for Panama, `PR` sending a headline
to Puerto Rico). Each such collision, once found, belongs in the stop-set *and*
in a regression fixture.

**4. Fact and entity extraction quality, plus merge hygiene.**
Read fifteen recent facts as prose and ask whether each is *true of the sentence
it came from*. This is the check that catches the worst class of defect, because
a badly-paired triple reads perfectly fluently. Then: are duplicate entity
clusters being adjudicated, or accumulating? Is the classifier defaulting
sensibly, or silently forcing one class and thereby blocking same-class merges?
Are junk entities being gated at write time, or being written and cleaned later?

**5. Cadence delivery — a reminder that EXISTS is not a reminder that FIRES.**
Compare each analyst's *actual* last run against its declared cadence:

```sql
SELECT analyst_id, max(created_at) AS last_run
  FROM analyst_outputs GROUP BY analyst_id ORDER BY last_run;
```

A registered reminder can look perfectly healthy while runs are being eaten
downstream. The reference instance shipped a cooldown anchored on run *end*, so
any slow run pushed the next cooldown past its own tick and the run dropped as a
no-op — ten analysts stale, every reminder green. Anchor cooldowns on run start,
and make a missed cadence log loudly as a missed cadence.

**6. Last-actual-run receipts.**
For each capability, read the receipt of its most recent run and check that it
records what it claims: model, status, duration, tokens, tool calls, and the
per-lever counters (§5.2). A receipt that omits a lever cannot tell you the lever
ran.

**7. Container-log error sweep, classified.**
Walk each container's recent log and bucket every distinct error into
**transient** (retried, self-healed, no action) or **action** (a real defect with
an owner). The classification is the work; an unclassified error list is noise
that trains you to ignore logs.

### 4.2 "Wired ≠ running" — count invocations before believing anything

The recurring lesson, stated as strongly as it deserves: **a capability that is
registered, granted, wired, and tested can still have executed zero times.**

The reference deployment shipped a handler whose semantic near-duplicate tier
queried a vector collection that did not exist — inside a silent best-effort
guard, from inception. It dispatched forever, caught its own failure, and
returned nothing. Every surface above it looked healthy. **An invocation count
would have caught it on day one.**

So, periodically, for every capability that has a ledger:

```sql
SELECT pack_id, count(*) FROM action_pack_invocations GROUP BY pack_id;
```

Zero is a finding. Investigate every zero, and be suspicious of any
best-effort `except` that swallows a failure without surfacing it in the run
receipt. This class of defect has appeared repeatedly and in more than one
guise — including configuration knobs that were documented, plumbed, and inert
because the schema silently rejected the field. If a knob's effect is not
visible in a counter, assume it is not happening.

### 4.3 Source triage vocabulary

Give your source states names, and use them consistently:

- **Dead** — no new content for a meaningful interval, regardless of what the
  polls say. Retire it. An active source producing nothing corrupts every
  coverage number you have.
- **Degraded** — erroring or rate-limited, but still delivering. Keep it and
  note the error rate; a feed running at a high 429 rate while still landing
  signals is often worth more than a clean feed nobody reads.
- **Upstream-frozen** — the hardest class and the reason this vocabulary exists.
  The endpoint returns 200, the feed metadata carries a *current* build
  timestamp, and the items behind it have not changed in weeks. Nothing in the
  poll outcome distinguishes this from healthy. Only comparing item identity or
  newest-entry timestamps over time catches it, and aggregator routes are
  especially prone.
- **Never-started** — created, active, zero signals ever. Usually an
  authentication or route path that was never completed. Retire rather than
  leave it looking live.

An empty-streak threshold helps: after N consecutive clean-200-but-zero-item
polls in a window, flag the source. The shipped default exists because a feed
sat returning 200-with-nothing for fifteen days while every poll-outcome row
logged `health_state='healthy'` — functionally dead, cosmetically fine.

### 4.4 Junk gates and entity hygiene are recurring, not one-shot

The junk gate is a living artifact. Its length bounds, its regex families, and
its exclusion lists are *measured* against the junk your sources actually
produce — markdown residue, widget chrome, quantity phrases, currency and time
ranges, clock fragments. New sources produce new junk classes; the gate does not
learn on its own.

So: audit it periodically. Sample recently created entity profiles, read them,
and ask which are not entities. Add the class to the gate, add a fixture, and
clean the backlog through a migration rather than an ad-hoc `DELETE` — bulk data
changes belong in the migration ledger where they are reviewable and repeatable.

Same for merges. Duplicate clusters accumulate continuously; adjudicated
same/not-same verdicts are what stops them regenerating, and those verdicts are
*decided*, not derived. Rank candidates by hub degree so the busiest duplicates
get adjudicated first — the long tail matters much less than the entity that
appears in everything. And measure the merge quality itself on a labeled sample
(`scripts/measure_entity_merge_quality.py`), with the rule that a recall
improvement must not move precision.

---

## 5. Gate governance

### 5.1 Verify-floor semantics

The floor is a fold, not a filter:

```
effective_confidence = min(confidence, faithfulness_overall_score)
```

Read that carefully. A model's self-asserted confidence **can only be lowered**
by the graded faithfulness of what it wrote. It can never be raised. A finding
that claims 0.9 and grades 0.3 is a 0.3 finding everywhere downstream.

Three properties worth preserving if you modify anything here:

- **Demote, never delete.** Every calibration guard that has been added to the
  pass adjusts the folded confidence or flags the row. Nothing removes a
  finding. A below-floor claim stays readable, stays attributable, and becomes a
  standing question rather than disappearing — which is also what makes the
  floor auditable after the fact.
- **Degrade to the floor, never to a number you made up.** When the LLM judge is
  disabled or unreachable, the result degrades to the deterministic
  citation-presence floor and is labelled unavailable. It does not emit a
  plausible score. An unavailable judge that silently returns 0.7 is worse than
  no judge.
- **Exemptions are narrow and named.** BLUF summary lines, honest
  corpus-scoped *absence* findings, and synthesis claims are exempt because
  crushing them to zero mis-scores honest work. Note the shape: each exemption
  is a named claim kind with a rubric, not a general "be lenient" adjustment. If
  you add one, add it the same way.

Set your floor deliberately and pin it in configuration. This project shipped
the footgun worth naming: for months the code default was `0.0` while the real
value was pinned in the reference deployment's environment, so a fresh install
silently ran an ungated composition. It was raised to `0.50` on 2026-08-15 —
`meta_findings_synthesizer.DEFAULT_VERIFY_FLOOR`, still overridable both ways
via `LEGBA_COMPOSITION_VERIFY_FLOOR`. Check your own gates for the same shape,
and note that if a constant appears in more than one place, they will drift.

### 5.2 Receipts culture

**A gate that cannot show its counts is not governed.** Every lever — every
filter, every damping rule, every cap, every exclusion — emits a per-run
counter into the run receipt: how many candidates it saw, how many it admitted,
how many it dropped and under which rule.

This is what makes the difference between "we tuned the matcher" and "on this
run the exclusion rule saw *this many* candidates, dropped *this many*, and the
cap accounted for *this many* of them." Without counters you cannot tell an
effective lever from an inert one, and inert levers are the norm (§4.2). With
them, a sweep becomes a reading exercise rather than an investigation.

Extend it to the model plane: per-run records of model, status, duration,
tokens, and prompt hash mean that "the analyst ran" and "the analyst called the
model" are separately checkable claims.

### 5.3 Never flip a gate without the measurement that justifies it

Concretely:

- **A threshold change is a measurement, not an edit.** Measure the
  distribution, change the number, record what you measured and when, and expect
  the next operator to re-measure.
- **A prompt change is a new version with a new measurement, never an edit to
  the old one.** Stamp the prompt version on every record it produces so that
  scores from two prompt generations are never silently pooled. Few-shot
  exemplars in particular are *data* — a shipped exemplar set drawn from someone
  else's signals is a reasonable prior and its measured precision does not
  transfer to you.
- **Never pool a gold set across configurations.** Labels are about the
  configuration that produced the rows.
- **Write the acceptance gate down before you look at the number** (§3.3).

### 5.4 Degrade loudly; declare seams instead of stubbing

The project's rule is that a capability is either built, or a **declared seam
that fails loudly** — never a silent stub that returns plausible emptiness. Keep
that rule in anything you add.

The reason is everything in §4.2: a silent stub and a working feature are
indistinguishable from the outside, and the difference only surfaces when
somebody trusts the output. A service that refuses to start half-configured is
better than one that serves 503s you have learned to ignore, which is in turn
far better than one that returns `[]`.

Practical form: when a dependency is missing, either fail at activation with a
typed error naming what is missing, or pass through explicitly un-enriched and
flip a health state. Both are honest. `except Exception: pass` is not.

### 5.5 Publish your own weaknesses

The reference deployment keeps a truth-in-labeling page whose first section is
titled "Where it is weak today (read this first)" and which volunteers, without
being asked, that the judge shares a model family with the writer, that a
matcher's measured precision is poor against its own recommended bar, that a
forecast pilot has no proven skill, that an optimizer's measured delta is
negative so it promotes nothing, and that a gold set is tiny.

Do the same for your instance. Not as a gesture — as an operating tool. A
written list of what you do not trust is the thing that stops you, six months
later, from trusting it by default.

---

## 6. Expectations timeline

What actually happens, when, and which column it lives in. "Practice" means it
does not happen unless a person does it.

| Horizon | Accumulates on its own (time + a live pipeline) | Only from operator practice |
|---|---|---|
| **Day 0** | Stack boots; control plane, registry, receipts, migrations all live. With no model endpoints: feeds ingest, dedupe, language-detect, BM25-index — **and produce zero analytic output**. | Provide the model endpoints. Load the vault. Author or bootstrap a seed baseline (§1). Activate a small, deliberate source set. Set the geocoder contact. Decide your verify floor. |
| **Week 1** | Thousands of signals; entity graph and vectors following; first findings within hours of the LLM plane working; first critiques and receipts; poll-outcome history starting. Derived layers correctly report insufficient data — that is not a fault. | First poll-health read (§4.1) and the first retirements. First payload/enrichment spot-checks. Corpus acquisition list drafted (§2.2) before any download. Start the cosine/threshold measurements (§3.2) — you now have data to measure. |
| **Month 1** | Supersession history worth reading; contention rows as competing claims appear; decay states; narrative structure; evidence archive filling behind verified findings; **first band-calibration resolutions at the 14-day horizon**; desk baselines becoming eligible at 28 days (many desks still short — expected). | 2–4 weekly gold cohorts labeled out-of-plane (§3.3). First re-measured thresholds committed with their provenance. First full seven-point sweep (§4.1) — expect it to find real defects. Corpus loaded with per-chunk provenance; retrieval enabled on **one** unit with a control and an armed guard (§2.4). |
| **Quarter** | Baselines mostly populated and meaningful; calibration resolving at both horizons; source-health trends long enough to detect a freeze; track records beginning to separate sources by earned reliability; enough standing questions for the coherence plane to have something to bear on. | Thresholds re-measured on *your* mix rather than inherited. A gold set large enough to gate a decision — and at least one feature you declined to build because its gate failed. Entity-merge hygiene as routine. Sweep protocol established, with prior sweeps defining what normal looks like. Your own published weakness list (§5.5). |

Two honest notes on that table.

**The right-hand column is the whole difference.** The left column arrives
whether you are paying attention or not. The right column is what separates an
instance that produces output from an instance whose output you can defend, and
it is why "how long until it's good" has no answer in wall-clock time alone.

**Nothing in the left column can be hurried, and trying to hurry it is the
characteristic new-operator error.** A baseline needs 28 days because it needs 28
days. The components that refuse to emit below their floors are the ones
protecting you.

---

## The loop

Strip everything above down and one thing remains: **measure → find a defect →
fix it → record why → re-measure.** The scripts are examples of it, the sweep is
a scheduled instance of it, the gold-label loop is its strictest form, and the
gate governance rules exist to keep it honest.

That loop is not in the repository and cannot be. It is the part you supply.

**See also:** [SETUP.md](SETUP.md) (bootstrap) ·
[RUNBOOK.md](RUNBOOK.md) (day-2 mechanics) ·
[DATA_SOURCES.md](DATA_SOURCES.md) (source kinds, adding feeds) ·
[ANALYSIS.md](ANALYSIS.md) (verify pass, compositions, evals) ·
[STATUS.md](STATUS.md) (what is built, gated, or only designed) ·
[SEAMS.md](SEAMS.md) (the declared not-built registry) ·
[MANUAL_INGEST_FORMAT.md](MANUAL_INGEST_FORMAT.md) (hand-loading facts and chunks)
