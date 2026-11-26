# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S1-T1 — retro-tag the existing country desks with scope applicability tags.

WHICH desks is the instance profile's business (ROSTER-2): :func:`desks` reads
them from the profile's own target files (``--profile``, else
``LEGBA_PROFILE``, else ``profiles/geopolitics`` — today's 32 desks).

The G20 (19) + watch (5) targets were registered CREATE-ONLY at bring-up
(``bringup_register_g20_country_targets.py`` / ``..._watch_country_targets.py``)
with only the coverage tags ``geopolitical`` / ``news`` / ``g20`` | ``watch``.
This script UPDATES each existing head via the registry REST surface to MERGE in
the new applicability vocabulary — WITHOUT clobbering the existing tags or any
other ``scope`` field:

  * a REGION tag — exactly one per desk, read off the desk's own target file
    (the tag a ``frame`` seed entry under the subject frame's parent kind
    selects; SPLIT-3); the Stream-2 region_composition frames read country
    compositions by this tag.
  * zero or more WATCH tags — the profile's ``constants.applicability_tags``
    the desk's file carries — the declarative unit-applicability matrix (S1-T3):
    per-unit predicates (proliferation -> ``nuclear_watch`` …) narrow unit
    fan-out from the blanket ``has_tag("g20") or has_tag("watch")`` to the
    desks that actually matter, capping LLM spend as the unit count grows.

