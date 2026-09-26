# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/contentions`` — the contrary-evidence pass's read surface (7a).

Three things are pinned here, and the middle one is the reason the route exists
in this shape at all:

  * the real binding path — the router mounted on a FastAPI app over a migrated
    test DB, reached through the ASGI transport, like the receipt-anchors and
    staleness-debt precedents;
  * the CONTRACT. A stance is a statement about a RETRIEVAL and never a verdict
    on the claim, and the route says so in a field on every response rather than
    leaving a client to infer it from a column name. The uncalibrated
    ``negation`` derivation IS served here — this is the human surface where it
    belongs — and a client can tell it apart from the calibrated one without
    parsing prose;
  * the honest empty state — an unreadable table is ``[]`` at HTTP 200, never a
    500. A reader surface must not fall over because an optional sidecar is
    missing, and "no records" is the same answer a genuinely empty table gives.

Plus the slim-image import guard: this module ships in the REGISTRY image, which
does not carry the analyst-runtime dependency stack. A deferred import would not
help — deferring moves WHEN the graph is walked, never HOW FAR.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.contentions_api import (
    CONTENTIONS_ROUTE_VERSION,
    build_contentions_router,
)
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

_ROUTE = "/api/v1/v3/contentions"
_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")

_CLAIM = "The Strait of Hormuz remains effectively shut to commercial traffic."


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig, clean_tables):
    os.environ.pop(API_TOKEN_ENV, None)
    await clean_tables("claim_contentions")

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    identity = SigningIdentity(
        signing_key=SigningKey(b"7a-contentions-route-test-seed1!"[:32]),
        signer_did="did:legba:registry:7a-contentions-test",
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
    app.include_router(build_contentions_router(deps), prefix="/api/v1/v3")
    yield app, pg_store
    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


async def _seed(pg_store, *, desk: str, stance: str = "contradicts",
                derivation: str = "polarity", live: bool = True,
                finding_id=None, block_ordinal: int = 1,
                target: str | None = None,
                independent_pages: int | None = 2,
                host_class: str | None = "unknown",
                page_published_at: str | None = "2026-09-20",
                subject_overlap: int | None = 4) -> str:
    claim_id = f"{stance}-{uuid4().hex}"
    # An EXPIRED row is one whose as_of is in the past — `expires_at` is always
    # `as_of + ttl`, and the schema's `claim_contentions_window` CHECK refuses
    # any other shape. Seeding it the way the handler would is the point.
    now = (
        datetime.now(timezone.utc) if live
        else datetime.now(timezone.utc) - timedelta(days=30)
    )
    expires = now + timedelta(days=7)
    refs = (
        [{"url": "https://counter.example/a", "sha256": "ab" * 32,
          "chars": 120, "published_at": "2026-09-20", "extracted": True,
          "fetched_at": now.isoformat(), "status_code": 200,
          "stance": stance, "quote": "the strait reopened on Tuesday",
          "host_class": host_class or "unknown",
          "page_published_at": page_published_at,
          "subject_overlap": subject_overlap or 0, "fence": ""}]
        if stance in ("contradicts", "qualifies") else []
    )
    async with pg_store.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO claim_contentions (
                claim_id, claim_text, finding_id, block_ordinal, target_id,
                desk_key, analyst_id, query, query_source, query_novel_tokens,
                stance, derivation, reason, statement, rung, refs,
                pipeline_version, retrieved_at, as_of, as_of_day, expires_at,
                host_class, page_published_at, subject_overlap,
                independent_pages
            ) VALUES (
                $1, $2, $3, $14, $4, $5, $6, $7, 'polarity', 3, $8, $9, '', $10,
                'searxng', $11::jsonb, '2026-09/7a.2', $12::timestamptz,
                $12::timestamptz, ($12::timestamptz)::date, $13,
                $15, $16::date, $17, $18
            )
            """,
            claim_id, _CLAIM, finding_id or uuid4(), target or desk, desk,
            "country_composition",
            "hormuz reopened operating", stance, derivation,
            ("retrieved counter-evidence; Not adjudicated."
             if stance in ("contradicts", "qualifies") else ""),
            json.dumps(refs), now, expires, int(block_ordinal),
            host_class,
            (date.fromisoformat(page_published_at)
             if page_published_at else None),
            subject_overlap, independent_pages,
        )
    return claim_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_table_reads_honest_empty(client) -> None:
    r = await client.get(_ROUTE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == CONTENTIONS_ROUTE_VERSION
    assert body["contentions"] == []
    assert body["stances"] == [
        "contradicts", "qualifies", "none_found", "search_failed",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_route_serves_a_record_with_its_refs_and_stamps(
    api_app, client,
) -> None:
    _, pg_store = api_app
    desk = f"country_watch_{uuid4().hex[:6]}"
    claim_id = await _seed(pg_store, desk=desk)

    r = await client.get(_ROUTE, params={"scope": desk})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == desk
    assert len(body["contentions"]) == 1
    row = body["contentions"][0]
    assert row["claim_id"] == claim_id
    assert row["stance"] == "contradicts"
    assert row["derivation"] == "polarity"
    assert row["query"] == "hormuz reopened operating"
    assert row["query_novel_tokens"] == 3
    assert row["rung"] == "searxng"
    assert row["live"] is True
    # The counter-ref is a page the platform HOLDS: a url, a hash and the date
    # the page itself stated. A search snippet never gets this far.
    assert len(row["refs"]) == 1
    assert row["refs"][0]["url"] == "https://counter.example/a"
    assert len(row["refs"][0]["sha256"]) == 64
    assert row["refs"][0]["published_at"] == "2026-09-20"
    # No signal was written by the pass, so the link count is honestly zero.
    assert row["linked_signals"] == 0
    # THE FOUR FENCES' NUMBERS (0222), on the row and on the ref it decided.
    assert row["host_class"] == "unknown"
    assert row["page_published_at"] == "2026-09-20"
    assert row["subject_overlap"] == 4
    assert row["independent_pages"] == 2
    assert row["refs"][0]["host_class"] == "unknown"
    assert row["refs"][0]["page_published_at"] == "2026-09-20"
    assert row["refs"][0]["fence"] == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unmeasured_fence_column_serves_null_never_zero(
    api_app, client,
) -> None:
    """A row written before migration 0222 measured none of the four fences.
    ``null`` is "not measured"; ``0`` would read as "measured, and the answer
    was nothing" — which is a different and false claim. Absence renders as
    absence, here as everywhere on this platform."""
    _, pg_store = api_app
    desk = f"country_watch_{uuid4().hex[:6]}"
    await _seed(
        pg_store, desk=desk, stance="none_found", derivation="none",
        independent_pages=None, host_class=None, page_published_at=None,
        subject_overlap=None,
    )
    row = (await client.get(_ROUTE, params={"scope": desk})).json()[
        "contentions"
    ][0]
    assert row["host_class"] is None
    assert row["page_published_at"] is None
    assert row["subject_overlap"] is None
    assert row["independent_pages"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scope_matches_the_target_or_the_desk_and_says_which(
    api_app, client,
) -> None:
    """``scope`` accepts EITHER handle, and the response names the one the
    returned rows matched on.

    Every other reader surface on this platform means the TARGET by ``scope``;
    this route shipped meaning only ``desk_key``, so an operator asking for
    ``country_g20_in`` — the handle on every other surface — got an empty list
    with no way to tell "nothing found" from "wrong handle". Nothing about
    either string says which kind it is, which is why the answer is read off
    the rows rather than guessed.
    """
    _, pg_store = api_app
    analyst = f"energy_security_{uuid4().hex[:6]}"
    target = f"country_g20_{uuid4().hex[:6]}"
    claim_id = await _seed(pg_store, desk=analyst, target=target)

    by_target = (await client.get(_ROUTE, params={"scope": target})).json()
    assert [r["claim_id"] for r in by_target["contentions"]] == [claim_id]
    assert by_target["scope"] == target
    assert by_target["scope_field"] == "target_id"

    by_desk = (await client.get(_ROUTE, params={"scope": analyst})).json()
    assert [r["claim_id"] for r in by_desk["contentions"]] == [claim_id]
    assert by_desk["scope_field"] == "desk_key"

    # A country desk where the two handles are the SAME string — the common
    # case, and the reason a shape heuristic would have been wrong.
    both = f"country_watch_{uuid4().hex[:6]}"
    await _seed(pg_store, desk=both, target=both)
    same = (await client.get(_ROUTE, params={"scope": both})).json()
    assert len(same["contentions"]) == 1
    assert same["scope_field"] == "both"

    # A scope that matched nothing names no column: saying "target_id" about a
    # filter that returned nothing would assert something the route did not do.
    missing = (await client.get(_ROUTE, params={"scope": "no_such_unit"})).json()
    assert missing["contentions"] == []
    assert missing["scope_field"] is None
    # ...and fleet-wide asks for no scope at all.
    fleet = (await client.get(_ROUTE)).json()
    assert fleet["scope"] is None and fleet["scope_field"] is None
    assert len(fleet["contentions"]) >= 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_contract_note_says_a_stance_is_not_a_verdict(client) -> None:
    """The one sentence a client must not have to infer.

    'contradicts' means a page this platform fetched states the opposite. It
    does not mean the claim is false, and the response carries that in a field
    rather than in documentation somebody may not read.
    """
    body = (await client.get(_ROUTE)).json()
    note = body["note"]
    assert "never a verdict" in note
    assert "does not mean the claim is false" in note
    assert "derivation='polarity'" in note


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_uncalibrated_fallback_is_served_here_and_is_labelled(
    api_app, client,
) -> None:
    """The negation derivation is withheld from COMPOSITIONS and served HERE —
    the human surface, where the counter-ref is one click away. A client can
    tell the two apart from a field, never from prose."""
    _, pg_store = api_app
    desk = f"country_watch_{uuid4().hex[:6]}"
    await _seed(pg_store, desk=desk, derivation="negation")
    rows = (await client.get(_ROUTE, params={"scope": desk})).json()["contentions"]
    assert [r["derivation"] for r in rows] == ["negation"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_records_are_labelled_not_hidden(api_app, client) -> None:
    """Silently hiding expired rows would make a pass that STOPPED RUNNING look
    exactly like a week with nothing to contend — the 08-12 shape, on a reader
    surface. So ``live`` is rendered, and ``live_only`` is the client's choice."""
    _, pg_store = api_app
    desk = f"country_watch_{uuid4().hex[:6]}"
    await _seed(pg_store, desk=desk, live=False)

    shown = (await client.get(_ROUTE, params={"scope": desk})).json()
    assert len(shown["contentions"]) == 1
    assert shown["contentions"][0]["live"] is False

    filtered = (await client.get(
        _ROUTE, params={"scope": desk, "live_only": True},
    )).json()
    assert filtered["contentions"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_claim_drill_and_the_stance_filter(api_app, client) -> None:
    _, pg_store = api_app
    desk = f"country_watch_{uuid4().hex[:6]}"
    contradicts = await _seed(pg_store, desk=desk)
    await _seed(pg_store, desk=desk, stance="none_found", derivation="none")

    by_claim = (await client.get(
        _ROUTE, params={"claim_id": contradicts},
    )).json()["contentions"]
    assert [r["claim_id"] for r in by_claim] == [contradicts]

    by_stance = (await client.get(
        _ROUTE, params={"scope": desk, "stance": "none_found"},
    )).json()["contentions"]
    assert [r["stance"] for r in by_stance] == ["none_found"]
    # A none_found row carries no refs and does not pretend to.
    assert by_stance[0]["refs"] == []
    assert by_stance[0]["statement"] == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_stance_is_ignored_rather_than_erroring(
    api_app, client,
) -> None:
    """An out-of-vocabulary filter returns the unfiltered page, never a 500 and
    never an empty list that reads as 'nothing matched'."""
    _, pg_store = api_app
    desk = f"country_watch_{uuid4().hex[:6]}"
    await _seed(pg_store, desk=desk)
    rows = (await client.get(
        _ROUTE, params={"scope": desk, "stance": "CONTRADICTED"},
    )).json()["contentions"]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# The slim-image import guard
# ---------------------------------------------------------------------------


def _probe(body: str) -> subprocess.CompletedProcess:
    script = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client",
                        "trafilatura"):
            sys.modules[blocked] = None
        """
    ) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )


def test_the_contentions_route_imports_in_the_slim_image() -> None:
    """The registry ships a SLIM image. A route module that pulls
    ``legba.data.analysts`` (which drags pycountry and more) 500s live even
    when the import is deferred inside a function — deferring moves WHEN the
    graph is walked, never HOW FAR. So the route carries its own SQL rather
    than importing the handler's store, and this is the proof."""
    result = _probe(
        """
        import legba.data.registry.contentions_api as api

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the registry: %r" % leaked
        assert api.build_contentions_router is not None
        assert api.CONTENTIONS_ROUTE_VERSION
        print("OK")
        """
    )
    assert result.returncode == 0, (
        f"slim import failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_the_registry_server_still_imports_slim_with_the_route_mounted(
) -> None:
    """The mount point too: ``server.py`` is what the slim image actually
    runs, and a leaf that imports cleanly on its own can still be dragged in
    behind a bad edge at the mount."""
    result = _probe(
        """
        import legba.data.registry.server as server

        leaked = sorted(
            m for m in sys.modules if m.startswith("legba.data.analysts")
        )
        assert not leaked, "analysts reachable from registry.server: %r" % leaked
        assert server.create_app is not None
        print("OK")
        """
    )
    assert result.returncode == 0, (
        f"slim import failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_inspector_join_is_the_read_id_and_the_block_ordinal(
    api_app, client,
) -> None:
    """The chip's own join, and it is NOT the composition read's join.

    The Inspector is rendering one published read, and
    ``|blocks| == |citations| == |distinct markers|`` makes block N the citation
    ``[[ref:N]]`` — so (finding_id, block_ordinal) puts a record beside the
    exact sentence it is about with no hashing in the browser. (The COMPOSITION
    tension rule joins on ``claim_id`` instead, because it is building a new
    row and needs a key that survives the cycle; migration 0221 §1 spells out
    the three joins and which surface uses which.)
    """
    _, pg_store = api_app
    desk = f"country_watch_{uuid4().hex[:6]}"
    read_id = uuid4()
    first = await _seed(pg_store, desk=desk, finding_id=read_id,
                        block_ordinal=2)
    await _seed(pg_store, desk=desk, finding_id=uuid4(), block_ordinal=2)

    rows = (await client.get(
        _ROUTE, params={"finding_id": str(read_id)},
    )).json()["contentions"]
    assert [r["claim_id"] for r in rows] == [first]
    assert rows[0]["block_ordinal"] == 2
    assert rows[0]["finding_id"] == str(read_id)
