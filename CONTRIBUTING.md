<!--
SPDX-FileCopyrightText: 2026 Lewis George
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Contributing to Legba

Read this before opening a pull request. It is short, and most of it is about
four gates that will fail your branch mechanically if you skip them.

## Set expectations first

Legba is a **single-maintainer project with a bus factor of one**. The public
history is squashed per release; the granular engineering record is private, and
[CHANGELOG.md](CHANGELOG.md) is the public record of what changed and why. That
means:

- There is no triage rota and no response-time promise. Open an issue before
  writing anything large — a PR that arrives unannounced against a design the
  maintainer already decided against is wasted work for both of us.
- **An outside code contribution needs a CLA** ([FAQ](docs/FAQ.md#running-it)).
  Dual/commercial licensing is intended, and that only works if the copyright
  position stays clean. Ask before you write; the CLA is not currently automated.
- Bug reports, reproductions, docs corrections and *"your README says X, your
  code does Y"* findings need no CLA and are the most useful thing you can send.
  This project's whole proposition is that its docs are load-bearing; a caught
  overstatement is a real contribution.

Everything here is **AGPL-3.0-or-later** ([LICENSE](LICENSE)) — note the §13
network clause. Every source file carries SPDX headers; new files must too:

```python
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
```

## Before you write code: is code even the answer?

Legba is **descriptor-driven**. Sources, desks (targets), analysts, stack
components and capability packs are declarative YAML registered into Postgres at
runtime — content-hashed, with an Ed25519-signed audit log
(`src/legba/data/registry/signing.py`). The live set is the `*_descriptors` **DB
rows**, not the files in `descriptors/`.

So, before adding a module:

- **A new feed** of an existing kind (`rss`, `geojson`, `json_api`, …) is a
  descriptor plus a registrar entry. No Python. See
  [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).
- **A new country desk / region frame** is a target descriptor — extend
  `WATCH_ISO2` in `scripts/bringup_register_watch_country_targets.py` and the
  whole spine (units → composition → scorecard) picks it up by predicate.
- **A new analyst** of an existing kind is a descriptor in `descriptors/` plus
  an entry in `ANALYST_FILES`.
- **Code** is the answer for a new *source kind*, a new *analyst kind*, a new
  deterministic sub-handler, or a change to the runtime/provenance planes.
  [docs/CODE_MAP.md](docs/CODE_MAP.md) is where everything lives;
  [docs/DESIGN.md](docs/DESIGN.md) is why it is shaped that way.

New descriptors ship `identity.state: draft` when they need operator
verification before going live. Bulk registration of a draft creates **no
actor** — activation is a deliberate per-descriptor act. Do not ship something
`active` that you could not verify.

## The four gates

These run in the nightly suite (`scripts/host_nightly_suite.sh`) and, for the
first three, in CI (`.github/workflows/ci.yml`). They are not style preferences;
each exists because the tree lost a specific argument without them.

### 1. No stubs — declare the seam or don't ship it

Anything not built is a **declared entry in [docs/SEAMS.md](docs/SEAMS.md) that
fails loud**. Never a silent no-op, never a fabricated default, never an empty
list where an answer belongs.

`tests/test_no_undeclared_stubs.py` scans all of `src/legba/**` for stub markers
(`raise NotImplementedError("…")` with a message, `NotImplementedError`
subclasses, `stub`/`placeholder`/`fake`/`mock`/`dummy` name segments, `mock`
imports, `return {}` next to a TODO) and asserts every hit is either the bare
abstract-method idiom or listed in the machine-readable allowlist between the
`BEGIN/END SEAM ALLOWLIST` markers in SEAMS.md.

**There are deliberately no per-line pragma escapes.** The only way to ship a
stub-shaped symbol is to register it as a seam, where it carries a
what / why / guard-rail entry a reviewer can see. If your change adds one and
you cannot write that entry honestly, the change is not ready.

### 2. Tests must traverse the real binding path

A test that hand-builds the object under test proves the object works. It does
not prove anything is *wired to it*. The house rule is that a test exercises the
production binding — the descriptor is parsed by the real registry, the handler
is resolved by the real dispatcher, the tool call lands a real
`action_pack_invocations` row (see `tests/data_pkg/agency/test_agency_binding.py`
for the shape).

The cost of ignoring this is written into `deploy/deploy.sh` §(e) and
`scripts/deploy_smoke_cold_activation.sh`: a descriptor-parse bug once took the fleet
down while every one of ten-thousand-odd tests stayed green,
because they all built descriptors *in process* and none traversed
registry-fetch → parse → activate → run against a live sidecar.

Corollary: **infra-gated skips are failures by default.** `LEGBA_TEST_STRICT`
defaults to strict (`tests/conftest.py`), so a test that skips because Postgres
or the Dapr sidecar is missing fails instead of silently shrinking coverage. Opt
out locally with `LEGBA_TEST_STRICT=0`; never in a committed default.

### 3. Module size gates — extract, don't raise

`tests/test_module_size_gate.py` pins a LOC ceiling on every `src/legba` module
that was already ≥1,500 lines when the gate was written, and fails on three
conditions: a pinned module over its ceiling, an unpinned module crossing 1,500
lines, and a ceiling that stopped constraining its file.

If your change breaches a ceiling, **extract a cohesive unit into a sibling
module and re-seed the ceiling downward in the same commit.** The section
banners in these files are already the seams; a split that re-exports the moved
names is invisible to every importer. Raising a ceiling is possible — it is one
visible, reviewable line in the diff — but it needs its reason in the commit
message, and "my feature didn't fit" is not one.

### 4. The ruff ratchet

`[tool.ruff]` in `pyproject.toml` is green on the tree exactly as it stands. The
selected families are correctness-shaped (pyflakes, bugbear, ASYNC, logging,
return paths), and every rule the current tree violates is parked in `ignore`,
which is the debt ledger.

- **Adding** a rule is always safe — it fires on nobody.
- **Removing** an `ignore` is a deliberate act that costs its own cleanup commit.
- Do not run a broad `ruff check --fix` sweep. A five-figure-line reformat
  collides with every branch in flight, which is why the ignore list exists.

## Running the tests

The host interpreter cannot collect this suite (pytest 9.x trips a CPython AST
`SystemError`), so tests run in a container pinned to `pytest<9`:

```bash
bash scripts/run_tests_in_container.sh                      # whole suite
bash scripts/run_tests_in_container.sh tests/data_pkg       # a subset
```

**Working in a git worktree?** Point the runner at your own tree, or you will
"verify" your branch against a different checkout's code:

```bash
LEGBA_REPO_ROOT="$PWD" bash scripts/run_tests_in_container.sh tests/…
```

Two families fail *only* in a worktree and are not your fault:
`tests/data_pkg/test_dockerfiles_build_clean.py` (hardcoded main-checkout path)
and the seed tests (the curated seed *data* under `seeds/` is gitignored — the
adapters ship, the data does not, so it is absent from a clone).

Run **targeted** paths while iterating. The full suite is about half an hour and shares
one Postgres. In a worktree or a clone, set `PYTHONPATH=src` on every invocation: the
editable install's `.pth` otherwise imports the main checkout.

### Host-side database tests are not supported

On a deployment host the **live** Postgres server is published on `127.0.0.1:5432`,
the same address the conftests use. Database-backed tests therefore run only inside
the container runner, on the isolated test stack (`deploy/compose.test.yml`), and the
suite enforces it (`tests/_pg_test_server_guard.py`):

- every Postgres connection the suite makes (direct or pooled, plus the session
  bootstrap's `CREATE DATABASE` and migrations) first requires
  `LEGBA_TEST_PG_ISOLATED=1`, which only the runner's isolated mode sets — a bare host
  `pytest` is refused before any socket is opened;
- the server must then prove it is a test server: its sessions read
  `current_setting('legba.test_stack') = 'on'` (the stack starts Postgres with
  `-c legba.test_stack=on`), `fsync` is `off` from the command line, and no database on
  it exceeds `LEGBA_TEST_PG_MAX_DB_BYTES` (3 GiB). The live server fails all three;
- with isolation declared and an unmarked server, the session aborts at start.

Pure unit tests still run on the host (`PYTHONPATH=src python3 -m pytest <path>`);
anything that needs Postgres fails there with a `REFUSED Postgres ...` message naming
this section. The legacy `LEGBA_TEST_STACK=host` rig gets the same refusal.

The runner holds `/tmp/legba-container-tests.lock` (the nightly's lock) for its whole
run, so an ad-hoc run waits for the nightly instead of colliding with it.

## What CI does and does not cover

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) is honest about its own
scope: it runs the ruff ratchet and the structural gates (size, no-stubs,
dspy/litellm hot-path guard, strict-mode gate) — the checks that need nothing
but the source tree. It does **not** run the suite that matters most: everything
touching Postgres/AGE, Qdrant, OpenSearch, NATS or a Dapr sidecar is out of
reach of a hosted runner, which is the great majority of the 14,000-odd tests.

A green CI badge here means "the tree is lint-clean and structurally sound",
not "the tests pass". The real gate is the nightly suite
(`scripts/host_nightly_suite.sh`, lint → ordered → shuffled) on a host with the
stack up. Please say in your PR which suite you ran and against what.

## Documentation is part of the change

- **Counts are generated, never hand-typed.** `docs/RELEASE_STATE.md` is emitted
  by `scripts/generate_release_manifest.py` from live SELECTs; do not hand-edit
  it. If a doc states a number, it should either come from there or be derived
  at run time. A label that pins a count is a label that will silently drift —
  `deploy/deploy.sh` carries the scar tissue on this in its header.
- If your change makes a shipped sentence untrue, fix the sentence in the same
  commit. That includes the README.
- If your change is *deliberately not finished*, it goes in
  [docs/SEAMS.md](docs/SEAMS.md) (not built, fails loud) or
  [docs/STATUS.md](docs/STATUS.md) (built, but here is its honest limit) —
  never in a comment nobody will read.

## Ticket and decision ids in comments

Source comments cite short ids — `L-205`, `D-3`, `K-1`, `RUST-4`, `GLASS-1` — as
the "why" behind a non-obvious choice. Their full rationale lives in the
project's planning history (commit messages and an internal design ledger),
which is not part of this repository, so a bare id cannot always be resolved
from the code alone. The ids stay in the code; this section makes them legible.

Ids come in two families, and reading them differently is the whole trick.

- **Ledger ids are global.** `L-NNN` is a numbered entry in one continuous
  design ledger, so `L-107` means the same thing wherever it appears. Where an
  id cites a section, as in `L-107 §7`, the section indexes into the ledger.
- **Program letters are local.** Every build program allocates its own single
  letters from `A` again, so `D-3` in one program and `D-5` in another are
  unrelated. A program letter resolves only inside its program, and the citing
  comment's own subject is the disambiguator — which is why a comment that
  cites a bare letter id and nothing else is a bug in the comment.
- **The hyphen carries no meaning.** `D3` and `D-3` are not the same id written
  two ways; they are two programs that both reached `D`. Read the surrounding
  subject, never the punctuation.

| Shape | Reads as | Example |
|---|---|---|
| `L-NNN` | A global ledger entry, optionally with a section. | `L-205`, `L-107 §7` |
| `X-N` / `XN` | A work item in program `X`. | `A-2`, `K-1`, `D3`, `M15` |
| `XN-TN` | A task inside a numbered phase of program `X`. | `P2-T8`, `S3-T4` |
| `XN-N` | A numbered item inside a phase, where the phase itself is numbered. | `P2-1`, `B0-13`, `C5-3` |
| `<program> <phase>` | A named program and one of its phases, written as two words. | `V3 P4a`, `V3 P5` |
| `X-Nx` | A revision of a work item. | `W-1b`, `T-1a`, `R2-FIX-3` |

A few ids are cited as standing rules rather than as history: **`D3`**, the
no-stubs rule that [docs/SEAMS.md](docs/SEAMS.md) enforces; **`N-1`**, the plan
item that built the stub scanner (both cited in `tests/test_no_undeclared_stubs.py`);
**`M13`/`M15`**, the world-knowledge and cross-target guards of the verify
pass (`provenance/world_knowledge_guards.py`); and **`M14`**, the citation
rules for range markers and `[no citation]` (`provenance/citation_markers.py`).
`M13` also appears on the translation path, a different program's item: read
the comment's subject.

Not every id-shaped token is a ticket: standards and versions (`SHA-256`,
`ISO-8601`, `RFC-####`, `AGPL-3.0`), domain names (`G20`, a Wikidata `Q`-id
such as `Q22686`) and the correctness grader's model families (`F0`, `F2`,
`F3`) are read literally.

When you cite a decision in code, add a few words of inline context so the
comment stands without the ledger — `# L-205: the target-owned poll path
retired sentence-transformers` — and put a dated statement about the decision
in [CHANGELOG.md](CHANGELOG.md), not in the doc that describes the mechanism.

## Commits and pull requests

- Conventional-commit subjects, lowercase: `feat(verify): …`, `fix(deploy): …`,
  `docs(readme): …`, `refactor(journal): …`.
- **The body is where the value is.** This project's commit messages state what
  was verified, what moved, and what was deliberately left alone. Match that.
- Small, focused commits. One concern per commit.
- **No AI-attribution trailers** of any kind (`Co-Authored-By`, generator
  footers). The copyright line is the SPDX header.
- Never commit secrets, `.env` contents, anything under `planning/`, or curated
  seed data under `seeds/` — all gitignored, and this repo is public.

## Likely to be rejected

- A new dependency without a stated reason it cannot be avoided (`dspy`/
  `litellm` in particular are **hard-banned** from the runtime image and the
  analyst hot path — they ship only in `docker/Dockerfile.worker`, and
  `tests/test_runtime_no_dspy_litellm.py` enforces it).
- A stub, a silent fallback, or a default that fabricates a number when the real
  one is unavailable. Degrade with a label, or fail.
- Raising a module-size ceiling to fit a feature.
- Tests that assert against a hand-built object instead of the wired path.
- Changing a shipped default's behavior without an operator-facing switch.
  Preserve today's behavior by default, implement the alternative behind a
  descriptor option, test both paths, and document the recommendation.

## Contact

legba@civislux.us — or open an issue on the
[repository](https://github.com/ldgeorge85/legba).
