# Legba Setup — from-zero bootstrap

The new-operator path: stand a fresh Legba instance up from **empty volumes**
through to current/full scope, end to end. Follow the steps below in
order — the ordering matters.

This is the **bootstrap** guide. For day-to-day operations (restarts, failure
modes, troubleshooting, credential rotation) use [RUNBOOK.md](RUNBOOK.md); for
the architecture read [ARCHITECTURE.md](ARCHITECTURE.md) and the
[README](../README.md). Before you start, skim **RUNBOOK §0 (critical operator
notes)** — it carries the hard-won "do not do this" rules this guide assumes.

> **Canonical one-command path: [`deploy/deploy.sh`](../deploy/deploy.sh).** After
> building the images (`docker compose --profile runtime build`), the entire
> ordered sequence in §3–§11 below runs as one idempotent, boot-verified command:
> ```
> deploy/deploy.sh            # real stack (project legba)
> deploy/deploy.sh --seed     # + the curated knowledge seeds (§10)
> ```
> It applies the single proven baseline (`deploy/baseline/0001_baseline.sql`)
> rather than the 23-file history, runs the registrars in the order below, then
> boots the runtime and verifies. The numbered manual steps that follow are the
> same sequence — kept as the explanation of what the script does and for partial
> re-runs. For a **throwaway clean-slate validation stack** on the same host, use
> `deploy/deploy.sh --project legba_val --no-caddy` (fully data-isolated from the
> real `legba` volumes; teardown with `deploy/deploy.sh --project legba_val --teardown`).

> **Clean-slate only — no migration path from pre-pivot Legba.** This is a
> complete refactor from the v1/v2 target-first design; the data model, substrate
> schema, and APIs are incompatible with pre-pivot instances. Stand up a fresh
> empty substrate and apply migrations from `0001_baseline` forward. **Do not
> point this build at a pre-pivot database.**

---

## 0. Source scope at a glance (read this first)

A fresh deploy has **two** distinct source sets, and the difference is the single
most common bootstrap mistake:

| Set | Count | Registered by | When |
|---|---|---|---|
| **Minimal cold-start** | **3** shared RSS (BBC World, Deutsche Welle, Al Jazeera) | `bringup_register_p17_workingset.py` (§7) | Always — part of the working set |
| **Full catalog** | **46** sources (~43 RSS + 3 GeoJSON hazard feeds) | `bringup_register_source_catalog.py` (§8) | **Separate MANUAL step — easy to miss** |

The 3 RSS feeds are a deliberately small, easily-verified **cold-start smoke
test** — they prove the source → enrich → fan-out → assess pipeline lights up.
They are **NOT** the deployed scope. The 46-source catalog is how you reach
current/full scope. **The one-command `deploy/deploy.sh` registers the full
catalog for you** (Phase 5). On the **manual path**, however, it is **not part of
the working-set bringup** — it is the separate step in **§8**. **If you take the
manual path and stop at the working set you will sit at 3 feeds** (this is exactly
why reviews correctly say "only 3 RSS feeds" of an under-bootstrapped instance).
On the manual path, do not skip §8.

For reference, the live production instance reaches **~49 distinct
signal-producing sources** (the 46 catalog sources plus seed/baseline adapters).
That is the "full scope" number you are aiming for.

This is the canonical, self-hostable demo set — G20 world-news plus hazard feeds.
It is an exemplar, not a closed list: the same pipeline applies to any domain
whose evidence arrives as open-source feeds. No accuracy or forecast-skill claim
is made about the assessments; see the README release-boundary table.

---

## 1. Prerequisites

- **Docker + Docker Compose v2** on the host. Everything runs in containers;
  there is no host-side Python requirement for the bootstrap (the bringup
  scripts run inside the registry container).
- **The repo** checked out at the canonical path `/usr/local/deployments/active/legba`
  (the `.env` and compose file resolve relative to it).
- **A `.env` file** (gitignored) at the repo root. Copy `.env.example` and fill it
  in. The keys that matter for a clean bootstrap (full notes in `.env.example`
  and RUNBOOK §4.0):
  - `LEGBA_REGISTRY_API_TOKEN` — bearer for the registry API. Without it the API
    fails **closed** (HTTP 503) unless `LEGBA_DEV_MODE=1`. Set a real token for
    anything beyond local dev.
  - `LEGBA_DATA_MASTER_KEY` — XSalsa20-Poly1305 key encrypting vault secrets.
    **Must be stable across restarts** or every stored credential becomes
    unreadable.
  - `LEGBA_REGISTRY_SIGNING_KEY` — Ed25519 signing key (hex) for the receipt
    chains. Generate a persistent one (RUNBOOK §11 "Set production-mode auth").
  - `LEGBA_GEOCODER_CONTACT_EMAIL` — a reachable address for the OSM Nominatim
    User-Agent (OSM ToS). If unset/`.invalid`, geocode **refuses to build** →
    signals land geo-less → geo-scoped analysts never match.
  - `LEGBA_PUBLIC_DOMAIN` + `LEGBA_BASIC_AUTH_HASH` — Caddy edge domain + the
    bcrypt hash for UI basic-auth (single-quote/`$$`-escape the hash in `.env`).
