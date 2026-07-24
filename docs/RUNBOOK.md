# Legba Runbook

Operator-facing reference for running Legba day-to-day. Lives next to
the substrate compose file. Concise on purpose — links to the deeper
specs under `docs/` for context. New here? Start with the
[README](../README.md) and [SETUP.md](SETUP.md).

**Contents:**
[0 Critical operator notes](#0-critical-operator-notes-read-these-first) ·
[1 Stack at a glance](#1-stack-at-a-glance) ·
[2 Bring-up](#2-bring-up-everything-canonical-container-mode) ·
[3 Migrations](#3-apply-migrations) ·
[4 Verify the registry](#4-verify-the-registry) ·
[5 Verify the UI](#5-verify-the-ui) ·
[6 Vault credentials](#6-load-credentials-into-the-vault) ·
[7 Stack components + working set](#7-register-stack-components--the-working-set) ·
[8 Per-image troubleshooting](#8-per-image-troubleshooting) ·
[9 Verify ingestion](#9-verify-ingestion-is-working) ·
[10 Bring it all down](#10-bring-it-all-down) ·
[11 Common operator tasks](#11-common-operator-tasks) ·
[12 Known issues](#12-known-issues-as-of-2026-05-23) ·
[13 Host-mode systemd](#13-alternative-host-mode-systemd) ·
[14 File layout](#14-file-layout-cheat-sheet) ·
[15 Multi-image split](#15-notes-on-the-multi-image-split-2026-05-23) ·
[16 Bring-up lessons](#16-lessons-from-the-2026-05-21-bring-up-host-mode-era) ·
[17 Source-first runtime bring-up](#17-source-first-runtime-bring-up) ·
[18 Release gate](#18-release-gate-ordered-fail-fast) ·
[19 Codename scan](#19-codename--prior-host-scan-findings-2026-06) ·
[20 Pre-push scan + squash](#20-pre-push-secretcodename-scan--neutral-identity-squash) ·
[21 Release checklist](#21-release-checklist-pre-tag) ·
[22 Multi-replica proof](#22-multi-replica-local-proof-scaling-multinode) ·
[23 Backup & restore](#23-backup--restore-resilience-observability-w-1b-5)

## 0. Critical operator notes (read these first)

- **DO NOT `git push`. The remote (`github.com/ldgeorge85/legba`) is PUBLIC.** Commit locally only; never push unless explicitly intended. (Operational-incident history and credential-rotation records are kept in the operator's internal tracking, not in this public doc.) The Caddy `basic_auth` bcrypt hash is read from the `LEGBA_BASIC_AUTH_HASH` env var in gitignored `.env` (never committed). To rotate: `docker exec legba-legba-caddy-1 caddy hash-password --plaintext '<new>'` → set it in `.env` — **single-quote it or `$$`-escape the `$` characters**, else docker-compose `env_file` interpolation mangles the hash and every password is rejected — → `docker compose up -d --force-recreate --no-deps legba-caddy`.

- **⚠️ Dapr runtime restarts: NEVER restart `legba-runtime-dapr` ALONE (2026-06-08).** The app and its daprd `dapr-sidecar` are separate containers; restarting only the app while the sidecar (and `dapr-placement`) keep running leaves the actor-host registration STALE → every reminder/invocation fails with `did not find address for actor` / `actor is closed` / `context canceled` (in the **SIDECAR** log, not the app log), so `run()` is killed before it pulls/assesses → the loop goes SILENT with no app-level error. This is what stalled ingestion 2026-06-05→08. **Always restart the control plane TOGETHER, dependency-ordered:** `docker compose --profile runtime up -d --force-recreate dapr-placement dapr-scheduler dapr-sidecar legba-runtime-dapr`.

- **⚠️ Silent loop stall: reminders fire once at boot then stop recurring; cursors freeze, no signals/findings, scheduler looks healthy, 0 errors (root-caused 2026-06-09).** ROOT CAUSE: **corrupted/inconsistent `dapr-scheduler` embedded-etcd state** in the persistent bind-mount `deploy/dapr-scheduler-data/`. The scheduler's cron then fires each actor reminder once at its `dueTime` but does NOT honor the recurring `period` → the whole loop (source polls + analyst cadence) goes silent after the first fire. **NOT a Dapr bug and NOT our reminder usage** — `register_reminder(due_time, period)` is correct per the Dapr docs, and a clean-format reminder reproduces fine on fresh etcd. **It is NOT enough to delete only the cluster-marker files** (`dapr-scheduler-existing-cluster` / `default-dapr-scheduler-server-0`) — a PARTIAL clear leaves etcd inconsistent ("Found existing cluster data, preserving…") and re-breaks recurrence. **FIX = FULL wipe of the data dir:**
  ```
  docker compose --profile runtime stop legba-runtime-dapr dapr-sidecar dapr-scheduler
  rm -rf deploy/dapr-scheduler-data/* deploy/dapr-scheduler-data/.[!.]*
  docker compose --profile runtime up -d --force-recreate dapr-scheduler dapr-sidecar legba-runtime-dapr
  ```
  Verify the scheduler log shows **`No existing cluster data found, deleting data dir contents`** (truly fresh) + `initial-cluster-state: "new"`, then that a *scheduled* (non-boot) cron reminder recurs (e.g. a 15s test reminder fires every 15s) and signals/findings flow. (Confirmed 2026-06-09: fresh etcd → source polls produced 67 signals + analyst cadence produced findings; recurrence held.) Minor follow-up: the dapr-python SDK serializes the reminder period as `0h10m0s0ms0μs` (Greek-mu µs), which the scheduler honors but with a small 4-fire burst per period (absorbed by per-(analyst,target) cooldown/dedup) — optionally register reminders with a clean Go-duration period (`10m`) to avoid the burst.

- **⚠️ Do NOT run heavy multi-agent / build fan-outs on the live-stack host (2026-06-08).** Spawning many concurrent docker build/test containers contends for host resources and destabilizes daprd placement/scheduler (actor errors clustered in the fan-out window, cleared when it ended). Run heavy fan-outs elsewhere, or pause the live runtime during them.

- **Caddy serves no TLS cert / browser SSL error** → the ACME state is corrupt (e.g. LE prod cert-download 404 + stale staging account). Fix: stop caddy, clear the ACME state, restart — it re-obtains a fresh Let's Encrypt prod cert:
  ```
  docker compose stop legba-caddy
  docker run --rm -v legba_caddy_data:/data alpine sh -c 'rm -rf /data/caddy/acme /data/caddy/certificates /data/caddy/locks'
  docker compose up -d legba-caddy
  # verify: curl -svk --resolve "$LEGBA_PUBLIC_DOMAIN:443:127.0.0.1" "https://$LEGBA_PUBLIC_DOMAIN/"  → issuer Let's Encrypt, 401 (basic_auth)
  ```

- **Entity knowledge-graph** — the NER filter populates `signals.payload.entities`, and the ongoing `entity_resolution` deterministic analyst auto-resolves them into the entity substrate (`entity_profiles`/`signal_entity_links`/`proposed_edges`) on its cadence. To backfill / re-run it manually (idempotent) for the Entities / Entity-Graph panels + `/api/v1/entities*`:
  ```
  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \
    LEGBA_DATA_PG_USER=legba LEGBA_DATA_PG_PASSWORD=legba \
    python3 scripts/backfill_entity_graph.py
  ```
  (Fast-follow: wire this as an ongoing deterministic analyst so new signals auto-link.)

- **⚠️ Seed the stack BEFORE the runtime boots — or restart the runtime after seeding (2026-06-09).** The runtime builds its NLP / embedding clients **once at boot** from the registered stack components. Bring the runtime up against an empty/un-seeded registry (e.g. a fresh DB) and seed the stack *afterwards*, and `nlp_client` stays `None` for the whole process lifetime → source enrichment fails to build (`source_deps_resolver.enrichment_build_failed … requires an nlp_client_factory`) → signals land with **no `geo`/entities** → geo-scoped analysts have nothing to match. **Order: migrate → register vault + stack → THEN bring up (or `--force-recreate`) the runtime.** Verify `dapr_host.nlp_client.built component=nlp.local.legba_models` and zero `enrichment_build_failed` in the runtime log.

- **⚠️ Reactive triggers silent (`trigger_state=0`) while signals flow and cadence still produces findings (root-caused 2026-06-09).** The published signal envelope must carry the same `owner_tenant` as the source's `scope.owner_tenant` (which is also the subject token and the subscription binding). The real-time matcher (`subscription/filter.matches`) rejects on an `owner_tenant` mismatch, so an envelope stamped with the model default (`default`) is **delivered** by the durable consumer but **matches zero** → no `(analyst, target)` pair goes dirty → no reactive fire, even though the batch/cadence path (which reads the correctly-stamped DB row) keeps producing findings. Fixed in `source_actor` (stamp the in-memory Signal before write + publish). Diagnose by sampling a published message's `owner_tenant` vs the source's scope tenant, and checking the trigger consumer's `delivered` vs `matched`.

- **Clean-slate only — no migration path from pre-pivot Legba.** This is a complete refactor from the v1/v2 target-first design; the data model, substrate schema, and APIs are incompatible with pre-pivot instances. There is no upgrade or data-migration path: stand up a fresh empty substrate (Postgres+AGE, NATS JetStream, Qdrant, Redis) and apply migrations from `0001_baseline` forward. **Do not point this build at a pre-pivot database.**

- **⚠️ Rebuild BOTH `legba-registry` AND `legba-runtime-dapr` on any `data/schemas/*` change (root-caused 2026-06-20).** A `src/legba/data/schemas/*` change (e.g. adding a `Literal` value) used by any descriptor body requires rebuilding **both** images. A stale registry 500s on `/typed` → the runtime gets `activate.no_deps` → `reminder_gc` drops the now-unbacked reminder → **the analyst silently stops firing**. Caught live: a situations-grounding deploy 500'd both grounded assessors, so no world/country assessment landed for ~24h. **`world_assessor` (6h cadence) is the canary** — if it stops producing, suspect a stale registry first. After any shared-schema change: `docker compose --profile runtime build legba-registry legba-runtime-dapr` then `up -d --force-recreate legba-registry legba-runtime-dapr`, and verify the registry serves `/typed` 200 for an affected descriptor before relying on activation.

- **⚠️ The journal needs its OWN dead-analyst canaries — two of them.** The journal assessor (Legba's first-person reflective voice) runs as **two META single-global-run analysts** (`target_filter=None`, like `world_assessor`) that SHARE one extension analyst kind, `journal_assessor` (registered via `register_analyst_kind` + the vocabulary-entries family — NOT a built-in `AnalystKind`; the built-in-kind count is unchanged): the **entry tier** (`journal_assessor`, descriptor `descriptors/analyst_journal_assessor.yaml`, cadence `0 0,12 * * *` = every 12h) and the **consolidation tier** (`journal_consolidator`, `descriptors/analyst_journal_consolidator.yaml`, cadence `0 2 * * *` = daily 02:00 UTC). Each global-run analyst can go silently dead WITHOUT a target to flag it, so **each needs its OWN activation canary** — the entry-tier canary exercises a DIFFERENT actor id than the consolidator, so a green entry tier does NOT prove the consolidator is alive. **The daily consolidator hides death the LONGEST** (a 24h beat); watch its `produced_at` directly. Producer output lands in the dedicated `journal_entries` table (migration **0048**, which also adds `journal_proposals`) — NOT `analyst_outputs` — and is **OFF the fact/finding/nexus chain**: a journal row is a *perspective OVER* the provenance chain, carrying an always-empty `derived_from` and deliberately excluded from the lineage catalog, so a `GET /api/v1/lineage/...` walk can never surface it. The two analysts are granted ONLY the `journal_read` (14 read tools incl. 9 self-instruments) + `journal_propose` packs (both non-write-fact — the grant-layer backstop for the never-write-a-fact invariant). Register them with `scripts/bringup_register_action_packs.py` (packs) + `scripts/bringup_register_analysts.py` (descriptors + the `journal_assessor` kind). The journal writes ONLY its own entries/consolidations directly; every outward effect (a correction, a change, or a self-revision) goes to the **human-gated `journal_proposals` queue**, never a live table — operator review happens via the accept/reject routes (`GET /api/v1/journal_proposals?status=pending`, `POST /api/v1/journal_proposals/{id}/accept`, `POST /api/v1/journal_proposals/{id}/reject` — reject REQUIRES a `decision_reason`; a `self_revision` touching a protected section auto-rejects on accept). Entries themselves render via `GET /api/v1/journal` + the `system.journal` UI panel.

- **Deploy a fresh instance to CURRENT scope (not just the 3-feed cold-start).** The canonical one-command path (`deploy/deploy.sh --seed`, §2) does all of this; the steps below are what it automates, for reference / partial re-runs. The minimal cold-start verification set is 3 shared world-news sources (BBC / Deutsche Welle / Al Jazeera) — that is the cold-start *smoke test*, NOT the deployed scope and NOT a proven-live limit. The live system runs the full source catalog (the catalog defines 46 handler integrations in `scripts/bringup_register_source_catalog.py`; ~57 registered source descriptors, ~50 live/active including seed/baseline plus the standalone state-media feeds IRNA / PressTV / Ukrinform and the UCDP GED adapter — the latter currently **paused pending an access token**). To stand a fresh instance up to current scope:
  1. Empty substrate up + schema (§2–§3): a fresh deploy applies the single proven baseline `deploy/baseline/0001_baseline.sql` (ledger pre-seeded to head **0053**), then `migrate` applies any future (`0054`+) migrations — currently `0054`…`0085` (live head **0085**).
  2. Vault + stack components (§6–§7), then the source-first working set — packs, the 3 minimal sources, 19 G20 targets, the analysts. **`deploy.sh` registers the LIVE analysis spine via the split registrars** — `bringup_register_analysts.py` registers the seven bounded units + the composition tower (`country_composition` / `region_composition` / `world_assessor` / thematic `escalation_composition`) + the deterministic I&W pair (`indicator_tracker` / `collection_gap`); `bringup_register_watch_country_targets.py` adds the 6-desk watch tier; `bringup_register_region_targets.py` adds the 5 region frames. (The older combined `scripts/bringup_register_p17_workingset.py` is a **frozen legacy path** that registers the RETIRED `country_assessor` monolith set — it does NOT bring up the current spine; prefer `deploy.sh`.)
  3. **Then the FULL source catalog** — run `scripts/bringup_register_source_catalog.py` to register the 46-source catalog (this is what takes the instance from the 3-feed cold-start to current scope), plus the deterministic cadence analysts + the budget envelope (§7).
  4. Seed the knowledge roots (§7.2) and verify ingestion (§9). A current-scope instance reaches order-of-magnitude tens-of-thousands of signals and tens-of-thousands of findings — the 3-feed set will not.

- **⚠️ Dapr long-activity workflow round-trip degrades to in-process (daprd 1.17.9, SEAM #23).** The durable Dapr Workflow does not resume the orchestrator after a *long* activity, so the GEPA optimizer and `deep_consult` run via their **in-process fallback** instead of the durable path. Results still complete (look for `optimizer_workflow.in_process` in the boot log, §4.2); what is lost is durable, externally-resumable execution, not output. The compile-hang sub-issue is FIXED (observable `workflow_timeout` trace). See `docs/SEAMS.md` #23.

## 1. Stack at a glance

* **Substrate (always on, no profile):** Postgres+AGE, Qdrant, Redis,
  NATS+JetStream. Defined in `docker-compose.yml`.
* **Dapr (profile `dapr`):** placement, scheduler, daprd-sidecar.
  Daprd's app-channel target is `legba-runtime-dapr` (the runtime
  container on the same compose network).
* **Durable workflows:** the optimizer analyst kind's durable GEPA workflow
  runs as a **Dapr Workflow** on the daprd sidecar (started in-process by
  `legba-runtime-dapr`). An optional scale-out workflow worker is available
  under **profile `dapr-workflow`** (`legba-dapr-workflow-worker`). There is
  no Temporal cluster, worker, or dependency anywhere in the stack.
* **App images (profile `runtime`):** `legba-registry`,
  `legba-runtime-dapr`, `legba-ui-build`
  (one-shot SPA build), `legba-caddy` (serves the UI + reverse-proxies
  the registry).
* **MCP image (profile `mcp`):** `legba-mcp` — stdio MCP server
  launched per-conversation by the MCP client (e.g. Claude Code) via
  `docker run -i --rm`.

The 2026-05-23 multi-image containerization makes container-mode the
canonical bring-up. Host-mode systemd units are documented as an
alternative in section 13.

## 2. Bring up everything (canonical, container-mode)

> **Canonical one-command bring-up: [`deploy/deploy.sh`](../deploy/deploy.sh).**
> After building the images, it runs the entire phased, idempotent, boot-verified
> sequence (schema via the single baseline → ordered registrars → optional seeds →
> runtime → verify). The `up -d` form below brings the **already-provisioned** stack
> up; on a FRESH/empty substrate use the script (or follow §3–§7 in order — the
> ordering matters: the runtime must boot LAST against a seeded registry).
> ```
> docker compose --profile runtime build      # build images first (one-time)
> deploy/deploy.sh                             # provision + boot + verify (project legba)
> ```
> For a throwaway clean-slate validation stack that is fully data-isolated from the
> real `legba` volumes: `deploy/deploy.sh --project legba_val --no-caddy --seed`,
> torn down with `deploy/deploy.sh --project legba_val --teardown` (which `down -v`s
> only the `legba_val_*` volumes). On the real `legba` project, `--teardown` only
> `stop`s — it refuses `down -v` (the only-instance rule).

```
cd /usr/local/deployments/active/legba

# Build the app images (one-time, or after a code change).
# Re-runs are layer-cached and fast.
docker compose --profile runtime build

# Start substrate + dapr + app services in one go.
# (On an ALREADY-PROVISIONED stack. For a fresh substrate use deploy/deploy.sh
#  or §3–§7 below — the runtime must boot last against a seeded registry.)
docker compose --profile runtime up -d
```

This activates 12 services:

  * 4 substrate (redis, postgres, qdrant, nats)
  * 4 dapr (placement, scheduler-init, scheduler, sidecar)
  * 4 app (registry, runtime-dapr, ui-build, caddy)

`docker compose ps` should show all containers healthy within ~60s.

To bring up just the substrate (e.g. for development against
host-mode python):

```
docker compose up -d                              # substrate only
docker compose --profile dapr up -d               # + dapr sidecar
```

### 2.1 Throwaway validation stack + teardown safety

`deploy/deploy.sh --project <NAME>` (any name **other** than `legba`) stands up a
**fully data-isolated** clean-slate stack for validation on the same host. The
`deploy/compose.isolation.yml` override re-declares every named volume and the Dapr
scheduler host-bind with a project-scoped name, so the validation stack physically
**cannot** touch the real `legba_*` volumes. `deploy.sh` **hard-gates** on a
config-render grep before it ever runs `up`: it renders the compose config and aborts
if any reference to a real `legba_*_data` volume (or the real scheduler bind) survives.
A validation stack reuses the same loopback ports, so the model is one stack up at a
time — bring it up with `--no-caddy` to skip the TLS edge:

```
deploy/deploy.sh --project legba_val --no-caddy --seed     # isolated clean-slate stack
deploy/deploy.sh --project legba_val --teardown            # down -v ONLY the legba_val_* volumes
```

**Teardown safety (the only-instance rule).** `--teardown` is destructive **only**
for a non-`legba` project, and even then only after `deploy.sh` re-confirms the
isolation grep. On the **real `legba` project** `--teardown` is **stop-only** — it
`stop`s every container in the project (volumes preserved) and **refuses `down -v`**.
The live `legba` volumes are never destroyed by this tool.

> **Honesty note.** The single baseline schema (`deploy/baseline/0001_baseline.sql`)
> and the data-isolation firewall are round-trip-proven (the baseline against a fresh
> `apache/age` DB — see `deploy/baseline/README.md`; isolation by the config-render
> gate). `deploy.sh` is the canonical, intended bring-up and wraps the same
> required ordering this runbook describes, but a full clean-slate fresh deploy
> end-to-end through registrars → app boot has **not** yet been validated start to
> finish on a fresh empty stack. Treat it as the canonical path, not as a fully
> battle-tested one-shot.

## 3. Apply migrations

Migrations run on the substrate Postgres; they don't depend on the
app images. Pick either path:

```
# (a) A one-off repo-mounted container. At cold-start the registry is NOT yet
#     up (it crashloops without the schema), so migrate FIRST:
docker compose run --rm --no-deps --entrypoint python legba-registry \
  -m legba.data.migrate

# (b) From the host (if a host-side python interpreter is available
#     and the .env is loaded):
python3 -m legba.data.migrate
```

Idempotent. Re-runs skip already-applied migrations
(ledger: `legba_data_migrations`).

> A **fresh deploy** does not replay the 23-file migration history: it applies the
> single round-trip-proven baseline `deploy/baseline/0001_baseline.sql` (which builds
> the schema + AGE graph and pre-seeds the ledger to head **0053**), then `migrate`
> applies any **future** (`0054`+) migrations — currently `0054`…`0085`; live head
> **0085**. Highlights: the contested-claims schema, the `unit_reference_labels`
> gold table, and the composition-tower supersession fold (`0054`…`0060`); the
> DQ-program migrations (`0061`…`0075`); and the 2026-07 audit-remediation sweep
> (`0076`…`0080`). The audit-remediation migrations are **demote/close-only** (they
> tombstone or re-fold junk, never hard-delete):
>
> - **0076** — entity re-fold + junk gate (`entity_profiles` 12,257 → 12,144).
> - **0077** — close semantic / demonym / relative-temporal junk facts (reversible `valid_until`).
> - **0078** — nexus junk + self-edge close and demonym/plural dyad canonicalize (reversible).
> - **0079** — `cross_correlator` stale-head sweep (reversible).
> - **0080** — state-media `source_credibility` seed + a cross-target mislabel close.
>
> The `migrate`-only path above is for an instance whose schema already exists.
> `deploy/deploy.sh` does both steps for you.

Verify:

```
docker exec legba-postgres-1 psql -U legba -d legba \
    -c "SELECT name FROM legba_data_migrations ORDER BY name"
docker exec legba-postgres-1 psql -U legba -d legba \
    -c "SELECT count(*) FROM iso_countries"        # expect 249
docker exec legba-postgres-1 psql -U legba -d legba \
    -c "SELECT to_regclass('public.ui_panel_registrations')"
```

## 4. Verify the registry

```
curl -H "Authorization: Bearer $LEGBA_REGISTRY_API_TOKEN" http://127.0.0.1:8090/api/v1/registry/stack
```

Returns `[]` on a clean substrate. **B-2 fail-closed (2026-06-09):**
`require_bearer` no longer fails open. With `LEGBA_REGISTRY_API_TOKEN`
unset/empty, every guarded request gets **HTTP 503** unless
`LEGBA_DEV_MODE=1` is set explicitly; a configured token is always
enforced (`hmac.compare_digest`). The live deploy sets the token in
`.env` (env_file surfaces it without an image rebuild) — so leave
`LEGBA_DEV_MODE` unset in production.

### 4.0 Environment keys added by the 2026-06 hardening

Set in the gitignored `.env` (placeholders + full notes in `.env.example`):

| Key | Purpose | Default behavior when unset |
|---|---|---|
| `LEGBA_DEV_MODE` | `=1` is the ONLY way to run the registry API without a bearer token | unset → fail-closed 503 (correct for prod) |
| `LEGBA_NATS_TOKEN` | NATS `--auth` token (defense-in-depth on the now-loopback-only substrate) | empty → NATS runs unauthenticated (loopback-only) |
| `LEGBA_REDIS_PASSWORD` | redis `--requirepass` | empty → no password (loopback-only) |
| `LEGBA_GEOCODER_CONTACT_EMAIL` | OSM Nominatim User-Agent contact (required by OSM ToS) | unset/`.invalid` → geocode **refuses** to build → signals land geo-less → **no geo-scoped findings**. Set a reachable address. |
| `LEGBA_MEDIA_API_URL` | hosted media-extraction endpoint | unset → `process_media` refuses loud (declared seam) |
| `LEGBA_A2A_ENABLED` / `LEGBA_A2A_TRUSTED_KEYS` | mount + key-gate the runtime `/a2a/skills` surface | unset → a2a UNMOUNTED |
| `LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS` | ActorProxy invoke round-trip budget (the trigger-engine → actor `run` call). Raised from 60→180 so a busy target's `cross_source_dedup` sweep doesn't time out; the actor's own cooldown + trigger-window CAS dedup a late completion. | unset / malformed / ≤0 → falls back to **180s** (`source_first_runtime.actor_invoke_timeout_seconds`) |

### 4.1 Endpoint surface (as of 2026-05-29)

Beyond the v1 registry CRUD (descriptors / stack / vault / DLQ / audit
/ vocabulary / conversions), the runtime exposes these substrate-read
endpoints for the UI + operator tooling:

| Path | Purpose |
|---|---|
| `GET /api/v1/v3/runtime/actors` | `actor_state` roster (lifecycle, last_run_at, source_cursors) |
| `GET /api/v1/v3/system/analyst-cadence` | per-analyst cadence health from `analyst_traces` (last run, age, runs 1h/24h, last outcome, status) — powers the System Status panel; reads `analyst_traces`, NOT the NULL `actor_state.last_run_at` (added 2026-06) |
| `GET /api/v1/v3/system/source-firing` | per-source firing matrix (signals 24h/7d, last-seen age, last poll outcome, recent error count, firing/silent/error/paused status) — powers the System Status panel (added 2026-06) |
| `GET /api/v1/v3/streams/consumer_lag` | per-consumer NATS lag (`num_pending`), orphaned/deleted durables filtered out — powers the System Status panel's Queues layer |
| `GET /api/v1/v3/optimizer/candidates?state=` | prompt-module candidate queue |
| `POST /api/v1/v3/optimizer/candidates/{id}/review` | promote / reject (descriptor lifecycle drives the flip) |
| `GET /api/v1/findings?since=&target_id=&analyst_id=&severity=&limit=&cursor=` | cross-target finding feed (cursor-paginated) |
| `GET /api/v1/situations?state=&target_id=` | situation roster |
| `GET /api/v1/signals?target_id=&since=&source_id=` | raw signals |
| `GET /api/v1/lineage/{row_kind}/{row_id}?direction=&depth=` | derived_from walk (≤10 hops; ~11 ms median at depth=3) |
| `GET /api/v1/targets/{id}/runtime` | actor_state + source_cursors per target |
| `GET /api/v1/analysts/runtime` | analyst roster + 7d aggregates |
| `GET /api/v1/analysts/{id}/runs|outputs|critiques` | per-analyst views |
| `GET /api/v1/budget/ledger|envelope|demotions` | budget surfaces |
| `GET /api/v1/source_credibility[/{host}]` | host-credibility CRUD |
| `PUT /api/v1/source_credibility/{host}` | upsert single host |
| `POST /api/v1/source_credibility/bulk` (CSV) | bulk import |
| `GET/WS /api/v1/registry/events?filter=<NATS subject>` | live multiplexer (`descriptor.>`, `stack.>`, `legba.dlq.>`, `analyst.<id>.>`, etc.) |

All gated by `Authorization: Bearer <LEGBA_REGISTRY_API_TOKEN>` —
fail-closed (503) when the token is unset, unless `LEGBA_DEV_MODE=1` (§4).

### 4.2 Runtime bootstrap startup log signposts

After `docker compose --profile runtime up -d`, expect these log
lines from `legba-runtime-dapr` in order (use `docker logs
legba-legba-runtime-dapr-1 | grep dapr_host` to filter):

```
dapr_host.actor_types.registered types=['TargetActor', 'AnalystActor']
dapr_host.nlp_client.built component=nlp.local.legba_models
dapr_host.qdrant_client.built
dapr_host.embedding_service.built
dapr_host.optimizer_workflow.ready
dapr_host.substrate_query_port.built
dapr_host.audit_checkpointer.started
dapr_actors.target.deps_resolver.registered
dapr_actors.analyst.deps_resolver.registered
dapr_host.deps_resolvers.registered
dapr_host.reconcile_loop.started
nats_informer.start stream=LEGBA_DESCRIPTOR_EVENTS subject_filter=descriptor.>
dapr_host.informer.started
dapr_host.initial_resync.enqueued count=<N>
action_executor.invoke kind=CREATE_ACTOR actor_id=<...>
dapr_actors.target.deps.fallback.cached
dapr_actors.target.reminder.registered reminder=<source_id> period=...
```

Each factory `built` line confirms the corresponding stack component
resolved + the client constructed:

| Factory | Backing | Failure mode + impact |
|---|---|---|
| `nlp_client` | `nlp.local.legba_models` stack component | `nlp_client.unavailable` — ner_multilingual + classify filters cannot activate |
| `qdrant_client` | `vector.qdrant.cluster_main` | `qdrant_client.unavailable` — dedupe_tier_3 + semantic correlators uninstalliable |
| `embedding_service` | `embed.primary.openai_compat` | `embedding_service.unavailable` — same set |
| optimizer workflow client | Dapr Workflow on the daprd sidecar (`dapr.ext.workflow`) | `optimizer_workflow.in_process` / `.unavailable` — optimizer falls back to its in-process GEPA loop |
| `substrate_query_port` | `pg_pool` + `qdrant_client` | `substrate_query_port.unavailable` — consult_on_demand uninstalliable |
| `audit_checkpointer` | `pg_pool` + `load_default_identity()` | hard-fail (blocks bootstrap; required for receipt-chain integrity) |

All five service factories degrade gracefully — descriptors that do
NOT use the affected kinds continue to activate. Operator action is
required only when the affected kinds need to fire.

### 4.3 A2A skill router

The A2A skill output kind mounts on the production runtime at
`/a2a/skills/*` (a `legba-runtime-dapr` route, NOT a `legba-registry`
route) **only when operator-enabled** — `LEGBA_A2A_ENABLED=1` plus a
non-empty `LEGBA_A2A_TRUSTED_KEYS` allowlist (or `LEGBA_DEV_MODE=1`).
This is the **default-OFF, fail-closed** B-2 posture: without the flag
the surface is NOT mounted, and the runtime answers `/a2a/skills` with a
**503** (`error: a2a_skill_surface_disabled`) carrying the enable recipe —
a fail-loud response, never a silent 404 (SEAMS #15; xfail-tracked by
`tests/runtime/test_a2a_skill_router_e2e.py`). When enabled, three paths:

```
GET  /a2a/skills              — list registered skills (signed envelopes)
GET  /a2a/skills/{skill_id}   — fetch one skill's metadata + recent outputs
POST /a2a/skills/{skill_id}   — invoke (signed envelope required)
```

Enable recipe: `LEGBA_A2A_ENABLED=1` +
`LEGBA_A2A_TRUSTED_KEYS=did:legba:caller=<verify-key-hex>,…`, then restart
the runtime.

Inbound envelope shape (must be Ed25519-signed by a key the registry's
`TrustedKeyDirectory` recognizes — `None` trust list in dev = accept
all signed envelopes):

```json
{
  "envelope": {
    "envelope_version": "1",
    "skill_id": "intelligence.india_energy_assessment",
    "nonce": "<uuid4-hex>",
    "issued_at": "2026-05-29T01:00:00Z",
    "sender_did": "did:key:zMyApp",
    "recipient_did": "did:legba:registry:<host>",
    "signer_did": "did:key:zMyApp",
    "payload": { ... }
  },
  "signature": "<base64url-Ed25519(canonical_json(envelope))>"
}
```

Descriptors declare an `outputs.a2a_skill` block to expose a skill;
the reconcile loop's action executor calls
`A2ASkillRegistry.register_from_descriptor` on activation +
`unregister_by_analyst` on retire. Outbound (Legba calling Mnemosyne):
see `src/legba/clients/mnemosyne_a2a.py` + the Mnemosyne wire-shape
note in §11.

### 4.4 Analyst output tables (post-2026-05-29)

- `analyst_critiques` — trace-finalizer rows when a critic kind emits
  a CRITIQUE output. Populated by `dapr_actors._write_critique_trace_record`.
  Keyed on `trace_id = run_id`; carries judge_analyst_id, rubric_uri,
  per-axis scores JSONB, overall_score, revision_delta.
- `alert_sink_deliveries` (migration `0023`) — per-attempt delivery
  audit for the alert output kind. Columns: alert_row_id (FK to
  analyst_outputs), descriptor_id, sink_kind (nats / pushover / xmpp
  / matrix), attempt_number, status (delivered / failed / retrying),
  error_message, attempted_at, delivered_at, payload_summary JSONB.
- (The pre-pivot `predictions` table + `GET /api/v1/predictions` route were
  dropped in the source-first pivot. Predictor/forecast output now lands in
  `analyst_outputs` like every other analyst kind; there is no separate
  predictions surface.)

### 4.5 Bootstrap scripts

`bringup_register_stack.py` registers the substrate stack components;
sibling scripts register descriptors for the validation analysts:

- `scripts/bringup_register_stack.py` — substrate (LLM providers,
  vector store, embedding, NLP service, NATS, Postgres+AGE, proxy).
- `scripts/bringup_register_multi_country_targets.py` — 5 country
  news targets (japan / germany / nigeria / mexico / turkey) for
  multi-target validation against the india_energy_infra baseline.
- `scripts/trigger_multi_country_runs.py` — fast-trigger first
  RSS pull on each newly-registered target via the Dapr sidecar HTTP
  actor-method API (no SDK dep required in operator shell).
- `scripts/seed_predictor_signals.py` — synthetic India signals for
  predictor validation when the live RSS volume is too low.

Each script is idempotent (uses the registry's POST/PUT lifecycle so
re-runs reconcile through audit log + content-hash without breaking
state). Host-side execution honors `LEGBA_REGISTRY_TOKEN`
(or `LEGBA_REGISTRY_API_TOKEN` — both names accepted) — set the bearer
from `.env` before running.

### 4.6 Deps-resolver kinds catalog

The runtime's two reconcile-loop deps resolvers — `_TARGET_DEPS_RESOLVER`
in `dapr_actors.py:374` and `_ANALYST_DEPS_RESOLVER` in
`dapr_actors.py:397` — bind one actor-kind family each. On
fallback-lookup miss (post-restart, the in-process `_TARGET_DEPS` /
`_ANALYST_DEPS` cache is empty) the resolver fetches the typed
descriptor from the registry via httpx and reconstructs:

**Target deps (`_TargetDeps`)**

  * `actor_id` — `kind::descriptor_id::version[:16]`
  * `descriptor` — TypedTargetDescriptor from `/typed`
  * `target_ctx` — `TargetContext(target_id, target_version,
    descriptor_source_id, schema_uri)`
  * `pg_pool` — shared asyncpg pool (bootstrap-time)
  * `nats_store` — shared NATS handle (bootstrap-time)
  * `redis_store` — shared Redis handle
  * `qdrant_client` — bootstrap-resolved; None when component not registered
  * `embedding_service` — bootstrap-resolved; None when unavailable
  * `nlp_client` — bootstrap-resolved; None when unavailable
  * `source_handlers` — `{source_id → SourceHandler}` per
    `descriptor.sources[]`
  * `pipeline` — `PipelineRunner` with the 7-filter chain (langdetect →
    geocode → NER → classify → dedupe → source-credibility → fact-extract).
    Two enrichment stages are **opt-in, off by default** and add no LLM hop
    unless an operator turns them on: the `slm_relationship_validate` stage
    inside `fact_extractor` (descriptor flag `slm_validate_relations`, routes
    extracted triples through the relationship-validation SLM before they
    become facts; degrade-not-drop on SLM failure), and the standalone
    `cross_source_coalesce` analyst (§7.1).
  * `audit_checkpointer` — bootstrap-shared
  * `vault_resolver` — credential resolver closure

**Analyst deps (`_AnalystDeps`)**

  * `actor_id`, `descriptor`, `analyst_ctx`
  * `pg_pool`, `nats_store`, `redis_store`
  * `kind_deps` — kind-specific bundle (see per-kind table below)
  * `output_kind` — `OutputKind` from kind discovery
  * `read_slice` — kind-specific `READ_SLICE` reader (or None for the
    default substrate slice)
  * `budget_enforcer` — `BudgetEnforcer(analyst_id, version,
    budget_tokens_per_day, provider, model)`
  * `audit_checkpointer`, `vault_resolver`

**Per-kind extras threaded via `kind_deps`** (set by
`register_analyst_kind_resolver_extras` in `analyst_deps_builder.py`):

| Kind | Extras |
|---|---|
| `inline_target` / `cross_target_raw` / `meta_findings_synthesizer` | `llm_handler` only |
| `predictor` | `llm_handler` (optional, None for stat-only); `horizon_days`, `ci_level` options threaded from `method.llm` |
| `critic` | `llm_handler`, `rubric_uri`, `analyzed_analyst_id`, `analyzed_analyst_version`, `analyzed_output_id`, `allow_self_correlated` — resolved by `_resolve_critic_context` from the analyzed analyst's descriptor at run time |
| `optimizer` | `llm_handler`, `temporal_client` (the durable-workflow client slot — Dapr-Workflow-backed, stably named), `parent_prompt_module_path`, `min_traces_required`, `min_critiques_required` |
| `consult_on_demand` | `llm_handler`, `substrate_query_port`, `max_rounds`, `tools_whitelist[]` |
| `deterministic` | sub-handler bundle from `deterministic_handlers/*.py` |
| `cross_analyst_correlator` | `llm_handler`, contradiction/agreement-detection options |

If a kind needs an extra not in this table, add it to
`analyst_deps_builder.build_analyst_run_method` AND to the kind's
factory closure in `dapr_host.bring_up_production_runtime` — the
resolver registration is symmetric across both modules.

### 4.7 Registry `/metrics` + Prometheus alerting

The registry exposes a Prometheus text-exposition endpoint at **`GET /metrics`**
(`metrics_api.py`, mounted app-level — no `/api/v1` prefix — in `server.py`).
It is **unauthenticated by design** — like `/healthz`, NOT bearer-gated — so a
scraper can poll it without a token; values are real registry counters/gauges.

Scrape config (add to your Prometheus `scrape_configs`):

```yaml
scrape_configs:
  - job_name: legba-registry
    metrics_path: /metrics
    static_configs:
      - targets: ['legba-registry:8090']
```

Wire the shipped alert rules via `rule_files`:

```yaml
rule_files:
  - deploy/prometheus/legba_alerts.yml
```

`deploy/prometheus/legba_alerts.yml` carries **8 alerts**: scrape-failure,
endpoint-down, ingest-cursor-frozen, DLQ-nonempty, DLQ-growing,
budget-near-cap, and budget-exhausted. (`/metrics` is internal-network /
loopback-only on the live deploy — it is not published off the host — so the
absence of bearer auth does not expose it publicly. Do NOT route it through
caddy basic_auth: scrapers can't carry creds and the rules would silently stop
firing.)

## 5. Verify the UI

Caddy serves the SPA + proxies the registry API on **:80 + :443** — the only
host-published ports. The substrate, the registry (`:8090`), and the runtime
(`:6090`) are all 127.0.0.1-internal / behind Caddy (B-1). Browse
`https://$LEGBA_PUBLIC_DOMAIN/` (Caddy basic_auth, user `legba`). Verify from the
host through the edge (SNI must match the configured domain):

```
curl -k --resolve "$LEGBA_PUBLIC_DOMAIN:443:127.0.0.1" \
     -u "legba:<password>" "https://$LEGBA_PUBLIC_DOMAIN/api/v1/registry/stack"
```

If the UI doesn't update after a code change, rerun the one-shot
build job (which republishes the volume contents):

```
docker compose --profile ui build legba-ui-build
docker compose --profile ui up -d --force-recreate legba-ui-build  # one-shot
```

Caddy continues serving from the same volume — no caddy restart
needed.

## 6. Load credentials into the vault

The bringup scripts are NOT baked into the images (the Dockerfile copies only
`pyproject.toml` + `src`), so run them from a **repo-mounted one-off container**
at the canonical path (so `.env` resolves). Canonical form, reused below:

```
docker compose run --rm --no-deps \
  -v "$PWD:/usr/local/deployments/active/legba" -w /usr/local/deployments/active/legba \
  -e LEGBA_REGISTRY_URL=http://legba-registry:8090/api/v1/registry \
  --entrypoint python legba-registry scripts/bringup_vault_load.py
```

Idempotent: secrets that already exist are skipped. The script never
echoes plaintext. Verify:

```
curl -H "Authorization: Bearer dev" \
     http://127.0.0.1:8090/api/v1/registry/vault/secrets/source.fred.api_key/exists
```

Manual single-secret form:

```
curl -X POST -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
     -d '{"secret_id": "my.new.secret", "plaintext": "value-here"}' \
     http://127.0.0.1:8090/api/v1/registry/vault/secrets
```

The plaintext is encrypted with `LEGBA_DATA_MASTER_KEY` (XSalsa20-
Poly1305) and stored in `stack_credentials`. The master key MUST be
stable across restarts — see `backups/master_key_2026-05-20.txt`.

## 7. Register stack components + the working set

> **New operator standing up a fresh instance from empty volumes?** Follow
> the ordered from-zero bootstrap guide [docs/SETUP.md](SETUP.md) instead — it
> walks the full sequence (build → up → migrate → vault → stack → working set →
> **full 46-source catalog** → ongoing analysts + budget → verify) with the exact
> commands. This section is the ops-reference form of the same steps.

Same repo-mounted form as §6 (`--entrypoint python legba-registry scripts/<x>.py`).
DB-direct registrars (`bringup_register_p17_workingset.py`, the ongoing-analyst
registrars, `bringup_set_budget_envelope.py`) also need `-e LEGBA_DATA_PG_DB=legba`
(they default to `legba_pivot_test`).

```
# Stack components (LLM / embedding / vector store / NATS / ...):
docker compose run --rm --no-deps -v "$PWD:$PWD" -w "$PWD" \
  -e LEGBA_REGISTRY_URL=http://legba-registry:8090/api/v1/registry \
  --entrypoint python legba-registry scripts/bringup_register_stack.py

# The fresh source-first working set — action packs (incl. substrate_read +
# escalate), the 3 shared sources, 19 G20 targets, and the 4 LEGACY analysts
# (country_assessor/critic/optimizer/consult_default). NOTE: p17_workingset is a
# FROZEN legacy path that registers the RETIRED monolith set — it does NOT register
# the live 7-unit spine + composition tower; use deploy.sh (bringup_register_analysts.py)
# for the current spine. Kept here for reference on the one-pass dependency ordering:
docker compose run --rm --no-deps -v "$PWD:$PWD" -w "$PWD" \
  -e LEGBA_DATA_PG_DB=legba \
  -e LEGBA_REGISTRY_URL=http://legba-registry:8090/api/v1/registry \
  --entrypoint python legba-registry scripts/bringup_register_p17_workingset.py
# then the ongoing analysts + budget:
#   scripts/bringup_register_finding_supersession.py
#   scripts/bringup_register_cross_source_dedup.py
#   scripts/bringup_register_entity_resolution.py
#   scripts/bringup_set_budget_envelope.py
```

**Full source-first working set (cold-start re-seed).** To populate a fresh DB
with the canonical demo set — 3 shared world-news sources, 19 G20 country
targets (geo-predicate `source_selector` + per-country subscription + inline
analyst), the action packs, and the analysts — use `deploy.sh` (which registers
the LIVE spine: the seven bounded units + the composition tower + the I&W pair via
the split registrars). The frozen `p17_workingset` one-pass path below registers only
the RETIRED `country_assessor` monolith set and is kept for dependency-ordering
reference; run the deterministic analysts + the daily
budget envelope. Pin `LEGBA_DATA_PG_DB=legba` (the working-set registrar
defaults to the `legba_pivot_test` DB). Run AFTER §6 vault + the stack above, and (per §0)
with the runtime already up or about to be `--force-recreate`d so it builds its
clients against the now-seeded stack:

```
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_p17_workingset.py        # LEGACY: packs + sources + 19 G20 targets + 4 RETIRED-monolith analysts (prefer deploy.sh for the live 7-unit spine)
for s in finding_supersession cross_source_dedup entity_resolution; do
  docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
    python scripts/bringup_register_$s.py                  # deterministic cadence analysts
done
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_set_budget_envelope.py            # global daily token envelope
```

The runtime's reconcile loop picks up the new descriptors and activates the
SourceActors + AnalystActors within a resync interval (or immediately on a
control-plane `--force-recreate`). Verify per §9 (signals flowing) + that
`analyst_outputs` accrues findings with `derived_from` provenance.

**⚠️ Register the FULL 46-source catalog (the often-missed step → current scope).**
The working-set bringup above registers only the **3 shared world-news sources**
(BBC / Deutsche Welle / Al Jazeera) — a deliberately small **cold-start
verification set**, NOT the deployed scope. The full source catalog lives in
`scripts/bringup_register_source_catalog.py` and defines **exactly 46 sources**
(~43 RSS + 3 GeoJSON hazard feeds: USGS quakes / NWS alerts / NASA EONET), each
with its enrichment chain (dedupe → language_detect → ner_multilingual →
[fact_extractor on 4 feeds] → geocode). It is **NOT auto-run on deploy and NOT
part of the working-set bringup** — it is a SEPARATE manual step that a fresh
operator currently misses, which is why an under-bootstrapped instance sits at
"only 3 RSS feeds." Run it to take the instance from the 3-feed cold-start to
current/full scope. Idempotent; pin `LEGBA_DATA_PG_DB=legba` (defaults to
`legba_pivot_test`); it also seeds host-level `source_credibility` rows
(ON CONFLICT DO NOTHING):

```
# Optional: live HTTP probe + parse check first — prints a verdict table,
# registers nothing.
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_source_catalog.py --verify

# Register all 46 catalog sources:
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_source_catalog.py
```

(Repo-mounted compose-run form, equivalent — use it if the registry container
is not already up:)

```
docker compose run --rm --no-deps -v "$PWD:$PWD" -w "$PWD" \
  -e LEGBA_DATA_PG_DB=legba \
  -e LEGBA_REGISTRY_URL=http://legba-registry:8090/api/v1/registry \
  --entrypoint python legba-registry scripts/bringup_register_source_catalog.py
```

After it runs, the reconcile loop activates the new SourceActors within a resync
interval. Verify you reached full scope by counting distinct producing sources —
expect dozens, not 3 (a current-scope instance reaches ~49 distinct
signal-producing `source_id` values once the catalog + seed/baseline adapters
are active):

```
docker exec legba-postgres-1 psql -U legba -d legba \
    -c "SELECT count(DISTINCT source_id) FROM signals"   # expect dozens (~49 at full scope), not 3
```

**`cross_source_dedup` per-run work cap.** The dedup sweep is bounded +
incremental: its candidate query skips content_hash groups already fully
canonicalised (filtered in the DB) and is capped at the run option
`max_groups_per_run` (default **500**, `DEFAULT_MAX_GROUPS_PER_RUN`) with a
stable `ORDER BY content_hash`, so each cadence does bounded work and a backlog
drains across successive runs — keeping the sweep inside the actor-invoke budget
(§4.0 `LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS`). Lower it for a tighter per-run
budget or raise it to drain a large backlog faster; set it in the analyst
descriptor's `method` run options.

Manual ad-hoc registration:

```
curl -X POST -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
     -d @descriptors/my_target.yaml \
     http://127.0.0.1:8090/api/v1/registry/descriptors/target
```

### 7.1 Opt-in cross-source-coalesce analyst

`scripts/bringup_register_cross_source_coalesce.py` registers the
`cross_source_coalesce` descriptor
(`descriptors/analyst_cross_source_coalesce.yaml`). It is **off by default**:
the `enabled` run option defaults False, so the registered analyst does
nothing until it is fired with run options `{"enabled": true}`. It also
requires a live **embedding service + Qdrant client** on the rig; without them
it **refuses loud** (declared SEAM #19) rather than fabricating cross-source
links.

The bringup scripts are NOT baked into the runtime image (the Dockerfile copies
only `pyproject.toml` + `src`), so run this one from the **host** against the
loopback registry — the run-from-host pattern (test image, host network,
loopback registry URL):

```bash
docker run --rm --network host \
  -v "$PWD:$PWD" -w "$PWD" \
  -e LEGBA_REGISTRY_URL=http://127.0.0.1:8090/api/v1/registry \
  --entrypoint python legba/legba-runtime-dapr:test \
  scripts/bringup_register_cross_source_coalesce.py
```

(`LEGBA_REGISTRY_URL` defaults to `http://127.0.0.1:8090/api/v1/registry` if
unset.) Then fire it with `{"enabled": true}` to actually coalesce.

### 7.2 Knowledge-roots seeding

`scripts/seed.py` runs a registered seed adapter (fetch → map → write) to
populate the knowledge roots — facts, nexuses, and entities — stamped via the
`seed_batches` ledger (migration `0034`). Writes are **row-level idempotent**.

```bash
python3 scripts/seed.py --list                      # list registered adapters
python3 scripts/seed.py --source world_baseline --dry-run
python3 scripts/seed.py --source world_baseline     # commit
```

> **The curated seed DATA is operator-provided, not bundled.** Legba ships the
> seed *machinery* (the adapters, the driver, the import CLI) but no curated
> data. `seeds/world_baseline.yaml` / `seeds/sipri_arms_transfers.yaml` are not
> in the repo — provide your own in the documented format (see **`seeds/README.md`**
> + `seeds/world_baseline.example.yaml`). If a curated file is absent, its adapter
> logs a warning and no-ops (0 rows), so `--seed` degrades cleanly rather than
> failing. The network adapters (`wikidata_leaders`, `acled_conflict`) need no
> local file.

Live adapters (registered in `legba.data.seed.ADAPTERS`):

| Adapter | Source |
|---|---|
| `world_baseline` | curated YAML — operator-provided (`seeds/world_baseline.yaml`; see `seeds/README.md`) |
| `wikidata_leaders` | live Wikidata SPARQL |
| `acled_conflict` | ACLED conflict-events feed |
| `sipri_arms_transfers` | curated SIPRI YAML — operator-provided (`seeds/sipri_arms_transfers.yaml`) |

#### Seed CURRENT world leaders (knowledge-grounding root)

`wikidata_leaders` is the leader-data adapter behind the analyst
knowledge-grounding feature (§7.3): it pulls CURRENT heads of state/government
from the live Wikidata Query Service (SPARQL) and emits, per country, a
**country-subject office fact** (`<country> | head of state | <leader>`, keyed
on the country) plus signed `MemberOf` bloc-membership nexuses. Idempotent + a
single SSRF-guarded GET against the public WDQS endpoint (live egress —
network-required, unlike the offline curated adapters):

```bash
python3 scripts/seed.py --source wikidata_leaders --dry-run   # parse only, no write
python3 scripts/seed.py --source wikidata_leaders             # commit (live SPARQL)
```

**Supersession (leader change).** The office fact is keyed on the COUNTRY, so a
re-pull that finds a NEW officeholder for a country CLOSES the prior one
(`valid_until = now` + `superseded_by`) and opens the new fact — this is how the
grounding store stays current (live-verified: US head of state resolved to
`Donald Trump` since 2025-01-20, superseding the prior officeholder). The seed
write-path threads `valid_until` through `FactPayload`/`NexusPayload`
(`seed/_driver.py`), so a curated YAML that carries an explicit `valid_until`
(e.g. a term end) is honoured too — no longer dropped.

**Bare-QID note.** Some Wikidata leaders have no English SPARQL label and arrive
as a bare `Qxxxx` id (e.g. Trump's `Q22686`); the adapter resolves these via a
`wbgetentities` label lookup with an enwiki-sitelink fallback. If a value stays
a bare QID, the grounding resolver SKIPS it (it never injects an unreadable
`Q…` line) — so a leader that fails label resolution simply isn't grounded,
rather than poisoning the preamble.

**Known gaps (be aware before relying on seeded data):**

- **SIPRI is registered but NOT yet seeded into the live DB** — the
  `sipri_arms_transfers` adapter is wired and listed by `--list`, but it has
  not been run against the live database (0 rows). "Deployed" here means
  code-wired, not data-present.

### 7.3 Analyst knowledge-grounding (stale-cutoff fix)

The analyst LLM's training cutoff predates the present, so a `world_assessor` /
`country_assessor` run can backfill stale world state (it once called the
CURRENT US president a "former" one). **Grounding** fixes this by curating
current data IN (§7.2 `wikidata_leaders` / `world_baseline`) and INJECTING it at
analysis time: before the LLM call, the deps-builder reads CURRENT authoritative
substrate facts/nexuses (temporal-honesty gate `superseded_by IS NULL AND
(valid_until IS NULL OR valid_until > now())`, curated/seed-preferred) for the
target geo + the slice's top entities and PREPENDS a dated "AUTHORITATIVE
CURRENT CONTEXT (as of <today> — treat as ground truth over prior knowledge)"
block to the prompt. Degrade-not-drop (a read miss → no preamble, never a
fabricated header) and token-capped via `max_facts`.

**Enable grounding on an analyst** — add a `grounding` block to its descriptor
(off by default; a descriptor without the block is unchanged):

```yaml
grounding:
  enabled: true
  scope: [target_geo, slice_entities]   # both is the default
  sources: [substrate, situations, graph_structure, vector:world_context]
  # vector:world_context is a relevance-floored, country-filtered, degrade-not-drop
  # RAG source, currently a GUARDED, MEASURED PILOT on internal_stability ONLY
  # (leadership_transition RAG is OFF as of the 2026-07-03 rollback). See the
  # "world_context RAG guarded pilot" task in §11 for the kill-switch + auto-rollback
  # controls; omit it to ground on the structured substrate only.
  max_facts: 30                         # token cap, 1..200
```

It is already opted IN on `descriptors/analyst_world_assessor.yaml` +
`descriptors/analyst_country_assessor.yaml`. The hook only constructs when
`grounding.enabled: true` AND the runtime holds a substrate `pg_pool`; with only
`vector:world_context` declared (no `substrate`) the hook logs
`analyst_deps_builder.grounding.no_substrate_source` and injects nothing (Tier-2
is a declared SEAM #20). Verify a run grounded by checking its context contains
the dated header, e.g. an injected line like
`United States — head of state: Donald Trump (since 2025-01-20)`.

## 8. Per-image troubleshooting

### Logs

```
docker logs legba-legba-registry-1 -f
docker logs legba-legba-runtime-dapr-1 -f
docker logs legba-legba-caddy-1 -f
docker logs legba-dapr-sidecar-1 -f
```

### Exec into a container

```
docker exec -it legba-legba-registry-1 bash      # python:3.11-slim base has bash
docker exec -it legba-legba-runtime-dapr-1 bash
```

### Restart one service

```
docker compose --profile runtime restart legba-registry
docker compose --profile runtime restart legba-runtime-dapr
```

### Force-rebuild after dep changes

```
docker compose --profile runtime build --no-cache legba-runtime-dapr
docker compose --profile runtime up -d legba-runtime-dapr
```

### Image-size investigation

```
docker images | grep '^legba/'
docker run --rm --entrypoint sh legba/legba-runtime-dapr:latest \
    -c "du -sh /install/lib/python3.11/site-packages/* | sort -h | tail -20"
```

## 9. Verify ingestion is working

Once the runtime is up and the resync loop has fired (5-minute default,
overridable via `LEGBA_RUNTIME_RESYNC_INTERVAL`), signals should land:

```
docker exec legba-postgres-1 psql -U legba -d legba <<'SQL'
SELECT count(*) AS signal_count                    FROM signals;
SELECT count(*) AS enriched_count                  FROM signals WHERE language IS NOT NULL;
SELECT count(*) AS analyst_output_count            FROM analyst_outputs;
SELECT count(*) AS budget_rows                     FROM budget_ledger;
SELECT count(*) AS panel_rows                      FROM ui_panel_registrations WHERE NOT retired;
SQL
```

Live tail of the runtime log:

```
docker logs -f legba-legba-runtime-dapr-1
```

## 10. Bring it all down

```
# Graceful: stop app services first, then substrate.
docker compose --profile runtime down

# Lose all data (descriptors, signals, audit, vault, vector indices):
docker compose --profile runtime down --volumes
```

## 11. Common operator tasks

### Update a descriptor

```
curl -X PUT -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
     -d @descriptors/my_target_v2.yaml \
     http://127.0.0.1:8090/api/v1/registry/descriptors/target/india_energy_infra
```

The registry stamps a fresh content-hash, preserves history, and emits
`descriptor.updated.target.india_energy_infra` on NATS. A registry hook
retires prior-version `ui_panel_registrations` rows and lands new ones
atomically. Caddy's UI mount auto-picks up panel changes on the next
WebSocket re-resolution.

### Retire a descriptor

```
curl -X POST -H "Authorization: Bearer dev" \
     http://127.0.0.1:8090/api/v1/registry/descriptors/target/india_energy_infra/retire \
     -d '{"reason": "decommissioning"}' -H "Content-Type: application/json"
```

### Activate a country target materialised from discovery

The country-list discovery descriptor materialises ~246 country targets
at `state=configured`. Activate per-country:

```
curl -X PUT -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
     http://127.0.0.1:8090/api/v1/registry/descriptors/target/country_geopolitical_br \
     -d '{...body with identity.state=active...}'
```

### Inspect the dead-letter queue

```
curl -H "Authorization: Bearer dev" \
     http://127.0.0.1:8090/api/v1/registry/dead_letter
```

Live tail via the WebSocket multiplexer (descriptor + output DLQ rows
both publish):

```
wscat -c "ws://127.0.0.1:8090/api/v1/registry/events?filter=legba.dlq.%3E" \
      -H "Authorization: Bearer dev"
```

### Deploy a code change to `legba-models`

The `legba-models` service runs on a separate GPU host (vLLM + spacy +
NLLB + GLiREL). Source lives at `<deploy-dir>/legba-models/` on that GPU
host (NOT alongside the rest of the stack on the legba host). Deploy
procedure (`$MODELS_HOST` = the GPU host, e.g. `user@gpu-host`):

```bash
# 1. Build locally on the legba host first to verify the patch compiles.
cd /usr/local/deployments/active/legba-models
docker compose build legba-models

# 2. Rsync just the changed file(s) to the GPU host.
rsync -av app/main.py \
    "$MODELS_HOST":<deploy-dir>/legba-models/app/main.py

# 3. Rebuild on the GPU host + force-recreate the container.
ssh "$MODELS_HOST" \
    'cd <deploy-dir>/legba-models && \
     docker compose build legba-models && \
     docker compose up -d --force-recreate legba-models'

# 4. Verify (the legba-models port is NOT exposed on the GPU host
#    — only on the docker network). Hit it through the legba runtime's
#    nlp client instead, or curl via the public caddy proxy:
curl -u <user>:<pass> -X POST \
    https://nlp.example.internal/extract \
    -H "Content-Type: application/json" \
    -d '{"text":"Russia imposed sanctions on Ukraine."}'
```

Container restart takes ~10-15 seconds to warm GLiREL + spaCy. The
service prints `[legba] All models cached.` followed by
`INFO: Uvicorn running on http://0.0.0.0:8700` when it's serving.

> **`legba-models` perimeter (defense-in-depth).** The five inference
> endpoints (`/translate`, `/classify`, `/extract`, `/summarize`, `/ner`)
> carry **no in-app auth by default** — they are deployment-mitigated by
> (1) the port NOT being published off the GPU host's docker network, and
> (2) caddy basic_auth in front. As a SECOND in-app layer, set
> `LEGBA_MODELS_API_SECRET` in the `legba-models` container env: when set,
> every inference endpoint requires a matching `X-Models-Secret` header and
> returns `401` otherwise (`/health` stays open for liveness probes). When
> UNSET the check is a no-op (dev default — unchanged behaviour). The legba
> runtime presents the header automatically when `MODELS_API_SECRET` (or
> `LEGBA_MODELS_API_SECRET`) is set in its own env (threaded by
> `NlpServiceClient.from_env`). **Release checklist:** confirm the
> `legba-models` port is 127.0.0.1/network-internal-only on the GPU host
> (`docker compose port legba-models 8700` must not show a public bind);
> optionally set `LEGBA_MODELS_API_SECRET` on both sides.

### Rotate MODELS_API credentials

The hosted NLP service (`nlp.example.internal` serving
NLLB/DeBERTa/GLiREL/T5) is fronted by caddy basic-auth on the GPU
host. Legba's `nlp.local.legba_models` stack component references
the username + password as vault secrets:

```
nlp.local.legba_models.api_user
nlp.local.legba_models.api_pass
```

To rotate:

1. **On the GPU host** — generate the new bcrypt hash + update the caddy config:
   ```
   ssh "$MODELS_HOST"
   NEW_PW=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
   HASH=$(docker exec $(docker ps --filter name=caddy --format '{{.Names}}' | head -1) \
            caddy hash-password --plaintext "$NEW_PW")
   # Edit /usr/local/deployments/caddy/etc/conf.d/nlp.example.internal.caddy
   # — replace the line for user 'legba' with: legba <HASH>
   # — then reload caddy:
   docker exec <caddy-container> caddy reload --config /etc/caddy/Caddyfile
   ```
2. **On the legba host** — push the new password into the vault:
   ```
   curl -X POST -H "Authorization: Bearer ${LEGBA_REGISTRY_API_TOKEN:-dev}" \
        -H "Content-Type: application/json" \
        http://127.0.0.1:8090/api/v1/registry/vault/secrets \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"secret_id":"nlp.local.legba_models.api_pass","plaintext":sys.argv[1],"notes":"rotated "+sys.argv[2]}))' "$NEW_PW" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
   ```
3. **Restart legba-runtime-dapr** to pick up the new vault entry:
   ```
   docker compose --profile runtime up -d --force-recreate legba-runtime-dapr
   ```
4. **Verify** — `docker logs legba-legba-runtime-dapr-1 | grep nlp_client.built`
   should show the success line; no `nlp_client.unavailable` warnings.

### Clean up stale daprd reminders

**First line (automatic): the orphan-reminder GC sweep.** The reconcile
loop now runs `legba.runtime.reminder_gc.sweep_orphan_reminders` once per
periodic resync (wired in `bring_up_production_runtime`). It enumerates
`actor_state` rows with `lifecycle='retired'` and issues an idempotent
`DELETE /v1.0/actors/{type}/{id}/reminders/{name}` against the daprd
sidecar for each reminder a now-retired actor would have owned
(`poll_<source>` / `run_cadence` / legacy `run_source_*`). It acts ONLY
on RETIRED actors — never a live/paused one — so it cannot re-create the
silent-cadence stall. When it actually removes a reminder it logs
`reminder_gc.removed …` and fires an operator alert on
`legba.alerts.reminder_gc`. Steady-state removed-count is 0. Watch it:

```
docker logs legba-legba-runtime-dapr-1 | grep reminder_gc
# reminder_gc.sweep retired_scanned=N candidates=M removed=K failed=0
```

This closes the "fires once then silent" orphan-on-retire failure mode
without the full scheduler-data wipe below. (Part 2 — enumerating the
*entire* scheduler reminder set to GC reminders whose `actor_state` row
was itself lost — is a declared seam, `docs/SEAMS.md` §15, gated behind
`LEGBA_REMINDER_GC_SCHEDULER_SCAN`; daprd 1.17.9 exposes no reminder-list
API on :3500.)

**Last resort (manual full wipe).** If actor_ids have changed (e.g.
descriptor content-hash bumps left orphan reminders the sweep cannot
reach because no `actor_state` row records them), the dapr-scheduler
holds them indefinitely. To clear:

```
# WARNING: nukes all persisted reminders + actor state in dapr-scheduler.
# Use only when fresh-start is acceptable (i.e. actors will re-register
# their reminders on next activation via the reconcile loop).

# Control-plane restarts go TOGETHER, dependency-ordered, else actor placement
# breaks silently:
docker compose --profile dapr --profile runtime stop \
  legba-runtime-dapr dapr-sidecar dapr-scheduler dapr-placement
# Scheduler state is a BIND MOUNT (./deploy/dapr-scheduler-data), NOT a named
# volume — and there is no placement volume. FULL-wipe it (a partial clear
# leaves a stale embedded-etcd cluster → reminders silently stop recurring):
rm -rf deploy/dapr-scheduler-data/*
docker compose --profile dapr --profile runtime up -d \
  dapr-placement dapr-scheduler dapr-sidecar legba-runtime-dapr

# Then restart runtime-dapr so it re-activates actors + re-registers reminders:
docker compose --profile runtime up -d --force-recreate legba-runtime-dapr
```

### Set production-mode auth + persistent signing key

Two operator items the bootstrap warns about — both gate "production
ready" auth + audit integrity. Done state as of 2026-05-29: both keys
live in `.env`; `.legba_signing_key` and `.legba_bearer_token` carry
the raw forms and are gitignored.

**For a fresh host, generate both:**

```bash
# Bearer token — 32-byte hex.
NEW_TOKEN=$(openssl rand -hex 32)

# Ed25519 signing key (hex) for LEGBA_REGISTRY_SIGNING_KEY, AND the
# 32 raw bytes at .legba_signing_key for future
# LEGBA_REGISTRY_SIGNING_KEY_FILE use (mount via docker compose volume).
KEY_HEX=$(python3 -c "from nacl.signing import SigningKey; \
  print(SigningKey.generate().encode().hex())")
python3 -c "
import sys
raw = bytes.fromhex('$KEY_HEX')
open('/usr/local/deployments/active/legba/.legba_signing_key', 'wb').write(raw)
"
chmod 600 .legba_signing_key
echo "$NEW_TOKEN" > .legba_bearer_token && chmod 600 .legba_bearer_token

# Append to .env (preserves prior content):
cat >> .env <<EOF

# production bearer token
LEGBA_REGISTRY_API_TOKEN=${NEW_TOKEN}
# persistent Ed25519 signing key (hex)
LEGBA_REGISTRY_SIGNING_KEY=${KEY_HEX}
EOF
```

Both env vars are read by `legba-registry` and `legba-runtime-dapr` via
the compose `env_file: .env` directive. Restart both services to pick
them up:

```bash
docker compose --profile runtime up -d --force-recreate \
    --no-deps legba-registry legba-runtime-dapr
```

Verify:

```bash
# Bearer enforcement — 200 / 403 / 401 pattern.
NEW_TOKEN=$(grep '^LEGBA_REGISTRY_API_TOKEN=' .env | cut -d= -f2)
curl -s -o /dev/null -w '%{http_code}\n' \
    -H "Authorization: Bearer ${NEW_TOKEN}" \
    http://127.0.0.1:8090/api/v1/registry/stack       # 200
curl -s -o /dev/null -w '%{http_code}\n' \
    -H 'Authorization: Bearer dev' \
    http://127.0.0.1:8090/api/v1/registry/stack       # 403
curl -s -o /dev/null -w '%{http_code}\n' \
    http://127.0.0.1:8090/api/v1/registry/stack       # 401

# Signing key — bootstrap log must NOT show ephemeral warning.
docker logs legba-legba-runtime-dapr-1 | grep -i ephemeral || \
    echo 'persistent key loaded'
docker logs legba-legba-runtime-dapr-1 | \
    grep audit_checkpointer.signer
# expect: did=did:legba:registry:<host-derived> (stable across restarts)
```

**Operator-side script auth:** as of 2026-06-02 the
`bringup_*.py` scripts auto-resolve the bearer token via
`scripts/_token.py::resolve_token()`. Resolution order:

  1. `LEGBA_REGISTRY_TOKEN` env var (legacy override)
  2. `LEGBA_REGISTRY_API_TOKEN` env var (production name)
  3. `LEGBA_REGISTRY_API_TOKEN=...` line read from `.env` at repo root
  4. `"dev"` fallback

Operator no longer needs to export the token manually; just run:

```bash
python3 scripts/bringup_register_multi_country_targets.py
```

The script picks up the `.env`-resident token automatically.

### Caddy auth chain (browser → caddy → registry)

The public surface is the configured `$LEGBA_PUBLIC_DOMAIN`; caddy fronts
both the SPA and the registry API. Two-layer auth, designed so they don't fight:

| Layer | What | Where |
|---|---|---|
| Perimeter (humans) | basic_auth — browser prompts at site entry, caches creds for session | `basic_auth_perimeter` snippet imported into the SPA-static handle and the `/api/*` handle |
| App-layer (machines) | Bearer injection — caddy replaces incoming Authorization with `Bearer ${LEGBA_REGISTRY_API_TOKEN}` for upstream | `header_up Authorization "Bearer {$LEGBA_REGISTRY_API_TOKEN}"` on every reverse_proxy block |

**WebSocket exception:** `/api/v1/registry/events*` is routed by a
handle block that **bypasses basic_auth**. Browsers can't carry
`Authorization` on `new WebSocket()` upgrade requests (the JS API
doesn't expose request headers), so basic_auth on this path produces
a 401-prompt loop on every reconnect. Security is preserved via the
bearer injection — only requests through this caddy get the bearer,
and the registry is internal-network only, so direct hits are
unreachable from the public internet.

**SPA-side contract:** `apiGet` in `src/lib/api.ts` sends no
`Authorization` header when `localStorage.legba_token` is empty (the
canonical state). The browser auto-attaches its cached
`Authorization: Basic ...` from when the operator typed the
basic_auth password; caddy validates Basic, then `header_up` replaces
it with the bearer for upstream. If a stale `legba_token` value sits
in localStorage, the SPA will send `Authorization: Bearer <stale>` —
caddy basic_auth rejects (Bearer != Basic) and the browser
re-prompts. Operator fix: dev-console `localStorage.removeItem('legba_token')`.

The compose service `legba-caddy` reads `LEGBA_REGISTRY_API_TOKEN`
via `env_file: .env` so the Caddyfile placeholder resolves at parse
time. After rotating the bearer, restart caddy with `docker compose
--profile runtime up -d --force-recreate --no-deps legba-caddy`.

### Cross-pillar Mnemosyne A2A — wire-shape note

The outbound A2A client (`legba.clients.mnemosyne_a2a`) ships
the legba-native signed-envelope shape (mirrors the inbound
`register_a2a_skill_route`). Mnemosyne's current `/a2a` endpoint
expects JSON-RPC `tasks/send` per its
`backend/app/services/a2a/server.py`. As of 2026-05-29 the two do
NOT interop directly. Two paths:

- (a) Mnemosyne adds a symmetric `/a2a/skills/{skill_id}` route that
  accepts the legba-native envelope (then no Legba-side change).
- (b) Translate inside `MnemosyneA2AClient.invoke()` — only the
  request-body construction needs to swap; the response-mapping is
  already in place.

For today's analyst-tool path, the `mnemosyne_trust_query.py`
tool speaks JSON-RPC directly per the 2026-05-12 contract — that
remains the working transport when an analyst whitelists the tool.

### MCP integration (Claude Code)

See `DIRECTION.md` §4 (MCP server) for the current state of the MCP
surface and its setup. The Claude Code MCP host config points at
the legba-mcp image launched per-conversation:

```jsonc
{
  "mcpServers": {
    "legba": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--network=legba_default",
               "--env-file=/usr/local/deployments/active/legba/.env",
               "legba/legba-mcp:latest"]
    }
  }
}
```

### Consult / deep_consult model picker (opus vs core)

Each on-demand `consult` / `deep_consult` request may choose **which registered
LLM plane answers it**:

- **`opus`** — the Anthropic Opus plane (**billed**). This is **THE DEFAULT** — a
  request that names no model preserves prior behavior exactly.
- **`core`** — the free self-hosted core (`openai_compat`) plane.

A **server-side allowlist** maps the friendly value → a registered component id;
the client never names a component directly. Selection is **FAIL-CLOSED**: if a
chosen non-default plane can't be honored, the run **raises** rather than silently
falling back to (and billing) the default. A provider outage surfaces as a
graceful **HTTP 503** that names the OTHER plane (e.g. *"The Core plane is
unavailable … select the Opus model"*), not a bare 502.

**UI:** the Consult and DeepConsult panels each carry a model dropdown — labels
*"Opus (Anthropic · billed)"* (default) and *"Core (free)"* — and remember the
last choice. (Backlog: the F1 UI image needs a redeploy to render the neutral
*"Core (free)"* label live.)

**Budget:** accounting keys off the *chosen* plane, but the shared per-day consult
token cap still binds on **both** planes.

**Anthropic is now reserved for `consult` / `deep_consult` ONLY.** Every other
analyst runs on the self-hosted core plane — including the **journal**, whose
GATHER *and* VOICE phases both now run on the core plane (the VOICE phase
previously ran on Anthropic Opus; the §0 journal note is otherwise unchanged).

### world_context RAG guarded pilot (kill-switch + auto-rollback)

Opportunistic RAG (`vector:world_context` BACKGROUND PRIORS, §7.3) is **re-activated
as a GUARDED, MEASURED PILOT on the `internal_stability` unit ONLY**
(`leadership_transition` RAG is OFF as of the 2026-07-03 rollback). The embedder
(bge-m3) was never the problem; the recalibration fixed *retrieval usage* — a
focused `"<country> <theme>"` query (was a diluted unit-name + entity blob), chunks
embedded with a `"<Country> — <section>"` context lead, the 293-point corpus
re-embedded in place, and the relevance floor lowered **0.65 → 0.55** (on-target
now ~0.6, off-target ~0.42). The injected priors stay **NON-CITABLE** (fenced
background, no `[N]` ids).

A **REAL per-run auto-rollback guard** (`src/legba/runtime/rag_rollback.py`)
replaces the old comments-only one: it re-checks the kill-switch **on EVERY
grounding build**, so a rollback suppresses injection on the unit's **NEXT run
WITHOUT a restart** (no descriptor PUT). Its inputs are a union:

- **`LEGBA_WORLD_CONTEXT_DISABLED_UNITS`** — env pin: a comma-separated list of
  `analyst_id`s to hold OFF. Set-and-forget.
- **`LEGBA_RAG_ROLLBACK_STATE`** — path to the persisted rollback state file that
  `rag_watch --enforce` writes. ⚠️ **This currently lives at an ephemeral `/tmp`
  path — point it at a mounted volume for the rollback to survive a container
  recreate.** With neither the env pin nor a state path set, an enforced rollback
  can't persist (`rag_rollback.record.no_state_path`).

Operate it:

```
# Report BEFORE/AFTER faithfulness + low-faith rate + token/latency for a unit,
# and evaluate the pre-registered rollback rule (read-only, no writes):
python3 scripts/rag_watch.py --unit internal_stability
python3 scripts/rag_watch.py --all-units

# ACTUATE: if the rule TRIGGERED, persist the unit into the kill-switch so the
# runtime auto-reverts on its NEXT run (writes LEGBA_RAG_ROLLBACK_STATE):
python3 scripts/rag_watch.py --unit internal_stability --enforce

# Re-embed the corpus in place after an embedding-convention change (dry-run first):
python3 scripts/reembed_world_context.py --dry-run
python3 scripts/reembed_world_context.py
```

**Rollback triggers:** a faithfulness drop, a rising low-faith ratio, or a
token-cost rise (≥35%). Per-run trace instrumentation records
`world_context_top_score` / `retained` / `min_score` so the measurement stays
honest. Floor override: **`LEGBA_WORLD_CONTEXT_MIN_SCORE`** (default **0.55**) —
after a re-embed you can tighten it toward 0.58–0.60 once the on-target probe
clears it.

⚠️ **KNOWN LIMIT (declared, not solved):** firing RAG has historically thickened
the low-faithfulness TAIL even with the non-citable header; the guard reverts if
that recurs, but this is a **guarded pilot, not a finished feature**, and its
state file sits at an ephemeral path until moved to a volume.

### Re-auth + un-pause the Telegram source

The `telegram_channel` source (descriptor `descriptors/source_telegram_monitor.yaml`,
id `source.telegram.org_channels`) is **PAUSED**. A dead/expired MTProto session had
dropped Telethon into a reconnect **hot-loop that flooded ~95% of the runtime log**;
that is fixed (bounded transport reconnect + per-pull client teardown + tamed
`telethon.*` loggers), but the source **cannot be un-paused until an operator
re-mints a valid session** — and that login is **INTERACTIVE** (Telegram requires
the account phone number + the code it texts, plus a 2FA password if set), so it is
done out-of-band, never by the runtime.

Re-auth (interactive — needs a TTY):

```
export TELEGRAM_API_ID=...        # from https://my.telegram.org
export TELEGRAM_API_HASH=...
python3 scripts/telethon_auth.py  # type the phone + the code Telegram texts you
# → prints a `TELEGRAM_SESSION_B64=...` line: a base64 Telethon SQLite session.
#   Treat it like a password — it grants FULL access to that account. Never commit.
```

Then load the three vault secrets (`source.telegram.api_id` / `api_hash` /
`session`) and un-pause the descriptor:

```
# 1. Put TELEGRAM_SESSION_B64=... (+ the api id/hash) into gitignored .env, then:
python3 scripts/bringup_vault_load.py
# 2. Un-pause the source descriptor by PUTting its body with identity.state=active
#    — same mechanism as "Activate a country target" above.
```

Verify each channel handle resolves to the organization's OFFICIAL channel before
flipping active (handles are claimable), then confirm it pulls fresh signals with
no `telethon` transport hot-loop in the runtime log. The runtime image must carry
the `telethon` dep (see §15) or the handler's lazy import fails loud at configure
time.

## 12. Known issues (as of 2026-05-23)

1. **Container-mode bring-up is new.** The 2026-05-21 production
   ingest run used host systemd units (`deploy/systemd/`). Some
   bring-up scripts may still reference 127.0.0.1 paths that need
   the docker-service-name substitution. Section 13 covers the
   host-mode fallback.

2. **Stale daprd actor reminders from prior bring-ups.** Daprd's
   scheduler persists reminders across restarts. After switching
   between embedded vs containerized runtime, expect a window where
   daprd dispatches reminders for actor IDs that 404 against the
   current runtime. Workaround: bring the dapr profile down, delete
   the `deploy/dapr-scheduler-data/` host bind directory, bring
   back up.

3. **No /health endpoint on the registry FastAPI app** (LOW).
   Caddy's healthcheck probes `/api/v1/registry/stack`; if you want
   a dedicated /healthz, add one in `legba.data.registry.api`.

4. **Ephemeral signing key in dev.** Without
   `LEGBA_REGISTRY_SIGNING_KEY[_FILE]` set, audit log signatures
   don't verify across process restarts. Generate + persist a real
   key for anything production-like.

5. **CSS `@import` ordering in `legba-ui-v3/src/globals.css`.** The
   Dockview stylesheet `@import` MUST appear before any `@tailwind`
   directive. CSS spec requires `@import` at the very top of the
   stylesheet; PostCSS drops mis-ordered imports silently. Symptom:
   built bundle has zero `.dv-*` classes, Dockview panels render as
   bare stacked divs (no tabs, no resize handles, no group
   decorations). Verify after any globals.css edit:

   ```bash
   docker run --rm -v legba_ui_dist:/dist alpine sh -c \
       "grep -oE '\\.dv-[a-zA-Z0-9_-]+' /dist/assets/index-*.css | sort -u | wc -l"
   # expect ~80; if 0, the import is being dropped.
   ```

6. **WebSocket basic_auth incompatibility.** The browser
   `new WebSocket(url)` JS API cannot send custom `Authorization`
   headers on the upgrade request. Any `/api/v1/registry/events*`
   path under caddy basic_auth will 401 the WS upgrade and trigger a
   prompt loop. The Caddyfile routes WS events to a handle block
   that bypasses basic_auth and relies on the upstream bearer
   injection for auth. Don't move the WS path back into the
   basic_auth-gated handle.

## 13. Alternative: host-mode systemd

The pre-2026-05-23 host-mode bring-up used systemd units that ran
`legba-registry` + `legba-runtime-dapr` directly on the host. The
unit files at `deploy/systemd/` are kept as a fallback for hosts
without docker daemon access.

```
# install:
sudo cp deploy/systemd/legba-registry.service /etc/systemd/system/
sudo cp deploy/systemd/legba-runtime-dapr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now legba-registry legba-runtime-dapr
```

For host-mode + containerized-dapr-sidecar (the original 2026-05-21
shape), set `LEGBA_DAPR_APP_CHANNEL=host.docker.internal` in `.env`
so the sidecar dispatches into the host process rather than into the
runtime container. Then:

```
docker compose --profile dapr up -d                     # substrate-mode
sudo systemctl start legba-registry legba-runtime-dapr  # host-mode app
```

The host-mode path is not the canonical bring-up; only fall back to
it when docker daemon access is unavailable.

## 14. File layout cheat sheet

```
descriptors/                 — YAML descriptor source (target / analyst / discovery)
docker/                      — multi-stage Dockerfiles + Caddyfile
  Dockerfile.registry        — legba-registry image
  Dockerfile.runtime         — legba-runtime-dapr image (also used by the
                               optional dapr-workflow worker)
  Dockerfile.mcp             — legba-mcp stdio image (Claude Code)
  Caddyfile                  — caddy reverse-proxy + SPA static serve
legba-ui-v3/Dockerfile       — multi-stage UI build (node:20-slim → alpine + dist/)
src/legba/data/migrations/   — *.sql, applied via `python -m legba.data.migrate`
src/legba/data/registry/     — registry API + storage
src/legba/runtime/           — actor host (embedded + Dapr variants)
deploy/systemd/              — *.service unit files (host-mode fallback only)
scripts/bringup_*.py         — bring-up scripts referenced above
logs/                        — log files when running services on the host
docs/                        — architecture + spec references
legba-models/                — GPU-host deployable (deploys SEPARATELY)
```

## 15. Notes on the multi-image split (2026-05-23)

| Image | Approx size | Deps highlights |
|---|---|---|
| `legba-registry` | ~420 MB | fastapi, asyncpg, pynacl, nats-py, starlark-pyo3, stix2 |
| `legba-runtime-dapr` | ~1.4 GB | + dapr + langdetect + statsforecast (numba/llvmlite) + pyod + sklearn (also runs the optional `dapr-workflow` worker) |
| `legba-mcp` | ~400 MB | + mcp + fastapi (transitively required by legba.data.outputs) |
| `legba-ui-build` | ~13 MB | alpine + dist/ static tree (multi-stage, build via node:20-slim) |

The runtime image carries the numeric-stack tail (numpy / scipy /
pandas / sklearn / numba / llvmlite / matplotlib) because the
deterministic + predictor + optimizer analyst kinds need those at
activation time. A dependency prune retired the genuinely heavy ML deps
(torch + sentence-transformers + spacy) — embedding / NER /
translation / classification are hosted-endpoint calls now
(see `docs/AI_MODELS.md`).

Heavy source-kind deps (`google-cloud-bigquery` for GDELT,
`telethon` for Telegram) are NOT in the base runtime image. They're
lazy-imported by their source handler. Operators who deploy GDELT or
Telegram source kinds: rebuild a custom runtime image with those
deps added to `docker/Dockerfile.runtime`, or wait for the
`legba[gdelt,telegram]` optional-extras path.

`legba-models/` (BGE-M3 embeddings + spacy NER + NLLB translation +
classification SLMs) deploys SEPARATELY to the GPU host via its own
`legba-models/docker-compose.yml`. It does NOT co-run on the legba
app host — the runtime hits its endpoints over HTTP per
`docs/AI_MODELS.md`.

## 16. Lessons from the 2026-05-21 bring-up (host-mode era)

* **asyncpg pool + `SET search_path` is a footgun.** The pool's `init`
  callback runs once per fresh connection, but asyncpg internally
  scrubs per-connection state between acquirers — `SET search_path`
  doesn't survive. Always pass a `setup` callback for any session
  state you depend on across acquires, OR fully qualify every
  table/schema reference. We did both in a follow-up so the
  next module that quietly relies on path doesn't bite us.

* **Schema lookups silently land in the wrong schema.**
  `CREATE TABLE IF NOT EXISTS foo` lands in whichever schema is
  *first* on `search_path`. If `ag_catalog` is first (AGE setup), an
  unqualified `actor_state` is created there — and only the *first*
  checkout sees it. Symptom is a flood of `UndefinedTableError`
  *after* a brief window of working operation.

* **Stale daprd reminders survive container restarts.** Daprd's
  scheduler service persists actor reminders in a host bind dir that
  outlives a `docker compose down/up`. After switching descriptors
  or runtime modes, daprd keeps dispatching the old IDs into the
  runtime — runtime returns 404 because those actors don't exist.
  Workaround: wipe scheduler state.

* **`legba.runtime.host` vs `legba.runtime.dapr_host`.** The embedded
  host runs the actor classes in-process and drives them with its
  own cron scheduler — no daprd dependency. The canonical
  containerized path uses `legba.runtime.dapr_host` (the Dapr-actor
  host). The embedded host's console script `legba-runtime` is
  deprecated (2026-05-21).

* **`Scheduler.register_cron` was a no-op after `start()`.** The
  embedded scheduler stored triggers in `_cron_triggers` but only
  spawned per-actor asyncio tasks during `start()` — anything
  registered later was silently never firing. Fixed; regression
  locked at `tests/runtime/test_scheduler.py`.

* **Reconcile cadence is slow on cold-start.** The reconcile loop's
  `_resync_loop` sleeps `LEGBA_RUNTIME_RESYNC_INTERVAL` seconds
  (default 300) *before* its first walk of the registry. Expect a
  ~5-minute gap between starting the runtime and the first
  actor_state row landing.

## 17. Source-first runtime bring-up

The `legba-runtime-dapr` host now boots the **source-first runtime** on top
of the actor surface + reconcile loop. `bring_up_production_runtime()` wires,
in order:

1. **Actor types** — `TargetActor`, `AnalystActor`, **and `SourceActor`** are
   registered with the Dapr `ActorRuntime`. `SourceActor` OWNS acquisition
   (poll Reminder / push webhook → one canonical signal → publish to
   `legba.signals.<tenant>.<source>.<modality>.<event_class>`, the coarse
   subject the `legba_signals` stream captures). The legacy
   target-owned pull path
   (TargetActor pulling inline `SourceBinding`s + `write_target_signal`) is NOT
   wired into the live acquisition chain. TargetActor stays registered only as
   (a) the discovery-materialiser host and (b) the subscriber identity the
   fan-out delivers to.
2. **Job worker pool** (`runtime/jobs`) — the `LEGBA_JOBS` JetStream
   work-queue + shared durable consumer + N competing-consumer workers
   (`LEGBA_JOB_WORKERS`, default 2). Runs `process_media`.
3. **Subscription / fan-out engine** (`runtime/subscription`) — ensures the
   shared `legba_signals` stream and binds ONE subject-filtered per-target
   JetStream consumer for each active target's resolved+authorized source
   bindings (`source_refs`).
4. **Coalescing trigger engine** (`runtime/triggers`) — a per-(analyst, target)
   registry over those bindings + a durable consumer on the matched-signal
   stream; fires the analyst's actor `run` on cadence / accumulation / severity
   (clamped by cooldown).
5. **Action-pack agency** (`data/analysts/agency`) — an `Agency` over the live
   job queue + a NATS governor-event publisher, exposed via
   `runtime.source_first_runtime.AGENCY_HOLDER` for the analyst run path.
6. **`source` descriptor family** in the reconcile loop — the action executor
   maps the `source` actor kind to `SourceActor` (CREATE/RETIRE/TRANSITION)
   alongside target/analyst.

### Production bring-up command

```bash
# daprd sidecar (routes ActorProxy → the host on :6090):
docker compose --profile dapr up -d
# the source-first host:
PYTHONPATH=src legba-runtime-dapr          # → legba.runtime.dapr_host:main
# (containerized: docker compose --profile runtime up -d legba-runtime-dapr)
```

### Startup log signposts (grep these to confirm a clean boot)

```
dapr_host.actor_types.registered types=[... 'SourceActor' ...]   # 3 types
dapr_host.deps_resolvers.registered
dapr_host.reconcile_loop.started
source_first.source_deps_resolver.registered
source_first.job_plane.ready stream=LEGBA_JOBS durable=legba-job-workers workers=N
source_first.agency.ready tools=[...]
source_first.subscription_engine.ready
source_first.wire.target target=<id> bindings=B refused=R filters=F      # per target
source_first.trigger_engine.consumer_ready registrations=M
source_first.worker_pool.started workers=N
source_first.trigger_engine.started
dapr_host.source_first.ready targets_wired=T trigger_regs=M
dapr_host.initial_resync.enqueued count=C
```

If `dapr_host.source_first.bringup_failed` appears, the actor surface +
reconcile loop are still up but the job / fan-out / trigger planes are NOT —
resolve NATS/PG reachability and restart. `targets_wired=0` on a fresh rig is
expected (no source-first targets registered yet); the NATS informer + the
5-min resync re-wire as descriptors land.

### Dev-rig bring-up harness (no daprd required)

To validate the source-first plane wiring against the dev rig WITHOUT a daprd
sidecar (the make-it-boot gate), run the harness — it brings up the same four
planes, prints the signposts, and shuts down clean:

```bash
PYTHONPATH=src \
LEGBA_DATA_PG_DB=legba_pivot_test \
LEGBA_NATS_URL=nats://127.0.0.1:4222 \
LEGBA_REGISTRY_API_URL=http://127.0.0.1:8090 \
python3 scripts/bringup_source_first_host.py --run-seconds 3
```

Exit 0 + `RESULT: clean boot + clean shutdown` = the planes assemble, declare
their NATS topology, ensure the `legba_jobs` + `trigger_state` schemas, and
tear down without leaking a consumer binding. The harness leaves the durable
`LEGBA_JOBS` stream behind (production-correct); the job-plane test suite uses
`jobs.>` per-test streams that overlap it — `delete_stream('LEGBA_JOBS')`
before running `tests/runtime/jobs` on a shared rig.

## 18. Release gate (ordered, fail-fast)

The release gate composes the strict test suite + the no-stub gate +
descriptor validation + the UI tsc build + the secret/codename scan + the
reproducible manifest + the deployed-stack smoke into ONE driver so a
release cut can't skip a step. Run it from the repo root:

```
bash scripts/release_gate.sh                 # full gate (writes release/gate-<utc>.log)
SKIP_SMOKE=1 bash scripts/release_gate.sh    # no live stack to smoke
SKIP_UI=1    bash scripts/release_gate.sh    # no docker for the UI build
```

Stages (each must pass; the gate stops at the first failure):

| # | Stage | Backing command | Pass condition |
|---|---|---|---|
| 1 | Strict test suite | `LEGBA_TEST_STRICT=1 scripts/run_tests_in_container.sh` | Green; INFRA-gated skips ESCALATE to failures (no silent coverage loss). Known pre-existing infra failures (SSRF-127.0.0.1, port-6090, daprd/webhook/agency/critic e2e) are documented exceptions — see PROJECT_STATE. |
| 2 | No-stub gate | `git grep` for stub/mock markers in `src/**` | Zero hits. A genuinely-deferred item is a fail-loud declared SEAM in `docs/SEAMS.md`, never a silent stub (`tests/test_no_undeclared_stubs.py` is the mechanical enforcer). |
| 3 | Descriptor validation | `validate_descriptors.py` (or YAML-parse fallback) | Every descriptor parses + type-checks. |
| 4 | UI build (tsc gate) | `docker compose --profile ui build legba-ui-build` | The container build IS the type-check — a tsc error fails the build. |
| 5 | Secret/codename scan | `scripts/prepush_scan.sh` | See §20. Exits non-zero on any tracked-content / identity hit. |
| 6 | Release manifest | `scripts/make_release_manifest.sh` | Writes `release/manifest-<gitsha>.txt` (image digests + pip freeze + UI lockfile hash + migration baseline + smoke commands). Fold-in keeps it from going stale vs the tag. |
| 7 | Deployed-stack smoke | `scripts/release_smoke.sh` | 401/403/200 bearer pattern, migration-ledger non-empty, caddy edge serving. Optional (`SKIP_SMOKE=1` when no stack is up). |

**Manifest reproducibility.** Compose pins the third-party substrate images
to floating tags (`apache/age:latest`, `qdrant:latest`,
`busybox:latest`) to keep dev velocity. The
manifest freezes the exact answer at release-tag time: resolved sha256
digests (`docker image inspect`), the real installed `pip freeze` from the
built runtime image, the UI `package-lock.json` hash, and the migration
baseline. Re-pinning compose to digests is optional — recording them in the
manifest is the required step.

## 19. Codename / prior-host scan findings (2026-06)

A tree scan for `mnemosyne` and `<prior-host>`:

* **`mnemosyne` — INTENDED component name, not a codename.** It is the
  federation sibling service Legba does A2A trust-query calls to
  (`src/legba/clients/mnemosyne_a2a.py:MnemosyneA2AClient`, the
  `mnemosyne_trust_query` analyst tool, the shared signed-envelope shape).
  Pervasive and legitimate across descriptors / clients / provenance.
  **Leave it.** The only edit made was neutralising one docstring's example
  URL (`mnemosyne.<operator-domain>` → `mnemosyne.example.org`).
* **`<prior-host>` — STRAY prior-host codename in git author metadata.** Zero
  tracked **file-content** hits. But ~90 commits in `origin/main..HEAD`
  carry the author/committer identity `root@<prior-host>` (a
  prior-host hostname under the operator domain). This is NOT a component
  name; it leaked through git config on an earlier host. The fix is the
  neutral-identity squash recipe in §20 — **do NOT rewrite already-public
  history** (it breaks clones); mint the release commit with a neutral
  identity instead.
* **Operator domain `<operator-domain>`.** Neutralised in the cosmetic tracked
  references this stream owns — the dead `<operator-domain>_edge` compose network
  (removed; it was `external: true`, referenced by no service), the compose
  comment, `mnemosyne_a2a.py` docstring, the UI `Dockerfile` comment, the
  a2a-test fallback URLs (now `$LEGBA_PUBLIC_DOMAIN`-driven), and this
  RUNBOOK's verify lines. Secrets themselves live only in the gitignored
  `.env` / vault (untracked) — never in the tree.

## 20. Pre-push secret/codename scan + neutral-identity squash

**The repo is PUBLIC.** Before any push, run the mechanical hygiene gate:

```
bash scripts/prepush_scan.sh           # scans tracked content + origin/main..HEAD
BASE=origin/main bash scripts/prepush_scan.sh
```

It exits non-zero on: the `<prior-host>` codename or `<operator-domain>` domain in tracked
content; a tracked `.env`/secret/private-key file; a `PASS|SECRET|TOKEN|
API_KEY` assigned a long literal; a high-entropy literal (heuristic); a
**non-neutral commit author/committer identity** on the push range; or a
tracked `planning/` file (that directory is gitignored internal tracking and
must never be committed). If `gitleaks` is on PATH it also runs (best-effort).
`mnemosyne` is deliberately NOT scanned (it is a real component — §19).

**Current tree state:** the file-content checks (1–6, 8) are CLEAN. Check 7
(commit identity) still flags `root@<prior-host>` (+ stray
`wave@localhost` / `wave-agent@localhost` wave-agent identities) on
`origin/main..HEAD` — resolved by the squash below, which is the intended
remediation and is **operator-only**.

### Neutral-identity squash recipe (operator-only)

The codename lives in commit *metadata*, not file content, and is ALREADY on
`origin/main`. Scrubbing public history breaks every clone — decline that.
Instead, mint ONE release commit with the neutral identity so the *new* tip
carries no codename:

```bash
# 0. Confirm git config is the neutral release identity FIRST.
git config user.name  "legba-dev"
git config user.email "dev@legba.invalid"

# 1. From the release branch, soft-reset to the public base so the working
#    tree is unchanged but staged as one diff. Do NOT use `-p main` style
#    parenting (it keeps the codename-identity parents reachable from the tip).
git checkout -B release working-2026-06
git reset --soft origin/main

# 2. Re-commit the entire delta as a single neutral-identity commit.
git commit --no-gpg-sign \
  --author="legba-dev <dev@legba.invalid>" \
  -m "Release: source-first Legba (squashed)"

# 3. Re-run the gate against the squashed tip — check 7 must now be clean.
BASE=origin/main bash scripts/prepush_scan.sh

# 4. Push is the OPERATOR's call (never automated).
#    git push origin release   # operator only
```

No `Co-Authored-By` trailer on any commit (project rule).

## 21. Release checklist (pre-tag)

- [ ] `bash scripts/release_gate.sh` green (or with documented infra-skip
      exceptions); gate log committed under `release/`.
- [ ] `scripts/prepush_scan.sh` file-content checks clean; commit-identity
      resolved via the §20 squash (operator).
- [ ] `release/manifest-<gitsha>.txt` regenerated and committed.
- [ ] **`legba-models` perimeter:** confirm the inference port is
      127.0.0.1 / docker-network-internal-only on the GPU host
      (`docker compose port legba-models 8700` shows NO public bind); the 5
      endpoints have no in-app auth by default (§11 perimeter note). Optionally
      set `LEGBA_MODELS_API_SECRET` on both sides.
- [ ] **Legacy `legba-models` credential decommissioned (operator).** An
      earlier hosted-NLP credential was exposed and must be **revoked at the
      provider** — a doc note cannot enforce revocation. Rotate per §11
      "Rotate MODELS_API credentials", then confirm the OLD credential no
      longer authenticates at the provider. Record the decommission date in
      the operator's internal tracking (not in this public doc).
- [ ] Persistent signing key + production bearer token set in `.env`
      (§7 "Set production-mode auth"); bootstrap log shows no ephemeral-key
      warning.
- [ ] Migrations applied; ledger verified (§3).
## 22. Multi-replica local proof (scaling-multinode)

> Locked decision **D3**: **prove the multi-node design LOCALLY, no load
> test.** This section is the documented
> procedure that proves Dapr placement redistributes actors + reminder
> scheduling + fan-out across two `legba-runtime-dapr` replicas, each with its
> OWN co-located daprd sidecar, while the **singleton control-plane loops run on
> exactly one replica** via Postgres-advisory-lock leader election.

### 22.0 What is replica-safe vs singleton

| Plane | Runs where | Why safe across replicas |
|---|---|---|
| SourceActor / AnalystActor / TargetActor | spread by **placement** | Dapr consistent-hashes actor IDs → each actor activates on exactly ONE replica; reminders fire on the owning node. |
| Coalescer fire-claim | all replicas | CAS on the fire anchor (`claim_fire`) — exactly one worker wins a fire. |
| Trigger engine | all replicas | ONE shared durable PULL consumer → JetStream load-balances messages across the replicas' subscriptions. |
| NATS source/job consumers | all replicas | per-instance durable / shared durable as designed. |
| **Reconcile resync + descriptor informer** | **LEADER ONLY** | NOT idempotent to double-run cheaply (each resync re-issues CREATE/RETIRE actions; the informer's per-instance durable fans every event to every replica). The **leader lease** gates them to one replica. |

### 22.1 The fail-loud guard (item a)

Every runtime boot calls `assert_singleton_safe()` (`src/legba/runtime/leader.py`):

* `LEGBA_REPLICA_COUNT <= 1` → boots (single-node; default).
* `LEGBA_REPLICA_COUNT > 1` **and** `LEGBA_LEADER_ELECTION` set (`pg-advisory`) → boots.
* `LEGBA_REPLICA_COUNT > 1` **and** `LEGBA_LEADER_ELECTION` unset → **`SingletonSafetyError` at boot** — the container refuses to start. This converts a naive `replicas: 2` into a loud refusal instead of a silent double-run.

Verify the guard fires (no stack needed — just the image):

```bash
docker run --rm -e LEGBA_REPLICA_COUNT=2 legba/legba-runtime-dapr:latest \
  python -c "from legba.runtime.leader import assert_singleton_safe; assert_singleton_safe()"
# → SingletonSafetyError ... LEGBA_REPLICA_COUNT=2 (>1) but LEGBA_LEADER_ELECTION is unset/off ...
# exit code != 0
```

### 22.2 Bring up the 2-replica stack (items b + c)

```bash
docker compose -f docker-compose.yml -f docker-compose.replicas.yml \
  --profile runtime up -d
```

`docker-compose.replicas.yml` adds `legba-runtime-dapr-2` + its sidecar
`dapr-sidecar-2` (same app-id `legba-runtime`, same placement/scheduler, its own
app-channel + loopback ports 6091 / 3501), and sets `LEGBA_REPLICA_COUNT=2` +
`LEGBA_LEADER_ELECTION=pg-advisory` on BOTH replicas. (`deploy.replicas: 2` is
NOT used — compose can't pair each replica with its own sidecar; see the header
comment in the override file. Kubernetes solves this natively via the sidecar
injector — the production target.)

### 22.3 Prove it

1. **Both replicas boot; guard passes; exactly one leader.**
   ```bash
   curl -s localhost:6090/healthz | jq '{leader,leader_election,replica_count,reconcile_running}'
   curl -s localhost:6091/healthz | jq '{leader,leader_election,replica_count,reconcile_running}'
   ```
   Exactly ONE reports `"leader": true` + `"reconcile_running": true`; the other
   reports `"leader": false` + `"reconcile_running": false`. Both report
   `"leader_election": "on"`, `"replica_count": 2`. Confirm in the logs:
   `leader.singleton_guard.ok` on both, `leader.acquired` on the leader,
   `dapr_host.standby` on the other.

2. **Placement spreads actors across replicas.** Each replica logs which actor
   IDs it activates. Grep both logs for `source_actor` / analyst-worker
   activations and confirm distinct IDs land on each:
   ```bash
   docker compose logs legba-runtime-dapr   | grep -iE "actor.activate|reminder.fire"
   docker compose logs legba-runtime-dapr-2 | grep -iE "actor.activate|reminder.fire"
   ```
   The two sets are disjoint (consistent-hash). Reminders for an actor fire on
   the replica that owns it — a poll/cadence reminder logged on replica 2 proves
   the scheduler routes to the owning node, not always replica 1.

3. **Only the leader mutates the control plane.** `reconcile.action`
   (CREATE/RETIRE/TRANSITION) + `nats_informer.enqueued` lines appear in the
   LEADER's log only:
   ```bash
   docker compose logs legba-runtime-dapr-2 | grep -cE "reconcile.action|nats_informer.enqueued"
   ```
   On the standby this count stays 0 (no duplicate mount/retire).

4. **Fan-out load-balances.** Inject signals (register a source, or let the live
   sources poll) and confirm trigger fires (`trigger.dispatch`) land on BOTH
   replicas — the shared durable consumer balances them.

5. **Failover — kill the leader.** Stop whichever replica is leader:
   ```bash
   docker compose stop legba-runtime-dapr   # if it was the leader
   ```
   Within `LeaderLease.acquire_interval_seconds` (~10s) the standby acquires the
   advisory lock (Postgres frees the dead leader's session lock) and promotes:
   `leader.acquired` + `dapr_host.reconcile_loop.started (leader)` on the
   survivor; `curl localhost:6091/healthz` now shows `"leader": true`. Dapr
   placement drains the dead replica's actors onto the survivor.

6. **Failover — kill a non-leader.** Stopping the standby changes nothing about
   the control plane; placement redistributes its actors onto the leader.

### 22.4 Record the outcome

Record the observed result (even actor spread, single leader, clean failover)
in the operator's internal tracking. A load-test ceiling
number is explicitly OUT of scope per D3 — the harness was dropped.

## 23. Backup & restore (resilience-observability W-1b §5)

Covers all four substrate stores: **Postgres** (incl. the AGE graph), **Redis**,
**Qdrant**, and **NATS JetStream**.

### 23.1 Manual / ad-hoc backup

```bash
# Everything (pg, redis, qdrant, nats):
bash scripts/backup.sh
# A subset:
bash scripts/backup.sh pg nats
```

Output lands under `/var/backups/legba/<timestamp>/`:
`postgres_legba.sql.gz`, `redis_dump.rdb`, `qdrant/`,
`nats.tar.gz`. NATS uses the `nats` CLI (`stream backup` per stream) when
present, else falls back to a tar of the JetStream `/data` store dir copied out
of the container.

### 23.2 Scheduled backup (cron / systemd timer)

`scripts/backup_scheduled.sh` wraps `backup.sh` with **retention** (keep the
newest `LEGBA_BACKUP_KEEP` generations, default 14) and an **offsite** push.

```bash
sudo cp deploy/systemd/legba-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now legba-backup.timer    # nightly 03:30 + jitter
systemctl list-timers legba-backup.timer          # confirm next run
sudo systemctl start legba-backup.service          # run once now
```

**Offsite is OFF until configured** (declared SEAM — see `docs/SEAMS.md`). Set
in `.env`:

```ini
LEGBA_BACKUP_OFFSITE_DEST=rsync://backup-host/legba   # or s3://bucket/legba
LEGBA_BACKUP_OFFSITE_TOOL=rsync                       # rsync | s3 | rclone
```

Until then the wrapper warns loudly and drops an `OFFSITE_NOT_CONFIGURED.txt`
marker in the generation directory. A configured-but-failing offsite push exits
non-zero (the systemd unit goes failed) — failure is never silent.

### 23.3 Restore drill (run periodically — an untested backup is not a backup)

Restore into a SCRATCH stack (never the live one) and verify. Example against a
parallel compose project `legba_restore`:

```bash
GEN=/var/backups/legba/<timestamp>            # the generation to restore

# Postgres — recreate DB, load AGE first, then the dump.
gunzip -c "$GEN/postgres_legba.sql.gz" \
  | docker compose -p legba_restore exec -T postgres \
      psql -U legba -d legba
# Sanity: row counts on the hot tables.
docker compose -p legba_restore exec -T postgres \
  psql -U legba -d legba -c \
  "select count(*) from signals; select count(*) from analyst_outputs;"

# Redis — drop the RDB in place and restart the container.
docker cp "$GEN/redis_dump.rdb" "$(docker compose -p legba_restore ps -q redis)":/data/dump.rdb
docker compose -p legba_restore restart redis

# Qdrant — recover each per-collection snapshot via the snapshots API.
for f in "$GEN"/qdrant/*; do
  coll="$(basename "$f" | sed 's/_[^_]*$//')"
  curl -sf -X PUT "http://127.0.0.1:6333/collections/${coll}/snapshots/recover" \
    -H 'content-type: application/json' \
    -d "{\"location\":\"file://$(realpath "$f")\"}"
done

# NATS JetStream — restore each stream (CLI path), or stop nats + replace /data.
tar -xzf "$GEN/nats.tar.gz" -C "$GEN"
for s in "$GEN"/nats/*/; do
  nats --server nats://127.0.0.1:4222 stream restore "$(basename "$s")" "$s"
done
# Store-dir fallback: docker compose stop nats; docker cp "$GEN/nats/." <nats>:/data; start.
```

**Acceptance for the drill:** the registry comes up healthy
(`/api/v1/registry/healthz` → 200), the `signals` + `analyst_outputs` row counts
match the source rig within tolerance, and a fan-out produces a fresh finding.
Record the drill date + result in the ops log.

## 24. Host stall watchdog (actor-plane auto-recovery)

The Dapr sidecar's actor plane can degrade silently — reminders/invokes stop,
ingestion and every analyst cadence die, yet every container reports healthy
(observed 2026-07-14 and 2026-07-15; the second stall cost ~39h). The
in-container `liveness_watchdog` detects the stall and writes a durable
`alert_sink_deliveries` row, but has no docker access, so recovery needs a
host-side actuator.

**The actuator:** `scripts/host_stall_watchdog.sh`, run by a root cron every 5
minutes (`/etc/cron.d/legba-watchdog`). It checks the freshest `signals` row's
age straight from Postgres; when the pipeline is provably dead (age > 30 min,
default `MAX_AGE_SECS=1800` — the healthy p100 inter-signal gap measured ≤18
min over 7 days) it executes the recovery in the **B0-13 P2 order (2026-07-23,
scheduler-log-proven): restart the RUNTIME first, wait for its healthcheck
(bounded 30×5s), then the sidecar, then the dapr-workflow worker.** Order is
load-bearing: the old sidecar-first order made the sidecar re-report its host
to placement before the ~19s Python boot bound :6090, so the placement table
published with only the workflow-engine actor types — business actors had no
host, and the first restart always failed while an identical second restart
(image hot in page cache, <1s boot) always won. It was never the worker and
never a delay. The script then **verifies the recovery took** (+12 min
re-check of the signal age) and, if flow has not resumed, repeats the trio
ONCE, cooldown-exempt. It stamps a 45-min cooldown and inserts an
`alert_sink_deliveries` row (`sink_kind='host_watchdog'`,
`status='auto_recovered'`) so the recovery is visible in the escalations
panel. Expected stall cost under this order: ~2-3 minutes detect-to-recovered.

**Safety ladder (all must pass before a restart):** maintenance flag → all
three containers running (else a deploy is in progress — hands off) → runtime
15-min warmup grace → the age query must SUCCEED (a query failure never
restarts; the fault is unproven) → age over threshold → cooldown not active
(a persisting stall after one auto-restart logs `ESCALATE` and stops — a
restart loop is the churn that causes stalls).

**Operations:**

```bash
# disable during maintenance / manual deploys
touch /etc/legba-watchdog.disabled       # re-enable: rm the flag
# observe
tail /var/log/legba_host_watchdog.log    # events only (skips/fires/escalates)
stat /var/lib/legba-watchdog/heartbeat   # mtime proves the cron itself is alive
# dry-run the decision path
DRY_RUN=1 MAX_AGE_SECS=1 GRACE_SECS=1 scripts/host_stall_watchdog.sh
```

The cron entry is host-side (not in the repo); reinstall on a new host:

```
*/5 * * * * root /usr/local/deployments/active/legba/scripts/host_stall_watchdog.sh >> /var/log/legba_host_watchdog.log 2>&1
```
