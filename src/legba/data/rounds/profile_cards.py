# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round cards a PROFILE supplies.

A spec that maps over a list of cards (``card: {from_item: ...}``) names no
card itself: the run's input list does, and the cards are the profile's
prose, not the platform's (a worldview panel's worldviews belong to the
domain the profile models). They live in the profile's prompt package, in a
``round_cards/`` folder beside its modules::

    profiles/<id>/prompts/legba_<id>/round_cards/<name>.md

The prompt package is what the images carry (the runtime and registry builds
copy it into site-packages), so the folder is found through the package, not
through ``profiles/``. The profile is the one ``LEGBA_PROFILE`` selects
(default ``profiles/geopolitics``; ``legba.data.profile.resolve_profile``).

A card the profile does not have is a :class:`~.seats.SeatFailure` naming
the profile and the card — never another profile's card, never an empty
prompt.
"""
from __future__ import annotations

import importlib.util
import re
from collections.abc import Mapping
from pathlib import Path

from ..profile import DEFAULT_PROFILE, PROFILE_ENV, prompts_package_name
from .seats import SeatFailure

#: The folder inside a profile's prompt package that holds its round cards.
CARDS_DIR = "round_cards"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def profile_id(env: Mapping[str, str] | None = None) -> str:
    """The selected profile's id (its directory's name)."""
    import os

    env = os.environ if env is None else env
    return Path(env.get(PROFILE_ENV) or DEFAULT_PROFILE).name


def cards_root(env: Mapping[str, str] | None = None) -> Path:
    """The selected profile's round-card folder. Refuses by name when the
    profile has no prompt package or the package has no such folder."""
    pid = profile_id(env)
    pkg = prompts_package_name(pid)
    spec = importlib.util.find_spec(pkg)
    locations = list(spec.submodule_search_locations or ()) if spec is not None else []
    if not locations:
        raise SeatFailure(f"profile_cards_missing: profile {pid!r} has no prompt package {pkg}")
    root = Path(locations[0]) / CARDS_DIR
    if not root.is_dir():
        raise SeatFailure(f"profile_cards_missing: profile {pid!r} has no {CARDS_DIR}/ folder in {pkg}")
    return root


def card_path(name: str, env: Mapping[str, str] | None = None) -> Path:
    """The file of the profile card ``name`` (a bare slug, no folder)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SeatFailure(f"card_missing: {name!r} is not a profile card name (a lower-case slug)")
    p = cards_root(env) / f"{name}.md"
    if not p.is_file():
        raise SeatFailure(f"card_missing: profile {profile_id(env)!r} has no round card {name!r}")
    return p


__all__ = ["CARDS_DIR", "card_path", "cards_root", "profile_id"]
