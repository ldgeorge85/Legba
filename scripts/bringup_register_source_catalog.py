# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 — register the no-auth source catalog (verified RSS/GeoJSON feeds).

The live working set registered only 4 sources while 13 source kinds exist;
this script is the registration-side recovery: a catalog of
independently verified, no-auth ``rss`` / ``geojson`` feeds with proper
geo/tags metadata (so target source-selectors can match), conservative
cadences (1h wires / 2–6h slow publishers, stagger-offset per source), and
``source_credibility`` seed rows carrying the migration-0031 ``tier`` +
``state_affiliation`` provenance columns.

ROSTER-1: the catalog is profile DATA — the selected profile's
``rosters.source_catalog`` file (the geopolitics exemplar's 46 feeds:
``profiles/geopolitics/rosters/source_catalog.yaml``). This module is the
LOADER over it (:func:`load_catalog`) plus the platform machinery: the entry
model, the descriptor builder, the credibility seeding and ``--verify``.

License note: feed URLs are facts. Every feed below was probed live by us
(2026-06-09/10: HTTP GET + feedparser/GeoJSON parse); descriptors are written
in OUR SourceDescriptor schema. No third-party catalog code or annotation
prose is copied.

Modes
-----
* ``--verify``  — live HTTP probe + parse check per feed; prints a verdict
  table (ok / ok-redirect / dead / parse-fail / empty). Registers NOTHING.
* default       — direct-DB registration via DescriptorRegistry against the
  migrated Postgres (default ``legba_pivot_test`` — override with
  LEGBA_DATA_PG_DB; same _p17_registrar pattern as the other bring-up
  scripts), then seeds ``source_credibility`` rows
  (INSERT .. ON CONFLICT DO NOTHING — operator overrides always win).
  Idempotent: re-runs report ``unchanged`` for already-current heads.

Probe drops (verified dead/blocked on 2026-06-09/10 — keep out until rechecked):
  * ReliefWeb updates RSS  — HTTP 202 WAF/challenge page on every variant.
  * ProMED                 — 404 on both historical feed paths.
  * Brookings              — /feed/ now 302-redirects to the homepage.
  * Nation Africa          — 404/403 on known feed paths.
  * DFAT Smartraveller     — connection timeout (repeated) from the rig.
  * CISA advisories        — HTTP 403 bot-blocked (edge "Access Denied");
                             scraper fallback is out of S-1 scope.
  * Kyodo + SIPRI          — feeds discontinued upstream (not probed).
  * USGS significant quakes — already live as ``source.usgs.earthquakes``
    (descriptors/source_usgs_earthquakes.yaml); the catalog adds the broader
    M4.5+ week feed instead of churning the existing descriptor.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, get_args

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
from _p17_registrar import (  # noqa: E402
    Family,
    RegisterResult,
    close_registry,
    open_registry,
    print_results,
    register_descriptor,
)

from legba.data import rosters  # noqa: E402
from legba.data.postgres import PostgresStore  # noqa: E402
from legba.data.profile import load_profile, resolve_profile  # noqa: E402
from legba.data.registry.descriptor import DescriptorRegistry  # noqa: E402
from legba.data.schemas.lifecycle import AbstractionLevel, LifecycleState  # noqa: E402
from legba.data.schemas.properties import Cron  # noqa: E402
from legba.data.schemas.source import (  # noqa: E402
    CadenceBlock,
    FilterStage,
    SourceDescriptor,
    SourceIdentity,
    SourceOutput,
    SourceClass,
    SourcePipeline,
    SourceScope,
)

# The UA the probes were verified with — also pinned into each descriptor's
# config so the runtime handler sends exactly what we verified against.
USER_AGENT = "Mozilla/5.0 (compatible; LegbaBot/2.0; +https://legba.invalid)"

OWNER = "s1_catalog"
CREATED = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)
SCORED_BY = "catalog.seed"

VALID_TIERS = ("wire", "gov", "aggregator", "thinktank", "social")

