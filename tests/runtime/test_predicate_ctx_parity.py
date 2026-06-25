# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G4 — kill the envelope-vs-row divergence class.

Two production representations of the SAME signal reach
``subscription.filter.matches()``:

  * **DB-row form** — fetched from the ``signals`` table (the batch /
    backfill read-slice and the coalescer's direct fetches). Pre-fix,
    asyncpg delivered ``payload`` as a JSON *str* here (only an agtype codec
    was registered), so ``severity_at_least()`` residuals silently failed on
    this path while passing on the reactive path.
  * **published-envelope form** — ``json.loads(Signal.model_dump_json())``
    off NATS (the reactive trigger path; ``triggers/engine._handle_msg``).

This module asserts the two forms produce IDENTICAL verdicts for EVERY
helper on the TARGET_SCOPE surface (generalizing the owner_tenant
regression fixed in 761be14), that the pool-level JSONB codec delivers
``payload`` as a dict on fetch, and that the new compile-time ctx-contract
gate refuses helpers no production ctx-builder can feed (org_match) while
every production descriptor predicate still compiles.

Runs against the real substrate (fresh migrated PG via the shared
``migrated_pg`` fixture); the contract/compile tests are pure-Python.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from legba.data.postgres import PostgresStore
from legba.data.predicates import (
    SURFACE_CTX_CONTRACTS,
    TARGET_SCOPE_APPLICABILITY_CTX,
    HELPER_NAMES,
    PredicateCompilationError,
    PredicateSurface,
    compile_predicate,
    helper_requirements,
    surface_helpers,
)
from legba.data.predicates.helpers import (
    SURFACE_ANALYST_SUBSCRIPTION,
    SURFACE_TARGET_SCOPE,
)
from legba.data.schemas.source import Subscription
from legba.data.sources._contract import Signal
from legba.runtime.source_actor import write_canonical_signal
from legba.runtime.subscription.filter import _signal_residual_ctx, matches

DESCRIPTORS_DIR = Path(__file__).resolve().parents[2] / "descriptors"

TENANT = "shared"
SOURCE_ID = "source.parity.test"

# G20 ISO-2 codes as the geopolitical discovery relabel emits them — the
# seeded per-country targets carry `geo_match(["<iso2>"])` subscriptions.
G20_ISO2 = [
    "AR", "AU", "BR", "CA", "CN", "DE", "FR", "GB", "ID", "IN",
    "IT", "JP", "KR", "MX", "RU", "SA", "TR", "US", "ZA",
]


# ---------------------------------------------------------------------------
# Helper parity cases — one predicate per TARGET_SCOPE helper.
#
# verdict_rich: expected verdict against the rich signal below (asserted, so
# the parity check is not vacuously False == False).
# org_match is compile-refused under the production contract (no ctx-builder
# feeds it); its "parity" is the fail-closed path on BOTH forms.
# ---------------------------------------------------------------------------

HELPER_CASES: dict[str, tuple[str, bool | None]] = {
    "mentions":          ('mentions("generator")', True),
    "mentions_any":      ('mentions_any(["generator", "nope"])', True),
    "geo_match":         ('geo_match(["BR"])', True),
    "geo_in":            ('geo_in(["BR", "AR"])', True),
    "has_tag":           ('has_tag("energy")', True),
    "has_any_tag":       ('has_any_tag(["energy", "nope"])', True),
    "severity_at_least": ('severity_at_least("high")', True),
    "recent":            ("recent(7)", True),
    "signal_age_hours":  ("signal_age_hours() < 1.0", True),
    "credibility":       ("credibility() >= 0.5", True),
    "entity_class_in":   ('entity_class_in(["generator"])', True),
    "contains_any":      ('contains_any(["substation"])', True),  # rich title="substation outage"
    "org_match":         ("org_match()", None),  # compile-refused → fail closed
}


def _rich_signal() -> Signal:
    return Signal(
        source_id=SOURCE_ID,
        owner_tenant=TENANT,
        modality="text",
        language="en",
        geo=["BR"],
        tags=["energy", "news"],
        entity_classes=["generator", "regulator"],
        source_credibility=0.9,
        fetched_at=datetime.now(tz=timezone.utc),
        payload={
            "severity": "critical",
            "classification_scores": {"severity": "critical"},
            "title": "substation outage",
        },
    )


def _sparse_signal() -> Signal:
    """No enrichment, no payload severity — most helpers verdict False."""
    return Signal(
        source_id=SOURCE_ID,
        owner_tenant=TENANT,
        modality="text",
        fetched_at=datetime.now(tz=timezone.utc),
        payload={"title": "bare"},
    )


def _envelope_form(sig: Signal) -> dict:
    """The reactive-path row: published JSON envelope, id-normalised exactly
    as ``triggers/engine._handle_msg`` does."""
    row = json.loads(sig.model_dump_json())
    if "id" not in row and "signal_id" in row:
        row["id"] = row["signal_id"]
    return row


def _subscription(predicate: str | None) -> Subscription:
    return Subscription(predicate=predicate, canonical_only=False)


# ---------------------------------------------------------------------------
# Live-substrate fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _signals():
    return {"rich": _rich_signal(), "sparse": _sparse_signal()}


@pytest.fixture
async def pg_store(migrated_pg):
    store = PostgresStore(migrated_pg)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def _insert_and_fetch(pg: PostgresStore, sig: Signal) -> dict:
    async with pg.acquire() as conn:
        await write_canonical_signal(
            conn, sig, source_version="0" * 16, owner_tenant=TENANT
        )
        row = await conn.fetchrow("SELECT * FROM signals WHERE id = $1", sig.signal_id)
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# 1. Pool-level JSONB codec
# ---------------------------------------------------------------------------


async def test_jsonb_codec_payload_is_dict_on_fetch(pg_store, _signals):
    """The codec delivers jsonb as dict on the production fetch path; both
    legacy (pre-serialized str param) and direct-dict writes round-trip."""
    row = await _insert_and_fetch(pg_store, _signals["rich"])
    assert isinstance(row["payload"], dict), (
        f"jsonb codec missing: payload fetched as {type(row['payload']).__name__}"
    )
    assert row["payload"]["severity"] == "critical"
    assert isinstance(row["raw_provenance"], dict)

    # Direct-dict param write (newly possible with the encoder).
    sig2 = _sparse_signal()
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO signals (id, source_id, owner_tenant, payload) "
            "VALUES ($1, $2, $3, $4::jsonb)",
            sig2.signal_id, SOURCE_ID, TENANT, {"severity": "low", "k": [1, 2]},
        )
        fetched = await conn.fetchrow(
            "SELECT payload FROM signals WHERE id = $1", sig2.signal_id
        )
    assert fetched["payload"] == {"severity": "low", "k": [1, 2]}


