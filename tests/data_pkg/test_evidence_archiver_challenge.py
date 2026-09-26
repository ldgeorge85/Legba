# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""(a′) — the archiver records a BLOCK as a block, end to end against live SQL.

Drives the real ``evidence_archiver.handle`` sweep (candidate SQL, licence
gates, CAS write, sidecar upsert, signal stamp) against a migrated Postgres and
a local HTTP fixture, exactly as the R-3b suite does. Nothing about the sweep
is mocked; only the origin server is ours.

THE DEFECT under test. A Cloudflare/DataDome interstitial is 5–13 kB, and the
wall detector's 500-char gate could not reach it. The page therefore landed as
``archived`` with a failed extraction — a row that says "we hold this page"
and carries no article, which downstream reads as *the web had nothing*. Two
outcomes now exist instead:

  * a **2xx carrying an interstitial** → ``evidence_archive.status =
    'blocked_challenge'`` (migration 0196), bytes still stored, no derived
    text, the tell in ``last_error``;
  * an **edge 401/403/429** → still ``failed``, because that may clear if an
    operator turns ``LEGBA_FETCH_IMPERSONATE`` on and only ``failed`` rows are
    re-attempted — but now with ``blocked_by_challenge: http_403`` in
    ``last_error`` and its own counter.

And the thing that must NOT change: a genuine article still archives with its
text, and the counters that existed before keep their meaning.
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
from legba.data.analysts.deterministic_handlers._challenge_detect import (
    BLOCKED_BY_CHALLENGE,
)
from legba.data.config import PostgresConfig
from legba.runtime.analyst_method import AnalystMethodResult

pytestmark = [pytest.mark.asyncio]

SUB = "evidence_archiver"

#: Cloudflare's interstitial, padded past the old 500-char wall gate so the
#: LENGTH dimension of the defect is exercised and not assumed away.
_CHALLENGE_HTML = (
    b"<!doctype html><html><head><title>Just a moment...</title></head><body>"
    b"<div id='cf-wrapper'><h1>example.test</h1>"
    b"<p>Verifying you are human. This may take a few seconds.</p>"
    b"<p>Enable JavaScript and cookies to continue.</p></div>"
    b"<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'>"
    b"</script>" + b"<!-- " + b"0" * 9_000 + b" -->"
    b"</body></html>"
)

_ARTICLE_HTML = (
    b"<!doctype html><html><head><title>Fuel embargo</title></head><body>"
    b"<article><p>Fuel deliveries into the capital fell by four fifths this "
    b"week as roadblocks held on the three southern corridors, according to "
    b"two haulage operators reached by telephone.</p><p>The importers' "
    b"association said reserves would last eleven days at current rationing "
    b"levels.</p></article></body></html>"
)


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802 - stdlib API
        if self.path.startswith("/challenge-200"):
            body, code = _CHALLENGE_HTML, 200
        elif self.path.startswith("/challenge-403"):
            body, code = _CHALLENGE_HTML, 403
        else:
            body, code = _ARTICLE_HTML, 200
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                       # quiet
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
    # The SHIPPED default: no impersonation. (a′) is independent of (a).
    monkeypatch.delenv("LEGBA_FETCH_IMPERSONATE", raising=False)
    return tmp_path


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """Same predicate the sibling archiver suites use — the counters asserted
    here are whole-run totals, so a leftover fixture row would join this run's
    candidate set."""
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
    return result.finding


async def _insert_signal(conn, url):
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, canonical_url, payload, "
        "  raw_provenance, content_hash) "
        "VALUES ($1, 'test_chal_src', $2, $3::jsonb, '{}'::jsonb, $4)",
        sid, url, json.dumps({"title": "test signal"}), uuid4().hex,
    )
    return sid


async def _cite(conn, derived_from, *, score=0.85):
    fid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, analyst_id, derived_from, "
        "   schema_uri) "
        "VALUES ($1, 'finding', 'test finding', '', 0.9, '{}'::jsonb, "
        "        'test_chal_unit', $2::uuid[], 'legba/finding/1.0.0')",
        fid, [str(s) for s in derived_from],
    )
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, analyst_id, schema_uri) "
        "VALUES ($1, 'critique', 'Faithfulness verify — test finding', '', 1.0, "
        "        $2::jsonb, 'test_chal_verify', 'legba/critique/1.0.0')",
        uuid4(), json.dumps({"analyzed_output_id": str(fid), "overall_score": score}),
    )


async def _sidecar(conn, sid):
    return await conn.fetchrow(
        "SELECT * FROM evidence_archive WHERE signal_id = $1", sid,
    )


