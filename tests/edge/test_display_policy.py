# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SENTINEL test — no reader transport carries archived publisher text.

One archived page is planted in the migrated test
database: a ``signals`` row whose ``payload.archived_text`` and
``payload.raw_body`` carry a unique SENTINEL string ~900 characters into the
article (well past any cited span), and a finding whose citation's
``source_text`` carries the same article, exactly as the GATHER path stamps it.
Then every reader transport is driven end to end and its emitted bytes are
searched for the sentinel:

  * ``rest``             — the registry app WITH ``DisplayPolicyMiddleware``:
                           ``/signals``, ``/findings``, ``/lineage`` (signal and
                           finding roots);
  * ``consult_context``  — ``consult_on_demand.run_method`` reading the page
                           through the REAL ``read_document`` Postgres reader
                           and a corpus hit; the probe is every message the
                           model was handed;
  * ``ws``               — the events WebSocket relaying a ``Signal`` frame off
                           NATS;
  * ``sse``              — the consult step relay;
  * ``mcp``              — the MCP server's ``tools/call`` over the registry
                           (reached through the ASGI app with the OPERATOR
                           token — MCP itself must project);
  * ``a2a``              — the A2A skill route over the finding rows;
  * ``export``           — ``POST /v3/export``, JSON and markdown.

The probes are enumerated from ``display_policy.READER_TRANSPORTS`` — ONE
list — so a transport added there without a probe fails
``test_every_reader_transport_has_a_probe``, and a transport added to the tree
without a row there fails ``test_no_unlisted_transport_in_the_tree``.

A reader here is a caller whose request state carries B2's role stamp
(``role=read``, set by a two-line test middleware standing in for the role
gate) or, on the SSE relay, a credential that is not the configured operator
token. The operator half: the operator keeps the full row
(``test_operator_keeps_the_full_row``), and every probe's operator run DOES
carry the sentinel where the transport carries it at all — so a green reader
run is a projection, never an empty pipe.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from nacl.signing import SigningKey

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.display_policy import (
    LICENSE_UNREVIEWED,
    OPERATOR,
    READER,
    READER_TRANSPORTS,
    SIGNAL_PROJECTION_FIELDS,
    display_payload,
    display_projection,
)
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps, build_router
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.display_class import (
    DisplayPolicyMiddleware,
    class_for_principal,
)
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.export_api import build_export_router
from legba.data.registry.lineage_api import build_lineage_router
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.substrate_reads_api import build_substrate_reads_router
from legba.data.registry.vocabulary_cache import VocabularyCache

pytestmark = [pytest.mark.integration]

os.environ.setdefault(
    MASTER_KEY_ENV,
    "0011223344556677889900112233445566778899001122334455667788990011",
)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"

#: The planted string. Unique per process so a stale row from another run can
#: never satisfy (or fail) this one.
SENTINEL = f"LEGBA-B4-SENTINEL-{uuid.uuid4().hex}"
#: 45 chars x 20 = 900 characters of lead before the sentinel — far past any
#: span the policy may carry (``SPAN_CHARS`` = 280).
_LEAD = "The ministry said the corridor stays closed. " * 20
ARTICLE = f"{_LEAD}{SENTINEL} The statement ended there. " + "Closing. " * 5
TITLE = "Ministry keeps the corridor closed"
#: A cited passage that IS in the article — the reader's legitimate span.
CITED = "The ministry said the corridor stays closed."

OP_TOKEN = f"b4-operator-{uuid.uuid4().hex}"
OPERATOR_HEADERS = {"Authorization": f"Bearer {OP_TOKEN}"}
#: The operator credential + B2's role stamp (the stand-in middleware below).
READER_HEADERS = {**OPERATOR_HEADERS, "X-Test-Role": "read"}


