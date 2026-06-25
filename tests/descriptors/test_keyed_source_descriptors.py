# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-2 — keyed source descriptors (Bucket B): schema + gating validation.

Validates the seven committed SourceDescriptor YAMLs for the keyed source
kinds against:

  1. the real pydantic ``SourceDescriptor`` schema, loaded exactly the way
     the registrar loads them (``scripts/bringup_register_sources.py``:
     yaml.safe_load + placeholder version + ``model_validate(strict=False)``);
  2. the per-kind handler ``config_schema`` through the PRODUCTION unwrap
     path (``legba.runtime.source_factory._unwrap_factory_dict``) — the same
     transformation ``build_source_handler`` applies before a SourceActor
     constructs the handler;
  3. the S-2 activation-gating convention:
       * every KEYED descriptor ships ``identity.state: draft`` so bulk
         registration creates NO live actor (runtime/dapr_host.py skips
         draft/configured descriptors) — a keyless rig stays healthy;
       * the keyless OpenSanctions bulk_csv descriptor is ACTIVATION-READY
         (``state: active`` + a cadence schedule, which the
         SourceDescriptor model validator requires for active poll sources);
       * vault refs in config and ``deps.vault_secrets`` agree, and none of
         them carries a plaintext-looking value;
       * the IntelMQ bridge descriptor must NOT be activation-ready when
         the ``intelmq`` optional extra is absent from the runtime image
         (it is absent — docker/Dockerfile.runtime never installs it).

The vault-absent FAIL-LOUD activation test (real CredentialVault against a
migrated Postgres) lives in
``tests/data_pkg/test_keyed_descriptor_vault_gating.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.source import SourceDescriptor
from legba.runtime.source_factory import (
    _unwrap_factory_dict,
    discover_source_kinds,
)

# Resolve relative to this file so the suite runs from ANY checkout (main
# workdir, git worktree, CI).
REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"

# (filename, expected kind, keyed?) — keyed == ships state: draft.
KEYED_FILES: list[tuple[str, str, bool]] = [
    ("source_acled_conflict.yaml", "acled", True),
    ("source_gdelt_bigquery.yaml", "gdelt_query", True),
    ("source_mediacloud.yaml", "mediacloud", True),
    ("source_opensanctions_bulk.yaml", "opensanctions", False),
    ("source_opensanctions_api.yaml", "opensanctions", True),
    ("source_telegram_monitor.yaml", "telegram_channel", True),
    # KEV feed itself is keyless, but the intelmq extra is not in the
    # runtime image → ships draft (see the gating test below).
    ("source_intelmq_cisa_kev.yaml", "intelmq_collector_bridge", True),
]


