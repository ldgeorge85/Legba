#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the descriptor-prompt MANIFEST the R4 drift gauge compares against.

WHY A MANIFEST AND NOT THE TREE. A bounded unit's system prompt does not live in
the code image — it lives in ``analyst_descriptors.body.method.system_prompt``, a
registry DB row, and it gets there by someone running ``voice_prompt_puts.py``.
So the analytic method of the system is a DB row, not a tracked file, and a fix
that is correct in the tree can sit wrong in production indefinitely with nothing
saying so. That is the exposure R4 closes.

The obvious implementation — hash the live row against ``descriptors/*.yaml`` —
cannot run in the engine: ``descriptors/`` is in neither container image and is
not volume-mounted. So the tree side is COMPILED here into a small JSON artefact
that ships inside the package, and the gauge compares the live row against that.

The artefact is a build output checked into the tree, which means it can go
stale, which would make the gauge quietly wrong — the exact disease. So
``tests/data_pkg/test_descriptor_prompt_manifest.py`` regenerates it and asserts
byte-equality. Editing a descriptor prompt without regenerating turns that test
red.

Usage::

    python3 scripts/gen_descriptor_prompt_manifest.py          # write
    python3 scripts/gen_descriptor_prompt_manifest.py --check  # verify only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"
MANIFEST_PATH = (
    REPO_ROOT / "src" / "legba" / "data" / "registry" / "descriptor_prompts.json"
)

#: Bumped when the manifest's SHAPE changes, so a gauge reading an old artefact
#: can say so instead of comparing hashes computed under different rules.
#: v2 (2026-08-05) adds ``states`` — see :func:`descriptor_family`.
MANIFEST_VERSION = 2


def descriptor_family(body: dict[str, Any], path: Path) -> str:
    """``analyst`` | ``action_pack`` | ``source`` — the tree's own inference rule.

    Kept byte-identical to the one in
    ``tests/data_pkg/test_descriptor_reference_resolution_k3.py``: a descriptor
    with a ``method`` block is an analyst, one with ``tools``/``channels`` is an
    action pack, everything else is a source. The YAML carries no family field, so
    this shape test is the only thing there is.
    """
    if "method" in body:
        return "analyst"
    if "tools" in body or "channels" in body:
        return "action_pack"
    return "source"


def prompt_hash(text: str) -> str:
    """The comparison key: sha256 over the prompt with trailing whitespace per
    line stripped and a single trailing newline.

    Normalized because YAML block scalars and a JSON round-trip through the
    registry disagree about trailing whitespace in ways no human ever intended
    as a prompt change — an un-normalized hash would report drift on every
    descriptor forever, which is the same as reporting it on none.
    """
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    normalized = "\n".join(lines).strip() + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_manifest() -> dict[str, Any]:
    prompts: dict[str, Any] = {}
    states: dict[str, Any] = {}
    for path in sorted(DESCRIPTORS_DIR.glob("*.yaml")):
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:  # pragma: no cover — a broken tree file
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(body, dict):
            continue
        identity = body.get("identity")
        desc_id = (
            identity.get("id")
            if isinstance(identity, dict) and identity.get("id")
            else path.stem
        )
        family = descriptor_family(body, path)

        # STATE — every family. The tree's declared lifecycle state for this
        # descriptor, keyed ``<family>:<id>`` so a source and an analyst sharing
        # a name can never collide.
        state = identity.get("state") if isinstance(identity, dict) else None
        if isinstance(state, str) and state.strip():
            states[f"{family}:{desc_id}"] = {
                "state": state.strip(),
                "source": path.name,
            }

        # PROMPT — analysts with an INLINE system_prompt only. A
        # ``prompt_module``-backed unit's prompt IS tracked code and cannot drift.
        if family != "analyst":
            continue
        method = body.get("method")
        if not isinstance(method, dict):
            continue
        prompt = method.get("system_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            continue
        prompts[str(desc_id)] = {
            "sha256": prompt_hash(prompt),
            "chars": len(prompt),
            "source": path.name,
        }
    return {
        "manifest_version": MANIFEST_VERSION,
        "note": (
            "Generated by scripts/gen_descriptor_prompt_manifest.py. Do not edit "
            "by hand — regenerate. Guarded by "
            "tests/data_pkg/test_production_gauge_integrity.py."
        ),
        "prompts": prompts,
        "states": states,
    }


def render(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the checked-in manifest is stale",
    )
    args = ap.parse_args()

    text = render(build_manifest())
    if args.check:
        current = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.exists() else ""
        if current != text:
            print(
                f"STALE: {MANIFEST_PATH.relative_to(REPO_ROOT)} does not match "
                f"descriptors/ — run scripts/gen_descriptor_prompt_manifest.py",
                file=sys.stderr,
            )
            return 1
        print(f"ok: manifest matches descriptors/")
        return 0

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(text, encoding="utf-8")
    parsed = json.loads(text)
    print(
        f"wrote {MANIFEST_PATH.relative_to(REPO_ROOT)} "
        f"({len(parsed['prompts'])} prompts, {len(parsed['states'])} states)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
