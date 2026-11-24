<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Operating your instance

For someone running an instance after it boots. [SETUP.md](SETUP.md) gets the stack up and
[RUNBOOK.md](RUNBOOK.md) has the commands for day-to-day mechanics. This page covers the work that
does not happen by itself: seeding, curating a corpus, re-measuring thresholds, checking data
quality, and turning on sign-in.

## 0. What ships and what you supply

The code, migrations, descriptors and console ship complete. Three things do not:

- **Seed data.** `seeds/` ships format examples only.
- **Time.** Desk baselines need 28 days of history; band calibration resolves at 14 and 28 days.
- **Measurements on your data.** The numeric thresholds in the tree were measured on another
  instance's sources. Treat them as starting values.

A narrower domain (fewer desks, fewer sources) converges faster and is easier to measure.

## 1. Seeding your world

A **source** writes raw `signals`. A **seed** adapter writes to the knowledge layer (`facts`,
`nexuses`, `entity_profiles`) with `valid_from` dates and idempotent upserts. A fresh clone has
no seeded facts, so the units run without structured grounding: a model can state an officeholder
it learned in training and nothing will correct it.

`seeds/world_baseline.example.yaml` (leaders, alliances, conflicts) and
`seeds/source_ratings.example.yaml` (source assurance) are the contracts; the sources in the second
file are invented placeholders. `seeds/README.md` lists the adapters.

A missing seed file makes its adapter log a warning and write nothing, so `deploy/deploy.sh --seed`
on a fresh checkout looks like success. Always dry-run, then check the rows:

```bash
python scripts/seed.py --list
python scripts/seed.py --source world_baseline --dry-run
python scripts/seed.py --source world_baseline
```

Success: `facts` and `nexuses` rows with `source_type='seed'` and a new `seed_batches` row.

When you write a baseline, use the `sides:` grouping for conflicts. It produces hostile edges across
coalitions and allied edges within them. Leader rows write both a person-is-leader fact and a
country office fact; the second is what the grounding step reads.

The `wikidata_leaders` adapter needs no file: it reads current officeholders from Wikidata. Live
upstream data can be vandalised, and a seed import supersedes your existing facts. Before applying
it, run the read-only preview and read every row it would change:

```bash
python scripts/diagnose_stale_leaders.py
```

If you write an adapter against a live upstream, write its read-only preview at the same time.

### Sources: start small

Most source descriptors ship `draft`. Registering one creates no live actor; activation is an
admin's step after checking the feed.

1. Activate a small set whose subjects your desks cover.
2. Let it run for several days.
3. Read the poll history (§4) and retire feeds that produce nothing.
4. Add the next batch.

Geocoding refuses to build a public-Nominatim backend until `LEGBA_GEOCODER_CONTACT_EMAIL` is a real
address. Sources that need credentials stay `draft` until the vault holds them.

## 2. Curating a corpus

The vector corpus is optional background retrieved by similarity and injected as a non-citable
prior. The verify pass never accepts it as support.

**Keep corpus bytes out of the repository and out of images.** Load them from a git-ignored working
directory. Many useful documents are publicly readable but not redistributable.

**Decide each document's licence before you download it.** Keep a list with one row per document:
canonical URL, a working retrieval URL, the verdict, the date checked, and any caveat.

| Verdict | Ingest? | Redistribute? |
|---|---|---|
| public domain | yes | yes |
| open licence (OGL, Creative Commons) | yes | yes, with attribution |
| treaty or UN official text | yes | yes, attributing the issuer |
| publicly released, no open licence | operational use only | no; summaries only |
| copyrighted | no full text | quotes and summaries only |
| no lawful licence | no by default | never |

**Every chunk carries** `license`, `source_url`, `effective_date`, `corpus` and `doc_id`.

**Turn retrieval on one unit at a time**, with a comparable unit left off as a control.
`scripts/rag_watch.py` compares each unit before and after: run count, mean faithfulness,
low-faithfulness rate, tokens per run and latency, against a rollback rule you write down first.
Watch the low tail as well as the mean. A model can repeat background text as if it were cited
evidence, and that shows up as more low-faithfulness findings.

**Set your own relevance floor.** The shipped floor was set on another corpus and embedder. Take a
dozen queries that should match and a dozen that should not, record the best chunk score for each,
and put the floor in the gap. If there is no gap, fix the chunking or the embedder. A floor set too
high means retrieval never fires.

