# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``reenrich_translation``.

Output: reenrich_translation — TRANSLATION backfill sweep (M13/T-1c):
translates the non-Latin backlog's title (+ body when present) via the
hosted /translate, side-writing payload.title_en / payload.text_en so
readers narrate English, not a transliterated surface. Its real product is
the side-written signal enrichment (like reenrich_ner); the per-run counts
live in the trace.

Dispatch: reenrich_translation — TRANSLATION-backfill sweep (M13/T-1c) that
translates the ~1.9k non-Latin signals ingested BEFORE T-1a stamped
title_en/text_en at ingest. Reuses the hosted /translate plane (never
reimplements it); idempotent + forward-progressing (the field IS the marker
— title_en NULL is the gate).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="reenrich_translation",
    implementation="legba.data.analysts.deterministic_handlers.reenrich_translation:handle",
    output="trace_only",
    doc="Translation backfill for the non-Latin backlog.",
)