#: The ``source_class`` vocabulary an entry may declare (the schema's
#: ``SourceClass``).
SOURCE_CLASSES: tuple[str, ...] = tuple(get_args(SourceClass))


def catalog_source_class(entry: "CatalogEntry") -> str:
    """S1-T8 — the source_class for a catalog entry (a fixed, auditable rule).

    An entry's own ``source_class`` wins: state_media is hand-curated, ON THE
    ENTRY in the profile's catalog YAML (ROSTER-2 — not derived from
    ``state_affiliation``, which is also true for state-FUNDED-but-independent
    public broadcasters like VOA / France 24 / Yonhap: only genuine
    state-controlled outlets whose rationale marks them a conduit for official
    positions carry it). The rest derive from the credibility ``tier`` so every
    entry lands in the schema vocabulary (reporting / analysis / official /
    state_media):
      * ``thinktank`` -> ``analysis``  (research / advocacy / OSINT)
      * ``gov``       -> ``official``  (government / IGO primary publishers)
      * ``wire`` / ``aggregator`` / ``social`` -> ``reporting`` (the default)
    """
    if entry.source_class is not None:
        return entry.source_class
    if entry.tier == "thinktank":
        return "analysis"
    if entry.tier == "gov":
        return "official"
    return "reporting"


# ---------------------------------------------------------------------------
# Catalog model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    """One verified no-auth feed + its credibility seed metadata."""

    id: str
    name: str
    kind: str                       # "rss" | "geojson"
    url: str
    tags: tuple[str, ...]
    cadence_minutes: int            # 30 / 60 / 120 / 360 — conservative only
    geo: tuple[str, ...] = ()       # ISO codes when region-specific; () = global
    language: str = "en"
    # source_credibility seed (host-level; deduped across entries)
    cred_host: str = ""
    cred_score: float = 0.5
    cred_rationale: str = ""
    tier: str = "wire"
    state_affiliation: bool = False
    extra_config: dict = field(default_factory=dict)
    # Opt a structured (geojson) feed into TEXT enrichment (language_detect +
    # ner_multilingual) on top of the default geocode-only chain. Default off:
    # most GIS feeds (USGS quakes, NASA EONET) are high-volume / low-text and
    # geocode is all they need. NWS Severe/Extreme alerts, by contrast, carry a
    # rich English headline/description in `properties` (mapped to the signal's
    # `title`), so leaving them text-unenriched dragged language/entity coverage
    # down (live audit: 400 NWS signals at 0% language/entity). Flagging them in
    # gives those high-value gov alerts the same metadata as the news feeds.
    enrich_text: bool = False
    # Opt a TEXT-rich news feed into ingest-time fact extraction (the
    # `fact_extractor` stage, after ner_multilingual). Default off: each stage
    # adds REBEL/extract calls, so we roll it out incrementally to the
    # full-article news feeds that yield rich (subject,predicate,value)
    # relations (france24/cna/npr/economist) and leave gov/finance/advisory +
    # GeoJSON feeds out (graph-and-data Wave-1b item 4; REVIEW §3.6). The stage
    # defaults reject_quantity_endpoints ON, so no per-descriptor config needed.
    fact_extract: bool = False
    # SNAPSHOT feed: every poll returns the FULL current alert set (NWS active
    # alerts, GDACS), so the same items are re-offered to the pipeline on every
    # poll. Enrichment runs BEFORE the write (run_baseline, then
    # write_canonical_signal's S-4 content-hash collapse), so an NER stage on a
    # snapshot feed POSTs the models host once per re-offered item, then the
    # row collapses. Snapshot feeds therefore never carry ``ner_multilingual``
    # (their geo comes from the feed's own geometry / location fields) and
    # cannot opt into ``fact_extract`` (which consumes NER's triples). The guard
    # is tests/data_pkg/test_source_catalog_snapshot_feeds_no_ner.py.
    snapshot: bool = False
    # Wave-E access class. Default ``public`` (a free, no-auth feed — the
    # taxonomy's rule); an entry whose live head carries another class says so.
    access_class: str = "public"
    # S1-T8 / ROSTER-2 — the entry's editorial class when it is curated rather
    # than derived from ``tier`` (:func:`catalog_source_class`): the catalog's
    # state-controlled outlets carry ``state_media``. ``None`` = derive.
    source_class: str | None = None
    # W10 TAX-2 — the feed's declared classification: ``<family>:<value>`` tags
    # of registered vocabulary families, written to ``scope.classifications``
    # and stamped on every signal it writes. Empty (the default) leaves the
    # built descriptor — and its content hash — exactly as before.
    classifications: tuple[str, ...] = ()
    # ``cadence.expect_quiet``: a feed quiet by nature, exempt from
    # the watchdog's empty-poll streak page. False leaves the body unchanged.
    expect_quiet: bool = False


