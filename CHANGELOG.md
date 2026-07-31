# Changelog

Legba's public history is intentionally squashed — each release lands as a single commit
on `main` — so this file is the release record. Entries are dated (newest first) and
written against the docs as they shipped; [docs/STATUS.md](docs/STATUS.md) remains the
always-current truth-in-labeling table.

## 2026-07-31

**The sweep and its repairs.** A seven-agent data-quality audit of the full pipeline —
sources, raw payloads, enrichment, facts/entities, cadences, container logs — followed the
2026-07-30 release, and what it found was repaired the same night. The top of the system
measured healthy (verified findings, receipts, archiving, the reminder plane); the middle
did not. The defect list is unflattering and is published as found:

- **Fact triple pairing was unsound.** The relation extractor returns real
  subject/object pairs; an upstream flattening step discarded them and triples were
  re-paired by list position, producing confidently-worded nonsense (a 15-fact
  spot-check passed 2). The extractor's own pairs now flow through end-to-end, legacy
  payloads are corroborated within a single sentence or refused (refusals are counted),
  confidence promotion now requires corroboration from **distinct sources** (repeats of
  a recurring digest no longer count), and every fact quotes the sentence it was read
  from. The pre-fix relational-fact family is queued for an operator-gated soft-close.
- **Semantic near-duplicate detection had never run.** The handler queried a vector
  collection that does not exist, inside a silent best-effort guard, since inception.
  One corrected default + a drift guard pinning it to the embedder's collection + the
  failure path now surfaces in the run receipt.
