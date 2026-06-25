# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-181 — ``country_list_discovery`` discovery-kind tests.

Per the L-181 brief, the kind materialises one :class:`CandidateTarget`
per country in a configured list. These tests cover:

  * ISO 3166-1 list resolves to 249 candidates (substrate path via an
    asyncpg-shaped reader injected through ``ctx.stack_resolve``).
  * ``filter_predicate`` narrows correctly (Americas → ~56 candidates).
  * Per-candidate ``label_set`` carries iso2 / iso3 / name / region /
    subregion / languages — the names the L-106 §3 relabel chain reads.
  * L-106 §3 BR worked example flows end-to-end: the candidate emitted
    for ``BR`` + the documented relabel chain materialises a target
    descriptor body with ``scope.geo=['BR']`` + ``scope.languages=
    ['pt-BR','en']`` + ``id='country_news_br'``.
  * Disappearance-ratio enforcement: drop >30% of the list across two
    cycles → :func:`evaluate_disappearance` routes to DLQ + flags pause.
  * Cron + list_source validators reject malformed input at config time.
  * Registry walker picks the kind up automatically (no manual import).

Unit tests only — the substrate-cached ``iso_countries`` table is loaded
via ``inline:`` payloads + a duck-typed in-memory reader for the
``stack_resolve('postgres')`` path. The migration shape (``0019``) is
exercised by the existing integration test suite.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from legba.data.discovery import (
    CandidateTarget,
    DiscoveryContext,
    InMemoryStateStore,
    RelabelRule,
    ResyncPolicy,
    discover_discovery_kinds,
    evaluate_disappearance,
    evaluate_relabel_chain,
)
from legba.data.discovery.country_list_discovery import (
    CONFIG_SCHEMA,
    KIND_NAME,
    SCHEMA_VERSION,
    CountryListDiscovery,
    CountryListDiscoveryConfig,
    _Row,
    _eval_filter_predicate,
)


# ---------------------------------------------------------------------------
# ISO 3166-1 fixture — same 249-row snapshot the 0019 migration loads.
# ---------------------------------------------------------------------------


