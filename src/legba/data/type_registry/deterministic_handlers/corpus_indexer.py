# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``corpus_indexer``.

Output: corpus_indexer — pure side-effect sweep: projects signals into the
OpenSearch full-text corpus (the INDEX PLANE) + stamps indexed_at. Emits no
analytical finding (like signal_summarizer / entity_resolution); the per-run
counts (examined / indexed / failed) live in the trace.

Dispatch: corpus_indexer — async sweep that projects signals into the
OpenSearch full-text corpus (the INDEX PLANE — BM25 lexical mining substrate
over the shared pool). Idempotent + forward-progressing (stamps
signals.indexed_at; OpenSearch `_id` = the signal id, so a re-index
overwrites in place).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="corpus_indexer",
    implementation="legba.data.analysts.deterministic_handlers.corpus_indexer:handle",
    output="trace_only",
    doc="Projects signals into the OpenSearch full-text corpus.",
)
