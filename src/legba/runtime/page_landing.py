# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a fetched PAGE lands — the database half of the ``page`` kind (lane I-2).

The handler (``data/sources/page.py``) has no database; three things a page
needs do. They live here, called from exactly three places:

1. BEFORE the run (:func:`prepare`, from ``intake_run``):

   * the HOST LICENCE LEDGER (``source_credibility.license_class``,
     migration 0192): a host the operator classified as refusing
     (``anti_ai_walled`` / ``tos_restrictive`` / ``personal_use_only``) is
     NEVER FETCHED — the request fails permanently, no bytes are asked for;
   * the PUBLISHER: a registered (active or paused) source whose own URL is
     on the page's host. Its id and ``scope.geo`` reach the handler through
     :func:`config_overlay`, so the page carries ``payload.publisher``
     (``registered_source_id``) and ``payload.publisher_origin`` exactly as
     that publisher's feed items carry it (the geo FALLBACK, never a tag on
     its own — ``data/sources/baseline.py``).

2. AT THE WRITE (:func:`land`, from the document sink, ONLY for a record
   whose ``raw_provenance.fetch_kind`` is ``page`` — every other source pays
   nothing): the document already held under the same canonical URL decides
   what happens (tier-1 identity: ``signals.canonical_url_hash``, 0247):

   * none held → the page is a NEW document. Its host credibility is read
     from the ``source_credibility`` ledger by the page's own host; an
     unknown host gets the pipeline's declared default
     (``unknown_host_credibility``) and is FLAGGED
     (``payload.page.credibility.basis = "declared_default"``); the host's
     ledger licence, when set, is stamped as ``payload.license_class``.
   * a document holding the FULL TEXT (a body of at least the follow step's
     ``FULL_BODY_CHARS``, in ``raw_body`` / ``text`` / ``archived_text``) →
     COLLAPSE: nothing is written; the request is linked to that signal and
     the page version to it (role ``page``).
   * a SUMMARY-ONLY document (a feed teaser) → FILL IN: the page's text is
     written onto THAT row as ``payload.archived_text`` (+
     ``archived_text_chars``), with ``payload.page_fill`` saying where it
     came from (the request, the parent, the page version's sha256), and the
     corpus dirty-marker contract honoured (``indexed_at = NULL``,
     ``updated_at = now()``). This is how the tree already models content
     depth: the evidence archiver fills ``archived_text`` on the feed row,
     and every reader — the desk slice, the corpus, verify, the forecast
     evidence read, the display policy — reads ``archived_text`` as the
     article behind a teaser. The feed's own ``raw_body`` (what the feed
     SAID), ``fetched_at`` and ``content_hash`` are untouched; no second
     row competes with the first in slices or dedupe.

3. AFTER the run (:func:`link_landings`, from ``intake_run``): the page
   version is linked to a collapsed / filled signal (the receipt writer only
   links rows the poll WROTE), so the intake's freshness rule finds the
   stored copy next time.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..data._url_canon import canonical_url_hash
from ..data.filters.source_credibility import extract_lookup_hosts
from ..data.intake_contract import host_of

logger = logging.getLogger("legba.runtime.source_actor")

__all__ = [
    "FORBID_FETCH_LICENSE_CLASSES", "Landing", "PagePrep", "config_overlay", "is_page_record",
    "land", "link_landings", "prepare", "run_error",
]

#: Ledger licences that mean "never fetch" (the evidence archiver's
#: FORBID_RETENTION_CLASSES; migration 0192's column comment).
FORBID_FETCH_LICENSE_CLASSES = frozenset({"anti_ai_walled", "tos_restrictive", "personal_use_only"})
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_PUBLISHER_TTL_S = 600.0
_PUBLISHERS: dict[str, Any] = {"at": 0.0, "by_host": {}}

_LEDGER_SQL = """
SELECT source_host, score, license_class FROM source_credibility WHERE source_host = ANY($1::text[])
"""
_SOURCES_SQL = """
SELECT descriptor_id, body FROM source_descriptors
 WHERE is_head AND state IN ('active', 'paused') AND kind <> 'page'
"""
_HELD_SQL = """
SELECT id, source_id, canonical_signal_id,
       left(COALESCE(payload->>'raw_body', ''), 20000) AS raw_body,
       length(COALESCE(payload->>'text', '')) AS text_chars,
       length(COALESCE(payload->>'archived_text', '')) AS archived_chars
  FROM signals
 WHERE canonical_url_hash = ANY($1::text[]) AND owner_tenant = $2
 ORDER BY (canonical_signal_id IS NULL) DESC, fetched_at ASC, id ASC
 LIMIT 5
"""
_FILL_SQL = """
UPDATE signals
   SET payload = payload || jsonb_build_object(
           'archived_text', $2::text, 'archived_text_chars', $3::int, 'page_fill', $4::jsonb),
       indexed_at = NULL,
       updated_at = now()
 WHERE id = $1
"""
_VERSION_SQL = """
SELECT id FROM fetch_versions WHERE sha256 = $1 AND role = 'page' AND receipt_kind = 'fetch'
 ORDER BY last_seen_at DESC, id DESC LIMIT 1
"""
_LINK_SQL = """
INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256)
VALUES ('signal', $1, 'page', $2, $3)
ON CONFLICT (record_kind, record_id, role) DO NOTHING
"""


