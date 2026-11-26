<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Instance profiles

A Legba instance is the platform plus one **profile**: a directory
`profiles/<id>/` whose `profile.yaml` is the only file the bring-up reads to
learn what is specific to that instance. Everything else (the runtime, the
registry, the schemas, the checking layer, the compose files) is the platform
and is the same for every instance.

Two profiles ship:

| profile | what it is |
|---|---|
| `profiles/geopolitics/` | the example instance. Its descriptors are the tree's `descriptors/`; its own prose is its `prompts/` package, `legba_geopolitics` (the six political-lean lenses). It is the default. |
| `profiles/sanctions_export_control/` | a **skeleton** of a focused instance: sanctions and export-control determination. Ten regime subjects, a source set, its five questions as data, its labels. It validates and plans; it is not registered anywhere, and it has no units, top floor or scorecard yet. |

## The manifest

`src/legba/data/profile.py` loads it. The model is closed: an unknown key is
refused at every level.

| key | meaning |
|---|---|
| `id` | the profile's slug; must equal its directory name |
| `descriptors_dir` | the directory the descriptor walker registers from; its `RETIRED.md`, when present, is the walker's exclusion list ([SETUP.md](SETUP.md)) |
| `lifecycle` | the live-state overlay: the state each descriptor holds on this instance when it differs from the state its file ships. The key names an overlay file (`LIFECYCLE.md`); the walker reads the per-row directory beside it (`LIFECYCLE.d/`, one `<family>__<id>.md` file per row) and any row left in the file. `null` = shipped states |
| `seeds` | the knowledge-seed adapters `deploy.sh --seed` runs, in order (`LEGBA_SEED_SOURCES` overrides) |
| `rosters` | the instance's rosters as data: `subject_tags` (every tag that puts a target on the subject roster), `scored_subject_tags` (the subset that carries the per-subject scorecard, baselines and baseline-deviation alerts), `units` (the bounded-unit roster, in display order; `question` only for a unit whose descriptor is not written yet), `scored_units` (the subset of `units` that are the fixed scorecard dimensions the correctness plane measures one by one), `source_catalog` (the profile's source-catalog YAML file, or `null`), `credibility_seeds` (the modules whose `seed_credibility` the walker runs), `gazetteer` (the profile's gazetteer YAML file — its tables of names and codes — or `null`; see "The gazetteer" below) |
| `prompts` | the directory of the profile's own prompt prose, holding one package named `legba_<id>` (see "Prompt packages"), or `null` |
| `top_floor` | the producer id of the instance's top read, or `none` |
| `subjects` | the subject ladder's data: `label` — how a subject's display name joins its tier and its name (`separator`, the `separator_aliases` a parser also accepts, `order`), read by `unit_names.unit_label` (see "The subject ladder"); undeclared = the platform default, `—` with tier first |
| `projection` | the profile's read-time view: `view` (the view file, `projection/view.yaml`) and `plane` (the plane id its rows carry in `profile_projection`); absent when the profile declares none, and every reader of its plane then refuses `projection_disabled` (see "The projection" below) |
| `constants` | the values the platform's mechanisms key on (see "Constants" below): `audit_tier_order`, `desk_tiers`, `subject_frame`, `anchor_class`, `auto_merge_class`, `translation_languages`, `adjacency`, `applicability_tags`, `reference_subject_tags`, `levels`; a key left out is `none` / empty |
| `ui` | the workstation's labels: `subject` (the singular and plural noun, and `kinds` when there is more than one subject family), `unit_noun`, `frames` (each frame kind with its label, `parent` and `producer`), `landing`, `stances` and `missions` (label overrides keyed by the shell's ids), `map.geometry` (`iso2`, `declared` or `none`) and `map.title`, and `labels` (the read and overview titles, the unscoped-subject label, the roster labels) |

Paths are relative to the profile directory and must stay inside the repo (the
bring-up reads them inside the registrar container, which mounts only the repo).

The stance and mission ids are the shell's own keys, shaped after the example
profile where the shell's are (`desk`, `desk_watch`); the workstation renames them with aliases,
and the manifests follow.

## Rosters are data