- **Scheduled runs were being silently eaten.** The cadence cooldown anchored on run
  *end*, so any slow run pushed the cooldown past the next tick and the run dropped as
  a no-op — with a perfectly healthy reminder. Ten analysts were stale this way,
  including the journal's noon leg two days running. The cooldown now anchors on run
  start (matching the trigger coalescer's existing semantics), and a missed cadence
  logs loudly as exactly that.
- **Geocode preferred incidental mentions over subjects.** The literal-text country
  sweep outranked recognized place entities, so multi-actor stories geocoded to
  whatever country appeared first anywhere in the body ("PR" even matched Puerto
  Rico). Candidates now rank by position (title, lead, then deep body), entity and
  text sweeps compete on offset, and the ISO-token stop-set covers common-word
  collisions. Six live mis-attributions became regression fixtures.
- **The entity classifier defaulted to person.** "White House"→person-class errors
  blocked same-class auto-merge across ~570 exact-key duplicate clusters
  (Zelensky ×9, Trump ×7). High-precision gazetteer/org/place signals now run before
  the fallback, merge candidates rank by hub degree so the busiest duplicates are
  adjudicated first, the trigram probe (previously dead config) is wired and bounded,
  and the junk gate learned quantities, currency, and time ranges.
- **Telegram was doubly muted.** The generic 30-second poll budget truncated the
  channel walk before its tail (the three newest channels had produced one signal
  ever), and message text was absent from the corpus field ladder so what did arrive
  was unsearchable. Polls now rotate through channels with a write-ahead resume
  pointer, handlers advertise their own poll bounds, chat text is a first-class corpus
  field, and the archiver no longer extracts widget chrome from t.me pages.
- **Structured feeds with prose were invisible.** 27k NWS alerts carry full bulletins
  nested where no text path looked; geojson features with real prose now flatten it
  and enter the text pipeline. Analyst receipts also gained what they always claimed
  to have: per-run LLM and tool call records (model, status, duration, tokens, prompt
  hash). Reminder GC stopped reporting phantom deletions, and two container images
  stopped stripping `numpy/testing`.

Nothing in the list above is a new capability. It is the difference between a pipeline
that runs and a pipeline whose middle layer does what its receipts imply.

**Evening train — the verify path learns its own scope, and compositions learn memory:**

- A two-panel, out-of-plane adjudication of both faithfulness judges (53 claims re-graded
  against their actual cited evidence) found **both judges safe on passes and trigger-happy
  on failures** — and the largest error drivers structural, not model-quality: citation-less
  findings auto-failing, scoped-absence claims judged against the citation subset instead of
  the retained input slice, metadata claims unjudgeable by construction. Six fixes shipped:
  non-propositional claim spans dropped; metadata claims verified **by lookup** against their
  own recorded values (mismatches now surface a previously invisible defect class — prose
  misquoting its own numbers); a hard contradiction now requires a verbatim quote of the
  evidence or demotes to soft; scoped negatives screen against the full input slice (a
  document-frequency filter drops non-discriminating terms, one bounded model call only on
  real collisions); citation-less grading is counted, and four producers that shipped
  citation-less findings now cite or carry their structural exemption honestly. Every
  critique now stamps a `judge_pipeline_version` so pre/post populations never pool — the
  expected upward shift in measured faithfulness is a **measurement correction**, and the
  adjudication protocol re-runs against the new stamp with a pre-declared acceptance gate.
- **Compositions and the world read now carry temporal continuity** — the previous verified
  read and a bounded register of open situations enter the evidence as ordinary citable
  blocks (the prior read deliberately stripped of its confidence and lineage so memory can
  never corroborate itself), with a prompt contract to state what changed, anchor time on
  the evidence's own dates, and name a first read as a first read.
- Extraction QA: consent-wall/JS-wall boilerplate is rejected at extraction (the deny-list
  was seeded from what was actually stored, with a length gate measured from the live
  corpus) and the historical pollution was purged from the archived-text layer.

**Quality wave 1 — tune the prompts UP, not down.** A full gallery of every assembled LLM
request (rendered through the deployed assembly code with live data, never reconstructed)
was read, annotated, and acted on — with an explicit design rule: optimize for output
quality, never token count; noise is bad because models reason worse over it, not because
it costs.

- **The analysis units now read real content.** What the gallery sampled as one dead
  citation was 12% of the slice pool: message-only signals rendered "(untitled)" with
  empty snippets, ~1,600 full archived articles hidden behind 100-char feed teasers,
  structured event records dumped as raw dicts, and untranslated bodies shown in scripts
  the model can't ground on. All fixed at the render layer — one shared body-precedence
  helper feeds both the analyst's working text and the judge's evidence text so they can
  never drift — and the per-clean counters land in every run's receipt. A live slice went
  from 23k tokens with 18 citable-nothing rows to 32k tokens of actual content with zero.
- **Units gained memory and context**: the same desk's previous verified read, a bounded
  open-situation register, the desk's measured baseline ("what's normal here"), and its
  standing open questions — all as ordinary citable blocks, the prior read deliberately
  stripped of confidence and lineage so memory can never corroborate itself, and gated so
  a unit with an empty evidence slice can never synthesize from memory alone. A per-unit
  slice-focus re-rank seam (order only, never a filter — the row set and lineage are
  byte-identical) ships inert for per-descriptor tuning.
- The input-token ceiling was raised deployment-side to match: richer bodies no longer
  trade away row coverage. The tracking exists as a feature, not a limitation.
- Composition hygiene: the contested-facts block is score-floored (it had been serving
  recency-ordered extraction noise to the world read), child compositions' citation
  markers are defused in parent-tier evidence, the evidence field carries only resolvable
  identifiers, and the lens-diff journal tier gained the empty-read retry its siblings had.
- The journal family's tool catalog is now derived from its actual grants (it had been
  advertising twelve generic tools, seven of them unusable, while its ten real instruments
  went formally undescribed), and the merge adjudicator gained a channel to report wrong
  upstream entity-class labels instead of silently reasoning past them.
- Consult: Anthropic prompt caching landed as a quality enabler (long multi-round consults
  re-read their context instead of re-billing it), and the final-answer contract moved
  from JSON-wrapped markdown to a sentinel format — removing the failure class where a
  token-capped long answer truncated mid-string, failed to parse, and burned rounds
  regenerating itself. The prompt's own example had been teaching the exact malformed
  shape it forbade; it no longer does.

**The same night, after the repairs (migrations 0117–0118 are the data side of them):**

