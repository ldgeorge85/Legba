# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source drift READS — the panel's data and the agents' version
history.

* :func:`drift_counts` — classifications over a window by class, by source and
  by (source, class), and the open/reviewed/resolved flag counts;
* :func:`open_flags` — flags not yet resolved, newest first, each with its
  dependant, the version pair, the class and the changed passages recorded
  on its classification (bounded);
* :func:`version_history` — every version of one address with the
  classification between consecutive ones;
* :func:`editing_sources` — the sources (else hosts) whose pages change
  after publication most: substantive-or-worse classifications per source,
  with the share of that source's classified pages; D-2 adds each source's
  EDIT RECORD over 7 and 30 days (assessments by type, acknowledged vs
  silent, history rewrites) beside its denominator (pages re-checked). Just
  a metric to track: it feeds NO credibility or score (a guard test pins
  that no credibility/scoring module imports it);
* :func:`assessments_for` — D-2's assessments of one change, one address or
  one flag, with each flag's claim effect.

D-2: flags carry their change's latest assessment and their claim effect,
and review lists sort history rewrites first, then claim reversed/removed,
retraction and page gone, then the rest, spelling corrections last.

Stdlib only — the registry's slim image serves these through
``legba.data.registry.evidence_drift_api``. ``None`` from any of them means
the tables are not there yet (migration 0252 not applied): the route answers
``available: false``.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

__all__ = [
    "MAX_WINDOW_HOURS",
    "SUBSTANTIVE_OR_WORSE",
    "drift_counts",
    "LATEST_ASSESSMENT_SQL",
    "assessments_for",
    "edit_record",
    "editing_sources",
    "open_flags",
    "version_history",
]

MAX_WINDOW_HOURS = 24 * 90
MAX_FLAGS = 500
SUBSTANTIVE_OR_WORSE = ("substantive_edit", "title_changed", "retracted_or_replaced", "gone")
_MISSING = ("UndefinedTableError", "UndefinedColumnError")


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


async def _guard(coro: Any) -> Any:
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ in _MISSING:
            return None
        raise


async def drift_counts(conn: Any, *, window_hours: int = 168) -> Optional[dict[str, Any]]:
    hours = max(1, min(int(window_hours), MAX_WINDOW_HOURS))

    async def _run() -> dict[str, Any]:
        rows = await conn.fetch(
            "SELECT coalesce(source_id, 'host:' || coalesce(host, 'unknown')) AS subject, drift_class, "
            "count(*) AS n, sum(flags_written) AS flags "
            "FROM evidence_drift_classifications "
            "WHERE classified_at >= now() - make_interval(hours => $1) GROUP BY 1, 2", hours)
        flag_states = await conn.fetch(
            "SELECT state, count(*) AS n FROM evidence_drift_flags "
            "WHERE created_at >= now() - make_interval(hours => $1) GROUP BY 1", hours)
        open_total = await conn.fetchval(
            "SELECT count(*) FROM evidence_drift_flags WHERE state <> 'resolved'")
        assessed = await conn.fetch(
            "SELECT change_type, count(*) AS n, count(*) FILTER (WHERE history_rewrite) AS hr "
            "FROM evidence_drift_assessments "
            "WHERE assessed_at >= now() - make_interval(hours => $1) GROUP BY 1", hours)
        auto = await conn.fetchrow(
            "SELECT count(*) AS total, count(*) FILTER (WHERE closed_at >= now() - make_interval(hours => $1)) "
            "AS recent, (SELECT count(*) FROM evidence_drift_flags r JOIN evidence_drift_auto_closes x "
            "            ON x.flag_id = r.reopened_from) AS reopened "
            "FROM evidence_drift_auto_closes", hours)
        shadow = await conn.fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE recorded_at >= now() - make_interval(hours => $1)) AS recent, "
            "max(rule_version) AS rule_version, "
            "(SELECT switch_on FROM evidence_drift_would_auto_close ORDER BY recorded_at DESC LIMIT 1) "
            "AS switch_on FROM evidence_drift_would_auto_close", hours)
        late_unexplained = await conn.fetchval(
            "SELECT count(*) FROM evidence_drift_assessments WHERE late_unexplained_change "
            "AND assessed_at >= now() - make_interval(hours => $1)", hours)
        by_class: dict[str, int] = {}
        by_source: dict[str, dict[str, int]] = {}
        flags_by_source: dict[str, int] = {}
        for r in rows:
            by_class[r["drift_class"]] = by_class.get(r["drift_class"], 0) + int(r["n"])
            by_source.setdefault(r["subject"], {})[r["drift_class"]] = int(r["n"])
            flags_by_source[r["subject"]] = flags_by_source.get(r["subject"], 0) + int(r["flags"] or 0)
        return {
            "window_hours": hours, "classified": sum(by_class.values()), "by_class": by_class,
            "by_source": by_source, "flags_by_source": flags_by_source,
            "flags": {r["state"]: int(r["n"]) for r in flag_states},
            "open_flags_total": int(open_total or 0),
            "assessments_by_type": {r["change_type"]: int(r["n"]) for r in assessed},
            "history_rewrites": sum(int(r["hr"]) for r in assessed),
            "auto_closed": {"window": int(auto["recent"]), "total": int(auto["total"]),
                            "reopened": int(auto["reopened"])},
            "late_unexplained_changes": int(late_unexplained or 0),
            "would_auto_close": {"window": int(shadow["recent"]), "total": int(shadow["total"]),
                                 "rule_version": shadow["rule_version"],
                                 "switch_on": bool(shadow["switch_on"])},
        }

    return await _guard(_run())