## 3. Re-measuring thresholds

[TUNABLES.md](TUNABLES.md) lists every budget, cap and threshold with where it lives. Some change
through `.env` or a descriptor's `method.options`; others are module constants. The comments next to
each constant say what was measured. These are the ones most likely to be wrong on a new instance:

| Threshold | Symptom when wrong |
|---|---|
| cosine floors | nothing matches (too high) or everything does (too low) |
| entity-ubiquity damping | one busy entity dominates every match |
| junk gates and stop-sets | junk entities accumulate, or real ones are rejected |
| geocoder stop-sets | signals land in the wrong country |
| slice caps and time windows | evidence is dropped at the cap, or slices are empty |
| cadence and poll bounds | runs are skipped; polls stop before the end of the feed |

### Measure a distribution before setting a floor

`scripts/measure_claim_watch_cosines.py` is the example. It reads the same vectors, embedder and
SQL the runtime uses, writes nothing, and prints the score distribution of pairs that should match
beside pairs that should not. Put the floor where the two separate.

### Label a gold set

`scripts/sample_k4_match_worksheet.py` and `scripts/measure_entity_merge_quality.py` work in two
steps: `sample` writes a worksheet, a person labels it, `score` reads it back.

1. Sample by stratum. A uniform sample of a skewed population is mostly the common class.
2. Leave the label columns blank; the sampler never pre-fills them.
3. Label with a different model family from the one that wrote the rows, or by hand, with web
   checks where a claim is checkable.
4. Fill `labeled_by` on every row. Nothing enforces it.
5. Label a small overlap set twice and check agreement before the bulk run.
6. Write the acceptance bar down before you score.
7. If the bar is missed, do not build the feature that depended on it.

Faithfulness checks a claim against its cited evidence, not against the world. Only correctness
labels measure the second, and they only exist if someone makes them. A small weekly sample is
enough; report its size next to the number.

### Preflight a desk before activating it

`scripts/preflight_supply_chain_lanes.py` mirrors the slice reader, read-only, and reports how many
rows each candidate desk would admit inside its time window against the pre-filter cap. A desk that
overshoots the cap drops evidence without an error.

### Changing something that can lower quality

Before the change, record a baseline and write down the rollback rule. Watch every run after the
change, and keep a one-line rollback you have tried. When you repoint the faithfulness judge,
compare old and new against gold labels; the old judge's scores are not a reference.

### Waiting periods

| Layer | Needs |
|---|---|
| desk baselines | 28 days of history; quiet desks take longer because only active days count |
| band calibration | first resolutions at 14 days, second at 28 |
| source-health trends | weeks of poll history |
| source track records | contested facts that resolve, over months |

Before then, components that report `insufficient_history` or refuse to produce a band are
working as designed.

## 4. Data-quality checks

Run this read-only pass monthly and after every significant upgrade. Every finding needs
evidence: a row id, a count with its query, or a `file:line`.

1. **Source freshness.** Use the newest signal a source wrote. A `success` poll outcome also fires
   when a poll only collapses duplicates, so it does not prove freshness.

   ```sql
   SELECT source_id, max(fetched_at) AS newest_signal, count(*) AS n
     FROM signals GROUP BY source_id ORDER BY newest_signal;
   ```

   In `source_poll_outcomes`, `capped` means the poll stopped at its budget, and `newest_entry_ts`
   separates a quiet feed from a stuck cursor.
2. **Payload completeness.** Read one real row per source kind. Check the text is where the corpus
   indexer looks; structured feeds often nest it elsewhere.
3. **Enrichment.** Sample live rows per kind and check language, geography and entities. Country
   codes that collide with common words (`AL`, `PA`, `PR`) send headlines to the wrong place. Add
   each collision to the stop-set and to a test fixture.
4. **Facts and entities.** Read fifteen recent facts and check each is true of its sentence. Check
   that duplicate entity clusters are being adjudicated and junk entities are gated at write time.
5. **Cadence.** Compare each analyst's last run with its declared cadence:

   ```sql
   SELECT analyst_id, max(created_at) AS last_run
     FROM analyst_outputs GROUP BY analyst_id ORDER BY last_run;
   ```

6. **Receipts.** Read the newest receipt for each analyst: model, status, duration, tokens, tool
   calls and the per-rule counters.
7. **Container logs.** Sort each distinct error into transient (retried, recovered) or action
   needed.

**Count invocations.** A capability that is registered and granted can still have run zero times:

