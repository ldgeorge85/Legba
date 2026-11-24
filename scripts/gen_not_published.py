# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the Accuracy page's *what this page does not publish* list (UI-1).

Writes ``legba-ui-v3/src/lib/not_published.json`` from two sources, so the
closing section of ``system.eval_scorecard`` is never hand-written prose:

* **The measurement seams of ``docs/SEAMS.md``.** The declared-seam registry
  carries no class column, so the measurement class is declared HERE, as
  :data:`MEASUREMENT_SEAMS` — the seam numbers whose "what is not built" is a
  measurement the platform does not take or does not publish. The text is the
  registry's own: the seam title, the first two sentences of its "What is not
  built" cell and the first sentence of its guard rail. A listed number that is
  missing from the declared table, or that has moved to the resolved appendix,
  fails the generation loudly rather than publishing a stale absence.
* **The eval routes' declared unmeasured classes.** Each entry of
  :data:`ROUTE_CLASSES` names a route the page reads and the declaration in the
  route's own module that states what it does not publish — a pydantic
  ``Field(description=...)``, a marked paragraph of a response model's
  docstring, or a module-level string constant. The text is read from the
  source with :mod:`ast` (no import, so the generator needs no runtime
  dependencies); a declaration that disappears fails the generation.

WHEN IT RUNS. By hand, at commit time, like ``gen_profile_ui.py``: the JSON is
committed and the UI imports it at build time. ``tests/test_gen_not_published.py``
fails when the committed file is not byte-identical to a fresh generation, so a
SEAMS edit or a route-declaration edit that changes the list cannot land
without the list::

    python3 scripts/gen_not_published.py           # write
    python3 scripts/gen_not_published.py --check   # exit 1 if stale
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEAMS_MD = REPO_ROOT / "docs" / "SEAMS.md"
OUT = REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "not_published.json"
SCHEMA = "legba/not_published/1"

#: The measurement class of docs/SEAMS.md: seams whose missing piece is a
#: measurement — a figure the platform does not take, or takes and cannot
#: label. Order is the order the page lists them.
#:
#: * 46 — a displayed number cannot say it is a fallback (most routes carry no
#:   degradation flag), so a fallback figure is under-labelled, never mislabelled.
#: * 55 — the correctness grader writes nothing without a current reference, so
#:   a subject × unit with no reference has no correctness number at all.
#: * 60 — the error rate of the curated layer map is not audited.
#: * 61 — whether any source covers a subject at all is not measured.
#:   (89, the standing evaluations on the console, was listed here until E-3
#:   resolved it: the owner publishes runs to ``GET /v3/eval/runs``.)
#: An id is a number (the numbered seams) or a slug (seams declared since R-1).
MEASUREMENT_SEAMS: tuple[int | str, ...] = (46, 55, 60, 61)

#: The routes' declared unmeasured classes. ``kind`` is how the text is found:
#: ``field`` = ``Class.field``'s ``Field(description=...)``; ``docstring`` = the
#: paragraph of ``Class``'s docstring that starts with ``marker``; ``constant``
#: = a module-level string. ``live`` names the figure the page attaches from the
#: route's own response (resolved in ``legba-ui-v3/src/lib/accuracyPage.ts``;
#: ``null`` = the class has no count to show).
ROUTE_CLASSES: tuple[dict[str, str | None], ...] = (
    {
        "id": "grader_unmeasured",
        "route": "/v3/eval/grader_roster",
        "file": "src/legba/data/registry/grader_roster_api.py",
        "kind": "field",
        "symbol": "RosterTotals.desks_unmeasured",
        "marker": None,
        "live": "grader_roster.unmeasured",
    },
    {
        "id": "forecast_skill",
        "route": "/v3/eval/calibration",
        "file": "src/legba/data/registry/v3_api.py",
        "kind": "docstring",
        "symbol": "CalibrationScoreboard",
        "marker": "HONESTY",
        "live": "calibration.forecast_status",
    },
    {
        "id": "band_no_brier",
        "route": "/v3/eval/calibration",
        "file": "src/legba/data/registry/v3_api.py",
        "kind": "docstring",
        "symbol": "BandCalibrationSection",
        "marker": "HONESTY",
        "live": None,
    },
    {
        "id": "goldset_tiny_n",
        "route": "/v3/eval/correctness",
        "file": "src/legba/data/registry/v3_api.py",
        "kind": "docstring",
        "symbol": "UnitCorrectnessRow",
        "marker": "TINY-n",
        "live": "correctness.below_floor",
    },
    {
        "id": "external_truth",
        "route": "/v3/eval/correctness",
        "file": "src/legba/data/provenance/external_truth.py",
        "kind": "constant",
        "symbol": "HONESTY_NOTE",
        "marker": None,
        "live": "correctness.external_truth",
    },
)


class GenerationError(RuntimeError):
    """A declared source is missing — the list would be stale, so nothing is written."""


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"(*])")


