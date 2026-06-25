# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Example conversion: `legba/target/2.0.0` → `legba/target/3.0.0`.

Second-link example so multi-step path-finding (v1 → v2 → v3) can be
exercised by the test suite. Pretend v3 changes:
  * `identity.schema_uri` bumps to `legba/target/3.0.0`
  * `scope.time_horizon_days` is renamed to `scope.horizon_days`.
  * Top-level field `deprecated_metadata_blob` is dropped — archived.
"""

from __future__ import annotations

from typing import Any


def convert(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == "deprecated_metadata_blob":
            continue
        if key == "identity" and isinstance(value, dict):
            identity = dict(value)
            identity["schema_uri"] = "legba/target/3.0.0"
            out[key] = identity
            continue
        if key == "scope" and isinstance(value, dict):
            scope = dict(value)
            if "time_horizon_days" in scope and "horizon_days" not in scope:
                scope["horizon_days"] = scope.pop("time_horizon_days")
            out[key] = scope
            continue
        out[key] = value
    return out
