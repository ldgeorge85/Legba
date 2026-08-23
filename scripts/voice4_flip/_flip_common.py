# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pieces of the VOICE-4 unit-prompt flip kit.

WHAT THE FLIP IS. The D6 wave (`planning/D6_DRAFTS_2026-08-19/`) rewrote the
nine bounded units' `method.system_prompt` around a shared preamble: WHO YOU
ARE, WHAT EACH MISTAKE COSTS, WHAT YOU ARE READING, the scoped-absence rider,
SPEAK ABOUT THE WORLD NOT THE PIPELINE, and five micro-amendments. The VOICE-3
replay (`planning/VOICE_REPLAY_2026-08-20/`) cleared EIGHT of the nine and HELD
``narrative_coordination`` — its replay could not catch the coordination signal
on the two positive windows. So this kit flips 8 and touches the 9th only to
prove it did NOT move.

WHY A SCRIPT AND NOT A DEPLOY — unchanged from ``scripts/voice_prompt_puts.py``:
a unit's system prompt does not live in the code image. ``inline_target`` reads
it from the descriptor the registry serves, so the new prompts reach production
only when these descriptors are PUT.

THE LIFECYCLE, as the registry actually implements it (``DescriptorRegistry.
update``): a PUT on an ACTIVE head is allowed DIRECTLY — there is no separate
version-bump call to make and no draft/promote dance to perform.

  * The registry re-reads the head itself, carries the live ``state`` onto the
    new descriptor when the body does not ask for a different one, mints the
    new version as the CONTENT HASH of the body, demotes the old row to
    ``is_head=false`` and keeps it.
  * ``identity.version`` in the PUT body is NOT the concurrency token: the
    server compares the head it read at entry against the head inside its own
    transaction, and then overwrites ``identity.version`` with the computed
    hash anyway (content_hash excludes it, per L-101 §7). It still has to PARSE
    as a hex string, so this kit carries the live head version into it — the
    same thing ``voice_prompt_puts`` does, and the reason that script's
    docstring calls it a concurrency token.
  * An unchanged body is a NO-OP that returns the existing head, so re-running
    ``--apply`` cannot churn versions.
  * A 409 means a genuine race (another writer moved the head or the state
    between the two reads) and the fix is to re-run, not to force.

THE ENVELOPE. ``GET /descriptors/{family}/{id}`` returns a ``DescriptorRowOut``
— ``{descriptor_id, version, state, body, …}`` — and the descriptor proper is
the ``body`` field. ``PUT`` wants that BARE body, not the envelope. Sending the
envelope back is the house's recurring registry mistake and it fails as a
validation error rather than as anything obvious.

BASE = THE LIVE HEAD, NOT THE TREE FILE. Each PUT body is the descriptor the
registry currently serves with ONLY ``method.system_prompt`` replaced. Rebuilding
the body from the YAML would silently revert any live-only state the tree does
not know about — a GEPA-promoted field, an operator edit, a cadence tune.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from _bringup_http import (  # noqa: E402
    DESCRIPTORS_DIR,
    registry_base,
    registry_client,
)

import yaml  # noqa: E402

from legba.data.analysts._tradecraft import (  # noqa: E402
    SEVERITY_AS_STATE_RULE,
)

#: The EIGHT units the VOICE-3 replay cleared to flip.
UNITS: tuple[str, ...] = (
    "escalation",
    "energy_security",
    "economic_coercion",
    "internal_stability",
    "military_posture",
    "leadership_transition",
    "disruption_status",
    "proliferation_watch",
)

#: The ninth unit — HELD. Never PUT by this kit; read only, to prove it did not
#: move while the other eight did.
HELD_UNIT: str = "narrative_coordination"

#: The dotted path this kit is allowed to touch. Exactly one, unlike
#: ``voice_prompt_puts``' three: the D6 wave is a PROSE change, and the
#: rubric/window fields it left alone must stay alone.
PROMPT_PATH: tuple[str, ...] = ("method", "system_prompt")

#: sha256 of each unit's INTENDED prompt — the value the tree YAML carries and
#: the value the live descriptor must hold after the flip.
#:
#: WHY PIN THEM HERE. ``planning/`` is gitignored (internal docs are not part of
#: the release), so the D6 drafts these prompts were extracted from do NOT exist
#: in a clean checkout. Without a pin, "byte-faithful to the draft" would be a
#: claim nobody downstream could re-check. These digests are that check: they
#: were computed from the draft blocks at extraction time, and
#: ``tests/data_pkg/test_voice4_flip_kit.py`` re-derives them from the tree on
#: every run. The digest covers the descriptor value, i.e. the draft's ```text
#: block PLUS the single trailing newline that YAML's ``|`` clip-chomping adds.
INTENDED_SHA256: dict[str, str] = {
    "escalation": "9d97da3ab47e8536721ffd7a89e762477f8bda7c6b098fffd26d327b50ecbaaf",
    "energy_security": "3709be2e3d39b3133491e5cb32bec5922932bdb13b6002853c6004e7b6313d0e",
    "economic_coercion": "25795927fcf6d29fdc1b5ac88e8fdd95249e2810b18a630a508eef2fd7c0468b",
    "internal_stability": "7837106e755014665e359f7dc2ae0d10b755fdf324f85961f2ccbcc33b5240a6",
    "military_posture": "bf7120da16bded053e43c7793ebbd24aaca153cfef91e718063df6125e38442b",
    "leadership_transition": "d0ebacfd07fc4683f90e572644d95400617fb181cffd85fc65ef3b782a61ed98",
    "disruption_status": "195af11deca1bda7711ab016ea7e0b8ae25714eff3821e4b60b2d8e315ed86e1",
    "proliferation_watch": "31558c75ffd7ceaeaa60b9cc0a9ca42ce576c445003d460bbca5e6988bb33620",
}

