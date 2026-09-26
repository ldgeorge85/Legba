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
#: Re-pinned 2026-09-24: Program 7 piece 7e gave narrative_coordination its
#: coordination DEFINITION (the SPREAD BLOCK paragraph) — a deliberate prompt
#: change outside this train; the VOICE hold itself is untouched (the v4 flip
#: still never PUTs this unit).
#: The whole point of the HOLD is that this value does not change.
HELD_SHA256: str = (
    "84c0fe7242a5121002a13581f1d744be547c2e798494c6bd2b8908e093e21739"
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


#: TITLE-FRAME-FIX (2026-09-01) — the second later train to touch these nine
#: prompts, and the first to change a LINE rather than append a PARAGRAPH.
#:
#: WHAT IT CHANGED AND WHY. ``VOICE_ORGANIC_REVIEW_2026-09-01`` §2.a measured the
#: per-desk ``"title"`` schema hint against each desk's own title corpus and
#: found it predicts that desk's lock exactly: a hint of the form "the driving X
#: vector" whose X composes with a risk noun produces the ``<subject> <verb>
#: <risk noun>`` frame (escalation 80.8%, internal_stability 40.6%), while a
#: hint of the form "its X level / its X pressure" produces a degenerate
#: constant instead (leadership_transition 23.1% distinct titles, one string
#: repeated 15 times). The two desks carrying the identical "driving … vector"
#: wording with a noun that does NOT compose — military_posture,
#: proliferation_watch — sit at 0.0%, which is what rules the shared HOUSE READ
#: CONTRACT out as the cause and puts it on this one line per desk.
#:
#: WHY IT IS PEELED RATHER THAN RE-PINNED. Identical reasoning to
#: :data:`LATER_CONTRACT_PARAGRAPHS` above, which see: :data:`INTENDED_SHA256`
#: is a frozen digest of gitignored drafts and its whole value is that it is NOT
#: re-derivable from the tree, so a later train can neither re-pin it (a
#: re-pinned digest proves nothing) nor be allowed to turn it red (the D6 claim
#: is still true). Restoring the D6 line before hashing keeps the pin covering
#: exactly the bytes it was written to cover, and makes the LAYERING the thing
#: the tests state: D6's prose, plus FRAME-3's paragraph, plus this train's one
#: line per desk, and nothing else.
#:
#: THE HELD DESK IS IN HERE TOO. ``narrative_coordination``'s VOICE hold is about
#: the D6 PROSE (MA2 / MA4, asserted separately below and still held). Its title
#: hint is a response-schema line, it carries the same measured defect class, and
#: §5.1 Option 5 scopes the fix to "the eight per-desk title hints" as one train
#: — a headline grammar that is per-desk is the defect, so the repair cannot be.
#:
#: Keyed unit -> (D6 line, TITLE-FRAME-FIX line). Both sides are the FULL
#: ``"title": "<…>"`` JSON-shape line minus its trailing comma, which is what
#: makes the rollback a single unambiguous substring swap per desk.
LATER_TITLE_HINTS: dict[str, tuple[str, str]] = {
    "escalation": (
        '"title": "<concise headline naming the country + the driving escalation vector — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<concise headline: the country + what this window actually shows about who is doing what to whom — the concrete development in its own words, never a category label and never a bare risk level; where nothing moved, name what is HOLDING it — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
    "internal_stability": (
        '"title": "<concise headline naming the country + the driving instability vector — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<concise headline: the country + what this window actually shows about who holds the street and who holds the state — the concrete development in its own words, never a category label and never a bare risk level; where nothing moved, name what is HOLDING it — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
    "economic_coercion": (
        '"title": "<concise headline naming the country + the driving coercion vector + target/wielder — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<concise headline: the country + what this window actually shows about who is squeezing whom and with what measure — name wielder and target; the concrete development in its own words, never a category label and never a bare risk level; where nothing moved, name what is HOLDING it — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
    "military_posture": (
        '"title": "<concise headline naming the country + the driving posture-shift vector — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<concise headline: the country + what this window actually shows about what moved, whose it was and where it went — the concrete development in its own words, never a category label and never a bare risk level; where nothing moved, name what is HOLDING it — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
    "proliferation_watch": (
        '"title": "<concise headline naming the country + the driving proliferation vector — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<concise headline: the country + what this window actually shows about the program or the safeguards around it — the concrete development in its own words, never a category label and never a bare risk level; where nothing moved, name what is HOLDING it — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
    "energy_security": (
        '"title": "<short headline: the country + its energy-security pressure — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<short headline: the country + what this window actually shows about what is moving through its energy system and what is not — the concrete development in its own words, never a category label and never a bare risk level; where nothing moved, name what is HOLDING it — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
    "leadership_transition": (
        '"title": "<short headline naming the country + its transition-risk level — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<short headline: the country + what this window actually shows about who holds power and what is testing that hold — the concrete development in its own words, never a category label and never a bare risk level; where nothing moved, name what is HOLDING it — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
    "disruption_status": (
        '"title": "<concise headline naming the lane/flow + the driving vector — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<concise headline: the lane/flow + what this window actually shows about what is moving through it and what is not — the concrete development in its own words, never a category label and never a bare risk level; where nothing moved, name what is HOLDING it — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
    "narrative_coordination": (
        '"title": "<short headline naming the narrative, if any — see TITLE in the HOUSE READ CONTRACT below>"',
        '"title": "<short headline: what this window actually shows about who is pushing which line, if anyone — the concrete development in its own words, never a category label and never a bare risk level; where no coordination appears, name what you CHECKED — see TITLE in the HOUSE READ CONTRACT below>"',
    ),
}


def pre_title_frame_fix(prompt: str) -> str:
    """``prompt`` with TITLE-FRAME-FIX's ``"title"`` hint rolled back to D6.

    A pure substring swap rather than a paragraph drop, because this train
    changed ONE LINE inside the response-schema paragraph rather than appending
    a paragraph of its own. Unit-agnostic by construction: each desk's new hint
    is a distinct string, so the map can be applied blind and at most one entry
    matches. Returns the input unchanged when no new hint is present, which is
    what makes it safe to run over a pre-train descriptor.

    Raises rather than silently double-swapping when a prompt somehow carries
    BOTH lines — that would mean the response schema grew a second ``"title"``
    example and the pin is no longer measuring what it claims to.
    """
    out = prompt
    for unit, (d6_line, new_line) in LATER_TITLE_HINTS.items():
        if new_line not in out:
            continue
        if d6_line in out:
            raise ValueError(
                f"{unit}: prompt carries BOTH the D6 title hint and the "
                "TITLE-FRAME-FIX one — the rollback is ambiguous"
            )
        out = out.replace(new_line, d6_line, 1)
    return out


def norm(text: str) -> str:
    """Whitespace-normalized text.

    The prompts are hard-wrapped in a YAML block scalar, so a sentence spans
    lines at a wrap point that is an artifact of the file rather than of the
    text. Fleet-sentence checks normalize; BYTE checks never do.
    """
    return " ".join(text.split())


def d6_base(prompt: str) -> str:
    """``prompt`` with every later train's edit peeled back off.

    TWO peels, in the order the trains landed, because the two trains changed
    the prompt in structurally different ways:

    1. TITLE-FRAME-FIX's per-desk ``"title"`` hint is rolled back by
       :func:`pre_title_frame_fix` — a LINE swap, because that is what it was.
    2. FRAME-3's ``SEVERITY_AS_STATE_RULE`` is dropped paragraph-wise. Paragraph
       -wise rather than by string surgery: the constants are hard-wrapped into
       the YAML at a width that belongs to the file, so a byte-level removal
       would have to know the wrap and would break on a re-wrap. Splitting on
       the blank-line separator and dropping whole paragraphs by their
       NORMALIZED text is wrap-independent, and rejoining is byte-exact for
       everything kept.

    The result is the D6 prompt as the drafts wrote it, or the input unchanged
    when no later edit is present.
    """
    rolled = pre_title_frame_fix(prompt)
    drop = {norm(p) for p in LATER_CONTRACT_PARAGRAPHS}
    return "\n\n".join(p for p in rolled.split("\n\n") if norm(p) not in drop)


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
