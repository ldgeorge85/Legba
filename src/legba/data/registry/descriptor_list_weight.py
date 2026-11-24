# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /registry/descriptors?fields=summary`` — the console's roster weight.

The descriptor list is the registry's machine API: the runtime, bring-up
scripts and the descriptor editor read whole bodies, so an omitted ``fields``
still answers them (``list_weights.py``). The console's rosters do not: the
shell's bound-panel synthesis reads an id, a name and a state; the subject
index, the map's geometry index and the supply-chain desks read
``body.identity.name`` and ``body.scope.{domain, geo, tags, geometry}``.

Roll 84's live walk: the target list was 279 KB (the phone landing's largest
response, over its 0.25 MB budget — the bodies grew with ``_auto_wired_sources``
and ``sources``) and the analyst list 546 KB on every desktop landing.
``fields=summary`` keeps every column and cuts ``body`` to
:data:`SUMMARY_SCOPE_KEYS` of ``scope`` and the identity's name (live: 73 KB
and 71 KB), plus :data:`DECLARED_SOURCES_KEY`: how many sources the body
declares.

Roll 86's live walk: the cut dropped ``body.sources`` whole, and the map reads
"does this subject declare a source" from it (``v4/globe/data.ts``
``subjectsFromRoster``). Every subject read as declaring none, every place was
hatched "not covered", and the hatch sat over every changed area. The count is
the one fact the roster needs from the list (a few bytes a row).

Registry-slim: stdlib only.
"""

from __future__ import annotations

from typing import Any, Literal

__all__ = ["DECLARED_SOURCES_KEY", "DescriptorFields", "SUMMARY_SCOPE_KEYS", "summary_body"]

#: The weights the list answers at; ``None`` (omitted) is the whole body.
DescriptorFields = Literal["summary", "full"]

#: The ``body.scope`` keys a summary keeps.
SUMMARY_SCOPE_KEYS: tuple[str, ...] = ("domain", "geo", "tags", "geometry", "themes")
#: The summary's count of ``body.sources``: present only when the body carries
#: a ``sources`` list (a body without one does not get an invented 0).
DECLARED_SOURCES_KEY = "declared_sources"


def summary_body(body: dict[str, Any] | None) -> dict[str, Any]:
    """``body`` cut to ``identity.name`` and the roster's ``scope`` keys."""
    body = body if isinstance(body, dict) else {}
    out: dict[str, Any] = {}
    identity = body.get("identity")
    if isinstance(identity, dict) and "name" in identity:
        out["identity"] = {"name": identity["name"]}
    scope = body.get("scope")
    if isinstance(scope, dict):
        out["scope"] = {k: scope[k] for k in SUMMARY_SCOPE_KEYS if k in scope}
    sources = body.get("sources")
    if isinstance(sources, list):
        out[DECLARED_SOURCES_KEY] = len(sources)
    return out
