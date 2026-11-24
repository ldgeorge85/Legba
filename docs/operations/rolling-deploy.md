# Rolling deploy

How to ship a code change to an already-provisioned Legba stack. This is
**not** bring-up — see [`deploy/deploy.sh`](../../deploy/deploy.sh),
[SETUP.md](../SETUP.md) and [RUNBOOK.md, "Stand up a fresh instance"](../RUNBOOK.md#stand-up-a-fresh-instance)
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
7. **After every UI deploy, walk the live console.** Run the first-time-user
   browser walk in live mode against what is now served:
   `LEGBA_TOKEN="$(…the registry bearer…)" legba-ui-v3/tools/walk/run.sh live`,
   or with sign-in on, as a named account through the real sign-in screen with
   `LEGBA_WALK_EMAIL` and `LEGBA_WALK_PASSWORD_FILE` (GET-only apart from
   sign-in; the token or password is read from the environment or the file and
   never printed). Keep
   the report (`/tmp/legba-walk/live-<utc>/report.txt`) with the roll; a
   `WALK_VERDICT FAIL` is a regression to fix or roll back, named budget by
   budget ([UI.md](../UI.md#the-browser-walk-every-ui-change-is-walked-before-it-ships)).
   The gate already walked the same bundle on synthetic fixtures before the
   build; the live walk is the check against real data and real latency.

`scripts/rolling_deploy.sh --help` has the full flag reference
(`--verify-file`, `--verify-grep`, `--dry-run`, `--skip-build`,
`--max-image-age-min`, `--ui`, `--tier`, `--walk`, `--drain-seconds`,
`--ignore-nightly`) and the exit-code table; [RUNBOOK.md §2](../RUNBOOK.md#2-deploy)
summarises them.

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
build failed but the script kept going) and whoever runs the roll would otherwise be
about to "deploy" an image that is actually last week's. `--skip-build`
does not waive the guard — it only skips the build step itself; whatever
images already exist still have to be fresh, which is the entire point of
having `--skip-build` at all (redeploying a known-good image without
rebuilding it).

## Deploy tiers

Not every change needs the full path. `scripts/deploy_tier.py` reads the range
from the deployed SHA to the rolled SHA and assigns the **highest** tier any
changed path needs; the actions are the union over all paths. The rules are
data (`PATH_RULES`, `BACKSTOPS` in the script) and
`tests/data_pkg/test_deploy_tier.py` reviews them. **When in doubt it is full**:
an unknown path, a module no service's import graph reaches, a deleted module,
no deployed SHA on record, or a classifier error all mean full.

| Tier | Triggered by | Gate | Action | Restart |
|---|---|---|---|---|
| `t0` | docs, tests, host scripts under `scripts/` (cron runs them from the checkout, so merging installs them), `deploy/cron.d` (copied to `/etc/cron.d` by hand), models-host code, example descriptors | the changed tests, the tests naming a changed script, the guard set, ruff | none: merge only; the roll records the SHA | no |
| `t1` | `action_pack` / `source` / `target` / `analyst` descriptor YAML (family read from `schema_uri`), vocabulary seeds, the lifecycle overlay (`LIFECYCLE.d/` rows, or a row left in `LIFECYCLE.md`), `RETIRED.md`, the profile's source catalog | the walker test (registers the whole tree into the test DB through the real registry), profile, bring-up and reference-resolution tests, `tests/descriptors/`, tests naming the descriptor, the guard set | the walker for the changed families only; then the first staleness sweep (one runtime restart only if a pack binding is stale) and a check for `deps_resolver.parse_failed` | only if a pack is stale |
| `t2` | `legba-ui-v3/**` | `tsc`, `vitest`, `lint:tokens`, `lint:absence`, then the browser walk in fixture mode (`legba-ui-v3/tools/walk/run.sh fixture --build`), the guard set | the UI build + caddy restart, then a live-mode walk (step 7) | caddy only |
| `t3` | Python that the import graph shows only ONE service reaches (the registry, or the runtime with its FROM-runtime worker), not on a protected path and not a write path | every test whose import closure reaches a changed module or names it, the slim-image probes when registry code changed, the guard set, ruff | rebuild and recreate only that service (runtime ⇒ worker rebuilt after it); no migration | that service |
| `full` | migrations, `schemas/`, `shared/`, the runtime core (actors, source actor, pipeline, reconcile, dispatcher, subscriptions, triggers, jobs, workflows), archive/store and substrate clients, output sinks, registry descriptor store and auth, any module with an SQL write or a bus publish, Python both services import, prompt packages, Dockerfiles, `pyproject.toml`, compose, `dapr/`, other descriptor families, and anything unplaced | the full ordered suite (+ UI checks and the fixture walk if UI changed) | today's rolling deploy | all three |

**A `t1` walker run is reconciled by the resync, not by an event.** The walker
registers direct-DB without a bus connection, so it emits no `descriptor.*`
event; the runtime creates or re-versions the actor at its next periodic
resync, up to five minutes later ([RUNBOOK.md, "The descriptor walker"](../RUNBOOK.md#the-descriptor-walker)). Check for
`reconcile.action ... create_actor` after that window, not immediately.

**Module → service is derived, not listed.** The classifier parses every
module under `src/` (and the profile prompt packages) at the target revision,
read from git, and walks the imports from each console-script entry point in
`pyproject.toml`: function-level and `TYPE_CHECKING` imports count, a string
literal naming a module counts (importlib targets), an f-string prefix fans out
to its whole package, and every module a descriptor YAML names is a root of
both the registry and the runtime. Over-approximation only ever makes a module
look more shared. The graph is cached per tree hash in
`~/.cache/legba-deploy-tier`. Most modules are reached by both the registry
and the runtime (the registry imports the provenance package and, through
descriptor reference resolution, the analyst handlers); a smaller set by the
registry alone (route leaves), a few by the runtime and its worker alone, and
some by no service (script-only helpers: full, cannot place). So `t3` is in
practice a registry-route change or a runtime leaf.

**Backstops** (`BACKSTOPS`, applied to every roll, `auto` or explicit):

* the newest clean full-suite nightly record on this lineage is FAIL → nothing
  below full until a full gate passes (a newer nightly PASS, or a roll whose
  full gate passed);
* after 5 consecutive lower-tier rolls the next roll is full;
* more than 24 hours since the last passing full gate → full;
* a `t3` roll on a service whose `t3` gate failed since the last full roll → full.

**How to use it.** The default is unchanged: `--tier full`. To preview:

```bash
python3 scripts/deploy_tier.py classify            # deployed SHA..HEAD
python3 scripts/deploy_tier.py classify --range A..B
python3 scripts/deploy_tier.py decide --requested auto
scripts/run_targeted_gate.sh t3 A..B --dry-run     # the gate's selection
```

To roll by tier: `scripts/rolling_deploy.sh --tier auto` (or an explicit
`--tier t0|t1|t2|t3`). Below full, the roll runs `scripts/run_targeted_gate.sh`
itself before building anything; anything but `GATE_VERDICT … verdict=PASS`
stops it (exit 10). An explicit tier below the computed one is refused (exit 9)
unless `--force-tier` is passed, and the force is written to the deploy log
with who forced it (`LEGBA_DEPLOY_ACTOR`, else the login). `auto` resolving to
full runs the full gate itself and adds the walk and the UI step when the range
needs them. To make `auto` the default, change
`TIER_REQUESTED="full"` to `"auto"` in `scripts/rolling_deploy.sh` (or passes
`--tier auto` on every roll).

**The deploy log.** Every real roll — success, refusal, gate failure or abort —
appends one line to `/var/log/legba_deploy.log` (`--deploy-log`,
`LEGBA_DEPLOY_LOG`):

```
DEPLOY_ROLL <utc> result=ok|refused|gate_fail|aborted exit=<n> from=<sha> to=<sha>
  tier_requested= tier_computed= tier_required= tier_used= forced=0|1 forced_by=
  backstops= gate=nightly-ledger|targeted|full gate_result= services= walk= ui=
  migrate= dry_run=0 actor=
```

Nothing recorded the deployed SHA before this log: its newest `result=ok` line
is the deployed SHA the classifier ranges from, so the FIRST tiered roll has no
range and is full. Dry runs print the line and write nothing.

**What a lower tier skips, and the risk it carries.** `t0`–`t2` restart no
backend at all. `t3` skips the migration and the services the change does not
reach; a runtime-only `t3` leaves the registry running its old image, which is
safe only because the classifier proved the registry imports none of the
changed modules — a dynamic import the graph cannot see (a computed module
name) is the residual risk, and the gate's tests are the second line. Every
lower tier skips the full suite; the nightly suite stays the backstop and a red
nightly forces the next roll full.

## The nightly suite

`scripts/host_nightly_suite.sh`, run from host cron (`deploy/cron.d/legba-nightly-suite`,
installed by hand), runs three phases in the isolated test stack: lint, the
whole suite in file order, and the whole suite again in a shuffled order under a
logged seed. Then three read-only hygiene reports run (list-route weights
against their budgets, schema objects no migration declares, unread indexes);
they are logged and never gate. Known rig-only failures sit on an allowlist in
the script, each with its reason; anything else fails the night. It appends one `NIGHTLY_VERDICT` line per run to
`/var/log/legba_nightly_suite.log` and posts the verdict to its own ntfy topic, `nightly`.
The roll reads that ledger before building anything (RUNBOOK §2, "The nightly
gate runs first"), and a shuffled failure is replayed from the summary's `seed=`
line (RUNBOOK §3).
