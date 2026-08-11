# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared bring-up helpers for the REST-registry transport.

The ``scripts/bringup_*.py`` family registers descriptors two ways, and only
one of them had been factored:

  * **direct-DB** — :mod:`_p17_registrar`, adopted by 13 scripts. It wraps
    ``DescriptorRegistry`` and talks to Postgres, so the bring-up is
    deterministic about WHICH database it populates.
  * **REST** — an ``httpx`` client against the registry server. Never
    factored: 31 copies of ``_client``, 29 of ``_load_yaml``, 28 of
    ``_exists_head``, and 25 **byte-identical** copies of ``main()``.

This module is that second factoring. Behavior is carried over verbatim from
the duplicated copies — same requests, same create-only semantics, same
printed lines, same exit codes.

WHY NOT INSIDE ``_p17_registrar``: importing it sets a process-global
``LEGBA_DATA_PG_DB=legba_pivot_test`` at import time (two tests snapshot and
restore the environment around that import for exactly this reason) and pulls
in the whole ``legba`` package plus asyncpg. The REST scripts need neither,
and an operator tool pointed at a live registry must NOT silently acquire a
default database it never asked for. The two transports stay two modules.

Env:
  * ``LEGBA_REGISTRY_URL``   — see :func:`registry_base`.
  * ``LEGBA_REGISTRY_TOKEN`` — resolved by each script via ``_token``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx
import yaml

#: The dev-rig registry the bring-up scripts default to.
DEFAULT_REGISTRY_URL = "http://127.0.0.1:8090/api/v1/registry"

#: The descriptor tree, resolved relative to this file (``scripts/`` sibling).
DESCRIPTORS_DIR = Path(__file__).resolve().parent.parent / "descriptors"


def registry_base() -> str:
    """The registry base URL — ``LEGBA_REGISTRY_URL`` or the dev-rig default."""
    return os.environ.get("LEGBA_REGISTRY_URL", DEFAULT_REGISTRY_URL)


def registry_client(base: str, token: str, *, timeout: float = 30) -> httpx.Client:
    """A bearer-authenticated sync client for the registry REST surface."""
    return httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )


def load_yaml(name: str, *, stamp_version: bool = True) -> dict[str, Any]:
    """Read ``descriptors/<name>`` into a POST-able body.

    YAML carries a placeholder for the version; the registry stamps the real
    content hash, but pydantic-strict still wants a hex string in the
    ``[a-f0-9]{16,64}`` shape until then — hence the 16 zeros. Pass
    ``stamp_version=False`` for descriptor files that already carry a
    placeholder in that shape and should be posted untouched.
    """
    with open(DESCRIPTORS_DIR / name) as f:
        body = yaml.safe_load(f)
    if stamp_version:
        identity = body.setdefault("identity", {})
        identity["version"] = "0" * 16
    return body


def exists_head(client: httpx.Client, family: str, descriptor_id: str) -> bool:
    """True if a head row already exists for ``family/descriptor_id``.

    A non-200/404 answer is a bad registry, not an absent descriptor — it
    raises rather than being read as "not there" and re-POSTed.
    """
    r = client.get(f"/descriptors/{family}/{descriptor_id}")
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(
        f"GET head failed for {family}/{descriptor_id}: "
        f"{r.status_code} {r.text[:200]}"
    )


def register_create_only(
    to_register: Iterable[Sequence[str]],
    *,
    base: str,
    token: str,
    timeout: float = 30,
) -> int:
    """POST every ``(family, yaml_file, descriptor_id)`` whose head is absent.

    CREATE-ONLY and idempotent: a descriptor whose head row already exists is
    reported skipped and never re-POSTed, so a re-run cannot bump a version or
    overwrite an operator's live edit. A pre-check that raises is recorded as a
    failure for that descriptor and the loop continues — one unreachable id
    does not abandon the rest of the set.

    Returns the process exit code: 1 if anything failed, else 0.
    """
    with registry_client(base, token, timeout=timeout) as client:
        registered: list[tuple[str, str, str]] = []
        skipped: list[tuple[str, str]] = []
        failures: list[str] = []

        for family, fname, desc_id in to_register:
            try:
                if exists_head(client, family, desc_id):
                    skipped.append((family, desc_id))
                    continue
            except Exception as exc:
                failures.append(f"{family}/{desc_id}: pre-check {exc}")
                continue

            body = load_yaml(fname)
            r = client.post(f"/descriptors/{family}", json=body)
            if r.status_code not in (200, 201):
                failures.append(
                    f"{family}/{desc_id}: HTTP {r.status_code} {r.text[:500]}"
                )
                continue
            out = r.json()
            registered.append((family, desc_id, out.get("version", "?")[:12]))

        print("Registered:")
        for f, d, v in registered:
            print(f"  + {f}/{d} @ {v}")
        print("Skipped (head already present):")
        for f, d in skipped:
            print(f"  = {f}/{d}")
        if failures:
            print("Failures:")
            for s in failures:
                print(f"  ! {s}")
            return 1
        return 0


__all__ = [
    "DEFAULT_REGISTRY_URL",
    "DESCRIPTORS_DIR",
    "exists_head",
    "load_yaml",
    "register_create_only",
    "registry_base",
    "registry_client",
]
