# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RA-1 — the reader's read access (the owner's pick: open the read-only
versions of what the reader surfaces need; every change stays with a
contributor or an admin).

  * The reads the reader surfaces make, opened to ``reader`` as they are
    (their answers are figures, ids, states and times): a reader gets 200,
    and each answer's keys are the route's declared shape — no person, note,
    credential, prompt or body.
  * Two admin lists whose whole answer is NOT a reader's get a narrow
    reader read (``reader_reads_api``): ``/v3/roster`` (no owner, no body
    beyond the summary, no tenant, no signature columns) and
    ``/v3/watch/alerts`` (the world's kinds; no who, no note, no sinks, no
    rule path). The admin lists keep ``admin``.
  * No credential is 401 on every one of them; every change stays refused
    to a reader (403).

PURE tests need no database; the rest run over the migrated test DB through
the real app (middlewares + the route gate) and real HTTP.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from legba.data import users as U
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.registry import reader_reads_api as rra
from legba.data.registry import route_policy as rp
from legba.data.registry import route_roles as rr
from legba.data.registry import session_auth as sa
from legba.data.registry.alert_log_api import (
    AlertEpisode,
    AlertKindOut,
    AlertLogOut,
    AttentionState,
    PagedAlert,
    needs_you_kinds,
)
from legba.data.registry.credentials import MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRow, Family
from legba.data.registry.list_weights import LIST_ROUTES
from legba.shared import auth_signin as S

os.environ.setdefault(
    MASTER_KEY_ENV, "0011223344556677889900112233445566778899001122334455667788990011",
)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)

V1 = "/api/v1"
V3 = "/api/v1/v3"
REG = "/api/v1/registry"
ADMIN_TOK = "ra1-admin-token-" + "7" * 24
SERVICE_TOK = "ra1-service-token-" + "8" * 24
_DOMAIN = "ra1read.example"

#: Opened to a reader as they are: (route template, a concrete GET path).
OPENED: dict[str, str] = {
    f"{V3}/system/source-firing": f"{V3}/system/source-firing",
    f"{V3}/system/analyst-cadence": f"{V3}/system/analyst-cadence",
    f"{V3}/system/judge-stats": f"{V3}/system/judge-stats?days=7",
    f"{V3}/eval/grader_roster": f"{V3}/eval/grader_roster",
    # Scoped to a subject nobody writes: the whole-table read inherits other
    # files' scorecard rows (test_production_gauge leaves target_id-NULL
    # ones, which the route's model refuses -> 500); the role is the point.
    f"{V3}/eval/country_scorecard": f"{V3}/eval/country_scorecard?target_id=ra1_nobody",
    f"{V3}/system/receipt-anchors": f"{V3}/system/receipt-anchors?limit=5",
    f"{V3}/evidence/drift": f"{V3}/evidence/drift?window_hours=168",
    f"{V3}/evidence/drift/editors": f"{V3}/evidence/drift/editors",
    f"{V3}/system/staleness-debt": f"{V3}/system/staleness-debt",
    f"{V3}/eval/scorecard": f"{V3}/eval/scorecard?limit=5",
    f"{V3}/eval/calibration": f"{V3}/eval/calibration",
    f"{V3}/eval/correctness": f"{V3}/eval/correctness",
    f"{V1}/eval/scores": f"{V1}/eval/scores",
    f"{V1}/units/{{target_id}}/correctness": f"{V1}/units/ra1_nobody/correctness?claims=true",
}
#: The narrow reader reads (new routes).
NARROW: dict[str, str] = {
    f"{V3}/roster": f"{V3}/roster?family=target",
    f"{V3}/watch/alerts": f"{V3}/watch/alerts?hours=24",
}
#: Admin lists that keep admin (the reader reads a narrow form instead), and
#: the engine reads the reader surfaces do not need.
STAYS_ADMIN = [
    f"{REG}/descriptors", f"{REG}/sources", f"{V3}/system/alerts/log",
    f"{V3}/evidence/drift/flags", f"{V3}/evidence/drift/history",
    f"{V3}/evidence/drift/assessments", f"{V3}/system/escalations",
    f"{V1}/targets/{{target_id}}/runtime", f"{REG}/descriptors/{{family}}/{{descriptor_id}}",
    f"{V3}/system/judge-stats/health", f"{V3}/flow/graph", f"{V1}/read-events/rollup",
]
#: Contributor work queues — hidden from a reader in the console, not opened.
STAYS_CONTRIBUTOR = [f"{V1}/journal_proposals", f"{V3}/ledger/resolution-queue"]

