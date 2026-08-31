# Changelog

Legba's public history is intentionally squashed — each release lands as a single commit
on `main` — so this file is the release record. Entries are dated (newest first) and
written against the docs as they shipped; [docs/STATUS.md](docs/STATUS.md) remains the
always-current truth-in-labeling table.

## 2026-08-30

**Every export this platform has ever produced shipped an empty citation
list, and that deserves saying plainly before anything else.** The export
route's citation reader had two defects in the same eight lines: it read the
wrong nesting level of the stored payload, and it filtered on a field that
only one of the citation kinds carries. On a real stored row the two
compounded into the same answer — zero citations, of any kind. So every
exported finding carried an empty `### Citations` section under the actively
false line *"(no resolved citations recorded on this row)"*, whatever the
finding had actually cited; a world or country report exported with 100% of
its citations gone. A product whose entire claim is that its reads are
checkable was shipping the one artifact meant to travel outside the console
with the checking stripped out.

- **The reader now reads the level the writer writes**, and keeps every kind
  — raw-signal references, composition sub-claim references, and all five of
  the desk-grounding block kinds, none of which carry the signal field the
  old filter demanded. Measured against the same real payload: 0 citations
  before, 4 after.
- **The test fixture was the reason nothing caught it.** The export suite
  hand-inserted a flat payload shape the writer has never produced, so the
  suite was faithfully testing a document that does not exist. The fixture
  now matches the column, and a new test pins the producer→consumer coupling
  directly so the two can't drift apart again silently.
- Each exported citation now also carries its **kind**, what it **resolves
  against**, its **marker class**, and the **source of its resolution** —
  four additive fields, deliberately named apart from the pre-existing
  resolution field rather than overloading it, because two fields called
  `resolution` meaning different things in one document is a trap.
- The same misreading had a console half: the citation model recognized one
  grounding kind out of six. A window-ledger reference rendered as an amber
  "unresolved citation" warning when it was resolved and fine, and a
  prior-read reference was labelled "signal" and drilled to a signal that
  does not exist. All six kinds are now carried verbatim, labelled honestly,
  and drilled to the row they actually name. Judge stamp `2026-08-30/1`.