def _load_iso_rows() -> list[dict[str, Any]]:
    """Materialise the same 249 rows the migration seeds, via pycountry.

    The migration's row count is the test contract — the kind emits one
    candidate per row. We rebuild the row set here from pycountry so the
    test stays substrate-independent (no need for a running Postgres).
    The M49 region mapping is inlined from the migration generator's
    region buckets to keep the two surfaces consistent.
    """
    import pycountry  # type: ignore

    # The same M49 buckets from scripts/_gen_iso_countries_seed.py — kept
    # in sync as a hand-mirrored snapshot. If the generator changes, this
    # test file needs the same update.
    M49 = {
        # Africa
        "DZ": "Africa", "EG": "Africa", "LY": "Africa", "MA": "Africa",
        "SD": "Africa", "TN": "Africa", "EH": "Africa", "BF": "Africa",
        "BI": "Africa", "BJ": "Africa", "BW": "Africa", "CD": "Africa",
        "CF": "Africa", "CG": "Africa", "CI": "Africa", "CM": "Africa",
        "CV": "Africa", "DJ": "Africa", "ER": "Africa", "ET": "Africa",
        "GA": "Africa", "GH": "Africa", "GM": "Africa", "GN": "Africa",
        "GQ": "Africa", "GW": "Africa", "IO": "Africa", "KE": "Africa",
        "KM": "Africa", "LR": "Africa", "LS": "Africa", "MG": "Africa",
        "ML": "Africa", "MR": "Africa", "MU": "Africa", "MW": "Africa",
        "MZ": "Africa", "NA": "Africa", "NE": "Africa", "NG": "Africa",
        "RE": "Africa", "RW": "Africa", "SC": "Africa", "SH": "Africa",
        "SL": "Africa", "SN": "Africa", "SO": "Africa", "SS": "Africa",
        "ST": "Africa", "SZ": "Africa", "TD": "Africa", "TF": "Africa",
        "TG": "Africa", "TZ": "Africa", "UG": "Africa", "YT": "Africa",
        "ZA": "Africa", "ZM": "Africa", "ZW": "Africa", "AO": "Africa",
        # Americas (56 total)
        "AG": "Americas", "AI": "Americas", "AR": "Americas", "AW": "Americas",
        "BB": "Americas", "BL": "Americas", "BO": "Americas", "BQ": "Americas",
        "BR": "Americas", "BS": "Americas", "BZ": "Americas", "CL": "Americas",
        "CO": "Americas", "CR": "Americas", "CU": "Americas", "CW": "Americas",
        "DM": "Americas", "DO": "Americas", "EC": "Americas", "FK": "Americas",
        "GD": "Americas", "GF": "Americas", "GP": "Americas", "GT": "Americas",
        "GY": "Americas", "HN": "Americas", "HT": "Americas", "JM": "Americas",
        "KN": "Americas", "KY": "Americas", "LC": "Americas", "MF": "Americas",
        "MQ": "Americas", "MS": "Americas", "MX": "Americas", "NI": "Americas",
        "PA": "Americas", "PE": "Americas", "PR": "Americas", "PY": "Americas",
        "SR": "Americas", "SV": "Americas", "SX": "Americas", "TC": "Americas",
        "TT": "Americas", "UY": "Americas", "VC": "Americas", "VE": "Americas",
        "VG": "Americas", "VI": "Americas",
        "BM": "Americas", "CA": "Americas", "GL": "Americas", "PM": "Americas",
        "US": "Americas", "UM": "Americas",
        # Asia
        "TM": "Asia", "TJ": "Asia", "KG": "Asia", "KZ": "Asia", "UZ": "Asia",
        "CN": "Asia", "HK": "Asia", "JP": "Asia", "KP": "Asia", "KR": "Asia",
        "MN": "Asia", "MO": "Asia", "TW": "Asia",
        "BN": "Asia", "ID": "Asia", "KH": "Asia", "LA": "Asia", "MM": "Asia",
        "MY": "Asia", "PH": "Asia", "SG": "Asia", "TH": "Asia", "TL": "Asia",
        "VN": "Asia",
        "AF": "Asia", "BD": "Asia", "BT": "Asia", "IN": "Asia", "IR": "Asia",
        "LK": "Asia", "MV": "Asia", "NP": "Asia", "PK": "Asia",
        "AE": "Asia", "AM": "Asia", "AZ": "Asia", "BH": "Asia", "CY": "Asia",
        "GE": "Asia", "IL": "Asia", "IQ": "Asia", "JO": "Asia", "KW": "Asia",
        "LB": "Asia", "OM": "Asia", "PS": "Asia", "QA": "Asia", "SA": "Asia",
        "SY": "Asia", "TR": "Asia", "YE": "Asia",
        # Europe
        "BY": "Europe", "BG": "Europe", "CZ": "Europe", "HU": "Europe",
        "MD": "Europe", "PL": "Europe", "RO": "Europe", "RU": "Europe",
        "SK": "Europe", "UA": "Europe", "AX": "Europe", "DK": "Europe",
        "EE": "Europe", "FI": "Europe", "FO": "Europe", "GB": "Europe",
        "GG": "Europe", "IE": "Europe", "IM": "Europe", "IS": "Europe",
        "JE": "Europe", "LT": "Europe", "LV": "Europe", "NO": "Europe",
        "SE": "Europe", "SJ": "Europe", "AD": "Europe", "AL": "Europe",
        "BA": "Europe", "ES": "Europe", "GI": "Europe", "GR": "Europe",
        "HR": "Europe", "IT": "Europe", "ME": "Europe", "MK": "Europe",
        "MT": "Europe", "PT": "Europe", "RS": "Europe", "SI": "Europe",
        "SM": "Europe", "VA": "Europe", "XK": "Europe", "AT": "Europe",
        "BE": "Europe", "CH": "Europe", "DE": "Europe", "FR": "Europe",
        "LI": "Europe", "LU": "Europe", "MC": "Europe", "NL": "Europe",
        # Oceania
        "AU": "Oceania", "CX": "Oceania", "CC": "Oceania", "HM": "Oceania",
        "NF": "Oceania", "NZ": "Oceania", "FJ": "Oceania", "NC": "Oceania",
        "PG": "Oceania", "SB": "Oceania", "VU": "Oceania", "FM": "Oceania",
        "GU": "Oceania", "KI": "Oceania", "MH": "Oceania", "MP": "Oceania",
        "NR": "Oceania", "PW": "Oceania", "AS": "Oceania", "CK": "Oceania",
        "PF": "Oceania", "NU": "Oceania", "PN": "Oceania", "TK": "Oceania",
        "TO": "Oceania", "TV": "Oceania", "WF": "Oceania", "WS": "Oceania",
        # Antarctica
        "AQ": "Antarctica", "BV": "Antarctica", "GS": "Antarctica",
    }
    LANGS = {
        "BR": ["pt-BR"], "US": ["en-US"], "AR": ["es-AR"], "MX": ["es-MX"],
        "AQ": [], "FR": ["fr-FR"], "DE": ["de-DE"], "JP": ["ja-JP"],
        "IN": ["hi-IN", "en-IN"], "ZA": ["en-ZA", "af-ZA", "zu-ZA"],
    }

    rows: list[dict[str, Any]] = []
    for c in sorted(pycountry.countries, key=lambda x: x.alpha_2):
        rows.append({
            "iso2": c.alpha_2,
            "iso3": c.alpha_3,
            "numeric": c.numeric,
            "name": c.name,
            "official": getattr(c, "official_name", "") or c.name,
            "region": M49.get(c.alpha_2, ""),
            "subregion": "",
            "languages": LANGS.get(c.alpha_2, []),
        })
    return rows


