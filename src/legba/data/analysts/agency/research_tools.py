# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``research`` pack's one tool — ``web_evidence`` (R-A, the write path).

``web_search`` finds pages and hands the planner their titles. ``web_evidence``
finds pages and **lands them in the substrate as evidence**: one ordinary
``signals`` row per kept hit, tagged ``retrieval_origin='web_search:<provider>'``,
archived where the licence ledger clears the host, capped at the credibility
ceiling, stamped with the question that dispatched it — and returned as
``rows`` so the planner can cite them ``[N]`` like any other document.

WHY THIS IS A NEW PACK AND NOT A TOOL INSIDE ``web_access``
-----------------------------------------------------------
Three census facts, none of them stylistic:

1. **``web_tools.py`` is read-only BY CONTRACT** (its own module docstring:
   "neither tool writes to the substrate — that is ``write_tools.py``"). The
   plane's discipline puts writes in their own pack.
2. **Tool RESULTS are deliberately never recorded** (``agency.py``'s
   ``_account_tool_call``: a ``substrate_read`` result can be the whole slice
   and is reconstructable by re-running the call). So the post-persist
   converter pattern — otherwise the right precedent — **cannot see the
   fetched bytes**. The write must happen at TOOL time. There is no other
   place it can happen.
3. **The cost cap has nowhere else to live.** ``web_access``'s governor
   declares ``max_cost_usd_per_day: null`` and, worse, a
   ``budget_account: web_access`` that OVERRIDES the binding's per-analyst
   account — so every ``web_access`` caller shares one rate window. Adding a
   paid provider's money cap there would silently re-budget the standing
   auditor, which is the R4 window's own instrument.

So: a new pack, its own governor, its own ``budget_account``, its own
``max_cost_usd_per_day``. Rollback is one descriptor state flip that leaves the
auditor and consult untouched, and ``web_access`` stays byte-identical (a test
pins that its handlers still perform no substrate write).

WHAT IS REUSED, AND IT IS NEARLY EVERYTHING
--------------------------------------------
The provider ladder (``resolve_tool_search_route``), the four honest outcomes,
``SearchStatus``, the single control-probe canary
(``verify_engine_liveness``/``apply_liveness``), ``compute_deferral`` and its
three deferral rules, ``HardSearchFailure``/``TransientSearchFailure``, and the
SSRF-guarded transport are all IMPORTED from the same modules ``web_search``
uses. Every failure shape is ``web_search``'s, unchanged, because the honesty
argument is identical and already ruled: an empty is never a ``completed``
count-0 result. This tool's only new behaviour is: land signals, archive bytes,
return ids.

ONE NEW FAILURE SHAPE, and it is loud: ``research_writeback_unavailable`` — the
pack was granted but no write surface is bound. A HARD failure with NO
deferral, mirroring ``search_provider_unresolved``'s reasoning: waiting cannot
fix a binding gap, and "the write path is missing" must never share a wire
shape with "the web has nothing".

TWO DEPTHS, AND THE ROWS ONLY CARRY ONE OF THEM
------------------------------------------------
``rows`` (the ``[N]``-citable list) carries **full_text hits only**. A teaser
hit has no body, and ``inline_target``'s numbering guard says exactly why that
matters: a numbered citation with ``source_text=None`` is a TITLE-ONLY citation
and earns the finding a spurious faithfulness DEMOTION. Teaser hits are landed,
tagged, counted and named to the planner in ``teaser_hits`` as prose — the same
treatment ``search_signals`` gets — and they reach DESKS through the slice,
which is the path that actually matters for the program.

