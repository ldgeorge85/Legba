# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``fact_decay`` sub-handler — L-203 migration of ``legba.maintenance.fact_decay``.

Fact temporal management. No LLM. Two operations:

  1. Expire facts with explicit ``valid_until`` in the past — set
     ``data.expired = true``.
  2. Decay confidence on stale open-ended facts (>30d since updated_at,
     confidence > 0.1, not expired). Subtracts 0.05 from scalar
     confidence floor 0.1 and from the ``confidence_components.decay``
     audit field.

Output ``data`` keys:
    expired_count       int — facts marked expired
    decayed_count       int — facts with confidence reduced
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

_STALE_DAYS = 30
_DECAY_AMOUNT = 0.05
_CONFIDENCE_FLOOR = 0.1


async def _expire_past_valid_until(pool: Any) -> int:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE facts SET
                data = jsonb_set(
                    COALESCE(data, '{}'::jsonb),
                    '{expired}',
                    '"true"'
                ),
                updated_at = NOW()
            WHERE valid_until IS NOT NULL
              AND valid_until < NOW()
              AND superseded_by IS NULL
              AND COALESCE(data->>'expired', 'false') != 'true'
            """
        )
        return int(result.split()[-1]) if result else 0


async def _decay_stale_confidence(pool: Any) -> int:
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""
            UPDATE facts SET
                confidence = GREATEST(confidence - {_DECAY_AMOUNT}, {_CONFIDENCE_FLOOR}),
                confidence_components = jsonb_set(
                    COALESCE(confidence_components, '{{}}'::jsonb),
                    '{{decay}}',
                    to_jsonb(
                        COALESCE((confidence_components->>'decay')::numeric, 0.0) - {_DECAY_AMOUNT}
                    )
                ),
                data = jsonb_set(
                    COALESCE(data, '{{}}'::jsonb),
                    '{{last_confidence_decay}}',
                    to_jsonb(NOW()::text)
                ),
                updated_at = NOW()
            WHERE superseded_by IS NULL
              AND confidence > {_CONFIDENCE_FLOOR}
              AND updated_at < NOW() - INTERVAL '{_STALE_DAYS} days'
              AND COALESCE(data->>'expired', 'false') != 'true'
              AND (valid_until IS NULL OR valid_until > NOW())
            """
        )
        return int(result.split()[-1]) if result else 0


def _build_finding(
    *,
    expired_count: int,
    decayed_count: int,
    target_id: str | None,
) -> FindingPayload:
    title = f"Fact decay: {expired_count} expired, {decayed_count} confidence-decayed"
    if target_id:
        title = f"{title} for {target_id}"
    body = f"expired_count={expired_count}\ndecayed_count={decayed_count}"
    tags = ["deterministic", "fact_decay"]
    if expired_count or decayed_count:
        tags.append("facts_modified")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "fact_decay",
            "expired_count": expired_count,
            "decayed_count": decayed_count,
        },
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring."""
    expired = 0
    decayed = 0
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        if bool(options.get("run_expire", True)):
            try:
                expired = await _expire_past_valid_until(pool)
            except Exception as exc:
                logger.warning("fact_decay.expire_failed err=%s", exc)
        if bool(options.get("run_decay", True)):
            try:
                decayed = await _decay_stale_confidence(pool)
            except Exception as exc:
                logger.warning("fact_decay.decay_failed err=%s", exc)

    finding = _build_finding(
        expired_count=expired,
        decayed_count=decayed,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