#: The latest assessment of a classification (a lateral subquery body;
#: ``c`` is the classification).
LATEST_ASSESSMENT_SQL = (
    "SELECT a.id, a.change_type, a.component_types, a.acknowledged, a.summary, a.history_rewrite, "
    "a.late_unexplained_change, a.model_type_doubt, a.acknowledged_signals, "
    "a.assessor, a.assessor_stamp, a.timing_bucket, a.interval_hours, a.claims_changed, a.share_changed "
    "FROM evidence_drift_assessments a WHERE a.classification_id = c.id "
    "ORDER BY a.assessed_at DESC, a.id DESC LIMIT 1")

#: Review order (D-2): history rewrites and late unexplained changes, then
#: rows the model doubts the code type of, then claim reversed / removed /
#: fact corrected / retraction / page gone / content replaced, then the
#: rest, spelling corrections last.
_PRIORITY_SQL = (
    "CASE WHEN a.history_rewrite OR a.late_unexplained_change THEN 0 "
    "WHEN a.model_type_doubt IS NOT NULL THEN 1 "
    "WHEN a.change_type IN ('claim_reversed', 'claim_removed', 'fact_corrected', 'retraction', "
    "  'page_gone', 'content_replaced') "
    "  OR a.component_types && ARRAY['claim_reversed', 'claim_removed', 'fact_corrected', 'retraction', "
    "  'page_gone', 'content_replaced']::text[] THEN 2 "
    "WHEN a.change_type = 'spelling_or_name_correction' THEN 4 ELSE 3 END")

CLOSED_BY = ("any", "system", "other")


