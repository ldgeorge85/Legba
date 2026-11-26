<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Runbook

For the person running an instance: start and stop the stack, ship a change,
check health, routine tasks, known failures and their fixes, and cutting a
release. Standing an instance up from empty volumes is [SETUP.md](SETUP.md);
accounts and roles are
[Operating your instance §9](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in);
every knob and its value is [TUNABLES.md](TUNABLES.md); what is live, off by
default or not built is [STATUS.md](STATUS.md).

Commands run from the repository root. The shipped systemd units, cron files
and host scripts assume the checkout is at `/usr/local/deployments/active/legba`;
edit them if yours is elsewhere. The registry listens on `127.0.0.1:8090` on
the host. Its admin token works whether sign-in is on or off (with sign-in on,
every use is logged as break-glass):

```bash
REG=http://127.0.0.1:8090
TOKEN=$(grep '^LEGBA_REGISTRY_API_TOKEN=' .env | cut -d= -f2-)
```

---

## 1. Start, stop, restart

| Layer | Services | Profile |
|---|---|---|
| Substrate | `postgres` (the Apache AGE image; the extension is installed, the graph is dropped and nothing uses it), `redis`, `qdrant`, `nats`, `opensearch` | none — always on |
| Dapr control plane | `dapr-placement`, `dapr-scheduler-init`, `dapr-scheduler`, `dapr-init-db`, `dapr-sidecar` | `dapr`, pulled in by `runtime` |
| Application | `legba-registry`, `legba-runtime-dapr`, `legba-ui-build`, `legba-caddy` | `runtime` |
| Workflow worker | `legba-dapr-workflow-worker` | `dapr-workflow` |
| Optional lanes | `searxng`, `ntfy`, `rsshub`, `legba-mcp`, `legba-media` | `search`, `alerts`, `sources-extra`, `mcp`, `media` |

Caddy is the only service publishing on all interfaces (`:80`, `:443`); every
other host port binds `127.0.0.1`.

Two rules govern every start and restart, stated here once.

- **The control-plane containers move together, dependency-ordered**:
  `dapr-placement`, `dapr-scheduler`, `dapr-sidecar`, `legba-runtime-dapr`.
  Restarting the runtime alone leaves its actor-host registration stale inside
  the sidecar; every reminder and invocation then fails in the sidecar's log
  with nothing in the app's, so ingestion and analyst cadence go silent while
  every container still reports healthy.
- **The registry applies migrations and is healthy before the runtime starts.**
  The two share one schema layer, so a runtime talking to a registry serving the
  older shape gets a 500 from `/typed` on the first request touching a changed
  field, and the analyst behind it stops firing without a visible error.

```bash
# start
docker compose --profile runtime up -d
docker compose --profile runtime --profile dapr-workflow \
  --profile search --profile alerts --profile sources-extra up -d   # with lanes
docker compose up -d && docker compose --profile dapr up -d         # substrate + Dapr only

# stop
docker compose --profile runtime stop            # containers down, volumes kept
docker compose --profile runtime down            # containers removed, volumes kept
docker compose --profile runtime down --volumes  # destroys descriptors, signals, audit, vault, vector indices

# restart one service — fine for the registry, caddy, any substrate service
docker compose --profile runtime restart legba-registry

# restart the control plane, together
docker compose --profile runtime up -d --force-recreate \
  dapr-placement dapr-scheduler dapr-sidecar legba-runtime-dapr
```

`dapr-scheduler` holds an embedded etcd carrying every live reminder, which is
why compose gives it a 45-second `stop_grace_period`: docker's default
ten-second SIGTERM-to-SIGKILL can corrupt that etcd mid-write. Never put a
recreate of that container on a timer.

### Halt and resume the model-calling services

Two scripts stop and restart only the processes that call a model —
`legba-runtime-dapr` and `legba-dapr-workflow-worker` — and leave the
substrate, the Dapr control plane, the registry, the UI and caddy up, so the
read surfaces keep answering through a halt.

```bash
scripts/fleet_stop.sh                    # watchdogs off, runtime + worker stopped
scripts/fleet_start.sh                   # runtime, wait healthy, then the worker
scripts/fleet_start.sh --resume-watchdog # ... and turn the watchdogs back on
scripts/fleet_start.sh --recreate --resume-watchdog   # after an .env change
scripts/fleet_stop.sh --dry-run          # either script: print the commands only
```

`fleet_stop.sh` first creates `/etc/legba-watchdog.disabled`, the maintenance
flag both host cron jobs honour (`host_stall_watchdog.sh` skips its recovery,
`host_llm_heartbeat.sh` exits quietly); without it the stall watchdog reads
the quiet actor plane as a stall and restarts the runtime it just stopped.
`fleet_start.sh` leaves the flag in place unless given `--resume-watchdog`,
and says so. Both run compose as `-p legba --env-file .env -f
docker-compose.yml` under `COMPOSE_PROFILES=runtime,dapr-workflow`, the set
the worker needs (without it compose answers `no such service:
dapr-placement`), and with `--no-deps`, so nothing else is touched. A plain
start reuses the stopped containers and their old environment; an `.env`
change needs `--recreate` (`up -d --force-recreate --no-deps`). If the
runtime is not healthy within 300 seconds the start stops there (exit 3) and
the worker is not started. If ingestion or analyst cadence stays silent after
a resume, restart the control plane together as above.

### Boot signposts

```bash
docker logs legba-legba-runtime-dapr-1 2>&1 | grep -E \
  'actor_types.registered|nlp_client_factory.built|deps_resolvers.registered|source_first|reconcile_loop.started|initial_resync'
```

In order: `dapr_host.actor_types.registered types=['TargetActor',
'AnalystActor', 'SourceActor']` — three types, or acquisition is not wired;
`nlp_client_factory.built component_id=nlp.local.legba_models` — enrichment
live; `dapr_host.audit_checkpointer.started` — required, and it blocks boot,
because the receipt chain depends on it; `dapr_host.deps_resolvers.registered`
with `source_first.job_plane.ready` and `source_first.subscription_engine.ready`;
`dapr_host.reconcile_loop.started`, then `dapr_host.informer.started`;
`dapr_host.source_first.ready targets_wired=<T> trigger_regs=<M>`; and
`dapr_host.initial_resync.enqueued count=<N>`. A
`dapr_host.source_first.bringup_failed` in place of those last two means the
actor surface is up but the job, fan-out and trigger planes are not: fix NATS or
Postgres reachability, then restart.

The runtime builds its NLP and embedding clients once at boot from the
registered stack components, so it must start against a seeded registry: one
that booted first keeps `nlp_client` at `None` for the life of the process, so
enrichment never builds, signals land with no geo and no entities, and
geo-scoped analysts have nothing to match. The log then shows
`enrichment_build_failed` and no `nlp_client_factory.built`, and the fix is a
`--force-recreate` after seeding. The reconcile loop sleeps
`LEGBA_RUNTIME_RESYNC_INTERVAL` (300 s) before its first walk of the registry,
so expect about five minutes on a cold rig before the first `actor_state` row.

---

## 2. Deploy

### Ship a code change to a provisioned stack

`scripts/rolling_deploy.sh` builds the registry and runtime images, builds the
worker after the runtime and with no cache, refuses to proceed on a stale image,
then recreates registry → migrate → runtime → worker.

```bash
scripts/rolling_deploy.sh \
  --verify-grep 'data/provenance/judge_pipeline_version.py:<version-string>' \
  --verify-file data/analysts/composition_prompt_assembly.py \
  --ui
```

