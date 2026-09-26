# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trigger class 9 — ``coverage_floor`` (#82, the IL-blindness regression guard).

WHY THIS CLASS EXISTS
---------------------
``planning/CAMPAIGN_2026-08-29/IL_BLINDNESS_DIAGNOSIS.md`` traced a whole desk's
silence about the war it was fighting to a register that had never opened a
frame for it: ingest, screening and the per-desk slice all passed clean (61
Iran/Hormuz/Hezbollah-naming IL signals in 14 days, from IL's OWN papers of
record), but ``situations`` held exactly one standing frame for Israel and it
was about settlements. Seven desks each picked the story with grounding-block
backing over the ungrounded war signals in their own numbered evidence. The
diagnosis's cheapest recommended repair was option (5): a target-agnostic
post-check that compares what a desk's SLICE carries against what its own
frames NAME, and says so when the two disagree.

This is that check. It is a DETECTOR, not a fix — it cannot open the missing
frame, only refuse to let its absence stay invisible.

WHAT THE 0188 SPLIT CHANGED, AND WHY THE ORIGINAL RULE HAD TO BE RESTATED
--------------------------------------------------------------------------
The diagnosis proposed a *frame-count* predictor ("a target with only one
ever-clustered frame is at risk"). Migration 0188 re-keyed the register onto
``sig:<topic>#dim:<analyst_id>`` and split the mega-frames, so every country
target now carries 7-8 open frames — one per producing dimension — and the
count predicts NOTHING (it is structurally constant fleet-wide). The blindness
did not go with it: at the 2026-09-03 census the IL register's eight frames
name a coalition split, a Dolphin-class submarine (twice), gasoline prices, an
October election, a Qatar defence-export ban, UNRWA coverage and a border-force
buildup — and not one of them names Iran, Lebanon, Gaza or Palestine, while
Palestine alone appears in 20% of IL's own 14-day entity-bearing slice.

So the measured quantity here is COVERAGE, not cardinality: does the target's
open frame set NAME the foreign polity its own evidence keeps naming?

THE BAR (every clause is load-bearing, and every one is env/option tunable)
---------------------------------------------------------------------------
An entity cluster breaches the coverage floor for a target when ALL hold:

1. **It is a foreign polity.** The cluster canonicalizes (through the shared
   :mod:`legba.data._entity_canon` spine — the same canon ingestion and the
   resolver use) to class ``country``, and is not the target's own country.
   V1 scope is deliberately narrow: person / organization / location clusters
   are where the false positives live (a head of government, a capital city, a
   national statistics agency recur at high salience on every desk and mean
   nothing), and the origin defect is precisely a foreign-polity one — Israel
   not naming Iran, Japan not naming North Korea. Widening the class set is a
   later decision with its own evidence, not a default.
2. **It recurs.** ``>= min_signals`` distinct signals in the window, on
   ``>= min_days`` distinct days. One loud day is an event, not a coverage gap.
3. **It is consequential.** Mean ``signals.salience.magnitude`` over the
   cluster ``>= min_mean_magnitude``, AND ``>= min_high_magnitude_signals``
   of its signals individually score ``>= high_magnitude``. Salience is what
   separates "Lionel Messi, 37 signals, mean 0.05" from "Lebanon, 90 signals,
   mean 0.71" — without it, recurrence alone flags the sports page.
4. **It is a real share of the desk's own read.** ``n_signals /
   (entity-bearing signals in this desk's window) >= min_slice_share``. This
   is the threshold that turns the detector from a 17-target firehose into a
   3-target report: it asks whether the desk's evidence is ABOUT this, not
   merely whether it mentions it.
5. **ZERO frame names it.** No open frame's ``name`` contains the cluster's
   canonical name or ANY of its curated alias / demonym surfaces. The surface
   set is what makes the check honest in both directions: the RU register
   never writes "Russia's war on Ukraine", it writes "Ukrainian drone attacks"
   — and that IS coverage, so RU must not fire. Six of the nine targets that
   fire on the naive substring check are cleared by demonym matching alone.
6. **The SA/Yemen soft-FP fix (2026-09-05, ``planning/COVERAGE_FLOOR_82_
   2026-09-04.md`` §2.7's "named next refinement"; CORRECTED 2026-09-06,
   ``planning/COVERAGE_FLOOR_OVERSUPPRESS_FIX_REPORT.md``).** Clause 5 alone
   still fired on ``country_g20_sa``/Yemen even though SA's desks DO engage
   (4/8): the register names the *actor* ("Houthi maritime embargo", "Houthi
   border and maritime attacks"), never the polity, so a pure name match
   cannot see the coverage.

   The FIRST cut (df088954, 2026-09-05) walked ``situations.derived_from``
   (a frame's own member findings) out one more hop to each finding's own
   ``derived_from`` (that finding's raw grounding SIGNALS) and asked whether
   any of THOSE signals' NER entities represented the missing polity. It
   shipped with a desk-merging bug (surfaces from every open frame's evidence
   were pooled into one blob per TARGET, so clause 6 could clear on
   *another* frame's evidence) — but fixing THAT bug alone turned out not to
   matter: a finding's ``derived_from`` is not a narrow per-claim citation
   list, it is the analyst's WHOLE trailing grounding window (measured live
   2026-09-06: every non-``country_composition`` dimension cites its full
   ~120 signals on IL / ~62 on SA, *every* time it runs), and a frame
   accumulates dozens of these over its open lifetime. So EVEN fenced
   correctly per frame, "does any signal this frame's findings ever read
   carry the polity" is mathematically indistinguishable from "does the
   desk's whole window carry it" — both IL/Palestine (0 of 9 desks writing
   it, 242 signals / 20.5% share) and SA/Yemen cleared, and clause 6 had
   silenced the detector's own founding case.

   THIS implementation instead reads each cited finding's own AUTHORED
   TITLE — the analyst's one-line judgment about what that specific finding
   is about, not its raw reading population — through the SAME curated
   matcher clause 5 already uses on frame names. A cluster clears when at
   least one open frame has ``frame_evidence_min_findings`` (default 2) of
   its OWN cited findings (``situations.derived_from`` -> that finding's own
   ``title``, never another frame's findings, never the desk's findings at
   large) whose title names the missing polity — recurrence, so a single
   incidental mention cannot silence a real gap (live 2026-09-06: IL's one
   candidate frame had exactly 1 such finding of 232 attached across its 8
   frames; SA's two genuinely-covering Houthi frames had 2, 5 and 6). See
   :data:`_FRAME_FINDING_TITLES_SQL` and :func:`candidate_clusters`.

THE NAMING-ONLY COUNTER (R1-e, ``planning/R1_FRAME_REPAIR_AMENDMENT_2026-09-06
.md`` §3)
--------------------------------------------------------------------------
Clause 6 and R-1's own anchor bar are *the same question read in opposite
directions*, and the amendment's F-11 says they are allowed to disagree: today
IL/Palestine breaches clause 6 (one qualifying title, one short of the
recurrence bar) while the desks' own prose anchors Palestine twelve findings
deep. The design's bar **B-1** ("the naming-only breach count reaches 0 BY
REPAIR — a frame NAMES the polity") therefore needs a producer that is
independent of clause 6, and this scan is the only place that number can be
computed honestly.

So every scan runs the bar TWICE. The second pass is one extra
:func:`candidate_clusters` call with ``frame_finding_titles`` left at its
default ``()`` — clause 5 still applies (a frame that NAMES the polity is
still covered), clause 6 suppresses nothing — and the resulting clusters are
put through the same phase-2 exact recount and the same :func:`clears_bar`.
The result rides ``stats`` as ``breaches_naming_only`` beside ``breaches``.

It is BEHAVIOUR-NEUTRAL by construction and that is the whole contract:

* the naming-only pass runs on ``dataclasses.replace`` COPIES of the extra
  clusters, so the objects the real pipeline holds are never mutated by it;
* it writes no watermark, builds no ``AlertCandidate``, dispatches nothing and
  never touches ``breaches_by_target``. Nothing downstream reads it;
* pairs the recount bound dropped are excluded from it exactly as they are
  excluded from ``resolved`` — "we ran out of budget" is still not a fact
  about the register;
* ``breaches_naming_only >= breaches`` always, because the naming-only
  nomination set is a strict superset of the clause-6-suppressed one and both
  pass through the identical bar.

The design's withdrawn option (a) — a clause-6 OFF SWITCH — is deliberately
NOT here: the corrected clause is not the over-suppressor that made one
attractive (amendment §3, R1-e (3)).

FIRE-ONCE ON THE RISING EDGE
----------------------------
Watermark key is ``<target_id>|<entity_fold>`` under this class's own
``trigger_class`` namespace, state fingerprinting ``{"breached": bool}``. A
standing breach is REFRESHED silently every scan (so the age-out prune cannot
delete a live breach and let it re-fire) and pages exactly once; a breach that
resolves — the frame finally names it, or the evidence recedes — re-arms
silently, because a coverage gap closing is not an operator event. The first
scan of the class adopts every standing breach WITHOUT paging (the 0091 seed
contract), and says how many in the receipt.

Per target the scan emits ONE candidate naming every NEWLY breaching cluster,
worst-first by signal count — never one alert per cluster. Severity is a flat
``low``: this is a quality signal about the register, not an event in the
world. At ``low`` it sorts LAST in ``apply_desk_cap`` and in the D2 daily
budget, so it can never displace something that pages; and because the fleet
runs ``LEGBA_ALERT_NTFY_MIN_SEVERITY=medium``, the ntfy sink filters it out
BEFORE any delivery attempt (a below-floor alert writes no
``alert_sink_deliveries`` row at all — a configured filter working as designed
is not a delivery event). The durable product is the ``kind='alert'``
``analyst_outputs`` row, which is where the read APIs and the UI find it. It
lands in the substrate and wakes nobody.

THE DISPATCH LEG (R-B, 2026-09-05) — WHAT THE DETECTOR NOW HANDS ON
--------------------------------------------------------------------
This class remains a DETECTOR: it still cannot open the missing frame. What
changed is that its finding no longer only *sits* in the substrate. Behind
``LEGBA_RESEARCH_EVIDENCE``, each NEWLY breaching cluster also builds one
dispatch descriptor (``AlertCandidate.research_dispatch``) that
``alert_trigger_scan`` turns, after the alert row lands, into ONE standing
``hypotheses`` open question carrying the target + its geo, the named gap and
the hottest desk's bounded question — which the corpus_researcher's existing
backlog drain then picks up. See ``_research_dispatch`` for the contract and
for why it is a question and not a queue.

With the flag off, this file computes NOTHING extra: no query, no payload, an
empty list on every candidate, and byte-identical output.

WHY THE SCAN IS INTERVAL-GATED
------------------------------
The entity aggregate is a 14-day unnest over every desk's slice — ~4s on the
2026-09-03 substrate. The alert scan ticks every 10 minutes; a 14-day coverage
gap does not move on that clock. So the class keeps its own ``_scan`` cursor
and does the heavy read at most once per ``min_scan_interval_hours`` (default
6), reporting ``skipped_interval`` in the receipt when it declines. Skipping
advances NOTHING — no watermark, no seed — so a skipped tick can never lose a
transition.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..._entity_canon import (
    canonicalize_entity,
    identity_fold,
    is_junk_entity,
)
# A-0 (ATTENTION_MEASUREMENT_DESIGN §4) — THE SHARED POLITY MATCHER. Everything
# below used to be defined HERE; the attention instrument asks the same question
# of different prose, and a second implementation of it would be a second set of
# false positives (§Appendix A.4 measures what a naive matcher reports instead:
# 3 of its 6 fleet-wide "gaps" are the desk's own country under a spelling the
# gazetteer does not join). The bodies moved to ``legba.data._polity_match``,
# beside the ``_entity_canon`` maps they are built from, and are RE-EXPORTED
# here so every call site in this module — and this module's own public surface
# — is unchanged. The one behaviour the move adds is the MECH-6 fold at the
# single normalisation site inside ``normalize_prose``; see that module's banner
# for why it is byte-identical on ASCII and a strict widening otherwise.
from ..._polity_match import (  # noqa: F401 — re-exported public surface
    _ISO2_HOME_ALIASES,
    _pycountry_names,
    _SURFACES,
    entity_surfaces,
    home_prose,
    is_home_country,
    normalize_prose,
    represented_by,
)
from ..._geo_routing import (
    GeoRoutingConfig,
    MAX_ROUTED_TARGETS_IN_RECEIPT,
    count_routed_elsewhere,
)
from ..research_regime import research_evidence_enabled
from . import _research_dispatch

logger = logging.getLogger(__name__)

TRIGGER_CLASS: str = "coverage_floor"

#: The class's outward verify-state prose (``alert_trigger_scan.
#: _UNVERIFIED_REASONS``). Declared HERE, beside the bar it describes — the
#: _situation_escalation_scan precedent.
UNVERIFIED_REASON: str = (
    "deterministic coverage comparison between a desk's own salience-scored "
    "signal entities and the names its open situation frames carry (no LLM "
    "prose, and no claim about the world beyond what its own slice already "
    "counted)"
)

#: Flat page severity. A register coverage gap is a quality signal about THIS
#: ENGINE's situational picture, not an event; it must never outrank a real
#: world alert in the shared per-desk cap or the D2 daily budget.
SEVERITY: str = "low"

#: The watermark key that holds the interval cursor (never a breach key —
#: breach keys always contain the '|' separator, this one never does).
SCAN_CURSOR_KEY: str = "_scan"

#: Descriptor-option prefix (the ``gauge_`` precedent).
OPTION_PREFIX: str = "coverage_floor_"

#: Env-var prefix for the same knobs. Env is the base default; a descriptor
#: option, when set, always wins — the order every other option here uses.
ENV_PREFIX: str = "LEGBA_COVERAGE_FLOOR_"

#: Defensive per-scan bounds. Hitting either is reported, never silent.
_MAX_ENTITY_ROWS = 40_000
_MAX_FRAMES = 4_000
#: Same defensive shape as ``_MAX_ENTITY_ROWS``, for the frame-evidence
#: finding-title read the SA/Yemen fix (clause 6) adds — bounded independently
#: because it walks a DIFFERENT join (situations -> findings) and could in
#: principle blow up on a target with pathologically many cited findings even
#: if the entity aggregate stays small.
_MAX_FRAME_EVIDENCE_ROWS = 40_000
#: Cap on exemplar signal refs carried per alert (lineage stays skimmable —
#: alert_trigger_scan._MAX_DERIVED_REFS is the ceiling the writer applies).
_MAX_EXEMPLARS_PER_CLUSTER = 3

#: Watermark rows age out on ``updated_at``. A STANDING breach is refreshed
#: every scan, so only genuinely dead (target, entity) pairs are pruned.
_WATERMARK_PRUNE_DAYS = 90

#: SQL-side pre-filter: surface rows below this many signals cannot plausibly
#: reach ``min_signals`` even after their fold merges with siblings. Kept far
#: below the real bar deliberately — this is a transfer bound, not a threshold.
_SURFACE_MIN_SIGNALS = 3

#: Bound on how many (desk, polity) pairs get the phase-2 exact recount. On
#: the 2026-09-03 fleet the nominated set is ~40; the bound exists so a
#: pathological substrate cannot turn one scan into thousands of slice reads.
#: Hitting it is REPORTED and the overflow is dropped for this scan only — no
#: watermark is written for a pair that was never counted, so nothing is
#: silently adopted as reported.
_MAX_RECOUNT_CANDIDATES = 300

#: The separator joining a cluster's surface list into one SQL text param
#: (ASCII unit separator — cannot occur in an NER surface form).
_SURFACE_SEP = "\x1f"


# ---------------------------------------------------------------------------
# Config — every threshold in one overridable place (the GaugeConfig idiom)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageFloorConfig:
    """Every knob, calibrated against the live fleet on 2026-09-03.

    At these defaults the detector would flag 3 of 32 country targets today
    (IL / SA / TR, one cluster each after the share gate) — see
    ``planning/COVERAGE_FLOOR_82_2026-09-04.md`` for the full would-flag census
    and the threshold sweep behind each number. Loosening ``min_slice_share``
    to 0.05 flags 3 targets and 5 clusters; removing it flags 5 targets;
    dropping the country-class gate flags 17. Conservative on purpose: this
    fleet's paging was only just brought under control.
    """

    #: Trailing window the entity census and the slice denominator share.
    window_days: int = 14
    #: Distinct signals a cluster needs before it is a recurrence at all.
    min_signals: int = 20
    #: Distinct DAYS it must span. Persistence, not one loud news cycle.
    min_days: int = 10
    #: Mean salience magnitude over the cluster's signals.
    min_mean_magnitude: float = 0.50
    #: Signals that must individually clear ``high_magnitude``.
    min_high_magnitude_signals: int = 15
    #: Share of the desk's entity-bearing window slice the cluster must reach.
    min_slice_share: float = 0.10
    #: What counts as an individually-consequential signal.
    high_magnitude: float = 0.50
    #: NER span confidence floor (payload entities carry their own score).
    min_entity_confidence: float = 0.5
    #: Clusters named per target alert, worst-first by signal count.
    max_clusters_per_target: int = 5
    #: How often the heavy 14-day aggregate may actually run.
    min_scan_interval_hours: float = 6.0
    #: Clause 6 (the SA/Yemen fix, corrected 2026-09-06). Minimum DISTINCT
    #: findings — of ONE open frame's own cited evidence — whose own authored
    #: TITLE must name the missing polity before that frame is allowed to
    #: clear it. Guards against a single incidental title mention (live
    #: 2026-09-06 census: IL's one candidate frame had exactly 1 such finding
    #: of 232 attached across its 8 frames; SA's genuinely-covering Houthi
    #: frames had 2, 5 and 6). See the module banner's clause 6.
    frame_evidence_min_findings: int = 2

    @classmethod
    def from_options(
        cls, options: Mapping[str, Any] | None
    ) -> "CoverageFloorConfig":
        """Build from ``coverage_floor_*`` handler options over env defaults.

        Env supplies the base value (retunable with no deploy); an option, when
        present, wins. A value that will not coerce keeps its predecessor
        rather than raising — a mistyped knob must not take the class offline.
        """
        kwargs: dict[str, Any] = {}
        opts = dict(options or {})
        for f in fields(cls):
            value = f.default
            env_raw = os.environ.get(ENV_PREFIX + f.name.upper())
            if env_raw is not None:
                value = _coerce(f.name, env_raw, value, source="env")
            opt_key = OPTION_PREFIX + f.name
            if opt_key in opts:
                value = _coerce(f.name, opts[opt_key], value, source="option")
            kwargs[f.name] = value
        return cls(**kwargs)


def _coerce(name: str, raw: Any, fallback: Any, *, source: str) -> Any:
    try:
        return type(fallback)(raw)
    except (TypeError, ValueError):
        logger.info(
            "coverage_floor.bad_%s name=%s value=%r — keeping %r",
            source, name, raw, fallback,
        )
        return fallback


def config_from_options(options: Mapping[str, Any]) -> CoverageFloorConfig:
    """The handler-facing constructor (mirrors ``_production_deficit_scan``)."""
    return CoverageFloorConfig.from_options(options)


# ---------------------------------------------------------------------------
# Pure helpers — no DB, no LLM, fully unit-testable
#
# The POLITY MATCHER (``normalize_prose`` / ``entity_surfaces`` /
# ``represented_by`` / ``_SURFACES``) and the HOME-COUNTRY EXCLUSION
# (``_ISO2_HOME_ALIASES`` / ``_pycountry_names`` / ``home_prose`` /
# ``is_home_country``) live in :mod:`legba.data._polity_match` and are imported
# above. They are re-exported from here — including in ``__all__`` — so this
# module's public surface, and every test pinned to it, is unchanged.
# ---------------------------------------------------------------------------


@dataclass
class EntityCluster:
    """One canonical polity's whole footprint in one desk's window slice.

    Counts arrive in TWO passes and the distinction is load-bearing. The
    per-surface aggregate can only give an UPPER BOUND on distinct signals — a
    headline saying "Iran" and "Iranian" is one signal counted by two surface
    rows, which is how a naive sum produced a 200%-of-slice share the first
    time this ran. So the summed numbers are used ONLY to nominate candidates
    (a strict over-count can never hide a real breach), and
    :meth:`adopt_exact_counts` then replaces them with a single exact
    ``count(DISTINCT signal_id)`` recount before the bar is applied.
    """

    fold: str
    name: str
    n_signals: int = 0
    n_days: int = 0
    n_high: int = 0
    magnitude_weight: float = 0.0
    exemplars: list[Any] = None  # type: ignore[assignment]
    #: The lowercased raw NER surfaces that folded here — the recount's
    #: membership predicate, so phase 2 asks about exactly this cluster.
    surfaces: set[str] = None  # type: ignore[assignment]
    #: True once exact counts replaced the summed upper bound.
    exact: bool = False

    def __post_init__(self) -> None:
        if self.exemplars is None:
            self.exemplars = []
        if self.surfaces is None:
            self.surfaces = set()

    @property
    def mean_magnitude(self) -> float:
        return self.magnitude_weight / self.n_signals if self.n_signals else 0.0

    def share(self, slice_size: int) -> float:
        return self.n_signals / slice_size if slice_size > 0 else 0.0

    def adopt_exact_counts(self, row: Mapping[str, Any]) -> None:
        """Replace the summed upper bound with the exact per-signal recount."""
        self.n_signals = int(row.get("n_signals") or 0)
        self.n_days = int(row.get("n_days") or 0)
        self.n_high = int(row.get("n_high") or 0)
        self.magnitude_weight = (
            float(row.get("mean_magnitude") or 0.0) * self.n_signals
        )
        exemplars = [r for r in (row.get("exemplars") or []) if r is not None]
        if exemplars:
            self.exemplars = exemplars[:_MAX_EXEMPLARS_PER_CLUSTER]
        self.exact = True


def cluster_entities(
    rows: Iterable[Mapping[str, Any]],
    *,
    home_blob: str,
) -> dict[str, EntityCluster]:
    """Fold raw NER surface aggregates onto canonical foreign-polity clusters.

    ``rows`` are the per-(surface, ner_class) aggregates; several surfaces
    ("Iran", "Iranian", "iran") collapse onto one fold. Junk spans, non-country
    canonicalizations and anything naming the desk's OWN country are dropped
    here rather than in SQL, because the judgment is the canon's and the canon
    is Python.
    """
    out: dict[str, EntityCluster] = {}
    for row in rows:
        surface = str(row.get("surface") or "").strip()
        if not surface or is_junk_entity(surface):
            continue
        canonical, canon_class = canonicalize_entity(
            surface, str(row.get("ner_class") or "")
        )
        if canon_class != "country":
            continue
        fold = identity_fold(canonical)
        if len(fold) < 3 or is_home_country(canonical, home_blob):
            continue
        cluster = out.get(fold)
        if cluster is None:
            cluster = EntityCluster(fold=fold, name=canonical)
            out[fold] = cluster
        elif len(canonical) < len(cluster.name):
            # Prefer the shortest canonical spelling as the display name — the
            # bare country, not a stray longer surface that folded onto it.
            cluster.name = canonical
        n_signals = int(row.get("n_signals") or 0)
        cluster.n_signals += n_signals
        cluster.n_days = max(cluster.n_days, int(row.get("n_days") or 0))
        cluster.n_high += int(row.get("n_high") or 0)
        cluster.magnitude_weight += float(row.get("mean_magnitude") or 0.0) * n_signals
        cluster.surfaces.add(surface.lower())
        for ref in list(row.get("exemplars") or [])[:_MAX_EXEMPLARS_PER_CLUSTER]:
            if ref is not None and ref not in cluster.exemplars:
                cluster.exemplars.append(ref)
    return out


def candidate_clusters(
    clusters: Mapping[str, EntityCluster],
    *,
    frame_names: Sequence[str],
    config: CoverageFloorConfig,
    frame_finding_titles: Sequence[Sequence[str]] = (),
) -> list[EntityCluster]:
    """The clusters worth an exact recount — a deliberate SUPERSET of breaches.

    Only the two clauses whose summed values are strict UPPER bounds on the
    distinct-signal truth are applied here (signal count, high-magnitude
    count), plus the two FREE frame checks. Persistence, mean salience and
    slice share are NOT decided on summed numbers: a weighted mean over
    duplicated signals is neither an upper nor a lower bound, and the day
    count is a lower one. Everything that could wrongly EXCLUDE a real breach
    waits for :func:`clears_bar` on the exact recount.

    The two frame checks (clauses 5 and 6 — see the module banner): a cluster
    is excluded from nomination when EITHER an open frame's own ``name``
    represents it directly, OR (the SA/Yemen fix, corrected 2026-09-06) at
    least one open frame has ``config.frame_evidence_min_findings`` of its OWN
    cited findings' TITLES naming it. ``frame_finding_titles`` is one inner
    sequence PER OPEN FRAME of this one target — never merged across frames,
    never another target's — each holding that frame's own cited findings'
    ``title`` strings; built by the caller from :data:`_FRAME_FINDING_TITLES_SQL`.
    Defaults to ``()`` (never suppresses anything) so every existing caller —
    direct unit tests included — is unaffected.
    """
    prose = " || ".join(str(n or "") for n in frame_names)
    min_findings = config.frame_evidence_min_findings

    def _a_frame_own_evidence_clears(cluster_name: str) -> bool:
        for titles in frame_finding_titles:
            hits = sum(
                1 for title in titles if represented_by(cluster_name, title) is not None
            )
            if hits >= min_findings:
                return True
        return False

    out = [
        c
        for c in clusters.values()
        if c.n_signals >= config.min_signals
        and c.n_high >= config.min_high_magnitude_signals
        and represented_by(c.name, prose) is None
        and not _a_frame_own_evidence_clears(c.name)
    ]
    out.sort(key=lambda c: (-c.n_signals, c.fold))
    return out


def clears_bar(
    cluster: EntityCluster, *, slice_size: int, config: CoverageFloorConfig
) -> bool:
    """Clauses 2-4 of the bar (the recurrence / consequence / share gates).

    Applied to EXACT counts only — see :meth:`EntityCluster.adopt_exact_counts`
    and :func:`candidate_clusters` for why the summed pass cannot be trusted
    with the share.
    """
    return (
        cluster.n_signals >= config.min_signals
        and cluster.n_days >= config.min_days
        and cluster.mean_magnitude >= config.min_mean_magnitude
        and cluster.n_high >= config.min_high_magnitude_signals
        and cluster.share(slice_size) >= config.min_slice_share
    )


def watermark_key(target_id: str, fold: str) -> str:
    return f"{target_id}|{fold}"


def build_body(
    target_id: str,
    new_clusters: Sequence[EntityCluster],
    *,
    standing: Sequence[EntityCluster],
    frame_names: Sequence[str],
    slice_size: int,
    config: CoverageFloorConfig,
) -> str:
    """The alert body: what the evidence says, what the register says, and an
    explicit statement of what this is NOT.

    The last part is not decoration. This class asserts a gap between two of
    the engine's own artifacts; it does NOT assert that the desk's read is
    wrong, and an operator must be able to tell those apart from the page.
    """
    lines = [
        f"DESK: {target_id}",
        (
            f"EVIDENCE: {slice_size} entity-bearing signal(s) in the trailing "
            f"{config.window_days} day(s)."
        ),
        (
            f"REGISTER: {len(frame_names)} open frame(s), none of which names "
            f"the polity/polities below."
        ),
        "",
        "UNCOVERED (new this scan, worst first):",
    ]
    for c in new_clusters:
        lines.append(
            f"  - {c.name}: {c.n_signals} signal(s) over {c.n_days} day(s), "
            f"mean salience {c.mean_magnitude:.2f}, {c.n_high} at or above "
            f"{config.high_magnitude:.2f}, "
            f"{c.share(slice_size) * 100:.1f}% of this desk's slice"
        )
    already = [c for c in standing if c not in new_clusters]
    if already:
        lines += [
            "",
            "ALSO STANDING (already reported, not re-paged):",
            "  " + ", ".join(f"{c.name} ({c.n_signals})" for c in already),
        ]
    lines += [
        "",
        "OPEN FRAMES CHECKED:",
    ]
    lines += [f"  - {str(n)[:160]}" for n in frame_names[:12]]
    if len(frame_names) > 12:
        lines.append(f"  ... and {len(frame_names) - 12} more")
    lines += [
        "",
        (
            "This is a COVERAGE gap between this desk's own signal evidence "
            "and its own situation register — not a claim that the desk's read "
            "is wrong, and not a claim about the world. The register is where "
            "a desk's continuity comes from (unit_grounding's OPEN SITUATION "
            "REGISTER block), so a story with no frame competes at a standing "
            "disadvantage against one that has months of it. See "
            "planning/CAMPAIGN_2026-08-29/IL_BLINDNESS_DIAGNOSIS.md."
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

#: Per-(desk, NER surface, NER class) aggregate over the desk's own geo slice.
#: The inner DISTINCT collapses a surface repeated inside ONE signal's entity
#: array, so a headline naming Iran three times counts once toward the mean.
_ENTITY_AGG_SQL = """
    WITH tg AS (
        SELECT td.descriptor_id AS target_id,
               ARRAY(
                   SELECT jsonb_array_elements_text(td.body -> 'scope' -> 'geo')
               ) AS geo
          FROM target_descriptors td
         WHERE td.is_head = TRUE
           AND td.descriptor_id = ANY($1::text[])
    ), hit AS (
        SELECT DISTINCT
               tg.target_id                                       AS target_id,
               e ->> 'text'                                       AS surface,
               e ->> 'class'                                      AS ner_class,
               COALESCE((s.salience ->> 'magnitude')::float, 0.0)  AS magnitude,
               s.fetched_at                                       AS fetched_at,
               s.id                                               AS signal_id
          FROM tg
          JOIN signals s
            ON s.geo && tg.geo
           AND s.fetched_at > now() - make_interval(days => $2)
           AND jsonb_typeof(s.payload -> 'entities') = 'array'
          CROSS JOIN LATERAL jsonb_array_elements(s.payload -> 'entities') e
         WHERE COALESCE((e ->> 'confidence')::float, 1.0) >= $3
    )
    SELECT target_id,
           surface,
           ner_class,
           count(*)                                              AS n_signals,
           count(DISTINCT date_trunc('day', fetched_at))         AS n_days,
           avg(magnitude)::float                                 AS mean_magnitude,
           count(*) FILTER (WHERE magnitude >= $4)               AS n_high,
           (array_agg(signal_id ORDER BY magnitude DESC))[1:3]   AS exemplars
      FROM hit
     GROUP BY 1, 2, 3
    HAVING count(*) >= $5
     ORDER BY target_id, n_signals DESC
     LIMIT $6
"""

#: PHASE 2 — the exact recount, for the handful of (desk, polity) pairs the
#: summed pass nominated. One row per SIGNAL (the surface set is an EXISTS
#: predicate, never a join), so ``count(DISTINCT s.id)``, the day span and the
#: mean magnitude are per-signal truths rather than per-mention sums. This is
#: what makes ``slice_share`` a share: the summed pass had IL's Iran cluster at
#: 200% of its own slice, because "Iran" and "Iranian" are usually the same
#: headline.
_EXACT_COUNTS_SQL = """
    WITH tg AS (
        SELECT td.descriptor_id AS target_id,
               ARRAY(
                   SELECT jsonb_array_elements_text(td.body -> 'scope' -> 'geo')
               ) AS geo
          FROM target_descriptors td
         WHERE td.is_head = TRUE
           AND td.descriptor_id = ANY($1::text[])
    ), cand AS (
        SELECT u.target_id,
               u.fold,
               -- separator: ASCII unit separator (chr 31), which
               -- cannot occur inside an NER surface form
               string_to_array(u.surfaces, chr(31)) AS surfaces
          FROM unnest($2::text[], $3::text[], $4::text[])
                 AS u(target_id, fold, surfaces)
    )
    SELECT c.target_id,
           c.fold,
           count(*)                                             AS n_signals,
           count(DISTINCT date_trunc('day', s.fetched_at))      AS n_days,
           avg(COALESCE((s.salience ->> 'magnitude')::float, 0.0))::float
                                                                AS mean_magnitude,
           count(*) FILTER (
               WHERE COALESCE((s.salience ->> 'magnitude')::float, 0.0) >= $6
           )                                                    AS n_high,
           (array_agg(
               s.id ORDER BY COALESCE((s.salience ->> 'magnitude')::float, 0.0)
                             DESC
           ))[1:3]                                              AS exemplars
      FROM cand c
      JOIN tg ON tg.target_id = c.target_id
      JOIN signals s
        ON s.geo && tg.geo
       AND s.fetched_at > now() - make_interval(days => $5)
       AND jsonb_typeof(s.payload -> 'entities') = 'array'
       AND EXISTS (
             SELECT 1
               FROM jsonb_array_elements(s.payload -> 'entities') e
              WHERE lower(e ->> 'text') = ANY(c.surfaces)
                AND COALESCE((e ->> 'confidence')::float, 1.0) >= $7
       )
     GROUP BY 1, 2
"""

#: The denominator: signals in the desk's window that COULD have carried an
#: entity. Using the entity-bearing count (not the raw count) keeps the share
#: honest on a desk whose NER coverage is partial — the detector must not read
#: an un-enriched slice as a smaller story.
_SLICE_SIZE_SQL = """
    WITH tg AS (
        SELECT td.descriptor_id AS target_id,
               ARRAY(
                   SELECT jsonb_array_elements_text(td.body -> 'scope' -> 'geo')
               ) AS geo
          FROM target_descriptors td
         WHERE td.is_head = TRUE
           AND td.descriptor_id = ANY($1::text[])
    )
    SELECT tg.target_id AS target_id,
           count(*) FILTER (
               WHERE jsonb_typeof(s.payload -> 'entities') = 'array'
                 AND jsonb_array_length(s.payload -> 'entities') > 0
           ) AS n_entity_signals,
           count(*) AS n_signals
      FROM tg
      JOIN signals s
        ON s.geo && tg.geo
       AND s.fetched_at > now() - make_interval(days => $2)
     GROUP BY 1
"""

#: The SAME open-frame predicate ``meta_findings_synthesizer.
#: read_open_situations`` uses (superseded / temporal / non-closed), because
#: this check must measure the register a desk actually reads, not a private
#: definition of "open".
_OPEN_FRAMES_SQL = """
    SELECT target_id, name, status, intensity_score
      FROM situations
     WHERE superseded_by IS NULL
       AND (valid_until IS NULL OR valid_until > now())
       AND status <> 'closed'
       AND target_id = ANY($1::text[])
     ORDER BY target_id, intensity_score DESC NULLS LAST
     LIMIT $2
"""

#: THE SA/YEMEN FIX (clause 6, 2026-09-05; CORRECTED 2026-09-06 — see the
#: module banner for the over-suppression this replaced and why). Every
#: FINDING an OPEN FRAME actually CITES as its own evidence (``situations.
#: derived_from``, finding ids per ``situation_tracker._NEW_EVIDENCE_SQL``'s
#: own comment — never another frame's findings, never the desk's findings
#: at large), paired with that finding's own ``id`` and ``title``. FRAME
#: IDENTITY is carried all the way through (``frame_id`` never collapses into
#: ``target_id``) so the caller can require the recurrence bar — clause 6's
#: predicate is decided per FRAME, never on a desk-wide pool. Window-scoped
#: on the finding's OWN ``produced_at`` (rather than a further signal hop)
#: because the finding's authored title, not its raw grounding population, is
#: what this clause reads.
_FRAME_FINDING_TITLES_SQL = """
    WITH frames AS (
        SELECT id AS frame_id, target_id, derived_from
          FROM situations
         WHERE superseded_by IS NULL
           AND (valid_until IS NULL OR valid_until > now())
           AND status <> 'closed'
           AND target_id = ANY($1::text[])
    ), finding_ids AS (
        SELECT DISTINCT frame_id, target_id, unnest(derived_from) AS finding_id
          FROM frames
    )
    SELECT fi.frame_id AS frame_id, fi.target_id AS target_id,
           ao.id AS finding_id, ao.title AS title
      FROM finding_ids fi
      JOIN analyst_outputs ao
        ON ao.id = fi.finding_id
       AND ao.kind = 'finding'
       AND ao.produced_at > now() - make_interval(days => $2)
     LIMIT $3
"""

_HOME_COUNTRIES_SQL = """
    SELECT iso2, name FROM iso_countries WHERE iso2 = ANY($1::text[])
"""

_PRUNE_SQL = """
    DELETE FROM alert_trigger_watermarks
     WHERE trigger_class = $1
       AND watermark_key <> $2
       AND watermark_key <> $3
       AND updated_at < now() - make_interval(days => $4)
"""


def _parse_iso(raw: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _interval_elapsed(
    cursor_state: Mapping[str, Any] | None, *, now: datetime, hours: float
) -> bool:
    """True when the heavy read is due. No cursor / unparseable cursor => due
    (degrade toward DOING the work, never toward silently skipping it)."""
    if hours <= 0 or not cursor_state:
        return True
    last = _parse_iso(cursor_state.get("last_scan_at"))
    if last is None:
        return True
    return now - last >= timedelta(hours=hours)


async def _count_naming_only_breaches(
    conn: Any,
    *,
    naming_only: Sequence[tuple[str, EntityCluster]],
    already_counted: set[tuple[str, str]],
    exact_by_pair: Mapping[tuple[str, str], Mapping[str, Any]],
    uncounted: set[tuple[str, str]],
    desk_ids: Sequence[str],
    slice_size: Mapping[str, int],
    config: CoverageFloorConfig,
    stats: dict[str, Any],
) -> int:
    """R1-e's B-1 producer: the breach count with clause 6 removed.

    ``naming_only`` is phase 1 re-run with ``frame_finding_titles=()``. Its
    clusters overlap the real pass's — those already carry the exact recount
    and are READ, never re-adopted. The remainder (exactly the pairs clause 6
    suppressed) get one more bounded :data:`_EXACT_COUNTS_SQL` fetch and are
    scored on ``dataclasses.replace`` COPIES, so no object the real pipeline
    holds is touched by this count. Pairs the main recount bound dropped are
    skipped here for the same reason they are skipped there: a pair that was
    never counted is not a fact about the register.

    Returns the count. Writes nothing, watermarks nothing, pages nothing.
    """
    if not naming_only:
        return 0
    extras = [
        (t, c) for t, c in naming_only if (t, c.fold) not in already_counted
    ]
    if len(extras) > _MAX_RECOUNT_CANDIDATES:
        stats["naming_only_recount_bound_hit"] = 1
        logger.warning(
            "coverage_floor_scan.naming_only_bound_hit extras=%d bound=%d — "
            "the overflow is DROPPED from breaches_naming_only only; the "
            "detector's own counts are unaffected",
            len(extras), _MAX_RECOUNT_CANDIDATES,
        )
        extras = extras[:_MAX_RECOUNT_CANDIDATES]
    extra_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    if extras:
        rows = await conn.fetch(
            _EXACT_COUNTS_SQL,
            list(desk_ids),
            [t for t, _c in extras],
            [c.fold for _t, c in extras],
            [_SURFACE_SEP.join(sorted(c.surfaces)) for _t, c in extras],
            int(config.window_days),
            float(config.high_magnitude),
            float(config.min_entity_confidence),
        )
        extra_by_pair = {
            (str(r["target_id"]), str(r["fold"])): r for r in rows
        }
    counted = 0
    for target_id, cluster in naming_only:
        pair = (target_id, cluster.fold)
        if pair in uncounted:
            continue
        if pair in exact_by_pair:
            probe = cluster  # already adopted by the real pass; read only
        elif pair in extra_by_pair:
            probe = replace(
                cluster,
                exemplars=list(cluster.exemplars),
                surfaces=set(cluster.surfaces),
            )
            probe.adopt_exact_counts(extra_by_pair[pair])
        else:
            # The recount found nothing the summed pass thought was there, or
            # the bound dropped it. Believe the recount, exactly as above.
            continue
        if clears_bar(
            probe, slice_size=slice_size.get(target_id, 0), config=config
        ):
            counted += 1
    return counted


async def scan_coverage_floor(
    conn: Any,
    *,
    config: CoverageFloorConfig | None = None,
    geo_config: GeoRoutingConfig | None = None,
    now: datetime | None = None,
) -> tuple[list[Any], list[tuple[str, str, dict[str, Any]]], bool, dict[str, Any]]:
    """Scan every country desk for a coverage-floor breach.

    Returns the 4-tuple ``(candidates, silent_watermarks, was_seeded, stats)``
    every sibling scan returns, so ``alert_trigger_scan.handle`` folds it in
    with no special-casing.
    """
    from .alert_trigger_scan import (
        _DESKS_SQL,
        _MAX_DERIVED_REFS,
        _MAX_DESKS,
        SEED_KEY,
        AlertCandidate,
        _load_class_watermarks,
        _parse_jsonish,
    )

    cfg = config or CoverageFloorConfig()
    geo_cfg = geo_config or GeoRoutingConfig()
    now = now or datetime.now(timezone.utc)
    stats: dict[str, Any] = {
        "targets": 0,
        "clusters": 0,
        "breaches": 0,
        "new_breaches": 0,
        "resolved": 0,
        "seeded": 0,
        "nominated": 0,
        #: R1-e's B-1 producer — the same bar with clause 6 removed. See the
        #: module banner's NAMING-ONLY COUNTER section. Declared here so the
        #: receipt's shape is identical on a skipped tick and a real one.
        "nominated_naming_only": 0,
        "breaches_naming_only": 0,
        "naming_only_recount_bound_hit": 0,
        #: THE ROUTING GAP, made visible. High-magnitude signals whose title
        #: names a desk's own polity and whose ``geo`` put them outside that
        #: desk's slice — the class clause 4 (slice share) structurally cannot
        #: see, because the material never reached the slice to be a share OF.
        #: Reported, never gated: see ``_count_routed_elsewhere``. Declared
        #: here so the receipt's shape is identical on a skipped tick.
        "routed_elsewhere": 0,
        "routed_elsewhere_by_target": {},
        "routed_elsewhere_bound_hit": 0,
        "skipped_interval": 0,
        "entity_row_bound_hit": 0,
        "frame_evidence_row_bound_hit": 0,
        "recount_bound_hit": 0,
        "no_home_gazetteer": 0,
        "unavailable": 0,
    }

    try:
        seeded, marks = await _load_class_watermarks(conn, TRIGGER_CLASS)
        cursor = marks.get(SCAN_CURSOR_KEY)
        if not _interval_elapsed(
            cursor, now=now, hours=cfg.min_scan_interval_hours
        ):
            stats["skipped_interval"] = 1
            return [], [], seeded, stats

        desks = await conn.fetch(_DESKS_SQL, _MAX_DESKS)
        desk_ids = [str(r["descriptor_id"]) for r in desks]
        if not desk_ids:
            return [], [], seeded, stats

        entity_rows = await conn.fetch(
            _ENTITY_AGG_SQL,
            desk_ids,
            int(cfg.window_days),
            float(cfg.min_entity_confidence),
            float(cfg.high_magnitude),
            int(_SURFACE_MIN_SIGNALS),
            int(_MAX_ENTITY_ROWS),
        )
        slice_rows = await conn.fetch(
            _SLICE_SIZE_SQL, desk_ids, int(cfg.window_days)
        )
        frame_rows = await conn.fetch(_OPEN_FRAMES_SQL, desk_ids, int(_MAX_FRAMES))
        frame_evidence_rows = await conn.fetch(
            _FRAME_FINDING_TITLES_SQL,
            desk_ids,
            int(cfg.window_days),
            int(_MAX_FRAME_EVIDENCE_ROWS),
        )
        geo_codes = sorted(
            {
                str(g)
                for r in desks
                for g in (_parse_jsonish(r["geo"]) or [])
                if isinstance(g, str)
            }
        )
        home_rows = await conn.fetch(_HOME_COUNTRIES_SQL, geo_codes)
    except Exception as exc:  # noqa: BLE001 — a broken class must SAY so
        # Degrade LOUD and empty rather than killing the other classes: a
        # substrate without iso_countries / salience takes THIS class offline,
        # never the whole alert scan.
        if type(exc).__name__ != "UndefinedTableError":
            raise
        logger.warning(
            "coverage_floor_scan.unavailable — a table this class reads is "
            "not present (%s); the class scanned nothing", exc,
        )
        stats["unavailable"] = 1
        return [], [], True, stats

    if len(entity_rows) >= _MAX_ENTITY_ROWS:
        stats["entity_row_bound_hit"] = 1
    if len(frame_evidence_rows) >= _MAX_FRAME_EVIDENCE_ROWS:
        stats["frame_evidence_row_bound_hit"] = 1

    iso_to_name = {str(r["iso2"]): str(r["name"]) for r in home_rows}
    by_target_entities: dict[str, list[Mapping[str, Any]]] = {}
    for row in entity_rows:
        by_target_entities.setdefault(str(row["target_id"]), []).append(row)
    slice_size = {
        str(r["target_id"]): int(r["n_entity_signals"] or 0) for r in slice_rows
    }
    frames_by_target: dict[str, list[str]] = {}
    for row in frame_rows:
        frames_by_target.setdefault(str(row["target_id"]), []).append(
            str(row["name"] or "")
        )
    #: The SA/Yemen fix's own data, kept FRAME-FENCED all the way through: each
    #: open frame's own cited findings' titles, never merged across a desk's
    #: frames. See ``_FRAME_FINDING_TITLES_SQL``.
    frame_titles_by_frame: dict[str, list[str]] = {}
    frame_ids_by_target: dict[str, set[str]] = {}
    for row in frame_evidence_rows:
        frame_id = str(row["frame_id"])
        title = str(row["title"] or "").strip()
        if title:
            frame_titles_by_frame.setdefault(frame_id, []).append(title)
        frame_ids_by_target.setdefault(str(row["target_id"]), set()).add(frame_id)

    # PHASE 1 — fold every desk's surface aggregates onto polity clusters and
    # NOMINATE the ones worth an exact recount (a deliberate superset).
    clusters_by_target: dict[str, dict[str, EntityCluster]] = {}
    nominated: list[tuple[str, EntityCluster]] = []
    #: R1-e — the same phase-1 nomination with clause 6 removed. Held apart
    #: from ``nominated`` in every way: it never reaches the recount bound's
    #: arithmetic, the watermarks, the candidates or the dispatch leg. See the
    #: module banner's NAMING-ONLY COUNTER.
    naming_only: list[tuple[str, EntityCluster]] = []
    for desk_row in desks:
        target_id = str(desk_row["descriptor_id"])
        geo_raw = _parse_jsonish(desk_row["geo"])
        iso2s = [str(g) for g in (geo_raw or []) if isinstance(g, str)]
        blob = home_prose(
            iso2s, [iso_to_name[g] for g in iso2s if g in iso_to_name]
        )
        if not blob:
            # Neither gazetteer knows this desk's geo => the home-country
            # exclusion cannot be applied, and without it the desk's OWN name
            # would breach its own register. Skip rather than lie; the desk is
            # visible in the receipt as the gap between `targets` and the
            # descriptor count.
            stats["no_home_gazetteer"] = stats.get("no_home_gazetteer", 0) + 1
            continue
        stats["targets"] += 1
        clusters = cluster_entities(
            by_target_entities.get(target_id, ()), home_blob=blob
        )
        stats["clusters"] += len(clusters)
        clusters_by_target[target_id] = clusters
        frame_finding_titles = [
            frame_titles_by_frame.get(fid, [])
            for fid in sorted(frame_ids_by_target.get(target_id, ()))
        ]
        for cluster in candidate_clusters(
            clusters,
            frame_names=frames_by_target.get(target_id, []),
            frame_finding_titles=frame_finding_titles,
            config=cfg,
        ):
            nominated.append((target_id, cluster))
        # THE SECOND PASS — clause 6 removed (``frame_finding_titles`` left at
        # its default ``()``, which suppresses nothing), clause 5 unchanged.
        # Runs HERE, in phase 1, because phase 2 mutates cluster counts in
        # place: nominating off already-recounted numbers would ask a
        # different question of a different bar.
        for cluster in candidate_clusters(
            clusters,
            frame_names=frames_by_target.get(target_id, []),
            config=cfg,
        ):
            naming_only.append((target_id, cluster))

    # THE ROUTING-GAP RECEIPT. Report-only, held apart from every gating path
    # in the same way ``breaches_naming_only`` is: it reaches no watermark, no
    # candidate, no dispatch and no threshold. Degrade-not-drop — a failure
    # here leaves the counter at 0 and the detector completely unaffected,
    # because a reporting line must never be able to take the scan offline.
    try:
        routed = await count_routed_elsewhere(
            conn, desks=desks, parse_jsonish=_parse_jsonish,
            window_days=int(cfg.window_days),
            high_magnitude=float(cfg.high_magnitude),
            config=geo_cfg, stats=stats,
        )
        stats["routed_elsewhere"] = sum(routed.values())
        stats["routed_elsewhere_by_target"] = dict(
            sorted(routed.items(), key=lambda kv: (-kv[1], kv[0]))[
                :MAX_ROUTED_TARGETS_IN_RECEIPT
            ]
        )
    except Exception:                                       # pragma: no cover
        logger.exception(
            "coverage_floor_scan.routed_elsewhere_failed — reported as 0; "
            "the detector's own counts are unaffected"
        )

    # PHASE 2 — one exact per-signal recount for every nominated pair, then
    # the real bar. Everything before this point was an upper bound.
    stats["nominated"] = len(nominated)
    stats["nominated_naming_only"] = len(naming_only)
    #: Pairs the bound dropped BEFORE they were ever counted. They must not be
    #: treated as resolved further down — "we ran out of budget" and "the
    #: register finally names it" are different facts, and conflating them
    #: would silently re-arm a live breach.
    uncounted: set[tuple[str, str]] = set()
    if len(nominated) > _MAX_RECOUNT_CANDIDATES:
        stats["recount_bound_hit"] = 1
        logger.warning(
            "coverage_floor_scan.recount_bound_hit nominated=%d bound=%d — "
            "the overflow is DROPPED for this scan (never watermarked, so "
            "nothing is adopted as reported)",
            len(nominated), _MAX_RECOUNT_CANDIDATES,
        )
        uncounted = {
            (t, c.fold) for t, c in nominated[_MAX_RECOUNT_CANDIDATES:]
        }
        nominated = nominated[:_MAX_RECOUNT_CANDIDATES]
    exact_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    if nominated:
        exact_rows = await conn.fetch(
            _EXACT_COUNTS_SQL,
            desk_ids,
            [t for t, _c in nominated],
            [c.fold for _t, c in nominated],
            [_SURFACE_SEP.join(sorted(c.surfaces)) for _t, c in nominated],
            int(cfg.window_days),
            float(cfg.high_magnitude),
            float(cfg.min_entity_confidence),
        )
        exact_by_pair = {
            (str(r["target_id"]), str(r["fold"])): r for r in exact_rows
        }
    breaches_by_target: dict[str, list[EntityCluster]] = {}
    for target_id, cluster in nominated:
        exact = exact_by_pair.get((target_id, cluster.fold))
        if exact is None:
            # The recount found nothing the summed pass thought was there.
            # Believe the recount: it is the per-signal truth.
            continue
        cluster.adopt_exact_counts(exact)
        if clears_bar(
            cluster, slice_size=slice_size.get(target_id, 0), config=cfg
        ):
            breaches_by_target.setdefault(target_id, []).append(cluster)
    for target_breaches in breaches_by_target.values():
        target_breaches.sort(key=lambda c: (-c.n_signals, c.fold))

    # R1-e — the NAMING-ONLY count, on the same recount and the same bar.
    # Nothing below this block is read by anything else in this function.
    stats["breaches_naming_only"] = await _count_naming_only_breaches(
        conn,
        naming_only=naming_only,
        already_counted={(t, c.fold) for t, c in nominated} | uncounted,
        exact_by_pair=exact_by_pair,
        uncounted=uncounted,
        desk_ids=desk_ids,
        slice_size=slice_size,
        config=cfg,
        stats=stats,
    )

    # R-B (RESEARCH_PROGRAM_SPEC §2.2) — the DISPATCH leg. Behind the program
    # flag: at LEGBA_RESEARCH_EVIDENCE=off this block runs NO query, builds NO
    # payload and every candidate below is byte-identical to its pre-program
    # self (which tests/data_pkg/test_coverage_floor_scan.py proves by passing
    # unchanged). The two reads resolve F-6's "which desk's bounded question
    # does a target-scoped gap carry" and are made ONCE per scan for every
    # breaching target, not per cluster.
    dispatch_on = research_evidence_enabled()
    dispatch_ctx: dict[str, dict[str, str]] = {}
    if breaches_by_target and dispatch_on:
        try:
            dispatch_ctx = await _research_dispatch.resolve_dispatch_context(
                conn, sorted(breaches_by_target)
            )
        except Exception as exc:  # noqa: BLE001 — dispatch never costs a detector
            logger.warning("coverage_floor_scan.dispatch_context_failed err=%s", exc)
            dispatch_ctx = {}

    candidates: list[Any] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for desk_row in desks:
        target_id = str(desk_row["descriptor_id"])
        if target_id not in clusters_by_target:
            continue
        n_slice = slice_size.get(target_id, 0)
        frame_names = frames_by_target.get(target_id, [])
        breaches = breaches_by_target.get(target_id, [])
        stats["breaches"] += len(breaches)
        breach_folds = {c.fold for c in breaches}

        # Resolved breaches: previously marked, no longer breaching -> re-arm.
        for key, state in marks.items():
            if not key.startswith(f"{target_id}|") or not isinstance(state, dict):
                continue
            fold = key.split("|", 1)[1]
            if (target_id, fold) in uncounted:
                continue  # dropped by the recount bound, not resolved
            if state.get("breached") and fold not in breach_folds:
                stats["resolved"] += 1
                silent.append((TRIGGER_CLASS, key, {"breached": False}))

        if not breaches:
            continue
        new_clusters = []
        for cluster in breaches:
            key = watermark_key(target_id, cluster.fold)
            prev = marks.get(key)
            state = {
                "breached": True,
                "n_signals": cluster.n_signals,
                "mean_magnitude": round(cluster.mean_magnitude, 4),
                "name": cluster.name,
            }
            if not seeded:
                stats["seeded"] += 1
                silent.append((TRIGGER_CLASS, key, state))
                continue
            if isinstance(prev, dict) and prev.get("breached"):
                # STANDING: refresh silently so the age-out prune can never
                # delete a live breach and let it page again as if new.
                silent.append((TRIGGER_CLASS, key, state))
                continue
            new_clusters.append((cluster, key, state))

        if not new_clusters:
            continue
        stats["new_breaches"] += len(new_clusters)
        shown = new_clusters[: cfg.max_clusters_per_target]
        deferred = new_clusters[cfg.max_clusters_per_target:]
        # Every NEW breach's watermark rides the candidate — including any past
        # the display cap, which are named in the payload's cluster list rather
        # than the body. Reported is reported; none of them re-pages.
        refs: list[Any] = []
        for cluster, _key, _state in shown:
            for ref in cluster.exemplars:
                if ref not in refs and len(refs) < _MAX_DERIVED_REFS:
                    refs.append(ref)
        names = ", ".join(c.name for c, _k, _s in shown)
        # R-B — one dispatch descriptor per NEWLY breaching cluster (including
        # any past the body display cap: reported is reported, and a gap the
        # body could not name is still a gap the researcher should investigate).
        # Built here, WRITTEN post-persist in alert_trigger_scan once the alert
        # row id exists. Empty list when the flag is off ⇒ nothing to write.
        research_dispatch: list[dict[str, Any]] = []
        if dispatch_on:
            iso2s = [
                str(g)
                for g in (_parse_jsonish(desk_row["geo"]) or [])
                if isinstance(g, str)
            ]
            for cluster, _key, _state in new_clusters:
                research_dispatch.append(
                    _research_dispatch.dispatch_payload_for_cluster(
                        target_id=target_id,
                        geo=iso2s,
                        cluster={
                            "name": cluster.name,
                            "entity_fold": cluster.fold,
                            "n_signals": cluster.n_signals,
                            "n_days": cluster.n_days,
                            "mean_magnitude": round(cluster.mean_magnitude, 4),
                            "slice_share": round(cluster.share(n_slice), 4),
                            "exemplar_signal_ids": [
                                str(r) for r in cluster.exemplars
                            ],
                        },
                        open_frame_count=len(frame_names),
                        rising_edge=now,
                        context=dispatch_ctx.get(target_id),
                    )
                )
        candidates.append(
            AlertCandidate(
                trigger_class=TRIGGER_CLASS,
                severity=SEVERITY,
                title=(
                    f"Coverage floor: {target_id}'s evidence names "
                    f"{names} — no open frame does"
                )[:2048],
                body=build_body(
                    target_id,
                    [c for c, _k, _s in shown],
                    standing=breaches,
                    frame_names=frame_names,
                    slice_size=n_slice,
                    config=cfg,
                ),
                target_id=target_id,
                derived_from=refs,
                data={
                    "trigger_class": TRIGGER_CLASS,
                    "target_id": target_id,
                    "window_days": cfg.window_days,
                    "slice_entity_signals": n_slice,
                    "open_frame_count": len(frame_names),
                    "open_frame_names": [n[:200] for n in frame_names[:12]],
                    "new_breach_count": len(new_clusters),
                    "standing_breach_count": len(breaches),
                    "clusters_deferred_from_body": len(deferred),
                    "clusters": [
                        {
                            "name": c.name,
                            "entity_fold": c.fold,
                            "n_signals": c.n_signals,
                            "n_days": c.n_days,
                            "mean_magnitude": round(c.mean_magnitude, 4),
                            "high_magnitude_signals": c.n_high,
                            "slice_share": round(c.share(n_slice), 4),
                            "exemplar_signal_ids": [str(r) for r in c.exemplars],
                        }
                        for c, _k, _s in new_clusters
                    ],
                    "thresholds": {
                        f.name: getattr(cfg, f.name)
                        for f in fields(CoverageFloorConfig)
                    },
                },
                watermarks=[(TRIGGER_CLASS, k, s) for _c, k, s in new_clusters],
                research_dispatch=research_dispatch,
            )
        )

    silent.append(
        (TRIGGER_CLASS, SCAN_CURSOR_KEY, {"last_scan_at": now.isoformat()})
    )
    if stats["seeded"]:
        logger.warning(
            "coverage_floor_scan.seeded n=%d — adopted the standing coverage "
            "gaps WITHOUT paging (the 0091 seed contract)",
            stats["seeded"],
        )
    await conn.execute(
        _PRUNE_SQL,
        TRIGGER_CLASS,
        SEED_KEY,
        SCAN_CURSOR_KEY,
        _WATERMARK_PRUNE_DAYS,
    )
    return candidates, silent, seeded, stats


__all__ = [
    "CoverageFloorConfig",
    "EntityCluster",
    "OPTION_PREFIX",
    "SEVERITY",
    "TRIGGER_CLASS",
    "UNVERIFIED_REASON",
    "build_body",
    "candidate_clusters",
    "clears_bar",
    "cluster_entities",
    "config_from_options",
    "entity_surfaces",
    "home_prose",
    "is_home_country",
    "normalize_prose",
    "represented_by",
    "scan_coverage_floor",
    "watermark_key",
]
