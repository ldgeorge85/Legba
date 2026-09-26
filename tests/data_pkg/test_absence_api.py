# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/absence?scope=`` — typed absence as one named thing (lane k5).

The route answers one desk's absences in ONE closed seven-kind vocabulary, and
stamps every one of them in time. These tests bind through the REAL ASGI app
over a migrated test DB (the ``receipt_anchors`` / staleness-debt precedent)
and pin:

  * one fixture per KIND, each producing its item with a PROOF and a full set
    of time stamps (``as_of`` / ``as_of_basis`` / ``expires_at`` / ``stale``);
  * the two pairs the vocabulary exists to keep apart — "searched and found
    nothing" vs. "the search failed", and "healthy but silent" vs. "stale";
  * ``as_of`` is a MEASURED instant, never the read's own clock;
  * a past ``expires_at`` renders ``stale: true`` — an old absence never reads
    as a current one;
  * a desk with nothing absent — an empty ``absences`` list, never an error and
    never a fabricated row;
  * the empty / blank scope → 422 naming the parameter;
  * "not checked" and "nothing absent" staying distinguishable
    (``not_measured`` vs. an empty list);
  * the slim-image import guard — this module ships in the registry image;
  * the CROSS-LANGUAGE drift guard: the nine bounded units and the grace
    multiple exist in Python here and in TypeScript in
    ``legba-ui-v3/src/lib/gapStripModel.ts``. They cannot share a literal, so
    the TS is parsed and compared — in both directions — and the unit cadence
    the strip hardcodes is checked against the unit's own descriptor.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.absence_api import (
    ABSENCE_KINDS,
    ABSENCE_ROUTE_VERSION,
    AUDIT_ANALYST_ID,
    BANDING_ANALYST_ID,
    BOUNDED_UNITS,
    UNIT_GRACE_MULTIPLE,
    below_floor_items,
    build_absence_router,
    poll_health,
    schedule_stamp,
    unit_item,
)
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "77" * 32)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = str(_REPO_ROOT / "src")
_ROUTE = "/api/v1/v3/absence"

#: Desk ids unique to this module — every table it touches is shared.
DESK = "country_watch_k5absence"
#: A desk seeded with a CURRENT read for every unit, to prove the empty state.
QUIET_DESK = "country_watch_k5quiet"
_GEO = "ZQ"
#: The fixture owner every descriptor row this module writes carries, so the
#: purge can never remove another test's row.
_OWNER = "test_k5_absence"


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    # Scope-scoped cleanup: this module never truncates a shared table, it
    # removes only the rows it owns (the 2026-09-24 concurrent-run rule).
    await _purge(pg_store)
    identity = SigningIdentity(
        signing_key=SigningKey(b"k5-absence-route-test-seed-0001!"[:32]),
        signer_did="did:legba:registry:k5-absence-test",
    )
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    descriptor_registry = DescriptorRegistry(
        pg_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_absence_router(deps), prefix="/api/v1/v3")
    yield app, pg_store
    await _purge(pg_store)
    await descriptor_registry.stop()
    await pg_store.close()


async def _purge(pg_store: PostgresStore) -> None:
    async with pg_store.acquire() as conn:
        await conn.execute(
            "DELETE FROM public.analyst_outputs WHERE target_id = ANY($1::text[])",
            [DESK, QUIET_DESK],
        )
        await conn.execute(
            "DELETE FROM public.desk_apertures WHERE target_id = ANY($1::text[])",
            [DESK, QUIET_DESK],
        )
        await conn.execute(
            "DELETE FROM public.target_descriptors "
            "WHERE descriptor_id = ANY($1::text[])",
            [DESK, QUIET_DESK],
        )
        await conn.execute(
            "DELETE FROM public.signals WHERE geo && $1::text[]", [_GEO]
        )
        await conn.execute(
            "DELETE FROM public.source_poll_outcomes WHERE source_id LIKE $1",
            "source.k5absence.%",
        )
        await conn.execute(
            "DELETE FROM public.source_descriptors WHERE descriptor_id LIKE $1",
            "source.k5absence.%",
        )
        # The analyst descriptor heads this module seeds carry the REAL unit /
        # producer ids (the route reads exactly those), so the delete is keyed
        # on the fixture's own owner + version and can never remove another
        # test's row.
        await conn.execute(
            "DELETE FROM public.analyst_descriptors "
            "WHERE owner = $1 AND version = 'k5v1'",
            _OWNER,
        )
        # `external_grades` is schema-enforced append-only (migration 0190), so
        # this module seeds claim keys it never re-uses rather than deleting;
        # the route's own window + target filter isolate them anyway.


