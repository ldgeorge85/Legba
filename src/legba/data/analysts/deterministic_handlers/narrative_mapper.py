# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``narrative_mapper`` sub-handler — P4-1 reified narratives + P4-2 source-echo
graph (planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md §A11).

The substrate-native narrative analysis the operator amended in: NOT
account-level platform forensics (blocked on firehose data we do not hold) but
the reification of NARRATIVES over OUR own graph — the same philosophy as the
signed ``nexuses`` and the contention arbiter.

What a narrative is
-------------------
A narrative = a CONTESTED-CLAIM FAMILY: a claim + its variants. That is exactly
one ``fact_contention`` group (migration 0055/0097) — a disputed
``(subject_key, predicate_key)`` whose competing ``fact_contention_values``
clusters ARE the variants. The arbiter already finds these; this handler
enriches each with the PROPAGATION dimension the arbiter does not compute,
resolved through the SAME lineage the arbiter counts on
(``fact_contention_values.supporting_fact_ids -> facts.derived_from ->
signals.source_id``, with the carrier's publish time at
``signals.payload->>'published_at'``, ``fetched_at`` as proxy):

  * carrier sources (who carries any variant);
  * first-seen per source (earliest carrier publish time);
  * echo lag (who published first — the LEAD — and who followed, at what delay);
  * per-source co-carriage ordering (the propagation edges, lead -> echoes).

P4-2 — the source-echo graph
----------------------------
A DIRECTED aggregate over the whole narrative population (``narrative_echo_edges``),
the descriptive counterpart of ``structural_balance`` reading the nexus graph:
leader_source -> follower_source, "across the narratives both carried, how often
did the follower publish AFTER the leader, and within ``echo_window_hours``?".
Two directed rows per pair carry the SAME ``co_carried`` but their OWN
``lead_count`` / lags / ``echo_ratio`` — the asymmetry IS the signal. A high
``echo_ratio`` at a small lag reads "source B systematically echoes source A
within N hours".

