# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared bearer-token resolution for bringup_*.py scripts.

Resolution order (highest first):
  1. ``LEGBA_REGISTRY_TOKEN`` env var (legacy name some scripts still set)
  2. ``LEGBA_REGISTRY_API_TOKEN`` env var (the production name set in .env)
  3. ``LEGBA_REGISTRY_API_TOKEN=...`` line in `.env` at the repo root
     (auto-sourced so operators don't have to remember to export)
  4. ``"dev"`` (dev-mode fallback — registry only accepts this when
     ``LEGBA_REGISTRY_API_TOKEN`` is unset on the registry side)
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_file_token() -> str | None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return None
    try:
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("LEGBA_REGISTRY_API_TOKEN="):
                val = line.split("=", 1)[1].strip()
                val = val.strip("'\"")
                return val or None
    except OSError:
        return None
    return None


def resolve_token() -> str:
    """Return the bearer token a bringup script should send to the registry."""
    return (
        os.environ.get("LEGBA_REGISTRY_TOKEN")
        or os.environ.get("LEGBA_REGISTRY_API_TOKEN")
        or _env_file_token()
        or "dev"
    )


__all__ = ["resolve_token"]