async def open_flags(conn: Any, *, limit: int = 100, state: str = "open",
                     source_id: Optional[str] = None, closed_by: str = "any",
                     would_auto_close: bool = False) -> Optional[list[dict[str, Any]]]:
    """Flags in ``state`` (``open``, ``reviewed``, ``resolved``, or
    ``unresolved`` for open + reviewed), in review order (D-2: history
    rewrites first, spelling corrections last; then newest first), each with
    the changed passages of its drift, its change's latest assessment and its
    claim effect. ``closed_by`` (resolved flags): ``system`` = closed by a
    ``system::`` principal (the drift assessor's tracked auto-close),
    ``other`` = by a person or a unit. ``would_auto_close``: only flags the
    auto-close rule would close (the shadow count)."""
    lim = max(1, min(int(limit), MAX_FLAGS))
    states = ["open", "reviewed"] if state == "unresolved" else [state]
    who = closed_by if closed_by in CLOSED_BY else "any"

    async def _run() -> list[dict[str, Any]]:
        rows = await conn.fetch(
            "SELECT f.id, f.drift_id, f.address, f.source_id, f.earlier_version_id, f.later_version_id, "
            "f.drift_class, f.dependant_kind, f.dependant_id, f.depth, f.via_kind, f.via_id, "
            "f.cited_span, f.span_in_earlier, f.span_in_later, f.span_check, f.state, f.created_at, "
            "f.reviewed_by, f.review_note, f.resolved_by, f.resolution, f.resolution_note, f.resolved_at, "
            "f.reopened_from, f.reopened_by, f.reopen_note, "
            "c.rule, c.earlier_fetched_at, c.later_fetched_at, "
            "c.evidence->'changed' AS changed, c.evidence->'removed' AS removed, "
            "c.evidence->'critical' AS critical, "
            "a.id AS assessment_id, a.change_type, a.component_types, a.acknowledged, a.summary, "
            "a.history_rewrite, a.late_unexplained_change, a.model_type_doubt, a.assessor, a.timing_bucket, "
            "e.claim_effect, e.after_text AS claim_after_text, e.model_effect, "
            "(x.flag_id IS NOT NULL) AS auto_closed, (w.flag_id IS NOT NULL) AS would_auto_close, " + _PRIORITY_SQL + " AS review_priority "
            "FROM evidence_drift_flags f JOIN evidence_drift_classifications c ON c.id = f.classification_id "
            "LEFT JOIN LATERAL (" + LATEST_ASSESSMENT_SQL + ") a ON true "
            "LEFT JOIN evidence_drift_claim_effects e ON e.flag_id = f.id AND e.assessment_id = a.id "
            "LEFT JOIN evidence_drift_auto_closes x ON x.flag_id = f.id "
            "LEFT JOIN LATERAL (SELECT w.flag_id FROM evidence_drift_would_auto_close w "
            "                    WHERE w.flag_id = f.id LIMIT 1) w ON true "
            "WHERE f.state = ANY($1::text[]) AND ($3::text IS NULL OR f.source_id = $3) "
            "AND ($4 = 'any' OR ($4 = 'system') = coalesce(f.resolved_by LIKE 'system::%', false)) "
            "AND (NOT $5 OR w.flag_id IS NOT NULL) "
            "ORDER BY review_priority, f.created_at DESC, f.id DESC LIMIT $2", states, lim, source_id, who,
            bool(would_auto_close))
        out = []
        for r in rows:
            d = {k: _iso(r[k]) for k in r.keys()}
            for k in ("changed", "removed", "critical", "span_check", "model_type_doubt"):
                d[k] = _json(d[k])
            if d.get("component_types") is not None:
                d["component_types"] = list(d["component_types"])
            if isinstance(d.get("cited_span"), str):
                d["cited_span"] = d["cited_span"][:600]
            out.append(d)
        return out

    return await _guard(_run())


async def assessments_for(conn: Any, *, drift_id: Optional[int] = None, flag_id: Optional[int] = None,
                          address: Optional[str] = None, limit: int = 20) -> Optional[dict[str, Any]]:
    """Every assessment (newest first) of one change (``drift_id``), of the
    change behind one flag (``flag_id``), or of every change of one address,
    with each flag's claim effect under each assessment."""
    lim = max(1, min(int(limit), 200))

    async def _run() -> dict[str, Any]:
        rows = await conn.fetch(
            "SELECT a.*, c.drift_class, c.rule, c.later_fetched_at FROM evidence_drift_assessments a "
            "JOIN evidence_drift_classifications c ON c.id = a.classification_id "
            "WHERE ($1::bigint IS NULL OR a.drift_id = $1) "
            "AND ($2::bigint IS NULL OR a.classification_id = "
            "     (SELECT classification_id FROM evidence_drift_flags WHERE id = $2)) "
            "AND ($3::text IS NULL OR a.address = $3) "
            "ORDER BY c.later_fetched_at DESC, a.assessed_at DESC, a.id DESC LIMIT $4",
            drift_id, flag_id, address, lim)
        ids = [int(r["id"]) for r in rows]
        effects = await conn.fetch(
            "SELECT e.assessment_id, e.flag_id, f.dependant_kind, f.dependant_id, e.claim_effect, "
            "e.after_text, e.basis, e.model_status, e.model_effect, e.model_after_text "
            "FROM evidence_drift_claim_effects e JOIN evidence_drift_flags f ON f.id = e.flag_id "
            "WHERE e.assessment_id = ANY($1::bigint[]) "
            "AND ($2::bigint IS NULL OR e.flag_id = $2) ORDER BY e.flag_id", ids, flag_id) if ids else []
        by_a: dict[int, list[dict[str, Any]]] = {}
        for e in effects:
            d = {k: _iso(e[k]) for k in e.keys() if k != "assessment_id"}
            d["basis"] = _json(d.get("basis"))
            by_a.setdefault(int(e["assessment_id"]), []).append(d)
        out = []
        for r in rows:
            d = {k: _iso(r[k]) for k in r.keys()}
            for k in ("model_output", "acknowledged_evidence", "scope_pairs", "removed_claims",
                      "observations", "history_rewrite_inputs", "late_unexplained_inputs", "model_type_doubt"):
                d[k] = _json(d.get(k))
            for k in ("component_types", "code_components", "acknowledged_signals"):
                d[k] = list(d.get(k) or [])
            d["claim_effects"] = by_a.get(int(r["id"]), [])
            out.append(d)
        return {"assessments": out}

    return await _guard(_run())


