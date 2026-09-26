<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Runbook — procedures for a running instance

Six things an operator does: start and stop the stack, ship a change, tell
whether it is healthy, the weekly tasks, the failures this platform has actually
shown, and cut a release. Standing an instance up from empty volumes is
`SETUP.md`; every knob and its live value is `TUNABLES.md`; what is live, off or
untested is `STATUS.md`; the incidents and retired eras behind the rules below
are `history/RUNBOOK_extracts.md`.

Commands run from the repository root, `/usr/local/deployments/active/legba`:

```bash
REG=http://127.0.0.1:8090
TOKEN=$(grep '^LEGBA_REGISTRY_API_TOKEN=' .env | cut -d= -f2-)
```

---

## 1. Start, stop, restart

| Layer | Services | Profile |
|---|---|---|
| Substrate | `postgres` (Postgres + AGE), `redis`, `qdrant`, `nats`, `opensearch` | none — always on |
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
| `--dry-run` | print the plan and the guard readout, touch nothing |

Exit codes: `2` image-age guard, `3` a recreated container never reported
healthy, `4` migration failed, `5` marker verification failed.

Three properties are load-bearing, each answering a way a deploy has silently
not happened. **The guard is on images, never logs** — a container logs a fresh
boot line while running week-old code, so the guard reads image creation times
before touching any container. **The worker is built from the runtime image, so
it is built after it** — `docker/Dockerfile.worker` is a thin layer over
`legba/legba-runtime-dapr`, and a worker built first ships old code under an
image whose own timestamp looks new, which only per-file marker verification
inside the worker container catches. **The worker build carries its profiles** —
with no profile set it fails on a sibling service behind the same profile, and
some compose versions print nothing that reads as an error, so the script runs
it under `COMPOSE_PROFILES=runtime,dapr-workflow`.

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
actors keep serving and everything reads green while the fleet cannot activate.
It separates "no trace" (cold-activation failure) from "failed trace"
(activated, run died) and exits non-zero loudly.

### Stand up a fresh instance

`deploy/deploy.sh` is the one-command bring-up: substrate and Dapr → baseline
schema plus future migrations → registry, then vault, stack, packs, sources,
catalog, targets, analysts, budget → optional seeds → runtime and UI →
boot-verify. It neither calls nor is called by `rolling_deploy.sh`.

```bash
docker compose --profile runtime build
deploy/deploy.sh

# a throwaway validation stack, fully isolated from the live volumes
deploy/deploy.sh --project legba_val --no-caddy --seed
deploy/deploy.sh --project legba_val --teardown   # down -v, only the legba_val_* volumes
```

It applies the round-trip-proven baseline `deploy/baseline/0001_baseline.sql` —
schema, AGE graph, ledger pre-seeded — then `python -m legba.data.migrate` for
everything after it; the runner discovers migrations by sorted glob, so gaps in
the numbering are harmless. Seeds run before the runtime boots, for the reason
under the boot signposts; curated seed data is operator-provided, and an absent
file makes its adapter no-op, so `--seed` degrades cleanly.
`deploy/compose.isolation.yml` re-declares every named volume and the scheduler
bind under the project name, and `deploy.sh` renders the compose config and
aborts before any `up` if a reference to a real `legba_*` volume survives. On
the real `legba` project `--teardown` only stops containers and refuses
`down -v`.

### The UI and the models host

```bash
docker compose --profile ui build legba-ui-build
docker compose --profile ui up -d --force-recreate legba-ui-build
docker compose --profile runtime restart legba-caddy
```

Caddy serves from the shared `ui_dist` volume the one-shot build republishes.
`legba-models` — vLLM, embeddings, NER, translation — runs on a separate GPU
host and deploys from its own checkout there: rsync the changed files, rebuild
and recreate on that host, then confirm through the runtime's NLP client rather
than the port, which is not published off that host's docker network.

---

## 3. Is it healthy