Honesty (carried verbatim on the finding + the /v3/narratives route + mig 0102)
------------------------------------------------------------------------------
  * DETECT-ONLY. Reads the contention sidecar + lineage; writes ONLY the two
    derived tables (``narratives`` / ``narrative_echo_edges``) + this summary
    finding. NEVER mutates a fact, a contention, or a value cluster (the
    arbiter's never-mutate-facts invariant B15, extended to its projection).
  * ECHO-LEAD IS DESCRIPTIVE, NOT CAUSAL. "B published after A within N hours"
    is observable publish-order TIMING — not evidence B copied A, and not a
    coordination claim. Both may draw on a common wire, a shared origin event,
    or independent reporting. Nothing here asserts coordination beyond
    co-carriage timing.
  * PUBLISH TIME IS BEST-EFFORT — a two-tier honesty split (geo_convergence_scan
    precedent). A narrative's first/last-seen may fall back to ``fetched_at``,
    but the echo GRAPH is built ONLY from PUBLISH-DATED carriers (both sides
    carry a real ``published_at``): a fetch-time-only pair NEVER mints an echo
    edge (we fetch many sources in one poll batch regardless of their publish
    order — fetch order is not publish order).

Recomputable readout (source_track_records precedent, 0099)
-----------------------------------------------------------
Every stored column is DERIVED and fully recomputable; each run refreshes both
tables WHOLESALE (upsert the current reified set, prune what is gone) — a LIVE
readout with no supersession chain (history lives in the trace). A ``deterministic``
META analyst on a DAILY cadence (after the arbiter's pass so the sidecar is
fresh); the honest per-run distribution IS the measurement product (the
source_track_record / fact_decay_scan precedent), so it stays a genuine FINDING —
which keeps this handler in the FINDING-emitting set the STRUCTURAL_VERIFY_EXEMPT
drift guard asserts. Registered via ``scripts/bringup_register_narrative_mapper.py``
(descriptor ``descriptors/analyst_narrative_mapper.yaml``, ships ``state: draft``).

Seams (documented, deliberately NOT wired here)
-----------------------------------------------
  * ``narrative_coordination`` input: the LLM unit could ground on this sidecar
    via a new ``"narratives"`` grounding-source token
    (``analyst_deps_builder.py`` sources dispatch + a ``grounding.py`` block
    builder). Additive and out of P4 scope — left as a seam.
  * A6 state-media echo families: each carrier could be tagged with its
    ``source_ratings.rubric->>'state_affiliation'`` (mig 0094) so an operator
    can read "state-media echo family"; the echo graph already surfaces the
    relationships, so this is display enrichment, not new detection.
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "narrative_mapper"

# ---------------------------------------------------------------------------
# Tunables.
# ---------------------------------------------------------------------------

#: Contention statuses reified as narratives. 'contested' (active dispute — the
#: most interesting, actively-propagating narrative) AND 'surfaced' (a winner
#: has been surfaced but the variants still spanned real sources). 'collapsed'
#: groups are no longer a live family and are excluded.
REIFIED_STATUSES: tuple[str, ...] = ("contested", "surfaced")

#: Echo window: a follower publishing within this many hours of the leader is an
#: "echo within N hours". Descriptive only — see the module honesty note.
DEFAULT_ECHO_WINDOW_HOURS = 48.0

#: An echo edge is STORED when the two sources co-carried at least this many
#: publish-dated narratives (below it there is no population to speak of).
DEFAULT_MIN_CO_CARRIAGE = 2

#: An edge is flagged ``systematic`` at/above this co-carriage AND ratio floor.
DEFAULT_SYSTEMATIC_FLOOR = 3
DEFAULT_ECHO_RATIO_FLOOR = 0.6

#: Defensive bounds (a pathological substrate can never blow a run up).
_MAX_NARRATIVES = 5_000              # contention groups reified per run
_MAX_CARRIERS_JSON = 60             # carriers listed in the narratives.carriers JSONB
_MAX_VARIANTS_JSON = 40             # variants listed in the narratives.variants JSONB
_MAX_PUB_SOURCES_FOR_EDGES = 50    # publish-dated sources per narrative used for pairwise edges
_MAX_EDGES = 200_000                # stored echo edges per run


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Pure timestamp helpers (unit-tested with NO database).
# ---------------------------------------------------------------------------


def parse_published_at(raw: Any) -> Optional[datetime]:
    """Parse a ``signals.payload->>'published_at'`` value to an aware datetime.

    ``published_at`` is a best-effort ISO-8601 string the source handlers stash
    in the payload (there is NO column). Returns ``None`` for anything not
    parseable — a junk / absent value must never mint a fake publish time (the
    entity_gc ``_parse_signal_published_at`` contract). A naive datetime is
    assumed UTC; a trailing ``Z`` is honored.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def effective_ts(
    published_raw: Any, fetched_at: Any
) -> tuple[Optional[datetime], bool]:
    """``(timestamp, publish_dated)`` for one carrier signal.

    Prefers the parsed ``published_at`` (``publish_dated=True``); falls back to
    ``fetched_at`` (``publish_dated=False``) so a narrative's first/last-seen is
    always datable — but the caller uses ``publish_dated`` to keep the ECHO
    graph honest (fetch-time-only carriage never leads/follows).
    """
    pub = parse_published_at(published_raw)
    if pub is not None:
        return pub, True
    if isinstance(fetched_at, datetime):
        return (
            fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
        ), False
    return None, False


def _hours_between(a: datetime, b: datetime) -> float:
    """``(b - a)`` in hours (signed)."""
    return (b - a).total_seconds() / 3600.0


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


# ---------------------------------------------------------------------------
# Aggregates.
# ---------------------------------------------------------------------------


@dataclass
class CarrierAgg:
    """One source's carriage of a single narrative (built from its signals)."""

    source_id: str
    signal_ids: set[str] = field(default_factory=set)
    value_keys: set[str] = field(default_factory=set)
    first_effective: Optional[datetime] = None
    last_effective: Optional[datetime] = None
    first_published: Optional[datetime] = None   # None => not publish-dated
    on_winning_side: bool = False

    def add_signal(
        self,
        *,
        signal_id: str,
        value_key: str,
        is_winner: bool,
        eff: Optional[datetime],
        pub: Optional[datetime],
    ) -> None:
        if signal_id:
            self.signal_ids.add(signal_id)
        if value_key:
            self.value_keys.add(value_key)
        if is_winner:
            self.on_winning_side = True
        if eff is not None:
            if self.first_effective is None or eff < self.first_effective:
                self.first_effective = eff
            if self.last_effective is None or eff > self.last_effective:
                self.last_effective = eff
        if pub is not None:
            if self.first_published is None or pub < self.first_published:
                self.first_published = pub

    @property
    def publish_dated(self) -> bool:
        return self.first_published is not None


@dataclass
class Narrative:
    """A reified contested-claim family (a ``narratives`` row)."""

    contention_id: str
    subject_key: str
    predicate_key: str
    status: str
    surfaced_value: Optional[str]
    variant_count: int
    carrier_source_count: int
    publish_dated_source_count: int
    signal_count: int
    fact_count: int
    first_seen_at: Optional[datetime]
    last_seen_at: Optional[datetime]
    span_hours: Optional[float]
    lead_source_id: Optional[str]
    lead_first_seen_at: Optional[datetime]
    max_echo_lag_hours: Optional[float]
    carriers: list[dict[str, Any]]
    variants: list[dict[str, Any]]
    opened_at: Optional[datetime]
    contention_surfaced_at: Optional[datetime]
    computed_at: datetime
    #: NOT stored — the publish-dated (source_id -> first_published) map the echo
    #: builder consumes. Kept off the row so the DB carries only the readout.
    pub_first: dict[str, datetime] = field(default_factory=dict, repr=False)


@dataclass
class PropagationEdge:
    """A directed source-echo edge (a ``narrative_echo_edges`` row)."""

    leader_source_id: str
    follower_source_id: str
    co_carried: int
    lead_count: int
    follow_within_count: int
    echo_ratio: Optional[float]
    median_lag_hours: Optional[float]
    mean_lag_hours: Optional[float]
    min_lag_hours: Optional[float]
    max_lag_hours: Optional[float]
    echo_window_hours: float
    systematic: bool
    computed_at: datetime


# ---------------------------------------------------------------------------
# P4-1 — reification (pure; unit-tested with NO database).
# ---------------------------------------------------------------------------


def reify_narratives(
    contention_meta: Iterable[Mapping[str, Any]],
    member_rows: Iterable[Mapping[str, Any]],
    source_names: Optional[Mapping[str, str]] = None,
    *,
    now: Optional[datetime] = None,
) -> list[Narrative]:
    """Fold contention groups + their carrier lineage into reified narratives.

    ``contention_meta``: one mapping per group (``contention_id`` /
    ``subject_key`` / ``predicate_key`` / ``status`` / ``surfaced_value`` /
    ``opened_at`` / ``surfaced_at``). ``member_rows``: one mapping per
    (value_key × carrier fact × carrier signal) with ``contention_id`` /
    ``value_key`` / ``is_winner`` / ``distinct_source_count`` / ``fact_id`` /
    ``signal_id`` / ``source_id`` / ``published_at`` (raw) / ``fetched_at``.
    ``source_names``: ``source_id -> display name`` (best-effort). Pure and
    deterministic — the same inputs always yield the same rows.
    """
    now = now or _now()
    names = dict(source_names or {})

    # Group members by contention.
    by_contention: dict[str, list[Mapping[str, Any]]] = {}
    for row in member_rows:
        cid = str(row.get("contention_id") or "")
        if cid:
            by_contention.setdefault(cid, []).append(row)

    out: list[Narrative] = []
    for meta in contention_meta:
        cid = str(meta.get("contention_id") or "")
        if not cid:
            continue
        members = by_contention.get(cid, [])

        carriers: dict[str, CarrierAgg] = {}
        # variant_key -> aggregate for the variants JSONB
        variants: dict[str, dict[str, Any]] = {}
        fact_ids: set[str] = set()

        for row in members:
            source_id = str(row.get("source_id") or "")
            value_key = str(row.get("value_key") or "")
            is_winner = bool(row.get("is_winner"))
            fact_id = str(row.get("fact_id") or "")
            signal_id = str(row.get("signal_id") or "")
            if fact_id:
                fact_ids.add(fact_id)
            eff, publish_dated = effective_ts(
                row.get("published_at"), row.get("fetched_at")
            )
            pub = eff if publish_dated else None
            if source_id:
                agg = carriers.setdefault(source_id, CarrierAgg(source_id=source_id))
                agg.add_signal(
                    signal_id=signal_id,
                    value_key=value_key,
                    is_winner=is_winner,
                    eff=eff,
                    pub=pub,
                )
            # Variant roll-up (per value cluster).
            if value_key:
                v = variants.setdefault(
                    value_key,
                    {
                        "value_key": value_key,
                        "is_winner": is_winner,
                        "distinct_source_count": int(
                            row.get("distinct_source_count") or 0
                        ),
                        "fact_ids": set(),
                        "source_ids": set(),
                    },
                )
                v["is_winner"] = v["is_winner"] or is_winner
                if row.get("distinct_source_count") is not None:
                    v["distinct_source_count"] = max(
                        v["distinct_source_count"],
                        int(row.get("distinct_source_count") or 0),
                    )
                if fact_id:
                    v["fact_ids"].add(fact_id)
                if source_id:
                    v["source_ids"].add(source_id)

        out.append(
            _assemble_narrative(
                meta=meta,
                cid=cid,
                carriers=carriers,
                variants=variants,
                fact_ids=fact_ids,
                names=names,
                now=now,
            )
        )
    return out


def _assemble_narrative(
    *,
    meta: Mapping[str, Any],
    cid: str,
    carriers: Mapping[str, CarrierAgg],
    variants: Mapping[str, dict[str, Any]],
    fact_ids: set[str],
    names: Mapping[str, str],
    now: datetime,
) -> Narrative:
    """Assemble one Narrative from its per-source carrier aggregates."""
    # The lead = the publish-dated source that published FIRST (tie -> id asc).
    pub_carriers = {
        sid: agg.first_published
        for sid, agg in carriers.items()
        if agg.first_published is not None
    }
    lead_source_id: Optional[str] = None
    lead_first: Optional[datetime] = None
    if pub_carriers:
        lead_source_id = min(pub_carriers, key=lambda s: (pub_carriers[s], s))
        lead_first = pub_carriers[lead_source_id]

    # first/last-seen over the EFFECTIVE timeline (always datable).
    eff_firsts = [a.first_effective for a in carriers.values() if a.first_effective]
    eff_lasts = [a.last_effective for a in carriers.values() if a.last_effective]
    first_seen = min(eff_firsts) if eff_firsts else None
    last_seen = max(eff_lasts) if eff_lasts else None
    span_hours = (
        _hours_between(first_seen, last_seen)
        if first_seen is not None and last_seen is not None
        else None
    )

    # Ordered carrier detail (lead first; publish-dated ordered by publish time,
    # then undated by first-effective).
    def _carrier_sort_key(agg: CarrierAgg) -> tuple[int, float, str]:
        if agg.first_published is not None:
            return (0, agg.first_published.timestamp(), agg.source_id)
        eff = agg.first_effective
        return (1, eff.timestamp() if eff else float("inf"), agg.source_id)

    ordered = sorted(carriers.values(), key=_carrier_sort_key)
    carrier_json: list[dict[str, Any]] = []
    max_echo_lag: Optional[float] = None
    for agg in ordered[:_MAX_CARRIERS_JSON]:
        echo_lag: Optional[float] = None
        role = "undated"
        if agg.first_published is not None and lead_first is not None:
            echo_lag = _hours_between(lead_first, agg.first_published)
            role = "lead" if agg.source_id == lead_source_id else "echo"
            if role == "echo" and (max_echo_lag is None or echo_lag > max_echo_lag):
                max_echo_lag = echo_lag
        carrier_json.append(
            {
                "source_id": agg.source_id,
                "source_name": names.get(agg.source_id),
                "role": role,
                "first_seen_at": _iso(agg.first_published or agg.first_effective),
                "publish_dated": agg.publish_dated,
                "echo_lag_hours": (round(echo_lag, 3) if echo_lag is not None else None),
                "signal_count": len(agg.signal_ids),
                "on_winning_side": agg.on_winning_side,
                "value_keys": sorted(agg.value_keys),
            }
        )

    variant_json = [
        {
            "value_key": v["value_key"],
            "is_winner": v["is_winner"],
            "distinct_source_count": v["distinct_source_count"],
            "fact_count": len(v["fact_ids"]),
            "carrier_source_count": len(v["source_ids"]),
        }
        for v in sorted(
            variants.values(),
            key=lambda x: (not x["is_winner"], -len(x["source_ids"]), x["value_key"]),
        )[:_MAX_VARIANTS_JSON]
    ]

    signal_count = len({s for a in carriers.values() for s in a.signal_ids})

    return Narrative(
        contention_id=cid,
        subject_key=str(meta.get("subject_key") or ""),
        predicate_key=str(meta.get("predicate_key") or ""),
        status=str(meta.get("status") or ""),
        surfaced_value=(
            str(meta["surfaced_value"])
            if meta.get("surfaced_value") is not None
            else None
        ),
        variant_count=len(variants),
        carrier_source_count=len(carriers),
        publish_dated_source_count=len(pub_carriers),
        signal_count=signal_count,
        fact_count=len(fact_ids),
        first_seen_at=first_seen,
        last_seen_at=last_seen,
        span_hours=(round(span_hours, 3) if span_hours is not None else None),
        lead_source_id=lead_source_id,
        lead_first_seen_at=lead_first,
        max_echo_lag_hours=(round(max_echo_lag, 3) if max_echo_lag is not None else None),
        carriers=carrier_json,
        variants=variant_json,
        opened_at=meta.get("opened_at"),
        contention_surfaced_at=meta.get("surfaced_at"),
        computed_at=now,
        pub_first=dict(pub_carriers),
    )


# ---------------------------------------------------------------------------
# P4-2 — the source-echo graph (pure; unit-tested with NO database).
# ---------------------------------------------------------------------------


@dataclass
class _EdgeAcc:
    co_carried: int = 0                 # symmetric (both directions share it)
    lead_count: int = 0
    follow_within: int = 0
    lags: list[float] = field(default_factory=list)


def build_echo_edges(
    narratives: Sequence[Narrative],
    *,
    window_hours: float = DEFAULT_ECHO_WINDOW_HOURS,
    min_co_carriage: int = DEFAULT_MIN_CO_CARRIAGE,
    systematic_floor: int = DEFAULT_SYSTEMATIC_FLOOR,
    ratio_floor: float = DEFAULT_ECHO_RATIO_FLOOR,
    now: Optional[datetime] = None,
) -> list[PropagationEdge]:
    """The directed source-echo graph over the narrative population (P4-2).

    Computed ONLY from PUBLISH-DATED carriage (``Narrative.pub_first``): for
    every narrative, each unordered publish-dated source pair contributes one
    ``co_carried`` to BOTH directed edges, and the ordered pair (earlier ->
    later) contributes a ``lead_count`` + its lag to the leader->follower edge
    (``follow_within`` when the lag <= ``window_hours``). A directed edge is
    emitted when the leader led at least once AND ``co_carried >=
    min_co_carriage``; ``echo_ratio = follow_within / co_carried``. Pure +
    deterministic. Equal publish times contribute co-carriage but no lead
    (order is genuinely undetermined).
    """
    now = now or _now()
    acc: dict[tuple[str, str], _EdgeAcc] = {}

    def _edge(leader: str, follower: str) -> _EdgeAcc:
        return acc.setdefault((leader, follower), _EdgeAcc())

    for nar in narratives:
        pub = sorted(nar.pub_first.items(), key=lambda kv: (kv[1], kv[0]))
        if len(pub) < 2:
            continue
        # Bound the pairwise blow-up: keep the earliest publishers (the leaders).
        pub = pub[:_MAX_PUB_SOURCES_FOR_EDGES]
        for i in range(len(pub)):
            si, ti = pub[i]
            for j in range(i + 1, len(pub)):
                sj, tj = pub[j]
                # Shared publish-dated exposure -> co_carried on both directions.
                _edge(si, sj).co_carried += 1
                _edge(sj, si).co_carried += 1
                if ti == tj:
                    continue  # co-carried but no determinable lead
                # pub is time-sorted, so si led sj (ti < tj) for i < j unless tie.
                leader, follower = (si, sj) if ti < tj else (sj, si)
                lag = abs(_hours_between(ti, tj))
                e = _edge(leader, follower)
                e.lead_count += 1
                e.lags.append(lag)
                if lag <= window_hours:
                    e.follow_within += 1

    edges: list[PropagationEdge] = []
    for (leader, follower), e in acc.items():
        if e.lead_count < 1 or e.co_carried < min_co_carriage:
            continue
        echo_ratio = (e.follow_within / e.co_carried) if e.co_carried else None
        systematic = (
            e.co_carried >= systematic_floor
            and echo_ratio is not None
            and echo_ratio >= ratio_floor
        )
        edges.append(
            PropagationEdge(
                leader_source_id=leader,
                follower_source_id=follower,
                co_carried=e.co_carried,
                lead_count=e.lead_count,
                follow_within_count=e.follow_within,
                echo_ratio=(round(echo_ratio, 4) if echo_ratio is not None else None),
                median_lag_hours=(
                    round(statistics.median(e.lags), 3) if e.lags else None
                ),
                mean_lag_hours=(
                    round(statistics.fmean(e.lags), 3) if e.lags else None
                ),
                min_lag_hours=(round(min(e.lags), 3) if e.lags else None),
                max_lag_hours=(round(max(e.lags), 3) if e.lags else None),
                echo_window_hours=float(window_hours),
                systematic=systematic,
                computed_at=now,
            )
        )
    # Deterministic order + defensive cap (systematic + strongest first).
    edges.sort(
        key=lambda x: (
            not x.systematic,
            -(x.echo_ratio or 0.0),
            -x.co_carried,
            x.leader_source_id,
            x.follower_source_id,
        )
    )
    return edges[:_MAX_EDGES]


# ---------------------------------------------------------------------------
# Summary finding — honest per-run distribution (the measurement product).
# ---------------------------------------------------------------------------

HONESTY_NOTE = (
    "Narratives are DETECT-ONLY reifications of contested-claim families and "
    "never mutate facts. Echo-lead is DESCRIPTIVE co-carriage timing (who "
    "published first, who followed within the window), computed only from "
    "publish-dated carriage — NOT a causal or coordination claim: two sources "
    "carrying a narrative in sequence may share a wire, an origin event, or "
    "report independently."
)


def build_summary(
    narratives: Sequence[Narrative],
    edges: Sequence[PropagationEdge],
    *,
    window_hours: float,
    min_co_carriage: int,
    systematic_floor: int,
    ratio_floor: float,
) -> FindingPayload:
    contested = [n for n in narratives if n.status == "contested"]
    surfaced = [n for n in narratives if n.status == "surfaced"]
    with_lead = [n for n in narratives if n.lead_source_id]
    systematic = [e for e in edges if e.systematic]

    if not narratives:
        title = (
            "Narrative mapper: 0 contested-claim families reified "
            "(no contested narratives in the substrate)"
        )
    else:
        title = (
            f"Narrative mapper: {len(narratives)} narrative(s) reified "
            f"({len(contested)} contested, {len(surfaced)} surfaced); "
            f"{len(edges)} source-echo edge(s), {len(systematic)} systematic"
        )

    def _edge_line(e: PropagationEdge) -> str:
        return (
            f"  {e.leader_source_id} -> {e.follower_source_id}: "
            f"echo_ratio={e.echo_ratio} (follow_within={e.follow_within_count}"
            f"/co_carried={e.co_carried}, lead={e.lead_count}); "
            f"median_lag={e.median_lag_hours}h"
        )

    def _nar_line(n: Narrative) -> str:
        lead = n.lead_source_id or "?"
        return (
            f"  [{n.status}] {n.subject_key} / {n.predicate_key}: "
            f"{n.carrier_source_count} carriers "
            f"({n.publish_dated_source_count} publish-dated), "
            f"{n.variant_count} variants; lead={lead}, span={n.span_hours}h"
        )

    top_edges = systematic[:8] if systematic else list(edges)[:8]
    # Most-carried narratives (the loudest families).
    top_nars = sorted(
        narratives, key=lambda n: (-n.carrier_source_count, -n.signal_count)
    )[:8]

    body_lines = [
        (
            f"echo_window_hours={window_hours} min_co_carriage={min_co_carriage} "
            f"systematic_floor={systematic_floor} echo_ratio_floor={ratio_floor}"
        ),
        (
            f"narratives={len(narratives)} contested={len(contested)} "
            f"surfaced={len(surfaced)} with_publish_dated_lead={len(with_lead)}"
        ),
        (
            f"echo_edges={len(edges)} systematic={len(systematic)} "
            f"(directed leader->follower over publish-dated co-carriage)"
        ),
    ]
    if top_edges:
        body_lines.append(
            "top systematic echo relationships:"
            if systematic
            else "top echo relationships (none systematic yet):"
        )
        body_lines.extend(_edge_line(e) for e in top_edges)
    if top_nars:
        body_lines.append("most-carried narratives:")
        body_lines.extend(_nar_line(n) for n in top_nars)
    body_lines.append(f"honesty: {HONESTY_NOTE}")

    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "detect_only": True,
            "descriptive_not_causal": True,
            "honesty_note": HONESTY_NOTE,
            "echo_window_hours": window_hours,
            "min_co_carriage": min_co_carriage,
            "systematic_floor": systematic_floor,
            "echo_ratio_floor": ratio_floor,
            "narratives_total": len(narratives),
            "contested": len(contested),
            "surfaced": len(surfaced),
            "with_publish_dated_lead": len(with_lead),
            "echo_edges_total": len(edges),
            "systematic_edges": len(systematic),
            # C2b (P4-6) — the structural_claims verify CONTRACT (the
            # geo_convergence_scan reference pattern). narrative_mapper is in
            # STRUCTURAL_CLAIMS_VERIFY_ANALYSTS, so the deterministic
            # re-derivation profile checks this claim after the finding lands.
            # The rollup identity — narratives_total == contested + surfaced —
            # is always true by construction (REIFIED_STATUSES admits exactly
            # those two statuses), so a future partition/miscount bug surfaces
            # as a FLAGGED structural critique instead of silently shipping a
            # wrong headline.
            "structural_claims": [
                {
                    "id": "reified_status_rollup",
                    "statement": (
                        f"narratives_total ({len(narratives)}) = "
                        f"contested ({len(contested)}) + "
                        f"surfaced ({len(surfaced)})"
                    ),
                    "op": "sum",
                    "asserted": len(narratives),
                    "basis": [len(contested), len(surfaced)],
                },
            ],
            "top_systematic_edges": [
                {
                    "leader_source_id": e.leader_source_id,
                    "follower_source_id": e.follower_source_id,
                    "co_carried": e.co_carried,
                    "lead_count": e.lead_count,
                    "follow_within_count": e.follow_within_count,
                    "echo_ratio": e.echo_ratio,
                    "median_lag_hours": e.median_lag_hours,
                }
                for e in systematic[:10]
            ],
        },
    )