| Flag | Effect |
|---|---|
| `--verify-file RELPATH` | the file must exist under `/install/lib/python3.11/site-packages/legba/` in all three containers afterwards; repeatable |
| `--verify-grep 'FILE:PATTERN'` | `grep -q -- PATTERN` must match in that file in all three containers; repeatable, `PATTERN` quoted as one argument |
| `--max-image-age-min N` | image-age guard threshold, default 30 |
| `--skip-build` | deploy the images that already exist; the guard still runs, so a stale image still aborts |
| `--ui` | also build `legba-ui-build` and restart `legba-caddy` for the new bundle |
| `--dry-run` | print the plan and the guard readout, touch nothing; the nightly gate is still enforced |
| `--ignore-nightly "REASON"` | roll despite a red nightly record; the reason is required and is appended to the nightly ledger as a `NIGHTLY_GATE_OVERRIDE` line |
| `--drain-seconds N` | at the runtime recreate, docker waits N s (1–60) after SIGTERM before SIGKILL instead of the compose default 10 s, so in-flight analyst runs can finish; needs `LEGBA_RUNTIME_DRAIN_SECONDS` on the running container (below) |
| `--tier auto\|t0\|t1\|t2\|t3\|full` | the deploy tier (below); default `full`, today's path |
| `--force-tier` | roll an explicit tier below the computed one; recorded in the deploy log with who forced it |
| `--range FROM..TO` / `--deploy-log PATH` | the range the tier is computed over (default: the last successful roll..HEAD) / the deploy log (default `/var/log/legba_deploy.log`) |

Exit codes: `2` image-age guard, `3` a recreated container never reported
healthy, `4` migration failed, `5` marker verification failed, `6` the nightly
gate refused, `9` a tier refused, `10` a tier's gate failed, `11` a descriptor
parse failure after a `t1` walk.

