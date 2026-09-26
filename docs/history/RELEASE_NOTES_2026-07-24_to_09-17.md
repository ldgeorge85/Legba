# Changelog

Legba's public history is intentionally squashed — each release lands as a single commit
on `main` — so this file is the release record. Entries are dated (newest first) and
written against the docs as they shipped; [docs/STATUS.md](../STATUS.md) remains the
always-current truth-in-labeling table.

## 2026-09-17

**The builder ran perfectly and brought back nothing, and the reason was four
characters.** Once every build terminated on time and named its own failure, the
remaining question was yield: nine developments for Israel, five for Ukraine, one
for Argentina on its third try, none at all for Australia. The Australia run was
read call by call, and it was not lazy or confused. It searched thirty-six times,
fetched eight pages, and ended with one usable page and an empty reference — and
almost every step of that waste was something the harness could have prevented
and did not.

It had been *shown* a Guardian article about the IMF downgrading Australia's
forecast, published that morning, exactly in window. It asked to read the same
address without the `www.`, which is a 404 of two hundred bytes. It did that
twice. Four more of its eight page reads went to Reuters and Bloomberg, two
publishers this system has measured, repeatedly, that it simply cannot read —
they were already being stripped out of search results for that reason, and the
model reconstructed the addresses from memory anyway, because nothing stopped a
read it had not been offered. Six of its searches carried a `site:` filter,
narrowing an already-filtered search onto hosts whose results are discarded. When
it did read a page, it wrote a proper note about it — and the loop threw the note
away, because the note arrived in the same breath as the next request. And when,
four minutes in, it wrote out the finished reference in full, the harness replied
"Noted. Continue." and bought thirteen more searches, because the reference did
not happen to be preceded by the words the loop was listening for.

**So the harness now keeps its own record of every page it reads, and builds the
final request out of that record.** After each successful read it files the
address, the publisher, the page's own publication date, whether that date falls
inside the fortnight under judgment, and the opening of the text. None of this
needs the model's cooperation and none of it can be lost. When the time comes to
write the reference, the model is handed that file: here are the pages you read,
here is which of them you may use and which fall outside the window and why, here
is the text you may quote. The old instruction — "commit from the notes you
have" — had been addressed to a model with no notes.

**An address that no search offered is no longer read at all.** The model finds
through search; the harness decides what may be opened. Nor is an address read
from a publisher known to be unreadable. A misspelled address is silently corrected
back to the one the model was actually shown. Search queries that try to
pre-filter — a `site:` operator, a date typed into the query, the name of a
publisher this system cannot open — are rewritten before the search runs, with
the reason handed back, and the instruction now carries a handful of worked query
patterns for each subject area. A build that searches more than three times per
page read is told to stop searching. None of these cost anything: a refused read
does not spend the budget it was protecting.

**And there is a new name for the failure that used to hide.** If the model is
handed a page that is in window, quotable and dated, and still writes an empty
reference, the run is recorded as exactly that rather than as "produced nothing".
The three outcomes now point in three different directions: nothing to fence
(look at the search plane), everything rejected (look at the fences), and an
empty commit with material in hand (look at the model). A live re-run of the same
Australian fortnight on the fixed builder spent thirty-five web calls instead of
forty-four, rewrote seventeen queries, and left three notes behind for the next
attempt instead of none — and still committed nothing, honestly, because not one
page it could reach carried a publication date inside the window. That is a thin
fortnight, and it now says so in those words.


**One country was holding up all thirty others, and the queue could not see
it.** The reference builder serves a roster of thirty-two countries an hour at
a time, and it chose which one by asking a simple question: whose reference is
oldest? That question has a blind spot. A build that fails writes nothing down,
so a country the lane *cannot* build never gets any less overdue — it stays at
the front of the queue for ever, and takes every hour the lane has.

That is not a hypothetical. Argentina is largely unreachable to this lane: of
sixteen pages it tried to read, three were usable, the rest sitting behind
challenges and paywalls. Two consecutive builds went to it and produced nothing.
The second one failed *correctly* — it stopped inside its new time limit and
said why, which was the previous day's fix working exactly as intended — and
that was the moment the real problem became visible. Nothing about failing well
moves a country out of the way. The next hour would have picked Argentina again,
and the hour after that, and thirty other countries that were perfectly
buildable would have waited indefinitely for a turn that never came.

**So the queue is now ordered by when each country was last *tried*, not when it
last succeeded.** A country nobody has attempted goes first; after that, whoever
has waited longest since their last attempt. Oldest-reference-first survives as
the tie-break, so a fresh roster behaves exactly as it did before. And a build
that produces nothing buys that country a rest — a day by default — so the hour
goes to the next in line instead. The one that was passed over is not hidden: it
stays on the receipt with the reason it was skipped and the time it comes back.

**Nothing new had to be stored to do this.** The builder already writes a
receipt for every hourly tick; it now records, in three fields, which country it
put on the plane and how that went. The next tick reads its own receipts back.
No new table, no migration, and a lane that cannot read its own history falls
back to the old ordering rather than refusing to run.

**A country that fails three times running is named rather than dropped.** After
three consecutive failures it is flagged on the receipt as one this lane cannot
build, and the operator top-up tool will list it on request, along with whether
there is an existing draft to improve or whether the country has nothing at all
yet. It keeps being retried, for ever, at the slower cadence: "we cannot fetch
this country" is a statement about today's blocked publishers and today's
licensing, both of which change — and a country quietly dropped from a roster is
how it disappears from a correctness programme without anyone deciding that it
should.

**A build that cannot finish now stops, says so, and keeps what it learned.**
The reference builder's first unattended tick spent a quarter of an hour and
four and a half million tokens on one country and produced nothing at all. The
shared web budget it draws on was already spent when the tick fired, so
essentially every search and page fetch came back refused — and the loop had no
way to notice. Its only route to writing anything down was to exhaust its
budget of web calls or for the model to volunteer that it was finished; a model
that kept looking simply kept looking, and each attempt re-sent the entire
conversation to a machine that had nothing new to add.

The cost did not stay inside the lane. The run ate the analyst's whole daily
token allowance, so the next hour's tick was paused by the budget guard for an
hour — silently, with no log line — the hour after that was still inside the
pause by under a second, and the liveness watchdog reported the analyst as
having gone dark. It had not gone dark. It was out of budget, which is the
budget working, and the alert sent the reader to check four things that could
not possibly explain it.

