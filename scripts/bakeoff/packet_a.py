# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm A's PACKET — the desk brief exactly as the platform serves it (design §2, arm A).

A asks no model. Its packet is, per desk in the question's scope, the document
the subject page's Brief tab composes — the SAME reads, in the SAME order:

  * ``GET /findings?target_id=<desk>&analyst_id=country_composition&limit=1``
    — the newest composition record (``state/exportBasket.fetchCompositionRow``);
  * each bounded unit's latest admitted read, in the composition's declared
    order — the record's ``assembly.coverage`` (else its blocks by ordinal),
    exactly ``exportBasket.composeDeskBrief``;
  * the open situations (every status but ``resolved``, intensity desc, last
    event desc, 20) and their tracked events (20 each) as the markdown appendix
    ``buildDeskBriefAppendix`` writes;
  * ``POST /v3/export`` with ``format: json`` and
    ``appendix: {markdown, absences: true, scope: <desk>}`` — which composes the
    items with their citations and verify state, and the desk's typed absence;
  * the fenced country voice (``country_assessment`` row whose
    ``derived_from[0]`` is the record; else the newest — the page's
    ``pickAssessmentRow``), only when the record's tier is ``country``;
  * the banded scorecard row (``/v3/eval/country_scorecard``).

Design additions: a lane / flow desk adds its ``disruption_status`` read; a
``situation_*`` desk adds its member desks' briefs, resolved by the same ladder
``list_findings`` widens with (``runtime/target_resolution``). A ``region_*``
desk would add the region rollup and the world record — no v1.0 question is
scoped to a region desk, so that leg is refused by name rather than guessed.

THE RENDERING. The packet is rendered to ONE text (the record's
``raw_answer``): each item's body with its citation markers rewritten to the
uniform ``(signal:<uuid>)`` / ``(finding:<uuid>)`` the normaliser's N2 maps
(two items' ``[3]`` are different sources, so the per-item marker table is
applied here, where it is known), a verify line, the absences one per line as
``<kind>: <subject>``, carried-state and scorecard bands. ``segments`` keeps,
per paragraph, the finding it came from and that finding's effective
confidence — what the normaliser needs for A's confidence rule when a desk has
no composition (design §2: the unit read that supplied the top sentence).
"""

from __future__ import annotations

import re
from typing import Any

COMPOSITION_ANALYST = "country_composition"
VOICE_ANALYST = "country_assessment"
DISRUPTION_ANALYST = "disruption_status"
SITUATION_LIMIT = 20
EVENTS_PER_SITUATION = 20

_MARKER = re.compile(r"\[\[ref:\d+\]\]|\[\d+\]")


class PacketNotBuilt(RuntimeError):
    """A design leg this harness does not build (named, never guessed)."""


class ExportRefused(RuntimeError):
    """``POST /v3/export`` refused the desk brief (its status and detail)."""


def _first(rows: Any) -> dict[str, Any] | None:
    data = rows.get("data") if isinstance(rows, dict) else None
    return data[0] if data else None


def _assembly(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    inner = ((row.get("data") or {}).get("data") or {})
    a = inner.get("assembly")
    return a if isinstance(a, dict) and a.get("schema") == "assembly.v1" else None


def compose_items(composition: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """``(finding ids in reading order, units with no admitted read)`` —
    ``exportBasket.composeDeskBrief``'s ordering, verbatim in Python."""
    if not composition:
        return [], []
    items = [composition["id"]]
    missing: list[str] = []
    a = _assembly(composition)
    if a:
        blocks = [b for b in a.get("blocks") or [] if isinstance(b, dict) and b.get("finding_id")]
        by_unit = {b.get("desk"): b for b in blocks}
        coverage = [c for c in a.get("coverage") or [] if isinstance(c, dict)]
        order = (
            [c.get("unit") for c in coverage] if coverage
            else [b.get("desk") for b in sorted(blocks, key=lambda b: b.get("ordinal") or 0)]
        )
        for unit in order:
            b = by_unit.get(unit)
            if b:
                items.append(str(b["finding_id"]))
            else:
                missing.append(str(unit))
    return items, missing


