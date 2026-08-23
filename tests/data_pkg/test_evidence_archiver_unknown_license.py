# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-4 — the archiver's CURATED fail-open licence posture becomes an option.

THE ASYMMETRY R-3b LEFT STANDING. `evidence_archiver` fails CLOSED for
web-retrieved rows with no affirmative licence, and still fails OPEN for
everything else: a REGISTERED source whose `license_class` was never classified
has its bytes archived. That posture came from the LIC-1 review finding no
anti-LLM EULA among the ~48 feeds then active — a finding about a specific
catalog on a specific date, not a property of the world, and not a measurement
anyone made for an operator who registers their own feeds.

THE FIX, shaped so it cannot surprise anyone: `options.unknown_license_gate`,
declared in `handler_options` so a descriptor PUT can move it, defaulting to
`archive` = today's behaviour byte for byte. Under `fail_closed` an unset or
`"unknown"` licence withholds the BYTES on any row, keeps the metadata, and
counts the refusal as `skipped_license_unknown` — its own counter, so the two
fail-closed policies stay separately priceable even though the sidecar CHECK
vocabulary makes them share the `skipped_license_unreviewed` status.

WHAT THESE TESTS HAVE TO PROVE, in order of what would actually go wrong:

  1. the DEFAULT changed nothing — a curated row with an unknown licence still
     archives, and the receipt is unchanged;
  2. the fail-closed path really withholds bytes, end to end, and records why;
  3. an operator can reach the option THROUGH THE DESCRIPTOR — the real
     `method.options` -> `_merge_descriptor_options` -> `run_method` binding,
     not a hand-passed dict;
  4. R-3b keeps its own accounting when both policies are on.

The end-to-end runs go through `legba.data.analysts.deterministic.run_method`
(the dispatcher the actor calls) against a migrated Postgres, real SQL, a real
HTTP fetch and the real CAS root.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.deterministic import run_method
from legba.data.analysts.deterministic_handlers import evidence_archiver as ea
from legba.data.analysts.handler_options import resolve_handler_options
from legba.data.config import PostgresConfig
from legba.data.retrieval_origin import CURATED_SOURCE, web_search_origin
from legba.data.schemas.analyst import AnalystDescriptor
from legba.runtime.analyst_method import AnalystMethodResult
from legba.runtime.dapr_actors import _merge_descriptor_options

SUB = "evidence_archiver"
WEB_ORIGIN = web_search_origin("search.searxng.local")

_HTML = (
    b"<!doctype html><html><head><title>Unclassified feed</title></head><body>"
    b"<article><p>A registered source nobody licence-reviewed published this. "
    b"Whether we may keep the bytes is the question the gate now lets an "
    b"operator answer.</p></article></body></html>"
)


# ---------------------------------------------------------------------------
# 1) The predicate — the full matrix, no DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "license_class",
    [None, "unknown", "cc_by", "public_domain", "open_gov_attribution"],
)
def test_default_policy_never_withholds_anything(license_class):
    """`fail_closed=False` is the shipped default and is inert by construction:
    no licence value, and no origin, can make it refuse."""
    assert ea.license_unreviewed(license_class) is False
    assert ea.license_unreviewed(license_class, fail_closed=False) is False


@pytest.mark.parametrize(
    "license_class,expected",
    [
        # No affirmative verdict → withhold.
        (None, True),
        ("unknown", True),
        # An operator classification → archive normally.
        ("cc_by", False),
        ("public_domain", False),
        ("open_gov_attribution", False),
    ],
)
def test_fail_closed_matrix(license_class, expected):
    assert ea.license_unreviewed(license_class, fail_closed=True) is expected


def test_the_gate_is_origin_blind_unlike_r3b():
    """R-3b keys on the retrieval origin; R-4 deliberately does not — its whole
    point is the CURATED rows R-3b leaves alone."""
    assert ea.license_unreviewed(None, fail_closed=True) is True
    assert ea.web_origin_license_unreviewed(None, CURATED_SOURCE) is False
    assert ea.web_origin_license_unreviewed(None, None) is False


def test_module_default_is_the_fail_open_posture():
    assert ea.UNKNOWN_LICENSE_ARCHIVES is True
    assert ea.UNKNOWN_LICENSE_GATE_ARCHIVE == "archive"
    assert ea.UNKNOWN_LICENSE_GATE_FAIL_CLOSED == "fail_closed"