| Check | Fine looks like |
|---|---|
| Containers | every service `running`; `legba-registry` and `legba-runtime-dapr` `healthy` |
| Registry | `{"status":"ok","checks":{"postgres":"ok","nats":"ok"}}` |
| Runtime | HTTP 200 |
| Ingestion freshness | newest signal under 30 minutes old — the measured worst-case inter-signal gap over a week, and the threshold the host stall watchdog acts on |
| Source fan-out | distinct producing sources over 24 h, about 105 on this deployment |
| Poll outcomes | `error` a small tail beside `success` and `empty`; `empty` is the normal outcome for a feed with nothing new |
| Analyst production | findings, critiques, alerts and scorecards all present over 24 h, in the hundreds. A kind at zero is a dead plane |
| Judge band | mean faithfulness around 0.83 today; movement toward the 0.50 composition verify floor is the signal, not the absolute value |
| Grader receipts | non-zero rows in 24 h — the correctness instrument ran |
| Fleet canaries | `world_assessor`, `journal_assessor` and `journal_consolidator` each produced recently |
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

**The world voice is the fleet canary** — a six-hour cadence on a single global
run, so it goes quiet first when the registry cannot serve `/typed`. The
journal's two tiers need their own canaries for the same reason: a global-run
analyst has no target to flag its death, and the daily consolidator hides it
longest.

**Disk is the silent one.** OpenSearch's flood-stage watermark is 95%, past
which it puts every shard into a read-only block silently — no crash, no error,
corpus indexing simply stops while every other plane keeps running. The LLM
heartbeat pages at 85% and again at 92%, which is the warning before that.

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
```

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

### Register, activate, pause or retire a descriptor

The registry API takes JSON, so convert the descriptor YAML first. A PUT stamps
a fresh content hash, preserves history, emits `descriptor.updated.<family>.<id>`
on NATS, and atomically retires the prior version's UI panel rows. The reconcile
loop picks the change up within a resync interval, and a descriptor that ships
`state: draft` stays inert until an operator flips it deliberately. Deterministic
handlers read their thresholds from a `method.options` block on the descriptor,
so a threshold change needs no rebuild and no recreate; precedence runs
runtime-stamped provenance, then a forced run's `payload.options`, then
`method.options`, then the handler's own default. An undeclared or out-of-range
key is dropped loudly and the handler default stands, so a mistyped knob never
takes a class offline; the rejection lands in the trace as a `handler_options`
step, `applied` or `degraded`, listing every dropped key with its cause.

Every shipped analyst has an idempotent registrar under `scripts/` named for it, `bringup_register_<analyst>.py` (for example `bringup_register_correctness_grader.py`, `bringup_register_reference_builder.py`, `bringup_register_standing_auditor.py`); each reads the descriptor from `descriptors/`, registers it, and leaves activation to the lifecycle route. `deploy/deploy.sh` runs the set a fresh instance needs.

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

New source descriptors live under `descriptors/` and register through the
registrar owning their batch (`bringup_register_sources.py`,
`bringup_register_source_catalog.py`, `bringup_register_supply_chain_sources.py`,
`bringup_register_wave_a_sources.py`, `bringup_register_rsshub_sources.py`). All
are idempotent; the direct-to-database ones default to the `legba_pivot_test`
database, so pin `LEGBA_DATA_PG_DB=legba`. Batches layered on the base catalog
ship `draft` and are activated one at a time. Dropping a source is the pause or
retire transition above: pausing keeps its signals and its cursor, retiring
releases its reminders through the orphan GC sweep.

```bash
# probe and parse-check first — prints a verdict table, registers nothing
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_source_catalog.py --verify
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_source_catalog.py
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

`scripts/backup.sh` covers the four stateful stores — Postgres including the AGE
graph, Redis, Qdrant, NATS JetStream — and is safe to run while the stack
cycles. Output lands under `/var/backups/legba/<timestamp>/`. The OpenSearch
corpus index is deliberately not backed up: it is a projection of `signals`,
rebuilt by `corpus_indexer` through its dirty-marker path. Offsite stays off
until `LEGBA_BACKUP_OFFSITE_DEST` and `LEGBA_BACKUP_OFFSITE_TOOL` are set in
`.env`; until then the wrapper warns and drops an `OFFSITE_NOT_CONFIGURED.txt`
marker into the generation directory, and a configured-but-failing push exits
non-zero, so a failure is never silent. Restore into a scratch compose project,
never the live one, and run the drill periodically — an untested backup is not a
backup. It passes when the registry comes up healthy, `signals` and
`analyst_outputs` row counts match the source rig within tolerance, and a
fan-out produces a fresh finding.