**Deploy tiers.** A change does not always need the full gate and three
recreates. `scripts/deploy_tier.py classify` names the tier a range needs:
`t0` (docs, tests, host scripts: merge only), `t1` (walked descriptors: the
walker only), `t2` (UI only: the UI build), `t3` (Python one service alone
imports, off the protected paths: rebuild that service only, no migration) or
`full` (migrations, shared schemas, the runtime core, write paths, images,
dependencies, and anything it cannot place). Below full the roll runs
`scripts/run_targeted_gate.sh` (the changed tests, their importers and the
guard set) instead of the full suite. A red nightly, five lower-tier rolls in a
row, a full gate older than 24 h or a repeat `t3` after a gate failure force
full. The default stays `--tier full`; `--tier auto` opts in. When in doubt it
is full. Full reference: [operations/rolling-deploy.md](operations/rolling-deploy.md#deploy-tiers).

**The nightly gate runs first.** The nightly suite appends one
`NIGHTLY_VERDICT` line per run to `/var/log/legba_nightly_suite.log` (full SHA,
PASS or FAIL, phases, pass and fail counts) and posts the verdict to its own
ntfy topic, `nightly`. Before building anything, the roll finds the newest
record from a full run on a clean tree whose SHA is the rolled commit or one of
its ancestors. If that record is FAIL, the roll stops. With no record at all it
warns and proceeds. To clear a red record without an override, fix the cause
and run `PHASES="lint ordered" scripts/host_nightly_suite.sh` on the new HEAD.

Three properties keep a deploy from silently not happening. **The guard reads
images, never logs**: a container logs a fresh boot line while running old
code, so the guard reads image creation times before touching any container. **The worker is built from the runtime image, so
it is built after it** — `docker/Dockerfile.worker` is a thin layer over
`legba/legba-runtime-dapr`, and a worker built first ships old code under an
image whose own timestamp looks new, which only per-file marker verification
inside the worker container catches. **The worker build carries its profiles** —
with no profile set it fails on a sibling service behind the same profile, and
some compose versions print nothing that reads as an error, so the script runs
it under `COMPOSE_PROFILES=runtime,dapr-workflow`.

**Draining the runtime.** An analyst run in flight when the runtime is
recreated has written its finding but not its critique; the `verify_backfill`
sweep (§5) writes the critique later. To lose fewer runs, set
`LEGBA_RUNTIME_DRAIN_SECONDS=45` in `.env` and deploy with `--drain-seconds 55`
(N ≥ D + 10). On the first SIGTERM the runtime stops taking new actor calls,
waits up to D seconds for in-flight turns, then shuts down; a second signal
stops it at once. The variable is read at start, so the deploy that first sets
it cannot drain yet (the script warns). A reminder that fires during the drain
is a missed tick, as without a drain.

### After the roll

```bash
docker compose ps --format '{{.Service}}\t{{.State}}\t{{.Health}}'

# cold activation: forces one unit on one desk through the sidecar and asserts a
# fresh analyst_traces row. It writes a real run and a real finding, which is
# why it is manual and not a deploy.sh phase.
scripts/deploy_smoke_cold_activation.sh --env-file .env

# the reconcile loop is converging, not failing — expect 0
docker logs --since 30m legba-legba-runtime-dapr-1 2>&1 | grep -c 'reconcile.failed'

# active descriptor heads did not drop across the roll
docker compose exec -T postgres psql -U legba -d legba -At \
  -c "select count(*) from analyst_descriptors where is_head and state='active'" \
  -c "select count(*) from source_descriptors where is_head and state='active'"
```

The cold-activation smoke exists because every other check proves only that
processes started: a descriptor-parse bug bites only on the cold path, so warm
actors keep serving and everything reads green while no new actor can activate.
It separates "no trace" (cold-activation failure) from "failed trace"
(activated, run died) and exits non-zero loudly.

### Stand up a fresh instance

`deploy/deploy.sh` is the one-command bring-up: substrate and Dapr, then the
baseline schema and every later migration, then the registry, vault, stack
components, every descriptor (one walker, below) and the budget envelope, then
optional seeds, then the runtime and UI, then a boot check. It does not call
`rolling_deploy.sh` and is not called by it.

```bash
docker compose --profile runtime build
deploy/deploy.sh

# a throwaway validation stack, isolated from the live volumes
deploy/deploy.sh --project legba_val --no-caddy
deploy/deploy.sh --project legba_val --teardown   # down -v, only the legba_val_* volumes

# another instance profile, on a throwaway stack
deploy/deploy.sh --project legba_val --no-caddy --profile profiles/<id>
```

- `--no-caddy` skips the edge. Only Caddy serves the built console, so a
  `--no-caddy` stack has no console; the registry API is on `127.0.0.1:8090`.
- `--seed` runs the profile's seed list. Only `seeds/*.example.yaml` ship, and an
  absent seed file makes its adapter do nothing, so `--seed` writes nothing
  until you provide seed data ([OPERATING_YOUR_INSTANCE.md](OPERATING_YOUR_INSTANCE.md) §1).
- ntfy is under the compose profile `alerts`; `deploy.sh` does not start it.
- The instance profile ([PROFILES.md](PROFILES.md)) is `--profile <dir>`, else
  `LEGBA_PROFILE`, else `profiles/geopolitics`. The manifest is validated
  before any write. The console's labels are a build-time export of the profile
  (`scripts/gen_profile_ui.py`); deploy.sh warns when the committed export is
  for another profile.

The schema step applies `deploy/baseline/0001_baseline.sql`, which reproduces
the first 23 migrations and pre-seeds the migration ledger, then
`python -m legba.data.migrate` applies the rest: numbered migrations in order,
then the time-named ones (`YYYYMMDDTHHMM_slug.sql`). A later migration drops
the empty graph the baseline creates. Seeds run before the runtime starts,
because the runtime builds its enrichment clients once at boot (see the boot
signposts above). `deploy/compose.isolation.yml` re-declares every named volume
under the project name, and deploy.sh aborts before any `up` if a rendered
reference to a real `legba_*` volume survives. On the real `legba` project,
`--teardown` only stops containers.

### The descriptor walker

One registrar writes every registry descriptor head:
`scripts/bringup_register_descriptors.py`. `deploy.sh` runs it once. Its rule:

    every descriptors/*.yaml of the families action_pack, source, target, analyst
      (the family is read from identity.schema_uri, not the filename)
    + every entry of the profile's source catalog (profiles/<id>/rosters/source_catalog.yaml)
    − every row of descriptors/RETIRED.md
    = what the instance registers,
      each at the state the lifecycle overlay records, else the state its file ships.

The directory, overlay, catalog and credibility seeds come from the selected
profile. A profile with no `RETIRED.md`, no lifecycle overlay and no source
catalog registers its descriptors directory at the shipped states.

- **`descriptors/RETIRED.md`** lists what the walker does not register:
  `retired` rows (terminal; a live head that is not retired is reported
  `would-retire`) and `parked` rows (in the tree, not registered).
- **The lifecycle overlay** is `descriptors/LIFECYCLE.d/`, one file per row
  (`<family>__<id>.md`): the state an instance holds where it differs from what
  the file ships ([descriptors/LIFECYCLE.md](../descriptors/LIFECYCLE.md)).
  Files ship `draft`, so registering never schedules anything; the overlay
  records an instance's activations and pauses. Each instance keeps its own.

The walker compares by content: a head whose stored body hashes the same as the
tree's descriptor is reported `unchanged` and not written, so a re-run is safe.
A head's lifecycle `state` column wins over its body. It also writes the
non-builtin `analyst_kind` rows, the vocabulary families
(`src/legba/data/taxon_families.py` and every `descriptors/vocabulary_<family>.yaml`;
an admin's deprecation is kept), and each source batch's `source_credibility`
seeds (`ON CONFLICT DO NOTHING`), and runs the bounded-unit drift guard.

Each output line reads `unchanged`, `register`, `would-update` (with the
changed keys), `would-retire`, `retired`, `parked` or `failed`. A `failed` line
exits non-zero and deploy.sh aborts.

```bash
# what a deploy would do, writing nothing; on a running host expect every head `unchanged`
docker compose -p legba run --rm --no-deps -v "$PWD:$PWD" -w "$PWD" \
  -e LEGBA_DATA_PG_DB=legba --entrypoint python legba-registry \
  scripts/bringup_register_descriptors.py --dry-run

# limit to one family (repeatable) or to named heads (repeatable)
... scripts/bringup_register_descriptors.py --dry-run --family analyst
... scripts/bringup_register_descriptors.py --dry-run --id <descriptor_id>
```

The walker writes directly to the database without a bus connection, so a head
it registers emits no `descriptor.*` event. The runtime's reconcile loop picks
it up at the next resync, within about five minutes
(`reconcile.action ... create_actor`). A registration through the registry API
emits the event and reconciles within seconds.

**Prompt packages.** A descriptor may name a module in the profile's prompt
package (`prompt_module: legba_<id>.<module>:ATTR`), and registration refuses
an active descriptor whose module does not import. The registry and runtime
images copy the profile's `prompts/` into site-packages (build arg
`LEGBA_PROFILE`); deploy.sh also puts the checkout's `<profile>/prompts` on the
registrar's `PYTHONPATH`. Check a deploy with `--verify-import`:

```bash
scripts/rolling_deploy.sh --verify-import legba_geopolitics.lens_left
PYTHONPATH=src:profiles/geopolitics/prompts python3 scripts/<script>.py   # a script run by hand
```

**Switching on a new analyst** that ships `draft`:

1. Deploy the code first: the registry image, then the runtime image and the
   worker built from it. On an older image the registration warns about unknown
   options and the runtime drops them.
2. Register it by id: `--id <id>`, dry run first; expect one `register` line.
3. Activate it through the lifecycle route (§4). A target is wired only when
   `active`, so activate the analyst's target before or with it if needed.
4. Record it with `python3 scripts/lifecycle_rows.py set analyst <id> active draft`,
   so a fresh instance comes up the same way.
5. Read its first run's receipt.

**Adding a feed to the source catalog:** add an entry to the profile's catalog
file; its credibility seed rides on the entry.
`scripts/bringup_register_source_catalog.py --verify` probes each feed and
registers nothing. The older `scripts/bringup_register_*.py` scripts are
inputs to the walker now; deploy.sh does not run them, and two of them
(`..._g20_country_targets.py`, `..._watch_country_targets.py`) strip tags if run
on their own.

### The UI and the models host

```bash
docker compose --profile ui build legba-ui-build
docker compose --profile ui up -d --force-recreate legba-ui-build
docker compose --profile runtime restart legba-caddy
```

Caddy serves from the shared `ui_dist` volume the one-shot build republishes.
Model servers and the `legba-models` NLP service run outside this stack
([AI_MODELS.md](AI_MODELS.md)). Deploy `legba-models` from its own directory on
its host ([legba-models/USAGE.md](../legba-models/USAGE.md)), then confirm it
through the runtime's NLP client: the runtime log line `nlp_client_factory.built`
after a recreate, and enrichment on new signals.

---

## 3. Is it healthy

| Check | Fine looks like |
|---|---|
| Containers | every service `running`; `legba-registry` and `legba-runtime-dapr` `healthy` |
| Registry | `{"status":"ok","checks":{"postgres":"ok","nats":"ok"}}` |
| Runtime | HTTP 200 |
| Ingestion freshness | newest signal under 30 minutes old — the measured worst-case inter-signal gap over a week, and the threshold the host stall watchdog acts on |
| Source fan-out | distinct producing sources over 24 h, close to the active source count less the weekday-only feeds at a weekend |
| Poll outcomes | `error` a small tail beside `success` and `empty`; `empty` is the normal outcome for a feed with nothing new |
| Analyst production | findings, critiques, alerts and scorecards all present over 24 h, in the hundreds. A kind at zero is a dead plane |
| Judge band | mean faithfulness near the band your instance has held; movement toward the 0.50 composition verify floor is the signal, not the absolute value |
| Grader receipts | non-zero rows in 24 h — the correctness instrument ran |
| Canaries | the global-run analysts (in the example instance `world_assessor`, `journal_assessor` and `journal_consolidator`) each produced recently |
| External audit | `present=true`, `stale=false`, `degraded=false` |
| Production gauge | `deficit` counts are what to chase |
| Dead letters | all three queues at zero |
| Disk | root under 90% |
| Watchdogs | cron file present, heartbeat fresh, no disable flag, collector following every service, last nightly verdict green |

Three readings need their reason stated beside them.

**Read the audit heartbeat, never `analyst_traces.status`.** The handler records
a degraded run as a success, because a degraded run is still a completed run, so
cadence health alone cannot tell you the auditor stopped auditing. Every run
writes a heartbeat including one that audits nothing, so "nothing to contradict"
and "the auditor is dead" never look alike.

**A global-run analyst is the canary** (in the example instance, the world voice): a six-hour cadence on a single global
run, so it goes quiet first when the registry cannot serve `/typed`. The
journal's two tiers need their own canaries for the same reason: a global-run
analyst has no target to flag its death, and the daily consolidator hides it
longest.

**Disk is the silent one.** OpenSearch's flood-stage watermark is 95%, past
which it puts every shard into a read-only block silently — no crash, no error,
corpus indexing simply stops while every other plane keeps running. Every disk
check reads the figure `df` prints as Use% (used / (used + available), rounded
up); OpenSearch's own figure runs about a point above it near full. The disk
floor (`scripts/disk_floor_check.sh`, cron every 30 min) pages `engine` once on
ENTERING a band — warning ≥ 85% (priority 3), urgent ≥ 90% (4), critical ≥ 93%
(5, two points ahead of flood stage) — never again while it stays there, except
critical, which re-notifies every 6 h. Leaving a band sends one recovery notice,
only once usage falls 2 points below the band's threshold, so a reading that
wobbles across a threshold does not flap. Its state is one line in
`/var/log/legba-disk.state` (`<band 0-3> <since epoch> <last notify epoch>`);
delete it to start over (a missing or corrupt file is a first run). The LLM
heartbeat's own disk check still logs at 85% and pages hourly at ≥ 92%.

```bash
docker compose ps --format '{{.Service}}\t{{.State}}\t{{.Health}}'
curl -s $REG/api/v1/registry/healthz
curl -s http://127.0.0.1:6090/healthz
curl -s -H "Authorization: Bearer $TOKEN" "$REG/api/v1/v3/system/external-audit?days=7" | jq '.heartbeat'
curl -s -H "Authorization: Bearer $TOKEN" "$REG/api/v1/v3/system/production-gauge" | jq '.totals'
curl -s -H "Authorization: Bearer $TOKEN" $REG/api/v1/registry/dead_letter

W="produced_at > now() - interval '24 hours'"
docker compose exec -T postgres psql -U legba -d legba -At \
  -c "select round(extract(epoch from now() - max(fetched_at))/60) from signals" \
  -c "select count(distinct source_id) from signals where fetched_at > now() - interval '24 hours'" \
  -c "select outcome, count(*) from source_poll_outcomes
       where occurred_at > now() - interval '24 hours' group by 1 order by 2 desc" \
  -c "select kind, count(*) from analyst_outputs where $W group by 1 order by 2 desc" \
  -c "select round(avg(confidence)::numeric,3), count(*) from analyst_outputs
       where kind='critique' and $W" \
  -c "select count(*) from unit_correctness where created_at > now() - interval '24 hours'" \
  -c "select analyst_id, max(produced_at) from analyst_outputs
       where analyst_id in ('world_assessor','journal_assessor','journal_consolidator') group by 1" \
  -c "select count(*) from descriptor_dead_letter" -c "select count(*) from output_dead_letter"

df -h / && docker system df
cat /etc/cron.d/legba-watchdog
stat -c %y /var/lib/legba-watchdog/heartbeat   # the stall watchdog's own liveness
tail /var/log/legba-watchdog.log               # silent when healthy, so an empty log is the steady state
ls /etc/legba-watchdog.disabled                # absent means the family is armed
scripts/host_log_collector.sh status           # a service reading NOT-FOLLOWING is the failure to chase
grep -E 'VERDICT|seed=' /var/log/legba/nightly/latest/summary.txt
grep '^NIGHTLY_VERDICT' /var/log/legba_nightly_suite.log | tail -3   # what the roll gate reads
```

**Replaying a shuffled nightly failure.** The summary's `seed=` line is the
replay key: `LEGBA_REPO_ROOT=<checkout> LEGBA_TEST_SHUFFLE_SEED=<seed> bash
scripts/run_tests_in_container.sh tests/` reruns the same order. Database-backed
tests never run on the host, where `127.0.0.1:5432` is the live server.

---

## 4. Routine tasks

### Rotate a credential

Secrets live in the vault, encrypted with `LEGBA_DATA_MASTER_KEY` into
`stack_credentials`; that key must stay stable across restarts. The runtime
resolves credentials at boot, so recreate it after a rotation. The bringup
scripts are not baked into the images, so the bulk loader runs from a
repo-mounted one-off container; it is idempotent and never echoes plaintext.
The Caddy basic-auth hash is the one credential with a formatting trap:
single-quote it in `.env`, or `$$`-escape its `$` characters, or compose's
`env_file` interpolation mangles the hash and every password is rejected.

```bash
curl -sX POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/registry/vault/secrets \
  -d '{"secret_id":"nlp.local.legba_models.api_pass","plaintext":"<new>"}'
curl -s -H "Authorization: Bearer $TOKEN" \
  $REG/api/v1/registry/vault/secrets/nlp.local.legba_models.api_pass/exists
docker compose --profile runtime up -d --force-recreate --no-deps legba-runtime-dapr

# or load every secret named in .env at once
docker compose run --rm --no-deps -v "$PWD:$PWD" -w "$PWD" \
  -e LEGBA_REGISTRY_URL=http://legba-registry:8090/api/v1/registry \
  --entrypoint python legba-registry scripts/bringup_vault_load.py

# the caddy hash: set LEGBA_BASIC_AUTH_HASH in .env, single-quoted, then recreate
docker exec legba-legba-caddy-1 caddy hash-password --plaintext '<new>'
docker compose --profile runtime up -d --force-recreate --no-deps legba-caddy
```

### Accounts and service credentials

Only with `LEGBA_AUTH_SIGNIN=1`. Turning sign-in on, creating the owner, and
what each role can do are in
[Operating your instance §9](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in).
The console has no user-management screen; an admin uses the API:

```bash
# invite someone (role: reader, contributor or admin); the answer's invite_path is shown once
curl -sX POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/auth/invite -d '{"email":"someone@example.org","role":"reader"}'
curl -s -H "Authorization: Bearer $TOKEN" $REG/api/v1/auth/users          # every account
curl -sX PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/auth/users/<user_id> -d '{"status":"disabled"}'            # ends every session it holds
```

To rotate a service's credential, set a new random value for
`LEGBA_SERVICE_TOKEN_<NAME>` in `.env` and recreate the registry first, then
that service; there is no overlap window (SEAMS #87), so the service is
refused between the two recreates. The registry log line
`registry.auth.break_glass` marks every use of the shared admin token, and
`principal=shared` at a service's start means it has no credential of its own.

### Register, activate, pause or retire a descriptor

The registry API takes JSON, so convert the descriptor YAML first. A PUT stamps
a fresh content hash, preserves history, emits `descriptor.updated.<family>.<id>`
on NATS, and atomically retires the prior version's UI panel rows. The reconcile
loop picks the change up within a resync interval, and a descriptor that ships
`state: draft` stays inert until an admin flips it deliberately. Deterministic
handlers read their thresholds from a `method.options` block on the descriptor,
so a threshold change needs no rebuild and no recreate; precedence runs
runtime-stamped provenance, then a forced run's `payload.options`, then
`method.options`, then the handler's own default. An undeclared or out-of-range
key is dropped loudly and the handler default stands, so a mistyped knob never
takes a class offline; the rejection lands in the trace as a `handler_options`
step, `applied` or `degraded`, listing every dropped key with its cause.

A descriptor file under `descriptors/` is its registration: `scripts/bringup_register_descriptors.py` (run by `deploy/deploy.sh`, or by hand with `--dry-run` first) registers every file of the four registry families, at the state the file ships unless the lifecycle overlay (one file per row under `descriptors/LIFECYCLE.d/`, `<family>__<id>.md`) records another for this host. Activate through the lifecycle route below, then add the descriptor's overlay row so a fresh instance comes up the same way; retire through the route, then add its RETIRED.md row (§2, "The descriptor walker").

```bash
python3 -c 'import json,sys,yaml; json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)' \
  descriptors/analyst_world_assessor.yaml > /tmp/body.json
curl -sX PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/registry/descriptors/analyst/world_assessor -d @/tmp/body.json

# lifecycle moves — draft, configured, active, paused
curl -sX POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/registry/descriptors/source/source.bbc.world/transition \
  -d '{"to_state":"paused","reason":"upstream returning 403"}'

# retiring is its own route, and takes a reason
curl -sX POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/registry/descriptors/target/country_g20_br/retire \
  -d '{"reason":"decommissioning"}'
```

`--id <descriptor_id>` (repeatable) limits the walker to those heads, for a
staged switch-on such as turning thinking on seat by seat; an id the walk does
not plan is refused. The vocabulary seeds still run; the source credibility
seeds do not.

**After switching an analyst to thinking** ([AI_MODELS.md](AI_MODELS.md),
"Thinking"), read its receipts once its first scheduled runs finish: the calls
carry `"thinking": true` and the thinking name as `model`; compare
`duration_ms` with the previous week; look for `thinking_fallback` on receipts
and in the alert log; read `gate_wait_ms` and `llm_gates` on the runtime's
`/healthz`.

### Force an analyst run

Forcing goes through the same door the cadence uses — a PUT to the Dapr
sidecar's actor-method API — so the run takes the identical path with the same
deps, model and packs, skipping only the cooldown. Options ride inside the
body's `options` object; a key at the top level is ignored silently. HTTP 200
means the actor ran, not that the instrument measured anything, so read the
receipt.

```bash
VER=$(curl -s -H "Authorization: Bearer $TOKEN" \
  $REG/api/v1/registry/descriptors/analyst/correctness_grader | jq -r .version)
curl -sX PUT -H 'Content-Type: application/json' \
  "http://127.0.0.1:3500/v1.0/actors/AnalystActor/analyst%3A%3Acorrectness_grader%3A%3A${VER:0:16}/method/run" \
  -d '{"trigger_kind":"method","options":{}}'
```

Two analysts ship a wrapper that builds the envelope and waits:
`scripts/correctness_grade_now.py` and `scripts/reference_build_now.py` (about
eight minutes per build; `--dry-run` builds without writing a row).

### Add or drop a source

New source descriptors live under `descriptors/` (or as an entry in the
profile's source catalog, `profiles/geopolitics/rosters/source_catalog.yaml`)
and register through the one walker,
`bringup_register_descriptors.py`, which also seeds every batch's
`source_credibility` rows. It is idempotent and defaults to the
`legba_pivot_test` database, so pin `LEGBA_DATA_PG_DB=legba`. New source
files ship `draft` and are activated one at a time, each activation recorded
as a row under `descriptors/LIFECYCLE.d/`. Dropping a source is the pause or
retire transition above: pausing keeps its signals and its cursor, retiring
releases its reminders through the orphan GC sweep.

```bash
# probe and parse-check first — prints a verdict table, registers nothing
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_source_catalog.py --verify
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_descriptors.py --dry-run --family source
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_descriptors.py --family source
```

### Read a trace

`analyst_traces` is the per-run receipt. `prompt_rendered` is capped at 32,000
characters, and on a desk with a verbose system prompt the static prompt plus
the grounding blocks can consume all of it before one numbered signal is stored
— so "grep `prompt_rendered` for X" is not executable on the desks where it
matters most. Two columns close that. `prompt_sha256` is computed over the full
untruncated text, so a capped row stays byte-verifiable against a re-render from
`scripts/render_prompt_pack.py`. `input_row_refs` is never truncated and is the
reachability answer — though it proves the row was in the slice, not that it
survived into the rendered prompt.

```sql
SELECT s.id, s.title, s.published_at
  FROM analyst_traces t
  JOIN signals s ON s.id = ANY (t.input_row_refs)
 WHERE t.run_id = '<run-id>' AND s.title ILIKE '%<term>%';
```

A run older than `LEGBA_TRACE_COMPRESS_AFTER_DAYS` may be compacted (when
`LEGBA_TRACE_COMPRESS` is on): its `prompt_rendered`, `input_payload` and
`output_payload` read NULL in SQL and `bulk_bundle_id` names the
`analyst_trace_bundles` row that holds them. The console, lineage and Flow
routes read them back as before. From a shell, in the runtime or registry
container:

```bash
python3 - <<'PY'
import asyncio, asyncpg
from legba.data.config import PostgresConfig
from legba.data.trace_bulk import hydrate_traces, pointer_columns_sql
async def main():
    c = await asyncpg.connect(PostgresConfig.from_env().dsn)
    row = await c.fetchrow(f"SELECT run_id, prompt_rendered, output_payload, {pointer_columns_sql()}"
                           " FROM analyst_traces WHERE run_id = $1", "<run-id>")
    print((await hydrate_traces(c, [row]))[0]["prompt_rendered"][:2000])
asyncio.run(main())
PY
```

After the first full compaction pass the freed space is reused by the table
for new traces (about two months of growth); it returns to the operating
system only when the table is rewritten with `VACUUM FULL analyst_traces`,
which holds an exclusive lock for the whole rewrite (every trace write and
read waits), so it belongs in a window with the analysts paused, if at all.

### Registry workers

The registry runs one uvicorn worker unless `LEGBA_REGISTRY_WORKERS` says
otherwise (`src/legba/data/registry/workers.py`). Every analyst's descriptor
read, the console and consult share that queue.

Before raising it: a consult run lives on the worker its `POST /consult` landed
on, so a reconnect, a stop (`POST /consult/runs/{id}/stop`) or the actor's
`POST /consult/writes` that reaches another worker answers 404 or 403 (SEAMS
#74). A missed stop leaves the run going to its own clock and cost ceiling.

```bash
grep -q '^LEGBA_REGISTRY_SIGNING_KEY' .env || echo "set the signing key first: N>1 refuses without it"
echo 'LEGBA_REGISTRY_WORKERS=2' >> .env      # or edit the existing line
docker compose --profile runtime up -d legba-registry
docker logs legba-legba-registry-1 --since 2m 2>&1 \
  | grep -E 'registry.workers|once_only|Started server process|Application startup complete'
```

Success: one `registry.workers n=2` line, two `Started server process`, two
`Application startup complete`, and exactly one `registry.once_only.acquired`.
Two `acquired` lines mean the lock failed; set it back to 1. Each worker gets
`LEGBA_DATA_PG_POOL_MAX // N` connections. Each worker is a whole process:
check `docker stats legba-legba-registry-1` against the service's `mem_limit`
and raise it in the same change if needed.

```bash
# latency of a descriptor read: run it while analysts fan out, before and after
for i in $(seq 50); do
  curl -s -o /dev/null -w '%{time_total}\n' -H "Authorization: Bearer $TOKEN" \
    $REG/api/v1/registry/descriptors/analyst/world_assessor
done | sort -n | awk '{a[NR]=$1} END {print "p50", a[int(NR*.5)], "p95", a[int(NR*.95)], "max", a[NR]}'
docker logs legba-legba-registry-1 --since 1h 2>&1 | grep -cE '" 5[0-9]{2}'   # 5xx in the last hour
```

If p95 does not improve, look at Postgres waits (`pg_stat_activity`). If 5xx
rise, go back to 1 with the same edit.

### Reclaim disk

Three levers, in the order that frees the most, none destructive to product
data. Run none of them during a deploy window: a build in flight can be starved
of a cache layer it is mid-use of, turning a disk fix into a deploy failure.

```bash
docker system df && docker builder prune -f   # usually the biggest win by far
journalctl --disk-usage && journalctl --vacuum-size=500M   # the container runtime's journal
docker images && docker image prune -a -f     # superseded builds nothing references
```

### Back up and restore

`scripts/backup.sh` takes one **encrypted backup set** of the whole deployment;
`scripts/restore_drill.sh` restores a set into a throwaway project and proves it.
A deployment that is lost without a set is lost: the vault master key and the
signing key exist only in the untracked `.env`, and the evidence archive lives
outside Postgres.

**What a set holds.** Postgres `legba` (a custom-format dump taken inside an
exported snapshot, with the named tables' row counts read in that same snapshot),
the `dapr` database and the cluster globals; the evidence archive (the
`legba_archive` volume — incremental, see below); one Qdrant snapshot per
collection; a logical export of every OpenSearch user index (the live node has no
`path.repo`, and adding one costs an OpenSearch restart); Redis; the JetStream
store; `seeds/` and `planning/`; and the **key escrow** — `.env` (which carries
`LEGBA_DATA_MASTER_KEY` and `LEGBA_REGISTRY_SIGNING_KEY`) plus any signing keyfile
it points at, sealed as its own artifact. The run refuses when a required secret
is missing from the escrow.

**Encryption.** Every artifact is encrypted to an OpenPGP public key as it is
written; no plaintext copy lands on disk here or at the destination. The script
refuses to run without `LEGBA_BACKUP_PUBKEY`. The escrow can go to a second key
(`LEGBA_BACKUP_ESCROW_PUBKEY`). Make the key pair **off this host** and keep the
secret key there: a backup whose key dies with the host cannot be restored.

```bash
# on the machine that will hold the secret key
gpg --quick-gen-key 'legba-backup' future-default default never
gpg --export -a legba-backup > backup-pubkey.asc       # copy THIS to the host
# on the host
sudo install -m 644 backup-pubkey.asc /etc/legba/backup-pubkey.asc
echo 'LEGBA_BACKUP_PUBKEY=/etc/legba/backup-pubkey.asc' >> .env
```

**Destination.** `LEGBA_BACKUP_DEST` is a local path (the default,
`/var/backups/legba` — not disaster-proof on its own), an rsync-over-ssh target
(`ssh://user@host[:port]/path` or `user@host:/path`), or an S3-compatible bucket
through rclone (`rclone:<remote>:<path>`, or `s3://bucket/path` on the remote
named by `LEGBA_BACKUP_RCLONE_REMOTE`). A remote set is staged locally
(`LEGBA_BACKUP_STAGING`), shipped (`set.json` last), then removed from staging.

```text
<dest>/sets/<SETID>/set.json            cleartext completeness marker, no content
<dest>/sets/<SETID>/SHA256SUMS          sums of the encrypted files
<dest>/sets/<SETID>/manifest.json.gpg   counts, sizes, the pack chain, fingerprints
<dest>/sets/<SETID>/*.gpg               the artifacts; escrow.tar.gpg is the escrow
<dest>/archive/pack-<SETID>.tar.zst.gpg the archive chain (+ .list.gpg per pack)
```

**The archive is incremental.** Archive files are named by their sha256 and never
change, so each run packs only the names never shipped to that destination (the
index lives in `LEGBA_BACKUP_STATE_DIR/archive_index/`). The first run is a full
pack; the next ones carry a day's evidence. Every manifest lists the whole pack
chain; retention never prunes `archive/`. Losing the state directory only costs a
second full pack.

**Disk guard.** The set's filesystem is the live one. The script projects the
fill before it starts and checks again before each component; above
`LEGBA_BACKUP_MAX_DISK_PCT` (90) it stops and removes the partial set, because
OpenSearch goes read-only at 95% on the same disk.

```bash
bash scripts/backup.sh                     # one full set
bash scripts/backup.sh postgres escrow     # a subset, still one set
bash scripts/backup_scheduled.sh           # a set, then 7-daily / 4-weekly retention

# nightly (no host_bootstrap.sh exists — install by hand; 03:30 HOST time, PDT)
sudo cp deploy/systemd/legba-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now legba-backup.timer
systemctl list-timers legba-backup.timer
```

Exit codes: `0` a complete set is at the destination; `1` failed (the partial set
is removed); `2` refused (no key, incomplete escrow, disk guard, bad destination);
`backup_scheduled.sh` adds `3` (set taken, retention failed). Interrupting a run
stops the in-container `pg_dump` it started (by its snapshot id).

**The restore drill.** Run it where the secret key is — weekly, and after any
change to the backup. It restores into `deploy/compose.drill.yml` (standalone,
project-scoped volumes, loopback ports 55432 / 56333 / 59200, so it runs beside
the live stack) and refuses if the rendered project would touch any volume that
is not its own. It asserts: the set's sums; every named table's row count equals
the manifest exactly; 100 random `evidence_archive` receipts from the restored
database re-hash to their sha256 out of the pack chain; the escrow opens, is
complete, and its master key decrypts one `stack_credentials` row of the restored
database (only the secret id and its length are printed); Qdrant's point counts
fall inside the manifest's before/after bracket and OpenSearch holds exactly the
exported documents; the rest decrypts. It prints the restore time and, on PASS,
writes `last_drill.json`. It tears the project down unless `--keep`.

Run beside the live stack, the drill needs about the live Postgres size free on
the same disk: it records each Qdrant and OpenSearch count and then drops that
restored copy, and restores Postgres into empty `template0` databases in one
transaction per database under `wal_level=minimal` (no WAL for the bulk load), but
the restored database itself is the size of the live one. A disk watch aborts and
tears down above `LEGBA_DRILL_MAX_DISK_PCT` (93). Run it on another machine
whenever one exists.

```bash
bash scripts/restore_drill.sh --secret-key ~/backup-secret.asc \
     [--escrow-secret-key FILE] [--set <dest>/sets/<SETID>] [--sample 100] [--keep]
# a remote destination: copy sets/<SETID>/ and archive/ down first, then --set
```

Do not use `deploy/compose.isolation.yml` for a restore: it re-declares only some
of the base file's volumes, so `opensearch_data`, `archive_data` and `ntfy_cache`
keep their live `legba_*` names under any project.

**Status.** Both scripts write a status file on success only
(`LEGBA_BACKUP_STATE_DIR`, default `/var/lib/legba/backup-state`:
`last_backup.json`, `last_drill.json`). The registry mounts that directory
read-only and serves `GET /api/v1/v3/system/backup-status`
(`last_backup_at`, `last_drill_at`); System Status shows both at the head of its
Infra section, "never recorded" when absent and amber past 36 hours (backup) or
8 days (drill).

**A real restore** is the drill against the target stack instead of the throwaway
one: restore the dumps with `pg_restore`, upload each Qdrant snapshot
(`POST /collections/<c>/snapshots/upload`), re-import each OpenSearch index with
`python3 scripts/backup_lib.py os-import <url> <meta.json> < <ndjson>`, untar the
pack chain oldest-first into the `legba_archive` volume, and put the escrowed
`.env` back before the registry starts.

### Compress and tier the evidence archive

Archive objects rest zstd-compressed; every reader detects the
codec by frame magic and returns the ORIGINAL bytes re-verified against the
address, so old (raw) and new (compressed) objects coexist with no migration.
`evidence_archive.sha256` and `size_bytes` always describe the original.

**Compact in place** (`scripts/archive_compact.py`, dry-run by default; run in
the runtime image, which ships `zstandard`):

```bash
docker exec -i legba-postgres-1 psql -U legba -d legba -At -c \
  "select sha256 from evidence_archive where sha256 is not null
   union select media_sha256 from evidence_archive where media_sha256 is not null" > /tmp/archive_shas.txt
docker run --rm -v legba_archive:/archive -v "$PWD/scripts:/s:ro" -v /tmp:/h \
  --entrypoint python legba/legba-runtime-dapr:latest /s/archive_compact.py \
  --root /archive --shas-file /h/archive_shas.txt --prefix 00 [--apply] --report /h/compact_00.json
```

Stage with `--prefix 00`, then `--prefix 01-0f`, then `10-ff`; `--max-mb-per-min`
paces reads (default 300). The job is idempotent: kill it and run it again.
A file whose bytes do not hash to its name is reported and never touched;
an orphan (no row names it) is reported and skipped unless `--include-orphans`.
Rollback: none is needed for correctness (every reader decodes); to restore a
raw object, `zstd -dc <file> > tmp && mv tmp <file>`. Backup chains already
hold the raw originals, and the restore drill's re-hash decodes.

**Tier to cold** (`scripts/archive_tier.py`, dry-run by default) needs
migration 0243 and `LEGBA_ARCHIVE_COLD_BACKEND`; it refuses otherwise. See
`docs/TUNABLES.md` for the knobs.

### Regenerate the generated docs

Five generators own docs or parts of docs; never hand-edit what they write.
Each has a `--check` mode that exits non-zero when the committed copy is stale,
and the suite runs the checks.

| Generator | Writes | Reads |
|---|---|---|
| `scripts/gen_api_roles_doc.py` | `docs/API_ROLES.md` | the registry's route-role tables (builds the app; no database) |
| `scripts/gen_type_tables.py` | `docs/TYPES.md`, the kind table in `docs/ALERTING.md`, and the console's structural-verify set in `legba-ui-v3/src/lib/verdictModel.ts` | the type registry |
| `scripts/gen_release_state_matrix.py` | the panel table in `docs/RELEASE_STATE_MATRIX.md` | the UI's panel registry |
| `scripts/gen_not_published.py` | `legba-ui-v3/src/lib/not_published.json` | the measurement seams of `docs/SEAMS.md` and the eval routes' declarations |
| `scripts/generate_release_manifest.py` | `docs/RELEASE_STATE.md` | the registry database (read-only); run it after registering or retiring a source, desk, unit or migration |

```bash
python3 scripts/gen_api_roles_doc.py --check
python3 scripts/gen_type_tables.py --check
python3 scripts/gen_release_state_matrix.py --check
python3 scripts/gen_not_published.py --check
PYTHONPATH=src python3 scripts/generate_release_manifest.py   # needs the database
```

---

## 5. When something is wrong

**A judge route is gone: 401, 402, 403 or 404.** The watchdog pages at once on
`llm_route_auth_failed` (severity high, budget-exempt, through the same ntfy
fan-out as every watchdog channel) the first time a HOSTED component's receipt
carries one of those statuses; it re-pages every 6 h while the route stays gone,
and closes the episode on the first successful call. Meanwhile the critic falls
down its judge fallback chain (`method.llm.judge_chain`, default: the small
self-hosted model alone; any further rung is opt-in) — read which rung is answering, and since when the
checking layer has written only floors:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$REG/api/v1/v3/system/judge-stats/health?days=7" | jq '{unverified_since, unverified_n,
   components: [.components[] | {component_id, failure_class, consecutive_failures,
   last_success_at, unverified_since}]}'
```

The fix is the provider's: top up (402), rotate the key (401/403), repoint
`config.model_name` (404). Floor critiques written while every rung was down are
NOT re-judged when the route recovers; inventory them, by UTC day, read-only:

```bash
PYTHONPATH=/app/src python3 /app/scripts/rejudge_floor_critiques.py \
  --since <from> --until <to>
```

The dry run also previews the next 50 (`--limit N`) that an apply would re-judge.
Once a rung answers again, re-judge them — it spends on the judge and supersedes
published verdicts, so the count is always explicit (at most 500 per run):

```bash
PYTHONPATH=/app/src LEGBA_VERIFY_LLM_JUDGE=1 python3 /app/scripts/rejudge_floor_critiques.py \
  --since <from> --until <to> --apply --limit 50
```

Oldest first, it re-runs the critic's verify pass over each finding's stored
citations against the analyst's current judge chain and prints old score → new
score, `judge_status` and `judge_llm_ref` per row. An answered row becomes a new
critique (`verification.rejudge_of`, `judge_trigger = 'rejudge_floor'`) and the
old floor is closed (`superseded_by`). A row the judge still cannot answer is
left as it was and counted (`judge_failed`). Five such rows in a row stop the
run. `--skip-weak` refuses verdicts from the 8B rung. Run it again until
`still floor-only` reads 0 (SEAMS #69, resolved).

**Findings with no critique at all.** A rolling deploy recreates the runtime; an
analyst run in flight then has already written its finding, but the verify leg
that writes the `Faithfulness verify` critique never runs, so the finding stands
published unverified. `--missing` selects those instead of floors:
cited `kind='finding'` rows of an analyst the critic covers (one with
faithfulness critiques in the window), not a deterministic rollup, not
superseded, older than `--min-age-minutes` (default 30, so a verify leg still
running is never raced), with no faithfulness critique. The dry run counts them
per UTC day and previews the next `--limit`:

```bash
PYTHONPATH=/app/src python3 /app/scripts/rejudge_floor_critiques.py --missing
PYTHONPATH=/app/src LEGBA_VERIFY_LLM_JUDGE=1 python3 /app/scripts/rejudge_floor_critiques.py \
  --missing --since <from> --apply --limit 50
```

The apply runs the same verify pass and writes the critique the critic would
have written (`derived_from` = the finding; the finding's analyst, version and
run), stamped `verification.judge_trigger = 'verify_backfill'` (outside the
sampled arm, like a re-judge) and `verification.backfill_of_run = <the finding's
run_id>`. Nothing is superseded. A judge that does not answer writes nothing. A
critique that lands between the selection and the write wins: the write
re-checks under a per-finding advisory lock and the row is counted `raced`.

You rarely need the script: the `verify_backfill` deterministic analyst runs the
same selection and write every 30 minutes (:12 / :42), `backfill_batch` (default
50) per tick over the last 7 days, `min_age_minutes` default 30. Each tick's
trace carries `examined` / `verified` / `failed` / `still_missing`, plus
`skipped`, `parked` and `skipped_no_judge` (judge flag off, or no registry or
secrets wiring). A finding that fails on a tick where another verified is
retried, and after 3 such failures it is parked until the runtime restarts. Five
judge failures in a row end the tick. Older debt is the script's (`--since`).
Steady state is `examined=0`. A `still_missing` that grows across ticks means the
judge chain is down (see the floors above) or rows are parked; read the tick's
trace.

**The search plane answers degraded.** The watchdog pages on `search_degraded`
when more than 20 % of a search provider's audit NOT_FOUND grades in 24 h ran
on a search that lost engines (the rule and its threshold: docs/TUNABLES.md §1).
Those grades read as `search_failed` on `/v3/absence`, never as "searched, found
nothing". Read which engines are failing, and how often, over 7 days:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$REG/api/v1/v3/system/search-health?days=7" | jq '{threshold, as_of,
   providers: [.providers[] | {provider, not_found, not_found_degraded_share}],
   engines: [.engines[] | {engine, error_share, errors}]}'
```

The fix is the engine set: disable or re-weight the failing engine in
`docker/searxng/settings.yml` and restart searxng. Grades written before
migration 0235 carry no engine record (`rows_without_engine_record`) and are
read by the provider's own degraded flag.

**The phone is too loud (or too quiet).** Outward alerts go to two ntfy topics: `world` (escalation transitions, verified high findings, one batched push
per trigger tick for band moves) and `engine` (source, search, cadence, judge
and alert-plane health; the host LLM heartbeat, search canary and disk floor
scripts post here too). `nightly` is the nightly suite's own. Subscribe the ntfy
web UI to `world` and `engine`; the pre-split topic (`legba-alerts`) receives
nothing new. The kind-to-topic table is in [ALERTING.md](ALERTING.md). The watchdog pages
`alert_volume_high` on `engine` when a topic sends more than its declared cap
in 24 h (`LEGBA_ALERT_VOLUME_DAILY_CAP_WORLD` / `_ENGINE`, 50). Read the count
per topic and kind — pushed, coalesced by the cooldown, level-only (an
escalation whose severity held — recorded, never pushed), below the sink's
severity floor, duplicate, failed. Every dispatch decision writes an
`alert_sink_deliveries` row with its reason (`error_message`); nothing is
"delivered" internally without one. The units' severities map onto the
dispatcher's ladder explicitly — `moderate` → `medium` (priority 3), `elevated`
→ `high` (priority 4); an unknown word becomes `medium` and logs
`alert_sinks.severity_unknown` (the table is `SEVERITY_ALIASES` in
`data/alerts/sinks.py`):

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$REG/api/v1/v3/system/alerts/volume?days=7" | jq '{as_of, caps,
   topics: [.topics[] | {topic, pushed, per_day, level_only, coalesced}],
   loudest: [.kinds[] | select(.pushed > 0) | {topic, kind, pushed, alerts_carried}]}'
```

The levers, in order: the trigger scan's daily page budget and per-kind cap,
the band batch's line cap, the level-only switch (`LEGBA_ALERT_SUPPRESS_LEVEL_ONLY=0`
pushes levels again), the per-sink cooldown. `LEGBA_ALERT_NTFY_TOPIC_SPLIT=0`
puts everything back on the single pre-split topic.

**A wedged actor; the actor plane looks slow.** A hung `activate()` holds its
actor's turn, and the reconciler's heal runs against every active analyst and
source on every resync, so one wedged actor can stall the rest. Two bounds work
together: a deadline on the caller (`LEGBA_RECONCILE_HEAL_TIMEOUT_SECONDS`) and
a bound on I/O inside the turn (`LEGBA_ACTOR_TURN_OP_TIMEOUT_SECONDS`), which is
what lets the turn finish. Some `reconcile.activate budget_exceeded` lines are
normal: the heal queues behind any run longer than 20 s that spans a resync.

```bash
docker logs --since 30m legba-legba-runtime-dapr-1 2>&1 | grep -E \
  'action_executor.deadline|action_executor.heal_suppressed|actor_turn.budget_exceeded|activate.deps_timeout|reminder.timeout'
```

Read `heal_phase` on `action_executor.deadline`:

- `queued_behind_turn`: another call held the turn (`held_by`, `held_for_s`); the actor is alive.
- `own_call_overran`: the activate itself ran long. `in_turn_op` names the bounded phase; `None`
  means an unbounded step, and `last_turn_op` says which phase it was past. Act on this one.
- `not_delivered`: no call reached this process; look at placement, the sidecar and the scheduler.

`heal_suppressed` means the breaker is open for that actor. `activate.deps_timeout` points at the
registry, not the actor. `reminder.timeout` is a scheduler problem, logged separately from
`reminder.invalid` (a descriptor error).

**A reactive fire logged as failed while the trace says success.** Actor turns are serial. A
re-fire sent to an actor already inside a turn waits behind it, and if the wait outlasts the invoke
timeout the dispatcher raises without learning the outcome. `trigger.coalesced_into_turn` (INFO)
means the actor was seen busy across the fire, so the work joined the running turn.
`trigger.run.failed` (ERROR) means no turn was seen; that work was lost. A rising coalesced count
means busy targets: adjust the analyst's cadence and `cooldown_seconds`, not
`LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS`.

```bash
docker logs --since 24h legba-legba-runtime-dapr-1 2>&1 | grep -cE \
  'trigger.coalesced_into_turn|trigger.run.failed'
```

**Reminders fire once at boot, then stop recurring.** Cursors freeze, no
signals, no findings, the scheduler looks healthy and nothing errors. The cause
is inconsistent embedded-etcd state in `deploy/dapr-scheduler-data/`: the cron
honours each reminder's `dueTime` but not its recurring `period`. Deleting only
the cluster-marker files leaves etcd inconsistent and re-breaks recurrence, so
the fix is a full wipe. The scheduler log must then read `No existing cluster
data found, deleting data dir contents` with `initial-cluster-state: "new"`, and
a scheduled — not boot-time — reminder must be seen to recur before calling it
fixed. Orphaned reminders from a retired actor do not need this: the reconcile
loop's GC sweep removes them once per resync, logs `reminder_gc.sweep …`, acts
only on retired actors, and has a steady-state removed count of 0.

```bash
docker compose --profile runtime stop legba-runtime-dapr dapr-sidecar dapr-scheduler
rm -rf deploy/dapr-scheduler-data/* deploy/dapr-scheduler-data/.[!.]*
docker compose --profile runtime up -d --force-recreate \
  dapr-scheduler dapr-sidecar legba-runtime-dapr
```

**A stack-component PUT that did not rebind a running actor.** Repointing a
component — a grader to another model, a search provider to another engine —
writes the new component, and a warm actor keeps its old binding for the life of
its deps bundle, so the PUT or flag flip alone changes nothing observable. Mint
a new descriptor head so the actor id changes, or recreate the runtime, then
confirm the binding on the next tick.

**A dead LLM model id.** A provider retires a slug upstream, every call on that
route fails, and the analysts behind it go quiet with no local defect. The
route-liveness check pages on it: under a 5% success rate over 24 hours across at
least 20 calls writes a severity-high `alert_sink_deliveries` row, exempt from
the daily page budget. Probe the ids before blaming cadence, then repoint the
stack component — a config-only PUT, no rebuild.

**Putting a stack component back.** A stack component's version is a hash of
its content, so a PUT whose body equals an earlier version re-heads that row
rather than writing a new one (audited `re_head`; a body equal to the current
head is a 409). To step back without restating the body,
`POST /api/v1/registry/stack/{component_id}/rollback` re-heads the version the
current head replaced — repeated calls walk further back — or the version named
in `{"version": "<hash>"}`, with an optional `reason`. It answers 404 for an
unknown component and 409 when the target is already the head, does not exist,
there is no earlier version, or the head or target is retired. Both paths
publish a stack event, so the handler cache evicts; a warm actor still needs
the rebind described above.

**JetStream on a streamless subject.** A publish to a subject no stream captures
blocks for about five seconds and is then swallowed, which reads downstream as
model slowness or a sluggish plane. Check the subject is captured by a declared
stream before profiling anything else.

**A weekend dip that looks like an outage.** Many publishers go quiet on
Saturday and Sunday, so the distinct-source count and the signal rate
both fall with nothing broken. Compare against the previous weekend and read the
poll outcomes before chasing a stall: a clean `empty` outcome is a quiet feed. A feed permanently dead but still polling cleanly escapes that exemption
once its clean-empty streak passes the configured threshold, and is tagged
`honest_quiet_prolonged`.

```bash
docker compose exec -T postgres psql -U legba -d legba -At -c \
  "select date_trunc('day', fetched_at) d, count(*), count(distinct source_id)
     from signals where fetched_at > now() - interval '14 days' group by 1 order by 1"
```

**Disk at flood stage.** Root past 90% is within reach of OpenSearch's 95%
flood-stage watermark, past which every shard goes read-only silently — corpus
indexing and the search surfaces reading it stop, with no signpost back to the
cause. Run the three reclaim levers in §4 before the watermark decides for you.

**The registry 500s on a runtime import.** The registry image is deliberately
slim: it carries the control plane, not the runtime's parsing and numeric stack.
A registry route importing a runtime handler — even lazily — returns 500 live
while every test passes, because the test image has both. Keep registry route
modules leaf modules, keep the poisoned-import guard test green, and curl the
route after the deploy; the boot log does not prove it.

**A stale registry after a shared-schema change.** Any change under
`src/legba/data/schemas/` used by a descriptor body needs both images rebuilt. A
stale registry 500s `/typed`, the runtime records `activate.no_deps`, the
reminder GC drops the now-unbacked reminder, and the analyst silently stops
firing. This is the failure the deploy ordering prevents and
`rolling_deploy.sh` enforces. Verify before relying on activation:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  $REG/api/v1/registry/descriptors/analyst/world_assessor/typed
```

**Caddy serves no certificate.** The ACME state is corrupt: stop caddy, clear
it, restart, and it re-obtains a fresh certificate.

```bash
docker compose stop legba-caddy
docker run --rm -v legba_caddy_data:/data alpine sh -c \
  'rm -rf /data/caddy/acme /data/caddy/certificates /data/caddy/locks'
docker compose up -d legba-caddy
```

**Before any maintenance**, quiet the host layer so no watchdog restarts a
container mid-deploy. And when checking what an image actually runs, verify by
import rather than by path: the images install the `legba` package into
site-packages with no repo-style copy of the code, so grepping a repo path
inside a container proves nothing.

```bash
touch /etc/legba-watchdog.disabled        # the stall watchdog, the heartbeats, the log collector
touch /etc/legba-nightly-suite.disabled   # the nightly suite alone
docker exec legba-legba-runtime-dapr-1 python -c \
  "import inspect, legba.data.alerts.sinks as m; print(m.__file__, 'suppressed_in_cooldown' in inspect.getsource(m))"
```

---

## 6. Release

### The gate

`scripts/release_gate.sh` composes every existing gate into one driver so a cut
cannot skip a step. It stops at the first failure and writes
`release/gate-<utc>.log`.

```bash
bash scripts/release_gate.sh
SKIP_SMOKE=1 bash scripts/release_gate.sh    # no live stack to smoke
SKIP_UI=1    bash scripts/release_gate.sh    # no docker for the UI build
```

| # | Stage | Pass condition |
|---|---|---|
| 1 | Strict test suite | green under `LEGBA_TEST_STRICT=1`, which escalates infra-gated skips to failures so coverage cannot be lost silently |
| 2 | No-stub gate | zero stub or mock markers in `src/`; a deferred item is a fail-loud declared seam in `SEAMS.md`, never a silent stub |
| 3 | Descriptor validation | every descriptor parses and type-checks, falling back to a typed load of every YAML when the validator script is absent |
| 4 | UI build | the container build is the type-check — a tsc error fails the build |
| 5 | Secret and codename scan | `scripts/prepush_scan.sh` exits zero |
| 6 | Release manifest | `scripts/make_release_manifest.sh` writes `release/manifest-<gitsha>.txt` |
| 7 | Deployed-stack smoke | the 401/403/200 bearer pattern, a non-empty migration ledger, the caddy edge serving |

Compose carries some third-party images on floating tags for dev velocity, and
the manifest freezes the exact answer at tag time: resolved digests, the real
`pip freeze` out of the built runtime image, the UI lockfile hash, the migration
baseline. Postgres is the exception and is pinned by digest, because that image
holds every row of truth in the system. Rolling it forward means re-reading the
digest, bumping the line, and re-running `scripts/age_probe/run_probe.sh` so the
engine's behaviour is measured before it reaches the data.

### The scan

`scripts/prepush_scan.sh` scans tracked content plus the about-to-push commit
range and exits non-zero on a tracked `.env` or secrets file, private-key
material, a `PASS`, `SECRET`, `TOKEN` or `API_KEY` assigned a long literal, a
high-entropy literal, or a tracked `planning/` or `corpora/` file. It also runs
`gitleaks` when that is on the path.

```bash
bash scripts/prepush_scan.sh
BASE=origin/main bash scripts/prepush_scan.sh
```

### Pre-tag checklist

- `bash scripts/release_gate.sh` green; the gate log committed under `release/`.
- `scripts/prepush_scan.sh` clean.
- `release/manifest-<gitsha>.txt` regenerated and committed.
- `RELEASE_STATE.md` and the `RELEASE_STATE_MATRIX.md` panel table regenerated.
- Migrations applied and the ledger verified.
- A persistent signing key and a production bearer token in `.env`; the boot log shows no ephemeral-key warning.
- The models host's inference port confirmed loopback- or network-internal-only: `docker compose port legba-models 8700` shows no public bind.
