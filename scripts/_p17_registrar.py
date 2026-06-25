# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-17 shared bring-up registrar — direct-DB descriptor registration.

The W6 cutover-prep bring-up scripts register the fresh source-first working
set directly against the migrated Postgres database (default
``legba_pivot_test`` on the dev rig), NOT through the REST registry server —
so the bring-up is deterministic about WHICH database it populates regardless
of which DB a running registry process happens to point at.

It wraps :class:`legba.data.registry.descriptor.DescriptorRegistry`, which owns
the full register path: pydantic validation, content-hash stamping, vocabulary
validation (for targets), audit-log write, and the head-row insert.

Idempotency: every helper is re-runnable.  A descriptor whose head row already
carries the SAME content hash is reported ``unchanged``; a changed body is
``update()``-d (a new version, prior version demoted from head); a brand-new id
is ``register()``-ed.

DB selection (env, both forms honored by PostgresConfig.from_env):
  * ``LEGBA_DATA_PG_DB`` / ``POSTGRES_DB``       — default ``legba`` upstream;
    bring-up scripts default it to ``legba_pivot_test`` if unset.
  * ``LEGBA_DATA_PG_HOST`` / ``LEGBA_DATA_PG_PORT`` / ``..._USER`` / ``..._PASSWORD``
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Default the bring-up DB to the pivot test DB unless the operator overrode it.
# Must happen before PostgresConfig.from_env() reads the environment.
os.environ.setdefault("LEGBA_DATA_PG_DB", "legba_pivot_test")

from collections import deque  # noqa: E402

from legba.data.postgres import PostgresStore  # noqa: E402
from legba.data.registry.descriptor import (  # noqa: E402
    DescriptorRegistry,
    Family,
    VersionConflict,
)
from legba.data.schemas import content_hash  # noqa: E402
from legba.data.schemas.lifecycle import ALLOWED_TRANSITIONS, LifecycleState  # noqa: E402

ACTOR = "p17_reregister"


def _legal_path(src: LifecycleState, dst: LifecycleState) -> list[LifecycleState] | None:
    """Shortest legal transition path src -> dst over ALLOWED_TRANSITIONS.

    Returns the list of intermediate+final states to step through (excluding
    src), or None if dst is unreachable (e.g. anything out of the terminal
    ``retired`` state).  Used so a fresh-instance bring-up that declares
    ``state=active`` can advance a stale ``draft`` head along the legal
    draft -> configured -> active chain instead of failing on the FSM guard.
    """
    if src == dst:
        return []
    seen = {src}
    queue: deque[list[LifecycleState]] = deque([[src]])
    while queue:
        path = queue.popleft()
        for nxt in ALLOWED_TRANSITIONS[path[-1]]:
            if nxt in seen:
                continue
            new_path = path + [nxt]
            if nxt == dst:
                return new_path[1:]
            seen.add(nxt)
            queue.append(new_path)
    return None


@dataclass
class RegisterResult:
    family: str
    descriptor_id: str
    action: str          # registered / updated / unchanged / failed
    version: str
    detail: str = ""


async def open_registry() -> tuple[PostgresStore, DescriptorRegistry]:
    pg = PostgresStore.from_env()
    await pg.connect()
    reg = DescriptorRegistry(pg)
    await reg.start()
    return pg, reg


async def close_registry(pg: PostgresStore, reg: DescriptorRegistry) -> None:
    await reg.stop()
    await pg.close()


async def _head(pg: PostgresStore, family: Family, descriptor_id: str) -> tuple[str, str] | None:
    """Return (version, state) of the head row, or None if absent."""
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT version, state FROM {family.table} "
            f"WHERE descriptor_id = $1 AND is_head LIMIT 1",
            descriptor_id,
        )
    return (row["version"], row["state"]) if row else None


def _with_state(descriptor: Any, state: LifecycleState) -> Any:
    """Return a copy of the descriptor model with identity.state replaced."""
    return descriptor.model_copy(
        update={"identity": descriptor.identity.model_copy(update={"state": state})}
    )