```bash
bash scripts/backup.sh              # all four
bash scripts/backup.sh pg nats      # a subset

# scheduled, with retention and an offsite push
sudo cp deploy/systemd/legba-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now legba-backup.timer   # nightly 03:30 plus jitter
```

```bash
GEN=/var/backups/legba/<timestamp>; P="docker compose -p legba_restore"

gunzip -c "$GEN/postgres_legba.sql.gz" | $P exec -T postgres psql -U legba -d legba
docker cp "$GEN/redis_dump.rdb" "$($P ps -q redis)":/data/dump.rdb && $P restart redis

for f in "$GEN"/qdrant/*; do                       # one snapshot per collection
  coll="$(basename "$f" | sed 's/_[^_]*$//')"
  curl -sf -X PUT "http://127.0.0.1:6333/collections/${coll}/snapshots/recover" \
    -H 'content-type: application/json' -d "{\"location\":\"file://$(realpath "$f")\"}"
done

tar -xzf "$GEN/nats.tar.gz" -C "$GEN"              # one restore per JetStream stream
for s in "$GEN"/nats/*/; do
  nats --server nats://127.0.0.1:4222 stream restore "$(basename "$s")" "$s"
done
```

### Regenerate the generated docs

The panel table in `RELEASE_STATE_MATRIX.md` and the whole of
`RELEASE_STATE.md` are generated and must not be hand-edited; a drift test fails
the suite over the first. Run the second after registering or retiring a source,
desk, unit or migration.

```bash
python3 scripts/gen_release_state_matrix.py            # regenerate in place
python3 scripts/gen_release_state_matrix.py --check    # exit 1 if stale
PYTHONPATH=src python3 scripts/generate_release_manifest.py
```

---

## 5. When something is wrong

**A wedged actor; the plane looks slow.** A hung `activate()` eats the reconcile
loop's 90-second `run_once` bound and holds its actor's turn, and because Dapr
actors are turn-based with reentrancy off and the reconciler's durability heal
fires against every active analyst and source on every resync, one cycle can
turn-poison the whole plane. Two bounds exist as a pair: a deadline on the caller
stops the queue paying for a wedged actor, and a bound on the hang-prone I/O
inside the turn is what makes the turn complete so the queue behind it drains —
a deadline on the caller alone does not release the callee's turn. The heal
deadline must stay well below the loop's 90-second bound, and a test pins the
ratio. Steady state emits none of the markers below.

```bash
docker logs --since 30m legba-legba-runtime-dapr-1 2>&1 | grep -E \
  'action_executor.deadline|action_executor.heal_suppressed|actor_turn.budget_exceeded|activate.deps_timeout|reminder.timeout'
```

`action_executor.deadline` carries the consecutive count;
`heal_suppressed` means the breaker is open for that actor, and fleet-wide means
the actors are not answering while the loop survives it rather than freezing;
`actor_turn.budget_exceeded` means an op inside a turn was released and the turn
completed; `activate.deps_timeout` points at registry load, not the actor; and
`reminder.timeout` is logged separately from `reminder.invalid` on purpose, so a
scheduler-plane problem is never misread as a descriptor typo.

**A reactive fire that logged a failure the trace calls a success.** Actor turns
are serial, so a coalesced re-fire dispatched onto a worker already inside a turn
— its cadence fan-out run, a heal, an earlier fire — waits behind it, and when
that wait outlasts the invoke line the dispatcher's invoke raises. It never
learned the outcome, which is not the same as learning that it failed: the
analyst reports its own failures by returning an outcome, and its
`analyst_traces` row reads `success`. The trigger plane now says which it was.
`trigger.coalesced_into_turn`, at INFO, means the actor turn witness saw that
actor id occupied across the fire, so the batch is the running turn's;
`witness=in_flight` is a turn still running, `witness=ended_after_fire` one that
finished after the fire was raised, and the transport error that ended the
invoke is carried on the same line. `trigger.run.failed`, at ERROR, is now
reserved for a dispatch with no turn behind it — work that really was lost, and
worth waking for. The witness is process-local to the actor host and claims
nothing it did not observe, so an unwitnessed dispatch degrades to `run.failed`
rather than to a false reassurance. Neither line is a reason to widen
`LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS`: a rising coalesced rate is a statement
about turn occupancy — cadence fan-out colliding with reactive fires on the same
busy targets — and the knobs for that are the analyst's cadence and its
`cooldown_seconds`, not the invoke line.

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
confirm the binding on the next tick rather than assuming it.

