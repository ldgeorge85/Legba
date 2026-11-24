# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SourceDescriptor.sink`` — which pipeline family a source's records land in.

A LEAF (pydantic + stdlib only) so the descriptor schema can import it in the
slim registry image.

The family vocabulary is closed and has two halves:

* BUILT — ``document`` (today's ``signals`` path, the default, and what every
  one of the live sources is) and ``observation`` (numeric series into the
  bitemporal ``observations`` table).
* DECLARED, NOT BUILT — ``register``, ``dataset``, ``library``, ``event``,
  ``media``. Naming one is refused AT SAVE (pydantic validation, so the
  registry PUT answers 422 and the body goes to the dead-letter table) with
  the seam number, never accepted and left to fail at the first poll.

HASH-NEUTRAL: the default block (``family: document``, empty config) is left
out of the dump, so every existing descriptor's body — and therefore its
content hash, its version and its actor id — is byte-identical.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "BUILT_FAMILIES",
    "DEFAULT_FAMILY",
    "ObservationSinkConfig",
    "PERIODS",
    "SEAM_FAMILIES",
    "SINK_FAMILY_SEAM",
    "SeriesSpec",
    "SinkFamily",
    "SourceSink",
    "UNITS",
]

SinkFamily = Literal[
    "document", "observation", "register", "dataset", "library", "event", "media",
]
DEFAULT_FAMILY = "document"
BUILT_FAMILIES: frozenset[str] = frozenset({"document", "observation"})
#: SEAMS #77 — every declared-not-built family and what it would land.
SINK_FAMILY_SEAM = 77
SEAM_FAMILIES: dict[str, str] = {
    "register": "register entries (notices, rules, list snapshots) with supersession links",
    "dataset": "release-versioned dataset rows with a codebook",
    "library": "library works parsed to sections and passages",
    "event": "already-coded events from an external coder, landed as events",
    "media": "image/audio/video items with extraction (SEAMS #1)",
}

#: Period granularity of one observation (what ``valid_from..valid_to`` spans).
PERIODS: frozenset[str] = frozenset({"day", "week", "month", "quarter", "year"})

#: Closed unit vocabulary. Extending it is a reviewed edit here, never a free
#: string in a descriptor — the same rule ``observations.unit`` readers rely on.
UNITS: frozenset[str] = frozenset({
    "count", "vessels", "metric_tonnes", "deadweight_tonnes", "usd", "percent",
    "index", "ratio", "persons", "events",
    # S-1: a price per barrel (EIA Brent / WTI spot) — not ``usd``, which is
    # an amount, and never a free string.
    "usd_per_barrel",
    # I-5: an exchange rate — units of the QUOTE currency per one unit of the
    # BASE currency. Which pair, and which way round, is named by the series
    # and its subject (and said in the series description), never by the
    # unit: one unit for every pair, not a free "usd_per_eur" string each.
    "fx_quote_per_base",
})


class SeriesSpec(BaseModel):
    """One series a source writes — the series registry lives in the sink
    config and is validated at save."""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=128)
    field: str = Field(min_length=1, max_length=128)     # the record field it is read from
    unit: str
    period: str = "day"
    subject_kind: Literal["country", "entity", "target", "global"] = "global"
    methodology_version: str = Field(min_length=1, max_length=64)
    description: str = ""

    @model_validator(mode="after")
    def _closed_vocab(self) -> "SeriesSpec":
        if self.unit not in UNITS:
            raise ValueError(f"series {self.id!r}: unit {self.unit!r} is not in {sorted(UNITS)}")
        if self.period not in PERIODS:
            raise ValueError(f"series {self.id!r}: period {self.period!r} is not in {sorted(PERIODS)}")
        return self


class ObservationSinkConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    series: list[SeriesSpec] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _unique_ids(self) -> "ObservationSinkConfig":
        ids = [s.id for s in self.series]
        if len(ids) != len(set(ids)):
            raise ValueError("sink.config.series ids must be unique")
        return self


class SourceSink(BaseModel):
    """``sink: {family, config}`` on a source descriptor."""

    model_config = ConfigDict(strict=True, extra="forbid")

    family: SinkFamily = "document"
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _family_is_built_and_config_fits(self) -> "SourceSink":
        if self.family in SEAM_FAMILIES:
            raise ValueError(
                f"sink.family={self.family!r} is declared but not built "
                f"(SEAMS #{SINK_FAMILY_SEAM}: {SEAM_FAMILIES[self.family]}); "
                f"built families: {sorted(BUILT_FAMILIES)}"
            )
        if self.family == "document" and self.config:
            raise ValueError("sink.family='document' takes no sink.config")
        if self.family == "observation":
            ObservationSinkConfig.model_validate(self.config, strict=False)
        return self

    def is_default(self) -> bool:
        return self.family == DEFAULT_FAMILY and not self.config

    def observation_config(self) -> ObservationSinkConfig:
        return ObservationSinkConfig.model_validate(self.config, strict=False)
