# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ingestion intake's store: submit (with admission control) and the
queue operations the drain uses (claim, complete, fail, reclaim, expire).

ONE function submits: :func:`submit_request`. Every producer — the follow
step, the agent tool, the drift re-check batch, the archiver's route, an
operator script — calls it, so admission is decided in one place, in code,
and every refusal is a ROW (``state = 'refused'``, ``refusal_reason``), which
is how refusals are counted (the read route groups them).

Admission, in order; the first rule that applies decides
(:data:`legba.data.intake_contract.REFUSAL_REASONS`):

1. egress posture — http(s) only, no userinfo, no literal private / loopback /
   link-local / reserved address, no ``localhost`` / ``.local`` /
   ``.internal`` name. This is the literal half of the egress guard; the
   DNS half runs where it always runs, in the transport at fetch time
   (``SsrfGuardedTransport``), so a name that RESOLVES privately is refused
   there and the request fails ``egress_blocked`` without retry.
2. the pipeline — a head source descriptor, ``active`` or ``paused`` (a
   paused descriptor is not polled by the actor plane); an ADDRESS request
   may also run under a ``configured`` one (I-2: a page pipeline has no
   schedule, so it can never be ``active`` and therefore never ``paused``;
   ``configured`` is validated, actor-less and unscheduled — how a page
   pipeline is kept for intake use only); an address needs a kind with
   an address adapter; a re-poll needs a poll source; the sink must be the
   pipeline's own family or ``receipt``.