#: sha256 of the HELD unit's prompt as it stands BEFORE and AFTER this train.
#: The whole point of the HOLD is that this value does not change.
HELD_SHA256: str = (
    "2b655f20155d7d8dd7e0327f2399ebdbb6e885dc3accb90983b1f75457c56e35"
)

#: MA2's replay addendum — the fleet sentence, word-identical on every draft.
#: ``IndicatorEntry`` silently drops a whole entry whose dates are prose, and
#: 2/40 replayed cells wrote them as prose, so this sentence is the fix and its
#: presence on ALL EIGHT is a shipped-together property.
MA2_DATE_FORMAT_SENTENCE: str = (
    "Write both dates in the schema's `YYYY-MM-DD` form; "
    "the human-date rule applies to prose only."
)

#: MA4 — the TITLE amendment (L2-11), spliced INSIDE the HOUSE READ CONTRACT.
#: The contract's own first line says it is "identical on every desk", so this
#: sentence has to land on every desk in the wave IN THE SAME TRAIN or the
#: contract's claim about itself becomes false. That is what makes the flip
#: all-at-once rather than desk-by-desk.
TITLE_AMENDMENT_SENTENCE: str = (
    "It is NEVER the as-of line and never begins with 'As of'."
)


#: Paragraphs a LATER train added to these same nine prompts, in the order they
#: landed. :func:`d6_base` peels them off so the D6 byte-faithfulness pin keeps
#: proving what it was written to prove.
#:
#: WHY THIS EXISTS AT ALL. :data:`INTENDED_SHA256` is a frozen digest of prompts
#: transcribed from drafts that are gitignored, and its whole value is that it is
#: NOT re-derivable from the tree — so a later train that edits these prompts
#: cannot simply re-pin it (a re-pinned digest proves nothing) and must not be
#: allowed to turn it red either (the D6 claim is still true and still worth
#: checking). Peeling the later paragraph off restores the exact bytes the digest
#: covers, so the pin keeps its meaning and the LAYERING becomes the thing the
#: test states: D6's prose, plus FRAME-3's paragraph, and nothing else.
#:
#: FRAME-3 (2026-08-21) added ``SEVERITY_AS_STATE_RULE`` to the HOUSE READ
#: CONTRACT on all NINE desks — the held one included, because it is a scorecard
#: dimension and the tag contract cannot be per-desk.
LATER_CONTRACT_PARAGRAPHS: tuple[str, ...] = (SEVERITY_AS_STATE_RULE,)


def norm(text: str) -> str:
    """Whitespace-normalized text.

    The prompts are hard-wrapped in a YAML block scalar, so a sentence spans
    lines at a wrap point that is an artifact of the file rather than of the
    text. Fleet-sentence checks normalize; BYTE checks never do.
    """
    return " ".join(text.split())


def d6_base(prompt: str) -> str:
    """``prompt`` with every later train's contract paragraph peeled off.

    Paragraph-wise rather than by string surgery: the constants are hard-wrapped
    into the YAML at a width that belongs to the file, so a byte-level removal
    would have to know the wrap and would break on a re-wrap. Splitting on the
    blank-line separator and dropping whole paragraphs by their NORMALIZED text
    is wrap-independent, and rejoining is byte-exact for everything kept — the
    result is the D6 prompt as the drafts wrote it, or the input unchanged when
    no later paragraph is present.
    """
    drop = {norm(p) for p in LATER_CONTRACT_PARAGRAPHS}
    return "\n\n".join(p for p in prompt.split("\n\n") if norm(p) not in drop)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def token() -> str:
    """The registry bearer token — env first, then the repo ``.env``.

    Never printed, never logged. Key order follows ``voice_prompt_puts``;
    ``LEGBA_BEARER_TOKEN`` is accepted last because operator runbooks name it,
    though the key that is actually in ``.env`` is ``LEGBA_REGISTRY_API_TOKEN``.
    """
    keys = (
        "LEGBA_REGISTRY_API_TOKEN",
        "LEGBA_REGISTRY_TOKEN",
        "LEGBA_BEARER_TOKEN",
    )
    for key in keys:
        if tok := os.environ.get(key):
            return tok
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            for key in keys:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    raise SystemExit(
        "no registry token: set one of "
        f"{', '.join(keys)} or put it in .env"
    )