def test_a_reviewed_forbidding_class_is_not_this_gates_business():
    """`anti_ai_walled` is refused upstream by license_forbids_retention, so
    this predicate must not claim it — otherwise the ledger loses which rule
    fired."""
    assert ea.license_forbids_retention(
        "anti_ai_walled", ea.FORBID_RETENTION_CLASSES,
    ) is True
    assert ea.license_unreviewed("anti_ai_walled", fail_closed=True) is False


def test_counters_declare_the_new_class_and_keep_the_old_ones():
    counters = ea._zero_counters()
    assert counters["skipped_license_unknown"] == 0
    assert counters["skipped_license_unreviewed"] == 0
    assert counters["skipped_license"] == 0


def test_the_default_receipt_title_is_unchanged():
    """The R-4 fragment appends ONLY when the policy refused something, so a
    default run's title is character-identical to before the option existed."""
    title = ea._build_finding(ea._zero_counters()).title
    assert "unknown-licence-skipped" not in title
    assert "unknown_license_fail_closed" not in ea._build_finding(
        ea._zero_counters()
    ).tags


def test_the_receipt_surfaces_the_refusal_when_it_happens():
    counters = dict(ea._zero_counters())
    counters.update({"examined": 2, "skipped_license_unknown": 2})
    finding = ea._build_finding(counters)
    assert "2 unknown-licence-skipped" in finding.title
    assert "unknown_license_fail_closed" in finding.tags
    assert "skipped_license_unknown=2" in finding.body


# ---------------------------------------------------------------------------
# 2) The DESCRIPTOR binding — the channel an operator actually uses
# ---------------------------------------------------------------------------


DESCRIPTOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "analyst_evidence_archiver.yaml"
)


def _shipped_descriptor(options: dict | None = None):
    """The SHIPPED descriptor, rehydrated exactly as the registry does.

    Deliberately the real YAML rather than a hand-built object: the whole claim
    is "one operator PUT flips this", and that is only true if the option rides
    the descriptor production actually registers, through the schema that
    validates it.
    """
    body = yaml.safe_load(DESCRIPTOR_PATH.read_text())
    body["identity"]["version"] = "0" * 16   # the registry stamps the real hash
    if options is not None:
        body["method"]["options"] = options
    return AnalystDescriptor.model_validate(body, strict=False)


def _runtime_options() -> dict:
    """The mapping the actor has built by the merge point."""
    return {
        "analyst_id": SUB,
        "analyst_version": "abc",
        "run_id": "r-1",
        "sub_handler": SUB,
    }


def test_the_shipped_descriptor_declares_no_options_so_the_default_stands():
    """THE regression guard for item 6's contract: the descriptor in the tree
    sets nothing, so every handler default — including this one — is untouched
    by this change."""
    descriptor = _shipped_descriptor()
    assert getattr(descriptor.method, "options", None) in (None, {})
    options = _runtime_options()
    assert _merge_descriptor_options(options, descriptor) is None
    assert "unknown_license_gate" not in options


def test_one_put_on_the_shipped_descriptor_reaches_the_run_options():
    """THE BINDING PATH: method.options on the real descriptor -> the actor's
    own _merge_descriptor_options -> the mapping the sub-handler reads."""
    descriptor = _shipped_descriptor({"unknown_license_gate": "fail_closed"})
    options = _runtime_options()
    receipt = _merge_descriptor_options(options, descriptor)

    assert options["unknown_license_gate"] == "fail_closed"
    assert receipt is not None
    assert receipt["status"] == "applied"
    assert receipt["sub_handler"] == SUB
    assert receipt["applied"] == {"unknown_license_gate": "fail_closed"}
    assert receipt["rejected"] == []


def test_a_bad_value_on_the_descriptor_degrades_loudly_and_keeps_the_default():
    descriptor = _shipped_descriptor({"unknown_license_gate": "fail-closed"})
    options = _runtime_options()
    receipt = _merge_descriptor_options(options, descriptor)

    assert "unknown_license_gate" not in options
    assert receipt["status"] == "degraded"
    assert receipt["applied"] == {}
    assert receipt["rejected"][0]["key"] == "unknown_license_gate"


def test_the_option_is_declared_so_a_descriptor_put_can_set_it():
    res = resolve_handler_options(SUB, {"unknown_license_gate": "fail_closed"})
    assert res.accepted == {"unknown_license_gate": "fail_closed"}
    assert res.rejected == ()
    res = resolve_handler_options(SUB, {"unknown_license_gate": "archive"})
    assert res.accepted == {"unknown_license_gate": "archive"}


