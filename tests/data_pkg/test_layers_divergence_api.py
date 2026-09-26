# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Program 6 L2 divergence-map route (7b-v).

Covers :mod:`legba.data.registry.layers_api`:

  * ``GET /api/v1/v3/layers/divergence`` -> ``LayerDivergenceOut``

Two layers, the house v3-route pattern:

  * PURE tests (no DB): route registration + no collision with the v3 telemetry
    router, the registry-slim import guard (the ``test_belief_api_imports``
    subprocess mechanism), the mirrored-constant DRIFT GUARDS against the
    handler that actually writes the receipt, and the pure projection.
  * INTEGRATION tests over the ephemeral ``migrated_pg`` database + real HTTP
    (the ``test_v3_since_api`` fixture shape): a fixture receipt shaped from the
    LIVE one (run ``55543970`` of 2026-09-24, AR/CN/IL/IR/RU), a fixture fired
    finding, the "no run yet" state, and the honest ordering.

The fixture receipt is a trimmed copy of a real ``analyst_traces.output_payload``
— the same keys, the same aperture reason prose, a 4-day window instead of 28 —
so the shape under test is the shape the handler writes, not one invented here.

Auth: tests run in dev-mode (``LEGBA_DEV_MODE=1`` from tests/conftest.py, no
``LEGBA_REGISTRY_API_TOKEN``), so ``require_bearer`` returns ``"anonymous"``.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

import legba.data.registry.layers_api as layers_api
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.layers_api import (
    FiredOut,
    build_layers_router,
    daily_series,
    desk_from_receipt,
    fired_by_desk,
    project_receipt,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)

_ROUTE = "/api/v1/v3/layers/divergence"


def _fixed_identity() -> SigningIdentity:
    seed = b"layers-divergence-api-test-seed!"
    assert len(seed) == 32
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:layers-divergence-test",
    )


# ---------------------------------------------------------------------------
# The fixture receipt — shaped from the live one (run 55543970, 2026-09-24)
# ---------------------------------------------------------------------------

_DAYS = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]

#: The AR aperture's real curated prose. Verbatim so the "an excluded layer
#: shows WHY" contract is tested against a reason an operator actually wrote.
_AR_OFFICIAL_REASON = (
    "curated: absent from our sources — no Argentine government or state-media "
    "feed is registered; Argentina has an official layer in the world, we do "
    "not ingest it"
)
_AR_SOCIAL_REASON = (
    "curated: absent from our sources — the Telegram list carries no Argentine "
    "channels and Discord is unconfigured; a regime–public gap cannot be "
    "measured here, which is one reason AR is the control"
)


def _daily(counts: list[int], *, folded: int = 0) -> dict[str, Any]:
    """``{day: {raw, kept, folded}}`` for the fixture window."""
    return {
        day: {"raw": kept + folded, "kept": kept, "folded": folded}
        for day, kept in zip(_DAYS, counts)
    }


def _layer(
    declared: str,
    counts: list[int],
    *,
    reason: str = "",
    sources_mapped: int = 0,
    thin_days: int = 0,
    folded: int = 0,
) -> dict[str, Any]:
    return {
        "declared": declared,
        "reason": reason,
        "sources_mapped": sources_mapped,
        "daily": _daily(counts, folded=folded),
        "thin_days": thin_days,
    }


def _series_point(day: str, a: int, b: int, z: float | None) -> dict[str, Any]:
    return {
        "day": day, "a": a, "b": b, "blank": a == 0 and b == 0,
        "log_ratio": -0.3626, "z": z,
        "baseline": {
            "n": 14, "centre": -0.4449, "mad": 0.7585, "scale": 1.1245,
            "scale_floored": False,
        },
    }


