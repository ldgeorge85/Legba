# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-3b Task 4 — the archiver's licence gate fails CLOSED for web-origin rows.

THE HOLE. ``evidence_archiver``'s licence gate fails OPEN: an unknown or unset
``license_class`` ARCHIVES. That posture was calibrated against a FINITE,
operator-reviewed set of ~48 active sources with zero anti-LLM EULAs. A search
provider returns arbitrary open-web domains — unbounded, unreviewed, and
certainly including anti-AI-walled publishers. Under the old rule such a hit
arrives with ``license_class = None``, takes the fail-open path, and its bytes
are archived.

THE FIX, scoped as narrowly as it can be: for rows whose ``retrieval_origin``
says web (migration 0112), an unset or ``"unknown"`` licence means the bytes
are NOT fetched and NOT archived. The skip is recorded with its OWN status
(``skipped_license_unreviewed``), its OWN counter, its OWN finding tag, and a
mention in the finding TITLE — counted and visible, never silent. Metadata
(URL, licence class, origin) is still recorded, because the ledger has to be
able to answer "what did fail-closed cost us?".

Registered sources are untouched, and the tests below assert that both as a
predicate and end-to-end against live SQL.

Pure tests (no DB): the gate predicate over the full matrix.
Ephemeral-DB tests (``migrated_pg`` + a local HTTP fixture allowlisted through
the SSRF guard): a web-origin unreviewed row is skipped with no bytes written,
a curated row with the same unknown licence still archives, an operator-
classified web row archives, and the ``inherit`` policy escape hatch works.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import evidence_archiver as ea
from legba.data.config import PostgresConfig
from legba.data.retrieval_origin import CURATED_SOURCE, web_search_origin
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "evidence_archiver"
WEB_ORIGIN = web_search_origin("search.searxng.local")

_HTML = (
    b"<!doctype html><html><head><title>Open web page</title></head><body>"
    b"<article><p>An unreviewed open-web domain published this. Whether we may "
    b"retain a copy of the bytes is exactly what the licence ledger does not "
    b"yet know.</p></article></body></html>"
)


# ---------------------------------------------------------------------------
# 1) The gate predicate — the full matrix, no DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "license_class,origin,expected",
    [
        # WEB origin + no affirmative verdict → withhold the bytes.
        (None, WEB_ORIGIN, True),
        ("unknown", WEB_ORIGIN, True),
        # WEB origin + an operator classification → archive normally
        # (the ledger-on-first-sight path).
        ("cc_by", WEB_ORIGIN, False),
        ("public_domain", WEB_ORIGIN, False),
        ("open_gov_attribution", WEB_ORIGIN, False),
        # CURATED origin — the fail-OPEN posture is untouched. This row is why
        # the change is a no-op for every source registered today.
        (None, None, False),
        ("unknown", None, False),
        (None, CURATED_SOURCE, False),
        ("unknown", CURATED_SOURCE, False),
    ],
)
def test_gate_matrix(license_class, origin, expected):
    assert ea.web_origin_license_unreviewed(license_class, origin) is expected


def test_inherit_policy_restores_fail_open_for_web_rows():
    """The documented escape hatch — a policy flip without a code change."""
    assert ea.web_origin_license_unreviewed(
        None, WEB_ORIGIN, fail_closed=False,
    ) is False


def test_module_default_is_fail_closed():
    assert ea.WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES is False
    assert ea.UNREVIEWED_LICENSE_CLASSES == frozenset({"unknown"})
    assert ea.STATUS_SKIPPED_LICENSE_UNREVIEWED == "skipped_license_unreviewed"


def test_the_two_skip_classes_stay_distinct():
    """`skipped_license` = a REVIEWED class forbade retention.
    `skipped_license_unreviewed` = we never reviewed this domain. Conflating
    them would make the ledger unable to price the fail-closed policy."""
    assert ea.license_forbids_retention(
        "anti_ai_walled", ea.FORBID_RETENTION_CLASSES,
    ) is True
    assert ea.web_origin_license_unreviewed("anti_ai_walled", WEB_ORIGIN) is False
    assert ea.STATUS_SKIPPED_LICENSE_UNREVIEWED != "skipped_license"


def test_the_origin_resolver_is_the_shared_one():
    from legba.data.retrieval_origin import resolve_retrieval_origin

    row = {"retrieval_origin": WEB_ORIGIN, "payload": {}}
    assert ea.resolve_signal_retrieval_origin(row) == resolve_retrieval_origin(row)


def test_counters_declare_the_new_classes():
    counters = ea._zero_counters()
    assert counters["skipped_license_unreviewed"] == 0
    assert counters["web_origin_examined"] == 0
    # The pre-existing counter is still there and still separate.
    assert counters["skipped_license"] == 0


