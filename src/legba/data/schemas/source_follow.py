# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SourceDescriptor.follow`` — which outbound references a source may follow.

A LEAF (pydantic + stdlib only) so the descriptor schema imports it in the
slim registry image.

A source that declares a follow policy lets the ingestion intake turn the
references found in its fetched documents into ingestion requests (reason
``followed_reference``, budget class ``follow``) — each one admitted, capped
and refused like any other request. Absent (the default, and every live
source today) = nothing is ever followed. The block is HASH-NEUTRAL: absent is
left out of the dump, so every existing body and content hash is unchanged.

Validated AT SAVE (pydantic, so the registry PUT answers 422):

* ``pipeline`` — the source descriptor the followed references are fetched
  under (its kind must take an address — checked at submit, where the
  registry is read; here only the id's shape);
* ``same_host`` / ``hosts`` — the reference's host must equal the document's
  host, or be on the allow-list (exact host, or ``*.example.org`` for any
  subdomain). Following nothing (``same_host: false`` and no hosts) is
  refused as a policy that cannot do anything;
* ``kinds`` — which links: ``article`` (the document's own page, for a feed
  that carries only a summary), ``anchor`` (a link in the body), ``document``
  (a link to a PDF/office/CSV file). ``image`` is DECLARED, NOT BUILT: the
  media family is a seam (SEAMS #77 / #1), so naming it is refused here;
* ``max_depth`` (1..3) — tightens the intake's global depth limit for chains
  that start at this source; ``max_per_document`` (1..50) — references taken
  from one document;
* ``sink`` — ``document`` (land the followed page as a signal) or
  ``receipt`` (keep the fetched version only).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["FOLLOW_LINK_KINDS", "FOLLOW_SEAM_KINDS", "SourceFollow"]

FollowLinkKind = Literal["article", "anchor", "document", "image"]
#: Built link kinds.
FOLLOW_LINK_KINDS: frozenset[str] = frozenset({"article", "anchor", "document"})
#: Declared, refused at save, with the seam that would build it.
FOLLOW_SEAM_KINDS: dict[str, str] = {
    "image": "SEAMS #77 (the media sink family) / #1 (media extraction): an image "
             "reference has no sink to land in yet",
}

_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

_PipelineId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=128)]


class SourceFollow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    pipeline: _PipelineId
    same_host: bool = True
    hosts: list[str] = Field(default_factory=list, max_length=50)
    kinds: list[FollowLinkKind] = Field(default_factory=lambda: ["article"], min_length=1)
    max_depth: int = Field(default=1, ge=1, le=3)
    max_per_document: int = Field(default=5, ge=1, le=50)
    sink: Literal["document", "receipt"] = "document"

    @field_validator("hosts", mode="after")
    @classmethod
    def _hosts(cls, hosts: list[str]) -> list[str]:
        out: list[str] = []
        for raw in hosts:
            host = str(raw).strip().lower().rstrip(".")
            if not _HOST_RE.match(host):
                raise ValueError(
                    f"follow.hosts entry {raw!r} is not a bare host name (no scheme, "
                    "path or port; '*.example.org' for any subdomain)"
                )
            if host not in out:
                out.append(host)
        return out

    @model_validator(mode="after")
    def _constraints(self) -> "SourceFollow":
        for kind in self.kinds:
            if kind in FOLLOW_SEAM_KINDS:
                raise ValueError(
                    f"follow.kinds {kind!r} is declared but not built: {FOLLOW_SEAM_KINDS[kind]}"
                )
        if not self.same_host and not self.hosts:
            raise ValueError(
                "follow policy follows nothing: same_host is false and no hosts are allowed"
            )
        return self

    def allows_host(self, document_host: str | None, ref_host: str | None) -> bool:
        """True when a reference on ``ref_host`` found in a document on
        ``document_host`` may be followed."""
        if not ref_host:
            return False
        ref_host = ref_host.lower()
        if self.same_host and document_host and ref_host == document_host.lower():
            return True
        for allowed in self.hosts:
            if allowed.startswith("*."):
                if ref_host.endswith(allowed[1:]):
                    return True
            elif ref_host == allowed:
                return True
        return False