A teaser hit whose PROVIDER SNIPPET is itself empty or whitespace-only is a
THIRD case, and it is not landed at all: at teaser depth the snippet IS the
text (no bytes are fetched), so an empty snippet is zero-text substrate — a
false increment to "N signals" that no desk could ever read. It is SKIPPED,
counted in ``skipped_empty_snippet``, and named (with its URL) in ``refused``
alongside the licence/robots/egress refusals. A full_text hit is never
affected — its text comes from extraction, not the snippet.
"""

from __future__ import annotations

import logging
from dataclasses import replace as dataclass_replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...archive import archive_root
from ...research_evidence import (
    DEPTH_FORBIDDEN,
    DEPTH_FULL_TEXT,
    DEPTH_TEASER,
    REASON_FETCH_BLOCKED,
    REASON_FETCH_FAILED,
    REASON_FETCH_NOT_REQUESTED,
    REASON_ROBOTS_DISALLOWED,
    ProviderContext,
    ResearchDispatch,
    ResearchHit,
    build_research_row,
    content_hash_for,
    depth_for_license,
    detect_challenge_page,
    extract_archived_text,
    land_research_row,
    probe_novelty,
    record_archive_status,
    research_source_id,
    resolve_host_verdict,
    stamp_archived_signal,
    store_archive_bytes,
)
from ...research_flag import (
    RESEARCH_EVIDENCE_ENV,
    RESEARCH_OFF,
    research_evidence_mode,
)
from ...retrieval_origin import web_search_origin
from ...schemas.action_pack import ActionPack
from ...sources._egress import (
    EgressBlockedError,
    assert_public_host,
    fetch_client,
    guarded_async_client,
)
from ...stack.search import (
    DEFAULT_LIVENESS_CACHE,
    HardSearchFailure,
    LivenessVerdict,
    SearchStatus,
    TransientSearchFailure,
    apply_liveness,
    compute_deferral,
    resolve_tool_search_route,
    verify_engine_liveness,
)
from ..deterministic_handlers._challenge_detect import (
    BLOCKED_BY_CHALLENGE,
    challenge_from_status,
)
from .robots import RobotsCache, robots_decision
from .robots import ALLOWED as ROBOTS_ALLOWED
from .robots import NO_RULES as ROBOTS_NO_RULES
from .robots import ROBOTS_USER_AGENT
from .tools import ToolCall, ToolContext, ToolResult

logger = logging.getLogger(__name__)

RESEARCH_PACK_ID = "research"

RESEARCH_TOOLS = ("web_evidence",)

#: Result cap per call. Same bound as ``web_search``'s and as the stack layer's
#: ``MAX_RESULTS_CAP`` — the planner asks for a page of hits, not a crawl, and a
#: page of hits from the deployed engine set is ~28, not 10.
_MAX_RESULTS = 30

#: Body cap for ONE archived page. Matches the archiver's own default; a page
#: past it is refused as ``skipped_size`` rather than truncated, because a
#: truncated body would content-address to a hash that is not the page.
_MAX_PAGE_BYTES = 5_000_000

#: Cap on the extracted main text stored in ``payload.text`` /
#: ``payload.archived_text``.
_MAX_TEXT_CHARS = 200_000

_DEFAULT_TIMEOUT_SECONDS = 20.0

#: The slice window novelty is measured against when no analyst descriptor is
#: in hand at tool time. 24h is ``resolve_slice_window_hours``'s default and
#: the window 8 of 9 desks run; the value is STAMPED on the novelty block
#: (``slice_window_hours``) so a reader never has to guess which window a
#: measurement used.
_DEFAULT_SLICE_WINDOW_HOURS = 24


def _tool_config(pack: ActionPack, tool_name: str) -> dict[str, Any]:
    for t in pack.tools:
        if t.name == tool_name:
            return dict(t.config)
    return {}


def _provider_language(result: Any) -> str | None:
    """The provider's own language label, or ``None``. NEVER detected here.

    A guessed language on an unreviewed page is exactly the kind of fabricated
    enrichment the ingest baseline exists to do properly; the re-enrichment
    sweeps own it.
    """
    raw = getattr(result, "raw", None)
    if isinstance(raw, dict):
        for key in ("language", "lang"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:16]
    return None


async def _fetch_page(
    client: httpx.AsyncClient, url: str, *, max_bytes: int,
) -> tuple[bytes, str | None, str | None, int, str]:
    """Stream one page under the size cap → ``(body, content_type, encoding,
    status_code, final_url)``.

    Mirrors ``evidence_archiver._fetch_bytes`` — the declared-Content-Length
    check first, then the cap enforced on the actual stream so a lying header
    cannot blow it — and could not simply CALL it: that helper discards
    ``resp.url`` and ``resp.status_code``, and R-A must record both (the
    POST-REDIRECT url is the row's ``canonical_url``, never the search
    provider's redirect wrapper, and the status goes into ``raw_provenance``).
    """
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise _PageTooLargeError(
                        f"declared {declared} bytes > cap {max_bytes}"
                    )
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise _PageTooLargeError(f"stream exceeded cap {max_bytes}")
            chunks.append(chunk)
        return (
            b"".join(chunks),
            resp.headers.get("content-type"),
            resp.charset_encoding,
            resp.status_code,
            str(resp.url),
        )


class _PageTooLargeError(Exception):
    """The page exceeded the per-object size cap."""


#: The statuses that make a ``hypotheses`` row a STANDING QUESTION rather than
#: an ACH hypothesis. ``in_progress`` is the claim a dispatched research run
#: holds (``runtime.dispatched_question.CLAIMED_STATUS``) — before this was
#: listed, a run answering its OWN assignment landed rows stamped
#: ``kind='hypothesis'``, because the claim it had just taken moved the row out
#: of ``open_question``.
_STANDING_QUESTION_STATUSES = frozenset({"open_question", "in_progress"})


def _dispatch_kind(row: Any) -> str:
    """What KIND of question this row is, for ``dispatch.kind`` on every landed
    row: the HARVEST CLASS when the question was dispatched by an alert
    (``coverage_floor`` / ``reference_gap``), plain ``open_question`` for a
    harvested one, ``hypothesis`` for an ACH row.

    The harvest class is the fact a reader of a landed signal actually wants —
    "this evidence was fetched to close a measured coverage gap" — and the
    dataclass has declared it in its own vocabulary since R-A
    (``open_question | coverage_floor | self_selected``) without anything ever
    producing it. Read through ``grounding.harvest_class_of``, the same marker
    reader the ranker and the assignment selector use, so the three cannot
    disagree about what class a question is.
    """
    if row["status"] not in _STANDING_QUESTION_STATUSES:
        return "hypothesis"
    from ....runtime.dispatched_question import DISPATCHED_HARVEST_CLASSES
    from ....runtime.grounding import harvest_class_of

    harvest_class = harvest_class_of(row["diagnostic_evidence"])
    return harvest_class if harvest_class in DISPATCHED_HARVEST_CLASSES else "open_question"


async def _resolve_dispatch(conn: Any, call: ToolCall, analyst_ctx: Any) -> ResearchDispatch:
    """WHO asked, and therefore WHICH desk (if any) this evidence can reach.

    The ``geo`` that decides desk reachability is read from the TARGET
    DESCRIPTOR — operator-owned — and never from the planner's arguments. The
    planner's only influence is the ``hypothesis_id`` it may name, and that id
    has to EXIST in ``hypotheses`` to resolve to anything; the target comes off
    that row, not off the argument.

    Resolution order:
      1. ``args.hypothesis_id`` naming a real ``hypotheses`` row → its
         ``target_id`` + ``thesis`` (the standing question this run is draining);
      2. the RUN's own ``target_id`` (a target-bound analyst calling the tool);
      3. neither → a SELF-SELECTED run, which carries no geo and therefore
         reaches NO desk. That is a real limit and it is recorded on the row
         (``novelty.scope = "none"``) rather than hidden.
    """
    args = call.args or {}
    hypothesis_id = str(args.get("hypothesis_id") or "").strip()
    target_id = getattr(analyst_ctx, "target_id", None)
    kind = "self_selected"
    bounded_question = ""
    resolved_hypothesis: str | None = None

    if hypothesis_id:
        try:
            row = await conn.fetchrow(
                "SELECT id::text AS id, target_id, thesis, status, "
                "diagnostic_evidence FROM hypotheses WHERE id = $1::uuid",
                hypothesis_id,
            )
        except Exception:  # a malformed id is a planner typo, not an error
            row = None
        if row is not None:
            resolved_hypothesis = row["id"]
            target_id = row["target_id"] or target_id
            bounded_question = str(row["thesis"] or "")[:2000]
            kind = _dispatch_kind(row)

    geo: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    if target_id:
        try:
            trow = await conn.fetchrow(
                "SELECT body FROM target_descriptors "
                "WHERE descriptor_id = $1 AND is_head = TRUE",
                target_id,
            )
        except Exception:  # pragma: no cover — defensive
            trow = None
        if trow is not None and trow["body"]:
            body = trow["body"]
            if isinstance(body, str):
                import json as _json

                try:
                    body = _json.loads(body)
                except Exception:
                    body = {}
            scope = (body or {}).get("scope") or {}
            geo = tuple(str(g) for g in (scope.get("geo") or []) if g)
            tags = tuple(str(t) for t in (scope.get("tags") or []) if t)
    return ResearchDispatch(
        kind=kind,
        hypothesis_id=resolved_hypothesis,
        target_id=target_id,
        bounded_question=bounded_question,
        geo=geo,
        tags=tags,
        window_hours=_DEFAULT_SLICE_WINDOW_HOURS,
    )


def _zero_counters() -> dict[str, int]:
    return {
        "landed": 0,
        "duplicate": 0,
        "archived": 0,
        "teaser": 0,
        "refused_license": 0,
        "refused_robots": 0,
        "refused_egress": 0,
        "fetch_failed": 0,
        # (a′) Of the fetch outcomes above, the ones a publisher's EDGE
        # refused: an anti-bot challenge/interstitial body, or a 401/403/429.
        # A planner reading a zero here alongside zero rows knows the web was
        # empty; a non-zero says it was CLOSED. Conflating the two is the
        # false-absence defect (FETCH_REVIEW §2) — it is why this counter, not
        # a log line, is the carry.
        BLOCKED_BY_CHALLENGE: 0,
        # A teaser-depth hit whose snippet is empty/whitespace has NO text to
        # land — the provider snippet IS the text at this depth (no bytes are
        # fetched), so an empty snippet means zero-text substrate. SKIPPED, not
        # landed, and counted here so "N signals" stays an honest count of rows
        # a desk can actually read. Never fires for a full_text hit (bytes
        # fetched ⇒ untouched by this guard).
        "skipped_empty_snippet": 0,
    }


async def _land_one(
    conn: Any,
    pool: Any,
    *,
    result: Any,
    provider: ProviderContext,
    dispatch: ResearchDispatch,
    query: str,
    run_id: Any,
    requested_by: str,
    regime: str,
    fetch: bool,
    robots_cache: RobotsCache,
    counters: dict[str, int],
    refused: list[dict[str, Any]],
    timeout: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Land ONE provider hit. Returns ``(row_entry, teaser_entry)`` — at most
    one of the two is non-None; both are None when the hit was refused.
    """
    url = str(getattr(result, "url", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None, None

    # THE SSRF GUARD, ASKED FIRST AND ANSWERED ONCE. The guarded transport
    # would refuse a private / loopback / link-local / metadata address before
    # connect anyway — but only at the moment of a fetch, and by then a teaser
    # row naming that URL would already be on its way into the substrate. A hit
    # pointing at 169.254.169.254 is not evidence at any depth, so it is
    # dropped ENTIRELY: nothing fetched, nothing landed, and the refusal
    # counted where an operator can see it.
    parts = urlsplit(url)
    try:
        assert_public_host(
            parts.hostname or "",
            parts.port or (443 if parts.scheme == "https" else 80),
        )
    except EgressBlockedError as exc:
        counters["refused_egress"] += 1
        refused.append(
            {"url": url, "host": parts.hostname or "", "reason": "egress_blocked"}
        )
        logger.warning("web_evidence.egress_blocked url=%s err=%s", url, exc)
        return None, None

    verdict = await resolve_host_verdict(conn, url)
    depth, depth_reason = depth_for_license(verdict.license_class)

    if depth == DEPTH_FORBIDDEN:
        # NEVER FETCHED, and no row is written at all (spec §1.5). Nothing to
        # key an ``evidence_archive`` sidecar on either — that table's PK is
        # ``signal_id`` and there is no signal — so the refusal is surfaced in
        # the tool output + counters, which is where a planner and an operator
        # can both see it.
        counters["refused_license"] += 1
        refused.append(
            {
                "url": url,
                "host": verdict.host,
                "reason": "license_forbids",
                "license_class": verdict.license_class,
            }
        )
        logger.info(
            "web_evidence.refused_license host=%s class=%s", verdict.host,
            verdict.license_class,
        )
        return None, None

    snippet = str(getattr(result, "snippet", "") or "")
    title = str(getattr(result, "title", "") or "")
    text = snippet
    body: bytes | None = None
    content_type: str | None = None
    encoding: str | None = None
    http_status: int | None = None
    final_url = url
    extract_source: str | None = None
    fetch_error: str | None = None
    #: (a′) The challenge tell, when a publisher's edge refused us. Carried
    #: onto the landed row so a later reader — not just the planner holding
    #: this call's counters — can still tell a block from an empty web.
    blocked_tell: str | None = None

    if depth == DEPTH_FULL_TEXT and not fetch:
        depth, depth_reason = DEPTH_TEASER, REASON_FETCH_NOT_REQUESTED
    elif depth == DEPTH_FULL_TEXT:
        # ROBOTS FIRST — before any connection to the page itself.
        decision = await robots_decision(
            url, cache=robots_cache, user_agent=ROBOTS_USER_AGENT,
        )
        if decision not in (ROBOTS_ALLOWED, ROBOTS_NO_RULES):
            counters["refused_robots"] += 1
            depth, depth_reason = DEPTH_TEASER, REASON_ROBOTS_DISALLOWED
            logger.info(
                "web_evidence.robots_refused url=%s decision=%s", url, decision,
            )
        else:
            try:
                async with fetch_client(
                    # ``guarded=`` hands ``fetch_client`` THIS module's own
                    # ``guarded_async_client``, so the flag-off path is the exact call
                    # this site made before, through the exact name the existing e2e
                    # suites monkeypatch. See _egress.fetch_client's docstring.
                    guarded=guarded_async_client,
                    follow_redirects=True,
                    timeout=timeout,
                    headers={"User-Agent": ROBOTS_USER_AGENT},
                ) as client:
                    body, content_type, encoding, http_status, final_url = (
                        await _fetch_page(client, url, max_bytes=_MAX_PAGE_BYTES)
                    )
            except (EgressBlockedError, _PageTooLargeError, httpx.HTTPError) as exc:
                counters["fetch_failed"] += 1
                fetch_error = f"{type(exc).__name__}: {exc}"
                depth, depth_reason = DEPTH_TEASER, REASON_FETCH_FAILED
                body = None
                # (a′) ``_fetch_page`` calls ``raise_for_status()`` INSIDE the
                # streaming context, so a 403's body is gone by the time we get
                # here. The status alone is enough: 401/403/429 is a publisher
                # refusing us, and must not be reported as an empty web.
                response = getattr(exc, "response", None)
                http_status = getattr(response, "status_code", None)
                blocked_tell = challenge_from_status(http_status)
                if blocked_tell is not None:
                    counters[BLOCKED_BY_CHALLENGE] += 1
                    depth_reason = REASON_FETCH_BLOCKED
                    fetch_error = f"{BLOCKED_BY_CHALLENGE}: {blocked_tell}"
                    logger.info(
                        "web_evidence.%s url=%s tell=%s",
                        BLOCKED_BY_CHALLENGE, url, blocked_tell,
                    )
            if body is not None:
                extracted = extract_archived_text(
                    body, content_type, encoding, max_chars=_MAX_TEXT_CHARS,
                )
                if extracted:
                    text = extracted
                    extract_source = "legba_trafilatura"
                elif getattr(result, "extracted_text", None):
                    # C-3: the provider returned genuine main text. The BYTES
                    # are ours either way; only the derived text is theirs.
                    text = str(result.extracted_text)[:_MAX_TEXT_CHARS]
                    extract_source = "provider"
                else:
                    # Bytes landed but no readable main text (a wall page, a
                    # PDF, a Trafilatura miss). Not a full_text hit: numbering
                    # a body-less row is the demotion this guard exists for.
                    depth, depth_reason = DEPTH_TEASER, REASON_FETCH_FAILED
                    fetch_error = "no_main_text_extracted"
                    # (a′) …but WHICH of those was it? A 200 carrying a
                    # Cloudflare/DataDome interstitial is a BLOCK, and the
                    # tells for it (``var dd={'rt'…``, ``/cdn-cgi/challenge-
                    # platform``) live in the raw markup, never in the
                    # extraction that just came back empty.
                    blocked_tell = detect_challenge_page(
                        body, content_type, encoding,
                    )
                    if blocked_tell is not None:
                        counters[BLOCKED_BY_CHALLENGE] += 1
                        depth_reason = REASON_FETCH_BLOCKED
                        fetch_error = f"{BLOCKED_BY_CHALLENGE}: {blocked_tell}"
                        logger.warning(
                            "web_evidence.%s url=%s tell=%s bytes=%d",
                            BLOCKED_BY_CHALLENGE, url, blocked_tell, len(body),
                        )

    # THE EMPTY-TEASER GUARD. Every path above that lands at TEASER depth
    # (unreviewed licence, fetch-not-requested, robots-disallowed, or a
    # downgrade from a failed/textless fetch) leaves ``text`` exactly equal to
    # ``snippet`` — no bytes are ever fetched at this depth, so the provider
    # snippet IS the text (spec: teaser depth stores no other body). A hit
    # whose snippet is empty or whitespace-only therefore has NO text to land:
    # writing it anyway would be a zero-text substrate row inflating the
    # "N signals" count with nothing a desk can read. It is SKIPPED — never
    # landed — and counted so the count stays honest. Fabricating text from
    # the title is refused on purpose (``content_hash_for``'s docstring: a
    # title-only hash would make two unrelated teasers look like the same
    # content). A full_text hit never reaches this check: bytes were fetched
    # and ``text`` came from extraction, not the snippet.
    if depth == DEPTH_TEASER and not text.strip():
        counters["skipped_empty_snippet"] += 1
        refused.append(
            {
                "url": url,
                "host": verdict.host,
                "reason": "empty_snippet",
                "depth_reason": depth_reason,
            }
        )
        logger.info(
            "web_evidence.skipped_empty_snippet url=%s host=%s depth_reason=%s",
            url, verdict.host, depth_reason,
        )
        return None, None

    hit = ResearchHit(
        url=final_url,
        title=title,
        snippet=snippet,
        text=text,
        published_at=getattr(result, "published_at", None),
        language=_provider_language(result),
        engine=getattr(result, "engine", None),
        rank=int(getattr(result, "rank", 0) or 0),
        depth=depth,
        depth_reason=depth_reason,
        license_class=verdict.license_class,
        host_score=verdict.score,
        http_status=http_status,
        content_type=content_type,
        url_chain=(url, final_url) if final_url != url else (url,),
        query=query,
    )
    novelty = await probe_novelty(
        conn,
        canonical_url=hit.url,
        content_hash=content_hash_for(hit.text),
        host=verdict.host,
        target_id=dispatch.target_id,
        target_geo=dispatch.geo,
        window_hours=dispatch.window_hours,
        regime=regime,
    )
    hit = dataclass_replace(hit, novelty=novelty)

    row = build_research_row(
        hit, provider, dispatch,
        run_id=run_id, requested_by=requested_by, regime=regime,
    )
    if extract_source:
        row["payload"]["research"]["extract_source"] = extract_source
    if fetch_error:
        row["payload"]["research"]["fetch_error"] = fetch_error[:512]
    if blocked_tell is not None:
        # The durable half of the carry: the counters die with the tool call,
        # this outlives it on the row.
        row["payload"]["research"][BLOCKED_BY_CHALLENGE] = blocked_tell[:128]

    landed = await land_research_row(conn, row)
    signal_id = row["id"]
    if landed is None:
        counters["duplicate"] += 1
    else:
        counters["landed"] += 1

    origin = web_search_origin(provider.component_id)
    if depth == DEPTH_FULL_TEXT and body is not None:
        root = archive_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            digest, object_ref, _existed = store_archive_bytes(root, body)
        except OSError as exc:
            counters["fetch_failed"] += 1
            await record_archive_status(
                pool, signal_id=signal_id, status="failed", url=hit.url,
                license_class=verdict.license_class, retrieval_origin=origin,
                last_error=f"store failed: {exc}",
            )
        else:
            # ARCHIVE-THEN-INDEX: bytes on disk, THEN the sidecar row, THEN the
            # signal's object_ref stamp (which also re-queues the corpus doc).
            await record_archive_status(
                pool, signal_id=signal_id, status="archived", url=hit.url,
                license_class=verdict.license_class, retrieval_origin=origin,
                object_ref=object_ref, sha256=digest, size_bytes=len(body),
                content_type=content_type, text_extracted=True,
            )
            await stamp_archived_signal(
                conn, signal_id=signal_id, object_ref=object_ref,
                archived_text=hit.text,
            )
            counters["archived"] += 1
            return (
                {
                    "id": str(signal_id),
                    "source": {
                        "title": hit.title,
                        "raw_body": hit.text,
                        "canonical_url": hit.url,
                        "published_at": hit.published_at,
                        "source_id": research_source_id(provider.component_id),
                        "retrieval_origin": origin,
                        "license_class": verdict.license_class,
                        "research_depth": DEPTH_FULL_TEXT,
                        "fetched_at": row["fetched_at"].isoformat(),
                    },
                },
                None,
            )

    # TEASER depth — landed, tagged, reachable by a desk slice, and NOT
    # numbered. The sidecar records WHY no bytes were kept, which is the first
    # time ``skipped_license_unreviewed`` has ever had an occasion to fire.
    counters["teaser"] += 1
    await record_archive_status(
        pool,
        signal_id=signal_id,
        status="skipped_license_unreviewed",
        url=hit.url,
        license_class=verdict.license_class,
        retrieval_origin=origin,
        attempts=0,
        last_error=(
            (f"{BLOCKED_BY_CHALLENGE}: {blocked_tell} — " if blocked_tell else "")
            + f"research depth={DEPTH_TEASER} reason={depth_reason} — bytes NOT "
            f"archived (host {verdict.host!r} license_class="
            f"{verdict.license_class!r}); teaser text only"
        ),
    )
    return (
        None,
        {
            "id": str(signal_id),
            "title": hit.title,
            "url": hit.url,
            "snippet": snippet,
            "depth": DEPTH_TEASER,
            "depth_reason": depth_reason,
            **(
                {BLOCKED_BY_CHALLENGE: blocked_tell}
                if blocked_tell is not None else {}
            ),
        },
    )


async def web_evidence_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Search, LAND the hits as evidence, and return the citable ones.

    ``args``:
      * ``query`` (required) — the search query string (planner-supplied).
      * ``limit`` (optional) — max hits, clamped to ``_MAX_RESULTS``.
      * ``fetch`` (optional, default true) — attempt full-text depth on a
        licence-cleared host. ``false`` lands every hit at teaser depth.
      * ``hypothesis_id`` (optional) — the standing question this run is
        draining. It is the only way a hit inherits a desk's ``geo``, and the
        geo comes off the TARGET DESCRIPTOR, never off this argument.
    """
    query = str((call.args or {}).get("query", "")).strip()
    if not query:
        return ToolResult(status="failed", error="web_evidence requires a 'query' arg")

    # ---- THE FLAG (spec §6.2). Granted-but-REFUSES, loudly, no write. ------
    regime = research_evidence_mode()
    if regime == RESEARCH_OFF:
        return ToolResult(
            status="failed",
            error=(
                "research_evidence_disabled: the research pack is granted but "
                f"{RESEARCH_EVIDENCE_ENV} is 'off', so NO query was issued and "
                "NO evidence was landed. This is a configuration state, not a "
                "statement about the web. Work from substrate evidence and "
                "leave any standing question open."
            ),
            output={
                "regime": regime,
                "flag": RESEARCH_EVIDENCE_ENV,
                "landed": 0,
                "rows": [],
                "teaser_hits": [],
            },
        )

    # ---- THE WRITE SURFACE. A hard failure with NO deferral. ---------------
    writeback = getattr(ctx, "writeback", None)
    pool = getattr(writeback, "pg_pool", None)
    analyst_ctx = getattr(writeback, "analyst_ctx", None)
    if pool is None or analyst_ctx is None:
        logger.error(
            "web_evidence.writeback_unavailable pool=%s analyst_ctx=%s — the "
            "research pack is granted but the runtime bound no write surface",
            pool is not None, analyst_ctx is not None,
        )
        return ToolResult(
            status="failed",
            error=(
                "research_writeback_unavailable: the research pack is granted "
                "but no substrate write surface is bound on this run — NO query "
                "was issued and NO evidence was landed. Waiting cannot fix a "
                "binding gap; this needs an operator."
            ),
            output={"regime": regime},
        )

    cfg = _tool_config(pack, "web_evidence")
    limit = max(1, min(_MAX_RESULTS, int((call.args or {}).get("limit", 5) or 5)))
    fetch = bool((call.args or {}).get("fetch", True))
    timeout = float(cfg.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
    cache = getattr(ctx, "search_liveness", None) or DEFAULT_LIVENESS_CACHE

    # ---- resolve the provider (the SAME ladder web_search documents) -------
    route = resolve_tool_search_route(cfg)
    bound = getattr(ctx, "search", None)
    if bound is None:
        component = route.component_id if route is not None else "(none declared)"
        source = route.source if route is not None else "no config.provider"
        logger.warning(
            "web_evidence.provider_unresolved component=%s source=%s",
            component, source,
        )
        advice = compute_deferral(
            "search_provider_unresolved",
            provider_key=component,
            cache=cache,
            detail=f"declared at {source}, not bound on this run",
        )
        return ToolResult(
            status="failed",
            error=(
                f"search_provider_unresolved: web_evidence routes to "
                f"{component!r} ({source}) but no search provider is bound on "
                "this run — NO query was issued. This is not an empty result set."
            ),
            output={"deferral": advice.to_dict(), "regime": regime},
        )
    handler = bound
    provider_label = (
        route.component_id if route is not None
        else getattr(bound, "component_id", "") or "runtime-bound"
    )

    extra_params = cfg.get("params") if isinstance(cfg.get("params"), dict) else None
    try:
        response = await handler.search(query, limit=limit, params=extra_params)
    except TransientSearchFailure as exc:
        logger.warning("web_evidence.transient provider=%s err=%s", provider_label, exc)
        advice = compute_deferral(
            "search_unavailable", provider_key=provider_label, cache=cache,
            detail=str(exc),
        )
        return ToolResult(
            status="failed", error=f"search_unavailable: {exc!s}",
            output={"deferral": advice.to_dict(), "regime": regime},
        )
    except HardSearchFailure as exc:
        logger.warning("web_evidence.hard_failure provider=%s err=%s", provider_label, exc)
        return ToolResult(status="failed", error=str(exc), output={"regime": regime})

    if response.status is SearchStatus.EMPTY:
        v, detail = await verify_engine_liveness(
            handler, provider_key=provider_label, cache=cache,
        )
        apply_liveness(response, v, detail)

    output = response.to_tool_output()
    output["provider"] = provider_label
    output["regime"] = regime
    if route is not None:
        output["provider_route"] = route.source
        output["provider_route_class"] = route.route_class

    if response.status in (SearchStatus.DEGRADED_EMPTY, SearchStatus.EMPTY):
        # EVERY hit was lost — to admitted degradation, or to a control probe
        # that could not show the engine set answering. ZERO signals land: a
        # `completed` count-0 result is exactly the shape a planner summarizes
        # as "no results found", and this state is UNKNOWN, not absence.
        probe_failed = response.liveness in (
            LivenessVerdict.DEAD, LivenessVerdict.PROBE_FAILED,
        )
        reason = (
            "search_liveness_unverified"
            if (probe_failed or response.status is SearchStatus.EMPTY)
            else "search_degraded_no_results"
        )
        advice = compute_deferral(
            reason, provider_key=provider_label, cache=cache,
            detail=response.degraded_detail or response.liveness_detail,
        )
        output["deferral"] = advice.to_dict()
        output.update({"landed": 0, "rows": [], "teaser_hits": []})
        detail = response.degraded_detail or response.liveness_detail or "no detail"
        error = (
            f"{reason}: the search returned zero results ({detail}) — this is "
            "UNKNOWN, not absence, and NO evidence was landed. Do NOT conclude "
            "that no evidence exists."
        )
        logger.warning(
            "web_evidence.%s provider=%s liveness=%s", reason, provider_label,
            response.liveness.value,
        )
        return ToolResult(status="failed", error=error, output=output, units=1)

    provider = ProviderContext(
        component_id=provider_label,
        subprovider=response.subprovider,
        route_class=(route.route_class if route is not None else ""),
        # The provider BUILD, when the bound handler carries one. Empty
        # otherwise — the column default, and never a fabricated hash: the
        # component id inside ``retrieval_origin`` is what makes a claim
        # attributable, and a version sharpens it when it is available.
        version=str(getattr(handler, "component_version", "") or ""),
        degraded=response.degraded,
        degraded_detail=response.degraded_detail,
        unresponsive_engines=tuple(response.unresponsive_engines),
    )
    counters = _zero_counters()
    refused: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    teaser_hits: list[dict[str, Any]] = []
    robots_cache = getattr(ctx, "robots_cache", None) or RobotsCache()
    requested_by = call.requested_by or "system"
    run_id = getattr(analyst_ctx, "run_id", None)

    async with pool.acquire() as conn:
        dispatch = await _resolve_dispatch(conn, call, analyst_ctx)
        for result in response.results[:limit]:
            try:
                row_entry, teaser_entry = await _land_one(
                    conn, pool,
                    result=result, provider=provider, dispatch=dispatch,
                    query=query, run_id=run_id, requested_by=requested_by,
                    regime=regime, fetch=fetch, robots_cache=robots_cache,
                    counters=counters, refused=refused, timeout=timeout,
                )
            except Exception as exc:  # one bad hit must not lose the others
                counters["fetch_failed"] += 1
                logger.warning(
                    "web_evidence.hit_failed url=%s err=%r",
                    getattr(result, "url", "?"), exc,
                )
                continue
            if row_entry is not None:
                rows.append(row_entry)
            elif teaser_entry is not None:
                teaser_hits.append(teaser_entry)

    output.update(
        {
            "rows": rows,
            "teaser_hits": teaser_hits,
            "robots": robots_cache.counters.to_dict(),
            "refused": refused,
            **counters,
        }
    )
    cache.record_success(provider_label)
    logger.info(
        "web_evidence.landed provider=%s query=%r landed=%d archived=%d "
        "teaser=%d refused_license=%d refused_robots=%d refused_egress=%d "
        "skipped_empty_snippet=%d blocked_by_challenge=%d target=%s",
        provider_label, query[:120], counters["landed"], counters["archived"],
        counters["teaser"], counters["refused_license"], counters["refused_robots"],
        counters["refused_egress"], counters["skipped_empty_snippet"],
        counters[BLOCKED_BY_CHALLENGE], dispatch.target_id,
    )
    return ToolResult(status="completed", output=output, units=1)


def register_research_tools(registry: Any) -> None:
    """Register the one research handler (called by ``default_tool_registry``)."""
    registry.register("web_evidence", web_evidence_tool)


__all__ = [
    "RESEARCH_PACK_ID",
    "RESEARCH_TOOLS",
    "register_research_tools",
    "web_evidence_tool",
]
