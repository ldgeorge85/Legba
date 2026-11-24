<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Adding a type

A *type* is one kind of thing the platform knows by name: a deterministic
handler, an alert kind, a principal kind (who can write a row), a citation
target (what a citation may name), an assertion type (what the held record may
hold), and, as further families migrate, others. Each type is **one module that ends in one `declare(...)`
call**. Nothing else lists it by hand: the dispatch tables, the verify-exempt
sets, the console mirror, the alert topics and the docs tables are derived
from the declarations, and tests check rules ("every handler has code, an
options home, a descriptor, a test and a doc line") instead of totals.

The registry is `src/legba/data/type_registry/`. At first use it walks each
family's package, then the `legba.types` entry-point group (so an installed
package can add types without touching this tree). Anything wrong — a module
that does not import, declares nothing or declares twice, an unknown field, a
missing required field, two declarations of one id — stops the process with
every failure named. Nothing is skipped quietly.

## A deterministic handler

1. The code: `src/legba/data/analysts/deterministic_handlers/<id>.py` with
   `async def handle(inputs, options, deps) -> AnalystMethodResult`.
2. The declaration: `src/legba/data/type_registry/deterministic_handlers/<id>.py`:

   ```python
   from legba.data.option_catalog.handler_options_base import OptionSpec
   from legba.data.type_registry import declare

   TYPE = declare(
       family="deterministic_handler",
       id="kev_digest",
       implementation="legba.data.analysts.deterministic_handlers.kev_digest:handle",
       output="finding",          # or "trace_only": the product is side-written
       requires_pool=True,        # refuses without deps.pg_pool (tested)
       alerts=(),                 # alert kind ids it raises, if any
       input_schema=(OptionSpec("max_entries", "int", "Entries per digest.", minimum=1),),
       doc="The KEV catalogue entries new since the last digest, copied field by field.",
   )
   ```

   The declaration imports nothing but the registry (and `OptionSpec`, a
   stdlib-only leaf): the slim registry image reads it without the handler's
   dependencies. Put the reasons behind `output` in its docstring.
3. A descriptor in `descriptors/` binding it, and a test that names it.
4. `python3 scripts/gen_type_tables.py` — regenerates [TYPES.md](TYPES.md)
   and, for a `finding` handler, the console mirror in
   `legba-ui-v3/src/lib/verdictModel.ts`. Generated lines, not hand edits.

What used to be edited and is not any more: `deterministic.py` (three maps),
`provenance/kinds.py` (the exempt set), the option catalogue, the console
mirror, the exempt pin and the requires-pool skip list in the tests, and the
catalogue's post-pin key list.

## An alert kind

1. The declaration: `src/legba/data/type_registry/alert_kinds/<id>.py`, with
   `subject_kind`, `lifecycle`, `quiet_close_hours`, `group`, `topic`
   (`world` / `engine` / `nightly`), `paging` (`page_rule` if the shipped
   `alerts/page_rule.yaml` gets an entry, else `logged_only`), `position` (its
   place in the Watch surface's order; ties break by id) and `doc` (the
   summary a person reads).
2. The producer that raises it, and, if it pages, its entry in
   `alerts/page_rule.yaml` (policy, the owner's call; a test holds the rule and
   the `paging` declarations to each other).
3. `python3 scripts/gen_type_tables.py` (the tables in
   [ALERTING.md](ALERTING.md) and [TYPES.md](TYPES.md)).

`alerts/kinds.py` and `alerts/topics.py` are no longer edited.

## A principal kind, a citation target, an assertion type

These three families carry the data model's "who" and "what":

* **`principal_kind`** (`type_registry/principal_kinds/`): one module per
  kind of writer (`user`, `service`, `agent`, `rule`, `shared`), declaring the
  shape of the name after the `<kind>:` prefix and an example. The database
  checks only the handle's shape; `data/principals.py` refuses an undeclared
  kind before any SQL runs, so a new kind is a declaration and no migration.
* **`citation_target`** (`type_registry/citation_targets/`): one module per
  record kind a citation may name (signal, observation, finding, event,
  situation, hypothesis, inquiry, desk baseline), declaring its table, the key
  column it uses on `citation_support`, and whether it is material. The
  citation-row writer refuses an undeclared kind; a test holds every declared
  column to the table, so a kind that reuses a column needs no migration.
