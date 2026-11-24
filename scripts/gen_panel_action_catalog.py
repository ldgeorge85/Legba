#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the server's PANEL ACTION CATALOGUE from the console's panel contracts (Q-1).

The console declares, per view, the actions Consult may drive and their typed
arguments in ``legba-ui-v3/src/panel-registry/contracts.ts`` (lane U-6). That
file is THE authority. The server must validate a consult's ``panel_action``
against the same catalogue, and the backend image does not ship the console
source, so this script READS the TypeScript and writes
``src/legba/data/panel_action_catalog.json``, which the backend loads
(``legba.data.panel_actions``). The drift test
(``tests/data_pkg/test_consult_panel_actions.py``) regenerates it from the
TypeScript and fails when the committed JSON differs — one list, two readers,
never hand-maintained twice.

The reader is deliberately narrow: it understands exactly the constructs the
contracts file uses (``const X = [...] as const``, ``const X: PanelActionSpec =
{...}``, ``setTab([...])``, inline action objects, the ``READER_CONTRACTS`` /
``SLOT_CONTRACTS`` / ``OPERATOR_CONTRACTS`` arrays, ``COMMON_ACTIONS``, ``BIND_SUBJECT`` / ``BIND_UNIT``,
``OPERATOR_KINDS_WITHOUT_CONTRACT`` and U-7's ``MAP_LAYER_IDS``, which is read
through the rows of ``v4/globe/catalog.ts``) and FAILS LOUD on anything else, so a
contract written in a new shape turns the drift test red instead of being
silently skipped.

Usage::

    python3 scripts/gen_panel_action_catalog.py                 # write
    python3 scripts/gen_panel_action_catalog.py --check         # exit 1 if stale
    python3 scripts/gen_panel_action_catalog.py --ui-src <dir>  # read another checkout's console source
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UI_SRC = REPO_ROOT / "legba-ui-v3" / "src"
CONTRACTS_REL = Path("panel-registry") / "contracts.ts"
OUT = REPO_ROOT / "src" / "legba" / "data" / "panel_action_catalog.json"
CATALOG_VERSION = "panel_action_catalog/1"


class ContractsShapeError(ValueError):
    """The contracts file used a construct this reader does not understand."""