def is_page_record(signal: Any) -> bool:
    return (getattr(signal, "raw_provenance", None) or {}).get("fetch_kind") == "page"


# ---------------------------------------------------------------------------
# 1. before the run
# ---------------------------------------------------------------------------


@dataclass
class PagePrep:
    refusal: str | None = None
    publisher: dict[str, Any] | None = None
    ledger: dict[str, Any] = field(default_factory=dict)


async def _ledger_row(conn: Any, host: str | None) -> dict[str, Any] | None:
    candidates = extract_lookup_hosts(host) if host else []
    if not candidates:
        return None
    rows = {r["source_host"]: r for r in await conn.fetch(_LEDGER_SQL, candidates)}
    for cand in candidates:
        if cand in rows:
            r = rows[cand]
            return {"host": cand, "score": r["score"], "license_class": r["license_class"]}
    return None


def _config_url(body: dict) -> str | None:
    cfg = body.get("config") or {}
    for key in ("url", "service_url", "base_url", "feed_url"):
        v = cfg.get(key)
        v = v.get("raw") if isinstance(v, dict) else v
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    return None


def _bare(host: str | None) -> str:
    h = (host or "").lower()
    return h[4:] if h.startswith("www.") else h


async def _publishers(conn: Any) -> dict[str, dict[str, Any]]:
    """host → the registered source on it (cached for ten minutes)."""
    if time.monotonic() - _PUBLISHERS["at"] < _PUBLISHER_TTL_S and _PUBLISHERS["by_host"]:
        return _PUBLISHERS["by_host"]
    by_host: dict[str, dict[str, Any]] = {}
    for row in await conn.fetch(_SOURCES_SQL):
        body = row["body"] if isinstance(row["body"], dict) else json.loads(row["body"])
        host = _bare(host_of(_config_url(body)))
        if not host:
            continue
        scope = body.get("scope") or {}
        cand = {"host": host, "source_id": row["descriptor_id"], "geo": list(scope.get("geo") or []),
                "source_class": scope.get("source_class")}
        prev = by_host.get(host)
        # Prefer the source that declares a publisher geo, then the lowest id.
        if prev is None or (not prev["geo"] and cand["geo"]) or (
                bool(prev["geo"]) == bool(cand["geo"]) and cand["source_id"] < prev["source_id"]):
            by_host[host] = cand
    _PUBLISHERS.update(at=time.monotonic(), by_host=by_host)
    return by_host


async def prepare(pool: Any, address: str) -> PagePrep:
    """The pre-run checks for a ``page`` pipeline run of ``address``. Never
    raises: a ledger read that fails leaves the run to proceed (logged)."""
    prep = PagePrep()
    host = host_of(address)
    try:
        async with pool.acquire() as conn:
            row = await _ledger_row(conn, host)
            if row is not None:
                prep.ledger = row
                lic = row.get("license_class")
                if lic in FORBID_FETCH_LICENSE_CLASSES:
                    prep.refusal = (f"license_refused: host {row['host']} is '{lic}' in the "
                                    "source_credibility licence ledger — never fetched")
                    return prep
            pub = (await _publishers(conn)).get(_bare(host))
            if pub is not None:
                prep.publisher = dict(pub)
    except Exception:
        logger.warning("page_landing.prepare_failed address=%s", address, exc_info=True)
    return prep


#: Page refusals that FAIL the request: never fetched for a reason a retry
#: cannot change (permanent), or a transport error (retried). Every other
#: refusal — the quality gates, an HTTP status — is a FINISHED request whose
#: result says why no document was written.
_FAIL_PERMANENT = frozenset({"robots_disallowed", "egress_blocked", "no_address"})
_FAIL_RETRY = frozenset({"transport_error"})