- **External model endpoints** the stack expects (registered as stack components
  in §6, resolved by the runtime at boot):
  - An **OpenAI-compatible LLM endpoint** for the core analyst plane.
  - An **embedding endpoint** (`embed.primary.openai_compat`) — used by
    dedupe tier-3 + semantic correlators.
  - The **`legba-models` NLP service** (`nlp.local.legba_models`: NLLB / DeBERTa /
    GLiREL / spaCy) for language-detect, NER, classify, and relation extraction.
    Runs on its own host; the runtime references its URL + vault creds.

  These are infrastructure you provide; their URLs/creds go into the vault
  (§5) and stack components (§6). The runtime **degrades gracefully** if one is
  missing — descriptors that don't use the affected kinds still activate — but
  enrichment quality depends on them.

---

## 2. Build the app images

```
cd /usr/local/deployments/active/legba
docker compose --profile runtime build
```

One-time (or after a code change); re-runs are layer-cached and fast. After any
`src/legba/data/schemas/*` change you must rebuild **both** `legba-registry`
and `legba-runtime-dapr` (RUNBOOK §0).

---

## 3. Bring up the substrate + registry only (NOT the runtime yet)

**Order matters here.** The runtime builds its NLP / embedding clients **once
at boot** from the registered stack. Boot it against an empty registry and
`nlp_client` stays `None` for the whole process lifetime → enrichment fails →
signals land with no geo/entities. So seed first, boot the runtime last (§9).

```
docker compose up -d                              # substrate: redis / postgres / qdrant / nats
docker compose up -d legba-registry               # registry API only — no actor host yet
```

---

## 4. Apply migrations

Idempotent; runs against the substrate Postgres (ledger:
`legba_data_migrations`).

```
docker exec legba-legba-registry-1 python -m legba.data.migrate
```

