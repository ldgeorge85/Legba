# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""sipri_arms_transfers — a curated-YAML SIPRI arms-transfer seed adapter.

SIPRI (Stockholm International Peace Research Institute) maintains the Arms
Transfers Database — the canonical public record of who supplies major
conventional weapons to whom. The runtime already has a streaming SIPRI RSS
*source* handler (a news feed); THIS is a different thing — a **seed** adapter
that folds a curated slice of the supplier→recipient relationships into the
knowledge layer as typed SIGNED nexuses:

  * each ``(supplier_country, recipient_country)`` transfer relationship →
    a :class:`SeedNexus`
    ``(subject=supplier, rel_type='ArmsTransferTo', object=recipient,
    polarity=+1, valid_from=<start of the supply relationship>)``.

POLARITY CONVENTION (documented; consistent with the signed-nexus model):
an arms transfer is a *materially cooperative / patronage* relationship — the
supplier is backing the recipient's military — so ``polarity = +1``
(supportive). This mirrors ``world_baseline``'s ``MemberOf(+1)``
institutional-cooperation precedent and is the OPPOSITE sign of
``acled_conflict``'s ``HostileTo(-1)`` antagonism, so structural-balance /
graph-mining read a supplier→recipient edge as alignment.

Like ``world_baseline`` this maps DIRECTLY to typed substrate payloads — an
arms transfer's sign is known a-priori, so no LLM reifier is needed (operator
decision: relational seeds → nexuses directly; the reifier is only for
free-text). ``source_type='seed'`` (curated/authoritative).

Zero external dependency (no network, no key for the proof): the curated
``seeds/sipri_arms_transfers.yaml`` is the ``fetch`` source; ``map`` is pure.
``ctx.options['yaml_path']`` overrides the file (tests point at a fixture).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from .._base import SeedContext, SeedEntity, SeedNexus, SeedPayload

logger = logging.getLogger(__name__)

# Repo-root-relative default (…/legba/seeds/sipri_arms_transfers.yaml). This
# file is …/src/legba/data/seed/adapters/sipri_arms_transfers.py → 6 parents up
# = the repo root, matching world_baseline's resolution.
_DEFAULT_YAML = (
    Path(__file__).resolve().parents[5] / "seeds" / "sipri_arms_transfers.yaml"
)

_ARMS_REL = "ArmsTransferTo"
#: An arms transfer is a supportive / patronage tie — see the module docstring.
_ARMS_POLARITY = 1
_DEFAULT_CONFIDENCE = 0.90  # curated, but a step below leaders (0.95): a flow,
#                             not a hard institutional fact.


def _parse_date(s: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` curated date into a tz-aware (UTC) datetime."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class SIPRIArmsTransfersSeedSource:
    """Curated-YAML SIPRI arms-transfer seed adapter (implements ``SeedSource``).

    Emits one typed SIGNED ``ArmsTransferTo`` nexus per curated supplier→
    recipient relationship, ``polarity=+1`` (supportive), with the start of the
    supply relationship as ``valid_from``.
    """

    name = "sipri_arms_transfers"
    source_type = "seed"

    def __init__(
        self,
        yaml_path: Path | str | None = None,
        *,
        confidence: float = _DEFAULT_CONFIDENCE,
    ) -> None:
        self._yaml_path = Path(yaml_path) if yaml_path else _DEFAULT_YAML
        self._confidence = confidence

    async def fetch(self, ctx: SeedContext) -> dict[str, Any]:
        """Load + parse the curated YAML (no network)."""
        override = ctx.options.get("yaml_path") if ctx and ctx.options else None
        path = Path(override) if override else self._yaml_path
        if not path.exists():
            logger.warning(
                "seed.%s: no seed file at %s — skipping; Legba ships no bundled "
                "seed data, provide your own (see seeds/README.md)",
                self.name,
                path,
            )
            return {}
        raw_text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text) or {}
        # Stash a content hash for the manifest (reproducibility / drift check).
        data["_source_sha256"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        data["_source_path"] = str(path)
        return data

    def map(self, raw: dict[str, Any]) -> Iterable[SeedPayload]:
        """Map the parsed YAML into typed seed payloads.

        Yields, per transfer: supplier + recipient country :class:`SeedEntity`
        enrichment (so endpoints land with the ``country`` class), then the
        signed ``ArmsTransferTo`` :class:`SeedNexus`. A row missing
        supplier/recipient/valid_from, or a self-loop (supplier == recipient),
        is skipped (degrade-not-drop). The driver resolves every endpoint
        against ``entity_profiles`` anyway; the explicit entities just tag the
        countries with the ``country`` class.
        """
        transfers = raw.get("transfers") or []
        seen_countries: set[str] = set()
        skipped = 0

        for row in transfers:
            supplier = str(row.get("supplier") or "").strip()
            recipient = str(row.get("recipient") or "").strip()
            raw_from = row.get("valid_from")
            if not supplier or not recipient or not raw_from:
                skipped += 1
                continue
            if supplier.lower() == recipient.lower():
                skipped += 1
                continue

            valid_from = _parse_date(str(raw_from))
            valid_until = (
                _parse_date(str(row["valid_until"])) if row.get("valid_until") else None
            )
            rel_type = str(row.get("rel_type", _ARMS_REL)).strip() or _ARMS_REL
            polarity = int(row.get("polarity", _ARMS_POLARITY))
            confidence = float(row.get("confidence", self._confidence))

            for country in (supplier, recipient):
                if country.lower() not in seen_countries:
                    yield SeedEntity(canonical_name=country, entity_class="country")
                    seen_countries.add(country.lower())

            yield SeedNexus(
                subject=supplier,
                object=recipient,
                rel_type=rel_type,
                polarity=polarity,
                valid_from=valid_from,
                valid_until=valid_until,
                confidence=confidence,
                label=f"{supplier} {rel_type} {recipient}",
                intent="arms_supply" if polarity > 0 else "",
                channel="military",
                data={
                    "seed_adapter": self.name,
                    "supplier": supplier,
                    "recipient": recipient,
                    "tiv_rank": row.get("tiv_rank"),
                },
            )

        if skipped:
            logger.info(
                "seed.%s skipped %d malformed/self-loop transfer rows",
                self.name,
                skipped,
            )


__all__ = ["SIPRIArmsTransfersSeedSource"]
