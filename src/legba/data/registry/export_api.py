# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection export surface (A10) — basket of findings + journal entries →
one markdown or JSON document, composed server-side at FULL fidelity.

``POST /api/v1/v3/export`` takes the UI collection basket
(``{items: [{kind: finding|journal_entry, id}], format: markdown|json,
title?}``) and returns ONE document:

  * **finding** items (any ``analyst_outputs`` row — unit findings, meta/
    composition reports) carry: title, analyst, target, produced_at, the cited
    body with its ``[N]`` markers left intact, the citation list resolved LIVE
    to signal titles + canonical_urls (falling back to the stored citation
    title/source when the signal row is gone — resolution state is stated,
    never faked), the verify state (``faithfulness=<score>`` with hard/soft
    fail-flag counts when spans exist, or an explicit ``unverified — <reason>``
    — the structural verify-exemption included), confidence + the
    verify-folded ``effective_confidence`` (min(confidence, critic score) —
    the same fold ``substrate_reads_api`` surfaces), and the lineage receipt
    link (relative API path always; absolute when ``LEGBA_PUBLIC_BASE_URL``
    is set — via the ONE ``receipt_link`` helper the alert sinks use).

  * **journal_entry** items carry: the entry body, its tier label
    (``entry|consolidation|chronicle|lens|lens_diff``) with the VOICE framing
    stated explicitly (GLOSSARY's journal-entry language: off the
    fact/finding/nexus chain, an up-only reference, never a lineage edge —
    reflective voice, NOT the product chain), the per-claim cited spans with
    every ref resolved to ``(kind, title)`` (reusing ``journal_api``'s
    resolver so there is ONE definition), honesty flags, and the journal
    verify score when its faithfulness critique exists. NO receipt link — a
    lineage walk can never surface a journal node, so fabricating one here
    would lie about the provenance model.

Missing ids are NEVER silently dropped: a basket item that resolves to no
substrate row exports as an explicit ``not found in substrate`` section and is
counted in the header (``items: N (M not found)``).

Size-capped at ``EXPORT_MAX_ITEMS`` (50) with an honest 413 beyond — the cap
is stated in the error, not silently truncated.

Wiring convention mirrors ``goldset_api.py``: ``build_export_router(deps)``
mounted from ``server.py`` under ``/api/v1/v3``, the shared
``RegistryAPIDeps`` bundle, the same ``require_bearer`` gate, reads via
``deps.descriptor_registry.pg.acquire()``. Composition (``build_document`` /
``render_markdown``) is PURE — DB-free — so the markdown shape is
golden-testable without a substrate.

