# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retention-policy operator config — ``/api/v1/v3/retention-policies`` (C2 follow-on).

The read + edit surface over the ``retention_policies`` table (migration
0109): the ONE-JANITOR config both ``signals_retention`` and
``analyst_traces_retention`` read at run time instead of hand-rolling their
own TTL constant. Until now the table had **no CRUD route at all** — an
operator edited it by SQL (``DATA_MODEL.md``'s honest "known thin / inert
legs" note). This module closes that gap for the operator-tunable columns
only:

  * ``ttl_days`` / ``keep_classes`` / ``batch_size`` / ``enabled`` /
    ``description`` — the columns the 0109 migration header calls out as
    operator-driven.
  * ``policy_name`` (the PK, and the sub_handler name the sweep engine keys
    on), ``table_name``, and ``env_fallback_var`` (the specific ``LEGBA_*``
    env var name a target's own module hardcodes) are **never writable
    here** — they are the code-side pairing between a row and the Python
    adapter that reads it, not operator config. Renaming or repointing one
    through this route would silently orphan the sweep engine's lookup.

Conventions mirror ``collection_requirements_api`` (the disposition-only
precedent): bearer-gated via :func:`~legba.data.registry.api.require_bearer`;
validation errors are 422; **no POST and no DELETE** — every row here is
paired 1:1 with a Python retention adapter (``signals_retention.py`` /
``analyst_traces_retention.py``) that reads it by ``policy_name``, so there
is no such thing as an API-created row anything would ever read, and no such
thing as an unreferenced row this route could safely remove.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

_MAX_DESCRIPTION = 4000
_MAX_KEEP_CLASSES = 32
_MAX_KEEP_CLASS_LEN = 200


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class RetentionPolicyOut(BaseModel):
    policy_name: str
    table_name: str
    ttl_days: int
    keep_classes: list[str]
    batch_size: int
    enabled: bool
    env_fallback_var: Optional[str] = None
    description: Optional[str] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class RetentionPolicyUpdate(BaseModel):
    """The writable surface — every field optional (partial update); a field
    left unset keeps its current value. ``policy_name`` / ``table_name`` /
    ``env_fallback_var`` are not here on purpose (see module doc)."""

    ttl_days: Optional[int] = Field(default=None, ge=0)
    keep_classes: Optional[list[str]] = Field(default=None, max_length=_MAX_KEEP_CLASSES)
    batch_size: Optional[int] = Field(default=None, ge=1)
    enabled: Optional[bool] = None
    description: Optional[str] = Field(default=None, max_length=_MAX_DESCRIPTION)

    def validate_keep_classes(self) -> None:
        if self.keep_classes is None:
            return
        for c in self.keep_classes:
            if not c or len(c) > _MAX_KEEP_CLASS_LEN:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "keep_classes entries must be non-empty and "
                        f"<= {_MAX_KEEP_CLASS_LEN} chars; got {c!r}"
                    ),
                )


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_COLUMNS = """
    policy_name, table_name, ttl_days, keep_classes, batch_size, enabled,
    env_fallback_var, description, created_by, created_at, updated_at
"""

_LIST_SQL = f"SELECT {_COLUMNS} FROM retention_policies ORDER BY policy_name"

_GET_SQL = f"SELECT {_COLUMNS} FROM retention_policies WHERE policy_name = $1"

_UPDATE_SQL = f"""
    UPDATE retention_policies
       SET ttl_days     = COALESCE($2, ttl_days),
           keep_classes  = COALESCE($3::text[], keep_classes),
           batch_size    = COALESCE($4, batch_size),
           enabled       = COALESCE($5, enabled),
           description   = COALESCE($6, description),
           updated_at    = now()
     WHERE policy_name = $1
    RETURNING {_COLUMNS}
"""


def _policy_out(row: Any) -> RetentionPolicyOut:
    return RetentionPolicyOut(
        policy_name=str(row["policy_name"]),
        table_name=str(row["table_name"]),
        ttl_days=int(row["ttl_days"]),
        keep_classes=list(row["keep_classes"] or []),
        batch_size=int(row["batch_size"]),
        enabled=bool(row["enabled"]),
        env_fallback_var=row["env_fallback_var"],
        description=row["description"],
        created_by=str(row["created_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_retention_policies_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the retention-policies config router (mount under
    ``/api/v1/v3``)."""
    router = APIRouter(tags=["retention-policies"])

    @router.get("/retention-policies", response_model=list[RetentionPolicyOut])
    async def list_policies(
        principal: str = Depends(require_bearer),
    ) -> list[RetentionPolicyOut]:
        """Every policy row, alphabetical by name — the table is small
        (one row per retention target) so no pagination/filter is needed."""
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(_LIST_SQL)
        return [_policy_out(r) for r in rows]

    @router.get(
        "/retention-policies/{policy_name}", response_model=RetentionPolicyOut
    )
    async def get_policy(
        policy_name: str,
        principal: str = Depends(require_bearer),
    ) -> RetentionPolicyOut:
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(_GET_SQL, policy_name)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"retention policy {policy_name!r} not found",
            )
        return _policy_out(row)

    @router.patch(
        "/retention-policies/{policy_name}", response_model=RetentionPolicyOut
    )
    async def update_policy(
        policy_name: str,
        body: RetentionPolicyUpdate,
        principal: str = Depends(require_bearer),
    ) -> RetentionPolicyOut:
        """The only write this route allows: ``ttl_days`` / ``keep_classes`` /
        ``batch_size`` / ``enabled`` / ``description``, each independently
        optional. Never touches ``policy_name`` / ``table_name`` /
        ``env_fallback_var`` — the code-side pairing to a Python adapter."""
        body.validate_keep_classes()
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                _UPDATE_SQL,
                policy_name,
                body.ttl_days,
                body.keep_classes,
                body.batch_size,
                body.enabled,
                body.description,
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"retention policy {policy_name!r} not found",
            )
        logger.info(
            "retention_policies.updated policy_name=%s ttl_days=%s enabled=%s",
            row["policy_name"], row["ttl_days"], row["enabled"],
        )
        return _policy_out(row)

    return router


__all__ = [
    "RetentionPolicyOut",
    "RetentionPolicyUpdate",
    "build_retention_policies_router",
]
