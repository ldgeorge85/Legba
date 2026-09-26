<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Design — the implementation contract

The rules a change to Legba follows, and the reason each one exists. Most of
them are enforced mechanically, which is the point: a rule nobody can skip does
not need to be remembered.

`ARCHITECTURE.md` owns the shape of the system and why it has that shape. This
document owns how code is added to it. `CODE_MAP.md` says where everything
lives; `CONTRIBUTING.md` is the short version of this page for an outside
contributor.

**Contents:**
[1 Descriptor first](#1-descriptor-first--is-code-even-the-answer) ·
[2 The four gates](#2-the-four-gates) ·
[3 The slim registry](#3-the-slim-registry-image) ·
[4 Migrations](#4-migrations) ·
[5 Flags and defaults](#5-flags-and-defaults) ·
[6 Descriptor conventions](#6-descriptor-conventions) ·
[7 The write path](#7-the-write-path) ·
[8 Predicates](#8-predicates) ·
[9 Banned dependencies](#9-banned-dependencies) ·
[10 Security boundaries](#10-security-boundaries) ·
[11 Deployment conventions](#11-deployment-conventions) ·
[12 Documentation](#12-documentation-is-part-of-the-change) ·
[13 Running the tests](#13-running-the-tests)

---

## 1. Descriptor first — is code even the answer?

Legba is descriptor-driven, so most additions are not code:

- **A new feed** of an existing kind (`rss`, `geojson`, `json_api`, …) is a
  descriptor plus a registrar entry.
- **A new country desk or region frame** is a target descriptor; the whole spine
  — units, composition, scorecard — picks it up by predicate.
- **A new analyst** of an existing kind is a descriptor plus an entry in the
  analyst file list.
- **Code** is the answer for a new *source kind*, a new *analyst kind*, a new
  deterministic sub-handler, or a change to the runtime or provenance planes.

Reusing an existing analyst kind is the cheap path on purpose: every integration
keyed on kind — the verify dispatch allowlist, the judge sampling default, the
findings route — stays a no-op, and no file at its module-size ceiling has to be
touched to make room for a clause.

## 2. The four gates

These run in the nightly suite; the first three also run in CI, because they
need nothing but the source tree. Each exists because the tree lost a specific
argument without it.

### 2.1 No stubs — declare the seam or do not ship it

Anything not built is a **declared entry in `SEAMS.md` that fails loud**. Never
a silent no-op, never a fabricated default, never an empty list where an answer
belongs.

`tests/test_no_undeclared_stubs.py` scans all of `src/legba/**` for stub markers
— a `NotImplementedError` raise carrying a message, a `NotImplementedError`
subclass and every raise of it, a definition whose name contains the word
segments `stub` / `placeholder` / `fake` / `mock` / `dummy` (plus `echo` for
classes), a `mock` import, a `return {}` next to a TODO — and asserts every hit
is either the bare abstract-method idiom or listed in the machine-readable
allowlist between the begin and end markers in `SEAMS.md`.

There are deliberately **no per-line escapes**. The only way to ship a
stub-shaped symbol is to register it as a seam, where it carries a what, a why
and a guard rail that a reviewer can see. A change that adds one and cannot
write that entry honestly is not ready. Adding an entry means two edits in
`SEAMS.md`: the narrative row and the allowlist line. Removing the code must
remove both — the test also fails on a stale allowlist line.

The guard rail is the substance of the entry. A seam refuses activation or
raises at its edge; it does not degrade into something that looks like an
answer. A search provider with no key raises rather than returning zero results,
because an empty result set is what a reader takes for "nothing exists".

### 2.2 Tests traverse the real binding path

A test that hand-builds the object under test proves the object works. It proves
nothing about whether anything is *wired* to it. The house rule is that a test
exercises the production binding: the descriptor is parsed by the real registry,
the handler is resolved by the real dispatcher, a tool call lands a real
invocation ledger row.

The cost of ignoring this is on the record: a descriptor-parse bug took the
fleet down while every one of some ten thousand tests stayed green, because they
all built descriptors in process and none traversed registry fetch, parse,
activate and run against a live sidecar. A cold-activation smoke step in the
deploy script is the scar tissue.

Two corollaries:

- **Every new route is exercised through the router factory and the deps
  bundle**, not by calling the handler function directly. For a new pack tool,
  the equivalent is a test that goes through the agency binding, the pack
  dispatch and the registered handler, so the invocation ledger actually records
  it.
- **Infra-gated skips are failures by default.** `LEGBA_TEST_STRICT` defaults to
  strict, so a test that skips because Postgres or a sidecar is missing fails
  instead of silently shrinking coverage. Opt out locally with
  `LEGBA_TEST_STRICT=0`; never in a committed default.

### 2.3 Module size — extract, do not raise

`tests/test_module_size_gate.py` pins a line ceiling on each module in its list
— seeded from the `src/legba` modules already at or past 1,500 lines, plus about
ten percent headroom so ordinary maintenance fits — and fails on three
conditions: a pinned module over its ceiling, an unpinned module crossing the
1,500-line entry threshold, and a ceiling that has stopped constraining its file
because a real extraction landed and the number was not re-seeded.

The gate exists because a decomposition can be completely undone by nothing more
than ordinary growth: a module that had two thousand lines extracted out of it
regrew past its post-extraction floor within weeks, because nothing in the tree
noticed the file getting bigger again.

The fix for a breach is to **extract a cohesive unit into a sibling module and
re-seed the ceiling downward in the same commit**. The section banners in these
files are already the author's own seams, and a split that re-exports the moved
names is invisible to every importer. Raising a ceiling is possible — it is one
visible, reviewable line in the diff, which is the entire mechanism — but it
needs its reason in the commit message, and "my feature did not fit" is not one.

### 2.4 The ruff ratchet

The ruff configuration in `pyproject.toml` is green on the tree exactly as it
stands. The selected families are correctness-shaped — pyflakes, bugbear, async,
logging, return paths — and every rule the current tree violates is parked in
`ignore`, which is the debt ledger.

- Adding a rule is always safe: it fires on nobody.
- Removing an `ignore` is a deliberate act that costs its own cleanup commit.
- A broad autofix sweep is not allowed. A five-figure-line reformat collides
  with every branch in flight, which is why the ignore list exists at all.

## 3. The slim registry image

The registry ships a deliberately minimal image: an explicit dependency list in
`docker/Dockerfile.registry`, installed into a clean slim base, with the source
tree installed without its declared dependencies so the heavy runtime sets never
arrive. No compiler toolchain reaches the runtime layer.

That thrift has one sharp consequence, and it is a rule:

**A registry-served module imports leaf modules only.** A route module that
pulls a runtime package — which transitively drags a dependency the registry
image does not carry — returns a 500 live even when the import is deferred
inside a function, because the import still executes on the first request. Every
new `*_api.py` ships with a poisoned-import guard test in the same commit, and
the shared deps bundle lives in its own leaf module for the same reason.

The failure this forecloses actually happened: one missing package meant the
registry process alone could not import a geocoding module that the analyst
options catalog pulls in, so the typed-descriptor route answered 500 for exactly
the analysts that carry options — and only those — while every health probe
stayed green. The dependency list carries a comment at that line saying so; keep
it and the runtime image's matching pin in lockstep.

## 4. Migrations

Migrations are SQL files in `src/legba/data/migrations/`, discovered by a sorted
glob over `*.sql`. There is no manifest and no head constant, so:

- **The filename is the ordering.** Use the number reserved for your change;
  another lane may be holding the next one.
- **Gaps are harmless.** An unused number breaks nothing, so a reserved-then-
  abandoned slot is simply left empty rather than renumbered.
- **Additive DDL.** `CREATE TABLE IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`
  leave every existing row untouched and unmarked, which is what makes a
  flag-gated feature land dark.
- **A backfill is dry-run first.** Run the counting half, put the measured
  numbers in the migration header, then commit the writing half. A backfill with
  a ceiling refuses above it and reports its counts rather than writing.
- **Park, never drop.** A backfill candidate it cannot resolve is parked with
  its reason, not discarded.

A cold start applies the proven baseline and then every migration after it, up
to whatever head the checkout carries. `RUNBOOK.md` owns the procedure.

## 5. Flags and defaults

A behaviour change ships behind a flag that is **off in code**, and the flag is
**read in exactly one place**. Both halves matter: a default that changes
behaviour on upgrade is not a default, and a flag read in three places is three
flags that will eventually disagree.

Three conventions hold:

- **The house shape is boolean `0`/`1`**, with unset meaning off, and the
  deployment turns a regime on rather than the code shipping it on.
- **A regime discriminator reads the row, not the environment.** A row written
  under a regime keeps that regime's semantics forever; reading the current
  environment to classify a historical row retroactively re-labels history,
  which is how populations silently pool across a semantics change.
- **A malformed value falls back to the default.** A typo can never silently
  disable a budget or a guard.

Preserving today's behaviour by default is also the rule for a behaviour change
an operator might not want: implement the alternative behind a descriptor
option, test both paths, and document the recommendation.

## 6. Descriptor conventions

A descriptor body is strict — `strict=True, extra="forbid"` — with a
`schema_uri` of the form `legba/<family>/<major.minor.patch>` and a `version`
that is the content hash of the body. New structured fields are declared on the
model rather than smuggled into an open payload dict.

**The YAML file under `descriptors/` is the reviewable home of the body, and it
is the only one.** A registrar script loads the file rather than carrying a
second copy of the config, so the tree and the registrar cannot drift. The live
set is still the database rows.

**A descriptor that needs operator verification ships `identity.state: draft`.**
Bulk registration of drafts creates no actor; activation is a deliberate
per-descriptor act. Do not ship something `active` that you could not verify.

**Every descriptor carries `budget_tokens_per_day: 0`, and every declared LLM
route carries `temperature: 1.0`.** The budget is zero because the scheduled
fleet runs on a plane that costs nothing per token, and metering a free plane
only creates a way for work to stop. The temperature is fixed because a sampled
default is one more unmeasured variable sitting between a prompt change and its
score. The self-hosted core handler does not send `max_tokens` at all unless a
caller explicitly opts in: the server serves its own budget, and a ceiling
applied from outside truncates a read rather than saving anything. Grep a new
descriptor for the budget and the temperature at the gate.

**A secret is a vault reference.** Descriptor config uses the named property
factories — `Property.Secret`, `Property.StackRef`, `Property.Cron`,
`Property.Dropdown` and the rest — so the runtime can render a form, validate a
value, store a credential by reference and resolve a dynamic option without each
descriptor reinventing them. Plaintext never enters a descriptor body.

**A count in a comment drifts.** Registrar and deploy step labels name the step
and leave the counting to the registrars, which print one line per descriptor
they touch, and to the summary, which reads counts back out of the database.

## 7. The write path

Every typed analyst output goes through one wrapper. It looks up the kind's
table, payload model, schema URI and subject; validates; routes an invalid
payload to the output dead-letter table rather than aborting the run; stamps the
universal provenance; inserts; and publishes best-effort, because a broker
hiccup must not lose a write.

**Do not add a bypass.** A second write path is a second place for provenance to
be forgotten, and the exceptions that already exist are counted and justified.
A new output kind is a new enum member, a payload model and a registry entry —
not a new writer.

Two invariants ride the write path and are enforced by tests rather than by
convention:

- A fact or nexus write closes any open prior row whose value differs, stamping
  the validity window and the supersession pointer, so the single open row per
  assertion is "what is true now" while the history accrues.
- The journal writes no fact, finding or nexus, and its table stays out of the
  lineage catalog. Both halves are enforced — one by a gating test, one by the
  grant layer, since neither pack the journal holds can write a fact.

An append-only ledger is append-only **at the database**: a trigger makes update
and delete fail loud. An attention record or a lifecycle ledger you can quietly
revise is not evidence. Where a vocabulary must not drift, it is a `CHECK`
constraint, so a typo in an emitter cannot silently create a kind no reader
counts.

## 8. Predicates

Predicates appear on four surfaces — target scope, source filter, analyst
subscription, cadence trigger — and are Starlark through a Rust binding.

- **Expression-only.** A predicate is a single expression; `def`, `load`,
  `lambda`, `while`, `import` and top-level `for` are rejected at compile.
  Comprehensions remain available.
- **A fixed helper catalog**, versioned, with each helper tagged by the surfaces
  it is bound on.
- **Compiled once at registration**, cached in a thread-safe LRU keyed by source
  hash, surface and catalog version.
- **A wall-clock budget at evaluation.** Per-evaluation step and memory caps are
  not exposed by the current binding; the expression-only gate and the wall-clock
  cap are the live guards, and that limitation is stated rather than implied.

A compilation failure routes to the descriptor dead-letter queue; schema
validators compile-check every predicate field at registration time, so a broken
predicate cannot reach an actor.

## 9. Banned dependencies

`dspy` and `litellm` are hard-banned from the runtime image and the analyst hot
path. They ship only in the optimizer worker image, and a test enforces the
boundary. Every model call in the analyst path goes through Legba's own provider
handler, resolved from a stack component — which is what keeps the model layer
swappable by descriptor and keeps one place responsible for budget, retry and
provenance stamping.

Any other new dependency needs a stated reason it cannot be avoided.

## 10. Security boundaries

- **Credential vault.** Secrets live encrypted in their own table
  (XSalsa20-Poly1305), decrypted with a master key supplied by environment and
  never baked into an image layer. Descriptors carry references only.
- **Signed descriptor audit log.** Every descriptor state change appends an
  Ed25519-signed row in the same transaction as the mutation, and the audit
  route verifies inline. An audit-write failure aborts the change.
- **Receipt chains.** Each analyst advances a SHA-256 hash chain over the
  canonical JSON of its runs. This is a single-node integrity chain: it detects
  an inconsistent local re-hash, and it is not a signed, distributed or
  tamper-proof guarantee — the badge says so. Chain heads are checkpointed, and
  those checkpoint rows are the signed artifact.
- **Dead-letter queues everywhere.** Descriptor validation failures, invalid
  output payloads and retry-exhausted publishes route to dead-letter surfaces
  with resubmit paths rather than aborting.
- **The API fails closed.** With the registry bearer token unset, every guarded
  request answers 503 "service misconfigured" unless development mode is
  explicitly set. An unauthenticated open surface is never the fallback.
- **Agency is granted, never assumed.** `AGENCY_GATING_MODEL.md` owns the trust
  model for tools; the short version is that capability is the intersection of
  the analyst's grant, the target's permit and the pack's applicability, the
  operator holds the permit leg, and a governor bounds what an effective pack may
  spend.

## 11. Deployment conventions

One `docker-compose.yml`, profile-gated. The substrate services carry no profile
and come up with a bare `up`; the `runtime` profile is the canonical bring-up
and transitively activates the Dapr control plane and the console; optional
sidecars each sit behind their own profile and are off unless an operator turns
them on.

- **The edge is the only service that publishes on all interfaces.** Every other
  host port binds loopback. The network perimeter is the security boundary, so
  it has exactly one door.
- **Secrets arrive by env file**, kept out of image layers.
- **The worker image builds from the runtime image**, so a change to the runtime
  means both profiles build, or the worker silently runs stale code.
- **Registry before runtime, always.** A roll recreates the registry, waits for
  it to report healthy, runs the migrations through its image, and only then
  recreates the runtime and the worker. The registry and the runtime share one
  schema layer, and the runtime reads descriptors through the registry's REST
  surface rather than the tables — which is how it inherits the auth, audit and
  content-hash-head logic for free, and also why a runtime recreated against a
  registry still serving the old schema goes quiet on stale typed descriptors.
  There is no flag to skip it.

`RUNBOOK.md` owns the procedures; `deploy/deploy.sh` is the one-command
parameterised bring-up.

## 12. Documentation is part of the change

- **Counts are generated, never hand-typed.** `RELEASE_STATE.md` is emitted from
  live queries. A doc that states a number should take it from there or derive
  it at run time. A label that pins a count is a label that will silently drift.
- **If a change makes a shipped sentence untrue, fix the sentence in the same
  commit** — including in the README.
- **A change that is deliberately not finished goes in `SEAMS.md`** (not built,
  fails loud) **or `STATUS.md`** (built, but here is its honest limit) — never in
  a comment nobody will read.
- **The commit body is where the value is.** State what was verified, what
  moved, and what was deliberately left alone. Subjects are conventional-commit
  and lowercase; there are no attribution trailers of any kind.

## 13. Running the tests

The suite runs in a container pinned to a compatible pytest, through
`scripts/run_tests_in_container.sh` — whole suite, or a subset by path. Run
targeted paths while iterating; the full suite takes on the order of a quarter
hour and shares one Postgres.

Working in a git worktree, point the runner at your own tree or you will verify
your branch against a different checkout's code. Two families fail only in a
worktree and are not your fault: the Dockerfile build test pins the main
checkout path, and the seed tests need curated seed data that is gitignored and
absent from a clone.

A green CI badge means the tree is lint-clean and structurally sound, not that
the tests pass. CI runs the ruff ratchet and the structural gates — the checks
that need nothing but the source tree. Everything touching Postgres, Qdrant,
OpenSearch, NATS or a Dapr sidecar is out of reach of a hosted runner, which is
the great majority of the suite. The real gate is the nightly run on a host with
the stack up.

---

- `ARCHITECTURE.md` — the shape and the reasons for it.
- `CODE_MAP.md` — where the code lives.
- `SEAMS.md` — the declared-seam registry. `STATUS.md` — built, with limits.
- `CONTRIBUTING.md` — the same gates, for an outside contributor.
- `RUNBOOK.md` — bring-up, health and failure procedures.
