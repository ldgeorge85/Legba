# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``corpus_retention``.

Output: corpus_retention — corpus_indexer's mirror: drains the
corpus_tombstones queue (mig 0175) by DELETING docs whose signals row is
gone. Pure side-effect sweep against OpenSearch; the per-run counts
(examined / deleted / pending / skipped_row_alive) live in the trace.

Dispatch: corpus_retention — the corpus DELETE path, the mirror of
corpus_indexer. Drains corpus_tombstones (written transactionally by every
signals-deletion site) against OpenSearch. Re-verifies the row is actually
gone before deleting, so a bad tombstone can never destroy a live doc.
Idempotent (deleting an absent doc is a success), bounded retries.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="corpus_retention",
    implementation="legba.data.analysts.deterministic_handlers.corpus_retention:handle",
    output="trace_only",
    doc="Drains the corpus tombstone queue: deletes documents whose signal row is gone.",
)
