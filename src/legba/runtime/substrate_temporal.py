# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The V3/P3 temporal reader leaf — as-of predicates, ISO-8601 parsing, and
the honest counters that go with them (DATA MODEL V3 §3).

The substrate's validity-time tables (``facts``, ``nexuses``, ``entity_edges``,
``situations``) all carry the same two clock columns and the same two reader
rules, and this module is the ONE place those rules are written down as code:

* **The open-row predicate** — ``valid_until IS NULL AND superseded_by IS
  NULL`` — is what every current reader applies when no ``as_of`` is supplied.
  It MUST NOT be applied when one is: a row that has since been superseded
  was *the* answer on date D, and excluding it is exactly the error the whole
  feature exists to fix (§3.2). ``valid_until`` alone carries the close, so
  ``superseded_by`` drops out of the filter whenever ``as_of`` is given.
* **The canonical as-of predicate** is
  ``COALESCE(valid_from, '-infinity') <= D AND (valid_until IS NULL OR valid_until > D)``.
  ``valid_from`` is frequently NULL, and the honest handling is to
  over-include (an unbounded start "held for all time up to valid_until") and
  say so: readers return ``unbounded_start: N`` beside the row count so a
  caller can see how much of the answer rests on a start date nobody recorded
  — the ``insufficient-evidence`` idiom applied to time.

The second clock — **decision time** — is a different question and a different
parameter name (§3.2): ``believed_as_of`` reads ``produced_at`` /
``superseded_at`` on ``analyst_outputs`` and answers "what did Legba believe
on D", which :func:`decision_predicate` builds. The two predicates are never
interchangeable.

V3/P7 — the as-of RENDERING itself moved to
:mod:`legba.data.provenance.origin` (:func:`as_of_gate_sql`), the one home
for the validity predicate and the new origin-class leg; this module
delegates to it so every reader shares one string. The open-row form here
stays the migration-0032 pair: it is only ever invoked for tables WITHOUT an
``origin_class`` column (``nexuses`` / ``entity_edges`` / ``situations``) —
``facts`` reads take :func:`live_gate_sql` at the call site instead.

Everything here is pure: no I/O, so the runtime port, the registry routes
and the agency coercion layer all share one definition of "as of". Parsing
REFUSES LOUD on a malformed value — a bad date raises
:class:`TemporalParameterError` and is never coerced to ``now()``, which would
answer a different question and say nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ..data.provenance import origin as _origin

#: Deploy marker — the rolling deploy greps for this exact string.
TEMPORAL_READER_VERSION = "2026-09/p3"

__all__ = [
    "TEMPORAL_READER_VERSION",
    "TemporalParameterError",
    "parse_instant",
    "temporal_predicate",
    "decision_predicate",
    "anchor_window_clause",
    "unbounded_start",
]


class TemporalParameterError(ValueError):
    """A temporal parameter that cannot be honored. Never defaults silently."""


def parse_instant(value: Any, *, name: str) -> datetime:
    """Parse an ISO-8601 ``value`` into an aware UTC ``datetime``, or refuse.

    Accepts a ``datetime`` (naive is read as UTC) or an ISO-8601 string —
    ``2026-08-15``, ``2026-08-15T00:00:00Z``, ``+HH:MM`` offsets all parse.
    A naive ISO string is read as UTC, which is the only honest reading of a
    timezone-free instant this platform stores. Anything else — a non-string,
    an empty string, ``"last tuesday"``, a bare year — raises
    :class:`TemporalParameterError` so the caller can surface the refusal
    rather than answer a different question.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            # datetime.fromisoformat accepts 'Z' on 3.11+; keep one explicit
            # fallback for the shapes it still rejects (e.g. a trailing 'z').
            try:
                dt = datetime.fromisoformat(raw.upper().replace("Z", "+00:00"))
            except ValueError as exc:
                raise TemporalParameterError(
                    f"{name}: cannot parse {value!r} as ISO-8601 — refusing "
                    "rather than guessing a date"
                ) from exc
    else:
        raise TemporalParameterError(
            f"{name}: expected an ISO-8601 string, got {value!r}"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def temporal_predicate(alias: str, as_of_param: int | None) -> str:
    """The WHERE fragment for one validity-time table reference.

    ``as_of_param=None`` returns the open-row predicate — byte-identical to
    what today's readers carry inline. An integer returns the canonical
    as-of predicate bound to ``$<as_of_param>``; the caller appends the parsed
    instant to its params at exactly that position. The as-of form carries
    NO ``superseded_by`` clause on purpose: ``valid_until`` alone carries the
    close (§3.2's second rule).
    """
    col = f"{alias}." if alias else ""
    if as_of_param is None:
        # The migration-0032 open-row pair — for tables that carry NO
        # ``origin_class`` column (nexuses / entity_edges / situations).
        # Origin-classed tables take ``origin.live_gate_sql`` at the call
        # site, which adds the class leg to this same pair.
        return f"{col}valid_until IS NULL AND {col}superseded_by IS NULL"
    # V3/P7 — the one rendering of the validity predicate lives in
    # provenance.origin; every as-of read shares it through this delegation.
    return _origin.as_of_gate_sql(alias, as_of_param)


def decision_predicate(alias: str, param: int) -> str:
    """The WHERE fragment for a *decision-time* read on ``analyst_outputs``.

    ``analyst_outputs`` has no ``valid_*`` columns (§3.1's first correction):
    its validity interval is ``[produced_at, superseded_at)``. This is the
    predicate ``believed_as_of`` uses — the row Legba had published and not
    yet superseded on date D. Not interchangeable with
    :func:`temporal_predicate`; the parameter names are deliberately
    different so the two clocks are never confused.
    """
    col = f"{alias}." if alias else ""
    return (
        f"{col}produced_at <= ${param}"
        f" AND ({col}superseded_at IS NULL OR {col}superseded_at > ${param})"
    )


def anchor_window_clause(
    anchor_expr: str, *, since_param: int | None, until_param: int | None
) -> str:
    """A half-open ``[since, until)`` WHERE fragment on a temporal anchor.

    ``get_timeline`` filters each stream on its anchor EXPRESSION (a fact on
    ``COALESCE(valid_from, produced_at, created_at)``, a signal on
    ``COALESCE(fetched_at, created_at)``), so the caller supplies the
    expression and this builds the bounds. Half-open matches the validity-
    interval convention the rest of the substrate uses. Returns ``""`` when
    neither bound is given — the caller's SQL is then byte-identical to
    today's.
    """
    parts: list[str] = []
    if since_param is not None:
        parts.append(f"{anchor_expr} >= ${since_param}")
    if until_param is not None:
        parts.append(f"{anchor_expr} < ${until_param}")
    return " AND ".join(parts)


def unbounded_start(
    rows: Iterable[Mapping[str, Any]], *, key: str = "valid_from"
) -> int:
    """Count result rows whose validity start was never recorded.

    This is the number the as-of contract says to publish: a NULL
    ``valid_from`` over-includes by construction (``-infinity``), so the
    reader is told how many of its rows rest on a start date nobody recorded.
    Accepts the row dicts a reader already built — ``None`` counts, an
    ISO string or a datetime does not.
    """
    return sum(1 for r in rows if r.get(key) is None)
