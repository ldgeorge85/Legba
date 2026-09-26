#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RETAG GENERATOR — a first-draft ``layer_map`` for one country, derived
from what the existing ``descriptors/source_*.yaml`` already declare
(Program 6 L0, docs/DIRECTION.md "The layered source fan-out").

WHAT THIS IS. A proposal, never a load. It prints (or writes) a
``layer_map_<cc>.yaml`` shaped exactly like
``legba.data.registry.layer_map_schema.LayerMapDescriptor`` expects, with
``reason: "derived: <rule>"`` on every entry and every aperture stamped
``declared: unmeasured`` — the code NEVER infers ``present`` or ``absent``.
The operator turns this draft into the curated map (editing/removing entries,
setting real apertures, choosing a real ``map_version``) and loads it with
``scripts/load_layer_map.py``. Nothing here touches a database.

THE RULES (in application order — see :func:`derive_layer`):

  1. a source with NO country scope at all that is structurally global
     (``kind: geojson`` — a model-free structured feed, or a channel-level
     monitor: ``kind: telegram_channel`` / ``discord_webhook``) is ALWAYS
     relevant, mapped to ``physical`` / ``social_digest`` respectively,
     regardless of ``--country``;
  2. otherwise a source must be RELEVANT to ``--country`` — either its
     ``scope.geo`` names this country, or it carries no geo at all (a global
     feed every desk reads);
  3. a source that is itself a wire/agency feed — a ``same_publisher`` entry
     under the ``ap`` key in ``descriptors/wire_map.yaml`` (the one
     ``same_publisher`` group that names an actual news agency rather than a
     broadcaster or an IGO desk) — reads as ``foreign_press``, even when its
     own regional edition is geo-scoped to this country: an agency dispatch
     is never this country's domestic masthead;
  4. ``scope.source_class == "state_media"`` scoped to this country reads as
     ``official`` — the state's own voice (docs/DIRECTION.md: "the same
     channel is independent commentary in one country and the state's voice
     in another"); state media NOT scoped to this country is skipped here
     (ambiguous which state it speaks for — the operator's call, not a
     default);
  5. ``scope.source_class == "official"`` scoped to this country reads as
     ``official`` too (a literal government/primary-source publisher); with
     no country scope at all it reads as ``public_data`` (a global IGO/gov
     data feed);
  6. ``scope.source_class == "reporting"`` scoped to this country reads as
     ``domestic_press``; with no country scope it reads as ``foreign_press``
     (international press, read from outside);
  7. ``scope.source_class == "analysis"`` (think-tank / OSINT / structured
     conflict-event datasets) reads as ``public_data`` — the layer vocabulary
     has no separate analytical bucket, and a structured dataset or a
     think-tank feed is closer to public data than to either press layer.

Templates (``identity.abstraction_level`` above ``L1``) and retired sources
(``identity.state == "retired"``) are skipped — neither is an instance a desk
actually reads today.

DETERMINISTIC BY CONSTRUCTION: entries are derived from static YAML on disk
and sorted by ``source_id``; the draft carries no timestamp of its own (a
fixed placeholder ``created``) — the same input tree always produces the same
output bytes, which is what the fixture test in
``tests/data_pkg/test_gen_layer_map_draft.py`` pins.

Usage::

    python3 scripts/gen_layer_map_draft.py --country IL
    python3 scripts/gen_layer_map_draft.py --country RU -o descriptors/layer_map_ru.yaml
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"
WIRE_MAP_PATH = DESCRIPTORS_DIR / "wire_map.yaml"

sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.layers._vocab import LAYER_VOCAB  # noqa: E402

#: Placeholder — a draft is not itself an event with a real creation instant;
#: the operator sets a true one when curating. Fixed so generator output is
#: byte-stable across runs (see the module docstring's determinism note).
_DRAFT_CREATED = "2026-01-01T00:00:00Z"

_ALWAYS_RELEVANT_KIND_LAYER = {
    "geojson": "physical",
    "telegram_channel": "social_digest",
    "discord_webhook": "social_digest",
}

_ISO2_RE = re.compile(r"^[A-Z]{2}$")


def load_wire_agency_ids(wire_map_path: Path = WIRE_MAP_PATH) -> frozenset[str]:
    """The source ids under ``wire_map.same_publisher.ap`` — the one
    ``same_publisher`` group in ``descriptors/wire_map.yaml`` that names an
    actual news AGENCY (as opposed to ``aljazeera``/``un_news``, which are a
    broadcaster and an IGO desk respectively, not wire services)."""
    if not wire_map_path.exists():
        return frozenset()
    body = yaml.safe_load(wire_map_path.read_text(encoding="utf-8")) or {}
    same_publisher = (body.get("wire_map") or {}).get("same_publisher") or {}
    return frozenset(same_publisher.get("ap") or [])


