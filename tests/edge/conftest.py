# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures for the edge (reader-transport) tests.

Re-exports the migrated test-DB fixtures from tests/data_pkg/conftest.py — the
same idiom tests/runtime/conftest.py uses — so an edge test reads and writes
the shared migrated test database without copying the bring-up code.
"""

from __future__ import annotations

from tests.data_pkg.conftest import (  # noqa: F401
    migrated_pg,
    substrate_up,
    test_pg_config,
)
