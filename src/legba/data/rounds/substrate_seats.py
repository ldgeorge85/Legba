# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The substrate code seats: the five code functions a round spec may name
that READ THE RECORD (catalog ``code_fns`` with ``pure: false``).

Each reads through an injected READ-ONLY ``fetch(sql, *params)`` (the hosts
open it on a read-only session with a statement timeout) and, for the source
fleet, the injected tool ``dispatch`` (the consult router); each applies the
run's as-of cut; each returns its typed row and WRITES NOTHING.

=========================  ==================  ====================================
Function                   Row                 What it reads, at the cut
=========================  ==================  ====================================
``scope_and_freeze``       ``frozen_scope/1``  the subject's desk slice over the
                                               window ending at the as-of time
``core_packet_and_slices`` ``seat_slices/1``   the frozen rows, cut to a core
                                               packet and the remainder
``series_panel``           ``series_panel/1``  observation series for the
                                               subject's places, recorded before
                                               the cut
``coverage_audit``         ``blind_spots/1``   the source fleet as it stood at
                                               the cut (``list_sources``):
                                               stale, dead, quiet, erroring feeds
``statement_gate_shadow``  ``statement_       the S-5 honesty gates over the
                           proposals/1``       ruling's sentences, in shadow:
                                               split, subject incl. actors,
                                               outcome (``.shadow_gate``)
=========================  ==================  ====================================

THE DESK SLICE, AT A CUT. A desk's live slice
(``runtime/actor_substrate_slice._read_substrate_slice``) is anchored on
``NOW()`` and cannot take an as-of time, so ``scope_and_freeze`` composes the
SAME named parts that reader is built from — the target head's sources, geo
and tag scope; ``SIGNALS_EXCLUDE_BACKFILL_SQL``; ``CANONICAL_ONLY_SQL``; the
live origin-class leg; the research exclusion; ``desk_geo_sql`` and the
homonym filter; ``target_tag_filter``; the window clause
(``slice_coverage.window_clauses``) — over a window that ENDS at the as-of
time, with the record-time cut (``created_at < as_of``), and selects with the
desk's own selector (``slice_coverage.select_by_salience``). Declared limits:
a thematic target's ``scope.predicate`` (a Starlark residual the live reader
applies after the query) is not applied; the target head and the
descriptor-free defaults below are today's, not the as-of time's.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .rows import sentences_of
from .seats import SeatCall, SeatFailure, SeatNotBuilt, SeatReply
from .tables import readable_text

Fetch = Callable[..., Awaitable[Sequence[Mapping[str, Any]]]]
Dispatch = Callable[[str, Mapping[str, Any], Any], Awaitable[Any]]

#: Hours of the frozen window (a desk's default reactive window is a week).
DEFAULT_WINDOW_HOURS = 168
#: Rows kept in the frozen scope, and in the core packet.
DEFAULT_ROW_CAP = 40
DEFAULT_CORE_ROWS = 24
#: Rows of one source in the core packet (the desk's strict cap shape).
DEFAULT_PER_SOURCE_CAP = 4
#: Top rows by salience the scope marks ``required`` (the context builder
#: must keep them: gate ``must_include_set``).
DEFAULT_REQUIRED = 3
#: Characters of a row's text handed to a seat.
ROW_TEXT_CHARS = 1500
#: Series and points in the series panel.
MAX_SERIES = 12
MAX_POINTS = 8
#: Staleness verdicts (``list_sources``) the coverage audit reports.
AUDIT_STATES = ("stale", "dead", "unknown", "quiet")


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v is not None else None)


def _as_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        dt = v
    else:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise SeatFailure(f"as_of must carry a timezone (got {v!r})")
    return dt.astimezone(timezone.utc)


