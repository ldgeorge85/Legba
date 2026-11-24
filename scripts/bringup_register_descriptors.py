# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ONE descriptor registrar: walk ``descriptors/`` and register every head.

W6 SPLIT-1 (DEC-13, 2026-10-04). Until this script, ``deploy/deploy.sh``
registered 46 of the reference host's 88 live analysts; the rest had been
registered by hand or by one of the 45 bring-up scripts deploy.sh never
called, so a fresh instance could not reproduce the host. This walker replaces
the per-family / per-descriptor registrars with one rule:

    every ``descriptors/*.yaml`` of the four registry families
    (``action_pack``, ``source``, ``target``, ``analyst``)
    + every entry of the profile's source catalog (ROSTER-1: data,
      ``profiles/<id>/rosters/source_catalog.yaml``, read by
      ``scripts/bringup_register_source_catalog.py``)
    − every row of ``descriptors/RETIRED.md``
    = what this instance registers, each at the lifecycle state
      ``descriptors/LIFECYCLE.md`` records for the reference host (or the
      state its file ships, when no row names it).

Files ship ``draft`` by convention — registering a file must never schedule
it; activation is an operator's flip. LIFECYCLE.md is where those flips are
recorded, so a fresh instance comes up the way the reference host runs.

The family is read from ``identity.schema_uri`` (``legba/<family>/x.y.z``),
not from the filename. Files of any other family (stack components, layer
maps, the wire map, collections) are not registry heads of these families and
are left to their own tools.

IDEMPOTENT, BY CONTENT. A head whose body, re-validated under the CURRENT
schema, hashes the same as the tree's descriptor is ``unchanged`` and is never
written. The comparison is semantic on purpose: a head registered before a
schema field was added carries a version hash over the older shape, and
re-stamping it would bump every such head on every deploy for no change.
The head's lifecycle ``state`` column is authoritative for the comparison
(the runtime's source circuit-breaker pauses a head by column, leaving the
body's ``identity.state`` and two annotation keys behind).

    unchanged     the head already carries the tree's content + state
    register      no head — a fresh register (draft, then the legal FSM walk
                  to the declared state)
    update        the head differs — the changed top-level keys are printed
    retire        a RETIRED.md ``retired`` row whose live head is not retired
    retired       a RETIRED.md row whose head is retired (or absent)
    parked        a RETIRED.md ``parked`` row (no head) — skipped
    failed        anything that cannot be reconciled (printed; exit 1)

``--profile DIR`` names the instance profile whose manifest
(``profiles/<id>/profile.yaml``, :mod:`legba.data.profile`) says which
descriptors directory, ``RETIRED.md``, live-state overlay, source catalog and
credibility seeds to walk. Unset, ``LEGBA_PROFILE`` decides, then the default
``profiles/geopolitics`` — which points at exactly the paths above, so the
default plan is today's plan (``tests/test_profile.py`` asserts the equality).

``--dry-run`` reads heads and prints the plan — ``register`` /
``would-update`` / ``would-retire`` / ``unchanged`` per head — and writes
NOTHING (no head, no vocabulary row, no credibility seed). Run it against a
live host after a roll: every live head should print ``unchanged``.

NO DESCRIPTOR EVENT (lane N-R). The walker writes heads direct-DB through
``_p17_registrar.open_registry``, whose registry has no bus connection, so a
registration emits no ``descriptor.*`` event: the runtime picks a new or
changed head up at the reconcile loop's periodic resync (5 minutes), not at
once. Allow up to ~5 minutes before expecting ``reconcile.action ...
create_actor`` for a head this walker registered.

Besides descriptor heads it seeds what the absorbed registrars seeded:
  * ``vocabulary_entries`` rows for the non-builtin analyst kinds
    (:data:`NON_BUILTIN_ANALYST_KINDS`; ON CONFLICT DO NOTHING),
  * ``vocabulary_entries`` rows for the four TAXON families (W7 TAXON-1:
    ``salience_class``, ``collection_gap_kind``, ``band``, ``source_layer``)
    from their code literals in :mod:`legba.data.taxon_families` (ON CONFLICT
    DO NOTHING — an operator's deprecation or note edit wins),
  * ``vocabulary_entries`` rows from every vocabulary seed file
    (``descriptors/vocabulary_<family>.yaml``, ``identity.schema_uri``
    ``legba/vocabulary/x.y.z``; the tree's parent / aliases / notes win, an
    operator-deprecated row stays deprecated — :func:`seed_vocabulary`), and
  * ``source_credibility`` rows from every source-batch module's
    ``seed_credibility`` (ON CONFLICT DO NOTHING — operator overrides win).
The P2-T7 bounded-unit drift guard runs over every analyst before any write.

Transport: direct-DB via :mod:`_p17_registrar` (``DescriptorRegistry`` against
``LEGBA_DATA_PG_DB``; unset defaults to ``legba_pivot_test`` — deploy.sh pins
the app DB). Usage::

    python scripts/bringup_register_descriptors.py [--profile DIR] [--dry-run] [--family F ...]
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

import yaml

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
from _p17_registrar import (  # noqa: E402
    ACTOR,
    ACTOR_ROLE,
    Family,
    close_registry,
    open_registry,
    register_descriptor,
)

from legba.data.profile import Profile, ProfileError, load_profile, resolve_profile  # noqa: E402
from legba.data.schemas import content_hash  # noqa: E402
from legba.data.schemas.analyst import register_analyst_kind  # noqa: E402
from legba.data.taxon_families import taxon_seed_rows  # noqa: E402

REPO_ROOT = _SCRIPTS_DIR.parent
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"
RETIRED_MD = DESCRIPTORS_DIR / "RETIRED.md"
LIFECYCLE_MD = DESCRIPTORS_DIR / "LIFECYCLE.md"

#: The registry families this walker owns, in registration order: packs and
#: sources first (analysts grant packs; targets select sources), analysts last.
WALKED_FAMILIES: tuple[str, ...] = ("action_pack", "source", "target", "analyst")

#: Analyst kinds outside the closed ``AnalystKind`` enum. The registry learns
#: them from ``vocabulary_entries`` (family ``analyst_kind``); a fresh database
#: carries ``inquiry`` (0217) and ``situation_tracker`` (0184) from migrations
#: and the other three only from a bring-up insert. All five are upserted.
NON_BUILTIN_ANALYST_KINDS: tuple[str, ...] = (
    "journal_assessor",
    "entity_researcher",
    "signal_salience",
    "inquiry",
    "situation_tracker",
)

#: The source-batch modules whose ``seed_credibility(pg)`` the walker runs
#: (each an ON CONFLICT DO NOTHING insert into ``source_credibility``).
CREDIBILITY_MODULES: tuple[str, ...] = (
    "bringup_register_source_catalog",
    "bringup_register_wave_a_sources",
    "bringup_register_rsshub_sources",
    "bringup_register_source_batch_2026_08",
    "bringup_register_source_batch_2026_09",
    "bringup_register_supply_chain_sources",
)

CATALOG_ORIGIN = "catalog"
_STATE_ROW_RE = re.compile(
    r"^\|\s*([a-z_]+)\s*\|\s*([^|\s]+)\s*\|\s*([a-z_]+)\s*\|\s*([a-z_]+)\s*\|\s*$"
)
_ROW_RE = re.compile(
    r"^\|\s*(retired|parked)\s*\|\s*([a-z_]+)\s*\|\s*([^|\s]+)\s*\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*$"
)


class PlanError(RuntimeError):
    """The tree cannot be walked as declared (a duplicate id, an unlisted
    retired file, a RETIRED.md row naming nothing). Fails before any DB I/O."""


@dataclass(frozen=True)
class Planned:
    family: str
    descriptor_id: str
    origin: str                     # descriptors/<file> or "catalog"
    body: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Excluded:
    status: str                     # retired | parked
    family: str
    descriptor_id: str
    origin: str
    why: str


@dataclass
class Decision:
    action: str                     # unchanged/register/update/retire/retired/parked/failed
    family: str
    descriptor_id: str
    origin: str
    version: str = "-"
    detail: str = ""
    model: Any = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# The plan — pure (no DB): what the tree says this instance registers
# ---------------------------------------------------------------------------


def family_of(body: dict[str, Any]) -> str | None:
    """``legba/<family>/x.y.z`` → ``<family>``; None for a non-descriptor."""
    identity = body.get("identity") if isinstance(body, dict) else None
    if not isinstance(identity, dict):
        return None
    parts = str(identity.get("schema_uri") or "").split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "legba" else None


def load_exclusions(path: pathlib.Path = RETIRED_MD) -> dict[tuple[str, str], Excluded]:
    """Parse the ``retired`` / ``parked`` table rows of ``descriptors/RETIRED.md``."""
    out: dict[tuple[str, str], Excluded] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        status, family, did, origin, why = m.groups()
        key = (family, did)
        if key in out:
            raise PlanError(f"RETIRED.md lists {family}/{did} twice")
        out[key] = Excluded(status, family, did, origin.strip(), why)
    return out


def lifecycle_rows_dir(path: pathlib.Path) -> pathlib.Path:
    """R-1: the per-row directory beside an overlay file (``LIFECYCLE.md`` ->
    ``LIFECYCLE.d/``). One file per row, ``<family>__<id>.md``, holding that
    row's table line — so two lanes recording different flips touch different
    files instead of the same hot table."""
    return path.with_suffix(".d")


def lifecycle_row_file_name(family: str, did: str) -> str:
    return f"{family}__{did}.md"


def load_lifecycle(path: pathlib.Path = LIFECYCLE_MD) -> dict[tuple[str, str], tuple[str, str]]:
    """``(family, id) -> (host_state, file_ships)`` from ``descriptors/LIFECYCLE.md``
    and its row directory ``descriptors/LIFECYCLE.d/`` (R-1). A row named twice —
    in either place, or both — is a :class:`PlanError`, as is a row file that is
    not exactly one row or whose name does not match its row."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    origin: dict[tuple[str, str], str] = {}

    def _add(family: str, did: str, state: str, ships: str, where: str) -> None:
        if (family, did) in out:
            raise PlanError(
                f"LIFECYCLE lists {family}/{did} twice ({origin[(family, did)]} and {where})")
        out[(family, did)] = (state, ships)
        origin[(family, did)] = where

    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _STATE_ROW_RE.match(line)
            if not m or m.group(1) == "family":
                continue
            _add(*m.groups(), where=path.name)
    rows_dir = lifecycle_rows_dir(path)
    if rows_dir.is_dir():
        for row_file in sorted(rows_dir.glob("*.md")):
            lines = [ln for ln in row_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            m = _STATE_ROW_RE.match(lines[0]) if len(lines) == 1 else None
            if not m or m.group(1) == "family":
                raise PlanError(
                    f"{rows_dir.name}/{row_file.name} must hold exactly one row "
                    "'| family | id | state | file ships |'")
            family, did, state, ships = m.groups()
            if row_file.name != lifecycle_row_file_name(family, did):
                raise PlanError(
                    f"{rows_dir.name}/{row_file.name} holds the row for {family}/{did}; "
                    f"name it {lifecycle_row_file_name(family, did)}")
            _add(family, did, state, ships, where=f"{rows_dir.name}/{row_file.name}")
    return out


def _origin(path: pathlib.Path) -> str:
    """A descriptor file's origin label: repo-relative when inside the repo."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _yaml_descriptors(descriptors_dir: pathlib.Path) -> Iterable[tuple[str, str, str, dict]]:
    for p in sorted(descriptors_dir.glob("*.yaml")):
        body = yaml.safe_load(p.read_text(encoding="utf-8"))
        fam = family_of(body)
        if fam not in WALKED_FAMILIES:
            continue
        yield fam, str(body["identity"]["id"]), _origin(p), body


def _catalog_entries(path: pathlib.Path | None = None) -> tuple[Any, ...]:
    """The source-catalog entries (ROSTER-1: profile data): the selected
    profile's catalog (``None``), or a profile's own catalog YAML file, read
    by ``bringup_register_source_catalog.load_catalog``."""
    import bringup_register_source_catalog as catalog

    if path is None:
        return catalog.CATALOG
    try:
        return catalog.load_catalog(path)
    except (ProfileError, ValueError) as exc:
        raise PlanError(f"cannot load the source catalog {path}: {exc}") from exc


def _catalog_descriptors(path: pathlib.Path | None = None) -> Iterable[tuple[str, str, str, dict]]:
    import bringup_register_source_catalog as catalog

    for entry in _catalog_entries(path):
        desc = catalog.build_descriptor(entry)
        yield "source", entry.id, CATALOG_ORIGIN, desc.model_dump(mode="json", by_alias=True)


def _credibility_kwargs(mod: Any, profile: Profile | None) -> dict[str, Any]:
    """The source-catalog module seeds the PROFILE's catalog (its own file under
    ``--profile``), not whichever one the module loaded at import."""
    if profile is None or not hasattr(mod, "load_catalog"):
        return {}
    path = profile.source_catalog_path
    return {"entries": _catalog_entries(path) if path is not None else ()}


def plan(
    descriptors_dir: pathlib.Path = DESCRIPTORS_DIR,
    *,
    retired_md: pathlib.Path | None = None,
    lifecycle_md: pathlib.Path | None = None,
    include_catalog: bool = True,
    profile: Profile | None = None,
) -> tuple[list[Planned], list[Excluded]]:
    """Every descriptor the tree registers, and every one it deliberately skips.

    With ``profile`` the manifest supplies every input instead: its
    ``descriptors_dir`` (and that directory's ``RETIRED.md``), its
    ``lifecycle`` overlay (none when it declares ``null``), and its source
    catalog (none when ``rosters.source_catalog`` is ``null``); the
    orphan-row checks always run for a profile plan.

    Raises :class:`PlanError` on a duplicate ``(family, id)``, on a tree file
    declaring ``state: retired`` that RETIRED.md does not list, on a
    RETIRED.md row that names no tree file or catalog entry, and on a
    LIFECYCLE.md row that names nothing planned or that disagrees with the
    state the file ships (its ``file ships`` column) or repeats it.
    """
    catalog_path: pathlib.Path | None = None
    strict = include_catalog
    if profile is not None:
        descriptors_dir = profile.descriptors_path
        exclusions = load_exclusions(profile.retired_path)
        lifecycle = load_lifecycle(profile.lifecycle_path) if profile.lifecycle_path else {}
        catalog_path = profile.source_catalog_path
        include_catalog, strict = catalog_path is not None, True
    else:
        exclusions = load_exclusions(retired_md or descriptors_dir / "RETIRED.md")
        lifecycle = load_lifecycle(lifecycle_md or descriptors_dir / "LIFECYCLE.md")
    sources: list[tuple[str, str, str, dict]] = list(_yaml_descriptors(descriptors_dir))
    if include_catalog:
        sources += list(_catalog_descriptors(catalog_path))

    seen: dict[tuple[str, str], str] = {}
    planned: list[Planned] = []
    excluded: list[Excluded] = []
    for fam, did, origin, body in sources:
        key = (fam, did)
        if key in seen:
            raise PlanError(f"{fam}/{did} is declared twice: {seen[key]} and {origin}")
        seen[key] = origin
        if key in exclusions:
            excluded.append(exclusions[key])
            continue
        state = str((body.get("identity") or {}).get("state") or "")
        if state == "retired":
            raise PlanError(
                f"{origin} declares state: retired but descriptors/RETIRED.md does "
                f"not list {fam}/{did} — add a `retired` row (the walker never "
                "registers a retired descriptor)"
            )
        body = copy.deepcopy(body)
        body.setdefault("identity", {})["version"] = "0" * 16
        if key in lifecycle:
            host_state, ships = lifecycle[key]
            shipped = str(getattr(body["identity"].get("state"), "value", body["identity"].get("state")))
            if ships != shipped or host_state == shipped:
                raise PlanError(
                    f"LIFECYCLE.md row {fam}/{did} says the file ships {ships} and the host "
                    f"holds {host_state}, but {origin} ships {shipped}"
                )
            body["identity"]["state"] = host_state
        planned.append(Planned(fam, did, origin, body))

    if strict:
        orphans = sorted(f"{f}/{d}" for (f, d) in exclusions if (f, d) not in seen)
        if orphans:
            raise PlanError(f"RETIRED.md rows name no tree descriptor: {orphans}")
        planned_keys = {(p.family, p.descriptor_id) for p in planned}
        stray = sorted(f"{f}/{d}" for (f, d) in lifecycle if (f, d) not in planned_keys)
        if stray:
            raise PlanError(f"LIFECYCLE.md rows name nothing the walker registers: {stray}")

    order = {f: i for i, f in enumerate(WALKED_FAMILIES)}
    planned.sort(key=lambda p: (order[p.family], p.origin == CATALOG_ORIGIN, p.descriptor_id))
    excluded.sort(key=lambda e: (order.get(e.family, 99), e.descriptor_id))
    return planned, excluded


def register_kinds_in_process() -> None:
    """Teach this process's schema validator the non-builtin analyst kinds."""
    for kind in NON_BUILTIN_ANALYST_KINDS:
        register_analyst_kind(kind)


def validate(p: Planned) -> Any:
    """The tree descriptor as its family's pydantic model (raises on invalid)."""
    return Family(p.family).model.model_validate(p.body, strict=False)


def assert_unit_eval_coverage(planned: list[Planned]) -> None:
    """The P2-T7 bounded-unit drift guard, over every planned analyst."""
    from bringup_register_analysts import _assert_unit_eval_coverage

    _assert_unit_eval_coverage(
        [(p.origin, p.body) for p in planned if p.family == "analyst"]
    )


# ---------------------------------------------------------------------------
# Decide — read the heads, compare by content
# ---------------------------------------------------------------------------


def normalize_head(family: str, body: Any, state: str) -> Any:
    """A stored head body as the CURRENT schema's model, with the head's state.

    Top-level keys the model does not declare are runtime annotations (the
    source circuit-breaker's ``auto_paused_at`` / ``auto_paused_reason``) and
    are dropped before validation; the ``state`` column wins over the body's
    ``identity.state``.
    """
    if isinstance(body, str):
        body = json.loads(body)
    body = json.loads(json.dumps(body))
    cls = Family(family).model
    names = set(cls.model_fields) | {f.alias for f in cls.model_fields.values() if f.alias}
    for k in [k for k in body if k not in names]:
        body.pop(k)
    identity = body.setdefault("identity", {})
    identity["version"] = "0" * 16
    identity["state"] = state
    return cls.model_validate(body, strict=False)


def changed_keys(a: Any, b: Any) -> list[str]:
    """The dotted paths (two levels deep) where two models' dumps differ."""
    da = a.model_dump(mode="json", by_alias=True, exclude={"identity": {"version"}})
    db = b.model_dump(mode="json", by_alias=True, exclude={"identity": {"version"}})
    out: list[str] = []
    for k in sorted(set(da) | set(db)):
        va, vb = da.get(k), db.get(k)
        if va == vb:
            continue
        if isinstance(va, dict) and isinstance(vb, dict):
            out += [f"{k}.{s}" for s in sorted(set(va) | set(vb)) if va.get(s) != vb.get(s)]
        else:
            out.append(k)
    return out


async def read_heads(pg: Any) -> dict[tuple[str, str], tuple[str, str, Any]]:
    """``(family, id) -> (version, state, body)`` for every head of the walked families."""
    heads: dict[tuple[str, str], tuple[str, str, Any]] = {}
    async with pg.acquire() as conn:
        for fam in WALKED_FAMILIES:
            rows = await conn.fetch(
                f"SELECT descriptor_id, version, state, body FROM {Family(fam).table} "
                "WHERE is_head"
            )
            for r in rows:
                heads[(fam, r["descriptor_id"])] = (r["version"], r["state"], r["body"])
    return heads


def decide(
    planned: list[Planned],
    excluded: list[Excluded],
    heads: dict[tuple[str, str], tuple[str, str, Any]],
) -> list[Decision]:
    """One decision per planned / excluded descriptor (pure over ``heads``)."""
    out: list[Decision] = []
    for p in planned:
        try:
            model = validate(p)
        except Exception as exc:  # noqa: BLE001
            out.append(Decision("failed", p.family, p.descriptor_id, p.origin,
                                detail=f"invalid: {type(exc).__name__}: {exc}"[:300]))
            continue
        head = heads.get((p.family, p.descriptor_id))
        if head is None:
            out.append(Decision("register", p.family, p.descriptor_id, p.origin,
                                detail=f"state {model.identity.state.value}", model=model))
            continue
        version, state, body = head
        if state == "retired":
            out.append(Decision(
                "failed", p.family, p.descriptor_id, p.origin, version,
                detail="the live head is retired (terminal) — add a `retired` row "
                       "to descriptors/RETIRED.md",
            ))
            continue
        try:
            live = normalize_head(p.family, body, state)
        except Exception as exc:  # noqa: BLE001
            out.append(Decision("failed", p.family, p.descriptor_id, p.origin, version,
                                detail=f"live head no longer parses: {type(exc).__name__}: {exc}"[:300]))
            continue
        if content_hash(live) == content_hash(model):
            out.append(Decision("unchanged", p.family, p.descriptor_id, p.origin, version))
        else:
            out.append(Decision("update", p.family, p.descriptor_id, p.origin, version,
                                detail=", ".join(changed_keys(live, model)), model=model))
    for e in excluded:
        head = heads.get((e.family, e.descriptor_id))
        if e.status == "retired":
            if head is None or head[1] == "retired":
                out.append(Decision("retired", e.family, e.descriptor_id, e.origin,
                                    head[0] if head else "-",
                                    detail="retired live" if head else "no head"))
            else:
                out.append(Decision("retire", e.family, e.descriptor_id, e.origin, head[0],
                                    detail=f"live head is {head[1]}; RETIRED.md: {e.why}"))
        else:  # parked
            if head is None:
                out.append(Decision("parked", e.family, e.descriptor_id, e.origin))
            else:
                out.append(Decision(
                    "failed", e.family, e.descriptor_id, e.origin, head[0],
                    detail=f"RETIRED.md parks it but a live {head[1]} head exists — "
                           "delete the parked row so the walker owns it",
                ))
    return out


# ---------------------------------------------------------------------------
# Apply + report
# ---------------------------------------------------------------------------

_MARK = {"unchanged": "=", "register": "+", "update": "~", "retire": "x",
         "retired": ".", "parked": ".", "failed": "!"}
_DRY_LABEL = {"update": "would-update", "retire": "would-retire"}
_DONE_LABEL = {"register": "registered", "update": "updated", "retire": "retired"}


def print_decisions(decisions: list[Decision], *, dry_run: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in decisions:
        label = (_DRY_LABEL if dry_run else _DONE_LABEL).get(d.action, d.action)
        counts[label] = counts.get(label, 0) + 1
        line = (f"  {_MARK.get(d.action, '?')} {label:>12}  {d.family}/{d.descriptor_id}"
                f"  @ {d.version[:16]}  ({d.origin})")
        if d.detail:
            line += f"  [{d.detail}]"
        print(line)
    print("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return counts


async def _vocabulary_missing(pg: Any) -> list[str]:
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT value FROM vocabulary_entries WHERE family = 'analyst_kind'"
        )
    have = {r["value"] for r in rows}
    return [k for k in NON_BUILTIN_ANALYST_KINDS if k not in have]


async def _taxon_missing(pg: Any) -> list[tuple[str, str, str]]:
    """The TAXON seed rows (family, value, notes) this database lacks."""
    seeds = taxon_seed_rows()
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT family, value FROM vocabulary_entries WHERE family = ANY($1::text[])",
            sorted({f for f, _, _ in seeds}),
        )
    have = {(r["family"], r["value"]) for r in rows}
    return [s for s in seeds if (s[0], s[1]) not in have]


async def seed_taxon_families(pg: Any, *, dry_run: bool) -> list[tuple[str, str, str]]:
    """Seed the four TAXON families from their literals (idempotent; ON
    CONFLICT DO NOTHING). Returns the rows that were missing — on a dry run,
    the rows that WOULD be added; nothing is written."""
    missing = await _taxon_missing(pg)
    by_family: dict[str, list[str]] = {}
    for fam, val, _ in missing:
        by_family.setdefault(fam, []).append(val)
    if dry_run:
        if missing:
            print(f"vocabulary: would add taxon values {by_family}")
        return missing
    if missing:
        async with pg.acquire() as conn:
            await conn.executemany(
                "INSERT INTO vocabulary_entries (family, value, introduced, notes) "
                "VALUES ($1, $2, now(), $3) ON CONFLICT (family, value) DO NOTHING",
                missing,
            )
        print(f"vocabulary: added taxon values {by_family}")
    return missing


# ---------------------------------------------------------------------------
# Vocabulary seeds — descriptors/vocabulary_<family>.yaml (W7 FRAME-1)
# ---------------------------------------------------------------------------

#: ``identity.schema_uri`` family of a vocabulary seed file. Not a walked
#: registry family: the plan skips these files and :func:`load_vocabulary_seeds`
#: reads them.
VOCABULARY_SEED_FAMILY = "vocabulary"


@dataclass(frozen=True)
class VocabSeed:
    family: str
    value: str
    parent: str | None
    aliases: tuple[str, ...]
    notes: str | None
    origin: str


def load_vocabulary_seeds(descriptors_dir: pathlib.Path = DESCRIPTORS_DIR) -> list[VocabSeed]:
    """Every entry of every vocabulary seed file, validated through the entry
    model (family admission, value shape, parent shape). Raises
    :class:`PlanError` on an invalid entry or a duplicate ``(family, value)``."""
    from datetime import datetime, timezone

    from legba.data.schemas.vocabulary import VocabularyEntry

    seeds: list[VocabSeed] = []
    seen: dict[tuple[str, str], str] = {}
    for p in sorted(descriptors_dir.glob("*.yaml")):
        body = yaml.safe_load(p.read_text(encoding="utf-8"))
        if family_of(body) != VOCABULARY_SEED_FAMILY:
            continue
        origin = f"descriptors/{p.name}"
        fam = str(body.get("family") or "")
        for raw in body.get("entries") or []:
            try:
                e = VocabularyEntry(
                    family=fam, value=str(raw["value"]),
                    schema_uri=str(body["identity"]["schema_uri"]),
                    introduced=datetime.now(timezone.utc),
                    aliases=[str(a) for a in raw.get("aliases") or []],
                    parent=raw.get("parent"), notes=raw.get("notes"),
                )
            except Exception as exc:  # noqa: BLE001
                raise PlanError(f"{origin}: invalid vocabulary entry {raw!r}: {exc}") from exc
            key = (e.family, e.value)
            if key in seen:
                raise PlanError(f"vocabulary {fam}/{e.value} is declared twice: {seen[key]} and {origin}")
            seen[key] = origin
            seeds.append(VocabSeed(e.family, e.value, e.parent, tuple(e.aliases), e.notes, origin))
    return seeds


async def seed_vocabulary(pg: Any, seeds: list[VocabSeed], *, dry_run: bool) -> dict[str, int]:
    """Upsert the seeds: absent → insert; present with a different parent /
    aliases / notes → update (the tree wins); otherwise unchanged. A row an
    operator DEPRECATED is left deprecated (printed, never revived)."""
    counts = {"register": 0, "update": 0, "unchanged": 0, "deprecated": 0}
    if not seeds:
        return counts
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT family, value, parent, aliases, notes, deprecated FROM vocabulary_entries "
            "WHERE family = ANY($1::text[])", sorted({s.family for s in seeds}),
        )
        have = {(r["family"], r["value"]): r for r in rows}
        for s in seeds:
            row = have.get((s.family, s.value))
            if row is None:
                action = "register"
            elif row["deprecated"] is not None:
                action = "deprecated"
            elif (row["parent"], tuple(row["aliases"] or ()), row["notes"]) != (s.parent, s.aliases, s.notes):
                action = "update"
            else:
                action = "unchanged"
            counts[action] += 1
            if action in ("register", "update"):
                print(f"  {'+' if action == 'register' else '~'} vocabulary "
                      f"{'would-' + action if dry_run else action}  {s.family}/{s.value}  ({s.origin})")
            if dry_run or action not in ("register", "update"):
                continue
            await conn.execute(
                "INSERT INTO vocabulary_entries (family, value, schema_uri, introduced, notes, aliases, parent) "
                "VALUES ($1, $2, 'legba/vocabulary/1.0.0', now(), $3, $4::text[], $5) "
                "ON CONFLICT (family, value) DO UPDATE SET notes = EXCLUDED.notes, "
                "aliases = EXCLUDED.aliases, parent = EXCLUDED.parent",
                s.family, s.value, s.notes, list(s.aliases), s.parent,
            )
    print("vocabulary seeds: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


async def _credibility_missing(
    pg: Any, modules: tuple[str, ...] = CREDIBILITY_MODULES, profile: Profile | None = None,
) -> int:
    import importlib

    hosts: set[str] = set()
    if not modules:
        return 0
    for name in modules:
        mod = importlib.import_module(name)
        rows = (
            mod.credibility_rows(**_credibility_kwargs(mod, profile))
            if hasattr(mod, "credibility_rows") else mod.CREDIBILITY_SEEDS
        )
        hosts |= {r[0] for r in rows}
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_host FROM source_credibility WHERE source_host = ANY($1::text[])",
            sorted(hosts),
        )
    return len(hosts - {r["source_host"] for r in rows})


async def apply(pg: Any, reg: Any, decisions: list[Decision]) -> None:
    """Write every register / update / retire decision, in place."""
    from legba.data.registry.descriptor import Family as RegFamily

    for d in decisions:
        if d.action in ("register", "update"):
            res = await register_descriptor(pg, reg, family=RegFamily(d.family), descriptor=d.model)
            if res.action == "failed":
                d.action, d.detail = "failed", res.detail
            else:
                d.version = res.version
        elif d.action == "retire":
            try:
                row = await reg.retire(d.descriptor_id, ACTOR, family=RegFamily(d.family),
                                       actor_role=ACTOR_ROLE,
                                       reason="descriptors/RETIRED.md")
                d.version = row.version
            except Exception as exc:  # noqa: BLE001
                d.action, d.detail = "failed", f"{type(exc).__name__}: {exc}"[:300]


class UnknownIdError(PlanError):
    """``--id`` named a descriptor the walk does not plan."""


def limit_to_ids(
    planned: list[Planned], excluded: list[Excluded], ids: set[str],
) -> tuple[list[Planned], list[Excluded]]:
    """T-2: the plan narrowed to ``ids`` (a staged switch-on registers a few
    heads, not the tree). An id the walk does not plan is refused — a typo must
    not register nothing and report success."""
    known = {p.descriptor_id for p in planned} | {e.descriptor_id for e in excluded}
    unknown = sorted(ids - known)
    if unknown:
        raise UnknownIdError(f"--id names descriptors the walk does not plan: {unknown}")
    return ([p for p in planned if p.descriptor_id in ids],
            [e for e in excluded if e.descriptor_id in ids])


async def run(
    pg: Any, reg: Any, *, dry_run: bool, families: set[str] | None = None,
    profile: Profile | None = None, ids: set[str] | None = None,
) -> tuple[int, list[Decision]]:
    """The whole bring-up against an opened store + registry.

    ``profile`` selects the instance's manifest (``None`` = the module's own
    paths, which the default geopolitics profile reproduces). ``ids`` (T-2)
    limits the heads written to those descriptor ids — the vocabulary seeds
    still run (idempotent), the credibility seeds do not. Returns
    ``(exit_code, decisions)`` — 1 when any decision failed.
    """
    planned, excluded = plan(profile=profile) if profile is not None else plan()
    credibility = profile.rosters.credibility_seeds if profile is not None else CREDIBILITY_MODULES
    # validated before any DB I/O; a profile reads its own descriptors dir
    vocab_seeds = (
        load_vocabulary_seeds(profile.descriptors_path) if profile is not None
        else load_vocabulary_seeds()
    )
    if families:
        planned = [p for p in planned if p.family in families]
        excluded = [e for e in excluded if e.family in families]
    assert_unit_eval_coverage(planned)
    if ids:
        planned, excluded = limit_to_ids(planned, excluded, ids)

    missing_kinds = await _vocabulary_missing(pg)
    if dry_run:
        if missing_kinds:
            print(f"vocabulary: would add analyst_kind {missing_kinds}")
    else:
        async with pg.acquire() as conn:
            for kind in NON_BUILTIN_ANALYST_KINDS:
                await conn.execute(
                    "INSERT INTO vocabulary_entries (family, value, introduced) "
                    "VALUES ('analyst_kind', $1, now()) ON CONFLICT (family, value) DO NOTHING",
                    kind,
                )
        await reg.sync_analyst_kinds()
        if missing_kinds:
            print(f"vocabulary: added analyst_kind {missing_kinds}")
    # In-process too: a dry run must validate the extension kinds without a
    # vocabulary write, and sync_analyst_kinds() above replaced the set.
    register_kinds_in_process()
    await seed_taxon_families(pg, dry_run=dry_run)
    await seed_vocabulary(pg, vocab_seeds, dry_run=dry_run)
    if not dry_run:
        # K-1: the registry's vocabulary snapshot was taken when it opened,
        # BEFORE the seeds above landed. A descriptor declaring a family seeded
        # in this same run (a source's scope.classifications, a target's
        # scope.signal_tags) would otherwise be refused as "family unloaded"
        # on a fresh instance and bind only on a second run. The sync replaces
        # the extension-kind set, so the in-process kinds go back on after it.
        await reg.sync_analyst_kinds()
        register_kinds_in_process()

    decisions = decide(planned, excluded, await read_heads(pg))
    if not dry_run:
        await apply(pg, reg, decisions)
    print(f"Descriptor bring-up ({'DRY RUN — nothing written' if dry_run else 'applied'}):")
    counts = print_decisions(decisions, dry_run=dry_run)

    if not ids and (not families or "source" in families):
        if dry_run:
            print(f"source_credibility: {await _credibility_missing(pg, credibility, profile)} "
                  "seed host(s) would be inserted")
        else:
            import importlib

            ins = skip = 0
            for name in credibility:
                mod = importlib.import_module(name)
                i, s = await mod.seed_credibility(pg, **_credibility_kwargs(mod, profile))
                ins, skip = ins + i, skip + s
            print(f"source_credibility seeds: +{ins} inserted, ={skip} already present")
    return (1 if counts.get("failed") else 0), decisions


async def _amain(
    dry_run: bool, families: set[str] | None, profile: Profile | None = None,
    ids: set[str] | None = None,
) -> int:
    register_kinds_in_process()
    pg, reg = await open_registry()
    try:
        code, _ = await run(pg, reg, dry_run=dry_run, families=families, profile=profile,
                            ids=ids)
        return code
    finally:
        await close_registry(pg, reg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="read heads and print the plan; write nothing")
    ap.add_argument("--family", action="append", choices=WALKED_FAMILIES,
                    help="limit to one family (repeatable)")
    ap.add_argument("--profile", default=None,
                    help="instance profile directory, relative to the repo root "
                         "(default: $LEGBA_PROFILE, "
                         "else profiles/geopolitics)")
    ap.add_argument("--id", action="append", dest="ids", metavar="DESCRIPTOR_ID",
                    help="T-2: register only these descriptor ids (repeatable; "
                         "an id the walk does not plan is refused)")
    args = ap.parse_args(argv)
    profile = load_profile(resolve_profile(args.profile, root=REPO_ROOT))
    print(f"profile: {profile.id} ({profile.manifest_path})")
    return asyncio.run(_amain(args.dry_run, set(args.family) if args.family else None, profile,
                              set(args.ids) if args.ids else None))


if __name__ == "__main__":
    sys.exit(main())
