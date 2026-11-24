# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``evidence_archiver``.

Output: evidence_archiver (P2-1) — pure side-effect sweep: archives the
original bytes behind CITED signals content-addressed onto the archive
volume, upserts the evidence_archive sidecar (mig 0104) + stamps
signals.object_ref / retention_class / payload.archived_text. Emits no
analytical finding (like corpus_indexer); the per-run counts (examined /
archived / skipped_license / …) live in the trace.

Dispatch: evidence_archiver — P2-1 cited-evidence archival sweep: fetches +
stores the original bytes behind signals cited by VERIFIED findings
(content- addressed at {LEGBA_ARCHIVE_ROOT}/{sha256[:2]}/{sha256}), stamps
signals.object_ref (cas:sha256/<hex>) + the evidence_archive sidecar, and
re-queues extracted full text for the corpus via the corpus_indexer dirty-
marker contract. Idempotent + forward-progressing (object_ref IS NULL is the
selection gate; sidecar attempt caps bound retries).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="evidence_archiver",
    implementation="legba.data.analysts.deterministic_handlers.evidence_archiver:handle",
    output="trace_only",
    doc="Archives the original bytes behind cited signals, content-addressed.",
)
