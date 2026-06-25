# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Example conversion webhooks (L-112).

Each module here defines one `convert(body: dict) -> dict` callable that
upgrades a descriptor body from one schema major version to the next.
Webhooks are referenced from `conversion_webhooks` rows by dotted-path
impl strings, e.g.:

    impl = "legba.data.conversions.target_v1_to_v2:convert"

These examples exist to exercise the framework end-to-end + document the
contract. Real conversion webhooks land alongside actual schema
major-version bumps (none in this repo yet — the vendored L-101 schemas
all live at `M.0.0`).

Webhook contract (L-101 §7, L-112):
  * Pure transformation: `convert(body: dict) -> dict`.
  * Sync or async — the executor awaits if the return is a coroutine.
  * Forward-only: never lose data silently. If a field is dropped in
    the upgraded shape, omit it from the returned dict and the executor
    will detect + archive it to `descriptor_conversion_archives`.
  * Idempotent on already-converted bodies is *not* required — the
    framework only calls a webhook for an input whose `schema_uri` (or
    equivalent positional context from the walk) matches its `from_uri`.
"""