def _receipt_payload() -> dict[str, Any]:
    """Two desks: AR (a pair excluded by the aperture) and RU (one pair fired
    shape, one below threshold)."""
    ar = {
        "target_id": "country_g20_ar",
        "country": "AR",
        "map_version": "layer_map_ar.v1",
        "sources_mapped": 49,
        "rows_scanned": 227,
        "rows_truncated": False,
        "aperture": {
            "present": ["domestic_press", "foreign_press", "physical",
                        "public_data"],
            "absent": [
                {"layer": "official", "reason": _AR_OFFICIAL_REASON},
                {"layer": "social_digest", "reason": _AR_SOCIAL_REASON},
            ],
            "unmeasured": [],
            "undeclared": [],
        },
        "counts_suppressed_by_aperture": {"social_digest": 9},
        "layers": {
            "official": _layer("absent", [0, 0, 0, 0],
                               reason=_AR_OFFICIAL_REASON, thin_days=4),
            "domestic_press": _layer("present", [7, 9, 11, 8],
                                     sources_mapped=21, folded=1),
            "foreign_press": _layer("present", [14, 17, 12, 19],
                                    sources_mapped=22, folded=3),
            "social_digest": _layer("absent", [0, 0, 0, 0],
                                    reason=_AR_SOCIAL_REASON, thin_days=4),
            "public_data": _layer("present", [5, 6, 4, 5], sources_mapped=5,
                                  thin_days=1),
            "physical": _layer("present", [0, 1, 0, 0], sources_mapped=1,
                               thin_days=4),
        },
        "pairs": [
            {
                "pair_id": "regime_public_gap",
                "layer_a": "official", "layer_b": "social_digest",
                "evaluable": False,
                "no_fire_reason": "aperture_excluded",
                "excluded_layers": [
                    {"layer": "official", "state": "absent",
                     "reason": _AR_OFFICIAL_REASON},
                    {"layer": "social_digest", "state": "absent",
                     "reason": _AR_SOCIAL_REASON},
                ],
            },
            {
                "pair_id": "narrative_control",
                "layer_a": "domestic_press", "layer_b": "foreign_press",
                "evaluable": True,
                "no_fire_reason": "below_threshold",
                "series": [
                    _series_point(_DAYS[0], 7, 14, None),
                    _series_point(_DAYS[1], 9, 17, 0.31),
                    _series_point(_DAYS[2], 11, 12, 0.98),
                    _series_point(_DAYS[3], 8, 19, 0.0733),
                ],
            },
            {
                "pair_id": "credibility_gap",
                "layer_a": "official", "layer_b": "public_data",
                "evaluable": False,
                "no_fire_reason": "aperture_excluded",
                "excluded_layers": [
                    {"layer": "official", "state": "absent",
                     "reason": _AR_OFFICIAL_REASON},
                ],
            },
        ],
        "divergence_count": 0,
    }
    ru = {
        "target_id": "country_g20_ru",
        "country": "RU",
        "map_version": "layer_map_ru.v1",
        "sources_mapped": 63,
        "rows_scanned": 1902,
        "rows_truncated": False,
        "aperture": {
            "present": ["official", "domestic_press", "foreign_press",
                        "social_digest", "public_data"],
            "absent": [],
            "unmeasured": [
                {"layer": "physical", "reason": "nobody has looked yet"},
            ],
            "undeclared": [],
        },
        "counts_suppressed_by_aperture": {},
        "layers": {
            "official": _layer("present", [12, 14, 3, 2], sources_mapped=9,
                               thin_days=2),
            "domestic_press": _layer("present", [30, 28, 33, 31],
                                     sources_mapped=24, folded=4),
            "foreign_press": _layer("present", [41, 39, 44, 40],
                                    sources_mapped=26, folded=7),
            "social_digest": _layer("present", [22, 25, 27, 24],
                                    sources_mapped=3),
            "public_data": _layer("present", [6, 7, 5, 6], sources_mapped=1),
            "physical": _layer("unmeasured", [0, 0, 0, 0],
                               reason="nobody has looked yet"),
        },
        "pairs": [
            {
                "pair_id": "regime_public_gap",
                "layer_a": "official", "layer_b": "social_digest",
                "evaluable": True,
                "no_fire_reason": "",
                "series": [
                    _series_point(_DAYS[0], 12, 22, None),
                    _series_point(_DAYS[1], 14, 25, -0.12),
                    _series_point(_DAYS[2], 3, 27, -2.41),
                    _series_point(_DAYS[3], 2, 24, -2.77),
                ],
            },
            {
                "pair_id": "narrative_control",
                "layer_a": "domestic_press", "layer_b": "foreign_press",
                "evaluable": True,
                "no_fire_reason": "below_threshold",
                "series": [
                    _series_point(_DAYS[0], 30, 41, None),
                    _series_point(_DAYS[1], 28, 39, 0.02),
                    _series_point(_DAYS[2], 33, 44, -0.11),
                    _series_point(_DAYS[3], 31, 40, 0.05),
                ],
            },
            {
                "pair_id": "credibility_gap",
                "layer_a": "official", "layer_b": "public_data",
                "evaluable": True,
                "no_fire_reason": "baseline_thin",
                "series": [
                    _series_point(_DAYS[0], 12, 6, None),
                    _series_point(_DAYS[1], 14, 7, None),
                    _series_point(_DAYS[2], 3, 5, None),
                    _series_point(_DAYS[3], 2, 6, None),
                ],
            },
        ],
        "divergence_count": 1,
    }
    return {
        "title": "Layer divergence: 1 gap change across 2 desk(s)",
        "body": "- [country_g20_ru/RU] regime_public_gap widening: …",
        "tags": ["deterministic", "layer_divergence", "severity:moderate"],
        "evidence": [],
        "confidence": 0.1,
        "kind_marker": "finding",
        "data": {
            "sub_handler": "layer_divergence",
            "method_version": "layer_divergence/2026-09.1",
            "payload_schema": "layer_divergence.v1",
            "as_of": _DAYS[-1],
            "window_days": 4,
            "baseline_days": 14,
            "z_threshold": 2.0,
            "mad_floor": 0.2,
            "consecutive_days": 2,
            "thin_min_per_day": 5,
            "severity": "moderate",
            "divergence_count": 1,
            "widening_count": 1,
            "narrowing_count": 0,
            "thin_count": 1,
            "divergences": [_divergence()],
            "targets_evaluated": 2,
            "targets": [ru, ar],
            "targets_unresolved": [
                {"target_id": "country_watch_zz", "reason": "no loaded map"},
            ],
            "classification_audit": (
                "unaudited: no sampled layer-classification audit has been run "
                "over this map (SEAMS #60). A source filed under the wrong "
                "layer is a DIRECTIONAL error in every number below, not noise."
            ),
            "warnings": [],
            "citations": [],
            "structural_claims": [],
        },
    }