def test_finding_surfaces_the_skip_in_the_title_and_tags():
    counters = dict(ea._zero_counters())
    counters.update({"examined": 3, "skipped_license_unreviewed": 2,
                     "web_origin_examined": 2})
    finding = ea._build_finding(counters)
    assert "web-unreviewed-skipped" in finding.title
    assert "web_origin_license_unreviewed" in finding.tags
    assert "skipped_license_unreviewed=2" in finding.body


# ---------------------------------------------------------------------------
# Local HTTP fixture (allowlisted through the SSRF guard per-test)
# ---------------------------------------------------------------------------


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        if self.path.startswith("/page"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_HTML)))
            self.end_headers()
            self.wfile.write(_HTML)
            return
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


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ea.ARCHIVE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv("LEGBA_EGRESS_ALLOW_HOSTS", "127.0.0.1")
    return tmp_path


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
    """Clear EVERY suite's archiver fixtures, not just this file's.

    The counters asserted below (`examined`, `archived`, …) are whole-run
    totals, so a sibling suite's leftover cited signals would silently join this
    run's candidate set and make the assertions order-dependent. Both archiver
    suites prefix their fixtures `test_`, so one predicate covers both.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM evidence_archive")
        await conn.execute("DELETE FROM signals WHERE source_id LIKE 'test\\_%'")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id LIKE 'test\\_%'"
        )
    yield


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool
        self.extras = {}


async def _run(pool, **opts):
    options = {
        "sub_handler": SUB, "analyst_id": SUB, "run_id": str(uuid4()),
        "per_host_delay_seconds": 0.0, "timeout_seconds": 10.0, **opts,
    }
    result = await ea.handle([], options, _Deps(pool))
    assert isinstance(result, AnalystMethodResult)
    return result.finding.data


async def _insert_signal(conn, url, *, origin=None, license_class=None):
    sid = uuid4()
    payload = {"title": "test signal"}
    if license_class is not None:
        payload["license_class"] = license_class
    await conn.execute(
        "INSERT INTO signals (id, source_id, canonical_url, payload, "
        "  raw_provenance, content_hash, retrieval_origin) "
        "VALUES ($1, 'test_r3b_src', $2, $3::jsonb, '{}'::jsonb, $4, $5)",
        sid, url, json.dumps(payload), uuid4().hex, origin,
    )
    return sid


async def _cite(conn, derived_from, *, score=0.85):
    fid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, analyst_id, derived_from, "
        "   schema_uri) "
        "VALUES ($1, 'finding', 'test finding', '', 0.9, '{}'::jsonb, "
        "        'test_r3b_unit', $2::uuid[], 'legba/finding/1.0.0')",
        fid, [str(s) for s in derived_from],
    )
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, analyst_id, schema_uri) "
        "VALUES ($1, 'critique', 'Faithfulness verify — test finding', '', 1.0, "
        "        $2::jsonb, 'test_r3b_verify', 'legba/critique/1.0.0')",
        uuid4(), json.dumps({"analyzed_output_id": str(fid), "overall_score": score}),
    )
    return fid


async def _sidecar(conn, sid):
    return await conn.fetchrow(
        "SELECT * FROM evidence_archive WHERE signal_id = $1", sid,
    )


# ---------------------------------------------------------------------------
# 2) End-to-end against live SQL
# ---------------------------------------------------------------------------


async def test_web_origin_unreviewed_licence_archives_no_bytes(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page", origin=WEB_ORIGIN)
        await _cite(conn, [sid])

    data = await _run(pg_pool)

    assert data["examined"] == 1
    assert data["web_origin_examined"] == 1
    assert data["skipped_license_unreviewed"] == 1
    assert data["archived"] == 0
    # NOT conflated with the reviewed-class skip.
    assert data["skipped_license"] == 0
    # No bytes anywhere on the archive volume.
    assert not [p for p in archive_env.rglob("*") if p.is_file()]
    assert data["bytes_stored"] == 0

    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
        signal = await conn.fetchrow(
            "SELECT object_ref, retention_class FROM signals WHERE id = $1", sid,
        )
    # Recorded, never silent — with the origin the gate evaluated.
    assert row["status"] == "skipped_license_unreviewed"
    assert row["retrieval_origin"] == WEB_ORIGIN
    assert row["license_class"] is None
    assert row["object_ref"] is None
    # METADATA retention is fine; only the bytes are withheld.
    assert row["fetched_url"] == f"{http_fixture}/page"
    assert "bytes NOT archived" in row["last_error"]
    assert "metadata kept" in row["last_error"]
    # The signal is untouched — nothing was stamped, nothing purged.
    assert signal["object_ref"] is None


async def test_web_origin_with_unknown_licence_is_also_withheld(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """`unknown` = reviewed and indeterminate. For an unbounded open-web domain
    set that is not an affirmative permission either."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(
            conn, f"{http_fixture}/page", origin=WEB_ORIGIN,
            license_class="unknown",
        )
        await _cite(conn, [sid])

    data = await _run(pg_pool)
    assert data["skipped_license_unreviewed"] == 1
    assert data["archived"] == 0
    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
    assert row["license_class"] == "unknown"


