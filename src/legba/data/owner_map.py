# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The owner map: which origins belong to one owner beyond what the domain says.

The generic rules in ``legba.data.origins`` already put two feeds of one
registrable domain in one group. They cannot know that one publisher runs two
domains (``example.com`` and ``example.net``), that a proxied feed and a direct
feed are the same publisher, or that a Telegram channel is run by a news
outlet. That knowledge is instance data, so it lives in a file the deployment
supplies, never in code:

    groups:      a derived group key  -> the owner group key it joins
    channels:    a Telegram channel   -> the owner group key it joins
    sources:     a source descriptor  -> the owner group key it joins
    aggregators: source descriptors whose items are owned by the publisher of
                 the underlying article (beyond the built-in aggregator kinds)
    labels:      an owner group key   -> a display label

The real file is git-ignored (``seeds/owner_map.yaml``); the tracked
``seeds/owner_map.example.yaml`` shows the format with invented names. An
absent default file is an empty map; a file named explicitly (argument or the
``LEGBA_OWNER_MAP`` variable) must exist.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DEFAULT_OWNER_MAP_PATH",
    "EMPTY_OWNER_MAP",
    "OWNER_MAP_ENV",
    "OwnerMap",
    "OwnerMapError",
    "channel_slug",
    "load_owner_map",
    "parse_owner_map",
]

OWNER_MAP_ENV = "LEGBA_OWNER_MAP"
DEFAULT_OWNER_MAP_PATH = Path(__file__).resolve().parents[3] / "seeds" / "owner_map.yaml"

#: The shape the ``origin_groups.key`` column accepts.
_GROUP_KEY = re.compile(r"^[a-z][a-z_]*:.{1,250}$")


class OwnerMapError(ValueError):
    """The owner map file is malformed."""


def channel_slug(name: Any) -> str:
    """A Telegram channel name as it is compared: no ``@``, lower case."""
    return re.sub(r"[^a-z0-9_]+", "", str(name or "").strip().lstrip("@").lower())


@dataclass(frozen=True)
class OwnerMap:
    groups: Mapping[str, str] = field(default_factory=dict)
    channels: Mapping[str, str] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)
    aggregators: frozenset[str] = frozenset()
    labels: Mapping[str, str] = field(default_factory=dict)

    def label_for(self, group_key: str, default: str) -> str:
        return self.labels.get(group_key, default)

    def __bool__(self) -> bool:
        return bool(self.groups or self.channels or self.sources or self.aggregators)


EMPTY_OWNER_MAP = OwnerMap()


def _mapping(raw: Any, name: str, *, key_fn=lambda k: k) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise OwnerMapError(f"{name}: expected a mapping")
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = key_fn(str(k).strip())
        target = str(v or "").strip()
        if not key:
            raise OwnerMapError(f"{name}: an empty key")
        if not _GROUP_KEY.match(target):
            raise OwnerMapError(f"{name}[{k!r}]: {target!r} is not a group key like 'org:name'")
        out[key] = target
    return out


def parse_owner_map(data: Any) -> OwnerMap:
    """Validate a parsed YAML document into an :class:`OwnerMap`."""
    if data is None:
        return EMPTY_OWNER_MAP
    if not isinstance(data, Mapping):
        raise OwnerMapError("the owner map must be a mapping")
    known = {"version", "groups", "channels", "sources", "aggregators", "labels"}
    extra = sorted(set(map(str, data)) - known)
    if extra:
        raise OwnerMapError(f"unknown keys: {', '.join(extra)}")
    aggregators = data.get("aggregators") or []
    if not isinstance(aggregators, (list, tuple)):
        raise OwnerMapError("aggregators: expected a list of source ids")
    labels = data.get("labels") or {}
    if not isinstance(labels, Mapping):
        raise OwnerMapError("labels: expected a mapping")
    return OwnerMap(
        groups=_mapping(data.get("groups"), "groups"),
        channels=_mapping(data.get("channels"), "channels", key_fn=channel_slug),
        sources=_mapping(data.get("sources"), "sources"),
        aggregators=frozenset(str(a).strip() for a in aggregators if str(a).strip()),
        labels={str(k).strip(): str(v).strip() for k, v in labels.items()},
    )


def load_owner_map(path: str | os.PathLike[str] | None = None) -> OwnerMap:
    """The deployment's owner map. ``path`` or ``LEGBA_OWNER_MAP`` name a file
    that must exist; with neither, the default file is used if present and the
    map is empty if not."""
    import yaml

    explicit = path or os.environ.get(OWNER_MAP_ENV)
    target = Path(explicit) if explicit else DEFAULT_OWNER_MAP_PATH
    if not target.is_file():
        if explicit:
            raise OwnerMapError(f"owner map not found: {target}")
        return EMPTY_OWNER_MAP
    with target.open("r", encoding="utf-8") as fh:
        return parse_owner_map(yaml.safe_load(fh))
