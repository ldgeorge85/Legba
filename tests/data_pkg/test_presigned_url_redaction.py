# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F59 — no presigned (signed) URL is persisted into ``signals``.

The OFAC download redirects to a presigned S3 URL carrying
``X-Amz-Security-Token``; ``instrument_snapshot`` strips its query before
persisting ``final_url``. The audit found every OTHER writer of a redirected URL
(the web-evidence fetcher's ``fetched_url_chain`` / ``canonical_url``, a
scraper's or crawler's provenance blob) unprotected, so the strip now lives in
ONE helper (``shared/url_redaction``) applied at each ``signals`` write path.
Each write path is proven on the real driver: the row is read back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

import legba.data.analysts  # noqa: F401  (A-F: a cold `import legba.data.research_evidence` is circular)

from legba.shared.url_redaction import (
    is_presigned,
    redact_presigned_in,
    redact_presigned_url,
    strip_query,
)

TOKEN = "IQoJb3JpZ2luX2VjEXAMPLE-F59-SESSION-TOKEN"
SIG = "deadbeefF59signature0123456789"
PRESIGNED = (
    "https://ofac-prod.s3.amazonaws.com/SDN.XML?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Security-Token={TOKEN}&X-Amz-Signature={SIG}&X-Amz-Expires=300"
)
PLAIN = "https://example.test/article?id=123&page=2"


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_a_presigned_url_loses_its_query_and_a_plain_one_keeps_it() -> None:
    out = redact_presigned_url(PRESIGNED)
    assert out == "https://ofac-prod.s3.amazonaws.com/SDN.XML"
    assert TOKEN not in out and SIG not in out
    assert redact_presigned_url(PLAIN) == PLAIN          # allow-list: untouched
    assert redact_presigned_url("") == ""
    # Other signing schemes.
    for signed in (
        "https://b.example/o?Signature=abc&Expires=1&AWSAccessKeyId=AKIA1",
        "https://storage.googleapis.com/b/o?X-Goog-Signature=ab&X-Goog-Credential=c",
        "https://acct.blob.core.windows.net/c/b?sv=2022&se=2026&sp=r&sig=abc",
    ):
        assert is_presigned(signed)
        assert "?" not in redact_presigned_url(signed)
    assert not is_presigned("https://x.test/p?sig=1")      # bare `sig` is not SAS
    assert strip_query(PLAIN) == "https://example.test/article"


def test_the_walker_reaches_nested_values_and_free_text() -> None:
    prov = {
        "fetch_kind": "x",
        "fetched_url_chain": ["https://a.test/go", PRESIGNED],
        "nested": {"links": [PLAIN, {"u": PRESIGNED}]},
        "error": f"fetch of {PRESIGNED}, then retry failed",
        "http_status": 200,
        "none": None,
    }
    out = redact_presigned_in(prov)
    blob = json.dumps(out)
    assert TOKEN not in blob and SIG not in blob and "X-Amz" not in blob
    assert out["fetched_url_chain"][1] == "https://ofac-prod.s3.amazonaws.com/SDN.XML"
    assert out["nested"]["links"][0] == PLAIN
    assert out["error"] == (
        "fetch of https://ofac-prod.s3.amazonaws.com/SDN.XML, then retry failed"
    )
    assert out["http_status"] == 200 and out["none"] is None
    assert TOKEN in json.dumps(prov), "the input must not be mutated"


def test_instrument_snapshot_still_strips_every_query() -> None:
    from legba.data.sources.instrument_snapshot import redact_url

    assert redact_url(PRESIGNED) == "https://ofac-prod.s3.amazonaws.com/SDN.XML"
    assert redact_url(PLAIN) == "https://example.test/article"


def test_every_signals_writer_goes_through_the_redactor() -> None:
    """A new ``INSERT INTO signals`` writer fails here until it redacts."""
    src = Path(__file__).resolve().parents[2] / "src" / "legba"
    allowed_without = {
        # A synthetic smoke row written by a hand-built literal; no fetch.
        "data/smoke.py",
    }
    writers = [
        p for p in src.rglob("*.py")
        if re.search(r"INSERT INTO (public\.)?signals\b", p.read_text("utf-8"))
    ]
    assert writers, "the scan found no signals writer at all"
    missing = [
        str(p.relative_to(src)) for p in writers
        if "redact_presigned_in" not in p.read_text("utf-8")
        and str(p.relative_to(src)) not in allowed_without
    ]
    assert not missing, f"signals writers that never redact presigned URLs: {missing}"