**So a build is now a bounded thing: seven minutes, 1.2 million tokens, eighty
web calls.** At seventy per cent of whichever runs out first the tools come off
the table and the model is asked, in one final turn, to write the reference from
the notes it already has — anything it cannot anchor to a page it actually
fetched is left out rather than guessed at. Past the wall clock the build ends
whether or not anything was written. And if two searches in a row come back with
nothing, it stops gathering immediately, because the third will not answer
either. The token ceiling is a third of the day's allowance, so no single build
can ever take the lane down again.

**Two receipts that used to say the same thing now say different things.**
"Everything was rejected" was being printed for runs where nothing had been
proposed in the first place — a table of seven zeros under the word *rejected* —
which sent a reader to inspect the checks when the fault was the search plane.
Those are opposite diagnoses and they now have opposite names, each carrying
what makes it actionable. A run that writes nothing also keeps the model's own
working notes, bounded and dated, so the next attempt on that country starts
from the leads rather than re-discovering them; a run that succeeds throws them
away, so a new fortnight is never seeded from the last one's guesses. And the
watchdog no longer reports an analyst as stalled while it is visibly paused for
budget — for one pause window only, so being out of budget every day is still
something you get told about.

**And the two budgets that made the failure inevitable are now sized for the
job.** Every part of the system that reads the open web was drawing on a single
allowance of 120 page-fetches an hour. One of them — the auditor that checks
published work — routinely spends all 120 on its own, which is why the builder's
tick found nothing left. It now has its own hourly allowance that nothing else
can touch: one whole build plus a quarter again, and still smaller than the
shared one, because the mechanism used deliberately cannot widen a limit, only
move it to a private account. The shared allowance is unchanged — raising it
would only have given the auditor a bigger hour.

The daily word budget was wrong in the same way. Thirty-two countries on a
weekly rebuild means five builds a day, and a build is now capped at 1.2 million
words-worth of reading: six million a day against a four-million allowance, so
the lane was guaranteed to pause itself roughly every day no matter what else
was fixed. It is eight million now, with a third to spare, and a test asserts
the three numbers stay consistent — so a change to the rebuild schedule, a
bigger country list, or a more generous per-build cap fails the build rather
than surfacing as an unexplained hour of silence days later.

Measured live on the same country that failed: **332 seconds and 543,000 tokens
against 928 seconds and 4.5 million**, a reference committed inside the cap,
$0 on any paid API.

## 2026-09-16

**The thing the correctness grader measures against now builds itself, for
nothing, on hardware we own.** The grader shipped able to ask whether a read
is true and unable to obtain the reference it asks against: that table was
populated by hand, once, for one country. It is now a scheduled job — one
independent reference per country per fortnight, built from the open web
without ever reading our own substrate, because a reference derived from the
reads it grades is a self-consistency check wearing the wrong label.

**The honest part is what it does not claim.** We measured the obvious
design first — let the model read the web and write the reference — twice,
on the same instruction and the same web. It cleared verification outright
and failed substance: 79% of its quotes were exact against the page they
cited and every dimension carried two developments, but it found only a
third of what a knowledgeable reader would call the fortnight's major
events, and it published one development that was simply false — a
two-year-old ministerial appointment dated off the website's masthead. A
thin reference under-covers. A reference carrying a false development marks
correct published work as wrong, which is worse than having no reference at
all.

So the model finds and quotes, and **code decides what is accepted**. Six
fences run after the model commits, over archived text it cannot reach: a
development may cite only a page this run actually fetched; a host that has
refused twice is refused for the rest of the run; every fetch must be
followed by a written note before another tool call; the page's own
machine-readable publish date must fall inside the window, and a page that
declares none cannot carry a development however good the quote; discovery
is filtered to official, wire and record-press sources, with lower-tier
sources admitted only for a dimension the record genuinely does not cover
and marked where they are; and every decisive quote must be an exact
substring of the archived page. Anything that fails is dropped and counted
by name.

**Where it is thin, it says so.** A dimension left with fewer than two
surviving developments is stamped in the column the grader already reads, so
the free lane under-claims rather than mis-grades. Closing the remaining
substance gap is an operator-triggered lane and nothing in the platform
schedules it or can start it: a script writes a packet listing what is
already established and asking only for what is missing, and the merge back
re-fetches every addition and puts it through the identical fences. A paid
lane gets no easier ride than the free one.

**It costs nothing, and the schedule is derived rather than declared.** The
core plane only; there is no paid route in the analyst's path to gate, which
is why it carries no spending ceiling — the ceiling is the architecture.
Tokens in, tokens out and wall time ride on each reference's own header, so
the cost of a reference travels with the reference. One build is about eight
minutes, so the tick fires hourly and takes the single most overdue country:
the roster staggers itself, a country joins by appearing in it, and most
ticks build nothing and cost one query. No new migration — the existing row
shape was right. Behind `LEGBA_REFERENCE_BUILDER_ENABLED`, off, doing
nothing at all.

---

**The platform can now ask whether a read is TRUE, not only whether it is
grounded.** About 84% of this fleet's LLM calls are the system grading
itself, and not one of them can see a tower that is internally immaculate
and factually wrong. Run by hand over Israel on 2026-09-16 — 8 desk heads
plus the composition, 54 claims, an independent reference built blind to our
substrate, three grader families reading a three-label rubric — the
instrument found two claims we had published that day that the reference
contradicted: a desk asserting a "deadlock over list submissions" on the day
the candidate lists were filed, and the composition asserting "no price
spikes" while crude was over $100. The desk had hedged the second claim; the
composition un-hedged it.

That instrument is now a registered job. Per country per day it resolves the
reference that is current at the stamp, freezes the desk and composition
heads under the same as-of predicate, segments them with the shipped
segmenter, builds one byte-identical leak-scanned packet, and writes two
numbers per bounded unit: `correctness_share` over the claims the reference
bears on, and `coverage_share` over everything the unit said. Neither is
readable without the other — a correctness share of 1.0 at a coverage of
0.05 is one claim confirmed and nineteen the reference never touched — and
both are NULL, never 0.0, when their denominator is empty. Where no two
grader families agree the claim is published `split`, counted in the
coverage denominator and in no numerator, never tie-broken.

**The rubric that made it possible was the third one.** The five-label
rubric with its tiers and its precedence ladder produced pooled three-family
agreement of 0.42 against a 0.75 bar; the round's verdict was
instrument-limited rather than a finding about the reads. Removing the tiers
and the ladder — contains, contradicts, silent, one question — measured
0.8444 on a fresh draw with every pair above the 0.70 floor. The rubric
ships as a data file whose sha256 is checked at import, because that digest
is the identity of every number written under it, and the job refuses to
publish anything unless a passing calibration row covers both that digest
and every model id it would use. Repoint a grader at a different model and
the instrument stops until somebody re-measures it.

