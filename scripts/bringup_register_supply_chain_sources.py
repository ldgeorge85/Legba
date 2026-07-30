# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Supply-chain domain — register the top-10 first-registration source batch.

Registers the 7 BRAND-NEW draft SourceDescriptors from the 2026-07-29
supply-chain domain source research
(planning/SUPPLY_CHAIN_SOURCES_2026-07-29.md, "§5 Top-10 recommended first
registrations"): 7 verified, keyless RSS feeds. Mirrors
scripts/bringup_register_wave_a_sources.py's structure (idempotent direct-DB
registration + host-level source_credibility seeds).

Ships INERT / activation is the operator's: every descriptor here ships
``identity.state: draft`` so bulk registration creates NO live actor
(``runtime/dapr_host.py`` skips draft/configured descriptors). The operator
activates each (draft -> configured -> active) after verifying the route is
live.

DELIBERATELY NOT in this batch — ``descriptors/source_telegram_monitor.yaml``
(the #1/#9-ranked telegram additions, @Almasirah_En + @ansarollah1, PLUS the
earlier TankerTrackers addition) to the EXISTING
``source.telegram.org_channels`` descriptor. That id's live head is already
``state: active`` in production (verified via a read-only SELECT on
2026-07-29) — re-registering it through this idempotent draft-batch registrar
would see the file's stale ``draft`` claim, diff it against the live
``active`` state, and refuse with "no legal FSM path active -> draft" (this
registrar's ``register_descriptor`` helper only walks FORWARD legal FSM
transitions). That edit is a LIVE CONFIG UPDATE to an already-running
Telethon actor and needs the registry's normal PUT/update path applied with
the same care as any other production descriptor change — see the extensive
header comment in that file for the full reasoning. Run it as its own
deliberate step, separate from this batch.

2026-07-29 OPERATOR DECISION (supersedes the original plan referenced above):
a SEPARATE ``source.telegram.ansarallah_channels`` descriptor + a second
Telegram account/session was rejected — a second concurrent TelegramClient on
the SAME session as ``source.telegram.org_channels`` triggers Telegram's
AUTH_KEY_DUPLICATED session-kill, and a second account was also rejected.
@Almasirah_En / @ansarollah1 now ride the EXISTING ``source.telegram.
org_channels`` descriptor/account instead, classed ``state_media`` via that
descriptor's NEW per-channel ``config.classes`` override (NOT this
script's SOURCE_FILES batch) — see source_telegram_monitor.yaml's header.
``descriptors/source_telegram_ansarallah.yaml`` is DELETED; its never-
activated DRAFT ``source_descriptors`` row (id
``source.telegram.ansarallah_channels``, no vault secrets ever loaded) is
retired by the operating session via the registry FSM, not by this script.

Source-class notes (S1-T8 taxonomy, docs/DATA_SOURCES.md §2.4):
  * ``source.pancanal.news`` / ``source.wto.news`` — ``official`` (primary-
    source government/IGO publishers, like EIA/UN/State Dept).
  * ``source.splash247.news`` / ``source.theloadstar.news`` /
    ``source.maritimeexecutive.news`` / ``source.digitimes.news`` /
    ``source.northernminer.news`` — ``reporting`` (independent commercial
    trade press).

DB selection: direct-DB via DescriptorRegistry (default ``legba_pivot_test``
on the dev rig — override with ``LEGBA_DATA_PG_DB=legba`` for production,
exactly as the other bring-up scripts).

REGISTRATION STEP (main session), once reviewed:
    docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \\
        python scripts/bringup_register_supply_chain_sources.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _p17_registrar import (  # noqa: E402
    Family,
    RegisterResult,
    close_registry,
    open_registry,
    print_results,
    register_descriptor,
)

from legba.data.postgres import PostgresStore  # noqa: E402
from legba.data.schemas.source import SourceDescriptor  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

