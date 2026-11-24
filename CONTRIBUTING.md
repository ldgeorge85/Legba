<!--
SPDX-FileCopyrightText: 2026 Lewis George
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Contributing to Legba

For anyone changing Legba's code, descriptors or docs. It covers what to check before writing
code, the rules a change must follow, the gates that enforce them, and how to run the tests.

## Before you start

Open an issue before writing anything large.

- An outside code contribution needs a contributor licence agreement (CLA). Ask for it before you
  write code; it is not automated.
- Bug reports, reproductions and docs corrections need no CLA.

Everything here is AGPL-3.0-or-later ([LICENSE](LICENSE)). Every source file carries SPDX headers,
and new files must too:

```python
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
```

## Is code the answer?

Sources, subjects (targets), analysts, stack components and action packs are YAML descriptors,
registered into Postgres at deploy time. The running set is the `*_descriptors` rows in the
database, not the files. Most additions are a descriptor:

- A feed of an existing kind (`rss`, `json_api`, `geojson` and others): a source descriptor, or
  an entry in the profile's source catalog. See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).
- A subject: a target descriptor. Analysts pick it up through their predicates.
- An analyst of an existing kind: an analyst descriptor.

`scripts/bringup_register_descriptors.py` registers every descriptor file of the profile at
deploy. Code is needed for a new source kind, a new analyst kind, a new deterministic
sub-handler, or a change to the runtime or provenance planes. [docs/CODE_MAP.md](docs/CODE_MAP.md)
says where things live.

## Descriptor rules

- A descriptor body is strict (`extra="forbid"`), with a `schema_uri` of the form
  `legba/<family>/<major.minor.patch>`. Its version is the content hash of the body. Add new
  fields to the model; do not put them in an open dict.
- The YAML file under `descriptors/` is the only copy of the body. Registrar scripts load the file.
- A descriptor that needs checking on a live instance ships `identity.state: draft`. Registering a
  draft creates no actor; an admin activates it. Do not ship something `active` that you could not
  verify.
- Every descriptor sets `budget_tokens_per_day: 0`, and every LLM route sets `temperature: 1.0`.
- A secret is a vault reference (`Property.Secret`). Plaintext never goes in a descriptor.
- Do not put counts in comments or step labels. They go stale; the registrars print their own.

## The four gates

These run in the nightly suite (`scripts/host_nightly_suite.sh`). The first three also run in CI
(`.github/workflows/ci.yml`).

### 1. No stubs: declare the seam

Anything not built is a declared entry in [docs/SEAMS.md](docs/SEAMS.md) and fails with an error
when reached. Never a silent no-op, a made-up default, or an empty list where an answer belongs. A
search provider with no key raises; it does not return zero results.

`tests/test_no_undeclared_stubs.py` scans `src/legba/**` for stub markers: `NotImplementedError`
raised with a message or subclassed, names containing `stub`, `placeholder`, `fake`, `mock` or
`dummy`, `mock` imports, and `return {}` next to a TODO. Every hit must be the bare abstract-method
idiom or listed in the allowlist between the `BEGIN/END SEAM ALLOWLIST` markers in SEAMS.md.
There are no per-line escapes. Adding a seam means two edits to SEAMS.md, the row and the allowlist
line; removing the code means removing both.

### 2. Tests go through the real binding

A test that builds the object under test by hand shows the object works, not that anything is
wired to it. Tests use the production path: the descriptor parsed by the real registry, the
handler resolved by the real dispatcher, a tool call landing a real `action_pack_invocations` row
(see `tests/data_pkg/agency/test_agency_binding.py`). A new route is tested through its router
factory and deps bundle, not by calling the handler function.

Skips caused by missing infrastructure fail by default: `LEGBA_TEST_STRICT` is strict
(`tests/conftest.py`). Set `LEGBA_TEST_STRICT=0` locally if you need to; never in a committed
default.

`deploy/deploy.sh` runs `scripts/deploy_smoke_cold_activation.sh` after a deploy for the same
reason: in-process tests cannot show that a descriptor parses, activates and runs on a live stack.

### 3. Module size: extract, do not raise

