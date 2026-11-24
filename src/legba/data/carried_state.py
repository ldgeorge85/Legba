# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CARRIED STATE EXPIRES (W11-F14) — a status nobody re-evidences stops reading as a status.

The defect, verified 2026-10-08: the semiconductor-flow unit read "degraded,
held by border seizures" in 62 of 63 September and 16 of 16 October findings.
The last read whose own indicators saw anything (a seizure, 24 September) was
two weeks old; every read since had all six indicators ``not_observed`` and
re-stated the status off its PRIOR READ block. Two mechanisms kept it alive:
the unit's own carry (its prior read is in its prompt, and "remains unchanged"
is the cheapest sentence to write), and the composition's FRAME-1 floor
fallback, which promotes the newest SUPERSEDED read that cleared the floor
however old it is.

THE RULE. A unit read that

  * declares indicators (``data.indicators`` is a list — the unit's own
    statement of what evidence it looked for; a read without one is NOT
    MEASURED here, never passed and never failed),
  * states a status (its standing severity level is set and is not ``low`` —
    the quiet level a collection-scoped "not observed" read takes), and
  * does not evidence that status itself (no indicator reads ``triggered``,
    and no LEVEL-shaped indicator reads ``observed_at_level`` on a citation —
    X4 LEVEL, W11-F36, :mod:`legba.data.indicator_shape`: a standing condition
    measured against a declared reference is re-evidenced by a report in the
    run's own window, which a change-shaped signpost never is),

is **carried, unsupported** when the newest read of the same unit on the same
subject whose indicators DID re-evidence it is older than N of the unit's own
cadence windows (N = :data:`CARRY_EXPIRY_WINDOWS_ENV`, default 3; the window
is the unit's ``cadence.cooldown_seconds``, the same cadence the stale-read
mark uses), or when no such read exists inside
:data:`EVIDENCE_LOOKBACK_DAYS`. A unit with no declared cooldown is not
measured. The verdict is a TYPED ABSENCE (``carried_unsupported``), never a
band: its proof names the last evidencing read and its age.

A CHANGE REPORT IS ITS OWN RE-EVIDENCE (roll 65). A desk whose indicators are
all change-shaped tripwires and that declares no level-shaped one can only be
re-evidenced by a tripwire firing — on the top-job desk that happened 9 times
in 885 reads over the 14 days to 2026-11-08, so a read reporting a chief of
staff replaced (US 3824a07b) or a government-formation mandate handed to the
opposition (IL 48967698) was dropped against a trigger 10-14 days old. So a
read is NOT convicted when, on top of the rule above,

  * its verdict REPORTS A CHANGE — the read's headline or its BLUF passes
    :func:`legba.data.change_report.text_reports_change`, the world tier's
    restraint-shift test (a verdict that opens on stasis — *"a change … remains
    unlikely"* — does not, and keeps the rule), and
  * it cites at least :data:`CHANGE_SELF_EVIDENCE_MIN_SIGNALS` distinct wire
    items fetched inside its own re-evidence window (the N cadence windows
    before the read was produced), and the read itself is inside that window.

The read is its OWN re-evidence only: :data:`LAST_EVIDENCE_SQL` still counts
indicator evidence alone, so a later stasis read is measured against the last
indicator that saw something, never against an earlier change report.

Two producers, one rule (both call :func:`carry_verdict` /
:func:`fallback_verdict` and the one :data:`LAST_EVIDENCE_SQL`):

  * the composition (``meta_findings_synthesizer`` READ_SLICE stamps
    :data:`CARRIED_ROW_KEY`; ``_run`` lifts stamped heads out of the basis and
    the coverage ledger names them), and the FRAME-1 floor fallback, which
    refuses to promote a superseded read older than N windows
    (``composition_window.select_floor_fallback``), and — the same bar —
    every LIVE basis head older than N windows (:func:`stamp_head_age_bar`,
    cause ``head_too_old``: withheld, never quoted as current);
  * the typed-absence route (``GET /v3/absence?scope=``), for the unit read on
    its own desk — which is how a flow or lane unit that no composition
    consumes still gets the absence.

Stdlib only: the registry's slim image imports this module through the
absence route, and ``tests/data_pkg/test_absence_api.py`` pins that nothing
under ``legba.data.analysts`` or ``legba.runtime`` comes with it.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .change_report import text_reports_change
from .indicator_shape import RE_EVIDENCE_JSONPATH, re_evidences

logger = logging.getLogger(__name__)

#: THE TUNABLE (docs/TUNABLES.md §3). A status no read re-evidenced inside this
#: many of the unit's own cadence windows is carried, unsupported.
CARRY_EXPIRY_WINDOWS_ENV = "LEGBA_CARRY_EXPIRY_WINDOWS"
CARRY_EXPIRY_WINDOWS_DEFAULT = 3.0

#: The typed-absence kind (``absence_api.ABSENCE_KINDS``) and coverage status.
CARRIED_UNSUPPORTED = "carried_unsupported"

#: Row key a verdict rides on, READ_SLICE → the DB-less ``_run``.
CARRIED_ROW_KEY = "_carried_unsupported"

#: The row key ``composition_stale_reads.stamp_unit_cadence`` writes (hours).
#: Spelled here because this module may not import ``legba.data.analysts``; a
#: test pins the two equal.
UNIT_CADENCE_ROW_KEY = "_unit_cadence_h"

#: How far back the last-evidence read looks. Bounded so the query is bounded;
#: "none in 90 days" is said in those words on the proof.
EVIDENCE_LOOKBACK_DAYS = 90

#: A change-reporting read is its own re-evidence when it cites at least this
#: many DISTINCT wire items fetched inside its re-evidence window. Three, from
#: the 14 days to 2026-11-08 (2,516 moderate-or-higher desk reads whose
#: indicators evidenced nothing): the four political change reads the rule wrongly dropped cite 12-16
#: such items; the 22 top-job reads whose verdict passes the change test cite
#: 3-25 (minimum 3); and one or two fresh items is what a stasis read restating
#: an old status picks up in passing (268 of 784 energy-desk reads, 77 of 466
#: top-job reads cite fewer than three). Two would let a single wire story and
#: its syndicated copy carry a status; three asks for corroboration.
CHANGE_SELF_EVIDENCE_MIN_SIGNALS = 3

#: Why a verdict fired.
CAUSE_NOT_RE_EVIDENCED = "status_not_re_evidenced"
CAUSE_FALLBACK_TOO_OLD = "fallback_too_old"
#: HEAD INTEGRITY (lane N-A): the same age bar on a LIVE basis head. Before it,
#: the bar ran only on superseded fallbacks, so an orphan head nobody superseded
#: (CD escalation bb2ce5fc, 184 h) entered the basis as an ordinary head with
#: ``stale: true`` gating nothing.
CAUSE_HEAD_TOO_OLD = "head_too_old"

TRIGGERED = "triggered"
_QUIET_LEVELS = frozenset({"low"})
_LEVELS = frozenset({"low", "moderate", "elevated", "high", "critical"})

#: The newest read per (unit, subject) whose own indicators re-evidenced its
#: status (:data:`legba.data.indicator_shape.RE_EVIDENCE_JSONPATH`: a
#: ``triggered`` indicator, or a level one ``observed_at_level`` on a citation).
#: One statement for every pair; the pair list is unnested so the plan walks the
#: ``target_id`` index once per subject rather than crossing two ANY lists.
LAST_EVIDENCE_SQL = f"""
SELECT DISTINCT ON (f.analyst_id, f.target_id)
       f.analyst_id, f.target_id, f.id::text AS id, f.produced_at
  FROM public.analyst_outputs f
  JOIN unnest($1::text[], $2::text[]) AS k(analyst_id, target_id)
    ON f.analyst_id = k.analyst_id AND f.target_id = k.target_id
 WHERE f.kind = 'finding'
   AND f.produced_at > $3
   AND jsonb_path_exists(f.data, '{RE_EVIDENCE_JSONPATH}')
 ORDER BY f.analyst_id, f.target_id, f.produced_at DESC, f.id DESC
"""

#: The unit heads of one desk for the absence route: the newest read per unit,
#: with what the rule reads off it, and the unit's own cooldown.
UNIT_HEADS_SQL = f"""
SELECT f.analyst_id, f.target_id, f.id::text AS id, f.produced_at, f.severity,
       f.data -> 'tags' AS tags,
       jsonb_typeof(f.data -> 'data' -> 'indicators') = 'array' AS has_indicators,
       jsonb_path_exists(f.data, '{RE_EVIDENCE_JSONPATH}') AS evidenced,
       f.data ->> 'title' AS title,
       f.data ->> 'body' AS body,
       jsonb_path_query_array(f.data, '$.data.citations[*].signal_id') AS cited_signal_ids,
       (SELECT (d.body -> 'cadence' ->> 'cooldown_seconds')::bigint
          FROM public.analyst_descriptors d
         WHERE d.is_head AND d.descriptor_id = f.analyst_id) AS cooldown_s
  FROM (SELECT DISTINCT ON (o.analyst_id) o.id
          FROM public.analyst_outputs o
         WHERE o.kind = 'finding'
           AND o.target_id = $1
           AND o.analyst_id = ANY($2::text[])
           AND (o.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE
         ORDER BY o.analyst_id, o.produced_at DESC, o.id DESC) h
  JOIN public.analyst_outputs f ON f.id = h.id
"""


#: When each cited wire item was fetched — the change report's freshness test.
SIGNAL_FETCHED_SQL = """
SELECT s.id::text AS id, s.fetched_at
  FROM public.signals s
 WHERE s.id = ANY($1::uuid[])
"""


def carry_expiry_windows() -> float:
    """N, from the env; a missing, unparsable or non-positive value is the
    default (a bad knob never disables the rule)."""
    raw = os.getenv(CARRY_EXPIRY_WINDOWS_ENV)
    if raw is None or not raw.strip():
        return CARRY_EXPIRY_WINDOWS_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return CARRY_EXPIRY_WINDOWS_DEFAULT
    return value if value > 0 else CARRY_EXPIRY_WINDOWS_DEFAULT


# ---------------------------------------------------------------------------
# Reading a row
# ---------------------------------------------------------------------------


def _envelope(row: Mapping[str, Any]) -> Mapping[str, Any]:
    env = row.get("data")
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except (ValueError, TypeError):
            return {}
    return env if isinstance(env, Mapping) else {}


def _indicators(row: Mapping[str, Any]) -> list[Any] | None:
    """The read's indicator list, or ``None`` when it declares none.

    The finding envelope nests the payload's data at ``data -> 'data'``; a
    route row may carry the computed ``has_indicators`` instead.
    """
    inner = _envelope(row).get("data")
    if isinstance(inner, Mapping) and isinstance(inner.get("indicators"), list):
        return list(inner["indicators"])
    return None


def has_indicators(row: Mapping[str, Any]) -> bool:
    if isinstance(row.get("has_indicators"), bool):
        return bool(row["has_indicators"])
    return _indicators(row) is not None


def evidences_status(row: Mapping[str, Any]) -> bool:
    """True when at least one of the read's own indicators re-evidences its
    status: one ``triggered``, or a level-shaped one ``observed_at_level`` on a
    citation (``indicator_shape.re_evidences`` — the SQL predicate's twin)."""
    if isinstance(row.get("evidenced"), bool):
        return bool(row["evidenced"])
    return any(re_evidences(ind) for ind in _indicators(row) or [])


def status_level(row: Mapping[str, Any]) -> str | None:
    """The read's standing severity level: the column, else the LAST
    ``severity:<level>`` tag (the analyst contract emits one)."""
    col = str(row.get("severity") or "").strip().lower()
    if col in _LEVELS:
        return col
    tags = row.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (ValueError, TypeError):
            tags = None
    if tags is None:
        tags = _envelope(row).get("tags")
    level: str | None = None
    for tag in tags if isinstance(tags, (list, tuple)) else ():
        if isinstance(tag, str) and tag.startswith("severity:"):
            candidate = tag.split(":", 1)[1].strip().lower()
            if candidate in _LEVELS:
                level = candidate
    return level


def carries_status(row: Mapping[str, Any]) -> bool:
    level = status_level(row)
    return level is not None and level not in _QUIET_LEVELS


_BLUF_RE = re.compile(r"\*\*\s*BLUF\s*:?\s*\*\*\s*:?\s*(?P<text>[^\n]+)", re.IGNORECASE)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _text_field(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        value = _envelope(row).get(key)
    return value if isinstance(value, str) else ""


def verdict_texts(row: Mapping[str, Any]) -> tuple[str, str]:
    """``(headline, bluf)`` — the read's two verdict surfaces. The BLUF is the
    line after the ``**BLUF:**`` label the desk contract writes; ``""`` when
    the body carries none."""
    m = _BLUF_RE.search(_text_field(row, "body"))
    return _text_field(row, "title").strip(), (m.group("text").strip() if m else "")


def reports_change(row: Mapping[str, Any]) -> bool:
    """Does the read's verdict REPORT A CHANGE? Its headline or its BLUF passes
    :func:`legba.data.change_report.text_reports_change`. Total."""
    return any(text_reports_change(t) for t in verdict_texts(row) if t)


def cited_signal_ids(row: Mapping[str, Any]) -> list[str]:
    """The distinct wire-item ids the read cites (``data.citations[].signal_id``,
    or the route's computed ``cited_signal_ids``), well-formed ids only."""
    raw = row.get("cited_signal_ids")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = None
    if not isinstance(raw, list):
        inner = _envelope(row).get("data")
        cits = inner.get("citations") if isinstance(inner, Mapping) else None
        raw = [c.get("signal_id") for c in cits if isinstance(c, Mapping)] if isinstance(cits, list) else []
    out: list[str] = []
    for sid in raw:
        key = str(sid or "").strip()
        if _UUID_RE.match(key) and key not in out:
            out.append(key)
    return out


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _hours(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    return max(0.0, (now - then).total_seconds() / 3600.0)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ---------------------------------------------------------------------------
# The verdicts — pure
# ---------------------------------------------------------------------------


def _verdict(
    row: Mapping[str, Any],
    *,
    cause: str,
    cadence_h: float,
    windows: float,
    now: datetime,
    last_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    head_at = _as_dt(row.get("produced_at"))
    ev_at = _as_dt((last_evidence or {}).get("produced_at"))
    head_age = _hours(now, head_at)
    ev_age = _hours(now, ev_at)
    return {
        "kind": CARRIED_UNSUPPORTED,
        "cause": cause,
        "unit": str(row.get("analyst_id") or ""),
        "target_id": (str(row["target_id"]) if row.get("target_id") is not None else None),
        "status": status_level(row),
        "head_id": (str(row["id"]) if row.get("id") is not None else None),
        "head_produced_at": head_at.isoformat() if head_at else None,
        "head_age_h": None if head_age is None else round(head_age, 2),
        "last_evidence_id": (
            str(last_evidence["id"]) if last_evidence and last_evidence.get("id") else None
        ),
        "last_evidence_at": ev_at.isoformat() if ev_at else None,
        "last_evidence_age_h": None if ev_age is None else round(ev_age, 2),
        "lookback_days": EVIDENCE_LOOKBACK_DAYS,
        "cadence_h": round(cadence_h, 3),
        "windows": windows,
        "limit_h": round(cadence_h * windows, 2),
    }


def change_self_evidence(
    row: Mapping[str, Any],
    *,
    signal_fetched_at: Mapping[str, Any],
    cadence_h: float | None,
    now: datetime,
    windows: float | None = None,
) -> dict[str, Any] | None:
    """The change report's proof, or ``None`` when the read is not its own
    re-evidence: its verdict reports a change (:func:`reports_change`), it is
    itself inside N windows of ``now``, and it cites at least
    :data:`CHANGE_SELF_EVIDENCE_MIN_SIGNALS` distinct wire items fetched inside
    the N windows before it was produced (``signal_fetched_at``: id -> fetch
    time; an id it lacks is not counted)."""
    if cadence_h is None or cadence_h <= 0:
        return None
    k = carry_expiry_windows() if windows is None else float(windows)
    limit_h = cadence_h * k
    head_at = _as_dt(row.get("produced_at"))
    head_age = _hours(now, head_at)
    if head_at is None or head_age is None or head_age > limit_h:
        return None
    if not reports_change(row):
        return None
    floor = head_at - timedelta(hours=limit_h)
    fresh = [
        sid for sid in cited_signal_ids(row)
        if (at := _as_dt(signal_fetched_at.get(sid))) is not None and floor <= at <= head_at
    ]
    if len(fresh) < CHANGE_SELF_EVIDENCE_MIN_SIGNALS:
        return None
    return {
        "re_evidence": "change_reported",
        "fresh_signals": len(fresh),
        "min_signals": CHANGE_SELF_EVIDENCE_MIN_SIGNALS,
        "limit_h": round(limit_h, 2),
    }


def carry_verdict(
    row: Mapping[str, Any],
    *,
    last_evidence: Mapping[str, Any] | None,
    cadence_h: float | None,
    now: datetime,
    windows: float | None = None,
    signal_fetched_at: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The carry rule for one unit read; ``None`` = not carried (or not measured).

    ``last_evidence`` is the newest read of the same (unit, subject) whose
    indicators triggered, inside the lookback, or ``None`` when there is none.
    ``signal_fetched_at`` (id -> fetch time of the wire items the read cites)
    lets a change-reporting read be its own re-evidence
    (:func:`change_self_evidence`); ``None`` = not consulted.
    """
    if not has_indicators(row) or not carries_status(row) or evidences_status(row):
        return None
    if cadence_h is None or cadence_h <= 0:
        return None
    k = carry_expiry_windows() if windows is None else float(windows)
    ev_age = _hours(now, _as_dt((last_evidence or {}).get("produced_at")))
    if ev_age is not None and ev_age <= cadence_h * k:
        return None
    if signal_fetched_at is not None and change_self_evidence(
        row, signal_fetched_at=signal_fetched_at, cadence_h=cadence_h, now=now, windows=k,
    ) is not None:
        return None
    return _verdict(
        row, cause=CAUSE_NOT_RE_EVIDENCED, cadence_h=cadence_h, windows=k,
        now=now, last_evidence=last_evidence,
    )


def _age_bar(
    row: Mapping[str, Any], *, cause: str, cadence_h: float | None,
    now: datetime, windows: float | None,
) -> dict[str, Any] | None:
    """THE age bar: a read older than N of its unit's cadence windows."""
    if cadence_h is None or cadence_h <= 0:
        return None
    k = carry_expiry_windows() if windows is None else float(windows)
    age = _hours(now, _as_dt(row.get("produced_at")))
    if age is None or age <= cadence_h * k:
        return None
    return _verdict(
        row, cause=cause, cadence_h=cadence_h, windows=k, now=now, last_evidence=None,
    )


def fallback_verdict(
    row: Mapping[str, Any],
    *,
    cadence_h: float | None,
    now: datetime,
    windows: float | None = None,
) -> dict[str, Any] | None:
    """The floor fallback's age bar: a superseded read older than N windows is
    never promoted. ``None`` = young enough (or no cadence: not measured)."""
    return _age_bar(
        row, cause=CAUSE_FALLBACK_TOO_OLD, cadence_h=cadence_h, now=now, windows=windows,
    )


def head_age_verdict(
    row: Mapping[str, Any],
    *,
    cadence_h: float | None,
    now: datetime,
    windows: float | None = None,
) -> dict[str, Any] | None:
    """The SAME bar on a live basis head: older than N windows, it is withheld
    rather than quoted as current. ``None`` = young enough or not measured."""
    return _age_bar(
        row, cause=CAUSE_HEAD_TOO_OLD, cadence_h=cadence_h, now=now, windows=windows,
    )


def stamp_head_age_bar(
    rows: Sequence[MutableMapping[str, Any]],
    *,
    now: datetime | None = None,
    windows: float | None = None,
) -> int:
    """READ_SLICE side, zero queries: stamp :data:`CARRIED_ROW_KEY` (cause
    :data:`CAUSE_HEAD_TOO_OLD`) on every BASIS head past the age bar, so
    ``partition_carried`` keeps it out of the basis and the coverage ledger names
    it with its date. Periphery, continuity and already-stamped rows are left
    alone, as is a row with no known cadence (not measured). Run AFTER
    :func:`stamp_carried_state`, whose verdict (if any) is kept. Returns the
    number stamped."""
    anchor = now or datetime.now(timezone.utc)
    stamped = 0
    for row in rows:
        if (
            row.get(CARRIED_ROW_KEY)
            or row.get("_continuity")
            or row.get("_evidence_tier") == "periphery"
        ):
            continue
        verdict = head_age_verdict(
            row, cadence_h=_num(row.get(UNIT_CADENCE_ROW_KEY)), now=anchor, windows=windows,
        )
        if verdict is not None:
            row[CARRIED_ROW_KEY] = verdict
            stamped += 1
    if stamped:
        logger.info("carried_state.head_too_old n=%d", stamped)
    return stamped


def verdict_sentence(v: Mapping[str, Any]) -> str:
    """The one human sentence both surfaces print for a verdict."""
    status = v.get("status") or "a status"
    limit = _num(v.get("limit_h"))
    cadence = _num(v.get("cadence_h"))
    bar = (
        f"{v.get('windows'):g} of its {cadence:.0f} h windows ({limit:.0f} h)"
        if limit is not None and cadence is not None else "its expiry bar"
    )
    if v.get("cause") == CAUSE_HEAD_TOO_OLD:
        age = _num(v.get("head_age_h"))
        age_txt = f"{age:.0f} h old" if age is not None else "of unknown age"
        return (
            f"the desk's newest verified read is {age_txt}, older than {bar}; "
            "it is withheld, never quoted as current"
        )
    if v.get("cause") == CAUSE_FALLBACK_TOO_OLD:
        age = _num(v.get("head_age_h"))
        age_txt = f"{age:.0f} h old" if age is not None else "of unknown age"
        return (
            f"the newest read that cleared the floor is {age_txt}, older than {bar}; "
            "it is not promoted in place of the newer read that failed"
        )
    ev_age = _num(v.get("last_evidence_age_h"))
    since = (
        f"the last read whose indicators re-evidenced it is {ev_age:.0f} h old"
        if ev_age is not None
        else f"no read in the last {v.get('lookback_days')} days had an indicator "
        "trigger or observe its level"
    )
    return (
        f"the latest read still states '{status}' with no indicator triggered or "
        f"observed at its level, and {since} — past {bar}; the status is carried, "
        "not re-evidenced"
    )


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


async def read_last_evidence(
    conn: Any,
    keys: Iterable[tuple[str, str]],
    *,
    now: datetime,
) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(analyst_id, target_id): {id, produced_at}}`` — one statement."""
    pairs = sorted({(str(a), str(t)) for a, t in keys if a and t})
    if not pairs:
        return {}
    rows = await conn.fetch(
        LAST_EVIDENCE_SQL,
        [a for a, _ in pairs],
        [t for _, t in pairs],
        now - timedelta(days=EVIDENCE_LOOKBACK_DAYS),
    )
    return {
        (str(r["analyst_id"]), str(r["target_id"])): {
            "id": r["id"], "produced_at": r["produced_at"],
        }
        for r in rows
    }


async def read_signal_fetched_at(conn: Any, ids: Iterable[str]) -> dict[str, Any]:
    """``{signal_id: fetched_at}`` — one statement; ``{}`` for no ids."""
    keys = sorted({str(i) for i in ids if i and _UUID_RE.match(str(i))})
    if not keys:
        return {}
    rows = await conn.fetch(SIGNAL_FETCHED_SQL, keys)
    return {str(r["id"]): r["fetched_at"] for r in rows}


async def _change_fetch_map(
    conn: Any, rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """The fetch times of the wire items the CHANGE-REPORTING rows cite, for
    :func:`change_self_evidence`. ``None`` when the read failed — the rule then
    convicts exactly as it did before roll 65 (best-effort, never a failed
    compose)."""
    ids = [sid for r in rows if reports_change(r) for sid in cited_signal_ids(r)]
    if not ids:
        return {}
    try:
        return await read_signal_fetched_at(conn, ids)
    except Exception as exc:  # noqa: BLE001 — the rule never fails a compose
        logger.warning("carried_state.signal_fetched_failed err=%s", exc)
        return None


async def stamp_carried_state(
    conn: Any,
    rows: Sequence[MutableMapping[str, Any]],
    *,
    now: datetime | None = None,
    windows: float | None = None,
) -> int:
    """READ_SLICE side: stamp :data:`CARRIED_ROW_KEY` on every BASIS head the
    rule convicts. Reads each row's cadence off :data:`UNIT_CADENCE_ROW_KEY`
    (stamp the cadence first). Zero queries when no row is a candidate;
    best-effort otherwise — a failed read stamps nothing, which leaves every
    head in the basis exactly as before. Returns the number stamped.
    """
    anchor = now or datetime.now(timezone.utc)
    candidates = [
        r for r in rows
        if not r.get(CARRIED_ROW_KEY)
        and not r.get("_continuity")
        and r.get("_evidence_tier") != "periphery"
        and r.get("target_id") is not None
        and has_indicators(r) and carries_status(r) and not evidences_status(r)
        and _num(r.get(UNIT_CADENCE_ROW_KEY)) is not None
    ]
    if not candidates:
        return 0
    try:
        evidence = await read_last_evidence(
            conn,
            [(str(r.get("analyst_id") or ""), str(r["target_id"])) for r in candidates],
            now=anchor,
        )
    except Exception as exc:  # noqa: BLE001 — the rule never fails a compose
        logger.warning("carried_state.last_evidence_failed err=%s", exc)
        return 0
    convicted = []
    for row in candidates:
        verdict = carry_verdict(
            row,
            last_evidence=evidence.get((str(row.get("analyst_id") or ""), str(row["target_id"]))),
            cadence_h=_num(row.get(UNIT_CADENCE_ROW_KEY)),
            now=anchor,
            windows=windows,
        )
        if verdict is not None:
            convicted.append((row, verdict))
    fetched = await _change_fetch_map(conn, (r for r, _ in convicted)) if convicted else {}
    stamped = kept = 0
    for row, verdict in convicted:
        if fetched and change_self_evidence(
            row, signal_fetched_at=fetched, cadence_h=_num(row.get(UNIT_CADENCE_ROW_KEY)),
            now=anchor, windows=windows,
        ) is not None:
            kept += 1
            continue
        row[CARRIED_ROW_KEY] = verdict
        stamped += 1
    if stamped or kept:
        logger.info("carried_state.stamped n=%d change_re_evidenced=%d", stamped, kept)
    return stamped


def partition_carried(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """``(kept, carried)`` — the ``_run`` side: a stamped head leaves the basis."""
    kept = [r for r in rows if not r.get(CARRIED_ROW_KEY)]
    if len(kept) == len(rows):
        return list(rows), []
    return kept, [r for r in rows if r.get(CARRIED_ROW_KEY)]


def empty_slice_text(n_carried: int, window_text: str) -> tuple[str, str]:
    """The composition's empty-slice ``(title, body)`` when the basis emptied
    because its heads were carried — never "no desk read", which would be false:
    the reads exist, and what they state is not evidenced."""
    return (
        "Every desk read is carried, unsupported",
        f"No re-evidenced desk read exists{window_text}: {n_carried} unit head(s) "
        "state a status that none of their indicators has re-evidenced inside N "
        "of their cadence windows (carried, unsupported — named in "
        "data.carried_unsupported). This is an absence of EVIDENCE, not of "
        "reads, and not a reading of calm.",
    )


async def unit_carried_verdicts(
    conn: Any,
    *,
    target: str,
    units: Sequence[str],
    now: datetime,
    windows: float | None = None,
) -> list[dict[str, Any]]:
    """The absence-route side: every unit head on one desk the rule convicts.
    Two statements (the heads; the last evidence for the candidates only)."""
    heads = await conn.fetch(UNIT_HEADS_SQL, target, list(units))
    cands = []
    for h in heads:
        row = dict(h)
        cooldown = row.get("cooldown_s")
        cadence_h = int(cooldown) / 3600.0 if cooldown and int(cooldown) > 0 else None
        if cadence_h is None or not row.get("has_indicators"):
            continue
        if not carries_status(row) or row.get("evidenced"):
            continue
        cands.append((row, cadence_h))
    if not cands:
        return []
    evidence = await read_last_evidence(
        conn, [(str(r["analyst_id"]), target) for r, _ in cands], now=now
    )
    fetched = await _change_fetch_map(conn, (r for r, _ in cands))
    out: list[dict[str, Any]] = []
    for row, cadence_h in cands:
        verdict = carry_verdict(
            row,
            last_evidence=evidence.get((str(row["analyst_id"]), target)),
            cadence_h=cadence_h,
            now=now,
            windows=windows,
            signal_fetched_at=fetched or None,
        )
        if verdict is not None:
            out.append(verdict)
    return out


__all__ = [
    "CARRIED_ROW_KEY",
    "CARRIED_UNSUPPORTED",
    "CARRY_EXPIRY_WINDOWS_DEFAULT",
    "CARRY_EXPIRY_WINDOWS_ENV",
    "CAUSE_FALLBACK_TOO_OLD",
    "CAUSE_HEAD_TOO_OLD",
    "CAUSE_NOT_RE_EVIDENCED",
    "CHANGE_SELF_EVIDENCE_MIN_SIGNALS",
    "EVIDENCE_LOOKBACK_DAYS",
    "LAST_EVIDENCE_SQL",
    "UNIT_CADENCE_ROW_KEY",
    "UNIT_HEADS_SQL",
    "carries_status",
    "carry_expiry_windows",
    "carry_verdict",
    "change_self_evidence",
    "cited_signal_ids",
    "empty_slice_text",
    "evidences_status",
    "fallback_verdict",
    "head_age_verdict",
    "has_indicators",
    "partition_carried",
    "read_last_evidence",
    "read_signal_fetched_at",
    "reports_change",
    "stamp_carried_state",
    "stamp_head_age_bar",
    "status_level",
    "unit_carried_verdicts",
    "verdict_sentence",
    "verdict_texts",
]