# ---------------------------------------------------------------------------


async def test_a_2xx_interstitial_is_recorded_as_blocked_not_archived(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """THE false-absence case. 200 + a 9 kB Cloudflare page: the bytes are
    real, the article is not, and the row must say which."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/challenge-200")
        await _cite(conn, [sid])

    finding = await _run(pg_pool)
    data = finding.data

    assert data["examined"] == 1
    assert data[BLOCKED_BY_CHALLENGE] == 1
    # NOT counted as a successful archive, and NOT as a plain failure.
    assert data["archived"] == 0
    assert data["fetch_failed"] == 0
    assert data["text_extracted"] == 0

    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
        signal = await conn.fetchrow(
            "SELECT object_ref, payload FROM signals WHERE id = $1", sid,
        )
    assert row["status"] == ea.STATUS_BLOCKED_CHALLENGE
    assert row["last_error"].startswith(f"{BLOCKED_BY_CHALLENGE}: ")
    # The BYTES are kept — they are the evidence that we were blocked.
    assert row["object_ref"] is not None
    assert row["sha256"] is not None
    assert row["size_bytes"] == len(_CHALLENGE_HTML)
    assert row["text_extracted"] is False
    assert [p for p in archive_env.rglob("*") if p.is_file()]
    # …and the signal carries the object_ref with NO derived text.
    assert signal["object_ref"] == row["object_ref"]
    assert json.loads(signal["payload"]).get("archived_text") is None


async def test_the_old_500_char_gate_would_have_missed_this_body():
    """Anchors the regression to the measurement, not to a feeling: the
    fixture is an order of magnitude past the cap that used to hide it."""
    assert len(_CHALLENGE_HTML) > 9_000
    assert len(_CHALLENGE_HTML) > ea._WALL_MAX_CHARS * 15


async def test_an_edge_403_stays_failed_but_says_it_was_blocked(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """``failed`` is deliberate: only ``failed`` rows are re-attempted, and a
    403 may clear if an operator turns the impersonation flag on. What changes
    is that the row now NAMES the refusal instead of reporting a mystery."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/challenge-403")
        await _cite(conn, [sid])

    data = (await _run(pg_pool)).data

    assert data[BLOCKED_BY_CHALLENGE] == 1
    assert data["fetch_failed"] == 1
    assert data["archived"] == 0

    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
    assert row["status"] == "failed"          # retryable, on purpose
    assert row["last_error"].startswith(f"{BLOCKED_BY_CHALLENGE}: http_403")
    assert row["object_ref"] is None


async def test_a_genuine_article_still_archives_with_its_text(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """The no-regression half. (a′) must not turn a real page into a block."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/article")
        await _cite(conn, [sid])

    data = (await _run(pg_pool)).data

    assert data[BLOCKED_BY_CHALLENGE] == 0
    assert data["archived"] == 1
    assert data["text_extracted"] == 1

    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
        payload = await conn.fetchval("SELECT payload FROM signals WHERE id = $1", sid)
    assert row["status"] == "archived"
    assert row["text_extracted"] is True
    assert "roadblocks" in json.loads(payload)["archived_text"]


async def test_the_receipt_names_the_block_only_when_one_happened(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """The R-4 discipline: the shipped-default title is unchanged character
    for character on a clean run, and grows only when something was refused."""
    async with pg_pool.acquire() as conn:
        clean = await _insert_signal(conn, f"{http_fixture}/article")
        await _cite(conn, [clean])
    finding = await _run(pg_pool)
    assert "challenge-blocked" not in finding.title
    assert BLOCKED_BY_CHALLENGE not in finding.tags

    async with pg_pool.acquire() as conn:
        blocked = await _insert_signal(conn, f"{http_fixture}/challenge-200")
        await _cite(conn, [blocked])
    finding = await _run(pg_pool)
    assert "1 challenge-blocked" in finding.title
    assert BLOCKED_BY_CHALLENGE in finding.tags


async def test_a_blocked_row_is_terminal_and_never_reburns_the_budget(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """``blocked_challenge`` is not in the re-attempt predicate (only
    ``failed`` is), and the signal carries an object_ref — so the row drops
    out of the candidate set and the fetch budget is never spent on it twice."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/challenge-200")
        await _cite(conn, [sid])

    first = (await _run(pg_pool)).data
    assert first[BLOCKED_BY_CHALLENGE] == 1

    second = (await _run(pg_pool)).data
    assert second["examined"] == 0
    assert second[BLOCKED_BY_CHALLENGE] == 0
