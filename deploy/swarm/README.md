# deploy/swarm/ — Docker Swarm conversion DRAFTS

> **THESE ARE DRAFTS. THEY ARE NOT DEPLOYABLE YET. NOTHING HERE IS RUNNING.**
>
> No swarm has been initialized, no stack has been deployed, no network or
> volume has been created from these files. Phase-1 (single-node) validation
> is **pending** — do not `docker stack deploy` anything in this directory
> until the phased runbook in
> `planning/SWARM_CONVERSION_ASSESSMENT_2026-07-25.md` says so and the
> operator green-lights it. The live production path remains
> `docker-compose.yml` + `deploy/deploy.sh`, unchanged.

## What this is

Machinery for the assessed (paper-only, 2026-07-25) conversion of the Legba
compose stack to `docker stack deploy`, targeting a 2-node split:

| file | stack | node pin | contents |
|---|---|---|---|
| `stack-data.yml` | `legba-data` | `node.labels.legba.role == data` (node2) | redis, postgres/AGE, qdrant, opensearch, nats |
| `stack-runtime.yml` | `legba-runtime` | `node.labels.legba.role == runtime` (node1) | dapr-placement, dapr-scheduler, dapr-sidecar, legba-registry, legba-runtime-dapr, legba-dapr-workflow-worker |
| `stack-edge.yml` | `legba-edge` | `node.labels.legba.role == runtime` (node1) | legba-ui-build (one-shot), legba-caddy (80/443, host-mode) |
| `registry-service.yml` | `legba-infra` | manager (node1) | local `registry:2` on :5000 (routing mesh) — because stack deploy ignores `build:` |

All three app stacks join one **external attachable overlay** (`legba-swarm`,
created by the runbook, not by these files) and use **network aliases** to
preserve the compose DNS names (`postgres`, `nats`, `legba-registry`, …) so no
service env changes: swarm's own service DNS would otherwise be
stack-prefixed (`legba-data_postgres`).

Deliberately **excluded** from swarm: `legba-mcp` (per-conversation
`docker run -i`, never a long-running service) and `legba-media`
(503-serving declared seam; stays compose-managed until a real backend
lands). `docker-compose.replicas.yml` scale-out is out of scope — gated by
DIRECTION.md §6 regardless of orchestrator.

## Validation status (what is and is not proven)

**Checked** (2026-07-25, Docker 29.2.1 / Compose v5.0.2, client-side renders
only — zero swarm state touched):

- All four files render clean through `docker stack config -c <file>` — the
  same loader `docker stack deploy` uses. This catches the stack-deploy
  schema (it hard-errors on `profiles:`, `mem_limit:` and long-form
  `depends_on:`, all of which the live compose uses and these drafts do not).
- All four also pass `docker compose -f <file> config --quiet` (compose-spec
  cross-check).
- `env_file:` baking, `${VAR}` shell-only interpolation, `$$` escaping,
  config-path resolution and network aliases were probe-verified against the
  live CLI (evidence in the assessment §1).

**NOT checkable without a swarm** (phase-1 items): whether `shm_size:` and
`ulimits:` are honored at the task level (tmpfs fallback for /dev/shm is
already in `stack-data.yml`); one-shot `restart_policy: none` semantics on
redeploy; daprd's address advertisement to placement over the overlay;
healthcheck-driven task restart behavior; config/secret rotation mechanics;
actual `depends_on`-free boot convergence (registry/runtime currently
crash-loop until their dependencies are up — see below).

## Known gaps these drafts do NOT hide

1. **Entrypoint wait loops are NOT yet added.** Startup ordering moved out of
   `depends_on` (ignored by swarm) and must land in the images' entrypoints:
   `legba-registry` (wait for postgres/nats/redis/qdrant), `legba-runtime-dapr`
   (wait for registry + postgres + nats + sidecar),
   `legba-dapr-workflow-worker` (wait for sidecar + registry), caddy (wait for
   ui_dist, or better: bake the SPA into a derived caddy image). Until added,
   swarm's restart policy gives crash-loop convergence — functional, ugly.
2. **One-shots replaced by runbook steps**: `dapr-scheduler-init` (chown of
   the scheduler etcd dir) and `dapr-init-db` (`CREATE DATABASE dapr`) have no
   swarm equivalent with completion gating; they become documented
   pre-deploy steps.
3. **The B-1 loopback-only port perimeter is unrepresentable in swarm.**
   These drafts publish NOTHING except caddy 80/443 (host-mode) and the image
   registry :5000 (mesh, must be firewalled). Host tooling that assumed
   `127.0.0.1:<port>` — `scripts/backup.sh`, `scripts/host_stall_watchdog.sh`,
   `deploy/deploy.sh` — needs the rework described in the assessment §4f.
4. **Secrets ride `env_file` for now** — baked into service specs, visible in
   `docker service inspect`. Swarm-secrets migration is phase-3 work
   (assessment §4e).

## Where the full analysis lives

`planning/SWARM_CONVERSION_ASSESSMENT_2026-07-25.md` — per-service inventory,
what breaks under `stack deploy` and each fix, the Dapr-on-swarm risk
analysis, the k3s fallback triggers, network/latency + backup implications of
the data node, and the phase 1/2/3 runbook draft. DIRECTION.md §6 carries the
one-paragraph pointer.