The code spells no roster. It reads every one through
`src/legba/data/rosters.py`: `subject_roster_tags()`, `scored_roster_tags()`,
`unit_roster()`, `scored_unit_roster()` and `source_catalog()`. The SQL that
selects the roster's subjects binds the tags as a parameter
(`(body -> 'scope' -> 'tags') ?| $1::text[]`): the desk-coverage roster the
compositions use to name a desk with no read, the crossroads silences, the
scorecard producer, the baseline-deviation alerts and the desk baselines. A tag
added to `subject_tags` therefore joins the desk-coverage read with no code
change, and an instance whose profile declares no `scored_subject_tags` gets no
scorecards rather than another instance's.

The runtime and registry images carry `src/` and not `profiles/`, so the
selected profile's rosters ship inside the package as
`src/legba/data/profile_rosters.json`, the same way its workstation labels ship
inside the UI build. With `LEGBA_PROFILE` set, the reader loads that profile
directory instead (and refuses one that does not load). The committed file is
the default profile's export, and the test suite fails when it is stale:

```bash
PYTHONPATH=src python3 scripts/gen_profile_rosters.py --profile profiles/<id>
PYTHONPATH=src python3 scripts/gen_profile_rosters.py --check   # exit 1 when stale
```

The rosters are read once per process and cached; `rosters.refresh()` drops
the cache. Constants built from a roster at import, such as the scorecard's
`DIMENSIONS`, hold the roster the process started with.

The `entity_class` set the entity researcher's classifier may assign is the
registered `entity_class` vocabulary family, read from the runtime's one
vocabulary snapshot; when that family is unloaded the researcher uses its own
documented seven-class literal.

### Units carry their data

Each `rosters.units[]` entry carries the data the platform keys by that unit,
inline. Every key is optional:

| key | what reads it |
|---|---|
| `label`, `short` | the workstation: the unit's display label and its one-to-four-character chip glyph (the desk's gap strip, the unit cards, the producer filter) |
| `scope_tag` | the one tag the unit's fan-out predicate narrows to (its descriptor's `has_tag("<tag>")`; a test pins the two together). Unset means the unit fires on every scored subject. A scope tag that is itself an unscored `subject_tags` entry puts the unit on that leg only |
| `scope_note` | the empty state a scoped unit shows on a subject it does not run on |
| `query_terms` | the desk reference's web-search terms (`desk_reference`) |
| `title_terms` | the title-frame gauge's vocabulary: lower-case stems a title uses when it is about this unit |
| `reference_queries` | the reference builder's query patterns; `{subject}` is the one substitution (`{country}`, the example profile's spelling, is filled from the same value) |
| `structural` | `true` when the unit's status is a standing condition (a sanctions regime in force, a war footing) rather than a run of events: its descriptor then declares level-shaped indicators (`method.options.level_indicators`), each measured against a declared reference and reading `observed_at_level` on a report from the run's own window — the re-evidence the carried-state rule accepts for a status no change-shaped signpost renews. A test pins the flag to the descriptor both ways; `scripts/census_carried_level.py` reads it. The example profile marks `energy_security` and `military_posture` |

A unit that declares none of them degrades by name: the desk reference skips it
and says so in its warnings, the reference builder gives it generic patterns,
the gauge does not count it, and the workstation shows its id de-underscored.
`rosters.scored_subject_units()` is the set of units a scored subject's desk
carries: every unit except one scoped to an unscored roster leg. The
correctness grader and the reference builder grade that set, and the desk's
unit cards render it. The instrument data ships in the packaged rosters export
(`profile_rosters.json`, schema `legba/profile_rosters/2`) and the labels ship
in `profile_ui.json`. Neither file carries the other's keys.

Doctrine stays in code: `collection_gap.SOURCE_CLASSES_BY_DIMENSION` (which
source classes would feed each unit) is a platform table keyed by the roster,
and a test fails if it names a unit that is not on it.

### The source catalog

`rosters.source_catalog` names a YAML file of feed entries
(`profiles/geopolitics/rosters/source_catalog.yaml` for the example profile). It is a
mapping with one key, `entries`; each entry carries the fields of the catalog
loader's `CatalogEntry` (`id`, `name`, `kind`, `url`, `tags`,
`cadence_minutes`, the credibility seed `cred_host` / `cred_score` /
`cred_rationale` / `tier` / `state_affiliation`, and the optional `geo`,
`language`, `extra_config`, `enrich_text`, `fact_extract`, `snapshot`,
`access_class`, `source_class`). A key left out takes the field's default and an unknown key is
refused. `source_class` is the entry's editorial class when it is curated
rather than derived from `tier`: the example profile's five state-controlled outlets
say `state_media`, and the catalog loader reads its state-media set from those
entries. It must be one of the source schema's classes (`reporting`,
`analysis`, `official`, `state_media`); left out, `thinktank` derives
`analysis`, `gov` derives `official`, and everything else derives `reporting`. The file is the instance's data; the loader, the descriptor builder,
the credibility seeding and the live probe (`--verify`) are the platform's, in
`scripts/bringup_register_source_catalog.py`. The sanctions skeleton declares
`null`: its sources are descriptor files.