async def register_descriptor(
    pg: PostgresStore,
    reg: DescriptorRegistry,
    *,
    family: Family,
    descriptor: Any,
) -> RegisterResult:
    """Idempotently register/update one validated descriptor model.

    The descriptor is already a parsed pydantic model (the caller validated it
    against the real schema).  We compute the body's content hash to decide
    register vs. update vs. unchanged so re-runs are no-ops.
    """
    descriptor_id = descriptor.identity.id
    declared_state = LifecycleState(descriptor.identity.state)
    new_hash = content_hash(descriptor)
    head = await _head(pg, family, descriptor_id)

    try:
        # Brand-new id — register (the core registry lands every new descriptor
        # in its initial DRAFT state), THEN advance to the descriptor's declared
        # state along the legal FSM path. Without this, a fresh cold-start that
        # declares e.g. `state: active` (every G20 target, the analyst set) left
        # the head stuck at draft — the FSM walk further down only fired on
        # RE-registration of an existing draft head, so the bug was invisible on
        # re-runs and only bit the very first boot.
        if head is None:
            row = await reg.register(descriptor, actor=ACTOR)
            post = await _head(pg, family, descriptor_id)
            post_state = LifecycleState(post[1]) if post else LifecycleState.DRAFT
            if declared_state == post_state:
                return RegisterResult(family.value, descriptor_id, "registered", row.version)
            path = _legal_path(post_state, declared_state)
            if path is None:
                return RegisterResult(
                    family.value, descriptor_id, "failed", row.version,
                    detail=f"no legal FSM path {post_state.value} -> {declared_state.value}",
                )
            last_version = row.version
            for step_state in path:
                r2 = await reg.update(descriptor_id, _with_state(descriptor, step_state), actor=ACTOR)
                last_version = r2.version
            return RegisterResult(
                family.value, descriptor_id, "registered", last_version,
                detail=f"advanced draft -> {declared_state.value}",
            )

        cur_version, cur_state_str = head
        cur_state = LifecycleState(cur_state_str)

        # Already at the exact content + declared state — no-op.
        if cur_version == new_hash and cur_state == declared_state:
            return RegisterResult(family.value, descriptor_id, "unchanged", cur_version)

        # If the live state already matches the declared state, a single
        # update() (which carries live state when equal) lands the new body.
        if cur_state == declared_state:
            row = await reg.update(descriptor_id, descriptor, actor=ACTOR)
            return RegisterResult(family.value, descriptor_id, "updated", row.version)

        # Live state differs from declared. Walk the legal FSM path
        # (e.g. a stale draft head + a descriptor declaring active goes
        # draft -> configured -> active), updating the body+state at each
        # hop.  Each update() declaring the next legal state advances one
        # step; the final hop carries the canonical body.
        path = _legal_path(cur_state, declared_state)
        if path is None:
            return RegisterResult(
                family.value, descriptor_id, "failed", cur_version,
                detail=f"no legal FSM path {cur_state.value} -> {declared_state.value}",
            )
        last_version = cur_version
        for step_state in path:
            stepped = _with_state(descriptor, step_state)
            row = await reg.update(descriptor_id, stepped, actor=ACTOR)
            last_version = row.version
        return RegisterResult(family.value, descriptor_id, "updated", last_version)
    except VersionConflict as exc:
        h2 = await _head(pg, family, descriptor_id)
        return RegisterResult(
            family.value, descriptor_id, "unchanged",
            (h2[0] if h2 else "?"), detail=str(exc)[:120],
        )
    except Exception as exc:  # noqa: BLE001
        return RegisterResult(
            family.value, descriptor_id, "failed",
            (head[0] if head else "-"), detail=f"{type(exc).__name__}: {exc}"[:300],
        )


def print_results(title: str, results: list[RegisterResult]) -> int:
    print(title)
    failures = 0
    for r in results:
        mark = {"registered": "+", "updated": "~", "unchanged": "=", "failed": "!"}.get(r.action, "?")
        line = f"  {mark} {r.action:>10}  {r.family}/{r.descriptor_id}  @ {r.version[:16]}"
        if r.detail:
            line += f"   ({r.detail})"
        print(line)
        if r.action == "failed":
            failures += 1
    return failures


__all__ = [
    "RegisterResult",
    "ACTOR",
    "open_registry",
    "close_registry",
    "register_descriptor",
    "print_results",
    "Family",
]
