# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-1/P2-2 — the ``evidence_archiver`` deterministic sub-handler.

Pure tests (no DB): registration + TRACE_ONLY wiring, the CAS address format
(roundtrip + never-fabricated parse), the P2-2 license-gate posture (unknown
ARCHIVES, forbidden classes skip), content-addressed store (atomic write +
dedup hit), textual sniffing, and the corpus projection upgrade
(``archived_text`` in the doc + ``best_body`` preference).

Ephemeral-DB tests (``migrated_pg`` + a LOCAL HTTP fixture server, allowlisted
through the SSRF guard via ``LEGBA_EGRESS_ALLOW_HOSTS``): cited-only selection
(uncited/unverified never archived), fetch→store→hash→stamp end-to-end against
live SQL + the 0104 sidecar, the corpus DIRTY-MARKER contract (indexed_at
nulled + updated_at bumped in the same stamp), license-gate skip rows, the
egress guard (a private-address canonical_url is blocked + terminal), the size
cap, failed-fetch attempt caps, CAS dedup, the media leg, idempotency
(re-run examines nothing), and counter honesty.
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import evidence_archiver as ea
from legba.data.archive import cas_object_ref, cas_path, sha256_from_object_ref
from legba.data.config import PostgresConfig
from legba.data.opensearch import signal_to_doc
from legba.data.provenance.kinds import (
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    TRACE_ONLY,
)
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "evidence_archiver"

_HTML = (
    b"<!doctype html><html><head><title>Strait incident</title></head><body>"
    b"<article><p>A maritime incident occurred in the strait on Tuesday, "
    b"with two vessels reporting damage after an exchange of fire. "
    b"Authorities confirmed the closure of the shipping lane.</p>"
    b"<p>Officials said the investigation is ongoing and traffic will "
    b"resume once the area is declared safe.</p></article></body></html>"
)
_MEDIA = b"\x89PNG\r\n\x1a\nfakepngbytes-evidence-archiver-test"
_BIG = b"x" * 65536


# ---------------------------------------------------------------------------
# Registration + TRACE_ONLY wiring
# ---------------------------------------------------------------------------


def test_registered_trace_only_and_not_structural_exempt():
    assert SUB in SUB_HANDLERS, "evidence_archiver missing from SUB_HANDLERS"
    assert SUB_HANDLERS[SUB] is ea.handle
    # Side-effect sweep: the real product is the archive + sidecar rows — no
    # analyst_outputs receipt, so it must NOT join the FINDING-emitters set
    # the STRUCTURAL_VERIFY_EXEMPT drift guard asserts equality against.
    assert OUTPUT_KIND_BY_SUB_HANDLER[SUB] is TRACE_ONLY
    assert SUB not in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