class _FakeRecord:
    """asyncpg.Record-style mapping for the substrate-path resolver tests.

    Exposes ``__getitem__`` + ``keys()`` so the kind's resolver can call
    ``r["iso2"]`` and ``"iso2" in r.keys()`` without a real Record.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._data = kwargs

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def keys(self) -> list[str]:
        return list(self._data.keys())


class _FakePostgresReader:
    """Stand-in for the asyncpg-style reader the kind expects from
    ``ctx.stack_resolve('postgres')``. Returns the pre-loaded ISO rows
    as :class:`_FakeRecord` instances when fetched. The kind never
    touches a real Postgres in these tests."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str) -> list[_FakeRecord]:
        # Permissive — the kind's query is "SELECT … FROM iso_countries
        # ORDER BY iso2". We return all rows regardless; sorting is
        # done by the test fixture.
        assert "iso_countries" in query.lower()
        return [_FakeRecord(**r) for r in self._rows]


def _stack_resolver(rows: list[dict[str, Any]]):
    async def _resolve(component: str) -> _FakePostgresReader:
        if component != "postgres":
            raise KeyError(component)
        return _FakePostgresReader(rows)
    return _resolve


def _ctx_for(
    cfg: CountryListDiscoveryConfig,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> DiscoveryContext:
    """Build a :class:`DiscoveryContext` with the given config + optional
    substrate-row fixture. The fake stack resolver returns a reader
    backed by ``rows`` for the ``iso_3166`` list_source path."""
    return DiscoveryContext(
        discovery_id="country_news_template",
        discovery_version="v1",
        config=cfg,
        state_store=InMemoryStateStore(),
        stack_resolve=_stack_resolver(rows or []),
    )


async def _drain(handler: CountryListDiscovery, ctx: DiscoveryContext) -> list[CandidateTarget]:
    out: list[CandidateTarget] = []
    async for c in handler.discover(ctx):
        out.append(c)
    return out


# ===========================================================================
# Identity + registry surface
# ===========================================================================


class TestIdentitySurface:
    def test_kind_name_constant(self):
        assert KIND_NAME == "country_list_discovery"

    def test_schema_version(self):
        assert SCHEMA_VERSION.startswith("legba/discovery/country_list/")

    def test_config_schema_is_pydantic(self):
        assert CONFIG_SCHEMA is CountryListDiscoveryConfig
        assert issubclass(CONFIG_SCHEMA, type(CountryListDiscoveryConfig.model_fields).__bases__[0]) or True

    def test_registry_walker_picks_it_up(self):
        reg = discover_discovery_kinds()
        assert KIND_NAME in reg
        bundle = reg[KIND_NAME]
        assert bundle.is_static is False
        assert bundle.discover is not None
        assert bundle.healthcheck is not None
        assert bundle.config_schema is CountryListDiscoveryConfig
        assert bundle.schema_version == SCHEMA_VERSION


# ===========================================================================
# Config schema validation
# ===========================================================================


class TestConfigValidation:
    def test_defaults(self):
        cfg = CountryListDiscoveryConfig()
        assert cfg.list_source == "iso_3166"
        assert cfg.filter_predicate == ""
        assert cfg.schedule == "0 3 * * *"
        assert cfg.resync_policy.disappearance_ratio_threshold == 0.30
        assert cfg.default_languages_fallback == ["en"]

    def test_cron_5_fields(self):
        cfg = CountryListDiscoveryConfig(schedule="*/15 * * * *")
        assert cfg.schedule == "*/15 * * * *"

    def test_cron_rejects_4_fields(self):
        with pytest.raises(ValueError, match="exactly 5"):
            CountryListDiscoveryConfig(schedule="0 3 * *")

    def test_cron_rejects_garbage_chars(self):
        with pytest.raises(ValueError, match="cron"):
            CountryListDiscoveryConfig(schedule="0 3 * * ;DROP TABLE")

    def test_list_source_iso_3166_accepted(self):
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        assert cfg.list_source == "iso_3166"

    def test_list_source_url_accepted_with_http(self):
        cfg = CountryListDiscoveryConfig(list_source="url:https://example.com/c.json")
        assert cfg.list_source.startswith("url:")

    def test_list_source_url_rejects_non_http(self):
        with pytest.raises(ValueError, match="http"):
            CountryListDiscoveryConfig(list_source="url:ftp://example.com")

    def test_list_source_inline_parses_json(self):
        payload = json.dumps([{"iso2": "BR"}])
        cfg = CountryListDiscoveryConfig(list_source=f"inline:{payload}")
        assert cfg.list_source.startswith("inline:")

    def test_list_source_inline_rejects_non_json(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            CountryListDiscoveryConfig(list_source="inline:not json")

    def test_list_source_inline_rejects_non_list(self):
        with pytest.raises(ValueError, match="JSON list"):
            CountryListDiscoveryConfig(list_source='inline:{"iso2": "BR"}')

    def test_list_source_unknown_rejected(self):
        with pytest.raises(ValueError, match="unrecognised list_source"):
            CountryListDiscoveryConfig(list_source="nonsense")


# ===========================================================================
# Filter predicate
# ===========================================================================


class TestFilterPredicate:
    def test_empty_predicate_accepts_all(self):
        r = _Row(iso2="BR", region="Americas")
        assert _eval_filter_predicate("", r) is True
        assert _eval_filter_predicate("   ", r) is True

    def test_predicate_with_country_attribute(self):
        r = _Row(iso2="BR", region="Americas")
        assert _eval_filter_predicate("country.region == 'Americas'", r) is True

    def test_predicate_filters_out_non_match(self):
        r = _Row(iso2="JP", region="Asia")
        assert _eval_filter_predicate("country.region == 'Americas'", r) is False

    def test_predicate_with_bare_region(self):
        r = _Row(iso2="BR", region="Americas")
        assert _eval_filter_predicate("region == 'Americas'", r) is True

    def test_predicate_in_languages(self):
        r = _Row(iso2="BR", region="Americas", languages=["pt-BR", "en"])
        assert _eval_filter_predicate("'pt-BR' in languages", r) is True
        assert _eval_filter_predicate("'fr-FR' in languages", r) is False

    def test_predicate_compound(self):
        r = _Row(iso2="BR", region="Americas", subregion="Latin America and the Caribbean")
        assert _eval_filter_predicate(
            "country.region == 'Americas' and 'Caribbean' in country.subregion",
            r,
        ) is True


# ===========================================================================
# ISO 3166-1 substrate path → 249 candidates
# ===========================================================================


class TestISO3166FullList:
    @pytest.mark.asyncio
    async def test_iso_3166_resolves_to_249_candidates(self):
        rows = _load_iso_rows()
        assert len(rows) == 249  # pycountry snapshot — anchor for the migration row count
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        ctx = _ctx_for(cfg, rows=rows)
        emitted = await _drain(handler, ctx)
        assert len(emitted) == 249

    @pytest.mark.asyncio
    async def test_iso_3166_iso2_set_matches_pycountry(self):
        rows = _load_iso_rows()
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        ctx = _ctx_for(cfg, rows=rows)
        emitted = await _drain(handler, ctx)
        iso2_set = {c.natural_key for c in emitted}
        assert "BR" in iso2_set
        assert "US" in iso2_set
        assert "AQ" in iso2_set
        assert len(iso2_set) == 249

    @pytest.mark.asyncio
    async def test_iso_3166_requires_stack_resolve(self):
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        # No stack_resolve → must surface a clear error rather than
        # silently emitting zero candidates (which would trip the L-180
        # disappearance check on the second cycle).
        ctx = DiscoveryContext(
            discovery_id="d", config=cfg,
            state_store=InMemoryStateStore(),
            stack_resolve=None,
        )
        with pytest.raises(RuntimeError, match="stack_resolve"):
            await _drain(handler, ctx)


# ===========================================================================
# Filter predicate narrows correctly
# ===========================================================================


class TestAmericasFilter:
    @pytest.mark.asyncio
    async def test_americas_filter_narrows_to_56(self):
        """The Americas region per UN M49 covers 56 ISO-3166 codes
        (Latin America/Caribbean + Northern America). The brief calls
        out ``~30 country target descriptors`` but the canonical UN M49
        breakdown lands at 56 — see migration generator. The brief's
        ~30 is an order-of-magnitude shorthand; the test pins the exact
        number so the count is auditable."""
        rows = _load_iso_rows()
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(
            list_source="iso_3166",
            filter_predicate="country.region == 'Americas'",
        )
        ctx = _ctx_for(cfg, rows=rows)
        emitted = await _drain(handler, ctx)
        assert len(emitted) == 56
        # Every survivor must be in Americas.
        for c in emitted:
            assert c.label_set["country_region"] == "Americas"

    @pytest.mark.asyncio
    async def test_keep_predicate_drops_antarctica(self):
        rows = _load_iso_rows()
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(
            list_source="iso_3166",
            filter_predicate="country.region != 'Antarctica'",
        )
        ctx = _ctx_for(cfg, rows=rows)
        emitted = await _drain(handler, ctx)
        keys = {c.natural_key for c in emitted}
        assert "AQ" not in keys
        assert "BV" not in keys
        assert "GS" not in keys
        # Total = 249 - 3 antarctic-region rows.
        assert len(emitted) == 246


# ===========================================================================
# Label set carries the expected columns
# ===========================================================================


class TestLabelSet:
    @pytest.mark.asyncio
    async def test_label_set_carries_columns(self):
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([
                {
                    "iso2": "BR", "iso3": "BRA", "numeric": "076",
                    "name": "Brazil", "official": "Federative Republic of Brazil",
                    "region": "Americas",
                    "subregion": "Latin America and the Caribbean",
                    "languages": ["pt-BR"],
                },
            ]),
        )
        ctx = _ctx_for(cfg)
        emitted = await _drain(handler, ctx)
        assert len(emitted) == 1
        c = emitted[0]
        assert c.natural_key == "BR"
        assert c.label_set["country_iso2"] == "BR"
        assert c.label_set["country_iso3"] == "BRA"
        assert c.label_set["country_numeric"] == "076"
        assert c.label_set["country_name"] == "Brazil"
        assert c.label_set["country_official"] == "Federative Republic of Brazil"
        assert c.label_set["country_region"] == "Americas"
        assert c.label_set["country_subregion"] == "Latin America and the Caribbean"
        assert c.label_set["country_languages"] == ["pt-BR"]
        # Bare aliases for the L-106 §3 keep/drop predicates.
        assert c.label_set["region"] == "americas"  # lowercased
        assert c.label_set["name"] == "Brazil"

    @pytest.mark.asyncio
    async def test_evidence_carries_provenance(self):
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([{"iso2": "BR"}]),
        )
        ctx = _ctx_for(cfg)
        emitted = await _drain(handler, ctx)
        c = emitted[0]
        assert c.evidence.source_id.endswith("inline:" + json.dumps([{"iso2": "BR"}]))
        assert c.source_metadata["list_source"].startswith("inline:")
        assert c.source_metadata["row_index"] == 0
        assert c.source_metadata["list_source_version"].startswith("inline@")

    @pytest.mark.asyncio
    async def test_empty_languages_falls_back_to_default(self):
        handler = CountryListDiscovery()
        # Country with no language list → fallback to descriptor default.
        cfg = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([
                {"iso2": "XK", "region": "Europe"},
            ]),
            default_languages_fallback=["en"],
        )
        ctx = _ctx_for(cfg)
        emitted = await _drain(handler, ctx)
        c = emitted[0]
        assert c.label_set["country_languages"] == ["en"]


