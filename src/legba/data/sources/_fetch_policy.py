# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-host FETCH POLICY — which transport posture a named host gets (W11-F138).

WHY THIS EXISTS. Every Times of Israel (826) and Jerusalem Post (1,628) signal
of the last 30 days carries a summary and no ``payload.text``: both hosts
answer the evidence archiver with an anti-bot CHALLENGE — an edge
``tell=http_403``, logged as ``evidence_archiver.blocked_by_challenge``
several times a day — so no body ever lands in the substrate, and a consult
that needs the quote cannot find it. The platform already owns the transport
that answers this class of block: the ``curl_cffi`` browser-fingerprint
impersonation path behind ``LEGBA_FETCH_IMPERSONATE``
(:mod:`._egress_impersonate`). What it lacked was a way to aim that path at
the hosts that need it without turning it on for the fleet.

This module is the aim: a small typed registry mapping a host — exact or
suffix — to a :class:`FetchPolicy` of ``{impersonate, http2, max_retries,
backoff_s}``. Every field defaults to TODAY'S behaviour, so a host absent
from the registry sees byte-identical client construction to before this
module existed (that claim is golden-tested, not asserted).

THE GLOBAL FLAG KEEPS ITS MEANING. ``LEGBA_FETCH_IMPERSONATE`` stays the
fleet-wide posture, and an EXPLICIT value always wins over the registry: a
global ``on`` / ``fallback`` impersonates everywhere (no row may weaken it),
and a global explicitly set ``off`` (or ``0`` / ``false`` / ``no``) is the
fleet-wide kill switch — no host is impersonated, whatever its row says.
Only an UNSET global leaves the per-host ``impersonate`` field to decide —
and then a host seeded ``impersonate: on`` gets the impersonated transport
for itself alone.

How the consumers read it:

  * **evidence_archiver** — the canonical fetch still goes out on the plain
    guarded client every host shares (the archiver's own ``http2=False`` pin,
    unchanged since the 2026-09-29 Cloudflare-fingerprint measurement). An
    edge block (401/403/429 via ``challenge_from_status``) on a policy host
    whose ``impersonate`` resolves ON is then re-issued through the
    impersonated leg — the ESCALATION — with ``max_retries`` extra attempts
    at ``backoff_s`` spacing for transient errors only. The archiver does
    not apply ``http2`` per host: the h1 pin is deliberate and fleet-wide
    (test_evidence_archiver_http1.py asserts the literal on every
    ``fetch_client`` call in the module).
  * **web_tools.web_fetch** — consults the policy BEFORE building the
    client: a policy host's client is constructed with the policy's
    ``impersonate``/``http2`` resolved in, and ``max_retries``/``backoff_s``
    extend its existing timeout-retry bound. An unlisted host's kwargs are
    today's, character for character.
  * **evidence_refetch** — reads :func:`policy_hosts` for the "hosts whose
    policy changed" selection leg of the textless-signal re-fetch sweep.