def run_error(page: dict[str, Any]) -> tuple[str | None, bool]:
    """``(error, permanent)`` for a page outcome that fails its request,
    else ``(None, False)``."""
    reason = page.get("refusal")
    if reason in _FAIL_PERMANENT:
        return f"{reason}: {page.get('detail')}"[:2000], True
    if reason in _FAIL_RETRY:
        return f"not_fetched: {reason}: {page.get('detail')}"[:2000], False
    return None, False


def config_overlay(run: Any, prep: PagePrep) -> dict[str, Any]:
    """What a page run adds to the pipeline's config: the request it serves
    and the publisher resolved above (``PageConfig.intake``/``publisher``)."""
    return {"intake": run.provenance(), "publisher": prep.publisher}


# ---------------------------------------------------------------------------
# 2. at the write
# ---------------------------------------------------------------------------


@dataclass
class Landing:
    action: str                    # "new" | "collapsed" | "filled"
    existing_signal_id: Any = None
    body_sha256: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_result(self) -> dict[str, Any]:
        return {"action": self.action, "existing_signal_id": str(self.existing_signal_id)
                if self.existing_signal_id else None, **self.detail}


def _body_chars(raw_body: str | None) -> int:
    return len(_WS.sub(" ", _TAG.sub(" ", raw_body or "")).strip())


async def _credibility(conn: Any, signal: Any) -> None:
    page = signal.payload.setdefault("page", {})
    host = host_of(signal.canonical_url)
    try:
        row = await _ledger_row(conn, host)
    except Exception:
        logger.warning("page_landing.ledger_failed host=%s", host, exc_info=True)
        row = None
    if row is not None and row.get("score") is not None:
        signal.source_credibility = float(row["score"])
        page["credibility"] = {"basis": "ledger", "host": row["host"], "score": float(row["score"])}
    elif page.get("credibility_default") is not None:
        signal.source_credibility = float(page["credibility_default"])
        page["credibility"] = {"basis": "declared_default", "host": host, "flag": "unknown_host",
                               "score": signal.source_credibility}
    else:
        page["credibility"] = {"basis": "none", "host": host, "flag": "unknown_host"}
    if row is not None and row.get("license_class") and "license_class" not in signal.payload:
        signal.payload["license_class"] = row["license_class"]


async def land(conn: Any, signal: Any, *, owner_tenant: str) -> Landing:
    """Decide how ONE page record lands (module docstring, part 2). On
    ``collapsed``/``filled`` the caller writes nothing."""
    from .intake_follow import FULL_BODY_CHARS  # noqa: PLC0415

    page = signal.payload.get("page") or {}
    sha = page.get("body_sha256")
    urls = {u for u in (signal.canonical_url, page.get("final_url"), page.get("requested_url")) if u}
    hashes = sorted({canonical_url_hash(u) for u in urls})
    held = await conn.fetch(_HELD_SQL, hashes, owner_tenant) if hashes else []
    if not held:
        await _credibility(conn, signal)
        return Landing("new", body_sha256=sha)
    target = held[0]
    best = max(_body_chars(target["raw_body"]), int(target["text_chars"] or 0), int(target["archived_chars"] or 0))
    detail = {"existing_source_id": target["source_id"], "existing_body_chars": best}
    text = signal.payload.get("raw_body") or ""
    if best >= FULL_BODY_CHARS or len(text) <= best:
        # The held document already has the article (or more text than this
        # fetch got): nothing to add.
        return Landing("collapsed", target["id"], sha, detail)
    fill = {
        "via": signal.source_id, "fetched_at": page.get("fetched_at"), "final_url": page.get("final_url"),
        "body_sha256": sha, "title": signal.payload.get("title"),
        "published_at": signal.payload.get("published_at"),
        "published_basis": page.get("published_basis"), "intake": signal.payload.get("intake"),
    }
    await conn.execute(_FILL_SQL, target["id"], text, len(text), json.dumps(fill, default=str))
    return Landing("filled", target["id"], sha, {**detail, "filled_chars": len(text)})


# ---------------------------------------------------------------------------
# 3. after the run
# ---------------------------------------------------------------------------


async def link_landings(pool: Any, landings: list[Landing]) -> None:
    """Link the page version to each collapsed / filled signal (best-effort)."""
    todo = [lnd for lnd in landings if lnd.action in ("collapsed", "filled") and lnd.body_sha256]
    if not todo:
        return
    try:
        async with pool.acquire() as conn:
            for lnd in todo:
                vid = await conn.fetchval(_VERSION_SQL, lnd.body_sha256)
                if vid is not None:
                    await conn.execute(_LINK_SQL, lnd.existing_signal_id, int(vid), lnd.body_sha256)
    except Exception:
        logger.warning("page_landing.link_failed", exc_info=True)
