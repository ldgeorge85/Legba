# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``reenrich_ner``.

Output: reenrich_ner — ONE-TIME NER backfill sweep: re-runs the LIVE
multilingual/ telegram NER over the pre-fix historical backlog, writing
payload.entities + promoting entity_classes + resetting entities_resolved_at
(so entity_resolution re-folds them) + stamping reenriched_at. Its real
product is the side-written signal enrichment (like signal_summarizer /
entity_resolution); the per-run counts (examined / reenriched / failed) live
in the trace.

Dispatch: reenrich_ner — ONE-TIME async backfill sweep that re-runs the LIVE
NERMultilingualHandler (translate-then-NER + telegram payload.text) over the
~9,143 signals ingested BEFORE the multilingual/telegram fix landed (they
carry 0 entities). Reuses the production handler (never reimplements NER);
idempotent + forward-progressing (stamps signals.reenriched_at on every
examined row).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="reenrich_ner",
    implementation="legba.data.analysts.deterministic_handlers.reenrich_ner:handle",
    output="trace_only",
    doc="One-time NER backfill over the pre-fix historical backlog.",
)