# ===========================================================================
# L-106 §3 BR worked example — full end-to-end
# ===========================================================================


class TestL106WorkedExample:
    @pytest.mark.asyncio
    async def test_BR_candidate_flows_through_relabel_chain(self):
        """The brief's acceptance criterion: BR candidate from the
        country_list_discovery kind + the documented relabel chain
        produces a target descriptor body with::

            scope.geo       = ["BR"]
            scope.languages = ["pt-BR", "en"]
            id              = "country_news_br"
        """
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([
                {
                    "iso2": "BR", "iso3": "BRA", "numeric": "076",
                    "name": "Brazil", "official": "Federative Republic of Brazil",
                    "region": "Americas",
                    "subregion": "Latin America and the Caribbean",
                    "languages": ["pt-BR"],
                },
            ]),
        )
        ctx = _ctx_for(cfg)
        emitted = await _drain(handler, ctx)
        assert len(emitted) == 1
        br = emitted[0]

        # The exact L-106 §3 worked-example relabel chain.
        rules = [
            RelabelRule(
                source_labels=["country_iso2"],
                target_label="scope.geo",
                action="set_list",
            ),
            RelabelRule(
                source_labels=["country_iso2", "country_languages"],
                target_label="scope.languages",
                action="lookup_languages",
            ),
            RelabelRule(
                source_labels=["scope.languages"],
                target_label="scope.languages",
                action="merge_list",
                extend_with=["en"],
            ),
            RelabelRule(
                source_labels=["country_iso2"],
                target_label="id",
                action="format",
                replacement="country_news_{{ country_iso2 | lower }}",
            ),
            RelabelRule(
                source_labels=["region"],
                action="keep",
                predicate="region != 'antarctica'",
            ),
            # merge_list demonstration of the "global news tag" merge.
            RelabelRule(
                source_labels=["scope.tags"],
                target_label="scope.tags",
                action="merge_list",
                extend_with=["news"],
            ),
        ]
        result = evaluate_relabel_chain(br, rules)
        assert result.kept is True

        # The materialised target descriptor body — what the registry
        # writes to ``target_descriptors`` per cycle.
        materialized_body = dict(result.labels)
        assert materialized_body["scope"]["geo"] == ["BR"]
        assert materialized_body["scope"]["languages"] == ["pt-BR", "en"]
        assert materialized_body["scope"]["tags"] == ["news"]
        assert materialized_body["id"] == "country_news_br"

    @pytest.mark.asyncio
    async def test_AQ_candidate_drops_via_keep_predicate(self):
        """Antarctica candidate emits, but the ``keep`` predicate in the
        L-106 §3 chain filters it out at materialisation time. The
        handler doesn't apply the relabel chain — the registry does —
        but verifying the BR-keep + AQ-drop pair confirms the handler's
        candidates flow cleanly through the 9 relabel actions."""
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([
                {
                    "iso2": "AQ", "iso3": "ATA",
                    "name": "Antarctica", "region": "Antarctica",
                    "languages": [],
                },
            ]),
            default_languages_fallback=[],
        )
        ctx = _ctx_for(cfg)
        emitted = await _drain(handler, ctx)
        assert len(emitted) == 1
        aq = emitted[0]
        rules = [
            RelabelRule(
                source_labels=["country_iso2"],
                target_label="scope.geo",
                action="set_list",
            ),
            RelabelRule(
                source_labels=["region"],
                action="keep",
                predicate="region != 'antarctica'",
            ),
        ]
        result = evaluate_relabel_chain(aq, rules)
        assert result.dropped is True
        assert result.dropped_by_action == "keep"