class _RoleStamp:
    """Stands in for B2's role gate: ``X-Test-Role`` → ``request.state.role``.
    Test-only by construction (it lives in this file)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            for key, value in scope.get("headers") or []:
                if key.lower() == b"x-test-role":
                    scope.setdefault("state", {})["role"] = value.decode()
        await self.app(scope, receive, send)


def _identity() -> SigningIdentity:
    return SigningIdentity(
        signing_key=SigningKey(b"display-policy-b4-test-seed-0001"),
        signer_did="did:legba:registry:display-policy-test",
    )


# ---------------------------------------------------------------------------
# The planted world
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def world(migrated_pg: PostgresConfig, monkeypatch):
    monkeypatch.setenv(API_TOKEN_ENV, OP_TOKEN)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    identity = _identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    descriptor_registry = DescriptorRegistry(
        pg_store, nats_store=nats_store, vocabulary_cache=vocab,
        signing_identity=identity, audit_logger=audit, dead_letter=dlq,
    )
    await descriptor_registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault, dlq=dlq, audit_logger=audit, vocabulary_cache=vocab,
        nats_store=nats_store, conversion_registry=None,
    )

    tag = uuid.uuid4().hex[:12]
    signal_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    source_id = f"b4test{tag}"
    target_id = f"target.b4test.{tag}"
    analyst_id = f"analyst.b4test.{tag}"
    url = f"https://publisher.example/{tag}/corridor"
    payload = {
        "title": TITLE,
        "summary": "The corridor stays closed, the ministry said.",
        "published_at": "2026-10-07T09:00:00+00:00",
        "archived_text": ARTICLE,
        "archived_text_chars": len(ARTICLE),
        "raw_body": f"<article><h1>{TITLE}</h1><p>{ARTICLE}</p></article>",
        "geo": {"lat": 48.5, "lon": 35.0},
    }
    citation = {
        "marker": "[1]",
        "signal_id": str(signal_id),
        "title": TITLE,
        "source": url,
        "source_id": source_id,
        "snippet": CITED,
        "source_text": ARTICLE,
        "source_truncated": False,
    }
    fetched = datetime.now(tz=timezone.utc)
    async with pg_store.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_kind, fetched_at,
                modality, payload, language, geo, content_hash, derived_from,
                schema_uri, canonical_url, object_ref
            ) VALUES (
                $1, $2, '', 'source', $3, 'text', $4::jsonb, 'en', $5::text[],
                $6, $7::uuid[], 'iglu:legba/signal/jsonschema/3-0-0', $8, $9
            )
            """,
            signal_id, source_id, fetched, json.dumps(payload), ["UA"],
            f"b4hash{tag}", [], url, "cas:sha256/" + "ab" * 32,
        )
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data, target_id,
                target_version, analyst_id, analyst_version, produced_at,
                derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'finding', $2, $3, 0.7, 'medium', $4::jsonb, $5, NULL, $6,
                NULL, $7, $8::uuid[], 'iglu:legba/finding/jsonschema/1-0-0', NULL
            )
            """,
            finding_id, f"Finding on {TITLE}",
            "The corridor remains closed [1].",
            json.dumps({"citations": [citation]}), target_id, analyst_id,
            fetched, [signal_id],
        )

    app = FastAPI()
    app.state.registry_deps = deps
    app.add_middleware(DisplayPolicyMiddleware)
    app.add_middleware(_RoleStamp)
    app.include_router(build_router(deps), prefix="/api/v1/registry")
    app.include_router(build_substrate_reads_router(deps), prefix="/api/v1")
    app.include_router(build_lineage_router(deps), prefix="/api/v1")
    app.include_router(build_export_router(deps), prefix="/api/v1/v3")

    w = {
        "app": app, "deps": deps, "pg": pg_store, "nats": nats_store,
        "signal_id": str(signal_id), "finding_id": str(finding_id),
        "source_id": source_id, "target_id": target_id,
        "analyst_id": analyst_id, "url": url, "payload": payload,
        "monkeypatch": monkeypatch,
    }
    try:
        yield w
    finally:
        async with pg_store.pool.acquire() as conn:
            await conn.execute("DELETE FROM analyst_outputs WHERE id = $1", finding_id)
            await conn.execute("DELETE FROM signals WHERE id = $1", signal_id)
        await descriptor_registry.stop()
        await nats_store.close()
        await pg_store.close()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://registry.test",
    )


def _headers(display_class: str) -> dict[str, str]:
    return READER_HEADERS if display_class == READER else OPERATOR_HEADERS


# ---------------------------------------------------------------------------
# The probes — one per reader transport. Each returns the text the transport
# EMITTED for a caller of ``display_class``.
# ---------------------------------------------------------------------------


async def _probe_rest(w: dict[str, Any], display_class: str) -> str:
    out: list[str] = []
    async with _client(w["app"]) as c:
        for path in (
            f"/api/v1/signals?source_id={w['source_id']}&fields=full",  # the full weight: the one that could leak
            f"/api/v1/findings?target_id={w['target_id']}&fields=full",  # the heaviest weight is the one that could leak
            f"/api/v1/lineage/signal/{w['signal_id']}",
            f"/api/v1/lineage/finding/{w['finding_id']}?direction=upstream",
        ):
            r = await c.get(path, headers=_headers(display_class))
            assert r.status_code == 200, (path, r.status_code, r.text[:300])
            out.append(r.text)
    return "\n".join(out)


async def _probe_export(w: dict[str, Any], display_class: str) -> str:
    out: list[str] = []
    async with _client(w["app"]) as c:
        for fmt in ("json", "markdown"):
            r = await c.post(
                "/api/v1/v3/export",
                json={"items": [{"kind": "finding", "id": w["finding_id"]}],
                      "format": fmt},
                headers=_headers(display_class),
            )
            assert r.status_code == 200, (fmt, r.status_code, r.text[:300])
            out.append(r.text)
    return "\n".join(out)


async def _probe_ws(w: dict[str, Any], display_class: str) -> str:
    from starlette.testclient import TestClient

    from legba.data.sources._contract import Signal

    subject = f"legba.signals.default.{w['source_id']}.text.raw"
    frame = Signal(
        signal_id=uuid.UUID(w["signal_id"]), source_id=w["source_id"],
        payload=w["payload"], canonical_url=w["url"],
        content_hash="b4hash", object_ref="cas:sha256/" + "ab" * 32,
    ).model_dump_json()
    with TestClient(w["app"]).websocket_connect(
        f"/api/v1/registry/events?filter={subject}",
        headers=_headers(display_class),
    ) as ws:
        assert ws.receive_json()["type"] == "subscribed"
        await w["nats"].nc.publish(subject, frame.encode("utf-8"))
        await w["nats"].nc.flush()
        for _ in range(5):
            msg = ws.receive_json()
            if msg.get("type") == "event":
                return json.dumps(msg)
    raise AssertionError("the events socket relayed no event")


async def _probe_sse(w: dict[str, Any], display_class: str) -> str:
    import legba.data.registry.consult_stream_api as stream_api
    from legba.data.registry._deps import principal_label
    from legba.runtime.substrate_corpus_readers import read_document

    label = (
        principal_label(OP_TOKEN) if display_class == OPERATOR
        else "bearer:reader000000"  # a credential that is not the operator's
    )
    w["monkeypatch"].setattr(
        stream_api, "_authorize_ws_token", lambda *a, **k: label,
    )
    doc = await read_document(None, "signals", w["pg"].pool, doc_id=w["signal_id"])
    request_id = uuid.uuid4().hex
    subject = f"legba.consult.steps.{request_id}"
    router = stream_api.build_consult_stream_router(w["deps"])
    endpoint = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == "/consult/stream/{request_id}"
    )
    resp = await endpoint(request_id, token="t", authorization=None)

    async def _publish() -> None:
        await asyncio.sleep(0.3)
        nc = w["nats"].nc
        await nc.publish(subject, json.dumps(
            {"type": "step", "phase": "act", "tool": "read_document",
             "result": doc}, default=str,
        ).encode())
        await nc.publish(subject, json.dumps({
            "type": "final", "final_source": "registry",
            "request_id": request_id, "status": "complete",
            "response": {"answer": "Closed.", "evidence": [
                {"signal_id": w["signal_id"], "title": TITLE,
                 "source_text": ARTICLE},
            ]},
        }).encode())
        await nc.flush()

    pub = asyncio.create_task(_publish())
    frames: list[bytes] = []

    async def _drain() -> None:
        async for chunk in resp.body_iterator:
            frames.append(chunk if isinstance(chunk, bytes) else chunk.encode())

    await asyncio.wait_for(_drain(), timeout=15.0)
    await pub
    return b"".join(frames).decode("utf-8")


async def _probe_consult_context(w: dict[str, Any], display_class: str) -> str:
    from legba.data.analysts.consult_on_demand import (
        ConsultOnDemandDeps,
        run_method,
    )
    from legba.runtime.corpus_read_projection import project_corpus_hits
    from legba.runtime.substrate_corpus_readers import read_document

    pool = w["pg"].pool
    hit = {"id": w["signal_id"], "score": 3.2, "source": {
        "title": TITLE, "canonical_url": w["url"], "source_id": w["source_id"],
        "archived_text": ARTICLE, "raw_body": w["payload"]["raw_body"],
        "summary": w["payload"]["summary"],
    }}

    class _Port:
        async def read_document(self, *, doc_id: str) -> dict[str, Any]:
            return await read_document(None, "signals", pool, doc_id=doc_id)

        async def search_corpus(self, **_: Any) -> dict[str, Any]:
            return project_corpus_hits([hit])

    final = json.dumps({
        "final": True, "answer": "The corridor stays closed.",
        "uncertainty": 0.3, "cited_refs": [w["signal_id"]],
        "unanswered_aspects": [],
    })
    script = [
        json.dumps({"tools": [
            {"tool": "read_document", "args": {"doc_id": w["signal_id"]}},
            {"tool": "search_corpus", "args": {"query": "corridor"}},
        ]}),
    ]

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content
            self.usage = None

    class _LLM:
        """Scripted planner: one tool round, then the answer. Records every
        message list it is handed — that IS the consult context."""

        subprovider = "vllm-test"

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def chat_complete(self, messages: Any, **kwargs: Any) -> Any:
            self.calls.append({"messages": messages, "system": kwargs.get("system")})
            return _Reply(script.pop(0) if script else final)

    llm = _LLM()
    await run_method(
        [{"question": "Is the corridor open?", "display_class": display_class}],
        {"analyst_id": "analyst.consult.b4test", "run_id": uuid.uuid4()},
        ConsultOnDemandDeps(llm=llm, substrate=_Port()),
    )
    assert len(llm.calls) >= 2, "the model was never handed the tool results"
    return json.dumps(llm.calls, default=str)


async def _probe_mcp(w: dict[str, Any], display_class: str) -> str:
    from mcp.types import CallToolRequest, CallToolRequestParams

    from legba.runtime.registry_client import RegistryHTTPClient
    from legba.ui.mcp_builtin_tools import builtin_tools
    from legba.ui.mcp_server import create_server

    # MCP is a reader surface by construction: the class is not an input. The
    # process holds the OPERATOR token, so the registry hands it full rows —
    # the projection is MCP's own.
    async with _client(w["app"]) as inner:
        client = RegistryHTTPClient(
            base_url="http://registry.test", token=OP_TOKEN, client=inner,
        )
        server = create_server(registry_client=client, builtins=builtin_tools())
        handler = server.request_handlers[CallToolRequest]
        out: list[str] = []
        for name, args in (
            ("substrate_signals", {"source_id": w["source_id"]}),
            ("substrate_findings", {"target_id": w["target_id"]}),
            ("lineage_walk", {"row_kind": "signal", "row_id": w["signal_id"]}),
        ):
            result = await handler(CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name=name, arguments=args),
            ))
            out.append(result.model_dump_json())
    return "\n".join(out)


async def _probe_a2a(w: dict[str, Any], display_class: str) -> str:
    from legba.data.outputs.a2a_skill import (
        A2ASkillRegistration,
        A2ASkillRegistry,
        TrustedKeyDirectory,
        register_a2a_skill_route,
    )

    pool = w["pg"].pool
    fetched: list[Any] = []

    async def _fetch_latest_outputs(*, analyst_ids, limit, target_filter):
        # The runtime's own query (dapr_host._fetch_latest_outputs).
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, kind, title, body, data, produced_at, target_id, "
                "analyst_id, analyst_version, schema_uri FROM analyst_outputs "
                "WHERE analyst_id = ANY($1::text[]) "
                "ORDER BY produced_at DESC LIMIT $2",
                list(analyst_ids), limit,
            )
        # JSON-normalised: the runtime's fetcher returns raw asyncpg values
        # (UUID, datetime) that the route's JSONResponse cannot serialise — a
        # pre-existing A2A defect, not this test's subject.
        out = json.loads(json.dumps([dict(r) for r in rows], default=str))
        fetched.extend(out)
        return out

    registry = A2ASkillRegistry()
    registry.register(A2ASkillRegistration(
        skill_id="intelligence.b4test", analyst_id=w["analyst_id"],
        analyst_version="ab" * 8, input_schema={"type": "object"},
        response_schema={"type": "object"}, auth_required=False,
    ))
    app = FastAPI()
    register_a2a_skill_route(
        app, registry=registry, identity=_identity(),
        fetch_latest_outputs=_fetch_latest_outputs,
        trusted_keys=TrustedKeyDirectory(),
    )
    async with _client(app) as c:
        r = await c.get("/a2a/skills/intelligence.b4test")
    assert r.status_code == 200, r.text[:300]
    # Non-vacuous: what the route was handed DID carry the planted text.
    assert SENTINEL in json.dumps(fetched, default=str)
    return r.text


#: transport → probe. Keyed by ``READER_TRANSPORTS`` (asserted below).
PROBES: dict[str, Callable[[dict[str, Any], str], Awaitable[str]]] = {
    "rest": _probe_rest,
    "consult_context": _probe_consult_context,
    "ws": _probe_ws,
    "sse": _probe_sse,
    "mcp": _probe_mcp,
    "a2a": _probe_a2a,
    "export": _probe_export,
}

#: Transports whose operator run carries the archived text at all (the
#: export never did; MCP and A2A have no operator class).
_OPERATOR_CARRIES_TEXT = ("rest", "consult_context", "ws", "sse")


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_every_reader_transport_has_a_probe() -> None:
    """ONE list: a transport in READER_TRANSPORTS without a probe fails here."""
    assert set(PROBES) == set(READER_TRANSPORTS), (
        "a reader transport has no sentinel probe (or a probe names no "
        f"transport): {sorted(set(PROBES) ^ set(READER_TRANSPORTS))}"
    )


@pytest.mark.parametrize("transport", sorted(READER_TRANSPORTS))
def test_every_transport_site_applies_the_policy(transport: str) -> None:
    """Each listed carrier module calls the policy (not a copy of it)."""
    text = (_SRC / READER_TRANSPORTS[transport]).read_text(encoding="utf-8")
    assert re.search(r"\bdisplay_(payload|projection)\(", text), (
        f"{transport}: {READER_TRANSPORTS[transport]} never calls the policy"
    )


#: Constructs that ARE a reader transport (a server-side socket, stream, MCP
#: tool surface, A2A route, export route, HTTP app) → the files allowed to
#: hold one. A new file matching a marker fails until it is listed — in
#: READER_TRANSPORTS, or here with the reason it is not a reader surface.
_TRANSPORT_MARKERS = (
    re.compile(r"\.websocket\("),
    re.compile(r"media_type\s*=\s*\"text/event-stream\""),
    re.compile(r"@server\.call_tool\("),
    re.compile(r"def register_a2a_skill_route\("),
    re.compile(r"\.(post|get)\(\s*\"/export"),
    re.compile(r"=\s*FastAPI\("),
)
_NON_READER_SITES = {
    # The registry app: every route is behind DisplayPolicyMiddleware (the
    # ``rest`` row); asserted to be mounted by the next test.
    "legba/data/registry/server.py",
    # The runtime app: the Dapr actor plane + /healthz; its only reader route
    # is the A2A surface, listed as ``a2a``.
    "legba/runtime/dapr_host.py",
}


def test_no_unlisted_transport_in_the_tree() -> None:
    allowed = set(READER_TRANSPORTS.values()) | _NON_READER_SITES
    unlisted: list[str] = []
    for path in sorted((_SRC / "legba").rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith(("#", "app = FastAPI()"))
        )
        if any(m.search(code) for m in _TRANSPORT_MARKERS) and rel not in allowed:
            unlisted.append(rel)
    assert not unlisted, (
        "a reader transport the display policy does not know about: "
        f"{unlisted} — list it in display_policy.READER_TRANSPORTS (and give "
        "it a probe here), or in _NON_READER_SITES with the reason"
    )


def test_the_registry_app_mounts_the_rest_choke_point() -> None:
    text = (_SRC / "legba/data/registry/server.py").read_text(encoding="utf-8")
    assert "app.add_middleware(DisplayPolicyMiddleware)" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", sorted(READER_TRANSPORTS))
async def test_the_sentinel_never_reaches_a_reader(world, transport: str) -> None:
    emitted = await PROBES[transport](world, READER)
    assert SENTINEL not in emitted, (
        f"{transport}: archived publisher text reached a READER"
    )
    # Non-vacuous: the row itself DID travel (its title or id is there) —
    # the sentinel is absent because it was projected away, not because the
    # pipe was empty.
    assert TITLE in emitted or world["signal_id"] in emitted, (
        f"{transport}: the probe carried nothing about the planted row"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", _OPERATOR_CARRIES_TEXT)
async def test_the_operator_run_of_each_probe_carries_the_text(
    world, transport: str,
) -> None:
    """The same probe, operator class: the sentinel IS there — the fixture is
    live and the transport is unchanged for operators."""
    emitted = await PROBES[transport](world, OPERATOR)
    assert SENTINEL in emitted, f"{transport}: operator lost the archived text"


@pytest.mark.asyncio
async def test_operator_keeps_the_full_row(world) -> None:
    """The operator's /signals row is the row: archived_text and raw_body
    verbatim, no projection marker, no withheld list."""
    async with _client(world["app"]) as c:
        r = await c.get(
            f"/api/v1/signals?source_id={world['source_id']}&fields=full",
            headers=OPERATOR_HEADERS,
        )
    assert r.status_code == 200
    (row,) = r.json()["data"]
    assert row["data"]["archived_text"] == ARTICLE
    assert row["data"]["raw_body"] == world["payload"]["raw_body"]
    assert "display_policy" not in row["data"]


@pytest.mark.asyncio
async def test_the_reader_row_is_the_projection(world) -> None:
    """A reader's /signals row: the envelope keeps its typed columns, ``data``
    is EXACTLY the projection (+ the geocoder's coordinates), and a NULL
    licence reads ``unreviewed``."""
    async with _client(world["app"]) as c:
        r = await c.get(
            f"/api/v1/signals?source_id={world['source_id']}&fields=full",
            headers=READER_HEADERS,
        )
    assert r.status_code == 200
    (row,) = r.json()["data"]
    assert set(row["data"]) == set(SIGNAL_PROJECTION_FIELDS) | {"geo"}
    assert row["data"]["license"] == LICENSE_UNREVIEWED
    assert row["data"]["license_class"] is None
    assert row["data"]["display_policy"] == READER
    assert row["data"]["canonical_url"] == world["url"]
    assert row["data"]["archived"] is True
    assert row["data"]["archive_sha256"] == "ab" * 32
    assert row["data"]["geo"] == {"lat": 48.5, "lon": 35.0}
    assert row["title"] == TITLE


# ---------------------------------------------------------------------------
# The function itself (no substrate)
# ---------------------------------------------------------------------------


def _row(**over: Any) -> dict[str, Any]:
    base = {
        "id": "00000000-0000-0000-0000-000000000001",
        "source_id": "source.x", "canonical_url": "https://p.example/a",
        "fetched_at": datetime(2026, 10, 8, tzinfo=timezone.utc),
        "content_hash": "h1", "object_ref": None,
        "payload": {"title": TITLE, "archived_text": ARTICLE},
    }
    base.update(over)
    return base


def test_operator_projection_is_the_row_itself() -> None:
    row = _row()
    assert display_projection(row, OPERATOR) is row
    assert display_payload(row, OPERATOR) is row


def test_reader_projection_fields_and_span() -> None:
    p = display_projection(_row(), READER, markers=[CITED])
    assert tuple(p) == SIGNAL_PROJECTION_FIELDS
    assert p["spans"] and all(len(s) <= 280 for s in p["spans"])
    assert CITED in p["spans"][0]
    assert SENTINEL not in json.dumps(p, default=str)
    assert p["fetched_at"] == "2026-10-08T00:00:00+00:00"


def test_a_marker_the_text_lacks_yields_no_span() -> None:
    assert display_projection(_row(), READER, markers=["not in it"])["spans"] == []


def test_recorded_license_is_read_where_it_exists() -> None:
    row = _row(payload={"title": TITLE, "license_class": "cc_by"})
    p = display_projection(row, READER)
    assert p["license_class"] == "cc_by" and p["license"] == "cc_by"


def test_an_unknown_class_fails_closed() -> None:
    out = display_payload({"archived_text": ARTICLE}, "admin?")
    assert SENTINEL not in json.dumps(out)
    assert out == {"display_withheld": ["archived_text"]}


def test_reader_payload_is_idempotent_and_names_what_it_withheld() -> None:
    page = {"data": [{"data": {"citations": [{
        "signal_id": "s1", "title": TITLE, "snippet": ARTICLE,
        "source_text": ARTICLE,
    }]}}]}
    once = display_payload(page, READER)
    assert display_payload(once, READER) == once
    cite = once["data"][0]["data"]["citations"][0]
    assert cite["display_withheld"] == ["source_text"]
    assert len(cite["snippet"]) <= 281 and SENTINEL not in cite["snippet"]


def test_jsonb_carried_as_text_is_projected_too() -> None:
    row = {"id": "f1", "data": json.dumps({"citations": [
        {"signal_id": "s1", "source_text": ARTICLE},
    ]})}
    assert SENTINEL not in json.dumps(display_payload(row, READER))


def test_class_for_principal_reads_the_role_stamp_first(monkeypatch) -> None:
    from legba.data.registry._deps import principal_label

    monkeypatch.setenv(API_TOKEN_ENV, OP_TOKEN)
    admin = principal_label(OP_TOKEN)
    assert class_for_principal(admin) == OPERATOR
    assert class_for_principal(admin, {"role": "read"}) == READER
    assert class_for_principal(admin, {"role": "reader-own-rows"}) == READER
    assert class_for_principal("bearer:someoneelse") == READER
    assert class_for_principal("bearer:someoneelse", {"principal_kind": "service"}) == OPERATOR
    monkeypatch.delenv(API_TOKEN_ENV)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")
    assert class_for_principal("anonymous") == OPERATOR


def test_the_policy_and_the_rest_choke_point_import_in_the_slim_image() -> None:
    """The registry ships a SLIM image: the policy leaf and the middleware must
    import with the analyst/runtime trees and the heavy deps poisoned."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client",
                        "trafilatura"):
            sys.modules[blocked] = None
        import legba.data.display_policy
        import legba.data.registry.display_class
        import legba.data.registry.server
        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the registry: %r" % leaked
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(_SRC), os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_b1_service_principal_is_operator_class() -> None:
    """B1 stamps ``request.state.principal = 'service'`` for the fleet; the
    display class must read that spelling as operator, or the runtime would
    receive projected registry JSON (merge follow-up, X2)."""
    from types import SimpleNamespace
    from legba.data.registry.display_class import class_for_principal

    assert class_for_principal(None, SimpleNamespace(principal="service")) == "operator"
    assert class_for_principal(None, SimpleNamespace(principal="operator")) == "operator"
    # B2's spelling of B3's user stamp.
    assert class_for_principal(None, SimpleNamespace(legba_role="read")) != "operator"
    # B1's gates return the audit actor, not the bare label: both principals
    # are operator class; anything else stays a reader.
    assert class_for_principal("operator:bearer:0123456789ab", None) == "operator"
    assert class_for_principal("service:bearer:0123456789ab", None) == "operator"