- **The stale-cutoff lesson bit the guard itself.** The seed layer's vandalism guard had
  blocked a leaders re-seed on five suspicious rows. Web-verification showed **three of
  the five were real post-cutoff events** — including a change of government the
  instance's grounding layer then carried wrong for ten days *because* the guard was
  blocking its own fix. Two rows were genuine vandalism and stayed excluded; the re-seed
  ran through the adapter's fixture path with exactly those two bindings dropped. The
  operational rule this writes: **a guard hit means adjudicate, never assume** — the
  diagnostic's job is to force verification, not to substitute for it.
- The supply-chain pack widened to **all six Tier-A desks** after a preflight re-run
  (one desk carries a measured single-source-concentration caveat, recorded as a watch
  item rather than smoothed over). Telegram's rotation fix proved out with volume the
  same night: the previously-starved channels went from one signal ever to dozens in
  hours, at a 0% poll-cap rate.
- **`docs/OPERATING_YOUR_INSTANCE.md`** — a new practice-layer guide for self-hosted
  instances: seeding, corpus curation, re-measuring inherited constants on your own
  source mix, the periodic data-quality sweep as a checklist, and gate governance.
  Written from an internal gap analysis of what a clone does and does not inherit;
  it teaches method, never data.
- First-clone fixes that same analysis surfaced: the vault loader no longer hard-fails
  on unset optional keys and finds `.env` repo-relatively; the env template documents
  all vault-mapped keys; the stack registrar honors the embedding-dimension env; setup
  docs no longer assert a rotting migration head; and an opt-in
  `LEGBA_LLM_SEND_MAX_TOKENS` protects instances on hosted LLM endpoints from silent
  finding truncation (unset — the default and the reference deployment — is
  byte-identical to prior behavior).

## 2026-07-30

One theme: **measurement over capability**. This release adds almost no new analytical
surface; it measures the surfaces that existed, publishes the numbers — including the
unflattering ones — and repairs what the measurements found. Migrations 0106–0116.

**The match-precision loop, measured twice**
- A stratified gold worksheet over the open-question matcher's edges, labeled
  out-of-plane (a different model family from the analytical plane, with web
  verification, provenance stamped per row; 243 rows across two rounds). Round 1:
  pooled pairwise precision **0.279**; the only clean class (vector+entity+geo,
  17/17) turned out to be a single dense event-cluster. Round 2, after three
  measured tuning levers, at volume: **0.15**. The pre-declared ≥0.85 gate for
  building an automatic question-closer failed both rounds, so **the closer remains
  unbuilt** — and every residual failure in round 2 is a *bearing* failure (right
  actor, wrong proposition), which is the honest limit of entity/geo/cosine fusion.
  The edges stay trace-only; nothing downstream treats them as evidence.
- The matcher itself moved on what the labels justified: a vector floor set from a
  14,000-pair live cosine measurement rather than intuition, exclusion of
  question classes a news signal structurally cannot answer, computed (not curated)
  damping of globally ubiquitous entities, and an omnibus-signal cap with
  same-URL dedup. Each lever's effect is receipt-counted per run.

