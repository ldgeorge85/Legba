# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.events — the DATA MODEL V3 event surface (P0).

An event is a bounded real-world occurrence evidenced by signals — not a
situation (a persistent frame composed of findings). The package owns:

* :mod:`~legba.data.events.lifecycle` — the five-state lifecycle FSM
  (``emerging`` / ``developing`` / ``active`` / ``evolving`` / ``resolved``),
  the transition vocabulary the 0204 ledger's CHECKs mirror, ``next_state``
  raising rather than coercing, the pure trigger evaluation
  (:func:`~legba.data.events.lifecycle.evaluate_transition` over an
  :class:`~legba.data.events.lifecycle.EventMeasurement`), and the validated
  ``LifecycleEvent`` row.
* :mod:`~legba.data.events.signature` — the ``evt:<topic>|<entities>#evt:
  <anchor>`` identity builder and its Postgres twin
  (:data:`~legba.data.events.signature.EVENT_SIGNATURE_SQL`), which the 0205
  tower backfill and P1's clusterer must agree on row-for-row.
* :mod:`~legba.data.events._writes` — the write-path SQL constants and link
  helpers driven by ``provenance.writes._insert_event``.

Deliberately NOT a re-exporting ``__init__``: ``provenance.writes`` imports
``_writes``, and an eager re-export of ``signature`` here would pull the
canon/handler chain into every writes import — a layering and cycle hazard
for no benefit. Import the leaves directly.

P0 ships no consumer: nothing reads ``events`` yet, and every live write goes
through ``provenance.writes._insert_event`` gated behind ``LEGBA_EVENTS``
(default off — ``writes.events_enabled``).
"""

from __future__ import annotations