#: Words no reader answer may carry as a KEY anywhere in it.
SENSITIVE_KEYS = {
    "owner", "owner_tenant", "acked_by", "snoozed_by", "ack_note", "raised_by",
    "outcomes", "rule_path", "rule_error", "type_signature", "signed_payload",
    "signer_did", "warnings", "inherits", "acquisition", "cadence", "credentials",
    "vault", "secret", "token", "password", "prompt", "system_prompt", "endpoint",
    "email", "users",
}


def _keys(obj: Any) -> set[str]:
    """Every dict key anywhere in a JSON value."""
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys(v)
    return out


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_policy_opens_reads_and_keeps_changes() -> None:
    for tpl in [*OPENED, *NARROW]:
        assert rp.ROUTE_MIN_ROLES[("GET", tpl)] == S.ROLE_READER, tpl
    for tpl in STAYS_ADMIN:
        assert rp.ROUTE_MIN_ROLES[("GET", tpl)] == S.ROLE_ADMIN, tpl
    for tpl in STAYS_CONTRIBUTOR:
        assert rp.ROUTE_MIN_ROLES[("GET", tpl)] == S.ROLE_CONTRIBUTOR, tpl
    # No write moved: every non-GET entry is still above reader, except the
    # reader's own rows and the three public credential steps (unchanged).
    own_rows = {
        ("POST", f"{V1}/consult"), ("POST", f"{V1}/consult/runs/{{request_id}}/stop"),
        ("POST", f"{V1}/consult/runs/{{request_id}}/synthesize"),
        ("POST", f"{V1}/deep_consult"), ("POST", f"{V1}/read-events"),
        ("PUT", f"{V3}/me/seen"), ("POST", f"{V3}/watchlist"),
        ("PUT", f"{V3}/watchlist/{{watch_id}}"), ("DELETE", f"{V3}/watchlist/{{watch_id}}"),
        ("POST", f"{V3}/export"),
        ("POST", f"{V1}/auth/accept"), ("POST", f"{V1}/auth/sign-in"),
        ("POST", f"{V1}/auth/sign-out"),
    }
    reader_writes = {
        k for k, role in rp.ROUTE_MIN_ROLES.items()
        if k[0] in ("POST", "PUT", "PATCH", "DELETE") and role == S.ROLE_READER
    }
    assert reader_writes == own_rows, sorted(reader_writes ^ own_rows)


def test_new_list_route_declared_in_list_weights() -> None:
    w = LIST_ROUTES[f"{V3}/roster"]
    assert w.exempt and w.page_budget > 0