```sql
SELECT pack_id, count(*) FROM action_pack_invocations GROUP BY pack_id;
```

Investigate every zero.

**Source states.** Use one vocabulary:

- **dead**: no new content for a long interval. Retire it.
- **degraded**: errors or rate limits, still delivering. Keep it and note the error rate.
- **frozen upstream**: HTTP 200 and a current build date, but the same items for weeks. Only item
  identity or newest-entry dates over time catch it.
- **never started**: active with zero signals. Usually unfinished credentials or a wrong route.

**Junk and merges.** Sample recent entity profiles and add each new junk class to the gate with a
fixture. Clean the backlog with a migration, not an ad-hoc `DELETE`. Adjudicate duplicate entities
busiest first, and measure merge quality on a labelled sample: a recall gain must not cost
precision.

## 5. Gates

**The verify floor.** `effective_confidence = min(confidence, faithfulness_overall_score)`. Graded
faithfulness can only lower a finding's confidence. A finding below the floor stays readable and
attributed; it is demoted, not deleted. When the judge is off or unreachable, the result falls back
to the citation-presence floor and is labelled unavailable. Claim kinds exempt from the floor (BLUF
lines, corpus-scoped absence findings, synthesis claims) are named, each with its own rubric.

The composition floor defaults to `0.50` (`DEFAULT_VERIFY_FLOOR`); set it explicitly with
`LEGBA_COMPOSITION_VERIFY_FLOOR`.

**Receipts.** Each filter, cap and damping rule writes a per-run counter to the run receipt: how
many candidates it saw, admitted and dropped. Model calls record model, status, duration, tokens and
prompt hash.

**Changes.** A threshold change is a measurement: record what you measured and when. A prompt change
is a new prompt version; every record carries its version, so scores from two versions are never
pooled. Gold labels belong to the configuration that produced the rows.

**Missing dependencies.** Fail at activation with an error that names what is missing, or pass the
row through un-enriched and mark the health state. Code added to an instance follows the project's
rule: either built, or a declared seam in [SEAMS.md](SEAMS.md) that fails loudly.

Keep a written list of what your instance does not do well, and review it.

## 6. What to expect when

| When | Happens by itself | Needs you |
|---|---|---|
| Day 0 | Stack boots. Without model endpoints, feeds ingest, deduplicate and index, and no analysis is produced. | Model endpoints, the vault, a seed baseline, a small source set, the geocoder contact, the verify floor. |
| Week 1 | Signals, entities and vectors accumulate; first findings within hours of the model endpoints working; derived layers report insufficient data. | First poll-health read and retirements; payload and enrichment checks; corpus licence list; first threshold measurements. |
| Month 1 | Supersession history, contention rows, decay states, first band calibrations at 14 days, baselines eligible at 28. | Weekly gold labels; re-measured thresholds committed; the first full check (§4); retrieval on one unit with a control. |
| Quarter | Baselines populated, calibration at both horizons, source-health trends, track records separating sources. | Thresholds measured on your data; a gold set large enough to decide with; routine merge review; the weakness list. |

## 7. The models host

The chat model, the embedder and the `legba-models` NLP service run outside the platform's
containers and are reached over HTTP ([AI_MODELS.md](AI_MODELS.md)). If you host them yourself,
the host is yours to run. Two things in the tree belong there and `deploy.sh` does not install them:

- `deploy/cron.d/models-host-saturation-watch` with `scripts/models_host_saturation_watch.sh`: a
  vLLM saturation watch for the models host's cron (install steps in the cron file's header).
- `scripts/models_host_vllm_watchdog.sh`: a liveness watchdog for the same host.

From the app host, the LLM heartbeat probe checks the endpoints ([TUNABLES.md](TUNABLES.md) §5).
Register each endpoint as a stack component with its credentials in the vault. Changing a model is a
registry update; re-measure (§3) before you rely on it.

## 8. The MCP surface

A stdio MCP server ships as the `legba-mcp` entry point, so an MCP-capable client can read the
substrate and run the sanctioned consult path without the console. Seven built-in tools wrap the
registry over HTTP: findings, situations, signals, a lineage walk, the since-cursor diff, export, and
consult. Reads and consult only; no registry mutation rides MCP, and a test asserts it. An analyst
descriptor can surface one more tool by declaring an `outputs.mcp_tool` binding, registered at
activation and removed at retire.