async def version_history(conn: Any, address: str, *, limit: int = 50) -> Optional[dict[str, Any]]:
    """Every fetched version of ``address`` (oldest first) and the classified
    change between consecutive ones."""

    async def _run() -> dict[str, Any]:
        vers = await conn.fetch(
            "SELECT id, url, role, sha256, text_sha256, text_chars, status, size_bytes, "
            "first_seen_at, last_seen_at, fetch_count, first_source_id, bytes_pruned_at "
            "FROM fetch_versions WHERE address = $1 AND receipt_kind = 'fetch' "
            "ORDER BY first_seen_at, id LIMIT $2", address, max(1, min(int(limit), 500)))
        changes = await conn.fetch(
            "SELECT c.drift_id, c.earlier_version_id, c.later_version_id, c.drift_class, c.rule, "
            "c.later_fetched_at, c.flags_written, d.later_gone, d.later_status, "
            "a.id AS assessment_id, a.change_type, a.component_types, a.acknowledged, a.summary, "
            "a.history_rewrite, a.late_unexplained_change, a.assessor, a.timing_bucket "
            "FROM evidence_drift_classifications c JOIN fetch_drift_observations d ON d.id = c.drift_id "
            "LEFT JOIN LATERAL (" + LATEST_ASSESSMENT_SQL + ") a ON true "
            "WHERE c.address = $1 ORDER BY c.later_fetched_at, c.id", address)
        unclassified = await conn.fetchval(
            "SELECT count(*) FROM fetch_drift_observations d WHERE d.address = $1 AND NOT EXISTS "
            "(SELECT 1 FROM evidence_drift_classifications c WHERE c.drift_id = d.id)", address)
        return {
            "address": address,
            "versions": [{k: _iso(r[k]) for k in r.keys()} for r in vers],
            "changes": [{k: (list(r[k]) if k == "component_types" and r[k] is not None else _iso(r[k]))
                         for k in r.keys()} for r in changes],
            "unclassified_drift_rows": int(unclassified or 0),
        }

    return await _guard(_run())


async def editing_sources(conn: Any, *, window_hours: int = 24 * 30,
                          limit: int = 25) -> Optional[list[dict[str, Any]]]:
    """Sources ranked by how often their pages change in substance after we
    read them (substantive edit, title change, retraction/replacement, gone)."""
    hours = max(1, min(int(window_hours), MAX_WINDOW_HOURS))

    async def _run() -> list[dict[str, Any]]:
        rows = await conn.fetch(
            "WITH c AS (SELECT coalesce(source_id, 'host:' || coalesce(host, 'unknown')) AS subject, "
            "                  address, drift_class "
            "             FROM evidence_drift_classifications "
            "            WHERE classified_at >= now() - make_interval(hours => $1)), "
            "per AS (SELECT subject, drift_class, count(*) AS n FROM c GROUP BY 1, 2), "
            "pages AS (SELECT subject, count(DISTINCT address) AS pages FROM c GROUP BY 1) "
            "SELECT per.subject, "
            "       coalesce(sum(n) FILTER (WHERE drift_class = ANY($2::text[])), 0) AS substantive, "
            "       sum(n) AS classified, max(pages.pages) AS pages, "
            "       jsonb_object_agg(drift_class, n) AS by_class "
            "  FROM per JOIN pages ON pages.subject = per.subject "
            " GROUP BY per.subject "
            "HAVING coalesce(sum(n) FILTER (WHERE drift_class = ANY($2::text[])), 0) > 0 "
            " ORDER BY 2 DESC, 3 DESC LIMIT $3", hours, list(SUBSTANTIVE_OR_WORSE),
            max(1, min(int(limit), 200)))
        out = []
        for r in rows:
            classified = int(r["classified"])
            out.append({
                "subject": r["subject"], "substantive_or_worse": int(r["substantive"]),
                "classified": classified, "pages": int(r["pages"]),
                "share": round(int(r["substantive"]) / classified, 3) if classified else None,
                "by_class": _json(r["by_class"]),
            })
        subjects = [o["subject"] for o in out]
        if subjects:
            record = await edit_record(conn, subjects)
            for o in out:
                o["edit_record"] = record.get(o["subject"], _empty_record())
        return out

    return await _guard(_run())


