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
[23 Backup & restore](#23-backup--restore-resilience-observability-w-1b-5) ·
[24 Host stall watchdog](#24-host-stall-watchdog-actor-plane-auto-recovery) ·
[24.1 LLM/search plane heartbeats](#241-llm--search-plane-heartbeats)

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

- **Deploy a fresh instance to CURRENT scope (not just the 3-feed cold-start).** The canonical one-command path (`deploy/deploy.sh --seed`, §2) does all of this; the steps below are what it automates, for reference / partial re-runs. The minimal cold-start verification set is 3 shared world-news sources (BBC / Deutsche Welle / Al Jazeera) — that is the cold-start *smoke test*, NOT the deployed scope and NOT a proven-live limit. The live system runs the full source catalog (the catalog defines 46 handler integrations in `scripts/bringup_register_source_catalog.py`; ~57 registered source descriptors, ~50 live/active including seed/baseline plus the standalone state-media feeds IRNA / PressTV / Ukrinform and the UCDP GED adapter — the latter **retired** pending an operator-held access token, SEAMS #37). To stand a fresh instance up to current scope:
  1. Empty substrate up + schema (§2–§3): a fresh deploy applies the single proven baseline `deploy/baseline/0001_baseline.sql` (ledger pre-seeded to head **0053**), then `migrate` applies any future (`0054`+) migrations — currently `0054`…`0105` (live head **0105**; `0095`/`0100` intentionally unused — the runner discovers by sorted glob, so gaps are harmless).
  2. Vault + stack components (§6–§7), then the source-first working set — packs, the 3 minimal sources, 19 G20 targets, the analysts. **`deploy.sh` registers the LIVE analysis spine via the split registrars** — `bringup_register_analysts.py` registers the eight geopolitics bounded units + the composition tower (`country_composition` / `region_composition` / `world_assessor` / thematic `escalation_composition`) + the deterministic I&W pair (`indicator_tracker` / `collection_gap`) — the ninth unit, `disruption_status`, ships instead with `bringup_register_supply_chain_pack.py` alongside its thematic lane/flow desks; `bringup_register_watch_country_targets.py` adds the watch tier (13 desks today — extend its `WATCH_ISO2` list to add more); `bringup_register_region_targets.py` adds the 5 region frames. (The older combined `scripts/bringup_register_p17_workingset.py` is a **frozen legacy path** that registers the RETIRED `country_assessor` monolith set — it does NOT bring up the current spine; prefer `deploy.sh`.)
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
  `docker run -i --rm`. Ships **seven built-in substrate tools**
  (`substrate_findings` / `substrate_situations` / `substrate_signals` /
  `lineage_walk` / `since` / `export` / `consult`) — reads + consult only,
  mutations rejected; needs the registry reachable (`--network legba_default`).
* **Alerts (profile `alerts`):** `ntfy` — a local push-notification
  service (image `binwiederhier/ntfy`, loopback `127.0.0.1:8093`, cache
  volume `legba_ntfy_cache` so topic history survives recreates;
  `NTFY_BASE_URL` via `LEGBA_NTFY_BASE_URL`). Inert unless the profile is
  started AND a sink env (`LEGBA_ALERT_NTFY_URL`) points at it — see §4.0.1.
* **Extra source lanes (profile `sources-extra`):** `rsshub` — a local
  RSSHub instance (loopback `127.0.0.1:1200`; compose peers reach it as
  `rsshub:1200`) backing the profile-gated RSSHub draft source
  descriptors. Inert by default; the runtime's RSS SSRF guard is punched
  for it via `LEGBA_EGRESS_ALLOW_HOSTS` (compose default `rsshub`).
* **Evidence-archive volume:** `legba-runtime-dapr` mounts the named
  volume `legba_archive` at `/var/lib/legba/archive` (`LEGBA_ARCHIVE_ROOT`)
  — the content-addressed store the `evidence_archiver` writes
  (`cas:sha256/<hex>`). Archived objects are evidence: nothing deletes
  them (SEAMS #42) — disk is an operator watch item.

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

> A **fresh deploy** does not replay the full migration history: it applies the
> single round-trip-proven baseline `deploy/baseline/0001_baseline.sql` (which builds
> the schema + AGE graph and pre-seeds the ledger to head **0053**), then `migrate`
> applies any **future** (`0054`+) migrations — currently `0054`…`0189`; live head
> **0189** (`0095`/`0100`/`0110`/`0111` and several later slots intentionally
> unused; the runner discovers by sorted glob, so gaps are harmless). Highlights: the contested-claims schema, the
> `unit_reference_labels`
> gold table, and the composition-tower supersession fold (`0054`…`0060`); the
> DQ-program migrations (`0061`…`0075`); the 2026-07 audit-remediation sweep
> (`0076`…`0080`); the signal-content-depth markers (`0081`…`0085`); the
> entity-identity / salience / journal-data wave (`0086`…`0090`); and the
> 2026-07-28 release wave (`0091`…`0105` — alert-trigger watermarks, poll
> `newest_entry_ts`, band-calibration claims, the source-assurance ledger,
> correctness labels + gold-set pinning, contention surfacing + the tie-break
> cache, fact-decay states, source track records, the traces-retention index,
> narratives + echo edges, desk baselines, the evidence archive, and the
> watchlist — all additive/idempotent); the follow-on wave (`0106`…`0116` —
> forward consumption, review flags + bearing edges, retention policies,
> retrieval origin, collection requirements, the source-quality view); and the
> 2026-08 arc (`0117`…`0189`, sparse-numbered — hygiene closes, the
> `entity_edges` graph substrate + backfills, corpus tombstones, the
> `situation_events` trajectory ledger at `0184`, the merge-keeper repoint at
> `0185`, and the four listed below; see `DATA_MODEL.md` for the per-table
> detail). The audit-remediation migrations are **demote/close-only** (they
> tombstone or re-fold junk, never hard-delete):
>
> - **0076** — entity re-fold + junk gate (`entity_profiles` 12,257 → 12,144).
> - **0077** — close semantic / demonym / relative-temporal junk facts (reversible `valid_until`).
> - **0078** — nexus junk + self-edge close and demonym/plural dyad canonicalize (reversible).
> - **0079** — `cross_correlator` stale-head sweep (reversible).
> - **0080** — state-media `source_credibility` seed + a cross-target mislabel close.
>
> The four **2026-08-27…30 migrations** need a word each, because two of them
> move live rows rather than only adding structure:
>
> - **0186** — `analyst_traces.prompt_sha256`. Additive column + index; no
>   backfill. See the `prompt_rendered` note in §11 (*Reconstruct a truncated
>   rendered prompt*) for why the hash matters operationally.
> - **0187** — `band_calibration_claims.semantics_migration` (boolean, default
>   false). Additive. Lets the calibration aggregation exclude
>   stamp-migration transitions **by query predicate** rather than by hiding
>   them; the excluded count is reported on the route as
>   `population.excluded_semantics_migration`.
> - **0188** — situation mega-frame split. **The heaviest migration in this
>   wave** (four statements, one idempotent transaction): re-stamps
>   `analyst_outputs.situation_signature` on derived-key findings inside a
>   120-day bound, re-stamps the matching `finding_supersessions` audit rows,
>   splits each OPEN mega-frame into one `situations` row per producing
>   dimension (parent id kept by the plurality-evidence dimension, the rest
>   carry `data.trajectory_parent_id`, `intensity_score` scaled by member
>   share), and re-bases `hypotheses.intensity_at_emit` by the same share,
>   preserving the original in `intensity_at_emit_pre_0188`. That last
>   statement is the guard that stops a re-scale from mass-refuting ~4,400
>   live hypotheses. **Run `EXPLAIN` on statement (1) before the deploy
>   window** — it is the largest UPDATE in the file and is narrowable from 120
>   days to 45 if it looks heavy. Forward code is safe on either side of 0188
>   (read-time normalization), so migrate-vs-recreate ordering is not
>   load-bearing here.
>   *Expected post-migration optics, not regressions:* open-frame population
>   grows roughly 4–6× (≈49 → ≈200–300), per-frame intensity falls ~8×, and
>   the first post-deploy tick emits a one-time burst of new thematic-proposal
>   slugs (self-limiting). Size the tracker budget accordingly — see
>   `LEGBA_SITUATION_TRACKER_MAX_SITUATIONS` in §4.0.5.
> - **0189** — `read_events`, the append-only read-telemetry ledger (§4.1.1).
>   Creates the table + four indexes and installs a trigger that makes
>   `DELETE` and `UPDATE` **fail loud**; `TRUNCATE` stays permitted for test
>   teardown and no app path uses it. No backfill — the ledger starts empty by
>   construction and the 90-day read clock starts at deploy.
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
| `LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS` | ActorProxy invoke round-trip budget (the trigger-engine → actor `run` call). Raised from 60→180 when the heaviest deterministic sweep was `cross_source_dedup` fanned out per target; as of 2026-08-02 that analyst is a singleton running ~0.85s, so the 180s is now headroom for the rest of the fleet rather than for dedup. The actor's own cooldown + trigger-window CAS dedup a late completion. | unset / malformed / ≤0 → falls back to **180s** (`source_first_runtime.actor_invoke_timeout_seconds`) |

### 4.0.1 Alerting + archive + retention keys (2026-07 wave)

Also set in the gitignored `.env`. These reach the containers via `env_file`
— most are NOT explicit compose `environment:` keys, so a `.env` edit +
`--force-recreate` is the activation path (no rebuild).

| Key | Purpose | Default behavior when unset |
|---|---|---|
| `LEGBA_ALERT_WEBHOOK_URL` | target URL for the generic webhook alert sink (`data/alerts/webhook_sink.py`) | unset → the sink declares itself unconfigured: attempts ledger as `skipped_unconfigured`, and the sink **drops out of fan-out entirely when a configured sibling sink exists** (the no-sinks-at-all case keeps writing the audited skip so the gap stays visible) |
| `LEGBA_ALERT_WEBHOOK_MIN_SEVERITY` | severity floor for the webhook sink | `high` |
| `LEGBA_ALERT_NTFY_URL` (+ `LEGBA_ALERT_NTFY_TOKEN`, `LEGBA_ALERT_NTFY_MIN_SEVERITY`) | the native ntfy push sink (`data/alerts/ntfy_sink.py` — `X-Title` / `X-Priority` / `X-Tags` / tap-to-open `X-Click` receipt link). Point it at the profile-gated local service, e.g. `http://ntfy/legba-alerts` | unset → unconfigured (same audited-skip semantics); min-severity defaults `high` |
| `LEGBA_ALERT_SINK_COOLDOWN_SECONDS` | per-sink cooldown in the `AlertSinkDispatcher`. Suppressed alerts are **coalesced, never dropped**: the next delivery carries "+N more alert(s) during cooldown" with a bounded preview | `60` |
| `LEGBA_PUBLIC_BASE_URL` | makes the receipt link on every outbound alert an absolute URL (`<base>/api/v1/lineage/…`) | unset → the payload carries the relative lineage path only (ntfy omits `X-Click`) |
| `LEGBA_ARCHIVE_ROOT` | filesystem root of the evidence-archive CAS store (`data/archive.py`) | `/var/lib/legba/archive` (the compose-mounted `legba_archive` volume) |
| `LEGBA_ANALYST_TRACES_TTL_DAYS` | TTL for the `analyst_traces_retention` purge handler (draft; mig 0101 adds its age-only purge-scan index; FK-safe — critiques cascade, DLQ rows null out). Keep the TTL **well above 7 days** (30+ recommended — the telemetry API aggregates a 7-day window over `analyst_traces`; documented guidance, not code-enforced) | `0` → **disabled** (ships inert; a positive value is the operator opt-in). Since X-1 (2026-07-29) the TTL can ALSO ride the descriptor as `method.options.ttl_days` — a `PUT /api/v1/descriptors/analyst/analyst_traces_retention` with no rebuild and no container recreate, which is now the preferred path. Resolution order: run options (which the descriptor block feeds) → this env var → the `retention_policies` row |
| `LEGBA_SIGNALS_RETENTION_TTL_DAYS` | TTL for the `signals_retention` purge handler (same env-fallback class as the traces TTL — SEAMS #43 RESOLVED). Purges aged signals + their `signal_entity_links` / `signal_aliases` children; `retain_always` / `evidence_hold` rows are NEVER purged regardless of age. **Deleting signals is a bigger call than telemetry — leave unset until deliberately decided**; deliberately not present in any shipped .env or descriptor | `0` → **disabled** (the shipped posture). Since X-1 also settable as `method.options.ttl_days` on the descriptor; the shipped descriptor carries no options block, so the default stands |

### 4.0.5 Alert-budget, register-budget + verify keys (2026-08-27…30 wave)

Same activation path as §4.0.1 — these reach the containers via `env_file`,
so a `.env` edit plus `--force-recreate` is enough; no rebuild. Every one of
them **also** has a descriptor `method.options` equivalent (the X-1 path,
§4.0.3), which is the preferred lever because it needs no container recreate
at all. Resolution order is the X-1 order: run options → env var → code
default.

| Key | Purpose | Default behavior when unset |
|---|---|---|
| `LEGBA_ALERT_DAILY_PAGE_BUDGET` | Fleet-wide cap on how many alerts actually PAGE per UTC day (descriptor option `daily_page_budget`). Survivors rank worst-first by severity then magnitude tier; everything over budget still writes its row tagged `data.budget_deferred=true` — **never a silent drop** | `5`. A malformed or negative value falls back to 5; `0` is honoured and means "page nothing" |
| `LEGBA_ALERT_BUDGET_PER_KIND_CAP` | Max slots any single `trigger_class` may take out of that daily budget, **day-cumulative across scans** (descriptor option `budget_per_kind_cap`). Exists because one always-critical class (`situation_escalation` is 100% `severity=critical`) would otherwise win every slot every day; a slot no other kind can fill stays **unused** rather than backfilled with more of the capped kind | `3` |
| `LEGBA_ALERT_CONTENTION_FLIP_ENABLED` / `LEGBA_ALERT_GEO_CONVERGENCE_ENABLED` | The **kill list** (descriptor options `contention_flip_enabled` / `geo_convergence_enabled`). Both classes ship **OFF**. The scans still run and their watermarks still advance *as if fired*, so re-enabling is a config flip and not a backlog replay; the would-have-fired count rides the run receipt | OFF (killed). Replay basis: these two classes were 646 pages / 5 days |
| *(descriptor-only)* `suppress_steady_state` / `steady_cooldown_hours` | The steady-state guard on `verified_finding` alerts: suppress only when the desk's banded severity is unchanged AND the finding's `severity_delta` reads `steady`/absent AND the desk was paged within the cooldown. A `rose`/`fell`/`new` tag ALWAYS pages. Suppressed candidates write their row tagged `suppressed:true` | guard ON, cooldown `24` hours. **Fails toward paging**: no prior desk record → page; unparseable timestamp → page |
| `LEGBA_SITUATION_TRACKER_MAX_SITUATIONS` | Per-tick selection budget for the situation tracker (descriptor option on `analyst_situation_tracker`; ceiling 500). **Set this at the 0188 deploy** — the mega-frame split multiplies the open-frame population 4–6×, and leaving the budget at 12 buys under one full pass per day. Sizing (hourly cadence, 24 ticks/day, declared 2M token/day budget): `24` → ~2–3 passes/day at ~52% of budget; `36` → ~3–4 passes at ~79%; `48` → ~4–6 passes but **~105% of budget** (needs `budget_tokens_per_day` raised to ~2.5M first). **24–36 is the affordable middle** | `12` (the shipped descriptor value; env currently unset). Env beats descriptor beats default |
| `LEGBA_JUDGE_FLOOR_ESCALATION` | Kill switch for floor-triggered judging — an unjudged finding about to be excluded by the composition verify floor is sent to the judge first, so the floor may only exclude on **judged** evidence | **unset = ON.** Set `=0` to disable; no rebuild. Expect ~+43% judge-plane volume (~136 → ~195 calls/day) and a fleet-mean faithfulness that moves UP — a selection change, not a quality change (partition on the new `judge_trigger` marker across the deploy date) |
| `LEGBA_COMPOSITION_VERIFY_FLOOR` | Pre-existing. Deliberately **reused** as the floor-escalation threshold rather than duplicated, so the exclusion bar and the escalation bar cannot drift apart | `0.50` |
| `LEGBA_SOURCE_HONEST_QUIET_STREAK_THRESHOLD` | Streak length past which an `honest_quiet` poll outcome escalates anyway, tagged `honest_quiet_prolonged`. The exemption protects genuinely low-cadence feeds; this bound catches a feed that is permanently dead but still polling cleanly (the motivating case ran 110 clean empty polls over 9 days, state `active`, no alert). Sizes the watchdog's own poll-history fetch window: `max(20, empty_streak+5, honest_quiet_streak+5)` | `36` (→ a 41-row fetch window). **Expect a burst of `honest_quiet_prolonged` escalations on the first post-deploy cycle** for the sources currently pinned at the old 20-row window — intended, not a regression |

### 4.0.2 Default-OFF / opt-in flag audit (C5-3, 2026-07-28)

Every `LEGBA_*` env var in `src/legba` whose code default is OFF (or that
otherwise gates a feature on an explicit opt-in), audited during the
2026-07-28 registry-hygiene pass. "Code default" is what a **fresh
install with no `.env` overrides** gets — this repo's `.env.example` and
an operator's actual `.env` may set some of these ON; where that's
known it's called out. Retention-TTL flags (`LEGBA_ANALYST_TRACES_TTL_DAYS`,
`LEGBA_SIGNALS_RETENTION_TTL_DAYS`) are documented in full in §4.0.1 above
and aren't repeated here.

| Flag | Code default | Purpose | Decision |
|---|---|---|---|
| `LEGBA_A2A_ENABLED` (+ `LEGBA_A2A_TRUSTED_KEYS`) | OFF | Mounts the inter-agent `/a2a/skills` surface | **KEEP.** Declared seam #15 (fail-closed B-2 posture) — exposing an unauthenticated skill surface must stay an explicit operator decision. |
| `LEGBA_AGE_DERIVED_FROM` | OFF | Opt-in Apache AGE `:DerivedFrom` graph-edge mirror alongside the relational `derived_from[]` lineage array | **KEEP** (investigated; not deleted). Zero code paths anywhere in `src/legba` or `legba-ui-v3` read the AGE `:DerivedFrom` edges — the relational recursive-CTE lineage is the sole consumer. That is NOT an oversight: `docs/ARCHITECTURE.md` §5.5's 2026-06-23 AGE re-evaluation already decided to keep the whole AGE graph "retained but dormant" (an optional acceleration path, not depended on), with a documented revisit trigger (~250k nexus edges or ~2s p95 traversal latency). That same writeup recommends *eventually* dropping AGE outright, but explicitly as a separate **operator-gated** change spanning more than this one flag (also `fact_extractor.py`'s `emit_graph_edges`) — out of scope for a single-flag decision here. |
| `LEGBA_COMPOSITION_TIERED_EVIDENCE` | OFF | Two-tier (verified basis + labeled periphery) composition evidence split (SEAMS #44/#45, resolved) | **KEEP.** ON in the operator's live `.env`; the code default stays OFF so a fresh install / test run gets byte-identical legacy behavior until the operator opts in deliberately. |
| `LEGBA_FACT_DECAY_WEIGHTING` | OFF | Decay-weighted fact evaluation in the eval/calibration paths | **KEEP.** Same rationale as above — ON live, OFF by code default for fresh installs. |
| `LEGBA_FACT_CONTENTION_LLM_TIEBREAK` | OFF | Bounded self-hosted-vLLM call to break a NEAR-TIE abstain in `fact_contention_arbiter` (never Anthropic/Opus) | **KEEP.** Opt-in per design (`fact_contention_arbiter.py`); the operator's `.env` has it. OFF-safe: with it off the arbiter is byte-for-byte the deterministic Wave-2 handler. |
| `LEGBA_FACT_CONTENTION` | OFF (code); **ON is the live default** per `manual_batch.py`'s own docstring | The write-path COEXISTENCE carve-out: a same-tier, fuzzy-distinct prior is NOT closed on a new write, so both stay open for the detect-only arbiter to surface as a contention rather than one silently overwriting the other | **KEEP.** Load-bearing prerequisite for the two contention flags above — turning it off collapses the contested-claims arbiter's input (nothing would ever coexist to arbitrate). |
| `LEGBA_CONTENTION_SURFACING_PREFER` | OFF | Additionally REORDERS grounding to prefer disputed facts (stronger than just annotating them) | **KEEP** — deliberate second gate. The sibling `LEGBA_CONTENTION_SURFACING` (default ON) only annotates a disputed fact CONTESTED/DISPUTED in the grounding preamble; this flag changes WHAT an analyst actually reads, which the code's own docstring says "never ships silently" — correctly kept a separate, harder opt-in. |
| `LEGBA_DEV_MODE` | OFF | Bypasses the registry-API bearer-token requirement and the A2A trusted-keys allowlist requirement | **KEEP, never flip.** This is the one flag in this table where flipping the default would be a straight security regression — the B-2 fail-closed posture depends on it staying an explicit, visible opt-in for local/dev bring-up only. |
| `LEGBA_LEADER_ELECTION` | OFF (single-node posture) | Enables the Postgres advisory-lock leader election for multi-replica safety (seam #17) | **KEEP.** Correct default for the current single-replica deployment; flipping to always-on would add advisory-lock overhead with no benefit until a real multi-replica topology exists (a direction item, not decided here). |
| `LEGBA_PROXY_DEEP_HEALTHCHECK_ENABLED` | OFF | Bright Data residential-proxy deep healthcheck (one `ipinfo.io/json` call through the proxy) | **KEEP.** Explicitly cost-gated in the code comment — Bright Data bills per byte even for a tiny probe; enabling by default would silently add operator cost. |
| `LEGBA_REGISTRY_HEALTH_LOOP` | OFF | Background stack-component health-poll loop in the registry server | **KEEP, but flag for operator attention.** The `False` default's stated rationale (`server.py:create_app` docstring) is test-isolation — "so unit/integration tests don't spin a probe thread unless they ask for it" — but the production CLI entrypoint (`server.py:main`) inherits that SAME default via a bare `os.getenv(..., "")`, and it isn't in `.env.example` at all. The test rationale doesn't obviously apply to the production container. Not flipped here (a production-behavior default shouldn't change without the operator seeing it) — recommend the operator confirm whether this loop is wanted live and either set it or split the CLI default from the test default. |
| `LEGBA_REIFY_DISCOVERED_CHAINS` | OFF | Lets `graph_mining` emit a NEW `nexus` row for a discovered (no-direct-edge) negative-polarity proxy chain, not just score existing edges | **KEEP.** This WRITES an inferred (not directly observed) relationship into the substrate; the code's own docstring calls it "Operator-gated" by design — the inference-noise risk belongs behind an explicit opt-in, same reasoning as the AGE-edge and structural-verify-gate flags below. |
| `LEGBA_OPTIMIZER_DAPR_WORKFLOW` | OFF | Opt-in gate to attempt the durable Dapr-Workflow client for the GEPA optimizer's compile dispatch | **KEEP.** Unset (the fresh-install default) always uses the in-process GEPA fallback, which has zero extra infra dependencies (no `dapr.ext.workflow`, no worker container required) — the safer default for portability. SEAMS #23 proved the durable round-trip *works* for this optimizer, but making it the ambient default is a topology decision (needs the worker image + sidecar actually healthy) better left to the operator, especially since the optimizer's own cadence stays frozen (seam #30) so this path barely fires in production today anyway. |
| `LEGBA_OPTIMIZER_IN_PROCESS` | OFF | Forces the in-process GEPA fallback even when a Dapr Workflow client could be built | **KEEP** — the documented escape hatch for when the durable path misbehaves; a no-op unless `LEGBA_OPTIMIZER_DAPR_WORKFLOW` was also set. |
| `LEGBA_STRUCTURAL_VERIFY_GATE` | OFF | Lets a structural-claims critique's score actually DEMOTE `effective_confidence` (vs. compute-and-show only) | **KEEP.** `docs/STATUS.md` already documents this as the deliberate "compute-and-show first, gate later" posture (C2b) — the same incremental-rollout pattern used for the tiered-evidence and decay-weighting flags above, just not yet graduated. |
| `LEGBA_VERIFY_LLM_JUDGE` | OFF (code); **`.env.example` ships it `=1`** | Enables the optional LLM judge inside the faithfulness verify pass (deterministic citation-presence floor runs regardless) | **KEEP.** Core to the product's verify/judge system — the code default only protects a bare install / unit test from needing a judge LLM wired; the shipped template turns it on. |
| `LEGBA_WORLD_CONTEXT_DISABLED_UNITS` | empty (nothing disabled) | Per-unit kill-switch list for the Tier-2 `vector:world_context` grounding pilot (SEAMS #20); read alongside the persisted auto-rollback state file | **KEEP.** The whole point is a cheap manual override sitting beside the automatic per-run rollback guard while the pilot is still single-unit and unproven — removing it would remove the operator's manual lever exactly while the pilot is least mature. |

**Excluded from the table (reserved name, not yet a working flag):**
`LEGBA_REMINDER_GC_SCHEDULER_SCAN` appears only in a docstring in
`src/legba/runtime/reminder_gc.py` — no `os.getenv` call reads it yet. It's
the reserved name for SEAMS #16 part 2 (scheduler-side etcd reminder scan),
which is not built. Nothing to flip or delete because nothing runs on it.

**Noted for completeness, opposite polarity (default-ON / opt-out — not this
audit's "default-OFF/opt-in" scope, no action taken):** `LEGBA_CONTENTION_SURFACING`
(disputed-fact annotation in grounding, on unless explicitly turned off),
`LEGBA_DEPS_FALLBACK_ENABLED` (reconcile-loop deps-fallback resolver after a
restart), `LEGBA_INTRASOURCE_DEDUP` (exact-hash intra-source collapse before
ingest), `LEGBA_EMBED_WORKFLOW_WORKER` (embedded-vs-external GEPA workflow
worker topology). Each already defaults to the safe/expected behavior for a
standard single-process deployment.

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
| `GET /api/v1/source_credibility[/{host}]` | host-credibility reads — **deprecated**, superseded by `/api/v1/v3/source-quality` (still serving; `Sunset` header carries the date) |
| `PUT /api/v1/source_credibility/{host}` | upsert single host |
| `POST /api/v1/source_credibility/bulk` (CSV) | bulk import |
| `GET/WS /api/v1/registry/events?filter=<NATS subject>` | live multiplexer (`descriptor.>`, `stack.>`, `legba.dlq.>`, `analyst.<id>.>`, etc.) |

The 2026-07-28 wave added a further `/api/v1/v3` read family — `since` /
`timeline` / `export` / `narratives`(+`/echo`) / `eval/goldset/*` /
`eval/desk_baselines` / `eval/band_trajectory` / `sources/{id}/assurance` /
`watchlist` (the family's first write surface) — and freshness grades on
`system/source-firing`. Since then: `source-quality` (+
`sources/{id}/quality`) — the merged source-quality ledger that supersedes
`sources/{id}/assurance` and the `source_credibility` reads — and
`system/staleness-debt`. The per-route table is `ARCHITECTURE.md` §8.7.

The 2026-08-29/30 wave adds two more, both bearer-gated like the rest:

| Path | Purpose |
|---|---|
| `POST /api/v1/read-events` · `GET /api/v1/read-events/rollup?days=N` | the read-telemetry ledger — see §4.1.1 |
| `GET /api/v1/v3/system/external-audit?days=N` | the standing external auditor's heartbeat + verdict board — see §4.1.2 |

All gated by `Authorization: Bearer <LEGBA_REGISTRY_API_TOKEN>` —
fail-closed (503) when the token is unset, unless `LEGBA_DEV_MODE=1` (§4).

### 4.1.1 Read telemetry — `read_events` (migration 0189)

The platform receipts ~80 write tables and, until this wave, receipted no
**read** at all. `read_events` is an append-only attention ledger; the console
emits at seven chokepoint surfaces (not call sites), batched behind a bounded
queue and flushed on tab close.

- `POST /api/v1/read-events` — appends a batch, **202 Accepted**, body
  `{accepted, rejected, reasons}`. Per-event validation: a malformed event is
  dropped and counted (deduped reasons), never a batch-wide 422 — one bad
  event must not lose 199 good ones. An out-of-vocabulary `event_kind` is the
  one hard 422. One `executemany` INSERT per batch, no joins at write time.
- `GET /api/v1/read-events/rollup?days=N` — daily cells by kind plus the
  headline scalars. `days` defaults to 30 and is bounded `[1, 365]` (422
  outside). The grouping happens in Postgres; the route never streams the raw
  log to a caller.
- **Closed vocabulary, enforced by a CHECK constraint** (seven values):
  `panel_open`, `workspace_open`, `finding_open`, `lineage_walk`,
  `citation_drill`, `consult_open`, `brief_read`. Adding a kind is a
  migration, deliberately — an open text column lets a typo in a UI emitter
  silently create a kind no rollup counts.
- **Append-only at the database**: a trigger makes `DELETE` and `UPDATE` fail
  loud. `TRUNCATE` is still permitted (test teardown only; no app path uses
  it).
- Load bound: ≤1 POST per 4s per tab, ≤200 events per batch. A failed batch
  is **dropped, not retried** — a retry would backdate a week of reading into
  one minute and corrupt the very number this table exists to produce.
- **No retention policy ships**, deliberately: the 90-day read clock wants
  the whole log. Adding retention later is a forward-only migration.
- Operator grading (SELECT-only, needs no panel): count distinct days with a
  `brief_read` over a trailing 90-day window, beside `active_days`,
  `lineage_walk`, `citation_drill` and `finding_open` counts. **Watch the
  bias**: `panel_open` is by far the highest-volume kind, so a naive "reads
  total" looks healthy even when the morning read is never opened. The
  scoreboard panel discloses this in-surface and the grading query never uses
  `panel_open` as a headline.
- This is a schema-adjacent change: **rebuild the registry image first, wait
  healthy, then the runtime** (the standing deploy-race rule).

### 4.1.2 Standing external auditor + its heartbeat

`standing_auditor` is a deterministic META analyst (daily, `12 5 * * *`, 22h
cooldown) that samples the world read plus a rotating subset of desk reads,
extracts 1–2 checkable world-claims per head, checks each against live
external search **through the `web_access` action pack** (never ad-hoc HTTP),
and writes a verdict per claim: `SUPPORTED` / `CONTRADICTED` / `NOT_FOUND` /
`UNCHECKED`. A `CONTRADICTED` verdict on a `high`/`critical` claim writes a
`kind='alert'` row (no outward page). Registered `TRACE_ONLY` — the real
product is side-written critique / alert / heartbeat rows.

**Bring-up order**

1. **Prereqs.** The `web_access` pack must be registered with a `web_search`
   provider bound (`scripts/bringup_register_action_packs.py`), and migration
   0091 applied (fleet-wide already — the auditor reuses
   `alert_trigger_watermarks` under `trigger_class='external_audit'`, no new
   migration). With either missing the auditor still runs and files a loudly
   **unaudited** heartbeat naming the gap.
2. **Registry image first**, wait healthy, then the runtime — the new
   sub-handler and the new route both ship in the registry image.
3. Register create-only:
   `PYTHONPATH=src python3 scripts/bringup_register_standing_auditor.py`.
4. **It ships `state: draft`.** Activation is an explicit operator decision —
   PUT the descriptor with `identity.state=active` (the "Update a descriptor"
   recipe in §11), then recreate the runtime to spin the actor.
5. First-run check:
   ```
   curl -sS -H "Authorization: Bearer $LEGBA_REGISTRY_API_TOKEN" \
     "http://127.0.0.1:8090/api/v1/v3/system/external-audit?days=7" | jq '.heartbeat'
   ```
   `present=true, degraded=false, claims_checked>0` is a working auditor;
   otherwise `degraded_reason` names the gap.

**Operating notes**

- **Every run writes a heartbeat, including a run that audits nothing** — so
  "nothing to contradict" and "the auditor is dead" can never look alike from
  outside. The route reports `stale` past `stale_after_hours=30`.
- **Check the heartbeat, not `analyst_traces.status`.** The handler
  deliberately sets `status='success'` even when degraded (a degraded run IS
  a completed run), and the liveness watchdog reads that column — so cadence
  health alone cannot tell you the auditor stopped auditing.
- **Both planes or neither**: with no search binding the handler refuses to
  spend a core-plane call at all. Missing DB access **raises**; every other
  gap **degrades** with the reason named.
- `contradiction_rate` is computed over *checked* claims and is **absent, not
  `0.0`**, when nothing was checked. The route never 500s at a polling panel.
- Scope is descriptor-tunable with no code change and no rebuild:
  `window_hours` (48), `max_desks` (3), `max_claims_per_head` (2),
  `max_claims_total` (6), `search_limit` (5). Worst case per day: ≤4 heads
  read, ≤10 core-plane calls, ≤6 external searches (inside the pack
  governor's 120/h · 20/min), well within the descriptor's 120k token/day
  budget.
- Its verdicts carry their own `EXTERNAL_AUDIT_PIPELINE_VERSION`, a
  deliberately separate population key from `JUDGE_PIPELINE_VERSION` — the
  two instruments must never pool.
- Once alerting's daily budget is live, give `trigger_class='external_audit'`
  a slot in the kind vocabulary (§4.0.5) — the rows already carry the class.

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

### 4.0.3 Handler thresholds without a rebuild (`method.options`, X-1)

Every deterministic handler reads its thresholds out of the run `options`
mapping (`options.get("per_desk_cap", DEFAULT_PER_DESK_CAP)` and ~60
siblings). Until 2026-07-29 nothing fed descriptor values into that mapping —
`MethodBlock` had no `options` field and the runtime built `options` from
scratch at fire time — so all of them were **dead config**: the in-source
default always won, and several descriptors documented knobs an operator
could not actually move. X-1 connected the channel.

**How to set one.** Add a `method.options` block to the analyst descriptor:

```yaml
method:
  kind: deterministic
  impl: legba.data.analysts.deterministic:run_method
  sub_handler: alert_trigger_scan
  options:
    per_desk_cap: 5
    baseline_sigma: 2.5
```

Live-editable with no code edit, no schema change and no image rebuild —
the runtime reads the descriptor from its **registry DB row**, not the YAML:

```bash
curl -sS -X PUT "$REG/api/v1/descriptors/analyst/alert_trigger_scan" \
  -H "Authorization: Bearer $LEGBA_REGISTRY_API_TOKEN" \
  -H 'Content-Type: application/json' -d @body.json
```

**Precedence** (highest first): runtime-stamped provenance → an explicit
forced-run `payload.options` → `method.options` → the handler's own default.
An ad-hoc force always beats the standing descriptor config.

**Which knobs exist:** `src/legba/data/analysts/handler_options.py` —
`HANDLER_OPTIONS` maps each sub-handler to its declared knobs with types and
ranges. It carries no default VALUES on purpose: the handler's own
`options.get(key, DEFAULT)` stays the single source of truth, so a descriptor
with no options block is byte-identical to the pre-X-1 behavior.

**Failure mode is loud, never fatal.** An undeclared key, a key the runtime
owns (`analyst_id`, `run_id`, `target_id`, `sub_handler`, …), or a value
outside its range is **dropped** — the handler default stands — with a
`handler_options.rejected` WARNING and a durable receipt entry. Read it back:

```sql
SELECT analyst_id, run_started_at, step
FROM analyst_traces, LATERAL jsonb_array_elements(intermediate_steps) AS step
WHERE step->>'phase' = 'handler_options'
ORDER BY run_started_at DESC LIMIT 20;
```

`status` is `applied` or `degraded`; `applied` lists the knobs that took
effect and `rejected` lists every dropped key with its cause. Registration
does NOT refuse an unknown key (it warns): registry rows outlive code, so a
knob renamed in a later release must not brick activation for descriptors
still carrying the old name.

**Scope.** `kind: deterministic` only — an options block on any other kind is
refused at registration rather than sitting silently inert. The LLM kinds'
tunables (e.g. `LEGBA_COMPOSITION_VERIFY_FLOOR`) stay env-var-driven.

### 4.0.4 Escalation action selection (`action_tool`)

The escalation edge — the one threshold→action rule that runs in production —
invoked a hardcoded `escalate`. It now reads the tool name from the same
`escalate` tool config the gates come from, in
`descriptors/action_pack_escalate.yaml`:

```yaml
tools:
  - name: escalate
    config:
      confidence_gate: 0.85
      action_tool: escalate      # unset → escalate (unchanged behavior)
```

Validated against the pack's live tool list when the binding is built. To
route crossings at a different action, add that tool to the pack's `tools:`
and name it here — same `PUT /api/v1/descriptors/action_pack/escalate_finding`,
no rebuild. An `action_tool` naming no tool on the pack **degrades loudly** to
`escalate` (ERROR log + a `config_note` on every
`alert_sink_deliveries.payload_summary` row) rather than taking the escalation
edge offline over a typo:

```sql
SELECT attempted_at, payload_summary->>'action', payload_summary->>'config_note'
FROM alert_sink_deliveries
WHERE payload_summary ? 'config_note' ORDER BY attempted_at DESC LIMIT 10;
```

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
# the live 8-unit spine + composition tower; use deploy.sh (bringup_register_analysts.py)
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
the LIVE spine: the nine bounded units + the composition tower + the I&W pair via
the split registrars). The frozen `p17_workingset` one-pass path below registers only
the RETIRED `country_assessor` monolith set and is kept for dependency-ordering
reference; run the deterministic analysts + the daily
budget envelope. Pin `LEGBA_DATA_PG_DB=legba` (the working-set registrar
defaults to the `legba_pivot_test` DB). Run AFTER §6 vault + the stack above, and (per §0)
with the runtime already up or about to be `--force-recreate`d so it builds its
clients against the now-seeded stack:

```
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_p17_workingset.py        # LEGACY: packs + sources + 19 G20 targets + 4 RETIRED-monolith analysts (prefer deploy.sh for the live 8-unit spine)
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
the **base** catalog. The 46 are no longer the whole picture: the breadth
batches layered on top of them (the RSSHub lane, the Wave-A batch, the
supply-chain batch — `DATA_SOURCES.md` §2.5–§2.7) each ship `draft` and are
activated deliberately, which is how this deployment reached its current active
count. That count is generated, never hand-typed here — see
`docs/RELEASE_STATE.md`. Idempotent; pin `LEGBA_DATA_PG_DB=legba` (defaults to
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

### Activate a thematic desk pack (the supply-chain lanes/flows)

A thematic pack is a set of non-country desks plus one tag-scoped bounded unit —
registered together, activated **one desk at a time**, because the whole point of
a bounded unit is that it should not be pointed at a desk whose signal slice
cannot support it. Order matters; do not skip the preflight.

1. **Register (idempotent).** `python scripts/bringup_register_supply_chain_pack.py`
   registers the 10 desks (`lane_*` / `flow_*`) and the `disruption_status` unit,
   all at `state: draft` — nothing fans out yet. Needs
   `scripts/_p17_registrar.py` alongside it. The registrar runs its own gate
   **before touching the DB**: a bounded `inline_target` unit missing an
   `eval.rubric` or a `method.llm.verify` block is refused outright, so an
   unmeasurable or unverified unit cannot be registered by accident.
   Sources: `python scripts/bringup_register_supply_chain_sources.py`
   (7 `rss` descriptors + host credibility seeds; `DATA_SOURCES.md` §2.7).
2. **Preflight — read-only, and it is the activation gate.**
   `python scripts/preflight_supply_chain_lanes.py` SELECTs only. Per candidate
   desk it reports rows available vs. the fetch cap, per-source concentration,
   and the slice the unit would actually receive. Activate a desk only when
   **`lost == 0`** (the cap is not silently truncating its slice); treat one
   source supplying more than ~30% of a desk's rows as a single-source-dependency
   flag, not a pass. It also asserts no desk carries `US` in `scope.geo` — a
   chokepoint desk that quietly widens into a country desk would double-count
   against the country plane.
3. **Flip the desks, then the unit** (`draft → configured → active`, the normal
   descriptor PUT). Desks first: activating the unit while every desk is still
   draft gives it an empty predicate match and a pointless run.
4. **Force one run per desk and read it.** Require ≥3 `[N]` citation markers and
   a non-empty `data.indicators[]` on each finding, and check the paired
   faithfulness critique cleared the floor. A unit that produces prose with no
   indicators is not wired — it is talking.
5. **Watch before widening.** Let the active desks run a cycle before flipping
   the next one. Adding all ten at once buys ten unmeasured desks, not ten desks
   of coverage.

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

### Reconstruct a truncated rendered prompt (the 32k trace cap)

**Know this before you plan an audit around `prompt_rendered`.**
`analyst_traces.prompt_rendered` is capped at
`_MAX_PROMPT_RENDERED_CHARS = 32_000` (`src/legba/data/run_accounting.py`).
For a desk with a verbose system prompt, the static prompt plus the
authoritative-context and situation-register blocks can consume the entire
32,000 characters **before a single numbered signal is stored** — so on those
desks the persisted trace *cannot* show which evidence the desk actually
read. A truncation is never silent (the stored value carries an explicit
marker naming the full length), but the practical consequence is real: an
instruction of the form "grep `prompt_rendered` for country X" is not
executable on the desks where it matters most.

Two things make the gap workable today:

- **`prompt_sha256` is always computed over the FULL, untruncated text**
  (its own column since migration 0186 — deliberately not folded into the
  receipt hash). So a capped row is still byte-verifiable against a
  re-rendered prompt: re-render with `scripts/render_prompt_pack.py` and
  compare hashes. If they match, the re-render IS the prompt, and you can
  read the part the column lost.
- **`analyst_traces.input_row_refs`** is the reachability answer, and it is
  not truncated. It is a `uuid[]` of the substrate rows the run consumed, so
  to prove *whether a desk was shown a given story*, intersect it against
  `signals` rather than grepping the prompt text:

  ```sql
  SELECT s.id, s.title, s.published_at
    FROM analyst_traces t
    JOIN signals s ON s.id = ANY (t.input_row_refs)
   WHERE t.run_id = '<run-id>'
     AND s.title ILIKE '%<term>%';
  ```

  This is the fallback that a 2026-08 blindness diagnosis had to use, and it
  answered the question the prompt grep could not. Note the direction of the
  evidence: `input_row_refs` proves the row was **in the slice**, not that it
  survived into the rendered prompt — for that you need the hash-verified
  re-render above.

Raising the cap (or storing the numbered-signal block in its own column) is a
known open item, deliberately not done in the 2026-08 wave: the row-bloat
argument that set the cap has not been re-measured.

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

   The SPA's own credential rides the `legba.bearer.v1` **subprotocol**
   (`Sec-WebSocket-Protocol: legba.bearer.v1, <base64url token>`) — the one
   custom header a browser CAN set on an upgrade — not `?token=`. The
   query-param path still authenticates during the rollout window and logs
   `registry.ws.auth.deprecated_query_token`; grep for that line to know when
   it is safe to delete. Manual probes (`wscat` below) can keep using
   `?token=` or an `Authorization: Bearer` header.

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
| 1 | Strict test suite | `LEGBA_TEST_STRICT=1 scripts/run_tests_in_container.sh` | Green; INFRA-gated skips ESCALATE to failures (no silent coverage loss). Known pre-existing infra failures (port-6090, daprd/webhook/agency/critic e2e) are documented exceptions — the authoritative, node-id-level list is now `KNOWN_FAILURES` in `scripts/host_nightly_suite.sh` (§24.4), which is measured rather than remembered. |
| 2 | No-stub gate | `git grep` for stub/mock markers in `src/**` | Zero hits. A genuinely-deferred item is a fail-loud declared SEAM in `docs/SEAMS.md`, never a silent stub (`tests/test_no_undeclared_stubs.py` is the mechanical enforcer). |
| 3 | Descriptor validation | `validate_descriptors.py` (or YAML-parse fallback) | Every descriptor parses + type-checks. |
| 4 | UI build (tsc gate) | `docker compose --profile ui build legba-ui-build` | The container build IS the type-check — a tsc error fails the build. |
| 5 | Secret/codename scan | `scripts/prepush_scan.sh` | See §20. Exits non-zero on any tracked-content / identity hit. |
| 6 | Release manifest | `scripts/make_release_manifest.sh` | Writes `release/manifest-<gitsha>.txt` (image digests + pip freeze + UI lockfile hash + migration baseline + smoke commands). Fold-in keeps it from going stale vs the tag. |
| 7 | Deployed-stack smoke | `scripts/release_smoke.sh` | 401/403/200 bearer pattern, migration-ledger non-empty, caddy edge serving. Optional (`SKIP_SMOKE=1` when no stack is up). |

**Manifest reproducibility.** Compose still carries some third-party images on
floating tags (`qdrant:latest` is pinned; `busybox:latest`, `caddy:2-alpine`,
`redis:7-alpine` and friends are not) to keep dev velocity. The manifest
freezes the exact answer at release-tag time: resolved sha256 digests
(`docker image inspect`), the real installed `pip freeze` from the built
runtime image, the UI `package-lock.json` hash, and the migration baseline.
Re-pinning compose to digests is optional for those — recording them in the
manifest is the required step.

**The one exception is the substrate.** `postgres` is pinned **by digest**
(`apache/age@sha256:4241e2d8…`), not by tag, because that image holds every
row of truth in the system: a silent `docker compose pull` swapping the
storage engine underneath 2 GB of facts, entities and provenance is not a
dev-velocity trade, it is an unreviewed migration. Rolling it forward is a
deliberate act — re-read the digest with
`docker buildx imagetools inspect apache/age:latest`, bump the line, and
re-run `scripts/age_probe/run_probe.sh` so the engine's behaviour is
re-measured *before* it reaches the data (see `docs/AGE_PROBE_REPORT.md`).

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

**Changelog step (required before the squash):** draft the release's
`CHANGELOG.md` entry FIRST — public history is squashed per release, so the
changelog is the only public release record, and writing the entry forces the
"what exactly is in this push?" review (public-docs vocabulary only: no hosts,
no codenames, nothing not already public). The entry rides inside the release
commit.

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

**Verifying a deploy while the watchdog is disabled — path-greps lie.** The
app images install the `legba` package into **site-packages**; there is no
`/app/src` copy of the code in a built image. Grepping a repo-style path
inside a container therefore proves nothing about what the image actually
runs. Verify a rebuilt image carries a change by **python-import**, e.g.:

```
docker exec legba-legba-runtime-dapr-1 python - <<'EOF'
import inspect, legba.data.alerts.sinks as m
print(m.__file__)                       # …/site-packages/legba/…
print("suppressed_in_cooldown" in inspect.getsource(m))   # the marker you shipped
EOF
```

## 24.1 LLM / search plane heartbeats

§24's watchdog watches the **actor plane** via signal freshness — it is blind
to a class of failure where signals keep flowing but one specific downstream
plane has quietly died, because deterministic ingestion keeps running through
either outage. Two small, alert-only host-side scripts close that:

- **`scripts/host_llm_heartbeat.sh`** — the LLM-plane silence alarm. Checks
  the newest **successful** run across every LLM-bearing analyst (deterministic
  analysts excluded by name); pages when none has succeeded within
  `MAX_LLM_AGE_SECS` (default 90 min). Closes the gap a 9h core-model outage
  (vLLM crashed, supervisord still reported RUNNING) left invisible to every
  signal-freshness check.
- **`scripts/host_search_canary.sh`** — the SEARCH-plane liveness canary
  (`docs/SEAMS.md` #50, now closed). The control probe that measures whether the
  `search_provider` engine set is actually answering
  (`verify_engine_liveness`, `src/legba/data/stack/search/liveness.py`) exists
  and fires reactively on an empty `web_search` call, but had no SCHEDULED
  half — nothing refreshed the verdict on its own clock. This script is that
  half: it docker-execs into the runtime container, resolves the registered
  search component the same way the runtime does, and forces a fresh probe.
  Pages only after **two consecutive** not-live probes (a persisted streak,
  not a single blip), rate-limited by the same cooldown-stamp pattern as the
  other watchdogs.

Both scripts: never restart anything (the fix is elsewhere — a remote model
host, or the local SearXNG/search-component config), respect
`/etc/legba-watchdog.disabled`, log to `/var/log/legba-watchdog.log`, and page
via the local ntfy **web UI** topic only (never the phone app).

> **2026-08-27 fix — the LLM heartbeat had never once succeeded.** Its
> completion / long-context probes defaulted `APP_CONTAINER` to the
> **registry** container, which is deliberately the lighter control-plane
> image and does not carry the runtime's parsing deps. The probe's own
> `ModuleNotFoundError` was raised outside any `try`/`except`, so `main()`
> died before printing anything the shell could classify — and the shell then
> reported *"container unreachable?"*, which was false every single time: the
> container was healthy throughout. **4,331 false fires since 2026-08-04,
> zero successful runs ever.** Three changes: `APP_CONTAINER` now defaults to
> the runtime-dapr container (the actual actor host, and the one the search
> canary already exec'd into); the probe body wraps its **own** imports, so
> an import failure prints a classified
> `FAIL … reason=probe_broken:<Error>` line instead of vanishing; and the
> bash side checks the container's real running state before concluding
> anything, so **`probe_broken` (the probe crashed) and
> `container_unreachable` (the container really is down) stay distinct
> verdicts**. If you see either word in the log, they now mean what they say.
> No `/etc` change was needed — both cron entries invoke the scripts from
> this checkout's live path, so the fix activates on the next tick.
>
> The wrong-container class was swept and does not repeat: the stall watchdog
> only runs `psql`, the search canary already targeted the right container,
> and the GPU-host vLLM watchdogs only `curl`.

**Cron — all three host watchdogs share one file.** Neither heartbeat script
installs its own line; they live beside the §24 stall watchdog in
`/etc/cron.d/legba-watchdog`, on deliberately different intervals so three
`docker exec`s never land on the same minute:

```cron
*/5  * * * * root /usr/local/deployments/active/legba/scripts/host_stall_watchdog.sh >> /var/log/legba_host_watchdog.log 2>&1
*/10 * * * * root /usr/local/deployments/active/legba/scripts/host_llm_heartbeat.sh
*/15 * * * * root /usr/local/deployments/active/legba/scripts/host_search_canary.sh >/dev/null 2>&1
```

**Retired: `scripts/loop_healthcheck.sh` (S-3, 2026-08-02).** A fourth watchdog
used to exist, scheduled from the ROOT CRONTAB rather than the file above:

```cron
*/10 * * * * bash scripts/loop_healthcheck.sh >> /var/log/legba_loop_health.log 2>&1
```

That line has a **relative path and no `cd`**. Cron runs it from `$HOME`
(`/root`), where `scripts/` does not exist, so every invocation since the
2026-06-09 install died with `bash: scripts/loop_healthcheck.sh: No such file
or directory` — 7,834 of 7,834 runs, a log of exactly one repeated line and not
one execution of the script body. The three lines in the block above use
ABSOLUTE paths, which is precisely why they work and this one never did.

It was retired rather than repaired because everything it tested is already
covered strictly better:

- its signal-freshness half → §24's stall watchdog, at a tighter threshold
  (30 min vs 35), twice the cadence, with an actuator and a durable
  `alert_sink_deliveries` row — where `loop_healthcheck` only echoed into a
  logfile that had no reader and no pager;
- its findings-freshness half → the LLM heartbeat, which excludes deterministic
  analysts (their output masks an LLM-plane outage — the blind spot a naive
  `max(analyst_outputs.created_at)` cannot see) and actually pages;
- its trigger was an **AND** of the two, so a pure LLM-plane outage with signals
  still flowing could never trip it — exactly the 2026-07-29 class;
- its printed remediation (`--force-recreate` the scheduler + wipe
  `deploy/dapr-scheduler-data/`) contradicts current doctrine, which is
  **restart, never recreate** (recreate churn is itself implicated in degrading
  the actor plane — see §24). Repairing the path would have armed a watchdog
  that prints actively harmful advice.

**The operator step is DONE** (verified 2026-08-03): `crontab -l` is empty and
`/var/log/legba_loop_health.log` no longer exists. Nothing references the
script — no cron.d file, no deploy path, no doc.

**Retired: `scripts/loop_watchdog.sh` (S-6 rider, 2026-08-03).** The sibling of
the above, and the last of the dead watchdog family (P6 H7). It was **never
scheduled anywhere** — not in `/etc/cron.d/legba-watchdog`, not in the root
crontab, not in `deploy/`, and referenced by no file in the repo but itself
since the day it was committed (2026-06-24). It has therefore never run once,
and unlike `loop_healthcheck` it did not even leave a log to prove it.

It was retired rather than armed, because arming it would have been actively
dangerous:

- Its remediation is
  `docker compose up -d --force-recreate dapr-placement dapr-scheduler
  dapr-sidecar legba-runtime-dapr`. Current doctrine is **restart, never
  recreate** — recreate churn is itself implicated in degrading the actor plane
  (§24). This is the same doctrine error that helped retire `loop_healthcheck`.
- Worse, it force-recreates **`dapr-scheduler`**, which is the single most
  destructive action available in this stack. The scheduler carries an embedded
  etcd holding every live reminder; `stop_grace_period: 45s` exists in
  `docker-compose.yml` *specifically* because docker's default 10 s
  SIGTERM→SIGKILL was corrupting that etcd mid-write, and the artefact of the
  time it did — `member/wal/…​.wal.broken`, 19 MB, 2026-06-15 — is still on disk.
  An automated, cron-driven recreate of that container is a machine that
  periodically risks total cadence loss to fix a stall a restart fixes.
- Its kill switch is `/tmp/legba_watchdog_off`, a **different flag** from the
  `/etc/legba-watchdog.disabled` every other host script honours — so an
  operator quieting the host layer the documented way before maintenance would
  not have quieted this one. Its restart cooldown marker also lives in `/tmp`,
  i.e. is cleared by a reboot.
- Its detection is `max(signals.fetched_at)` OR `max(analyst_outputs.created_at)`
  at 22 min. §24's stall watchdog supersedes it: signal freshness at 30 min on a
  `*/5` cadence, behind a seven-rung safety ladder, with the P2-ordered restart
  recipe and a durable `alert_sink_deliveries` row. The `analyst_outputs` half
  is the same blind spot `loop_healthcheck` had — deterministic analysts keep
  producing output straight through an LLM-plane outage and mask it — which is
  the gap §24.1's LLM heartbeat was built for.
- It logged its remediation into `/var/log/legba_loop_health.log`, the log
  retired above.

The only repo change is the deletion. There is **no operator step** this time:
there was never a cron line to remove.

All three watchdogs above are installed and running on this host. Verify with
`cat /etc/cron.d/legba-watchdog` plus `tail /var/log/legba-watchdog.log`; note
that the canary and heartbeat **exit silently when healthy**, so an empty log is
the expected steady state and the absence of a streak file
(`/tmp/legba-search-canary.streak`) is the positive signal, not a missing run.
To confirm the canary can actually fire, run it once with a deliberately bogus
component — `SEARCH_COMPONENT_ID=search.nonexistent.local
scripts/host_search_canary.sh` — twice, and check the page lands.

## 24.2 Container log collector (off-box logs, S-5)

**The problem it solves.** Docker's json-file driver is capped (`x-logging` in
`docker-compose.yml`, 100 MB × 5), but the log lives inside the container's
storage and **dies with it**. Every `docker compose up -d --force-recreate`
therefore deletes history. That has already cost a real investigation: on
2026-08-01 the runtime froze at 17:30 and the 19:31 recreate destroyed the
container that froze, taking every line with it. The minute-level
reconstruction of that outage exists only because `legba-registry` happened
**not** to be part of that recreate, so its access log still covered the window.
That is luck, not observability.

**What it is.** `scripts/host_log_collector.sh` — one detached
`docker logs --follow --timestamps` per compose container, appending to
`/var/log/legba/containers/<service>.log`. The script itself is the
**supervisor** for those followers: cron runs it every minute and it (re)starts
any follower that is missing, dead, or bound to a container id that no longer
exists (i.e. was recreated under it). Because the followers stream
*continuously*, every line is already on the host filesystem by the time a
container is destroyed — that is what "surviving recreates" means here.

Deliberately **not** Vector / Fluent Bit / an OpenSearch pipeline (the design
drafted in `FEATURE_COMPLETE_PLAN.md`). Those are right for a fleet; this is one
host that needs its evidence to outlive a recreate, with no new container, no
new dependency, and nothing extra to keep healthy.

**Install** (operator, one step — the repo ships the cron file, it does not
install it):

```bash
cp /usr/local/deployments/active/legba/deploy/cron.d/legba-log-collector \
   /etc/cron.d/legba-log-collector
chmod 0644 /etc/cron.d/legba-log-collector
```

The cron line is:

```cron
* * * * * root /usr/local/deployments/active/legba/scripts/host_log_collector.sh >> /var/log/legba_log_collector.log 2>&1
```

**Absolute paths, everywhere.** That is not style. `scripts/loop_healthcheck.sh`
was installed with a relative path and no `cd`, so cron ran it from `/root` and
it failed **7,834 out of 7,834 times over 54 days without executing one line of
its body** (§24.1). Every path in the collector is absolute and nothing in it
reads `$PWD`.

> **Install the cron, don't just run the script.** Running it by hand starts
> followers that deliberately **outlive your shell** (`setsid`, so cron cannot
> kill them either). Rotation, however, happens only on a supervisor **tick** —
> so a hand-run with no `/etc/cron.d/legba-log-collector` leaves 15 detached
> followers appending to files **nothing will ever trim**. Half-installed is the
> one state worse than not installed. If you find yourself there:
> `scripts/host_log_collector.sh stop`, then install the cron and let it start
> them properly.

**Verify it is actually running** — the steady state is silent, so silence is
not evidence:

```bash
stat -c %y /var/lib/legba-logship/heartbeat     # touched every tick
/usr/local/deployments/active/legba/scripts/host_log_collector.sh status
ls -la /var/log/legba/containers/
```

`status` prints one line per compose service with the follower pid, the
container id it is bound to, and the live file size. A service reading
`NOT-FOLLOWING` is the failure to chase.

**Operator surface.**

| Command | Effect |
|---|---|
| `host_log_collector.sh` | supervise once — what cron runs |
| `host_log_collector.sh status` | follower + size, one line per service |
| `host_log_collector.sh stop` | stop every follower; the files stay |
| `DRY_RUN=1 host_log_collector.sh` | print what it *would* start; start nothing |
| `touch /etc/legba-watchdog.disabled` | quiets it, and the §24 watchdogs, together |

**Which containers.** Discovered from compose labels
(`com.docker.compose.project=legba`), not a hardcoded list — a service added to
`docker-compose.yml` is collected on the next tick with no edit here. It also
skips non-compose strays automatically: `legba-test-age-w1`, the orphan
test-fixture Postgres, carries no compose labels and is correctly ignored.
`LEGBA_LOGSHIP_SERVICES` is an explicit allowlist when you want fewer.

**Rotation is copy-truncate, performed by the script**, not by logrotate. That
is not a preference — it is the only correct shape here. The follower holds an
**open append fd** on the live file, so a rename-based rotation (`mv live
live.1`) leaves it writing into `live.1` forever while `live` stays empty. This
is verifiable in ten seconds and worth doing once if you ever change it:

```bash
( for i in $(seq 1 200); do echo "line-$i"; sleep 0.05; done ) >> /tmp/t.log &
sleep 1.5; mv -f /tmp/t.log /tmp/t.log.1; : > /tmp/t.log; sleep 1.5
wc -l /tmp/t.log        # 0 — the writer was stranded on the rotated inode
```

Copy-truncate (`cp live live.1 && : > live`) keeps the fd valid: an `O_APPEND`
writer always writes at EOF, which is 0 after the truncate. The collector also
rotates **its own** cron log (`/var/log/legba_log_collector.log`) the same way,
since "no logrotate for any `/var/log/legba*.log`" is a standing finding and a
log collector that leaks its own log would be a poor answer to it.

**Cost, stated rather than buried.** The host has been running near 86% disk, so
size the collector before it surprises you:

- **Disk** — worst case `services × LEGBA_LOGSHIP_MAX_BYTES × (LEGBA_LOGSHIP_KEEP + 1)`.
  At the defaults (32 MiB, keep 3) and the current 15-container project that is
  **~1.9 GB**.
- **First-run burst** — the one number that surprised us, so it is called out
  separately. `docker logs --follow` with no `--since` replays the container's
  **entire retained json log**, which compose caps at 100 MB × 5 = **500 MB per
  container**. Arming the collector against the live stack wrote **125 MB from
  `legba-caddy` alone in the first seconds** — while that same container's
  steady-state rate is ~180 bytes/20 s, four orders of magnitude apart. So the
  first follow is the entire disk risk and it is pure backfill of history the
  json-file driver still holds anyway. `LEGBA_LOGSHIP_FIRST_RUN_LOOKBACK`
  (default `1h`) bounds it: the same caddy first-run now writes **18.7 KB**.
  A *recreate* still replays from the beginning — that container is seconds old
  and its window is exactly what the recreate would have destroyed.
- **Memory** — one `docker logs --follow` CLI process per container, ~15 MB RSS
  each, **~225 MB** across the project. That is the honest price of the simple
  design; `LEGBA_LOGSHIP_SERVICES` trims it.

> **2026-08-27 — the budget is now per-service, not global.** One global
> `MAX_BYTES × (KEEP+1)` = 128 MiB applied to every compose service, and the
> runtime-dapr container's by-design high-volume reminder-GC existence-check
> logging outpaces it — measured at ~17.3 MiB/hour, i.e. ~174 MB retained
> across a 9.5-hour window. The effect was silent: a three-day span of
> runtime logs was simply unrecoverable when it was wanted for a forensics
> pass. A `MAX_BYTES_OVERRIDE` map now applies **360 MiB/file × 4 files =
> 1,440 MiB (~83 h retention at the measured rate)** to `legba-runtime-dapr`
> alone. Deliberately not a raised global default: that would have added
> ~19 GB on a host already at 86–92% disk, for services that do not need it.
> If another service ever starts losing its window, add it to the override
> map rather than raising the global.

**Two failure modes this script was caught making, during its own bring-up
test** — both are in the file's comments, and both would have made it *look*
like it worked:

1. Splitting the container's stdout and stderr into two files left
   `legba-registry.log` at **0 bytes** while every line of the access log went
   to a `.err` sidecar nobody would think to open — Python's `logging` defaults
   to stderr, so this affects most of the fleet. The streams are now merged into
   one file, which is also what `docker logs` shows you.
2. A background follower **inherits** the supervisor's `flock` fd, and an
   inherited fd shares the open file description that carries the lock — so the
   first follower held the single-flight lock for its entire life and every
   later tick `flock -n`-failed and exited silently. Rotation would have stopped
   and a recreated container would never have been re-followed. The launch now
   closes fd 9 in the child (`9>&-`).

## 24.3 Actor turn budgets (S-6) — why a hung activate no longer freezes the plane

**The 08-01 mechanism, in four facts that compose.** A strict-mode parse bug
made `proxy.activate()` **hang** rather than fail. `ENSURE_ACTIVE` — the
reconciler's durability heal — fires against *every* active analyst and source
on *every* periodic resync. The reconcile main loop is **strictly serial**. And
Dapr actors are **turn-based** with reentrancy disabled everywhere
(`dapr/components/configuration.yaml` declares no reentrancy). So each hung
activate ate the full 90 s `run_once` bound *and* held its actor's turn forever;
the queue crawled at one descriptor per 90 s (visible in the registry access log
as one perfectly-spaced GET every 90 s), and every cadence reminder and
coalesced fire queued behind a turn that would never complete. The whole plane
was turn-poisoned by its own durability heal inside one resync cycle.

**Two bounds now exist, and they are a pair.** This is the part worth
remembering, because it is counter-intuitive: a deadline on the *caller* cancels
the caller's coroutine, it does **not** release the callee's turn — the actor
runtime holds a per-id lock inside the app process. So:

1. **`src/legba/runtime/dapr_host.py`** bounds every proxy lifecycle call in the
   reconcile executor. This stops the queue *paying* for a wedged actor. A
   timed-out call is a logged skip, and the observed-state row is deliberately
   **not** written — recording an activate that never confirmed is how the
   reconciler goes blind to the one actor that is actually broken.
2. **`AnalystActor._on_activate`, `SourceActor._core` / `activate` / `retire`**
   bound the hang-prone I/O *inside* the turn (the registry deps refetch, the
   reminder registration, upstream provisioning). This is what makes the turn
   **complete**, which is what lets the queue behind it drain.

Plus a **breaker** on the heal only: after N consecutive deadline misses on one
actor, `ENSURE_ACTIVE` is suppressed for a cooloff instead of being re-poked
every resync. Safe because the heal re-runs next resync anyway; at 217 active
actors a fleet-wide wedge would otherwise burn `217 × deadline` per cycle,
every cycle. CREATE / RETIRE / TRANSITION are deadline-bounded but never
breaker-skipped — those are one-shot convergence steps, and skipping one means a
descriptor that never reaches its declared state.

**Tunables** (all fail safe — unset, malformed or non-positive falls back to the
default, so a typo can never silently disable a budget):

| Env | Default | What it bounds |
|---|---|---|
| `LEGBA_RECONCILE_HEAL_TIMEOUT_SECONDS` | 20 | one proxy lifecycle call from the reconcile executor |
| `LEGBA_ACTOR_TURN_OP_TIMEOUT_SECONDS` | 30 | one hang-prone op inside an actor turn |
| `LEGBA_RECONCILE_HEAL_BREAKER_TRIPS` | 3 | consecutive heal misses before the breaker opens |
| `LEGBA_RECONCILE_HEAL_BREAKER_COOLOFF_SECONDS` | 600 | how long an open breaker suppresses the heal |

The heal deadline must stay **well below** `ReconcileLoop.run_once_timeout`
(90 s) — that gap is the entire fix, and a test pins the ratio.

**What to grep when the plane looks slow:**

```bash
docker logs --since 30m legba-legba-runtime-dapr-1 2>&1 | grep -E \
  'action_executor.deadline|action_executor.heal_suppressed|actor_turn.budget_exceeded|activate.deps_timeout|reminder.timeout'
```

- `action_executor.deadline` — a lifecycle call blew the heal budget; carries
  the consecutive count.
- `action_executor.heal_suppressed` — the breaker is open for that actor. If
  this is fleet-wide you are in an 08-01-shaped event: the actors are not
  answering, and the reconcile loop is now surviving it rather than freezing.
- `actor_turn.budget_exceeded` — an op *inside* a turn was released. The turn
  completed; the queue behind it drained.
- `dapr_actors.analyst.activate.deps_timeout` — the registry did not answer the
  deps refetch. Check `legba-registry` load before blaming the actor.
- `dapr_actors.analyst.reminder.timeout` — daprd/scheduler did not answer the
  reminder registration. Logged separately from `reminder.invalid` on purpose,
  so a scheduler-plane problem is never misread as a descriptor typo.

Steady state emits **none** of these.

## 24.4 Nightly CI-lite suite (R7, 2026-08-04)

**The condition.** There is no CI. The suite was green whenever somebody last
remembered to run it, which has been as much as a week, so a regression
surfaced when the next person ran the suite for an unrelated reason — usually
mid-wave, and usually blamed on whatever they had just changed.

**What runs.** `scripts/host_nightly_suite.sh`, nightly at 03:00 local, in
three phases, cheapest first:

| Phase | What | Why |
|---|---|---|
| `lint` | `ruff check` per `[tool.ruff]` in `pyproject.toml` | Seconds. A syntax-level mistake pages in one minute, not two hours. |
| `ordered` | Full suite, `-p no:randomly` | The run that is comparable night to night. |
| `shuffled` | Full suite, `--randomly-seed=<logged>` | File order is an accident, not a contract. A suite that only passes in one order is hiding shared state between tests. |

All three go through `scripts/run_tests_in_container.sh` — one image, one
mount, one `REPO_ROOT` — so lint and tests can never drift onto different
interpreters or a different checkout. That runner pins
`LEGBA_DATA_PG_DB=legba_pivot_test`, so the nightly never touches live `legba`.

**Install (operator, one step)** — after the branch carrying this is merged
into the main checkout. The cron line points at the main checkout's copy of
`host_nightly_suite.sh`, and that script plus the `--lint` mode it calls both
land with the same branch; installing the cron file first gives a nightly that
silently does nothing.

```bash
cp /usr/local/deployments/active/legba/deploy/cron.d/legba-nightly-suite \
   /etc/cron.d/legba-nightly-suite
chmod 0644 /etc/cron.d/legba-nightly-suite
```

**Where the evidence lands.** `/var/log/legba/nightly/<UTC-timestamp>/` with
`lint.log`, `ordered.log`, `shuffled.log`, `summary.txt`; `latest` is a symlink
to the newest. The script rotates its own run dirs (14 kept) and caps its own
cron log — P6 §6 item 11 is "no logrotate for any `/var/log/legba*.log`".

```bash
ls -l /var/log/legba/nightly/latest
grep -E 'VERDICT|seed=' /var/log/legba/nightly/latest/summary.txt
```

**Reproducing a shuffled failure.** The seed is printed in `summary.txt` and in
the alert body. Replay the exact order with:

```bash
bash /usr/local/deployments/active/legba/scripts/run_tests_in_container.sh \
  tests/ --randomly-seed=<SEED>
```

**The allowlist is the whole point.** A nightly that always fails is a nightly
nobody reads, so `KNOWN_FAILURES` in the script names the failures this rig
produces for reasons that are not the code's fault — the `LEGBA_TEST_STRICT`
infra class, where strict mode deliberately escalates infra-gated skips to
failures and the live stack already owns the ports those tests want (daprd
6090/NATS contention, the webhook binder, the SSRF 127.0.0.1 guard). Anything
**not** on that list pages. Anything **on** it that stops failing is reported
in the summary as a stale entry to retire.

**The bar for adding an entry** is that the failure is a property of the RIG,
not the code — something a clean checkout on a clean host would not reproduce.
Merely flaky or order-dependent does not qualify; that is a bug with a
misleading name. Four such tests were fixed rather than listed when this
landed: the production-gauge truncation trap, two dspy assertions, and the K-4
pre-registered acceptance gate, which had been failing on every full-suite run
in the main checkout because its subprocess replaced the environment and so
lost `pydantic`.

**The second list, `KNOWN_SHARED_STATE`, is a work queue.** The first shuffled
run turned up **14 genuine order dependencies** — tests that read state another
test wrote, in a suite that shares one Postgres. They are frozen in that list
for one reason: so a *new* one is visible above them. Without it the shuffled
pass reports fourteen failures every night, nobody reads it, and the fifteenth
arrives unnoticed. **The list may only ever shrink**; an addition means someone
introduced shared state, and the answer is a fix, not an entry.

They share one shape, worth naming because it is what the gauge fixture taught:
an assertion written as a *global* statement over a substrate the whole suite
shares — "nothing else was wired", "exactly 3 entities were damped". Written as
a statement about the test's own rows, each would be order-proof. Reproduce any
of them with the recorded seed:

```bash
bash /usr/local/deployments/active/legba/scripts/run_tests_in_container.sh \
  tests/ --randomly-seed=20260804
```

**Lint policy.** `[tool.ruff]` is configured to be **green on the tree as it
stands**. It is a ratchet for new code, not a cleanup mandate: every rule the
current tree violates is parked in `ignore` with its violation count, and that
list is the debt ledger. Adding a rule is therefore always safe; removing an
`ignore` is the deliberate act that costs a cleanup commit. Never run a
tree-wide `ruff check --fix` — the families left out (`I`, `UP`, `TID`, `S`)
are five-figure-line diffs that would collide with every held branch.

**Disable during maintenance:**

```bash
touch /etc/legba-nightly-suite.disabled   # this job only
touch /etc/legba-watchdog.disabled        # the whole watchdog family
```

## 25. OpenSearch corpus orphan purge (W2-C, 2026-08-03)

**The condition.** `legba_signals_corpus` had no delete path of any kind — the
store exposed index/search/get and nothing else — so every `signals` purge in
the platform's history left its documents behind. Measured exhaustively on
2026-08-03 (every `_id` scrolled and set-differenced against `signals`, not
sampled):

```
corpus docs                     182,648
signals rows                    111,537
ORPHAN docs (no signals row)     75,871   (41.5%)
live docs                       106,777
indexed signals carrying NO doc    4,743
```

An orphan stays BM25-searchable forever, and `read_document` serves it verbatim
— that path does no join back to Postgres. The 41.5% supersedes the 54% in the
2026-08-02 engine review §3.3, which was a 200-doc sample.

**What is now automatic.** Every signals-deletion site writes a
`corpus_tombstones` row (migration 0175) in the SAME transaction as its DELETE,
and the `corpus_retention` analyst drains that queue against OpenSearch every
15 minutes. Nothing further is required for orphans to stop accumulating.

**The historical 75,871 are NOT queued automatically** — the deletion already
happened, so there is nothing to hook. Queue them deliberately:

```bash
# 1. census only, writes nothing — confirm the numbers first
docker exec legba-legba-runtime-dapr-1 \
  python scripts/seed_corpus_orphan_tombstones.py

# 2. queue them (the sweep drains ~2,000/tick → the backlog clears in ~5h)
docker exec legba-legba-runtime-dapr-1 \
  python scripts/seed_corpus_orphan_tombstones.py --apply

# 3. optional: re-queue the 4,743 signals stamped indexed but carrying no doc,
#    so corpus_indexer rebuilds them through its normal dirty-marker path
docker exec legba-legba-runtime-dapr-1 \
  python scripts/seed_corpus_orphan_tombstones.py --apply --requeue-missing
```

**Reversible until the sweep runs.** The queue is plain rows:

```sql
DELETE FROM corpus_tombstones
 WHERE purged_at IS NULL AND reason = 'orphan_backfill';
```

After the drain the OpenSearch delete is real, but a doc whose Postgres row is
gone is a projection of nothing — there is nothing to restore. Every dropped id
stays queryable (`purged_at IS NOT NULL`), which is the audit trail that did not
previously exist.

**Watching it.** The queue is a declared `backlog_drain` loop
(`corpus_tombstone_drain`) on the S-1 production gauge, owned by
`corpus_retention`, so a drain that stalls pages instead of quietly growing:

```bash
curl -s "$REG/api/v1/v3/system/production-gauge?loop_class=backlog_drain" | jq
```

Per-run receipts live in the analyst trace (`examined` / `deleted` / `pending` /
`skipped_row_alive` / `max_attempts`). **`skipped_row_alive` should always be
0** — it counts tombstones whose `signals` row still exists, which the drain
refuses to delete. A non-zero value means the QUEUE is wrong, not the corpus,
and nothing was destroyed.

## 26. Cold-activation deploy smoke (the 08-01 outage gate)

`deploy/deploy.sh`'s verify phases prove that processes STARTED; none of them
proves an actor can go from cold to a completed run. That is the exact gap the
2026-08-01 outage fell through — a descriptor-parse bug that only bites on the
COLD path kept every warm actor serving (all green) while the fleet could not
activate, and the test suite missed it too (descriptors are built in-process;
nothing traverses registry-fetch → parse → activate → run against a live
sidecar).

After every deploy train, run the smoke by hand:

```bash
scripts/deploy_smoke_cold_activation.sh --env-file .env
```

It forces ONE unit on ONE desk through the sidecar and asserts a **fresh
`analyst_traces` row**, distinguishing "no trace" (cold-activation failure —
the 08-01 shape) from "failed trace" (activated, run died), and exits nonzero
loudly. It is deliberately a manual step rather than a `deploy.sh` verify
phase because it WRITES — a real analyst run, a real finding — and whether the
deploy script may mutate the substrate is an operator decision, not a wiring
detail. Pair it with the standing deploy discipline in §0: registry FIRST,
wait healthy, then the runtime — a stale registry 500s `/typed` and silently
stops analysts.