`tests/test_module_size_gate.py` pins a line ceiling on each `src/legba` module that was already
1,500 lines or more. It fails when a pinned module passes its ceiling, an unpinned module crosses
1,500 lines, or a ceiling no longer constrains its file.

If your change breaks a ceiling, move a coherent part into a sibling module, re-export the moved
names, and lower the ceiling in the same commit. Raising a ceiling is one visible line in the
diff and needs its reason in the commit message.

### 4. The ruff ratchet

`[tool.ruff]` in `pyproject.toml` passes on the tree as it stands. The selected rule families are
correctness rules (pyflakes, bugbear, async, logging, return paths). Rules the tree still breaks
are listed in `ignore`.

- Adding a rule is always safe.
- Removing an `ignore` entry takes its own cleanup commit.
- Do not run a broad `ruff check --fix`; a large reformat conflicts with every open branch.

## Code rules

### The registry image

`docker/Dockerfile.registry` installs an explicit dependency list
and the source tree without its declared dependencies. A module the registry serves may import
leaf modules only: a route that pulls in a runtime package answers 500 on its first request, even
when the import is inside a function. Each new `*_api.py` ships with a test that guards its
imports.

### Migrations

Migrations are SQL files in `src/legba/data/migrations/`. Create one with
`python3 scripts/new_migration.py <slug>`, which names it `YYYYMMDDTHHMM_<slug>.sql`. The numbered
files are closed. The runner applies the numbered files in order, then the time-named ones, and
records each by name and hash; never edit a migration that has shipped.

