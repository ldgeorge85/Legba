# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-side classification — the source half of the tag spine (W10 TAX-2).

A source descriptor DECLARES what its stream is: ``scope.classifications`` —
``<family>:<value>`` tags of registered vocabulary families (``demo.axis:x``,
``sanctions.programme:ofac_sdn``). The legacy ``scope.tags`` stay and bind as
``src:<tag>`` (provenance, never a classification). This module is the ONE
home of what happens to that declaration:

* **The tag shape** (`TAG_PATTERN`, `tag_shape_error`) — one rule for a tag a
  source descriptor carries: a bare ``[a-z][a-z0-9_]*`` value OR a namespaced
  ``<family>:<value>`` whose prefix is family-SHAPED (``tags.is_family_shaped``:
  ``src``, a platform family, or ``<profile>.<axis>``). ``schemas/source.py``'s
  ``Tag`` and ``ClassificationTag`` are built on it.
* **The binding** (`bind_declared`, `source_classification`) — the declared
  classifications plus the legacy tags, bound through ``tags.bind_tag`` against
  the process's vocabulary snapshot. ``SourceContext.classification`` carries
  the result; every construction site calls one of these two.
* **Tier-1 stamping** (`lift_declared_tags`) — called by the per-source
  baseline beside the ``scope_geo`` lift: the declared NAMESPACED keys are put
  on the signal's own ``tags`` (first, so the 64-tag cap never drops them), so
  the slice (W8 TAX-1, ``tags && $n``) finds them. A source that declares none
  leaves ``tags`` untouched (the same list object).
* **The write-time bag** (`signal_data`) — ``signals.data.classifications``
  (migration 0228's column, TAG-1's bag shape ``[{family, value, status}]``):
  the source's declared classifications (bound, with status — the legacy
  ``src:`` ones included) followed by the signal's OWN namespaced tags, bound;
  first occurrence of each ``(family, value)`` wins. Bare signal tags (the
  publisher categories) are not classifications and are not stamped. Nothing
  to stamp ⇒ ``None`` ⇒ the column stays NULL, exactly as before.
* **The ingest coercion's preserve rule** (`preserved_tag`) — a payload tag
  that is family-shaped and fits the tag shape is kept VERBATIM (value case
  included — a profile's values may be upper-case and ``bind_tag`` compares
  them exactly); everything else is flattened by the baseline as before.
* **Registration** (`declared_source_classifications`) — what the registry
  validates through ``signal_slice_scope.signal_tag_refusals``: an unloaded
  family is refused by name, then an unknown value of a loaded one.

A LEAF: stdlib + ``legba.data.tags`` (itself stdlib-only). ``schemas/source.py``
imports it, so it must never import ``schemas`` (``tags`` documents the same
cycle).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Iterable, Sequence

from .tags import (
    CLASSIFICATIONS_KEY,
    SRC_FAMILY,
    STATUS_LEGACY_SRC,
    BoundTag,
    VocabularyIndex,
    bind_tags,
    split_tag,
    tag_vocabulary_index,
)

#: The longest tag a source descriptor (or the ingest coercion) keeps. The
#: longest live ``<family>:<value>`` key on 2026-10-04 is 43 characters
#: (``crosswalk``), so the historical 64 holds for namespaced tags too.
TAG_MAX_LENGTH = 64

#: ONE regex, bare OR namespaced: ``x`` / ``<family>:<value>`` where the family
#: is one segment (a platform family, ``src``) or a dotted ``<profile>.<axis>``
#: (at most three segments) and the value is any run of non-space characters
#: (a profile's value may be upper-case; the crosswalk's carries a colon). The
#: prefix being an actual family SHAPE is checked by `tag_shape_error`.
TAG_PATTERN = r"^[a-z][a-z0-9_]*(?:(?:\.[a-z][a-z0-9_]*){0,2}:\S+)?$"
_TAG_RE = re.compile(TAG_PATTERN)

#: The baseline's cap on ``signals.tags`` (unchanged).
SIGNAL_TAG_CAP = 64

#: At most this many declared classifications per source.
MAX_DECLARED_CLASSIFICATIONS = 32


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def tag_shape_error(tag: Any) -> str | None:
    """Why ``tag`` is not a source-descriptor tag, or ``None`` when it is."""
    if not isinstance(tag, str) or not tag:
        return "a tag is a non-empty string"
    if len(tag) > TAG_MAX_LENGTH:
        return f"tag {tag!r} is longer than {TAG_MAX_LENGTH} characters"
    if _TAG_RE.match(tag) is None:
        return (
            f"tag {tag!r} is neither a bare [a-z][a-z0-9_]* value nor "
            "<family>:<value>"
        )
    if ":" in tag and split_tag(tag)[0] is None:
        prefix = tag.partition(":")[0]
        return (
            f"{prefix!r} is not a family: a namespaced tag's prefix is src, a "
            "platform family, or <profile>.<axis>"
        )
    return None


