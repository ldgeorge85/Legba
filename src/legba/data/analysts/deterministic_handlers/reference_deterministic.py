# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""INST-1 — ``reference_deterministic``: a reference that is a LIST, not a read.

The correctness grader needs a reference per subject per window. The web
builder (``reference_builder``) makes one for a subject whose record is prose.
This kind makes one for a subject whose record is a MACHINE-READABLE LIST — the
design names CISA KEV and openFDA (Review B §1.3 F2: "a deterministic reference
per (family, window) … cheaper, higher-coverage and stronger than the country
reference builder") — and the grader then grades against it EXACTLY, present
or absent by key, with no model (:mod:`_correctness_keyed`).

PER TICK: fetch the declared list ONCE (the SSRF-guarded client; a test or an
operator hands a fetcher under ``deps.extras["reference_deterministic_fetch"]``),
then for every BOUND subject (``bindings``: ``target_id=value|value``, matched
against the item's ``subject_field``) cut the items dated inside the window
(:mod:`_reference_keyed`) and write ONE ``unit_references`` row through the
shared writer (``_reference_store`` — the only writer of that table, beside
the web builder and the operator loader), builder
``deterministic:<feed_id>``. The subject's code / name / kind come from the
subject ladder exactly as the web builder's do, and its units from its own
head's tags (``_reference_roster.expected_units``).

WHAT IS NOT WRITTEN. An empty cut of a list that does NOT declare itself
complete says nothing (KEV lists the exploited; it does not certify the rest),
so it is a named ``empty_window`` and no row. An empty cut of a COMPLETE list is
a real reference — "nothing listed in the window" — and is written.

THE EXEMPLAR BINDS NOTHING. No list in the geopolitics catalog is keyed by
anything a lane's or a flow's read names (a disruption read names no CVE and no
designation id), so a binding there would grade every claim ``silent`` and call
it coverage. The descriptor ships ``draft`` with the KEV list declared and NO
binding; its first real binding is the focused instance's (docs/SEAMS.md).

FLAG ``LEGBA_REFERENCE_DETERMINISTIC_ENABLED``, default OFF: off, nothing is
fetched and nothing written. COST: none — no model, no paid route; the
descriptor's ``budget_tokens_per_day`` is 0.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload
from . import _reference_keyed as KEYED
from . import _reference_roster as ROSTER
from . import _reference_store as STORE

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "reference_deterministic"
ENABLED_ENV = "LEGBA_REFERENCE_DETERMINISTIC_ENABLED"
#: deps.extras key for an injected fetcher: ``async (url) -> str | bytes``.
FETCH_DEPS_EXTRA_KEY = "reference_deterministic_fetch"
DEFAULT_WINDOW_DAYS = 14
FETCH_TIMEOUT_SECONDS = 60.0
_RECEIPT_CAP = 24

Fetcher = Callable[[str], Awaitable["str | bytes"]]


def enabled() -> bool:
    """The flag. Off, the handler fetches nothing and writes nothing."""
    return str(os.getenv(ENABLED_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def _strs(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(v).strip() for v in raw if str(v).strip())


def _t0(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            return STORE.iso(raw)
        except ValueError:
            logger.warning("reference_deterministic.as_of_unparseable raw=%r", raw)
    return datetime.now(timezone.utc)


def feed_spec(options: Mapping[str, Any]) -> KEYED.FeedSpec:
    """The list's shape, read from the descriptor's options."""
    try:
        tier = int(options.get("source_tier") or 1)
    except (TypeError, ValueError):
        tier = 1
    return KEYED.FeedSpec(
        feed_id=str(options.get("feed_id") or "").strip(),
        url=str(options.get("feed_url") or "").strip(),
        items_path=str(options.get("items_path") or "").strip(),
        key_field=str(options.get("key_field") or "").strip(),
        key_pattern=str(options.get("key_pattern") or "").strip(),
        date_field=str(options.get("date_field") or "").strip(),
        subject_field=str(options.get("subject_field") or "").strip(),
        summary_fields=_strs(options.get("summary_fields")),
        outlet=str(options.get("outlet") or "").strip(),
        complete=bool(options.get("list_complete") is True),
        source_tier=tier,
    )


async def _guarded_fetch(url: str) -> bytes:
    """The default fetcher: the platform's SSRF-guarded client, one GET."""
    from ...sources._egress import guarded_async_client

    async with guarded_async_client(
        follow_redirects=True, timeout=FETCH_TIMEOUT_SECONDS,
        headers={"Accept": "application/json"},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _receipt(
    *, t0: datetime, on: bool, spec: KEYED.FeedSpec, per_target: Sequence[Mapping[str, Any]],
    warnings: Sequence[str], dry_run: bool, feed: Mapping[str, Any] | None,
) -> FindingPayload:
    written = [t for t in per_target if t.get("status") == "built"]
    if not on:
        headline = f"deterministic reference DISABLED ({ENABLED_ENV} is off)"
    elif not per_target:
        headline = "no bound subject — nothing built"
    else:
        headline = (
            f"built {len(written)} keyed reference(s) from "
            f"{spec.outlet or spec.feed_id or 'no list'}"
        )
    body = [
        f"Deterministic reference — {headline}.",
        f"  t0={t0.isoformat()} feed={spec.feed_id or '—'} complete={spec.complete} "
        f"pipeline={KEYED.PIPELINE_VERSION} model=none paid_api=$0.00"
        + ("  DRY RUN (nothing written)" if dry_run else ""),
    ]
    for t in list(per_target)[:_RECEIPT_CAP]:
        body.append(
            f"  - [{t.get('status')}] {t.get('target_id')} ({t.get('name')}): "
            f"{t.get('n_developments', 0)} keyed item(s) over {t.get('dimensions')}"
        )
    body.extend(f"  WARNING: {w}" for w in list(warnings)[:_RECEIPT_CAP])
    return FindingPayload(
        title=f"Deterministic reference — {headline}"[:2048],
        body="\n".join(body)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "correctness_measurement", "severity:low"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "pipeline_version": KEYED.PIPELINE_VERSION,
            "enabled": bool(on),
            "t0": t0.isoformat(),
            "feed": dict(feed or {}),
            "list_complete": bool(spec.complete),
            "dry_run": bool(dry_run),
            "n_references_written": len(written),
            "per_target": [dict(t) for t in per_target],
            "warnings": list(warnings),
            "caveat": (
                "A keyed reference is the list itself, cut to one subject and "
                "one window. It grades EXACTLY what the list can say — a key on "
                "it, or (for a list declared complete) a key off it — and "
                "nothing else: a claim that names no key reads `silent`."
            ),
        },
    )


async def handle(inputs: Any, options: Mapping[str, Any], deps: Any) -> AnalystMethodResult:
    """One tick: fetch the list once, write each bound subject's reference."""
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "reference_deterministic requires a live deps.pg_pool — refusing to "
            "report a reference run without reading the substrate"
        )
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    t0 = _t0(options.get("as_of"))
    spec = feed_spec(options)
    dry_run = options.get("dry_run") is True
    window_days = int(options.get("window_days") or DEFAULT_WINDOW_DAYS)
    on = enabled()
    warnings: list[str] = []
    if not on:
        return AnalystMethodResult(finding=_receipt(
            t0=t0, on=False, spec=spec, per_target=(), dry_run=dry_run, feed=None,
            warnings=(f"{ENABLED_ENV} is off — nothing fetched, nothing written.",),
        ), usage=usage)

    raw_bindings = _strs(options.get("bindings"))
    bindings = KEYED.parse_bindings(raw_bindings)
    if len(bindings) < len(raw_bindings):
        warnings.append(
            f"{len(raw_bindings) - len(bindings)} binding line(s) are not "
            "`target_id=value|value` and were ignored"
        )
    named = _strs(options.get("reference_targets"))
    if named:
        bindings = {t: v for t, v in bindings.items() if t in named}
    problems = spec.problems()
    if problems or not bindings:
        warnings.append(
            "nothing to build: "
            + (f"the feed spec is incomplete ({', '.join(problems)})" if problems
               else "no subject is bound (`bindings`)")
            + " — no fetch was made."
        )
        return AnalystMethodResult(finding=_receipt(
            t0=t0, on=True, spec=spec, per_target=(), warnings=warnings,
            dry_run=dry_run, feed=None,
        ), usage=usage)

    extras = dict(getattr(deps, "extras", None) or {})
    fetch: Fetcher = extras.get(FETCH_DEPS_EXTRA_KEY) or _guarded_fetch
    try:
        body = await fetch(spec.url)
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        items = KEYED.parse_feed(raw, spec)
    except Exception as exc:  # noqa: BLE001 — a dead list is a named outcome
        warnings.append(f"{spec.feed_id}: the list could not be read ({type(exc).__name__}: {exc})")
        return AnalystMethodResult(finding=_receipt(
            t0=t0, on=True, spec=spec, per_target=(), warnings=warnings,
            dry_run=dry_run, feed={"id": spec.feed_id, "url": spec.url, "error": str(exc)[:300]},
        ), usage=usage)
    feed = {"id": spec.feed_id, "url": spec.url, "n_items": len(items),
            "sha256": hashlib.sha256(raw).hexdigest()}
    window_start = t0 - timedelta(days=window_days)
    units = ROSTER.reference_units()

    per_target: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        identities = await ROSTER.target_identity(conn, list(bindings), detail=True)
        for target_id, values in sorted(bindings.items()):
            identity = identities.get(target_id) or {"code": target_id, "name": target_id}
            dims = list(
                _strs(options.get("dimensions"))
                or await ROSTER.expected_units(conn, target_id, units)
                or units
            )
            cut = KEYED.select_items(
                items, spec, values,
                window_start=window_start.date(), window_end=t0.date(),
            )
            outcome: dict[str, Any] = {
                "target_id": target_id, "name": identity.get("name"),
                "code": identity.get("code"), "kind": identity.get("kind"),
                "bound_to": list(values), "dimensions": dims,
                "n_developments": len(cut), "status": "built",
            }
            if not cut and not spec.complete:
                outcome["status"] = "empty_window"
                warnings.append(
                    f"{target_id}: no item bound to it inside the window, and "
                    "the list does not declare itself complete — nothing written "
                    "(an absence on an incomplete list is not a reference)."
                )
                per_target.append(outcome)
                continue
            reference = KEYED.build_keyed_reference(
                target_id=target_id, identity=identity, items=cut, spec=spec,
                dimensions=dims, t0=t0, window_start=window_start,
                feed_sha256=feed["sha256"],
            )
            outcome["sha256"] = STORE.canonical_sha256(reference)
            if dry_run:
                outcome["status"] = "built_not_written"
                outcome["reference"] = reference
                per_target.append(outcome)
                continue
            ref_id, derived = await STORE.write_reference(
                conn, target_id=target_id, reference=reference,
                window_start=window_start, window_end=t0,
                built_at=datetime.now(timezone.utc),
                builder=f"{KEYED.BUILDER_PREFIX}:{spec.feed_id}",
                sha256=outcome["sha256"],
            )
            outcome["derived"] = derived
            if ref_id is None:
                outcome["status"] = "duplicate"
            else:
                outcome["reference_id"] = str(ref_id)
            per_target.append(outcome)

    return AnalystMethodResult(finding=_receipt(
        t0=t0, on=True, spec=spec, per_target=per_target, warnings=warnings,
        dry_run=dry_run, feed=feed,
    ), usage=usage)


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "ENABLED_ENV",
    "FETCH_DEPS_EXTRA_KEY",
    "SUB_HANDLER_NAME",
    "enabled",
    "feed_spec",
    "handle",
]