# ---------------------------------------------------------------------------
# The catalog — profile DATA (ROSTER-1). The geopolitics exemplar's 46 feeds
# live in profiles/geopolitics/rosters/source_catalog.yaml (every URL probed
# live 2026-06-09/10: HTTP 200 + feedparser/GeoJSON parse); the selected
# profile's ``rosters.source_catalog`` names the file. This module is the
# platform half: the entry model above, the loader below, the descriptor
# builder, the credibility seeding and ``--verify``.
# ---------------------------------------------------------------------------

#: The fields an entry may carry — exactly :class:`CatalogEntry`'s.
CATALOG_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(CatalogEntry))
_TUPLE_FIELDS = ("tags", "geo", "classifications")


def catalog_entry(row: Mapping[str, Any]) -> CatalogEntry:
    """One YAML row -> a :class:`CatalogEntry` (``tags`` / ``geo`` as tuples).
    An unknown key or a missing required one is refused, naming the entry."""
    unknown = sorted(set(row) - set(CATALOG_FIELDS))
    if unknown:
        raise ValueError(f"catalog entry {row.get('id')!r}: unknown key(s) {unknown}")
    kwargs = {k: (tuple(v) if k in _TUPLE_FIELDS and v is not None else v) for k, v in row.items()}
    if kwargs.get("source_class") is not None and kwargs["source_class"] not in SOURCE_CLASSES:
        raise ValueError(
            f"catalog entry {row.get('id')!r}: source_class {kwargs['source_class']!r} "
            f"is not one of {list(SOURCE_CLASSES)}"
        )
    try:
        return CatalogEntry(**kwargs)
    except TypeError as exc:
        raise ValueError(f"catalog entry {row.get('id')!r}: {exc}") from None


def load_catalog(path: pathlib.Path | str | None = None) -> tuple[CatalogEntry, ...]:
    """The catalog entries of a catalog YAML file, or — ``None`` — of the
    selected profile (``LEGBA_PROFILE``, else ``profiles/geopolitics``,
    resolved against the repo root); ``()`` when that profile declares no
    catalog (``rosters.source_catalog: null``)."""
    if path is None:
        rows = rosters.source_catalog(load_profile(resolve_profile(root=REPO_ROOT)))
    else:
        rows = rosters.load_source_catalog(path)
    return tuple(catalog_entry(r) for r in rows)


#: The selected profile's catalog, loaded once at import (the walker passes a
#: ``--profile``'s own file to :func:`load_catalog` instead).
CATALOG: tuple[CatalogEntry, ...] = load_catalog()

#: The catalog's state-controlled feeds — READ from the catalog (each entry's
#: ``source_class: state_media``), never a second hand-kept list (ROSTER-2).
STATE_MEDIA_IDS: frozenset[str] = frozenset(e.id for e in CATALOG if e.source_class == "state_media")


# ---------------------------------------------------------------------------
# Descriptor construction
# ---------------------------------------------------------------------------


def _stagger_minute(source_id: str, modulo: int = 60) -> int:
    """Stable per-source minute offset so 40+ feeds don't all poll at :00."""
    return int(hashlib.sha256(source_id.encode()).hexdigest(), 16) % modulo


