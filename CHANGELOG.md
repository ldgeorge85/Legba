# Changelog

Legba's public history is intentionally squashed — each release lands as a single commit
on `main` — so this file is the release record. Entries are dated (newest first) and
written against the docs as they shipped; [docs/STATUS.md](docs/STATUS.md) remains the
always-current truth-in-labeling table.

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