**A cross-family judge**
- The faithfulness judge no longer has to share a model family with the prose it
  grades: judge routes are registry stack components, repointable with one
  environment variable and rolled back the same way, with the blast radius
  provably limited to verify-declaring descriptors. The default deployment ships
  same-model (self-hostable). An in-line cross-family flip was trialed on the
  reference instance and rolled back the same day — free-route judge latency
  blocks the emit path. A second in-line trial on a low-latency commercial
  endpoint went live the same evening as a bounded day-trial; its verdict
  distribution is compared against the gold labels (not against the same-model
  judge's scores) before any permanent routing decision. Score deltas across a judge swap are explicitly *not* treated as
  evidence of judge quality — comparison happens against the gold labels.

**Dead config made real, or removed**
- Descriptor `method.options` now actually reaches deterministic handlers: 125
  documented knobs across 34 handlers were silently inert (the schema forbade the
  field; the runtime rebuilt options at fire time). They are now live-editable,
  validated, loudly degraded on unknown keys, and drift-guarded in both
  directions so dead config cannot re-accrete. Registration warns on inert
  inline analyst blocks; seven dead ones were removed.
- The trust gate itself was deduplicated under a byte-identical, mutation-tested
  bar: six copies of the citation-ordinal traversal became one, and the
  composer's eight ad-hoc prompt splices sit behind one assembler. Zero behavior
  change, proven by execution — and the new equivalence suite catches a
  regression class the previous tests provably missed.

**A second domain, thin by design**
- A supply-chain disruption pack: chokepoint-lane and flow desks (three lanes
  active, the rest gated on measured collection), one bounded unit, riding the
  unchanged verify gate, composition, indicator and alert machinery — the
  domain-agnostic claim demonstrated rather than asserted. Sources to match
  (maritime, freight, semiconductor, trade press), including per-channel
  source-class overrides on Telegram so actor-aligned channels carry their
  honest editorial class without a second session.
- The lane windows were chosen from a preflight that measured the slice reader's
  real capacity — at the standard 72-hour window one lane would have silently
  dropped 48% of its evidence; at 24 hours, zero drops.

**Truth-in-labeling, tightened**
- The headline verdict badge now reads **grounding-verified** (the claim follows
  from its cited evidence — groundedness, not world truth), and the structural
  chip says what it actually is: recomputation-verified.
- A generated release-state manifest (`docs/RELEASE_STATE.md`) replaces
  hand-maintained counts everywhere; the docs consume it, so drift between the
  system and its description is now a script failure instead of a review finding.
- A source-quality ledger (one view, typed `asserted_`/`earned_`/`computed_`
  columns, deliberately no composite score) supersedes the scattered credibility
  reads; the old routes serve with deprecation and sunset headers.

**The bearing pipeline (same-day follow-on)**
- The measurement above pointed at a semantic fix, and the fix shipped the same
  day: a two-stage bearing pipeline behind the matcher — an idle self-hosted
  8B judges "does this signal bear on this thesis?" before an edge is written
  (measured against the gold labels: yes-precision 0.842 with a few-shot
  prompt tuned on a train split and validated held-out, specificity 0.969),
  with an optional batched confirm pass on the primary model for survivors.
  Ships **off** by default (a descriptor with no options block is
  byte-identical to the previous release); an 8B outage stamps edges
  `unavailable` rather than silencing the matcher; every gate decision is
  receipt-counted and every passed edge carries the prompt version that
  judged it. The question-closer remains unbuilt — its ≥0.85 gate now has a
  measured path instead of a hope.

**Keeping itself honest at runtime**
- The alert plane consolidated: geo-convergence now rides the shared trigger-class
  machinery (watermarks, caps, rollup) instead of a bespoke path.
- Watchdog coverage extended and corrected: a search-plane canary; an LLM-plane
  heartbeat whose blind spot (an analyst that degrades *gracefully* during an
  outage kept resetting the silence clock) was found by a real outage and fixed;
  and an auto-restart watchdog for the model host, because supervision that
  reports RUNNING over a dead port is the documented failure mode.

## 2026-07-28 (second wave)

A follow-on wave the same day, with one theme: **the chain could say what a finding
rested on, but not what now rests on it** — and nothing kept an unresolved question
alive after the run that raised it ended. Migrations 0106–0113 (0110/0111 unused —
slots a parallel branch reserved and never filled).

**Coherence — a question that outlives its run**
- **Forward lineage**: `output_consumption` inverts `derived_from`. It is stamped
  where consumption is *decided* — inside the composition's own basis/periphery
  split, and at the journal's slice selection — and it keeps the distinction that
  matters for triage: whether a live product is **built on** a claim or merely
  **mentioned it as a caveat**.
- **Standing open questions** are now durable, queryable objects (a `hypotheses` row
  with `status='open_question'` — the existing shape reused, no new table). Two
  faucets fill them: a deterministic harvest across five classes of question-shaped
  state the substrate was already recording (scorecard↔composition disagreements,
  compose-time staleness advisories, below-floor findings, open contested-fact
  groups, starved collection cells), and a per-finding faucet letting each of the ten
  inline units emit what it genuinely could not resolve — with the prompt explicit
  that an empty list is the right answer and questions are never invented to fill a
  quota. The harvest is **an operator-run one-shot, dry-run by default; it is not
  wired to any cadence.**
- The **corpus researcher drains that backlog** as a Tier-1 grounding source, ordered
  by whether anything still *live* rests on the question (a bounded forward walk over
  the new consumption index), and links an answer back with an append-only bearing
  edge. It never closes a question — no code path in the tree moves a row out of
  `open_question`. The link is a pointer for a human, not a verdict.
- **`claim_watch`**: the other direction — has anything arrived that bears on a
  standing question? A deterministic ($0, no LLM) matcher riding the *existing*
  change-detection plane rather than becoming another bespoke watcher. Three fused
  planes (vector / entity / geo) with the entity plane graded by document-frequency
  **specificity**, so an entity most of a desk's questions carry counts for little —
  the arithmetic guarantees that mere desk co-membership can never constitute a
  match. Its cursor carries a bounded freshness horizon that, when it skips ahead,
  reports the **exact count** of signals it abandoned rather than reporting a clean
  run, and a tail-hold so it cannot outrun the embedder and strand signals as
  "seen, vector-less".
- Stated plainly, because it is the honest shape of the feature: **`claim_watch`
  flags and stops.** It writes review flags and edges, counts a staleness debt, and
  writes no correction content, never writes back to the flagged producer, and never
  recomposes — true by construction of what the handler can write, not a toggle. The
  closing half is not built, gated behind a match-precision measurement not yet
  taken, and the debt count has **no read route**: it lives in the run's receipt.

**Reaching outside — external retrieval, with the absence contract spelled out**
- A **`search_provider` stack family**: a ninth component kind, registered,
  credentialed and health-checked exactly like the model/vector families, with
  per-provider handlers behind a component id. Resolution copies the judge route's
  ladder including its opt-in gate — the global env override can *repoint* a surface
  that already opted in, never *enable* one.
- The contract that makes it usable in an evidence system: a search returning zero
  results is **not** evidence of absence unless the engines were shown to be
  answering at that moment. Five statuses separate the cases; only a
  liveness-verified empty may support an absence statement, and only the **scoped**
  one the response hands back verbatim. A degraded empty is returned as a tool
  **failure**, not a zero-result success — because "completed with zero results"
  reads to any downstream reader as "nothing exists". Liveness is **measured** by a
  fixed, deliberately non-topical control probe, not assumed.
- **Web-retrieved evidence is demoted, not pooled.** A `retrieval_origin` axis marks
  it as the new exogenous input it is, and a calibration outcome resolved that way
  lands in the weak tier — structurally excluded from the exogenous set the headline
  Brier is computed over, reported beside it with its own sample size. The system
  cannot improve its own headline score by searching harder. The evidence archiver
  **fails closed** on web-origin content with an unreviewed licence: it records the
  skip with URL, licence class and origin, and does not fetch the bytes.
- Shipped **inert**: the local search engine sits behind an off-by-default compose
  profile, and starting it changes no analyst behaviour until an operator also binds
  a component, opens egress, and the pack/target grants line up. On the two consult
  surfaces the `web_access` grant is presently the **grant leg only**.

**Honesty**
- **`unscoped_absence_claim`**, a new soft verify class, exists because a correctness
  review found the failure faithfulness is structurally blind to: findings that were
  *faithful to their inputs and wrong about the world*. In one week's gold-set cohort
  **5 of 8** downgrades were the same shape — a thin-collection desk asserting an
  absence as a world fact when all it had established was that its own sources
  carried nothing. The response is a deterministic, conservative lexical backstop
  (hedged, cited, forward-looking, survey-shaped and already-scoped forms all pass;
  a hit adds one unsupported claim to the score, never a delete) plus a
  collection-scoped absence rule on all ten inline-unit prompts, voice-matched per
  descriptor and test-pinned so it cannot quietly drift out.
- Scoping honesty about the backstop itself: on the judge-on path the judge's own
  absence rubric already covers this and the deterministic hit is deduped away — it
  bites on the floor-only path. It is a backstop, not a second opinion, and neither
  leg checks a claim against the world.
- The **correctness gold set's first cohort is labeled** (n=8 — the weekly sample
  size, not a corpus), with every label stamped with its labeler. It earned its keep
  immediately: it is what surfaced the absence class above. The honest limit is that
  "labels come from outside the production plane" is operational discipline — the
  stamp is recorded, not validated.

