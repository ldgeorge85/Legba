#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase-V VOICE — re-stamp the nine bounded units' live registry descriptors.

WHY A SCRIPT AND NOT A DEPLOY. A bounded unit's system prompt does NOT live in
the code image: ``inline_target`` reads it from the descriptor the registry
serves (``method.system_prompt``), and the critic grades against
``eval.rubric`` from the same row. So the Phase-V unit-side deltas — D2 (ask
for the judgment, not the sentence), D3 (bind absence-scoping to the BLUF),
D4 (one body shape), D5 (no instrument readings in prose) and D8a
(narrative_coordination's 24h/72h lie) — reach production ONLY when these
descriptors are PUT. A container rebuild alone changes nothing about them.

WHAT ELSE IS IN THE SAME RELEASE, and why the two halves must land together:

  * CODE (a deploy): the AS-OF clause on ``unit_grounding.UNIT_GROUNDING_CLAUSE``
    and ``meta_findings_synthesizer._continuity_rule``; the slice header the
    as-of line is copied from (``inline_target._render_user_prompt`` +
    ``options['slice_window_hours']``); and the four composition prompt
    rewrites, which are code constants.
  * DESCRIPTORS (this script): the nine unit prompts, and — added by V-N2 —
    the two non-unit ``inline_target`` prompts (cross_doc_corroborator,
    corpus_researcher). The non-unit half carries the DATED-RETRIEVAL rules:
    they run a GATHER loop over a corpus that spans years, and their retrieved
    documents now render with their own collection/publication dates (the code
    half), which the prompts must tell them to read. Same both-halves-together
    argument as the units. ``disruption_status`` gathers too and keeps those
    rules — DS-1 moved it into ``UNITS`` for the read contract it was always
    owed, not out of the retrieval half it already had.

  Ship them TOGETHER. The banned-phrase list ships in the descriptor half; the
  replacement judgment shape ships there too, but the as-of line the new body
  shape opens with is stamped by the code half. Descriptors ahead of code =
  units told to open with an as-of line whose window the slice header does not
  yet print. Code ahead of descriptors = the old prompts still ordering the
  exact sentences the new rules ban. Neither is fatal; both are avoidable.

USAGE — one command, dry by default:

    LEGBA_REGISTRY_URL=... python3 scripts/voice_prompt_puts.py            # diff only
    LEGBA_REGISTRY_URL=... python3 scripts/voice_prompt_puts.py --apply    # PUT

    ... --only narrative_coordination            # one desk (canary)
    ... --apply --only escalation --only energy_security

The default is a DIFF: it prints, per descriptor, a unified diff of the LIVE
head's prompt text against the tree's, plus the rubric delta, and touches
nothing. Read that diff before passing ``--apply``.

METHOD — live head is the base, per SESSION_CAPTURE §3. Each PUT body is the
descriptor the registry currently serves, with ONLY the fields named in
:data:`SYNCED_PATHS` replaced from the tree file, and the head's own version
carried into ``identity.version`` (the registry's optimistic-concurrency
token). Everything else on the live row — a GEPA-promoted field, an operator
edit, a cadence tune — is preserved byte-for-byte. Rebuilding the body from
the tree file instead would silently revert any live state the tree does not
know about.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _bringup_http import (  # noqa: E402
    DESCRIPTORS_DIR,
    registry_base,
    registry_client,
)

import yaml  # noqa: E402

#: The NINE bounded reasoning units whose prompts Phase-V rewrites. Every one
#: is an ``inline_target`` descriptor in the ``analyst`` family.
#:
#: DS-1 — ``disruption_status`` is the ninth, moved here from :data:`NON_UNITS`
#: on 2026-08-06. Phase-V filed it as a non-unit because it runs a GATHER loop,
#: which is a statement about where its evidence comes from, not about what
#: shape its answer takes: it names a driver, states a direction, and writes a
#: BLUF over a bounded question exactly as the other eight do. It kept ordering
#: the banned template sentence at 93% of findings, before and after the wave,
#: because the contract that replaces that sentence was never pasted into it.
#: GATHERING and UNIT-hood are orthogonal — this desk is BOTH, and the two rule
#: sets compose (see ``_tradecraft`` and ``test_retrieved_context_contract``).
UNITS: tuple[str, ...] = (
    "escalation",
    "energy_security",
    "economic_coercion",
    "internal_stability",
    "military_posture",
    "proliferation_watch",
    "leadership_transition",
    "narrative_coordination",
    "disruption_status",
)

#: V-N2 — the NON-UNIT ``inline_target`` analysts whose descriptor prompts this
#: release also rewrites. They are a separate tuple, not more ``UNITS``, because
#: they are a different thing: they carry no HOUSE READ CONTRACT (their body
#: shapes are legitimately their own — a corroboration verdict and a deep
#: corpus read are not bounded-question reads; see ``_tradecraft`` on why the
#: contract is pasted into the nine and not code-appended to all twelve), and
#: what they get here is the DATED-RETRIEVAL half of D1, worded per analyst.
#:
#: NOT a synonym for "runs a GATHER loop". FOUR analysts gather
#: (cross_doc_corroborator, corpus_researcher, country_assessor AND the unit
#: ``disruption_status``) and they are the only ones that can be shown a
#: RETRIEVED block at all — but gathering says nothing about answer shape, which
#: is what DS-1 corrected. Of the two tuples, ``country_assessor`` is in
#: NEITHER: it is ``state: draft`` (retired live) AND takes its prompt from
#: ``method.prompt_module`` rather than ``method.system_prompt``, so there is no
#: descriptor prompt to stamp — it still inherits the code-appended clause like
#: every other inline_target analyst.
NON_UNITS: tuple[str, ...] = (
    "cross_doc_corroborator",
    "corpus_researcher",
)

#: Everything this script can stamp. ``--only`` accepts any id from either
#: tuple; the default is all of them, so tonight's single ``--apply`` ships the
#: unit prompts and the non-unit prompts together.
STAMPABLE: tuple[str, ...] = UNITS + NON_UNITS

#: The dotted paths this script is allowed to overwrite on a live descriptor.
#: Deliberately short: the prompt the unit runs, and the rubric the critic
#: grades it against. The rubric is here because leaving it behind would make
#: the critic penalize the new voice for not reciting the enum the prompt no
#: longer asks for — the optimizer loop would fight the change every cycle.
SYNCED_PATHS: tuple[tuple[str, ...], ...] = (
    ("method", "system_prompt"),
    ("eval", "rubric"),
    # D8a: narrative_coordination's window is the descriptor field the prompt
    # used to contradict. Synced so prose and query can never disagree again.
    ("subscription", "targets", "time_window"),
)


def _token() -> str:
    """The registry bearer token — env first, then the repo ``.env``."""
    if tok := os.environ.get("LEGBA_REGISTRY_API_TOKEN"):
        return tok
    if tok := os.environ.get("LEGBA_REGISTRY_TOKEN"):
        return tok
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            for key in ("LEGBA_REGISTRY_API_TOKEN=", "LEGBA_REGISTRY_TOKEN="):
                if line.startswith(key):
                    return line.split("=", 1)[1].strip()
    raise SystemExit(
        "no registry token: set LEGBA_REGISTRY_API_TOKEN or put it in .env"
    )


def _dig(body: Any, path: tuple[str, ...]) -> Any:
    """Value at ``path``, or ``None`` when any hop is missing."""
    cur = body
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _plant(body: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Set ``path`` to ``value``, creating intermediate dicts as needed."""
    cur = body
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def get_head(client, family: str, desc_id: str) -> tuple[dict[str, Any], str]:
    """The live head body + its version (the concurrency token for the PUT)."""
    r = client.get(f"/descriptors/{family}/{desc_id}")
    r.raise_for_status()
    row = r.json()
    body = row.get("body") or row.get("descriptor") or row
    version = row.get("version") or (body.get("identity") or {}).get("version")
    return body, str(version)


def diff_field(label: str, live: Any, tree: Any) -> bool:
    """Print a unified diff for one field. Returns True when it differs."""
    live_s = "" if live is None else str(live)
    tree_s = "" if tree is None else str(tree)
    if live_s == tree_s:
        return False
    lines = list(
        difflib.unified_diff(
            live_s.splitlines(keepends=True),
            tree_s.splitlines(keepends=True),
            fromfile=f"live:{label}",
            tofile=f"tree:{label}",
            n=1,
        )
    )
    print(f"    --- {label}: {len(live_s)} -> {len(tree_s)} chars")
    for line in lines:
        print("    " + line.rstrip("\n"))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-stamp the Phase-V VOICE unit prompts into the registry.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually PUT (default is a dry-run diff that writes nothing)",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "restrict to this descriptor id (repeatable); default is all "
            f"{len(STAMPABLE)} ({len(UNITS)} units + {len(NON_UNITS)} non-units)"
        ),
    )
    args = ap.parse_args()

    units = tuple(args.only) if args.only else STAMPABLE
    unknown = [u for u in units if u not in STAMPABLE]
    if unknown:
        raise SystemExit(
            f"unknown descriptor(s): {', '.join(unknown)}\n"
            f"  units:     {', '.join(UNITS)}\n"
            f"  non-units: {', '.join(NON_UNITS)}"
        )

    mode = "APPLY (PUT)" if args.apply else "DRY-RUN (no writes)"
    n_units = sum(1 for u in units if u in UNITS)
    n_non = sum(1 for u in units if u in NON_UNITS)
    print(f"Phase-V VOICE prompt re-stamp — {mode}")
    print(f"registry: {registry_base()}")
    print(f"stamping: {len(units)}  ({n_units} unit(s), {n_non} non-unit(s))\n")

    changed: list[str] = []
    unchanged: list[str] = []
    failures: list[str] = []

    with registry_client(registry_base(), _token()) as client:
        for unit in units:
            tree = yaml.safe_load((DESCRIPTORS_DIR / f"analyst_{unit}.yaml").read_text())
            try:
                live, version = get_head(client, "analyst", unit)
            except Exception as exc:
                failures.append(f"{unit}: GET head — {exc}")
                print(f"  ! {unit}: GET head failed — {exc}\n")
                continue

            print(f"  * {unit} @ head {version[:16]}")
            dirty = False
            for path in SYNCED_PATHS:
                tree_val = _dig(tree, path)
                if tree_val is None:
                    continue
                if diff_field(".".join(path), _dig(live, path), tree_val):
                    dirty = True
                    _plant(live, path, tree_val)

            if not dirty:
                unchanged.append(unit)
                print("    (no change)\n")
                continue
            changed.append(unit)

            if not args.apply:
                print("    [dry-run] would PUT\n")
                continue

            live.setdefault("identity", {})["version"] = version
            r = client.put(f"/descriptors/analyst/{unit}", json=live)
            if r.status_code != 200:
                failures.append(f"{unit}: HTTP {r.status_code} {r.text[:300]}")
                print(f"    FAILED: HTTP {r.status_code} {r.text[:300]}\n")
                continue
            new_version = str(r.json().get("version"))
            print(f"    PUT ok -> {new_version[:16]}\n")

    print("=" * 60)
    print(f"changed:   {len(changed)}  {', '.join(changed) or '-'}")
    print(f"unchanged: {len(unchanged)}  {', '.join(unchanged) or '-'}")
    print(f"failed:    {len(failures)}")
    for f in failures:
        print(f"  ! {f}")
    if not args.apply and changed:
        print("\nDry run — nothing was written. Re-run with --apply to PUT.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
