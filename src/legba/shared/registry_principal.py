# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry's two principals — the fleet's SERVICE token, the operator's own.

Until X2-B1 one bearer did everything: caddy's basic-auth perimeter
injected ``LEGBA_REGISTRY_API_TOKEN`` upstream for every viewer, and the
runtime, the workflow worker, the bring-up registrars and the scripts all
presented that same token. Every audit row said "bearer:<hash>" and no row
could say WHO — the operator at a curl, or the fleet on a schedule.

Now there are two credentials:

  * ``LEGBA_REGISTRY_SERVICE_TOKEN`` — the FLEET's. The runtime (actors, the
    scheduler, the watchdogs, the resync), the Dapr workflow worker and the
    bring-up registrars that run inside the stack present it. The registry
    stamps ``service:`` on every row it writes for it and refuses it on the
    human-only routes (``_deps.HUMAN_ONLY_ROUTES``).
  * ``LEGBA_REGISTRY_API_TOKEN`` — the OPERATOR's: their curl, the UI
    perimeter (caddy injects it), the MCP surface an operator's client drives,
    the by-hand tools. Stamped ``operator:``.

FALLBACK, NEVER SILENT. A fleet process whose service token is unset falls
back to the operator token so a deploy that has not set the new key keeps
running — but every fallback increments ``principal_fallback_count()`` and
logs ``principal_fallback`` (the first time, then every
``FALLBACK_LOG_EVERY``-th), and :func:`check_service_principal` logs an ERROR
at startup naming the key to set. The registry then labels those calls
``operator:``, which is the truth about the credential presented.

A LEAF: stdlib only. The runtime, the workflow worker, the scripts and the
registry gate (``data/registry/_deps.py``) all import the names below, so
there is exactly one spelling of each env key and each principal.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Mapping


class _signin:  # noqa: N801 — a namespace, spelled like the module it mirrors
    """The four names this leaf needs from ``legba.shared.auth_signin`` (A-1),
    MIRRORED here because this module stays stdlib-only
    (``test_service_principal.test_deps_module_stays_a_leaf``); a drift test
    (``test_signin_switch_a1``) pins them equal to the source of truth."""

    SIGNIN_ENV = "LEGBA_AUTH_SIGNIN"
    SERVICE_NAME_ENV = "LEGBA_SERVICE_NAME"
    SERVICE_NAMES: tuple[str, ...] = ("registry", "runtime", "worker", "console")
    COMPONENT_SERVICE: dict[str, str] = {
        "registry": "registry",
        "runtime": "runtime",
        "dapr_workflow_worker": "worker",
        "worker": "worker",
        "console": "console",
    }

    @staticmethod
    def signin_enabled(environ: Mapping[str, str]) -> bool:
        return (environ.get(_signin.SIGNIN_ENV) or "").strip() == "1"

    @staticmethod
    def service_token_env(name: str) -> str:
        return f"LEGBA_SERVICE_TOKEN_{name.upper()}"

logger = logging.getLogger(__name__)

#: The fleet's credential (X2-B1).
SERVICE_TOKEN_ENV = "LEGBA_REGISTRY_SERVICE_TOKEN"
#: The operator's credential — the original single registry bearer.
OPERATOR_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"

#: The two principal kinds the registry stamps on its rows.
PRINCIPAL_SERVICE = "service"
PRINCIPAL_OPERATOR = "operator"
PRINCIPALS = (PRINCIPAL_SERVICE, PRINCIPAL_OPERATOR)

#: The log event + counter name for a fleet call that presented the operator
#: token because the service token is unset.
FALLBACK_EVENT = "principal_fallback"
#: Log the fallback on its first occurrence and then every N-th, so a resync
#: loop cannot flood the log — the counter itself counts every one.
FALLBACK_LOG_EVERY = 100

_lock = threading.Lock()
_fallbacks = 0
#: Which service this process is (A-1) — recorded by
#: :func:`check_service_principal`; read only when the sign-in switch is on.
_component_service: str | None = None


def role_for_actor(actor: str | None) -> str:
    """The audit ``actor_role`` for an audit actor label.

    The registry's gates stamp ``<principal>:bearer:<sha12>``
    (``_deps.principal_actor``), so the label's leading segment IS the
    principal kind: ``service:…`` -> ``service``, ``operator:…`` ->
    ``operator``. Anything else (a legacy ``bearer:…`` label, ``anonymous``, a
    by-hand reviewer name, ``None``) is ``operator`` — the role every such
    row was stamped with before the two principals existed.
    """
    kind = (actor or "").split(":", 1)[0] if ":" in (actor or "") else ""
    return kind if kind in PRINCIPALS else PRINCIPAL_OPERATOR


def _read(environ: Mapping[str, str], name: str) -> str | None:
    return (environ.get(name) or "").strip() or None


def operator_token(environ: Mapping[str, str] | None = None) -> str | None:
    """The operator's bearer (``LEGBA_REGISTRY_API_TOKEN``), or ``None``."""
    return _read(os.environ if environ is None else environ, OPERATOR_TOKEN_ENV)