def situations_appendix(situations: list[dict[str, Any]], events: list[list[dict[str, Any]]]) -> str | None:
    """``buildDeskBriefAppendix``, verbatim."""
    if not situations:
        return None
    lines = ["## Open situations & tracked events", ""]
    for s, evs in zip(situations, events):
        lines += [f"### {s['name']}", ""]
        lines.append(
            f"- status: {s['status']} · category: {s['category']} · "
            f"intensity: {float(s.get('intensity_score') or 0):.2f} · situation id: {s['id']}"
        )
        if not evs:
            lines.append("- tracked events: none linked yet")
        else:
            lines.append(f"- tracked events ({len(evs)}):")
            for e in evs:
                lines.append(f"  - {e['title']} — {e['lifecycle_state']} (event id: {e['id']})")
        lines.append("")
    return "\n".join(lines).rstrip()


def _open_situations(reg: Any, desk: str) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    rows = (reg.get("/api/v1/situations", target_id=desk, limit=100) or {}).get("data") or []
    open_rows = [s for s in rows if s.get("status") != "resolved"]
    open_rows.sort(key=lambda s: (
        -float(s.get("intensity_score") or 0),
        -(_ts(s.get("last_event_at"))),
    ))
    open_rows = open_rows[:SITUATION_LIMIT]
    events = [
        (reg.get("/api/v1/v3/events", situation_id=s["id"], limit=EVENTS_PER_SITUATION) or {}).get("data") or []
        for s in open_rows
    ]
    return open_rows, events


def _ts(raw: Any) -> float:
    from datetime import datetime

    if not raw:
        return float("-inf")
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _rewrite_markers(body: str, citations: list[dict[str, Any]]) -> str:
    table: dict[str, str] = {}
    for c in citations or []:
        mk = c.get("marker")
        if not mk:
            continue
        if c.get("signal_id"):
            table[mk] = f"(signal:{c['signal_id']})"
        elif c.get("ref_id") and c.get("citation_kind") == "finding":
            table[mk] = f"(finding:{c['ref_id']})"
    return _MARKER.sub(lambda m: table.get(m.group(0), m.group(0)), body or "")


def _absence_lines(absences: dict[str, Any] | None) -> list[str]:
    lines: list[str] = []
    grouped = (absences or {}).get("by_kind")
    if grouped is None:  # the flat ``/v3/absence`` shape
        flat = (absences or {}).get("absences") or []
        grouped = [{"kind": k, "items": [a for a in flat if a.get("kind") == k]}
                   for k in dict.fromkeys(a.get("kind") for a in flat)]
    for kind in grouped:
        for item in kind.get("items") or []:
            label = item.get("subject") or ""
            quoted = re.search(r"“([^”]+)”", item.get("reason") or "")
            if quoted and re.fullmatch(r"[0-9a-f]{64}", str(label)):
                label = quoted.group(1)
            lines.append(f"{kind.get('kind')}: {label}")
    return lines


def _scorecard_lines(reg: Any, desk: str) -> list[str]:
    rows = reg.get_or_none("/api/v1/v3/eval/country_scorecard", target_id=desk) or []
    if not rows:
        return []
    dims = rows[0].get("dimensions") or {}
    out = []
    for unit, d in sorted(dims.items()):
        band = d.get("band")
        reason = d.get("reason")
        out.append(f"{unit}: {band}" + (f" ({reason})" if band == "insufficient-evidence" and reason else ""))
    return out