def test_an_out_of_choices_value_is_dropped_not_applied(caplog):
    with caplog.at_level(logging.WARNING):
        res = resolve_handler_options(SUB, {"unknown_license_gate": "fail-closed"})
    assert res.accepted == {}
    assert res.rejected[0].cause == "invalid_value"
    assert res.rejected[0].key == "unknown_license_gate"


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
    """Clear EVERY archiver suite's fixtures — the counters asserted below are
    whole-run totals, and all three suites prefix their rows `test_`."""
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
    """Through the real dispatcher — `run_method` resolves `options.sub_handler`
    against SUB_HANDLERS, so this also proves the handler is still registered
    under the name the descriptor names."""
    options = {
        "sub_handler": SUB, "analyst_id": SUB, "run_id": str(uuid4()),
        "per_host_delay_seconds": 0.0, "timeout_seconds": 10.0, **opts,
    }
    result = await run_method([], options, _Deps(pool))
    assert isinstance(result, AnalystMethodResult)
    return result.finding


async def _insert_signal(conn, url, *, origin=None, license_class=None):
    sid = uuid4()
    payload = {"title": "test signal"}
    if license_class is not None:
        payload["license_class"] = license_class
    await conn.execute(
        "INSERT INTO signals (id, source_id, canonical_url, payload, "
        "  raw_provenance, content_hash, retrieval_origin) "
        "VALUES ($1, 'test_r4_src', $2, $3::jsonb, '{}'::jsonb, $4, $5)",
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
        "        'test_r4_unit', $2::uuid[], 'legba/finding/1.0.0')",
        fid, [str(s) for s in derived_from],
    )
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, analyst_id, schema_uri) "
        "VALUES ($1, 'critique', 'Faithfulness verify — test finding', '', 1.0, "
        "        $2::jsonb, 'test_r4_verify', 'legba/critique/1.0.0')",
        uuid4(), json.dumps({"analyzed_output_id": str(fid), "overall_score": score}),
    )
    return fid


async def _sidecar(conn, sid):
    return await conn.fetchrow(
        "SELECT * FROM evidence_archive WHERE signal_id = $1", sid,
    )


# ---------------------------------------------------------------------------
# 3) End-to-end — the DEFAULT path is unchanged
# ---------------------------------------------------------------------------


async def test_default_still_archives_a_curated_unknown_licence(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """THE regression guard. Same row, no options: today's behaviour."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page")
        await _cite(conn, [sid])

    finding = await _run(pg_pool)
    data = finding.data

    assert data["archived"] == 1
    assert data["skipped_license_unknown"] == 0
    assert data["bytes_stored"] > 0
    assert "unknown-licence-skipped" not in finding.title

    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
        signal = await conn.fetchrow(
            "SELECT object_ref FROM signals WHERE id = $1", sid,
        )
    assert row["status"] == "archived"
    assert signal["object_ref"].startswith("cas:sha256/")


async def test_the_explicit_archive_value_is_the_same_as_no_value(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page")
        await _cite(conn, [sid])

    data = (await _run(pg_pool, unknown_license_gate="archive")).data
    assert data["archived"] == 1
    assert data["skipped_license_unknown"] == 0


# ---------------------------------------------------------------------------
# 4) End-to-end — the FAIL-CLOSED path
# ---------------------------------------------------------------------------


async def test_fail_closed_withholds_the_bytes_of_a_curated_unknown_row(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page")
        await _cite(conn, [sid])

    finding = await _run(pg_pool, unknown_license_gate="fail_closed")
    data = finding.data

    assert data["examined"] == 1
    assert data["skipped_license_unknown"] == 1
    assert data["archived"] == 0
    assert data["bytes_stored"] == 0
    # NOT conflated with either older skip class.
    assert data["skipped_license"] == 0
    assert data["skipped_license_unreviewed"] == 0
    # Nothing was fetched-then-discarded: the volume is empty.
    assert not [p for p in archive_env.rglob("*") if p.is_file()]
    # Visible in the receipt an operator reads without opening a row.
    assert "1 unknown-licence-skipped" in finding.title
    assert "unknown_license_fail_closed" in finding.tags

    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
        signal = await conn.fetchrow(
            "SELECT object_ref FROM signals WHERE id = $1", sid,
        )
    # Recorded, never silent — and the row says WHICH policy refused it.
    assert row["status"] == ea.STATUS_SKIPPED_LICENSE_UNREVIEWED
    assert row["object_ref"] is None
    assert row["license_class"] is None
    # Metadata retention is unaffected.
    assert row["fetched_url"] == f"{http_fixture}/page"
    assert "unknown_license_gate='fail_closed'" in row["last_error"]
    assert "metadata kept" in row["last_error"]
    # A curated row carries no origin — which is how the two fail-closed
    # populations stay separable at row level despite the shared status.
    assert row["retrieval_origin"] is None
    assert signal["object_ref"] is None


async def test_fail_closed_also_withholds_an_explicit_unknown_class(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """`unknown` = reviewed and indeterminate. Not an affirmative permission."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(
            conn, f"{http_fixture}/page", license_class="unknown",
        )
        await _cite(conn, [sid])

    data = (await _run(pg_pool, unknown_license_gate="fail_closed")).data
    assert data["skipped_license_unknown"] == 1
    assert data["archived"] == 0
    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
    assert row["license_class"] == "unknown"