async def test_curated_source_with_the_same_unknown_licence_still_archives(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """THE regression guard: behaviour for existing registered sources is
    unchanged. Same licence, same URL — only the origin differs."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page", origin=None)
        await _cite(conn, [sid])

    data = await _run(pg_pool)

    assert data["archived"] == 1
    assert data["skipped_license_unreviewed"] == 0
    assert data["web_origin_examined"] == 0
    assert data["bytes_stored"] > 0

    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
    assert row["status"] == "archived"
    # Absence is honest: a curated row carries no origin, not a backfilled one.
    assert row["retrieval_origin"] is None


async def test_explicit_curated_origin_also_archives(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(
            conn, f"{http_fixture}/page", origin=CURATED_SOURCE,
        )
        await _cite(conn, [sid])
    data = await _run(pg_pool)
    assert data["archived"] == 1
    assert data["skipped_license_unreviewed"] == 0


async def test_operator_classified_web_domain_archives(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """Ledger-on-first-sight: once an operator classifies the domain, the web
    hit archives like anything else."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(
            conn, f"{http_fixture}/page", origin=WEB_ORIGIN, license_class="cc_by",
        )
        await _cite(conn, [sid])

    data = await _run(pg_pool)
    assert data["archived"] == 1
    assert data["skipped_license_unreviewed"] == 0
    assert data["web_origin_examined"] == 1
    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
    assert row["status"] == "archived"
    assert row["retrieval_origin"] == WEB_ORIGIN
    assert row["license_class"] == "cc_by"


async def test_reviewed_forbidding_class_still_takes_the_older_path(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """A web row whose class is REVIEWED-forbidding is `skipped_license`, not
    `skipped_license_unreviewed` — the ledger must not lose the distinction."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(
            conn, f"{http_fixture}/page", origin=WEB_ORIGIN,
            license_class="anti_ai_walled",
        )
        await _cite(conn, [sid])

    data = await _run(pg_pool)
    assert data["skipped_license"] == 1
    assert data["skipped_license_unreviewed"] == 0
    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
    assert row["status"] == "skipped_license"
    assert row["retrieval_origin"] == WEB_ORIGIN


async def test_inherit_option_reopens_the_gate_without_a_code_change(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page", origin=WEB_ORIGIN)
        await _cite(conn, [sid])

    data = await _run(pg_pool, web_origin_license_gate="inherit")
    assert data["archived"] == 1
    assert data["skipped_license_unreviewed"] == 0


async def test_an_unrecognised_gate_value_stays_fail_closed(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """A typo must not silently re-open the gate."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page", origin=WEB_ORIGIN)
        await _cite(conn, [sid])

    data = await _run(pg_pool, web_origin_license_gate="inherti")
    assert data["skipped_license_unreviewed"] == 1
    assert data["archived"] == 0


async def test_a_skipped_row_is_terminal_and_never_reburns_budget(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """Same discipline as skipped_license / skipped_size: recorded once,
    excluded from re-selection; a policy change re-evaluates it explicitly."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page", origin=WEB_ORIGIN)
        await _cite(conn, [sid])

    first = await _run(pg_pool)
    assert first["skipped_license_unreviewed"] == 1
    second = await _run(pg_pool)
    assert second["examined"] == 0
    assert second["skipped_license_unreviewed"] == 0


async def test_payload_stamped_origin_is_honoured_without_the_column(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """The ingest stamp lands on the payload; the column is its projection.
    Either one alone must gate."""
    async with pg_pool.acquire() as conn:
        sid = uuid4()
        await conn.execute(
            "INSERT INTO signals (id, source_id, canonical_url, payload, "
            "  raw_provenance, content_hash) "
            "VALUES ($1, 'test_r3b_src', $2, $3::jsonb, '{}'::jsonb, $4)",
            sid, f"{http_fixture}/page",
            json.dumps({"title": "t", "retrieval_origin": WEB_ORIGIN}),
            uuid4().hex,
        )
        await _cite(conn, [sid])

    data = await _run(pg_pool)
    assert data["skipped_license_unreviewed"] == 1
    assert data["archived"] == 0
