# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift guard (C5-4, 2026-07-28 registry-hygiene wave) — the generated panel
table in ``docs/RELEASE_STATE_MATRIX.md`` §2 must stay current with
``legba-ui-v3/src/panel-registry/registry.ts``.

Mirrors the ``tests/test_no_undeclared_stubs.py`` idiom: a generator
(``scripts/gen_release_state_matrix.py``) owns a marker-delimited block inside
a docs/ file, and this test re-derives the block from the live source and
fails if the committed doc disagrees — so a panel added (or re-tiered/hidden)
in ``registry.ts`` without re-running the generator turns this test red
instead of silently going stale.

Pure — no DB, no network, no live system. Reads two files from the checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gen_release_state_matrix as gm  # noqa: E402


def test_registry_ts_is_parseable_and_nonempty() -> None:
    """Sanity: the parser finds panel rows in the real registry.ts (never a
    silent empty table — a shape change must fail loud, not regenerate a
    doc that looks fine but lists zero panels)."""
    text = gm.REGISTRY_TS.read_text(encoding="utf-8")
    rows = gm.parse_registry_ts(text)
    assert len(rows) >= 50, (
        f"expected at least 50 panel kinds parsed from {gm.REGISTRY_TS}, "
        f"got {len(rows)} — the def()/PREVIEW_KINDS/HIDDEN_KINDS regexes in "
        f"gen_release_state_matrix.py may be out of sync with registry.ts's "
        f"current shape"
    )
    # Every kind is unique (a genuine duplicate PanelKind registration would
    # be a registry.ts bug, not something to silently collapse).
    kinds = [r.kind for r in rows]
    assert len(kinds) == len(set(kinds)), "duplicate panel kind parsed"


def test_release_state_matrix_matches_registry_ts() -> None:
    """The committed docs/RELEASE_STATE_MATRIX.md generated block must equal
    what `scripts/gen_release_state_matrix.py` would (re)generate from the
    CURRENT registry.ts — i.e. the doc is not stale.

    On failure: run `python3 scripts/gen_release_state_matrix.py` and commit
    the result.
    """
    registry_text = gm.REGISTRY_TS.read_text(encoding="utf-8")
    rows = gm.parse_registry_ts(registry_text)
    current_text = gm.MATRIX_MD.read_text(encoding="utf-8")
    regenerated_text = gm.render_matrix(current_text, rows)

    assert regenerated_text == current_text, (
        f"{gm.MATRIX_MD.relative_to(gm.REPO_ROOT)}'s generated panel table is "
        f"STALE relative to {gm.REGISTRY_TS.relative_to(gm.REPO_ROOT)}. Run "
        f"`python3 scripts/gen_release_state_matrix.py` and commit the "
        f"result — never hand-edit between the "
        f"'{gm.BEGIN_MARKER}' / '{gm.END_MARKER}' markers."
    )


def test_markers_present_exactly_once() -> None:
    """The BEGIN/END markers must each appear exactly once — the generator
    refuses to guess a splice point on 0 or >1 occurrences, so a doc edit
    that duplicates or deletes a marker must fail here before it fails
    mysteriously inside the generator."""
    text = gm.MATRIX_MD.read_text(encoding="utf-8")
    assert text.count(gm.BEGIN_MARKER) == 1, (
        f"expected exactly one {gm.BEGIN_MARKER!r} in {gm.MATRIX_MD}"
    )
    assert text.count(gm.END_MARKER) == 1, (
        f"expected exactly one {gm.END_MARKER!r} in {gm.MATRIX_MD}"
    )