**It costs nothing by default.** `LEGBA_GRADER_DAILY_CEILING_USD` defaults
to $0, at which the two paid families are never called — not resolved, not
attempted, not estimated — and the whole thing runs on the self-hosted core
plane with every claim flagged `single_family`, because a one-family label
is a weaker label and hiding that would be the dishonest part. Above zero
the guard refuses the call that *would* breach rather than reporting the
breach afterwards. The ceiling is an environment variable and not a
descriptor field: raising the amount of money a job may spend should take a
deploy step with a human at the other end, not an API call. The whole train
ships behind `LEGBA_CORRECTNESS_GRADER_ENABLED`, off, writing nothing at
all.

**What it cannot claim, stated on its own receipt.** Correctness against one
reference is not correctness against the world: a true claim the reference
never mentions reads `silent`. The reference builder itself is not in this
train — the grader can read `unit_references` and can never write one,
because a grader that could build its own reference could close the loop on
itself. With no reference for a country it records `no_reference` and grades
nothing, which is the honest outcome and not an error.
**A blocked page stopped reading as an empty web.** The evidence archiver's
wall detector already knew the phrasing of a bot challenge — "are you a
robot", "checking your browser before accessing" — but every pattern sat
behind a 500-character gate. That gate was honestly derived: a live audit of
the archived text had measured the longest pure-boilerplate body at 499
characters and the shortest genuine article containing one of those phrases at
852. It is simply out of range for a modern interstitial. Measured this week,
Associated Press's Cloudflare page is 5 486 bytes and the Times of Israel's is
12 582. Both sailed past the gate, so a publisher's refusal landed as a thin
extraction, and a thin extraction reads downstream exactly like a page with
nothing on it. A desk could not tell "this publisher blocked us" from "nothing
was published" — the same false-absence failure the search plane's degradation
reads exist to prevent, and a worse defect than the 403 itself.

It is closed now, under one name that travels the whole way down:
`blocked_by_challenge`. A challenge is classified at three tiers — the
extracted text with no length cap at all, the raw body for the tells that
never survive extraction (DataDome's inline config object, Cloudflare's
challenge-platform include), and the HTTP status for the case where the body
was already discarded by `raise_for_status` inside a streaming fetch. The
archiver counts it, names it in the run's receipt, and records a stored
interstitial as a new `evidence_archive` status, `blocked_challenge`
(migration 0196), with the tell in `last_error`; the bytes stay archived,
because they are the evidence of the block. An edge 401/403/429 stays `failed`
so it remains retryable. `web_evidence` counts it on the tool output, gives
the hit a new depth reason `fetch_blocked` distinct from `fetch_failed`, and
stamps the tell on the landed row. The licence gate did not move: a blocked
host with no affirmative `license_class` is a teaser exactly as before, which
is the operator's decision and not a fetcher's. On a 24-url sample drawn from
the archive's own failure rows, eight urls — a third — now report as blocked
that previously reported as nothing.

**Browser-fingerprint impersonation shipped behind a flag, and the
measurement says leave it off.** `LEGBA_FETCH_IMPERSONATE` (default `off`)
switches the page-fetch client for `web_evidence`, the archiver and
`web_fetch` from the SSRF-guarded httpx client to a `curl_cffi` adapter
presenting a Chrome TLS and HTTP/2 fingerprint. It is fingerprint only: the
identifying `legba-research/1.0` User-Agent is unchanged, because the
robots.txt decision is computed for that token and must describe the agent in
the publisher's log. robots.txt itself is still fetched with the plain client.
The SSRF guard is an httpx transport and cannot travel, so it is re-expressed
on the new path — the host asserted before the request, redirects switched
off, and every hop hand-walked and re-asserted — and the flag-off path is a
single statement returning the client the call site always opened, with zero
existing test files edited to keep it green.

Then it was measured, twice, and it does not work. Over 24 urls and 13 hosts:
not one of the eight challenge-class urls became fetchable, and two that the
plain client fetched at 200 came back 403 under the fingerprint — a
UA/TLS-consistency refusal, which the design anticipated and accepts as the
publisher's answer. Reuters, Associated Press, the Times of Israel and Haaretz
are unmoved; track R1 measured the same thing independently. These are
IP-reputation and real JS challenges, not fingerprint blocks. The flag stays
off and the machinery ships as a tested negative result rather than as a fix.
One loose thread is worth naming: four `aa.com.tr` urls that httpx cannot
complete at all fetch cleanly under curl_cffi, which is an HTTP-stack
incompatibility rather than a block, and a per-host question for an operator
rather than a reason to flip a global switch against two regressions.

**The first live run graded nothing, and both reasons are now fixed.** The
deploy was proven end to end the same evening — descriptor active, actor
activated, a forced run dispatching to the handler and reading its
calibration — and the number it produced was zero units, which is exactly
what an instrument should say when it cannot measure. Two defects sat under
that.

The first was a channel that did not exist. The forced-run script sent the
stamp and the target list at the top level of the actor's method body,
alongside `trigger_kind`; the actor reads its own contract keys from there
and merges a handler's per-run parameters from a nested `options` object
only. Unknown top-level keys are dropped without an error and without a log
line, so the run stamped the wall clock and swept the wrong population while
reporting success. Parameters now travel nested, and the regression test
drives the real actor method with the body the script itself builds,
asserting on the `as_of` column of the row that lands rather than on a
mapping inspected in passing. The script also exits non-zero now when the
sweep wrote no unit numbers: HTTP 200 is the sidecar saying the actor ran,
never the instrument saying it measured anything.

The second was reference currency, and it would have made the nightly run
ungradable forever. A reference is built for a window that ends at its own
T0, and every read it grades lands after that instant — so requiring the
stamp to fall inside the window meant a reference built at 19:30 matched
nothing at 22:32 the same evening. A reference now stays current until a
newer one for that target supersedes it or it ages out of a grace window
(`LEGBA_GRADER_REFERENCE_GRACE_DAYS`, seven days, environment over
descriptor for the same reason the spend ceiling is), and the age it carried
— `as_of` minus `window_end`, fractional, floored at zero — is written on
every row and printed on every receipt. "88% correct" and "88% correct
against a reference that closed six days ago" are different statements, and
only the second one is true. Past the grace the target is named
`reference_stale` rather than `no_reference`: one says the reference builder
never reached this country, the other says it did and then stopped, and they
are different things to go and do.

