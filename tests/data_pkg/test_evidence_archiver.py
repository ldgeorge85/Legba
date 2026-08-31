# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-1/P2-2 — the ``evidence_archiver`` deterministic sub-handler.

Pure tests (no DB): registration + TRACE_ONLY wiring, the CAS address format
(roundtrip + never-fabricated parse), the P2-2 license-gate posture (unknown
ARCHIVES, forbidden classes skip), content-addressed store (atomic write +
dedup hit), textual sniffing, and the corpus projection upgrade
(``archived_text`` in the doc + ``best_body`` preference).

V-E1/V-E2 pure tests: the JS-wall/bot-check/redirect deny-gate
(``_match_wall_pattern``) — the exact live artifact from JUDGE_READOUT §5
(Le Monde's no-JS fallback page), the other live-DB-confirmed patterns, the
length-gate false-positive guard (a wall phrase incidentally present inside a
genuine long article must never be rejected), and clean-text passthrough.

Ephemeral-DB tests (``migrated_pg`` + a LOCAL HTTP fixture server, allowlisted
through the SSRF guard via ``LEGBA_EGRESS_ALLOW_HOSTS``): cited-only selection
(uncited/unverified never archived), fetch→store→hash→stamp end-to-end against
live SQL + the 0104 sidecar, the corpus DIRTY-MARKER contract (indexed_at
nulled + updated_at bumped in the same stamp), the V-E2 substance-floor marker
(``payload.archived_text_chars``), the V-E1 rejection path end-to-end (bytes
still archived, no text stored, dirty marker NOT tripped, counter + sidecar
honesty), license-gate skip rows, the egress guard (a private-address
canonical_url is blocked + terminal), the size cap, failed-fetch attempt caps,
CAS dedup, the media leg, idempotency (re-run examines nothing), and counter
honesty.
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

# V-E1 — a no-JS fallback page shaped exactly like the LIVE lemonde.fr artifact
# JUDGE_READOUT §5 named ("JavaScript is disabled in your browser..."):
# Trafilatura extracts it CLEANLY (it's well-formed HTML with real <article>
# text) — the deny-gate is what must reject it, not extraction failure.
_JSWALL_HTML = (
    b"<!doctype html><html><head><title>Le Monde</title></head><body>"
    b"<article><p>JavaScript is disabled in your browser.</p>"
    b"<p>Please enable JavaScript to proceed.</p>"
    b"<p>A required part of this site could not load.</p></article>"
    b"</body></html>"
)

# The SAME wall phrase, but as ONE sentence inside a genuine long article (the
# real france24.com shape: an embedded-video caption inside real prose about
# Zidane's appointment) — must NEVER be rejected; the length gate protects it.
_LONG_ARTICLE_WITH_WALL_MENTION_HTML = (
    b"<!doctype html><html><head><title>Zidane</title></head><body><article>"
    b"<p>Iconic footballer Zinedine Zidane and the France national football "
    b"team share a love story with a bright future stretching back over "
    b"decades of shared history on and off the pitch, fans and pundits agree, "
    b"even as the sport itself continues to evolve around them in ways few "
    b"could have predicted when he first rose to prominence.</p>"
    b"<p>French-Algerian football icon Zinedine Zidane became the new manager "
    b"of France's national football team on Tuesday, bringing an end to what "
    b"little suspense there was over Didier Deschamp's successor. Zidane "
    b"frequently stated his desire to take over the French team and much of "
    b"the football world supported his ambitions over the following years, "
    b"citing his playing career, his tactical acumen, and his standing among "
    b"both players and supporters alike as reasons for optimism about what "
    b"comes next for the national side under his stewardship.</p>"
    b"<p>One of your browser extensions seems to be blocking the video player "
    b"from loading. To watch this content, you may need to disable it on "
    b"this site.</p></article></body></html>"
)


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


# ---------------------------------------------------------------------------
# R6b — Telegram embed-widget pages never get text-extracted (chrome, not
# prose: "Download\nContext\nEmbed\n...telegram-widget.js...")
# ---------------------------------------------------------------------------


def test_skip_text_extraction_telegram_widget_hosts():
    assert ea._skip_text_extraction("https://t.me/somechannel/12345") is True
    # The public "instant view" preview variant — same widget chrome.
    assert ea._skip_text_extraction("https://t.me/s/somechannel/12345") is True
    assert ea._skip_text_extraction("https://telegram.me/somechannel/12345") is True
    # Case + port + userinfo are normalized before the host comparison.
    assert ea._skip_text_extraction("https://T.ME:443/somechannel/1") is True
    # A genuine article host is never affected.
    assert ea._skip_text_extraction("https://example.com/article") is False
    assert ea._skip_text_extraction("https://news.example.com/t.me-mentions") is False
    # A malformed URL never raises — degrades to "attempt extraction" (the
    # existing best-effort _extract_text failure path covers a bad fetch).
    assert ea._skip_text_extraction("not a url \x00") is False


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
# V-E1 — JS-wall/bot-check/redirect deny-gate (planning/
# VERIFY_PATH_STRUCTURAL_FIXES_SPEC_2026-07-31.md §V-E1; JUDGE_READOUT §5)
# ---------------------------------------------------------------------------


def test_match_wall_pattern_rejects_the_live_judge_readout_artifact():
    """The EXACT artifact JUDGE_READOUT §5 named: a stored archived_text
    reading "JavaScript is disabled in your browser" (Le Monde) grounded a
    judged claim. Reproduced verbatim from a live-DB row (2026-07-31 audit,
    lemonde.fr, 286 chars)."""
    le_monde = (
        "JavaScript is disabled in your browser.\n"
        "Please enable JavaScript to proceed.\n"
        "A required part of this site couldn’t load. This may be due to a "
        "browser extension, network issues, or browser settings. Please check "
        "your connection, disable any ad blockers, or try using a different "
        "browser."
    )
    assert len(le_monde) <= ea._WALL_MAX_CHARS
    assert ea._match_wall_pattern(le_monde) == "javascript is disabled"


def test_match_wall_pattern_rejects_other_live_confirmed_patterns():
    """Every one of these is a VERBATIM (or near-verbatim) live-DB-confirmed
    garbage body from the same 2026-07-31 audit — not invented examples."""
    # en.irna.ir — Google-redirect interstitial, 83 confirmed rows, 70 chars.
    assert ea._match_wall_pattern("Transferring to the website...") == (
        "transferring to the website"
    )
    # france24.com — ad-blocker/video-wall notice, 148 confirmed rows (short).
    video_wall = (
        "One of your browser extensions seems to be blocking the video player "
        "from loading. To watch this content, you may need to disable it on "
        "this site."
    )
    assert ea._match_wall_pattern(video_wall) == (
        "one of your browser extensions seems to be blocking the video player"
    )
    # A ShopShield-style bot-mitigation "please wait" page (1 confirmed row).
    assert ea._match_wall_pattern(
        "Please wait\nWe are optimizing your request for the best experience."
    ) == "we are optimizing your request for the best experience"
    # A literal, never-substituted template placeholder (2 confirmed rows).
    assert ea._match_wall_pattern(
        "ERROR MESSAGE HEADINGERROR MESSAGE SUBHEADING"
    ) == "error message heading"
    # Industry-standard bot-challenge phrasing — not observed live in THIS
    # audit, but included defensively (see _WALL_DENY_PATTERNS docstring).
    assert ea._match_wall_pattern("Are you a robot? Please verify below.") == (
        "are you a robot"
    )


def test_match_wall_pattern_case_insensitive():
    assert ea._match_wall_pattern("JAVASCRIPT IS DISABLED in your browser.") == (
        "javascript is disabled"
    )


def test_match_wall_pattern_length_gate_protects_long_genuine_articles():
    """The SAME wall phrase, but as one sentence inside genuine long prose
    (the real france24.com shape — an embedded-video caption) must NEVER be
    rejected. Live-audit-derived cutoff: the longest confirmed 100%-boilerplate
    body was 499 chars; the shortest confirmed genuine article incidentally
    containing a wall phrase was 852 chars — 500 sits cleanly in the gap."""
    long_article = (
        "Iconic footballer Zinedine Zidane and the France national team share "
        "a rich history together. " * 15
    ) + (
        "One of your browser extensions seems to be blocking the video player "
        "from loading. To watch this content, you may need to disable it on "
        "this site."
    )
    assert len(long_article) > ea._WALL_MAX_CHARS
    assert ea._match_wall_pattern(long_article) is None


def test_match_wall_pattern_clean_text_never_matches():
    assert ea._match_wall_pattern(
        "A maritime incident occurred in the strait on Tuesday, with two "
        "vessels reporting damage after an exchange of fire."
    ) is None
    assert ea._match_wall_pattern("") is None


def test_match_wall_pattern_deliberately_excludes_decorative_cookie_footers():
    """DELIBERATE exclusion, live-audit-verified: CGTN appends a cookie-notice
    footer ("By continuing to browse our site you agree to our use of
    cookies...") to EVERY article — 77 confirmed rows from 314 to 7,517 chars,
    always co-occurring with real content, never the whole body. A blind
    "accept cookies" pattern would false-reject real cited news; this is why
    it is NOT in :data:`ea._WALL_DENY_PATTERNS`."""
    cgtn_short_but_real = (
        "By continuing to browse our site you agree to our use of cookies, "
        "revised Privacy Policy and Terms of Use. You can change your cookie "
        "settings through your browser.\nCGTN\n, Updated 10:36, 31-Jul-2026"
        "The Japanese government held the first meeting of the National "
        "Intelligence Council on Friday, local media reported."
    )
    assert ea._match_wall_pattern(cgtn_short_but_real) is None


def test_clean_extraction_passes_the_gate_byte_identical():
    """Composition-level passthrough: a genuine extraction is untouched by
    the V-E1 gate — same text in, same text out, nothing stripped/mutated."""
    text = ea._extract_text(_HTML, None, max_chars=200_000)
    assert text is not None
    assert ea._match_wall_pattern(text) is None
    # Re-running extraction is deterministic — byte-identical on repeat.
    assert ea._extract_text(_HTML, None, max_chars=200_000) == text


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
            "/jswall": (200, "text/html; charset=utf-8", _JSWALL_HTML),
            "/long_with_wall_mention": (
                200, "text/html; charset=utf-8",
                _LONG_ARTICLE_WITH_WALL_MENTION_HTML,
            ),
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


async def _drain_backlog(pool) -> None:
    """Exhaust any PRE-EXISTING archiver candidate backlog before a test
    inserts its own signal(s).

    ``_SELECT_CANDIDATES_SQL`` (evidence_archiver.py) has no tenant/source
    scoping — by design, it is a global "every verified-cited, unarchived
    signal in the substrate" scan. ``clean_slate`` only retires THIS file's
    own ``test_p2_1_*``-prefixed rows, so it cannot remove a candidate a
    sibling file (any of the 20+ other files that insert a signal + a
    'Faithfulness verify' critique without cleanup) left behind on the
    session-shared ``migrated_pg`` DB. The 2026-08-23 shuffled nightly hit
    exactly this: five tests asserting ``data["examined"] == N`` failed with
    an off-by-however-many-strays-were-left count.

    A read-only baseline COUNT (an earlier version of this fix) only
    immunized the ``examined`` line — a stray candidate is still a REAL
    candidate, so ``_run()`` still fetches/archives/fails it, corrupting
    ``archived`` / ``fetch_failed`` / ``skipped_*`` too (confirmed with a
    synthetic cross-file polluter during this fix's own proof pass — see
    planning/CAMPAIGN_2026-08-29/SHUFFLE_FIX_REPORT.md). DRAINING instead:
    call ``_run()`` up to :data:`ea._DEFAULT_MAX_ATTEMPTS` times BEFORE the
    test's own insert. Bounded, not backlog-size-dependent: every candidate
    reaches a TERMINAL state (archived, a permanent ``skipped_*``, or a
    failure that exhausts its ``attempts`` budget) within that many runs by
    the archiver's own contract, so after the loop the candidate pool holds
    ONLY whatever this test inserts next — restoring the original exact-count
    assertions' full precision rather than merely a delta on one field.
    """
    for _ in range(ea._DEFAULT_MAX_ATTEMPTS):
        await _run(pool)


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
    await _drain_backlog(pg_pool)
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
        # V-E2: the substance-floor marker — a free len() stamped alongside
        # the text itself, in the SAME update, so a later verify-side pass
        # never has to re-read the body to know how much was extracted.
        assert payload["archived_text_chars"] == len(payload["archived_text"])
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


async def test_no_content_region_host_withholds_derived_text_not_bytes(
    pg_pool, clean_slate, http_fixture, archive_env, monkeypatch,
):
    """R6b end-to-end wiring: a candidate whose URL resolves to a known
    no-content-region host (the real ``t.me`` can't be dialed in a hermetic
    test, so the local fixture host is monkeypatched into the set) archives
    the BYTES exactly as normal but must NEVER write ``archived_text`` — even
    though the fixture page is genuinely well-formed, extractable HTML (the
    same page ``test_archives_cited_verified_only`` proves DOES extract
    cleanly when the host is NOT in the no-content-region set)."""
    monkeypatch.setattr(ea, "_NO_CONTENT_REGION_HOSTS", frozenset({"127.0.0.1"}))
    async with pg_pool.acquire() as conn:
        cited = await _insert_signal(conn, f"{http_fixture}/article")
        await _insert_finding(conn, [cited], verified=True)

    data = await _run(pg_pool)
    assert data["archived"] == 1
    assert data["text_extracted"] == 0
    assert data["text_extract_failed"] == 0
    assert data["text_extract_skipped"] == 1

    async with pg_pool.acquire() as conn:
        row = await _signal(conn, cited)
        # The bytes are archived (object_ref stamped) — only the derived-text
        # upgrade is withheld.
        assert row["object_ref"] is not None
        payload = json.loads(row["payload"])
        assert "archived_text" not in payload

        side = await _sidecar(conn, cited)
        assert side["status"] == "archived"
        assert side["text_extracted"] is False


async def test_wall_pattern_rejected_bytes_archived_no_text_dirty_marker_not_tripped(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """V-E1 end-to-end: a JS-wall body extracts CLEANLY (Trafilatura succeeds
    — this is genuinely well-formed, parseable HTML) but is REJECTED as
    boilerplate. The bytes are archived exactly as normal; payload.archived_text
    is NEVER written; the corpus dirty-marker contract is NEVER tripped (a
    rejection must not falsely re-queue a doc for text it doesn't have); and
    the rejection is counted + the sidecar reflects "not extracted"."""
    async with pg_pool.acquire() as conn:
        cited = await _insert_signal(conn, f"{http_fixture}/jswall")
        await _insert_finding(conn, [cited], verified=True)
        # Simulate a previously-indexed doc so a false dirty-marker trip would
        # be OBSERVABLE (indexed_at flipping to NULL) rather than vacuously
        # "still NULL".
        await conn.execute(
            "UPDATE signals SET indexed_at = now() WHERE id = $1", cited,
        )
        pre = await _signal(conn, cited)
        assert pre["indexed_at"] is not None

    data = await _run(pg_pool)
    assert data["archived"] == 1
    assert data["text_extracted"] == 0
    assert data["text_extract_failed"] == 0
    assert data["text_extract_rejected_boilerplate"] == 1

    digest = hashlib.sha256(_JSWALL_HTML).hexdigest()
    stored = cas_path(archive_env, digest)
    assert stored.read_bytes() == _JSWALL_HTML   # bytes archive untouched

    async with pg_pool.acquire() as conn:
        row = await _signal(conn, cited)
        assert row["object_ref"] == cas_object_ref(digest)   # bytes ARE archived
        payload = json.loads(row["payload"])
        assert "archived_text" not in payload         # NO text stored
        assert "archived_text_chars" not in payload
        # The dirty-marker contract was NOT tripped — a rejection is not a
        # text write, so the corpus doc is never falsely re-queued.
        assert row["indexed_at"] == pre["indexed_at"]
        assert row["updated_at"] > pre["updated_at"]   # still bumps (harmless)

        side = await _sidecar(conn, cited)
        assert side["status"] == "archived"
        assert side["text_extracted"] is False


async def test_wall_pattern_inside_long_article_not_rejected(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    """False-positive guard, end-to-end: the SAME wall phrase appears as one
    sentence inside a genuine long article (the real france24.com shape) —
    the length gate must let it through untouched, exactly like any other
    clean extraction."""
    async with pg_pool.acquire() as conn:
        cited = await _insert_signal(
            conn, f"{http_fixture}/long_with_wall_mention",
        )
        await _insert_finding(conn, [cited], verified=True)

    data = await _run(pg_pool)
    assert data["archived"] == 1
    assert data["text_extracted"] == 1
    assert data["text_extract_rejected_boilerplate"] == 0

    async with pg_pool.acquire() as conn:
        row = await _signal(conn, cited)
        payload = json.loads(row["payload"])
        assert "Zinedine Zidane" in payload["archived_text"]
        assert "browser extensions" in payload["archived_text"]
        assert payload["archived_text_chars"] == len(payload["archived_text"])
        assert payload["archived_text_chars"] > ea._WALL_MAX_CHARS

        side = await _sidecar(conn, cited)
        assert side["text_extracted"] is True


async def test_license_gate_skips_recorded_never_silent(
    pg_pool, clean_slate, http_fixture, archive_env,
):
    await _drain_backlog(pg_pool)
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
    await _drain_backlog(pg_pool)
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
    await _drain_backlog(pg_pool)
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
    # Not one of the 5 tests the 2026-08-23 nightly actually caught (pytest
    # stops at a test's FIRST failed assert, and none of the other 5 got
    # far enough to prove whether THEIR later assertions were also
    # order-fragile) — but this test's own `fetch_failed == 1` is the exact
    # same shape, confirmed independently vulnerable during this fix's
    # synthetic cross-file-polluter proof pass. Drained for the same reason
    # as its siblings below.
    await _drain_backlog(pg_pool)
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
    await _drain_backlog(pg_pool)
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