# ---------------------------------------------------------------------------
# SQL — read the contention sidecar + carrier lineage (READ-ONLY).
# ---------------------------------------------------------------------------

# The contention groups to reify: active families (contested / surfaced),
# most-recent activity first, bounded per run.
_CONTENTION_SQL = """
    SELECT id            AS contention_id,
           subject_key,
           predicate_key,
           status,
           surfaced_value,
           value_count,
           junk_count,
           opened_at,
           surfaced_at
      FROM fact_contention
     WHERE status = ANY($1::text[])
     ORDER BY COALESCE(surfaced_at, opened_at) DESC NULLS LAST, id
     LIMIT $2
"""

# The carrier lineage for the reified groups: one row per
# (value cluster × carrier fact × carrier signal). Non-junk clusters only (the
# real competing positions). The exact lineage the arbiter counts distinct
# sources on: supporting_fact_ids -> facts.derived_from -> signals. Dedup
# aliases are DELIBERATELY kept: source B's cross-source-dedup copy of source
# A's story IS the echo evidence (collapsing it would erase the propagation).
_MEMBERS_SQL = """
    SELECT v.contention_id                 AS contention_id,
           v.value_key                     AS value_key,
           v.surfaced_winner               AS is_winner,
           v.distinct_source_count         AS distinct_source_count,
           f.id                            AS fact_id,
           s.id                            AS signal_id,
           s.source_id                     AS source_id,
           s.payload->>'published_at'      AS published_at,
           s.fetched_at                    AS fetched_at
      FROM fact_contention_values v
      JOIN facts f ON f.id = ANY(v.supporting_fact_ids)
     CROSS JOIN LATERAL unnest(f.derived_from) AS d(sig)
      JOIN signals s ON s.id = d.sig
     WHERE v.contention_id = ANY($1::uuid[])
       AND v.is_junk = false
"""

