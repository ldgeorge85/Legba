# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The archiver's canonical-fetch leg, split for the module-size gate.

``evidence_archiver`` crossed the 1,500-line new-entrant threshold while the
W11-F138 per-host policy was landing, so the cohesive fetch unit moved here
— the same ``consult_clocks.py`` idiom the gate prescribes:

  * :class:`_ObjectTooLargeError` + :func:`_fetch_bytes` — the streamed,
    size-capped body read every archiver leg shares (unchanged; imported
    back into ``evidence_archiver`` so the historical ``ea._fetch_bytes``
    name still resolves);
  * :data:`_FEED_COUNTER_KEYS` + :func:`_feed` — the four per-feed outcome
    counters W11-F138 adds (``fetched`` / ``challenge_blocked`` /
    ``boilerplate_rejected`` / ``body_stored``), carried under
    ``counters["per_source"][source_id]``;
  * :func:`_fetch_canonical` — the two-leg fetch: the plain guarded client
    first, then the impersonation ESCALATION a per-host policy permits.

The client CONSTRUCTION deliberately stayed in ``evidence_archiver``
(``_FetchClients``): the h1-pin AST gate reads that module's own
``fetch_client`` call sites, and moving them would move the pin it guards.
This module only ever receives already-opened clients.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...sources._fetch_policy import fetch_policy_for, impersonate_kwarg
from ._challenge_detect import BLOCKED_BY_CHALLENGE, challenge_from_status

logger = logging.getLogger(__name__)


class _ObjectTooLargeError(Exception):
    """The response exceeded the archiver's size cap."""


async def _fetch_bytes(
    client: httpx.AsyncClient, url: str, *, max_bytes: int,
) -> tuple[bytes, str | None, str | None]:
    """Stream ``url`` → ``(body, content_type, encoding)`` under the size cap.

    Raises :class:`_ObjectTooLargeError` past the cap (checked against the
    declared Content-Length first, then enforced on the actual stream — a
    lying header can't blow the cap), and lets httpx/egress errors propagate
    to the per-row handler."""
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise _ObjectTooLargeError(f"declared {declared} bytes > cap {max_bytes}")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise _ObjectTooLargeError(f"stream exceeded cap {max_bytes}")
            chunks.append(chunk)
        return (
            b"".join(chunks),
            resp.headers.get("content-type"),
            resp.charset_encoding,
        )


#: The four per-feed outcome counters W11-F138 adds, carried under
#: ``counters["per_source"][source_id]`` — the numerator set the
#: source-health surface's body-extraction ratio reads from the
#: ``evidence_archive`` sidecar with the SAME status vocabulary.
_FEED_COUNTER_KEYS = (
    "fetched", "challenge_blocked", "boilerplate_rejected", "body_stored",
)


def _feed(counters: dict[str, Any], source_id: Any) -> dict[str, int]:
    """The per-feed bucket for ``source_id``, created on first touch."""
    return counters["per_source"].setdefault(
        str(source_id or "?"), dict.fromkeys(_FEED_COUNTER_KEYS, 0)
    )


async def _fetch_canonical(
    clients: Any,
    politeness: Any,
    url: str,
    *,
    max_bytes: int,
    counters: dict[str, Any],
    feed: dict[str, int],
) -> tuple[bytes | None, str | None, str | None, BaseException | None, str | None]:
    """Fetch ``url``'s bytes: the plain client, then the policy's escalation.

    ``clients`` is a ``_FetchClients`` bundle (``primary()`` /
    ``impersonated()``); ``politeness`` a ``_HostPoliteness``. Both are
    duck-typed here — they are the caller's, and this module never reaches
    back into ``evidence_archiver`` (the import stays one-way).

    Returns ``(body, content_type, encoding, error, escalated_tell)`` —
    ``error is None`` on success; ``escalated_tell`` names the challenge tell
    of a block that was ALREADY counted here because the policy escalated
    it (so the caller never counts the same block twice), and is otherwise
    ``None``.

    The policy leg: an edge block (401/403/429) on a host whose
    ``impersonate`` resolves ON (``impersonate_kwarg`` — a global ``on`` or
    ``fallback`` never reaches here, it already impersonates the primary) is
    counted once and then re-issued through the impersonated client, with
    ``policy.max_retries`` extra attempts at ``policy.backoff_s`` spacing for
    TRANSIENT errors only — a second 403 from the same edge is the same
    answer, and re-asking it is exactly the hammering this module's
    politeness delay exists to prevent.
    """
    policy = fetch_policy_for(url)
    feed["fetched"] += 1
    try:
        await politeness.wait(url)
        body, content_type, encoding = await _fetch_bytes(
            await clients.primary(), url, max_bytes=max_bytes,
        )
        return body, content_type, encoding, None, None
    except httpx.HTTPStatusError as exc:
        tell = challenge_from_status(
            getattr(getattr(exc, "response", None), "status_code", None)
        )
        if tell is None or impersonate_kwarg(policy) is not True:
            return None, None, None, exc, None
        # A counted block, then the escalation leg. The refusal happened and
        # is tallied even when the impersonated retry lands the body —
        # "a publisher challenged us" is a fact about the feed, not about
        # whether we later won.
        counters[BLOCKED_BY_CHALLENGE] += 1
        feed["challenge_blocked"] += 1
        logger.warning(
            "evidence_archiver.%s host=%s tell=%s — host policy escalates "
            "to the impersonated leg",
            BLOCKED_BY_CHALLENGE, urlsplit(url).hostname, tell,
        )
        last_exc: BaseException = exc
        for attempt in range(1 + max(0, int(policy.max_retries))):
            if attempt:
                await asyncio.sleep(max(0.0, float(policy.backoff_s)))
            try:
                await politeness.wait(url)
                body, content_type, encoding = await _fetch_bytes(
                    await clients.impersonated(), url, max_bytes=max_bytes,
                )
                return body, content_type, encoding, None, tell
            except httpx.HTTPStatusError as esc_exc:
                # An edge verdict is not transient — a retry cannot clear it.
                return None, None, None, esc_exc, tell
            except httpx.HTTPError as esc_exc:
                last_exc = esc_exc  # transient (timeout/transport): retry
            except Exception as esc_exc:
                # Missing curl_cffi, a construction fault — never retried.
                return None, None, None, esc_exc, tell
        return None, None, None, last_exc, tell
    except Exception as exc:
        return None, None, None, exc, None


__all__ = [
    "_FEED_COUNTER_KEYS",
    "_ObjectTooLargeError",
    "_feed",
    "_fetch_bytes",
    "_fetch_canonical",
]