**The number reached the read, and the composition can now be made to obey
it.** Two things that were owed the moment the grader started writing rows.
The first is a badge: beside each bounded unit's read — every desk card on a
country, and the cited composition card itself — the latest measurement now
appears as `correctness 90.9% (10/11) · coverage 24.4% (n=45) · as of
2026-09-16`, with `single-family` and `reference stale (2.1 d)` appended when
they are true. It is a composable element inside the panels that already
exist, not a new screen and not a new panel kind, and the string is composed
server-side on a new SELECT-only route so that exactly one place in the tree
decides how a null share prints. Clicking it opens the claim ledger
underneath: every grader family's label unpooled, the adjudicated outcome,
the decisive span each family quoted, `contradicts` first — because a claim
the reference refuted is the reason anyone opens it. What it will not do is
show a number it does not have. A unit nobody has graded yet renders nothing
at all; a country with no reference says `no reference`. An empty space is the
truthful render, and a `0%`, a dash or an optimistic `100%` would each be a
claim we have not earned.

**The composition gate is the spine's own rule, made executable and left
off.** *"Authority climbs only as far as the verification underneath it
reaches."* Behind `LEGBA_COMPOSITION_CORRECTNESS_GATE`, default off,
`country_composition` composes only over desk units whose latest number
exists, is not single-family, and clears both an operator-set correctness bar
(default 0.8) and an operator-set coverage bar (default 0.2). Both bars,
because correctness alone is gamed by silence: on 2026-09-16
`economic_coercion` measured 100% correct on one decided claim out of seven,
and a correctness-only gate would have ranked it above a desk that was right
about two of three. A unit that fails is **quoted, not dropped** — demoted
into the periphery tier the tree already has, still visible, still
lineage-linked, hedged and excluded from the load-bearing basis. A desk that
silently vanished would make a composition over three units look identical to
one over eight. The payload records per unit whether it was `composed`,
`quoted` or has `no_number`, with the numbers and the bars in force, so the
render can say why; and when the gate empties the basis entirely the finding
says the gate withheld the reads rather than claiming there were none. The
thresholds are environment variables, not descriptor fields, for the reason
the grader's ceiling is: a bar that decides what the platform is willing to
assert should move at a deploy step with a human at the other end. Off, the
composition is byte-identical — no query issued, no row marked, no key
stamped — and a flag-off proof on the real `READ_SLICE` and `_run` entry
points is what says so.

## 2026-09-10

**The journal's connective layer got an audit, and the audit found the
aperture.** An outside reading of a journal entry that scored 0.93 on
faithfulness found every error in a transition sentence: "the same conflict"
joining a Ukraine strike to Iran-war casualties, an evaluative "brightened,"
three inferences stacked on one signal, and a paragraph narrating GDELT event
codes as if they were reports. Checked against the rows, all of it held. The
graded unit is a cited span, and the connective inside it is exempt by
design, so the facts were scored and the relationships never were. A
fourteen-day census then measured the class honestly: about one true
connective fault per twenty-five entries, on a detector that ran at 13
percent precision. That number deferred the relation-claim machinery and
built the rest. Live since 2026-09-10: event-coded rows are labelled as
instrument output in the writer's window and flagged if narrated as
reporting; scheduled products are labelled routine and flagged when a
change connective rides on them; the writer's register rules name the three
failures; a hand-copied reference typo is repaired against the run's own
window instead of resolving to nothing; and the reader labels a signal
reference as what it is rather than "unknown."

The larger finding was selection. The journal's "24-hour slice" was the
newest 360 rows by fetch time, about three hours of a 3,570-row day. The
day's third-largest story, twelve governments coordinating restrictions on
settlements, had one of its 77 rows fetched. Two changes now run together
behind flags: a salience-aware fetch leg for the journal only, which widens
the span to the whole day, and a cluster-first window that groups the pool
by shared anchors and geography and leads with the largest objects, so the
voice sees the thing rather than its fragments. Flag-off is byte-identical
for every analyst.

**A polity-matching defect in the desk routing.** The whole-token fix
(merged `2e28a7bf`) closes a squeezed-substring match that read "US" inside
russia, australia, and jerusalem. On the US desk, 20 of 30 by-name slots had
gone to rows naming no American anything, mostly the East Jerusalem
consulate story, displacing 20 that did. The same match had led the frame
gauge to refuse "United States" on 38 desks as boilerplate, and had deleted
the US from Australia's and Russia's own candidate sets as their home
country. Surfaces of three characters or fewer now match as whole tokens.
Everything longer is character-identical to before.

**The researcher completed its first assignment.** After the 09-08 planner
fix the runs still died at the final write, because the synthesis turn
reused the gathering prompt and the model ended with a tool object five
times in six. The closing turn now has its own instruction, the coercer
reads a finding that follows a tool object, hard failures persist their
prompt and completions, and a failed run releases its claim. On 2026-09-10
03:37Z the dispatched run wrote "Coverage gap: Palestine not reflected in
Israel watch signals" with eight citations.

## 2026-09-08

**The verify judge had been failing silently for a day and a half.** The
free-tier judge route answers some requests with HTTP 200 and a 502 inside
the body ("upstream service temporarily overloaded", no choices). The
client retried on status codes only, so it never retried these, and its
accounting booked them as successes: 599 of 2,019 judge calls since 09-07
04:00Z, about 30 percent. A finding with two judge partitions lost both when
either came back empty, so by 09-08 two thirds of verify rows were graded on
the deterministic floor with a provisional ceiling. Fixed at the judge's
transport (merged `095e63e4`, deployed 2026-09-08 ~20:53Z): bounded retry
with jittered backoff that honours Retry-After, receipts naming the
attempts and the router-versus-inner statuses on every verify row, and a
graded partition now survives an empty sibling as `partial`. The provisional
ceiling applies only when no judged verdict exists. This is one graded-
behaviour change, so the judge stamp moved `2026-09-06/1` → `2026-09-08/1`;
rows differ from the prior stamp only where one partition was empty. Model,
route, rubric, and prompts are unchanged; the free slug has a single
provider endpoint, so there is nothing to fail over to at zero cost. The
same blindness sat in the shared LLM client under every route, latent
elsewhere (zero occurrences outside the judge route); the client now
classifies a router 200 carrying an error envelope and no usable choice by
its inner code inside its existing retry loop, and the accounting records
the inner status beside the router's (merged `a0e7ceb6`).

