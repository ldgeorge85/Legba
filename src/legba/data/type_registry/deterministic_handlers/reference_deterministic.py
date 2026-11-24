# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``reference_deterministic``.

Output: INST-1 reference_deterministic — the KEYED reference kind's receipt
(a FINDING on the reference_builder precedent): which bound subjects got a
reference cut from the declared list, how many keyed items each carries. The
references are side-written to unit_references; it never gates.

Dispatch: INST-1 reference_deterministic — a reference that is a machine-
readable LIST (KEV / openFDA shape): fetch it once through the guarded
client, cut each bound subject's items inside the window, write one keyed
unit_references row each; the grader then grades it exactly, by key, with no
model. Flag LEGBA_REFERENCE_DETERMINISTIC_ENABLED, default OFF.

Verify exemption: INST-1: the keyed reference kind binds the same way as the
builder it sits beside — its receipt counts what a machine-readable list
carried for each bound subject, and the reference it writes is an INPUT to
the grader, never a finding to be judged. Added here because
OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and this set MUST stay equal to
the FINDING emitters.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="reference_deterministic",
    implementation="legba.data.analysts.deterministic_handlers.reference_deterministic:handle",
    output="finding",
    requires_pool=True,
    doc="Cuts a machine-readable list into keyed per-subject references.",
)
