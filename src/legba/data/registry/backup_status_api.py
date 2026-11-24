# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/backup-status`` — when the deployment was last backed up,
and when a backup was last PROVEN by a restore drill.

Two fields, read from the status files the host scripts write on success only
(``scripts/backup.sh`` → ``last_backup.json``; ``scripts/restore_drill.sh`` →
``last_drill.json``) under ``LEGBA_BACKUP_STATE_DIR`` (compose mounts the host
directory read-only at the same path). A missing, unreadable or malformed file
is ``null`` — absence, never a fabricated time; the System Status panel renders
it as "never recorded".

Registry-slim: stdlib + fastapi + pydantic + ``.api`` only.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import pathlib
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

_ROUTE = "/system/backup-status"
STATE_DIR_ENV = "LEGBA_BACKUP_STATE_DIR"
DEFAULT_STATE_DIR = "/var/lib/legba/backup-state"


class BackupStatusOut(BaseModel):
    """ISO-8601 UTC timestamps of the last successful backup set and the last
    passing restore drill; ``null`` when none is recorded."""

    last_backup_at: Optional[str] = None
    last_drill_at: Optional[str] = None


def _read_stamp(path: pathlib.Path, field: str) -> Optional[str]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    val = raw.get(field) if isinstance(raw, dict) else None
    if not isinstance(val, str):
        return None
    try:
        ts = _dt.datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    return ts.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")


def read_backup_status(state_dir: str | os.PathLike | None = None) -> BackupStatusOut:
    root = pathlib.Path(state_dir or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR)
    return BackupStatusOut(
        last_backup_at=_read_stamp(root / "last_backup.json", "last_backup_at"),
        last_drill_at=_read_stamp(root / "last_drill.json", "last_drill_at"),
    )


def build_backup_status_router(deps: RegistryAPIDeps) -> APIRouter:
    """Read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["system"])

    @router.get(_ROUTE, response_model=BackupStatusOut)
    async def system_backup_status(
        principal: str = Depends(require_bearer),
    ) -> BackupStatusOut:
        return read_backup_status()

    return router