def tree_prompt(unit: str) -> str:
    """The intended prompt: ``method.system_prompt`` from the tree descriptor."""
    doc = yaml.safe_load((DESCRIPTORS_DIR / f"analyst_{unit}.yaml").read_text())
    return doc["method"]["system_prompt"]


def tree_body(unit: str) -> dict[str, Any]:
    return yaml.safe_load((DESCRIPTORS_DIR / f"analyst_{unit}.yaml").read_text())


def dig(body: Any, path: tuple[str, ...]) -> Any:
    """Value at ``path``, or ``None`` when any hop is missing."""
    cur = body
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def plant(body: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Set ``path`` to ``value``, creating intermediate dicts as needed."""
    cur = body
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def get_head(client: Any, unit: str) -> tuple[dict[str, Any], str, str]:
    """The live head's BARE body, its version, and its lifecycle state.

    Unwraps the ``DescriptorRowOut`` envelope — see the module docstring on why
    PUTting the envelope back is the mistake this function exists to prevent.
    """
    r = client.get(f"/descriptors/analyst/{unit}")
    r.raise_for_status()
    row = r.json()
    body = row.get("body") or row.get("descriptor") or row
    version = row.get("version") or (body.get("identity") or {}).get("version")
    state = row.get("state") or (body.get("identity") or {}).get("state") or "?"
    return body, str(version), str(state)


#: Paths a live head is EXPECTED to hold differently from its tree file, and
#: which therefore say nothing about drift:
#:
#:   * ``identity.version`` — the tree carries the 16-zero placeholder and the
#:     registry stamps the real content hash;
#:   * ``method.system_prompt`` — the field this train is FOR;
#:   * ``identity.state`` — the LIFECYCLE, which belongs to the registry and the
#:     operator rather than to the file. ``disruption_status`` is the live proof:
#:     its descriptor ships ``state: draft`` on purpose (bulk registration must
#:     create it inert) and it runs ``active``, promoted by a transition. This is
#:     the single sharpest reason the PUT base is the LIVE HEAD and not the tree
#:     file — rebuilding the body from YAML would ask the registry to move an
#:     active descriptor back to draft. Reported by ``apply_flip`` as a note, per
#:     unit, so it stays visible instead of merely excused.
STRUCTURAL_EXEMPT: tuple[tuple[str, ...], ...] = (
    ("identity", "version"),
    ("identity", "state"),
    ("method", "system_prompt"),
)


def structural_diff(live: dict[str, Any], tree: dict[str, Any]) -> list[str]:
    """Paths the TREE DECLARES on which the live head disagrees with it.

    TREE-DIRECTED, and that direction is the whole design. A live body is the
    registry's ``model_dump`` of a typed descriptor, so it materializes every
    pydantic DEFAULT the YAML leaves unwritten — ``method.retries``,
    ``eval.judge``, ``outputs``, and a ``governor_override: null`` inside each
    ``action_packs`` entry. A symmetric comparison reports all ~22 of those per
    unit as "drift", which is noise that would train an operator to wave the
    check through on the one run where it means something.

    So: every path the tree states, the live head must agree with; anything the
    tree does not state, the registry owns. A non-empty result means the tree
    and the registry genuinely disagree about a field this train is not
    supposed to touch, and ``apply_flip`` refuses to PUT that unit rather than
    shipping the disagreement alongside the prose.
    """
    exempt = {".".join(p) for p in STRUCTURAL_EXEMPT}
    out: list[str] = []

    def walk(tree_node: Any, live_node: Any, path: str) -> None:
        if path in exempt:
            return
        if isinstance(tree_node, dict):
            if not isinstance(live_node, dict):
                out.append(f"{path}: tree declares a mapping, live has {type(live_node).__name__}")
                return
            for key in sorted(tree_node):
                sub = f"{path}.{key}" if path else key
                if sub in exempt:
                    continue
                if key not in live_node:
                    out.append(f"{sub}: declared in tree, absent live")
                else:
                    walk(tree_node[key], live_node[key], sub)
            return
        if isinstance(tree_node, list):
            if not isinstance(live_node, list):
                out.append(f"{path}: tree declares a list, live has {type(live_node).__name__}")
                return
            if len(tree_node) != len(live_node):
                out.append(
                    f"{path}: {len(tree_node)} entr(ies) in tree, "
                    f"{len(live_node)} live"
                )
                return
            for i, (t_item, l_item) in enumerate(zip(tree_node, live_node)):
                walk(t_item, l_item, f"{path}[{i}]")
            return
        if tree_node != live_node:
            out.append(f"{path}: tree {tree_node!r} != live {live_node!r}")

    walk(tree, live, "")
    return out


__all__ = [
    "HELD_SHA256",
    "HELD_UNIT",
    "INTENDED_SHA256",
    "MA2_DATE_FORMAT_SENTENCE",
    "PROMPT_PATH",
    "REPO_ROOT",
    "TITLE_AMENDMENT_SENTENCE",
    "UNITS",
    "dig",
    "get_head",
    "norm",
    "plant",
    "registry_base",
    "registry_client",
    "sha",
    "structural_diff",
    "token",
    "tree_body",
    "tree_prompt",
]