def derive_layer(
    desc: dict[str, Any], country: str, wire_agency_ids: frozenset[str]
) -> tuple[str, str] | None:
    """``(layer, reason)`` for this source in ``country``'s draft, or ``None``
    when the source has no place in this country's draft at all. See the
    module docstring for the rule order."""
    identity = desc.get("identity") or {}
    scope = desc.get("scope") or {}

    if identity.get("abstraction_level", "L1") != "L1":
        return None
    if identity.get("state") == "retired":
        return None

    kind = identity.get("kind")
    sid = identity.get("id", "")
    geo = scope.get("geo") or []
    source_class = scope.get("source_class", "reporting")

    always_layer = _ALWAYS_RELEVANT_KIND_LAYER.get(kind)
    if always_layer is not None:
        return always_layer, f"derived: kind={kind} — always relevant regardless of country"

    relevant = (not geo) or (country in geo)
    if not relevant:
        return None
    scoped_here = country in geo

    if sid in wire_agency_ids:
        return (
            "foreign_press",
            "derived: wire/agency feed — descriptors/wire_map.yaml "
            "same_publisher['ap']",
        )

    if source_class == "state_media":
        if scoped_here:
            return (
                "official",
                f"derived: state_media source_class scoped to {country} — "
                "read as that state's own voice",
            )
        return None

    if source_class == "official":
        if scoped_here:
            return (
                "official",
                f"derived: official source_class scoped to {country} — a "
                "government/primary-source publisher",
            )
        return (
            "public_data",
            "derived: official source_class, no country scope — a global "
            "public-data/IGO feed",
        )

    if source_class == "reporting":
        if scoped_here:
            return (
                "domestic_press",
                f"derived: reporting source_class scoped to {country} — a "
                "domestic masthead",
            )
        return (
            "foreign_press",
            "derived: reporting source_class, no country scope — "
            "international press",
        )

    if source_class == "analysis":
        where = f"scoped to {country}" if scoped_here else "no country scope"
        return (
            "public_data",
            f"derived: analysis source_class, {where} — treated as a "
            "structured/analytical public-data feed (no separate "
            "analysis layer in the vocabulary)",
        )

    return None


def build_draft(
    country: str,
    descriptors_dir: Path = DESCRIPTORS_DIR,
    wire_map_path: Path = WIRE_MAP_PATH,
) -> dict[str, Any]:
    if not _ISO2_RE.match(country):
        raise ValueError(f"--country must be uppercase ISO-3166-1 alpha-2, got {country!r}")

    wire_agency_ids = load_wire_agency_ids(wire_map_path)
    entries: list[dict[str, str]] = []
    for path in sorted(descriptors_dir.glob("source_*.yaml")):
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        result = derive_layer(body, country, wire_agency_ids)
        if result is None:
            continue
        layer, reason = result
        entries.append({
            "source_id": (body.get("identity") or {}).get("id", path.stem),
            "layer": layer,
            "reason": reason,
        })
    entries.sort(key=lambda e: e["source_id"])

    apertures = [
        {
            "layer": layer,
            "declared": "unmeasured",
            "reason": (
                "derived: the retag generator does not measure aperture "
                "presence/absence — the operator declares present/absent/"
                "unmeasured per layer after review"
            ),
        }
        for layer in LAYER_VOCAB
    ]

    cc_lower = country.lower()
    return {
        "identity": {
            "id": f"layer_map_{cc_lower}",
            "name": f"{country} layer map (DRAFT, generated)",
            "schema_uri": "legba/layer_map/1.0.0",
            "kind": "layer_map",
            "owner": "layer_map_generator",
            "created": _DRAFT_CREATED,
            "state": "draft",
        },
        "country": country,
        "map_version": f"layer_map_{cc_lower}.draft",
        "entries": entries,
        "apertures": apertures,
    }


_HEADER = (
    "# GENERATED DRAFT — scripts/gen_layer_map_draft.py --country {cc}\n"
    "# A first-draft LAYER TABLE derived from descriptors/source_*.yaml's own\n"
    "# scope.geo / scope.source_class / identity.kind, plus\n"
    "# descriptors/wire_map.yaml's same_publisher['ap'] wire/agency set. Every\n"
    "# entry reason starts \"derived:\" and every aperture is stamped\n"
    "# declared: unmeasured — the generator NEVER infers present/absent. This\n"
    "# is a PROPOSAL for the operator to curate, never loaded automatically.\n"
)


def render_yaml(draft: dict[str, Any], country: str) -> str:
    body = yaml.safe_dump(
        draft, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return _HEADER.format(cc=country) + "\n" + body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--country", required=True, help="ISO-3166-1 alpha-2, e.g. IL")
    ap.add_argument(
        "--descriptors-dir", type=Path, default=DESCRIPTORS_DIR,
        help="directory of source_*.yaml to scan (default: descriptors/)",
    )
    ap.add_argument(
        "--wire-map", type=Path, default=WIRE_MAP_PATH,
        help="path to wire_map.yaml (default: descriptors/wire_map.yaml)",
    )
    ap.add_argument("-o", "--out", type=Path, default=None,
                     help="write here instead of stdout")
    args = ap.parse_args()

    country = args.country.strip().upper()
    draft = build_draft(country, args.descriptors_dir, args.wire_map)
    text = render_yaml(draft, country)

    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(draft['entries'])} entries)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