def _strip_comments(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _match(text: str, start: int) -> int:
    """Index just past the bracket that closes the one at ``start``."""
    pairs = {"[": "]", "{": "}", "(": ")"}
    stack = [pairs[text[start]]]
    i = start + 1
    while stack:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c == stack[-1]:
            stack.pop()
        elif c in ")]}":
            raise ContractsShapeError(f"unbalanced {c!r} at {i}")
        i += 1
    return i


def _split_top(body: str) -> list[str]:
    """Split a bracket body on its top-level commas."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(body):
        c = body[i]
        if c in "'\"`":
            j = i + 1
            while body[j] != c:
                j += 2 if body[j] == "\\" else 1
            cur.append(body[i:j + 1])
            i = j + 1
            continue
        if c in "[{(":
            depth += 1
        elif c in "]})":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _fields(obj: str) -> dict[str, str]:
    """``{key: raw value}`` of one ``{ ... }`` object literal."""
    inner = obj.strip()
    if not (inner.startswith("{") and inner.endswith("}")):
        raise ContractsShapeError(f"not an object literal: {inner[:60]!r}")
    out: dict[str, str] = {}
    for part in _split_top(inner[1:-1]):
        m = re.match(r"([A-Za-z_]\w*)\s*:\s*(.*)$", part, re.S)
        if not m:
            raise ContractsShapeError(f"unreadable field {part[:60]!r}")
        out[m.group(1)] = m.group(2).strip()
    return out


GLOBE_CATALOG_REL = Path("v4") / "globe" / "catalog.ts"
# U-7: the map's layer ids are DERIVED in contracts.ts from the globe's own
# rows — exactly this expression, nothing looser, is read through the catalog.
_MAP_LAYER_IDS_EXPR = re.compile(
    r"(?:export\s+)?const\s+(\w+)\s*:\s*readonly string\[\]\s*=\s*"
    r"GLOBE_LAYERS\.filter\(\(l\)\s*=>\s*!l\.unavailable\)\.map\(\(l\)\s*=>\s*l\.id\)"
)


def globe_layer_ids(ui_src: Path) -> list[str]:
    """The ids of ``GLOBE_LAYERS`` rows in ``v4/globe/catalog.ts`` that carry no
    ``unavailable`` field, in order — what ``GLOBE_LAYERS.filter((l) =>
    !l.unavailable).map((l) => l.id)`` evaluates to. Fails loud on any other shape."""
    path = ui_src / GLOBE_CATALOG_REL
    text = _strip_comments(path.read_text(encoding="utf-8"))
    m = re.search(r"export const GLOBE_LAYERS\s*:\s*readonly GlobeLayerDef\[\]\s*=\s*\[", text)
    if not m:
        raise ContractsShapeError(f"missing GLOBE_LAYERS in {GLOBE_CATALOG_REL}")
    start = m.end() - 1
    ids: list[str] = []
    for obj in _split_top(text[start + 1:_match(text, start) - 1]):
        f = _fields(obj)
        if not re.fullmatch(r"'[^']*'", f.get("id", "")):
            raise ContractsShapeError(f"a GLOBE_LAYERS row without a literal id: {obj[:60]!r}")
        if "unavailable" not in f:
            ids.append(f["id"].strip("'"))
    if not ids:
        raise ContractsShapeError("GLOBE_LAYERS has no available layer")
    return ids


class _Reader:
    def __init__(self, text: str, ui_src: Path | None = None) -> None:
        self.text = _strip_comments(text)
        self.arrays: dict[str, list[str]] = {}
        self.specs: dict[str, dict[str, Any]] = {}
        for m in re.finditer(r"const\s+(\w+)\s*=\s*\[", self.text):
            end = _match(self.text, m.end() - 1)
            items = _split_top(self.text[m.end():end - 1])
            if all(re.fullmatch(r"'[^']*'", i) for i in items):
                self.arrays[m.group(1)] = [i.strip("'") for i in items]
        for m in _MAP_LAYER_IDS_EXPR.finditer(self.text):
            if ui_src is None:
                raise ContractsShapeError(f"{m.group(1)} reads GLOBE_LAYERS; the reader needs the console source")
            self.arrays[m.group(1)] = globe_layer_ids(ui_src)

    def _block(self, pattern: str) -> str:
        m = re.search(pattern, self.text)
        if not m:
            raise ContractsShapeError(f"missing {pattern!r}")
        start = m.end() - 1
        return self.text[start:_match(self.text, start)]

    def strings(self, raw: str) -> list[str]:
        raw = raw.strip()
        if raw in self.arrays:
            return list(self.arrays[raw])
        if raw.startswith("["):
            items = _split_top(raw[1:-1])
            if all(re.fullmatch(r"'[^']*'", i) for i in items):
                return [i.strip("'") for i in items]
        raise ContractsShapeError(f"not a string list: {raw[:60]!r}")

    def arg(self, raw: str) -> dict[str, Any]:
        f = _fields(raw)
        out: dict[str, Any] = {
            "name": f["name"].strip("'"),
            "type": f["type"].strip("'"),
            "required": f.get("required", "false") == "true",
        }
        if out["type"] not in ("string", "boolean", "number", "ref", "enum"):
            raise ContractsShapeError(f"unknown arg type {out['type']!r}")
        if "values" in f:
            out["values"] = self.strings(f["values"])
        if "refKinds" in f:
            out["ref_kinds"] = self.strings(f["refKinds"])
        return out

    def spec(self, raw: str) -> dict[str, Any]:
        raw = raw.strip()
        if raw in self.specs:
            return self.specs[raw]
        m = re.fullmatch(r"setTab\((.*)\)", raw, re.S)
        if m:
            return {"id": "set_tab", "args": [{
                "name": "tab", "type": "enum", "required": True, "values": self.strings(m.group(1)),
            }]}
        if re.fullmatch(r"\w+", raw):
            block = self._block(rf"const\s+{raw}\s*:\s*PanelActionSpec\s*=\s*\{{")
            self.specs[raw] = self.spec(block)
            return self.specs[raw]
        f = _fields(raw)
        args_raw = f.get("args", "[]").strip()
        if not args_raw.startswith("["):
            raise ContractsShapeError(f"args is not a list: {args_raw[:60]!r}")
        return {
            "id": f["id"].strip("'"),
            "args": [self.arg(a) for a in _split_top(args_raw[1:-1])],
        }

    def contracts(self, name: str) -> list[dict[str, Any]]:
        block = self._block(rf"export const {name}\s*:\s*readonly PanelContract\[\]\s*=\s*\[")
        out = []
        for obj in _split_top(block[1:-1]):
            f = _fields(obj)
            actions_raw = f["actions"].strip()
            actions = [self.spec(a) for a in _split_top(actions_raw[1:-1])] if actions_raw != "[]" else []
            out.append({
                "id": f["id"].strip("'"),
                "side": f["side"].strip("'"),
                "binds": self.strings(f["binds"]),
                "home": f["home"].strip("'"),
                "actions": {a["id"]: a["args"] for a in actions},
            })
        return out


def build(ui_src: Path) -> dict[str, Any]:
    path = ui_src / CONTRACTS_REL
    source = path.read_text(encoding="utf-8")
    r = _Reader(source, ui_src)
    common_block = r._block(r"export const COMMON_ACTIONS\s*:\s*readonly PanelActionSpec\[\]\s*=\s*\[")
    common = [r.spec(s) for s in _split_top(common_block[1:-1])]
    views = {c["id"]: c for c in r.contracts("READER_CONTRACTS") + r.contracts("SLOT_CONTRACTS")
             + r.contracts("OPERATOR_CONTRACTS")}
    for kind in r.strings(r._block(r"export const OPERATOR_KINDS_WITHOUT_CONTRACT\s*:\s*readonly string\[\]\s*=\s*\[")):
        views.setdefault(kind, {"id": kind, "side": "operator", "binds": [], "home": "stage",
                                "actions": {}, "declared": False})
    for v in views.values():
        v.setdefault("declared", True)
    doc = {
        "version": CATALOG_VERSION,
        "source": str(CONTRACTS_REL).replace("\\", "/"),
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "common_actions": {a["id"]: a["args"] for a in common},
        "bind_subject": r.spec("BIND_SUBJECT")["args"],
        "bind_unit": r.spec("BIND_UNIT")["args"],
        "views": dict(sorted(views.items())),
    }
    return doc


def render(ui_src: Path = DEFAULT_UI_SRC) -> str:
    return json.dumps(build(ui_src), sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--ui-src", type=Path, default=DEFAULT_UI_SRC)
    args = parser.parse_args(argv)
    text = render(args.ui_src)
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale — run scripts/gen_panel_action_catalog.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
