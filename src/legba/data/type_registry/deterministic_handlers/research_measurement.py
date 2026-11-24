# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``research_measurement``.

Output: R-D the outbound-research program's three counters (NOVELTY /
CORROBORATION / CONSEQUENCE, RESEARCH_PROGRAM_SPEC §4). The counters ARE the
product — §4.4 rules them onto kind='finding' +
analyst_id='research_measurement' explicitly, on the calibration_tracking
precedent, so that /api/v1/findings?analyst_id=research_measurement serves
them with no API edit and NO new OutputKind is minted. A measurement that
only reached analyst_traces would be a program watching itself in private,
which is the exact failure §4 exists to prevent.

Dispatch: R-D MEASUREMENT — the outbound-research program's three counters
(RESEARCH_PROGRAM_SPEC_2026-09-05 §4). Daily LLM-free sweep over the
research signals (`retrieval_origin LIKE 'web_search:%'`, the seam migration
0112 built and nothing has ever written) computing NOVELTY from R-A's write-
time stamp, CORROBORATION over a matured 7-day forward window against NON-
research rows, and the CONSEQUENCE reach ladder (cited by anything -> cited
by a DESK head -> that head quoted into a composition). A GAUGE: it COUNTS +
NAMES and repairs nothing — no ceiling lift, no sidecar, no UPDATE. Refuses
loud without a pool; publishes every rate beside its n and honest-null (with
a reason) below gate G9's floor.

Verify exemption: R-D: the research program's three counters. Pure SQL
arithmetic over the research signals — there is no model prose in the row to
grade, and its own honest-null discipline (every rate beside its n, null
below gate G9's floor) is a stronger statement than a faithfulness score
would be. Added here because OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and
this set MUST stay equal to the FINDING emitters; it changes no existing
analyst's verify routing.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="research_measurement",
    implementation="legba.data.analysts.deterministic_handlers.research_measurement:handle",
    output="finding",
    requires_pool=True,
    doc="The outbound-research counters: novelty, corroboration, consequence.",
)
