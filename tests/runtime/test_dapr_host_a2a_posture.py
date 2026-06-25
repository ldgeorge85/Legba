# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-2 — default posture of the production A2A skill mount.

`legba.runtime.dapr_host.main()` used to mount the A2A skill surface
unconditionally with ``trusted_keys=None`` ("accept all signed envelopes in
DEV mode") — open GET endpoints plus accept-all on any
``auth_required=False`` skill. B-2 replaces that with
:func:`legba.runtime.dapr_host.resolve_a2a_mount`, which main() consults
before mounting:

  * default (``LEGBA_A2A_ENABLED`` unset) → ``None`` → surface UNMOUNTED;
  * enabled without an allowlist and without ``LEGBA_DEV_MODE=1`` →
    RuntimeError (refuse activation, fail loud);
  * enabled + ``LEGBA_A2A_TRUSTED_KEYS`` → directory of caller DIDs;
  * enabled + empty allowlist + explicit dev flag → empty directory
    (auth-required skills still reject everyone).
"""

from __future__ import annotations

import pytest
from nacl.signing import SigningKey

from legba.runtime.dapr_host import (
    A2A_ENABLED_ENV,
    A2A_TRUSTED_KEYS_ENV,
    resolve_a2a_mount,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(A2A_ENABLED_ENV, raising=False)
    monkeypatch.delenv(A2A_TRUSTED_KEYS_ENV, raising=False)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)


def test_default_posture_is_unmounted():
    assert resolve_a2a_mount() is None


def test_enabled_flag_other_values_stay_unmounted(monkeypatch):
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv(A2A_ENABLED_ENV, value)
        assert resolve_a2a_mount() is None, value


def test_enabled_without_allowlist_refuses_activation(monkeypatch):
    monkeypatch.setenv(A2A_ENABLED_ENV, "1")
    with pytest.raises(RuntimeError, match="fail-closed"):
        resolve_a2a_mount()


def test_enabled_with_allowlist_returns_directory(monkeypatch):
    verify_hex = SigningKey.generate().verify_key.encode().hex()
    monkeypatch.setenv(A2A_ENABLED_ENV, "1")
    monkeypatch.setenv(
        A2A_TRUSTED_KEYS_ENV, f"did:legba:peer-b2={verify_hex}",
    )
    directory = resolve_a2a_mount()
    assert directory is not None
    assert directory.get("did:legba:peer-b2") is not None
    assert directory.get("did:legba:unknown") is None


def test_enabled_empty_allowlist_with_dev_flag_returns_empty_directory(
    monkeypatch,
):
    monkeypatch.setenv(A2A_ENABLED_ENV, "1")
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")
    directory = resolve_a2a_mount()
    assert directory is not None
    assert directory.keys == {}
