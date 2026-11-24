# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SourceScope.claim`` — the declaration a CLAIM-class source carries.

A LEAF (pydantic + stdlib only) so the descriptor schema imports it in the
slim registry image.

A source whose ``scope.source_class`` is ``claim`` publishes a party's OWN
claim: a group saying it did something, or a tracker listing what groups
posted. What such a row can support is narrow and fixed in code
(:mod:`legba.data.claim_support`): "<claimant> <verb> <what>", attributed and
dated — never the claimed event as fact. This block says who the claimant is
and records the per-source sign-off a claim source needs before it may go
``active``.

* ``claimant`` — the DECLARED claimant: the name a row is attributed to when
  the feed gives no structured claimant field (for a tracker, the tracker
  itself: "ransomware.live listed ...").
* ``claimant_path`` — OPTIONAL dotted path into the signal's ``payload``
  (e.g. ``item.group_name``) where the feed names the claimant per row. When
  the path resolves to a non-empty string, that string is the claimant
  (``claimant_from: feed_field``); otherwise the declared one is
  (``claimant_from: declared``). The claimant is never taken from model
  output.
* ``verb`` — the reporting verb a supported sentence uses: ``claimed``,
  ``posted`` or ``listed``.
* ``sign_off`` — who signed this source off, when, with a note. Absent = not
  signed off; the lifecycle route refuses ``active`` without it.

HASH-NEUTRAL: ``SourceScope.claim`` is left out of the dump when absent, so
every non-claim descriptor's body and content hash are unchanged.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["CLAIM_VERBS", "ClaimDeclaration", "ClaimSignOff"]

ClaimVerb = Literal["claimed", "posted", "listed"]
CLAIM_VERBS: tuple[str, ...] = ("claimed", "posted", "listed")

_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,5}$")


class ClaimSignOff(BaseModel):
    """One recorded sign-off: who, when, and why."""

    model_config = ConfigDict(strict=True, extra="forbid")

    by: str = Field(min_length=1, max_length=128)
    at: datetime
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("by", "note", mode="after")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a claim sign-off names who signed and why (blank refused)")
        return v


class ClaimDeclaration(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    claimant: str = Field(min_length=1, max_length=200)
    claimant_path: str | None = Field(default=None, max_length=128, exclude_if=lambda v: v is None)
    verb: ClaimVerb = "claimed"
    # C-2: other names the claimant is written as ("NJ Team" for "Nightjar"),
    # read by the attribution check. Left out of the dump when empty, so a
    # C-1 body's hash is unchanged.
    claimant_aliases: list[str] = Field(default_factory=list, max_length=12,
                                        exclude_if=lambda v: not v)
    sign_off: ClaimSignOff | None = Field(default=None, exclude_if=lambda v: v is None)

    @field_validator("claimant", mode="after")
    @classmethod
    def _claimant(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("claim.claimant is blank")
        return v.strip()

    @field_validator("claimant_aliases", mode="after")
    @classmethod
    def _aliases(cls, v: list[str]) -> list[str]:
        out = [a.strip() for a in v if a.strip()]
        if any(len(a) < 2 or len(a) > 200 for a in out):
            raise ValueError("claim.claimant_aliases: each alias is 2-200 characters")
        return out

    @field_validator("claimant_path", mode="after")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v is not None and not _PATH_RE.match(v):
            raise ValueError(
                f"claim.claimant_path {v!r} is not a dotted payload path "
                "(e.g. 'item.group_name')"
            )
        return v