def desk_brief(reg: Any, desk: str) -> dict[str, Any]:
    """One desk's served brief: the export document + the reads beside it."""
    if desk.startswith("region_"):
        raise PacketNotBuilt(
            f"{desk}: the region rollup + world record leg is not built — no v1.0 "
            "question is scoped to a region desk"
        )
    composition = _first(reg.get("/api/v1/findings", target_id=desk, analyst_id=COMPOSITION_ANALYST, limit=1, fields="full"))
    items, missing_units = compose_items(composition)
    disruption = None
    if desk.startswith(("lane_", "flow_")):
        disruption = _first(reg.get("/api/v1/findings", target_id=desk, analyst_id=DISRUPTION_ANALYST, limit=1, fields="full"))
        if disruption:
            items.append(disruption["id"])
    situations, events = _open_situations(reg, desk)
    appendix_md = situations_appendix(situations, events)
    if items:
        try:
            doc = reg.export_json({
                "items": [{"kind": "finding", "id": i} for i in items],
                "format": "json",
                "appendix": {"markdown": appendix_md, "absences": True, "scope": desk},
            })
        except ExportRefused as exc:
            # §3.5: the brief the export refuses at T is VOID FOR A, and the
            # refusal is A's published result for the row — never a re-composed
            # brief the platform did not serve.
            doc = {"items": [], "export_refused": str(exc)}
    else:
        # The route refuses an empty basket (400); a desk with no record and no
        # lane read is served as its appendix + its typed absence, read off the
        # same reader the export composes from (``GET /v3/absence``).
        doc = {
            "items": [],
            "appendix": appendix_md,
            "absences": reg.get("/api/v1/v3/absence", scope=desk),
            "export_refused": "no basket items: the desk has no record and no lane read",
        }
    voice = None
    a = _assembly(composition)
    if composition and a and a.get("tier") == "country":
        rows = (reg.get("/api/v1/findings", analyst_id=VOICE_ANALYST, target_id=desk, limit=5, fields="full") or {}).get("data") or []
        cands = [r for r in rows if r.get("analyst_id") == VOICE_ANALYST and r.get("target_id") == desk]
        voice = next((r for r in cands if (r.get("derived_from") or [None])[0] == composition["id"]), None)
        if voice is None and cands:
            voice = sorted(cands, key=lambda r: _ts(r.get("produced_at")), reverse=True)[0]
    return {
        "desk": desk,
        "composition_id": composition["id"] if composition else None,
        "composition_confidence": composition.get("confidence") if composition else None,
        "missing_units": missing_units,
        "export": doc,
        "voice": voice,
        "scorecard": _scorecard_lines(reg, desk),
    }


def scope_desks(desk: str, members: list[str] | None) -> list[str]:
    """The desk itself, then (for a ``situation_*`` desk) its resolved members."""
    return [desk, *[m for m in (members or []) if m != desk]]


def render(briefs: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
    """``(packet text, segments, cited refs)`` — see the module docstring."""
    parts: list[str] = []
    segments: list[dict[str, Any]] = []
    refs: list[dict[str, str]] = []

    def add(text: str, finding_id: str | None = None, eff: Any = None) -> None:
        text = text.strip()
        if not text:
            return
        parts.append(text)
        segments.append({"text": text, "finding_id": finding_id, "effective_confidence": eff})

    for b in briefs:
        doc = b.get("export") or {}
        for it in doc.get("items") or []:
            if it.get("error"):
                add(f"{it.get('id')}: not found in substrate")
                continue
            body = _rewrite_markers(it.get("body") or "", it.get("citations") or [])
            add(f"{it.get('title') or ''}\n\n{body}", it.get("id"), it.get("effective_confidence"))
            if it.get("verify_state"):
                add(f"Verification: {it['verify_state']}.", it.get("id"), it.get("effective_confidence"))
            for c in it.get("citations") or []:
                if c.get("signal_id"):
                    refs.append({"ref": str(c["signal_id"]), "kind": "signal"})
                elif c.get("ref_id") and c.get("citation_kind") == "finding":
                    refs.append({"ref": str(c["ref_id"]), "kind": "finding"})
        voice = b.get("voice")
        if voice:
            vbody = _rewrite_markers(voice.get("body") or "", (
                ((voice.get("data") or {}).get("data") or {}).get("citations") or []
            ))
            add(f"{voice.get('title') or ''}\n\n{vbody}", voice.get("id"), voice.get("effective_confidence"))
        if doc.get("appendix"):
            add(str(doc["appendix"]))
        for line in _absence_lines(doc.get("absences")):
            add(line)
        for unit in b.get("missing_units") or []:
            add(f"no_read: {unit}")
        for line in b.get("scorecard") or []:
            add(line)
    seen: set[str] = set()
    uniq = [r for r in refs if not (r["ref"] in seen or seen.add(r["ref"]))]
    return "\n\n".join(parts), segments, uniq
