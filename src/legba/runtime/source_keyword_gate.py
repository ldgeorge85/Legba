# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SourceCore's keyword gate: apply + count, in a leaf (W3 SRC-3).

Kept out of ``source_actor.py`` (size-gated) so the actor carries only the
call sites. :class:`SourceIngestGate` owns:

  * the ``keyword_gate`` stages of the descriptor's
    ``pipeline.ingestion_filters``, each built by the REAL
    :func:`legba.runtime.pipeline.build_filter_handler` (other kinds in that
    list — the ingest dedupe tiers — are handled by ``ingest_dedupe`` and
    ignored here);
  * :meth:`admit`, which SourceCore calls on each raw signal BEFORE
    ``run_baseline`` — so before ``pipeline.enrichment`` (the NER POSTs), the
    write and the fan-out;
  * the source handler's DECLARED gate — the RSS ``keyword_include`` /
    ``keyword_exclude`` sugar builds the same :class:`KeywordGate` and exposes
    it as ``handler.keyword_gate``; the handler still yields every entry and
    this class applies the gate. Applying it here rather than inside
    ``handler.pull`` is deliberate: SourceCore counts an entry toward cursor
    progress (``last_processed_ts``) BEFORE ``_process_one``, so a dropped
    entry advances ``since`` and is judged once. Dropped inside the handler it
    would never reach the actor, the cursor would hold on an all-dropped poll,
    and the same entries would be re-judged and re-counted on every poll;
  * the per-source drop counter. Drops from the stages and the handler gate
    are drained once per poll into the source's ``cursor`` state row
    (``actor_filter_state``, key ``cursor``) — the same row that already
    carries the cumulative ``rows_pulled``:

      ``keyword_filtered_total``      cumulative drops since the counter began
      ``keyword_filtered_last_poll``  drops on the most recent poll
      ``keyword_filtered_by_reason``  cumulative drops per reason
                                       (``exclude:<term>`` / ``include:none`` /
                                       ``include:missing:<term>``)

    The keys are written only for a GATED source — absent means "this source
    has no keyword gate", never 0.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..data.filters.keyword_gate import KEYWORD_GATE_KIND, KeywordGate, KeywordGateStats
from .pipeline import build_filter_handler
from .source_factory import _unwrap_factory_dict

#: Bound on the persisted per-reason map (terms change over a source's life).
_MAX_REASONS = 50


def _stage_field(stage: Any, name: str) -> Any:
    return stage.get(name) if isinstance(stage, dict) else getattr(stage, name, None)


class SourceIngestGate:
    """Pre-baseline keyword gating + the per-source drop counter."""

    def __init__(self, stages: Iterable[Any] = ()) -> None:
        self._handlers = [
            build_filter_handler(
                kind=KEYWORD_GATE_KIND,
                config=_unwrap_factory_dict(dict(_stage_field(s, "config") or {})),
            )
            for s in stages or []
            if _stage_field(s, "kind") == KEYWORD_GATE_KIND
        ]
        self._handler_gate: KeywordGate | None = None
        self.last_poll_dropped: int | None = None

    @property
    def gated(self) -> bool:
        return bool(self._handlers) or self._handler_gate is not None

    def observe_handler(self, handler: Any) -> None:
        """Pick up a source handler's own gate (the RSS sugar), if any."""
        gate = getattr(handler, "keyword_gate", None)
        self._handler_gate = gate if isinstance(gate, KeywordGate) else None

    async def admit(self, signal: Any, ctx: Any) -> bool:
        """Run every ``keyword_gate`` stage in declared order, then the source
        handler's own gate (the RSS sugar); False = drop."""
        for handler in self._handlers:
            if await handler.transform(signal, ctx) is None:
                return False
        if self._handler_gate is not None:
            return self._handler_gate.admit(
                signal, source_id=getattr(ctx, "source_id", None),
            )
        return True

    def _drain(self) -> KeywordGateStats:
        total = KeywordGateStats()
        gates = [h.gate for h in self._handlers]
        if self._handler_gate is not None:
            gates.append(self._handler_gate)
        for gate in gates:
            got = gate.drain_stats()
            total.dropped += got.dropped
            total.by_reason.update(got.by_reason)
        return total

    def cursor_fields(self, prior_cursor: Any) -> dict[str, Any]:
        """Drain this poll's drops into cumulative cursor fields ({} if ungated)."""
        if not self.gated:
            self.last_poll_dropped = None
            return {}
        prior = prior_cursor if isinstance(prior_cursor, dict) else {}
        polled = self._drain()
        self.last_poll_dropped = polled.dropped
        try:
            prior_total = max(0, int(prior.get("keyword_filtered_total") or 0))
        except (TypeError, ValueError):
            prior_total = 0
        reasons: dict[str, int] = {}
        prior_reasons = prior.get("keyword_filtered_by_reason")
        if isinstance(prior_reasons, dict):
            for k, v in prior_reasons.items():
                if isinstance(k, str) and isinstance(v, int) and v > 0:
                    reasons[k] = v
        for k, v in polled.by_reason.items():
            reasons[k] = reasons.get(k, 0) + int(v)
        top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_REASONS]
        return {
            "keyword_filtered_total": prior_total + polled.dropped,
            "keyword_filtered_last_poll": polled.dropped,
            "keyword_filtered_by_reason": dict(top),
        }


__all__ = ["SourceIngestGate"]
