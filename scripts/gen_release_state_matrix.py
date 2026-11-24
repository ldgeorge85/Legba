# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the panel-classification table in ``docs/RELEASE_STATE_MATRIX.md``
(§2) from the single source of truth, ``legba-ui-v3/src/panel-registry/registry.ts``
(C5-4, 2026-07-28 registry-hygiene wave).

WHY A GENERATOR: the matrix's own header has said "regenerate the panel table
from ... registry.ts when panels change tier" since it was written, but
nothing ever did — §2's Live/Preview classification was hand-copied
from ``registry.ts`` and could silently drift (a panel's ``tier`` flag changes
in code, nobody remembers to edit the doc). §3 of the matrix
names this exact gap: "there is still no automated check that THIS matrix
stays in sync with registry.ts." This script + its drift test
(``tests/data_pkg/test_release_state_matrix_current.py``) close it.

SCOPE (deliberately narrow, honest about what IS and ISN'T generated):
``registry.ts`` only knows a panel's kind/category/title/tier/binding/modes,
and ``kindPlan.ts`` which singletons are drills — neither carries narrative
("why is this preview", "why is this a drill", backend-route wiring detail). Sections 1 / 1.1 / 3 of the matrix and
the hand-authored "why" prose inside §2 encode operator knowledge that has no
machine-readable source anywhere in the tree; this script does NOT touch
them. It owns exactly one thing: the generated table between the
``<!-- BEGIN GENERATED PANEL TABLE -->`` / ``<!-- END GENERATED PANEL TABLE -->``
markers in §2. Regenerating never rewrites anything outside those markers —
if the markers are missing, the script refuses loud rather than guessing
where to splice (same discipline as ``docs/SEAMS.md``'s allowlist block +
``tests/test_no_undeclared_stubs.py``).

PARSING: ``registry.ts`` is TypeScript, not Python/JSON, and pulling in a JS
toolchain for one small, extremely regular table is not worth the dependency.
The panel rows are all shaped as a single-line ``def(...)`` call (kind,
panelId, category, scopeKey, defaultTitle, requiresBinding, modes[],
iconName) plus the ``PREVIEW_KINDS`` ``ReadonlySet<PanelKind>`` literal that
overrides the tier after the fact. UI-B1/shell (W7) retired ``HIDDEN_KINDS``:
"hidden" is now a DERIVED property — a DRILL is a kind whose params need a
ref, i.e. every ``requiresBinding`` kind plus the keys of
``panel-registry/kindPlan.ts::SINGLETON_DRILLS``, which this script reads the
same way — regexes over that fixed shape are simpler and more auditable than a
full TS AST, and the parser fails loud (asserts a nonzero row count, asserts
every ``def()`` call it finds parses cleanly) rather than silently emitting
an empty/partial table on a shape change.

Usage::

    python3 scripts/gen_release_state_matrix.py            # regenerate in place
    python3 scripts/gen_release_state_matrix.py --check     # exit 1 if stale (CI/test use)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_TS = REPO_ROOT / "legba-ui-v3" / "src" / "panel-registry" / "registry.ts"
KIND_PLAN_TS = REPO_ROOT / "legba-ui-v3" / "src" / "panel-registry" / "kindPlan.ts"
# W10 UI-B2: a default title may be a `PROFILE_UI.<path>` read rather than a
# literal; it resolves through the packaged profile labels the workstation
# imports (`lib/profileUi.ts` reads this file; gen_profile_ui.py writes it).
PROFILE_UI_JSON = REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "profile_ui.json"
MATRIX_MD = REPO_ROOT / "docs" / "RELEASE_STATE_MATRIX.md"

BEGIN_MARKER = "<!-- BEGIN GENERATED PANEL TABLE -->"
END_MARKER = "<!-- END GENERATED PANEL TABLE -->"

# One `def(...)` registration call — see the module docstring for the shape.
# Matches across the literal argument list `def(` ... `)` on one logical
# line (registry.ts writes every entry on a single line).
_DEF_CALL_RE = re.compile(
    r"""def\(
        \s*'(?P<kind>[^']+)'\s*,
        \s*'(?P<panel_id>[^']+)'\s*,
        \s*'(?P<category>[^']+)'\s*,
        \s*(?P<scope_key>null|'[^']*')\s*,
        \s*(?:'(?P<title>[^']+)'|(?P<title_ref>PROFILE_UI(?:\.\w+)+))\s*,
        \s*(?P<requires_binding>true|false)\s*,
        \s*\[(?P<modes>[^\]]*)\]\s*,
        \s*'(?P<icon>[^']+)'
        \s*\)
    """,
    re.VERBOSE,
)

_SET_LITERAL_RE = re.compile(
    r"const\s+(?P<name>PREVIEW_KINDS)\s*:\s*ReadonlySet<PanelKind>\s*="
    r"\s*new Set(?:<PanelKind>)?\(\s*\[(?P<body>[\s\S]*?)\]\s*\)",
)

# `export const SINGLETON_DRILLS: ... = { 'kind': {...}, ... }` in kindPlan.ts —
# the block runs to the first line that is exactly `}`.
_DRILLS_BLOCK_RE = re.compile(
    r"export const SINGLETON_DRILLS[^=]*=\s*\{(?P<body>[\s\S]*?)\n\}",
)
_DRILL_KEY_RE = re.compile(r"^\s*'([^']+)'\s*:", re.M)

_QUOTED_RE = re.compile(r"'([^']+)'")


@dataclass(frozen=True)
class PanelRow:
    kind: str
    panel_id: str
    category: str
    scope_key: str | None
    title: str
    requires_binding: bool
    modes: tuple[str, ...]
    drill: bool
    tier: str  # 'live' | 'preview'

    def as_markdown_row(self) -> str:
        scope = self.scope_key or "—"
        binding = "yes" if self.requires_binding else "no"
        modes = ", ".join(self.modes) if self.modes else "—"
        drill = "yes" if self.drill else "no"
        return (
            f"| `{self.kind}` | `{self.panel_id}` | {self.category} | "
            f"{scope} | {self.title} | {binding} | {modes} | "
            f"**{self.tier}** | {drill} |"
        )


def _parse_kind_set(text: str, name: str) -> frozenset[str]:
    m = _SET_LITERAL_RE.search(text)
    while m and m.group("name") != name:
        m = _SET_LITERAL_RE.search(text, m.end())
    assert m is not None, (
        f"{REGISTRY_TS}: could not find a `const {name}: ReadonlySet<PanelKind> "
        f"= new Set([...])` literal — registry.ts's shape changed; update "
        f"_SET_LITERAL_RE in {Path(__file__).name}"
    )
    return frozenset(_QUOTED_RE.findall(m.group("body")))


def parse_singleton_drills(text: str) -> frozenset[str]:
    m = _DRILLS_BLOCK_RE.search(text)
    assert m is not None, (
        f"{KIND_PLAN_TS}: could not find the `export const SINGLETON_DRILLS = {{...}}` "
        f"block — kindPlan.ts's shape changed; update _DRILLS_BLOCK_RE in "
        f"{Path(__file__).name}"
    )
    return frozenset(_DRILL_KEY_RE.findall(m.group("body")))


def _resolve_profile_ui(ref: str, profile_ui: dict) -> str:
    """``PROFILE_UI.map.title`` -> the packaged profile label at that path."""
    node: object = profile_ui
    for key in ref.split(".")[1:]:
        assert isinstance(node, dict) and key in node, (
            f"{REGISTRY_TS}: {ref} names no string in {PROFILE_UI_JSON.name} "
            f"(missing {key!r}) — regenerate it with scripts/gen_profile_ui.py"
        )
        node = node[key]
    assert isinstance(node, str) and node, f"{ref} resolves to {node!r}, not a title"
    return node


def parse_registry_ts(
    text: str, kind_plan_text: str | None = None, profile_ui: dict | None = None
) -> list[PanelRow]:
    preview_kinds = _parse_kind_set(text, "PREVIEW_KINDS")
    if kind_plan_text is None:
        kind_plan_text = KIND_PLAN_TS.read_text(encoding="utf-8")
    singleton_drills = parse_singleton_drills(kind_plan_text)

    rows: list[PanelRow] = []
    seen_kinds: set[str] = set()
    for m in _DEF_CALL_RE.finditer(text):
        kind = m.group("kind")
        if kind in seen_kinds:
            # def() is also referenced in the module docstring / comments;
            # only the FIRST (real) registration call should match this
            # regex shape there, but guard against a genuine duplicate
            # PanelKind registration (would be a registry.ts bug worth
            # surfacing, not silently double-counting).
            continue
        seen_kinds.add(kind)
        scope_raw = m.group("scope_key")
        scope_key = None if scope_raw == "null" else scope_raw.strip("'")
        modes = tuple(_QUOTED_RE.findall(m.group("modes")))
        requires_binding = m.group("requires_binding") == "true"
        tier = "preview" if kind in preview_kinds else "live"
        title = m.group("title")
        if title is None:
            if profile_ui is None:
                profile_ui = json.loads(PROFILE_UI_JSON.read_text(encoding="utf-8"))
            title = _resolve_profile_ui(m.group("title_ref"), profile_ui)
        rows.append(
            PanelRow(
                kind=kind,
                panel_id=m.group("panel_id"),
                category=m.group("category"),
                scope_key=scope_key,
                title=title,
                requires_binding=requires_binding,
                modes=modes,
                drill=requires_binding or kind in singleton_drills,
                tier=tier,
            )
        )

    registrations = len(re.findall(r"definition:\s*def\(", text))
    assert len(seen_kinds) >= registrations, (
        f"{REGISTRY_TS}: {registrations} `definition: def(...)` registrations but "
        f"only {len(seen_kinds)} parsed — a def() call's shape changed; update "
        f"_DEF_CALL_RE in {Path(__file__).name} (never drop a panel silently)"
    )
    assert rows, (
        f"{REGISTRY_TS}: parsed ZERO panel rows — either the file is empty/"
        f"moved, or its def() call shape changed and _DEF_CALL_RE needs "
        f"updating in {Path(__file__).name} (never emit an empty table "
        f"silently)."
    )
    return rows


_CATEGORY_ORDER = {"target": 0, "analyst": 1, "operator": 2, "system": 3}


def render_table(rows: list[PanelRow]) -> str:
    ordered = sorted(
        rows,
        key=lambda r: (_CATEGORY_ORDER.get(r.category, 99), r.kind),
    )
    header = (
        "| Kind | Panel ID | Category | Scope key | Default title | "
        "Requires binding | Modes | Tier | Drill |\n"
        "|---|---|---|---|---|---|---|---|---|"
    )
    body = "\n".join(r.as_markdown_row() for r in ordered)
    live_n = sum(1 for r in rows if r.tier == "live")
    preview_n = sum(1 for r in rows if r.tier == "preview")
    drill_n = sum(1 for r in rows if r.drill)
    summary = (
        f"{len(rows)} panel kinds registered — {live_n} live, {preview_n} "
        f"preview; {drill_n} are drills (a drill needs a ref and opens from "
        f"its parent object, never from the catalog — a navigation property, "
        f"not a build state)."
    )
    return (
        f"_Generated by `scripts/{Path(__file__).name}` from "
        f"`legba-ui-v3/src/panel-registry/registry.ts` — do not hand-edit "
        f"between the markers; re-run the script instead. {summary}_\n\n"
        f"{header}\n{body}"
    )


def render_matrix(current_text: str, rows: list[PanelRow]) -> str:
    assert BEGIN_MARKER in current_text and END_MARKER in current_text, (
        f"{MATRIX_MD} is missing the {BEGIN_MARKER!r} / {END_MARKER!r} "
        f"marker pair — refusing to guess where the generated table goes. "
        f"Add the markers (see the module docstring) before regenerating."
    )
    before, rest = current_text.split(BEGIN_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    table = render_table(rows)
    return f"{before}{BEGIN_MARKER}\n\n{table}\n\n{END_MARKER}{after}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 1 (no write) if regenerating would change the file — "
        "used by the drift test / CI, never mutates on disk.",
    )
    args = ap.parse_args(argv)

    registry_text = REGISTRY_TS.read_text(encoding="utf-8")
    rows = parse_registry_ts(registry_text)
    current_text = MATRIX_MD.read_text(encoding="utf-8")
    new_text = render_matrix(current_text, rows)

    if new_text == current_text:
        print(f"{MATRIX_MD.relative_to(REPO_ROOT)} is current ({len(rows)} panels).")
        return 0

    if args.check:
        print(
            f"{MATRIX_MD.relative_to(REPO_ROOT)} is STALE relative to "
            f"{REGISTRY_TS.relative_to(REPO_ROOT)} — run "
            f"`python3 scripts/{Path(__file__).name}` to regenerate.",
            file=sys.stderr,
        )
        return 1

    MATRIX_MD.write_text(new_text, encoding="utf-8")
    print(f"Regenerated {MATRIX_MD.relative_to(REPO_ROOT)} ({len(rows)} panels).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