# The 7 new draft descriptors — all keyless RSS. Ranked per
# planning/SUPPLY_CHAIN_SOURCES_2026-07-29.md §5 (top-10 list); #1/#9
# (the telegram additions) and TankerTrackers are handled separately — see
# module docstring.
SOURCE_FILES = [
    "source_pancanal.yaml",              # #2
    "source_splash247.yaml",              # #3
    "source_theloadstar.yaml",           # #4
    "source_maritime_executive.yaml",    # #5
    "source_digitimes.yaml",             # #6
    "source_wto_news.yaml",              # #8
    "source_northernminer.yaml",         # #10
]

SCORED_BY = "supply_chain_top10"

# Host-level credibility seeds for the 7 RSS upstream outlets (host, score,
# rationale, tier, state_affiliation) — same vocabulary/convention as
# CREDIBILITY_SEEDS in bringup_register_wave_a_sources.py. No seed for the
# telegram additions: every telegram Signal's canonical_url host is always
# `t.me` (telegram.py's D5 fix), so a host-keyed credibility row could never
# distinguish one channel from another — weighting rides source_class (now
# per-channel via config.classes) + payload.channel.username
# instead (source_telegram_monitor.yaml never seeds per-channel credibility
# either).
CREDIBILITY_SEEDS: list[tuple[str, float, str, str, bool]] = [
    ("pancanal.com", 0.82, "Panama Canal Authority (ACP) — Panama's autonomous government canal operator; primary-source transit/operational announcements.", "gov", True),
    ("splash247.com", 0.65, "Asia Shipping Media's flagship maritime/shipping B2B trade-press title.", "wire", False),
    ("theloadstar.com", 0.68, "Independent ocean/air/rail freight-logistics trade press; strong analytical depth despite partial-paywall depth pieces.", "wire", False),
    ("maritime-executive.com", 0.65, "General maritime-industry trade press (vessels, ports, offshore, regulation).", "wire", False),
    ("digitimes.com", 0.65, "Taiwan-based semiconductor/electronics supply-chain trade press; free daily tier, deeper research paywalled separately.", "wire", False),
    ("wto.org", 0.85, "World Trade Organization — primary-source multilateral trade-policy institution.", "gov", True),
    ("northernminer.com", 0.68, "Canada-based, long-standing (est. 1915) mining-industry trade press; sector-standard for exploration/mine-development news.", "wire", False),
]


def _load(name: str) -> SourceDescriptor:
    """Mirror scripts/bringup_register_sources.py::_load — yaml + placeholder
    version + strict=False validation against the real SourceDescriptor schema."""
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return SourceDescriptor.model_validate(body, strict=False)


async def seed_credibility(pg: PostgresStore) -> tuple[int, int]:
    """Seed host-level source_credibility rows (0014/0031 convention:
    INSERT .. ON CONFLICT (source_host) DO NOTHING). Returns (inserted,
    already_present)."""
    inserted = 0
    skipped = 0
    async with pg.acquire() as conn:
        for host, score, rationale, tier, state_aff in CREDIBILITY_SEEDS:
            status = await conn.execute(
                """
                INSERT INTO source_credibility
                    (source_host, score, score_rationale, scored_by, tier, state_affiliation)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (source_host) DO NOTHING
                """,
                host, score, rationale, SCORED_BY, tier, state_aff,
            )
            if status.endswith("1"):
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


async def main() -> int:
    pg, reg = await open_registry()
    try:
        results: list[RegisterResult] = []
        for fname in SOURCE_FILES:
            desc = _load(fname)
            results.append(
                await register_descriptor(pg, reg, family=Family.SOURCE, descriptor=desc)
            )
        failures = print_results(
            f"Supply-chain top-10 draft sources ({len(results)} descriptors):", results
        )
        inserted, skipped = await seed_credibility(pg)
        print(f"source_credibility seeds: +{inserted} inserted, ={skipped} already present")
        print(
            "\nNOT included above (deliberately): descriptors/source_telegram_monitor.yaml "
            "(TankerTrackers addition + the @Almasirah_En/@ansarollah1 per-channel "
            "state_media override) — its live head is already `active`; apply that "
            "edit via the registry's normal update path, not this draft-batch registrar. "
            "See that file's header comment. source_telegram_ansarallah.yaml (a separate "
            "descriptor/account) is RETIRED — deleted, not registered here."
        )
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