async def test_synthetic_no_deps_zeroed_run():
    result = await run_method(
        [], {"sub_handler": SUB, "analyst_id": "ea", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["examined"] == 0
    assert data["archived"] == 0
    assert data["skipped_license"] == 0
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# CAS address format — one owner, never fabricated
# ---------------------------------------------------------------------------


def test_cas_object_ref_roundtrip_and_reject():
    digest = hashlib.sha256(b"bytes").hexdigest()
    ref = cas_object_ref(digest)
    assert ref == f"cas:sha256/{digest}"
    assert sha256_from_object_ref(ref) == digest
    # Never fabricate a hash from foreign/NULL/garbage refs.
    assert sha256_from_object_ref(None) is None
    assert sha256_from_object_ref("") is None
    assert sha256_from_object_ref("s3://bucket/key") is None
    assert sha256_from_object_ref("cas:sha256/nothex") is None
    assert sha256_from_object_ref("cas:sha256/" + "a" * 63) is None


def test_cas_path_shards_by_prefix(tmp_path: Path):
    digest = "ab" + "c" * 62
    assert cas_path(tmp_path, digest) == tmp_path / "ab" / digest


def test_store_bytes_atomic_and_dedup(tmp_path: Path):
    digest, ref, existed = ea._store_bytes(tmp_path, _HTML)
    assert not existed
    assert digest == hashlib.sha256(_HTML).hexdigest()
    stored = cas_path(tmp_path, digest)
    assert stored.read_bytes() == _HTML          # the BYTES are the archive
    assert ref == cas_object_ref(digest)
    # Same bytes again — dedup hit, never rewritten.
    digest2, ref2, existed2 = ea._store_bytes(tmp_path, _HTML)
    assert (digest2, ref2) == (digest, ref)
    assert existed2 is True
    # No temp litter left behind.
    assert not [p for p in stored.parent.iterdir() if p.name.startswith(".tmp.")]


# ---------------------------------------------------------------------------
# P2-2 license-gate posture
# ---------------------------------------------------------------------------


def test_license_gate_posture():
    forbid = ea.FORBID_RETENTION_CLASSES
    # Unknown/unset ARCHIVES (open-web quotation-for-evidence default).
    assert ea.license_forbids_retention(None, forbid) is False
    assert ea.license_forbids_retention("permissive_feed_unreviewed", forbid) is False
    assert ea.license_forbids_retention("cc_by", forbid) is False
    assert ea.license_forbids_retention("cc_nc", forbid) is False
    # The forbidden classes skip.
    for cls in ("anti_ai_walled", "tos_restrictive", "personal_use_only"):
        assert cls in forbid
        assert ea.license_forbids_retention(cls, forbid) is True


def test_resolve_license_class_payload_then_provenance():
    assert ea.resolve_license_class(
        {"payload": {"license_class": "cc_by"}, "raw_provenance": {}}
    ) == "cc_by"
    # Manual-batch provenance fallback (jsonb may arrive as str from asyncpg).
    assert ea.resolve_license_class(
        {"payload": "{}", "raw_provenance": {"provenance": {"license": "CC-BY-4.0"}}}
    ) == "CC-BY-4.0"
    assert ea.resolve_license_class({"payload": {}, "raw_provenance": {}}) is None


# ---------------------------------------------------------------------------
# Textual sniffing + corpus projection upgrade
# ---------------------------------------------------------------------------


def test_is_textual():
    assert ea._is_textual("text/html; charset=utf-8", b"") is True
    assert ea._is_textual("application/xhtml+xml", b"") is True
    # Declared non-text is authoritative — no sniffing past it.
    assert ea._is_textual("image/png", _HTML) is False
    assert ea._is_textual("application/pdf", b"%PDF") is False
    # No declared type → sniff the head.
    assert ea._is_textual(None, _HTML) is True
    assert ea._is_textual(None, _MEDIA) is False


def test_signal_to_doc_carries_archived_text_and_best_body_preference():
    """The S-10 depth fix: the archived FULL text upgrades the corpus doc —
    its own indexed field, AND best_body when no distilled brief exists; the
    distilled brief still wins best_body when present."""
    doc = signal_to_doc({
        "id": "sid",
        "payload": {"title": "t", "raw_body": "teaser", "archived_text": "FULL TEXT"},
    })
    assert doc["archived_text"] == "FULL TEXT"
    assert doc["best_body"] == "FULL TEXT"
    doc2 = signal_to_doc({
        "id": "sid",
        "payload": {
            "distilled_body": "brief",
            "archived_text": "FULL TEXT",
            "raw_body": "teaser",
        },
    })
    assert doc2["best_body"] == "brief"
    assert doc2["archived_text"] == "FULL TEXT"


# ---------------------------------------------------------------------------
# Local HTTP fixture server (allowlisted through the SSRF guard per-test)
# ---------------------------------------------------------------------------


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        routes = {
            "/article": (200, "text/html; charset=utf-8", _HTML),
            "/dup": (200, "text/html; charset=utf-8", _HTML),
            "/media.png": (200, "image/png", _MEDIA),
            "/big": (200, "application/octet-stream", _BIG),
        }
        if self.path in routes:
            status, ctype, body = routes[self.path]
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture(scope="module")
def http_fixture():
    server = HTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Ephemeral-DB rig
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM evidence_archive")
        await conn.execute("DELETE FROM signals WHERE source_id = 'test_p2_1_src'")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id LIKE 'test_p2_1_%'"
        )
    yield


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool
        self.extras = {}


async def _run(pool, **opts):
    options = {
        "sub_handler": SUB,
        "analyst_id": SUB,
        "run_id": str(uuid4()),
        "per_host_delay_seconds": 0.0,   # keep the suite fast
        "timeout_seconds": 10.0,
        **opts,
    }
    result = await ea.handle([], options, _Deps(pool))
    assert isinstance(result, AnalystMethodResult)
    return result.finding.data


async def _insert_signal(
    conn, url, *, media_ref=None, payload=None, raw_provenance=None,
):
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, canonical_url, media_ref, payload, "
        "  raw_provenance, content_hash) "
        "VALUES ($1, 'test_p2_1_src', $2, $3, $4::jsonb, $5::jsonb, $6)",
        sid, url, media_ref,
        json.dumps(payload or {"title": "test signal"}),
        json.dumps(raw_provenance or {}),
        uuid4().hex,
    )
    return sid