### The gazetteer

`rosters.gazetteer` names one YAML file of name and code tables
(`profiles/geopolitics/rosters/gazetteer.yaml` for the example profile). Five modules
used to carry these as code literals: the absence-slice claim scope and the
cross-target guard's vocabulary (`provenance/absence_slice.py`), the
officeholder guard and fact supersession (`provenance/world_knowledge_guards.py`,
`provenance/writes_supersession.py`), the polity canon (`_polity_match.py`,
`_geo_routing.py`), entity canonicalisation (`_entity_canon.py`) and the
off-target guard and RAG country name (`runtime/grounding.py`). They now read
one loader, `src/legba/data/gazetteer.py`, and the code holds no table of
country names.

The file is sectioned by the tables it replaced. Every section is optional:

| section | what it holds | who reads it |
|---|---|---|
| `desk_ids` | `marker`: the substring that marks a per-subject desk id whose trailing `_<token>` is its ISO-2 code (`country` on the example profile) | the absence-slice and cross-target guards, the off-target guard, the grounding fast path |
| `desk_countries`, `slice_geo_countries` | desk slug → the names a slice row uses; the wider set of names a row can be about | the absence-slice claim scope |
| `slug_to_country`, `country_tokens`, `slug_to_abbrev`, `irregular_demonyms` | desk slug → names; the "another country" set; case-sensitive short forms; demonyms the suffix rule cannot derive | the cross-target guard and the slice screen |
| `screen_stopwords` | the subject nouns of the stage-1 title screen's stoplist (the rest of the stoplist is platform) | the absence-slice screen |
| `classifier_patterns` | named title classifiers; `machine_structured_title` is the GDELT/CAMEO event-coding shape | the machine-structured row screen |
| `officeholder_country_groups`, `officeholder_acronyms`, `polity_offices` | one alias group per country, in order (it feeds a regex alternation); uppercase-only acronyms → a group; the office roles and their fact predicates | the facts-reconciled officeholder guard |
| `functional_role_country_side` | which fact column names the country, per functional-role predicate | office-keyed fact supersession |
| `iso2_home_aliases` | the newsroom spellings of a desk's own country | the polity canon (home-country exclusion, the search name) |
| `polities` | `universe: pycountry` (derive the ISO-3166-1 records, don't copy them) plus `curated` names pycountry stores under another head | the country gazetteer, the polity canon's official names |
| `alias_map`, `alias_org`, `demonym_map`, `region_adjective_map`, `known_regions` | entity canonicalisation's collapse maps and region surfaces | `_entity_canon.canonicalize_entity` and the polity canon built from it |
| `target_slug_names`, `known_country_tokens` | desk slug → names (the first, title-cased, is the RAG query's country); the "another country" set | grounding's off-target guard and RAG query |
| `subject_homonyms` | per subject code: `exclude` — the forms that look like the subject but name something else ("KP" = Khyber Pakhtunkhwa on the North Korea desk); `reads_as` — what they name; `subject` — names of the subject beyond the tables above. A form with no lowercase letter matches case-sensitively | the desk slice's homonym exclusion (`signal_slice_scope.HomonymSliceFilter`), the critic's `off_subject` check, the correctness grader's `off_subject:<code>` exclusion (`data/subject_homonyms.py`) |

**A section left out is absent, by name.** The loader returns a typed `Absent`
for it, never an empty table that would let a check pass. A guard whose
sections a profile does not declare answers `not_applicable`: the cross-target
leak, the officeholder guard, the slice claim scope, the machine-structured
title screen, functional-role supersession, the off-target guard and the
critic's `off_subject` check. Each
answer is counted (`gazetteer.not_applicable_census()`), logged once per
process, and written on the verify report as
`gazetteer_not_applicable_<guard>`. [SEAMS.md](SEAMS.md) #67 lists them. A
canonicaliser (the entity canon's maps, the polity canon) reads an absent
section as empty, so names pass through uncollapsed, and the absent read is
recorded by name (`gazetteer.absent_reads()`); the slice's homonym exclusion
and the grader's read `subject_homonyms` the same way (no rules, nothing
excluded).

The images carry `src/` and not `profiles/`, so the selected profile's
gazetteer ships inside the package as `src/legba/data/profile_gazetteer.json`,
the same way its rosters do. With `LEGBA_PROFILE` set, the loader reads that
profile directory instead. `scripts/gen_profile_gazetteer.py` writes the
export, and `tests/data_pkg/test_gazetteer.py` fails when it is stale. That
test also pins every geopolitics table by sha256 to the literal it replaced,
and checks that 50 sample signal titles get the same guard verdicts as before.

The sanctions skeleton's gazetteer declares only the polity universe and the
spellings of the four designating authorities (United States, United Kingdom,
European Union, United Nations). Its regime and counterparty targets are not
country desks, so every country-desk guard is `not_applicable` there.

The class gates (`_frame_anchor`'s class gate and the entity auto-merge class)
read the `anchor_class` and `auto_merge_class` constants below. The matchers
themselves (the suffix demonym rule, the whole-token rule, the Republic of
China / People's Republic of China substring guard) are platform code: they
work on whatever table the profile supplies.

## Constants

A handful of values the platform's mechanisms key on belong to the instance: they
are the manifest's `constants` block (closed), and the code reads each one through
one leaf, `src/legba/data/profile_constants.py`, never a literal.

| key | what it is | who reads it | geopolitics | sanctions skeleton |
|---|---|---|---|---|
| `audit_tier_order` | the external-audit drain's tier order, highest first, as `ui.frames` ids (each frame's `producer` is ranked; one not listed ranks 0) | `_external_audit_queue._TIER_RANK` | `world, thematic, assessment, country` | none (no frame has a producer) |
| `desk_tiers` | `{analyst, tier}` pairs: the external-truth stratum each producer's grades fall in (`voice` = the authoring channel); anything else is `other` | `external_truth.desk_tier_for_analyst` and its SQL `CASE`, rendered from the same table | the six record and voice producers | none (every grade reads `other`) |
| `subject_frame` | the `ui.frames` id of the per-subject tier: its producer is the subject read, and a topic carrying the frame id as its id prefix names a subject's own target | the scorecard route's reconciliation (`subject_producer`), `situation_clustering._target_for_category` | `country` | `regime` |
| `anchor_class` | the entity class a frame anchor canonicalizes to, or `none` | `_frame_anchor.FrameAnchorConfig.class_gate`, the coverage floor's fold | `country` | `none` |
| `auto_merge_class` | the entity class whose curated alias pairs merge without the adjudicator, or `none` (every alias pair stays gray) | `_entity_candidates` | `country` | `none` |
| `translation_languages` | the source languages translated before NER | the NER filter, the two backfill sweeps, the journal's raw-language warning | the eleven non-Latin NLLB codes | none (its sources publish in English) |
| `adjacency` | edge families: family id → undirected `[a, b]` pairs of subject codes | `desk_baseline` (`land_border`, the neighbour-spillover feature) | 20 land-border pairs | none |
| `applicability_tags` | the closed tag vocabulary a subject's own target file carries to narrow unit fan-out | `scripts/bringup_tag_targets.py` | `nuclear_watch, conflict_active, sanctions_regime` | none |
| `reference_subject_tags` | the non-scored `rosters.subject_tags` whose subjects the correctness roster also covers: the reference builder builds them a reference over the units scoped to the tag, and the grader grades them (each must be a `subject_tags` entry) | `_reference_roster.reference_roster_tags` / `reference_units` (the reference builder, the correctness grader, the keyed reference kind) | `supply_chain` — the lanes and flows are graded desks | none (the roster is the scored subjects alone) |
| `levels` | the level words a desk's typed read may give its subject, lowest first; `none` is reserved for "nothing in this window bears on the question" and is not a level | `desk_rows_contract.severity_levels` (the desk-rows seat's prompt and parser) | `low`, `moderate`, `elevated`, `high`, `critical` | the desk-rows seat refuses by name (`desk_rows.levels_undeclared`) |

`top_floor` stays a top-level key; `profile_constants.top_floor()` reads it.

The skeleton's `none`s are deliberate. Its subjects are regimes and counterparties, so
the anchor a frame would need is an organization, and the frame anchor refuses an
organization anchor until there is a home-entity exclusion and an organization fold;
a compliance record merges no entity without the adjudicator. Each reader refuses by
name (`frame_anchor.no_anchor_class`, `profile_constants.adjacency_undeclared`, an
empty drain rank, the stratum `other`), never with the example profile's value.

Like the rosters, the constants ship in the package as `src/legba/data/profile_constants.json`
(frame ids resolved to their producers), `LEGBA_PROFILE` selects a profile directory
instead, and the committed file is drift-tested:

```bash
PYTHONPATH=src python3 scripts/gen_profile_constants.py           # write
PYTHONPATH=src python3 scripts/gen_profile_constants.py --check   # exit 1 when stale
```

With no profile at all (no `LEGBA_PROFILE` and no packaged export) the leaf reads its
documented fallback, the example profile's values as the code spelled them, and logs
`profile_constants.unloaded`. Constants a module builds at import (the drain rank,
the adjacency edges, the NER translate set) hold the values the process started with.

`scripts/bringup_tag_targets.py` reads its tag tables from the profile too: a desk's
group tag is the one tag of its own target file that a frame of the subject frame's
parent kind selects (the frame seed's `tag:` rules), and its applicability tags are
the file's tags in `applicability_tags`, in declared order.

## The subject ladder

Every per-subject key the platform writes — a reference's item ids, the grader's
packet kind, the desk reference's query subject, the grounding guards' own geo
token, a collection requirement's ISO filter — comes from one resolver,
`src/legba/data/subject.py`, and none is parsed out of a target id. Three rungs
each, most authoritative first:

| | rung 1 | rung 2 | rung 3 |
|---|---|---|---|
| `subject_code(target)` | the descriptor's `scope.subject_code` | its single `scope.geo` code | the target id |
| `subject_name(target)` | the gazetteer's name for that code (when the code is a declared geo code and the caller hands a gazetteer; a `spell` picks the newsroom spelling) | `identity.name` | the de-slugged id |
| `subject_kind(target)` | the frame kind, when the target is itself a frame of the `frame` family | `scope.domain` | `""` |

`scope.subject_code` is optional and additive: a descriptor that does not declare
it dumps and hashes exactly as before. Declare it when neither the single geo
code nor the id is the right key (a subject code a reference or an external
system already uses). A target with several geo codes and no declared code — a
supply-chain lane, a flow — is keyed by its own id.

A target arrives as a descriptor body, a flat SQL row or a bare id. A bare id is
looked up in the profile's declared subjects: every L1 target file in its
`descriptors_dir`, with its frame kinds from the `frame` vocabulary seed and the
`subjects.label` style, packaged as `src/legba/data/profile_subjects.json` (the
images carry `src/` and not `profiles/`). An id the profile does not declare
resolves to itself. `LEGBA_PROFILE` set reads the profile from disk instead. The
committed file is the default profile's export and is drift-tested:

```bash
PYTHONPATH=src python3 scripts/gen_profile_subjects.py           # write
PYTHONPATH=src python3 scripts/gen_profile_subjects.py --check   # exit 1 when stale
```

The DB-free callers (the grounding guards, the collection-gap row builder, the
forecast and calibration geo fallbacks) therefore see a target as the profile
declares it, and an async caller with a connection reads the live head
(`load_subjects`). A subject registered live but absent from the profile's
target files resolves by its id on the DB-free paths until it is added to the
profile and the export regenerated.

**The correctness roster reads the ladder too.** The reference builder
names a subject by the ladder and calls it by its label's tier (`Lane — Strait of
Hormuz` → "a knowledgeable *lane* analyst"; a polity — named by the gazetteer's
polity rung — stays "country", as before) and
searches it by the label's name half (`Strait of Hormuz shipping traffic`). Its
query patterns are each unit's `reference_queries`, else the unit's own
`query_terms` cut into two-word queries after the subject, else the generic
patterns. The gazetteer rung is read only when the profile's gazetteer declares
`polities`, and only for a code that is one of the subject's declared geo codes; a
profile with no gazetteer reads it as absent and names every subject by its
descriptor. A subject whose record is a machine-readable list gets a keyed
reference instead (`reference_deterministic`, [GRADING.md](GRADING.md)):
the descriptor declares the list's shape and `bindings` (`target_id=value|value`),
and the grader grades it exactly, by key.

The label `Watch — Pakistan (country_watch_pk)` has one producer and one parser,
`src/legba/data/analysts/unit_names.py`: `unit_label` (with an optional `tier`)
and `subject_display` join, `parse_unit_label` and `label_name` take apart, and
`label_name_sql` renders the same rule for a SQL column (the edge-qualification
score's desk names). Its separator, the glyphs a parser also accepts and the
order of tier and name are the profile's `subjects.label`.

## Prompt packages

Prose that belongs to an instance and not to the platform lives in the profile,
as one Python package: `profiles/<id>/prompts/legba_<id>/`. The package name is
the profile id behind a `legba_` prefix (`legba.data.profile.prompts_package_name`),
a top-level name of its own beside the platform's `legba` package, so two
profiles' prose cannot shadow each other or a platform module. The manifest
declares the directory (`prompts: prompts`), and the loader refuses a declared
directory that does not hold the package. A descriptor names a module in it the
way it names any prompt module, by dotted path: `prompt_module:
legba_<id>.<module>:ATTR`. The runtime resolves it with the same import as a
platform prompt, so a profile prompt needs no code.

The images carry `src/`, not `profiles/`, so the registry and runtime builds
copy the selected profile's `prompts/` directory into site-packages beside
`legba` (`docker/Dockerfile.runtime` and `docker/Dockerfile.registry`, build
arg `LEGBA_PROFILE`, a directory relative to the repo root; the compose file
passes `${LEGBA_PROFILE:-profiles/geopolitics}`), and the build imports every
module in it, so a missing or broken package fails the build. The registry
needs it as much as the runtime: registration resolves every analyst
descriptor's `prompt_module` and refuses a dead one on an active descriptor.
From a checkout, the directory goes on `PYTHONPATH`: `deploy/deploy.sh` adds it
to every registrar run (after the image's own copy), and pytest's `pythonpath`
carries the default profile's. After a rolling deploy, `scripts/rolling_deploy.sh
--verify-import legba_<id>.<module>` proves the package landed in all three
containers, and deploy.sh's boot check imports it in the runtime.

### A profile's lenses: the `<profile>.lens` family

The journal kind's lens tier runs two rosters. The five epistemic lenses
(trend, base rate, capability, intent, and the chorus diff over the first
four) are platform prose in `legba.prompts`. A profile adds lenses of its own
on the same tier: their prose in its prompt package, their descriptors in its
descriptors directory, and their ids in a `<profile>.lens` vocabulary family,
seeded by the walker from a `descriptors/vocabulary_<profile>_lens.yaml` seed
file. The geopolitics profile's six political-lean lenses are registered this
way (`geopolitics.lens`, `legba_geopolitics.lens_*`).

`src/legba/data/lens_family.py` reads the family through the process's
installed vocabulary source, with the six-id literal as the documented
fallback when the family is absent, empty or unloaded; a loaded family can
narrow or extend the roster. The journal kind builds its lean roster from it at
import, so a process holds the roster it started with, and the runtime, which
installs no such source, holds the literal. The family the platform reads is
the example profile's; reading the running profile's own family is not built yet. The
chorus diff's roster stays the four epistemic lenses whatever the family holds.

## The projection

A profile's read-time vocabulary — its subject groups, its unit roster with each unit's own
cadence, its tier labels, the Morning Read's bands, the absence route's scope resolution and its
crosswalk rows — is one declared shape per profile, `projection/view.yaml`, materialized into the
`profile_projection` table as the profile's plane ([DATA_MODEL.md](DATA_MODEL.md)). It is the
`graph_arcs` pattern in four parts: the view, the leaf `src/legba/data/profile_projection.py`
(the flag, the staleness read, the plane catalog, the crosswalk resolver), the builder
`deterministic_handlers/profile_projector.py` (a deterministic analyst, hourly, a whole rebuild
per plane), and the route `GET /api/v1/registry/projection/<plane>[/<section>]`.

The view's keys are the plane's sections; a key left out is a section the plane does not carry.
The model is closed (`schema: legba/projection_view/1`):

| key | the section it builds |
|---|---|
| `subject_groups: {kinds: [...]}` | the `frame` family's subtrees under these kinds: each frame's label, parent, member rule and resolved member target heads, read at build time |
| `units: {cadence_from: descriptor_head}` | the profile's `rosters.units` in display order, each with its scored flag and the cadence its live descriptor head declares (null, and named in the build receipt, for a unit with no head) |
| `tiers: ui.frames` | the manifest's frames in order, each with its label, parent and producer |
| `labels: ui.labels` | the subject and unit nouns, the landing stance, the map title and `ui.labels` |
| `bands: [{id, label, question}]` | the Morning Read's reading order |
| `scope: {banding_analyst, audit_analyst, unit_grace_multiple}` | the absence route's scope-resolution table; its unit list is the roster |
| `crosswalk: {namespace}` | the `crosswalk` rows under the profile's own namespace (it must be the profile's id) |

The images carry `src/` and not `profiles/`, so every profile's view, resolved against its
manifest, ships as `src/legba/data/profile_projection.json` with the default plane named in it.
`LEGBA_PROFILE` set resolves that profile from disk instead. The committed file is drift-tested:

```bash
PYTHONPATH=src python3 scripts/gen_profile_projection.py           # write
PYTHONPATH=src python3 scripts/gen_profile_projection.py --check   # exit 1 when stale
```

Two profiles on one substrate are two planes: a rebuild touches one plane, and a read of one never
returns another's rows. Off (`LEGBA_PROFILE_PROJECTION`, the default), nothing is built and every
read refuses `projection_disabled`; a plane never built refuses `projection_empty`; a build older
than `LEGBA_PROFILE_PROJECTION_MAX_AGE_SECONDS` refuses `projection_stale`; an unknown plane or an
undeclared section is a 404 naming what is known. The sanctions skeleton declares no projection.

What reads it: the absence route resolves its scope (the units it checks, the analysts that
stamp the banding and audit kinds, the unit grace) from the plane's `scope` section when the flag
is on and the plane is fresh, and from its constants otherwise; on the geopolitics data the two
answers are byte-identical. The workstation still reads its literals.

### The crosswalk

A profile's value maps to a platform value through the `crosswalk` vocabulary family: each entry's
`value` is `<profile>.<axis>:<value>` (the profile's own value shape) and its `parent` is the
platform value, `<platform_family>:<value>`. Rows are seeded from
`descriptors/vocabulary_crosswalk.yaml` by the walker's generic seed loader, and each profile's
plane carries the rows under its namespace in its `crosswalk` section. A value with no row is
unmapped, and readers leave it as it is.

## Selecting a profile

`--profile <dir>` beats `LEGBA_PROFILE`, which beats the default
`profiles/geopolitics`. A plain `deploy/deploy.sh` therefore runs the geopolitics
example instance.

```bash
# validate a manifest and print its summary (the registry image carries the loader)
python -m legba.data.profile --profile profiles/sanctions_export_control check

# what the walker would register for a profile, writing nothing
python scripts/bringup_register_descriptors.py --profile profiles/sanctions_export_control --dry-run

# bring an instance up on a profile
deploy/deploy.sh --profile profiles/<id> [--project <name>] [--no-caddy]
```

## The workstation's labels

The workstation reads its labels at build time from
`legba-ui-v3/src/lib/profile_ui.json`, which `scripts/gen_profile_ui.py`
exports from the profile's `ui` and roster keys, plus the profile's projection
`plane` (the plane the workstation reads `GET /registry/projection/{plane}/…`
on). The committed file is the default profile's export; the workstation reads
every label from it and spells none of its own (`tests/test_profile.py` checks
the golden against the manifest and that no TypeScript site carries a label
literal; `lib/profileUi.test.ts` pins the geopolitics strings byte for byte).
When the projection answers, the read tiers, the Morning Read's bands and the
rail's subject groups come from it instead, and a refusal is printed by name
(docs/UI.md "Labels come from the profile and the projection"). A key a
profile does not declare is left out of the export, and the workstation renders
its platform noun in its place. A second instance regenerates the file from its own profile and rebuilds
the UI image; `deploy.sh` warns when the committed file is not the selected
profile's export.

```bash
PYTHONPATH=src python3 scripts/gen_profile_ui.py --profile profiles/<id>
PYTHONPATH=src python3 scripts/gen_profile_ui.py --check   # exit 1 when stale
```

## Starting a new profile

Copy the sanctions skeleton's shape: a `profile.yaml`, a `descriptors/`
directory of the instance's own target and source files (shipping `draft`), and
symlinks to any example-profile descriptor it reuses unchanged. Validate it with
`check`, then dry-run the walker against an empty database. The platform's own
analysts and action packs still live in the example profile's `descriptors/`; until they
are split out, a second instance's walk registers only its own directory.