def cron_for(entry: CatalogEntry) -> str:
    """Conservative, constant-period cron with a stable stagger offset."""
    if entry.cadence_minutes < 60:
        # e.g. "7-59/30 * * * *" — every 30 min, offset by a stable 0..29.
        offset = _stagger_minute(entry.id, entry.cadence_minutes)
        return f"{offset}-59/{entry.cadence_minutes} * * * *"
    minute = _stagger_minute(entry.id)
    hours = entry.cadence_minutes // 60
    if hours == 1:
        return f"{minute} * * * *"
    hour_offset = _stagger_minute(entry.id, hours)
    return f"{minute} {hour_offset}-23/{hours} * * *"


def build_descriptor(entry: CatalogEntry) -> SourceDescriptor:
    """Build + validate one SourceDescriptor from a catalog entry.

    Mirrors the registered YAML descriptors (source_bbc_world.yaml /
    source_usgs_earthquakes.yaml): shared open poll source, baseline
    enrichment ONCE at the source, media by reference.
    """
    if entry.snapshot and entry.fact_extract:
        raise ValueError(
            f"{entry.id}: a snapshot feed cannot opt into fact_extract "
            "(the stage consumes ner_multilingual's triples; snapshot feeds "
            "carry no NER stage)"
        )
    if entry.kind == "rss":
        enrichment = [FilterStage(kind="language_detect", config={})]
        # Snapshot feeds re-offer every item on every poll and enrich before
        # the write collapses the repeat: no NER POST for them (see
        # CatalogEntry.snapshot).
        if not entry.snapshot:
            enrichment.append(FilterStage(kind="ner_multilingual", config={}))
        # Ingest-time fact extraction (opt-in, text-rich news feeds only).
        # Mirrors the registered source_bbc_world.yaml stage: reuse the REBEL
        # triples NER already produced; reject_quantity_endpoints is ON by the
        # stage's own default, so no per-descriptor config is needed.
        if entry.fact_extract:
            enrichment.append(
                FilterStage(
                    kind="fact_extractor",
                    config={
                        "backend": "relation",
                        "emit_graph_edges": False,
                        "max_facts_per_signal": 30,
                    },
                )
            )
        enrichment.append(FilterStage(kind="geocode", config={}))
        max_age = 86_400            # 24h news window
    elif entry.kind == "geojson":
        # Model-free structured modality — geocode-only by default. Feeds that
        # carry meaningful text in their feature properties (NWS alert
        # headlines) opt into language + entity enrichment via enrich_text so
        # they get the same language/entity coverage as the news feeds; the
        # enricher reads payload["title"] (geojson maps the feature headline →
        # title), which language_detect / ner_multilingual both consume.
        if entry.enrich_text:
            enrichment = [FilterStage(kind="language_detect", config={})]
            if not entry.snapshot:
                enrichment.append(FilterStage(kind="ner_multilingual", config={}))
            enrichment.append(FilterStage(kind="geocode", config={}))
        else:
            enrichment = [FilterStage(kind="geocode", config={})]
        max_age = 604_800           # match the hazard feeds' weekly windows
    else:                            # pragma: no cover - guarded by tests
        raise ValueError(f"unsupported catalog kind: {entry.kind!r}")

    config: dict = {
        "url": {"factory_kind": "text", "raw": entry.url},
        "user_agent": {"factory_kind": "text", "raw": USER_AGENT},
    }
    for key, raw in entry.extra_config.items():
        factory_kind = "number" if isinstance(raw, (int, float)) else "text"
        config[key] = {"factory_kind": factory_kind, "raw": raw}

    return SourceDescriptor(
        identity=SourceIdentity(
            id=entry.id,
            name=entry.name,
            kind=entry.kind,
            schema_uri="legba/source/1.0.0",
            version="0" * 16,        # placeholder — registry stamps content hash
            abstraction_level=AbstractionLevel.L1,
            state=LifecycleState.ACTIVE,
            owner=OWNER,
            created=CREATED,
        ),
        scope=SourceScope(
            owner_tenant="shared",
            geo=list(entry.geo),
            languages=[entry.language],
            tags=list(entry.tags),
            classifications=list(entry.classifications),   # W10 TAX-2
            source_class=catalog_source_class(entry),   # S1-T8
            # Wave-E: a catalog entry is a free, no-auth, openly available feed —
            # the taxonomy's `public` rule, per entry. The live heads carry it;
            # the builder omitted it and fell to the fail-closed `restricted`
            # default (W6 SPLIT-1 fold).
            access_class=entry.access_class,
        ),
        acquisition="poll",
        config=config,
        cadence=CadenceBlock(
            schedule=Cron(raw=cron_for(entry)),
            cooldown_seconds=60,
            jitter_seconds=30,
            expect_quiet=entry.expect_quiet,
        ),
        pipeline=SourcePipeline(
            ingestion_filters=[
                FilterStage(kind="dedupe_tier_1", config={}),
                FilterStage(kind="dedupe_tier_2", config={}),
            ],
            enrichment=enrichment,
            media="reference",
        ),
        output=SourceOutput(retention="interest", max_age_seconds=max_age, delivery="lossy"),
        subscription_policy="open",
    )


