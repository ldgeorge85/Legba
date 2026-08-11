<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Tour — your first ten minutes

You've deployed (see the [README quick start](../README.md#quick-start) or
[SETUP.md](SETUP.md)) and sources have been polling for a little while. This
page is the product tour: the UI workstation, told as a walk a competent
analyst can follow cold. If you want the underlying API instead of the UI, skip
to the [appendix](#appendix-the-api).

## What Legba is

Legba watches a set of open feeds — news RSS, hazard/GeoJSON, APIs, webhooks —
and turns them into short intelligence reads, one per country desk, rolled up
into per-region and world reads. Every claim in a read is cited to a source,
and a mandatory second pass checks whether the claim actually follows from
what it cites *before* the read is trusted — nothing gets deleted for failing,
it gets flagged and demoted where you can see it. The whole chain — source
item, citation, verify verdict, composed conclusion — is preserved with a
hash-chained trail back to the original source bytes, so any read is
replayable and auditable after the fact.

## 1. Boot: land on what's happening now

Open the UI (Caddy on `:443`, basic-auth). On first load the workspace seeds a
mission-control grid, no clicking required: a glance strip of top-line counts
across the top; the **Live Feed** (unified findings + signals, live-tailed)
anchoring the left column with **Timeline** lanes beneath it; the **World
Map** at real size in the center; and the composed **World Assessment** —
the top of the synthesis spine — tabbed with the **Inspector** on the right.
Click anything, anywhere, and its full detail loads in the Inspector; a
persistent sidebar tree (grouped **Awareness** / **Investigation** /
**Analysis** / **Products** / **Operations**, then your desks and analysts)
reaches every other panel.

To see *what changed since you last looked* specifically — the honest answer
to "what's new" — open **The Wall** from the Awareness group. Its "movers
since last visit" quadrant diffs against a cursor of your last visit (first
ever open looks back 24h): band changes first, then reversed findings, then
situation lifecycle edges, plus the newest high-severity grounding-verified
findings (the claim follows from its cited evidence — groundedness, not world
truth) and a system-health rollup. It reads the same per-desk verified data as
the World Map's choropleth, so the grid and the map never disagree.

## 2. Pick a desk

A "desk" is one country target — Legba runs one per G20 member plus a
13-desk high-consequence watch tier (Israel, Iran, North Korea, Pakistan,
Taiwan, Ukraine, and the escalation-risk band Sudan, Mali, Burkina Faso,
Niger, DR Congo, Myanmar, Haiti), 32 desks in all. Pick one two ways:

- Click a desk chip in the Wall's per-desk grid, or a country on the World
  Map's choropleth — either selects that desk into the Inspector.
- Or browse: every desk has its own Findings / Overview / Situations / Map /
  Timeline panels under its instance group in the sidebar.

Either path lands you on the desk's **findings** — the atoms of the product:
one bounded question (leadership transition, energy security, escalation,
narrative coordination, internal stability, military posture, economic
coercion — seven per desk, plus an eighth, proliferation_watch, on the ~8
nuclear-relevant desks only; a ninth unit, disruption_status, works the
thematic supply-chain lane/flow desks rather than countries), answered from
cited evidence over a rolling
window plus accumulated context. A finding's body reads like a miniature
intelligence note: a BLUF ("bottom line up front"), the supporting claims with
inline `[N]` citation markers, and — for the reasoning units — forward-looking
indicators to watch.

## 3. Believe or disbelieve it — this is the section that matters

Everything above is an assertion. This is how you check one, without leaving
the Inspector. This is the product's actual differentiator: not that Legba
writes reads, but that it hands you the means to distrust each one.

- **Citation chips.** Every `[N]` marker in a rendered report is a live chip.
  Click one and it scrolls to and flashes the matching row in the Evidence
  panel below the read — a claim drills straight to the signal it came from.