**The Assessment's judge now sees what its voice sees.** The 09-07
coverage roster put every declared unit in front of the voice while the
judge's evidence map listed five and "and 28 more"; the voice relayed names
the judge could not find and was hard-failed for it. The map now renders the
roster whole and names the carried set (merged `580d6704`, deployed ~20:04Z);
in replay the count of unsupported names written fell from 90 to 0 on the
rostered spines while a byte-identical control held steady. A companion
change puts each declared tension's statement and both heads into the map
(merged `314bec68`); the replay was honest that this alone did not lift
tension-sentence support, because the spine carries no human names for its
units and the voice mistranslates slugs. The names now travel: every
declared unit's descriptor name rides beside its handle in the roster,
tension, and drop lines the voice reads and in the judge's map (merged
`82486c32`); in replay the mistranslations went to zero and tension relays
went from two of five supported to four of four, while the voice's new
appetite for enumerating readable names showed up as a rise in instrument
prose, which is rubric work for after the proof round.

**The researcher now executes its assignment.** The first dispatched
coverage-floor run emitted its tool call in a flat envelope the gathering
loop could not read and then wrote that object out as its finding; the
claim it placed on the question then hid the question from the next two
runs. Fixed and deployed ~19:08Z (merged `51b7dbee`): the loop reads the
live envelope shapes, refuses a narrated action on an assigned run and
leaves the gap open, stamps the assignment's hypothesis onto the web call
so landed rows carry the target's geography, and re-renders a
claimed-but-unanswered question to the analyst that owns it.

## 2026-09-07

**The reads were audited by an outside reader, and most of what it found was
true.** An external review of the 09-06 regional and world compositions
found citations that pointed at the wrong desk in every region, regional
"carries" that promoted the weakest thread, same-day events the platform
never saw, and a world assessment whose lineage was a single row. Each was
run to ground against live rows and fixed at the producer (merged
`99944969`, deployed 2026-09-07 ~05:56Z). Regional rollups now number their
citations in render order, and the read-side remaps history. A region
carries the block with the highest cited mass and labels why
(`carry_reason`: earned_lead / by_mass). The country crown counts leads
against a floor that ignores headings written as a negation ("No sign
of…"), so an absence sentence no longer wins the lead. The world read
gained a coverage roster (`## Coverage`) over every desk it could have read,
tension counters that say what was carried and why, and stopped letting
regional rollups leak into its periphery. Desk slices admit stories that
name the desk's polity at slice time, not only stories tagged to it. On
the first flag-on cycle the country reads went from 5 earned leads out of
32 to 17, then 22 the following day; regional citations resolve
section-for-section.

**The width plane started deciding.** The external grader's window is now
based on the evidence it cites, with a 96-hour grace, instead of the heads'
production spread; the decisive ledger went from 1 row on 09-06 to 62 in
the following 36 hours (59 supported, 3 contradicted).

**Coverage gaps became assignments.** When the coverage floor breaches, the
researcher's next run receives the open question as its assignment (the
same run, no second scheduler). The first assigned run did not answer it:
the planner emitted its tool action in a flat envelope the gathering loop
could not read, then wrote that same object out as its finding, and the
claim it had placed on the question hid the question from the next two
runs. Fixed and deployed 2026-09-08 ~19:08Z (merged `51b7dbee`): the loop
now reads the live envelope shapes, refuses a narrated action on an
assigned run and leaves the gap open, stamps the assignment's hypothesis
onto the web call so landed rows carry the target's geography, and
re-renders a claimed-but-unanswered question to the analyst that owns it.

**Sources and surfaces.** Sixteen sources were added and are delivering
(six Sahel-region outlets, three UN News regional feeds, Kyiv Independent,
GroundUp, Daily Maverick, Dabanga, Sudan War Monitor, SCMP China, RBI press
releases), with a first-pass licence register. The workstation regained a
flow: a shared scope that every panel reads, a navigator rail, the Consult
docked open with real pinned context. A separate mobile build serves at
`/m` behind the same perimeter. The GeoJSON handler keeps a per-feature
cursor so a feed whose newest entry never changes cannot stall the rest
(EONET resumed at 32 signals a day). Findings list endpoints accept
`fields=summary`.

## 2026-09-06

**The verify-regime fix deployed, and the world read stopped starving.**
Both defects the previous entry flagged live and unresolved — the ARM-2
`scope_widened` false positive, and the legacy verify branches flooring a
byte-correct assembled read to ~0.22 — merged and deployed 2026-09-05
~22:40Z (judge stamp `2026-09-03/1` → `2026-09-05/1`). ARM 2 now checks the
`collection_denominator` scope token through the same predicate its sibling
check already used correctly, instead of a literal substring match that
could never find it in prose. For an assembly row, the D-3 arms become the
grader of record (`regrade_to_arms`); the legacy `citation_support` /
`synthesis` branches — built for free-text prose and never adapted to
quote-stitched spans — stay as telemetry only. A third thing was found in
the same pass: the Assessment's evidence map was missing one of the three
inputs its own prompt introduces as quotable facts (the record's own
arithmetic), not the whole map — fixed at the producer. Read against live
rows: arm false-fires 26 → 0, the assembly verify branch's pass rate 23/35
→ 35/35, overall confidence mean 0.35 → 0.85, below-floor rows 33/35 → 0/35,
and the world composer's input pool went from 1 admitted country assembly
out of 34 to 34/34. A forced replay run the same evening showed the world
read move from "1 verified desk read, 0 not carried" to "8 verified desk
reads, 56 not carried"; the next *unforced* cycle (09-06 00:00Z) landed 8
blocks at an overall score of 0.60 on its own.

**Two further passes on Assessment fidelity, same week.** The D-6 voice's
first natural read scored 0.4286 against the G3 bar of 0.90 with zero
deterministic unsupported markers caught — evidence the voice was
over-elaborating while the marker check under-reported it. A prompt-clause
fix (merged `d5ff79e7`, deployed 09-06 ~01:07Z) tells the voice to *read*
the record's own arithmetic to decide, never *narrate* it as a claim of its
own, adding an `instrument_prose` unsupported class (`unsupported.v2`);
replay dropped instrument-class failures 7/11 → 3/8, fidelity 0.74 → 0.79,
still short of the bar. A second fix (merged `46774fa9`, deployed 09-06
~10:15Z) exempts weighted-comparison claims that cite the arithmetic
reference (`provenance/assessment_weighting.py`, new) and adds an
`aperture_unrostered` unsupported class after a replay caught a false fire
on a genuinely-grounded sentence (`assessment_prompt.v3` /
`unsupported.v3`, stamp `2026-09-05/1` → `2026-09-06/1`). Pooled replay
fidelity: 0.724 → 0.854, ≥0.90 on 3 of 5 sampled reads, `judge_contradicted`
4 → 0. The residual moved rather than closed — INSTRUMENT-class failures
7 → 1, but APERTURE 1 → 3 — and the next candidate fix is named but not yet
built (evidence-map dilution vs. a rubric that never names the arithmetic
rule).

