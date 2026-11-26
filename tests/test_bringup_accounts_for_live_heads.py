# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tree can rebuild every live head of the reference host.

A platform review found that
``deploy/deploy.sh`` registered 46 of 88 live analysts: the rest had been
registered by hand or by one of the bring-up scripts deploy.sh never calls, so
a fresh instance could not reproduce the host. This file is the accounting.

The input is a committed roster of every live head (family, id, kind, state —
no bodies), ``tests/fixtures/bringup/live_head_roster_2026-10-04.json``. For
every NON-retired head the tree must carry a registrar that deploy.sh runs:

  * a descriptor the walker (``scripts/bringup_register_descriptors.py``)
    plans — a ``descriptors/*.yaml`` of the head's family, or a source-catalog
    entry — when deploy.sh calls the walker; or
  * a ``descriptors/*.yaml`` a script deploy.sh calls names literally (the
    registrars before the walker), or the catalog when deploy.sh calls it; or
  * an explicit ``retired`` row in ``descriptors/RETIRED.md`` (a retire is an explicit
    decision).

A head that fails this is NAMED in the assertion. Pure: no database, no HTTP.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"
DEPLOY_SH = REPO_ROOT / "deploy" / "deploy.sh"
ROSTER_PATH = REPO_ROOT / "tests" / "fixtures" / "bringup" / "live_head_roster_2026-10-04.json"
WALKER = "scripts/bringup_register_descriptors.py"
CATALOG = "scripts/bringup_register_source_catalog.py"

# ``_p17_registrar`` (imported by the walker) sets a process-global
# LEGBA_DATA_PG_DB default at import — snapshot/restore so it can't leak.
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    try:
        import bringup_register_descriptors as walker
    except ImportError:  # the walker does not exist yet — the gap is the point
        walker = None
    import bringup_register_source_catalog as catalog
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)


def _roster() -> list[dict]:
    return json.loads(ROSTER_PATH.read_text(encoding="utf-8"))["heads"]


def _family_of(body: dict) -> str | None:
    parts = str((body.get("identity") or {}).get("schema_uri") or "").split("/")
    return parts[1] if len(parts) >= 3 else None


def _yaml_index() -> dict[str, tuple[str, str]]:
    """descriptors/<file>.yaml -> (family, id) for every registry descriptor file."""
    out: dict[str, tuple[str, str]] = {}
    for p in sorted(DESCRIPTORS_DIR.glob("*.yaml")):
        body = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(body, dict) and _family_of(body):
            out[p.name] = (_family_of(body), str(body["identity"]["id"]))
    return out


def deploy_called_scripts(text: str | None = None) -> list[str]:
    """Every ``scripts/*.py`` deploy.sh runs through ``run_registrar``,
    expanding the ``for X in a b c; do run_registrar "...${X}..."`` loops."""
    text = DEPLOY_SH.read_text(encoding="utf-8") if text is None else text
    joined = text.replace("\\\n", " ")
    called: list[str] = []
    for line in joined.splitlines():
        if line.lstrip().startswith("#"):
            continue
        called += re.findall(r'run_registrar\s+"?(scripts/[\w]+\.py)', line)
    for var, words, body in re.findall(r"for (\w+) in ([^;]+);\s*do(.*?)done", joined, re.S):
        for tmpl in re.findall(r'run_registrar\s+"?(scripts/[\w${}]+\.py)', body):
            for w in words.split():
                called.append(tmpl.replace("${" + var + "}", w).replace("$" + var, w))
    return [c for c in dict.fromkeys(called) if "$" not in c]


def registrar_coverage() -> dict[tuple[str, str], str]:
    """(family, id) -> the deploy-run registrar that registers it."""
    index = _yaml_index()
    cover: dict[tuple[str, str], str] = {}
    for script in deploy_called_scripts():
        if script == WALKER and walker is not None:
            planned, _ = walker.plan()
            for p in planned:
                cover.setdefault((p.family, p.descriptor_id), f"{WALKER} ({p.origin})")
            continue
        if script == CATALOG:
            for e in catalog.CATALOG:
                cover.setdefault(("source", e.id), CATALOG)
        path = REPO_ROOT / script
        if not path.exists():
            continue
        for fname in re.findall(r"[\w]+\.yaml", path.read_text(encoding="utf-8")):
            if fname in index:
                cover.setdefault(index[fname], script)
    return cover


def _retired_rows() -> dict[tuple[str, str], str]:
    from_md = {}
    md = DESCRIPTORS_DIR / "RETIRED.md"
    if md.exists():
        for line in md.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\|\s*(retired|parked)\s*\|\s*([a-z_]+)\s*\|\s*([^|\s]+)\s*\|", line)
            if m:
                from_md[(m.group(2), m.group(3))] = m.group(1)
    return from_md


# ---------------------------------------------------------------------------


def test_roster_fixture_is_ids_only():
    """The fixture carries no descriptor bodies (no seed data in the repo)."""
    heads = _roster()
    assert len(heads) >= 279
    assert all(set(h) == {"family", "id", "kind", "state"} for h in heads)


def test_every_live_head_has_a_registrar_deploy_runs():
    """THE accounting: a live non-retired head with no deploy-run registrar is a
    host the tree cannot rebuild. Names every gap."""
    cover = registrar_coverage()
    rows = _retired_rows()
    gaps = [
        f"{h['family']}/{h['id']} ({h['state']}, kind={h['kind']})"
        for h in _roster()
        if h["state"] != "retired"
        and (h["family"], h["id"]) not in cover
        and rows.get((h["family"], h["id"])) != "retired"
    ]
    assert not gaps, (
        f"{len(gaps)} live head(s) have no registrar deploy/deploy.sh runs "
        f"(deploy.sh calls: {deploy_called_scripts()}):\n  " + "\n  ".join(gaps)
    )


def test_deploy_calls_the_one_walker():
    """deploy.sh registers descriptors through the walker and nothing else."""
    called = deploy_called_scripts()
    assert WALKER in called, f"deploy.sh does not call {WALKER}: {called}"
    descriptor_registrars = [
        s for s in called
        if re.match(r"scripts/bringup_register_\w+\.py$", s) and s != WALKER
        and s != "scripts/bringup_register_stack.py"   # stack components, not a descriptor family
    ]
    assert not descriptor_registrars, (
        f"deploy.sh still calls per-family registrars beside the walker: {descriptor_registrars}"
    )


@pytest.mark.skipif(walker is None, reason="the walker does not exist yet")
def test_walker_plan_is_consistent_and_valid():
    """No duplicate id, no unlisted retired file, no orphan RETIRED.md row, and
    every planned descriptor validates through its family's pydantic model."""
    walker.register_kinds_in_process()
    planned, excluded = walker.plan()
    assert planned and excluded
    bad = []
    for p in planned:
        try:
            walker.validate(p)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{p.origin}: {type(exc).__name__}: {str(exc)[:200]}")
    assert not bad, "\n".join(bad)
    walker.assert_unit_eval_coverage(planned)


@pytest.mark.skipif(walker is None, reason="the walker does not exist yet")
def test_no_retired_head_can_be_resurrected():
    """A head retired on the reference host whose descriptor is still in the
    tree must carry a RETIRED.md `retired` row — else a fresh deploy registers
    it active again."""
    planned, _ = walker.plan()
    plan_ids = {(p.family, p.descriptor_id): p.origin for p in planned}
    resurrect = [
        f"{h['family']}/{h['id']} ({plan_ids[(h['family'], h['id'])]})"
        for h in _roster()
        if h["state"] == "retired" and (h["family"], h["id"]) in plan_ids
    ]
    assert not resurrect, "retired live, still registered by the walker:\n  " + "\n  ".join(resurrect)


@pytest.mark.skipif(walker is None, reason="the walker does not exist yet")
def test_no_live_head_is_parked_or_retired_by_the_tree():
    """SPLIT-1 retires nothing: no live non-retired head sits in a RETIRED.md row."""
    rows = _retired_rows()
    hit = [
        f"{h['family']}/{h['id']} ({h['state']}) is a `{rows[(h['family'], h['id'])]}` row"
        for h in _roster()
        if h["state"] != "retired" and (h["family"], h["id"]) in rows
    ]
    assert not hit, "\n".join(hit)


@pytest.mark.skipif(walker is None, reason="the walker does not exist yet")
def test_every_tree_analyst_kind_is_known_to_the_walker():
    """A non-builtin analyst kind the walker does not upsert would fail on a
    fresh database (the registry learns extension kinds from vocabulary_entries)."""
    from legba.data.schemas.analyst import AnalystKind

    builtin = {k.value for k in AnalystKind}
    planned, _ = walker.plan()
    kinds = {str(p.body["identity"]["kind"]) for p in planned if p.family == "analyst"}
    unknown = sorted(kinds - builtin - set(walker.NON_BUILTIN_ANALYST_KINDS))
    assert not unknown, f"analyst kinds the walker does not register: {unknown}"


def test_deploy_parser_expands_loops():
    """The parser the accounting relies on reads deploy.sh's loop form."""
    sample = (
        'info "x"; run_registrar scripts/a.py\n'
        "for det in one two \\\n  three; do\n"
        '  run_registrar "scripts/bringup_register_${det}.py"\n'
        "done\n"
        '# run_registrar scripts/commented.py\n'
    )
    assert deploy_called_scripts(sample) == [
        "scripts/a.py",
        "scripts/bringup_register_one.py",
        "scripts/bringup_register_two.py",
        "scripts/bringup_register_three.py",
    ]
