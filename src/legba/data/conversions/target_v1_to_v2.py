# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Example conversion: `legba/target/1.0.0` → `legba/target/2.0.0`.

Demonstration of the conversion-webhook contract per L-101 §7 + L-112.
This file is a stand-in — there is no live `legba/target/1.0.0` schema
in the package; the vendored L-101 shape ships at `2.0.0`. Real upgrade
webhooks land when an actual major version bumps.

Pretend v1 → v2 changes:
  * `identity.schema_uri` bumps to `legba/target/2.0.0`
  * Field `scope.region_codes` (v1, deprecated synonym) is renamed
    to `scope.geo` (v2, canonical).
  * Field `scope.lang` (singular, v1) becomes `scope.languages` (plural,
    v2).
  * Field `legacy_owner_email` is dropped — archived by the framework.

Apart from those mutations the body passes through untouched. The
executor handles the field-dropping archival automatically (any top-level
key in the input absent from the output gets recorded in
`descriptor_conversion_archives`).
"""

from __future__ import annotations

from typing import Any


def convert(body: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a v1 target body to the v2 shape."""
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == "legacy_owner_email":
            # Dropped in v2 — let the executor archive it.
            continue
        if key == "identity" and isinstance(value, dict):
            identity = dict(value)
            identity["schema_uri"] = "legba/target/2.0.0"
            out[key] = identity
            continue
        if key == "scope" and isinstance(value, dict):
            scope = dict(value)
            if "region_codes" in scope and "geo" not in scope:
                scope["geo"] = scope.pop("region_codes")
            if "lang" in scope and "languages" not in scope:
                lang = scope.pop("lang")
                # v1 was a single string; v2 wants a list.
                scope["languages"] = [lang] if isinstance(lang, str) else list(lang)
            out[key] = scope
            continue
        out[key] = value
    return out
