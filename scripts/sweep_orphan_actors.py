# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-1 — one-shot orphan actor sweep (G1 cleanup).

Before the observed-state fix, lifecycle never propagated: descriptor edits
minted new actor ids (the id embeds ``content_hash[:16]``) while the old
actor — and its durable Dapr reminder — kept running, and retired/paused
descriptors kept polling. This script clears the accumulated pollution:

  1. Build the EXPECTED actor-id set from the registry's head descriptors
     (``_default_actor_id(kind, id, version)`` for every head whose state is
     active or paused).
  2. Enumerate EXISTING actor ids from two places:
       * Dapr's own actor records — ``dapr_state`` table in the dedicated
         ``dapr`` database (keys ``<app-id>||<ActorType>||<actor_id>||record``),
       * the runtime's observed-state table — ``public.actor_state`` in the
         substrate DB.
  3. Every existing id NOT in the expected set is an orphan: invoke its
     ``retire`` method through daprd (``PUT /v1.0/actors/<Type>/<id>/method/
     retire``). Post-A-1, ``retire()`` unregisters the actor's own durable
     reminder (``run_cadence`` / ``poll_<id>``) and marks its record retired.
     The observed-state row is then upserted to ``retired`` so the reconcile
     loop agrees.

Worker exemption: per-target analyst WORKERS share the id grammar
(``analyst::<id>::<target_id>``) but carry no version and no reminder. A
candidate whose descriptor is an active head and whose tail is NOT a
16-char hex content-hash prefix is treated as a worker and skipped (it
lazily re-activates on the next fan-out; retiring it is churn, not cleanup).

Legacy ``run_source_*`` reminders on TargetActors (the L-205-retired poll
path) cannot be enumerated here — the scheduler owns them — but the A-1
reminder guard self-disarms them on their next fire; this script reports
that as a note, not a failure.

Default is DRY-RUN: prints the plan. Pass ``--apply`` to retire.