* **`assertion_type`** (`type_registry/assertion_types/`): one module per type
  the held record may hold, declaring its class (`statement`, `attributed`,
  `assessment`, `absence`), the fields that make up its claim key, and the desk
  row kinds it comes from. A new class, by contrast, changes what the product
  may assert and is a migration.

[DATA_MODEL.md](DATA_MODEL.md) describes the tables that refer to them.

## The other shared counters and tables

| Thing | Before | Now |
|---|---|---|
| A migration | the next free number, which parallel branches collided on | `python3 scripts/new_migration.py <slug>` → `YYYYMMDDTHHMM_<slug>.sql` (UTC). Applied after every numbered one, then by time and slug; recorded by name. A new numbered file above 0268, the last numbered one, is refused. |
| A declared seam | the next free number in `docs/SEAMS.md` | a lower-kebab slug id, its row after the numbered rows in slug order ([SEAMS.md](SEAMS.md)) |
| A host lifecycle flip | a row appended to `descriptors/LIFECYCLE.md` | one file per row in `descriptors/LIFECYCLE.d/`: `python3 scripts/lifecycle_rows.py set <family> <id> <state> <ships>` |
| The prompt manifest | one regenerated `registry/descriptor_prompts.json` | one JSON per descriptor in `registry/descriptor_prompts/`: `python3 scripts/gen_descriptor_prompt_manifest.py` |

The gate runs every generator with `--check`: `gen_type_tables.py`,
`gen_descriptor_prompt_manifest.py`, `lifecycle_rows.py`. If two branches'
generated lines conflict on merge, re-run the generator: its output is a pure
function of the declarations.

## The cost, measured

| Adding | Before the registry (measured) | Now |
|---|---|---|
| A deterministic handler plus one alert kind | 18 files, 14 of them edits to shared files, 3 pinned tests, 3 copies of the alert kind | 3 new files (handler, its declaration, the alert kind's declaration) + 1 test + 1 descriptor; 0 hand edits to shared files; regenerated lines in up to 3 generated files (TYPES.md, ALERTING.md, the console mirror); 1 new row file if the host activates it |

The fixture in `tests/data_pkg/type_registry_fixture/` is that change, made
for real and proven by `tests/data_pkg/test_type_registry_fixture_proof.py`.

## Families not yet migrated

| Family | Where it is | What migrating it takes | Size |
|---|---|---|---|
| Source kinds | Nearly there: `data/sources/kinds/` declares `(module, class)`; discovery is fail-loud | Declare `id` and `implementation` per kind (one module each instead of the 18 in `core.py`); move the per-kind config branches of `schemas/source.py` into each kind's `input_schema`; replace the three source pins (`len(kinds)`, `public_count`, `EXPECTED_YAML_ACCESS`) with invariants. | S |
| Analyst kinds | `analysts/__init__._KIND_MODULE_NAMES`, a hand tuple walked fail-loud | A `analyst_kind` family; the tuple derived. | S |
| Output kinds | `provenance/kinds.py`: the `OutputKind` enum, `KIND_REGISTRY` (table, payload model, schema URI, subject) | Declarations with family, standing and clock columns (the merged recommendation's §4.1); the enum stays as the stable public name and is tested against the registry; the lineage catalog and gate inventory derived. This is where the `record_kinds` mirror table attaches (one row per declaration written at registry start) once the storage decision is made — not built here. | M |
| Round seats | Round specs as data are not settled | A `round_seat` family: input and output schema, route, prompt, gates; a round spec's edges type-checked against seat declarations, once the spec format is settled. | S once specs land |
| Console panel views | `panel_action_catalog.json` and console-side lists | Serve the declared list from one route and read it at console start instead of generated TS; or generate per-panel files. | M |

## Not built here

* The `record_kinds` mirror table: storage decisions for record kinds are
  staged. It would attach at `type_registry.all_types()`.
* The contract test suite parametrised over output kinds (round trip, lineage
  resolves, citation resolves, as-of read): it belongs with the output-kind
  family above.
