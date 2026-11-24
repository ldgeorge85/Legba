# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``signal_summarizer``.

Output: signal_summarizer — pure side-effect sweep: writes OUR analysis-
tuned summary into signals.payload.distilled_body + stamps summarized_at.
Emits no analytical finding (like entity_resolution); the per-run counts
live in the trace.

Dispatch: signal_summarizer — async sweep that distills long signal bodies
into payload.distilled_body via the CORE self-hosted LLM plane ($0), so
downstream synthesis reads OUR analysis-tuned brief instead of the
publisher's teaser. Idempotent + forward-progressing (stamps
signals.summarized_at on every examined row).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="signal_summarizer",
    implementation="legba.data.analysts.deterministic_handlers.signal_summarizer:handle",
    output="trace_only",
    doc="Distils long signal bodies into an analysis-tuned summary on the core plane.",
)
