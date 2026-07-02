<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Tour — your first ten minutes

You've deployed (see the [README quick start](../README.md#quick-start) or
[SETUP.md](SETUP.md)) and sources have been polling for a little while. This
page shows you what Legba actually produces and how to check it — the whole
point of the platform in one sitting: **read an assessment, distrust it, and
verify it yourself.**

Commands below run from the deploy directory. `$TOKEN` is your registry bearer
(`LEGBA_REGISTRY_API_TOKEN` in `.env`); the registry API listens on `:8090`.

## 1. Is it alive?

```bash
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT count(*) FROM signals;
   SELECT kind, count(*) FROM analyst_outputs GROUP BY kind ORDER BY 2 DESC;"
```

You should see signals in the hundreds-to-thousands (they accrue continuously)
and analyst outputs of several kinds — `finding` is the one you care about
first. On a fresh install give the units one cadence cycle (they run twice a
day per desk, plus reactively when signals accumulate) before expecting
findings everywhere.

## 2. Read a finding

Findings are the atoms of the product: one bounded question, one country desk,
answered from cited evidence. Pull the latest for a desk (Ukraine here):

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/findings?target_id=country_watch_ua&limit=3" | python3 -m json.tool
```

Each finding's body reads like a miniature intelligence note:

> **BLUF:** Ukraine faces an escalating near-term risk of military
> confrontation, driven by confirmed large-scale Russian missile and drone
> strikes on Kyiv **[2][5][8]** …
>
> **Indicators to watch** — …

How to read it:

- **BLUF** ("bottom line up front") — the one-sentence judgment.
- **`[N]` markers** — citations. Each maps to a specific source signal id in
  the finding's `data.citations` array. A claim without a citation is treated
  by the verifier as unsupported.
- **`confidence`** — the analyst's self-assessed confidence.
- **`faithfulness` / `effective_confidence`** — the verifier's opinion of the
  analyst (next step). `effective_confidence = min(confidence, faithfulness)`:
  a confident claim the verifier couldn't ground reads LOW.
- **Indicators to watch** — forward-looking tripwires; explicitly not
  present-tense claims.

## 3. Distrust it — check the verification

Every cited finding gets a mandatory second pass: a deterministic
citation-presence check plus an LLM judge asking, claim by claim, *does this
follow from the evidence it cites?* You already have the verdict — it rides on
the finding you fetched in step 2:

- `critic_score` — the verify pass's faithfulness score in `[0,1]`.
- `effective_confidence` — `min(confidence, critic_score)`; the number every
  higher layer actually uses.
- `verification` — the detail block: checkable-claim counts and, the
  interesting part, the **unsupported spans** quoted verbatim with a reason
  (`no_citation`, `unresolved_citation`, `judge_unsupported`).

This is the honesty mechanism: fabrication doesn't get deleted, it gets
*flagged and demoted* where you can see it. A finding whose prose sailed but
whose citations didn't ground reads with a visibly lower
`effective_confidence` than its author claimed.

## 4. Drill to source

Every output row carries lineage. Walk it:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/lineage/finding/$FINDING_ID" | python3 -m json.tool
```

The walk resolves hop by hop — finding → the signals it derived from → each
signal's real source URL — and every analyst hop carries a SHA-256
`receipt_hash` with a recomputed `chain_consistent` badge. The badge says
"chain-consistent (single-node)" and means exactly that — an integrity check
on one node, not a distributed tamper-proof ledger.

## 5. Up the spine: composition, world read, scorecard

Desk-level synthesis fuses the four units' **verified** claims (unverified
ones never enter), and the world read composes the desk reads:

```bash
# The latest per-country composition for a desk:
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/findings?target_id=country_g20_us&analyst_id=country_composition&limit=1" \
  | python3 -m json.tool

# The banded scorecard — one row per desk, with the claim ids each band rests on:
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/v1/v3/eval/country_scorecard" | python3 -m json.tool
```

In compositions the citation markers look like `[[ref:N]]` — they point at the
underlying *unit claims* rather than raw signals, so the drill is: world read →
country read → unit claim → signal → source. On the scorecard, expect a mix:
some desks band from verified claims; a desk whose evidence didn't clear the
bar reads `insufficient-evidence` with the reason. That's a feature — the
scorecard never invents a band.

## 6. Ask it something

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"What changed in the Iran picture in the last 48 hours, and what is it based on?"}' \
  "http://127.0.0.1:8090/api/v1/consult" | python3 -m json.tool
```

Consult is a ReAct agent over governed read-tools against the live substrate.
Note: consult runs on a billed hosted model (see
[AI_MODELS.md](AI_MODELS.md)) — the always-on analysis spine runs entirely on
the self-hosted plane.

## 7. The UI

Everything above, pointable and clickable: browse to your Caddy host (`:443`,
basic-auth). The UI is a composable panel workstation — compose your own wall
from the Live Feed, Inspector (the finding reader with clickable citations),
World Map, per-desk views, Scorecard, lineage and entity graphs.
[UI.md](UI.md) is the panel-by-panel guide.

## Where to next

- What every coined term means: [GLOSSARY.md](GLOSSARY.md)
- What's real vs gated vs planned: [STATUS.md](STATUS.md)
- How the pipeline actually works: [ARCHITECTURE.md](ARCHITECTURE.md), then
  [FLOWS.md](FLOWS.md) for life-of-a-signal narratives
- Day-2 operations: [RUNBOOK.md](RUNBOOK.md)