# ---------------------------------------------------------------------------
# 2. Row-vs-envelope parity for EVERY TARGET_SCOPE helper
# ---------------------------------------------------------------------------


def test_helper_case_coverage_is_complete():
    """Every helper available on TARGET_SCOPE has a parity case. A new
    signal-scoped helper without a case fails THIS test, so the parity net
    can never silently lag the catalog."""
    assert set(HELPER_CASES) == surface_helpers(SURFACE_TARGET_SCOPE)


async def test_row_vs_envelope_parity_every_target_scope_helper(pg_store, _signals):
    """THE generalized 761be14 regression: for every TARGET_SCOPE helper, the
    same signal in DB-row form and published-envelope form must produce the
    SAME verdict through subscription.filter.matches()."""
    for kind, sig in _signals.items():
        db_row = await _insert_and_fetch(pg_store, sig)
        env_row = _envelope_form(sig)
        # Codec-less legacy row (payload as JSON str) — the exact G4 shape;
        # the residual-ctx coercion must keep it in agreement too.
        legacy_row = dict(db_row)
        legacy_row["payload"] = json.dumps(legacy_row["payload"])

        for name, (src, verdict_rich) in HELPER_CASES.items():
            if verdict_rich is None:
                continue  # compile-refused helpers asserted separately below
            sub = _subscription(src)
            got_db = matches(sub, db_row, source_id=SOURCE_ID, owner_tenant=TENANT)
            got_env = matches(sub, env_row, source_id=SOURCE_ID, owner_tenant=TENANT)
            got_legacy = matches(
                sub, legacy_row, source_id=SOURCE_ID, owner_tenant=TENANT
            )
            assert got_db == got_env == got_legacy, (
                f"helper {name!r} diverged on {kind} signal: "
                f"db={got_db} envelope={got_env} legacy_str_payload={got_legacy}"
            )
            if kind == "rich":
                assert got_db is verdict_rich, (
                    f"helper {name!r}: expected {verdict_rich} on the rich "
                    f"signal, got {got_db} — parity would be vacuous"
                )