# -- D-2: the per-source edit record (a metric; feeds nothing) ----------------------

#: The edit record's two fixed windows, in days.
EDIT_RECORD_WINDOWS = (7, 30)

_SUBJECT_C = "coalesce(c.source_id, 'host:' || coalesce(c.host, 'unknown'))"

#: The LATEST assessment of each change classified in the last 30 days, by
#: subject: type, acknowledged, history_rewrite.
_EDIT_RECORD_SQL = (
    "SELECT " + _SUBJECT_C + " AS subject, c.classified_at, a.change_type, a.acknowledged, a.history_rewrite, "
    "a.late_unexplained_change "
    "FROM evidence_drift_classifications c "
    "JOIN LATERAL (" + LATEST_ASSESSMENT_SQL + ") a ON true "
    "WHERE c.classified_at >= now() - interval '30 days' AND " + _SUBJECT_C + " = ANY($1::text[])")

#: The denominator: pages of each subject the platform READ AGAIN (a second
#: fetch of the same bytes, or a second version) whose last read falls in the
#: window, changed or not.
_RECHECKED_SQL = """
WITH v AS (
    SELECT address,
           coalesce(min(first_source_id),
                    'host:' || lower(split_part(split_part(address, '://', 2), '/', 1))) AS subject,
           max(last_seen_at) AS last_seen, sum(fetch_count) AS fetches, count(*) AS versions
      FROM fetch_versions
     WHERE receipt_kind = 'fetch' AND last_seen_at >= now() - interval '30 days'
     GROUP BY address
)
SELECT subject,
       count(*) FILTER (WHERE last_seen >= now() - interval '7 days') AS rechecked_7d,
       count(*) AS rechecked_30d
  FROM v
 WHERE (fetches > 1 OR versions > 1) AND subject = ANY($1::text[])
 GROUP BY subject
"""


def _empty_window() -> dict[str, Any]:
    return {"assessed": 0, "by_type": {},
            "acknowledged": {"correction_notice": 0, "visible_update_stamp": 0, "metadata_date_only": 0,
                             "silent": 0},
            "history_rewrite": 0, "late_unexplained_change": 0, "pages_rechecked": 0}


def _empty_record() -> dict[str, Any]:
    return {f"{d}d": _empty_window() for d in EDIT_RECORD_WINDOWS}


async def edit_record(conn: Any, subjects: list[str]) -> dict[str, dict[str, Any]]:
    """Per subject, over 7 and 30 days: assessments by type (a ``mixed``
    change counts once, as ``mixed``), the four acknowledgement levels,
    history rewrites, late unexplained changes, and the denominator, pages re-checked. A METRIC TO TRACK ONLY:
    nothing reads it into source credibility, host credibility or any score
    (that is a later, separate decision; SEAMS row 80)."""
    from datetime import timedelta, timezone  # noqa: PLC0415

    out: dict[str, dict[str, Any]] = {s: _empty_record() for s in subjects}
    now = datetime.now(timezone.utc)
    for r in await conn.fetch(_EDIT_RECORD_SQL, subjects):
        age = now - r["classified_at"]
        for d in EDIT_RECORD_WINDOWS:
            if age > timedelta(days=d):
                continue
            w = out[r["subject"]][f"{d}d"]
            w["assessed"] += 1
            w["by_type"][r["change_type"]] = w["by_type"].get(r["change_type"], 0) + 1
            w["acknowledged"][r["acknowledged"]] = w["acknowledged"].get(r["acknowledged"], 0) + 1
            w["history_rewrite"] += bool(r["history_rewrite"])
            w["late_unexplained_change"] += bool(r["late_unexplained_change"])
    for r in await conn.fetch(_RECHECKED_SQL, subjects):
        rec = out.get(r["subject"])
        if rec is not None:
            rec["7d"]["pages_rechecked"] = int(r["rechecked_7d"])
            rec["30d"]["pages_rechecked"] = int(r["rechecked_30d"])
    return out