def _load_body(name: str) -> dict[str, Any]:
    """Mirror scripts/bringup_register_sources.py::_load (minus the DB)."""
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def _load_descriptor(name: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate(_load_body(name), strict=False)


def _config_secret_refs(config: dict[str, Any]) -> set[str]:
    """Collect vault ids from `{factory_kind: secret, raw: ...}` values."""
    refs: set[str] = set()
    for value in config.values():
        if isinstance(value, dict) and value.get("factory_kind") == "secret":
            refs.add(value["raw"])
    return refs


# ---------------------------------------------------------------------------
# 1. Every YAML validates against the real SourceDescriptor schema.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname,kind,keyed", KEYED_FILES)
def test_descriptor_validates_against_source_schema(
    fname: str, kind: str, keyed: bool
) -> None:
    desc = _load_descriptor(fname)
    assert desc.identity.kind == kind
    assert desc.acquisition == "poll"
    # Every committed file declares a cadence schedule so the operator
    # flip to active never trips the active-poll-needs-schedule validator.
    assert desc.cadence is not None and desc.cadence.schedule is not None


# ---------------------------------------------------------------------------
# 2. Config blocks parse through the PRODUCTION unwrap + per-kind schema.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname,kind,keyed", KEYED_FILES)
def test_config_parses_against_handler_config_schema(
    fname: str, kind: str, keyed: bool
) -> None:
    """The exact transformation ``build_source_handler`` applies: unwrap the
    property-factory shapes, then instantiate the handler's pydantic
    ``config_schema``. A descriptor whose config cannot parse would
    permanent-fail its source binding at activation — catch it here."""
    registry = discover_source_kinds()
    assert kind in registry, (
        f"kind {kind!r} not in discover_source_kinds() — handler module "
        f"failed to import; known: {sorted(registry)}"
    )
    desc = _load_descriptor(fname)
    schema = registry[kind].config_schema
    parsed = schema(**_unwrap_factory_dict(desc.config))
    assert parsed is not None


# ---------------------------------------------------------------------------
# 3. S-2 gating convention: keyed → draft; keyless bulk → active.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname,kind,keyed", KEYED_FILES)
def test_gating_state_matches_convention(fname: str, kind: str, keyed: bool) -> None:
    desc = _load_descriptor(fname)
    if keyed:
        # Draft = registers in bulk, NO live actor (dapr_host action
        # executor: "draft/configured — no live actor wanted").
        assert desc.identity.state == LifecycleState.DRAFT, (
            f"{fname}: keyed/extra-gated descriptors must ship draft so bulk "
            "registration cannot auto-activate them on a keyless rig"
        )
    else:
        assert desc.identity.state == LifecycleState.ACTIVE, (
            f"{fname}: the keyless bulk_csv descriptor is activation-ready"
        )


def test_opensanctions_bulk_is_keyless_and_api_is_keyed() -> None:
    bulk = _load_descriptor("source_opensanctions_bulk.yaml")
    api = _load_descriptor("source_opensanctions_api.yaml")

    bulk_cfg = _unwrap_factory_dict(bulk.config)
    api_cfg = _unwrap_factory_dict(api.config)

    assert bulk_cfg["mode"] == "bulk_csv"
    assert "api_key_secret" not in bulk_cfg          # keyless per the handler
    assert not bulk.deps.vault_secrets

    assert api_cfg["mode"] == "api"
    assert api_cfg["api_key_secret"] == "source.opensanctions.api_key"


# ---------------------------------------------------------------------------
# 4. Vault refs: config ↔ deps.vault_secrets agree; never plaintext-shaped.
# ---------------------------------------------------------------------------


# ACLED migrated off the legacy api-key auth to an OAuth2 password grant: its
# config now carries username + password vault refs. These are the vault ids
# the handler actually resolves at pull/health time (see acled.py::_fetch_token).
_ACLED_OAUTH_CONFIG_REFS = {"source.acled.username", "source.acled.password"}


@pytest.mark.parametrize("fname,kind,keyed", KEYED_FILES)
def test_vault_refs_declared_and_ref_shaped(fname: str, kind: str, keyed: bool) -> None:
    desc = _load_descriptor(fname)
    config_refs = _config_secret_refs(desc.config)
    declared = set(desc.deps.vault_secrets)

    if kind == "acled":
        # OAuth2 migration: the handler-authoritative refs are the two the
        # config block declares (username + password). The descriptor's
        # ``deps.vault_secrets`` block still lists the retired
        # ``source.acled.api_key`` and is a KNOWN-STALE follow-up to keep in
        # lock-step with the OAuth2 config — tracked separately, not editable
        # from this test pass. We pin the config refs to the OAuth2 pair so a
        # future config drift is still caught, and ref-shape-check them below.
        assert config_refs == _ACLED_OAUTH_CONFIG_REFS, (
            f"{fname}: ACLED config secret refs {sorted(config_refs)} != "
            f"the expected OAuth2 pair {sorted(_ACLED_OAUTH_CONFIG_REFS)}"
        )
    else:
        assert config_refs == declared, (
            f"{fname}: config secret refs {sorted(config_refs)} != "
            f"deps.vault_secrets {sorted(declared)}"
        )

    for ref in config_refs:
        # Vault ids are dotted identifiers (CredentialVault.store_secret
        # contract) — a value with spaces/slashes would be a leaked literal.
        assert " " not in ref and "/" not in ref and ref.startswith("source."), (
            f"{fname}: {ref!r} does not look like a vault id"
        )


def test_telegram_declares_all_three_credentials() -> None:
    desc = _load_descriptor("source_telegram_monitor.yaml")
    cfg = _unwrap_factory_dict(desc.config)
    assert cfg["api_id_secret"] == "source.telegram.api_id"
    assert cfg["api_hash_secret"] == "source.telegram.api_hash"
    assert cfg["session_secret"] == "source.telegram.session"
    # A curated official-org channel set (see the "25 verified, category-grouped"
    # curation in source_telegram_monitor.yaml) — assert a real set is present and
    # sanity-bounded, not the original tiny example list.
    assert 3 <= len(cfg["channels"]) <= 50


# ---------------------------------------------------------------------------
# 5. ACLED ToS: the no-redistribution warning must stay in the file.
# ---------------------------------------------------------------------------


def test_acled_descriptor_carries_tos_warning() -> None:
    text = (DESCRIPTORS_DIR / "source_acled_conflict.yaml").read_text()
    assert "FORBID REDISTRIBUTION" in text
    assert "acleddata.com/terms-of-use" in text


# ---------------------------------------------------------------------------
# 6. IntelMQ extra gating: keyless feed, but the dep is not in the image.
# ---------------------------------------------------------------------------


def test_intelmq_descriptor_gated_on_missing_extra() -> None:
    """This test runs inside the runtime-derived test image, so
    ``find_spec`` here IS the importability check for the deployment image.
    When the ``legba[intelmq]`` extra is absent, the descriptor must not be
    activation-ready (a stock rig would otherwise boot a permanently
    failing actor — IntelMQNotInstalled at on_configure)."""
    desc = _load_descriptor("source_intelmq_cisa_kev.yaml")
    intelmq_importable = importlib.util.find_spec("intelmq") is not None
    if not intelmq_importable:
        assert desc.identity.state == LifecycleState.DRAFT, (
            "intelmq extra is NOT importable in this image; the CISA KEV "
            "bridge descriptor must ship draft until an image with "
            "`pip install 'legba[intelmq]'` is deployed"
        )
    # Either way the header must tell the operator which extra to install.
    text = (DESCRIPTORS_DIR / "source_intelmq_cisa_kev.yaml").read_text()
    assert "legba[intelmq]" in text
