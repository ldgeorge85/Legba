#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render every committed artefact that LISTS declared types (R-1).

The type registry (``src/legba/data/type_registry``) is the one place a type
is declared. Three committed files still need the list as text, because
something outside the Python process reads them:

* ``legba-ui-v3/src/lib/verdictModel.ts`` — the console's mirror of the
  verify-exempt structural analysts (live-tail rows never pass through the
  reads-API stamp, so the console needs the set itself);
* ``docs/ALERTING.md`` — the kind table;
* ``docs/TYPES.md`` — the per-family reference tables.

Each is a region between BEGIN/END markers that this script owns. Rows are
sorted (by id, or by the family's declared order), so two lanes adding
different types touch different lines; if they still conflict, re-run this
script — the output is a pure function of the declarations.

Usage::

    python3 scripts/gen_type_tables.py          # write
    python3 scripts/gen_type_tables.py --check  # exit 1 if any region is stale
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.type_registry import TypeRegistry, default_registry  # noqa: E402
from legba.data.type_registry.alert_kinds import GROUP_LABELS  # noqa: E402

VERDICT_MODEL = REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "verdictModel.ts"
ALERTING = REPO_ROOT / "docs" / "ALERTING.md"
TYPES = REPO_ROOT / "docs" / "TYPES.md"

TS_BEGIN = "  // BEGIN GENERATED structural-verify-exempt (scripts/gen_type_tables.py; do not edit)"
TS_END = "  // END GENERATED structural-verify-exempt"
MD_BEGIN = "<!-- BEGIN GENERATED {name} (scripts/gen_type_tables.py; do not edit) -->"
MD_END = "<!-- END GENERATED {name} -->"


class StaleError(RuntimeError):
    pass


def _replace_region(text: str, begin: str, end: str, body: str, *, path: Path) -> str:
    if text.count(begin) != 1 or text.count(end) != 1:
        raise StaleError(f"{path}: expected exactly one {begin!r} … {end!r} region")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{body}{end}{tail}"


def structural_exempt_ids(reg: TypeRegistry) -> list[str]:
    return [d.id for d in reg.types("deterministic_handler") if d.output == "finding"]


def render_ts_set(reg: TypeRegistry) -> str:
    return "".join(f"  '{i}',\n" for i in structural_exempt_ids(reg))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").strip()


def render_alert_table(reg: TypeRegistry) -> str:
    groups: dict[str, list] = {}
    for d in reg.types("alert_kind"):
        groups.setdefault(d.attr("group"), []).append(d)
    rows = ["| Group | Kinds | Subject | Topic |", "|---|---|---|---|"]
    for group, decls in groups.items():
        subjects: list[str] = []
        topics: list[str] = []
        for d in decls:
            subject = d.attr("subject_note") or d.attr("subject_kind")
            if subject not in subjects:
                subjects.append(subject)
            if d.attr("topic") not in topics:
                topics.append(d.attr("topic"))
        kinds = ", ".join(f"`{d.id}`" for d in decls)
        rows.append(
            f"| {GROUP_LABELS[group]} | {kinds} | {' / '.join(subjects)} | {' / '.join(topics)} |")
    return "\n".join(rows) + "\n"


def render_handler_reference(reg: TypeRegistry) -> str:
    rows = [
        "| Handler | One run writes | Refuses without a pool | Structural claims verified | What it does |",
        "|---|---|---|---|---|",
    ]
    for d in sorted(reg.types("deterministic_handler"), key=lambda x: x.id):
        writes = "a finding" if d.output == "finding" else "a trace only"
        rows.append(
            f"| `{d.id}` | {writes} | {'yes' if d.attr('requires_pool') else ''} | "
            f"{'yes' if d.attr('structural_claims') else ''} | {_cell(d.doc)} |")
    return "\n".join(rows) + "\n"


def render_alert_reference(reg: TypeRegistry) -> str:
    rows = [
        "| Kind | Group | Subject | Lifecycle | Topic | Pages | Summary |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in reg.types("alert_kind"):
        pages = "per the page rule" if d.attr("paging") == "page_rule" else "logged only"
        rows.append(
            f"| `{d.id}` | {GROUP_LABELS[d.attr('group')]} | {d.attr('subject_kind')} | "
            f"{d.attr('lifecycle')} | {d.attr('topic')} | {pages} | {_cell(d.doc)} |")
    return "\n".join(rows) + "\n"


def render_citation_target_reference(reg: TypeRegistry) -> str:
    rows = [
        "| Kind | Table | Key column | Material (bytes pinned) | Standing | Named by | What it is |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in sorted(reg.types("citation_target"), key=lambda x: x.id):
        named = [f"`{r}`" for r in d.attr("ref_kinds")] + [
            f"`{b}` block" for b in sorted(d.attr("member_fields"))]
        rows.append(
            f"| `{d.id}` | `{d.attr('table')}` | `{d.attr('column')}` | "
            f"{'yes' if d.attr('material') else ''} | {d.evidence} | {', '.join(named)} | {_cell(d.doc)} |")
    return "\n".join(rows) + "\n"


def render_principal_kinds(reg: TypeRegistry) -> str:
    rows = ["| Kind | Name after the colon | Example | What it is |", "|---|---|---|---|"]
    for d in sorted(reg.types("principal_kind"), key=lambda x: x.id):
        rows.append(
            f"| `{d.id}` | {_cell(d.attr('name_shape'))} | `{d.attr('example')}` | {_cell(d.doc)} |")
    return "\n".join(rows) + "\n"


def render_assertion_types(reg: TypeRegistry) -> str:
    rows = ["| Type | Class | Claim key | Bases | Desk row kinds | What it is |", "|---|---|---|---|---|---|"]
    for d in sorted(reg.types("assertion_type"), key=lambda x: (x.attr("assertion_class"), x.id)):
        rows.append(
            f"| `{d.id}` | {d.attr('assertion_class')} | subject + {', '.join(d.attr('key'))} | "
            f"{', '.join(d.attr('bases'))} | {', '.join(f'`{k}`' for k in d.attr('desk_row_kinds'))} | "
            f"{_cell(d.doc)} |")
    return "\n".join(rows) + "\n"


def render_all(reg: TypeRegistry) -> dict[Path, str]:
    out: dict[Path, str] = {}
    ts = VERDICT_MODEL.read_text(encoding="utf-8")
    out[VERDICT_MODEL] = _replace_region(ts, TS_BEGIN, TS_END, render_ts_set(reg), path=VERDICT_MODEL)
    md = ALERTING.read_text(encoding="utf-8")
    out[ALERTING] = _replace_region(
        md, MD_BEGIN.format(name="alert-kinds"), MD_END.format(name="alert-kinds"),
        render_alert_table(reg), path=ALERTING)
    types_md = TYPES.read_text(encoding="utf-8")
    for name, body in (
        ("deterministic-handlers", render_handler_reference(reg)),
        ("alert-kinds", render_alert_reference(reg)),
        ("citation-targets", render_citation_target_reference(reg)),
        ("principal-kinds", render_principal_kinds(reg)),
        ("assertion-types", render_assertion_types(reg)),
    ):
        types_md = _replace_region(
            types_md, MD_BEGIN.format(name=name), MD_END.format(name=name), body, path=TYPES)
    out[TYPES] = types_md
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="verify only; exit 1 if stale")
    args = parser.parse_args(argv)
    rendered = render_all(default_registry())
    stale = [p for p, text in rendered.items() if p.read_text(encoding="utf-8") != text]
    if args.check:
        for p in stale:
            print(f"stale: {p.relative_to(REPO_ROOT)} — run scripts/gen_type_tables.py", file=sys.stderr)
        return 1 if stale else 0
    for p in stale:
        p.write_text(rendered[p], encoding="utf-8")
        print(f"wrote {p.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