def _clean(text: str) -> str:
    """Markdown/RST → plain text with single backticks kept for code spans."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [label](link) → label
    text = text.replace("``", "`").replace("**", "")
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str, n: int) -> str:
    parts = _SENTENCE_END.split(text)
    return " ".join(parts[:n]).strip()


# ---------------------------------------------------------------------------
# docs/SEAMS.md
# ---------------------------------------------------------------------------


def _section(md: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", md, re.M | re.S)
    if not m:
        raise GenerationError(f"docs/SEAMS.md has no '## {heading}' section")
    return m.group(1)


def _table_rows(section: str) -> dict[int | str, list[str]]:
    """Seam rows keyed by id: an int for a numbered seam, the slug for a named
    one (R-1: new seams take a slug, never the next number)."""
    rows: dict[int | str, list[str]] = {}
    for line in section.splitlines():
        m = re.match(r"^\|\s*(\d+|[a-z][a-z0-9]*(?:-[a-z0-9]+)+)\s*\|(.*)\|\s*$", line)
        if m:
            key: int | str = int(m.group(1)) if m.group(1).isdigit() else m.group(1)
            rows[key] = [c.strip() for c in m.group(2).split("|")]
    return rows


def seam_items(md: str, numbers: tuple[int | str, ...] = MEASUREMENT_SEAMS) -> list[dict[str, object]]:
    declared = _table_rows(_section(md, "Declared seams"))
    resolved = _table_rows(_section(md, "Resolved seams"))
    out: list[dict[str, object]] = []
    for n in numbers:
        if n in resolved:
            raise GenerationError(
                f"SEAMS #{n} is in the resolved appendix — it is no longer an "
                "absence; drop it from MEASUREMENT_SEAMS"
            )
        cells = declared.get(n)
        if cells is None:
            raise GenerationError(f"SEAMS #{n} is not a row of the declared-seam table")
        if len(cells) != 3:
            raise GenerationError(f"SEAMS #{n} does not parse as (seam, not built, guard rail)")
        title, not_built, guard = (_clean(c) for c in cells)
        if not_built in ("", "—", "-"):
            raise GenerationError(f"SEAMS #{n} declares nothing unbuilt")
        out.append({
            "id": f"seam-{n}",
            "seam": n,
            "title": title,
            "summary": _sentences(not_built, 2),
            "guard": _sentences(guard, 1),
            "source": f"docs/SEAMS.md #{n}",
        })
    return out


# ---------------------------------------------------------------------------
# Route declarations (read with ast — never imported)
# ---------------------------------------------------------------------------


def _str_value(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _field_description(tree: ast.Module, symbol: str) -> str | None:
    cls_name, _, field = symbol.partition(".")
    cls = _class(tree, cls_name)
    if cls is None:
        return None
    for node in cls.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == field
            and isinstance(node.value, ast.Call)
        ):
            for kw in node.value.keywords:
                if kw.arg == "description":
                    return _str_value(kw.value)
    return None


def _docstring_paragraph(tree: ast.Module, symbol: str, marker: str) -> str | None:
    cls = _class(tree, symbol)
    doc = ast.get_docstring(cls) if cls is not None else None
    if not doc:
        return None
    for para in re.split(r"\n\s*\n", doc):
        para = para.strip()
        if para.startswith(marker):
            # The marker is the model's own label ("HONESTY (…):", "TINY-n:"),
            # not part of the sentence a reader needs — drop it with its colon.
            return re.sub(rf"^{re.escape(marker)}[^:]*:\s*", "", para)
    return None


def _constant(tree: ast.Module, symbol: str) -> str | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == symbol for t in node.targets
        ):
            return _str_value(node.value)
    return None


def route_items(root: Path = REPO_ROOT) -> list[dict[str, object]]:
    trees: dict[str, ast.Module] = {}
    out: list[dict[str, object]] = []
    for spec in ROUTE_CLASSES:
        path = str(spec["file"])
        if path not in trees:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"))
        tree = trees[path]
        symbol = str(spec["symbol"])
        kind = spec["kind"]
        if kind == "field":
            text = _field_description(tree, symbol)
        elif kind == "docstring":
            text = _docstring_paragraph(tree, symbol, str(spec["marker"]))
        elif kind == "constant":
            text = _constant(tree, symbol)
        else:
            raise GenerationError(f"{spec['id']}: unknown kind {kind!r}")
        if not text or not text.strip():
            raise GenerationError(
                f"{spec['id']}: {path}:{symbol} no longer declares its unmeasured class"
            )
        out.append({
            "id": spec["id"],
            "route": spec["route"],
            "source": f"{path}:{symbol}",
            "text": _clean(text),
            "live": spec["live"],
        })
    return out


# ---------------------------------------------------------------------------
# Render / CLI
# ---------------------------------------------------------------------------


def render(root: Path = REPO_ROOT) -> str:
    doc = {
        "_generated": (
            "scripts/gen_not_published.py from docs/SEAMS.md and the eval routes' "
            "declared unmeasured classes - do not edit by hand"
        ),
        "schema": SCHEMA,
        "seams": seam_items((root / "docs" / "SEAMS.md").read_text(encoding="utf-8")),
        "route_classes": route_items(root),
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(OUT), help="output path")
    parser.add_argument("--check", action="store_true", help="exit 1 if the output is stale")
    args = parser.parse_args(argv)
    try:
        text = render()
    except GenerationError as exc:
        print(f"gen_not_published: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out)
    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current != text:
            print(f"{out} is stale — run scripts/gen_not_published.py", file=sys.stderr)
            return 1
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