3. depth — at most ``max_depth`` (and, for a followed reference, the source
   follow policy's own ``max_depth``).
4. follow policy (reason ``followed_reference``) — the parent source's
   ``follow`` block must allow the reference's host and link kind.
   4b. claim origin (C-1) — an address request whose parent signal is a
   CLAIM row, or whose follow source is a claim source, is refused
   (``claim_link_refused``) unless the target host is an ordinary
   (non-claim) registered source's address host.
5. one pending request per target/pipeline/sink (``duplicate_pending``).
6. freshness — an address fetched within ``freshness_hours`` is SATISFIED
   from the stored copy: ``done`` with ``satisfied_from`` set and no fetch
   (a ``receipt`` request: a receipt version of the address; a record-
   writing request: that version's linked signal whose stored canonical-URL
   hash is the address's own, migration 0247).
7. fan-out per parent, per-host daily, per-requester daily and per-class
   daily budgets (UTC day; refused and satisfied rows do not spend).

Concurrency: a submit runs in one transaction under a transaction-scoped
advisory lock, so two submitters never both pass a budget with one slot
left. Claims run under a second advisory lock with ``FOR UPDATE SKIP
LOCKED``; a claim carries a token and a lease, and only the holder of the
token can complete or fail it — a worker whose lease was reclaimed cannot
overwrite the re-run.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from ._url_canon import canonical_url, canonical_url_hash
from .intake_contract import (
    ADDRESS_ADAPTERS,
    ADDRESS_RUNNABLE_STATES,
    WINDOW_RUNNABLE_STATES,
    BUDGET_CLASSES,
    REASON_BUDGET_CLASS,
    REASONS,
    SINKS,
    TARGET_KINDS,
    IntakeLimits,
    dedupe_key,
    host_of,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ADMISSION_LOCK_KEY",
    "CLAIM_LOCK_KEY",
    "ClaimedRequest",
    "IntakeRequestInvalid",
    "SubmitRequest",
    "SubmitResult",
    "claim_requests",
    "complete_request",
    "expire_requests",
    "fail_request",
    "get_request",
    "reclaim_expired_leases",
    "submit_request",
]

#: Transaction-scoped advisory lock keys (two distinct 64-bit constants).
ADMISSION_LOCK_KEY = 0x1A7A_4E00_0249_0001
CLAIM_LOCK_KEY = 0x1A7A_4E00_0249_0002

_FOLLOW_REASON = "followed_reference"
_BLOCKED_NAME_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class IntakeRequestInvalid(ValueError):
    """A malformed request (a caller bug, not a policy refusal): nothing is
    recorded."""


@dataclass
class SubmitRequest:
    target_kind: str
    reason: str
    requested_by: str
    pipeline_source_id: str | None = None
    address: str | None = None
    source_id: str | None = None
    window_start: datetime | None = None
    version_id: int | None = None
    #: ``None`` = the pipeline's own family.
    sink: str | None = None
    on_behalf_of: str | None = None
    note: str | None = None
    link_kind: str | None = None
    parent_request_id: uuid.UUID | None = None
    parent_signal_id: uuid.UUID | None = None
    #: The source whose follow policy a ``followed_reference`` is checked
    #: against (the source of the parent signal).
    follow_source_id: str | None = None
    #: The host of the document the reference was found in (``same_host``).
    follow_document_host: str | None = None
    depth: int = 0
    priority: int = 50
    not_before: datetime | None = None
    expires_at: datetime | None = None
    budget_class: str | None = None
    max_attempts: int | None = None


@dataclass
class SubmitResult:
    id: uuid.UUID
    state: str
    refusal_reason: str | None = None
    satisfied_from: str | None = None
    result_version_id: int | None = None
    duplicate_of: uuid.UUID | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["id"] = str(self.id)
        out["duplicate_of"] = str(self.duplicate_of) if self.duplicate_of else None
        return out


@dataclass
class ClaimedRequest:
    id: uuid.UUID
    claim_token: uuid.UUID
    row: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.row[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _utc_day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def egress_posture(address: str) -> str | None:
    """``None`` when ``address`` passes the literal egress check, else why
    not. Never resolves DNS (that is the transport's half, at fetch)."""
    from .sources._egress import _allowed_internal_hosts, _ip_blocked  # noqa: PLC0415

    try:
        parts = urlsplit(address)
        parts.port  # noqa: B018 — a malformed port raises here
    except ValueError:
        return "unparseable address"
    if parts.scheme not in ("http", "https"):
        return f"scheme {parts.scheme or '(none)'!r} is not http(s)"
    if parts.username or parts.password:
        return "userinfo in the address"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return "no host"
    if host in _allowed_internal_hosts():
        return None
    if host == "localhost" or host.endswith(_BLOCKED_NAME_SUFFIXES):
        return f"{host} is an internal name"
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return None
    if _ip_blocked(literal):
        return f"{host} is a non-public address"
    return None


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


def _validate(req: SubmitRequest) -> None:
    if req.target_kind not in TARGET_KINDS:
        raise IntakeRequestInvalid(f"target_kind must be one of {TARGET_KINDS}, got {req.target_kind!r}")
    if req.reason not in REASONS:
        raise IntakeRequestInvalid(f"reason must be one of {REASONS}, got {req.reason!r}")
    if req.sink is not None and req.sink not in SINKS:
        raise IntakeRequestInvalid(f"sink must be one of {SINKS}, got {req.sink!r}")
    if req.budget_class is not None and req.budget_class not in BUDGET_CLASSES:
        raise IntakeRequestInvalid(f"budget_class must be one of {BUDGET_CLASSES}")
    if not (req.requested_by or "").strip():
        raise IntakeRequestInvalid("requested_by is required")
    if not 0 <= int(req.priority) <= 100:
        raise IntakeRequestInvalid("priority must be within 0..100")
    if int(req.depth) < 0:
        raise IntakeRequestInvalid("depth must be >= 0")
    if req.target_kind == "address":
        if not (req.address or "").strip():
            raise IntakeRequestInvalid("an address request needs an address")
        if not req.pipeline_source_id:
            raise IntakeRequestInvalid("an address request needs a pipeline_source_id")
    elif req.target_kind == "source_window":
        if not req.source_id:
            raise IntakeRequestInvalid("a source_window request needs a source_id")
        if req.window_start is None:
            raise IntakeRequestInvalid("a source_window request needs a window_start")
    elif req.version_id is None:
        raise IntakeRequestInvalid("a stored_version request needs a version_id")
    if req.reason == _FOLLOW_REASON and not req.follow_source_id:
        raise IntakeRequestInvalid("a followed_reference needs the follow_source_id whose policy admits it")


async def _head(conn: Any, descriptor_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT kind, state, body FROM source_descriptors WHERE descriptor_id = $1 AND is_head",
        descriptor_id,
    )
    if row is None:
        return None
    body = row["body"]
    if isinstance(body, str):
        body = json.loads(body)
    return {"kind": row["kind"], "state": row["state"], "body": body or {}}


def _family(body: dict[str, Any]) -> str:
    return str(((body.get("sink") or {}).get("family")) or "document")


async def _count(conn: Any, sql: str, *args: Any) -> int:
    return int(await conn.fetchval(sql, *args) or 0)


_INSERT_SQL = """
INSERT INTO ingestion_requests (
    id, target_kind, address, address_canonical, host, source_id, window_start,
    version_id, pipeline_source_id, sink, reason, budget_class, requested_by,
    on_behalf_of, note, link_kind, parent_request_id, parent_signal_id, depth,
    priority, not_before, expires_at, dedupe_key, state, refusal_reason,
    satisfied_from, max_attempts, result, result_version_id, created_at,
    updated_at, finished_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
          $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28::jsonb, $29,
          $30, $30, $31)
"""

_SPEND = "state <> 'refused' AND satisfied_from IS NULL"


async def submit_request(
    db: Any, req: SubmitRequest, *, limits: IntakeLimits | None = None,
    now: datetime | None = None,
) -> SubmitResult:
    """Admit (or refuse, or satisfy) ONE request and record it. ``db`` is a
    pool (a connection is acquired) or a connection (used as given).

    Raises :class:`IntakeRequestInvalid` for a malformed request; every
    policy outcome is returned AND recorded."""
    _validate(req)
    if not hasattr(db, "transaction"):   # a pool (asyncpg's has fetchrow too)
        async with db.acquire() as conn:
            return await _submit(conn, req, limits or IntakeLimits.from_env(), now)
    return await _submit(db, req, limits or IntakeLimits.from_env(), now)


async def _submit(conn: Any, req: SubmitRequest, limits: IntakeLimits, now: datetime | None) -> SubmitResult:
    now = _utc(now) or datetime.now(tz=timezone.utc)
    request_id = uuid.uuid4()
    address = (req.address or "").strip() or None
    version_row = None
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", ADMISSION_LOCK_KEY)
        if req.target_kind == "stored_version":
            version_row = await conn.fetchrow(
                "SELECT id, url, address FROM fetch_versions WHERE id = $1 AND receipt_kind = 'fetch'",
                int(req.version_id),
            )
            if version_row is not None:
                address = version_row["url"]
        pipeline_id = req.pipeline_source_id or req.source_id or ""
        canonical = (canonical_url(address) or address) if address else None
        host = host_of(address)
        refusal: str | None = None
        detail: dict[str, Any] = {}
        head: dict[str, Any] | None = None
        sink = req.sink

        # 1. egress posture (literal)
        if address is not None:
            why = egress_posture(address)
            if why is not None:
                refusal, detail = "egress_refused", {"why": why}
        # 2. pipeline
        if refusal is None:
            head = await _head(conn, pipeline_id)
            if head is None:
                refusal = "unknown_pipeline"
            elif head["state"] not in (
                WINDOW_RUNNABLE_STATES if req.target_kind == "source_window" else ADDRESS_RUNNABLE_STATES
            ):
                refusal, detail = "pipeline_not_runnable", {"state": head["state"]}
            elif req.target_kind == "source_window":
                acq = str(head["body"].get("acquisition") or "poll")
                if acq != "poll":
                    refusal, detail = "pipeline_not_runnable", {"acquisition": acq}
                else:
                    host = host_of(_config_url(head["body"]))
            elif head["kind"] not in ADDRESS_ADAPTERS:
                refusal, detail = "pipeline_not_address_capable", {"kind": head["kind"]}
            if refusal is None and head is not None:
                family = _family(head["body"])
                sink = sink or family
                if sink not in (family, "receipt"):
                    refusal, detail = "sink_mismatch", {"pipeline_family": family}
        if req.target_kind == "stored_version" and refusal is None and version_row is None:
            refusal = "unknown_version"
        sink = sink or "document"
        # 3. depth (+ 4. follow policy)
        follow = None
        if refusal is None and req.reason == _FOLLOW_REASON:
            parent_head = await _head(conn, str(req.follow_source_id))
            follow = (parent_head or {}).get("body", {}).get("follow") if parent_head else None
        max_depth = limits.max_depth
        if follow:
            max_depth = min(max_depth, int(follow.get("max_depth") or 1))
        if refusal is None and int(req.depth) > max_depth:
            refusal, detail = "depth_exceeded", {"depth": int(req.depth), "max_depth": max_depth}
        if refusal is None and req.reason == _FOLLOW_REASON:
            refusal, detail = _follow_check(follow, req, host)
        # 4b. C-1 claim origin — a request born of a CLAIM row's links (its
        # parent signal is a claim, or the follow source is a claim source)
        # is refused unless the target host is an ordinary registered source.
        if refusal is None and address is not None:
            refusal, detail = await _claim_origin_check(conn, req, host)
        # 5. one pending per target/pipeline/sink
        key = dedupe_key(
            target_kind=req.target_kind, pipeline_source_id=pipeline_id, sink=sink,
            address_canonical=canonical, source_id=req.source_id,
            window_start=_utc(req.window_start), version_id=req.version_id,
        )
        duplicate_of = None
        if refusal is None:
            duplicate_of = await conn.fetchval(
                "SELECT id FROM ingestion_requests WHERE dedupe_key = $1 "
                "AND state IN ('queued', 'claimed') LIMIT 1", key,
            )
            if duplicate_of is not None:
                refusal, detail = "duplicate_pending", {"duplicate_of": str(duplicate_of)}
        # 6. freshness: satisfied from the stored copy
        satisfied_from = None
        result_version_id = None
        result: dict[str, Any] | None = None
        if refusal is None and canonical is not None and limits.freshness_hours > 0:
            satisfied_from, result_version_id, result = await _fresh_copy(
                conn, canonical, sink=sink, since=now - timedelta(hours=limits.freshness_hours),
            )
        # 7. fan-out and budgets
        day = _utc_day_start(now)
        budget_class = req.budget_class or REASON_BUDGET_CLASS[req.reason]
        if refusal is None and satisfied_from is None:
            refusal, detail = await _budget_check(
                conn, req, limits, host=host, day=day, budget_class=budget_class,
            )

        state = "refused" if refusal is not None else ("done" if satisfied_from else "queued")
        if refusal is not None:
            result = {"refusal": refusal, **detail}
            if req.version_id is not None:
                result["asked_version_id"] = int(req.version_id)
        expires_at = _utc(req.expires_at)
        if expires_at is None and state == "queued" and limits.expire_hours > 0:
            expires_at = now + timedelta(hours=limits.expire_hours)
        await conn.execute(
            _INSERT_SQL, request_id, req.target_kind, address, canonical, host,
            req.source_id, _utc(req.window_start),
            int(version_row["id"]) if version_row is not None else None,
            pipeline_id, sink, req.reason, budget_class, req.requested_by.strip()[:256],
            (req.on_behalf_of or None), (req.note or None) and req.note[:2000], req.link_kind,
            req.parent_request_id, req.parent_signal_id, int(req.depth), int(req.priority),
            _utc(req.not_before) or now, expires_at, key, state, refusal, satisfied_from,
            int(req.max_attempts or limits.max_attempts),
            json.dumps(result) if result is not None else None, result_version_id, now,
            now if state != "queued" else None,
        )
    if refusal is not None:
        logger.info(
            "intake.refused id=%s reason=%s refusal=%s requested_by=%s target=%s",
            request_id, req.reason, refusal, req.requested_by, address or req.source_id,
        )
    elif satisfied_from is not None:
        logger.info("intake.satisfied id=%s from=%s address=%s", request_id, satisfied_from, address)
    return SubmitResult(
        id=request_id, state=state, refusal_reason=refusal, satisfied_from=satisfied_from,
        result_version_id=result_version_id,
        duplicate_of=duplicate_of if refusal == "duplicate_pending" else None, detail=detail,
    )


def _config_url(body: dict[str, Any]) -> str | None:
    """The address a poll source fetches (``url`` / ``url_template``), for
    host politeness on a re-poll. Property-factory wrappers unwrapped."""
    cfg = body.get("config") or {}
    for key in ("url", "url_template", "feed_url"):
        val = cfg.get(key)
        if isinstance(val, dict):
            val = val.get("raw")
        if isinstance(val, str) and val.startswith(("http://", "https://")):
            return val
    return None


_CLAIM_PARENT_SQL = (
    "SELECT source_id FROM signals WHERE id = $1 "
    "AND payload->'claim'->>'source_class' = 'claim'"
)
#: Head descriptors' config addresses + class, for the ordinary-host test.
_SOURCE_HOSTS_SQL = (
    "SELECT jsonb_build_object('config', body->'config') AS body, "
    "COALESCE(body->'scope'->>'source_class', 'reporting') AS source_class "
    "FROM source_descriptors WHERE is_head AND state <> 'retired'"
)


def _bare_host(host: str | None) -> str:
    host = (host or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


async def _claim_origin_check(conn: Any, req: SubmitRequest, host: str | None) -> tuple[str | None, dict]:
    """C-1 — refuse a request that originates from a CLAIM row's links.

    Origin = the parent signal carries the claim stamp, or the follow source
    is a ``source_class: claim`` descriptor. Such a request is admitted ONLY
    when the target host is the address host of an ordinary (non-claim)
    registered source head — a claim may point at a page the platform
    already reads on its own terms, never anywhere else."""
    claim_source: str | None = None
    if req.parent_signal_id is not None:
        claim_source = await conn.fetchval(_CLAIM_PARENT_SQL, req.parent_signal_id)
    if claim_source is None and req.follow_source_id:
        parent = await _head(conn, str(req.follow_source_id))
        if parent and ((parent["body"].get("scope") or {}).get("source_class") == "claim"):
            claim_source = str(req.follow_source_id)
    if claim_source is None:
        return None, {}
    target = _bare_host(host)
    if target:
        for row in await conn.fetch(_SOURCE_HOSTS_SQL):
            if row["source_class"] == "claim":
                continue
            body = row["body"]
            if isinstance(body, str):
                body = json.loads(body)
            if _bare_host(host_of(_config_url(body or {}))) == target:
                return None, {}
    return "claim_link_refused", {
        "why": (
            f"the request originates from a claim row of {claim_source!r}; links in a "
            "claim are not followed unless the target host is an ordinary registered "
            f"source (host {host!r} is not)"
        ),
        "claim_source": claim_source,
    }


def _follow_check(follow: dict[str, Any] | None, req: SubmitRequest, host: str | None) -> tuple[str | None, dict]:
    from .schemas.source_follow import SourceFollow  # noqa: PLC0415

    if not follow:
        return "follow_not_allowed", {"why": "the source declares no follow policy"}
    try:
        policy = SourceFollow.model_validate(follow, strict=False)
    except Exception as exc:  # a stored body that no longer validates follows nothing
        return "follow_not_allowed", {"why": f"follow policy invalid: {exc}"[:300]}
    if req.pipeline_source_id and req.pipeline_source_id != policy.pipeline:
        return "follow_not_allowed", {"why": "pipeline is not the policy's pipeline"}
    if req.link_kind not in policy.kinds:
        return "follow_not_allowed", {"why": f"link kind {req.link_kind!r} not in {policy.kinds}"}
    if not policy.allows_host(req.follow_document_host, host):
        return "follow_not_allowed", {"why": f"host {host!r} not allowed"}
    return None, {}


async def _fresh_copy(
    conn: Any, canonical: str, *, sink: str, since: datetime,
) -> tuple[str | None, int | None, dict[str, Any] | None]:
    """A stored copy fresh enough to satisfy the request without a fetch.

    "Fetched" means the ADDRESS itself was fetched (a receipt version of it
    inside the window) — a feed entry that merely names the address does not
    count, so following a summary-only entry to its page still fetches it. A
    record-writing request also needs the record that fetch produced: a
    signal linked to that version whose stored canonical-URL hash is the
    address's own (0247) — the document AT the address, not a feed entry
    parsed from a response there."""
    version = await conn.fetchrow(
        "SELECT id, last_seen_at FROM fetch_versions WHERE address = $1 "
        "AND receipt_kind = 'fetch' AND last_seen_at >= $2 "
        "ORDER BY last_seen_at DESC, id DESC LIMIT 1",
        canonical, since,
    )
    if version is None:
        return None, None, None
    if sink == "receipt":
        return "stored_version", int(version["id"]), {
            "stored_version_id": int(version["id"]),
            "stored_at": version["last_seen_at"].isoformat(),
        }
    signal = await conn.fetchrow(
        "SELECT s.id, s.fetched_at FROM fetch_versions v "
        "JOIN fetch_version_records r ON r.version_id = v.id AND r.record_kind = 'signal' "
        "JOIN signals s ON s.id = r.record_id AND s.canonical_url_hash = $3 "
        "WHERE v.address = $1 AND v.last_seen_at >= $2 "
        "ORDER BY s.fetched_at DESC LIMIT 1",
        canonical, since, canonical_url_hash(canonical),
    )
    if signal is None:
        return None, None, None
    return "stored_signal", int(version["id"]), {
        "stored_signal_id": str(signal["id"]),
        "stored_version_id": int(version["id"]),
        "stored_at": signal["fetched_at"].isoformat(),
    }


async def _budget_check(
    conn: Any, req: SubmitRequest, limits: IntakeLimits, *, host: str | None,
    day: datetime, budget_class: str,
) -> tuple[str | None, dict]:
    if req.parent_request_id is not None or req.parent_signal_id is not None:
        n = await _count(
            conn,
            "SELECT count(*) FROM ingestion_requests WHERE state <> 'refused' AND ("
            "($1::uuid IS NOT NULL AND parent_request_id = $1) OR "
            "($2::uuid IS NOT NULL AND parent_signal_id = $2))",
            req.parent_request_id, req.parent_signal_id,
        )
        if n >= limits.fanout_per_parent:
            return "fanout_exceeded", {"children": n, "cap": limits.fanout_per_parent}
    if host is not None:
        n = await _count(
            conn, f"SELECT count(*) FROM ingestion_requests WHERE host = $1 AND created_at >= $2 AND {_SPEND}",
            host, day,
        )
        if n >= limits.host_daily:
            return "host_budget", {"host": host, "used": n, "cap": limits.host_daily}
    n = await _count(
        conn, f"SELECT count(*) FROM ingestion_requests WHERE requested_by = $1 AND created_at >= $2 AND {_SPEND}",
        req.requested_by.strip()[:256], day,
    )
    if n >= limits.requester_daily:
        return "requester_budget", {"used": n, "cap": limits.requester_daily}
    cap = int(limits.class_daily.get(budget_class, 0))
    n = await _count(
        conn, f"SELECT count(*) FROM ingestion_requests WHERE budget_class = $1 AND created_at >= $2 AND {_SPEND}",
        budget_class, day,
    )
    if n >= cap:
        return "class_budget", {"budget_class": budget_class, "used": n, "cap": cap}
    return None, {}


# ---------------------------------------------------------------------------
# queue operations (the drain)
# ---------------------------------------------------------------------------

_CLAIM_ONE_SQL = """
SELECT r.* FROM ingestion_requests r
 WHERE r.state = 'queued'
   AND r.not_before <= now()
   AND (r.expires_at IS NULL OR r.expires_at > now())
   AND (r.host IS NULL OR (
        (SELECT count(*) FROM ingestion_requests c
          WHERE c.state = 'claimed' AND c.host = r.host) < $1
        -- a scalar subquery (never pulled up into an anti-join over the
        -- table): the host's newest claim, one backward probe of
        -- ingestion_requests_host_claimed_idx per candidate
        AND COALESCE((SELECT max(d.claimed_at) FROM ingestion_requests d
                       WHERE d.host = r.host AND d.claimed_at IS NOT NULL), '-infinity')
            <= now() - make_interval(secs => $2)))
 ORDER BY r.priority DESC, r.not_before, r.created_at
 LIMIT 1
 FOR UPDATE OF r SKIP LOCKED
"""

_MARK_CLAIMED_SQL = """
UPDATE ingestion_requests
   SET state = 'claimed', claim_token = $2, claimed_by = $3, claimed_at = now(),
       lease_expires_at = now() + make_interval(secs => $4),
       attempts = attempts + 1, updated_at = now()
 WHERE id = $1
RETURNING *
"""


def _row(rec: Any) -> dict[str, Any]:
    out = dict(rec)
    if isinstance(out.get("result"), str):
        try:
            out["result"] = json.loads(out["result"])
        except ValueError:
            pass
    return out


async def claim_requests(
    pool: Any, *, worker: str, limit: int, limits: IntakeLimits | None = None,
) -> list[ClaimedRequest]:
    """Claim up to ``limit`` runnable requests (priority, not-before, age),
    honouring per-host concurrency and minimum interval. One claimer at a time
    (a second concurrent claimer gets nothing this tick)."""
    limits = limits or IntakeLimits.from_env()
    out: list[ClaimedRequest] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", CLAIM_LOCK_KEY):
                return []
            for _ in range(max(0, int(limit))):
                rec = await conn.fetchrow(
                    _CLAIM_ONE_SQL, int(limits.host_concurrency), float(limits.host_min_interval_s),
                )
                if rec is None:
                    break
                token = uuid.uuid4()
                claimed = await conn.fetchrow(
                    _MARK_CLAIMED_SQL, rec["id"], token, worker[:256], float(limits.lease_s),
                )
                out.append(ClaimedRequest(id=claimed["id"], claim_token=token, row=_row(claimed)))
    return out


async def complete_request(
    pool: Any, request_id: uuid.UUID, claim_token: uuid.UUID, *, result: dict[str, Any],
    result_version_id: int | None = None, records: Iterable[tuple[str, Any]] = (),
) -> bool:
    """``done`` — only by the token holder. Links the written records. False
    when the claim was lost (reclaimed after the lease expired)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE ingestion_requests SET state = 'done', result = $3::jsonb, "
                "result_version_id = $4, finished_at = now(), updated_at = now(), "
                "lease_expires_at = NULL "
                "WHERE id = $1 AND claim_token = $2 AND state = 'claimed' RETURNING id",
                request_id, claim_token, json.dumps(result, default=str), result_version_id,
            )
            if row is None:
                return False
            recs = [(k, r) for k, r in records]
            if recs:
                await conn.execute(
                    "INSERT INTO ingestion_request_records (request_id, record_kind, record_id) "
                    "SELECT $1, k, r FROM unnest($2::text[], $3::uuid[]) AS t(k, r) "
                    "ON CONFLICT DO NOTHING",
                    request_id, [k for k, _ in recs], [r for _, r in recs],
                )
    return True


async def fail_request(
    pool: Any, request_id: uuid.UUID, claim_token: uuid.UUID, *, error: str,
    permanent: bool = False, limits: IntakeLimits | None = None,
) -> str | None:
    """A failed run: back to ``queued`` after the back-off while attempts
    remain (and the failure is not permanent), else ``failed``. Returns the
    new state, or ``None`` when the claim was lost."""
    limits = limits or IntakeLimits.from_env()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT attempts, max_attempts FROM ingestion_requests "
            "WHERE id = $1 AND claim_token = $2 AND state = 'claimed' FOR UPDATE",
            request_id, claim_token,
        )
        if row is None:
            return None
        final = permanent or int(row["attempts"]) >= int(row["max_attempts"])
        state = "failed" if final else "queued"
        delay = limits.retry_delay_s(int(row["attempts"]))
        await conn.execute(
            "UPDATE ingestion_requests SET state = $3, last_error = $4, claim_token = NULL, "
            "lease_expires_at = NULL, updated_at = now(), "
            "not_before = CASE WHEN $3 = 'queued' THEN now() + make_interval(secs => $5) ELSE not_before END, "
            "finished_at = CASE WHEN $3 = 'failed' THEN now() ELSE NULL END "
            "WHERE id = $1 AND claim_token = $2 AND state = 'claimed'",
            request_id, claim_token, state, error[:2000], float(delay),
        )
    return state


async def reclaim_expired_leases(pool: Any) -> list[uuid.UUID]:
    """Claimed requests whose lease expired (their worker died): back to
    ``queued`` (or ``failed`` once attempts are spent). One UPDATE, so two
    concurrent reclaimers never both reclaim a row."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE ingestion_requests SET "
            "state = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END, "
            "claim_token = NULL, lease_expires_at = NULL, last_error = 'lease_expired', "
            "updated_at = now(), "
            "finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END "
            "WHERE state = 'claimed' AND lease_expires_at < now() RETURNING id",
        )
    ids = [r["id"] for r in rows]
    if ids:
        logger.warning("intake.reclaimed count=%d ids=%s", len(ids), [str(i) for i in ids[:10]])
    return ids


async def expire_requests(pool: Any) -> int:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE ingestion_requests SET state = 'expired', finished_at = now(), updated_at = now() "
            "WHERE state = 'queued' AND expires_at IS NOT NULL AND expires_at <= now() RETURNING id",
        )
    return len(rows)


async def get_request(
    pool: Any, request_id: uuid.UUID, *, requested_by: str | None = None,
) -> dict[str, Any] | None:
    """One request (scoped to ``requested_by`` when given), with its linked
    record ids."""
    async with pool.acquire() as conn:
        rec = await conn.fetchrow(
            "SELECT * FROM ingestion_requests WHERE id = $1 "
            "AND ($2::text IS NULL OR requested_by = $2)",
            request_id, requested_by,
        )
        if rec is None:
            return None
        links = await conn.fetch(
            "SELECT record_kind, record_id FROM ingestion_request_records "
            "WHERE request_id = $1 ORDER BY record_kind, record_id LIMIT 200",
            request_id,
        )
    out = _row(rec)
    out["records"] = [{"kind": r["record_kind"], "id": str(r["record_id"])} for r in links]
    return out