**Collection**
- Gaps become **objects**. The collection-gap analyst now also drains the standing
  source-request backlog and writes durable, operator-reviewable **collection
  requirements**: desk, dimension, topic, rationale, the evidence it came from, and
  up to five candidate sources matched **deterministically** against the registered
  catalogue — no model proposes a feed, and where nothing matches the requirement
  says so ("no known feed") rather than inventing a suggestion.
- The route is **disposition-only**: no create, no delete, and no path to registering
  a source. Marking one "registered" records that an operator added a source through
  the normal path; it performs no activation. **A proposal is never an activation.**
  Honest gaps: there is no UI panel yet, and nothing consumes a requirement — it is a
  note to the operator, and the operator is the loop.

**Housekeeping**
- **One janitor**: a retention-policy table plus a single shared sweep engine; the
  two retention handlers are now thin shims over it instead of separate purgers. TTL
  stays **0 (disabled) by default** — deleting substrate data is an operator
  decision, so every seeded policy ships inert — and there is no CRUD route yet
  (an operator edits the table by SQL).
- Docs currency across STATUS, ANALYSIS, DATA_MODEL, ARCHITECTURE, ACQUISITION and
  SEAMS, including two new declared seams (the `claim_watch` closer's missing read
  route, and the scheduled half of the search liveness canary) and a corrected desk
  count in STATUS that had been stale at 25/6 against 32/13 everywhere else.

