<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Tour — your first ten minutes

You have deployed ([Setup](SETUP.md)) and the sources have been polling for a while. This is the
product tour: the workstation, told as a walk an analyst can follow cold. If you would rather read the
API than click, skip to the [appendix](#appendix-the-api).

## What you are looking at

Legba watches open feeds and turns them into short intelligence reads, one per desk, quoted upward
into regional and world records, each with its own interpretive voice. Every claim in a read is cited
to a source, and a second pass checks whether the claim follows from what it cites before the read is
trusted. Nothing is deleted for failing; it is flagged and demoted where you can see it. The whole
chain, source item to citation to verdict to composed conclusion, is preserved with a hash-chained
trail back to the original bytes.

## 1. Land on what is happening now

Open the console (Caddy on `:443`, basic auth). On first load the workspace seeds a mission-control
grid: a glance strip of top-line counts across the top; the Live Feed of findings and signals down the
left with Timeline lanes beneath it; the World Map in the centre; and the World Assessment, the top of
the spine, tabbed with the Inspector on the right. Click anything and its full detail loads in the
Inspector. A sidebar tree, grouped Awareness, Investigation, Analysis, Products and Operations, then
your desks and analysts, reaches every other panel.

For what changed since you last looked, open The Wall from the Awareness group. Its "movers since
last visit" quadrant diffs against a cursor of your last visit: band changes first, then reversed
findings, then situation lifecycle edges, plus the newest high-severity verified findings and a
system-health rollup. It reads the same per-desk verified data as the map's choropleth, so the grid
and the map never disagree.

## 2. Pick a desk

A desk is one subject frame. The shipped set is one desk per G20 member plus a high-consequence watch
tier, 32 country desks, and a family of thematic supply-chain desks on the same primitive. Pick one by
clicking a desk chip on the Wall, a country on the map, or its instance group in the sidebar, where
every desk has its own Findings, Overview, Situations, Map and Timeline panels.

Either path lands you on the desk's findings, the atoms of the product. Each answers one bounded
question: leadership transition, energy security, escalation, narrative coordination, internal
stability, military posture and economic coercion on every country desk; proliferation on the
nuclear-relevant desks; disruption status on the thematic desks. A finding's body reads like a short
intelligence note: a bottom line up front, the supporting claims with inline `[N]` citation markers,
and forward-looking indicators to watch.

## 3. Believe it or not

Everything above is an assertion. This is how you check one without leaving the Inspector, and it is
the product's differentiator: not that Legba writes reads, but that it hands you the means to distrust
each one.

- **Citation chips.** Every `[N]` marker is a live chip. Click one and it scrolls to the matching row
  in the Evidence panel below the read: the claim drills to the signal it came from.
- **Hover for the verdict card.** The source, the cited passage verbatim, the citation's credibility,
  and the per-claim verify verdict for that citation. A flagged claim shows its label (contradicted,
  unsupported, hedge-laundering); an unflagged one says so against the pooled checkable and supported
  counts; a floor-only row says plainly that no claim-level verdict was recorded.
- **The badge.** Above the read, two chips: likelihood on a seven-point verbal scale, and analytic
  confidence, derived from the faithfulness pass, the judge status and citation breadth.
  `effective_confidence = min(confidence, faithfulness)`, so a confidently written claim the verifier
  could not ground reads low, visibly. A deterministic finding that never enters the faithfulness pass
  reads "unverified, structural"; one whose numbers were re-derived from its own lineage and matched
  reads "recomputation-verified".
- **The trail to source.** The provenance trail is built into every selected record. It walks the
  `derived_from` chain hop by hop, finding to signals to each signal's real source URL. Every hop
  carries a receipt hash; a matching re-hash shows "chain-consistent (single-node)", which means an
  integrity check on one node, not a distributed tamper-proof ledger.

For a composition or a voice, the markers look like `[[ref:N]]` and point at verified unit claims,
so the drill is world record to country record to unit claim to signal to source. The voice row
beside each record is the interpretive read, fenced to that record and graded on its own.

## 4. Ask a question, then export

- **Consult**, in the Analysis group, answers a question in line with its tool trace and citations
  over the live substrate; pin any record into its context as you go. For a longer question submit
  the same prompt as a deep run, a detached analysis that returns a lineage-walkable finding when it
  completes. Consult is the one billed path; it runs only when you press it.
- **Export.** Collect findings into one persistent basket from the Inspector, a feed row or a journal
  entry, then open Report Export in the Products group to review, title and export as Markdown or
  JSON. Each item carries its cited body, resolved sources, its verify state, and its receipt link.
  Markdown renders a print-ready preview.

## 5. What runs on its own

- **The units** re-assess each desk twice a day on cadence, or sooner when enough new signals
  accumulate for that desk. The compositions and voices follow on the same rhythm, quoting only claims
  that already passed verification.
- **The graders.** The faithfulness judge scores every read as it lands. The correctness grader
  scores the desks nightly against references the platform built independently, in short turns
  across the roster. The external audit samples top-layer claims and checks them against live web
  search. All three publish their numbers; none pools with another.
- **Alerts** on verified state changes and watchlist hits are scanned every few minutes and delivered
  through whatever sink you configured. The Alert and Watchlist surfaces in the Awareness group are
  the audited record of what fired, and what was suppressed, whether or not a sink was wired.
- **The scorecard** bands every desk across its broad dimensions from verified sub-claims, and reads
  "insufficient" with the reason where the evidence did not clear the bar. It never invents a band.
- **The journal, the chronicle and the lenses** write on their own cadences, off the product chain.

## Where next

- Every coined term: [Glossary](GLOSSARY.md).
- The whole panel set: [UI](UI.md).
- What is real, gated, or only designed: [Status](STATUS.md).
- How the pipeline works: [Architecture](ARCHITECTURE.md), then [Flows](FLOWS.md).
- Day-two operations: [Runbook](RUNBOOK.md).

---

## Appendix: the API

Everything above has a `curl` equivalent. Commands run from the deploy directory; `$TOKEN` is your
registry bearer from `.env`; the registry API listens on `:8090`.

```bash
# Is it alive?
docker compose exec -T postgres psql -U legba -d legba -c \
  "SELECT count(*) FROM signals; SELECT kind, count(*) FROM analyst_outputs GROUP BY kind ORDER BY 2 DESC;"

# Read a finding
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/findings?target_id=country_watch_ua&limit=3" | python3 -m json.tool
```

Each finding carries its cited claims (each `[N]` maps to a signal id in `data.citations`; an uncited
claim is unsupported by definition), a self-assessed `confidence`, the verifier's `critic_score` and
`effective_confidence`, and a `verification` block with checkable-claim counts and the unsupported
spans quoted verbatim with a reason.

```bash
# Drill to source: the lineage walk, every hop with a receipt hash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/lineage/finding/$FINDING_ID" | python3 -m json.tool

# Up the spine: the latest country record for a desk, and the banded scorecard
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/findings?target_id=country_g20_us&analyst_id=country_composition&limit=1" | python3 -m json.tool
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/v3/eval/country_scorecard" | python3 -m json.tool

# What changed since you last looked (the Wall's movers quadrant; server_now is your next cursor)
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/v3/since?cursor=$CURSOR" | python3 -m json.tool

# Ask it something (billed; the scheduled spine never is)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"What changed in the Iran picture in the last 48 hours, and what is it based on?"}' \
  "http://127.0.0.1:8090/api/v1/consult" | python3 -m json.tool

# The detached form: returns a task id, then poll it
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"..."}' "http://127.0.0.1:8090/api/v1/deep_consult" | python3 -m json.tool
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8090/api/v1/deep_consult/$TASK_ID" | python3 -m json.tool
```

The route list with each route's maturity is the [release matrix](RELEASE_STATE_MATRIX.md).