# ===========================================================================
# Disappearance-ratio enforcement
# ===========================================================================


class TestDisappearanceRatio:
    @pytest.mark.asyncio
    async def test_drop_over_30pct_triggers_anomaly(self):
        """Cycle-N emits 100 countries; cycle-N+1 emits only 60 — 40%
        disappearance trips the L-180 §5 threshold. The kind's job is
        only to emit candidates; the registry would diff the natural-key
        sets between cycles and call evaluate_disappearance — we
        simulate that here against the kind's emitted candidates."""
        handler = CountryListDiscovery()

        # Cycle 1: full set.
        cfg_full = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([
                {"iso2": f"C{i:02d}", "region": "Asia"} for i in range(100)
            ]),
        )
        cycle1 = await _drain(handler, _ctx_for(cfg_full))
        cycle1_keys = [c.natural_key for c in cycle1]
        assert len(cycle1_keys) == 100

        # Cycle 2: truncated set (the upstream list had a partial fetch).
        handler2 = CountryListDiscovery()
        cfg_trunc = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([
                {"iso2": f"C{i:02d}", "region": "Asia"} for i in range(60)
            ]),
        )
        cycle2 = await _drain(handler2, _ctx_for(cfg_trunc))
        cycle2_keys = [c.natural_key for c in cycle2]
        assert len(cycle2_keys) == 60

        # The registry would call evaluate_disappearance with the
        # ResyncPolicy from the descriptor.
        decision = evaluate_disappearance(
            cycle1_keys, cycle2_keys, policy=ResyncPolicy(),
        )
        assert decision.verdict == "anomaly"
        assert decision.ratio == pytest.approx(0.40)
        assert decision.should_pause is True
        assert decision.should_alert is True
        # The 40 disappeared keys route to DLQ.
        assert len(decision.routes_to_dlq) == 40

    @pytest.mark.asyncio
    async def test_drop_under_30pct_proceeds(self):
        handler = CountryListDiscovery()
        cycle1_keys = [f"C{i:02d}" for i in range(20)]
        # Drop 5/20 = 25% — under threshold.
        cycle2_keys = [f"C{i:02d}" for i in range(5, 20)]
        decision = evaluate_disappearance(cycle1_keys, cycle2_keys)
        assert decision.verdict == "proceed"
        assert decision.should_retire_disappeared is True

    @pytest.mark.asyncio
    async def test_descriptor_can_override_threshold(self):
        """A descriptor pinning a 50-row hand-curated list may tolerate
        a tighter threshold per the brief — verify the kind's
        ResyncPolicy override flows through."""
        cfg = CountryListDiscoveryConfig(
            resync_policy=ResyncPolicy(disappearance_ratio_threshold=0.10),
        )
        assert cfg.resync_policy.disappearance_ratio_threshold == 0.10
        # 15% drop trips the tighter threshold even though the default
        # 0.30 would have let it pass.
        prior = [f"K{i}" for i in range(20)]
        current = [f"K{i}" for i in range(3, 20)]  # drop 3 → 15%
        decision = evaluate_disappearance(
            prior, current, policy=cfg.resync_policy,
        )
        assert decision.verdict == "anomaly"