## 2026-07-28

The largest release in the project's history (~215 commits over four days): the product
gained its **alerting loop**, its **evidence archive**, and most of the program a
far-back design review laid out. Migrations 0091–0105 (0095/0100 intentionally unused).

**The loop — verification-gated alerting, end to end**
- A modular alert-sink plane (dispatcher + ledger row per outcome + per-alert idempotency)
  with a generic webhook sink and a native **ntfy** push sink (title/priority/tags,
  tap-to-open receipt link). Every outward alert states its verification posture —
  a real faithfulness score or an explicit `unverified — <reason>` — and carries a
  receipt link into the lineage API.
- Anti-noise done honestly: a tunable per-sink cooldown whose suppressed alerts
  **coalesce onto the next notification** ("+N more during cooldown" with a bounded
  preview) — bursts are distilled, never silently thinned.
- `alert_trigger_scan`: deterministic triggers on verified state transitions — scorecard
  band crossings (both directions), new high-severity verified findings, contested-claim
  flips, and per-desk deviation from a statistical baseline — with durable watermarks
  (a transition never re-fires) and per-desk caps with honest rollups.
- **Watchlists**: operator-defined standing watches — an entity (alias-resolved), a
  free-text topic (with honestly-stated search limits), or a place (countries, or
  point+radius on trustworthy-precision geo only) — alerting through the same loop.
- Watchdog precision: per-source polls record the newest entry timestamp so an empty
  streak distinguishes "the feed is quiet" from "our cursor is eating entries";
  per-source/per-analyst alerts fire on state *transitions* (entered/recovered), never
  as a repeating level. A profile-gated local ntfy service and an `alerts.` subdomain
  vhost complete the path to a phone.

**The record — a provable moat**
- **Evidence archival**: signals cited by verified findings get their original bytes
  fetched (SSRF-guarded, size-capped, per-host politeness), stored content-addressed
  (`cas:sha256/<hex>`), license-gated (forbidden classes skip with an honest counter),
  marked `evidence_hold`, and their extracted full text indexed into the search corpus —
  the receipt chain now terminates in a verifiable copy, not a rotting URL.