``scope.tags`` is an already-accepted free-form snake_case field on the target
schema (``target._ScopeBase.tags``, pattern ``^[a-z][a-z0-9_]*$``, max 32) — so
this is a pure DATA change, NO ``data/schemas/*`` edit and NO registry+runtime
rebuild.

IDEMPOTENT: reads the current head, merges (order-preserving dedupe), and PUTs
back ONLY when the merge actually adds a tag. A re-run finds every tag already
present and reports ``unchanged`` (no PUT, no new version). The existing
``g20`` / ``watch`` coverage tags are preserved, so the units' current
subscription predicate keeps matching every desk (no desk loses coverage).

Usage:
    python scripts/bringup_tag_targets.py --dry-run   # print the per-desk diff, write nothing
    python scripts/bringup_tag_targets.py             # apply (PUT) the merged tags
    python scripts/bringup_tag_targets.py --profile profiles/<id> --dry-run

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_API_TOKEN`` (or ``LEGBA_REGISTRY_TOKEN``) — bearer token,
    resolved via ``scripts/_token.py`` (falls back to the .env line, then "dev").
"""
from __future__ import annotations

import argparse
import copy
import functools
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "src"))
from _bringup_http import registry_base, registry_client  # noqa: E402
from _token import resolve_token  # noqa: E402

from legba.data.profile import Profile, load_profile, resolve_profile  # noqa: E402

BASE = registry_base()
FAMILY = "target"

# ---------------------------------------------------------------------------
# Tag vocabulary + the per-desk assignment table (DATA — the whole design).
# ---------------------------------------------------------------------------
# WHICH desks: read from the instance profile (ROSTER-2), never a literal list
# — see :func:`desks`.

# WHICH tags (SPLIT-3, W9-F4): read from the profile too, never a literal
# table. Each desk's own target file already declares its tags; this script
# merges two kinds of them onto the live head:
#
#   * its GROUP tag — exactly one per desk: the tag a frame of the subject
#     frame's parent kind selects (``ui.frames``: the exemplar's ``country``
#     frame rolls up into ``region``; the frame seed
#     ``<descriptors_dir>/vocabulary_frame.yaml`` registers each region frame
#     with its member rule ``tag:region_<slug>``). The judgment calls the old
#     table carried (RU under Europe, TR under MENA) live in the desk files.
#   * its APPLICABILITY tags — the profile's ``constants.applicability_tags``
#     (the unit-applicability matrix: ``nuclear_watch`` /
#     ``conflict_active`` / ``sanctions_regime`` for the exemplar), in that
#     declared order, wherever the desk's file carries one.
#
# The module tables below are the selected profile's (``LEGBA_PROFILE``, else
# ``profiles/geopolitics``), resolved at import; ``--profile`` resolves its own.

#: A seed file's frame entry selects members with ``tag:<t>`` aliases.
_FRAME_SEED = "vocabulary_frame.yaml"
_TAG_SELECTOR = "tag:"


def _group_tags(profile: Profile) -> tuple[str, ...]:
    """The member tags of every frame the profile's frame seed registers under
    the subject frame's PARENT kind, in seed order; ``()`` when the profile
    has no subject frame, the frame no parent, or the seed no such frames."""
    frames = {f.id: f for f in profile.ui.frames}
    subject = frames.get(profile.constants.subject_frame or "")
    kind = subject.parent if subject is not None else None
    seed = profile.descriptors_path / _FRAME_SEED
    if kind is None or not seed.is_file():
        return ()
    doc = yaml.safe_load(seed.read_text(encoding="utf-8")) or {}
    return tuple(
        alias[len(_TAG_SELECTOR):]
        for entry in doc.get("entries") or ()
        if entry.get("parent") == f"frame:{kind}"
        for alias in entry.get("aliases") or ()
        if str(alias).startswith(_TAG_SELECTOR)
    )


def tag_tables(profile: Profile) -> tuple[dict[str, str], dict[str, list[str]]]:
    """``(group tag by iso2, applicability tags by iso2)`` for the profile's
    desks (:func:`desks`), read off each desk's own target file. A desk whose
    file carries no group tag, or more than one, is refused by name; a desk
    with no applicability tag is absent from the second table."""
    groups = set(_group_tags(profile))
    applicability = tuple(profile.constants.applicability_tags)
    region: dict[str, str] = {}
    watch: dict[str, list[str]] = {}
    for did, iso2, tags in _desk_plan(
        profile.descriptors_path, profile.retired_path, tuple(profile.rosters.scored_subject_tags),
    ):
        mine = [t for t in tags if t in groups]
        if len(mine) != 1:
            raise ValueError(f"{did}: exactly one group tag of {sorted(groups)} expected, got {mine}")
        region[iso2] = mine[0]
        flags = [t for t in applicability if t in tags]
        if flags:
            watch[iso2] = flags
    return region, watch


#: ``| retired | target | <id> |`` / ``| parked | target | <id> |`` rows of the
#: profile's ``RETIRED.md`` — the walker's exclusion list.
_EXCLUDED_ROW_RE = re.compile(r"^\|\s*(?:retired|parked)\s*\|\s*target\s*\|\s*([a-z0-9_]+)\s*\|")


@functools.lru_cache(maxsize=8)
def _desk_plan(
    descriptors_dir: Path, retired_md: Path, legs: tuple[str, ...],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    excluded: set[str] = set()
    if retired_md.is_file():
        for line in retired_md.read_text(encoding="utf-8").splitlines():
            m = _EXCLUDED_ROW_RE.match(line)
            if m:
                excluded.add(m.group(1))
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for path in sorted(descriptors_dir.glob("*.yaml")):
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
        identity = body.get("identity") if isinstance(body, dict) else None
        if not isinstance(identity, dict) or not str(identity.get("schema_uri") or "").startswith("legba/target/"):
            continue
        did = str(identity["id"])
        if did in excluded or identity.get("state") == "retired":
            continue
        scope = body.get("scope") or {}
        if not set(legs) & set(scope.get("tags") or ()):
            continue
        geo = list(scope.get("geo") or ())
        if len(geo) != 1:
            raise ValueError(f"{path.name}: a scored-leg desk names exactly one country, got {geo}")
        out.append((did, str(geo[0]), tuple(scope.get("tags") or ())))
    return tuple(out)


def desks(profile: Profile | None = None) -> list[tuple[str, str]]:
    """The (descriptor_id, iso2) pairs this script tags — READ FROM THE PROFILE
    (ROSTER-2; Review A §2.5 row 3), never a literal list: every target file
    the profile's walker registers (its ``descriptors_dir`` minus its
    ``RETIRED.md``) that carries one of the profile's SCORED subject-roster
    tags (``rosters.scored_subject_tags`` — the per-country legs; the other
    leg's lanes and flows span several countries and take no region tag from
    this table), keyed by its one ``scope.geo`` code. ``None`` = the selected
    profile (``LEGBA_PROFILE``, else ``profiles/geopolitics``)."""
    if profile is None:
        profile = load_profile(resolve_profile(root=REPO_ROOT))
    return [(did, iso2) for did, iso2, _tags in _desk_plan(
        profile.descriptors_path, profile.retired_path, tuple(profile.rosters.scored_subject_tags),
    )]


_SELECTED = load_profile(resolve_profile(root=REPO_ROOT))
#: The group (region) tags and the applicability (watch) tags this script may
#: assign — the closed vocabulary, from the profile.
REGION_TAGS: tuple[str, ...] = _group_tags(_SELECTED)
WATCH_TAGS: tuple[str, ...] = tuple(_SELECTED.constants.applicability_tags)
TAG_VOCABULARY = frozenset(REGION_TAGS + WATCH_TAGS)
#: iso2 -> its group tag / its applicability tags, off the desks' own files.
REGION_BY_ISO2, WATCH_TAGS_BY_ISO2 = tag_tables(_SELECTED)

# Mirrors target._ScopeTag (pattern + length). A local guard so a typo in a
# profile file fails at import, not on a 422 from the registry.
_SCOPE_TAG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _self_check_vocabulary() -> None:
    for tag in TAG_VOCABULARY:
        if not _SCOPE_TAG_RE.match(tag) or len(tag) > 64:
            raise ValueError(f"illegal ScopeTag in vocabulary: {tag!r}")
    # Every value used in the tables must be in the declared vocabulary.
    for iso2, tag in REGION_BY_ISO2.items():
        if tag not in REGION_TAGS:
            raise ValueError(f"{iso2}: region tag {tag!r} not in REGION_TAGS")
    for iso2, tags in WATCH_TAGS_BY_ISO2.items():
        for tag in tags:
            if tag not in WATCH_TAGS:
                raise ValueError(f"{iso2}: watch tag {tag!r} not in WATCH_TAGS")


_self_check_vocabulary()


Tables = tuple[dict[str, str], dict[str, list[str]]]


def tags_for_iso2(iso2: str, tables: Tables | None = None) -> list[str]:
    """Region tag (always) + any watch tags for a desk's ISO-3166-1 alpha-2,
    from ``tables`` (:func:`tag_tables`; default: the selected profile's)."""
    region_by, watch_by = tables if tables is not None else (REGION_BY_ISO2, WATCH_TAGS_BY_ISO2)
    region = region_by.get(iso2)
    if region is None:
        raise KeyError(f"no region tag assigned for {iso2!r}")
    return [region, *watch_by.get(iso2, [])]


# ---------------------------------------------------------------------------
# Pure merge / planning logic (no IO — unit-tested directly).
# ---------------------------------------------------------------------------
def merge_tags(existing: list[str], add: list[str]) -> list[str]:
    """Order-preserving dedupe merge: keep ``existing`` verbatim, then append
    each tag in ``add`` that is not already present. Never drops a tag."""
    merged = list(existing)
    seen = set(existing)
    for tag in add:
        if tag not in seen:
            merged.append(tag)
            seen.add(tag)
    return merged


@dataclass
class DeskPlan:
    descriptor_id: str
    iso2: str
    current_tags: list[str]
    add_tags: list[str]
    merged_tags: list[str]
    new_body: dict
    changed: bool
    # Which of add_tags were genuinely new (the diff surfaced in --dry-run).
    newly_added: list[str] = field(default_factory=list)


def build_plan(descriptor_id: str, iso2: str, body: dict, tables: Tables | None = None) -> DeskPlan:
    """Compute the merged-tags plan for one desk from its current head ``body``.

    Only ``scope.tags`` is touched; every other scope/body field is carried
    through verbatim (deep-copied so the caller's input is never mutated).
    """
    add = tags_for_iso2(iso2, tables)
    scope = body.get("scope") or {}
    current = list(scope.get("tags") or [])
    merged = merge_tags(current, add)

    new_body = copy.deepcopy(body)
    new_body.setdefault("scope", {})["tags"] = merged

    return DeskPlan(
        descriptor_id=descriptor_id,
        iso2=iso2,
        current_tags=current,
        add_tags=add,
        merged_tags=merged,
        new_body=new_body,
        changed=merged != current,
        newly_added=[t for t in merged if t not in current],
    )


# ---------------------------------------------------------------------------
# Apply loop (IO injected as callables so it unit-tests with a fake registry).
# ---------------------------------------------------------------------------
@dataclass
class DeskResult:
    descriptor_id: str
    iso2: str
    action: str          # updated / unchanged / would_update / missing / failed
    current_tags: list[str] = field(default_factory=list)
    merged_tags: list[str] = field(default_factory=list)
    newly_added: list[str] = field(default_factory=list)
    version: str = "-"
    detail: str = ""


def run(
    *,
    get_body,
    put_body,
    desk_list: list[tuple[str, str]] | None = None,
    dry_run: bool = False,
    tables: Tables | None = None,
) -> list[DeskResult]:
    """Read → merge → (PUT) each desk. ``get_body(id) -> body|None`` and
    ``put_body(id, body) -> version`` are the only IO; both are injected so the
    loop is a pure function of the registry contents under test."""
    results: list[DeskResult] = []
    for descriptor_id, iso2 in (desk_list if desk_list is not None else desks()):
        try:
            body = get_body(descriptor_id)
        except Exception as exc:  # noqa: BLE001
            results.append(DeskResult(
                descriptor_id, iso2, "failed",
                detail=f"GET {type(exc).__name__}: {exc}"[:200],
            ))
            continue
        if body is None:
            results.append(DeskResult(
                descriptor_id, iso2, "missing",
                detail="no head row for desk (not registered yet)",
            ))
            continue

        plan = build_plan(descriptor_id, iso2, body, tables)
        common = dict(
            current_tags=plan.current_tags,
            merged_tags=plan.merged_tags,
            newly_added=plan.newly_added,
        )

        if not plan.changed:
            results.append(DeskResult(descriptor_id, iso2, "unchanged", **common))
            continue
        if dry_run:
            results.append(DeskResult(descriptor_id, iso2, "would_update", **common))
            continue
        try:
            version = put_body(descriptor_id, plan.new_body)
        except Exception as exc:  # noqa: BLE001
            results.append(DeskResult(
                descriptor_id, iso2, "failed", **common,
                detail=f"PUT {type(exc).__name__}: {exc}"[:200],
            ))
            continue
        results.append(DeskResult(
            descriptor_id, iso2, "updated", version=version, **common,
        ))
    return results


def print_results(results: list[DeskResult], *, dry_run: bool) -> int:
    """Human-readable per-desk report; returns the count of missing/failed."""
    mark = {
        "updated": "~", "unchanged": "=", "would_update": "+",
        "missing": "?", "failed": "!",
    }
    title = f"Retro-tag {len(results)} country desks (DRY-RUN — no writes):" if dry_run \
        else f"Retro-tag {len(results)} country desks:"
    print(title)
    bad = 0
    for r in results:
        glyph = mark.get(r.action, "?")
        line = f"  {glyph} {r.action:>12}  {FAMILY}/{r.descriptor_id}"
        if r.action in ("would_update", "updated"):
            line += f"   +{r.newly_added}  -> {r.merged_tags}"
        elif r.action == "unchanged":
            line += f"   ({r.merged_tags})"
        if r.detail:
            line += f"   [{r.detail}]"
        print(line)
        if r.action in ("missing", "failed"):
            bad += 1
    n = len(results)
    changed = sum(r.action in ("updated", "would_update") for r in results)
    same = sum(r.action == "unchanged" for r in results)
    print(f"  --- {n} desks: {changed} changed, {same} unchanged, {bad} missing/failed")
    return bad


def _make_io(client: httpx.Client):
    """Bind ``get_body`` / ``put_body`` over a live httpx registry client."""

    def get_body(descriptor_id: str) -> dict | None:
        r = client.get(f"/descriptors/{FAMILY}/{descriptor_id}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {r.text[:400]}")
        return r.json().get("body")

    def put_body(descriptor_id: str, body: dict) -> str:
        r = client.put(f"/descriptors/{FAMILY}/{descriptor_id}", json=body)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {r.status_code} {r.text[:600]}")
        return r.json().get("version", "?")

    return get_body, put_body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the planned per-desk tag diff and write nothing",
    )
    parser.add_argument(
        "--profile", default=None,
        help="instance profile directory (default: $LEGBA_PROFILE, else profiles/geopolitics)",
    )
    args = parser.parse_args(argv)
    profile = load_profile(resolve_profile(args.profile, root=REPO_ROOT))
    desk_list = desks(profile)
    tables = tag_tables(profile)

    token = resolve_token()
    with registry_client(BASE, token) as client:
        get_body, put_body = _make_io(client)
        results = run(get_body=get_body, put_body=put_body, desk_list=desk_list,
                      dry_run=args.dry_run, tables=tables)
    bad = print_results(results, dry_run=args.dry_run)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