def _divergence() -> dict[str, Any]:
    return {
        "pair_id": "regime_public_gap",
        "meaning": "the regime-public gap — …",
        "layer_a": "official", "layer_b": "social_digest",
        "day": _DAYS[-1],
        "direction": "widening",
        "louder_layer": "social_digest",
        "quieter_layer": "official",
        "counts": {"official": 2, "social_digest": 24},
        "log_ratio": -3.4594,
        "baseline": {"n": 14, "centre": -0.72, "mad": 0.66, "scale": 0.98,
                     "scale_floored": False},
        "z": -2.77,
        "z_prior_days": [-2.41],
        "consecutive_days": 2,
        "severity": "moderate",
        "target_id": "country_g20_ru",
        "country": "RU",
        "map_version": "layer_map_ru.v1",
        "thin": True,
        "thin_layers": ["official"],
        "thin_min_per_day": 5,
        "confidence": 0.1,
        "citations": [],
    }


# ---------------------------------------------------------------------------
# Pure tests — registration, slimness, drift guards, projection (no DB)
# ---------------------------------------------------------------------------


def test_layers_route_registered_and_does_not_collide() -> None:
    """The one route registers and shadows nothing on the v3 telemetry router."""
    router = build_layers_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert paths == {"/layers/divergence"}

    from legba.data.registry.v3_api import build_v3_router

    v3_paths = {
        r.path
        for r in build_v3_router(deps=object()).routes  # type: ignore[arg-type]
    }
    assert not (paths & v3_paths)


def test_layers_route_module_has_no_runtime_imports() -> None:
    """The text check: no analyst / runtime import LINE in this module."""
    with open(layers_api.__file__, "r", encoding="utf-8") as fh:
        text = fh.read()
    import_lines = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip().startswith(("import ", "from "))
    )
    assert "analysts" not in import_lines
    assert "deterministic" not in import_lines
    assert "legba.runtime" not in import_lines and "..runtime" not in import_lines


