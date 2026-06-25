# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioning + content-hashing (per L-101 §7)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_json_bytes(obj: dict[str, Any]) -> bytes:
    """Sort keys, no spaces, UTF-8, ensure_ascii=False."""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def content_hash(descriptor: BaseModel) -> str:
    """Compute the SHA-256 hex content-hash of a descriptor.

    The `identity.version` field is excluded from the hash input so the hash
    doesn't chase its own tail. Per L-101 §7.
    """
    payload = descriptor.model_dump(
        mode="json", exclude={"identity": {"version"}}, by_alias=True
    )
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class ConversionWebhook(BaseModel):
    """Schema-major-bump webhook (per L-101 §7)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    from_uri: str = Field(pattern=r"^legba/.+/\d+\.\d+\.\d+$")
    to_uri: str = Field(pattern=r"^legba/.+/\d+\.\d+\.\d+$")
    impl: str
    direction: Literal["forward", "bidirectional"] = "forward"

    @model_validator(mode="after")
    def _same_family(self) -> "ConversionWebhook":
        fam_from = self.from_uri.rsplit("/", 1)[0]
        fam_to = self.to_uri.rsplit("/", 1)[0]
        if fam_from != fam_to:
            raise ValueError(
                f"cross-family conversion not allowed: {fam_from} → {fam_to}"
            )
        return self