@pytest_asyncio.fixture
async def client(api_app):
    app, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


def _by_kind(body: dict, kind: str) -> list[dict]:
    return [a for a in body["absences"] if a["kind"] == kind]


def _assert_stamped(item: dict, *, measured: bool = True) -> None:
    """Every item carries the closed kind and the full time stamp set."""
    assert item["kind"] in ABSENCE_KINDS, item["kind"]
    assert item["as_of_basis"], "an absence must name which instant its as_of is"
    assert isinstance(item["stale"], bool)
    if measured:
        assert item["as_of"] is not None, "as_of must be a measured instant"
    assert item["proof"]["what_was_checked"]


# ---------------------------------------------------------------------------
# The route, bound through the app
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_blank_scope_is_422_naming_the_parameter(client):
    r = await client.get(_ROUTE, params={"scope": "   "})
    assert r.status_code == 422, r.text
    assert "scope" in r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_scope_is_422(client):
    r = await client.get(_ROUTE)
    assert r.status_code == 422, r.text
    assert "scope" in r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_route_publishes_the_closed_kind_vocabulary(client):
    r = await client.get(_ROUTE, params={"scope": QUIET_DESK})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == ABSENCE_ROUTE_VERSION
    # Eight kinds, each with its one-line meaning, on the wire (7g-2 added
    # `history_gap` — the only one about the PAST).
    assert set(body["kinds"]) == set(ABSENCE_KINDS)
    assert len(body["kinds"]) == 8
    assert all(v.strip() for v in body["kinds"].values())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_desk_with_nothing_absent_reads_an_empty_list(client, api_app):
    """A desk with a CURRENT read for every unit, no apertures, no audit rows
    and no geo-resolved roster: every kind read cleanly, the answer is an empty
    list — not an error, and not a fabricated row."""
    _, pg_store = api_app
    now = datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        for unit in BOUNDED_UNITS:
            await _insert_analyst_head(conn, unit, "0 1,13 * * *")
            await _insert_finding(conn, QUIET_DESK, unit, now - timedelta(hours=1))

    r = await client.get(_ROUTE, params={"scope": QUIET_DESK})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == QUIET_DESK
    assert body["absences"] == []
    # "not checked" stays distinguishable from "nothing absent": this desk has
    # no scorecard and no geo, and BOTH say so by name rather than by silence.
    joined = " ".join(body["not_measured"])
    assert "below_floor" in joined
    assert "source_stale (sources)" in joined
    assert "units" not in joined
    assert "layer_declared_absent" not in joined


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_unit_with_no_read_is_not_collected(client, api_app):
    """No read on record ⇒ ``not_collected``, carrying a ``window`` rather than
    an invented instant, and stamped with the banding run's own scan when one
    exists — never with the read's clock."""
    _, pg_store = api_app
    now = datetime.now(timezone.utc)
    card_at = now - timedelta(hours=3)
    async with pg_store.acquire() as conn:
        for unit in BOUNDED_UNITS:
            await _insert_analyst_head(conn, unit, "0 1,13 * * *")
        await _insert_analyst_head(conn, BANDING_ANALYST_ID, "40 4 * * *")
        await _insert_scorecard(conn, DESK, card_at, {})

    r = await client.get(_ROUTE, params={"scope": DESK, "limit_per_kind": 50})
    body = r.json()
    items = {a["subject"]: a for a in _by_kind(body, "not_collected")}
    assert set(items) == set(BOUNDED_UNITS)
    one = items[BOUNDED_UNITS[0]]
    _assert_stamped(one)
    assert one["since"] is None
    assert one["window"] == "no read on record for this desk"
    assert one["as_of"] == card_at.isoformat()
    assert one["as_of"] != body["read_at"], "as_of is measured, never now()"
    assert "scorecard" in one["as_of_basis"]
    assert one["expires_at"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_unit_past_its_cadence_is_source_stale_and_reads_stale(
    client, api_app
):
    """A read older than the unit's own cadence ⇒ ``source_stale``, stamped
    with that unit's last run and an ``expires_at`` already past: the fire that
    should have refreshed it never happened, so the absence is LAST KNOWN, NOT
    RE-CHECKED."""
    _, pg_store = api_app
    now = datetime.now(timezone.utc)
    stale_unit, fresh_unit = BOUNDED_UNITS[0], BOUNDED_UNITS[1]
    last_run = now - timedelta(hours=40)
    async with pg_store.acquire() as conn:
        await _insert_analyst_head(conn, stale_unit, "0 1,13 * * *")
        await _insert_analyst_head(conn, fresh_unit, "0 4,16 * * *")
        stale_id = await _insert_finding(conn, DESK, stale_unit, last_run)
        await _insert_finding(conn, DESK, fresh_unit, now - timedelta(hours=1))

    r = await client.get(_ROUTE, params={"scope": DESK, "limit_per_kind": 50})
    body = r.json()
    items = {a["subject"]: a for a in _by_kind(body, "source_stale")}

    assert fresh_unit not in items, "a unit inside its cadence is not an absence"
    assert fresh_unit not in {a["subject"] for a in body["absences"]}

    stale = items[stale_unit]
    _assert_stamped(stale)
    assert stale["since"] == last_run.isoformat()
    assert stale["window"] is None
    assert stale["as_of"] == last_run.isoformat()
    assert stale["as_of_basis"] == "this unit's last run for this desk"
    assert stale["expires_at"] is not None
    assert stale["expires_at"] < body["read_at"]
    assert stale["stale"] is True
    assert stale["proof"]["ref"] == stale_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_layer_declared_absent_has_no_clock_and_a_review(client, api_app):
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.desk_apertures "
            "(target_id, layer, declared, reason, map_version) "
            "VALUES ($1, 'official', 'absent', $2, 'k5-test-map/v1')",
            DESK,
            "curated: no state feed is registered for this desk",
        )
        # A `present` layer is not an absence and must not appear.
        await conn.execute(
            "INSERT INTO public.desk_apertures "
            "(target_id, layer, declared, reason, map_version) "
            "VALUES ($1, 'foreign_press', 'present', '', 'k5-test-map/v1')",
            DESK,
        )

    r = await client.get(_ROUTE, params={"scope": DESK})
    items = _by_kind(r.json(), "layer_declared_absent")
    assert [i["subject"] for i in items] == ["official"]
    _assert_stamped(items[0])
    assert items[0]["reason"] == "curated: no state feed is registered for this desk"
    assert "k5-test-map/v1" in items[0]["proof"]["what_was_checked"]
    # A declaration is revised by a map revision, not by a clock, so it carries
    # no expiry and is never stale.
    assert items[0]["expires_at"] is None
    assert items[0]["review"] == "map revision"
    assert items[0]["stale"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_below_floor_reads_the_live_card_and_skips_no_finding(client, api_app):
    _, pg_store = api_app
    now = datetime.now(timezone.utc)
    card_at = now - timedelta(hours=2)
    async with pg_store.acquire() as conn:
        await _insert_analyst_head(conn, BANDING_ANALYST_ID, "40 4 * * *")
        await _insert_scorecard(
            conn,
            DESK,
            card_at,
            {
                "escalation": {
                    "band": "insufficient-evidence",
                    "basis": [],
                    "reason": "low-faithfulness",
                    "critic_score": 0.31,
                    "produced_at": card_at.isoformat(),
                },
                # `no-finding` is the card's own window, NOT a floor failure —
                # the unit kinds own it and this kind must skip it.
                "energy_security": {
                    "band": "insufficient-evidence",
                    "basis": [],
                    "reason": "no-finding",
                    "critic_score": None,
                    "produced_at": None,
                },
                "military_posture": {
                    "band": "elevated",
                    "basis": ["x"],
                    "reason": "qualified",
                    "critic_score": 0.8,
                    "produced_at": card_at.isoformat(),
                },
            },
        )

    r = await client.get(_ROUTE, params={"scope": DESK})
    body = r.json()
    items = _by_kind(body, "below_floor")
    assert [i["subject"] for i in items] == ["escalation"]
    _assert_stamped(items[0])
    assert "low-faithfulness" in items[0]["reason"]
    assert "0.31" in items[0]["proof"]["what_was_checked"]
    assert items[0]["as_of"] == card_at.isoformat()
    assert items[0]["as_of"] != body["read_at"]
    assert "scorecard" in items[0]["as_of_basis"]
    # Banding is daily; a card two hours old has not come due again.
    assert items[0]["expires_at"] is not None
    assert items[0]["stale"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_two_audit_kinds_are_never_conflated(client, api_app):
    """NOT_FOUND is "we looked and saw nothing"; UNCHECKED is "we could not
    look". They are different facts and get different kinds."""
    _, pg_store = api_app
    now = datetime.now(timezone.utc)
    found_key, failed_key = f"k5-{uuid4().hex}", f"k5-{uuid4().hex}"
    graded_at = now - timedelta(hours=2)
    async with pg_store.acquire() as conn:
        await _insert_analyst_head(conn, AUDIT_ANALYST_ID, "7 * * * *")
        await _insert_grade(
            conn,
            claim_key=found_key,
            verdict="NOT_FOUND",
            unchecked_reason=None,
            graded_at=graded_at,
        )
        await _insert_grade(
            conn,
            claim_key=failed_key,
            verdict="UNCHECKED",
            unchecked_reason="robots_disallowed",
            graded_at=graded_at,
        )

    r = await client.get(_ROUTE, params={"scope": DESK, "limit_per_kind": 50})
    body = r.json()

    found = {i["subject"]: i for i in _by_kind(body, "searched_found_nothing")}
    failed = {i["subject"]: i for i in _by_kind(body, "search_failed")}
    assert found_key in found and found_key not in failed
    assert failed_key in failed and failed_key not in found

    _assert_stamped(found[found_key])
    assert "nothing in the results decided" in found[found_key]["reason"]
    assert "2 result(s) returned, none decisive" in (
        found[found_key]["proof"]["what_was_checked"]
    )
    assert found[found_key]["as_of"] == graded_at.isoformat()
    assert found[found_key]["as_of"] != body["read_at"]
    assert "audit" in found[found_key]["as_of_basis"]
    # The auditor fires hourly; a two-hour-old grade is past its next pass.
    assert found[found_key]["stale"] is True

    _assert_stamped(failed[failed_key])
    assert "did not answer" in failed[failed_key]["reason"]
    assert "robots_disallowed" in failed[failed_key]["proof"]["what_was_checked"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_healthy_source_that_carried_nothing_is_its_own_kind(
    client, api_app
):
    """The distinction the vocabulary exists for: a source still polling
    cleanly that carried nothing for THIS desk is ``collected_but_silent`` (a
    fact about the world's quiet), while one whose polls are failing is
    ``source_stale`` (a fact about our pipe)."""
    _, pg_store = api_app
    now = datetime.now(timezone.utc)
    healthy = "source.k5absence.healthy"
    broken = "source.k5absence.broken"
    unregistered = "source.k5absence.unregistered"
    last_signal = now - timedelta(days=4)
    async with pg_store.acquire() as conn:
        await _insert_target(conn, DESK, [_GEO])
        for sid in (healthy, broken):
            await _insert_source_head(conn, sid, "*/15 * * * *", "active")
            await _insert_signal(conn, sid, [_GEO], last_signal)
        await _insert_signal(conn, unregistered, [_GEO], last_signal)
        # The healthy one is still polling cleanly; the broken one is erroring.
        await _insert_poll(conn, healthy, "success", now - timedelta(minutes=10))
        await _insert_poll(conn, broken, "error", now - timedelta(minutes=10))

    r = await client.get(_ROUTE, params={"scope": DESK, "limit_per_kind": 50})
    body = r.json()
    silent = {i["subject"]: i for i in _by_kind(body, "collected_but_silent")}
    stale = {i["subject"]: i for i in _by_kind(body, "source_stale")}

    assert healthy in silent and healthy not in stale
    assert broken in stale and broken not in silent
    # An unregistered producer has no declared cadence to be late against.
    assert unregistered not in {a["subject"] for a in body["absences"]}

    item = silent[healthy]
    _assert_stamped(item)
    assert "healthy and carried nothing" in item["reason"]
    assert "polling cleanly" in item["proof"]["what_was_checked"]
    assert item["as_of"] == last_signal.isoformat()
    assert item["as_of"] != body["read_at"]
    assert "most recent signal in this desk's scope" in item["as_of_basis"]
    # A 15-minute poller's next fire after a four-day-old signal is long past.
    assert item["stale"] is True
    assert "no successful poll in the last 7d" in (
        stale[broken]["proof"]["what_was_checked"]
    )
    assert "late or failing" in stale[broken]["reason"]
    # The roster read succeeded, so neither source kind is in not_measured.
    assert not any("sources" in n for n in body["not_measured"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_fresh_in_scope_source_is_not_an_absence(client, api_app):
    _, pg_store = api_app
    now = datetime.now(timezone.utc)
    fresh = "source.k5absence.fresh"
    async with pg_store.acquire() as conn:
        await _insert_target(conn, DESK, [_GEO])
        await _insert_source_head(conn, fresh, "*/15 * * * *", "active")
        await _insert_signal(conn, fresh, [_GEO], now - timedelta(minutes=5))
        await _insert_poll(conn, fresh, "success", now - timedelta(minutes=5))

    r = await client.get(_ROUTE, params={"scope": DESK})
    assert fresh not in {a["subject"] for a in r.json()["absences"]}


# ---------------------------------------------------------------------------
# Pure projection
# ---------------------------------------------------------------------------


def test_schedule_stamp_is_past_for_a_measurer_that_did_not_repeat():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    # Measured two days ago on a 2x/day schedule — the next fire is long past.
    expires_at, stale = schedule_stamp(
        cadence_raw="0 1,13 * * *",
        as_of=now - timedelta(days=2),
        now=now,
    )
    assert expires_at is not None and stale is True
    # Measured five minutes ago on a daily schedule — not yet due.
    expires_at, stale = schedule_stamp(
        cadence_raw="40 4 * * *",
        as_of=now - timedelta(minutes=5),
        now=now,
    )
    assert expires_at is not None and stale is False


def test_schedule_stamp_never_guesses_a_schedule():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    assert schedule_stamp(cadence_raw=None, as_of=now, now=now) == (None, False)
    assert schedule_stamp(cadence_raw="not a cron", as_of=now, now=now) == (
        None,
        False,
    )
    assert schedule_stamp(cadence_raw="0 1 * * *", as_of=None, now=now) == (
        None,
        False,
    )


def test_unit_item_returns_none_without_an_honest_threshold():
    """A unit whose descriptor declares no parsable cadence is never reported
    silent against a budget nobody declared."""
    now = datetime.now(timezone.utc)
    assert (
        unit_item(
            unit="escalation",
            latest_id="f1",
            latest_at=now - timedelta(days=30),
            cadence_raw=None,
            interval_minutes=None,
            card_id=None,
            card_produced_at=None,
            now=now,
        )
        is None
    )


def test_a_unit_with_no_read_and_no_scan_has_a_null_as_of_that_says_so():
    """``as_of`` is never now(). Where no instant exists at all, it is null and
    ``as_of_basis`` states the fact in words."""
    now = datetime.now(timezone.utc)
    item = unit_item(
        unit="escalation",
        latest_id=None,
        latest_at=None,
        cadence_raw="0 1,13 * * *",
        interval_minutes=720.0,
        card_id=None,
        card_produced_at=None,
        now=now,
    )
    assert item is not None
    assert item.kind == "not_collected"
    assert item.as_of is None
    assert "no run and no scan on record" in item.as_of_basis
    assert item.expires_at is None
    assert item.stale is False


def test_below_floor_items_ignores_a_non_dict_dimension_block():
    now = datetime.now(timezone.utc)
    assert (
        below_floor_items(
            card_id="c",
            card_produced_at=None,
            dimensions=None,
            cadence_raw=None,
            now=now,
        )
        == []
    )
    assert (
        below_floor_items(
            card_id="c",
            card_produced_at=None,
            dimensions={"a": 3},
            cadence_raw=None,
            now=now,
        )
        == []
    )


def test_poll_health_never_calls_an_unobserved_poller_healthy():
    healthy, note = poll_health(None, health_days=7)
    assert healthy is False
    assert "no poll recorded" in note


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_absence_route_imports_without_the_runtime_stack() -> None:
    """The route must import in the slim image: no analyst/runtime modules in
    the import graph, and the router factory is present."""
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.absence_api as api

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime package reachable: %r" % leaked
        assert api.build_absence_router is not None
        assert api.ABSENCE_ROUTE_VERSION == "2026-09/k5"
        # 7g-2 added `history_gap` as the eighth. The count is pinned so
        # a kind cannot be added to the vocabulary without a reader
        # surface being updated in the same commit.
        assert len(api.ABSENCE_KINDS) == 8
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, (
        f"slim import failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


_GAP_STRIP_TS = _REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "gapStripModel.ts"
_ABSENCE_TS = _REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "absenceModel.ts"


def _ts_units() -> list[tuple[str, int]]:
    """The `(id, cadenceHours)` pairs the gap strip's TypeScript declares."""
    text = _GAP_STRIP_TS.read_text(encoding="utf-8")
    block = re.search(
        r"export const GAP_STRIP_UNITS: GapUnit\[\] = \[(.*?)\n\]", text, re.S
    )
    assert block, "GAP_STRIP_UNITS not found in gapStripModel.ts"
    return [
        (m.group(1), int(m.group(2)))
        for m in re.finditer(
            r"\{\s*id:\s*'([a-z_]+)'.*?cadenceHours:\s*(\d+)\s*\}", block.group(1)
        )
    ]


def test_bounded_units_match_the_gap_strips_typescript() -> None:
    """The route and the strip name the SAME nine units in the SAME order.

    They cannot share a literal across the language boundary, so the drift is
    caught here rather than discovered as a cell nobody can explain (or an
    explanation with no cell).
    """
    ts = _ts_units()
    assert len(ts) == 9, f"expected nine units in the TS table, got {len(ts)}"
    assert [unit for unit, _ in ts] == list(BOUNDED_UNITS)


def test_grace_multiple_matches_the_gap_strips_typescript() -> None:
    text = _GAP_STRIP_TS.read_text(encoding="utf-8")
    m = re.search(r"export const GAP_STRIP_GRACE_MULTIPLE = ([0-9.]+)", text)
    assert m, "GAP_STRIP_GRACE_MULTIPLE not found in gapStripModel.ts"
    assert float(m.group(1)) == UNIT_GRACE_MULTIPLE


def test_the_kind_vocabulary_matches_the_readers_typescript() -> None:
    """The reader labels the same seven kinds the route publishes — a kind the
    UI cannot name renders as a blank, which is the defect this lane closes."""
    text = _ABSENCE_TS.read_text(encoding="utf-8")
    block = re.search(
        r"ABSENCE_KIND_LABEL: Record<AbsenceKind, string> = \{(.*?)\n\}", text, re.S
    )
    assert block, "ABSENCE_KIND_LABEL not found in absenceModel.ts"
    ts_kinds = set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))
    assert ts_kinds == set(ABSENCE_KINDS)


def test_the_strips_hardcoded_cadence_matches_each_units_descriptor() -> None:
    """The strip hardcodes an hours-per-unit number; the ROUTE derives the same
    number from the unit's own descriptor at request time. This pins the two to
    the descriptors in the tree, so a cadence change cannot land in the
    descriptor and quietly leave the reader surface saying something else."""
    from legba.data.registry import source_freshness

    for unit, cadence_hours in _ts_units():
        path = _REPO_ROOT / "descriptors" / f"analyst_{unit}.yaml"
        assert path.exists(), f"no descriptor for bounded unit {unit}"
        m = re.search(
            r"^\s*fallback_schedule:\s*\"([^\"]+)\"",
            path.read_text(encoding="utf-8"),
            re.M,
        )
        assert m, f"{unit} declares no cadence.fallback_schedule"
        interval = source_freshness.cadence_interval_minutes(m.group(1))
        assert interval is not None, f"{unit}: cadence did not parse"
        assert interval / 60.0 == pytest.approx(cadence_hours), (
            f"{unit}: descriptor says {interval / 60.0}h, the gap strip says "
            f"{cadence_hours}h"
        )


def test_the_measuring_producers_have_declarable_cadences() -> None:
    """``expires_at`` is the next run of the thing that measured the absence,
    so the two producers behind the audit and banding kinds must declare a
    schedule this route can walk."""
    from legba.data.registry import source_freshness

    for analyst, filename in (
        (BANDING_ANALYST_ID, "analyst_scorecard_producer.yaml"),
        (AUDIT_ANALYST_ID, "analyst_standing_auditor.yaml"),
    ):
        path = _REPO_ROOT / "descriptors" / filename
        m = re.search(
            r"^\s*fallback_schedule:\s*\"([^\"]+)\"",
            path.read_text(encoding="utf-8"),
            re.M,
        )
        assert m, f"{analyst} declares no cadence.fallback_schedule"
        assert source_freshness.cadence_interval_minutes(m.group(1)) is not None


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _insert_finding(conn, target: str, analyst: str, produced_at: datetime) -> str:
    row_id = uuid4()
    await conn.execute(
        "INSERT INTO public.analyst_outputs "
        "  (id, kind, title, body, confidence, data, target_id, analyst_id, "
        "   produced_at, schema_uri) "
        "VALUES ($1, 'finding', $2, 'k5 absence fixture', 0.8, '{}'::jsonb, "
        "        $3, $4, $5, 'iglu:legba/finding/jsonschema/1-0-0')",
        row_id,
        f"{analyst} read",
        target,
        analyst,
        produced_at,
    )
    return str(row_id)


async def _insert_scorecard(conn, target: str, produced_at: datetime, dims: dict) -> None:
    await conn.execute(
        "INSERT INTO public.analyst_outputs "
        "  (id, kind, title, body, confidence, data, target_id, analyst_id, "
        "   produced_at, schema_uri) "
        "VALUES ($1, 'scorecard', 'k5 card', '', 1.0, $2::jsonb, $3, "
        "        $4, $5, 'iglu:legba/scorecard/jsonschema/1-0-0')",
        uuid4(),
        json.dumps({"data": {"bands": {"dimensions": dims}}}),
        target,
        BANDING_ANALYST_ID,
        produced_at,
    )


async def _insert_grade(
    conn,
    *,
    claim_key: str,
    verdict: str,
    unchecked_reason: str | None,
    graded_at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO public.external_grades
            (claim_key, population, graded_output_id, analyst_id, target_id,
             claim_text, verdict, unchecked_reason, search_provider,
             search_status, search_liveness, search_degraded, source_urls,
             grader_family, grader_component_id, grader_pipeline_version,
             rubric_version, rater_role, graded_at)
        VALUES ($1, 'assembly_span', $2, 'escalation', $3,
                'No cross-border strike was reported this week.',
                $4, $5, 'searxng', 'ok', 'verified', false,
                ARRAY['https://example.test/a','https://example.test/b'],
                'k5-test', 'k5-test-component', 'k5/1', 'k5-rubric',
                'primary', $6)
        """,
        claim_key,
        uuid4(),
        DESK,
        verdict,
        unchecked_reason,
        graded_at,
    )


async def _insert_target(conn, target: str, geo: list[str]) -> None:
    await conn.execute(
        "INSERT INTO public.target_descriptors "
        "  (descriptor_id, version, schema_uri, is_head, state, owner, name, "
        "   body) "
        "VALUES ($1, 'k5v1', 'legba/target/2.0.0', TRUE, 'active', $2, $1, "
        "        $3::jsonb) "
        "ON CONFLICT DO NOTHING",
        target,
        _OWNER,
        json.dumps({"scope": {"geo": geo}}),
    )


async def _insert_source_head(conn, source_id: str, cron: str, state: str) -> None:
    await conn.execute(
        "INSERT INTO public.source_descriptors "
        "  (descriptor_id, version, schema_uri, is_head, kind, state, owner, "
        "   name, body) "
        "VALUES ($1, 'k5v1', 'legba/source/1.0.0', TRUE, 'rss', $2, $3, $1, "
        "        $4::jsonb) "
        "ON CONFLICT DO NOTHING",
        source_id,
        state,
        _OWNER,
        json.dumps({"cadence": {"schedule": {"raw": cron}}}),
    )


async def _insert_analyst_head(conn, analyst: str, cron: str) -> None:
    await conn.execute(
        "INSERT INTO public.analyst_descriptors "
        "  (descriptor_id, version, schema_uri, is_head, kind, state, owner, "
        "   name, body) "
        "VALUES ($1, 'k5v1', 'legba/analyst/2.0.0', TRUE, 'inline_target', "
        "        'active', $2, $1, $3::jsonb) "
        "ON CONFLICT DO NOTHING",
        analyst,
        _OWNER,
        json.dumps({"cadence": {"fallback_schedule": cron}}),
    )


async def _insert_signal(conn, source_id: str, geo: list[str], fetched_at: datetime) -> None:
    await conn.execute(
        "INSERT INTO public.signals "
        "  (id, source_id, fetched_at, payload, geo) "
        "VALUES ($1, $2, $3, $4::jsonb, $5::text[])",
        uuid4(),
        source_id,
        fetched_at,
        json.dumps({"title": "k5 absence fixture"}),
        geo,
    )


async def _insert_poll(conn, source_id: str, outcome: str, at: datetime) -> None:
    await conn.execute(
        "INSERT INTO public.source_poll_outcomes "
        "  (id, source_id, outcome, occurred_at) "
        "VALUES ($1, $2, $3, $4)",
        uuid4(),
        source_id,
        outcome,
        at,
    )