_SOURCE_NAMES_SQL = """
    SELECT descriptor_id, name
      FROM source_descriptors
     WHERE is_head
"""


async def _load_reification_inputs(
    conn: Any, *, statuses: Sequence[str], max_narratives: int
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], dict[str, str]]:
    contention_rows = await conn.fetch(
        _CONTENTION_SQL, list(statuses), int(max_narratives)
    )
    if not contention_rows:
        return [], [], {}
    cids = [r["contention_id"] for r in contention_rows]
    member_rows = await conn.fetch(_MEMBERS_SQL, cids)
    name_rows = await conn.fetch(_SOURCE_NAMES_SQL)
    names = {str(r["descriptor_id"]): str(r["name"] or "") for r in name_rows}
    return list(contention_rows), list(member_rows), names


# ---------------------------------------------------------------------------
# Storage — wholesale refresh of both derived tables (upsert + prune).
# ---------------------------------------------------------------------------

_UPSERT_NARRATIVE_SQL = """
INSERT INTO narratives (
    contention_id, subject_key, predicate_key, status, surfaced_value,
    variant_count, carrier_source_count, publish_dated_source_count,
    signal_count, fact_count, first_seen_at, last_seen_at, span_hours,
    lead_source_id, lead_first_seen_at, max_echo_lag_hours,
    carriers, variants, opened_at, contention_surfaced_at, computed_at
) VALUES (
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
    $17::jsonb,$18::jsonb,$19,$20,$21
)
ON CONFLICT (contention_id) DO UPDATE SET
    subject_key                = EXCLUDED.subject_key,
    predicate_key              = EXCLUDED.predicate_key,
    status                     = EXCLUDED.status,
    surfaced_value             = EXCLUDED.surfaced_value,
    variant_count              = EXCLUDED.variant_count,
    carrier_source_count       = EXCLUDED.carrier_source_count,
    publish_dated_source_count = EXCLUDED.publish_dated_source_count,
    signal_count               = EXCLUDED.signal_count,
    fact_count                 = EXCLUDED.fact_count,
    first_seen_at              = EXCLUDED.first_seen_at,
    last_seen_at               = EXCLUDED.last_seen_at,
    span_hours                 = EXCLUDED.span_hours,
    lead_source_id             = EXCLUDED.lead_source_id,
    lead_first_seen_at         = EXCLUDED.lead_first_seen_at,
    max_echo_lag_hours         = EXCLUDED.max_echo_lag_hours,
    carriers                   = EXCLUDED.carriers,
    variants                   = EXCLUDED.variants,
    opened_at                  = EXCLUDED.opened_at,
    contention_surfaced_at     = EXCLUDED.contention_surfaced_at,
    computed_at                = EXCLUDED.computed_at
"""