- **Judge provenance**: every faithfulness critique stamps which model judged it
  (`judge_llm_ref`), classifies failures hard/soft (entity-scramble vs unsupported
  inference), and persists a **full per-claim verdict ledger including supported
  claims** — visible in the UI as a citation-hover verdict card. An independence-posture
  judge prompt ships dormant behind a profile flag for a future second model.
- **Calibration**: scorecard band changes are logged as resolvable claims and graded
  deterministically at 14/28-day horizons (held / reverted / worsened), published as
  persistence and reversal rates — explicitly *not* a Brier score (bands aren't
  probabilities, and the docs say so).
- **A correctness gold-set loop**: a pinned weekly stratified sample of verified findings
  rendered as a labeling worksheet; operator verdicts feed an additive
  operator-correctness figure that is never pooled with the deterministic recall leg.
- **Two-tier composition evidence**: compositions consume a verified **basis** (≥ the
  0.50 floor) plus an explicitly-labeled, capped **periphery** of weak/unverified
  signals that may only inform hedged context — with conflicts against the basis
  surfaced as "tensions worth watching." Unhedged use of weak evidence is a counted
  verify failure. Each composition records "built on N verified + M weak signals."

**The fabric — sources that earn their standing**
- A **source assurance ledger**: multi-rater ratings (public and private annexes as
  concurrent currents), Admiralty display vocabulary, cited dossiers — plus an **earned
  track record** computed from the system's own substrate: how often a source's claims
  ended on the winning side of resolved contentions (Beta-smoothed, Wilson-bounded,
  with a lag + self-exclusion acyclicity guard before it may influence tie-breaks).
- The **contested-claims arbiter tail**: soak-gated weighted tie-breaks (source count,
  diversity, credibility), a cached LLM near-tie adjudicator, and coexistence surfacing
  — a winner is surfaced with rationale and history, the losing claim is never mutated,
  and new evidence re-opens the dispute.
- **Fact decay**: per-class confidence decay curves (structural facts age slow, event
  facts fast) with corroborations as sightings that reset the clock — computed as a
  readout sidecar; consumption is flag-gated.
- **Narratives as first-class objects**: contested-claim families reified with their
  carrier sources, first-seen times, and echo lags, plus a directed source-echo graph
  (who publishes first, who follows, at what delay) — detect-only, descriptive-not-causal,
  and honest when no systematic echo exists.
- New deterministic reads: geographic convergence detection (distinct source *families*
  converging in honest two-tier bins), per-source freshness grades against
  cadence-derived budgets, and per-desk statistical baselines (lags, rolling means,
  neighbour spillover — a falsifiable prior, never a forecast claim).
- Acquisition quality: intra-source exact-duplicate collapse at ingest (recency-preserving),
  publisher-origin/dateline geo contamination fixed (content-corroborated tagging),
  the officeholder seed adapter now selects current holders only (with a read-only
  stale-leader diagnostic), Telegram poller hardening, and 51 new draft source
  descriptors (41 verified feeds + 10 via a profile-gated RSSHub lane).
- **Structural claims verification**: deterministic analysts that assert checkable
  quantities now have those numbers re-derived from their own lineage — a miscount
  becomes a flagged critique, and the badge distinguishes structural-verified from
  unverified-structural.