def _body(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _input(call: SeatCall, name: str) -> Any:
    return next((h.payload for h in call.inputs if h.ref_id == f"input.{name}"), None)


def _output(call: SeatCall, row_type: str) -> Mapping[str, Any] | None:
    return next((h.payload for h in call.inputs
                 if h.row_type == row_type and isinstance(h.payload, Mapping)), None)


@dataclass
class SubstrateSeats:
    """The substrate code functions as a :data:`~.seats.SeatFn` (code seats
    only). ``fetch`` and ``dispatch`` are read-only; ``as_of`` is the run's
    cut; ``run`` runs a coroutine from the calling thread."""

    fetch: Fetch
    run: Callable[[Awaitable[Any]], Any]
    as_of: datetime
    subject: str | None = None
    dispatch: Dispatch | None = None
    window_hours: int = DEFAULT_WINDOW_HOURS
    row_cap: int = DEFAULT_ROW_CAP
    core_rows: int = DEFAULT_CORE_ROWS
    per_source_cap: int = DEFAULT_PER_SOURCE_CAP
    required: int = DEFAULT_REQUIRED

    FNS = ("scope_and_freeze", "core_packet_and_slices", "series_panel", "coverage_audit",
           "statement_gate_shadow")

    def __call__(self, call: SeatCall) -> SeatReply:
        fn = call.seat.fn or ""
        if call.seat.kind != "code" or fn not in self.FNS:
            raise SeatNotBuilt(f"SubstrateSeats has no code function {fn!r}")
        payload = self.run(getattr(self, f"_{fn}")(call))
        return SeatReply(payload)

    # -- shared ----------------------------------------------------------------

    async def _target(self, subject: str) -> tuple[dict[str, Any], str | None]:
        rows = await self.fetch(
            "SELECT name, body FROM target_descriptors WHERE descriptor_id = $1 AND is_head",
            subject)
        if not rows:
            return {}, None
        return _body(rows[0]["body"]), rows[0]["name"]

    # -- scope_and_freeze ------------------------------------------------------

    async def _scope_and_freeze(self, call: SeatCall) -> dict[str, Any]:
        from ..nats import SIGNALS_EXCLUDE_BACKFILL_SQL
        from ..provenance.origin import origin_class_clause
        from ..research_flag import RESEARCH_SLICE_EXCLUSION_SQL, research_reaches_desks
        from ..signal_slice_scope import (
            CANONICAL_ONLY_SQL,
            SLICE_TEXT_SQL,
            desk_geo_sql,
            homonym_slice_filter,
            target_tag_filter,
        )
        from ..slice_coverage import SliceWindow, row_score, select_by_salience, window_clauses

        subject = str(_input(call, "subject") or "")
        as_of = _as_dt(_input(call, "as_of") or self.as_of)
        if as_of != self.as_of:
            raise SeatFailure("as_of input differs from the run's cut")
        body, _name = await self._target(subject)
        if not body:
            raise SeatFailure(f"subject {subject!r} is not a target head")
        scope = body.get("scope") if isinstance(body.get("scope"), Mapping) else {}
        source_ids = [s.get("source_id") for s in body.get("sources") or ()
                      if isinstance(s, Mapping) and s.get("source_id")]
        geo = [g for g in scope.get("geo") or () if g]
        params: list[Any] = []
        window = SliceWindow(start=as_of - timedelta(hours=self.window_hours), end=as_of,
                             basis="as_of_cut")
        clauses = [*window_clauses(window, params)]
        params.append(as_of)
        clauses.append(f"fetched_at < ${len(params)}::timestamptz")
        clauses.append(f"created_at < ${len(params)}::timestamptz")  # the record-time cut
        clauses += [SIGNALS_EXCLUDE_BACKFILL_SQL, CANONICAL_ONLY_SQL, origin_class_clause("")]
        if not research_reaches_desks():
            clauses.append(RESEARCH_SLICE_EXCLUSION_SQL)
        if source_ids:
            params.append(source_ids)
            clauses.append(f"source_id = ANY(${len(params)})")
        clauses.extend(target_tag_filter(scope).sql(params))
        if geo:
            params.append(geo)
            clauses.append(f"({desk_geo_sql(len(params))})")
            clauses.extend(homonym_slice_filter(geo).sql(params, admit_unfiled=True))
        fetch_limit = max(200, self.row_cap * 3)
        params.append(fetch_limit)
        sql = ("SELECT id, source_id, fetched_at, created_at, salience, access_class, "
               "payload->>'title' AS title, payload->>'title_en' AS title_en, "
               "payload->>'origin_group_id' AS origin_group_id, "
               "payload->>'published_at' AS published_at, payload->'geo_fallback' AS geo_fallback, "
               f"{SLICE_TEXT_SQL} AS text FROM signals WHERE " + " AND ".join(clauses)
               + f" ORDER BY fetched_at DESC LIMIT ${len(params)}")
        records = [dict(r) for r in await self.fetch(sql, *params)]
        for r in records:
            r["salience"] = _body(r.get("salience"))
            r["payload"] = {"geo_fallback": bool(r.get("geo_fallback"))}
        chosen = select_by_salience(records, limit=self.row_cap, per_source_cap=self.per_source_cap)
        chosen.sort(key=lambda r: (row_score(r), _iso(r.get("fetched_at")) or ""), reverse=True)
        rows = []
        for i, r in enumerate(chosen):
            text = readable_text(r.get("text"))[:ROW_TEXT_CHARS]
            row: dict[str, Any] = {"id": str(r["id"]), "kind": "signal", "text": text or None,
                                   "title": r.get("title_en") or r.get("title"),
                                   "source_id": r.get("source_id"),
                                   "origin_group_id": r.get("origin_group_id"),
                                   "access_class": r.get("access_class"),
                                   "published_at": r.get("published_at") or _iso(r.get("fetched_at")),
                                   "required": i < self.required}
            rows.append({k: v for k, v in row.items() if v is not None})
        return {"subject": subject, "as_of": as_of.isoformat(), "rows": rows}

    # -- core_packet_and_slices ------------------------------------------------

    async def _core_packet_and_slices(self, call: SeatCall) -> dict[str, Any]:
        from ..signal_slice_scope import strict_per_source_cap

        scope = _output(call, "frozen_scope/1")
        if scope is None:
            raise SeatFailure("core_packet_and_slices needs the frozen scope")
        rows = [dict(r) for r in scope.get("rows") or ()]  # salience order (scope_and_freeze)
        for r in rows:
            r.setdefault("source_id", r.get("id"))
        core = strict_per_source_cap(rows, per_source_cap=self.per_source_cap, limit=self.core_rows)
        core_ids = [r["id"] for r in core]
        rest = [r["id"] for r in rows if r["id"] not in set(core_ids)]
        out_rows = [{k: v for k, v in r.items()} for r in rows]
        for r in out_rows:
            if r.get("source_id") == r.get("id"):
                r.pop("source_id")
        return {"core": core_ids, "slices": {"remainder": rest} if rest else {}, "rows": out_rows}

    # -- series_panel ----------------------------------------------------------

    async def _series_panel(self, call: SeatCall) -> dict[str, Any]:
        scope = _output(call, "frozen_scope/1")
        subject = str((scope or {}).get("subject") or _input(call, "subject") or "")
        body, _ = await self._target(subject)
        geo = [g for g in ((body.get("scope") or {}).get("geo") or ()) if g]
        subjects = geo + [subject]
        records = await self.fetch(
            "SELECT id, series_id, subject, unit, valid_from, value FROM ("
            " SELECT id, series_id, subject, unit, valid_from, value,"
            "  row_number() OVER (PARTITION BY series_id, subject ORDER BY valid_from DESC,"
            "   record_time DESC) AS rn"
            " FROM observations"
            " WHERE subject = ANY($1::text[]) AND value IS NOT NULL"
            "  AND record_time < $2::timestamptz AND loaded_at < $2::timestamptz"
            "  AND valid_from >= ($2::timestamptz - interval '15 years')::date"
            "  AND valid_from <= $2::timestamptz::date"
            ") s WHERE rn <= $3 ORDER BY series_id, subject, valid_from",
            subjects, self.as_of, MAX_POINTS)
        series: dict[tuple[str, str], dict[str, Any]] = {}
        for r in records:
            key = (str(r["series_id"]), str(r["subject"]))
            s = series.setdefault(key, {"key": f"{key[0]}:{key[1]}",
                                        "label": f"{key[0]} ({key[1]}, {r['unit']})",
                                        "values": [], "rows": []})
            s["values"].append(float(r["value"]))
            s["rows"].append(f"{r['id']}")
        ordered = sorted(series.values(), key=lambda s: -len(s["values"]))[:MAX_SERIES]
        return {"series": ordered}

    # -- coverage_audit --------------------------------------------------------

    async def _coverage_audit(self, call: SeatCall) -> dict[str, Any]:
        if self.dispatch is None:
            raise SeatFailure("coverage_audit needs the source-fleet read (dispatch)")
        scope = _output(call, "frozen_scope/1")
        subject = str((scope or {}).get("subject") or _input(call, "subject") or "")
        body, _ = await self._target(subject)
        wanted = {s.get("source_id") for s in body.get("sources") or ()
                  if isinstance(s, Mapping) and s.get("source_id")}
        if not wanted and scope is not None:
            wanted = {r.get("source_id") for r in scope.get("rows") or () if r.get("source_id")}
        out = await self.dispatch("list_sources", {"active_only": True}, self.as_of)
        rows = (out or {}).get("rows") or [] if isinstance(out, Mapping) else []
        absences = []
        for r in rows:
            sid = r.get("source_id")
            if wanted and sid not in wanted:
                continue
            state, outcome = r.get("staleness"), r.get("last_poll_outcome")
            if state in AUDIT_STATES or outcome == "error":
                absences.append({"kind": f"source_{state if state in AUDIT_STATES else 'erroring'}",
                                 "what": str(sid),
                                 "detail": f"last signal {r.get('last_signal_at')}; "
                                           f"last poll {outcome or 'never'}"})
        return {"absences": absences}

    # -- statement_gate_shadow -------------------------------------------------

    async def _statement_gate_shadow(self, call: SeatCall) -> dict[str, Any]:
        """The ruling's sentences through the S-5 gates in shadow
        (:mod:`legba.data.rounds.shadow_gate`): split, subject (desk, the
        countries it spans, the actors linked to the cited rows), outcome."""
        from ..signal_slice_scope import SLICE_TEXT_SQL
        from ..statement_actor_subjects import LINKED_ENTITIES_SQL, linked_from_rows
        from .shadow_gate import gate_sentence, handed_slice, offered_subjects

        ruling = _output(call, "ruling/1")
        if ruling is None:
            raise SeatFailure("statement_gate_shadow needs the ruling")
        tally = _output(call, "tally/1") or {}
        sentences = [s for s in sentences_of(ruling) if s in (ruling.get("sentences") or [])]
        cited = sorted({str(r.get("ref_id")) for s in sentences for r in s.get("support") or ()
                        if r.get("kind", "signal") == "signal"})
        rows: dict[str, Mapping[str, Any]] = {}
        uuids = [c for c in cited if _is_uuid(c)]
        linked: list[Any] = []
        if uuids:
            for r in await self.fetch(
                    "SELECT id, source_id, access_class, payload->>'title' AS title, "
                    "payload->>'published_at' AS published_at, "
                    f"{SLICE_TEXT_SQL} AS text FROM signals "
                    "WHERE id = ANY($1::uuid[]) AND created_at < $2::timestamptz",
                    uuids, self.as_of):
                rows[str(r["id"])] = dict(r)
            linked = linked_from_rows(dict(r) for r in await self.fetch(LINKED_ENTITIES_SQL, uuids))
        subject = str(_input(call, "subject") or self.subject or "")
        offered = offered_subjects(subject)
        handed = handed_slice(rows.keys(), call.run_id, subject=subject, as_of=self.as_of,
                              window_hours=self.window_hours,
                              source_ids={str(r.get("source_id")) for r in rows.values() if r.get("source_id")})
        proposals = []
        for s in sentences:
            proposals.extend(gate_sentence(s, subject=subject, offered=offered, rows=rows,
                                           linked=linked, handed=handed,
                                           verifier=tally.get("outcome")))
        return {"proposals": proposals}


def _is_uuid(v: str) -> bool:
    import uuid

    try:
        uuid.UUID(v)
        return True
    except ValueError:
        return False


__all__ = ["SubstrateSeats"]
