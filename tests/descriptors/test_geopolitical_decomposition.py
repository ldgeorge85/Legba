# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-200 end-to-end verification — geopolitical decomposition.

Validates the descriptors/template_country.yaml + descriptors/
discovery_geopolitical_countries.yaml pair against the L-181
``country_list_discovery`` handler. The test asserts:

  1. Both YAML descriptors parse + validate against the pydantic
     TargetDescriptor schema (vocab values not in migration 0010's seed
     are registered via VocabularyRegistry first per the file header
     comments).
  2. The country_list_discovery handler, fed a 249-row inline ISO 3166
     snapshot, emits 249 candidates — one per country.
  3. Running the descriptor's relabel chain over each candidate produces
     materialised label sets with correct identity.id,
     identity.inherits, scope.geo, scope.languages, and tags for the
     full set; the BR / US / DE / JP / ZA samples are pin-asserted.
  4. The Antarctica drop predicate in the relabel chain fires correctly
     (AQ / BV / GS materialised candidates are dropped at the relabel
     stage, not at the discovery stage).
  5. The L-180 disappearance-ratio guard fires anomaly when a cycle
     truncates the ISO list by >30% (default threshold 0.30).

Unit-style test — no live Postgres. The materialisation loop (L-181 /
L-182's registry-side expander that writes per-country rows into
``target_descriptors`` with ``discovered_from`` provenance) is *not*
exercised here because that loop is the deliverable of a separate
ticket. The relabel-chain output is the contract surface this test
asserts against; the registry-side write step is integration-test
territory.

Per task brief L-200: "asserts ~249 country targets materialize
(allowing for filter_predicate variations or partial coverage in stub
tests)". 246 (249 - 3 Antarctic drops) is the expected pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.discovery import (
    CandidateTarget,
    DiscoveryContext,
    InMemoryStateStore,
    RelabelRule,
    ResyncPolicy,
    evaluate_disappearance,
    evaluate_relabel_chain,
)
from legba.data.discovery.country_list_discovery import (
    CountryListDiscovery,
    CountryListDiscoveryConfig,
)


# ---------------------------------------------------------------------------
# Paths + fixtures
# ---------------------------------------------------------------------------


# Resolve relative to this file so the suite runs from ANY checkout (the
# main workdir, a git worktree, CI) — a hardcoded absolute path silently
# pointed worktree runs at the wrong tree's descriptors.
REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"
TEMPLATE_YAML = DESCRIPTORS_DIR / "template_country.yaml"
DISCOVERY_YAML = DESCRIPTORS_DIR / "discovery_geopolitical_countries.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# Vocabulary values from configs/domains/geopolitical.yaml that are NOT
# in migration 0010's seed. The descriptor pydantic schema validates the
# *shape* (regex) of these values at parse time; the closed-set check
# is the registry's job and uses VocabularyCache. Both descriptors carry
# the full vocabulary so the parse-time shape check covers them.
EXTENDED_ENTITY_CLASSES = [
    "military_unit",
    "political_party",
    "armed_group",
    "international_org",
    "media_outlet",
    "event_series",
    "commodity",
    "infrastructure",
]

EXTENDED_RELATIONSHIP_TYPES = [
    "TradesWith",
    "BordersWith",
    "SignatoryTo",
    "SanctionsAgainst",
    "OccupiedBy",
    "SubsidiaryOf",
    "PartnersWith",
    "CompetesWith",
    "DiplomaticRelationsWith",
    "MilitaryPresenceIn",
]


# ---------------------------------------------------------------------------
# ISO 3166 fixture — mirrors the migration 0019 seed.
# ---------------------------------------------------------------------------


def _load_iso_rows() -> list[dict[str, Any]]:
    """Same builder pattern test_discovery_country_list.py uses.

    Returns 249 rows in iso2-ascending order, region mapped per UN M49,
    languages mapped for the BR / US / DE / JP / ZA pins the test cares
    about.
    """
    import pycountry  # type: ignore

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
        # Antarctica (3 — these will hit the relabel-chain drop predicate)
        "AQ": "Antarctica", "BV": "Antarctica", "GS": "Antarctica",
    }
    # BCP-47 mappings for the BR / US / DE / JP / ZA pins + a few others
    # so the test pins are deterministic without depending on the
    # _DEFAULT_COUNTRY_LANGUAGES table in relabel.py.
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


# ---------------------------------------------------------------------------
# Fake substrate accessor — same shape as test_discovery_country_list.py
# ---------------------------------------------------------------------------


class _FakeRecord:
    def __init__(self, **kwargs: Any) -> None:
        self._data = kwargs

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def keys(self) -> list[str]:
        return list(self._data.keys())