- Use additive DDL (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`).
- Run a backfill's counting half first and put the counts in the migration header. A backfill
  with a ceiling refuses above it. A row the backfill cannot resolve is parked with a reason, never
  dropped.

### Flags

A behaviour change ships behind a flag that is off in code and read in one place. Flags
are `0`/`1`, unset means off, and a malformed value falls back to the default. A regime marker is
read from the row, not from the environment, so old rows keep the meaning they were written
with. To change a shipped default, keep the old behaviour as the default, add the new one behind a
descriptor option, and test both.

### The write path

Every typed analyst output goes through one wrapper. It validates the payload,
sends an invalid one to the output dead-letter table, stamps provenance, inserts, and publishes. Do
not add a second writer; a new output kind is a new enum member, a payload model and a registry
entry. A fact or relation write closes the open prior row whose value differs. The journal writes
no facts, findings or relations. Append-only tables are enforced by a database trigger, and a
closed vocabulary is a `CHECK` constraint.

### Predicates

Predicates on target scope, source filters, analyst subscriptions and cadence triggers are
single Starlark expressions (`def`, `load`, `lambda`, `while`, `import` and top-level `for` are
rejected). They are compiled once at registration, with a wall-clock limit at evaluation. A
predicate that does not compile sends its descriptor to the dead-letter queue.

### Banned dependencies

`dspy` and `litellm` may not enter the runtime image or the analyst path.
They ship only in `docker/Dockerfile.worker`, and `tests/test_runtime_no_dspy_litellm.py` enforces
it. Model calls go through Legba's own provider handler, resolved from a stack component. Any other
new dependency needs a stated reason.

### Security boundaries

- Secrets are stored encrypted in the credential vault (XSalsa20-Poly1305) under a master key from
  the environment, never in an image layer.
- Every descriptor state change appends an Ed25519-signed audit row in the same transaction. If the
  audit write fails, the change fails.
- Each analyst's runs form a SHA-256 hash chain. It detects an inconsistent re-hash on one node; it
  is not a distributed or tamper-proof guarantee.
- With the registry token unset, guarded routes answer 503 unless development mode is set.
- Tools are granted, not assumed. See [docs/AGENCY_GATING_MODEL.md](docs/AGENCY_GATING_MODEL.md).

### Deployment

One profile-gated `docker-compose.yml`. Only the Caddy edge listens on all
interfaces; every other port binds loopback. The worker image is built from the runtime image, so
a runtime change means building both. A deploy recreates the registry, waits for it to be healthy,
runs migrations, and only then recreates the runtime and the worker.

## Naming conventions

| Thing | Pattern | Example |
|---|---|---|
| Analyst output subject on NATS | `analyst.<analyst_id>.<channel>` | `analyst.cross_target_energy_correlator.findings` |
| Dead-letter subject | `legba.dlq.<namespace>.<id>` | `legba.dlq.output.<kind>.<analyst_id>`, `legba.dlq.descriptor.<family>.<id>` |
| Migration file | `YYYYMMDDTHHMM_<slug>.sql` | `20261115T1828_citations_as_rows.sql` |
| Descriptor schema | `legba/<family>/<major.minor.patch>` | `legba/analyst/1.0.0` |

## Running the tests

The suite runs in a container (the host's pytest cannot collect it):

```bash
bash scripts/run_tests_in_container.sh                   # the whole suite
bash scripts/run_tests_in_container.sh tests/data_pkg    # a subset
```

In a git worktree, point the runner at your tree and set `PYTHONPATH=src`, or the editable install
imports the main checkout:

```bash
LEGBA_REPO_ROOT="$PWD" bash scripts/run_tests_in_container.sh tests/...
```

Two families fail only in a worktree: `tests/data_pkg/test_dockerfiles_build_clean.py`, which
expects the main checkout's path, and the seed tests, which need seed data that is not in the
repository.

The full suite takes about half an hour. Run targeted paths while you work. The runner holds
`/tmp/legba-container-tests.lock`, so a second run waits for the first.

### Host-side database tests are not supported

Tests that need Postgres run only on the isolated test stack (`deploy/compose.test.yml`), which the
runner starts. A deployment's own Postgres is on `127.0.0.1:5432`, the same address the tests use,
so `tests/_pg_test_server_guard.py` refuses any other server:

- every connection requires `LEGBA_TEST_PG_ISOLATED=1`, which only the runner sets;
- the server must show it is a test server: `legba.test_stack` is `on`, `fsync` is `off`, and no
  database is larger than `LEGBA_TEST_PG_MAX_DB_BYTES` (3 GiB).

Unit tests that need no database run on the host with `PYTHONPATH=src python3 -m pytest <path>`.
Anything that needs Postgres fails there with a `REFUSED Postgres ...` message.

### What CI covers

CI runs the ruff ratchet and the structural gates (module size, no stubs, the banned-dependency
check, strict mode). It cannot run anything that needs Postgres, Qdrant, OpenSearch, NATS or a Dapr
sidecar, which is most of the suite. A green CI badge means the tree is lint-clean and passes the
structural gates. Say in your pull request which tests you ran.

## Documentation

- Counts come from generators, not from hand. `docs/RELEASE_STATE.md`, `docs/TYPES.md`,
  `docs/API_ROLES.md` and the panel table in `docs/RELEASE_STATE_MATRIX.md` are generated; do not
  edit them by hand.
- If your change makes a sentence in the docs untrue, fix the sentence in the same commit.
- Something deliberately not finished goes in [docs/SEAMS.md](docs/SEAMS.md) (not built) or
  [docs/STATUS.md](docs/STATUS.md) (built, with a limit), not in a code comment.

## Ticket and decision ids in comments

Some source comments cite short internal ids (`L-205`, `D-3`). They cannot be resolved outside the
project; read the comment's own words. A new comment should explain itself without an id.

## Commits and pull requests

- Conventional-commit subjects in lower case: `feat(verify): …`, `fix(deploy): …`, `docs(readme): …`.
- Say in the body what you checked, what moved, and what you left alone on purpose.
- One concern per commit.
- No AI-attribution trailers. The copyright line is the SPDX header.
- Never commit secrets, `.env`, anything under `planning/`, or seed data under `seeds/`. They are
  gitignored.

## Likely to be rejected

- A new dependency without a reason it cannot be avoided.
- A stub, a silent fallback, or a default that makes up a number when the real one is missing.
  Degrade with a label, or fail.
- Raising a module-size ceiling to fit a feature.
- Tests against a hand-built object instead of the wired path.
- Changing a shipped default without a switch that keeps the old behaviour.

## Contact

legba@civislux.us, or open an issue on the [repository](https://github.com/ldgeorge85/legba).