STIX is deliberately NOT built here (operator decision, program doc §A10 —
demoted to optional-later); print-PDF stays a client-side browser print of
the markdown view.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..alerts.sinks import receipt_link, unverified_state, verify_state_from_score
from ..archive import sha256_from_object_ref
from ..provenance.kinds import verify_exempt_reason
from ..provenance.verify import fail_class_for_reason
from .api import RegistryAPIDeps, require_bearer
from .journal_api import (
    _ENTRY_COLS as _JOURNAL_ENTRY_COLS,
    _load_jsonb,
    _read_verify_results,
    _resolve_refs,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Hard cap on basket size — beyond this the route answers an honest 413
#: (the cap + the actual count in the detail), never a silent truncation.
EXPORT_MAX_ITEMS = 50

#: The header's honest provenance note — stamped on every export, both formats.
PROVENANCE_NOTE = (
    "machine-generated export; every claim carries its citations; "
    "verify states as recorded"
)

#: The VOICE framing for journal items — the exact GLOSSARY journal-entry
#: language (docs/GLOSSARY.md, "Journal (OutputKind)"): the journal is
#: reflective voice, explicitly OFF the product chain.
JOURNAL_VOICE_NOTE = (
    "reflective journal voice — off the fact/finding/nexus chain: an "
    "always-empty derived_from, excluded from the lineage catalog; citations "
    "live only in the row's claims / cited_substrate_refs, an up-only "
    "reference, not a lineage edge"
)

#: Human tier labels for the journal ``entry_kind`` vocabulary (the same set
#: ``journal_api._VALID_KINDS`` validates).
JOURNAL_TIER_LABELS: dict[str, str] = {
    "entry": "entry (12h diary tier)",
    "consolidation": "consolidation (daily forward-carried narrative)",
    "chronicle": "chronicle (weekly third-person tier)",
    "lens": "lens (weekly faculty lens)",
    "lens_diff": "lens_diff (weekly chorus diff)",
}

# Non-ASCII citation brackets wrapping a bare integer (``【3】``/``［3］``/…)
# that some core-plane models emit instead of ASCII ``[3]``. Local mirror of
# ``inline_target._VARIANT_CITATION_RE`` (that module drags the whole analyst
# runtime in; the 2-line regex is the stable part) — normalize BEFORE the body
# is exported so the prose markers and the citation list key on the SAME
# ``[N]`` (the full-width-bracket trap, 2026-06-30).
_VARIANT_CITATION_RE = re.compile(r"[【［〔〖](\s*\d+\s*)[】］〕〗]")


def _normalize_citation_markers(text: str) -> str:
    if not text:
        return text
    return _VARIANT_CITATION_RE.sub(lambda m: f"[{m.group(1).strip()}]", text)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class ExportItemIn(BaseModel):
    """One basket item — a substrate finding/report row or a journal entry."""

    kind: Literal["finding", "journal_entry"]
    id: UUID


class ExportRequest(BaseModel):
    """``POST /export`` body — the basket + format + optional document title."""

    items: list[ExportItemIn]
    format: Literal["markdown", "json"]
    title: str | None = None


# ---------------------------------------------------------------------------
# Fetch — findings (any analyst_outputs row) with verify + citations.
# ---------------------------------------------------------------------------


# The SAME faithfulness-critique lateral `/findings` uses (substrate_reads_api,
# S8-T2): pinned to `title LIKE 'Faithfulness verify%'` so a later generic
# critique can never win the produced_at race and mask the verify verdict.
_FINDING_SQL = """
    SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity,
           f.data, f.target_id, f.analyst_id, f.analyst_version,
           f.produced_at, f.derived_from, f.superseded_by,
           c.critic_score AS critic_score,
           c.verification AS verification
      FROM analyst_outputs f
      LEFT JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS critic_score,
                 (cr.data->'data'->'verification') AS verification
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) c ON TRUE
     WHERE f.id = ANY($1::uuid[])
"""


def _stored_citations(data: Any) -> list[dict[str, Any]]:
    """The finding's persisted ``data['citations']`` entries (P0-T1 shape:
    ``{"marker": "[N]", "signal_id": ..., "title"?, "source"?}``), defensively
    filtered to dict entries that carry a signal_id."""
    payload = _load_jsonb(data) or {}
    raw = payload.get("citations") if isinstance(payload, dict) else None
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("signal_id"):
                out.append(entry)
    return out


async def _resolve_citation_signals(
    conn: Any, signal_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Batch-resolve cited signal ids → ``{id: {title, canonical_url}}`` from
    the LIVE signals table (one query over the whole basket's citation set).
    Ids that no longer resolve are simply absent — the caller states that
    honestly instead of fabricating a link."""
    out: dict[str, dict[str, Any]] = {}
    uniq: list[str] = []
    seen: set[str] = set()
    for raw in signal_ids:
        s = str(raw)
        if s in seen:
            continue
        seen.add(s)
        try:
            UUID(s)
        except (ValueError, TypeError, AttributeError):
            continue
        uniq.append(s)
    if not uniq:
        return out
    rows = await conn.fetch(
        "SELECT id, payload->>'title' AS title, canonical_url, object_ref "
        "FROM signals WHERE id = ANY($1::uuid[])",
        uniq,
    )
    for row in rows:
        out[str(row["id"])] = {
            "title": row["title"],
            "canonical_url": row["canonical_url"],
            # P2-1 evidence archival (additive): derived from the existing
            # signals.object_ref column (cas:sha256/<hex>) — the export can
            # state "evidence preserved" + the verifiable hash, never
            # fabricated for un-archived rows.
            "archived": row["object_ref"] is not None,
            "archive_sha256": sha256_from_object_ref(row["object_ref"]),
        }
    return out


def _fail_flag_counts(verification: dict[str, Any]) -> dict[str, int]:
    """Count hard/soft fail flags across the verify block's unsupported spans.

    Uses the span's P2-4 ``fail_class`` when persisted; derives it from the
    span ``reason`` via the ONE mapping table for legacy blocks written before
    the label existed. Empty dict when no spans — no fabricated zero-flags."""
    counts: dict[str, int] = {}
    spans = verification.get("unsupported_spans")
    if not isinstance(spans, list):
        return counts
    for span in spans:
        if not isinstance(span, dict):
            continue
        fail_class = span.get("fail_class") or fail_class_for_reason(
            str(span.get("reason") or "")
        )
        counts[fail_class] = counts.get(fail_class, 0) + 1
    return counts


def _finding_verify(
    verification: dict[str, Any] | None, analyst_id: str | None,
) -> tuple[str, dict[str, int]]:
    """``(verify_state, fail_flags)`` for one finding — the alert-edge verify
    grammar (``faithfulness=<score>`` / ``unverified — <reason>``), with the
    structural verify-exemption stated as its own honest reason."""
    if verification is not None:
        score = verification.get("faithfulness_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            return verify_state_from_score(score), _fail_flag_counts(verification)
    exempt = verify_exempt_reason(analyst_id)
    if exempt is not None:
        return (
            unverified_state(f"{exempt} (verify-exempt deterministic analyst)"),
            {},
        )
    return (
        unverified_state("no faithfulness verdict recorded for this finding"),
        {},
    )


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _finding_export_item(
    row: Any, signal_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """One finding's full-fidelity export dict (pure given the fetched row +
    the resolved signal index)."""
    verification = row["verification"]
    if isinstance(verification, str):
        try:
            verification = json.loads(verification)
        except json.JSONDecodeError:
            verification = None
    if not isinstance(verification, dict):
        verification = None

    confidence = float(row["confidence"]) if row["confidence"] is not None else None
    critic_score = (
        float(row["critic_score"]) if row["critic_score"] is not None else None
    )
    effective = (
        min(confidence, critic_score)
        if confidence is not None and critic_score is not None
        else confidence
    )

    citations: list[dict[str, Any]] = []
    for entry in _stored_citations(row["data"]):
        sid = str(entry["signal_id"])
        live = signal_index.get(sid)
        citations.append(
            {
                "marker": str(entry.get("marker") or ""),
                "signal_id": sid,
                # Live signal title/url first (full fidelity); stored citation
                # fields as the fallback for a pruned signal — and `resolved`
                # states which one the reader is looking at.
                "title": (live or {}).get("title") or entry.get("title"),
                "canonical_url": (live or {}).get("canonical_url")
                or entry.get("source"),
                "resolved": live is not None,
                # P2-1: evidence-archive surface (False/None when un-archived
                # or the signal no longer resolves — never fabricated).
                "archived": bool((live or {}).get("archived")),
                "archive_sha256": (live or {}).get("archive_sha256"),
            }
        )

    verify_state, fail_flags = _finding_verify(verification, row["analyst_id"])
    path, url = receipt_link(str(row["id"]), row_kind="finding")

    return {
        "kind": "finding",
        "id": str(row["id"]),
        "row_kind": row["kind"],
        "title": row["title"],
        "analyst_id": row["analyst_id"],
        "analyst_version": row["analyst_version"],
        "target_id": row["target_id"],
        "severity": row["severity"],
        "produced_at": _iso(row["produced_at"]),
        "superseded": row["superseded_by"] is not None,
        "body": _normalize_citation_markers(row["body"] or ""),
        "citations": citations,
        "verify_state": verify_state,
        "verify_flags": fail_flags,
        "confidence": confidence,
        "effective_confidence": effective,
        "receipt_path": path,
        "receipt_url": url,
    }


# ---------------------------------------------------------------------------
# Fetch — journal entries (reusing journal_api's resolvers).
# ---------------------------------------------------------------------------


def _journal_claims(row: Any, resolved: dict[str, Any]) -> list[dict[str, Any]]:
    """The entry's ``claims`` sidecar with every ref resolved to (kind, title)
    — the same hydration shape ``journal_api._hydrate_entry`` builds, reduced
    to plain dicts for the document."""
    out: list[dict[str, Any]] = []
    raw_claims = _load_jsonb(row["claims"]) or []
    if not isinstance(raw_claims, list):
        return out
    for c in raw_claims:
        if not isinstance(c, dict):
            continue
        span = c.get("text_span")
        if not isinstance(span, str) or not span:
            continue
        refs = []
        for rid in (c.get("refs") or []):
            r = resolved.get(str(rid))
            if r is not None:
                refs.append({"id": r.id, "kind": r.kind, "title": r.title})
        out.append(
            {
                "text_span": span,
                "kind": str(c.get("kind") or "fact"),
                "refs": refs,
            }
        )
    return out


def _journal_export_item(
    row: Any,
    resolved: dict[str, Any],
    verify: dict[str, Any],
) -> dict[str, Any]:
    """One journal entry's export dict — tier-labeled, VOICE-framed, claims +
    resolved refs, verify score when its faithfulness critique exists."""
    entry_kind = str(row["entry_kind"])
    vr = verify.get(str(row["id"]))
    cited = []
    for rid in (row["cited_substrate_refs"] or []):
        r = resolved.get(str(rid))
        if r is not None:
            cited.append({"id": r.id, "kind": r.kind, "title": r.title})
    return {
        "kind": "journal_entry",
        "id": str(row["id"]),
        "tier": entry_kind,
        "tier_label": JOURNAL_TIER_LABELS.get(entry_kind, entry_kind),
        "voice_note": JOURNAL_VOICE_NOTE,
        "title": row["title"],
        "analyst_id": row["analyst_id"],
        "analyst_version": row["analyst_version"],
        "period_start": _iso(row["period_start"]),
        "period_end": _iso(row["period_end"]),
        "produced_at": _iso(row["produced_at"]),
        "honesty_flags": list(row["honesty_flags"] or []),
        "body": _normalize_citation_markers(row["body"] or ""),
        "claims": _journal_claims(row, resolved),
        "cited_substrate_refs": cited,
        "verify_state": (
            verify_state_from_score(vr.score)
            if vr is not None
            else unverified_state(
                "no faithfulness verdict recorded for this journal entry"
            )
        ),
    }


def _missing_export_item(kind: str, item_id: str) -> dict[str, Any]:
    """The honest placeholder for a basket id that resolves to no row —
    exported, counted, never silently dropped."""
    return {
        "kind": kind,
        "id": item_id,
        "error": "not found in substrate",
    }


# ---------------------------------------------------------------------------
# Composition — PURE (DB-free), golden-testable.
# ---------------------------------------------------------------------------


def build_document(
    *,
    title: str | None,
    generated_at: datetime,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ONE structured document both formats serialize — JSON returns it
    verbatim; markdown renders it. ``items`` is in basket order and may carry
    ``error`` placeholders for missing ids."""
    missing = sum(1 for i in items if i.get("error"))
    return {
        "title": (title or "").strip() or "Legba export",
        "generated_at": generated_at.isoformat(),
        "item_count": len(items),
        "missing_count": missing,
        "provenance_note": PROVENANCE_NOTE,
        "items": items,
    }


def _md_finding_section(n: int, item: dict[str, Any]) -> list[str]:
    lines = [f"## {n}. {item['title']}", ""]
    meta = [
        f"- kind: `{item['row_kind']}`",
        "- analyst: "
        + (item["analyst_id"] or "(none)")
        + (f" `{item['analyst_version']}`" if item["analyst_version"] else ""),
        f"- target: {item['target_id'] or '(global)'}",
        f"- produced_at: {item['produced_at']}",
    ]
    if item.get("severity"):
        meta.append(f"- severity: {item['severity']}")
    if item.get("confidence") is not None:
        eff = item.get("effective_confidence")
        conf = f"- confidence: {item['confidence']:.2f}"
        if eff is not None and eff != item["confidence"]:
            conf += f" (effective {eff:.2f} after verify fold)"
        meta.append(conf)
    verify_line = f"- verify: {item['verify_state']}"
    flags = item.get("verify_flags") or {}
    if flags:
        flag_bits = ", ".join(f"{v} {k}" for k, v in sorted(flags.items()))
        verify_line += f" · flags: {flag_bits}"
    meta.append(verify_line)
    if item.get("superseded"):
        meta.append("- note: this finding has been SUPERSEDED by a newer row")
    if item.get("receipt_url") or item.get("receipt_path"):
        meta.append(
            f"- receipt: {item.get('receipt_url') or item.get('receipt_path')}"
        )
    lines.extend(meta)
    lines.append("")
    if item.get("body"):
        lines.append(item["body"].rstrip())
        lines.append("")
    citations = item.get("citations") or []
    if citations:
        lines.append("### Citations")
        lines.append("")
        for c in citations:
            title = c.get("title") or "(untitled signal)"
            url = c.get("canonical_url")
            entry = f"- {c['marker']} {title}"
            if url:
                entry += f" — {url}"
            if c.get("archive_sha256"):
                # P2-1: our archived copy of the original bytes exists — the
                # receipt chain terminates in a verifiable hash, not the URL.
                entry += f" — evidence preserved, sha256:{c['archive_sha256']}"
            if not c.get("resolved"):
                entry += " *(signal no longer in substrate; stored citation shown)*"
            lines.append(entry)
        lines.append("")
    else:
        lines.append("*(no resolved citations recorded on this row)*")
        lines.append("")
    return lines


def _md_journal_section(n: int, item: dict[str, Any]) -> list[str]:
    lines = [f"## {n}. {item['title']}", ""]
    lines.extend(
        [
            f"- kind: journal entry · tier: {item['tier_label']}",
            f"- voice: {item['voice_note']}",
            "- analyst: "
            + (item["analyst_id"] or "(none)")
            + (f" `{item['analyst_version']}`" if item["analyst_version"] else ""),
            f"- period: {item['period_start']} → {item['period_end']}",
            f"- produced_at: {item['produced_at']}",
            f"- verify: {item['verify_state']}",
        ]
    )
    if item.get("honesty_flags"):
        lines.append(f"- honesty flags: {', '.join(item['honesty_flags'])}")
    lines.append("")
    if item.get("body"):
        lines.append(item["body"].rstrip())
        lines.append("")
    claims = item.get("claims") or []
    if claims:
        lines.append("### Claims & cited refs")
        lines.append("")
        for c in claims:
            refs = c.get("refs") or []
            ref_bits = (
                "; ".join(
                    f"{r['kind']}: {r['title'] or r['id']}" for r in refs
                )
                if refs
                else "(no refs — unverified perspective)"
            )
            lines.append(f"- [{c['kind']}] \"{c['text_span']}\" → {ref_bits}")
        lines.append("")
    return lines


def render_markdown(doc: dict[str, Any]) -> str:
    """Render the structured document as ONE markdown file: header block
    (generated-at, item count, the honest provenance note), then per-item
    sections in basket order. Deterministic given the document — the golden
    test pins this shape."""
    header_count = str(doc["item_count"])
    if doc.get("missing_count"):
        header_count += f" ({doc['missing_count']} not found)"
    lines: list[str] = [
        f"# {doc['title']}",
        "",
        f"> {doc['provenance_note']}",
        "",
        f"- generated_at: {doc['generated_at']}",
        f"- items: {header_count}",
        "",
    ]
    for n, item in enumerate(doc["items"], start=1):
        lines.append("---")
        lines.append("")
        if item.get("error"):
            lines.append(f"## {n}. ({item['kind']} {item['id']})")
            lines.append("")
            lines.append(f"**{item['error']}** — the basket referenced a row "
                         "this substrate does not hold (superseded/pruned or a "
                         "different environment).")
            lines.append("")
        elif item["kind"] == "journal_entry":
            lines.extend(_md_journal_section(n, item))
        else:
            lines.extend(_md_finding_section(n, item))
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_export_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the export router bound to the registry deps. Mount under
    ``/api/v1/v3`` so the path resolves at ``/api/v1/v3/export``."""
    router = APIRouter(tags=["export"])

    @router.post("/export")
    async def export_collection(
        req: ExportRequest,
        principal: str = Depends(require_bearer),
    ) -> Response:
        if not req.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="items must contain at least one basket entry",
            )
        if len(req.items) > EXPORT_MAX_ITEMS:
            # 413 — the constant was renamed CONTENT_TOO_LARGE in newer
            # Starlette; fall back to the legacy name on older pins.
            code_413 = getattr(
                status,
                "HTTP_413_CONTENT_TOO_LARGE",
                getattr(status, "HTTP_413_REQUEST_ENTITY_TOO_LARGE", 413),
            )
            raise HTTPException(
                status_code=code_413,
                detail=(
                    f"export is capped at {EXPORT_MAX_ITEMS} items; "
                    f"got {len(req.items)} — split the basket"
                ),
            )

        finding_ids = [str(i.id) for i in req.items if i.kind == "finding"]
        journal_ids = [str(i.id) for i in req.items if i.kind == "journal_entry"]

        findings_by_id: dict[str, dict[str, Any]] = {}
        journal_by_id: dict[str, dict[str, Any]] = {}

        async with deps.descriptor_registry.pg.acquire() as conn:
            if finding_ids:
                rows = await conn.fetch(_FINDING_SQL, finding_ids)
                # One batched signal resolution over the WHOLE basket's
                # citation set (no per-finding round-trips).
                all_signal_ids = [
                    str(e["signal_id"])
                    for r in rows
                    for e in _stored_citations(r["data"])
                ]
                signal_index = await _resolve_citation_signals(
                    conn, all_signal_ids
                )
                for r in rows:
                    findings_by_id[str(r["id"])] = _finding_export_item(
                        r, signal_index
                    )
            if journal_ids:
                jrows = await conn.fetch(
                    f"SELECT {_JOURNAL_ENTRY_COLS} FROM journal_entries "
                    "WHERE id = ANY($1::uuid[])",
                    journal_ids,
                )
                ref_ids = [
                    str(rid)
                    for r in jrows
                    for rid in (
                        list(r["cited_substrate_refs"] or [])
                        + [
                            ref
                            for c in (_load_jsonb(r["claims"]) or [])
                            if isinstance(c, dict)
                            for ref in (c.get("refs") or [])
                        ]
                    )
                ]
                resolved = await _resolve_refs(conn, ref_ids)
                verify = await _read_verify_results(
                    conn, [str(r["id"]) for r in jrows]
                )
                for r in jrows:
                    journal_by_id[str(r["id"])] = _journal_export_item(
                        r, resolved, verify
                    )

        # Reassemble in basket order, missing ids as honest placeholders.
        items: list[dict[str, Any]] = []
        for item in req.items:
            key = str(item.id)
            found = (
                findings_by_id.get(key)
                if item.kind == "finding"
                else journal_by_id.get(key)
            )
            items.append(found or _missing_export_item(item.kind, key))

        generated_at = datetime.now(tz=timezone.utc)
        doc = build_document(
            title=req.title, generated_at=generated_at, items=items
        )

        stamp = generated_at.strftime("%Y%m%d")
        if req.format == "json":
            return JSONResponse(
                doc,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="legba-export-{stamp}.json"'
                    )
                },
            )
        return Response(
            content=render_markdown(doc),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="legba-export-{stamp}.md"'
                )
            },
        )

    return router