def _record_fallback() -> int:
    global _fallbacks
    with _lock:
        _fallbacks += 1
        n = _fallbacks
    if n == 1 or n % FALLBACK_LOG_EVERY == 0:
        logger.warning(
            "%s count=%d — %s is unset; this fleet process is presenting the "
            "OPERATOR token (%s), so the registry labels its writes operator:. "
            "Set %s in .env and recreate the runtime.",
            FALLBACK_EVENT, n, SERVICE_TOKEN_ENV, OPERATOR_TOKEN_ENV,
            SERVICE_TOKEN_ENV,
        )
    return n


def service_token(environ: Mapping[str, str] | None = None) -> str | None:
    """The bearer a FLEET process presents to the registry.

    ``LEGBA_REGISTRY_SERVICE_TOKEN``; when unset, the operator token WITH a
    counted, logged ``principal_fallback``; ``None`` when neither is set (the
    registry's explicit dev mode accepts no bearer — never a made-up one).
    """
    env = os.environ if environ is None else environ
    if _signin.signin_enabled(env):
        own = own_service_token(env)
        if own is not None:
            return own
    tok = _read(env, SERVICE_TOKEN_ENV)
    if tok is not None:
        return tok
    human = _read(env, OPERATOR_TOKEN_ENV)
    if human is not None:
        _record_fallback()
    return human


def this_service(environ: Mapping[str, str] | None = None) -> str | None:
    """The service this process is (A-1): ``LEGBA_SERVICE_NAME`` when set to
    a known name, else what :func:`check_service_principal` recorded."""
    env = os.environ if environ is None else environ
    explicit = (_read(env, _signin.SERVICE_NAME_ENV) or "").lower()
    if explicit in _signin.SERVICE_NAMES:
        return explicit
    return _component_service


def own_service_token(environ: Mapping[str, str] | None = None) -> str | None:
    """This process's OWN credential, ``LEGBA_SERVICE_TOKEN_<NAME>`` (A-1), or
    ``None`` (unknown service, or the key is unset — the caller then falls
    back to the shared fleet token, exactly as with the switch off)."""
    env = os.environ if environ is None else environ
    name = this_service(env)
    if name is None:
        return None
    return _read(env, _signin.service_token_env(name))


def service_auth_headers(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """``{"Authorization": "Bearer <service token>"}``, or ``{}`` when no token."""
    tok = service_token(environ)
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def principal_fallback_count() -> int:
    """How many fleet resolutions in this process fell back to the operator token."""
    with _lock:
        return _fallbacks


def reset_principal_fallback_count() -> None:
    """Test hook — zero the in-process counter."""
    global _fallbacks
    with _lock:
        _fallbacks = 0


def check_service_principal(
    component: str, environ: Mapping[str, str] | None = None,
) -> str:
    """The startup check every fleet entrypoint runs once.

    Returns ``"service"`` when the service token is set and distinct from the
    operator token; otherwise logs at ERROR (loud, greppable as
    ``principal_fallback``) and returns ``"principal_fallback"`` (operator
    token in use), ``"indistinct"`` (both set to the same value — the audit
    trail cannot tell them apart) or ``"unset"`` (neither set).
    """
    env = os.environ if environ is None else environ
    global _component_service
    _component_service = _signin.COMPONENT_SERVICE.get(component, _component_service)
    if _signin.signin_enabled(env):
        own_key = _signin.service_token_env(this_service(env) or component)
        if own_service_token(env) is not None:
            logger.info("%s.principal=service (%s set)", component, own_key)
            return "service"
        logger.error(
            "%s.principal=shared — %s is on but %s is UNSET; this process "
            "presents the shared fleet token instead of its own. Set %s.",
            component, _signin.SIGNIN_ENV, own_key, own_key,
        )
    svc = _read(env, SERVICE_TOKEN_ENV)
    human = _read(env, OPERATOR_TOKEN_ENV)
    if svc is not None and svc != human:
        logger.info("%s.principal=service (%s set)", component, SERVICE_TOKEN_ENV)
        return "service"
    if svc is not None:
        logger.error(
            "%s.principal=indistinct — %s equals %s; the audit trail cannot "
            "tell the fleet from the operator. Give the fleet its own token.",
            component, SERVICE_TOKEN_ENV, OPERATOR_TOKEN_ENV,
        )
        return "indistinct"
    if human is not None:
        logger.error(
            "%s.%s — %s is UNSET; the fleet will present the operator token "
            "and every write it makes will be labelled operator:. Set %s in "
            ".env (a second, distinct token) and recreate.",
            component, FALLBACK_EVENT, SERVICE_TOKEN_ENV, SERVICE_TOKEN_ENV,
        )
        return "principal_fallback"
    logger.error(
        "%s.principal=unset — neither %s nor %s is set; registry calls carry "
        "no bearer (accepted only by an explicit LEGBA_DEV_MODE=1 registry).",
        component, SERVICE_TOKEN_ENV, OPERATOR_TOKEN_ENV,
    )
    return "unset"


__all__ = [
    "FALLBACK_EVENT",
    "FALLBACK_LOG_EVERY",
    "OPERATOR_TOKEN_ENV",
    "PRINCIPALS",
    "PRINCIPAL_OPERATOR",
    "PRINCIPAL_SERVICE",
    "SERVICE_TOKEN_ENV",
    "check_service_principal",
    "operator_token",
    "own_service_token",
    "principal_fallback_count",
    "reset_principal_fallback_count",
    "role_for_actor",
    "service_auth_headers",
    "service_token",
    "this_service",
]