Fields
------
``impersonate``   ``on`` | ``off`` | ``auto``. ``auto`` defers entirely to
                  the global flag (today's behaviour); ``on`` opts the host
                  into impersonation (archiver: on a counted block;
                  web_fetch: up front); ``off`` asks for never — and loses
                  to a global ``on``, per the rule above.
``http2``         ``bool | None`` — ALPN offer for this host's guarded
                  client. ``None`` (default) = the call site's own default.
``max_retries``   Extra attempts for transient failures (timeouts,
                  transport errors) — never for an edge verdict, which a
                  second identical ask cannot change.
``backoff_s``     Spacing between those attempts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from ._egress import FETCH_IMPERSONATE_ENV

#: ``FetchPolicy.impersonate`` vocabulary.
IMPERSONATE_ON = "on"
IMPERSONATE_OFF = "off"
IMPERSONATE_AUTO = "auto"
IMPERSONATE_VALUES = frozenset({IMPERSONATE_ON, IMPERSONATE_OFF, IMPERSONATE_AUTO})

#: Env values that mean "impersonation is OFF fleet-wide" — an operator who
#: writes ``LEGBA_FETCH_IMPERSONATE=off`` is switching the curl_cffi leg off
#: for every host, the registry's ``on`` rows included. Only an UNSET flag
#: defers to the registry.
_ENV_OFF_VALUES = frozenset({"off", "0", "false", "no"})


@dataclass(frozen=True)
class FetchPolicy:
    """The transport posture one host resolves to (module docstring)."""

    impersonate: str = IMPERSONATE_AUTO
    http2: bool | None = None
    max_retries: int = 0
    backoff_s: float = 0.0


#: What an unlisted host gets — today's behaviour, exactly.
DEFAULT_FETCH_POLICY = FetchPolicy()

#: THE REGISTRY. Seeded in code (house rule: machinery ships in code, data
#: ships as operator-entered records) with the two measured challenge-blocked
#: hosts. Suffix-matched, so ``www.`` and regional subdomains are covered.
_HOST_POLICIES: dict[str, FetchPolicy] = {
    # Cloudflare-managed challenge on every fetch — measured 403s against the
    # archiver's h1 client, every variant of the 2026-09-29 probe. The
    # impersonated fingerprint is the transport that answers.
    "timesofisrael.com": FetchPolicy(
        impersonate=IMPERSONATE_ON, http2=False, max_retries=1, backoff_s=1.0,
    ),
    # Same Cloudflare front, same measured refusal — 1,628 textless signals
    # in the last 30 days.
    "jpost.com": FetchPolicy(
        impersonate=IMPERSONATE_ON, http2=False, max_retries=1, backoff_s=1.0,
    ),
}


def host_of(url_or_host: str) -> str:
    """The registry-comparison host of a URL or bare host string.

    Lowercased, userinfo/port stripped; a malformed input degrades to the
    bare string so a weird value can never raise its way past the policy.
    """
    text = str(url_or_host or "").strip().lower()
    if not text:
        return ""
    try:
        parts = urlsplit(text if "://" in text else f"//{text}")
    except ValueError:
        return text
    return (parts.hostname or text).lower()


def fetch_policy_for(url_or_host: str) -> FetchPolicy:
    """The :class:`FetchPolicy` for a URL or host — the default when unlisted.

    Exact match first, then suffix (``www.timesofisrael.com`` →
    ``timesofisrael.com``); the LONGEST matching suffix wins so a more
    specific registration can never be shadowed by a parent one.
    """
    host = host_of(url_or_host)
    if not host:
        return DEFAULT_FETCH_POLICY
    if host in _HOST_POLICIES:
        return _HOST_POLICIES[host]
    best: str | None = None
    for registered in _HOST_POLICIES:
        if host.endswith(f".{registered}") and (
            best is None or len(registered) > len(best)
        ):
            best = registered
    return _HOST_POLICIES[best] if best is not None else DEFAULT_FETCH_POLICY


def policy_hosts() -> tuple[str, ...]:
    """The registered hosts — the re-fetch sweep's "policy changed" set."""
    return tuple(sorted(_HOST_POLICIES))


def _global_raw() -> str:
    """The global flag's normalised value (``""`` when unset)."""
    return os.environ.get(FETCH_IMPERSONATE_ENV, "").strip().lower()


def impersonate_kwarg(policy: FetchPolicy) -> bool | None:
    """The ``impersonate=`` a ``fetch_client`` call should carry, resolved.

    ``None`` = say nothing (the env decides — today's behaviour);
    ``True``/``False`` = an explicit override. The ordering rule lives here:

      * a global EXPLICITLY off (``off`` / ``0`` / ``false`` / ``no``) →
        ``False`` always — the fleet-wide kill switch beats every row;
      * a non-off GLOBAL (``on`` / ``fallback``) → ``None`` always. The env
        impersonates everywhere already, and no registry row may weaken it;
      * global unset + policy ``on`` → ``True`` — the ONE direction the
        registry may move;
      * global unset + policy ``off`` → ``False``;
      * global unset + policy ``auto`` → ``None``.
    """
    raw = _global_raw()
    if raw in _ENV_OFF_VALUES:
        return False
    if raw:
        return None
    if policy.impersonate == IMPERSONATE_ON:
        return True
    if policy.impersonate == IMPERSONATE_OFF:
        return False
    return None


__all__ = [
    "DEFAULT_FETCH_POLICY",
    "FetchPolicy",
    "IMPERSONATE_AUTO",
    "IMPERSONATE_OFF",
    "IMPERSONATE_ON",
    "IMPERSONATE_VALUES",
    "fetch_policy_for",
    "host_of",
    "impersonate_kwarg",
    "policy_hosts",
]
