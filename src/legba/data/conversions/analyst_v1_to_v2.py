# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Example conversion: `legba/analyst/1.0.0` → `legba/analyst/2.0.0`.

Demonstration of the analyst-side webhook. Pretend v2 changes:
  * `identity.schema_uri` bumps.
  * `method.timeout` (v1, seconds, named without a unit) is renamed to
    `method.timeout_seconds` (v2, explicit unit) — no value change.
  * `legacy_prompt_template` (v1 free-text field) is dropped, archived.

The executor handles the archival side; this function just returns the
upgraded body.
"""

from __future__ import annotations

from typing import Any


def convert(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == "legacy_prompt_template":
            continue
        if key == "identity" and isinstance(value, dict):
            identity = dict(value)
            identity["schema_uri"] = "legba/analyst/2.0.0"
            out[key] = identity
            continue
        if key == "method" and isinstance(value, dict):
            method = dict(value)
            if "timeout" in method and "timeout_seconds" not in method:
                method["timeout_seconds"] = method.pop("timeout")
            out[key] = method
            continue
        out[key] = value
    return out


async def convert_async(body: dict[str, Any]) -> dict[str, Any]:
    """Async variant — same logic. Used by tests to exercise the async
    code path in `ConversionExecutor`. Production webhooks pick sync or
    async per the impl's external dependencies (DB lookup, HTTP call, etc.)."""
    return convert(body)
