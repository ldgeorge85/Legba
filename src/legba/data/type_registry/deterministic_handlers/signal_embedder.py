# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``signal_embedder``.

Output: signal_embedder — pure side-effect sweep: projects signals into the
Qdrant legba_signals collection (the VECTOR PLANE — semantic retrieval that
lights up vector_search) + stamps embedding_ref. Emits no analytical finding
(like corpus_indexer / signal_summarizer); the per-run counts (examined /
embedded / failed) live in the trace.

Dispatch: signal_embedder — async sweep that embeds signal bodies into the
Qdrant legba_signals collection (the VECTOR PLANE — semantic retrieval that
lights up vector_search, which no-ops today with 0 points). Idempotent +
forward-progressing (stamps signals.embedding_ref; the Qdrant point _id =
the signal id, so a re-embed overwrites in place).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="signal_embedder",
    implementation="legba.data.analysts.deterministic_handlers.signal_embedder:handle",
    output="trace_only",
    doc="Embeds signal bodies into the vector collection.",
)
