<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Tour — your first ten minutes

You have deployed ([Setup](SETUP.md)) and the sources have been polling for a while. This is a walk
through the console that a first-time reader can follow cold. If you would rather read the API than
click, skip to the [appendix](#appendix-the-api). The panels and controls are described in full in
[UI.md](UI.md).

The examples on this page come from the geopolitics example instance, where a subject is a country
desk. On your instance a subject is whatever your profile declares, and the screens use your profile's
words.

## What you are looking at

Legba watches the sources you configure and turns them into short reads, one per subject, which
higher reads quote rather than paraphrase. Every claim in a read is cited to a source, and a second
pass checks whether the claim follows from what it cites before the read is trusted. Nothing is deleted
for failing; it is flagged and demoted where you can see it. The chain from source item to citation to
verdict to composed conclusion is kept, with a hash-chained trail back to the original bytes.

## 1. Sign in and land on the Board

Open the console at your instance's address. Out of the box the edge asks for its perimeter password
and every person is the same admin. If your instance has sign-in turned on, you sign in with your own
account; your role decides what you can change ([what each role can do](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in)).
A reader can read everything on this tour except the operator side, and changes nothing shared.

You land on the **Board**: today's read on the stage in the centre, Consult under it, and panels to
the right with the map leading. The top bar has the four reader pages (**Board**, **Subject**,
**Ledger**, **Watch**), a subject switcher, the view catalog (`＋`), Undo, Reset and Copy link. The
footer is the health line: how many sources are firing, the gaps on record, the judge's mean
faithfulness with its sample size, and how much the independent grader reached. A figure that has not
been measured prints `unmeasured`, never `0`.

On a phone, the same reader pages open at `/m/` in a shell made for a small screen; a phone-width
browser that opens the desktop is sent there.

## 2. Pick a subject

Pick a subject from the switcher, the rail on the left, or the map. Its page opens on the stage: its
latest reads, what changed, its forecasts, its place on the map. Each read answers one bounded question
about that subject. In the example instance a country desk has units for questions such as leadership
transition, energy security and escalation, and the thematic desks have their own. A read's body is a
short note: a bottom line up front, the supporting claims with inline `[N]` citation markers, and
indicators to watch.

## 3. Check a claim

Everything above is an assertion. This is how you check one, and it is the point of the product: not
that it writes reads, but that it hands you the means to distrust each one.

- **Citation chips.** Every `[N]` marker is a live chip. Click it and the **Record** sheet slides in
  over the panels: the statement as published, the row it maps to, the evidence with its source and
  class, and the fetch receipt (when, from where, the hash, the stored version).
- **The verdict.** The source, the cited passage verbatim, and the per-claim verify verdict. A flagged
  claim shows its label (contradicted, unsupported, hedge-laundering); a row with only the
  deterministic floor says that no claim-level verdict was recorded.
- **The badge.** Likelihood on a seven-point verbal scale, and analytic confidence derived from the
  faithfulness pass. `effective_confidence = min(confidence, faithfulness)`, so a confidently written
  claim the verifier could not ground reads low, visibly. A deterministic finding that never enters the
  faithfulness pass reads "unverified, structural".
- **The trail to source.** The record walks the `derived_from` chain hop by hop to each signal's source
  URL. Every hop carries a receipt hash; a matching re-hash shows "chain-consistent (single-node)", an
  integrity check on one node, not a distributed tamper-proof ledger.

A composed read cites verified unit claims rather than signals, so the drill goes from the composed
read to the unit claim to the signal to the source.

## 4. Ask a question

**Consult** sits under the stage on every page. Ask it a question and it answers with its tool trace
and citations over the live substrate; whatever you are looking at is named above the box as what you
are asking about, and you can pin records into it. A longer question can run as a deep consult, which
returns a finding you can walk like any other. Consult runs only when someone asks. With **Drive the
view** on, an answer can also present a case: a sequence of ordinary console moves that you can stop
and undo. A signed-in person sees only their own Consult history.

## 5. The other reader pages

- **Ledger.** Every sealed forecast with its probability, its dates, its resolution rule in plain words
  and a seal you can check; the track record, which shows no score until enough questions have
  resolved; and published evaluation runs.
- **Watch.** The one alert log: what paged and what was only logged, the silences, and source health.
  A contributor or an admin can acknowledge or snooze an alert.

## 6. What runs on its own

- **The units** re-read each subject on cadence, or sooner when enough new signals arrive. Compositions
  follow, quoting only claims that passed verification.
- **The graders.** The faithfulness judge scores every read as it lands. The correctness grader scores
  subjects nightly against references built independently of the platform's own substrate. The external
  audit samples claims and checks them against live web search. The three publish their numbers and
  never pool them.
- **Alerts** on verified state changes are recorded in the Watch log whether or not a push sink is
  wired; with one wired, what pages goes to ntfy.
- **The scorecard** bands each subject from verified claims and reads "insufficient" with the reason
  where the evidence did not clear the bar.
- **The journal, the chronicle and the lenses** write on their own cadences, off the product chain.

## Where next

- Every coined term: [Glossary](GLOSSARY.md).
- The whole console: [UI](UI.md).
- What is live, off by default, or not built: [Status](STATUS.md).
- How the pipeline works: [Architecture](ARCHITECTURE.md), then [Flows](FLOWS.md).
- Day-two operations: [Runbook](RUNBOOK.md).

---

## Appendix: the API

Everything above has a `curl` equivalent. These run on the host from the deploy directory against the
registry's loopback port, with `$TOKEN` set to the registry credential from `.env`. With sign-in on, a
browser uses its session instead, and the minimum role of every route is in [API_ROLES.md](API_ROLES.md).
Replace `$TARGET_ID` with one of your subjects' target ids.

```bash
REG=http://127.0.0.1:8090/api/v1
AUTH="Authorization: Bearer $TOKEN"

# Is it alive?
curl -s $REG/registry/healthz

# Read a subject's newest findings (summary rows; add fields=full for bodies)
curl -s -H "$AUTH" "$REG/findings?target_id=$TARGET_ID&limit=3" | python3 -m json.tool

# One finding at full weight
curl -s -H "$AUTH" "$REG/findings/$FINDING_ID" | python3 -m json.tool
```

A full finding carries its cited claims (each `[N]` maps to a record in `data.citations`; an uncited
claim is unsupported by definition), a self-assessed `confidence`, the verifier's `critic_score` and
`effective_confidence`, and a `verification` block with checkable-claim counts and the unsupported
spans quoted verbatim with a reason.

```bash
# Drill to source: the lineage walk, every hop with a receipt hash
curl -s -H "$AUTH" "$REG/lineage/finding/$FINDING_ID" | python3 -m json.tool

# The banded scorecard
curl -s -H "$AUTH" "$REG/v3/eval/country_scorecard?target_id=$TARGET_ID" | python3 -m json.tool

# What changed since you last looked (server_now is your next cursor)
curl -s -H "$AUTH" "$REG/v3/since?cursor=$CURSOR" | python3 -m json.tool

# Ask it something
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"question":"What changed for this subject in the last 48 hours, and what is it based on?"}' \
  "$REG/consult" | python3 -m json.tool

# The detached form: returns a task id, then poll it
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"question":"..."}' "$REG/deep_consult" | python3 -m json.tool
curl -s -H "$AUTH" "$REG/deep_consult/$TASK_ID" | python3 -m json.tool
```

The route list with each route's maturity is the [release matrix](RELEASE_STATE_MATRIX.md).