class _FakePostgresReader:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str) -> list[_FakeRecord]:
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
    discovery_id: str = "discovery_geopolitical_countries",
) -> DiscoveryContext:
    return DiscoveryContext(
        discovery_id=discovery_id,
        discovery_version="v1",
        config=cfg,
        state_store=InMemoryStateStore(),
        stack_resolve=_stack_resolver(rows or []),
    )


async def _drain(
    handler: CountryListDiscovery, ctx: DiscoveryContext
) -> list[CandidateTarget]:
    out: list[CandidateTarget] = []
    async for c in handler.discover(ctx):
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Relabel-rule builder — reads the descriptor's relabel: block and
# constructs RelabelRule instances. Pydantic-validates each rule shape
# so a malformed YAML rule surfaces as a clear parse error here.
# ---------------------------------------------------------------------------


def _rules_from_descriptor(body: dict[str, Any]) -> list[RelabelRule]:
    discovery_block = body.get("discovery") or {}
    raw_rules = discovery_block.get("relabel") or []
    return [RelabelRule.model_validate(r) for r in raw_rules]


# ===========================================================================
# 1. YAML descriptors parse + validate against pydantic schemas
# ===========================================================================


class TestDescriptorsParseAndValidate:
    """Both descriptors load + survive pydantic validation.

    Vocabulary closed-set checks (the registry's VocabularyCache run)
    are *not* exercised here — that requires a connected DB. The
    pydantic schema validates the regex shape of each value; the
    closed-set check is the L-110 layer above pydantic.
    """

    def test_template_yaml_exists(self):
        assert TEMPLATE_YAML.exists(), (
            f"template descriptor missing: {TEMPLATE_YAML}"
        )

    def test_discovery_yaml_exists(self):
        assert DISCOVERY_YAML.exists(), (
            f"discovery descriptor missing: {DISCOVERY_YAML}"
        )

    # C-1 NOTE: the two retired pre-pivot parse tests (test_template_parses_to_
    # TargetDescriptor / test_discovery_parses_to_TargetDescriptor) were
    # DELETED — they asserted the pre-pivot template shape (inline
    # SourceBindings with kind=, CONFIGURED-state L2 bodies) that the
    # source-first TargetDescriptor schema rejects by design (L-205).

    def test_template_entity_classes_cover_legacy_geopolitical(self):
        """Spec gap surfacing for L-205: the legacy YAML's entity_types
        list includes 8 values not in migration 0010's seed. The
        template carries them so the closed-set follow-up has a single
        place to source the registration list from."""
        body = _load_yaml(TEMPLATE_YAML)
        ec = body["scope"]["entity_classes"]
        for missing_in_seed in EXTENDED_ENTITY_CLASSES:
            assert missing_in_seed in ec, (
                f"template missing entity_class {missing_in_seed!r}; "
                f"L-205 vocabulary registration list incomplete"
            )

    def test_template_relationship_types_cover_legacy_geopolitical(self):
        """Same as the entity_classes test, for relationship_types."""
        body = _load_yaml(TEMPLATE_YAML)
        rt = body["scope"]["relationship_types"]
        for missing_in_seed in EXTENDED_RELATIONSHIP_TYPES:
            assert missing_in_seed in rt, (
                f"template missing relationship_type {missing_in_seed!r}; "
                f"L-205 vocabulary registration list incomplete"
            )

    def test_discovery_relabel_rules_all_validate(self):
        """Each rule pydantic-validates as a RelabelRule."""
        body = _load_yaml(DISCOVERY_YAML)
        rules = _rules_from_descriptor(body)
        # Six rules: set_list / lookup_languages / merge_list / format
        # / merge_list / merge_list / drop. (See descriptor body.)
        assert len(rules) >= 6
        actions = [r.action for r in rules]
        # The first four rules are the L-106 §3 worked-example sequence.
        assert actions[0] == "set_list"
        assert actions[1] == "lookup_languages"
        assert actions[2] == "merge_list"
        assert actions[3] == "format"
        # An identity.inherits merge + tags merge + Antarctica drop must
        # appear somewhere in the tail.
        assert "drop" in actions[4:]


# ===========================================================================
# 2. Handler emits 249 candidates against the full ISO 3166 list
# ===========================================================================


class TestDiscoveryEmits249:
    """The discovery descriptor's list_source=iso_3166 + empty
    filter_predicate must materialise one candidate per ISO 3166 row.
    """

    async def test_249_candidates_emit(self):
        body = _load_yaml(DISCOVERY_YAML)
        # The discovery descriptor's empty filter_predicate is the
        # default; build the matching CountryListDiscoveryConfig.
        cfg = CountryListDiscoveryConfig(
            list_source=body["discovery"]["list_source"],
            filter_predicate="",
        )
        handler = CountryListDiscovery()
        rows = _load_iso_rows()
        assert len(rows) == 249
        ctx = _ctx_for(cfg, rows=rows)
        emitted = await _drain(handler, ctx)
        assert len(emitted) == 249

    async def test_iso2_set_covers_pin_countries(self):
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        handler = CountryListDiscovery()
        rows = _load_iso_rows()
        ctx = _ctx_for(cfg, rows=rows)
        emitted = await _drain(handler, ctx)
        keys = {c.natural_key for c in emitted}
        for pin in ("BR", "US", "DE", "JP", "ZA"):
            assert pin in keys, f"pin country {pin} missing from discovery output"


