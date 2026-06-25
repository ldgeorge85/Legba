# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""actor_id grammar — content-hash slice is 16 chars (Phase 5 hardening item 7).

Pre-bring-up the slice was 8 chars; two descriptor versions whose
content hashes share an 8-char prefix would collide on actor_id. The
production constructor was widened to 16 chars in 2026-05.

This test pins the contract so the widening doesn't silently revert.
"""

from __future__ import annotations

import pytest

from legba.runtime.reconcile import _default_actor_id


def test_default_actor_id_uses_16_chars():
    actor_id = _default_actor_id(
        "target", "india_energy", "abcdef0123456789ffffffffffff",
    )
    # Grammar: ``kind::id::ver16``.
    assert actor_id == "target::india_energy::abcdef0123456789"
    parts = actor_id.split("::")
    assert len(parts) == 3
    assert len(parts[2]) == 16


def test_default_actor_id_pads_short_versions():
    """If the version is shorter than 16 chars, leave it as-is (caller is
    responsible for providing well-formed content hashes — only the
    empty/None case gets the fallback)."""
    actor_id = _default_actor_id("analyst", "foo", "shorthash")
    parts = actor_id.split("::")
    assert parts[2] == "shorthash"


def test_default_actor_id_fallback_for_empty_version():
    actor_id = _default_actor_id("target", "foo", "")
    parts = actor_id.split("::")
    assert parts[2] == "0" * 16
    assert len(parts[2]) == 16


def test_default_actor_id_fallback_for_none_version():
    # Defensive — production callers should always pass a string, but the
    # helper accepts a None-ish input via `or ""`.
    actor_id = _default_actor_id("target", "foo", None)  # type: ignore[arg-type]
    parts = actor_id.split("::")
    assert parts[2] == "0" * 16


def test_distinct_long_hashes_no_longer_collide():
    """Two hashes sharing an 8-char prefix get distinct actor_ids now."""
    hash_a = "deadbeef" + "1" * 8 + "rest"
    hash_b = "deadbeef" + "2" * 8 + "rest"
    a = _default_actor_id("target", "x", hash_a)
    b = _default_actor_id("target", "x", hash_b)
    assert a != b
    # Sanity: under the OLD [:8] grammar these would have collided.
    assert hash_a[:8] == hash_b[:8]


def test_kind_and_id_round_trip_through_actor_id():
    """Identity is recoverable from the actor_id by splitting on ``::``."""
    actor_id = _default_actor_id(
        "analyst", "weather.predictor", "0123456789abcdef" + "0" * 16,
    )
    kind, descriptor_id, ver_short = actor_id.split("::")
    assert kind == "analyst"
    assert descriptor_id == "weather.predictor"
    assert ver_short == "0123456789abcdef"