Setup: the server needs the registry reachable at `LEGBA_REGISTRY_URL` and a bearer in
`LEGBA_REGISTRY_API_TOKEN` that is allowed the routes the tools call ([API_ROLES.md](API_ROLES.md)).
Consult blocks for up to five minutes while its tool loop runs. Build the image with
`docker compose --profile mcp build`, then point the client at it per conversation:

```bash
docker run -i --rm --network=legba_default --env-file=.env legba/legba-mcp:latest   # from the deploy directory
```

The network flag lets the container reach the registry host named in the URL. The host-mode
alternative is `PYTHONPATH=src python -m legba.ui.mcp_server`. Logging goes to stderr so stdout stays
clean for the protocol; every tool fails loud, returning a described error for a registry error, a
`registry_unreachable` object for a transport failure, and the available-tool list for an unknown
tool. Transport is stdio only, and the descriptor-declared catalog is empty in a standalone process:
only the built-ins survive without a shared runtime.

## 9. Turning on sign-in

Out of the box the edge (`legba-caddy`) asks every browser for one perimeter password and then hands
it the shared admin token, so every person is the same admin. One switch, `LEGBA_AUTH_SIGNIN`, turns on
real accounts. It is off by default, and while it is off nothing below changes anything.

With `LEGBA_AUTH_SIGNIN=1`:

- **People sign in.** Local accounts, passwords stored as argon2id, a session held in an
  `HttpOnly; Secure; SameSite=Lax` cookie (72 hours at most, ended after 12 idle hours, set with
  `LEGBA_SESSION_IDLE_HOURS`). A write sent from another site with that cookie is refused. The edge
  no longer hands any browser a token: a browser without a session reaches the registry with no
  credential and the console shows its sign-in screen.