> The only CLI flag is `--dry-run` (discover-but-don't-apply). There is **no**
> `--primary-only` flag — a stale earlier draft of these docs referenced one;
> passing it makes argparse exit 2.
>
> At a true cold-start where the registry container isn't up yet, use the
> repo-mounted one-off form instead:
> `docker compose run --rm --no-deps --entrypoint python legba-registry -m legba.data.migrate`
>
> A fresh deploy applies the single proven baseline
> (`deploy/baseline/0001_baseline.sql`) and then this runner applies any FUTURE
> (`0054`+) migrations. The baseline pre-seeds the ledger to head `0053`, so on a
> baseline-provisioned DB `migrate` reports nothing pending. (`deploy/deploy.sh`
> does both steps for you.)

Verify (migration head should be **0053**; ISO countries table fully seeded):

```
docker exec legba-postgres-1 psql -U legba -d legba \
    -c "SELECT name FROM legba_data_migrations ORDER BY name"   # head 0053
docker exec legba-postgres-1 psql -U legba -d legba \
    -c "SELECT count(*) FROM iso_countries"                     # expect 249
```

---

## 5. Load credentials into the encrypted vault

```
docker exec legba-legba-registry-1 python scripts/bringup_vault_load.py
```

Idempotent — secrets that already exist are skipped; it never echoes plaintext.
Plaintext is encrypted with `LEGBA_DATA_MASTER_KEY` and stored in
`stack_credentials`. (Single-secret + manual forms: RUNBOOK §6.)

---

## 6. Register the substrate stack components

```
docker exec legba-legba-registry-1 python scripts/bringup_register_stack.py
```

Registers the stack the runtime resolves at boot: the LLM providers, the
embedding service, the NLP service, the vector store, NATS, Postgres+AGE, and the
proxy. Each `built` line in the runtime boot log (§9) confirms one resolved.

---

## 7. Register the source-first working set (3 RSS + G20 + analysts + packs)

This is the canonical demo set in one dependency-ordered pass. Pin
`LEGBA_DATA_PG_DB=legba` — the DB-direct registrars default to
`legba_pivot_test`.

```
# Action packs + the 3 shared sources + 19 G20 country targets + 4 analysts:
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_p17_workingset.py
```

This registers the **3 shared sources** (`source.bbc.world` /
`source.aljazeera.world` / `source.dw.world`), the **19 G20 country targets**
(`country_g20_<iso2>`, geo-predicate `source_selector` + per-country subscription
+ inline analyst), the **4 source-first analysts** (`country_assessor` /
`country_critic` / `country_optimizer` / `consult_default`), and the action packs
(`media_processing` / `incident_response` / `discovery`).

> Note: `scripts/bringup_register_sources.py` is a standalone registrar for the
> same 3 feeds; the working-set script above already covers them, so you do not
> need to run it separately.

At this point the instance has **only the 3 cold-start RSS feeds**. Continue to
§8 — do not stop here.

---

## 8. ⚠️ Register the FULL 46-source catalog (reach current/full scope)

**This is the step a fresh operator usually misses.** It is not auto-run on
deploy and not part of the §7 working-set bringup — it is a **separate manual
step**, and skipping it is why an instance sits at "only 3 RSS feeds."

`scripts/bringup_register_source_catalog.py` registers the full catalog —
**exactly 46 sources** (~43 RSS + 3 GeoJSON hazard feeds: USGS significant
earthquakes / NWS severe-weather alerts / NASA EONET natural-events). Each RSS
source carries its enrichment chain (dedupe → language_detect → ner_multilingual
→ [fact_extractor on 4 feeds] → geocode); GeoJSON is geocode-first. It is
idempotent, supports a dry `--verify` (live HTTP probe, registers nothing), and
seeds host-level `source_credibility` rows (ON CONFLICT DO NOTHING). Pin
`LEGBA_DATA_PG_DB=legba`.

```
# Optional first: live HTTP probe + parse check per feed — prints a verdict
# table (ok / redirect / dead / parse-fail / empty). Registers NOTHING.
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_source_catalog.py --verify

# Register all 46 catalog sources:
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_register_source_catalog.py
```

> Repo-mounted compose-run form (equivalent; use it if the registry container is
> not already up):
> ```
> docker compose run --rm --no-deps -v "$PWD:$PWD" -w "$PWD" \
>   -e LEGBA_DATA_PG_DB=legba \
>   -e LEGBA_REGISTRY_URL=http://legba-registry:8090/api/v1/registry \
>   --entrypoint python legba-registry scripts/bringup_register_source_catalog.py
> ```

---

## 9. Boot the runtime (against the now-seeded registry)

Now — and only now, after the stack + working set + catalog are registered — boot
the runtime + Dapr + UI + Caddy, so the runtime builds its `nlp_client` against
the seeded stack.

```
docker compose --profile runtime up -d --force-recreate
```

> `--force-recreate` is **required** if the runtime was ever booted earlier in
> this lifetime (e.g. you ran `docker compose --profile runtime up -d` before
> seeding) — a plain restart will not rebuild the boot-time `nlp_client`, which
> stays `None` and pins enrichment off (RUNBOOK §0).

Watch the boot log; expect the factory `built` lines in order (RUNBOOK §4.2):

```
docker logs legba-legba-runtime-dapr-1 | grep dapr_host
# dapr_host.nlp_client.built component=nlp.local.legba_models   ← enrichment is live
# ... and zero  enrichment_build_failed  lines
```

---

## 10. Register the ongoing analysts + the budget envelope

The deterministic cadence analysts and the global daily token envelope. Pin
`LEGBA_DATA_PG_DB=legba`.

```
for s in finding_supersession cross_source_dedup entity_resolution; do
  docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
    python scripts/bringup_register_$s.py
done
docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \
  python scripts/bringup_set_budget_envelope.py
```

(Optional knowledge-grounding seeds — current world leaders, blocs — via
`scripts/seed.py`; see RUNBOOK §7.2/§7.3. Recommended so stale-cutoff analyst
LLMs ground on current authoritative facts, but not required to light up
ingestion.)

---

## 11. Verify ingestion + that you reached full scope

Once the runtime is up and the resync loop has fired (5-minute default), signals
should land. Give it a cadence interval, then:

```
docker exec legba-postgres-1 psql -U legba -d legba <<'SQL'
SELECT count(*)                       AS signal_count          FROM signals;
SELECT count(*)                       AS enriched_count        FROM signals WHERE language IS NOT NULL;
SELECT count(*)                       AS analyst_output_count  FROM analyst_outputs;
SELECT count(DISTINCT source_id)      AS distinct_sources      FROM signals;
SQL
```

**Verify you reached full scope** — `distinct_sources` must be **dozens, not 3**.
A current-scope instance reaches **~49 distinct signal-producing sources** once
the §8 catalog + the seed/baseline adapters are active. If you see only 3, you
skipped §8 — go back and register the catalog.

Enriched signals (`language IS NOT NULL`) confirm the `nlp_client` built; findings
in `analyst_outputs` (with `derived_from` provenance) confirm the fan-out →
analyst loop is closing.

The UI is served by Caddy on `:80`/`:443` at `https://$LEGBA_PUBLIC_DOMAIN/`
(basic-auth, user `legba`). See RUNBOOK §5 to verify the edge.

---

## 12. Where to go next

- **Operations** (restarts, failure modes, credential rotation, cleaning stale
  reminders): [RUNBOOK.md](RUNBOOK.md), starting with **§0**.
- **Architecture / data model**: [ARCHITECTURE.md](ARCHITECTURE.md),
  [DATA_MODEL.md](DATA_MODEL.md), [FLOWS.md](FLOWS.md).
- **Sources catalog detail**: [DATA_SOURCES.md](DATA_SOURCES.md).
- **Known seams** (declared-but-unbuilt boundaries): [SEAMS.md](SEAMS.md).
