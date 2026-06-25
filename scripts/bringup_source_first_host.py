#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-CUT host bring-up harness — boot the source-first runtime planes.

The acceptance gate for the live deploy. This is the equivalent host-bootstrap
harness referenced in the P-CUT task: it brings up the SAME source-first planes
that ``legba.runtime.dapr_host.bring_up_production_runtime`` wires (job worker
pool, subscription/fan-out engine, coalescing trigger engine, action-pack
agency, SourceActor deps resolver) against the live dev rig — WITHOUT a daprd
sidecar — and prints the startup signposts.

What it proves:

  * The source-first plane wiring imports + assembles with no errors.
  * The job-plane work-queue stream + shared durable consumer are declared
    (NATS), and the job ledger schema is ensured (Postgres).
  * The subscription engine declares the shared ``legba_signals`` stream and
    binds per-target consumers for every active target's resolved+authorized
    source bindings.
  * The coalescing trigger engine builds its per-(analyst, target) registry +
    durable consumer.
  * The action-pack Agency is constructed over the live job queue.
  * The four planes shut down cleanly.

What it does NOT do (needs a real daprd sidecar): register the SourceActor /
TargetActor / AnalystActor types or drive the reconcile loop's ActorProxy
invocations. Those are exercised by ``legba-runtime-dapr`` against daprd (see
the runbook note printed at the end).

Usage
-----
    PYTHONPATH=src \\
    LEGBA_POSTGRES_DATABASE=legba_pivot_test \\
    LEGBA_NATS_URL=nats://127.0.0.1:4222 \\
    LEGBA_REGISTRY_API_URL=http://127.0.0.1:8090 \\
    python3 scripts/bringup_source_first_host.py [--run-seconds N]

Exit code 0 = clean boot + clean shutdown.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logger = logging.getLogger("bringup_source_first_host")


async def _run(run_seconds: float) -> int:
    from legba.data.config import NatsConfig, PostgresConfig
    from legba.data.nats import NatsStore
    from legba.data.postgres import PostgresStore
    from legba.data.registry.credentials import CredentialVault
    from legba.runtime.deps import StandardDeps
    from legba.runtime.registry_client import RegistryHTTPClient
    from legba.runtime.source_first_runtime import (
        AGENCY_HOLDER,
        bring_up_source_first_planes,
    )

    print("=" * 72)
    print("P-CUT host bring-up harness — source-first runtime planes")
    print("=" * 72)

    # ---- substrate ----------------------------------------------------
    pg_cfg = PostgresConfig.from_env()
    print(f"[signpost] postgres: {pg_cfg.host}:{pg_cfg.port}/{pg_cfg.database}")
    pg_store = PostgresStore(pg_cfg)
    await pg_store.connect()
    print("[signpost] postgres.connected")

    nats_cfg = NatsConfig.from_env()
    print(f"[signpost] nats: {nats_cfg.url}")
    nats_store = NatsStore(nats_cfg)
    await nats_store.connect()
    print("[signpost] nats.connected")

    registry_client = RegistryHTTPClient()
    print(f"[signpost] registry: {registry_client.base_url}")

    vault = CredentialVault(pg_store)

    async def _secrets_resolve(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    async def _nats_publish(subject: str, payload: bytes) -> None:
        await nats_store.publish_json(subject, payload)

    standard_deps = StandardDeps(
        pg_pool=pg_store.pool,
        nats_publish=_nats_publish,
        secrets_resolve=_secrets_resolve,
    )

    # ---- bring up the four planes -------------------------------------
    print("-" * 72)
    print("[signpost] bring_up_source_first_planes (run_loops=%s)"
          % (run_seconds > 0))
    handles = await bring_up_source_first_planes(
        pg_store=pg_store,
        nats_store=nats_store,
        standard_deps=standard_deps,
        registry_client=registry_client,
        run_loops=run_seconds > 0,
    )
    print("-" * 72)
    print("[ok] job plane     : stream=%s durable=%s workers=%d"
          % (handles.job_queue.stream, handles.job_queue.durable,
             len(handles.worker_pool.workers)))
    print("[ok] subscription  : targets_wired=%d"
          % len(handles.registered_targets))
    if handles.registered_targets:
        print("                     %s" % ", ".join(handles.registered_targets))
    print("[ok] trigger engine: registrations=%d"
          % handles.trigger_registrations)
    print("[ok] agency        : present=%s tool_context_queue=%s"
          % (AGENCY_HOLDER.get("agency") is not None,
             AGENCY_HOLDER.get("tool_context") is not None
             and AGENCY_HOLDER["tool_context"].queue is not None))

    # Job queue depth (proves the consumer is bound + queryable).
    try:
        pending = await handles.job_queue.consumer_pending()
        print("[ok] job queue depth: %d" % pending)
    except Exception as exc:
        print("[warn] job queue depth probe failed: %s" % exc)

    rc = 0
    if run_seconds > 0:
        print("-" * 72)
        print("[signpost] loops running for %.1fs ..." % run_seconds)
        await asyncio.sleep(run_seconds)
        print("[signpost] worker_pool.total_processed=%d"
              % handles.worker_pool.total_processed)

    # ---- clean shutdown -----------------------------------------------
    print("-" * 72)
    print("[signpost] stopping planes ...")
    await handles.stop()
    await registry_client.close()
    await nats_store.close()
    await pg_store.close()
    print("[signpost] all planes stopped + substrate closed")
    print("=" * 72)
    print("RESULT: clean boot + clean shutdown")
    print("=" * 72)
    print()
    print("Production bring-up (needs a daprd sidecar):")
    print("  docker compose --profile dapr up -d   # daprd routes to :6090")
    print("  PYTHONPATH=src legba-runtime-dapr      # main() boots the full host")
    print("Watch for the startup signposts:")
    print("  dapr_host.actor_types.registered types=[... 'SourceActor' ...]")
    print("  source_first.job_plane.ready / subscription_engine.ready")
    print("  source_first.trigger_engine.consumer_ready")
    print("  source_first.agency.ready")
    print("  dapr_host.source_first.ready targets_wired=N trigger_regs=M")
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--run-seconds", type=float, default=0.0,
        help="If >0, launch the worker-pool + trigger-engine loops for N "
             "seconds before shutting down (proves the loops run clean).",
    )
    ap.add_argument("--log-level", default=os.getenv("LEGBA_LOG_LEVEL", "INFO"))
    args = ap.parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        rc = asyncio.run(_run(args.run_seconds))
    except Exception:
        logger.exception("bring-up harness FAILED")
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()
