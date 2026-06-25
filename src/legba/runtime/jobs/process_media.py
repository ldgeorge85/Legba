# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``process_media`` job handler (P-07 / PIVOT §4.6 tier 3).

The on-demand media-extraction job. An analyst calls ``process_media(media_ref,
kind)`` (the action-pack tool, P-11) → a :class:`JobEnvelope` lands on the work-
queue → a worker runs THIS handler:

  1. Refuse loudly (terminal) if the loop cannot close: no real media endpoint
     configured, or no subscription engine wired to re-publish the result.
  2. Parse the typed :class:`ProcessMediaInput` out of the generic envelope.
  3. Read the raw parent signal (the lineage source).
  4. Call the hosted media edge (:class:`MediaClient`) for the extraction.
  5. Build the DERIVED signal (``produced_by_kind='job'``, ``derived_from`` →
     raw, parent geo/tags/entity_classes/language inherited) and land it in
     the source-first ``signals`` table.
  6. **Publish the landed derived signal back into fan-out**
     (``SubscriptionEngine.publish_signal(event_class="derived")``) so it
     re-enters the subscription/trigger path and can mark (analyst, target)
     pairs dirty — the A-2 / review-G3 loop close. Land-then-publish ordering
     + the deterministic derived id make a crash between the two steps safe:
     the retry's insert is an ``ON CONFLICT DO NOTHING`` no-op and the publish
     happens then.

Idempotency is enforced one layer up (the worker claims the envelope's
idempotency_key before calling this handler), AND belt-and-suspenders here: the
derived signal id is deterministic in (job_id, parent, extraction) so a replay
that reaches the insert is an ``ON CONFLICT DO NOTHING`` no-op.

A transient extraction failure (endpoint down / 5xx →
:class:`MediaEndpointUnreachable`) propagates out of this handler so the
worker releases the claim and JetStream redelivers (retry); a 4xx /
configuration refusal is terminal.
"""

from __future__ import annotations

import logging

from ...data.jobs.envelope import JobEnvelope, JobResult
from ...data.jobs.media import (
    MediaEndpointNotConfiguredError,
    ProcessMediaInput,
    build_derived_signal,
)
from ...data.jobs.store import JobStore
from .media_client import MediaClient, MediaExtractionError
from .dispatch import JobContext

logger = logging.getLogger(__name__)


def _failed(env: JobEnvelope, ctx: JobContext, error: str) -> JobResult:
    return JobResult(
        job_id=env.job_id,
        job_kind=env.job_kind,
        status="failed",
        error=error,
        worker_id=ctx.worker_id,
    )


async def process_media_handler(
    env: JobEnvelope, ctx: JobContext
) -> JobResult:
    """Handle one ``process_media`` job. See module docstring for the flow."""
    inp = ProcessMediaInput.from_envelope_refs(env.input_refs)
    media = ctx.media or MediaClient.from_env()

    # --- refuse loudly before any work / any row (A-2 hard guards) -------
    if not media.has_endpoint:
        logger.error(
            "process_media.refused job_id=%s reason=endpoint_not_configured "
            "env=LEGBA_MEDIA_API_URL media_ref=%s extraction=%s "
            "(no stub output may land in the pool)",
            env.job_id, inp.media_ref, inp.extraction,
        )
        return _failed(
            env, ctx,
            "media endpoint not configured (LEGBA_MEDIA_API_URL unset) — "
            "process_media refused; no stub output may land in the pool",
        )
    if ctx.subscriptions is None:
        logger.error(
            "process_media.refused job_id=%s reason=no_subscription_engine "
            "(derived signal could land but never re-enter fan-out — refusing "
            "the half-state)",
            env.job_id,
        )
        return _failed(
            env, ctx,
            "no subscription engine wired into the job plane — process_media "
            "refused (a landed derived signal must re-enter fan-out)",
        )

    async with ctx.pg.acquire() as conn:
        parent = await JobStore.get_signal(conn, inp.derived_from)
    if parent is None:
        return _failed(
            env, ctx, f"raw parent signal {inp.derived_from} not found",
        )

    # --- external-model edge (real hosted call only) ---------------------
    try:
        result = await media.extract(
            media_ref=inp.media_ref,
            extraction=inp.extraction,
            modality=inp.modality,
            mime_type=inp.mime_type,
            language_hint=inp.language_hint,
        )
    except MediaEndpointNotConfiguredError as exc:
        # Belt-and-suspenders for an injected client constructed without an
        # endpoint — same terminal refusal as the has_endpoint guard above.
        logger.error("process_media.refused job_id=%s err=%s", env.job_id, exc)
        return _failed(env, ctx, f"media extraction refused: {exc}")
    except MediaExtractionError as exc:
        # A reachable endpoint refused the request — a real, terminal failure.
        return _failed(env, ctx, f"media extraction refused: {exc}")
    # MediaEndpointUnreachable propagates → the worker releases the claim and
    # naks (transient outage = retry, terminal only at max_deliver).

    # --- land the DERIVED signal with lineage to the raw parent ----------
    derived = build_derived_signal(
        job_id=env.job_id, parent_row=parent, inp=inp, result=result,
    )
    async with ctx.pg.acquire() as conn:
        derived_id = await JobStore.land_derived_signal(conn, derived)

    # --- publish it back into fan-out (the loop close) -------------------
    # A publish failure propagates: the worker releases the claim + naks, the
    # redelivery re-lands (no-op) and re-publishes. At-least-once publish is
    # safe — the deterministic derived id dedups downstream (the coalescer's
    # seen-set keys on the signal id).
    subject = await ctx.subscriptions.publish_signal(
        signal=derived, event_class="derived",
    )

    logger.info(
        "process_media done job_id=%s extraction=%s parent=%s derived=%s "
        "model_source=%s published=%s",
        env.job_id, inp.extraction, inp.derived_from, derived_id,
        result.source, subject,
    )
    return JobResult(
        job_id=env.job_id,
        job_kind=env.job_kind,
        status="completed",
        output_refs={
            "derived_signal_id": str(derived_id),
            "extraction": inp.extraction,
            "model_source": result.source,
            "derived_from": [str(inp.derived_from)],
            "published_subject": subject,
        },
        worker_id=ctx.worker_id,
    )


__all__ = ["process_media_handler"]