- **Hover the chip for the verdict card.** Hovering (or tapping) a chip opens
  a card with the source, the cited passage verbatim (or an honest "cited
  passage not recorded" if the pass didn't persist one), the citation's
  credibility, and — the important part — the **per-claim verify verdict**
  for that specific citation: a flagged claim shows its honesty label
  (contradicted / unsupported / hedge-laundering / …); an unflagged one says
  "not flagged" against the pooled checkable/supported context; nothing is
  ever invented — a floor-only or legacy row says plainly "claim-level verdict
  not recorded."
- **The faithfulness score.** Above the read, a header strip carries a
  **verdict badge**: two separate chips — **L** (likelihood, the finding's
  probability on a seven-point verbal scale) and **C** (analytic confidence:
  Low/Moderate/High, derived from the faithfulness-verify pass, the judge
  status, and citation breadth). `effective_confidence = min(confidence,
  faithfulness)` — a confidently-written claim the verifier couldn't ground
  reads low, visibly. Two honest exceptions exist and are labeled as such:
  a deterministic (non-LLM) finding that never enters the faithfulness pass
  reads `unverified — structural`; one whose numbers were independently
  re-derived from its own lineage and matched reads `structural —
  recomputation-verified`.
- **The trace back to source.** Scroll down inside the Inspector itself — the
  **provenance trail** is built into every selected record, not a separate
  panel you have to go find. It walks the `derived_from` chain hop by hop:
  finding → the signals/claims it derived from → each signal's real,
  clickable source URL. Every hop in the chain carries a SHA-256
  `receipt_hash`; a re-hash that matches shows **"chain-consistent
  (single-node)"** — read that literally: it is an integrity check on one
  node, not a signed, distributed, tamper-proof ledger. That's the whole
  honesty mechanism in one place: cited → checked → traceable, and every
  weak spot in that chain is a label, not a hidden gap.

## 4. Ask a question, then export what you found

- **Consult** (Analysis group) is an on-demand chat analyst: it answers your
  question in-line, with its tool trace and citations, over the live
  substrate — pin any record you're looking at into its context as you go.
  For a longer question, submit the same prompt as a **deep** run instead (a
  detached background analysis that returns a full lineage-walkable finding
  once it completes, rather than an inline chat answer) — useful when you want
  the multi-step synthesis rather than a quick read.
- **Export** — collect findings as you go: the Inspector's "add to export",
  a Live Feed row, or a Journal entry all drop into one persistent basket (a
  status-bar chip tracks the count). Open **Report Export** (Products group)
  to review the basket, title it, and export as **Markdown or JSON** — each
  item carries its cited body, resolved citation sources, its verify state
  (faithfulness or an explicit `unverified — <reason>`), and its lineage
  receipt link. Markdown renders a print-ready preview (`window.print()` →
  PDF). Capped at 50 items per export.

## 5. What runs on its own

You don't have to ask for any of this — it's already running:

- **The reasoning units** re-assess each desk on a roughly 6–12 hour beat (or
  reactively, sooner, once enough new signals accumulate for that desk), and
  the country → region → world composition tower runs on the same rhythm,
  folding in only claims that already passed verification.
- **Alert triggers** (watchlist hits, band flips, geo convergence, and more)
  are scanned every few minutes and fan out to whatever delivery sink you've
  configured (a webhook, a push sink) — the Alert / Watchlist surfaces in the
  Awareness group are always the audited record of what fired, whether or not
  you had a sink wired up.
- **The weekly grading loop is the one exception — it's operator-run, not
  automatic.** Each week a small stratified sample of verified findings is
  pinned for correctness labeling in a dedicated worksheet reachable from the
  sidebar; the labels feed an additive, clearly-separated "operator-graded"
  line in the eval scoreboard — they are never pooled with the automatic
  faithfulness numbers. Nobody grades it, nothing accumulates there that
  week.
- A daily **scorecard** pass bands every desk across its seven broad dimensions
  from already-verified sub-claims (an `insufficient — <reason>` state is
  shown honestly where the evidence hasn't cleared the bar, never a
  fabricated band) — that's what you're reading when you open the
  per-country scorecard from the Analysis group. `proliferation_watch` is
  deliberately not a fixed scorecard dimension (it would mis-render on the
  17 non-nuclear desks); its read still surfaces via the per-country
  composition.

## Where to next

- What every coined term means: [GLOSSARY.md](GLOSSARY.md)
- The whole panel set, one by one: [UI.md](UI.md)
- What's real vs gated vs planned: [STATUS.md](STATUS.md)
- How the pipeline actually works: [ARCHITECTURE.md](ARCHITECTURE.md), then
  [FLOWS.md](FLOWS.md) for life-of-a-signal narratives
- Day-2 operations: [RUNBOOK.md](RUNBOOK.md)

---

## Appendix: the API

Everything above has a `curl` equivalent — useful for scripting, or if you'd
rather read the substrate directly than click through it. Commands run from
the deploy directory. `$TOKEN` is your registry bearer
(`LEGBA_REGISTRY_API_TOKEN` in `.env`); the registry API listens on `:8090`.

### Is it alive?

```bash
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT count(*) FROM signals;
   SELECT kind, count(*) FROM analyst_outputs GROUP BY kind ORDER BY 2 DESC;"
```

You should see signals in the hundreds-to-thousands (they accrue continuously)
and analyst outputs of several kinds — `finding` is the one you care about
first. On a fresh install give the units one cadence cycle before expecting
findings everywhere.

### Read a finding

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/findings?target_id=country_watch_ua&limit=3" | python3 -m json.tool
```

Each finding's body reads like a miniature intelligence note — BLUF, cited
`[N]` claims (each maps to a source signal id in `data.citations`; an
uncited claim is treated by the verifier as unsupported), a self-assessed
`confidence`, the verifier's `faithfulness` / `effective_confidence` (`= min
(confidence, faithfulness)`), and forward-looking indicators to watch.

### Check the verification

The verdict rides on the finding you already fetched:

- `critic_score` — the verify pass's faithfulness score in `[0,1]`.
- `effective_confidence` — `min(confidence, critic_score)`, the number every
  higher layer actually uses.
- `verification` — checkable-claim counts and the **unsupported spans**
  quoted verbatim with a reason (`no_citation`, `unresolved_citation`,
  `judge_unsupported`).

### Drill to source

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/lineage/finding/$FINDING_ID" | python3 -m json.tool
```

The walk resolves hop by hop — finding → the signals it derived from → each
signal's real source URL — and every hop carries a SHA-256 `receipt_hash`
with a recomputed `chain_consistent` badge: "chain-consistent (single-node)"
means an integrity check on one node, not a distributed tamper-proof ledger.

### Up the spine: composition, world read, scorecard

```bash
# The latest per-country composition for a desk:
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/findings?target_id=country_g20_us&analyst_id=country_composition&limit=1" \
  | python3 -m json.tool

# The banded scorecard — one row per desk, with the claim ids each band rests on:
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/v3/eval/country_scorecard" | python3 -m json.tool
```

In compositions the citation markers look like `[[ref:N]]` — they point at
the underlying *unit claims* rather than raw signals, so the drill is: world
read → country read → unit claim → signal → source. On the scorecard, expect
a mix: some desks band from verified claims; a desk whose evidence didn't
clear the bar reads `insufficient-evidence` with the reason — the scorecard
never invents a band.

### What changed since you last looked

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/v3/since?cursor=$CURSOR" | python3 -m json.tool
```

The same diff API the Wall's "movers" quadrant calls: band changes,
reversals, and situation lifecycle edges since the given cursor timestamp
(the response's `server_now` is your next cursor).

### Ask it something

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"What changed in the Iran picture in the last 48 hours, and what is it based on?"}' \
  "http://127.0.0.1:8090/api/v1/consult" | python3 -m json.tool
```

Consult is a ReAct agent over governed read-tools against the live substrate.
Note: consult runs on a billed hosted model (see
[AI_MODELS.md](AI_MODELS.md)) — the always-on analysis spine runs entirely on
the self-hosted plane. For a detached deep run instead:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"What changed in the Iran picture in the last 48 hours, and what is it based on?"}' \
  "http://127.0.0.1:8090/api/v1/deep_consult" | python3 -m json.tool
# returns a task_id immediately; poll:
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/deep_consult/$TASK_ID" | python3 -m json.tool
```

### Export a basket

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"weekend read","format":"markdown","items":[{"kind":"finding","id":"'"$FINDING_ID"'"}]}' \
  "http://127.0.0.1:8090/api/v1/v3/export" | python3 -m json.tool
```

Server-composed: each finding carries its cited body, citations resolved live
to signal titles + canonical URLs, its verify state, and its lineage receipt
link. Capped at 50 items (an honest `413` beyond).