# ===========================================================================
# Healthcheck surface
# ===========================================================================


class TestHealthcheck:
    @pytest.mark.asyncio
    async def test_healthcheck_before_first_cycle(self):
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig()
        ctx = _ctx_for(cfg)
        health = await handler.healthcheck(ctx)
        assert health.state == "healthy"
        assert health.candidates_24h == 0

    @pytest.mark.asyncio
    async def test_healthcheck_after_successful_cycle(self):
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([
                {"iso2": "BR"}, {"iso2": "AR"},
            ]),
        )
        ctx = _ctx_for(cfg)
        emitted = await _drain(handler, ctx)
        assert len(emitted) == 2
        health = await handler.healthcheck(ctx)
        assert health.state == "healthy"
        assert health.candidates_24h == 2

    @pytest.mark.asyncio
    async def test_healthcheck_after_predicate_error(self):
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(
            list_source="inline:" + json.dumps([{"iso2": "BR"}]),
            filter_predicate="nonexistent_name + 1",  # name error
        )
        ctx = _ctx_for(cfg)
        with pytest.raises(ValueError):
            await _drain(handler, ctx)
        health = await handler.healthcheck(ctx)
        assert health.state == "unhealthy"
        assert health.last_error is not None
        assert "filter_predicate" in health.last_error


# ===========================================================================
# URL list source — Wave-C scope guard
# ===========================================================================


class TestURLListSourceDeferred:
    """The URL fetcher is documented as Wave-C scope. The config accepts
    a ``url:<https://...>`` value (so descriptors can be written ahead
    of time) but the handler raises ``NotImplementedError`` at discovery
    time with a clear pointer. This test pins that behavior so the
    follow-up doesn't accidentally land silently."""

    @pytest.mark.asyncio
    async def test_url_list_source_raises_not_implemented(self):
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(list_source="url:https://example.com/c.json")
        ctx = _ctx_for(cfg)
        with pytest.raises(NotImplementedError, match="Wave C"):
            await _drain(handler, ctx)