def credibility_rows(
    entries: Iterable[CatalogEntry] | None = None,
) -> list[tuple[str, float, str, str, str, bool]]:
    """Deduped (host, score, rationale, scored_by, tier, state_affiliation) rows
    of ``entries`` (default :data:`CATALOG`).

    Entries sharing a host (e.g. the two cdc.gov feeds) must agree on the
    credibility fields — enforced here so the catalog can't drift silently.
    """
    rows: dict[str, tuple[str, float, str, str, str, bool]] = {}
    for entry in (CATALOG if entries is None else entries):
        row = (
            entry.cred_host, entry.cred_score, entry.cred_rationale,
            SCORED_BY, entry.tier, entry.state_affiliation,
        )
        existing = rows.get(entry.cred_host)
        if existing is not None and existing != row:
            raise ValueError(
                f"conflicting credibility seeds for host {entry.cred_host!r}"
            )
        rows[entry.cred_host] = row
    return list(rows.values())


# ---------------------------------------------------------------------------
# Registration (direct-DB, _p17_registrar pattern)
# ---------------------------------------------------------------------------


# Operator OFF-states that a re-run of this registrar must NEVER override back to
# active. Every catalog entry declares `state=active`; without this guard a
# `clean` re-run (e.g. deploy/deploy.sh, which is idempotent) would drive a source
# the operator PAUSED (a bad/blocked feed held for a token/recheck) or RETIRED (a
# dead feed) straight back to active — silently reviving daily 401s / errors. When
# the live head is in one of these states we leave it ALONE (never force active).
_STICKY_OFF_STATES: frozenset[str] = frozenset({"paused", "retired"})


async def register_catalog(
    pg: PostgresStore, reg: DescriptorRegistry,
    entries: Iterable[CatalogEntry] | None = None,
) -> list[RegisterResult]:
    """Idempotently register every catalog descriptor. Pure function of the
    injected store/registry — the test suite drives it against the per-session
    test DB; the operator's `main()` wires the env-selected DB.

    Fresh-deploy source guard: a source whose LIVE head is paused/retired is
    PRESERVED (skipped), never forced back to the catalog's declared `active`
    state. A fresh instance (no live head) registers the full active catalog as
    before, so this only ever suppresses re-activation of an operator OFF-state.
    """
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT descriptor_id, state FROM source_descriptors WHERE is_head"
        )
    live_state = {r["descriptor_id"]: r["state"] for r in rows}

    results: list[RegisterResult] = []
    for entry in (CATALOG if entries is None else entries):
        cur = live_state.get(entry.id)
        if cur in _STICKY_OFF_STATES:
            results.append(
                RegisterResult(
                    Family.SOURCE.value, entry.id, "unchanged", "-",
                    detail=f"preserved live {cur} state (not forced active)",
                )
            )
            continue
        desc = build_descriptor(entry)
        results.append(
            await register_descriptor(pg, reg, family=Family.SOURCE, descriptor=desc)
        )
    return results