async def test_fail_closed_still_archives_a_classified_source(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """The gate withholds for ABSENCE of a verdict, not as a blanket refusal —
    which is what makes 'classify your catalog, then flip' a real path."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(
            conn, f"{http_fixture}/page", license_class="cc_by",
        )
        await _cite(conn, [sid])

    data = (await _run(pg_pool, unknown_license_gate="fail_closed")).data
    assert data["archived"] == 1
    assert data["skipped_license_unknown"] == 0
    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
    assert row["status"] == "archived"
    assert row["license_class"] == "cc_by"


async def test_a_reviewed_forbidding_class_still_takes_the_p22_path(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """With R-4 on, `skipped_license` must still mean what it always meant."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(
            conn, f"{http_fixture}/page", license_class="anti_ai_walled",
        )
        await _cite(conn, [sid])

    data = (await _run(pg_pool, unknown_license_gate="fail_closed")).data
    assert data["skipped_license"] == 1
    assert data["skipped_license_unknown"] == 0
    async with pg_pool.acquire() as conn:
        row = await _sidecar(conn, sid)
    assert row["status"] == "skipped_license"


async def test_r3b_keeps_its_own_accounting_when_both_policies_are_on(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """A web row is refused by R-3b FIRST, so turning R-4 on never re-attributes
    a skip that was already being made — the two counters stay comparable
    across the flip."""
    async with pg_pool.acquire() as conn:
        web = await _insert_signal(
            conn, f"{http_fixture}/page?a", origin=WEB_ORIGIN,
        )
        curated = await _insert_signal(conn, f"{http_fixture}/page?b")
        await _cite(conn, [web, curated])

    data = (await _run(pg_pool, unknown_license_gate="fail_closed")).data
    assert data["examined"] == 2
    assert data["skipped_license_unreviewed"] == 1   # the web row
    assert data["skipped_license_unknown"] == 1      # the curated row
    assert data["archived"] == 0

    async with pg_pool.acquire() as conn:
        web_row = await _sidecar(conn, web)
        curated_row = await _sidecar(conn, curated)
    # Same terminal status (closed CHECK vocabulary), separable by origin.
    assert web_row["status"] == curated_row["status"]
    assert web_row["retrieval_origin"] == WEB_ORIGIN
    assert curated_row["retrieval_origin"] is None


async def test_an_unrecognised_value_keeps_the_default_and_warns(
    pg_pool, clean_slate, http_fixture, archive_env, caplog,
):
    """A typo must not move a licence policy in EITHER direction silently."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page")
        await _cite(conn, [sid])

    with caplog.at_level(logging.WARNING):
        data = (await _run(pg_pool, unknown_license_gate="fail-closed")).data
    assert data["archived"] == 1
    assert data["skipped_license_unknown"] == 0
    assert "unknown_license_gate.bad_value" in caplog.text


async def test_a_fail_closed_skip_is_terminal_and_never_reburns_budget(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """Same discipline as every other recorded skip: written once, excluded
    from re-selection, re-evaluated only by an explicit policy change."""
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, f"{http_fixture}/page")
        await _cite(conn, [sid])

    first = (await _run(pg_pool, unknown_license_gate="fail_closed")).data
    assert first["skipped_license_unknown"] == 1
    second = (await _run(pg_pool, unknown_license_gate="fail_closed")).data
    assert second["examined"] == 0
    assert second["skipped_license_unknown"] == 0