_PRUNE_NARRATIVES_SQL = (
    "DELETE FROM narratives WHERE contention_id <> ALL($1::uuid[])"
)


async def store_narratives(conn: Any, narratives: Sequence[Narrative]) -> None:
    """Upsert the current narrative set + prune contention_ids no longer
    reified, in one transaction (readers see the old or the new set, never a
    half-refresh)."""
    async with conn.transaction():
        seen: list[str] = []
        for n in narratives:
            await conn.execute(
                _UPSERT_NARRATIVE_SQL,
                UUID(n.contention_id),
                n.subject_key,
                n.predicate_key,
                n.status,
                n.surfaced_value,
                n.variant_count,
                n.carrier_source_count,
                n.publish_dated_source_count,
                n.signal_count,
                n.fact_count,
                n.first_seen_at,
                n.last_seen_at,
                n.span_hours,
                n.lead_source_id,
                n.lead_first_seen_at,
                n.max_echo_lag_hours,
                json.dumps(n.carriers),
                json.dumps(n.variants),
                n.opened_at,
                n.contention_surfaced_at,
                n.computed_at,
            )
            seen.append(n.contention_id)
        await conn.execute(
            _PRUNE_NARRATIVES_SQL, [UUID(s) for s in seen]
        )


_UPSERT_EDGE_SQL = """
INSERT INTO narrative_echo_edges (
    leader_source_id, follower_source_id, co_carried, lead_count,
    follow_within_count, echo_ratio, median_lag_hours, mean_lag_hours,
    min_lag_hours, max_lag_hours, echo_window_hours, systematic, computed_at
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
ON CONFLICT (leader_source_id, follower_source_id) DO UPDATE SET
    co_carried          = EXCLUDED.co_carried,
    lead_count          = EXCLUDED.lead_count,
    follow_within_count = EXCLUDED.follow_within_count,
    echo_ratio          = EXCLUDED.echo_ratio,
    median_lag_hours    = EXCLUDED.median_lag_hours,
    mean_lag_hours      = EXCLUDED.mean_lag_hours,
    min_lag_hours       = EXCLUDED.min_lag_hours,
    max_lag_hours       = EXCLUDED.max_lag_hours,
    echo_window_hours   = EXCLUDED.echo_window_hours,
    systematic          = EXCLUDED.systematic,
    computed_at         = EXCLUDED.computed_at
"""