def test_slim_import() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.reader_reads_api
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts")
                        or m.startswith("legba.runtime"))
        assert not leaked, leaked
        print("OK")
        """
    )
    src_root = str(pathlib.Path(rra.__file__).resolve().parents[3])
    r = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        # keep the parent's PYTHONPATH tail (the test image's deps live there)
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            x for x in (src_root, os.environ.get("PYTHONPATH", "")) if x)},
    )
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


def _row(family: Family, *, descriptor_id: str, body: dict[str, Any], kind: str | None = None) -> DescriptorRow:
    return DescriptorRow(
        descriptor_id=descriptor_id, version="v" * 64, schema_uri=f"legba/{family.value}/1.0.0",
        is_head=True, state="active", owner="someone@ra1read.example", name=f"{descriptor_id} name",
        body=body, created_at=datetime(2026, 11, 1, tzinfo=timezone.utc), family=family,
        abstraction_level="L1", inherits=["parent.x"], kind=kind,
        type_signature={"in": "secret-shape"}, warnings=["dead config"],
    )


_TARGET_BODY = {
    "identity": {"id": "t1", "name": "Harbour", "owner": "someone@ra1read.example"},
    "scope": {"domain": "geo", "geo": ["BR"], "tags": ["t"], "geometry": None,
              "entity_classes": ["org"], "languages": ["pt"]},
    "sources": [{"id": "s1"}, {"id": "s2"}],
    "prompts": {"system_prompt": "PRIVATE PROMPT TEXT"},
    "credentials": {"vault": "vault://secret/key"},
}
_SOURCE_BODY = {
    "identity": {"id": "s1", "name": "Feed", "kind": "rss", "owner": "someone@ra1read.example"},
    "scope": {"owner_tenant": "tenant-x", "geo": ["US"], "languages": ["en"], "tags": ["news"]},
    "acquisition": "poll",
    "cadence": {"schedule": {"raw": "*/15 * * * *"}},
    "endpoint": {"url": "https://feed.example/rss?token=PRIVATE"},
    "credentials": {"vault_ref": "vault://feeds/token"},
}


def test_roster_row_projection_drops_everything_private() -> None:
    t = rra.roster_row(_row(Family.TARGET, descriptor_id="t1", body=_TARGET_BODY)).model_dump(mode="json")
    assert set(t) == set(rra.RosterRow.model_fields)
    assert t["body"] == {
        "identity": {"name": "Harbour"},
        "scope": {"domain": "geo", "geo": ["BR"], "tags": ["t"], "geometry": None},
        "declared_sources": 2,
    }
    s = rra.roster_row(_row(Family.SOURCE, descriptor_id="s1", body=_SOURCE_BODY, kind="rss")).model_dump(mode="json")
    assert s["kind"] == "rss" and s["geo"] == ["US"] and s["languages"] == ["en"] and s["tags"] == ["news"]
    for out in (t, s):
        blob = repr(out)
        for word in ("PRIVATE", "vault://", "someone@", "tenant-x", "secret-shape", "dead config", "parent.x", "*/15"):
            assert word not in blob, (word, blob)
        assert not (_keys(out) & SENSITIVE_KEYS), _keys(out) & SENSITIVE_KEYS


def _full_log(world: str, engine: str) -> AlertLogOut:
    att = AttentionState(acked_at="2026-11-15T00:00:00+00:00", acked_by="user:abc",
                         ack_note="private note", ack_active=True, snoozed_by="user:def")

    def ep(kind: str) -> AlertEpisode:
        return AlertEpisode(
            id=f"id-{kind}", kind=kind, group="g", topic="t", subject_kind="target",
            subject_id="x", severity="high", max_severity="high", summary=f"{kind} s",
            opened_at="2026-11-15T00:00:00+00:00", last_raised_at="2026-11-15T00:00:00+00:00",
            occurrences=1, paged_count=1, raised_by="service:worker", attention=att,
        )

    def pg(kind: str) -> PagedAlert:
        return PagedAlert(
            event_id=f"e-{kind}", raised_at="2026-11-15T00:00:00+00:00", kind=kind,
            severity="high", subject_kind="target", subject_id="x", summary="s",
            action="page", why="rule", rule_entry="high+", rule_version="v1",
            outcomes=["ntfy:private-topic", "delivered"], delivered=True, raised_by="service:worker",
        )

    return AlertLogOut(
        measured=True, window_hours=24, read_at="2026-11-15T00:00:00+00:00",
        rule_version="v1", rule_path="/etc/legba/page_rule.yaml", rule_error="boom at /etc",
        kinds=[AlertKindOut(kind=k, group="g", topic="t", subject_kind="target",
                            lifecycle="episode", summary="k", pages_when=["high+"])
               for k in (world, engine)],
        open=[ep(world), ep(engine)], closed=[ep(world)], paged=[pg(world), pg(engine)],
    )


def test_reader_alert_log_is_the_world_without_people_or_sinks() -> None:
    world_kinds = needs_you_kinds(is_admin=False)
    assert world_kinds, "the world topic has kinds"
    world = world_kinds[0]
    engine = "source_degraded"
    assert engine not in world_kinds
    out = rra.reader_alert_log(_full_log(world, engine), kinds=world_kinds).model_dump(mode="json")
    assert {e["kind"] for e in out["open"] + out["closed"] + out["paged"]} == {world}
    assert [k["kind"] for k in out["kinds"]] == [world]
    assert out["open"][0]["attention"]["ack_active"] is True
    assert out["paged"][0]["delivered"] is True
    blob = repr(out)
    for word in ("user:abc", "user:def", "private note", "service:worker", "ntfy:", "/etc/legba", "boom"):
        assert word not in blob, word
    assert not (_keys(out) & SENSITIVE_KEYS), _keys(out) & SENSITIVE_KEYS


# ---------------------------------------------------------------------------
# The real app, sign-in ON
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
    yield p
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{_DOMAIN}")
        await conn.execute("DELETE FROM alerts WHERE raised_by = 'ra1_test'")
    await p.close()


@pytest_asyncio.fixture
async def on_app(migrated_pg: PostgresConfig, monkeypatch, pool):
    """Sign-in ON: a reader, a contributor and an admin with cookies."""
    from legba.data.registry.server import create_app

    monkeypatch.setenv(S.SIGNIN_ENV, "1")
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", ADMIN_TOK)
    monkeypatch.setenv("LEGBA_REGISTRY_SERVICE_TOKEN", SERVICE_TOK)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv(rr.ROLE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(sa.COOKIE_INSECURE_ENV, raising=False)
    app = create_app(
        pg_config=migrated_pg, nats_config=NatsConfig.from_env(),
        enable_healthcheck_loop=False, enable_vocabulary_subscription=False,
    )
    store = app.state.registry_deps.descriptor_registry.pg
    await store.connect()
    who: dict[str, dict[str, str]] = {}
    async with pool.acquire() as conn:
        for name, role in (("rhea", "reader"), ("cole", "contributor"), ("ada", "admin")):
            uid = await conn.fetchval(
                "INSERT INTO users (email, role, status) VALUES ($1, $2, 'active') "
                "RETURNING id", f"{name}@{_DOMAIN}", role,
            )
            session = await U.create_session(conn, await U.get_user(conn, uid))
            who[name] = {
                "Cookie": f"{sa.SESSION_COOKIE_NAME}={session.token}",
                "Sec-Fetch-Site": "same-origin",
            }
    who["admin_token"] = {"Authorization": f"Bearer {ADMIN_TOK}"}
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as c:
            yield c, who, app
    finally:
        await store.close()


async def _episode(pool: Any, *, kind: str) -> str:
    at = datetime.now(timezone.utc) - timedelta(minutes=2)
    subject = f"ra1-{uuid.uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        eid = str(await conn.fetchval(
            """
            INSERT INTO alerts (kind, subject_kind, subject_id, dedupe_key, state,
                                severity, max_severity, summary, raised_by,
                                opened_at, last_raised_at, paged_count,
                                paged_severity, last_paged_at)
            VALUES ($1, 'target', $2, $3, 'open', 'high', 'high', 'ra1 episode',
                    'ra1_test', $4, $4, 1, 'high', $4)
            RETURNING id
            """,
            kind, subject, f"{kind}:{subject}", at,
        ))
        await conn.execute(
            "UPDATE alerts SET acked_at = now(), acked_by = 'user:ra1-someone', "
            "ack_note = 'ra1 private note', ack_severity = 'high' WHERE id = $1::uuid", eid)
    return eid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reader_reads_200_without_sensitive_keys(on_app) -> None:
    c, who, _ = on_app
    for tpl, path in {**OPENED, **NARROW}.items():
        r = await c.get(path, headers=who["rhea"])
        assert r.status_code == 200, (tpl, r.status_code, r.text[:300])
        found = _keys(r.json()) & SENSITIVE_KEYS
        assert not found, (tpl, found)
        # No credential at all: 401 (the bearer middleware's own answer).
        r = await c.get(path)
        assert r.status_code == 401, (tpl, r.status_code)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_lists_stay_admin(on_app) -> None:
    c, who, _ = on_app
    for path in (f"{REG}/descriptors?family=target&fields=summary", f"{REG}/sources",
                 f"{V3}/system/alerts/log?hours=24", f"{V3}/evidence/drift/flags",
                 f"{V3}/system/judge-stats/health"):
        r = await c.get(path, headers=who["rhea"])
        assert r.status_code == 403, (path, r.text)
        assert r.json()["detail"]["required_role"] == "admin"
        assert (await c.get(path, headers=who["ada"])).status_code == 200, path
    for path in (f"{V1}/journal_proposals", f"{V3}/ledger/resolution-queue"):
        r = await c.get(path, headers=who["rhea"])
        assert r.status_code == 403 and r.json()["detail"]["required_role"] == "contributor"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reader_writes_still_refused(on_app, pool) -> None:
    c, who, _ = on_app
    eid = await _episode(pool, kind=needs_you_kinds(is_admin=False)[0])
    refused = [
        ("POST", f"{V3}/system/alerts/{eid}/ack", {}),
        ("DELETE", f"{V3}/system/alerts/{eid}/ack", None),
        ("POST", f"{V3}/system/alerts/{eid}/snooze", {"duration": "1h"}),
        ("DELETE", f"{V3}/system/alerts/{eid}/snooze", None),
        ("POST", f"{V1}/journal_proposals/{uuid.uuid4()}/accept", {}),
        ("POST", f"{V1}/journal_proposals/{uuid.uuid4()}/reject", {}),
        ("POST", f"{V3}/ledger/resolutions/{uuid.uuid4()}/confirm", {}),
        ("POST", f"{V3}/ledger/resolutions/{uuid.uuid4()}/reject", {}),
        ("POST", f"{V3}/ledger/resolutions/{uuid.uuid4()}/judgement", {}),
        ("POST", f"{REG}/descriptors/target", {}),
        ("PUT", f"{REG}/descriptors/target/x", {}),
        ("POST", f"{REG}/descriptors/source/x/transition", {}),
        ("POST", f"{V1}/eval/labels", {}),
        ("POST", f"{V3}/eval/goldset/label", {}),
        ("POST", f"{V3}/optimizer/candidates/x/review", {}),
        ("PATCH", f"{V3}/retention-policies/x", {}),
        ("POST", f"{V1}/consult/writes", {}),
    ]
    for method, path, body in refused:
        r = await c.request(method, path, headers=who["rhea"], json=body)
        assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
        assert r.json()["detail"]["error"] == "role_forbidden", (method, path)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_roster_route_over_the_real_gate(on_app, monkeypatch) -> None:
    c, who, app = on_app
    reg = app.state.registry_deps.descriptor_registry
    seen: list[Any] = []

    async def fake_list(pred: Any) -> list[DescriptorRow]:
        seen.append(pred)
        if pred.family is Family.SOURCE:
            return [_row(Family.SOURCE, descriptor_id="s1", body=_SOURCE_BODY, kind="rss")]
        return [_row(pred.family, descriptor_id="t1", body=_TARGET_BODY)]

    monkeypatch.setattr(reg, "list", fake_list)
    for family in ("target", "analyst", "source"):
        r = await c.get(f"{V3}/roster", params={"family": family}, headers=who["rhea"])
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1 and set(rows[0]) == set(rra.RosterRow.model_fields)
        assert rows[0]["family"] == family
        blob = r.text
        for word in ("PRIVATE", "vault://", "someone@", "tenant-x", "secret-shape", "*/15"):
            assert word not in blob, (family, word)
        assert not (_keys(rows) & SENSITIVE_KEYS)
    assert all(p.head_only and p.limit == rra.ROSTER_DEFAULT_LIMIT for p in seen)
    # Only the three roster families; a bad family is 422.
    r = await c.get(f"{V3}/roster", params={"family": "action_pack"}, headers=who["rhea"])
    assert r.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watch_alerts_over_the_real_gate(on_app, pool) -> None:
    c, who, _ = on_app
    world = await _episode(pool, kind=needs_you_kinds(is_admin=False)[0])
    engine = await _episode(pool, kind="source_degraded")
    r = await c.get(f"{V3}/watch/alerts", params={"hours": 24}, headers=who["rhea"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["measured"] is True, body
    ids = {e["id"] for e in body["open"]}
    assert world in ids and engine not in ids
    ep = next(e for e in body["open"] if e["id"] == world)
    assert ep["attention"]["ack_active"] is True and ep["attention"]["acked_at"]
    assert "ra1 private note" not in r.text and "ra1-someone" not in r.text
    assert "ra1_test" not in r.text  # raised_by
    assert not (_keys(body) & SENSITIVE_KEYS), _keys(body) & SENSITIVE_KEYS
    # The admin's full log still names both and who acted.
    full = (await c.get(f"{V3}/system/alerts/log", params={"hours": 24},
                        headers=who["ada"])).json()
    full_ids = {e["id"] for e in full["open"]}
    assert world in full_ids and engine in full_ids
    assert any(e["attention"]["acked_by"] == "user:ra1-someone" for e in full["open"])
