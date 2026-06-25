# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""validate-before-register (P-13) — liveness + trial pull/parse for a candidate source.

Per ``SourceDiscoveryBlock.validate_before_register`` (the P-13 default), a
candidate source emitted by a source-discovery handler does NOT become a
registered :class:`~legba.data.schemas.source.SourceDescriptor` until it passes
a real probe:

  1. **liveness** — build the candidate's source handler (via the same
     :func:`legba.runtime.source_factory.build_source_handler` the runtime uses)
     and run its ``health_check``. A handler that reports ``unhealthy`` (or
     raises) is rejected.
  2. **trial pull/parse** — call ``handler.pull(ctx, since=None)`` and consume
     up to ``max_trial_signals`` signals. A handler that raises while pulling
     (DNS failure, 404, malformed body the parser chokes on) is rejected. An
     empty-but-clean pull is allowed by default (a freshly-published feed may
     have no items yet) unless ``require_nonempty`` is set.

The probe uses an :class:`~legba.data.sources._contract.InMemoryStateStore` so
it never touches substrate cursor state — it is a pure, side-effect-free dry run
against the *live upstream*. Signals it pulls are discarded; the only output is
the :class:`SourceCandidateValidation` verdict.

This is the gate that keeps the source pool clean: only sources that actually
respond + parse get registered, so the selector auto-wire (PIVOT §4.4) never
attracts a dead feed.
"""

from __future__ import annotations

import logging
from typing import Any

from ..sources._contract import InMemoryStateStore, SourceContext
from .source_contract import CandidateSource, SourceCandidateValidation

logger = logging.getLogger(__name__)


# A bounded default — we only need to prove the feed parses, not drain it.
_DEFAULT_MAX_TRIAL_SIGNALS = 3


def _build_probe_context(candidate: CandidateSource) -> SourceContext:
    """Build a throwaway :class:`SourceContext` for the trial pull.

    The context uses an in-memory state store (no cursor persistence) + the
    candidate's natural_key as the source id, so the probe is a pure dry run.
    """

    class _ProbeConfig:  # minimal BaseModel-shaped stand-in is not enough;
        pass

    from pydantic import BaseModel, ConfigDict

    class _OpenConfig(BaseModel):
        model_config = ConfigDict(extra="allow")

    return SourceContext(
        target_id=candidate.natural_key,
        target_version="probe",
        source_id=candidate.natural_key,
        config=_OpenConfig(**dict(candidate.probe_config)),
        state_store=InMemoryStateStore(),
    )


async def validate_candidate_source(
    candidate: CandidateSource,
    *,
    secrets_resolve: Any = None,
    source_registry: Any = None,
    max_trial_signals: int = _DEFAULT_MAX_TRIAL_SIGNALS,
    require_nonempty: bool = False,
    handler: Any = None,
) -> SourceCandidateValidation:
    """Run liveness + trial pull/parse for one :class:`CandidateSource`.

    Parameters
    ----------
    candidate:
        The candidate to probe. ``source_kind`` selects the handler;
        ``probe_config`` feeds it.
    secrets_resolve:
        Optional async secret resolver threaded into the probe context (a
        candidate source needing an API key resolves it here).
    source_registry:
        Optional pre-built ``kind -> handler-class`` map (avoids the
        discovery-walk cost; tests inject a tiny registry).
    max_trial_signals:
        Stop the trial pull after this many signals — we only need to prove the
        feed parses, not drain it.
    require_nonempty:
        When True, a live-but-empty trial pull is rejected (the candidate must
        produce at least one signal). Default False — an empty clean feed is
        valid (it may simply have no items yet).
    handler:
        Optional pre-built handler instance (tests inject a fake to avoid network
        I/O). When omitted, the handler is built via
        :func:`legba.runtime.source_factory.build_source_handler`.

    Returns
    -------
    SourceCandidateValidation
        ``valid=True`` iff liveness passed AND the trial pull/parse did not
        raise (and produced >=1 signal when ``require_nonempty``).
    """
    nk = candidate.natural_key

    # --- build the handler --------------------------------------------------
    if handler is None:
        try:
            from ...runtime.source_factory import build_source_handler

            handler = build_source_handler(
                candidate.source_kind,
                dict(candidate.probe_config),
                secrets_resolve=secrets_resolve,
                registry=source_registry,
            )
        except Exception as exc:
            return SourceCandidateValidation(
                natural_key=nk,
                valid=False,
                live=False,
                reason=f"handler_build_failed: {type(exc).__name__}: {exc}",
                detail={"source_kind": candidate.source_kind},
            )

    ctx = _build_probe_context(candidate)
    ctx = ctx.model_copy(update={"secrets_resolve": secrets_resolve})

    # --- liveness probe -----------------------------------------------------
    live = False
    degraded = False
    health_detail: dict[str, Any] = {}
    try:
        health = await handler.health_check(ctx)
        state = getattr(health, "state", "unhealthy")
        health_detail = {"health_state": state}
        # 'unhealthy' is a hard fail; 'degraded' still proceeds to trial pull
        # (a rate-limited-but-reachable feed is acceptable for registration) —
        # BUT a degraded source must PROVE it works by producing at least one
        # signal in the trial pull (see the require-proof check below). Many
        # handlers (e.g. RSS) swallow a failed fetch into 'degraded' + an empty
        # pull rather than raising, so degraded+empty is not provably live.
        if state == "unhealthy":
            return SourceCandidateValidation(
                natural_key=nk,
                valid=False,
                live=False,
                reason=f"liveness_unhealthy: {getattr(health, 'last_error', '') or 'unhealthy'}",
                detail=health_detail,
            )
        degraded = state == "degraded"
        live = True
    except Exception as exc:
        return SourceCandidateValidation(
            natural_key=nk,
            valid=False,
            live=False,
            reason=f"liveness_probe_raised: {type(exc).__name__}: {exc}",
            detail={"source_kind": candidate.source_kind},
        )

    # --- trial pull / parse -------------------------------------------------
    pulled = 0
    sample_detail: dict[str, Any] = {}
    try:
        async for signal in handler.pull(ctx, None):
            pulled += 1
            if pulled == 1:
                # Capture a tiny sample for the verdict detail (observability).
                payload = getattr(signal, "payload", {}) or {}
                sample_detail = {
                    "sample_title": str(payload.get("title", ""))[:120],
                    "sample_url": getattr(signal, "canonical_url", None),
                }
            if pulled >= max_trial_signals:
                break
    except Exception as exc:
        return SourceCandidateValidation(
            natural_key=nk,
            valid=False,
            live=live,
            trial_signals=pulled,
            reason=f"trial_pull_raised: {type(exc).__name__}: {exc}",
            detail={**health_detail, "pulled_before_error": pulled},
        )

    if require_nonempty and pulled == 0:
        return SourceCandidateValidation(
            natural_key=nk,
            valid=False,
            live=live,
            trial_signals=0,
            reason="trial_pull_empty (require_nonempty)",
            detail=health_detail,
        )

    # A degraded liveness state must be backed by a non-empty trial pull —
    # otherwise the source hasn't proven it works (a dead host whose handler
    # reports 'degraded' + yields nothing is rejected here, not registered).
    if degraded and pulled == 0:
        return SourceCandidateValidation(
            natural_key=nk,
            valid=False,
            live=live,
            trial_signals=0,
            reason="liveness_degraded_and_trial_pull_empty (source not provably live)",
            detail=health_detail,
        )

    return SourceCandidateValidation(
        natural_key=nk,
        valid=True,
        live=live,
        trial_signals=pulled,
        reason="",
        detail={**health_detail, **sample_detail},
    )


__all__ = [
    "validate_candidate_source",
]