def test_layers_route_imports_without_the_runtime_stack() -> None:
    """The SLIM-IMAGE guard (the ``test_belief_api_imports`` mechanism).

    A deferred import moves WHEN the graph is walked, never how far, so the text
    check above cannot see a transitive reach. Subprocess, heavy third-party
    modules poisoned to ``None``, assert nothing under ``legba.data.analysts`` /
    ``legba.runtime`` was pulled in.
    """
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.layers_api as api

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime package reachable: %r" % leaked
        assert api.build_layers_router is not None
        assert api.ANALYST_ID == "layer_divergence"
        assert len(api.PAIRS_OF_INTEREST) == 3
        print("OK")
        """
    )
    src_root = str(pathlib.Path(layers_api.__file__).resolve().parents[4])
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        # Keep the parent's PYTHONPATH tail: in the test container the deps
        # live outside src/, reachable ONLY through it (2026-09-22 fix).
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            pp for pp in (src_root, os.environ.get("PYTHONPATH", "")) if pp)},
    )
    assert result.returncode == 0, (
        f"slim import failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_drift_guard_pairs_of_interest() -> None:
    """The mirrored pair table MUST stay equal to the handler's — id, both
    layers and the MEANING prose, which the panel renders verbatim."""
    from legba.data.analysts.deterministic_handlers import layer_divergence

    mirrored = [
        (p, a, b, m) for (p, a, b, m) in layers_api.PAIRS_OF_INTEREST
    ]
    truth = [
        (p.pair_id, p.a, p.b, p.meaning)
        for p in layer_divergence.PAIRS_OF_INTEREST
    ]
    assert mirrored == truth


def test_drift_guard_analyst_id() -> None:
    """The route reads the analyst the handler actually registers as."""
    from legba.data.analysts.deterministic_handlers import layer_divergence

    assert layers_api.ANALYST_ID == layer_divergence.SUB_HANDLER_NAME


def test_daily_series_orders_by_day_and_invents_nothing() -> None:
    """ISO keys sort chronologically; a malformed cell is dropped, never
    replaced with zeroes."""
    rows = daily_series({
        "2026-09-23": {"raw": 5, "kept": 4, "folded": 1},
        "2026-09-21": {"raw": 2, "kept": 2, "folded": 0},
        "2026-09-22": "not-a-cell",
    })
    assert [r.day for r in rows] == ["2026-09-21", "2026-09-23"]
    assert (rows[1].raw, rows[1].kept, rows[1].folded) == (5, 4, 1)
    assert daily_series(None) == []


def test_desk_projection_is_a_pass_through() -> None:
    """Layers come back in vocabulary order; pairs come back VERBATIM; the
    aperture reason survives; ``thin_days`` is null on an excluded layer."""
    payload = _receipt_payload()
    ar = [t for t in payload["data"]["targets"] if t["country"] == "AR"][0]
    desk = desk_from_receipt(ar, {})

    assert list(desk.layers.keys()) == [
        "official", "domestic_press", "foreign_press", "social_digest",
        "public_data", "physical",
    ]
    assert desk.layers["official"].declared == "absent"
    assert desk.layers["official"].reason == _AR_OFFICIAL_REASON
    # An excluded layer was never counted, so a thin-day tally on it would be a
    # statement about days nobody read.
    assert desk.layers["official"].thin_days is None
    assert desk.layers["public_data"].thin_days == 1
    assert [d.day for d in desk.layers["domestic_press"].daily] == _DAYS
    assert desk.layers["domestic_press"].daily[0].kept == 7
    assert desk.layers["domestic_press"].daily[0].folded == 1

    # Pairs verbatim — including the series and the excluded_layers reasons.
    assert desk.pairs == ar["pairs"]
    assert desk.counts_suppressed_by_aperture == {"social_digest": 9}
    assert desk.fired is None


def test_project_receipt_shape_and_ordering() -> None:
    """The envelope: the method/payload stamps, the SEAMS #60 note, the knobs,
    the unresolved desks, and desks ordered by country."""
    now = datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc)
    out = project_receipt(
        _receipt_payload(),
        run_id=UUID("55543970-6c2d-4974-8864-577238ebb59f"),
        run_started_at=now - timedelta(minutes=10),
        fired={},
        now=now,
    )
    assert out.measured is True
    assert out.as_of == "2026-09-24"
    assert out.method_version == "layer_divergence/2026-09.1"
    assert out.payload_schema == "layer_divergence.v1"
    assert out.classification_audit is not None
    assert "SEAMS #60" in out.classification_audit
    assert (out.window_days, out.baseline_days, out.consecutive_days) == (4, 14, 2)
    assert (out.z_threshold, out.mad_floor, out.thin_min_per_day) == (2.0, 0.2, 5)
    # Receipt order was RU, AR; the wire is ordered by country so the map reads
    # the same way twice.
    assert [d.country for d in out.desks] == ["AR", "RU"]
    assert out.desks_unresolved == [
        {"target_id": "country_watch_zz", "reason": "no loaded map"},
    ]
    assert [p.pair_id for p in out.pairs_declared] == [
        "regime_public_gap", "narrative_control", "credibility_gap",
    ]
    assert out.layer_vocab[0] == "official"


def test_project_receipt_survives_a_payload_it_cannot_read() -> None:
    """A run with no parseable receipt is an empty MAP, not a 500 and not a
    fabricated as_of."""
    out = project_receipt(
        "{not json", run_id="r1", run_started_at=None, fired={},
    )
    assert out.measured is True
    assert out.as_of is None
    assert out.desks == []
    assert out.receipt_run_id == "r1"


def test_fired_by_desk_parses_the_text_columns() -> None:
    """``z`` / ``thin`` arrive as the JSON text they were stored as; an
    unparseable z is null, never 0.0."""
    ts = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = [
        {"target_id": "country_g20_ru", "finding_id": "f1", "produced_at": ts,
         "pair_id": "regime_public_gap", "direction": "widening",
         "severity": "moderate", "day": "2026-09-24", "z": "-2.77",
         "thin": "true"},
        {"target_id": "country_g20_il", "finding_id": "f2", "produced_at": ts,
         "pair_id": "narrative_control", "direction": "narrowing",
         "severity": "elevated", "day": "2026-09-24", "z": "nope",
         "thin": None},
        {"target_id": "", "finding_id": "f3", "pair_id": "x"},
    ]
    out = fired_by_desk(rows)
    assert set(out) == {"country_g20_ru", "country_g20_il"}
    assert out["country_g20_ru"].z == pytest.approx(-2.77)
    assert out["country_g20_ru"].thin is True
    assert out["country_g20_il"].z is None
    assert out["country_g20_il"].thin is None


def test_desk_carries_its_fire() -> None:
    payload = _receipt_payload()
    ru = [t for t in payload["data"]["targets"] if t["country"] == "RU"][0]
    fired = {
        "country_g20_ru": FiredOut(
            finding_id="f1", pair_id="regime_public_gap", direction="widening",
            severity="moderate", day=_DAYS[-1], z=-2.77, thin=True,
        ),
    }
    desk = desk_from_receipt(ru, fired)
    assert desk.fired is not None
    assert desk.fired.pair_id == "regime_public_gap"
    assert desk.fired.direction == "widening"


# ---------------------------------------------------------------------------
# App fixture (ephemeral migrated DB + real HTTP — the since_api shape)
# ---------------------------------------------------------------------------


#: The scratch analyst id one case uses to prove the route reads ITS analyst and
#: not merely the newest trace. Deliberately not a real one: a far-future
#: ``run_started_at`` under a live id would poison every later test that reads
#: that analyst's newest trace on this session-scoped database.
_OTHER_ANALYST = "zz_not_layer_divergence"


@pytest_asyncio.fixture
async def layers_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    # `migrated_pg` is SESSION-scoped and shared (see conftest's `clean_tables`
    # banner), and `analyst_outputs` / `analyst_traces` are two of the busiest
    # tables in the suite — so this file never truncates them. It deletes only
    # the rows carrying its own analyst ids, which no other file writes, which
    # is both the narrow shape that banner asks for and what makes these cases
    # independent of each other's leftovers.
    async with pg_store.acquire() as conn:
        for analyst_id in (layers_api.ANALYST_ID, _OTHER_ANALYST):
            await conn.execute(
                "DELETE FROM analyst_outputs WHERE analyst_id = $1", analyst_id,
            )
            await conn.execute(
                "DELETE FROM analyst_traces WHERE analyst_id = $1", analyst_id,
            )

    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)

    descriptor_registry = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()

    stack_registry = StackRegistry(pg_store, vault, audit=audit, dlq=dlq)

    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=stack_registry,
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=nats_store,
        conversion_registry=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_layers_router(deps), prefix="/api/v1/v3")

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(layers_app):
    app, _, _ = layers_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


async def _insert_trace(
    pg_store: PostgresStore,
    *,
    payload: dict[str, Any],
    run_started_at: datetime,
    analyst_id: str = "layer_divergence",
) -> UUID:
    run_id = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_traces (
                run_id, analyst_id, analyst_version, cadence_trigger,
                output_payload, status, run_started_at, run_ended_at,
                receipt_hash
            ) VALUES (
                $1, $2, 'v-test', 'cadence', $3::jsonb, 'success', $4, $4, $5
            )
            """,
            run_id, analyst_id, json.dumps(payload), run_started_at,
            f"sha256:{run_id.hex}",
        )
    return run_id


