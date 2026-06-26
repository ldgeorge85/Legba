# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""world_baseline — the curated-YAML proof seed adapter (flavor b roots).

Reads ``seeds/world_baseline.yaml`` (a hand-curated set: current G20 heads of
state/government + the major bloc memberships + the CURRENT active conflicts)
and maps it to typed substrate payloads:

  * each leader  -> a :class:`SeedFact`
    (subject=leader, predicate=``'LeaderOf'``, value=country, valid_from=term
    start, confidence 0.95) PLUS a country-SUBJECT office fact
    (subject=country, predicate=``'head of state'`` by default — overridable
    per row via ``office`` so a head-of-state and a head-of-government for the
    SAME country don't supersede each other, value=leader);
  * each alliance membership -> a typed SIGNED :class:`SeedNexus`
    (subject=country, rel_type=``'MemberOf'``, object=bloc, polarity=+1,
    valid_from=accession date);
  * each ACTIVE CONFLICT -> a typed SIGNED :class:`SeedNexus`
    (subject=country, rel_type=``'InActiveConflictWith'``, object=belligerent,
    polarity=-1, valid_from=conflict start) — the curated current-world-state
    layer the world_assessor reads so a months-old event is not mistaken for
    current. Each belligerent pair is a DISTINCT typed triple, so a country in
    several simultaneous wars keeps an open edge per opponent (no
    self-supersession): adding a conflict is a one-line YAML add.

All relational seeds map DIRECTLY to typed signed nexuses, no LLM reifier
(operator decision: the reifier is only for free-text).

Zero external dependency (no network) — the cheapest adapter that proves the
fetch → map → resolve → write → batch path end-to-end. The YAML is the
``fetch`` source; ``map`` is pure.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from .._base import SeedContext, SeedEntity, SeedFact, SeedNexus, SeedPayload

# Repo-root-relative default (…/legba/seeds/world_baseline.yaml). This file is
# …/src/legba/data/seed/adapters/world_baseline.py → 6 parents up = repo root.
_DEFAULT_YAML = (
    Path(__file__).resolve().parents[5] / "seeds" / "world_baseline.yaml"
)

_LEADER_PREDICATE = "LeaderOf"
# Country-SUBJECT office predicate (subject=country, value=leader). The
# `LeaderOf` fact (subject=leader) feeds the AGE graph + read paths, but it
# CANNOT auto-supersede on a leader change: supersession keys on
# (lower(subject), lower(predicate)) and the subject is the PERSON, so a new
# leader is a different subject → both rows stay open → two "current" leaders.
# This country-subject fact is keyed on the country, so when a new officeholder
# lands the prior one is closed (valid_until=now + superseded_by) and the new
# one opened — the temporal honesty the grounding injection reads. (head_of_*
# normalizes to a lowercase-spaced canonical via PREDICATE_CANONICAL.)
_HEAD_OF_STATE_PREDICATE = "head of state"
# Signed -1 relation for the curated active-conflict layer. Distinct from
# ACLED's event-level ``HostileTo``: this is the operator-curated "these two
# states are at war RIGHT NOW" edge the grounding preamble surfaces so a
# stale-cutoff assessor anchors on the current conflict state. normalize_predicate
# renders it ``in active conflict with`` (PREDICATE_CANONICAL), and the grounding
# nexus render shows it as "Iran in active conflict with United States
# [antagonistic] (since 2026-02-28)". Keyed on the full (subject, object,
# rel_type) triple, so a country in multiple wars keeps one open edge PER
# opponent — adding a conflict never supersedes an existing one.
_CONFLICT_REL = "InActiveConflictWith"
_CONFLICT_POLARITY = -1
# D31: coalition-aware conflicts. A conflict expressed with explicit ``sides``
# emits HOSTILE (-1) edges only ACROSS sides (so co-belligerents on the SAME
# side are NOT modelled as at war with each other), plus an optional ALLIED (+1)
# edge WITHIN each multi-member side (US + Israel allied vs Iran). The signed +1
# intra-side edge uses ``AlliedWith`` so it renders distinct from a bloc
# MemberOf and never collides with the hostile triple's unique key.
_ALLIED_REL = "AlliedWith"
_ALLIED_POLARITY = 1
_DEFAULT_CONFIDENCE = 0.95


def _parse_date(s: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` curated date into a tz-aware (UTC) datetime."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class WorldBaselineSeedSource:
    """Curated-YAML world-baseline seed adapter (implements ``SeedSource``)."""

    name = "world_baseline"
    source_type = "seed"

    def __init__(self, yaml_path: Path | str | None = None) -> None:
        self._yaml_path = Path(yaml_path) if yaml_path else _DEFAULT_YAML

    async def fetch(self, ctx: SeedContext) -> dict[str, Any]:
        """Load + parse the curated YAML (no network)."""
        override = ctx.options.get("yaml_path") if ctx and ctx.options else None
        path = Path(override) if override else self._yaml_path
        raw_text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text) or {}
        # Stash a content hash for the manifest (reproducibility / drift check).
        data["_source_sha256"] = hashlib.sha256(
            raw_text.encode("utf-8")
        ).hexdigest()
        data["_source_path"] = str(path)
        return data

    def map(self, raw: dict[str, Any]) -> Iterable[SeedPayload]:
        """Map the parsed YAML into typed seed payloads.

        Yields, in order: country/leader/bloc :class:`SeedEntity` enrichment,
        then leader :class:`SeedFact` rows, then alliance + active-conflict
        :class:`SeedNexus` rows. The driver resolves every endpoint against
        ``entity_profiles`` anyway; the explicit entities just tag countries
        with the ``country`` class.
        """
        leaders = raw.get("leaders") or []
        alliances = raw.get("alliances") or []
        conflicts = raw.get("conflicts") or []

        countries: set[str] = set()

        # 1) Leaders → facts (+ entity enrichment for leader + country).
        for row in leaders:
            leader = str(row["leader"]).strip()
            country = str(row["country"]).strip()
            valid_from = _parse_date(str(row["valid_from"]))
            confidence = float(row.get("confidence", _DEFAULT_CONFIDENCE))
            valid_until = (
                _parse_date(str(row["valid_until"]))
                if row.get("valid_until")
                else None
            )
            # The country-SUBJECT office predicate. Default 'head of state', but
            # overridable per row (e.g. a President who is NOT the actual head of
            # state — Iran's executive President alongside the Supreme Leader —
            # carries 'head of government' so the two open office facts for the
            # same country DON'T supersede each other on the (country, predicate)
            # key. normalize_predicate canonicalizes the surface form at write.)
            office_predicate = str(
                row.get("office", _HEAD_OF_STATE_PREDICATE)
            ).strip() or _HEAD_OF_STATE_PREDICATE
            countries.add(country)

            yield SeedEntity(canonical_name=leader, entity_class="person")
            yield SeedEntity(canonical_name=country, entity_class="country")
            yield SeedFact(
                subject=leader,
                predicate=_LEADER_PREDICATE,
                value=country,
                valid_from=valid_from,
                valid_until=valid_until,
                confidence=confidence,
                data={"seed_adapter": self.name, "relation": "leader_of"},
            )
            # Country-SUBJECT office fact — the supersession-correct shape (see
            # _HEAD_OF_STATE_PREDICATE). A leader change for the SAME office
            # closes the prior row; distinct offices (head of state vs head of
            # government) coexist as separate open rows.
            yield SeedFact(
                subject=country,
                predicate=office_predicate,
                value=leader,
                valid_from=valid_from,
                valid_until=valid_until,
                confidence=confidence,
                data={"seed_adapter": self.name, "relation": "office_holder"},
            )

        # 2) Alliances → typed signed nexuses (country MemberOf bloc, +1).
        for bloc_row in alliances:
            bloc = str(bloc_row["bloc"]).strip()
            rel_type = str(bloc_row.get("rel_type", "MemberOf")).strip()
            polarity = int(bloc_row.get("polarity", 1))
            yield SeedEntity(canonical_name=bloc, entity_class="organization")

            for m in bloc_row.get("members") or []:
                country = str(m["country"]).strip()
                valid_from = _parse_date(str(m["valid_from"]))
                valid_until = (
                    _parse_date(str(m["valid_until"]))
                    if m.get("valid_until")
                    else None
                )
                confidence = float(m.get("confidence", _DEFAULT_CONFIDENCE))
                if country not in countries:
                    yield SeedEntity(canonical_name=country, entity_class="country")
                    countries.add(country)
                yield SeedNexus(
                    subject=country,
                    object=bloc,
                    rel_type=rel_type,
                    polarity=polarity,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    confidence=confidence,
                    label=f"{country} {rel_type} {bloc}",
                    intent="alliance" if polarity > 0 else "",
                    channel="institutional",
                    data={"seed_adapter": self.name, "bloc": bloc},
                )

        # 3) Active conflicts → typed SIGNED nexuses (the curated
        #    current-world-state layer). Two YAML shapes are supported:
        #
        #    (a) ``sides`` (D31, PREFERRED for any multi-party war): a list of
        #        coalitions, each a list of countries. HOSTILE (-1) edges are
        #        emitted ONLY for ORDERED pairs ACROSS distinct sides, so two
        #        co-belligerents on the SAME side (US + Israel vs Iran) are NOT
        #        modelled as at war with each other — fixing the wrong
        #        "Israel in active conflict with United States" nexuses. An
        #        optional ALLIED (+1) edge is emitted between same-side members.
        #
        #    (b) ``belligerents`` (LEGACY, flat list): every ordered pair is
        #        hostile (the original all-pairs behaviour). Kept for back-compat
        #        and for genuinely all-vs-all conflicts; a war with coalitions
        #        should use ``sides``.
        #
        #    Either way we emit one directed edge per ordered hostile pair so the
        #    grounding resolver — which matches on nexuses.subject — surfaces the
        #    war whichever side a country analyst is scoped to. Distinct
        #    (subject, object, rel_type) triples never supersede one another, so a
        #    country in several wars keeps one open edge per opponent.
        for conflict_row in conflicts:
            name = str(conflict_row.get("name", "")).strip()
            valid_from = _parse_date(str(conflict_row["valid_from"]))
            valid_until = (
                _parse_date(str(conflict_row["valid_until"]))
                if conflict_row.get("valid_until")
                else None
            )
            confidence = float(conflict_row.get("confidence", _DEFAULT_CONFIDENCE))

            sides_raw = conflict_row.get("sides")
            if sides_raw:
                sides = [
                    [str(c).strip() for c in (side or []) if str(c).strip()]
                    for side in sides_raw
                ]
                sides = [s for s in sides if s]
                all_members = [c for side in sides for c in side]
            else:
                # Legacy flat ``belligerents`` → one all-vs-all side.
                flat = [
                    str(b).strip()
                    for b in (conflict_row.get("belligerents") or [])
                    if str(b).strip()
                ]
                sides = [[c] for c in flat]  # each country its own side → all-pairs
                all_members = flat

            for c in all_members:
                if c not in countries:
                    yield SeedEntity(canonical_name=c, entity_class="country")
                    countries.add(c)

            # HOSTILE edges: ordered pairs ACROSS distinct sides only.
            for i, side_a in enumerate(sides):
                for j, side_b in enumerate(sides):
                    if i == j:
                        continue
                    for subject in side_a:
                        for obj in side_b:
                            if subject == obj:
                                continue
                            yield self._conflict_nexus(
                                subject=subject,
                                obj=obj,
                                name=name,
                                valid_from=valid_from,
                                valid_until=valid_until,
                                confidence=confidence,
                            )

            # ALLIED edges: ordered pairs WITHIN each multi-member side (+1). Only
            # emitted from the explicit ``sides`` shape — the legacy flat form has
            # no coalition information so it asserts no alliances.
            if sides_raw:
                for side in sides:
                    for subject in side:
                        for obj in side:
                            if subject == obj:
                                continue
                            yield self._allied_nexus(
                                subject=subject,
                                obj=obj,
                                name=name,
                                valid_from=valid_from,
                                valid_until=valid_until,
                                confidence=confidence,
                            )

    def _conflict_nexus(
        self,
        *,
        subject: str,
        obj: str,
        name: str,
        valid_from: datetime,
        valid_until: datetime | None,
        confidence: float,
    ) -> SeedNexus:
        return SeedNexus(
            subject=subject,
            object=obj,
            rel_type=_CONFLICT_REL,
            polarity=_CONFLICT_POLARITY,
            valid_from=valid_from,
            valid_until=valid_until,
            confidence=confidence,
            label=name or f"{subject} {_CONFLICT_REL} {obj}",
            intent="conflict",
            channel="military",
            data={
                "seed_adapter": self.name,
                "relation": "active_conflict",
                "conflict": name,
            },
        )

    def _allied_nexus(
        self,
        *,
        subject: str,
        obj: str,
        name: str,
        valid_from: datetime,
        valid_until: datetime | None,
        confidence: float,
    ) -> SeedNexus:
        return SeedNexus(
            subject=subject,
            object=obj,
            rel_type=_ALLIED_REL,
            polarity=_ALLIED_POLARITY,
            valid_from=valid_from,
            valid_until=valid_until,
            confidence=confidence,
            label=f"{subject} {_ALLIED_REL} {obj}",
            intent="alliance",
            channel="military",
            data={
                "seed_adapter": self.name,
                "relation": "war_coalition",
                "conflict": name,
            },
        )


__all__ = ["WorldBaselineSeedSource"]