#: Prune edges no longer present. An empty current set deletes every row
#: (correct: no edges this run). The (leader, follower) tuple key is expressed
#: as a NOT IN over a VALUES list built from the current set.
_PRUNE_EDGES_ALL_SQL = "DELETE FROM narrative_echo_edges"


async def store_echo_edges(conn: Any, edges: Sequence[PropagationEdge]) -> None:
    """Upsert the current edge set + prune edges no longer present, in one
    transaction. Prune is a full delete of stale keys: we delete every edge
    whose (leader, follower) is not in the current set, then upsert the current
    set (both inside one transaction, so readers never see a half-refresh)."""
    async with conn.transaction():
        current = {(e.leader_source_id, e.follower_source_id) for e in edges}
        if not current:
            await conn.execute(_PRUNE_EDGES_ALL_SQL)
            return
        # Prune stale rows: fetch existing keys, delete those not in `current`.
        existing = await conn.fetch(
            "SELECT leader_source_id, follower_source_id FROM narrative_echo_edges"
        )
        stale = [
            (r["leader_source_id"], r["follower_source_id"])
            for r in existing
            if (r["leader_source_id"], r["follower_source_id"]) not in current
        ]
        for leader, follower in stale:
            await conn.execute(
                "DELETE FROM narrative_echo_edges "
                "WHERE leader_source_id = $1 AND follower_source_id = $2",
                leader,
                follower,
            )
        for e in edges:
            await conn.execute(
                _UPSERT_EDGE_SQL,
                e.leader_source_id,
                e.follower_source_id,
                e.co_carried,
                e.lead_count,
                e.follow_within_count,
                e.echo_ratio,
                e.median_lag_hours,
                e.mean_lag_hours,
                e.min_lag_hours,
                e.max_lag_hours,
                e.echo_window_hours,
                e.systematic,
                e.computed_at,
            )


