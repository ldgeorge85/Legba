# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-E1 historical backfill script: ``scripts/reextract_boilerplate_signals.py``.

Exercises the script against the ephemeral migrated test DB (``migrated_pg``)
and a ``tmp_path`` CAS root (NEVER the live archive — ``archive_root_override``
keeps the whole suite hermetic):

  * dry-run reports garbage rows (by pattern + by host) and changes NOTHING —
    it does not even touch the filesystem;
  * ``--apply``, still-garbage-on-re-extraction -> STRIPS
    ``payload.archived_text`` / ``archived_text_chars``, resets the sidecar
    ``text_extracted`` to ``false``, trips the corpus dirty-marker contract
    (``indexed_at = NULL``);
  * ``--apply``, genuinely-clean-on-re-extraction -> UPGRADES with the newly
    extracted text;
  * missing CAS bytes / a missing-or-unparseable ``object_ref`` are skipped
    and counted — never guessed, never deleted;
  * ``by_pattern`` / ``by_host`` group multiple rows correctly.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers.evidence_archiver import _store_bytes
from legba.data.archive import cas_object_ref
from legba.data.config import PostgresConfig

# Import the script as a module (scripts/ is not a package). Resolve relative
# to this test file so the MAIN checkout and worktrees each test their own copy.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reextract_boilerplate_signals.py"
_spec = importlib.util.spec_from_file_location("reextract_boilerplate_signals", _SCRIPT)
reextract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reextract)

# JS-wall body (the Le Monde no-JS-fallback shape) — genuinely extractable
# HTML whose Trafilatura output matches the V-E1 deny-gate.
_JSWALL_HTML = (
    b"<!doctype html><html><head><title>Le Monde</title></head><body>"
    b"<article><p>JavaScript is disabled in your browser.</p>"
    b"<p>Please enable JavaScript to proceed.</p>"
    b"<p>A required part of this site could not load.</p></article>"
    b"</body></html>"
)
_CLEAN_HTML = (
    b"<!doctype html><html><head><title>Strait incident</title></head><body>"
    b"<article><p>A maritime incident occurred in the strait on Tuesday, "
    b"with two vessels reporting damage after an exchange of fire. "
    b"Authorities confirmed the closure of the shipping lane.</p>"
    b"<p>Officials said the investigation is ongoing and traffic will "
    b"resume once the area is declared safe.</p></article></body></html>"
)


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


@pytest_asyncio.fixture
async def clean_slate(conn):
    """The ``migrated_pg`` DB is SESSION-scoped (shared across this file's
    tests) — without this, an earlier test's rows would still be present and
    get rescanned/recounted by a later test's ``run()`` call."""
    await conn.execute(
        "DELETE FROM evidence_archive WHERE signal_id IN "
        "(SELECT id FROM signals WHERE source_id = 'src.reextract')"
    )
    await conn.execute("DELETE FROM signals WHERE source_id = 'src.reextract'")
    yield


async def _insert_signal(
    conn, *, canonical_url, archived_text, object_ref=None,
    source="src.reextract", tenant="t_reextract",
):
    sid = uuid4()
    payload = {"title": "x"}
    if archived_text is not None:
        payload["archived_text"] = archived_text
        payload["archived_text_chars"] = len(archived_text)
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, "
        "  canonical_url, payload, content_hash, object_ref) "
        "VALUES ($1,$2,$3,'text',$4,$5::jsonb,$6,$7)",
        sid, source, tenant, canonical_url, json.dumps(payload),
        uuid4().hex, object_ref,
    )
    return sid


async def _insert_sidecar(conn, sid, *, object_ref, sha256, content_type="text/html"):
    await conn.execute(
        "INSERT INTO evidence_archive (signal_id, status, object_ref, sha256, "
        "  content_type, text_extracted) "
        "VALUES ($1,'archived',$2,$3,$4,true)",
        sid, object_ref, sha256, content_type,
    )