async def _insert_summary_finding(
    pg_store: PostgresStore,
    *,
    divergences: list[dict[str, Any]],
    produced_at: datetime,
) -> UUID:
    """The per-run summary finding — a META analyst writes no target_id, so the
    desk lives inside ``data.divergences[]``."""
    row_id = uuid4()
    data = {"sub_handler": "layer_divergence", "divergences": divergences}
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, analyst_id, produced_at, derived_from, schema_uri
            ) VALUES (
                $1, 'finding', 'Layer divergence', '', 0.1, 'moderate',
                $2::jsonb, '', 'layer_divergence', $3, '{}',
                'iglu:legba/finding/jsonschema/1-0-0'
            )
            """,
            row_id, json.dumps(data), produced_at,
        )
    return row_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_run_yet_is_not_an_empty_measurement(client) -> None:
    """Nothing in analyst_traces: measured, no receipt, no desks — and the
    declared pairs + vocabulary still ship so the panel can draw its frame."""
    res = await client.get(_ROUTE)
    assert res.status_code == 200
    body = res.json()
    assert body["measured"] is True
    assert body["receipt_run_id"] is None
    assert body["desks"] == []
    assert body["as_of"] is None
    assert len(body["pairs_declared"]) == 3
    assert len(body["layer_vocab"]) == 6
    assert body["unit_sentence"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_newest_receipt_wins_and_carries_every_desk(
    client, layers_app,
) -> None:
    _, _, pg_store = layers_app
    now = datetime.now(timezone.utc)

    stale = _receipt_payload()
    stale["data"]["as_of"] = "2026-09-23"
    stale["data"]["targets"] = stale["data"]["targets"][:1]
    await _insert_trace(pg_store, payload=stale,
                        run_started_at=now - timedelta(days=1))
    fresh_run = await _insert_trace(
        pg_store, payload=_receipt_payload(), run_started_at=now,
    )
    # A different analyst's trace must not be read as this one's.
    await _insert_trace(
        pg_store, payload={"data": {"as_of": "2999-01-01", "targets": []}},
        run_started_at=now + timedelta(hours=1), analyst_id=_OTHER_ANALYST,
    )

    res = await client.get(_ROUTE)
    assert res.status_code == 200
    body = res.json()
    assert body["receipt_run_id"] == str(fresh_run)
    assert body["as_of"] == "2026-09-24"
    assert [d["country"] for d in body["desks"]] == ["AR", "RU"]

    ar = body["desks"][0]
    # The excluded layer says WHY, in the operator's own words.
    assert ar["layers"]["official"]["declared"] == "absent"
    assert ar["layers"]["official"]["reason"] == _AR_OFFICIAL_REASON
    # The no_fire_reason is named, not summarised.
    by_pair = {p["pair_id"]: p for p in ar["pairs"]}
    assert by_pair["regime_public_gap"]["no_fire_reason"] == "aperture_excluded"
    assert by_pair["regime_public_gap"]["excluded_layers"][0]["state"] == "absent"
    assert by_pair["narrative_control"]["no_fire_reason"] == "below_threshold"
    assert len(by_pair["narrative_control"]["series"]) == 4
    assert by_pair["narrative_control"]["series"][-1]["z"] == pytest.approx(0.0733)
    # The map/declaration disagreement is reported, not silently applied.
    assert ar["counts_suppressed_by_aperture"] == {"social_digest": 9}
    # No fired finding exists yet.
    assert ar["fired"] is None and body["desks"][1]["fired"] is None

    ru = body["desks"][1]
    assert ru["layers"]["physical"]["declared"] == "unmeasured"
    assert [d["day"] for d in ru["layers"]["official"]["daily"]] == _DAYS
    assert [d["kept"] for d in ru["layers"]["official"]["daily"]] == [12, 14, 3, 2]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fired_finding_attaches_to_its_desk_newest_first(
    client, layers_app,
) -> None:
    """Each desk gets the NEWEST divergence naming it, out of the summary
    findings — the rows carry no target_id of their own."""
    _, _, pg_store = layers_app
    now = datetime.now(timezone.utc)
    await _insert_trace(pg_store, payload=_receipt_payload(), run_started_at=now)

    old = _divergence()
    old["pair_id"] = "narrative_control"
    old["direction"] = "narrowing"
    await _insert_summary_finding(
        pg_store, divergences=[old], produced_at=now - timedelta(days=2),
    )
    newest_id = await _insert_summary_finding(
        pg_store, divergences=[_divergence()], produced_at=now - timedelta(hours=2),
    )

    res = await client.get(_ROUTE)
    body = res.json()
    desks = {d["target_id"]: d for d in body["desks"]}
    fired = desks["country_g20_ru"]["fired"]
    assert fired is not None
    assert fired["finding_id"] == str(newest_id)
    assert fired["pair_id"] == "regime_public_gap"
    assert fired["direction"] == "widening"
    assert fired["z"] == pytest.approx(-2.77)
    assert fired["thin"] is True
    # A desk with no divergence of its own keeps a null fire — never the other
    # desk's, and never a zero.
    assert desks["country_g20_ar"]["fired"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fired_days_window_bounds_the_fire_read(client, layers_app) -> None:
    """``fired_days`` bounds the fire read and leaves the receipt alone."""
    _, _, pg_store = layers_app
    now = datetime.now(timezone.utc)
    await _insert_trace(pg_store, payload=_receipt_payload(), run_started_at=now)
    await _insert_summary_finding(
        pg_store, divergences=[_divergence()], produced_at=now - timedelta(days=20),
    )

    wide = (await client.get(_ROUTE, params={"fired_days": 30})).json()
    narrow = (await client.get(_ROUTE, params={"fired_days": 7})).json()

    assert {d["target_id"]: d["fired"] for d in wide["desks"]}[
        "country_g20_ru"
    ] is not None
    assert {d["target_id"]: d["fired"] for d in narrow["desks"]}[
        "country_g20_ru"
    ] is None
    # The receipt is the newest one either way.
    assert wide["receipt_run_id"] == narrow["receipt_run_id"]
    assert (await client.get(_ROUTE, params={"fired_days": 0})).status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_divergences_do_not_take_the_route_down(
    client, layers_app,
) -> None:
    """A summary row whose ``divergences`` is not an array is skipped by the
    SQL guard rather than raising out of ``jsonb_array_elements``."""
    _, _, pg_store = layers_app
    now = datetime.now(timezone.utc)
    await _insert_trace(pg_store, payload=_receipt_payload(), run_started_at=now)

    row_id = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, data,
                target_id, analyst_id, produced_at, derived_from, schema_uri
            ) VALUES (
                $1, 'finding', 'broken', '', 0.1,
                '{"divergences": "not-a-list"}'::jsonb,
                '', 'layer_divergence', $2, '{}',
                'iglu:legba/finding/jsonschema/1-0-0'
            )
            """,
            row_id, now,
        )

    res = await client.get(_ROUTE)
    assert res.status_code == 200
    assert res.json()["measured"] is True