# ---------------------------------------------------------------------------
# Public handler entry point.
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — reify narratives, refresh the echo graph, emit
    the honest summary finding.

    REFUSES LOUD on a missing pool (the sibling deterministic-META contract): a
    mapper that cannot read the substrate must error visibly, never report a
    quiet zero-narrative run.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "narrative_mapper requires a live deps.pg_pool — refusing to report "
            "a zero-narrative run without reading the substrate"
        )

    raw_run_id = options.get("run_id")
    try:
        _ = UUID(str(raw_run_id)) if raw_run_id else uuid4()
    except (ValueError, TypeError):
        _ = uuid4()

    window_hours = max(float(options.get("echo_window_hours", DEFAULT_ECHO_WINDOW_HOURS)), 0.0)
    min_co = max(1, int(options.get("min_co_carriage", DEFAULT_MIN_CO_CARRIAGE)))
    systematic_floor = max(min_co, int(options.get("systematic_floor", DEFAULT_SYSTEMATIC_FLOOR)))
    ratio_floor = min(1.0, max(0.0, float(options.get("echo_ratio_floor", DEFAULT_ECHO_RATIO_FLOOR))))
    max_narratives = max(1, int(options.get("max_narratives", _MAX_NARRATIVES)))
    statuses = tuple(options.get("statuses") or REIFIED_STATUSES)
    now = _now()

    async with pool.acquire() as conn:
        contention_rows, member_rows, names = await _load_reification_inputs(
            conn, statuses=statuses, max_narratives=max_narratives
        )

    narratives = reify_narratives(contention_rows, member_rows, names, now=now)
    edges = build_echo_edges(
        narratives,
        window_hours=window_hours,
        min_co_carriage=min_co,
        systematic_floor=systematic_floor,
        ratio_floor=ratio_floor,
        now=now,
    )

    async with pool.acquire() as conn:
        await store_narratives(conn, narratives)
        await store_echo_edges(conn, edges)

    logger.info(
        "narrative_mapper.tick narratives=%d edges=%d systematic=%d "
        "window_h=%s min_co=%d",
        len(narratives),
        len(edges),
        sum(1 for e in edges if e.systematic),
        window_hours,
        min_co,
    )
    finding = build_summary(
        narratives,
        edges,
        window_hours=window_hours,
        min_co_carriage=min_co,
        systematic_floor=systematic_floor,
        ratio_floor=ratio_floor,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "SUB_HANDLER_NAME",
    "REIFIED_STATUSES",
    "DEFAULT_ECHO_WINDOW_HOURS",
    "DEFAULT_MIN_CO_CARRIAGE",
    "DEFAULT_SYSTEMATIC_FLOOR",
    "DEFAULT_ECHO_RATIO_FLOOR",
    "HONESTY_NOTE",
    "CarrierAgg",
    "Narrative",
    "PropagationEdge",
    "parse_published_at",
    "effective_ts",
    "reify_narratives",
    "build_echo_edges",
    "build_summary",
    "store_narratives",
    "store_echo_edges",
    "handle",
]