- **Three roles.** The owner is an admin who cannot be demoted or disabled through the API. Every
  route's minimum role is listed in [API_ROLES.md](API_ROLES.md) under "With sign-in on".

  | Role | Can | Cannot |
  |---|---|---|
  | `reader` | read every reader surface (the Board, subjects, the Ledger, Watch, the Record sheet) and the engine figures those surfaces print; ask Consult and keep their own Consult sessions, watch list, read receipts and exports; use the phone shell | change anything shared: no grading, no decisions, no alert acknowledgement, no source, descriptor or user change, no Consult turn that writes; see the operator side, another person's Consult sessions, the private source-rating annex or unit-correctness claim spans |
  | `contributor` | everything a reader can, plus grading labels and the gold-set worksheet, journal proposal decisions, forecast outcome decisions in the resolution queue, and acknowledging or snoozing alerts | change configuration (sources, descriptors, the stack, the vault, users) or make a Consult turn that writes (SEAMS #86) |
  | `admin` | everything: sources, descriptors, the stack, the vault, users, curation, Consult-driven changes, and the engine's own surfaces | demote or disable the owner |

  The console reads the role from `GET /api/v1/auth/me`: a reader is offered no way across to the
  operator side, and a control that needs a higher role says which role instead of sending a request
  that will be refused. The registry enforces every route either way.
- **Every write names who made it.** Where a table has an author column (`watchlist.created_by`,
  `collection_requirements.reviewed_by`, the label tables' `labeled_by`, `source_credibility.scored_by`,
  the optimizer review's reviewer, the audit log's actor), the server records the signed-in person
  (`user:<id>`) or the service; a name in the request body is ignored.
- **Every judgement row names its writer, switch on or off.** The tables that hold labels,
  decisions, descriptor versions, watch lists and entered knowledge carry `written_by`, a reference
  to a row in `principals`: a person (`user:<id>`), a service (`service:<name>`), an analyst seat
  (`agent:<descriptor>@<version>`), a deterministic rule (`rule:<name>`), or the shared admin
  credential (`shared:admin-token`). With the switch off every person writes as
  `shared:admin-token` and the author columns above keep the name the request gave, exactly as
  before. Principals are created on first use, never by hand. Which tables carry the column, and
  which already name their writer another way, is declared in `src/legba/data/write_attribution.py`
  ([TYPES.md](TYPES.md), "Principal kinds"); `scripts/report_written_by.py --stored` counts them.
- **Each service has its own credential.** The registry, the runtime, the workflow worker and the
  console each read `LEGBA_SERVICE_TOKEN_REGISTRY`, `_RUNTIME`, `_WORKER` and `_CONSOLE`, and are
  recorded as `service:<name>`. The stock console needs none: its edge forwards the person's
  session. The shared `LEGBA_REGISTRY_API_TOKEN` keeps working as a break-glass credential; every
  use is logged at WARNING (`registry.auth.break_glass`).

- **The perimeter can go.** With sign-in on, `LEGBA_EDGE_PERIMETER=0` drops the edge's perimeter
  password from the console, so the registry's sign-in is the only gate. The default (`1`) keeps the
  password as an outer wall in front of sign-in. The edge refuses to start on `0` with sign-in off,
  because then it would hand the admin token to anyone. The ntfy site keeps its password whatever
  this says.
- **The phone shell installs.** With sign-in on, the edge serves the phone shell's manifest, worker
  script and icons without the perimeter password, so `/m/` can be installed from the browser as a
  web app ([UI.md](UI.md#the-phone-shell)). Every page and API route keeps its gate.

### Turn it on

1. **Apply the migration** (0255 adds the owner flag, the three role names and the idle clock):
   `docker compose exec legba-registry python -m legba.data.migrate`.
2. **Create the owner, once, on the host.** The command asks for the password twice without echo;
   nothing secret is passed on the command line, written to the repository or logged:

   ```bash
   docker compose exec legba-registry python -m legba.data.users_bootstrap --email you@example.org --name "Your Name"
   ```

   It refuses if an owner already exists.
3. **Give each service its own credential.** Generate one random value per service (for example
   `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`) and put it in `.env` (or the
   vault you load `.env` from) under the names `LEGBA_SERVICE_TOKEN_RUNTIME`,
   `LEGBA_SERVICE_TOKEN_WORKER` and `LEGBA_SERVICE_TOKEN_REGISTRY`. The registry must hold all of them
   (it checks them); each service presents only its own. A service whose own key is unset falls back
   to `LEGBA_REGISTRY_SERVICE_TOKEN` and says so at startup (`principal=shared`).
4. **Flip the switch.** Add `LEGBA_AUTH_SIGNIN=1` to `.env`, then recreate the registry first, wait
   for it to be healthy, then the runtime, the workflow worker and the edge (`legba-caddy` reads
   the switch to stop handing out the admin token).
5. **Check.** Open the console: the sign-in screen appears; sign in as the owner; the identity chip
   shows your name, `admin` and `owner`. `GET /api/v1/auth/me` with each service token answers
   `kind: service` and its name. The registry log shows no `principal=shared` line and no
   `registry.auth.break_glass` line from a service.
6. **Invite people.** The console has no user-management screen; an admin calls the API. From the
   host, with the break-glass token (logged on every use):

   ```bash
   curl -s -X POST -H "Authorization: Bearer $LEGBA_REGISTRY_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"email":"someone@example.org","role":"reader","display_name":"Someone"}' \
     http://127.0.0.1:8090/api/v1/auth/invite
   ```

   The answer carries `invite_path` (`/#invite=<token>`) once; send the person your instance's address
   with that fragment, and they choose a password on the invitation screen. Each link works once.
   `GET /api/v1/auth/users` lists accounts; `PATCH /api/v1/auth/users/{user_id}` changes a role or
   disables an account, which ends every session it holds.
7. **Optionally drop the perimeter.** Once sign-in works for everyone who needs it, set
   `LEGBA_EDGE_PERIMETER=0` and recreate the edge.

### Turn it off

Set `LEGBA_EDGE_PERIMETER=1` (or remove the line) first if you had dropped it, then set
`LEGBA_AUTH_SIGNIN=0` (or remove the line) and recreate the registry, the runtime, the worker and the
edge in the same order. The edge injects the admin token again, the per-service tokens stop being
accepted (services fall back to `LEGBA_REGISTRY_SERVICE_TOKEN`), and the single shared credential
returns. Accounts, sessions and the owner flag stay in the database, unused; turning the switch on
again picks them up. The switch takes only `0` or `1`; the edge refuses to start on any other value.

What this step does not do yet — groups, single sign-on, per-source and per-set permissions, a
contributor's own Consult changes, narrowed service credentials — is listed in
[SEAMS.md](SEAMS.md) #83–#87.

See also: [RUNBOOK.md](RUNBOOK.md) · [DATA_SOURCES.md](DATA_SOURCES.md) ·
[MANUAL_INGEST_FORMAT.md](MANUAL_INGEST_FORMAT.md) · [SEAMS.md](SEAMS.md)