**The landing page becomes a stance you choose, not a grid somebody
hardcoded.** The console's boot layout is now one of **six workspaces** —
Morning Read, Desk, Investigate, Trust, The Gate, Engine — each answering a
different question, each with its own persisted layout, each one keystroke
away (`Alt+1`…`Alt+6`, `` Alt+` `` to cycle, `Alt+Shift+R` to reset the
current one). Morning Read is the new landing: an at-a-glance strip, the
wall, the live feed and the world assessment, seeded in a single paint. A
custom layout saved under the old scheme is preserved and copied once into
the Morning Read slot on first boot, so nobody loses a workspace they built.

- **The catalog folds.** The sidebar's 36 always-open rows collapse into five
  verb-grouped headers with counts, so the panel list stops spending the
  whole sidebar budget on itself.
- **Twelve retired panel kinds get an alias table instead of a graveyard.**
  Kinds that had been merged into better successors were previously just
  hidden — present in the registry, invisible in the catalog, and still
  liable to be restored out of a saved layout. They are now *aliases*: a
  saved layout naming a retired kind silently resolves to the survivor that
  replaced it, with the tab it belongs on. The registry drops from 67 kinds
  to 55, and no component was deleted to get there.
- Building this found a real defect in the alias pre-pass: duplicate
  collapsing was tracked per dock group rather than globally, so two retired
  tiles in *different* groups resolving to the same survivor mounted that
  panel three times. Caught by a test that mounts the real dock rather than a
  stand-in, which is now the standing pattern for layout-level behavior.
- **The palette is recalibrated.** One meaning, one channel, one ramp:
  severity's worst rung was rendering in a colour that read as *safe*, and
  confidence shared hues with severity so the two could be mistaken for each
  other at a glance. Severity now runs a single red→amber→blue ramp with a
  neutral floor; confidence runs a desaturated blue sequential ramp of its
  own. This is the most visible change in the release and the most
  arguable one.

**The platform receipts every write and had never once receipted a read.**
Roughly eighty tables record what the machine produced; nothing anywhere
recorded whether a human ever looked at it. A new append-only **read-events
ledger** (migration `0189`) closes that, with a closed seven-value vocabulary
enforced by a database constraint rather than convention — panel opens,
workspace switches, finding opens, lineage walks, citation drills, consult
opens, and the headline *brief read* (opening the morning landing). The
console emits at seven chokepoint surfaces rather than at call sites, batched
every four seconds behind a bounded queue that drops oldest rather than
growing, flushed on tab close, and fail-silent in every path — telemetry that
can break the product it measures is worse than none.

- `POST /api/v1/read-events` appends a batch (202, per-event validation: a
  malformed event is dropped and counted, never a batch-wide rejection) and
  `GET /api/v1/read-events/rollup?days=N` serves a bounded daily rollup
  grouped in the database, so a scoreboard on a timer can never turn into a
  table scan.
- Deletes and updates on the ledger **fail loud at the database** — an
  attention record you can quietly revise is not evidence.
- A new **Read Scoreboard** panel (the 56th kind) shows reads today, morning
  reads, drills, a per-kind table and a fourteen-day strip. An empty log
  renders as a stated finding — *"nothing read in the last 30 days"* — not as
  a broken panel, because the whole point of the instrument is that it must
  be able to return bad news.

**Alerting learns to spend a daily budget instead of a per-event impulse.**
A desk under standing sanctions was re-paging every cycle because the model
had re-written the same unchanged fact in new words. Three mechanisms, in
order, and none of them drops anything:

- **A steady-state guard** suppresses a verified-finding page only when all
  three of these hold: the desk's banded severity is unchanged, the finding's
  own movement tag reads *steady* or is absent, and the desk was paged within
  the last 24 hours. A *rose* / *fell* / *new* tag always pages. No prior
  record for a desk, or an unreadable timestamp, pages — it fails toward
  noise, never toward silence. Measured over 221 real alerts: 62.9%
  suppressed.
- **A fleet-wide daily page budget** — five pages per UTC day by default,
  ranked worst-first — plus a **kind-diversity cap** of three slots per
  trigger class per day, because one always-critical class would otherwise
  take every slot every day and starve everything else. A slot no other kind
  can fill goes unused rather than being backfilled with more of the capped
  kind. Everything over budget still writes its row, tagged as deferred.
- **A kill list**: two low-signal trigger classes now default to not paging.
  Their scans still run and their watermarks still advance as though they had
  fired, so re-enabling is a config flip and not a backlog. Replayed over the
  same window, 1,507 pages become 25.
- All of it is tunable without a rebuild — `LEGBA_ALERT_DAILY_PAGE_BUDGET`,
  `LEGBA_ALERT_BUDGET_PER_KIND_CAP`, and per-descriptor options for the
  cooldown, the caps and the master switch.

**A finding is no longer excluded from a composition on evidence nobody
graded.** The LLM judge is sampled by a content-independent hash of the
finding id, which means whether a finding was judged is a coin flip with no
relationship to whether it was any good. Measured over fourteen days,
findings the judge never sampled failed the composition's 0.50 verify floor
at **24.2%**, against **3.2%** for judged findings — the gap being almost
entirely a failure class the deterministic scorer cannot recognize and the
judge can. So the floor was excluding real reads for the crime of not having
been sampled. Now any unjudged finding about to be excluded by the floor is
sent to the judge *first*: the floor may only ever exclude on judged
evidence. The escalation happens at the verify boundary, not in the
composition query, and it carries its own marker so an escalated verdict is
never mistaken for a sampled one. Expect roughly +43% judge volume and a
fleet-mean faithfulness that moves *up* after deploy — that is a selection
change, not a quality improvement, and anything tracking the mean across the
deploy date must partition on the new marker to stay honest.

**Legba now checks its own claims against the outside world.** Every
verification surface the platform had graded internal consistency: does this
read follow from what it cited, does this composition follow from its inputs.
None of them could tell you whether the underlying claim was *true*. A new
**standing external auditor** runs once daily on the free model plane: it
samples the world read plus a rotating subset of desk reads, extracts one or
two checkable world-claims from each, checks them against live external
search through the governed web-access pack (never ad-hoc HTTP), and records
a verdict per claim — supported, contradicted, not found, or unchecked. A
contradiction on a high-severity claim writes an alert row.

- **It writes a heartbeat on every run, including runs that audit nothing** —
  so "there was nothing to contradict" and "the auditor is dead" can never
  look the same from outside. That distinction is not hypothetical: a judge
  outage went unnoticed for days once, for exactly this reason.
- `GET /api/v1/v3/system/external-audit` serves the heartbeat, the verdict
  mix, the contradiction rate over *checked* claims (absent, never `0.0`,
  when nothing was checked), and the contradicted rows by name with their
  source URLs. It never returns a 500 at a polling panel.
- **Both planes or neither**: with no search binding the auditor refuses to
  spend a model call at all and files a loudly unaudited heartbeat naming the
  gap. Missing database access raises; every other gap degrades honestly.
- It ships as a **draft descriptor** — activation is an explicit operator
  decision, not a side effect of deploying.

**The situations register stops being one frame per desk.** The clustering
key was topic-only, so every producing dimension on a country desk collapsed
into a single mega-frame — one situation per desk, fleet-wide, absorbing
everything. The key now carries the producing dimension (migration `0188`
splits the existing open frames accordingly, conserving members, intensity
share and validity windows, and re-basing the hypotheses that were emitted
against the old intensity so a re-scale cannot mass-refute 4,405 live
hypotheses). Dormancy detection, which had been structurally unreachable —
zero transitions to dormant across 2,052 ledger rows, because it keyed off
any desk's last write rather than the evidence clock — now shares one
evidence-anchored predicate with the register's forgetting curve.

- The tracker's fixed selection was an absorbing state: it re-adjudicated its
  own top rows and never reached a frame that had never been picked. It now
  splits its per-tick budget between an intensity leg and a **staleness leg**,
  and the budget itself is tunable
  (`LEGBA_SITUATION_TRACKER_MAX_SITUATIONS`, default 12) because the frame
  population grows several-fold under the split.
- The register's checkpoint rows stop printing free prose. An unchanged
  checkpoint now renders as a date and a movement name only; prose survives
  only on deltas that carry cited evidence — closing a path where the
  system's own bookkeeping read as testimony that an event was still live.
- Ten desk prompts were carrying **two** register blocks: the guarded one
  that ships staleness and corroboration warnings, and an older unguarded
  duplicate that did not. The duplicate is gone, along with a cross-target
  leak that fell back to a global top-list for non-country desks.
- The journal's "new situations" counter was reading a modified timestamp
  that a twenty-minute clustering pass touches on every live frame, so it
  reported 44 new situations per cycle when the true number was zero. It
  reads creation time now. Expect that number to fall to about zero — that
  is the fix, not a collection failure.

**The world read shipped its own JSON wrapper as the body.** When a
composition returned a JSON envelope with one malformed key, the whole
response was discarded and the raw envelope published as the finding — a raw
JSON blob standing on the surface as the platform's current assessment of the
world, scoring a healthy-looking faithfulness on the two claims a wrapper
happens to contain. The body is now unwrapped when the intent is
unambiguous, and **fails loud to the dead-letter queue** when it genuinely
cannot be recovered — never published. A sibling hole is closed the same way:
tool-call JSON that parsed "successfully" into an empty body was scoring a
vacuous perfect faithfulness, and now raises instead. Salvaged rows are
marked as salvaged. Historical rows are deliberately not rewritten.

**The verify floor's exemptions now belong to the clause that earns them.**
Three exemption rungs — synthesis prefixes, assessment scaffolding, and
absence phrasing — were keyed to the whole span. A sentence that opened with
a scaffold prefix and then named a president, an agency head and a country
escaped scoring entirely. Each rung is now tested positionally, against the
clause that earns the exemption, gated by whether the rest of the span
asserts a specific fact — the same standard the judge itself already applies.
Strictly additive: **+6,772 spans enter the denominator, none leave**, over
54,610 segmented spans and 6,000 replayed findings. The floor arm's spread
between published and true gate narrows 0.452 → 0.308; the judged arm is
byte-identical across all 1,394 replayed rows. Unassessable verdicts fall 67%.
Judge stamp `2026-08-29/1`.

- Riding the same stamp: **a guard that had never once fired**. The
  hedged-conflict rule shipped in the prior release was spelled with ASCII
  hyphens, while 58% of graded claims carry a non-breaking hyphen the
  producer emits — so it matched nothing, 0 of 573 graded claims, and the
  regression suite could not see it because the suite's fixtures were typed
  by hand in ASCII. The Unicode fold is applied; four of seven archived
  specimens now fire as designed and the other three correctly stay silent.
  A sibling omission of the same fold, in a place where fixing it can move
  published scores, is deliberately held for its own stamp rather than
  smuggled in here.

**Calibration pooling shipped, and its honest result is zero.** Band
calibration and unit correctness partition their populations on the current
judge-pipeline stamp, so a stamp change starts the population over. They can
now *pool* consecutive stamps into one population when the pipeline's own
lineage prose affirmatively declares that a metric family cannot move across
that boundary — tracked per metric family, disclosed on the wire as the exact
pooled stamp set rather than silently widened. Applied to real lineage it
yields **nothing**: fourteen stamps collapse to twelve populations, both
poolable pairs are historical, and the live headline stays at zero. That is
the finding, not a failure of the mechanism — band calibration resolves a
claim at fourteen days while the mean stamp lifetime is about 2.3 days, so a
claim can never be both currently-stamped and resolved. The population has
been empty every day since 2026-08-04: 1,802 claims, all excluded, for
twenty-five days. The fix is a slower stamp cadence, which is a decision, not
a patch. No stamp bump — this is a reader-only change.

**Ingestion smalls.**

- A dead-source escalation added in the prior release was **dead code**: its
  poll-history fetch window was smaller than the streak length required to
  escalate, so a permanently-dead-but-cleanly-polling source could never
  alert however long it stayed silent. The window is now sized from the
  thresholds it is measured against. Expect a burst of prolonged-quiet
  escalations on the first cycle after deploy for the sources currently
  pinned just under the old window — intended.
- One upstream feed intermittently exports rows carrying the **previous
  year** in their date field. Ingest now detects that exact signature (prior
  year, matching month and day, within a bounded future skew) and stores the
  corrected date while preserving the original for audit. 369 existing rows
  carry the defect; this change is forward-only and does not rewrite them.
- One more evidenced near-miss spelling of a severity-movement tag
  normalizes to its canonical value; anything ambiguous still reads as
  absent, never guessed.

## 2026-08-27

**A situation could not stop being urgent, because the only clock it had was
the one the product wound itself.** An event enters the situations register.
The desks' 72-hour slices stop seeing it, so they write "no material change
since the prior read." The register records those as activity, reports the
frame back as standing intensity, and the desks cite that as confirmation the
event is live. The composition then leads with it. At one measured moment a
frame stood at intensity 59.3, event count 396, status active — on a strike
that had **ended three weeks earlier**, with one wire signal in 45 days
headlined that the workers had resumed. Nothing was miscalculated. Intensity
was a measure of how often the pipeline ran.

- **A second clock, wound only by the world.** Intensity now decays against
  the newest *significant* ledger delta — a movement that cannot be written
  without cited evidence, whose timestamp is the evidence's own. The
  half-life scales with evidence density, so a frame with one corroborated
  move decays fast and a frame with nine decays slowly. Past the desks' own
  72-hour horizon a frame is demoted to dormant. Demotion only: it never
  promotes and never auto-closes.
- **A frame the ledger has never moved decays on age alone** from its own
  opening. This is the largest class in the fleet: 24 of 50 non-closed frames
  had no evidence-bearing ledger row *ever*, and all of them were rendering
  as active, one of them 73 days old.
- **A resolution now reaches the register.** A trajectory close had been
  landing only in the event ledger while both register reads gate on the
  frame's own status — so a frame that had been formally closed went on
  rendering at full intensity.
- **The register says what it is.** Both renders now carry the frame's last
  corroboration time, its evidence age, and an explicit stale-no-new-evidence
  or never-corroborated label, under a stated rule that the register is the
  system's own bookkeeping and may never be evidence that an event is
  current. A finding whose citations are *all* register references asserting
  currency is now a counted soft verify failure.
- Fleet effect, replayed against live data: active frames 42 → 17, dormant
  8 → 26, and the frames the world is genuinely moving keep 95–98% of their
  intensity. Judge stamp `2026-08-27/1`.

**A composition may no longer claim what its own inputs don't support.** The
rule already existed and was already obeyed one layer up — by prompt. The
prompts are good and it failed anyway. This is the mechanical version: a new
deterministic grader reads a composition against **the desk reads it cites**,
using evidence the verify pass already held and had never once read. Four
arms, four named failures:

- **Scope laundering** (soft) — a desk wrote "no coordinated narrative
  appears in *this desk's collection*"; the composition deleted the qualifier
  and led with "in the country's information environment". Every one of that
  read's inaccurate verdicts came off that single deletion.
- **Direction conflict** (hard) — a composition asserting a cited read
  "confirms increasing and expanding" activity over a head whose verdict was
  "remains unchanged". The house definition of a hard failure, with the
  aggravator that the composition named that source as its authority. It
  quotes both poles verbatim or it declines.
- **Asserting a desk negative** the desk never wrote, and **quoting a desk**
  words that appear nowhere in its read (both soft).
- A time bound answers *when*; only a collection bound answers *what was
  searched*. The composition-layer scope test therefore drops the two time
  nouns the unit-layer lexicon carries — a read claiming a whole country's
  information environment "in the latest 72-hour slice" is the exact case
  that motivates the distinction. Measured over ten graded compositions with
  a deliberately over-broad citation set: five violations found, all
  grader-confirmed, zero false positives.

**The confidence damper is retired from the banding path.** A band sitting
between the confidence floor and the confident knee shipped one rung *down*.
When the tag being banded described a week's movement, discounting a
weakly-evidenced week was a defensible hedge. Since the tag became the
standing *state* of a dimension, the identical subtraction says something
absurd: "we are 55% sure this desk read the war correctly, so call the war
one rung smaller." A dimension carrying a moderate severity and a *rose*
movement shipped as `low`; the only dimension in its read that had risen was
the only one damped, and it shipped as `watch` in the sixth month of a
shooting war.

- Weak confidence is now **named** (`qualified-low-confidence`) rather than
  subtracted, the floors decide admission and nothing else, and every row
  records the rung the retired damper *would* have shipped — so the change is
  auditable per row instead of from a deploy log. The damper's definition is
  kept; restoring it is one branch.
- **The card and the prose stopped contradicting each other.** Every
  insufficient-evidence slot in the graded round sat beside a composition
  that had *consumed* a verified read for that same desk — 20 of 21 clearing
  the composition's own bar. The divergence was an ordering: the composition
  applies its floor before folding to the freshest head, this engine folded
  first and applied the floor after, so it abstained on a failing head with a
  passing one one cycle behind it, unread. The card now resolves the rows the
  prose actually rests on. It is not a softer path — a consumed head that
  fails a guard is refused exactly as a fresh one is, and when none can be
  banded the dimension still reads insufficient-evidence, but now names which
  rule refused which rows.
- Replayed over ten countries pinned to each card's own instant: mean
  distance to the graders' blind reference bands 1.449 → 1.245, exact matches
  6 → 8, and 20 of 21 abstentions recovered with none landing above
  reference. Attribution is clean — every band move in the previously-banded
  population came from the damper alone, every recovery from the alignment
  alone.

**A stamp change is a migration, not a world event.** Retiring the damper
legitimately moves about thirty bands fleet-wide on the first sweep after
deploy, and every one of those moves straddles a change in the banding
semantics stamp. Both the alert scan and the calibration tracker would have
read that as thirty deteriorations and thirty resolvable calibration claims —
the platform paging the operator about its own upgrade. A semantics mismatch
between two cards now pre-empts every other classification: the transition is
labelled a semantics migration at low severity regardless of which way the
band moved, folded into **one** informational alert per desk rather than one
per dimension, and excluded from calibration aggregates by a query predicate
that reports the excluded count honestly rather than hiding it (migration
`0187`). Cards missing the stamp on both sides read as unchanged, so
untouched history is byte-identical.

**A composition could freeze before the reads it was composing had run.** The
country and region compositions read their own units' heads and never consume
a raw signal — but every analyst matched onto a target was being registered
for that target's raw-signal trigger regardless of what it reads. Two
unrelated wire signals could therefore wake a composition hours before that
day's later units had run, and the reactive fire's cooldown then suppressed
the correctly-ordered scheduled tick outright. Measured: 30 of 31 country
targets had desk heads landing *after* their own composition had frozen. An
analyst that does not read signals is no longer wired to the signal trigger,
so a composition runs only on its own cadence — and the ordering invariant
(every composition's tick lands strictly after every one of its units') is
now pinned by a test against the shipped descriptors.

**A composition now declares the evidence window it actually covers.** The
self-description was a single as-of instant the model derived by scanning the
rendered blocks, and it drifted — one read claimed a latest timestamp fifteen
hours earlier than the heads the render had shown it. The real oldest and
newest timestamps among the consumed heads are now computed from rows already
being read (no extra queries) and handed to the model as a copy-only block,
and the same computed value is stamped onto the finding so a downstream
reader can check the prose against data instead of trusting it.

**Two publishers, one wire story, one numbered signal.** The last
false-positive class on the narrative desk, and the one four rounds of prompt
text could not close: a single agency dispatch reaching a desk under two
mastheads as two separately numbered signals, on which the desk then called
"coordination" — quoting the identical phrasing while describing the sources
as independent. No prompt text makes a desk un-see two numbered signals it
was handed. The substrate-level dedup cannot reach this either, and not by
oversight: two publishers hash differently however identical the headlines,
and the semantic tier's threshold is deliberately high because a false link
hides a signal from every desk on the platform.

- So the collapse lives where its blast radius matches its confidence: one
  desk, one run, one prompt. Nothing is written to the substrate, both
  signal ids stay in the provenance chain — the desk read both, it simply
  reads them as the one story they are — and the survivor renders a line
  naming the mastheads, which turns the false-positive surface into the
  corroboration datum it always was.
- The precision guard was measured rather than assumed. Keying on headline
  and day alone collapsed five groups in the sample window and only one was a
  wire pair; the other four were disaster alerts sharing an auto-generated
  title while describing different events. Requiring **two distinct
  mastheads** drops every one of them and keeps the pair — syndication *is*
  one story under several mastheads, and a same-publisher repeat never
  presents the two-publishers surface.
- It was never desk-scoped, and a later sweep read the code's own motivating
  example as saying it was. Regression coverage now runs the collapse across
  five named desks so it cannot silently re-scope.

**A source can be dead for nine days and healthy the whole time.** One feed
returned 110 consecutive empty-but-successful polls, state active throughout,
and no alert ever fired — because a poll the discriminator classes as
*honestly quiet* (the source's own crawl saw nothing newer) was exempted from
escalation with no ceiling on how long the streak could run. The exemption
exists to protect genuinely low-cadence feeds and still does; there is now a
much higher, tunable bound past which an honest-quiet run escalates anyway,
worded to tell the operator this is **not** a cursor or filter fault so the
wrong investigation is ruled out up front.

**An honest hedge stops being a hard failure.** The composition layer
correctly writes sentences like "a weakly-supported read says no such event,
which conflicts with the verified finding that it occurred; the former is
below the verification floor" — two conflicting inputs, both named, the
stronger preferred. But the absence-claim test was a substring match over the
whole span, so the embedded quoted negative tripped the absence grammar,
found a "violating" row, and that row resolved back to the same weak side the
sentence had already named and already cited. The sentence was hard-failed
for not believing the thing it explicitly said it did not believe. A
deterministic guard now recognizes the shape — a weakness marker governing
the absence idiom, a strength marker bound to a finding noun, and a conflict
connective separating them — and returns the detail naming both poles
verbatim or nothing at all, so the demotion is auditable from the ledger row
alone. It demotes; it does not acquit. Judge stamp `2026-08-28/1`.

- Two over-firing families from the same census are one word spanning two
  subject matters, which no lexical test can separate — a forestry penalty
  read as trade coercion, a civilian power station read as military
  procurement. They ship as worked negatives in the judge's rubric rather
  than as a rule.

**Smaller repairs.** The absence screen was reading a signal's raw title
while the desk had always read the stored English translation, so on a
non-Latin-script source the screen and the desk were grading different text
and an English content term could never collide with a native-script title
(judge stamp `2026-08-25/1`). A movement tag emitted once in a spelling
outside its vocabulary now normalizes through a narrow evidenced table —
`rise`/`rising` to *rose*, `fall`/`falls`/`falling` to *fell* — with anything
ambiguous still reading as absent. And a journal critic query named two
columns that do not exist on the table it reads, returning a 500 from the
proposals endpoint whenever a self-revision proposal sat in the queue.

## 2026-08-21

**The ops deck, and the number nobody could see.** Seven server
endpoints had been live, tested and consumed by *nothing*: the production gauge
and its integrity bricks, staleness debt, source quality, and the three eval
boards. They were built, they answered, and no surface in the workstation asked
them anything. This train gives them readers — four Dockview panel kinds
(Production Gauge, Judge Stats, Source Health, Eval Boards), registered like the
existing sixty and landing in Engine Room, whose rows fold behind one collapsed
header and so cost nothing against the sidebar's spent row budget.

- **`served_by` becomes a fact you can act on.** The one new API. The upstream
  provider a router actually dispatched a judge call to has been recorded on
  every LLM receipt since 2026-08-16 and read by nothing at all — while a
  provider change was measured to flip 13.6% of verdicts. That is an
  unannounced, upstream input to the faithfulness numbers the whole product is
  graded on, and it was observable only by hand-decoding a JSONB array.
  `GET /v3/system/judge-stats` aggregates the verdict mix by
  `judge_status` × `served_by` × day × judge-pipeline stamp, off receipts and
  critique rows that already existed. No migration, no new writer.
- **The attribution refuses to inflate, and refuses to guess.** The
  critique-to-receipt join is many-to-many — one run yields several critiques,
  one finding partitions into several judge calls — so the naive join multiplies
  every verdict by its receipt count and reports a cube that is pure fiction. The
  provider is resolved per *run* before being attached to that run's critiques.
  Where it cannot be resolved it is bucketed, never assigned: a run that flipped
  provider mid-way is `(mixed)`, a direct provider that never reports who served
  is `(unrouted)`, and a verdict with no judge call at all — every
  `deterministic` and `unsampled` one, which by definition never asked an LLM —
  is `(no receipt)`. Each bucket ships its own meaning on the wire so no client
  hardcodes the glossary.
- **Every metric carries its n and its provider.** Enforced structurally rather
  than by convention: no field on the response carries a rate or a mean without
  the count it was computed over beside it, and a mean over zero rows is absent
  rather than `0.0`. The panel's drift readout reports a delta between two
  providers only when *both* clear a minimum sample, and otherwise says how far
  short it is — because "not enough data yet" and "no drift" are opposite
  findings, and an instrument built to detect a real 13.6% effect must not be
  able to invent one. The cube is keyed by judge-pipeline stamp too, so a window
  straddling a judge swap shows two rows and a warning instead of one pooled
  average that could flatten a regression into a straight line.
- **Two deliberate disagreements with the health gauge**, stated in the route
  because the two surfaces will differ: a legacy NULL `judge_status` is reported
  as `(unknown)` rather than folded into `deterministic`, and `unsampled` is a
  first-class bucket rather than excluded. The gauge's folds are right for a
  health check and wrong for the measuring instrument.
- **The accept-reason gap, closed.** `decision_reason` has been on the journal
  proposals row since migration 0048, and only *reject* ever wrote it — the
  accept path set the status and hardcoded a null reason, with no body to carry
  one. The decision trail was asymmetric by construction: every refusal explained
  itself, and every applied change, the half that actually mutates the substrate,
  could not. Accept now takes the same reason, optional where reject's is
  required, recorded on the same atomic claim as the status flip; and if the
  apply then fails, the operator's note is carried into the archived row rather
  than overwritten by the machine's. A decided row with no reason now says so
  instead of rendering nothing.
- Read failures across the ops deck degrade the way the `/system/*` family
  already does — an honest empty payload at HTTP 200 with `measured: false`,
  never a 500 at a polling panel — and every new panel renders that as a loud
  failed read rather than an all-clear.
**Severity as state.** The correctness round's other finding was smaller
to state and harder to see: one tag was answering two questions and therefore
neither. A desk tagged the severity of *what moved in its 72-hour slice*, so a war
in its fourth month was tagged "low" in a week that added nothing to it, the
scorecard banded the dimension `low` off that tag, and every one of the round's
thirty-seven inexact bands sat *below* the reference. Nothing was miscalculated;
the number simply meant something other than what the page said it meant.

- **The tag splits.** `severity` is now the **standing state** of a dimension —
  where it stands today, not how far it moved — and the movement gets its own
  `severity_delta` of *rose*, *fell*, *steady* or *new*. The pair is the point: a
  serious condition that is still running and a quiet desk that just twitched are
  no longer the same reading. `new` is the honest answer when a desk has no prior
  read to compare against; `steady` is a claim that the comparison was made.
- **One contract, every desk, one train.** The rule is a single paragraph in the
  house read contract that all nine bounded units carry verbatim — including the
  desk held back from the earlier voice rewrite, whose hold is about prose and
  cannot apply here: it is one of the seven scorecard dimensions, and a scorecard
  whose dimensions mixed two meanings of the same word would be worse than one
  uniformly on the old meaning.
- **The band is the condition; the movement never touches it.** The banding engine
  reads the standing level exactly where it always read the tag, and carries the
  movement call beside the band rather than inside it — otherwise a war reported
  "steady" for a fortnight would decay a rung a fortnight, which is the same defect
  arriving from the other side. Damping is untouched. Every card now records which
  severity contract produced it, so a `low` from before the change and a `low` from
  after are distinguishable rather than three identical characters.
- **The composition reads both halves.** Each consumed block prints its source's
  standing severity *and* its movement, and all four composition prompts are told
  how to read the pair — a steady delta is never a reason to demote, drop or bury a
  high-severity block, and a block showing no movement call carries none rather than
  an implied "steady". The ranking rule that used to bar anything its unit called
  "holding steady" from leading was corrected in the same breath: that phrase
  describes the delta, never the stakes.
- Absence stays first-class throughout. Until a desk's next run lands under the new
  prompt its heads carry no movement call at all, every render omits the field, and
  nothing anywhere substitutes a default — so the two halves of the change may land
  in either order and an unflipped desk reads exactly as it did before.

**A second, replay-measured revision to the composition prompts' doctrine.**
The four composition prompts were rewritten with sharper, more explicit
language for naming a below-floor unit rather than glossing over the gap.
Replayed against the same reads under both wordings, the revision named 28
of 28 below-floor units, against 17 of 28 under the prior phrasing — with
zero contract violations in either arm, and citations roughly doubling at
zero fabrication.

**The absence-route verify path gets its own system prompt and a fourth
verdict.** Absence claims — "no new sanctions," "not_observed" — were
graded through the same judge prompt as every other claim shape; they now
run under a dedicated system prompt built from their own failure history. A
fourth verdict, "this span is not a proposition," is earn-gated: it can
only fire on shapes the judge has positively learned to recognize, never as
a catch-all demotion. Measured against the same census, false-fail
suppression is +9.5 percentage points with catch rate held, and the
absence screen itself now reads a signal's full rendered body rather than
its title alone. Judge stamp `2026-08-21/1`.

**A model quirk that silently corrupted confidence scores is repaired
before parsing.** One model's output occasionally rendered a confidence as
prose-with-a-decimal — "0. nine" for 0.9 — which the parser read as 0.30
and, in the process, dropped the finding's indicators entirely. The
malformed shape is now detected and repaired before parsing runs, rather
than silently misread.

**The UI's docking library jumps four majors with zero source changes.**
The upgrade is proven, not assumed: runtime tests restore a layout
serialized under the old major version and confirm it still renders
correctly under the new one. Around 200 dead packages came out of the
dependency tree in the same pass. Separately, consult conversations now
survive a layout change or a page reload — the answer is persisted
server-side, proven against a client disconnecting mid-stream, rather than
living only in browser state.

**The optimizer's compile plane is retired to mothball.** Across its full
run history, exactly one real compile ever produced a candidate that
cleared the promotion bar — the plane never earned production trust. The
nightly suite's mask that had been quietly forcing its tests green
regardless is replaced with honest skips, and its watchdog hooks are
removed. The code stays in the tree, not deleted.

## 2026-08-20

**Correctness, measured from the outside for the first time.** Everything
this project has published about verification so far answers one question:
does a claim trace to the evidence we collected? That machinery is silent on
whether the read is *right*. This round asked the other question: does a
country read match reality as a knowledgeable third party would judge it?

Ten country reads (stratified: four high-coverage, three active-conflict,
three sparse-watch desks) were graded by a fresh-context, web-enabled model
that first committed its own reference read — top developments plus a risk
band per dimension — before seeing our product, with the blindness enforced
by staged delivery rather than an instruction not to peek. Every decisive
verdict carries a verbatim source span mechanically checked against an
archived copy of the page: 176 of 176 resolved, zero fabricated. Two
countries were graded twice for inter-rater reliability and a second model
family re-graded them as a check on grader bias.

- **Factual accuracy: 0.893**, weighted over 61 scored assertions (48
  accurate, 13 partially, zero inaccurate; 7 unverifiable disclosed and
  excluded). No invented event, number, name, or place turned up in any
  read.
- **Risk-band accuracy, 44 comparisons:** 7 exact, 15 within one rung, 22
  two-or-more rungs off — and every one of those 37 non-exact calls had our
  product BELOW the reference band; none above. A further 24 of 70
  dimension-slots published "insufficient evidence" where the reference
  found enough to band. The one-directional pattern (never over-banded)
  held under the cross-family check too.
- **Coverage of major developments: 9 of 32 fully present (28%)**; 8 more
  were in the pipeline but dropped before the reader saw them; sparse-watch
  desks covered 0 of 7.
- **Hedging: 12 flat assertions were under-hedged; zero were over-hedged.**
- The dominant failure was architectural, not factual: reads are composed
  from short trailing slices, and developments from earlier in the window
  age out with nothing carrying them forward, so the prose stays true while
  the window's defining story goes missing. The two fixes directly below
  this section — the admissibility horizon and the fortnight ledger — are
  the response to exactly that mechanism, traced per miss before either was
  written.
- Honest limits, stated as plainly as the results: one round, one stamped
  day, ten reads. Grading a read's correctness has an irreducible
  salience-judgment component; nothing here changes production behavior by
  itself. A second, wider round is the next step.

**The unit prompt contract's second revision lands on eight of the nine
bounded desks.** The as-of-line, banned-template-phrase, and
collection-scoped-absence contract from the prior voice pass is joined by a
shared preamble and two fleet-wide repairs: a machine-parseable date now
rides beside every human-readable one in structured output fields (a prompt
that let a model write a prose date was silently dropping the entry
downstream — caught on 2 of 40 sampled cells before it shipped), and the
house read contract gains an explicit line that a trajectory claim's date is
never the read's own as-of line. The ninth desk (a narrative-coordination
unit) is held back because measurement showed it would silence genuine
coordination signal; it stays held after seven measured revision rounds,
with the residual false-positive class traced to wire-syndication pairs
reaching the desk as distinct signals — eight of nine is the intended
stopping point, not a partial job. Landed through direct, byte-verified PUTs
against the live registry rather than a redeploy.

**A rotated credential now evicts its cache immediately**, closing the same
failure shape an earlier fix closed for stack-component changes: rotating a
secret previously kept serving the old cached model handler until the next
container recreate. And `analyst_traces` now records the exact prompt each
run sent the model (capped, with a SHA-256 of the untruncated text) —
previously wired to always store nothing, which meant the self-optimizer's
training-set reader had been silently reading empty input from every one of
187,550 rows. Migration 0186.

**Two more read surfaces get a UI.** The human-review queue for the
journal's self-proposed edits — previously API-only, exercised only by hand
— becomes a clickable panel: two-click accept, a mandatory non-empty reason
to reject, and no optimistic success anywhere (a rejected or conflicting
apply renders exactly what the server recorded, never a green checkmark over
nothing having happened). Alongside it, two panels that had routes and no
consumer: a situation's append-only trajectory, each entry dated by the
finding that established it rather than by when the tracker last ran, and a
contested-claim carriage view (who published a claim first, who followed, at
what lag) that states throughout that it shows publication order, never
influence.

**The judge now sees exactly the bytes the corpus scores.** Evidence shown
to the faithfulness judge was rendered with non-ASCII characters and line
breaks escaped; the check that verifies a contradicting quote was matching
against the *unescaped* original. A quote copied verbatim from what the
judge was shown — the literal rule the system asks for — could never
resolve if it crossed a line break or contained non-Latin script. Measured
over the prior two weeks: 36% of contradiction attempts failed to resolve
their quote this way, concentrated on Cyrillic, Arabic, and CJK sources,
where every character had been escaped. Fixed on both sides of the
comparison; the published faithfulness score is unaffected by construction
(this only affects whether a genuine contradiction registers as one).

**The Live Feed's verification filter now filters where the data lives.**
The verified/judge-status facet fetched a page of results and then discarded
what didn't match, client-side — which silently misrepresented the filtered
population at any real corpus size. It now filters server-side, and the
newly-introduced "unsampled" judge status (below) is a first-class filter
value alongside verified and deterministic.

**The carry.** The window change below stopped the composition forgetting the fortnight;
this stops the *reads* forgetting it. The round's largest attributed failure class —
roughly twelve of twenty-three missed major developments — was an event that happened
in the window's first ten days, *was* in some desk's slice at the time, and had aged
out of every 72-hour slice by the time the reader saw it, with nothing carrying it
forward. The desks were meanwhile printing "mass protest: not_observed" (Argentina),
"State of emergency – not_observed" (Britain) and "no new or tightened sanctions"
(Ukraine) about a fortnight that contained exactly those things. The memory each read
had was one previous 900-character head and an instruction to diff against it.

- **A window ledger carries the fortnight.** Each read now receives a bounded, dated,
  citable block of the verified, severity-tagged heads *it or its desk already
  produced* over the trailing 14 days — one line per unit per day, severest first,
  built at prompt-build time from rows that already existed. No new table, no new
  writer, no new analyst kind. A unit gets its own dimension's record; the country
  composition gets the whole desk's. The block is cited like any other evidence and
  graded against its own rendered bytes.
- **Superseded rows are carried on purpose.** Supersession is a freshness relation,
  not a retraction, and under a head-fold a fortnight's record is almost entirely
  superseded rows — including the Argentine protest head the round's reads should have
  remembered.
- **A read can no longer contradict its own record.** Every ledger line prints its own
  calendar date in the form the prose is required to use, and one clause — stated
  identically at both layers, from one definition — makes a carried event *already
  established, dated, and never news*, licenses standing-state and duration claims
  only where the ledger supports them, and flatly forbids writing that something was
  absent or not observed *in this window* when a ledger line records it. If the current
  slice simply does not show it, the read must say that instead, which is a different
  and honest statement.
- **The situations register stops arguing that nothing is happening.** Two bounded
  repairs to an instrument that was right in shape and wrong in selection. Its
  trajectory now renders the newest *significant* movements — escalations,
  de-escalations, broadenings — with at most one trailing "last checkpoint" line,
  instead of the three same-day "unchanged" checkpoints the hourly tracker happened to
  write last; and a frame is named for its highest-severity member that actually
  asserts something, rather than for whichever absence read landed most recently. Three
  desks were literally titled "No observable shift…" at the time of the round. Names
  re-derive on the next cadence tick; no migration.
- Under the module-size gate, the ledger and the composition's continuity section share
  `data/analysts/window_ledger.py` — the seam the window train's own ceiling note named — and
  the synthesizer's ceiling ratchets down again even though the train added a whole
  carry mechanism.

**Compose over the window.** The 2026-08-20 correctness round found one
architecture defect carrying most of its mass: *a 72-hour pipeline forgets its own
window*. The country composition subscribed to its unit heads under a trailing
**24-hour wall-clock gate**, while the units beneath it fire on an 11-hour cooldown
that deliberately HOLDS on a quiet desk. On 20 August the Burkina Faso units had last
fired 42 hours earlier, the trailing-24h slice was mechanically empty, and the product
printed "No source findings to synthesize" over seven two-day-old heads that carried
the window's major story. Nothing was broken; every instrument was green.

- **The window becomes an admissibility horizon, not a freshness cliff.** The three
  composition descriptors subscribe over 336h (14 days) instead of 24h. Mechanically
  small — the fold to exactly one newest non-superseded head per (unit, desk) already
  existed — and the code half is the honesty a wider window obliges: every consumed
  head now prints its own calendar date and its **age** in the prompt, each run stamps
  `data.head_ages` on its envelope, and the composition is told to state the oldest
  read's age in the prose rather than writing as if everything were composed today.
- **The floor's action becomes visible — its level does not move.** 0.50 stands. What
  changes is that a dimension the floor withheld stops being narrated as an unassessed
  gap: a deterministic **coverage ledger** in the prompt states, per declared unit,
  in-basis / below-verification-floor (with its date and score) / no read at all inside
  the horizon, and the coverage rule forks to match. "Below verification floor," never
  "no read this cycle" — the audit precedent, now a prompt-enforced contract. The
  empty-slice sentence gets the same fork: an all-below-floor desk reads as a
  verification withholding, not an absence of reads.
- **The newest read that cleared the floor reaches the page.** When a unit's freshest
  head fails verification, the newest in-horizon head that PASSED is admitted to the
  basis — dated and labelled as not-the-latest — while the newer failing head stays in
  the weakly-supported section with its own date and score. Showing both is strictly
  more honest than showing neither, which is what happened before (the newer head hid
  the older one behind supersession, and the dimension vanished from both tiers).
- **A cadence-staleness gauge closes the loop.** A new S-1 production-gauge class reads
  the `head_ages` stamp the composition itself published and alarms when a desk's
  newest consumed head passes 34 hours — twice the units' cooldown plus fallback slack,
  so the 42-hour stall pages and an ordinary overnight quiet does not. **The trigger
  policy is untouched**: a stalled desk is surfaced, never silently re-fired. Forcing
  runs on empty slices would spend budget manufacturing "no change" heads; the honest
  fix is that 42 hours of silence stops being invisible.
- Under the module-size gate, the two-tier evidence subsystem and the new window
  machinery live in `data/analysts/composition_window.py`; the synthesizer's ceiling
  ratchets down even though the train added behavior.

## 2026-08-19

- A prior finding's own citation markers, embedded into the next run's
  prompt as-is, could land in a numbering space that pointed at entirely
  different sources in the new prompt. A claim that copied one of those
  stale markers was correctly failed by the judge — the defect was in what
  the prompt showed, not in the model's reasoning. Old markers are now
  neutralized at render time and labeled as the prior run's numbering,
  never a citable handle in the current one.

## 2026-08-16

- **Receipts now record who actually served a routed call, not just which
  model was requested.** When a request is routed to one of several
  providers hosting nominally the same open-weights model, that choice was
  invisible — no field could name it, so nothing could page on it if it
  mattered. It mattered: replaying the same model, prompt, and 94 critiques
  against two different providers of the identical weights flipped 13.6% of
  pass/fail verdicts, including one case in the pass stratum, on a
  verification plane whose stated invariant is zero false passes.
- **The public repository gets its first CI workflow and its first
  CONTRIBUTING guide**, prompted by an outside read of the repo that found
  real gaps: no CI, no contributor guide, and several places where the docs
  had drifted from what the code does. CI runs lint plus four structural
  gates (module-size ceilings, a scan for stub code masquerading as
  finished work, a ban on two libraries in the production path, and the
  strict-test-mode gate) — and says explicitly, in its own output, that it
  does *not* run the ~10,000 tests needing a live database and model
  endpoints; that suite still runs nightly on operated infrastructure.
  CONTRIBUTING.md covers the CLA position, the project's descriptor-first
  design (most new sources or desks are a registration, not code), the four
  gates in detail, and commit conventions.
- The same read caught the README overstating the source catalog ("100+")
  against what actually registers on a fresh deploy (53, with ~64 more
  available behind a manual activation step) — corrected to the real
  numbers everywhere it was stated. And the shipped verification floor's
  code-level default is now 0.50, matching the documented default; it had
  been 0.0 in code with the real value supplied only by the reference
  deployment's own configuration; note the README's language was already
  accurate.
- The evidence archiver's license posture for sources whose license was
  never classified — previously always fail-open (bytes archived anyway) —
  is now a per-source operator option, default unchanged, so a self-hosted
  instance that adds its own uncatalogued feeds can choose to withhold
  archiving until it classifies them.
- Two more shared-state test-ordering leaks rooted in the nightly suite,
  continuing the cleanup from the past two releases; a source whose feed
  serves its publish date as free-form prose (rather than a standard date
  format) had been silently nulling every entry's timestamp, which
  defeated that source's own "gone quiet" detection.

## 2026-08-15

- **The faithfulness judge moves off a single vendor and learns to sample
  its own budget.** A second, independent judge model — reached through a
  router rather than a direct endpoint, preserving the cross-family
  property (a different model family judging the analysis) — joins the
  judge rotation on a rate-limited lane. Because that lane can't carry
  every verification call, a deterministic sampling gate now decides, per
  finding, whether the judge is called at all: the decision is a hash of
  the finding's own id, so it is 100% reproducible on replay with no
  randomness anywhere, and it always includes the higher-stakes analysis
  kinds (country/region/world compositions and the journal) regardless of
  the sample rate. A finding the gate skips publishes a new, honest status
  — "unsampled" — rather than a fabricated pass: it still clears the
  deterministic citation-presence floor and a capped provisional score, and
  spends zero judge calls. The judge-health alarm excludes this population
  from its math, so sampling can never look like an outage.
- Ahead of a planned increase to how much each run can read, a new watch
  gauge tracks GPU-side saturation on the model host (queue depth,
  memory-pressure, and request preemptions) and pages before the queue
  actually backs up. Paired with new per-component latency and spend
  gauges across every model endpoint — hosted judge lanes had been
  receipting $0 toward nothing, uncounted, since they were added.
- The consult plane's per-answer output budget rises from 2,048 to 32,768
  tokens (streamed, so the change doesn't trip provider timeout limits) —
  the old cap had been silently truncating real answers mid-sentence.

## 2026-08-10

**The clearing train — the whole tracked queue, knocked out in one wave.** Four
agents and an evening: every open work item that didn't require an operator
decision shipped together.

- **The deferred repoint finally lands.** Migration 0185 replaces the twice-deferred
  0183: the seventh collision shape (case-differing-namesake stayers occupying mover
  destinations) is solved by computing the mover set to closure before any write —
  set-based demotion passes with a proven bound — plus a transitive name-map closure
  its own replay demanded. Proven on a full copy of live data (idempotent, third-run
  byte-identical) before the train applied it live: 3,218 edges folded onto keepers,
  no errors, seconds.
- **claim_watch 4.1.0**: watched questions that no consumer ever reads now raise
  their own review flags (a detect surface where there was none — armed live); the
  bearing gate demands the signal speak to the thesis's named consequence, not just
  the upstream event; publisher article-id URLs canonicalize (62 duplicate groups
  measured, zero cross-story collapses); a URL-date audit script for the stale-feed
  class.
- **Verify stamp `2026-08-10/1`**: the numeral-fingerprint suppression now also
  withdraws when the claim and quote assert opposite prose directions for the same
  subject — six bounded direction axes, withdraw-only, replay-proven to flip exactly
  the adjudicated case with zero collateral.
- **The nightly suite's last order-dependences rooted**: an alert-scan spike stream
  aging into other files' windows, a UUID-ordered OFFSET coin flip, a three-way
  seed-batch collision — and a fixture time bomb defused the same day it armed (a
  pinned clock crossed a decay floor at 10:48Z; eight tests would have paged that
  night). Both remaining condemned-class suite wipes retired behind hermetic
  fixtures. Three historical failing seeds replay clean.
- **Correction (2026-08-20)**, to the line above: "both remaining" overstated the
  scope. It named exactly two wipes — the band-calibration scorecard fixture and
  the fact-contention facts fixture — and both were genuinely retired that day
  behind hermetic, own-row fixtures; it did not mean "every wipe of this class in
  the suite." Two more of the SAME class were live then and still are:
  `tests/data_pkg/test_narrative_mapper_db.py` and `test_source_track_record_db.py`
  each carry a `clean` fixture that unconditionally `DELETE FROM`s shared tables
  (`signals`, `facts`, `fact_contention`, plus `narratives`/`narrative_echo_edges`
  or `source_track_records` respectively) against the session-scoped test
  database, with no per-test or per-run scoping. Not a regression introduced
  since — simply never counted. Left as-is deliberately: narrowing them to their
  own rows needs the same own-row-proof redesign the two retired fixtures got, and
  removing the wipes without it would manufacture inter-test ordering dependencies
  rather than remove one. Tracked debt, stated plainly rather than left to read as
  done.
- **Findings stop titling themselves with their own date stamp** (the As-of header
  is skipped by the title fallback), and the journal's tool-call leak guard learned
  the JSON-lines transcript shape that slipped past it.

## 2026-08-09

**The solidity train, closing the three-day soak's findings.** A full unattended-days
review found the core healthy and every alarm decomposable — this train fixes what the
review actually found, so the board is clean before any direction decision:

- **The gauge stops crying wolf.** All five false pages had one shape: honest quiet
  misread as deficit. Voided forecasts now count as drained work (the standing false
  CRITICAL); sparse publishers judge against the feed's own `newest_entry_ts` — a feed
  holding nothing newer than our last ingest reads *upstream-quiet with evidence*, a
  feed with fresh unconverted content still pages (`conversion_stall`); and an ACTIVE
  descriptor with zero polls in the window pages loudly instead of vanishing into
  ungauged — the shape a silently-stopped source actually has. Live result: 8 paging
  loops → exactly 1, and the survivor is the deliberate operator-policy page.
- **Two verify-precision fixes under stamp `2026-08-09/1`**: the numeral fingerprint is
  endpoint-aware (matching digits with divergent range endpoints no longer suppress a
  hard fail — replay flips only the adjudicated case, 58 hard fails byte-stable), and
  an unassessable critique publishes *no* faithfulness score instead of a perfect 1.0.
- **Seven unit rubrics parse again** — the voice pass had injected the same
  unescaped-quote phrase into seven eval rubrics at the same column; all fixed in tree
  and re-PUT live.
- **The nightly suite's remaining order-dependence is rooted, not allowlisted**: a
  calibration test helper left two open head scorecards per desk (one phantom alert per
  scan, forever), a seed fixture left a contested-leader pair standing for the export
  round-trip to collapse, and eighteen stale allowlist entries retired. Full-suite
  replays under the exact historical failing seeds now grade PASS.
- **The situation trajectory ledger produces.** Its tracker had been registered but
  never activated; activation went through the audited FSM route, the first run seeded
  all twelve situations, and real transition events landed on the next tick.

## 2026-08-05 (second train)

**The precision train, answering the first clean measurement.** Panel round 4 — the
first acceptance round with a healthy cross-family judge throughout — held for the
fifth time, but for the first time it *named* every cause: pass-side integrity held at
zero on every cut, while failure precision sat near 50% on five identified judge
blindnesses. This train ships the answer, replay-proven against the round's own
14-hard-fail census before deploying:

- **A quote that confirms can no longer refute.** Word-numerals, digits, units and
  percent forms normalize before a hard fail can ground ("sixteen lives and thirty-six
  injuries" is confirmed, not contradicted, by "16 people were killed, and another 36
  were injured"). Replay: hard census 14 → 11 with all six panel-correct fails intact.
- **Zero-claim critiques can no longer score** — bodies whose claims fail to segment
  publish an explicit `unassessable` state instead of a perfect 1.0, floor-graded
  critiques publish PROVISIONAL under a ceiling (22.9% of a measured week was
  floor-only masquerading as adjudicated), and the escalation gate caps on the
  published score it turns out it never actually capped on.
- **Contradiction between findings is computed, not hoped for**: a claim-level check
  across each desk's verified set feeds the composition's Tension section, calibrated
  from 57 false pairs to zero on 1,592 real claims with the Hormuz case still firing.
- **The buried-lead detector costs something now**; severity and salience render beside
  confidence in composition inputs, so consequence has numbers on the page.
- **Three integrity gauges**: judge availability (replays the 26-hour outage as
  critical-and-paging), prompt drift, and state drift between tree and registry.
- The machine-coded-row and continuity-routing bypasses to the judge are closed; three
  citation-marker spellings stop reading as uncited; the trajectory ledger (G-2,
  migration 0184) lands — situations' run-over-run evolution becomes queryable.
- `verify.py` shed 270 lines into three new judge-subsystem bricks under a
  twice-ratcheted ceiling.

Judge stamp → `2026-08-05/1`. Round 5 measures the first day on which every named
failure-precision defect has a shipped fix behind it.

## 2026-08-05

**The convergence train.** Everything the measurement hold protected, landing together
at a clean day boundary (judge stamp → `2026-08-04/1`):

- **Verify residuals (W1-D)**: the four adjudicated xfails go green — the judge sees the
  citing outlet, plain watch-bullet headings grade, `_metadata_dominant` admits the
  verified+residual case, and an enumerated-denial check catches quote-affirms hard
  fails (chosen over a small model by replaying two stamped days: 1/24 fired, exactly
  the adjudicated row, zero false demotions).
- **The voice wave (Phase V)**: every read opens with an as-of line copied from a
  printed slice header (run date + window — the prompt can no longer drift from the
  query that built it); the template sentences are banned WITH a replacement judgment
  shape; compositions become ≤3 paragraphs of argument ordered by consequence with a
  Tension section that covers factual disagreement and a Coverage footer; machine
  internals (microsecond timestamps, internal scores) are barred from prose; absence
  claims carry collection scoping. Eleven descriptor prompts re-stamped in one pass —
  the eight bounded units plus the non-unit analysts, whose model output now parses
  through an enforced contract (a tool-plan preamble can never become a finding title
  again) and whose retrieved evidence renders dated and marked RETRIEVED.
- **The judge reads the article (R1-0)**: `archived_text` now leads the judge's
  source-text chain — previously the analyst read the full archived article while the
  judge graded against a ~545-char teaser on 6,512 measured citations.
- **The graph readers migrate**: `/entities/graph`, entity detail, paths/brokers,
  mining/balance (family-aware), and grounding all read `entity_edges`; the facts
  population backfills (mig 0180); the proposed-edges merge-propagation defects close
  (mig 0181); the parked endpoints are adjudicated without guessing (mig 0182).
- Wiring: cross-target union runs, source-discovery dispatch, the optimizer
  prompt-path convention, and WS auth out of the query string (deprecation window).
  Entity quality: the compass-direction gate and the NER person-class ladder.
- Two regrowth ceilings breached by merge arithmetic were paid at their seams in the
  same train (slice rendering out of `inline_target`, the first judge-subsystem brick
  out of `verify`), both ceilings re-seeded down.

Context for the record: the verify judge's hosted endpoint was down on a billing wall
for ~30 hours spanning the prior measurement day (every critique in that window carries
an honest `judge_status='deterministic'` marker); the acceptance panel ran anyway,
measured the floor, and held for the fourth time. This train ships the fixes the panel
could not measure; round 4 measures the converged system on the first clean judge day.

## 2026-08-03

**Wave 1 of the residuals program.** The engine review's remaining ❌ items, built by five
parallel agents against a pinned base. Four slices integrated and deployed in one train;
the fifth (verify residuals, incl. the `2026-08-04/1` judge stamp) is built but holds for
the acceptance panel's third round, so the stamped measurement day is never truncated
mid-flight.

- **Correctness has a real denominator.** The correctness scorer read a dead table with
  one stale row while the operator's gold-set verdicts surfaced nowhere. The gold-set
  arithmetic now lives in one module shared by the scorer, the eval scoreboard, the
  scorecard fold, the v3 route (`/v3/eval/correctness`), and GEPA's gate — displayed as
  its own axis with honest tiny-n labeling, structurally barred from pooling into
  faithfulness calibration. Every faithfulness aggregate now splits by
  `judge_pipeline_version`; prior judge populations get their own annotated readout,
  never summed into the current stamp's headline.
- **A hung activate degrades instead of freezing the plane.** Actor turns run under a
  deadline with a heal breaker (the 08-01 outage mechanism); reconciler per-actor heals
  time out to skip-and-retry. Container logs now ship to rotated host-side files that
  survive recreates, and `analyst_traces` records tool arguments (bounded,
  secret-redacted) — the prior justification for not recording them cited a table column
  that does not exist. `loop_watchdog.sh` is retired: never correctly wired, and its
  remediation (force-recreating the Dapr scheduler) was the exact SIGKILL its grace
  period exists to prevent.
- **Three wired-but-never-fired limbs are real.** The journal gets a bounded PROPOSE
  phase after narration — it was previously offered the propose tool only before it had
  reasoned and punished for using it after (zero invocations ever; five warranted
  proposals found in a six-day replay). `review_flags` fires now that the hypothesis
  consumption edge its walk starts from is actually written. `contention_flip` compared
  disjoint id populations (fact ids against signal ids) and could never match; bridged
  along the substrate's own lineage, 1,243 of 2,152 contention groups become walkable.
- **The built-but-unbound set is adjudicated.** Nine draft descriptors bind the unbound
  analyst kinds and source adapters (zero live actors until individually activated); one
  schema field referenced since birth but never declared is declared; and
  `cross_source_dedup` is struck from the "unbound" list — it was live all along with
  143k successful runs.

Migration 0170 (correctness-axis promotion, comment-only). Judge pipeline stamp
unchanged at `2026-08-03/1` — the bump ships with the gated verify slice.

**Wave 2, the same night.** Four more slices, one train:

- **The graph is walkable.** `/graph/ego` + `/graph/edge/{id}` over `entity_edges`
  (anchored 1-hop ego, 5.5 ms on the highest-degree node, family/confidence/time
  filters in the index condition) and a Graph Walk panel in the workstation shell:
  expand-on-click, edge-evidence detail, families visually distinct — relation solid
  and polarity-coloured (the only family with a real signed distribution), reference
  dashed, cooccurrence faint and off by default so co-mentions cannot bury claims.
  No depth parameter by design: every hop is a fresh anchored ego.
- **The corpus can forget.** OpenSearch had no delete path and 41.5% of it (75,871
  docs) pointed at purged rows — served verbatim, contrary to the review's assumed
  mitigation. Now: transactional tombstones (migration 0175), a retention drain that
  re-verifies each row is gone before deleting, gauge-visible backlog, and a dry-run
  backfill for the historical population. Migration 0176 soft-closes the 200
  capital-metonymy facts, their 171 value rows and 40 stranded contention groups —
  and the contention arbiter gains the metonymy gate without which the cohort would
  have rebuilt itself.
- **A rename can no longer silently change an analyst.** Descriptor string
  resolution fails loud at all three layers (boot, registry validation, runtime
  dispatch). The live audit: of 339 string references, 17 are real module-path
  reads — all prompts — and two dead references were found and fixed in-tree,
  including a prompt package that never existed. The `registry/api.py` kernel
  (bearer gate, deps bundle, sunset stamp) moved to a leaf module with re-exports,
  ceiling ratcheted down, byte-identically verified.
- Canonicalizer variant folds verified live and instrumented for the first time
  (26 www keys, 823 wire-revision titles folded); claim_watch's dedupe counters now
  say so.

## 2026-08-02

**The engine review, and the hardening it demanded.** A six-plane component-by-component
review of the entire engine (acquisition, substrate, analysis, coherence, products,
runtime) ran against the live system, alongside a code-organization analysis and a
pre-declared acceptance readout of the 07-31 verify-path fixes. Its central finding: this
engine's characteristic failure is **silent absence, not error** — a census of twelve
capabilities that were wired, green, and had never run, or died traceless. What deployed
the same day:

- **Dead runs now write a trace.** `analyst_traces` was `status='success'` on all
  186,435 rows ever — a run that died wrote nothing, which is how two incidents hid.
  Every started run now lands a row; failures carry the error class, retry bucket, and
  attempt count.
- **The 08-01 outage class is closed at the schema layer.** The strict-mode wire-string
  coercion fix is generalized to every identity model (10 enum fields across 6 classes
  plus 9 stack families), with a drift guard that fails the suite if a future
  strict-mode model grows an uncovered enum field.
- **The LLM heartbeat now completes something.** The old probe accepted a `/v1/models`
  200 from a server that hadn't completed a request in 19 hours. The new probe demands a
  real completion every 10 minutes and a long-context needle hit hourly; an empty 200
  counts as failure.
- **Cold activation is a deploy gate.** A smoke script forces one unit run end-to-end
  after every deploy and asserts the trace row, not the transport 200 — the exact check
  that would have caught 08-01. (Its own first live run found a bug in itself: a
  whitespace strip mangled the poll watermark and misreported a success as a failure.
  Fixed; failure-path poll errors now surface instead of masquerading.)
- **The scheduler's OOM cliff is gone.** The reminder store's etcd sat at 91% of its
  container memory limit, pinned by default revision retention — one growth step from
  taking down every reminder in the system. Limit raised, retention made time-based,
  history compacted and defragmented: 380 MB → 38 MB with 335 live keys.
- The verify-path acceptance readout **failed its own pre-declared gates** and is
  recorded as such: the pass-side fixes adjudicated clean, but the new
  absence-contradiction check fires on off-target and machine-coded rows at ~46%
  precision. A precision train landed and deployed the same day — target-scope filtering
  on violators, body screening on composition slices, machine-coded-row exclusion,
  carve-out clauses handed to the adjudicator, persisted hard-fail quotes, a
  refutes-vs-resolves check on demotions — measured against the live ledger to remove 20
  of the 27 false hard fails while keeping the genuine catches. The gate still does not
  declare until a re-run passes on a fresh day of stamped verdicts. Honest measurement is
  the product; this entry is part of that. (The train also cleared one panel finding as a
  false alarm: the "citationless under-fires 12×" claim was a mis-projection — the audit
  queried one JSONB level too high; the guard had been honest all along.)

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