**The width plane's grading ledger lost its span check for a full day, and
found it by accident.** `external_span_check.check_span` (pure, never
fetches) and the drain's `check_decisive_span` wrapper (documented as
fetching the page itself) had shipped with call signatures that could never
bind — every call raised `TypeError`, caught by a broad `except Exception`
that wrote the row as `UNCHECKED / span_check_unavailable`, byte-identical
to the row the *designed* "the checker hasn't landed yet" condition would
have produced. The degrade disguised the defect as its own expected state,
so it survived from the width plane's first deploy until it was found and
fixed 09-06 (`d22f87b7`, width judge stamp `2026-09-06/1`): 211 ledger rows
with a non-null decisive URL and span, and 168
`external_audit.span_check_raised` warnings, had a proposed
SUPPORTED/CONTRADICTED verdict that was never actually checked. The fix
gives the drain a real fetch leg (`_external_audit_fetch.py`, new) —
robots-gated, status-checked, `published_at` via `trafilatura` — and calls
`check_span` with the arguments it actually declares. A `no_publish_date`
demotion class is now countable separately (a page whose date cannot be
found loses a decisive verdict it may have deserved) and a dry-run-only
requeue script (`scripts/width_requeue_span_check_unavailable.py`) can
re-derive and refill the 208 of 211 affected claims still traceable to
their read once the fix is confirmed live; it has not been run against the
live database. A second, independent seam bug was found and fixed in the
same pass: the evidence-window keys three different readers expected
(`oldest`/`newest` vs. `earliest`/`latest` vs. `from`/`to`) didn't all
match what the composer actually writes, so the time-recency check (G-3)
was silently un-exercised on every real width read; all three readers now
accept all three spellings.

**Two more defects in the same ledger writer, both closed the same day
width first ran live.** The first width sweep (09-05 22:07Z) lost 21 of 40
grading rows to a nonempty-grader-attribution CHECK: two of three code
paths in `grade_one()` (the pre-filter branches for an unverifiable claim
or an unverified absence claim) built their row before ever calling a
grader, and never threaded the resolved `grader_family`/
`grader_component_id` through — fixed by passing them at both call sites
(`4aa3bcfd`). The second sweep (09-05 23:07Z) lost 14 of 40 — precisely the
rows carrying a dated result, the most informative ones — because the raw
search-provided date string (`'2026-07'`, `'1 day ago'`, `'Aug 20, 2026'`,
…) was sent straight to an asyncpg `timestamptz` positional argument, which
hard-rejects a `str`. A new `parse_decisive_published_at` handles ISO,
month-only, relative ("N days ago", resolved against the grading tick's own
clock, never wall time), and long human date forms, falling back to `None`
rather than raising or guessing (`af117fa6`). A third fix closes the
mechanism both of the above exposed: a ledger write that failed used to
both drop the claim from the durable pending queue *and* durably block it
from ever being rediscovered for the rest of the UTC day — `write_grades()`
now reports per-row landed/failed outcomes, and only a claim whose write
actually landed is marked graded; a failed one goes back on the queue,
bounded, dead-lettered after 3 attempts (`bf4e7e8a`).

**The research dispatcher was ranking a month-old backlog row ahead of a
same-week alert.** The first live research run (09-06 03:37Z) addressed a
`unit_payload` question from `2026-08-02` instead of the IL/Palestine
`coverage_floor` gap dispatched two days earlier — not a class-priority bug
(`coverage_floor` already ranked first among *classes*), but a sort-key
tier evaluated *before* class ordinal: any row with real forward
consumption (`live_reach > 0`) outranks a freshly-dispatched row by
construction, since nothing has had time to consume the new one yet. A
backlog row's `live_reach` now decays to zero for ranking purposes past
`LEGBA_OPEN_QUESTION_BACKLOG_DECAY_DAYS` (default 14, env-overridable),
and `coverage_floor` moved to class ordinal 0 — now genuinely first once
decay stops the false tie-break (`2eb9a420`). Separately: a teaser-depth
research hit with an empty provider snippet was landing as a `signals` row
with `payload.text == ''` — zero-text evidence counted as a landed
citation; such hits are now skipped and counted
(`skipped_empty_snippet`), never written (`c99925c3`). A fourth research
dispatch leg was built, behind `LEGBA_REFERENCE_GAP_DISPATCH_ENABLED`
(default off): each uncollected, material item from the attention
instrument's outside reference now side-writes a `collection_requirements`
row (`origin='reference_gap'`, migration `0193` widens the
origin/evidence_kind CHECKs) through the existing `collection_gap` writer,
and dispatches an `open_question` (`harvest_class='reference_gap'`,
ranked just behind `coverage_floor`) into the same researcher backlog —
flag-off payload byte-identical (`4eb83cbc`). Last: the `nats_stream`
output kind had never once successfully emitted for *any* analyst, on any
kind that declares it — `OutputDeps` exposes the publisher at `.nats`, and
the resolver only ever checked `.nats_publish`/`.nats_store`, so every call
raised into the same generic except-branch. Fixed with a third resolution
path (`src/legba/data/outputs/nats_stream.py`). A second, unrelated cause
on the same log line — bring-up had never provisioned a JetStream stream
covering `analyst.>` subjects, so the separate direct-publish notification
always timed out — is closed by adding that `ensure_stream` call alongside
the others bring-up already issues (`b81a8260`).

**Two correctness bugs, found auditing the week's own trains.** A
coverage-floor clause added the day before to fix a SA/Yemen soft
false-positive (a desk's cited evidence names the missing polity only by
actor, never the polity itself) turned out to clear *every* open breach,
including IL/Palestine — the detector's own founding case. The real
defect: a finding's `derived_from` is not a citation list — every
dimension analyst but `country_composition` cites its whole trailing
grounding window (~120 signals on IL, ~60 on SA) as `derived_from` on
every run, so a raw-signal "does the evidence name the polity" check is
mathematically identical whether it's scoped per-frame or desk-wide; either
way it is reading the desk's entire slice, not the frame's actual
evidence. The clause now reads each cited finding's own authored title
instead (the one thing in the chain that is genuinely narrow), requiring
at least 2 titles naming the polity before a breach clears. IL/Palestine
re-breaches (242 signals / 15 days / 20.4% share); SA/Yemen genuinely
clears. Separately, `region_rollup.v1` was inflating its own `derived_from`
column: a generic evidence-tiers block appends every kept periphery row's
id unconditionally, a rule written for prose compositions (a hedged claim
resting on a weak signal is real lineage) that a rollup — which cites
nothing and never looks at periphery — should never have run through.
`region_americas` asserted `derived_from` length 1 (correct, the roster
basis) while the actual column held 6; the append is now gated on the row
not being a rollup.