async def seed_credibility(
    pg: PostgresStore, entries: Iterable[CatalogEntry] | None = None,
) -> tuple[int, int]:
    """Seed source_credibility rows of ``entries`` (default :data:`CATALOG`;
    0014 convention: ON CONFLICT DO NOTHING — operator overrides and the 0014
    baseline always win). Returns (inserted, already_present). Requires
    migration 0031 (tier / state_affiliation columns) — fails loud if it has
    not been applied."""
    inserted = 0
    skipped = 0
    async with pg.acquire() as conn:
        for host, score, rationale, scored_by, tier, state_aff in credibility_rows(entries):
            status = await conn.execute(
                """
                INSERT INTO source_credibility
                    (source_host, score, score_rationale, scored_by, tier, state_affiliation)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (source_host) DO NOTHING
                """,
                host, score, rationale, scored_by, tier, state_aff,
            )
            if status.endswith("1"):
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


# ---------------------------------------------------------------------------
# --verify mode: live probe + parse check, registers nothing
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    entry_id: str
    verdict: str                    # ok / ok-redirect / dead / parse-fail / empty
    detail: str = ""


async def _probe_one(client, entry: CatalogEntry) -> ProbeResult:
    import httpx

    try:
        resp = await client.get(entry.url)
    except httpx.HTTPError as exc:
        return ProbeResult(entry.id, "dead", f"{type(exc).__name__}: {exc}"[:120])
    if resp.status_code >= 400:
        return ProbeResult(entry.id, "dead", f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        return ProbeResult(entry.id, "dead", f"HTTP {resp.status_code} (non-200 challenge?)")

    redirected = bool(resp.history)
    if entry.kind == "rss":
        import feedparser

        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            why = "no entries"
            if getattr(parsed, "bozo", 0):
                why += f"; bozo: {getattr(parsed, 'bozo_exception', '')}"
            return ProbeResult(entry.id, "empty", why[:120])
    else:  # geojson
        try:
            doc = json.loads(resp.content)
        except ValueError as exc:
            return ProbeResult(entry.id, "parse-fail", str(exc)[:120])
        if not isinstance(doc, dict) or (
            doc.get("type") not in ("FeatureCollection", "Feature")
            and "features" not in doc
        ):
            return ProbeResult(entry.id, "parse-fail", "not a GeoJSON document")

    if redirected:
        return ProbeResult(entry.id, "ok-redirect", f"final: {resp.url}"[:120])
    return ProbeResult(entry.id, "ok")


async def verify_catalog() -> int:
    """Probe every catalog feed; print a verdict table. Returns #failures."""
    import httpx

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=25.0,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        sem = asyncio.Semaphore(8)

        async def bounded(entry: CatalogEntry) -> ProbeResult:
            async with sem:
                return await _probe_one(client, entry)

        results = await asyncio.gather(*(bounded(e) for e in CATALOG))

    failures = 0
    print(f"catalog verify — {len(results)} feeds:")
    for r in results:
        mark = {"ok": "+", "ok-redirect": "~"}.get(r.verdict, "!")
        line = f"  {mark} {r.verdict:>12}  {r.entry_id}"
        if r.detail:
            line += f"   ({r.detail})"
        print(line)
        if r.verdict not in ("ok", "ok-redirect"):
            failures += 1
    print(f"  -> {len(results) - failures} ok, {failures} failing")
    return failures


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def _amain(verify_only: bool) -> int:
    if verify_only:
        return 1 if await verify_catalog() else 0

    pg, reg = await open_registry()
    try:
        results = await register_catalog(pg, reg)
        failures = print_results(f"source catalog ({len(results)} sources):", results)
        inserted, skipped = await seed_credibility(pg)
        print(f"source_credibility seeds: +{inserted} inserted, ={skipped} already present")
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify", action="store_true",
        help="live HTTP probe + parse check per feed; registers nothing",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args.verify))


if __name__ == "__main__":
    sys.exit(main())