# ---------------------------------------------------------------------------
# The write paths, on the real driver
# ---------------------------------------------------------------------------


@pytest.fixture
async def conn(migrated_pg):
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        await c.close()


async def _prov(conn, sid) -> tuple[str, str | None]:
    row = await conn.fetchrow(
        "SELECT raw_provenance::text AS p, canonical_url FROM signals WHERE id = $1",
        sid,
    )
    assert row is not None, "the row was not written"
    return row["p"], row["canonical_url"]


async def _drop(conn, sid) -> None:
    await conn.execute("DELETE FROM signals WHERE id = $1", sid)


async def test_source_actor_write_redacts_raw_provenance(conn) -> None:
    from legba.data.sources._contract import Signal
    from legba.runtime.source_actor import write_canonical_signal

    sig = Signal(
        source_id=f"f59src{uuid4().hex[:8]}",
        payload={"title": "t"},
        canonical_url=PLAIN,
        content_hash=f"h-{uuid4().hex}",
        raw_provenance={"final_url": PRESIGNED, "keep": PLAIN,
                        "links": [PRESIGNED]},
    )
    sid = await write_canonical_signal(
        conn, sig, source_version="v1", owner_tenant="default",
    )
    assert sid is not None
    try:
        prov, canonical = await _prov(conn, sid)
        assert TOKEN not in prov and SIG not in prov and "X-Amz" not in prov
        assert json.loads(prov)["keep"] == PLAIN
        assert json.loads(prov)["final_url"].endswith("/SDN.XML")
        assert canonical == PLAIN
    finally:
        await _drop(conn, sid)


async def test_research_write_redacts_chain_and_canonical_url(conn) -> None:
    from legba.data.research_evidence import (
        ProviderContext,
        ResearchDispatch,
        ResearchHit,
        build_research_row,
        land_research_row,
    )

    hit = ResearchHit(
        url=PRESIGNED, title="t", snippet="s", text="body text f59",
        url_chain=("https://search.test/redirect?u=1", PRESIGNED),
        query="q",
    )
    row = build_research_row(
        hit, ProviderContext(component_id="search.f59"), ResearchDispatch(),
        run_id=uuid4(), requested_by="test", regime="open",
    )
    sid = await land_research_row(conn, row)
    assert sid is not None
    try:
        prov, canonical = await _prov(conn, sid)
        assert TOKEN not in prov and SIG not in prov and "X-Amz" not in prov
        chain = json.loads(prov)["fetched_url_chain"]
        assert chain == ["https://search.test/redirect?u=1",
                         "https://ofac-prod.s3.amazonaws.com/SDN.XML"]
        assert canonical == "https://ofac-prod.s3.amazonaws.com/SDN.XML"
        payload = await conn.fetchval(
            "SELECT payload::text FROM signals WHERE id = $1", sid)
        assert TOKEN not in payload and "X-Amz" not in payload
    finally:
        await _drop(conn, sid)


async def test_research_hit_keeps_an_ordinary_query_url(conn) -> None:
    from legba.data.research_evidence import ResearchHit

    hit = ResearchHit(url=PLAIN, url_chain=("https://s.test/r?x=1", PLAIN))
    assert hit.url == PLAIN
    assert hit.url_chain == ("https://s.test/r?x=1", PLAIN)


async def test_derived_job_signal_write_redacts_raw_provenance(conn) -> None:
    from legba.data.jobs.store import JobStore
    from legba.data.sources._contract import Signal

    sig = Signal(
        source_id=f"f59job{uuid4().hex[:8]}",
        produced_by_kind="job",
        produced_by_id="job.f59",
        payload={"title": "t"},
        content_hash=f"h-{uuid4().hex}",
        raw_provenance={"job_id": "j", "origin": PRESIGNED},
    )
    sid = await JobStore.land_derived_signal(conn, sig)
    try:
        prov, _ = await _prov(conn, sid)
        assert TOKEN not in prov and SIG not in prov and "X-Amz" not in prov
        assert json.loads(prov)["job_id"] == "j"
    finally:
        await _drop(conn, sid)