def classification_shape_error(tag: Any) -> str | None:
    """Why ``tag`` is not a declared classification (``<family>:<value>`` of a
    real family — never bare, never the reserved ``src``), or ``None``."""
    err = tag_shape_error(tag)
    if err is not None:
        return err
    fam, _ = split_tag(tag)
    if fam is None:
        return (
            f"classification {tag!r} is bare: declare <family>:<value> (a bare "
            "value belongs in scope.tags)"
        )
    if fam == SRC_FAMILY:
        return f"classification {tag!r}: 'src' is reserved for provenance, not a family"
    return None


def preserved_tag(raw: Any) -> str | None:
    """The ingest coercion's preserve rule: ``raw`` stripped, when it is a
    family-shaped ``<family>:<value>`` within the tag shape; else ``None`` (the
    caller flattens it as a bare value)."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if ":" not in s or tag_shape_error(s) is not None:
        return None
    return s


# ---------------------------------------------------------------------------
# The binding (SourceContext.classification)
# ---------------------------------------------------------------------------


def _scope_list(scope: Any, key: str) -> list[str]:
    if scope is None:
        return []
    raw = scope.get(key) if isinstance(scope, Mapping) else getattr(scope, key, None)
    if not isinstance(raw, (list, tuple)):
        return []
    return [t for t in raw if isinstance(t, str) and t.strip()]


def declared_raw(scope: Any) -> list[str]:
    """The raw strings a source scope declares: ``classifications`` then the
    legacy ``tags`` (which bind as ``src:``). ``scope`` is a ``SourceScope``,
    a head body's ``scope`` mapping, or ``None``."""
    return _scope_list(scope, "classifications") + _scope_list(scope, "tags")


def bind_declared(scope: Any, *, families: VocabularyIndex) -> tuple[BoundTag, ...]:
    """``SourceContext.classification`` for ``scope`` against ``families``."""
    return tuple(bind_tags(declared_raw(scope), families=families))


async def source_classification(scope: Any) -> tuple[BoundTag, ...]:
    """`bind_declared` against the process's current vocabulary snapshot."""
    if not declared_raw(scope):
        return ()
    return bind_declared(scope, families=await tag_vocabulary_index())


# ---------------------------------------------------------------------------
# Tier-1 stamping (beside the scope_geo lift) and the write-time bag
# ---------------------------------------------------------------------------


def declared_tag_keys(classification: Iterable[BoundTag]) -> list[str]:
    """The ``<family>:<value>`` keys of the declared NAMESPACED classifications
    (the legacy ``src:`` ones stay off ``signals.tags``, as ``scope.tags``
    always did)."""
    return [
        b.key for b in classification or ()
        if b.family and b.value and b.status != STATUS_LEGACY_SRC
    ]


def lift_declared_tags(tags: list[str], classification: Iterable[BoundTag]) -> list[str]:
    """``tags`` with the declared keys first (deduplicated, capped). No
    declared key ⇒ ``tags`` itself, unchanged."""
    keys = declared_tag_keys(classification)
    if not keys:
        return tags
    return list(dict.fromkeys([*keys, *tags]))[:SIGNAL_TAG_CAP]


def _own_namespaced(signal_tags: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for t in signal_tags or ():
        if not isinstance(t, str):
            continue
        fam, _ = split_tag(t.strip())
        if fam is not None and fam != SRC_FAMILY:
            out.append(t)
    return out


def signal_classifications(
    classification: Sequence[BoundTag],
    signal_tags: Iterable[Any],
    *,
    families: VocabularyIndex,
) -> list[dict[str, str]]:
    """The ``data.classifications`` list for one signal (see the module doc)."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    own = bind_tags(_own_namespaced(signal_tags), families=families)
    for b in [*(classification or ()), *own]:
        k = (b.family, b.value)
        if not b.family or k in seen:
            continue
        seen.add(k)
        out.append(b.as_classification())
    return out


async def signal_data(
    signal_tags: Iterable[Any], classification: Sequence[BoundTag] = (),
) -> dict[str, Any] | None:
    """The ``signals.data`` bag a canonical write carries, or ``None`` (NULL)
    when there is nothing to stamp. Reads the vocabulary snapshot only when the
    signal carries a namespaced tag of its own."""
    own = _own_namespaced(signal_tags)
    if not own and not classification:
        return None
    families = await tag_vocabulary_index() if own else VocabularyIndex()
    cls = signal_classifications(classification, own, families=families)
    return {CLASSIFICATIONS_KEY: cls} if cls else None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def declared_source_classifications(descriptor: Any) -> list[str]:
    """The ``scope.classifications`` a SOURCE descriptor declares (``[]`` for
    any other family — a target scope has no such key)."""
    return _scope_list(getattr(descriptor, "scope", None), "classifications")


__all__ = [
    "MAX_DECLARED_CLASSIFICATIONS",
    "SIGNAL_TAG_CAP",
    "TAG_MAX_LENGTH",
    "TAG_PATTERN",
    "bind_declared",
    "classification_shape_error",
    "declared_raw",
    "declared_source_classifications",
    "declared_tag_keys",
    "lift_declared_tags",
    "preserved_tag",
    "signal_classifications",
    "signal_data",
    "source_classification",
    "tag_shape_error",
]