async def test_severity_residual_parity_is_not_vacuous(pg_store, _signals):
    """The original G4 symptom, asserted directly: a severity_at_least()
    residual must PASS on the DB-row form (str-payload pre-fix made it fail
    silently while the envelope path passed)."""
    db_row = await _insert_and_fetch(pg_store, _signals["rich"])
    assert isinstance(db_row["payload"], dict)
    sub = _subscription('severity_at_least("high")')
    assert matches(sub, db_row, source_id=SOURCE_ID, owner_tenant=TENANT) is True


def test_org_match_fails_closed_identically_on_both_forms(_signals):
    """org_match is compile-refused under the production contract. A
    pre-gate registered predicate (bypassing schema validation) must fail
    CLOSED — identically — on both forms, never raise out of matches()."""
    with pytest.raises(PredicateCompilationError, match="org_match"):
        compile_predicate("org_match()", PredicateSurface.TARGET_SCOPE)
    # Registration now refuses it too:
    with pytest.raises(ValueError, match="org_match"):
        Subscription(predicate="org_match()")
    # Runtime fail-closed (model_construct skips the validator — simulating
    # a predicate registered before this gate existed):
    sub = Subscription.model_construct(
        geo=[], languages=[], tags=[], entity_classes=[], modalities=[],
        predicate="org_match()", canonical_only=False,
    )
    sig = _signals["rich"]
    env_row = _envelope_form(sig)
    assert matches(sub, env_row, source_id=SOURCE_ID, owner_tenant=TENANT) is False


# ---------------------------------------------------------------------------
# 3. Compile-time ctx-contract gate — production predicates stay green
# ---------------------------------------------------------------------------


def _iter_descriptor_predicates():
    """Yield (file, surface, source) for every predicate in descriptors/.

    Surfaces per descriptor shape:
      * scope.predicate                          → target.scope
      * sources[*].subscription.predicate        → target.scope
      * sources[*].source_selector.predicate     → analyst.subscription
      * subscription.targets.predicate           → analyst.subscription
      * applicability_predicate                  → target.scope (applicability ctx)
      * cadence.trigger                          → cadence.trigger
    (Discovery relabel predicates bind bare label identifiers, not helpers —
    they are compiled by the relabel engine with its own fallback and are
    exercised by the discovery tests.)
    """
    for path in sorted(DESCRIPTORS_DIR.glob("*.yaml")):
        body = yaml.safe_load(path.read_text()) or {}
        scope_pred = (body.get("scope") or {}).get("predicate")
        if scope_pred:
            yield path.name, PredicateSurface.TARGET_SCOPE, None, scope_pred
        for sref in body.get("sources") or []:
            if not isinstance(sref, dict):
                continue
            sub_pred = (sref.get("subscription") or {}).get("predicate")
            if sub_pred:
                yield path.name, PredicateSurface.TARGET_SCOPE, None, sub_pred
            sel_pred = (sref.get("source_selector") or {}).get("predicate")
            if sel_pred:
                yield path.name, PredicateSurface.ANALYST_SUBSCRIPTION, None, sel_pred
        targets_pred = (
            ((body.get("subscription") or {}).get("targets") or {}).get("predicate")
            if isinstance(body.get("subscription"), dict)
            else None
        )
        if targets_pred:
            yield path.name, PredicateSurface.ANALYST_SUBSCRIPTION, None, targets_pred
        app_pred = body.get("applicability_predicate")
        if app_pred:
            yield (
                path.name,
                PredicateSurface.TARGET_SCOPE,
                TARGET_SCOPE_APPLICABILITY_CTX,
                app_pred,
            )
        trigger = (body.get("cadence") or {}).get("trigger")
        if isinstance(trigger, str) and trigger:
            yield path.name, PredicateSurface.CADENCE_TRIGGER, None, trigger