async def _insert_finding(conn, derived_from, *, verified=True, score=0.85):
    fid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, analyst_id, derived_from, "
        "   schema_uri) "
        "VALUES ($1, 'finding', 'test finding', '', 0.9, '{}'::jsonb, "
        "        'test_p2_1_unit', $2::uuid[], 'legba/finding/1.0.0')",
        fid, [str(s) for s in derived_from],
    )
    if verified:
        await conn.execute(
            "INSERT INTO analyst_outputs "
            "  (id, kind, title, body, confidence, data, analyst_id, schema_uri) "
            "VALUES ($1, 'critique', 'Faithfulness verify — test finding', '', 1.0, "
            "        $2::jsonb, 'test_p2_1_verify', 'legba/critique/1.0.0')",
            uuid4(),
            json.dumps({"analyzed_output_id": str(fid), "overall_score": score}),
        )
    return fid


async def _sidecar(conn, sid):
    return await conn.fetchrow(
        "SELECT * FROM evidence_archive WHERE signal_id = $1", sid,
    )


async def _signal(conn, sid):
    return await conn.fetchrow(
        "SELECT object_ref, retention_class, payload, indexed_at, updated_at "
        "FROM signals WHERE id = $1", sid,
    )


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """Archive root → tmp; the LOCAL fixture host allowlisted through the SSRF
    guard (exact-hostname permit — everything else private stays blocked)."""
    monkeypatch.setenv(ea.ARCHIVE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv("LEGBA_EGRESS_ALLOW_HOSTS", "127.0.0.1")
    return tmp_path


# ---------------------------------------------------------------------------
# End-to-end: cited-only selection → fetch → store → hash → stamp → sidecar
# ---------------------------------------------------------------------------


async def test_archives_cited_verified_only(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        cited = await _insert_signal(conn, f"{http_fixture}/article")
        uncited = await _insert_signal(conn, f"{http_fixture}/article")
        unverified = await _insert_signal(conn, f"{http_fixture}/article")
        await _insert_finding(conn, [cited], verified=True)
        await _insert_finding(conn, [unverified], verified=False)
        low_scored = await _insert_signal(conn, f"{http_fixture}/article")
        await _insert_finding(conn, [low_scored], verified=True, score=0.2)
        pre = await _signal(conn, cited)

    data = await _run(pg_pool)
    assert data["examined"] == 1          # ONLY the verified-cited signal
    assert data["archived"] == 1
    assert data["fetch_failed"] == 0

    digest = hashlib.sha256(_HTML).hexdigest()
    stored = cas_path(archive_env, digest)
    assert stored.read_bytes() == _HTML   # content-addressed original bytes

    async with pg_pool.acquire() as conn:
        row = await _signal(conn, cited)
        assert row["object_ref"] == cas_object_ref(digest)
        assert sha256_from_object_ref(row["object_ref"]) == digest
        # Archived citation can never be TTL-purged out from under its archive.
        assert row["retention_class"] == "evidence_hold"
        # S-10: the archived FULL text landed in the payload…
        payload = json.loads(row["payload"])
        assert "maritime incident" in payload["archived_text"]
        # …and the corpus DIRTY-MARKER contract was honored: indexed_at nulled
        # AND updated_at bumped IN THE SAME UPDATE (both load-bearing — see
        # corpus_indexer's contract).
        assert row["indexed_at"] is None
        assert row["updated_at"] > pre["updated_at"]

        side = await _sidecar(conn, cited)
        assert side["status"] == "archived"
        assert side["sha256"] == digest
        assert side["size_bytes"] == len(_HTML)
        assert side["text_extracted"] is True
        assert side["archived_at"] is not None

        # The uncited / unverified / below-floor signals were never touched.
        for sid in (uncited, unverified, low_scored):
            other = await _signal(conn, sid)
            assert other["object_ref"] is None
            assert await _sidecar(conn, sid) is None

    # Idempotency: everything terminal → the re-run examines nothing.
    data2 = await _run(pg_pool)
    assert data2["examined"] == 0
    assert data2["archived"] == 0


async def test_license_gate_skips_recorded_never_silent(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        walled = await _insert_signal(
            conn, f"{http_fixture}/article",
            payload={"title": "walled", "license_class": "anti_ai_walled"},
        )
        open_web = await _insert_signal(
            conn, f"{http_fixture}/article",
            payload={"title": "open", "license_class": "cc_by"},
        )
        await _insert_finding(conn, [walled, open_web], verified=True)

    data = await _run(pg_pool)
    assert data["examined"] == 2
    assert data["skipped_license"] == 1
    assert data["archived"] == 1

    async with pg_pool.acquire() as conn:
        side = await _sidecar(conn, walled)
        assert side["status"] == "skipped_license"
        assert side["license_class"] == "anti_ai_walled"
        assert (await _signal(conn, walled))["object_ref"] is None
        # The archivable one recorded ITS class too (policy-flip audit trail).
        ok = await _sidecar(conn, open_web)
        assert ok["status"] == "archived"
        assert ok["license_class"] == "cc_by"

    # Skip rows never re-burn budget.
    assert (await _run(pg_pool))["examined"] == 0


async def test_egress_guard_blocks_private_target_terminally(
    pg_pool, clean_slate, archive_env,
):
    async with pg_pool.acquire() as conn:
        # TEST-NET-1 — non-public, refused by the guard BEFORE any connect.
        private = await _insert_signal(conn, "http://192.0.2.1/secret")
        await _insert_finding(conn, [private], verified=True)

    data = await _run(pg_pool, max_attempts=3)
    assert data["examined"] == 1
    assert data["fetch_failed"] == 1
    assert data["egress_blocked"] == 1
    assert data["archived"] == 0

    async with pg_pool.acquire() as conn:
        side = await _sidecar(conn, private)
        assert side["status"] == "failed"
        assert "egress blocked" in side["last_error"]
        # Terminal: attempts capped immediately — never retried.
        assert side["attempts"] >= 3
        assert (await _signal(conn, private))["object_ref"] is None
    assert (await _run(pg_pool, max_attempts=3))["examined"] == 0


async def test_size_cap_skips_and_records(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        big = await _insert_signal(conn, f"{http_fixture}/big")
        await _insert_finding(conn, [big], verified=True)

    data = await _run(pg_pool, max_object_bytes=1024)
    assert data["examined"] == 1
    assert data["skipped_size"] == 1
    assert data["archived"] == 0
    async with pg_pool.acquire() as conn:
        side = await _sidecar(conn, big)
        assert side["status"] == "skipped_size"
        assert (await _signal(conn, big))["object_ref"] is None
    # Recorded once — never re-burned at the same cap.
    assert (await _run(pg_pool, max_object_bytes=1024))["examined"] == 0


async def test_failed_fetch_attempt_capped(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        gone = await _insert_signal(conn, f"{http_fixture}/nope")
        await _insert_finding(conn, [gone], verified=True)

    data = await _run(pg_pool, max_attempts=2)
    assert data["fetch_failed"] == 1
    async with pg_pool.acquire() as conn:
        assert (await _sidecar(conn, gone))["attempts"] == 1
    # Retry once more (attempts → 2 = the cap)…
    data2 = await _run(pg_pool, max_attempts=2)
    assert data2["examined"] == 1
    assert data2["fetch_failed"] == 1
    async with pg_pool.acquire() as conn:
        assert (await _sidecar(conn, gone))["attempts"] == 2
    # …then excluded: the budget is bounded, not burned forever.
    assert (await _run(pg_pool, max_attempts=2))["examined"] == 0


async def test_cas_dedup_and_media_leg(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        one = await _insert_signal(
            conn, f"{http_fixture}/article",
            media_ref=f"{http_fixture}/media.png",
        )
        two = await _insert_signal(conn, f"{http_fixture}/dup")  # same bytes
        await _insert_finding(conn, [one, two], verified=True)

    data = await _run(pg_pool)
    assert data["examined"] == 2
    assert data["archived"] == 2
    # /article and /dup serve IDENTICAL bytes → one CAS object, one dedup hit.
    assert data["already_present"] == 1
    assert data["media_archived"] == 1
    # bytes_stored counts NEW bytes only (one html object + one media object).
    assert data["bytes_stored"] == len(_HTML) + len(_MEDIA)

    html_digest = hashlib.sha256(_HTML).hexdigest()
    media_digest = hashlib.sha256(_MEDIA).hexdigest()
    assert cas_path(archive_env, html_digest).exists()
    assert cas_path(archive_env, media_digest).read_bytes() == _MEDIA

    async with pg_pool.acquire() as conn:
        side_one = await _sidecar(conn, one)
        assert side_one["media_sha256"] == media_digest
        assert side_one["media_object_ref"] == cas_object_ref(media_digest)
        assert side_one["media_size_bytes"] == len(_MEDIA)
        # Both signals point at the SAME content address.
        for sid in (one, two):
            assert (await _signal(conn, sid))["object_ref"] == cas_object_ref(
                html_digest
            )


async def test_missing_archive_root_noops_loudly(
    pg_pool, clean_slate, http_fixture, monkeypatch,
):
    """An unusable archive root is a LOUD no-op tick, never a silent drop and
    never a half-archived row."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/article")
        await _insert_finding(conn, [sid], verified=True)
    monkeypatch.setenv("LEGBA_EGRESS_ALLOW_HOSTS", "127.0.0.1")
    monkeypatch.setenv(ea.ARCHIVE_ROOT_ENV, "/proc/definitely/not/writable")

    data = await _run(pg_pool)
    assert data["skipped_no_root"] == 1
    assert data["examined"] == 0
    async with pg_pool.acquire() as conn:
        assert (await _signal(conn, sid))["object_ref"] is None
        assert await _sidecar(conn, sid) is None