async def _run(conn, **kw):
    kw.setdefault("quiet", True)
    return await reextract.run(conn, **kw)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_reports_and_writes_nothing(conn, clean_slate, tmp_path):
    digest = hashlib.sha256(_JSWALL_HTML).hexdigest()
    sid = await _insert_signal(
        conn, canonical_url="https://www.lemonde.fr/en/a.html",
        archived_text=(
            "JavaScript is disabled in your browser.\n"
            "Please enable JavaScript to proceed."
        ),
        object_ref=cas_object_ref(digest),
    )
    await _insert_sidecar(conn, sid, object_ref=cas_object_ref(digest), sha256=digest)
    clean_sid = await _insert_signal(
        conn, canonical_url="https://example.com/clean.html",
        archived_text="A maritime incident occurred in the strait on Tuesday.",
    )

    res = await _run(conn, archive_root_override=tmp_path)
    assert res["candidates_scanned"] == 2
    assert res["garbage_found"] == 1
    assert res["by_pattern"] == {"javascript is disabled": 1}
    assert res["by_host"] == {"www.lemonde.fr": 1}
    assert res["stripped"] == 0
    assert res["upgraded"] == 0
    assert res["bytes_missing"] == 0
    assert res["no_object_ref"] == 0

    # Nothing changed — dry-run never even touches the filesystem (no CAS
    # object was written for this test, and it still passes: proof the code
    # path never opens the file in dry-run mode).
    row = await conn.fetchrow("SELECT payload FROM signals WHERE id = $1", sid)
    payload = json.loads(row["payload"])
    assert "archived_text" in payload
    clean_row = await conn.fetchrow(
        "SELECT payload FROM signals WHERE id = $1", clean_sid,
    )
    assert json.loads(clean_row["payload"])["archived_text"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_strips_when_still_garbage_on_reextraction(conn, clean_slate, tmp_path):
    digest, ref, _ = _store_bytes(tmp_path, _JSWALL_HTML)
    sid = await _insert_signal(
        conn, canonical_url="https://www.lemonde.fr/en/a.html",
        # A candidate is selected on the STORED text (this is what the live
        # audit found); the exact wording differs slightly from what a fresh
        # extraction of the bytes below will produce — proving the script
        # RE-DERIVES from the CAS bytes rather than trusting the stored value
        # verbatim, while still correctly landing on "still a wall".
        archived_text="JavaScript is disabled in your browser. (cached copy)",
        object_ref=ref,
    )
    await conn.execute("UPDATE signals SET indexed_at = now() WHERE id = $1", sid)
    await _insert_sidecar(conn, sid, object_ref=ref, sha256=digest)

    res = await _run(conn, apply=True, archive_root_override=tmp_path)
    assert res["garbage_found"] == 1
    assert res["stripped"] == 1
    assert res["upgraded"] == 0
    assert res["bytes_missing"] == 0

    row = await conn.fetchrow(
        "SELECT payload, indexed_at FROM signals WHERE id = $1", sid,
    )
    payload = json.loads(row["payload"])
    assert "archived_text" not in payload
    assert "archived_text_chars" not in payload
    assert row["indexed_at"] is None   # corpus dirty-marker contract tripped

    side = await conn.fetchrow(
        "SELECT text_extracted, last_error FROM evidence_archive WHERE signal_id = $1",
        sid,
    )
    assert side["text_extracted"] is False
    assert "javascript is disabled" in side["last_error"]

    # Idempotent: a second --apply finds nothing left to fix for this row.
    res2 = await _run(conn, apply=True, archive_root_override=tmp_path)
    assert res2["garbage_found"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_upgrades_when_genuinely_clean_on_reextraction(conn, clean_slate, tmp_path):
    """The STORED text happens to be garbage (e.g. an old extraction bug),
    but the ARCHIVED BYTES are a genuine clean article — re-extraction
    recovers it instead of blindly stripping."""
    digest, ref, _ = _store_bytes(tmp_path, _CLEAN_HTML)
    sid = await _insert_signal(
        conn, canonical_url="https://example.com/a.html",
        archived_text="JavaScript is disabled in your browser.",  # stale/wrong
        object_ref=ref,
    )
    await _insert_sidecar(conn, sid, object_ref=ref, sha256=digest)

    res = await _run(conn, apply=True, archive_root_override=tmp_path)
    assert res["garbage_found"] == 1
    assert res["upgraded"] == 1
    assert res["stripped"] == 0

    row = await conn.fetchrow("SELECT payload FROM signals WHERE id = $1", sid)
    payload = json.loads(row["payload"])
    assert "maritime incident" in payload["archived_text"]
    assert payload["archived_text_chars"] == len(payload["archived_text"])

    side = await conn.fetchrow(
        "SELECT text_extracted, last_error FROM evidence_archive WHERE signal_id = $1",
        sid,
    )
    assert side["text_extracted"] is True
    assert side["last_error"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_skips_missing_cas_bytes_never_guesses(conn, clean_slate, tmp_path):
    missing_digest = "a" * 64
    sid = await _insert_signal(
        conn, canonical_url="https://www.lemonde.fr/en/gone.html",
        archived_text="JavaScript is disabled in your browser.",
        object_ref=cas_object_ref(missing_digest),
    )
    await _insert_sidecar(
        conn, sid, object_ref=cas_object_ref(missing_digest), sha256=missing_digest,
    )

    res = await _run(conn, apply=True, archive_root_override=tmp_path)
    assert res["garbage_found"] == 1
    assert res["bytes_missing"] == 1
    assert res["stripped"] == 0
    assert res["upgraded"] == 0

    row = await conn.fetchrow("SELECT payload FROM signals WHERE id = $1", sid)
    assert "archived_text" in json.loads(row["payload"])   # untouched


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_skips_unparseable_object_ref(conn, clean_slate, tmp_path):
    sid = await _insert_signal(
        conn, canonical_url="https://www.lemonde.fr/en/x.html",
        archived_text="JavaScript is disabled in your browser.",
        object_ref=None,
    )
    await _insert_sidecar(conn, sid, object_ref=None, sha256=None)

    res = await _run(conn, apply=True, archive_root_override=tmp_path)
    assert res["garbage_found"] == 1
    assert res["no_object_ref"] == 1
    assert res["stripped"] == 0
    assert res["upgraded"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_by_pattern_and_by_host_group_correctly_across_hosts(conn, clean_slate, tmp_path):
    digest, ref, _ = _store_bytes(tmp_path, _JSWALL_HTML)
    await _insert_signal(
        conn, canonical_url="https://www.lemonde.fr/en/a.html",
        archived_text="JavaScript is disabled in your browser.", object_ref=ref,
    )
    await _insert_signal(
        conn, canonical_url="https://www.lemonde.fr/en/b.html",
        archived_text="JavaScript is disabled in your browser.", object_ref=ref,
    )
    await _insert_signal(
        conn, canonical_url="https://en.irna.ir/news/x",
        archived_text="Transferring to the website...", object_ref=ref,
    )

    res = await _run(conn, archive_root_override=tmp_path)
    assert res["garbage_found"] == 3
    assert res["by_host"] == {"www.lemonde.fr": 2, "en.irna.ir": 1}
    assert res["by_pattern"] == {
        "javascript is disabled": 2, "transferring to the website": 1,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clean_rows_never_touched_or_counted(conn, clean_slate, tmp_path):
    await _insert_signal(
        conn, canonical_url="https://example.com/clean.html",
        archived_text="A maritime incident occurred in the strait on Tuesday.",
    )
    # No archived_text at all — must not even be a candidate.
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, "
        "  canonical_url, payload, content_hash) "
        "VALUES ($1,'src.reextract','t_reextract','text',$2,'{}'::jsonb,$3)",
        uuid4(), "https://example.com/no-text.html", uuid4().hex,
    )

    res = await _run(conn, apply=True, archive_root_override=tmp_path)
    assert res["candidates_scanned"] == 1   # only the archived_text-bearing row
    assert res["garbage_found"] == 0
    assert res["stripped"] == 0
    assert res["upgraded"] == 0
