# Rolling deploy

How to ship a code change to an already-provisioned Legba stack. This is
**not** bring-up — see [`deploy/deploy.sh`](../../deploy/deploy.sh) and
[RUNBOOK.md §2](../RUNBOOK.md#2-bring-up-everything-canonical-container-mode)
for taking a stack from empty volumes to boot-verified. This page and its
script, [`scripts/rolling_deploy.sh`](../../scripts/rolling_deploy.sh), cover
the far more common case: the substrate is already up, code changed, and
three images need to go out in an order that does not silently break
anything.

## The sequence

1. **Build the registry and runtime images.**
   `docker compose build legba-registry legba-runtime-dapr`
2. **Build the worker AFTER the runtime**, with both profiles active, no
   cache:
   `COMPOSE_PROFILES=runtime,dapr-workflow docker compose build --no-cache legba-dapr-workflow-worker`
3. **Guard.** Every one of the three images' `docker inspect -f
   '{{.Created}}'` must be younger than a threshold (default 30 minutes,
   `--max-image-age-min`) or the script aborts before touching a single
   container.
4. **Recreate in order, each waited-healthy before the next starts:**
   registry → migrate → runtime → worker.
   Migration runs as a one-off container:
   `docker compose run --rm --no-deps -e LEGBA_DATA_PG_DB=legba --entrypoint python legba-registry -m legba.data.migrate`
5. **Verify markers inside all three containers** — not just the one that
   was supposed to change. A file or grep pattern is checked at
   `/install/lib/python3.11/site-packages/legba/<relpath>` in the registry,
   runtime, and worker containers alike; any miss names the container and
   fails the deploy.
6. **Optionally roll the UI.** `--ui` builds and deploys
   `legba-ui-build` under `COMPOSE_PROFILES=runtime,ui`, then restarts
   `legba-caddy` so the new static bundle is actually served.

`scripts/rolling_deploy.sh --help` has the full flag reference
(`--verify-file`, `--verify-grep`, `--dry-run`, `--skip-build`,
`--max-image-age-min`, `--ui`) and the exit-code table.

## Why the registry always goes first

There is no `--registry-only-if-needed` flag, and there will not be one.
The registry and the runtime/worker share one Pydantic schema layer. A
runtime recreated against a registry still serving the OLD shape sees a
silent `/typed` 500 on the first request that touches a changed field — it
reads like network noise, not a schema mismatch, to most callers, and
nothing pages on it. Registry-first, waited healthy before the runtime
moves, is the only ordering that cannot hit this.

## The two traps

**Trap 1 — the worker image is `FROM` the runtime image, and staleness
hides.** `legba-dapr-workflow-worker`'s Dockerfile builds `FROM
legba/legba-runtime-dapr`. If the runtime image is rebuilt and the worker
is not rebuilt *after* it, the worker keeps running the OLD runtime
layer's code — under a worker image whose own build timestamp looks
perfectly fresh. A log line proves the process started, not that the code
it started is current. The only real proof is `docker exec … grep` for a
specific marker *inside* the worker's site-packages, which is exactly what
step 5 above (and `--verify-file` / `--verify-grep`) does. This is why the
guard in step 3 checks image `Created` timestamps, never log output — see
"the guard is on images, never on logs" in the script's own header.

**Trap 2 — the profile-less worker build fails and looks like nothing
happened.** `docker compose build legba-dapr-workflow-worker` with no
`COMPOSE_PROFILES` set fails with `no such service: dapr-placement` — a
sibling service the worker's dependency graph needs, gated behind the same
profile — and some compose versions print nothing that reads as an error
if the caller doesn't check the exit code. The worker build **and**
recreate must run with `COMPOSE_PROFILES=runtime,dapr-workflow` set. The
registry and runtime builds don't need this (their own dependency graphs
don't reach a profile-gated sibling the same way), which is precisely why
this trap is easy to miss — it only bites the third image, and only on
the step everyone assumes is the safe repeat of the first two.

## The guard

`scripts/rolling_deploy.sh` refuses to recreate anything until all three
images (`legba/legba-registry:latest`, `legba/legba-runtime-dapr:latest`,
`legba/legba-dapr-workflow-worker:latest`) pass the age check. This catches
the case where a build step silently no-op'd (cache hit, wrong profile,
build failed but the script kept going) and an operator would otherwise be
about to "deploy" an image that is actually last week's. `--skip-build`
does not waive the guard — it only skips the build step itself; whatever
images already exist still have to be fresh, which is the entire point of
having `--skip-build` at all (redeploying a known-good image without
rebuilding it).
