# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared bearer-token resolution for bringup_*.py scripts.

TWO PRINCIPALS (X2-B1). The registry knows the fleet's SERVICE token
(``LEGBA_REGISTRY_SERVICE_TOKEN``) and the operator's own
(``LEGBA_REGISTRY_API_TOKEN``), stamps ``service:`` / ``operator:`` on every
row it writes, and refuses the service token on the human-only routes (the
vault, descriptor lifecycle changes by hand — ``_deps.HUMAN_ONLY_ROUTES``).

``resolve_token()`` (``principal="service"``, the default) — the bring-up
registrars that ``deploy.sh`` runs inside the stack:
  1. ``LEGBA_REGISTRY_SERVICE_TOKEN`` env var
  2. ``LEGBA_REGISTRY_SERVICE_TOKEN=...`` line in `.env` at the repo root
  3. the OPERATOR resolution below, with a ``principal_fallback`` warning on
     stderr — loud, never silent — so a deploy that has not set the new key
     keeps working while it says so.

``resolve_token(principal="operator")`` — scripts that touch a human-only
route (the vault loaders) or are run by hand:
  1. ``LEGBA_REGISTRY_TOKEN`` env var (legacy name some scripts still set)
  2. ``LEGBA_REGISTRY_API_TOKEN`` env var (the production name set in .env)
  3. ``LEGBA_REGISTRY_API_TOKEN=...`` line in `.env` at the repo root
     (auto-sourced so operators don't have to remember to export)
  4. ``"dev"`` (dev-mode fallback — registry only accepts this when no
     registry token is set on the registry side)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE_TOKEN_ENV = "LEGBA_REGISTRY_SERVICE_TOKEN"
OPERATOR_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"


def _env_file_value(key: str) -> str | None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return None
    try:
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
                val = val.strip("'\"")
                return val or None
    except OSError:
        return None
    return None


def _env_file_token() -> str | None:
    return _env_file_value(OPERATOR_TOKEN_ENV)


def _operator_token() -> str:
    return (
        os.environ.get("LEGBA_REGISTRY_TOKEN")
        or os.environ.get(OPERATOR_TOKEN_ENV)
        or _env_file_token()
        or "dev"
    )


def resolve_token(principal: str = "service") -> str:
    """Return the bearer token a bringup script should send to the registry."""
    if principal == "operator":
        return _operator_token()
    if principal != "service":
        raise ValueError(f"principal must be 'service' or 'operator', not {principal!r}")
    tok = (os.environ.get(SERVICE_TOKEN_ENV) or "").strip() or _env_file_value(SERVICE_TOKEN_ENV)
    if tok:
        return tok
    print(
        f"principal_fallback: {SERVICE_TOKEN_ENV} is unset (env and .env) — this "
        f"script presents the OPERATOR token ({OPERATOR_TOKEN_ENV}) and the "
        "registry will label its writes operator:. Set "
        f"{SERVICE_TOKEN_ENV} in .env.",
        file=sys.stderr,
    )
    return _operator_token()


__all__ = ["resolve_token"]
