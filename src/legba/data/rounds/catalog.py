# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registered names a round spec may use (V5, V8, V10, V12).

The validator is pure: it is handed a :class:`Catalog` and never looks
anything up on its own. :func:`default_catalog` builds the house one from
``catalog.yaml`` (planes and their families, packs and whether they are
run-cut, code functions, score functions, gate names, reader allowances) plus
the row types of :mod:`.rows`. A test pins the gate names to the gates
:mod:`.gates` implements.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .rows import ROW_TYPES

CATALOG_PATH = Path(__file__).with_name("catalog.yaml")


@dataclass(frozen=True)
class CodeFn:
    name: str
    output: str
    pure: bool


@dataclass(frozen=True)
class Catalog:
    planes: Mapping[str, str]
    packs: Mapping[str, bool]
    code_fns: Mapping[str, CodeFn]
    score_fns: frozenset[str]
    gates: frozenset[str]
    row_types: frozenset[str]
    reader_allowed: frozenset[str] = frozenset()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def family(self, plane: str | None) -> str | None:
        return self.planes.get(plane) if plane else None

    def with_reader_allowed(self, *spec_ids: str) -> Catalog:
        return replace(self, reader_allowed=self.reader_allowed | frozenset(spec_ids))


def catalog_from_mapping(raw: Mapping[str, Any]) -> Catalog:
    packs = {str(k): bool((v or {}).get("run_cut", False)) for k, v in (raw.get("packs") or {}).items()}
    fns = {str(k): CodeFn(str(k), str(v["output"]), bool(v.get("pure", False)))
           for k, v in (raw.get("code_fns") or {}).items()}
    return Catalog(
        planes={str(k): str(v) for k, v in (raw.get("planes") or {}).items()},
        packs=packs,
        code_fns=fns,
        score_fns=frozenset(raw.get("score_fns") or ()),
        gates=frozenset(raw.get("gates") or ()),
        row_types=frozenset(ROW_TYPES),
        reader_allowed=frozenset(raw.get("reader_allowed") or ()),
    )


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    return catalog_from_mapping(yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")))
