# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S1-T1 — retro-tag the 24 existing country desks with scope applicability tags.

The G20 (19) + watch (5) targets were registered CREATE-ONLY at bring-up
(``bringup_register_g20_country_targets.py`` / ``..._watch_country_targets.py``)
with only the coverage tags ``geopolitical`` / ``news`` / ``g20`` | ``watch``.
This script UPDATES each existing head via the registry REST surface to MERGE in
the new applicability vocabulary — WITHOUT clobbering the existing tags or any
other ``scope`` field:

  * a REGION tag — exactly one per desk
    (``region_europe`` | ``region_mena`` | ``region_indo_pacific`` |
     ``region_americas`` | ``region_africa``); the Stream-2 region_composition
    frames read country compositions by this tag.
  * zero or more WATCH tags — ``nuclear_watch`` / ``conflict_active`` /
    ``sanctions_regime`` — the declarative unit-applicability matrix (S1-T3):
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

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_API_TOKEN`` (or ``LEGBA_REGISTRY_TOKEN``) — bearer token,
    resolved via ``scripts/_token.py`` (falls back to the .env line, then "dev").
"""
from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _token import resolve_token  # noqa: E402

BASE = os.environ.get(
    "LEGBA_REGISTRY_URL",
    "http://127.0.0.1:8090/api/v1/registry",
)
FAMILY = "target"

# ---------------------------------------------------------------------------
# Tag vocabulary + the 24-desk assignment table (DATA — the whole design).
# ---------------------------------------------------------------------------
# The desk id convention mirrors the two bring-up registrars:
#   G20   -> country_g20_<iso2.lower()>
#   watch -> country_watch_<iso2.lower()>
G20_ISO2 = [
    "AR", "AU", "BR", "CA", "CN", "DE", "FR", "GB", "ID", "IN",
    "IT", "JP", "KR", "MX", "RU", "SA", "TR", "US", "ZA",
]
WATCH_ISO2 = [
    "IL", "IR", "UA", "TW", "KP", "PK",
    # A3 / DEC-C (2026-07-16): escalation-risk watch desks
    "SD", "ML", "BF", "NE", "CD", "MM", "HT",
]

# Exactly one region tag per desk. Two assignments are judgment calls flagged to
# the operator (see the task's operator_questions): RU spans Eurasia (filed
# under Europe given the Ukraine theatre); TR straddles Europe/MENA (filed under
# MENA given the Middle-East framing that dominates its desk).
REGION_BY_ISO2: dict[str, str] = {
    # Americas
    "AR": "region_americas",
    "BR": "region_americas",
    "CA": "region_americas",
    "MX": "region_americas",
    "US": "region_americas",
    # Europe
    "DE": "region_europe",
    "FR": "region_europe",
    "GB": "region_europe",
    "IT": "region_europe",
    "RU": "region_europe",       # judgment call (Eurasia)
    "UA": "region_europe",
    # MENA
    "SA": "region_mena",
    "TR": "region_mena",         # judgment call (Europe/MENA straddle)
    "IL": "region_mena",
    "IR": "region_mena",
    # Indo-Pacific
    "AU": "region_indo_pacific",
    "CN": "region_indo_pacific",
    "ID": "region_indo_pacific",
    "IN": "region_indo_pacific",
    "JP": "region_indo_pacific",
    "KR": "region_indo_pacific",
    "TW": "region_indo_pacific",
    "KP": "region_indo_pacific",
    "PK": "region_indo_pacific",   # S1-T2: Pakistan watch desk
    # Africa
    "ZA": "region_africa",
    # A3 / DEC-C (operator-approved 2026-07-16): escalation-risk watch desks
    "SD": "region_africa",         # Sudan
    "ML": "region_africa",         # Mali
    "BF": "region_africa",         # Burkina Faso
    "NE": "region_africa",         # Niger
    "CD": "region_africa",         # DR Congo
    "MM": "region_indo_pacific",   # Myanmar
    "HT": "region_americas",       # Haiti
}

# Watch tags per desk (canonical order: nuclear_watch, conflict_active,
# sanctions_regime). Every non-region assignment here is a judgment call the
# operator should confirm — see operator_questions. Defensible defaults:
#   nuclear_watch   = review §1 core {IR, KP} + the review's explicit optional
#                     nuclear-armed set {IN, IL, CN, RU, US}. GB/FR (declared
#                     NPT NWS) deliberately EXCLUDED — the review omitted them
#                     and the proliferation unit tracks concern states, not the
#                     established P5 stockpile.
#   conflict_active = states in an active armed conflict as of mid-2026 (RU-UA
#                     war; US-Iran war + Israel regional war per the operator's
#                     grounding seed). TIME-SENSITIVE — re-review on world-state
#                     change. US tagged as an active belligerent (operator call
#                     2026-07-02).
#   sanctions_regime= states under a sanctions regime {RU, IR, KP} plus CN
#                     (targeted export controls / tech sanctions — operator call
#                     2026-07-02).
WATCH_TAGS_BY_ISO2: dict[str, list[str]] = {
    "CN": ["nuclear_watch", "sanctions_regime"],
    "IN": ["nuclear_watch"],
    "US": ["nuclear_watch", "conflict_active"],
    "RU": ["nuclear_watch", "conflict_active", "sanctions_regime"],
    "UA": ["conflict_active"],
    "IL": ["nuclear_watch", "conflict_active"],
    "IR": ["nuclear_watch", "conflict_active", "sanctions_regime"],
    "KP": ["nuclear_watch", "sanctions_regime"],
    "PK": ["nuclear_watch"],   # S1-T2: declared nuclear state (IN-PK dyad)
    # A3 / DEC-C (2026-07-16): the escalation-risk sample — all in active
    # armed conflict or armed-group state-fragility as of mid-2026.
    "SD": ["conflict_active"],
    "ML": ["conflict_active"],
    "BF": ["conflict_active"],
    "NE": ["conflict_active"],
    "CD": ["conflict_active"],
    "MM": ["conflict_active", "sanctions_regime"],
    "HT": ["conflict_active"],
}

# The full closed vocabulary this script may assign (region + watch). Used for a
# fail-fast self-check that every mapping value is a legal ScopeTag.
REGION_TAGS = (
    "region_europe", "region_mena", "region_indo_pacific",
    "region_americas", "region_africa",
)
WATCH_TAGS = ("nuclear_watch", "conflict_active", "sanctions_regime")
TAG_VOCABULARY = frozenset(REGION_TAGS + WATCH_TAGS)

# Mirrors target._ScopeTag (pattern + length). A local guard so a typo in the
# mapping table above fails at import, not on a 422 from the registry.
_SCOPE_TAG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _self_check_vocabulary() -> None:
    for tag in TAG_VOCABULARY:
        if not _SCOPE_TAG_RE.match(tag) or len(tag) > 64:
            raise ValueError(f"illegal ScopeTag in vocabulary: {tag!r}")
    # Every value used in the mapping tables must be in the declared vocabulary.
    for iso2, tag in REGION_BY_ISO2.items():
        if tag not in REGION_TAGS:
            raise ValueError(f"{iso2}: region tag {tag!r} not in REGION_TAGS")
    for iso2, tags in WATCH_TAGS_BY_ISO2.items():
        for tag in tags:
            if tag not in WATCH_TAGS:
                raise ValueError(f"{iso2}: watch tag {tag!r} not in WATCH_TAGS")


_self_check_vocabulary()


def desks() -> list[tuple[str, str]]:
    """The 24 (descriptor_id, iso2) pairs — 19 G20 + 5 watch."""
    out: list[tuple[str, str]] = []
    for iso2 in G20_ISO2:
        out.append((f"country_g20_{iso2.lower()}", iso2))
    for iso2 in WATCH_ISO2:
        out.append((f"country_watch_{iso2.lower()}", iso2))
    return out


def tags_for_iso2(iso2: str) -> list[str]:
    """Region tag (always) + any watch tags for a desk's ISO-3166-1 alpha-2."""
    region = REGION_BY_ISO2.get(iso2)
    if region is None:
        raise KeyError(f"no region tag assigned for {iso2!r}")
    return [region, *WATCH_TAGS_BY_ISO2.get(iso2, [])]


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


def build_plan(descriptor_id: str, iso2: str, body: dict) -> DeskPlan:
    """Compute the merged-tags plan for one desk from its current head ``body``.

    Only ``scope.tags`` is touched; every other scope/body field is carried
    through verbatim (deep-copied so the caller's input is never mutated).
    """
    add = tags_for_iso2(iso2)
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

        plan = build_plan(descriptor_id, iso2, body)
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
    title = "Retro-tag 24 country desks (DRY-RUN — no writes):" if dry_run \
        else "Retro-tag 24 country desks:"
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


def _client(base: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


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
    args = parser.parse_args(argv)

    token = resolve_token()
    with _client(BASE, token) as client:
        get_body, put_body = _make_io(client)
        results = run(get_body=get_body, put_body=put_body, dry_run=args.dry_run)
    bad = print_results(results, dry_run=args.dry_run)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
