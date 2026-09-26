# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``LEGBA_RESEARCH_EVIDENCE`` — the ONE flag the whole research program rides.

Three values, not two booleans (RESEARCH_PROGRAM_SPEC §6.2 / §8 F-4):

``off`` (the DEFAULT)
    The write path is dark. ``web_evidence`` is granted-but-REFUSES, loudly,
    naming this flag; no ``signals`` row is written, no byte is fetched, and
    the exclusion predicate below is moot because there is nothing to exclude.
    The old path is byte-identical — asserted by
    ``test_research_flag_off_byte_identical``, not hoped for.
``substrate``
    The write path runs in FULL: signals landed, tagged with
    ``retrieval_origin``, archived where the licence ledger clears the host,
    ceiling applied, novelty stamped, counters computable. They are EXCLUDED
    from every desk's reactive slice by the single clause in
    ``runtime/actor_substrate_slice.py``.
``desks``
    The exclusion lifts. This is the cutover.

WHY THE MIDDLE RUNG EXISTS. It is not a build stage — it is the POISONING
ROLLBACK. On a bad day the operator pulls research out of every desk's eyes
with one env var while keeping the evidence, the archive, the counters and the
audit trail intact. Two booleans would admit the incoherent state "excluded
from desks but not written", which means nothing and which a test would then
have to forbid.

The house style for a regime flag is boolean 0/1 (``LEGBA_COMPOSITION_ASSEMBLY``,
``LEGBA_REGION_ROLLUP``); this one is a string because it has three states.
That divergence is deliberate and is flagged in the spec (§8 F-4).

An UNRECOGNISED value reads as ``off``. Fail-safe, and logged once by the
caller that notices — a typo'd flag must never silently open the write path.
"""

from __future__ import annotations

import os

from .retrieval_origin import WEB_SEARCH_PREFIX

#: The env var. One name, read in three places (the tool, the slice, the row
#: stamp) and NOWHERE else.
RESEARCH_EVIDENCE_ENV = "LEGBA_RESEARCH_EVIDENCE"

RESEARCH_OFF = "off"
RESEARCH_SUBSTRATE = "substrate"
RESEARCH_DESKS = "desks"

#: The full vocabulary, in increasing order of reach.
RESEARCH_MODES: tuple[str, ...] = (RESEARCH_OFF, RESEARCH_SUBSTRATE, RESEARCH_DESKS)

#: The shipped default. Every train lands dark.
RESEARCH_DEFAULT_MODE = RESEARCH_OFF


def research_evidence_mode() -> str:
    """The current regime: ``off`` | ``substrate`` | ``desks``.

    Read from the environment on EVERY call (never cached at import): the
    operator flips this with a container env change, and a cached read would
    make the rollback require a code deploy.
    """
    raw = (os.environ.get(RESEARCH_EVIDENCE_ENV) or "").strip().lower()
    return raw if raw in RESEARCH_MODES else RESEARCH_DEFAULT_MODE


def research_writes_enabled() -> bool:
    """True when ``web_evidence`` may land rows (``substrate`` or ``desks``)."""
    return research_evidence_mode() in (RESEARCH_SUBSTRATE, RESEARCH_DESKS)


def research_reaches_desks() -> bool:
    """True ONLY at ``desks`` — the one predicate the slice exclusion reads."""
    return research_evidence_mode() == RESEARCH_DESKS


#: THE EXCLUSION PREDICATE — one clause, one place (spec §1.3b). Appended to
#: ``actor_substrate_slice._read_substrate_slice``'s ``clauses`` list whenever
#: :func:`research_reaches_desks` is False, and to the novelty probe's
#: comparison set so the two can never disagree about what "the desk's slice"
#: means.
#:
#: The ``LIKE 'web_search:%'`` form is BUILT FROM
#: :data:`legba.data.retrieval_origin.WEB_SEARCH_PREFIX` rather than inlined,
#: so the literal cannot drift from the value ``web_search_origin()`` writes or
#: from the test ``is_web_retrieved`` performs. A NULL origin (every one of the
#: 229,893 rows that existed before this program) passes the clause untouched —
#: which is what makes the flag-off path byte-identical.
RESEARCH_SLICE_EXCLUSION_SQL = (
    "(retrieval_origin IS NULL OR retrieval_origin NOT LIKE "
    f"'{WEB_SEARCH_PREFIX}%')"
)


__all__ = [
    "RESEARCH_DEFAULT_MODE",
    "RESEARCH_DESKS",
    "RESEARCH_EVIDENCE_ENV",
    "RESEARCH_MODES",
    "RESEARCH_OFF",
    "RESEARCH_SLICE_EXCLUSION_SQL",
    "RESEARCH_SUBSTRATE",
    "research_evidence_mode",
    "research_reaches_desks",
    "research_writes_enabled",
]
