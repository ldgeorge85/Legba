# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ACCESS-CLASS axis — WHO may read a substrate row, in one closed
vocabulary (Wave-E `access_class` lane, 2026-09-24; SEAMS #59).

Orthogonal to the other two provenance axes already in this package:

``origin_class`` (:mod:`legba.data.provenance.origin`)
    WHERE a row came from — live ingestion vs. imported history.
``license_class`` (:mod:`legba.data.schemas.source`, ``SourceScope``)
    What Legba may KEEP/republish of a fetched page (the LIC-2 ledger).
``access_class`` (this module)
    WHO, inside a Legba deploy, may READ a row once it is written. A single
    deploy can carry units or users with very different privileges (a
    commercially-licensed feed a subset of units may see; an operator-only
    ingest), or need them kept apart for research/testing.

THE VOCABULARY — closed, in restriction order (index = rank; a "ceiling"
over several rows is the entry with the HIGHEST rank, i.e. the LEAST
permissive):

``public``
    No restriction — any unit/user with substrate read access.
``licensed_commercial``
    A paid, commercially-licensed feed — read gated to units covered by
    that licence.
``licensed_noncommercial``
    Free-for-noncommercial-use (an academic/NGO dataset, typically) — read
    gated to noncommercial units.
``restricted``
    Reviewed and found sensitive, or simply never classified — the
    fail-closed default when a source's terms could not be determined.
``internal``
    Operator-only.

THIS IS MECHANISM, NOT ENFORCEMENT (SEAMS #59). Every symbol here is real
and exercised: the vocabulary, the per-source scope field
(``SourceScope.access_class``), the ``signals.access_class`` stamp at ingest
(migration 0216), and the opt-in ``access_class_in`` read-side filter the
signals and findings routes accept. Nothing gates a read by caller identity
or unit today, and no reader applies this filter unless a caller passes it —
"accept but never apply by default" is the whole of the mechanism this pass
builds. :func:`access_ceiling_sql` renders the readers' inheritance rule (a
fact/finding/event is as restricted as the most restricted signal it derives
from) for the enforcement side to pick up later; nothing in this tree calls
it yet.

A LEAF: stdlib only — no runtime, no I/O, loadable in the slim image (same
discipline as :mod:`legba.data.provenance.origin`).
"""

from __future__ import annotations

from typing import Iterable

#: Deploy marker — grep target for this exact string.
ACCESS_CLASS_VERSION = "2026-09/waveE"

#: The closed five-class vocabulary, in canonical restriction order (rank
#: ascends with index; used both as the CHECK constraint's IN-list and as
#: the ceiling ranking in :func:`access_ceiling_sql`).
ACCESS_CLASSES: tuple[str, ...] = (
    "public",
    "licensed_commercial",
    "licensed_noncommercial",
    "restricted",
    "internal",
)

#: One-line meaning per class, in the same canonical order.
ACCESS_CLASS_MEANINGS: dict[str, str] = {
    "public": "no restriction — any unit/user with substrate read access",
    "licensed_commercial": (
        "a paid, commercially-licensed feed — read gated to units covered "
        "by that licence"
    ),
    "licensed_noncommercial": (
        "free-for-noncommercial-use — read gated to noncommercial units"
    ),
    "restricted": (
        "reviewed-sensitive, or simply never classified (the fail-closed "
        "default)"
    ),
    "internal": "operator-only",
}

__all__ = [
    "ACCESS_CLASSES",
    "ACCESS_CLASS_MEANINGS",
    "ACCESS_CLASS_VERSION",
    "AccessClassError",
    "access_class_clause",
    "access_ceiling_sql",
]


class AccessClassError(ValueError):
    """An ``access_class`` value outside the closed vocabulary was asked for.

    Raised rather than silently returning zero rows: a mistyped class in an
    ``access_class_in`` parameter must not read as "the substrate is empty".
    """


def access_class_clause(alias: str, classes: Iterable[str]) -> str:
    """A ``<a>.access_class IN (...)`` WHERE fragment over validated classes.

    Unlike :func:`legba.data.provenance.origin.origin_class_clause`, there is
    no default set: this filter is OPT-IN with nothing applied when the
    caller passes nothing (the goal's "accept but never apply by default"),
    so ``classes`` must be a non-empty, caller-supplied iterable — an empty
    selection raises the same as an unknown one, rather than silently
    matching every class or none.
    """
    asked = list(classes)
    unknown = sorted(set(asked) - set(ACCESS_CLASSES))
    if unknown:
        raise AccessClassError(
            f"access_class_in: unknown access_class {unknown} — the closed "
            f"vocabulary is {list(ACCESS_CLASSES)}"
        )
    if not asked:
        raise AccessClassError("access_class_in: no access_class selected")
    selected = [c for c in ACCESS_CLASSES if c in set(asked)]
    col = f"{alias}." if alias else ""
    listed = ",".join(f"'{c}'" for c in selected)
    return f"{col}access_class IN ({listed})"


def access_ceiling_sql(alias: str) -> str:
    """SEAMS #59 — the readers' inheritance rule, rendered but UNWIRED.

    A fact/finding/event is as restricted as the MOST restricted signal it
    derives from. This is a SINGLE-HOP rendering: it intersects
    ``<a>.derived_from`` against ``signals.id`` directly and does not walk a
    multi-level ``analyst_outputs -> analyst_outputs`` derivation chain (a
    higher-tier finding's ``derived_from`` often names other analyst outputs,
    not raw signals — see :mod:`legba.data.provenance.origin`'s own note
    about ``facts.derived_from`` mixing signal and fact lineage). Returns
    ``'public'`` when no entry in ``<a>.derived_from`` is a signal id — there
    is nothing to restrict on, so the ceiling does not invent a restriction.

    Like :func:`legba.data.provenance.origin.live_gate_sql`, one rendering —
    but, deliberately, not called from any reader in this tree yet. The
    enforcement side (SEAMS #59) is what wires it in.
    """
    col = f"{alias}." if alias else ""
    ranked = ",".join(f"'{c}'" for c in ACCESS_CLASSES)
    return (
        f"COALESCE((SELECT s.access_class FROM public.signals s "
        f"WHERE s.id = ANY({col}derived_from) "
        f"ORDER BY array_position(ARRAY[{ranked}]::text[], s.access_class) "
        f"DESC LIMIT 1), 'public')"
    )