Env:
  LEGBA_REGISTRY_API_URL    (default http://localhost:8090)
  LEGBA_REGISTRY_API_TOKEN  (default "dev")
  LEGBA_DAPR_PG_CONNSTRING  DSN of the dedicated ``dapr`` DB (statestore.yaml);
                            omit to skip the dapr_state enumeration leg.
  LEGBA_DAPRD_HTTP          (default http://127.0.0.1:3500)
  LEGBA_DATA_PG_*           substrate DB (PostgresConfig.from_env defaults)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any

import asyncpg
import httpx

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.runtime.reconcile import _default_actor_id
from legba.runtime.state import ActorStateRecord, ActorStateStore

_ACTOR_TYPE_BY_KIND = {
    "target": "TargetActor",
    "analyst": "AnalystActor",
    "source": "SourceActor",
}
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_FAMILIES = ("target", "analyst", "source")


async def _fetch_heads(base_url: str, token: str) -> dict[str, dict[str, Any]]:
    """descriptor_id → {family, version, state} for every head descriptor."""
    heads: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=30) as c:
        for family in _FAMILIES:
            r = await c.get(
                f"{base_url}/api/v1/registry/descriptors",
                params={"family": family, "head_only": "true", "limit": 500},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            for row in r.json():
                heads[row["descriptor_id"]] = {
                    "family": row["family"],
                    "version": row["version"],
                    "state": row["state"],
                }
    return heads


async def _existing_from_dapr_state(dsn: str) -> dict[str, str | None]:
    """actor_id → record lifecycle (or None) from Dapr's own state table."""
    out: dict[str, str | None] = {}
    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            "SELECT key, value FROM dapr_state WHERE key LIKE '%||record'"
        )
    finally:
        await conn.close()
    for r in rows:
        parts = r["key"].split("||")
        # <app-id>||<ActorType>||<actor_id>||record
        if len(parts) != 4 or parts[1] not in _ACTOR_TYPE_BY_KIND.values():
            continue
        actor_id = parts[2]
        lifecycle: str | None = None
        raw = r["value"]
        try:
            rec = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if isinstance(rec, dict):
                lifecycle = rec.get("lifecycle")
        except Exception:
            pass
        out[actor_id] = lifecycle
    return out


def _classify(
    actor_id: str,
    lifecycle: str | None,
    heads: dict[str, dict[str, Any]],
    expected: set[str],
) -> tuple[str, str]:
    """→ (verdict, reason); verdict ∈ {keep, skip, retire}."""
    if actor_id in expected:
        return "keep", "matches an active/paused head"
    if lifecycle == "retired":
        return "skip", "already retired"
    parts = actor_id.split("::", 2)
    if len(parts) < 2 or parts[0] not in _ACTOR_TYPE_BY_KIND:
        return "skip", "unrecognized id grammar"
    kind, descriptor_id = parts[0], parts[1]
    tail = parts[2] if len(parts) == 3 else ""
    head = heads.get(descriptor_id)
    if (
        kind == "analyst"
        and head is not None
        and head["state"] == "active"
        and head["family"] == "analyst"
        and not _HEX16.match(tail)
    ):
        return "skip", "per-target worker of an active analyst"
    if head is None:
        return "retire", "descriptor no longer in registry"
    if head["state"] not in ("active", "paused"):
        return "retire", f"descriptor head state={head['state']}"
    return "retire", (
        f"version drift (head={head['version'][:16]}, actor tail={tail or '?'})"
    )


async def _retire_via_daprd(daprd: str, actor_id: str) -> str | None:
    kind = actor_id.split("::", 1)[0]
    actor_type = _ACTOR_TYPE_BY_KIND[kind]
    url = f"{daprd}/v1.0/actors/{actor_type}/{actor_id}/method/retire"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.put(url, json={})
        if r.status_code >= 300:
            return f"HTTP {r.status_code}: {r.text[:200]}"
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retire orphaned Dapr actors (stale versions, deleted/"
        "retired descriptors) and unregister their reminders.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually retire (default: dry-run report)",
    )
    args = parser.parse_args()

    registry_url = os.environ.get(
        "LEGBA_REGISTRY_API_URL", "http://localhost:8090",
    ).rstrip("/")
    token = os.environ.get("LEGBA_REGISTRY_API_TOKEN") or "dev"
    daprd = os.environ.get("LEGBA_DAPRD_HTTP", "http://127.0.0.1:3500").rstrip("/")
    dapr_dsn = os.environ.get("LEGBA_DAPR_PG_CONNSTRING", "")

    heads = await _fetch_heads(registry_url, token)
    expected: set[str] = set()
    for descriptor_id, h in heads.items():
        if h["state"] in ("active", "paused"):
            expected.add(
                _default_actor_id(h["family"], descriptor_id, h["version"])
            )
    print(f"heads: {len(heads)} descriptors, {len(expected)} expected live actor ids")

    candidates: dict[str, str | None] = {}
    if dapr_dsn:
        candidates.update(await _existing_from_dapr_state(dapr_dsn))
        print(f"dapr_state: {len(candidates)} actor records")
    else:
        print("dapr_state: SKIPPED (LEGBA_DAPR_PG_CONNSTRING not set)")

    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    try:
        state_store = ActorStateStore(store.pool)
        await state_store.ensure_schema()
        async with store.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT actor_id, lifecycle FROM public.actor_state"
            )
        for r in rows:
            candidates.setdefault(r["actor_id"], r["lifecycle"])
        print(f"candidates total (dapr_state ∪ actor_state): {len(candidates)}")

        verdicts: dict[str, list[tuple[str, str]]] = {
            "keep": [], "skip": [], "retire": [],
        }
        for actor_id, lifecycle in sorted(candidates.items()):
            verdict, reason = _classify(actor_id, lifecycle, heads, expected)
            verdicts[verdict].append((actor_id, reason))

        for verdict in ("keep", "skip", "retire"):
            print(f"\n== {verdict.upper()} ({len(verdicts[verdict])})")
            for actor_id, reason in verdicts[verdict]:
                print(f"  {actor_id}  — {reason}")

        if not args.apply:
            print(
                "\nDRY-RUN — nothing retired. Re-run with --apply to retire "
                f"{len(verdicts['retire'])} orphan(s)."
            )
            return 0

        failures = 0
        for actor_id, reason in verdicts["retire"]:
            err = await _retire_via_daprd(daprd, actor_id)
            if err is not None:
                failures += 1
                print(f"RETIRE FAILED {actor_id}: {err}")
                continue
            parts = actor_id.split("::", 2)
            rec = await state_store.get(actor_id) or ActorStateRecord(
                actor_id=actor_id,
                actor_kind=parts[0],
                descriptor_id=parts[1] if len(parts) > 1 else actor_id,
                descriptor_version=parts[2] if len(parts) > 2 else "",
                lifecycle="retired",
            )
            rec.lifecycle = "retired"
            rec.last_outcome = "sweep_orphan_actors"
            await state_store.upsert(rec)
            print(f"retired {actor_id}  ({reason})")

        print(
            f"\nDone: {len(verdicts['retire']) - failures} retired, "
            f"{failures} failed."
        )
        print(
            "Note: legacy run_source_* reminders (if any) self-disarm on "
            "their next fire via the A-1 reminder guard — they cannot be "
            "enumerated from here (scheduler-owned)."
        )
        return 1 if failures else 0
    finally:
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
