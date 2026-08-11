# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vocabulary registry (per L-101 §8).

Operators add new `entity_class` / `relationship_type` / etc. values at
runtime without a schema migration. The seed set lives in
`legba.data.vocabulary` and is inserted by migration 0010; runtime entries
land via the registry CRUD which L-110 layers on top.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VocabularyEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    family: Literal[
        "entity_class",
        "relationship_type",
        "analyst_kind",
        "source_kind",
        "output_kind",
        "filter_kind",
        "enrichment_kind",
        "discovery_kind",
        # K-G1 (migration 0143): the `entity_edges.edge_family` tier —
        # relation / reference / cooccurrence / structural. The column carries
        # its own CHECK; this row makes the tier map readable through the same
        # registry every other closed vocabulary uses. NOTE the literal is
        # closed: a family registered in the TABLE but missing HERE fails the
        # whole `VocabularyCache.refresh()`, not just its own row.
        "edge_family",
    ]
    value: str
    schema_uri: str = Field(pattern=r"^legba/vocabulary/\d+\.\d+\.\d+$")
    introduced: datetime
    deprecated: datetime | None = None
    notes: str | None = None
    aliases: list[str] = Field(default_factory=list)
    parent: str | None = None

    @field_validator("value")
    @classmethod
    def _value_shape(cls, v: str, info) -> str:
        family = info.data.get("family")
        if family == "relationship_type":
            if not v[:1].isupper() or not v.isidentifier():
                raise ValueError("relationship_type must be PascalCase identifier")
        elif family in (
            "entity_class", "source_kind", "filter_kind", "enrichment_kind",
            "output_kind", "discovery_kind", "analyst_kind", "edge_family",
        ):
            if v != v.lower() or " " in v:
                raise ValueError(f"{family} must be lowercase_snake_case")
        return v


class VocabularyRegistry(BaseModel):
    entries: list[VocabularyEntry]

    def values(self, family: str) -> set[str]:
        return {
            e.value
            for e in self.entries
            if e.family == family and e.deprecated is None
        }
