# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The typed records a source handler yields — a small discriminated union.

==================  ===========================  ==============================
family              record type                  lands in
==================  ===========================  ==============================
``document``        :class:`Signal` (unchanged)  ``signals`` + NATS + sweeps
``observation``     :class:`ObservationRecord`   ``observations`` (bitemporal)
==================  ===========================  ==============================

The union is discriminated by TYPE, not by a tag field: ``Signal`` cannot grow
a discriminator without changing every published message
(``model_dump_json`` is the wire envelope), and the document path must stay
byte-identical. :func:`record_family` is the one place the mapping lives.

Register entries, dataset releases, library works, external-coder events and
media are DECLARED families (:data:`~legba.data.schemas.source_sink.SEAM_FAMILIES`,
SEAMS #77) refused at descriptor save; there is deliberately no empty record
class for them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..schemas.source_sink import PERIODS, UNITS
from ._contract import Signal

__all__ = ["ObservationRecord", "RECORD_FAMILIES", "record_family"]


class ObservationRecord(BaseModel):
    """One value of one series for one subject and one period.

    TIME, explicitly: ``valid_from``/``valid_to`` is the period the number is
    ABOUT (``period`` names its granularity); ``record_time`` is when the
    PROVIDER published or revised it, or ``None`` when the provider gives no
    revision stamp — the sink then records the fetch time of the poll that
    first saw this value. Our own write time is the row's ``loaded_at``.

    ``fetch_sha256`` is the sha256 of the response body this value was parsed
    from. A handler that pages may set it itself; otherwise the actor fills it
    from the fetch receipt of the response the record was yielded after.
    """

    model_config = ConfigDict(extra="forbid")

    record_type: Literal["observation"] = "observation"
    series_id: str = Field(min_length=1, max_length=128)
    subject_kind: Literal["country", "entity", "target", "global"] = "global"
    subject: str = Field(min_length=1, max_length=256)
    valid_from: date
    valid_to: date
    period: str = "day"
    value: Decimal | None = None
    value_text: str | None = None
    unit: str
    record_time: datetime | None = None
    methodology_version: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1, max_length=4096)
    fetch_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> "ObservationRecord":
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")
        if (self.value is None) == (self.value_text is None):
            raise ValueError("exactly one of value / value_text")
        if self.unit not in UNITS:
            raise ValueError(f"unit {self.unit!r} is not in {sorted(UNITS)}")
        if self.period not in PERIODS:
            raise ValueError(f"period {self.period!r} is not in {sorted(PERIODS)}")
        if self.record_time is not None and self.record_time.tzinfo is None:
            raise ValueError("record_time must be timezone-aware")
        return self

    def key(self) -> tuple[str, str, date, date]:
        """The identity a revision is detected on."""
        return (self.series_id, self.subject, self.valid_from, self.valid_to)


RECORD_FAMILIES: dict[type, str] = {
    Signal: "document",
    ObservationRecord: "observation",
}


def record_family(record: Any) -> str:
    """The sink family of ``record``; ``TypeError`` for an untyped yield."""
    for cls, family in RECORD_FAMILIES.items():
        if isinstance(record, cls):
            return family
    raise TypeError(
        f"a source handler yielded {type(record).__name__}, which is not a "
        f"typed record ({', '.join(c.__name__ for c in RECORD_FAMILIES)})"
    )