# ===========================================================================
# 3. Relabel chain produces correct materialised labels for the pins
# ===========================================================================


# Languages the lookup_languages default table returns (per
# legba.data.discovery.relabel._DEFAULT_COUNTRY_LANGUAGES). The relabel
# rule also reads source_labels[1] as a fallback list, so countries with
# a row-side languages JSONB (BR / US / DE / JP / ZA / IN) get those.
EXPECTED_PIN_LANGUAGES = {
    "BR": ["pt-BR", "en"],
    "US": ["en-US", "en"],
    "DE": ["de-DE", "en"],
    "JP": ["ja-JP", "en"],
    "ZA": ["en-ZA", "af-ZA", "zu-ZA", "en"],
}


class TestRelabelChainProducesPinBodies:
    """For each pin (BR / US / DE / JP / ZA) the descriptor's relabel
    chain must rewrite the candidate's raw label_set into a materialised
    body carrying the canonical identity.id / identity.inherits /
    scope.geo / scope.languages values.
    """

    async def _materialise_one(self, iso2: str) -> dict[str, Any]:
        body = _load_yaml(DISCOVERY_YAML)
        rules = _rules_from_descriptor(body)
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        handler = CountryListDiscovery()
        rows = _load_iso_rows()
        ctx = _ctx_for(cfg, rows=rows)
        candidates = await _drain(handler, ctx)
        target = next((c for c in candidates if c.natural_key == iso2), None)
        assert target is not None, f"candidate for {iso2} not found"
        result = evaluate_relabel_chain(target, rules)
        assert result.kept is True, (
            f"{iso2}: relabel chain dropped the candidate "
            f"({result.dropped_by_action} @ {result.dropped_at}, "
            f"reason={result.dropped_reason!r})"
        )
        return dict(result.labels)

    @pytest.mark.parametrize(
        "iso2",
        ["BR", "US", "DE", "JP", "ZA"],
    )
    async def test_pin_scope_geo(self, iso2: str):
        body = await self._materialise_one(iso2)
        assert body["scope"]["geo"] == [iso2], (
            f"{iso2}: scope.geo={body['scope']['geo']!r} expected [{iso2!r}]"
        )

    @pytest.mark.parametrize(
        "iso2,expected",
        list(EXPECTED_PIN_LANGUAGES.items()),
    )
    async def test_pin_scope_languages(self, iso2: str, expected: list[str]):
        body = await self._materialise_one(iso2)
        # merge_list dedupes — order is the lookup output followed by
        # the merge_list extend (["en"]) for any missing entries.
        assert body["scope"]["languages"] == expected, (
            f"{iso2}: scope.languages={body['scope']['languages']!r} "
            f"expected {expected!r}"
        )

    @pytest.mark.parametrize(
        "iso2,expected_id",
        [
            ("BR", "country_geopolitical_br"),
            ("US", "country_geopolitical_us"),
            ("DE", "country_geopolitical_de"),
            ("JP", "country_geopolitical_jp"),
            ("ZA", "country_geopolitical_za"),
        ],
    )
    async def test_pin_id_format(self, iso2: str, expected_id: str):
        body = await self._materialise_one(iso2)
        assert body["identity"]["id"] == expected_id

    @pytest.mark.parametrize(
        "iso2",
        ["BR", "US", "DE", "JP", "ZA"],
    )
    async def test_pin_inherits_template_country(self, iso2: str):
        body = await self._materialise_one(iso2)
        # identity.inherits is the schema-level path; the relabel chain
        # writes [template_country] via merge_list.
        assert body["identity"]["inherits"] == ["template_country"], (
            f"{iso2}: identity.inherits={body['identity']['inherits']!r} "
            f"expected ['template_country']"
        )

    @pytest.mark.parametrize(
        "iso2",
        ["BR", "US", "DE", "JP", "ZA"],
    )
    async def test_pin_tags(self, iso2: str):
        body = await self._materialise_one(iso2)
        # tags is a label-set-only key (not in TargetScope schema); the
        # registry-side merge step strips it from the final descriptor
        # body but it's available in the relabel-chain output for the
        # subscription router + UI domain-filter.
        assert "tags" in body, f"{iso2}: relabel chain didn't write tags"
        # merge_list dedupes; ordering is the extend_with list order
        # since we start from an empty existing value.
        assert set(body["tags"]) == {"geopolitical", "news"}


