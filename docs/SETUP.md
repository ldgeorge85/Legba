<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Setup — from zero to a running instance

One script brings the platform up in the order that matters. This page is what you need before you run
it, what it does, and how you know it worked. Day-to-day operation is the [Runbook](RUNBOOK.md);
tuning your instance to your own sources and standards is
[Operating your instance](OPERATING_YOUR_INSTANCE.md).

## 1. Before you start

**Host.** Docker with Compose v2. Everything runs in containers; the bring-up scripts run inside the
registry container, so the host needs no Python. The repository lives at a fixed path,
`/usr/local/deployments/active/legba`, because the env file and the compose file resolve relative to it.

**Model endpoints.** Legba hosts no model itself. Provide, and register as stack components:

| Endpoint | Used by | Shape |
|---|---|---|
| an OpenAI-compatible chat endpoint | every scheduled analyst: units, compositions, voices, lenses, the reference builder | `llm.primary.openai_compat`; self-hosted vLLM on the reference deployment |
| an embedding endpoint | deduplication and semantic correlation | `embed.primary.openai_compat` |
| the `legba-models` NLP service | language detection, translation, entity and relation extraction | `nlp.local.legba_models`; its own host, reached by URL and vault credentials |
| a hosted cross-family judge | the faithfulness verify pass | optional; the shipped default is same-model on the primary endpoint |

The runtime degrades rather than fails if one is missing: descriptors that do not use the affected
kind still activate, and enrichment quality falls accordingly. The planes, and who pays for each, are in
[TUNABLES.md](TUNABLES.md).

**The env file.** Copy `.env.example` to `.env` at the repository root and set the keys below. The
example file documents every key; these are the ones a clean bootstrap depends on.

| Key | Why it matters |
|---|---|
| `LEGBA_REGISTRY_API_TOKEN` | bearer for the registry API; without it the API fails closed |
| `LEGBA_DATA_MASTER_KEY` | encrypts every vault secret; it must stay stable across restarts or every stored credential becomes unreadable |
| `LEGBA_REGISTRY_SIGNING_KEY` | the signing key for receipt chains; generate a persistent one (Runbook, routine tasks) |
| `LEGBA_GEOCODER_CONTACT_EMAIL` | the contact the geocoder sends to OpenStreetMap; unset, geocoding refuses to build and geo-scoped desks never match a signal |
| `LEGBA_PUBLIC_DOMAIN`, `LEGBA_BASIC_AUTH_HASH` | the Caddy edge domain and the bcrypt hash for the console's basic-auth perimeter; quote the hash so the shell does not expand it |
| `LEGBA_DATA_PG_*` | the Postgres connection; the compose defaults work for a single-host stack |

Model credentials do not go in the env file. They go into the encrypted vault in step 3.

## 2. Build

```bash
docker compose --profile runtime build
```

Builds the registry, runtime and worker images. The worker image is built from the runtime image, so
the profile builds both and in that order. Re-runs are layer-cached.

## 3. Bring it up

```bash
deploy/deploy.sh                 # the real stack, project "legba"
deploy/deploy.sh --seed          # also load the curated knowledge seeds
deploy/deploy.sh --no-caddy      # loopback only, no TLS edge (a validation stack)
```

The script is phased and idempotent. In order:

1. **Preflight.** The env file, the compose file, the images.
2. **Isolation gate.** For any project name other than `legba`, the script proves the stack uses its
   own volumes before it starts anything, so a validation stack can never bleed into real data. The
   real project skips this phase by design.
3. **Substrate and control plane.** Postgres, Redis, Qdrant, OpenSearch, NATS, SearXNG, RSSHub, then
   the Dapr placement, scheduler and sidecar.
4. **Schema.** Migrations run in a one-off registry container before the registry serves anything.
5. **Registrars.** Credentials into the vault, then stack components, action packs, the source catalog,
   the desks and the analysts, through the registry API. Each registrar is idempotent.
6. **Seeds** (with `--seed`). The curated knowledge seeds load here, before the runtime boots. The
   order is load-bearing: the runtime builds its enrichment clients once at boot, and a runtime that
   booted before the seeds landed would keep stale clients for its whole life. The script force-recreates
   the runtime for the same reason.
7. **App up.** Registry, runtime, worker, and the Caddy edge unless `--no-caddy`.
8. **Boot verify.** The registry answers its health route; the runtime's boot log shows enrichment
   built and no build failure; sources are registered; no migration is pending. A failed check prints
   a warning and sets a non-zero exit; it does not tear anything down.

The whole run takes a few minutes on a warm image cache.

**Credentials.** The vault loader in phase 5 reads plaintext from the env file's credential block,
encrypts it with the master key, and stores it. It is idempotent and never echoes a secret. To add or
rotate one later, see the Runbook's routine tasks.

## 4. Is it alive

```bash
docker compose ps                                   # every service Up
curl -s http://127.0.0.1:8090/api/v1/registry/healthz   # the registry
docker compose exec -T postgres psql -U legba -d legba -c \
  "SELECT count(*) FROM signals; SELECT kind, count(*) FROM analyst_outputs GROUP BY kind;"
```

Signals land within the first poll cycle. The first unit findings follow on the units' cadence, the
first compositions after them, and the first faithfulness critiques alongside. The Runbook's health
page lists each check with the number that means fine.

Then open the console at your public domain, or on the loopback stack at the port the compose file
publishes, and take the [Tour](TOUR.md).

## 5. What you have, and what you do not

Out of the box the catalog registers a fixed set of unauthenticated feeds and the shipped desks.
Further verified sources ship in the tree as draft descriptors, inert until you activate them one
feed at a time through the lifecycle route, once you have confirmed each route is live from your
instance. The live counts for your deployment are generated into [RELEASE_STATE.md](RELEASE_STATE.md).

Credentialed sources, the paid judge and audit routes, Telegram, and the consult path are all opt-in
and each is one descriptor or one env line. Nothing scheduled bills a third party until you point a
route at one.

There is no migration path from earlier designs of the platform. Fresh volumes only.

## 6. Tearing down

```bash
deploy/deploy.sh --teardown            # stops the stack; volumes are kept
docker compose down -v                 # also destroys every volume: descriptors, signals, vault, indices
```

The second form loses everything. Back up first: the Runbook's backup and restore task covers all
four stores.