**The workstation**
- The **Wall** (band grid + movers since your last visit + newest verified + health),
  a **validity-window timeline** (the temporal substrate's first temporal view), a
  deepened **map** (density hexes, echo arcs, a working time window, convergence
  markers, watch locations), **provenance badges** (`live|fallback|absent`) on displayed
  numbers, a rebuilt **report export** (collection basket → markdown/JSON with verify
  states and evidence hashes), and bound-panel reachability restored via live-registry
  synthesis. A "what changed since" diff API backs the movers view.
- The MCP server gained seven built-in substrate tools (reads + consult), fixing the
  standalone-empty catalog; a Docker Swarm conversion assessment ships as draft stack
  files with an honest Dapr verdict.

**Honesty & operations**
- The journal's faculty lenses gained a numeric-fabrication guard (written source-health
  counts are validated against the deterministic tool and flagged on divergence) and an
  empty-read fallback to the verified corpus. The stale-leader verify guard now also
  reconciles officeholder claims against the facts table. Findings reads stamp
  `below_floor`. Unit token budgets were raised 100× (the caps had been silently pausing
  every bounded unit daily). Telemetry tables gained TTL retention (opt-in).

## 2026-07-24

The largest release since initial publication (`c9b65f6`, covering ~three weeks of work).

**Analysis & verification**
- Per-kind faithfulness judge profiles: a dedicated absence-claim branch now scores
  "no evidence of X" claims beside the citation-support judge (the class that previously
  showed 0.0↔1.0 variance on identical prose).
- Per-signal salience scoring with compose-time consumption, plus an advisory salience
  check in the verify path ("does the lead match the top-magnitude input, or is the
  demotion explained?").
- Compositions re-resolve every input finding to its **current head** at compose time
  (with input-as-of annotation) — a reversal at the unit tier can no longer be quoted
  stale by the country/region/world tower.
- Contradicted-claim honesty stamps: a claim the support-judge marks
  contradicted-by-its-own-source now flags the containing entry's honesty state.

**Journal & the voice roster**
- The journal grew from two tiers into a roster: the 12h first-person entry tier and
  daily consolidation are joined by a weekly third-person **chronicle**, four
  falsifiable-prior faculty **lens** reads (trend / base-rate / capability / intent),
  and a **chorus diff** that reconciles them. All append tiers flow through the journal
  API's default stream; all stay off the product chain.
- Journal claims now pass their own verify profile: cited-fact claims are judged against
  their resolved substrate rows; perspective claims are exempt but visibly flagged, never
  stripped; judge-unavailable renders as un-judged, never silently passed.
- A Voices reading surface in the console: kind-filtered rail, grouped cycles, per-claim
  verdict chips.

**Signal depth & retrieval**
- A full-text signals corpus (BM25) with governed search/read tools, vector search over
  signal embeddings, a corpus researcher, and a cross-document corroborator — analysts
  cite documents, not just headlines.

**Language & entities**
- Translation persistence: English titles/bodies stored alongside originals (NLLB), with
  an attribution guard and explicit untranslated tagging — closing a class of
  translated-content inversions at the data layer.
- Translate-then-NER for non-English war-beat sources, with a ~10k-signal re-enrichment
  backfill (drained).
- Entity identity machinery: alias + pairwise-judgement tables, an LLM adjudicator for
  gray-band merge candidates (conservative, cached, human-not-clobbered), an
  entity-bucket reclassifier (ships disabled), and garbage-collection bounds.

**Sources**
- Telegram: bounded catch-up after re-authentication. GDELT: a 15-minute file-dump lane
  (registered draft). Freshness-gated auto-unpause for stalled sources; cursor-poison
  recovery fixes; roster retirements recorded in STATUS.

**Operations**
- The pipeline-stall class root-caused and bounded: graph mining's path enumeration is
  capped and moved off the event loop with a hard abandon timeout; a host watchdog
  auto-recovers silent stalls; the restart order is validated and documented; the global
  stall alert persists durably.
- Deploy ordering hardened (registry first, health-gated) against a stale-registry race.

**Docs**
- Currency pass across README, STATUS, DATA_MODEL, SEAMS, GLOSSARY — including honest
  new entries for the voice roster and a new declared seam (action-pack staleness).

## Earlier (2026-06-11 → 2026-07-05)

Pushed with full commit history — see the git log up to `df491d8`. Highlights: initial
public release under AGPL-3.0 (2026-06-24); the source-first go-live; the seven-phase
data-quality program (verify floor, retrieval guardrails, geo + entity-merge cleanup,
fact tiering, situations, comparisons/alerts).