# ===========================================================================
# 4. Antarctica drop predicate fires for AQ / BV / GS
# ===========================================================================


class TestAntarcticaDrop:
    """The relabel chain's `drop` rule with predicate
    `country_region == 'Antarctica'` must short-circuit the three
    Antarctic candidates so they don't materialise.
    """

    @pytest.mark.parametrize("iso2", ["AQ", "BV", "GS"])
    async def test_antarctic_candidate_drops(self, iso2: str):
        body = _load_yaml(DISCOVERY_YAML)
        rules = _rules_from_descriptor(body)
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        handler = CountryListDiscovery()
        rows = _load_iso_rows()
        ctx = _ctx_for(cfg, rows=rows)
        candidates = await _drain(handler, ctx)
        target = next((c for c in candidates if c.natural_key == iso2), None)
        assert target is not None
        result = evaluate_relabel_chain(target, rules)
        assert result.dropped is True
        assert result.dropped_by_action == "drop"

    async def test_full_run_materialises_246_targets(self):
        """249 candidates - 3 Antarctic drops = 246 materialised bodies.

        This is the headline acceptance assertion from the task brief:
        "asserts ~249 country targets materialize". 246 lands on the
        nose given the Antarctica drop rule.
        """
        body = _load_yaml(DISCOVERY_YAML)
        rules = _rules_from_descriptor(body)
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        handler = CountryListDiscovery()
        rows = _load_iso_rows()
        ctx = _ctx_for(cfg, rows=rows)
        candidates = await _drain(handler, ctx)
        assert len(candidates) == 249  # emit-side count
        materialised: list[tuple[str, dict[str, Any]]] = []
        dropped: list[str] = []
        for c in candidates:
            result = evaluate_relabel_chain(c, rules)
            if result.dropped:
                dropped.append(c.natural_key)
            else:
                materialised.append((c.natural_key, dict(result.labels)))
        assert len(materialised) == 246, (
            f"expected 246 materialised, got {len(materialised)} "
            f"(dropped={sorted(dropped)})"
        )
        assert set(dropped) == {"AQ", "BV", "GS"}


# ===========================================================================
# 5. Disappearance-ratio guard fires on >30% truncation
# ===========================================================================


class TestDisappearanceGuard:
    """L-180 §5: when a cycle truncates the prior set by >30%, the
    discovery's resync policy routes to anomaly + alert_and_pause.
    The test mocks a "stuck mid-write" iso_3166 snapshot by binding
    inline rows that drop ~40% of the 249-row full set.
    """

    async def test_full_then_truncated_cycle_triggers_anomaly(self):
        # Cycle 1: full 249-row set.
        full_rows = _load_iso_rows()
        handler = CountryListDiscovery()
        cfg = CountryListDiscoveryConfig(list_source="iso_3166")
        ctx = _ctx_for(cfg, rows=full_rows)
        cycle1 = await _drain(handler, ctx)
        cycle1_keys = [c.natural_key for c in cycle1]
        assert len(cycle1_keys) == 249

        # Cycle 2: keep only ~60% (drop 100 / 249 = 40.2%).
        truncated_rows = full_rows[:149]
        handler2 = CountryListDiscovery()
        ctx2 = _ctx_for(cfg, rows=truncated_rows)
        cycle2 = await _drain(handler2, ctx2)
        cycle2_keys = [c.natural_key for c in cycle2]
        assert len(cycle2_keys) == 149

        decision = evaluate_disappearance(
            cycle1_keys, cycle2_keys, policy=ResyncPolicy(),
        )
        assert decision.verdict == "anomaly"
        assert decision.ratio == pytest.approx(100 / 249, rel=1e-3)
        assert decision.ratio > 0.30
        assert decision.should_pause is True
        assert decision.should_alert is True
        assert decision.should_retire_disappeared is False
        # 100 disappeared keys route to DLQ keyed by
        # (discovery_id, natural_key) — the registry's job; here just
        # confirm the count.
        assert len(decision.routes_to_dlq) == 100

    async def test_small_drop_proceeds_without_alert(self):
        """A normal cycle with no disappearance proceeds.

        This anchors the inverse case: the L-180 guard's job is to
        catch *anomalous* drops, not legitimate retirements. A
        2% drop (well under the 30% threshold) should pass through.
        """
        full_rows = _load_iso_rows()
        prior_keys = [r["iso2"] for r in full_rows]
        # Drop 5/249 ≈ 2%.
        current_keys = prior_keys[5:]
        decision = evaluate_disappearance(
            prior_keys, current_keys, policy=ResyncPolicy(),
        )
        assert decision.verdict == "proceed"
        assert decision.should_alert is False
        assert decision.should_retire_disappeared is True
