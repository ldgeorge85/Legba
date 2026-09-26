# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE RESEARCH PROGRAM'S ONE FLAG — ``LEGBA_RESEARCH_EVIDENCE``.

``planning/RESEARCH_PROGRAM_SPEC_2026-09-05.md`` §6.2 / §0.12 rules ONE flag
with THREE values for the whole outbound-research program::

    off        the write path is dark. ``web_evidence`` refuses loudly, the
               coverage-floor dispatch writes NO question, and the pre-program
               behaviour of every touched surface is BYTE-IDENTICAL. That is a
               TEST (G2), not a hope.
    substrate  the write path runs in full — signals landed, tagged, archived
               where the licence clears, ceiling applied, counters computable —
               and research is EXCLUDED from every desk's reactive slice.
    desks      the slice exclusion lifts. This is the cutover.

WHY THREE VALUES AND NOT TWO BOOLEANS (§6.2, flagged F-4). The middle rung is
not a build stage, it is the POISONING ROLLBACK: on a bad day the operator
pulls research out of every desk's eyes with one env var while keeping the
evidence, the archive, the counters and the audit trail intact. Two booleans
would admit the incoherent state "excluded from desks but never written",
which means nothing. The cost is a STRING flag where the house style
(``LEGBA_COMPOSITION_ASSEMBLY``, ``assembly_payload.assembly_enabled``) is
boolean 0/1 — the spec flags that trade and takes it deliberately.

WHY THIS MODULE EXISTS RATHER THAN A PRIVATE READER PER TRAIN. Three trains
read the same flag from three packages — R-A's ``web_evidence`` tool
(``data/analysts/agency/``), R-A's slice exclusion predicate
(``runtime/actor_substrate_slice.py``, §1.3b, which the spec names
``research_reaches_desks()``), and R-B's coverage-floor dispatch
(``data/analysts/deterministic_handlers/_research_dispatch.py``). A flag whose
three-way semantics are re-implemented per caller is a flag that will
eventually mean different things in the write path and the read path, which is
precisely the half-flipped state D-5's ruling collapsed a second env var to
avoid (``region_rollup.py:175-177``). One reader, three callers.

UNKNOWN VALUES FAIL SAFE, LOUDLY. An unrecognised value reads as ``off`` and
logs a warning naming the value and the three legal ones. The alternative —
treating a typo as ``desks`` — would put unreviewed web text into a desk's
read because someone mistyped an env var.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: The one program flag (§6.2). Absent/empty == :data:`REGIME_OFF`.
RESEARCH_EVIDENCE_ENV: str = "LEGBA_RESEARCH_EVIDENCE"

#: The three legal values, in escalation order.
REGIME_OFF: str = "off"
REGIME_SUBSTRATE: str = "substrate"
REGIME_DESKS: str = "desks"

REGIMES: tuple[str, ...] = (REGIME_OFF, REGIME_SUBSTRATE, REGIME_DESKS)


def research_evidence_regime() -> str:
    """The program's current regime — one of :data:`REGIMES`, never anything else.

    Absent, empty or unrecognised → :data:`REGIME_OFF` (an unrecognised value
    logs a warning naming it; see the module note on failing safe). Read from
    the environment on EVERY call rather than cached at import, so a process
    that re-reads its environment — and every test that monkeypatches it — sees
    the current value, matching ``assembly_payload.assembly_enabled``'s
    discipline.
    """
    raw = (os.environ.get(RESEARCH_EVIDENCE_ENV) or "").strip().lower()
    if not raw:
        return REGIME_OFF
    if raw in REGIMES:
        return raw
    logger.warning(
        "research_regime.unrecognised_value %s=%r — reading as %r "
        "(legal values: %s)",
        RESEARCH_EVIDENCE_ENV, raw, REGIME_OFF, ", ".join(REGIMES),
    )
    return REGIME_OFF


def research_evidence_enabled() -> bool:
    """Is the program's WRITE path live at all (``substrate`` or ``desks``)?

    This is the gate R-B's coverage-floor dispatch rides: at ``off`` the
    detector's output is byte-identical to its pre-program self and no
    ``hypotheses`` row is minted. Dispatching a research question into a
    backlog whose researcher cannot reach the web would be a question nothing
    can answer — noise in a backlog that already holds 1,795 rows.
    """
    return research_evidence_regime() in (REGIME_SUBSTRATE, REGIME_DESKS)


def research_reaches_desks() -> bool:
    """Does research evidence reach a desk's reactive slice (``desks`` only)?

    §1.3b names this predicate for R-A's ONE-clause slice exclusion:
    ``if not research_reaches_desks(): clauses.append("(retrieval_origin IS
    NULL OR retrieval_origin NOT LIKE 'web_search:%')")``. It lives here, and
    not in the slice module, so the write path and the read path can never
    disagree about what regime the process is in.
    """
    return research_evidence_regime() == REGIME_DESKS


__all__ = [
    "REGIMES",
    "REGIME_DESKS",
    "REGIME_OFF",
    "REGIME_SUBSTRATE",
    "RESEARCH_EVIDENCE_ENV",
    "research_evidence_enabled",
    "research_evidence_regime",
    "research_reaches_desks",
]