**Housekeeping.** `meta_findings_synthesizer.py` had reached its module-size
ceiling exactly (4,820 of 4,820) — the PLAN-assembly splice named as the
next seam by two prior trains moved to `composition_prompt_assembly.py`
(606 new lines), byte-identical output proven by a block-equivalence spy
across 16 live rows × 2 arms. The auditor/desk_reference actor-wiring
blocks moved to a new `analyst_deps_kinds.py` leaf (`analyst_deps_builder.py`
3,035 → 2,984 lines); the W-9/A-1/R-D handler-option blocks moved to
`handler_options_programs.py` (`handler_options.py` 1,704 → 1,630 lines) —
both moves, and the import cycle the second one left behind (`handler_options.py`
and `handler_options_programs.py` imported from each other, working only
because every existing caller happened to import one before the other) are
now broken by a shared `handler_options_base.py` leaf holding `OptionSpec`
and its helpers, with both import orders proven in a fresh interpreter.
Separately: `legba.data.config._load_env()`'s fallback candidate is a
hardcoded absolute path to the main checkout's live `.env` — every test
process on this host, in any worktree, silently inherited every
operator-tuned `LEGBA_*` flag. Seven single-flag conftest fixtures
(accumulated one at a time, each added only after a specific test broke)
are replaced by one 16-flag tuple pinned to shipped defaults via a single
autouse fixture, shared between `tests/data_pkg` and `tests/runtime`. A
same-day hygiene batch added a disk-usage gauge to the host LLM heartbeat
(WARN 85% / CRITICAL 92%, ahead of OpenSearch's 95% flood-stage cliff that
silently stops indexing), a shared `source_credibility` reseed fixture, and
scoped a shuffle-order-fragile timeline test pair; a separate fix closed a
cross-file test-ordering leak where one file's leftover IL-geo desk row
could pollute another file's own IL-geo dispatch tests in the same run.
The third-family grader slot (`standing_auditor.method.llm.grader`,
`desk_reference.method.llm.primary`) was actually repointed live from the
unfunded Cerebras Gemma component to OpenRouter Mistral Large 3 — the
operator action the prior entry had flagged as still outstanding.

**Migration `0193`** widens the `collection_requirements` origin and
evidence_kind CHECK constraints to admit the new `reference_gap` origin.
New env flags: `LEGBA_DESK_REFERENCE_ENABLED` (the attention instrument's
own kill switch — separate from the design-time default-off flag on the
instrument as a whole), `LEGBA_REFERENCE_GAP_DISPATCH_ENABLED` (default
off, the new fourth dispatch leg above), `LEGBA_RESEARCH_EVIDENCE`,
`LEGBA_OPEN_QUESTION_BACKLOG_DECAY_DAYS` (default 14), and
`LEGBA_BRAVE_SEARCH_API_KEY` (a paid search fallback rung behind SearXNG,
component ships `state: draft`, inert until an operator both sets the key
and adds it as a `fallback_providers` entry on the web_access pack, and
only then behind a mandatory `governor.max_cost_usd_per_day` spend
ceiling — the ladder refuses a metered rung with none declared).

**Deploy status.** Everything in this entry is live in production as of
2026-09-06 11:16Z, in two rolling windows: 10:15Z (the G3 weighting/aperture
repair and verify stamp 2026-09-06/1, the width span-check seam fix, the
coverage-floor over-suppression fix, the researcher ranking and empty-teaser
fixes, the synthesizer prompt-assembly extraction, the rollup-miscount and
import-cycle fixes) and 11:15Z (migration 0193 with the reference-gap
dispatch enabled, the NATS stream emit fix and the analyst-outputs stream,
the R-1 pre-R4 gauge, the descriptor-state reconciliation, the rolling
deploy script). The `dapr_actors.py` decomposition is built and held on its
branch until after the proof round.

## 2026-09-05

**The read the flip was built to fix went live, said everything correctly,
and was nearly invisible anyway.** Composed prose had been externally graded
twice at ~0.48 accuracy against a 0.75 bar, so the composition layer was
demoted: a country, region, and world read is no longer free-text synthesis
over cited sub-claims — it is an **assembly**, built from byte-identical
quoted spans lifted verbatim from the desk findings that support them, under
a deterministically generated title, with a separately labeled **Assessment**
carrying whatever interpretive voice survives, fenced to the assembly's own
record (it can only read what the assembly itself contains, guarded at the
syntax level so it cannot quietly widen its own sources). The flip went live
2026-09-05. The first assembled cycle proved the construction honest — quote
fidelity 1.0, zero real quote or scope-truncation failures across every
audited row — and then the *grading* of that cycle betrayed it: the world
read that morning carried **one verified block of a possible thirty-two**,
because two verify checks built for the old free-text prose graded the new
quote-stitched form on a grain they were never adapted for, scoring a
byte-correct assembled read around 0.22 and flooring 31 of 32 country reads
out of the world composition before it ever ran. A second, smaller defect
compounded it: the check that watches for a claim's scope quietly widening
matched against the wrong representation and false-positived on every
collection-scoped absence statement, understating an otherwise-perfect
assembly branch by about ten points. A third: the Assessment's own
fidelity-to-its-record score reads zero, because the live path does not yet
carry the evidence map that score needs. None of the three touch the reads
themselves — the assembled prose and the Assessment prose are both
byte-correct on inspection — the defect is entirely in how the verify plane
grades a form of prose it had never seen before. The fix is written and
staged, disposed **fix-forward** (the reads are right; the instrument is
wrong) rather than flagged off, and is expected to land before the next
scheduled world cycle; the pre-committed rollback (one flag,
`LEGBA_COMPOSITION_ASSEMBLY=0`) restores the byte-identical legacy prose in
one deploy if an operator wants it sooner. The verify stamp moves from
`2026-09-03/1` to `2026-09-05/1` when the fix lands, carrying the lineage
note that a new population enters this ledger with no prior history to
compare against.

