# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``due_and_absent``.

Kind: A row of the record that carries an explicit date (a sealed forecast,
a calendar row, an inquiry's resolves-by, a live series' next period) is
past it with no matching record collected. Opened when first seen, closed
when it is resolved late or voided. The subject kind of each raise is the
expectation's own (question / target / analyst / source). Never paged by the
shipped rule.

Topic: E-1: an expectation due and absent in the collection
(due_and_absent.py).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="due_and_absent",
    position=320,
    subject_kind="platform",
    lifecycle="condition",
    quiet_close_hours=None,
    group="expectations",
    topic="engine",
    paging="logged_only",
    subject_note="question / target / analyst / source (the expectation's own)",
    doc='Something expected by a date has no matching record in this collection (logged only; an absence in the collection, not a statement about the world).',
)
