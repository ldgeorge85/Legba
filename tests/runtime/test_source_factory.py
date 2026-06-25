# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :mod:`legba.runtime.source_factory` — the generic source-kind
factory wired into :attr:`legba.runtime.dapr_actors._TargetDeps.source_factory`.

Coverage:

  * :func:`discover_source_kinds` returns the first-party kinds whose
    optional deps are always present in the test environment (rss,
    acled, mediacloud, opensanctions, plus a couple more we expect to
    succeed defensively).
  * :func:`build_source_handler` constructs an ``RSSSourceHandler``
    from a Brazil-style descriptor config (``url`` only, plus the
    property-factory wrapper shape the registry emits).
  * Unknown kinds raise :class:`ValueError` with a helpful operator
    message listing the kinds the runtime actually knows about.
  * The ``secrets_resolve`` callable is threaded into handlers that
    declare a matching constructor parameter (mediacloud's
    ``secret_resolver``), without breaking handlers that don't (RSS).

These tests deliberately avoid network I/O — we only exercise the
construction surface, not the handler ``pull`` loop (which has its own
test module under ``tests/data_pkg/``).
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.runtime.source_factory import (
    build_source_handler,
    discover_source_kinds,
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_source_kinds_includes_first_party_kinds() -> None:
    """The registry must list every kind whose deps are guaranteed by the
    base ``pyproject.toml`` install (no optional extras).

    RSS / ACLED / MediaCloud / OpenSanctions all depend only on
    ``httpx`` + ``pydantic`` (+ ``feedparser`` for RSS) — these are
    hard deps of ``legba``, so their handler modules must import in
    every environment.  We assert against this floor rather than the
    full kind list so optional-extras-missing environments
    (no ``google-cloud-bigquery`` for GDELT, no ``telethon`` for
    Telegram) don't flunk this test.
    """
    registry = discover_source_kinds()

    expected_floor = {"rss", "geojson", "acled", "mediacloud", "opensanctions"}
    missing = expected_floor - set(registry.keys())
    assert not missing, (
        f"first-party hard-dep kinds missing from registry: {missing}; "
        f"got kinds={sorted(registry.keys())}"
    )


def test_discover_source_kinds_returns_handler_classes() -> None:
    """Each registry value must be the handler class itself (NOT an
    instance), with a ``config_schema`` classvar pointing at the
    pydantic config type the factory uses for parsing.
    """
    registry = discover_source_kinds()

    cls = registry["rss"]
    # Classes — instantiation is the factory's job, not discovery's.
    assert isinstance(cls, type)
    # L-102 §2 classvar surface.
    assert getattr(cls, "kind", None) == "rss"
    assert hasattr(cls, "config_schema")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_build_rss_handler_from_brazil_style_config() -> None:
    """Brazil's ``epe_rss`` source binding stores its config as
    ``{"url": "https://www.epe.gov.br/feed"}`` — the simplest shape
    the descriptor emits.  The factory must parse it into the handler's
    ``RSSConfig`` and return a fully-constructed ``RSSSourceHandler``.
    """
    handler = build_source_handler(
        "rss",
        {"url": "https://www.epe.gov.br/feed"},
    )
    assert handler.kind == "rss"
    # config_schema parsed the dict; the handler stashed it.
    assert handler._config.url == "https://www.epe.gov.br/feed"


def test_build_rss_handler_unwraps_property_factory_dict() -> None:
    """Registry-stored configs wrap scalars in property-factory dicts
    (``{"raw": "...", "ui_hint": {}, "factory_kind": "text"}``).  The
    factory must unwrap these before validating against
    ``config_schema``.
    """
    handler = build_source_handler(
        "rss",
        {
            "url": {
                "raw": "https://www.epe.gov.br/feed",
                "ui_hint": {},
                "factory_kind": "text",
            },
        },
    )
    assert handler.kind == "rss"
    assert handler._config.url == "https://www.epe.gov.br/feed"


def test_build_geojson_handler_from_descriptor_config() -> None:
    """The model-free GIS source: the factory must parse a GeoJSON source
    binding (``url`` + the optional structured-feed knobs, in the
    property-factory wrapper shape the registry emits) into a
    ``GeoJSONSourceHandler``.
    """
    handler = build_source_handler(
        "geojson",
        {
            "url": {
                "raw": "https://earthquake.invalid/feed.geojson",
                "factory_kind": "text",
            },
            "max_features": {"raw": 5000, "factory_kind": "number"},
        },
    )
    assert handler.kind == "geojson"
    assert handler._config.url == "https://earthquake.invalid/feed.geojson"
    assert handler._config.max_features == 5000


def test_build_unknown_kind_raises_valueerror() -> None:
    """Unknown kinds must surface a loud ``ValueError`` listing the
    kinds the runtime actually knows — operators copy-paste these
    when reconciling a descriptor that fails to activate.
    """
    with pytest.raises(ValueError) as exc:
        build_source_handler("not_a_real_kind", {})

    msg = str(exc.value)
    assert "not_a_real_kind" in msg
    # Helpful: enumerate the kinds the runtime IS willing to build.
    assert "rss" in msg
    assert "known" in msg.lower()


def test_secrets_resolve_threaded_to_handler_that_accepts_it() -> None:
    """The Discord webhook handler takes ``secret_resolver`` in ``__init__``.
    The factory must thread the passed-in ``secrets_resolve`` callable
    through to that slot so the handler can resolve its Ed25519 public
    key at activation time.

    Discord is a good probe here because its config schema uses plain
    ``str`` fields (no nested ``Secret`` model), so the unwrap path
    doesn't change shape — we can assert on resolver-threading without
    fighting the descriptor-side factory wrapper.
    """
    # Discord webhook requires nacl for runtime cryptographic
    # verification; the module imports it eagerly so we skip cleanly
    # when the extra isn't installed in this test environment.
    pytest.importorskip("nacl.signing")

    async def resolver(secret_id: str) -> bytes:
        return b"a" * 64  # hex-encoded Ed25519 pubkey shape, fake.

    handler = build_source_handler(
        "discord_webhook",
        {
            "application_id": "1234567890",
            "public_key_secret": "discord.brazil.pubkey",
        },
        secrets_resolve=resolver,
    )
    assert handler.kind == "discord_webhook"
    # DiscordWebhookSourceHandler stashes it on ``_secret_resolver``.
    assert handler._secret_resolver is resolver


def test_secrets_resolve_omitted_when_handler_doesnt_take_one() -> None:
    """RSS doesn't have a ``secret_resolver`` parameter — passing one
    must NOT error.  The factory inspects the constructor signature
    and only fills slots the handler declares.
    """
    async def resolver(secret_id: str) -> bytes:
        return b"unused"

    # Smoke: this must not raise a TypeError about an unexpected kwarg.
    handler = build_source_handler(
        "rss",
        {"url": "https://example.invalid/feed"},
        secrets_resolve=resolver,
    )
    assert handler.kind == "rss"


def test_build_passes_registry_through_for_test_isolation() -> None:
    """A pre-built registry must be honored — tests can scope the
    factory to just one kind without paying the discovery walk.
    """
    real = discover_source_kinds()
    scoped: dict[str, Any] = {"rss": real["rss"]}

    handler = build_source_handler(
        "rss",
        {"url": "https://example.invalid/feed"},
        registry=scoped,
    )
    assert handler.kind == "rss"

    # And the unknown-kind path against the scoped registry surfaces
    # only the kinds in the scope.
    with pytest.raises(ValueError) as exc:
        build_source_handler("acled", {}, registry=scoped)
    msg = str(exc.value)
    assert "acled" in msg
    assert "['rss']" in msg