**The standing external auditor caught its first faithful-but-wrong absence
live.** A desk read had said no new military activity occurred in its
country over a several-day window; the auditor's own web check found a
contradicting event — a weapons test-firing — inside that exact window. This
is the class this project's own status page has named as a structural blind
spot since the gold-set labeling loop first surfaced it (a desk stating an
absence as a world fact when what it actually established is that its own
sources carried nothing): a claim that is perfectly faithful to its inputs
and wrong about the world, which no amount of grading the prose against its
own citations can ever catch. This time something reading the actual world
caught it, on the first day it had the chance to.

**Outbound research — a fetched web page can now become an ordinary,
citable signal instead of dying inside one finding's citation list.** A new
action pack gives the research analyst one tool that lands a fetched page as
a real `signals` row, tagged with its retrieval origin — a column that has
existed in the schema for months and that nothing had ever written. Two
honesty mechanisms travel with every such row: a credibility ceiling shared
with the platform's existing salience floor, so an uncorroborated fetch can
be cited but contributes nothing toward promoting a read up the composed
world; and a licence gate that only archives full text for a host an
operator has classified as clear, everything else landing as a
title-and-snippet teaser with no bytes retained. Dispatch needed no new
machinery: a coverage-floor breach — a desk with a real gap in what it has
seen — now becomes a standing open question that the research analyst's
existing backlog already drains, carrying the gap's country and scope so a
fetched page can actually reach the desk that needed it (before this, a
self-directed research run had no way to land anywhere). A new three-position
flag gates the whole program (writes-and-archives-but-excluded-from-reads is
its own middle position, the deliberate rollback rung if a fetched source
ever needs pulling back out of circulation without losing the record of it).
Three counters — how often a fetched page said something new, how often it
was later corroborated by the platform's own sources, and how often it
actually reached a published read — publish honestly-null until they have
enough rows to mean anything; the corroboration count in particular cannot
report a real number for its first two weeks by construction (it needs a
week's forward window to mature). A paid search fallback (Brave) is built as
a second rung behind the existing free metasearch engine, wired but inert
until a key is armed and an operator raises its budget cap — the free engine
carries the load in the meantime. All of it ships behind default-off flags
and a draft-state descriptor; nothing is deployed or registered yet, and
every flag-off code path is proven byte-identical to what shipped before it.

**External grading, taken from a daily sample to every claim, every day —
built, not yet switched on.** The standing external auditor today checks six
claims a day on a rotation; extending it to grade the whole assembled read
population (an estimated 470–545 checkable claims a day) needed no new claim
extractor, because under the new assembly regime the claim population is
already a field in the read's own payload — a quoted span with its byte
origin — so grading the record grades the desks it quotes, as a standing
measurement rather than a one-off exercise. A third model family, chosen to
share no family with either the writer or the production judge, grades each
claim against a code-enforced version of the same evidentiary bar the
platform's own graded proof rounds have used by hand: a recognized source
tier, a verbatim decisive quote, and a time-anchored publication date — never
a rubric left to a prompt's discretion. Verdicts land in a new append-only
ledger (migration `0190`) that a database trigger physically refuses to let
anyone update or delete, and publish as a judge-independent number on the
eval scorecard, sitting beside the existing operator-labeled correctness axis
and never pooled with it. A live, hash-gated double-grade sample keeps a
second, independent read on the grader itself and withholds the headline
number entirely below a pre-registered agreement bar, rather than publishing
a number nobody has checked. Only a claim two independent model families
both call contradicted, with a source and a resolvable quote, ever pages
anyone; a single grader's contradicted verdict still lands in the ledger and
the published number, it just doesn't wake anyone up. Ships behind a
default-off flag; its chosen primary grader needs an operator repoint before
it can run at all (the provider account behind the originally chosen model
turned out to be unfunded).

**Attention measurement — does a desk even notice the story its own sources
already carried?** A new deterministic analyst writes one independent
reference a day per country-and-topic pair, built entirely from its own web
search on a model that has never seen this platform's substrate — zero
in-house evidence anywhere near its prompt — then diffs that outside
reference against what the desk's own collected sources actually held and
what the desk actually wrote. Two numbers come out of that diff, kept
deliberately apart: whether the desk's own sources carried the story at all,
and — only among stories the desk *did* have — whether its read named or
cited it. A live, on-the-record proof of exactly the gap this exists to
measure: one country's Iran desk had the country's own Iran-war reporting in
9 of 120 of its own collected sources and referenced none of them, zero
times, across every read checked. The instrument itself ships honestly
unvalidated — a continuous internal control runs from day one, but a
number only becomes quotable and a gap only becomes something that can page
someone once an operator-invoked grading pass marks the instrument valid.
Migration `0191` extends a reference-labeling table that had sat in the
schema with a single row since it was built. Ships behind a default-off flag
and a draft descriptor; nothing is deployed or registered yet.

**A licence ledger column for the sources research fetches from**
(migration `0192`) — the classification research's licence gate above reads
to decide full-text-and-archive versus teaser-only. It ships with zero
verdicts seeded: classifying a host's licence is a legal reading this
project will not have a model make, so every fetched host reads
teaser-only until an operator classifies it by hand.

**A 400-character blind spot between what a composed read showed its model
and what its own audit trail could see closed.** A country or region
composition could render up to 4,000 characters of a source body into the
model's prompt, but the separate mechanism that captures a copy of that body
for the audit trail to check spans against — built so the auditor never has
to re-fetch a row that might have since changed — only kept the first 3,600.
A quote built from the missing 400 characters was constructed correctly
against the real, full body, and then failed its own audit as if it had been
fabricated, because the audit trail's copy had been silently truncated out
from under it. The capture ceiling now matches the render ceiling exactly,
with a test that pins the two together so they cannot drift apart again.
Measured against two weeks of live desk content: zero bodies exceeded 4,000
characters, and exactly two sat in the newly-closed gap — the only two
citations whose audit-trail copy changes shape at all, and only by growing
to match what the model already saw.

**An ops note.** Disk usage climbed to 94% this week, within reach of the
point (95%) at which the search index quietly refuses to accept new writes
rather than erroring loudly — corpus indexing would have stalled with no
alert anywhere. Reclaimed by pruning the container build cache, vacuuming
the container-runtime's own log journal, and removing dead image tags, back
down to 74%. Nothing watches disk as a standing gauge yet; that is now a
named gap rather than a surprise waiting to recur.

All of the above (research, external grading at width, attention
measurement, and the composition-tier evidence-capture fix) landed on a
merged tree with the full suite green — 12,601 tests passed — and every
flag-gated addition proven byte-identical with its flag off. None of it is
deployed; the demotion flip and the standing auditor's live behavior are the
only pieces of this entry running in production today.

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