def test_every_production_descriptor_predicate_compiles():
    seen = 0
    for fname, surface, contract, src in _iter_descriptor_predicates():
        try:
            if contract is not None:
                compile_predicate(src, surface, ctx_contract=contract)
            else:
                compile_predicate(src, surface)
        except PredicateCompilationError as exc:  # pragma: no cover - failure path
            pytest.fail(
                f"{fname}: production predicate {src!r} no longer compiles "
                f"on {surface.value}: {exc}"
            )
        seen += 1
    # Guard against the walker silently matching nothing (e.g. a renamed
    # field) — the catalog ships at least the G20 + assessor + inline preds.
    assert seen >= 4, f"descriptor predicate walk found only {seen} predicates"


def test_seeded_g20_subscription_predicates_compile():
    """The materialized per-country targets carry geo_match(["<iso2>"]) —
    every one must compile on TARGET_SCOPE under the production contract."""
    for code in G20_ISO2:
        compile_predicate(f'geo_match(["{code}"])', PredicateSurface.TARGET_SCOPE)


def test_known_production_predicates_compile_on_their_surfaces():
    # target_country_g20.yaml subscription residual
    compile_predicate('geo_match(["BR"])', PredicateSurface.TARGET_SCOPE)
    # analyst_country_assessor.yaml subscription.targets selector
    compile_predicate('has_tag("g20")', PredicateSurface.ANALYST_SUBSCRIPTION)
    # brazil predictor / inline analysts
    compile_predicate(
        'target_id() == "india_energy_infra"', PredicateSurface.ANALYST_SUBSCRIPTION
    )
    # GeoScope-style scope predicate (schema test fixture shape)
    compile_predicate(
        'mentions("generator") and geo_match()', PredicateSurface.TARGET_SCOPE
    )
    # subscription-engine test residuals
    compile_predicate("mentions('organization')", PredicateSurface.TARGET_SCOPE)


def test_unfed_surfaces_refuse_helpers_loudly():
    # No production cadence-trigger evaluator → helper triggers refused.
    with pytest.raises(PredicateCompilationError, match="cannot be fed"):
        compile_predicate('event_type() == "x"', PredicateSurface.CADENCE_TRIGGER)
    # source.filter feeds bare relabel labels, no helper ctx → refused...
    with pytest.raises(PredicateCompilationError, match="cannot be fed"):
        compile_predicate('mentions("generator")', PredicateSurface.SOURCE_FILTER)
    # ...but relabel-style bare-identifier predicates still compile.
    compile_predicate(
        "country_region == 'Antarctica'", PredicateSurface.SOURCE_FILTER
    )


# ---------------------------------------------------------------------------
# 4. Contract ↔ builder sync (the declarations stay honest)
# ---------------------------------------------------------------------------


def test_signal_residual_ctx_matches_declared_contract():
    declared = {
        k for k in SURFACE_CTX_CONTRACTS[SURFACE_TARGET_SCOPE]
        if k.startswith("signal.")
    }
    provided = {f"signal.{k}" for k in _signal_residual_ctx({})["signal"]}
    assert provided == declared


def test_applicability_ctx_is_target_half_of_scope_contract():
    target_half = {
        k for k in SURFACE_CTX_CONTRACTS[SURFACE_TARGET_SCOPE]
        if k.startswith("target.")
    }
    assert TARGET_SCOPE_APPLICABILITY_CTX == frozenset(target_half)


def test_analyst_subscription_contract_satisfies_its_helpers():
    """Every helper permitted on analyst.subscription must be satisfiable by
    the production builders' contract — they all run today (has_tag("g20"),
    target_id(), ...), so an unsatisfiable one means the contract regressed."""
    contract = SURFACE_CTX_CONTRACTS[SURFACE_ANALYST_SUBSCRIPTION]
    from legba.data.predicates import helper_unsatisfied

    for name in surface_helpers(SURFACE_ANALYST_SUBSCRIPTION):
        assert helper_unsatisfied(name, contract) == (), (
            f"helper {name!r} became unsatisfiable on analyst.subscription"
        )


def test_every_catalog_helper_declares_requirements():
    for name in HELPER_NAMES:
        assert helper_requirements(name), (
            f"helper {name!r} has no required-ctx declaration — the G4 gate "
            f"cannot validate it"
        )
