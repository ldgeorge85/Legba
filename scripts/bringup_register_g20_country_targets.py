# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-17 — register the G20 country targets on the source-first model.

Materialises ONE TargetDescriptor per G20 country (19 ISO-3166-1 alpha-2 codes;
the EU has no country code).  Each target:

  * is a GeoScope target (domain=geo) scoped to its own country;
  * owns NO inline sources — it wires to the shared global-news sources by a
    PREDICATE source_selector (tags=[news], kinds=[rss], owner_tenant=shared);
  * narrows the shared raw pool to its country via a per-target
    Subscription.geo + a geo_match() Starlark residual;
  * carries ONE correctly-scoped inline analyst (the country-situational-
    awareness assessment) — NOT a shared brazil-scoped analyst fanning out
    across every country.  This is the fix for the live duplicate-findings
    issue (the brazil-analyst-on-all-countries scoping + the stray
    india_energy_inline_critic_test).

Country metadata (name, BCP-47 languages) is read from the migrated
``iso_countries`` table (the same P-13 seed the country_list discovery uses) —
so the per-country bodies match what the live country_list_discovery would
materialise, just authored explicitly here for the cutover working set.

Direct-DB registration via DescriptorRegistry against the migrated Postgres
(default ``legba_pivot_test``).  Idempotent: re-runs report ``unchanged``.

Every synthesised body is validated against the real pydantic TargetDescriptor
schema before it touches the DB.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _p17_registrar import (  # noqa: E402
    Family,
    RegisterResult,
    close_registry,
    open_registry,
    print_results,
    register_descriptor,
)

from legba.data.schemas.target import TargetDescriptor  # noqa: E402

# G20 membership minus the EU (no ISO-3166-1 alpha-2). 19 codes.
G20_ISO2 = [
    "AR", "AU", "BR", "CA", "CN", "DE", "FR", "GB", "ID", "IN",
    "IT", "JP", "KR", "MX", "RU", "SA", "TR", "US", "ZA",
]

# The full geopolitical vocabulary the G20 targets reason over.  Every value
# here is present in the migrated vocabulary_entries seed (entity_class /
# relationship_type families) — the registry's vocabulary validator rejects
# unknown values.
ENTITY_CLASSES = [
    "country", "organization", "corporation", "person", "location",
    "political_party", "international_org", "infrastructure", "media_outlet",
    "military_unit", "armed_group", "event_series", "commodity", "concept",
]
RELATIONSHIP_TYPES = [
    "AlliedWith", "HostileTo", "MemberOf", "LeaderOf", "TradesWith",
    "BordersWith", "SanctionsAgainst", "DiplomaticRelationsWith",
    "MilitaryPresenceIn", "SuppliesWeaponsTo",
]


async def _country_meta(pg) -> dict[str, dict]:
    """ISO2 -> {name, languages[]} from the iso_countries seed table."""
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT iso2, name, languages FROM iso_countries "
            "WHERE iso2 = ANY($1::text[])",
            G20_ISO2,
        )
    import json

    out: dict[str, dict] = {}
    for r in rows:
        langs = r["languages"]
        if isinstance(langs, (str, bytes, bytearray)):
            try:
                langs = json.loads(langs)
            except Exception:
                langs = []
        out[r["iso2"]] = {"name": r["name"], "languages": list(langs or [])}
    return out


def _build_target(iso2: str, name: str, languages: list[str]) -> TargetDescriptor:
    iso_lower = iso2.lower()
    # English fallback on every country (mirrors the country_list relabel chain).
    langs = list(languages)
    if "en" not in langs:
        langs.append("en")
    body = {
        "identity": {
            "id": f"country_g20_{iso_lower}",
            "name": f"G20 — {name}",
            "schema_uri": "legba/target/2.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "inherits": [],
            "state": "active",
            "owner": "p17_reregister",
            "created": "2026-06-03T00:00:00Z",
        },
        "scope": {
            "domain": "geo",
            "geo": [iso2],
            "languages": langs,
            "entity_classes": ENTITY_CLASSES,
            "relationship_types": RELATIONSHIP_TYPES,
            "time_horizon_days": 90,
            "predicate": None,
            "tags": ["geopolitical", "news", "g20"],
        },
        # NEW MODEL: no inline sources — a geo-predicate selector + per-country
        # subscription. Resolves to the shared sources (source.bbc.world /
        # source.aljazeera.world / source.dw.world), one connection each.
        "sources": [
            {
                "source_selector": {
                    "tags": ["news"],
                    "kinds": ["rss"],
                    "owner_tenant": "shared",
                },
                "subscription": {
                    "geo": [iso2],
                    "predicate": f'geo_match(["{iso2}"])',
                    "canonical_only": True,
                },
            }
        ],
        # ONE inline analyst per target, correctly geo-scoped to THIS country.
        "analyst": {
            "use": "inline_target",
            "cadence": {"fallback_schedule": "*/10 * * * *"},
            "method": {
                "kind": "llm_planner",
                "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
                "budget_tokens_per_day": 200000,
                "llm": {
                    "primary": {
                        "factory_kind": "stack_ref",
                        "raw": "llm.primary.openai_compat",
                        "expected_family": "llm_provider",
                    }
                },
            },
        },
        # Action packs this target's analysts may use (the inline analyst
        # inherits these; the cross-target country_assessor grants its own).
        "allowed_action_packs": [
            {"pack_id": "media_processing"},
            {"pack_id": "incident_response"},
            # A-3c: the escalate_finding example pack — the country_assessor
            # grants it; G20 targets allow it; applies_to_tags matches the
            # g20 scope tag. The three-way intersection is live end-to-end.
            {"pack_id": "escalate_finding"},
        ],
        "pipeline": {"ingestion_filters": [], "enrichment": [], "routing": []},
        "outputs": [
            {
                "kind": "a2a_skill",
                "config": {"skill_id": f"intelligence.country_g20_{iso_lower}_assessment"},
            }
        ],
    }
    return TargetDescriptor.model_validate(body, strict=False)


async def main() -> int:
    pg, reg = await open_registry()
    try:
        meta = await _country_meta(pg)
        missing = [c for c in G20_ISO2 if c not in meta]
        if missing:
            print(f"WARNING: iso_countries missing G20 codes {missing} — "
                  f"falling back to bare codes for those.")

        results: list[RegisterResult] = []
        for iso2 in G20_ISO2:
            m = meta.get(iso2, {"name": iso2, "languages": []})
            desc = _build_target(iso2, m["name"], m["languages"])
            results.append(
                await register_descriptor(pg, reg, family=Family.TARGET, descriptor=desc)
            )
        failures = print_results("G20 country targets (source-first model):", results)
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
