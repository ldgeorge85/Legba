# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the panel-classification table in ``docs/RELEASE_STATE_MATRIX.md``
(§2) from the single source of truth, ``legba-ui-v3/src/panel-registry/registry.ts``
(C5-4, 2026-07-28 registry-hygiene wave).

WHY A GENERATOR: the matrix's own header has said "regenerate the panel table
from ... registry.ts when panels change tier" since it was written, but
nothing ever did — §2's Live/Preview/Hidden classification was hand-copied
from ``registry.ts`` and could silently drift (a panel's ``tier``/``hidden``
flag changes in code, nobody remembers to edit the doc). §3 of the matrix
names this exact gap: "there is still no automated check that THIS matrix
stays in sync with registry.ts." This script + its drift test
(``tests/data_pkg/test_release_state_matrix_current.py``) close it.

SCOPE (deliberately narrow, honest about what IS and ISN'T generated):
``registry.ts`` only knows a panel's kind/category/title/tier/hidden/binding/
modes — it carries no narrative ("why is this preview", "why was this
hidden", backend-route wiring detail). Sections 1 / 1.1 / 3 of the matrix and
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
iconName[, hidden]) plus two ``ReadonlySet<PanelKind>`` literals
(``PREVIEW_KINDS`` / ``HIDDEN_KINDS``) that override tier/hidden after the
fact — regexes over that fixed shape are simpler and more auditable than a
full TS AST, and the parser fails loud (asserts a nonzero row count, asserts
every ``def()`` call it finds parses cleanly) rather than silently emitting
an empty/partial table on a shape change.

Usage::

    python3 scripts/gen_release_state_matrix.py            # regenerate in place
    python3 scripts/gen_release_state_matrix.py --check     # exit 1 if stale (CI/test use)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_TS = REPO_ROOT / "legba-ui-v3" / "src" / "panel-registry" / "registry.ts"
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
        \s*'(?P<title>[^']+)'\s*,
        \s*(?P<requires_binding>true|false)\s*,
        \s*\[(?P<modes>[^\]]*)\]\s*,
        \s*'(?P<icon>[^']+)'
        (?:\s*,\s*(?P<hidden_literal>true|false))?
        \s*\)
    """,
    re.VERBOSE,
)

_SET_LITERAL_RE = re.compile(
    r"const\s+(?P<name>PREVIEW_KINDS|HIDDEN_KINDS)\s*:\s*ReadonlySet<PanelKind>\s*="
    r"\s*new Set(?:<PanelKind>)?\(\s*\[(?P<body>[\s\S]*?)\]\s*\)",
)

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
    hidden: bool
    tier: str  # 'live' | 'preview'

    def as_markdown_row(self) -> str:
        scope = self.scope_key or "—"
        binding = "yes" if self.requires_binding else "no"
        modes = ", ".join(self.modes) if self.modes else "—"
        hidden = "yes" if self.hidden else "no"
        return (
            f"| `{self.kind}` | `{self.panel_id}` | {self.category} | "
            f"{scope} | {self.title} | {binding} | {modes} | "
            f"**{self.tier}** | {hidden} |"
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


def parse_registry_ts(text: str) -> list[PanelRow]:
    preview_kinds = _parse_kind_set(text, "PREVIEW_KINDS")
    hidden_kinds = _parse_kind_set(text, "HIDDEN_KINDS")

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
        literal_hidden = m.group("hidden_literal") == "true"
        hidden = literal_hidden or kind in hidden_kinds
        tier = "preview" if kind in preview_kinds else "live"
        rows.append(
            PanelRow(
                kind=kind,
                panel_id=m.group("panel_id"),
                category=m.group("category"),
                scope_key=scope_key,
                title=m.group("title"),
                requires_binding=m.group("requires_binding") == "true",
                modes=modes,
                hidden=hidden,
                tier=tier,
            )
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
        "Requires binding | Modes | Tier | Hidden |\n"
        "|---|---|---|---|---|---|---|---|---|"
    )
    body = "\n".join(r.as_markdown_row() for r in ordered)
    live_n = sum(1 for r in rows if r.tier == "live")
    preview_n = sum(1 for r in rows if r.tier == "preview")
    hidden_n = sum(1 for r in rows if r.hidden)
    summary = (
        f"{len(rows)} panel kinds registered — {live_n} live, {preview_n} "
        f"preview, {hidden_n} hidden (a panel can be both live/preview AND "
        f"hidden — hidden is a navigation-tier flag, not a build state)."
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