**A dead LLM model id.** A provider retires a slug upstream, every call on that
route fails, and the analysts behind it go quiet with no local defect. The
route-liveness check pages on it: under a 5% success rate over 24 hours across at
least 20 calls writes a severity-high `alert_sink_deliveries` row, exempt from
the daily page budget. Probe the ids before blaming cadence, then repoint the
stack component — a config-only PUT, no rebuild.

**JetStream on a streamless subject.** A publish to a subject no stream captures
blocks for about five seconds and is then swallowed, which reads downstream as
model slowness or a sluggish plane. Check the subject is captured by a declared
stream before profiling anything else.

**A weekend dip that looks like an outage.** Around thirty weekday publishers go
quiet on Saturday and Sunday, so the distinct-source count and the signal rate
both fall with nothing broken. Compare against the previous weekend and read the
poll outcomes before chasing a stall: a clean `empty` outcome is an honest quiet
feed. A feed permanently dead but still polling cleanly escapes that exemption
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
route after the roll rather than trusting the boot log.

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

### The scan and the neutral-identity squash

The remote is public. `scripts/prepush_scan.sh` scans tracked content plus the
about-to-push commit range, and exits non-zero on any of: a prior-host codename
or private infra-host fragment in tracked content; the operator domain; a tracked
`.env` or secrets file; private-key material; a `PASS`, `SECRET`, `TOKEN` or
`API_KEY` assigned a long literal; a high-entropy literal; a non-neutral commit
author or committer identity; a tracked `planning/` file; a tracked `corpora/`
file. It also runs `gitleaks` when that is on the path. `mnemosyne` is
deliberately not scanned for: it names the federation peer this platform makes
trust-query calls to, not a stray codename.

Commit metadata on the branch carries identities that should not reach a public
remote, and the same metadata is already on `origin/main`. Rewriting published
history breaks every clone, so decline that and mint one release commit with the
neutral identity, so the new tip carries none of it. Draft the `CHANGELOG.md`
entry before the squash, not after: public history is squashed per release, so
the changelog is the only public release record, and writing the entry is what
forces the "what exactly is in this push?" review. It rides inside the release
commit, in public-docs vocabulary only — no hosts, no codenames, nothing not
already public.

```bash
bash scripts/prepush_scan.sh
BASE=origin/main bash scripts/prepush_scan.sh

# 0. confirm the git identity before anything else
git config user.name  "legba-dev"
git config user.email "dev@legba.invalid"

# 1. stage the whole delta as one diff against the public base. Do not use
#    `-p main` style parenting — it keeps the old-identity parents reachable.
git checkout -B release <working-branch>
git reset --soft origin/main

# 2. re-commit as a single neutral-identity commit
git commit --no-gpg-sign --author="legba-dev <dev@legba.invalid>" \
  -m "Release: <summary> (squashed)"

# 3. re-run the scan against the squashed tip; the identity check must be clean
BASE=origin/main bash scripts/prepush_scan.sh
```

### Pre-tag checklist

- `bash scripts/release_gate.sh` green; the gate log committed under `release/`.
- `scripts/prepush_scan.sh` content checks clean; the commit identity resolved by the squash.
- `release/manifest-<gitsha>.txt` regenerated and committed.
- `RELEASE_STATE.md` and the `RELEASE_STATE_MATRIX.md` panel table regenerated.
- Migrations applied and the ledger verified.
- A persistent signing key and a production bearer token in `.env`; the boot log shows no ephemeral-key warning.
- The models host's inference port confirmed loopback- or network-internal-only: `docker compose port legba-models 8700` shows no public bind.

The push is the operator's, always, and is never automated.

```bash
git push origin release   # operator only
```
